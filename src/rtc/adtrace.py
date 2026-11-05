"""优化雅可比：给 rtc.kernel 的单步播下设计变量的切向种子，一次追迹得到全部变量列。

求交按隐函数定理 ``dt/dθ = −F_θ/F_t``，Newton 迭代本身不展开。发射对变量的依赖分三类：

- 入瞳位置和半径：由光瞳的近轴量给出（``_pupil``）；
- 有限物距的发射方向：朝向随变量移动的入瞳；
- 实际瞄准：发射量 a 由 ``G(a, θ) = stop(a, θ) − target(θ) = 0`` 决定，``da/dθ = −J⁻¹·(∂stop/∂θ − ∂target/∂θ)``。
  a 多占两个切向分量一起走完，到终点再线性组合，不用追两遍。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace

import numpy as np

from . import dual as D
from . import geometry, kernel
from .system import OpticalSystem, na_pupil_radius, stop_transfer
from .trace import aim_by_position, launched, paraxial_lens_phase


@dataclass(frozen=True)
class Variable:
    """一个优化变量：``system.items[item]`` 上的某个标量。"""

    item: int        # OpticalSystem.items 的下标（不是 .zmx 的面号）
    field: str       # 曲率、圆锥、厚度、参数或光阑半口径
    param: int = 0   # field == "param" 时的参数号

    def key(self):
        return (self.item, self.field, self.param)

    def value(self, system: OpticalSystem) -> float:
        it = system.items[self.item]
        if self.field == "thickness":
            return float(it.thickness)
        if self.field == "semi_diameter":
            return float(it.semi)
        if self.field == "param":
            return float(it.surface.params.get(self.param, 0.0))
        return float(getattr(it.surface, self.field))

    def label(self, system: OpticalSystem | None = None) -> str:
        surf = system.items[self.item].index if system is not None else self.item
        if self.field == "param":
            return f"面{surf}.参数{self.param}"
        if self.field == "semi_diameter":
            return f"面{surf}.半口径"
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
        if s.semi_variable:
            out.append(Variable(k, "semi_diameter"))
        for p in sorted(s.variable_params):
            out.append(Variable(k, "param", p))
    return out


def system_with_variables(system: OpticalSystem, variables: list[Variable], theta) -> OpticalSystem:
    """把变量写入独立的镜头与 items，供求交值、差分和优化终点共同使用。"""
    theta = np.asarray(theta, dtype=float)
    if theta.shape != (len(variables),) or not np.all(np.isfinite(theta)):
        raise ValueError("变量值须为与变量表等长的一维有限数组")
    if len({v.key() for v in variables}) != len(variables):
        raise ValueError("变量表不能重复同一个参数")
    if not variables:
        return system
    lens = copy.deepcopy(system.lens)
    for it in system.items:
        surface = copy.deepcopy(it.surface)
        surface.thickness, surface.semi_diameter = it.thickness, it.semi
        lens.surfaces[lens.surfaces.index(lens.surface(it.index))] = surface
    for v, value in zip(variables, theta):
        if not 0 <= v.item < len(system.items):
            raise ValueError(f"变量面下标越界：{v.item}")
        s = lens.surface(system.items[v.item].index)
        if v.field == "param":
            s.params[v.param] = float(value)
        elif v.field in ("curvature", "conic", "thickness", "semi_diameter"):
            if v.field == "semi_diameter":
                if lens.aperture_type != "FLOA" or not s.is_stop or value <= 0:
                    raise ValueError("半口径变量仅用于 Float By Stop Size 的光阑，且必须大于零")
                if s.aperture_code == 10:
                    s.ap_max = float(value)
            setattr(s, v.field, float(value))
        else:
            raise ValueError(f"未知变量字段：{v.field}")
    for row in lens.mce:
        if row.op == "THIC" and row.config == system.config:
            row.value = lens.surface(row.surface).thickness
    return OpticalSystem(lens, wavelength_um=system.wavelength, config=system.config,
                         aim_scale=system.aim_scale, glasses=system.glasses, media=system._media,
                         allow_unverified=system.allow_unverified)


def thickness_sensitivity(system: OpticalSystem, variables: list[Variable], theta) -> np.ndarray:
    """各面厚度对变量的导数，形状 (面数, 变量数)。多重结构的 pickup 让一个面的厚度跟着另一个面走，
    所以不一定是对角阵；厚度对变量线性，变量加 1 后重建系统读差值就是精确导数。"""
    out = np.zeros((len(system.items), len(variables)))
    thickness = [j for j, v in enumerate(variables) if v.field == "thickness"]
    if not any(row.pickup for row in system.lens.mce):
        for j in thickness:
            out[variables[j].item, j] = 1.0
        return out
    base = np.array([it.thickness for it in system.items], dtype=float)
    for j in thickness:
        probe = np.array(theta, dtype=float)
        probe[j] += 1.0
        changed = system_with_variables(system, variables, probe)
        with np.errstate(invalid="ignore"):
            out[:, j] = np.nan_to_num(np.array([it.thickness for it in changed.items], dtype=float) - base)
    return out


def _dual_items(system: OpticalSystem, variables: list[Variable], theta, thickness: np.ndarray):
    """面属性换成 Dual 的 items，只给近轴追迹和理想透镜相位用；原件不动。"""
    p = len(variables)
    tang = {v.key(): D.Dual.param(float(theta[i]), i, p) for i, v in enumerate(variables)}
    items = []
    for k, it in enumerate(system.items):
        surf = copy.copy(it.surface)
        surf.params = dict(it.surface.params)
        surf.curvature = tang.get((k, "curvature", 0), surf.curvature)
        surf.conic = tang.get((k, "conic", 0), surf.conic)
        for (i, field, pa), val in tang.items():
            if i == k and field == "param":
                surf.params[pa] = val
        thick = D.Dual(np.asarray(it.thickness, dtype=np.float64), thickness[k].copy()) if thickness[k].any() else it.thickness
        focal = surf.params.get(1, it.focal) if it.type == "PARAXIAL" and it.focal is not None else it.focal
        items.append(replace(it, surface=surf, shape=geometry.make(surf), thickness=thick, focal=focal,
                             semi=tang.get((k, "semi_diameter", 0), it.semi)))
    return items


class ADBlock:
    """AD 追迹结果：``at(面号)`` 返回各量的 ``Dual``，切向轴长 = 变量个数。"""

    def __init__(self, out, tangents, slot, error, vig, phases, surface_index: list[int]):
        self.out, self.tangents, self.slot, self.error, self.vig = out, tangents, slot, error, vig
        self.phases = phases
        self.surface_index = surface_index

    def at(self, surf: int) -> dict:
        k = self.surface_index.index(surf)
        place = self.slot[k]
        if place < 0:
            raise KeyError(f"第 {surf} 面的切向没有记录，用 surfaces 参数要求它")
        names = ("x", "y", "z", "l", "m", "n", "opl")
        result = {name: D.Dual(self.out[:, k, c], np.moveaxis(self.tangents[:, place, :, c], 0, 1)) for c, name in enumerate(names)}
        result["phase"] = self.phases[k]
        return result


def _vector(value, P: int) -> np.ndarray:
    if not isinstance(value, D.Dual) or value.d.shape[0] == 0:
        return np.zeros(P)
    return np.ravel(value.d).copy()


def _pupil(system: OpticalSystem, reference, primary: OpticalSystem, P: int):
    """入瞳半径和位置对变量的导数 (dr, dz_ep)，形状 (P,)。入瞳按主波长定（见 OpticalSystem.pupil_reference）。"""
    a, b = (D._as(v) for v in stop_transfer(reference, system.stop))
    z_ep = b / a if abs(float(np.ravel(a.v)[0])) >= 1e-12 else None
    r = None
    if system.lens.aperture_type == "FLOA":
        primary.stop_semi_diameter()
        if not np.isfinite(a.v) or abs(float(a.v)) < 1e-14:
            raise ValueError("光阑传递系数为零，半口径导数没有定义")
        r = reference[system.stop].semi / abs(a)
    elif system.lens.aperture_type == "OBNA" and z_ep is not None:
        r = na_pupil_radius(z_ep, system.object_thickness if system.finite_object else 0.0, system.lens.aperture_value)
    return _vector(r, P), _vector(z_ep, P)


def _launch_seed(system, n_ray, P, extra, qx, qy, hx, hy, dr, dz_ep, position_aim, x0, y0, z0):
    """发射光线对变量的切向种子，形状 (光线, P + extra, 7)。瞄准解的那一部分由隐函数项补上。"""
    seed = np.zeros((n_ray, P + extra, kernel.TANGENT))
    if not system.finite_object:
        if system.lens.aperture_type in ("EPD", "FLOA"):
            seed[:, :P, 2] = dz_ep
            if not position_aim:
                seed[:, :P, 0] = qx[:, None] * dr
                seed[:, :P, 1] = qy[:, None] * dr
        return seed
    legacy_obna = (system.lens.aperture_type == "OBNA" and system.object_thickness == 0.0
                   and system.lens.field_type == "ANGLE" and not system.allow_unverified)
    if legacy_obna:
        return seed
    # 有限物距：物点随入瞳移动（角度视场），发射方向指向入瞳上的 (qx·r, qy·r, z_ep)
    dxo, dyo = np.zeros((n_ray, P)), np.zeros((n_ray, P))
    if system.lens.field_type == "ANGLE":
        tx, ty = system.field_slopes(hx, hy)
        dxo, dyo = -np.asarray(tx)[:, None] * dz_ep, -np.asarray(ty)[:, None] * dz_ep
    seed[:, :P, 0], seed[:, :P, 1] = dxo, dyo
    if extra:
        return seed
    z_ep, r = system.pupil_reference()
    vx, vy, vz = qx * r - x0, qy * r - y0, np.full(n_ray, z_ep) - z0
    length = np.sqrt(vx * vx + vy * vy + vz * vz)[:, None]
    dvx = qx[:, None] * dr - dxo
    dvy = qy[:, None] * dr - dyo
    dvz = np.broadcast_to(dz_ep, (n_ray, P))
    projection = (vx[:, None] * dvx + vy[:, None] * dvy + vz[:, None] * dvz) / length ** 3
    seed[:, :P, 3] = dvx / length - vx[:, None] * projection
    seed[:, :P, 4] = dvy / length - vy[:, None] * projection
    seed[:, :P, 5] = dvz / length - vz[:, None] * projection
    return seed


def _seed_aim_unknowns(seed, P, position_aim, d) -> None:
    """瞄准未知量占最后两个切向分量：发射位置 (x0, y0) 或发射斜率 (ux, uy)。"""
    if position_aim:
        seed[:, P, 0] = 1.0
        seed[:, P + 1, 1] = 1.0
        return
    w = 1.0 / d[:, 2]
    ux, uy = d[:, 0] * w, d[:, 1] * w
    w3 = w * w * w
    seed[:, P, 3], seed[:, P, 4], seed[:, P, 5] = 1.0 / w - ux * ux / w3, -ux * uy / w3, -ux / w3
    seed[:, P + 1, 3], seed[:, P + 1, 4], seed[:, P + 1, 5] = -ux * uy / w3, 1.0 / w - uy * uy / w3, -uy / w3


def _target_tangent(system, primary, reference, variables, theta, links, dvz):
    """光阑半径 (Rx, Ry) 对变量的导数，形状 (P, 2)：瞄准目标是 (px·Rx, py·Ry)。"""
    P = len(variables)
    if system.aim_scale is not None:
        return np.zeros((P, 2))
    if system.lens.aperture_type == "FLOA":
        dR = _vector(reference[system.stop].semi, P)
        return np.stack([dR, dR], axis=1)
    if system.lens.aperture_type == "EPD" and not system.finite_object and system.lens.ray_aiming != 1:
        # 主波长的实际边缘光线（轴上、平行入射、高度 EPD/2）在光阑面的高度
        seed = np.zeros((1, P, kernel.TANGENT))
        slot = np.full(len(primary.items), -1, np.int64)
        slot[primary.stop] = 0
        out, _, _, out_t = kernel.trace(primary.packed(), [0.0], [primary.lens.aperture_value / 2.0], [0.0], [[0.0, 0.0, 1.0]],
                                        kernel.Tangents(seed, dvz, *links, slot))
        dR = np.sign(out[0, primary.stop, 1]) * out_t[0, 0, :, 1]
        return np.stack([dR, dR], axis=1)
    # 其余定义未对账或只有近轴标量，每个雅可比只算一次，中心差分足够
    out = np.zeros((P, 2))
    for j in range(P):
        step = 1e-6 * max(abs(theta[j]), 1e-3)
        plus, minus = np.array(theta, dtype=float), np.array(theta, dtype=float)
        plus[j] += step
        minus[j] -= step
        a = system_with_variables(primary, variables, plus).aim_target(1.0, 1.0)
        b = system_with_variables(primary, variables, minus).aim_target(1.0, 1.0)
        out[j] = (np.asarray(a, float) - np.asarray(b, float)) / (2.0 * step)
    return out


def _links(system, variables):
    """设计变量落到面参数行的连接表，按面排好。"""
    per_surface = [[] for _ in system.items]
    for j, v in enumerate(variables):
        for col, mult in system.items[v.item].shape.links(v.field, v.param):
            per_surface[v.item].append((j, col, mult))
    ptr = np.zeros(len(system.items) + 1, np.int64)
    ptr[1:] = np.cumsum([len(rows) for rows in per_surface])
    flat = [row for rows in per_surface for row in rows]
    return (ptr, np.array([r[0] for r in flat], np.int64), np.array([r[1] for r in flat], np.int64),
            np.array([r[2] for r in flat], np.float64))


def _vertex_tangent(thickness: np.ndarray, extra: int) -> np.ndarray:
    """面顶点 z 对变量的导数，形状 (面, P + extra)。"""
    out = np.zeros((thickness.shape[0], thickness.shape[1] + extra))
    out[1:, :thickness.shape[1]] = np.cumsum(thickness[:-1], axis=0)
    return out


def _run(system, primary, reference, variables, theta, qx, qy, hx, hy, do_aim, thickness, primary_thickness, links, wanted):
    """追一批光线，返回 (out, 切向 (光线, 记录的面, P, 7), slot, error, vig)，切向里已含瞄准的隐函数项。"""
    P = len(variables)
    n_ray = qx.size
    position_aim = do_aim and aim_by_position(system)
    extra = 2 if do_aim else 0
    x0, y0, z0, d = launched(system, qx, qy, hx, hy, do_aim)
    x0, y0, z0 = (np.ascontiguousarray(np.broadcast_to(v, qx.shape), dtype=float) for v in (x0, y0, z0))
    d = np.ascontiguousarray(d, dtype=float)
    dr, dz_ep = _pupil(system, reference, primary, P) if system.stop is not None else (np.zeros(P), np.zeros(P))
    seed = _launch_seed(system, n_ray, P, extra, qx, qy, hx, hy, dr, dz_ep, position_aim, x0, y0, z0)
    if do_aim:
        _seed_aim_unknowns(seed, P, position_aim, d)
    slot = np.full(len(system.items), -1, np.int64)
    rows = sorted(set(wanted) | ({system.stop} if do_aim else set()))
    slot[rows] = np.arange(len(rows))
    out, error, vig, tangents = kernel.trace(system.packed(), x0, y0, z0, d,
                                             kernel.Tangents(seed, _vertex_tangent(thickness, extra), *links, slot))
    if not do_aim:
        return out, tangents, slot, error, vig
    k = system.stop
    target = _target_tangent(system, primary, reference, variables, theta, links, _vertex_tangent(primary_thickness, 0))
    tx, ty = system.aim_target(qx, qy)
    # 没瞄准到（退回近轴发射）的光线不满足 G = 0，隐函数项不成立，发射按常量处理
    solved = (np.abs(out[:, k, 0] - tx) < 1e-8) & (np.abs(out[:, k, 1] - ty) < 1e-8) & (error == 0)
    at_stop = tangents[:, slot[k]]
    J = np.stack([at_stop[:, P:, 0], at_stop[:, P:, 1]], axis=1)  # (光线, 交点分量, 未知量)
    solved &= np.abs(J[:, 0, 0] * J[:, 1, 1] - J[:, 0, 1] * J[:, 1, 0]) > 1e-300
    rhs = np.stack([at_stop[:, :P, 0] - qx[:, None] * target[:, 0], at_stop[:, :P, 1] - qy[:, None] * target[:, 1]], axis=1)
    da = np.zeros((n_ray, 2, P))
    if P and solved.any():
        da[solved] = -np.linalg.inv(J[solved]) @ rhs[solved]
    return out, tangents[:, :, :P] + np.einsum("nsuc,nuj->nsjc", tangents[:, :, P:], da), slot, error, vig


def _phases(system, out, tangents, slot, chief_out, chief_tangents, row, reference, P):
    """各面累计的理想透镜相位，以同视场主光线为零；公式与浮点追迹共用 trace.paraxial_lens_phase。"""
    def column(values, derivatives, k, c):
        return D.Dual(values[:, k, c], np.moveaxis(derivatives[:, slot[k], :, c], 0, 1))

    n_ray = out.shape[0]
    accumulated = D.Dual(np.zeros(n_ray), np.zeros((P, n_ray)))
    result = []
    for k, it in enumerate(system.items):
        if it.type == "PARAXIAL" and it.focal:
            ray = [column(out, tangents, k, c) for c in (0, 1, 3, 4, 5)]
            chief = [column(chief_out, chief_tangents, k, c)[row] for c in (0, 1, 3, 4, 5)]
            accumulated = accumulated + paraxial_lens_phase(reference[k].focal, system.n_before(k), it.n_after, *ray, *chief)
        result.append(accumulated)
    return result


def trace_ad(system: OpticalSystem, px, py, variables: list[Variable], theta,
             hx=0.0, hy=0.0, aim: bool | None = None, surfaces: list[int] | None = None) -> ADBlock:
    """一批光线的值和对变量的导数。``surfaces`` 是要切向的面号（默认全部）；光线多、变量多时只要像面能省内存。
    ``phase`` 列同 ``trace``，以同视场主光线为零。"""
    from .trace import aiming

    theta = np.asarray(theta, dtype=float)
    system = system_with_variables(system, variables, theta)
    px, py, hx, hy = np.broadcast_arrays(*(np.atleast_1d(np.asarray(v, dtype=float)) for v in (px, py, hx, hy)))
    px, py, hx, hy = (np.ascontiguousarray(v.ravel()) for v in (px, py, hx, hy))
    qx, qy = (np.ascontiguousarray(v.ravel(), dtype=float) for v in system.vignetted_pupil(px, py, hx, hy))
    do_aim = aiming(system) if aim is None else bool(aim) and system.stop is not None
    P = len(variables)
    thickness = thickness_sensitivity(system, variables, theta)
    primary = system.primary_system()
    primary_thickness = thickness if primary is system else thickness_sensitivity(primary, variables, theta)
    reference = _dual_items(primary, variables, theta, primary_thickness)
    links = _links(system, variables)
    index = [it.index for it in system.items]
    wanted = set(range(len(index))) if surfaces is None else {index.index(s) for s in surfaces}
    lenses = [k for k, it in enumerate(system.items) if it.type == "PARAXIAL" and it.focal]
    common = (primary, reference, variables, theta)
    tail = (do_aim, thickness, primary_thickness, links, wanted | set(lenses))
    out, tangents, slot, error, vig = _run(system, *common, qx, qy, hx, hy, *tail)
    if lenses:
        fields, row = np.unique(np.stack([hx, hy], axis=1), axis=0, return_inverse=True)
        zero = np.zeros(len(fields))
        fx, fy = np.ascontiguousarray(fields[:, 0]), np.ascontiguousarray(fields[:, 1])
        cx, cy = (np.ascontiguousarray(np.ravel(v), dtype=float) for v in system.vignetted_pupil(zero, zero, fx, fy))
        chief_out, chief_tangents, _, _, _ = _run(system, *common, cx, cy, fx, fy, *tail)
        with np.errstate(divide="ignore", invalid="ignore"):
            phases = _phases(system, out, tangents, slot, chief_out, chief_tangents, np.ravel(row), reference, P)
    else:
        phases = [D.Dual(np.zeros(px.size), np.zeros((P, px.size)))] * len(index)
    return ADBlock(out, tangents, slot, error, vig, phases, index)
