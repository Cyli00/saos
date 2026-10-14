"""通用工作流：按需求框定取值判定（check），记录调试轮次并给出终止判断（round）。

    saos check --job J                 # 当前镜头逐项取值判定，不记录
    saos round --job J --tier local    # 存为 lenses/rounds/rNNN.zmx，取值判定，写 ledger.jsonl，返回终止判断

第 0 轮记录初始状态（tier 为 baseline）。轮次只能在用户确认需求框定之后记录（DESIGN.md 三条底线）。
"""

from __future__ import annotations

import hashlib
from typing import Any

from saos import harness, jobs, ledger
from saos.harness import TIERS
from saos.protocol import ErrorCode, SaosError, write_json


def load_harness(ctx) -> dict[str, Any]:
    path = ctx.path(harness.HARNESS_FILE)
    if not path.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"job {ctx.job.id} has no {harness.HARNESS_FILE}; frame the requirements with the user first")
    return harness.load(path)


def check(ctx) -> dict[str, Any]:
    if ctx.lens is None:
        raise SaosError(ErrorCode.NO_LENS_LOADED, f"job {ctx.job.id} has no lens loaded")
    return {**ctx.describe(), **harness.evaluate(ctx, load_harness(ctx))}


def _confirmation(h: dict[str, Any]) -> str:
    confirmed = h.get("confirmed") or {}
    quote = str(confirmed.get("quote", "")).strip() if isinstance(confirmed, dict) else ""
    if not quote:
        raise SaosError(ErrorCode.BAD_ARGS, "the harness is not confirmed: add confirmed.quote with the user's own words of approval before recording rounds")
    return hashlib.sha256(quote.encode("utf-8")).hexdigest()[:16]


def record_round(ctx, tier: str | None = None, note: str = "") -> dict[str, Any]:
    if ctx.lens is None:
        raise SaosError(ErrorCode.NO_LENS_LOADED, f"job {ctx.job.id} has no lens loaded")
    h = load_harness(ctx)
    confirmation = _confirmation(h)
    sha = harness.digest(h)
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
        if sha == last.get("harness_sha"):
            status = ledger.decide(history, h["stop"])["status"]
            if status != "continue":
                raise SaosError(ErrorCode.BAD_ARGS, f"rounds already stopped ({status}); change the harness with the user to continue", {"status": status})
    try:
        merit = float(ctx.backend.merit_value())
    except Exception:  # noqa: BLE001 - 没有评价函数或后端不支持时只是不记录
        merit = None
    rel = ctx.save_as(f"rounds/r{number:03d}.zmx", overwrite=True)
    evaluation = harness.evaluate(ctx, h)
    record = {
        "round": number,
        "tier": tier,
        "note": str(note),
        "file": rel,
        "shortfall": evaluation["shortfall"],
        "achieved": evaluation["achieved"],
        "values": {i["id"]: i["value"] for i in evaluation["items"]},
        "failing": {i["id"]: i["shortfall"] for i in evaluation["items"] if i.get("shortfall", 0) > 0},
        "merit": merit,
        "harness_sha": sha,
        "confirmation": confirmation,
        "time": jobs.now_iso(),
    }
    ledger.append(path, record)
    # 每轮的完整判定（含取值依据）另存，交付报告据此逐项列出
    write_json(ctx.path(f"rounds/r{number:03d}.json"), {"round": number, **evaluation})
    decision = ledger.decide(history + [record], h["stop"])
    ctx.job.update_state(round=number, round_status=decision["status"])
    return {"record": record, "decision": decision, "evaluation": evaluation}
