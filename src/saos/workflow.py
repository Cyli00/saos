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
import time
from pathlib import Path
from typing import Any

from saos import demand, jobs, ledger, model_gate
from saos.demand import TIERS
from saos.protocol import ErrorCode, SaosError, jsonable, write_json

# 连续这么多轮没有明显进步，就提醒先审视需求和结构，而不是继续加码
# after this many rounds without a clear gain, remind the agent to question the demand and structure before escalating
REVIEW_AFTER = 2
# 一项连续这么多轮没有有效值、或硬条件这么多轮都没满足，就提出质疑 / raise a doubt after this many rounds without a valid value or a met hard limit
DOUBT_AFTER = 2


def check(ctx, only: list[str] | None = None) -> dict[str, Any]:
    if ctx.lens is None:
        raise SaosError(ErrorCode.NO_LENS_LOADED, f"job {ctx.job.id} has no lens loaded")
    before, sha = lens_state(ctx)
    result = {**ctx.describe(), **demand.evaluate(ctx, demand.load_job(ctx.job.root), only=only)}
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


def doubts(d: dict[str, Any], window: list[dict[str, Any]]) -> list[str]:
    """由证据触发的质疑：一项连续几轮没有有效值、硬条件在本版本中从未满足、未达标的界限只是 agent 的假设。
    Doubts raised by evidence: an item without a valid value for several rounds, a hard limit never met in this
    version, a failing limit that rests only on an agent assumption."""
    valid = [r for r in window if ledger.valid(r)]
    if not valid:
        return []
    out = []
    current = valid[-1]
    weak = {w["id"]: w for w in demand.weak_basis(d)}
    for item in demand.checked(d):
        k = item["id"]
        run = 0
        for r in reversed(valid):
            v = (r.get("values") or {}).get(k)
            if isinstance(v, (int, float)) and math.isfinite(v):
                break
            run += 1
        if run >= DOUBT_AFTER:
            out.append(f"{k} has had no valid value for {run} rounds: recheck its preconditions and the structure, "
                       "or whether the demand asks for something this setup cannot give")
        if item["kind"] in demand.HARD_KINDS and len(valid) >= DOUBT_AFTER and all(k in (r.get("hard_failing") or []) for r in valid):
            out.append(f"{k} has not been met in any of the {len(valid)} rounds of this demand version: is the limit right (source: {_source(d, k)})?")
        if k in (current.get("failing") or {}) and k in weak:
            out.append(f"{k} fails and its limit rests on {weak[k]['source'] or 'no source'}: confirm the limit with the user before spending more compute")
    return out


def _source(d: dict[str, Any], item_id: str) -> str:
    entry = next((e for s in demand.SECTIONS for e in d.get(s) or [] if e["id"] == item_id), {})
    return str(entry.get("source") or "none")


def brief(d: dict[str, Any], evaluation: dict[str, Any], decision: dict[str, Any], window: list[dict[str, Any]] | None = None) -> dict[str, Any]:
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
    found = doubts(d, window or [])
    out = {"objectives": objectives, "hard_failing": evaluation.get("hard_failing", []),
           "open_questions": [q["question"] for q in d.get("open_questions") or []], "review": review, "doubts": found}
    if found:
        # 只在有证据时提醒质疑需求；给 agent 的文字一律英文 / only with evidence; text for the agent is always English
        out["reminder"] = demand.QUESTION_LINE["en"]
    return out


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


VIZ_FILE = "viz.json"
REBASE_PURPOSE = "rebase"


