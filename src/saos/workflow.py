"""通用工作流：按需求取值判定（check），记录调试轮次并给出终止判断（round）。
Generic workflow: measure and judge the demand (check), record rounds and return the stop decision (round).

    saos check --job J                 # 当前镜头逐项取值判定，不记录 / judge the current lens, no record
    saos round --job J --tier local    # 存为 lenses/rounds/rNNN.zmx，取值判定，写 ledger.jsonl，返回终止判断
                                       # save lenses/rounds/rNNN.zmx, judge, append ledger.jsonl, return the decision

记录轮次和运行优化前的关卡 / Gates before a round is recorded or an optimisation runs:
1. 当前需求内容有用户的确认（saos demand confirm，绑定内容哈希）/ the current demand content carries the user's confirmation;
2. 需求里没有 blocking 的待确认问题 / the demand has no blocking open question;
3. （仅轮次）当前镜头的结构与某份已确认的建模说明一致（saos.model_gate）/ (rounds only) the lens matches an approved model description.
第 0 轮记录初始状态（tier 为 baseline）。/ Round 0 records the starting state (tier "baseline").
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from saos import demand, jobs, ledger, model_gate
from saos.demand import TIERS
from saos.protocol import ErrorCode, SaosError, jsonable

# 连续这么多轮没有明显进步，就提醒先审视需求和结构，而不是继续加码
# after this many rounds without a clear gain, remind the agent to question the demand and structure before escalating
REVIEW_AFTER = 2


def check(ctx) -> dict[str, Any]:
    if ctx.lens is None:
        raise SaosError(ErrorCode.NO_LENS_LOADED, f"job {ctx.job.id} has no lens loaded")
    before, sha = lens_state(ctx)
    result = {**ctx.describe(), **demand.evaluate(ctx, demand.load_job(ctx.job.root))}
    after, sha_after = lens_state(ctx)
    if sha_after != sha:
        # 只取值时也提示：同样的判定在记录轮次时会被拒绝 / warn here too: the same checks would be refused when recording a round
        result["lens_modified_by_checks"] = model_gate.diff(jsonable(before), jsonable(after))[:30]
    return result


def preflight(root: Path) -> tuple[dict[str, Any], str, str]:
    """耗时计算之前的统一检查：需求已确认、没有 blocking 问题。返回需求、内容哈希和确认原话的哈希。
    The common check before any expensive computation: the demand is confirmed and has no blocking question.
    Returns the demand, its content hash and a hash of the confirming words."""
    d = demand.load_job(root)
    sha = demand.digest(d)
    confirmed = demand.confirmation_for(root, sha)
    if confirmed is None:
        raise SaosError(ErrorCode.DEMAND_NOT_CONFIRMED, "this version of the demand has no user confirmation: show it to the user, then record their words with saos demand confirm",
                        {"demand_sha": sha})
    blocking = demand.blocking_questions(d)
    if blocking:
        raise SaosError(ErrorCode.DEMAND_BLOCKED, "the demand has blocking open questions; ask the user before spending compute", {"blocking_questions": blocking})
    return d, sha, hashlib.sha256(confirmed["quote"].encode("utf-8")).hexdigest()[:16]


def brief(d: dict[str, Any], evaluation: dict[str, Any], decision: dict[str, Any]) -> dict[str, Any]:
    """每轮返回的需求提要：目标现状、硬条件、待确认问题，以及该停下来审视时的提醒。
    The demand brief returned with every round: where the objectives stand, hard conditions, open questions,
    and a prompt to stop and review when the evidence says so."""
    objectives = [{k: i.get(k) for k in ("id", "origin", "weight", "value", "goal", "wish", "passed", "wish_met") if k in i}
                  for i in evaluation["items"] if i["kind"] == "objective"]
    stalled = decision.get("rounds_without_gain", 0) >= REVIEW_AFTER
    escalating = decision.get("next_tier") in ("global", "structure")
    review = None
    if decision["status"] in ("exhausted", "limit", "stopped") or stalled or escalating:
        why = (f"rounds stopped ({decision['status']})" if decision["status"] in ("exhausted", "limit", "stopped")
               else f"{decision['rounds_without_gain']} rounds without a clear gain" if stalled
               else f"next tier is {decision['next_tier']}")
        review = (f"{why}. Before spending more compute, reread the demand summary and your first-order analysis. If the demand, "
                  "the structure or a measurement looks wrong or unreachable, stop and ask the user, with the evidence, its impact and options.")
    lang = d.get("lang") or "zh"
    return {"objectives": objectives, "hard_failing": evaluation.get("hard_failing", []),
            "open_questions": [q["question"] for q in d.get("open_questions") or []],
            "review": review, "reminder": demand.QUESTION_LINE.get(lang, demand.QUESTION_LINE["zh"])}


def lens_state(ctx) -> tuple[dict[str, Any], str]:
    """当前镜头的完整数据和内容哈希（不含文件路径和布局图），用来发现被改动的镜头和重复记录。
    The full data of the current lens and a hash of its content (file path and pictures excluded),
    used to catch a lens changed by a check and a round recorded twice."""
    desc = ctx.backend.describe_model(None)
    body = jsonable({k: v for k, v in desc.items() if k not in ("file", "layout")})
    return desc, hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _set_aside(ctx, target) -> None:
    """上一次失败的尝试留下、ledger 里没有的归档镜头挪到 rounds/orphaned/，不覆盖。
    An archive left by a failed attempt (not in the ledger) is moved to rounds/orphaned/ instead of being overwritten."""
    if not target.exists():
        return
    folder = target.parent / "orphaned"
    folder.mkdir(exist_ok=True)
    stamp = jobs.now_iso().replace(":", "").replace("+", "_")
    for f in target.parent.glob(target.stem + ".*"):
        f.rename(folder / f"{f.stem}-{stamp}{f.suffix}")


def record_round(ctx, tier: str | None = None, note: str = "", merit: float | None = None) -> dict[str, Any]:
    if ctx.lens is None:
        raise SaosError(ErrorCode.NO_LENS_LOADED, f"job {ctx.job.id} has no lens loaded")
    h, sha, confirmation = preflight(ctx.job.root)
    # 结构核对只读数据、不画布局图 / the structure check reads data only, no layout pictures
    desc, lens_sha = lens_state(ctx)
    approved = model_gate.require_approved(ctx.job, desc)
    scripts = demand.prepare_scripts(ctx.job.root, h)
    method_sha = demand.method_digest(scripts)
    path = ctx.path(ledger.LEDGER_FILE)
    history = ledger.load(path)
    number = len(history)
    if number == 0:
        tier = "baseline"
    else:
        if tier not in h["stop"]["tiers"]:
            raise SaosError(ErrorCode.BAD_ARGS, f"tier must be one of {h['stop']['tiers']} (ladder: {', '.join(TIERS)})")
        last = history[-1]
        if (sha, method_sha) == ledger.version(last):
            status = ledger.decide(history, h["stop"], ledger.stop_for(ctx.job.root, sha))["status"]
            if status != "continue":
                raise SaosError(ErrorCode.BAD_ARGS, f"rounds already stopped ({status}); review the demand with the user to continue", {"status": status})
            if last.get("lens_sha") == lens_sha:
                # 同一镜头、同一需求和判定版本再记一次没有意义；多半是超时后的重试，返回已有的那一轮
                # the same lens under the same version again: most likely a retry after a timeout; point to the existing round
                raise SaosError(ErrorCode.DUPLICATE_ROUND, f"round {last['round']} already recorded this lens under the same demand and checks",
                                {"round": last["round"], "file": last.get("file"), "evaluation": last.get("evaluation")})
    merit_from_agent = merit is not None
    if merit is not None:
        # agent 在 OpticStudio 评价函数之外优化时，记录它实际优化的目标值
        # when the agent optimised outside the OpticStudio merit function, record the value it actually optimised
        if isinstance(merit, bool) or not isinstance(merit, (int, float)) or not math.isfinite(merit):
            raise SaosError(ErrorCode.BAD_ARGS, f"--merit must be a finite number (got {merit!r})")
        merit = float(merit)
    else:
        try:
            merit = float(ctx.backend.merit_value())
        except Exception:  # noqa: BLE001 - 没有评价函数或后端不支持时只是不记录 / no merit function: just not recorded
            merit = None
    evaluation = demand.evaluate(ctx, h, scripts)
    # 判定不能改动镜头：否则归档的镜头和测得的数值对不上，本轮不记录
    # checks must not change the lens; otherwise the archived lens would not be the one measured, so no round is recorded
    desc_after, sha_after = lens_state(ctx)
    if sha_after != lens_sha:
        changed = model_gate.diff(jsonable(desc), jsonable(desc_after))
        raise SaosError(ErrorCode.LENS_MODIFIED_BY_CHECK, "a check changed the lens while measuring; make the checks read-only (work on a copy or restore the lens), then record again",
                        {"changed": changed[:30], "evaluation": evaluation["evidence_dir"]})
    # 存一份本轮的归档镜头，然后回到原来的工作文件，避免之后的 lens save 改写归档
    # archive this round's lens, then switch back to the working file so a later lens save cannot rewrite the archive
    working = ctx.lens
    _set_aside(ctx, ctx.job.resolve_lens(f"rounds/r{number:03d}.zmx", must_exist=False))
    rel = ctx.save_as(f"rounds/r{number:03d}.zmx", archive=True)
    archived = ctx.path(f"lenses/{rel}")
    if working is not None and not ctx.job.is_archive_lens(working):
        ctx.save_as(ctx.job.relative_lens(working), overwrite=True)
    record = {
        "round": number,
        "tier": tier,
        "note": str(note),
        "file": rel,
        "lens_sha": lens_sha,
        # 完整判定只存一份：evaluations/NNN/result.json 是权威记录
        # the full judgement is stored once: evaluations/NNN/result.json is the authoritative record
        "evaluation": evaluation["evidence_file"],
        "shortfall": evaluation["shortfall"],
        "achieved": evaluation["achieved"],
        "measurement_errors": evaluation["measurement_errors"],
        # 常量或变量范围不满足的轮次不能交付 / a round that breaks a constant or a variable range is not deliverable
        "hard_failing": evaluation["hard_failing"],
        "values": {i["id"]: i["value"] for i in evaluation["items"]},
        "failing": {i["id"]: i["shortfall"] for i in evaluation["items"] if i.get("shortfall", 0) > 0},
        "merit": merit,
        **({"merit_source": "agent"} if merit_from_agent else {}),
        "demand_sha": sha,
        "method_sha": method_sha,
        "confirmation": confirmation,
        "model": approved["model"],
        # 所用材料逐轮留档；含模型玻璃的轮次只是探索，不能交付
        # materials are archived per round; a round with a model glass is exploration only and cannot be delivered
        "materials": model_gate.materials(desc),
        # 环境面和外购件（建模说明中声明）不受模型玻璃限制 / environment and bought-in surfaces (declared in the approved model) are exempt
        "model_glass": [i for i in model_gate.model_glass_surfaces(desc) if i not in approved["environment"]],
        "environment": approved["environment"],
        "time": jobs.now_iso(),
    }
    ctx.job.seal(archived)
    # 写进 ledger 是本轮完成的标志；之前任何一步失败都不留下轮次
    # appending to the ledger is what completes a round; a failure before this leaves no round behind
    ledger.append(path, record)
    decision = ledger.decide(history + [record], h["stop"], ledger.stop_for(ctx.job.root, sha))
    ctx.job.update_state(round=number, round_status=decision["status"])
    return {"record": record, "decision": decision, "brief": brief(h, evaluation, decision), "evaluation": evaluation}
