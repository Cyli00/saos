"""调整轮次的记录与停机判断（DESIGN.md 流程第 4 步；比较规则见 docs/optical-spec-and-acceptance.md 4.2 节）。

每个设计任务一份 rounds.jsonl：第 0 轮是初始结构的验收评估，之后每轮一条。
"""

from __future__ import annotations

import json
import math
from typing import Any

from saos.protocol import ErrorCode, SaosError

# 停机参数为初始值，未校准：第 5 步正式批次的调参组样本全部 1–2 轮通过，无法区分候选（docs/step5-calibration.md）
STAGNATION_ROUNDS = 3
STAGNATION_REL = 0.01
MAX_ROUNDS = 20


def significant(g_best: float, g_k: float, rel: float = STAGNATION_REL) -> bool:
    """g_k 相对此前最好值 g_best 是否明显缩小（至少 rel）。"""
    if math.isinf(g_k):
        return False
    if math.isinf(g_best):
        return True
    return (g_best - g_k) / g_best >= rel


def decide(history: list[float], n: int = STAGNATION_ROUNDS, rel: float = STAGNATION_REL, cap: int = MAX_ROUNDS) -> dict[str, Any]:
    """history 是第 0 轮起各轮的规格差距 G。返回当前状态：pass / stagnated / limit / continue。

    n、rel、cap 只在离线回放比较停机参数时改，实际任务用模块常量。"""
    if not history:
        raise ValueError("至少需要第 0 轮")
    g_best = history[0]
    flat = 0
    for g in history[1:]:
        if g == 0:
            break
        flat = 0 if significant(g_best, g, rel) else flat + 1
        g_best = min(g_best, g)
    rounds = len(history) - 1
    current = history[-1]
    if current == 0:
        status = "pass"
    elif flat >= n:
        status = "stagnated"
    elif rounds >= cap:
        status = "limit"
    else:
        status = "continue"
    return {"status": status, "rounds": rounds, "g_best": min(history), "rounds_without_progress": flat}


def freeze(job, spec_name: str, config: dict[str, Any], digest: str) -> None:
    """第 0 轮：保存完整验收配置，并在 job.json 记录规格路径和配置哈希。"""
    path = job.spec_dir / "frozen_config.json"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    job.update_state(spec=spec_name, frozen={"spec": spec_name, "config": "spec/frozen_config.json", "sha256": digest})


def check_frozen(job, spec_name: str, config: dict[str, Any], digest: str) -> None:
    """之后各轮：规格路径和补全后的验收配置都必须与第 0 轮一致。"""
    frozen = job.read_state().get("frozen")
    if not frozen:
        raise SaosError(ErrorCode.SPEC_CHANGED, "任务已有调整轮次，但没有冻结的验收配置；请新建任务")
    if spec_name != frozen["spec"]:
        raise SaosError(ErrorCode.SPEC_CHANGED, f"光学规格已冻结为 {frozen['spec']}，不能更换为 {spec_name}", {"frozen": frozen})
    if digest != frozen["sha256"]:
        old = json.loads((job.root / frozen["config"]).read_text(encoding="utf-8"))
        changed = sorted(k for k in set(old) | set(config) if old.get(k) != config.get(k))
        raise SaosError(
            ErrorCode.SPEC_CHANGED,
            "补全后的验收配置与第 0 轮冻结的不一致，规格差距不再可比；请新建任务（或派生任务）",
            {"changed_keys": changed, "frozen_sha256": frozen["sha256"], "current_sha256": digest},
        )


def load(path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
