"""阻尼最小二乘（DLS）。

雅可比走**前向自动微分**（:mod:`rtc.adtrace`）：一次追迹给出全部变量列，
不再用中心差分。求交用隐函数定理 ``dt/dθ = −F_θ/F_t``，Newton 迭代本身不展开。

Zemax 的 DLS 内部细节未公开，这里只做"算法同类"（ADR-0001 第 3 节）：
阻尼系数初值、下降判据、收敛判据都是自定的，不保证与 OpticStudio 逐迭代相同。

``lm`` 保持原来的调用方式（``lm(residual, x0)``），那时才会回退到中心差分；
传 :class:`Design` 则用 AD。
"""

from __future__ import annotations

import copy
from dataclasses import replace

import numpy as np

from . import geometry
from .adtrace import Variable, stack, trace_ad, variables_from_lens
from .system import OpticalSystem


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

    def residual(self, theta) -> np.ndarray:
        return self.jacobian(theta)[0]

    def jacobian(self, theta):
        """返回 (残差, 雅可比)。雅可比由一次 AD 追迹得到，形状 (m, P)。"""
        sys = self.system
        k_img = sys.items[-1].index
        chiefs = {}
        residuals = []

        def chief(hx, hy):
            key = (hx, hy)
            if key not in chiefs:
                b = trace_ad(sys, [0.0], [0.0], self.variables, theta, hx, hy)
                chiefs[key] = b.at(k_img)
            return chiefs[key]

        for hx, hy, px, py in self.rays:
            if px == 0.0 and py == 0.0:
                continue
            b = trace_ad(sys, [px], [py], self.variables, theta, hx, hy)
            got = b.at(k_img)
            ref = chief(hx, hy)
            if self.mode in ("transverse", "both"):
                residuals.append(got["x"][0] - ref["x"][0])
                residuals.append(got["y"][0] - ref["y"][0])
            if self.mode in ("wavefront", "both"):
                residuals.append((got["opl"][0] + got["phase"][0]) - (ref["opl"][0] + ref["phase"][0]))
        if not residuals:
            raise ValueError("rays 里至少要有一条非主光线")
        return stack(residuals)

    # ---- 把变量写回一个可用的系统副本（优化完取结果用）----

    def system_with(self, theta) -> OpticalSystem:
        vals = {v.key(): float(t) for v, t in zip(self.variables, theta)}
        sys = copy.copy(self.system)
        sys.items = []
        z = self.system.items[0].vertex_z if self.system.items else 0.0
        for k, it in enumerate(self.system.items):
            surf = copy.copy(it.surface)
            surf.params = dict(it.surface.params)
            for (i, field, pa), val in vals.items():
                if i != k:
                    continue
                if field == "param":
                    surf.params[pa] = val
                elif field != "thickness":
                    setattr(surf, field, val)
            thick = vals.get((k, "thickness", 0), it.thickness)
            focal = it.focal
            if it.type == "PARAXIAL" and focal is not None:
                focal = surf.params.get(1, focal)
            # 顶点位置由前面各面厚度累加而来，改了厚度必须重算，否则追迹仍用旧位置
            sys.items.append(replace(it, surface=surf, shape=geometry.make(surf),
                                     thickness=thick, focal=focal, vertex_z=z))
            if np.isfinite(thick):
                z += thick
        sys.image_z = z
        # 浅拷贝带着原系统的内核数据与入瞳缓存，换了 items 必须丢掉
        sys.invalidate()
        return sys


def lm(fun, x0, *, max_iter: int = 100, tol: float = 1e-12, lam0: float = 1e-3,
       verbose: bool = False):
    """最小化 ||residual(x)||²。

    ``fun`` 可以是残差函数（返回 (m,) 数组，雅可比用中心差分），
    也可以是 :class:`Design`（雅可比用 AD）。
    """
    has_ad = isinstance(fun, Design)
    residual = fun.residual if has_ad else fun

    x = np.asarray(x0, dtype=float).copy()
    r = np.asarray(residual(x), dtype=float).ravel()
    cost = float(r @ r)
    lam = lam0
    dx = np.zeros_like(x)
    it = 0
    for it in range(max_iter):
        if has_ad:
            r, J = fun.jacobian(x)
            r = np.asarray(r, dtype=float).ravel()
        else:
            J = _jacobian(residual, x, r)
        A = J.T @ J
        g = J.T @ r
        improved = False
        for _ in range(12):
            try:
                dx = np.linalg.solve(A + lam * np.diag(np.maximum(np.diag(A), 1e-12)), -g)
            except np.linalg.LinAlgError:
                lam *= 10.0
                continue
            rn = np.asarray(residual(x + dx), dtype=float).ravel()
            cn = float(rn @ rn)
            if cn < cost:
                x, r, cost = x + dx, rn, cn
                lam = max(lam / 3.0, 1e-12)
                improved = True
                break
            lam *= 10.0
        if verbose:
            print(f"  iter {it:3d} cost={cost:.6e} lam={lam:.1e}")
        if not improved or np.max(np.abs(dx)) < tol:
            break
    return x, cost, it + 1


def _jacobian(residual, x, r0):
    """中心差分雅可比（只在 ``lm`` 拿到普通残差函数时用）。"""
    n = x.size
    J = np.zeros((r0.size, n))
    for i in range(n):
        h = 1e-7 * max(1.0, abs(x[i]))
        xp = x.copy()
        xp[i] += h
        xm = x.copy()
        xm[i] -= h
        J[:, i] = (np.asarray(residual(xp)).ravel() - np.asarray(residual(xm)).ravel()) / (2 * h)
    return J
