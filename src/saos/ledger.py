"""调试轮次记录与终止判断（DESIGN.md「终止规则」）。
Round ledger and stop decision (DESIGN.md, "终止规则" / stop rule).

每个设计任务一份 ledger.jsonl：第 0 轮是初始状态（tier 为 baseline），之后每轮一条，记录本轮使用的
手段层级、各目标的实测值和目标差距 G。
One ledger.jsonl per design job: round 0 is the starting state (tier "baseline"); every later round records
its strategy tier, the measured value of every target and the shortfall G.

终止判断的依据（参考李晓彤《几何光学·像差·光学设计》第 17、18 章）：阻尼最小二乘只收敛到局部极小；
变量要分批放开；全局搜索和 Hammer 优化能跳出局部极小，但会一直运行到人为中断。所以"几轮没进步"
不足以说明无法再优化，还要看是否已经逐级升级过手段。层级从低到高：
Why tiers (after Li Xiaotong, Geometrical Optics, Aberrations and Optical Design, ch. 17–18): damped least
squares only reaches a local minimum, variables are freed in batches, and global search / Hammer escape
local minima but run until stopped. "No gain for a few rounds" therefore says nothing unless the strategy
has also been escalated. Tiers, low to high:

    local      在现有变量下局部优化 / local optimisation on the current variables
    variables  放开更多变量或调整评价函数的构成与权重 / more variables, or a different merit construction
    materials  在有来源的真实材料之间替换 / substitute among sourced real materials
    global     全局搜索或 Hammer，也包括自写追迹的粗搜 / global search or Hammer, incl. an own coarse search
    structure  改结构 / change the structure

判定（按顺序）/ Decision, in this order:
    achieved   本轮所有约束和性能指标都达标，且不含模型玻璃
               every constraint and performance target is met and the lens has no model glass
    limit      当前需求框定版本下的轮次数达到 max_rounds / rounds of the current harness version reached max_rounds
    exhausted  自最后一次明显进步（G 比此前最好值至少再小 rel）以来，允许的每个层级都至少试过一轮，仍没有明显进步
               since the last significant gain (G at least rel below the best so far) every allowed tier was tried
    continue   其他情况；给出最后一次进步以来还没试过的最低层级 / otherwise; suggests the lowest untried tier
需求框定的内容一变（哈希不同），G 的定义就变了，进步从改动后的第一轮重新算起。
When the harness content changes (different hash), G means something else and progress is counted afresh.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from saos.harness import TIERS
from saos.protocol import jsonable

LEDGER_FILE = "ledger.jsonl"


def load(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for r in rows:
        r["shortfall"] = float(r["shortfall"])
    return rows


def append(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(jsonable(record), ensure_ascii=False) + "\n")


def significant(best: float, g: float, rel: float) -> bool:
    if not math.isfinite(g):
        return False
    if not math.isfinite(best):
        return True
    return best > 0 and (best - g) / best >= rel


def version(record: dict[str, Any]) -> tuple[Any, Any]:
    return record.get("harness_sha"), record.get("method_sha")


def current_window(rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    start = max((k for k, r in enumerate(rounds) if k == 0 or version(r) != version(rounds[k - 1])), default=0)
    return rounds[start:]


def valid(record: dict[str, Any]) -> bool:
    """有判定执行出错的轮次不算数：不计进步、不算试过某层级。
    A round whose checks failed to run counts for nothing: no progress, no tier tried."""
    return not record.get("measurement_errors")


def deliverable(record: dict[str, Any]) -> bool:
    """含模型玻璃或测量出错的轮次不能交付。/ Rounds with a model glass or failed checks are not deliverable."""
    return not record.get("model_glass") and valid(record)


def decide(rounds: list[dict[str, Any]], stop: dict[str, Any]) -> dict[str, Any]:
    """rounds 为 ledger 中的全部轮次（含第 0 轮）。返回状态、理由和建议的下一层级。
    rounds: every round in the ledger (round 0 included). Returns the status, the reason and the suggested next tier."""
    if not rounds:
        raise ValueError("ledger has no rounds")
    rel, cap, tiers = float(stop["rel"]), int(stop["max_rounds"]), [t for t in TIERS if t in stop["tiers"]]
    # 需求框定最后一次改动之后的轮次才可比 / only rounds since the last harness change are comparable
    window = current_window(rounds)
    best, last_gain = (window[0]["shortfall"] if valid(window[0]) else math.inf), 0
    for k, r in enumerate(window[1:], start=1):
        if not valid(r):
            continue
        if significant(best, r["shortfall"], rel):
            last_gain = k
        best = min(best, r["shortfall"])
    since = window[last_gain + 1 :]
    tried = {r.get("tier") for r in since if valid(r)}
    untried = [t for t in tiers if t not in tried]
    current = rounds[-1]
    number = int(current["round"])
    base = {
        "round": number,
        "best_shortfall": best,
        "last_gain_round": int(window[last_gain]["round"]),
        "rounds_without_gain": len(since),
        "tiers_tried_since_gain": sorted(tried & set(tiers), key=TIERS.index),
        "untried_tiers": untried,
        # 轮次预算按需求框定版本计：用户确认新版本后重新计数 / the round budget counts per harness version
        "rounds_in_version": len(window) - 1,
    }
    if current.get("achieved") and deliverable(current):
        return {**base, "status": "achieved", "reason": "all constraints and performance targets are met"}
    if len(window) - 1 >= cap:
        return {**base, "status": "limit", "reason": f"reached max_rounds = {cap} for this harness version"}
    if not valid(current):
        return {**base, "status": "continue", "next_tier": untried[0] if untried else None,
                "reason": f"checks failed to run ({', '.join(current['measurement_errors'])}); fix them and measure again; this round is not a design result"}
    if current.get("achieved"):
        # 指标达到了，但材料还不是真实材料：继续，换成有来源的材料
        # targets met with a model glass: keep going and replace it with a sourced material
        return {**base, "status": "continue", "next_tier": "materials",
                "reason": f"targets met, but surfaces {current['model_glass']} still use a model glass; replace it with a sourced material"}
    if not untried:
        return {
            **base,
            "status": "exhausted",
            "reason": f"no gain of at least {rel:.0%} since round {base['last_gain_round']} after trying every allowed tier ({', '.join(tiers)})",
        }
    return {**base, "status": "continue", "reason": f"next escalate to {untried[0]} if the current tier stops gaining", "next_tier": untried[0]}
