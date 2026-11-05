"""最小二乘设计：变量 → 系统 → 光线 → 残差和雅可比。雅可比走切向（:mod:`rtc.adtrace`），一次追迹给出全部变量列。

带约束的生产优化器是 :mod:`saos.backend.rtc_optim`。
"""

from __future__ import annotations

import numpy as np

from .adtrace import Variable, trace_ad, variables_from_lens
from .system import OpticalSystem
from .trace import trace


class Design:
    """变量 → 系统 → 光线 → 残差 的最小二乘设计。

    ``rays`` 是 ``(hx, hy, px, py)`` 的列表，归一化视场/光瞳坐标。同一个 ``(hx, hy)``
    的主光线（``px = py = 0``）由内部自己补追，用于去掉横向平移/光程基准。

    ``mode``：
    - ``transverse``：像面交点的横向偏移（mm）× 2 分量；
    - ``wavefront``：相对主光线的光程差（mm）；
    - ``both``：两者都算。
    """

    def __init__(self, system: OpticalSystem, variables: list[Variable] | None = None,
                 rays=None, mode: str = "transverse"):
        self.system = system
        self.variables = list(variables) if variables is not None else variables_from_lens(system)
        if rays is None:
            rays = [(0.0, 0.0, 1.0, 0.0), (0.0, 0.0, 0.0, 1.0), (0.0, 0.0, 0.7, 0.7)]
        self.rays = [tuple(float(x) for x in r) for r in rays]
        if mode not in ("transverse", "wavefront", "both"):
            raise ValueError(f"未知残差模式 {mode}")
        self.mode = mode

    # ---- 变量 ----

    def values(self) -> np.ndarray:
        return np.array([v.value(self.system) for v in self.variables], dtype=float)

    def labels(self) -> list[str]:
        return [v.label(self.system) for v in self.variables]

    # ---- 残差 ----

    def _columns(self, tracer):
        """残差的各列。每个视场的主光线和普通光线合成一批，只追一次。tracer(px, py, hx, hy) 返回像面的 x、y、opl、phase，
        可以是数组或 Dual。"""
        rays = np.asarray(self.rays, dtype=float)
        rays = rays[(rays[:, 2] != 0.0) | (rays[:, 3] != 0.0)]
        if not len(rays):
            raise ValueError("rays 里至少要有一条非主光线")
        fields, row = np.unique(rays[:, :2], axis=0, return_inverse=True)
        chief = np.ravel(row) + len(rays)
        ordinary = np.arange(len(rays))
        zero = np.zeros(len(fields))
        got = tracer(np.r_[rays[:, 2], zero], np.r_[rays[:, 3], zero], np.r_[rays[:, 0], fields[:, 0]], np.r_[rays[:, 1], fields[:, 1]])
        columns = []
        if self.mode in ("transverse", "both"):
            columns += [got["x"][ordinary] - got["x"][chief], got["y"][ordinary] - got["y"][chief]]
        if self.mode in ("wavefront", "both"):
            path = got["opl"] + got["phase"]
            columns.append(path[ordinary] - path[chief])
        return columns

    def residual(self, theta) -> np.ndarray:
        """只算值，走编译内核：阻尼试探步不需要雅可比。"""
        system, image = self.system_with(theta), self.system.items[-1].index
        columns = self._columns(lambda px, py, hx, hy: trace(system, px, py, hx, hy).at(image))
        return np.stack(columns, axis=1).ravel()

    def jacobian(self, theta):
        """返回 (残差, 雅可比)。雅可比由一次成批 AD 追迹得到，形状 (m, P)，行序与 residual 相同。"""
        image = self.system.items[-1].index
        columns = self._columns(lambda px, py, hx, hy: trace_ad(self.system, px, py, self.variables, theta, hx, hy, surfaces=[image]).at(image))
        values = np.stack([np.ravel(c.v) for c in columns], axis=1)
        tangents = np.stack([c.d for c in columns], axis=2)  # (P, 光线, 列)
        return values.ravel(), np.moveaxis(tangents, 0, 2).reshape(values.size, len(self.variables))

    # ---- 把变量写回一个可用的系统副本（优化完取结果用）----

    def system_with(self, theta) -> OpticalSystem:
        from .adtrace import system_with_variables

        return system_with_variables(self.system, self.variables, theta)
