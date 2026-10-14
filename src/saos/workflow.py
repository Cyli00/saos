"""通用工作流：按需求框定取值判定（check），记录调试轮次并给出终止判断（round）。
Generic workflow: measure and judge the harness (check), record rounds and return the stop decision (round).

    saos check --job J                 # 当前镜头逐项取值判定，不记录 / judge the current lens, no record
    saos round --job J --tier local    # 存为 lenses/rounds/rNNN.zmx，取值判定，写 ledger.jsonl，返回终止判断
                                       # save lenses/rounds/rNNN.zmx, judge, append ledger.jsonl, return the decision

记录轮次有两道关卡 / Two gates before any round is recorded:
1. 需求框定已由用户确认（confirmed.quote）/ the harness carries the user's approval (confirmed.quote);
2. 当前镜头的结构与某份已确认的建模说明一致（saos.model_gate）/ the lens matches an approved model description.
第 0 轮记录初始状态（tier 为 baseline）。/ Round 0 records the starting state (tier "baseline").
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from saos import harness, jobs, ledger, model_gate
from saos.harness import TIERS
from saos.protocol import ErrorCode, SaosError, jsonable


def load_harness(ctx) -> dict[str, Any]:
    path = ctx.path(harness.HARNESS_FILE)
    if not path.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"job {ctx.job.id} has no {harness.HARNESS_FILE}; frame the requirements with the user first")
    return harness.load(path)


def check(ctx) -> dict[str, Any]:
    if ctx.lens is None:
        raise SaosError(ErrorCode.NO_LENS_LOADED, f"job {ctx.job.id} has no lens loaded")
    before, sha = lens_state(ctx)
    result = {**ctx.describe(), **harness.evaluate(ctx, load_harness(ctx))}
    after, sha_after = lens_state(ctx)
    if sha_after != sha:
        # 只取值时也提示：同样的判定在记录轮次时会被拒绝 / warn here too: the same checks would be refused when recording a round
        result["lens_modified_by_checks"] = model_gate.diff(jsonable(before), jsonable(after))[:30]
    return result


def _confirmation(h: dict[str, Any]) -> str:
    confirmed = h.get("confirmed") or {}
    quote = str(confirmed.get("quote", "")).strip() if isinstance(confirmed, dict) else ""
    if not quote:
        raise SaosError(ErrorCode.BAD_ARGS, "the harness is not confirmed: add confirmed.quote with the user's own words of approval before recording rounds")
    return hashlib.sha256(quote.encode("utf-8")).hexdigest()[:16]


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


def record_round(ctx, tier: str | None = None, note: str = "") -> dict[str, Any]:
    if ctx.lens is None:
        raise SaosError(ErrorCode.NO_LENS_LOADED, f"job {ctx.job.id} has no lens loaded")
    h = load_harness(ctx)
    confirmation = _confirmation(h)
    # 结构核对只读数据、不画布局图 / the structure check reads data only, no layout pictures
    desc, lens_sha = lens_state(ctx)
    approved = model_gate.require_approved(ctx.job, desc)
    sha = harness.digest(h)
    scripts = harness.prepare_scripts(ctx.job.root, h)
    method_sha = harness.method_digest(scripts)
    path = ctx.path(ledger.LEDGER_FILE)
    history = ledger.load(path)
    number = len(history)
    if number == 0:
        tier = "baseline"
    else:
        if tier not in h["stop"]["tiers"]:
            raise SaosError(ErrorCode.BAD_ARGS, f"tier must be one of {h['stop']['tiers']} (ladder: {', '.join(TIERS)})")
        last = history[-1]
        if sha != last.get("harness_sha") and confirmation == last.get("confirmation"):
            raise SaosError(ErrorCode.SPEC_CHANGED, "the harness changed without a new user confirmation; the shortfall would no longer be comparable")
        if (sha, method_sha) == ledger.version(last):
            status = ledger.decide(history, h["stop"])["status"]
            if status != "continue":
                raise SaosError(ErrorCode.BAD_ARGS, f"rounds already stopped ({status}); change the harness with the user to continue", {"status": status})
            if last.get("lens_sha") == lens_sha:
                # 同一镜头、同一需求和判定版本再记一次没有意义；多半是超时后的重试，返回已有的那一轮
                # the same lens under the same version again: most likely a retry after a timeout; point to the existing round
                raise SaosError(ErrorCode.DUPLICATE_ROUND, f"round {last['round']} already recorded this lens under the same harness and checks",
                                {"round": last["round"], "file": last.get("file"), "evaluation": last.get("evaluation")})
    try:
        merit = float(ctx.backend.merit_value())
    except Exception:  # noqa: BLE001 - 没有评价函数或后端不支持时只是不记录 / no merit function: just not recorded
        merit = None
    evaluation = harness.evaluate(ctx, h, scripts)
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
        "values": {i["id"]: i["value"] for i in evaluation["items"]},
        "failing": {i["id"]: i["shortfall"] for i in evaluation["items"] if i.get("shortfall", 0) > 0},
        "merit": merit,
        "harness_sha": sha,
        "method_sha": method_sha,
        "confirmation": confirmation,
        "model": approved["model"],
        # 所用材料逐轮留档；含模型玻璃的轮次只是探索，不能交付
        # materials are archived per round; a round with a model glass is exploration only and cannot be delivered
        "materials": model_gate.materials(desc),
        "model_glass": model_gate.model_glass_surfaces(desc),
        "time": jobs.now_iso(),
    }
    ctx.job.seal(archived)
    # 写进 ledger 是本轮完成的标志；之前任何一步失败都不留下轮次
    # appending to the ledger is what completes a round; a failure before this leaves no round behind
    ledger.append(path, record)
    decision = ledger.decide(history + [record], h["stop"])
    ctx.job.update_state(round=number, round_status=decision["status"])
    return {"record": record, "decision": decision, "evaluation": evaluation}
