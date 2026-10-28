"""非球面候选的线性投影排名。

按面分组，用 SVD 求残差在系数列空间上的投影，再按斜率预算缩放步长。
每轮扣除选中面的缩放投影，为剩余面重排；不重新求受约束最小二乘方向。
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import math
import numpy as np


@dataclass
class SurfaceSensitivity:
    surface: int
    jacobian: np.ndarray
    slope_factor: np.ndarray | None = None
    current_slope: np.ndarray | None = None
    budget: float | None = None


def usable_fraction(current_squared, predicted_squared, budget_squared):
    """用斜率平方和确定可用步长比例；已超限但斜率下降也允许完整步长。"""
    values = (current_squared, predicted_squared, budget_squared)
    if any(not math.isfinite(float(x)) or x < 0 for x in values):
        raise ValueError("squared slope norms and budget must be finite and nonnegative")
    if current_squared < budget_squared or predicted_squared < current_squared:
        if current_squared < predicted_squared:
            return math.sqrt(min(1., (budget_squared-current_squared)/(predicted_squared-current_squared)))
        return 1.
    return 0.


def _vector(x, name):
    value = np.asarray(x, dtype=float)
    if value.ndim != 1 or not np.isfinite(value).all():
        raise ValueError(f"{name} must be a finite vector")
    return value


def rank(residual, models, *, cumulative=True, rcond=1e-12):
    """J 为带权残差对实际系数的导数。分数是向量范数比，不是 merit 平方下降量。

    slope_factor 将系数增量映射到与 current_slope、budget 同单位的斜率向量。
    斜率向量可取经标定的系数度量或带面积权重的实际斜率样本。
    每轮只扣除所选面的缩放投影，不重新优化或改变其余面的列空间。
    """
    original = _vector(residual, "residual")
    if not original.size:
        raise ValueError("a residual vector must not be empty")
    if not 0 < rcond < 1:
        raise ValueError("rcond must be between zero and one")
    remaining = [replace(m) for m in models]
    if len({m.surface for m in remaining}) != len(remaining):
        raise ValueError("surface identifiers must be unique")
    for m in remaining:
        m.jacobian = np.asarray(m.jacobian, dtype=float)
        if (m.jacobian.ndim != 2 or m.jacobian.shape[0] != len(original)
                or m.jacobian.shape[1] == 0 or not np.isfinite(m.jacobian).all()):
            raise ValueError("Jacobian shape or values are invalid")
        if m.budget is not None:
            if not math.isfinite(m.budget) or m.budget < 0:
                raise ValueError("slope budget must be finite and nonnegative")
            if m.slope_factor is None:
                raise ValueError("a finite slope budget requires a calibrated slope_factor")
            m.slope_factor = np.asarray(m.slope_factor, dtype=float)
            if (m.slope_factor.ndim != 2 or m.slope_factor.shape[1] != m.jacobian.shape[1]
                    or not np.isfinite(m.slope_factor).all()):
                raise ValueError("slope_factor shape or values are invalid")
            if m.current_slope is None:
                m.current_slope = np.zeros(m.slope_factor.shape[0])
            m.current_slope = _vector(m.current_slope, "current_slope")
            if len(m.current_slope) != m.slope_factor.shape[0]:
                raise ValueError("current_slope length differs from slope_factor")
    # 一次分解复用于所有选择轮次；不在每轮重新做 SVD。
    inverses = {m.surface: np.linalg.pinv(m.jacobian, rcond=rcond) for m in remaining}
    nominal_norm = float(np.linalg.norm(original))
    current = original.copy()
    rounds, selected = [], []
    while remaining:
        table = []
        for m in remaining:
            free_step = -(inverses[m.surface] @ current)
            projection = -(m.jacobian @ free_step)
            factor = float(np.linalg.norm(projection))/nominal_norm if nominal_norm else 0.
            fraction = 1.
            if m.budget is not None:
                before = m.current_slope
                after = before + m.slope_factor @ free_step
                fraction = usable_fraction(float(before@before), float(after@after), m.budget*m.budget)
            table.append({"surface": m.surface, "unconstrained_factor": factor,
                          "usable_fraction": fraction, "improvement_factor": factor*fraction,
                          "step": (free_step*fraction).tolist()})
        # 精确并列时选择后遇到的候选，使并列处理保持确定。
        winner = max(range(len(table)), key=lambda i: (table[i]["improvement_factor"], i))
        rounds.append(table)
        best = table[winner]
        selected.append(dict(best))
        if not cumulative:
            break
        m = remaining.pop(winner)
        current += m.jacobian @ np.asarray(best["step"])
    return {"order": [r["surface"] for r in selected], "selected": selected, "rounds": rounds,
            "method": "linear_projection"}
