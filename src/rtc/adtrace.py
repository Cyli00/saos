"""自动微分追迹：给 DLS 提供雅可比。

设计（见 docs/plans/rtc/HANDOFF.md §8.2）：

1. **求交**只用值做（与 float 路径同一套括号 + Newton 混合，初值也相同），
   然后把解 ``t`` 当隐函数：``F(x0+t·l, y0+t·m) − (z0+t·n) = 0``，
   由隐函数定理

   ``dt/dθ = −F_θ / F_t``，``F_t = (∂z/∂x)·l + (∂z/∂y)·m − n``

   其中 ``F_θ`` 是 ``t`` 固定时对参数的偏导（参数已经以 ``Dual`` 的形式挂在面型上，
   所以这一步就是普通的 Dual 算术）。Newton 迭代本身不展开。
2. **折射**用 ``dual.sqrt`` / ``dual.where`` 把整条链前向传播。
3. **向量化切向**：每个变量占一个切向分量，一次追迹就得到全部雅可比列。

明确不做的（不静默降级，调用方自己决定要不要用）：

- ``BINARY_2`` 的衍射相位不参与求导（相位梯度对参数的导数没有传播；面形与折射仍然求导）。
  三件最终镜头没有 Binary2 面，中间轮次才有。
- 实际光线瞄准（``RAIM = 2``）与 ``OBNA`` 的发射环节不参与求导：取 float 瞄准/发射结果
  后当成常量。三件最终镜头里只有 piB 是 ``RAIM = 2``。
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, replace

import numpy as np

from . import dual as D
from . import geometry
from .system import OpticalSystem


@dataclass(frozen=True)
class Variable:
    """一个优化变量：``system.items[item]`` 上的某个标量。"""

    item: int        # OpticalSystem.items 的下标（不是 .zmx 的面号）
    field: str       # "curvature" | "conic" | "thickness" | "param"
    param: int = 0   # field == "param" 时的参数号

    def key(self):
        return (self.item, self.field, self.param)

    def value(self, system: OpticalSystem) -> float:
        it = system.items[self.item]
        if self.field == "thickness":
            return float(it.thickness)
        if self.field == "param":
            return float(it.surface.params.get(self.param, 0.0))
        return float(getattr(it.surface, self.field))

    def label(self, system: OpticalSystem | None = None) -> str:
        surf = system.items[self.item].index if system is not None else self.item
        if self.field == "param":
            return f"面{surf}.参数{self.param}"
        return f"面{surf}.{'曲率' if self.field == 'curvature' else '圆锥' if self.field == 'conic' else '厚度'}"


def variables_from_lens(system: OpticalSystem) -> list[Variable]:
    """从 ``.zmx`` 的变量标志读出变量表（``CURV`` 第 2 列 = 1、``DISZ`` 第 2 列 = 1、``VPAR n``）。"""
    out: list[Variable] = []
    for k, it in enumerate(system.items):
        s = it.surface
        if getattr(s, "solve_radius", 0) == 1:
            out.append(Variable(k, "curvature"))
        if getattr(s, "solve_thickness", 0) == 1:
            out.append(Variable(k, "thickness"))
        for p in sorted(s.variable_params):
            out.append(Variable(k, "param", p))
    return out


class ADBlock:
    """AD 追迹结果：每个面一列 ``Dual``（切向轴长 = 变量个数）。"""

    def __init__(self, cols: dict[str, list], surface_index: list[int]):
        self.cols = cols
        self.surface_index = surface_index

    def at(self, surf: int) -> dict:
        j = self.surface_index.index(surf)
        return {name: vals[j] for name, vals in self.cols.items()}


def _curvature_axis(it, axis: str):
    """近轴曲率，支持 Dual（与 ``OpticalSystem.curvature_axis`` 同一条规则）。"""
    t = it.type
    if t in ("BICONICX", "BICONICZ"):
        rx = it.surface.params.get(1, 0.0)
        if axis == "x":
            if isinstance(rx, D.Dual):
                return D.where(rx.v == 0.0, 0.0, 1.0 / rx)
            return 0.0 if rx == 0.0 else 1.0 / rx
        return it.surface.curvature
    if t == "TOROIDAL":
        return 0.0 if axis == "x" else it.surface.curvature
    return it.surface.curvature


def _dual_items(system: OpticalSystem, variables: list[Variable], theta):
    """把参数值塞进面属性，返回 (Dual 化的 items, 参数个数)。

    每个 item 都是浅拷贝：``surface`` 与 ``surface.params`` 是新对象，原件不动。
    """
    p = len(variables)
    tang = {v.key(): D.Dual.param(float(theta[i]), i, p) for i, v in enumerate(variables)}
    items = []
    z = D.Dual.konst(0.0)
    for k, it in enumerate(system.items):
        surf = copy.copy(it.surface)
        surf.params = dict(it.surface.params)
        surf.curvature = tang.get((k, "curvature", 0), D.Dual.konst(it.surface.curvature))
        surf.conic = tang.get((k, "conic", 0), D.Dual.konst(it.surface.conic))
        for (i, field, pa), val in tang.items():
            if i == k and field == "param":
                surf.params[pa] = val
        thick = tang.get((k, "thickness", 0), D.Dual.konst(it.thickness))
        focal = it.focal
        if it.type == "PARAXIAL" and focal is not None:
            focal = surf.params.get(1, focal)
        new = replace(it, surface=surf, shape=geometry.make(surf), thickness=thick,
                      vertex_z=z, focal=focal)
        items.append(new)
        tv = thick.v if isinstance(thick, D.Dual) else thick
        if np.isfinite(float(np.ravel(tv)[0])):
            z = z + thick
    return items


def _paraxial_dual(items, y, u, axis: str = "y"):
    """逐面近轴追迹（Dual 版）。返回 [(y_at, u_before, u_after)]。"""
    n = 1.0
    rows = []
    for k, it in enumerate(items):
        if k > 0:
            y = y + u * items[k - 1].thickness
        if it.type == "PARAXIAL" and float(np.ravel(np.asarray(getattr(it.focal, 'v', it.focal or 0.0)))[0]) != 0.0:
            ua = u - y / it.focal
        else:
            c = _curvature_axis(it, axis)
            n2 = it.n_after
            ua = (n * u - y * c * (n2 - n)) / n2
            n = n2
        rows.append((y, u, ua))
        u = ua
    return rows


def _entrance_pupil_z(items, stop: int):
    """入瞳相对第 1 面的位置（Dual）：z_ep = B/A，A、B 是到光阑面的近轴矩阵元。"""
    if stop is None:
        return None
    a = _paraxial_dual(items, D.Dual.konst(1.0), D.Dual.konst(0.0))[stop][0]
    b = _paraxial_dual(items, D.Dual.konst(0.0), D.Dual.konst(1.0))[stop][0]
    if abs(float(np.ravel(a.v)[0])) < 1e-12:
        return None  # 入瞳在无穷远，EPD 发射面不动
    return b / a


def _const_like(value, shape):
    return D.Dual(np.broadcast_to(np.asarray(value, float), shape).copy())


def _launch_dual(system: OpticalSystem, items, px, py, hx, hy, aim: bool):
    """发射。EPD 且不做实际瞄准时，入瞳位置的参数导数也传播（Dual）；否则取 float 结果当常量。"""
    from .trace import _aim, _launch

    shape = np.shape(px)
    if system.lens.aperture_type == "EPD" and not aim:
        r = system.pupil_radius()
        z_ep = _entrance_pupil_z(items, system.stop)
        x0 = _const_like(r * np.asarray(px, float), shape)
        y0 = _const_like(r * np.asarray(py, float), shape)
        if z_ep is None:
            z0 = _const_like(0.0, shape)
        else:
            z0 = z_ep + _const_like(0.0, shape)
        fx, fy = system.field_slopes(hx, hy)
        norm = np.sqrt(fx * fx + fy * fy + 1.0)
        return (x0, y0, z0,
                (_const_like(fx / norm, shape), _const_like(fy / norm, shape),
                 _const_like(1.0 / norm, shape)))
    # OBNA / 实际瞄准：发射环节当成常量（不参与求导，见模块 docstring）
    x0, y0, z0, d = _launch(system, px, py, hx, hy)
    if aim:
        x0, y0, z0, d = _aim(system, x0, y0, z0, d, px, py)
    return (_const_like(x0, np.shape(x0)), _const_like(y0, np.shape(y0)),
            _const_like(z0, np.shape(z0)),
            (_const_like(d[..., 0], shape), _const_like(d[..., 1], shape),
             _const_like(d[..., 2], shape)))


def _refract_dual(L, M, N, Nx, Ny, Nz, n1: float, n2: float):
    """向量 Snell（Dual 版）。全反射时把切向清零，避免把 inf/nan 带进雅可比。"""
    dn = L * Nx + M * Ny + N * Nz
    flip = dn.v > 0.0
    Nx = D.where(flip, -Nx, Nx)
    Ny = D.where(flip, -Ny, Ny)
    Nz = D.where(flip, -Nz, Nz)
    cos1 = D.where(dn.v > 0.0, dn, -dn)
    mu = n1 / n2
    sin2_2 = mu * mu * (1.0 - cos1 * cos1)
    tir = sin2_2.v > 1.0
    cos2 = D.sqrt(D.maximum(1.0 - sin2_2, 0.0))
    k = mu * cos1 - cos2
    Lt = mu * L + k * Nx
    Mt = mu * M + k * Ny
    Nt = mu * N + k * Nz
    norm = D.sqrt(Lt * Lt + Mt * Mt + Nt * Nt)
    out = (Lt / norm, Mt / norm, Nt / norm)
    if np.any(tir):
        return tuple(D.where(tir, D.Dual.konst(0.0), o) for o in out), tir
    return out, tir


def _is_lens(it) -> bool:
    return it.type == "PARAXIAL" and it.focal is not None and float(np.ravel(np.asarray(getattr(it.focal, 'v', it.focal)))[0]) != 0.0


def trace_ad(system: OpticalSystem, px, py, variables: list[Variable], theta,
             hx: float = 0.0, hy: float = 0.0, aim: bool | None = None, _phase: bool = True) -> ADBlock:
    """AD 追迹一批光线，返回每个面每列都是 ``Dual`` 的结果。``phase`` 列同 ``trace``，以同视场主光线为零。"""
    px = np.atleast_1d(np.asarray(px, float))
    py = np.atleast_1d(np.asarray(py, float))
    px, py = np.broadcast_arrays(px, py)
    n_ray = px.size
    do_aim = (system.lens.ray_aiming == 2) if aim is None else aim

    from .trace import _paraxial_lens_phase

    items = _dual_items(system, variables, theta)
    chief = None
    if _phase and any(_is_lens(it) for it in items):
        chief = trace_ad(system, [0.0], [0.0], variables, theta, hx, hy, aim, _phase=False).cols
    X, Y, Z, (L, M, N) = _launch_dual(system, items, px, py, hx, hy, do_aim)

    n_items = len(items)
    cols = {k: [] for k in ("x", "y", "z", "l", "m", "n", "opl", "phase")}
    opl = D.Dual.konst(np.zeros(n_ray))
    phase = D.Dual.konst(np.zeros(n_ray))  # 理想透镜相位，同 trace._paraxial_phase
    n_cur = np.full(n_ray, system.n_before(0))
    lam = system.wavelength * 1e-3

    for k, it in enumerate(items):
        zloc = Z - it.vertex_z
        xv = np.asarray(X.v, float).ravel()
        yv = np.asarray(Y.v, float).ravel()
        zv = np.asarray(zloc.v, float).ravel()
        lv = np.asarray(L.v, float).ravel()
        mv = np.asarray(M.v, float).ravel()
        nv = np.asarray(N.v, float).ravel()
        fshape = system.items[k].shape
        t = fshape.intersect_batch(xv, yv, zv, lv, mv, nv)
        good = np.isfinite(t)
        t = np.where(good, t, 0.0)

        # 隐函数定理：F 在 t 固定时对参数的偏导 / F 对 t 的偏导
        xs = X + L * t
        ys = Y + M * t
        zs, zx, zy = it.shape.sag(xs, ys)
        F = zs - (zloc + N * t)
        ft = (np.asarray(zx.v, float) * lv + np.asarray(zy.v, float) * mv - nv)
        safe = np.where(np.abs(ft) < 1e-14, 1e-14, ft)
        dt = D.Dual(-F.v / safe, -F.d / safe)
        if not np.all(good):
            dt = D.where(good, dt, D.Dual.konst(0.0))

        if k > 0:
            opl = opl + n_cur * (D.Dual(t, np.zeros((F.d.shape[0], n_ray))) + dt)
        tdt = D.Dual(t, np.zeros((F.d.shape[0], n_ray))) + dt
        X = X + L * tdt
        Y = Y + M * tdt
        Z = Z + N * tdt
        zloc = Z - it.vertex_z

        zs, zx, zy = it.shape.sag(X, Y)
        if it.shape.flat_intersection:
            zx, zy = it.shape.refraction_gradient(X, Y)

        if _is_lens(it):
            n2 = it.n_after
            # 同 trace：n'·tan u' = n·tan u − y/f
            ux = (n_cur[0] * L / N - X / it.focal) / n2
            uy = (n_cur[0] * M / N - Y / it.focal) / n2
            norm = D.sqrt(ux * ux + uy * uy + 1.0)
            L, M, N = ux / norm, uy / norm, 1.0 / norm
            if chief is not None:
                phase = phase + _paraxial_lens_phase(
                    D.sqrt, it.focal, n_cur[0], n2, X, Y, L, M, N,
                    chief["x"][k], chief["y"][k], chief["l"][k], chief["m"][k], chief["n"][k])
            n_cur = np.full(n_ray, n2)
        else:
            n2 = it.n_after
            Nx, Ny, Nz = -zx, -zy, D.Dual.konst(np.ones_like(np.asarray(X.v, float)))
            nn = D.sqrt(Nx * Nx + Ny * Ny + Nz * Nz)
            (L, M, N), _ = _refract_dual(L, M, N, Nx / nn, Ny / nn, Nz / nn, n_cur[0], n2)
            n_cur = np.full(n_ray, n2)

        cols["x"].append(X)
        cols["y"].append(Y)
        cols["z"].append(zloc)
        cols["l"].append(L)
        cols["m"].append(M)
        cols["n"].append(N)
        cols["opl"].append(opl)
        cols["phase"].append(phase)

    return ADBlock(cols, [it.index for it in items])


def stack(residuals) -> tuple[np.ndarray, np.ndarray]:
    """把一组标量 ``Dual`` 残差堆成 (值, 雅可比)。雅可比形状 (len(residuals), P)。"""
    vals = np.empty(len(residuals))
    cols = []
    p = 0
    for i, r in enumerate(residuals):
        r = r if isinstance(r, D.Dual) else D.Dual.konst(r)
        vals[i] = float(np.ravel(r.v)[0])
        flat = np.ravel(r.d)
        p = max(p, flat.size)
        cols.append(flat)
    J = np.zeros((len(residuals), p))
    for i, flat in enumerate(cols):
        if flat.size:
            J[i, : flat.size] = flat
    return vals, J