def export_viz(ctx, evidence_dir: str) -> dict[str, Any]:
    """在本轮证据目录里写 viz.json（saos ui 的点列图、布局和 MTF 数据），OpticStudio 的文本导出放在 viz/。
    轮次已经写进 ledger 之后才做：失败只记在 viz.json 和返回值里，不影响本轮。
    Write viz.json (spot, layout and MTF data for saos ui) into the round's evidence folder, with OpticStudio's text
    exports in viz/. Runs after the round is in the ledger: a failure is noted, never undoes the round."""
    folder = ctx.job.root / evidence_dir
    started = time.monotonic()
    try:
        data = {"ok": True, **ctx.backend.viz_data(folder / "viz")}
    except Exception as exc:  # noqa: BLE001 - 界面数据缺失不影响轮次
        data = {"ok": False, "error": f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"}
    data["elapsed_s"] = round(time.monotonic() - started, 2)
    write_json(folder / VIZ_FILE, data)
    ctx.job.seal(folder / VIZ_FILE, *sorted((folder / "viz").glob("*")) if (folder / "viz").is_dir() else ())
    return {"file": f"{evidence_dir}/{VIZ_FILE}", "ok": data["ok"], "elapsed_s": data["elapsed_s"],
            "errors": data.get("errors") or ({"viz": data["error"]} if not data["ok"] else {})}


def record_round(ctx, tier: str | None = None, note: str = "", merit: float | None = None, viz: bool = True) -> dict[str, Any]:
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
    ruled = ledger.ruled_out_for(ctx.job.root, sha)
    best = None
    if number == 0:
        tiers = ["baseline"]
    else:
        # 一轮可以同时用几个层级，例如 "variables,materials" / a round may use several tiers, e.g. "variables,materials"
        tiers = list(dict.fromkeys(t.strip() for t in str(tier or "").split(",") if t.strip()))
        if not tiers or any(t not in h["stop"]["tiers"] for t in tiers):
            raise SaosError(ErrorCode.BAD_ARGS, f"tier must be one or more (comma-separated) of {h['stop']['tiers']} (ladder: {', '.join(TIERS)})")
        last = history[-1]
        status = ledger.decide(history, h["stop"], ledger.stop_for(ctx.job.root, sha), ruled)["status"]
        if ledger.version(last) == (sha, method_sha):
            if status != "continue":
                raise SaosError(ErrorCode.BAD_ARGS, f"rounds already stopped ({status}); review the demand with the user to continue", {"status": status})
            if last.get("lens_sha") == lens_sha:
                # 同一镜头、同一需求和判定版本再记一次没有意义；多半是超时后的重试，返回已有的那一轮
                # the same lens under the same version again: most likely a retry after a timeout; point to the existing round
                raise SaosError(ErrorCode.DUPLICATE_ROUND, f"round {last['round']} already recorded this lens under the same demand and checks",
                                {"round": last["round"], "file": last.get("file"), "evaluation": last.get("evaluation")})
        elif ledger.version(last)[0] == sha:
            # 同一需求下判定代码改了：先用新判定重测此前最好的一轮，之后的比较都对着它
            # the check code changed under the same demand: re-measure the best earlier round first and compare with that
            window = [r for r in ledger.current_window(history) if ledger.valid(r) and r.get("file")]
            best = min(window, key=lambda r: r["shortfall"]) if window else None
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
    rebase_number, number = (number, number + 1) if best is not None else (None, number)
    _set_aside(ctx, ctx.job.resolve_lens(f"rounds/r{number:03d}.zmx", must_exist=False))
    rel = ctx.save_as(f"rounds/r{number:03d}.zmx", archive=True)
    archived = ctx.path(f"lenses/{rel}")
    if working is not None and not ctx.job.is_archive_lens(working):
        ctx.save_as(ctx.job.relative_lens(working), overwrite=True)
    rebase = None
    if best is not None:
        rebase = _rebase(ctx, h, scripts, best, rebase_number, sha, method_sha, confirmation)
        # 旧记录没有判定版本，不算同一版本内的终止 / records from before check versions existed never stopped this version
        if rebase["equivalent"] and status != "continue" and best.get("method_sha"):
            raise SaosError(ErrorCode.BAD_ARGS, f"rounds already stopped ({status}) and the new checks give the same numbers; review the demand with the user to continue",
                            {"status": status, "evaluation": rebase["evaluation"]})
    record = {
        "round": number,
        # tier 留第一个层级，兼容旧记录；tiers 是本轮用到的全部层级 / tier keeps the first for older readers; tiers lists them all
        "tier": tiers[0],
        "tiers": tiers,
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
    added = [rebase, record] if rebase is not None else [record]
    for row in added:
        ledger.append(path, row)
    decision = ledger.decide(history + added, h["stop"], ledger.stop_for(ctx.job.root, sha), ruled)
    ctx.job.update_state(round=number, round_status=decision["status"])
    out = {"record": record, "decision": decision, "brief": brief(h, evaluation, decision, ledger.current_window(history + added)),
           "evaluation": evaluation}
    if rebase is not None:
        out["rebase"] = {k: rebase[k] for k in ("round", "rebase_of", "equivalent", "diffs", "shortfall", "evaluation")}
    if viz:
        out["viz"] = export_viz(ctx, evaluation["evidence_dir"])
    return out


# 判定代码改了以后，重测的数值与原记录在这个相对容差内一致，就算口径没变 / within this the new checks measure the same thing
EQUAL_RTOL = 1e-9


def _same(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    a, b = float(a), float(b)
    return a == b or abs(a - b) <= EQUAL_RTOL * max(abs(a), abs(b))


def _rebase(ctx, h: dict[str, Any], scripts: dict[str, Any], best: dict[str, Any], number: int, sha: str, method_sha: str,
            confirmation: str) -> dict[str, Any]:
    """用新判定重测此前最好一轮的归档镜头，返回要写进 ledger 的记录；之后重新加载当前镜头（调用前已保存）。
    Re-measure the archived lens of the best earlier round with the new checks and return its ledger record; the current
    lens (saved before this call) is reloaded afterwards."""
    current = ctx.lens
    try:
        ctx.job.write_progress(stage=f"rebase r{int(best['round']):03d}")
        ctx.backend.load(ctx.path(f"lenses/{best['file']}"))
        _, before = lens_state(ctx)
        ev = demand.evaluate(ctx, h, scripts, lens={"file": best["file"], "round": best["round"], "purpose": REBASE_PURPOSE})
        _, after = lens_state(ctx)
    finally:
        ctx.backend.load(current)
    if after != before:
        raise SaosError(ErrorCode.LENS_MODIFIED_BY_CHECK, f"a check changed the lens of round {best['round']} while re-measuring it; make the checks read-only",
                        {"evaluation": ev["evidence_dir"]})
    values = {i["id"]: i["value"] for i in ev["items"]}
    old = best.get("values") or {}
    diffs = {k: {"recorded": old.get(k), "now": values.get(k)} for k in sorted(set(old) | set(values)) if not _same(old.get(k), values.get(k))}
    return {
        "round": number, "tier": ledger.REBASE, "tiers": [ledger.REBASE], "note": f"r{int(best['round']):03d} re-measured with the new checks",
        "file": best["file"], "lens_sha": before, "rebase_of": best["round"],
        # 数值不变说明只是换了写法，比较窗口延续；否则从这条开始新窗口 / same numbers: the window goes on; otherwise a new one starts here
        "equivalent": not diffs and not ev["measurement_errors"], "diffs": diffs,
        "evaluation": ev["evidence_file"], "shortfall": ev["shortfall"], "achieved": ev["achieved"],
        "measurement_errors": ev["measurement_errors"], "hard_failing": ev["hard_failing"], "values": values,
        "failing": {i["id"]: i["shortfall"] for i in ev["items"] if i.get("shortfall", 0) > 0},
        "merit": best.get("merit"), "demand_sha": sha, "method_sha": method_sha, "confirmation": confirmation,
        **{k: best.get(k) for k in ("model", "materials", "model_glass", "environment")}, "time": jobs.now_iso(),
    }
