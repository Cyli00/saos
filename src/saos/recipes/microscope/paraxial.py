"""不依赖 Zemax 的近轴计算，用来核对从专利、教材提取的面数据。

这是 ADR-0008（光学计算只经 ZOS-API）的唯一例外：只用作入库前的预筛，条目还要经 OpticStudio 复现校验才收录。
与 25 个收录条目的 OpticStudio 复现记录对比：EFL 相对差 ≤ 5.4e-4，工作距离差 ≤ 0.0134 mm（tests/recipes/microscope/test_paraxial.py）。

面序列从物方到像方：每个面给出曲率半径 r（平面为 inf）、该面之后的间隔 d、该面之后介质的折射率 n。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Surface:
    r: float
    d: float
    n_after: float


def _power(r: float, n_before: float, n_after: float) -> float:
    return 0.0 if math.isinf(r) else (n_after - n_before) / r


def system_matrix(surfaces: Sequence[Surface], n_object: float = 1.0) -> tuple[float, float, float, float]:
    """从第一面到最后一面（不含最后一面之后的间隔）的 ABCD 矩阵，作用于 (y, n·u)。"""
    a, b, c, dd = 1.0, 0.0, 0.0, 1.0
    n_before = n_object
    for k, s in enumerate(surfaces):
        phi = _power(s.r, n_before, s.n_after)
        # 折射：(y, nu) -> (y, nu - y*phi)
        a, b, c, dd = a, b, c - phi * a, dd - phi * b
        if k < len(surfaces) - 1:
            t = s.d / s.n_after
            a, b, c, dd = a + t * c, b + t * dd, c, dd
        n_before = s.n_after
    return a, b, c, dd


def effective_focal_length(surfaces: Sequence[Surface], n_object: float = 1.0) -> float:
    _, _, c, _ = system_matrix(surfaces, n_object)
    return -1.0 / c


def front_focal_distance(surfaces: Sequence[Surface], n_object: float = 1.0) -> float:
    """物方焦点到第一面的距离（正值表示焦点在第一面之前），即无限远物镜的工作距离。"""
    _, _, c, dd = system_matrix(surfaces, n_object)
    # 从前焦点发出的光线出射后平行：C·y + D·(n·u) = 0，且 u = y / s
    return -n_object * dd / c


def back_focal_distance(surfaces: Sequence[Surface], n_object: float = 1.0) -> float:
    """最后一面到像方焦点的距离（在最后一面之后的介质中）。"""
    a, _, c, _ = system_matrix(surfaces, n_object)
    return -surfaces[-1].n_after * a / c
