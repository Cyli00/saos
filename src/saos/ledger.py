"""调试轮次记录与终止判断（DESIGN.md「终止规则」）。

每个设计任务一份 ledger.jsonl：第 0 轮是初始状态（tier 为 baseline），之后每轮一条，记录本轮使用的
手段层级、各目标的实测值和目标差距 G。

终止判断的依据（参考李晓彤《几何光学·像差·光学设计》第 17、18 章）：阻尼最小二乘只收敛到局部极小；
变量要分批放开；Zemax 的全局搜索和 Hammer 优化能跳出局部极小，但会一直运行到人为中断。所以"几轮没进步"
不足以说明无法再优化，还要看是否已经逐级升级过手段。层级从低到高：

    local      在现有变量下做局部优化（阻尼最小二乘）
    variables  放开更多变量（厚度、空气间隔、二次曲面等）或调整评价函数构成与权重
    materials  换玻璃、让材料参与优化
    global     全局搜索或 Hammer 优化
    structure  改结构：增减镜片、拆分、非球面化

判定（按顺序）：
    achieved   本轮所有约束和性能指标都达标
    limit      轮次达到 max_rounds
    exhausted  自最后一次明显进步（G 比此前最好值至少再小 rel）以来，需求框定允许的每个层级都至少试过一轮，
               仍没有明显进步
    continue   其他情况；同时给出最后一次进步以来还没试过的层级，按从低到高排列，作为下一轮的建议
需求框定的内容一变（哈希不同），G 的定义就变了，进步从改动后的第一轮重新算起。
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


def decide(rounds: list[dict[str, Any]], stop: dict[str, Any]) -> dict[str, Any]:
    """rounds 为 ledger 中的全部轮次（含第 0 轮）。返回状态、理由和建议的下一层级。"""
    if not rounds:
        raise ValueError("ledger has no rounds")
    rel, cap, tiers = float(stop["rel"]), int(stop["max_rounds"]), [t for t in TIERS if t in stop["tiers"]]
    # 需求框定最后一次改动之后的轮次才可比
    start = max((k for k, r in enumerate(rounds) if k == 0 or r.get("harness_sha") != rounds[k - 1].get("harness_sha")), default=0)
    window = rounds[start:]
    best, last_gain = window[0]["shortfall"], 0
    for k, r in enumerate(window[1:], start=1):
        if significant(best, r["shortfall"], rel):
            last_gain = k
        best = min(best, r["shortfall"])
    since = window[last_gain + 1 :]
    tried = {r.get("tier") for r in since}
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
    }
    if current.get("achieved"):
        return {**base, "status": "achieved", "reason": "all constraints and performance targets are met"}
    if number >= cap:
        return {**base, "status": "limit", "reason": f"reached max_rounds = {cap}"}
    if not untried:
        return {
            **base,
            "status": "exhausted",
            "reason": f"no gain of at least {rel:.0%} since round {base['last_gain_round']} after trying every allowed tier ({', '.join(tiers)})",
        }
    return {**base, "status": "continue", "reason": f"next escalate to {untried[0]} if the current tier stops gaining", "next_tier": untried[0]}
