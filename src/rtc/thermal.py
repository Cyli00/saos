"""热跟随（OpticStudio MakeThermal 的 Thermal Pickup）：由源结构的面数据算出某一温度下的面数据。

规则由 OpticStudio 24.1 单元格实测反推，官方文档没有给公式；真值在 tests/fixtures/cases/thermal.json。
记 ΔT = 目标结构温度 − 源结构温度，a = α·ΔT（α 是这一面的膨胀系数），b = TCE·ΔT（TCE 是间隔前一面的 TCED）：

- 曲率 c' = c / (1 + a)；半口径、机械半口径 h' = h·(1 + a)；Par n（偶次项 r^(2n)）p' = p / (1 + a)^(2n−1)
- α 的来源：目录玻璃取 AGF 的 ED 第 1 列；模型玻璃和空气中的反射镜取本面 TCED；
  空气面和玻璃内的反射镜沿用前一面的 α；前一面没有 α（空气接空气、光阑）时这一面不缩放
- 玻璃里的厚度 t' = t·(1 + a)
- 其余间隔（空气，以及反射镜之后）t' = (t + z2 − z1)·(1 + b) − z2' + z1'，z 是两侧各自在接触半径处的矢高
- 接触半径：半口径 < 机械半口径时取半口径，随玻璃缩放；否则同样取半口径，但随间隔件缩放（h' = h·(1 + b)）。
  物面之后的间隔（有限物距才有这一行，物面 TCED 为 0）一律按后一种

气压不影响几何（实测把结构 2 的 PRES 改成 0.5，所有几何单元格不变）。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from math import isfinite
from typing import Callable

import numpy as np

from . import geometry
from .material.types import Catalog, Mirror, Model


@dataclass(frozen=True)
class Nominal:
    """一面在源结构的数据。"""

    curvature: float
    semi: float
    mech: float
    params: dict[int, float]
    tce: float  # 1/K，本面 TCED


class ThermalUnsupported(ValueError):
    """这份镜头的热跟随超出已对账的范围。"""


def glass_alpha(spec, tce: float, expansion: Callable[[str], float]) -> float | None:
    """玻璃面的 α（1/K）；不是玻璃时返回 None。"""
    if isinstance(spec, Catalog):
        return expansion(spec.name) * 1e-6
    if isinstance(spec, Model):
        return tce
    return None


def surface_alphas(specs: list, tces: list[float], expansion: Callable[[str], float]) -> list[float | None]:
    """逐面的 α。下标同面号（含物面）。"""
    out: list[float | None] = [None] * len(specs)
    previous_glass = False
    for i in range(1, len(specs)):
        spec = specs[i]
        alpha = glass_alpha(spec, tces[i], expansion)
        if alpha is not None:
            out[i], previous_glass = alpha, True
        elif isinstance(spec, Mirror):
            out[i] = out[i - 1] if previous_glass else tces[i]
        else:
            out[i], previous_glass = (out[i - 1] if previous_glass else None), False
    return out


def scaled(nominal: Nominal, alpha: float | None, dt: float) -> Nominal:
    """一面在目标温度的曲率、半口径、机械半口径和 Par 系数。"""
    if alpha is None:
        return nominal
    f = 1.0 + alpha * dt
    return Nominal(nominal.curvature / f, nominal.semi * f, nominal.mech * f,
                   {n: p / f ** (2 * n - 1) for n, p in nominal.params.items()}, nominal.tce)


def _sag(surface, curvature: float, params: dict[int, float], h: float) -> float:
    """这一面在半径 h 处的矢高。面型取自 surface，曲率和 Par 用给定值。"""
    shape_surface = copy.copy(surface)
    shape_surface.curvature = curvature
    shape_surface.params = dict(surface.params) | params
    value = float(np.ravel(geometry.make(shape_surface).sag(np.array([0.0]), np.array([float(h)]))[0])[0])
    if not np.isfinite(value):
        raise ThermalUnsupported(f"面 {surface.index}: 半径 {h} 处没有矢高，算不出热跟随间隔")
    return value


def _contact(surface, nominal: Nominal, target: Nominal, alpha: float | None, dt: float, b: float, on_barrel: bool = False) -> tuple[float, float]:
    """(源结构矢高, 目标结构矢高)。on_barrel：接触半径随间隔件缩放，不看半口径和机械半口径的关系（物面之后的间隔实测如此）。"""
    z = _sag(surface, nominal.curvature, nominal.params, nominal.semi)
    if nominal.semi < nominal.mech and not on_barrel:
        h = nominal.semi * (1.0 + (alpha or 0.0) * dt)
    else:
        h = nominal.semi * (1.0 + b)
    return z, _sag(surface, target.curvature, target.params, h)


def gap_thickness(front, back, front_nominal: Nominal, back_nominal: Nominal, alphas: tuple[float | None, float | None],
                  t: float, dt: float) -> float:
    """间隔 front→back 的热跟随厚度。"""
    b = front_nominal.tce * dt
    z1, z1p = _contact(front, front_nominal, scaled(front_nominal, alphas[0], dt), alphas[0], dt, b)
    z2, z2p = _contact(back, back_nominal, scaled(back_nominal, alphas[1], dt), alphas[1], dt, b, on_barrel=front.index == 0)
    return (t + z2 - z1) * (1.0 + b) - z2p + z1p


# ---- 解析一个结构 ----

# 能解析的多重结构操作数；CHZN 只支持 0（rtc 没有切边区）
GEOMETRY_OPS = {"THIC": "thickness", "CRVT": "curvature", "CONN": "conic", "SDIA": "semi_diameter", "MCSD": "mech_semi_diameter"}
SUPPORTED_OPS = frozenset(GEOMETRY_OPS) | {"PRAM", "GLSS", "TEMP", "PRES", "CHZN", "MOFF"}
FIXED, VARIABLE, CONFIG_PICKUP = 0, 1, 2


def _literal(lens, op: str, index: int, config: int, param: int, base: float) -> float:
    """一个几何量在结构 config 的数值：有单元格读单元格（拾取跟到源结构），否则取 LDE。热跟随不能作为源。"""
    cell = lens.cell(op, index, config, param)
    if cell is None:
        return base
    code = cell.solve_code
    if code in (FIXED, VARIABLE):
        return cell.value
    if code == CONFIG_PICKUP:
        scale, offset = cell.pickup or (1.0, 0.0)
        return scale * _literal(lens, op, index, cell.source_config, param, base) + offset
    raise ThermalUnsupported(f"{op} 面 {index} 结构 {config}: 热跟随的源必须是固定值或变量")


def _material(lens, index: int, config: int):
    s = lens.surface(index)
    cell = lens.cell("GLSS", index, config)
    if cell is None:
        return s.material
    if cell.solve_code == CONFIG_PICKUP:
        return _material(lens, index, cell.source_config)
    return Catalog(cell.text.upper()) if cell.text else None


def _nominal(lens, index: int, config: int) -> Nominal:
    s = lens.surface(index)
    params = {n: _literal(lens, "PRAM", index, config, n, base) for n, base in s.params.items()}
    return Nominal(_literal(lens, "CRVT", index, config, 0, s.curvature),
                   _literal(lens, "SDIA", index, config, 0, s.semi_diameter),
                   _literal(lens, "MCSD", index, config, 0, s.mech_semi_diameter), params, s.tce * 1e-6)


def _temperature(lens, config: int) -> float:
    return _literal(lens, "TEMP", 0, config, 0, lens.environment.temperature)


def _expansion(lens) -> Callable[[str], float]:
    lookup = lens.glass_lookup
    if lookup is None:
        from .system import catalog

        lookup = catalog().get

    def get(name: str) -> float:
        glass = lookup(name, lens.catalogs)
        if glass.ignore_expansion:
            raise ThermalUnsupported(f"{name}: 目录里标了忽略热膨胀，这条路径没有 OpticStudio 真值")
        return glass.tce

    return get


def _alphas(lens, config: int) -> list[float | None]:
    ordered = sorted(lens.surfaces, key=lambda s: s.index)
    return surface_alphas([_material(lens, s.index, config) for s in ordered], [s.tce * 1e-6 for s in ordered], _expansion(lens))


def thickness_cell(lens, cell, alphas: list[float | None] | None = None) -> float:
    """一个热跟随厚度单元格的值。"""
    source, index = cell.source_config, cell.surface
    if alphas is None:
        alphas = _alphas(lens, source)
    s = lens.surface(index)
    t = _literal(lens, "THIC", index, source, 0, s.thickness)
    dt = _temperature(lens, cell.config) - _temperature(lens, source)
    spec = _material(lens, index, source)
    if isinstance(spec, (Catalog, Model)):
        return t * (1.0 + alphas[index] * dt)
    return gap_thickness(s, lens.surface(index + 1), _nominal(lens, index, source), _nominal(lens, index + 1, source),
                         (alphas[index], alphas[index + 1]), t, dt)


def thermal_value(lens, row, alphas: list[float | None]) -> float:
    """一个热跟随单元格的值。"""
    if row.op == "THIC":
        return thickness_cell(lens, row, alphas)
    source, index = row.source_config, row.surface
    dt = _temperature(lens, row.config) - _temperature(lens, source)
    target = scaled(_nominal(lens, index, source), alphas[index], dt)
    if row.op == "CRVT":
        return target.curvature
    if row.op == "SDIA":
        return target.semi
    if row.op == "MCSD":
        return target.mech
    if row.op == "PRAM":
        return target.params.get(row.param, 0.0)
    raise ThermalUnsupported(f"{row.op} 面 {index}: 这一行没有热跟随规则")


def resolve(lens, config: int):
    """结构 config 的完整状态（见 zmx.Lens.configured）。"""
    from dataclasses import replace

    from .material.types import Environment

    if not 1 <= config <= max(1, lens.num_configs):
        raise ValueError(f"结构号 {config} 超出 1–{lens.num_configs}")
    bad = sorted({row.op for row in lens.mce} - SUPPORTED_OPS)
    if bad:
        raise ThermalUnsupported(f"多重结构操作数 {bad} 没有接入")
    surfaces = [copy.copy(s) for s in lens.surfaces]
    for s in surfaces:
        s.params = dict(s.params)
    by_index = {s.index: s for s in surfaces}
    alphas: dict[int, list[float | None]] = {}
    for row in lens.mce:
        if row.config != config or row.op in ("MOFF", "TEMP", "PRES"):
            continue
        if row.op == "CHZN":
            if row.value:
                raise ThermalUnsupported(f"面 {row.surface} 结构 {config}: rtc 没有切边区（CHZN {row.value}）")
            continue
        target = by_index[row.surface]
        if row.op == "GLSS":
            target.material = _material(lens, row.surface, config)
            continue
        if row.solve_code == lens.thermal_code:
            value = thermal_value(lens, row, alphas.setdefault(row.source_config, _alphas(lens, row.source_config)))
        elif row.op == "THIC" and row.pickup is not None:
            value = lens.thickness(row.surface, config)
        elif row.op == "PRAM":
            value = _literal(lens, "PRAM", row.surface, config, row.param, target.params.get(row.param, 0.0))
        else:
            value = _literal(lens, row.op, row.surface, config, 0, getattr(target, GEOMETRY_OPS[row.op]))
        if row.op == "PRAM":
            target.params[row.param] = value
        else:
            setattr(target, GEOMETRY_OPS[row.op], value)
            if row.op == "SDIA":
                target.semi_fixed = True
            elif row.op == "MCSD":
                target.mech_fixed = True
    env = lens.environment
    environment = Environment(_temperature(lens, config), _literal(lens, "PRES", 0, config, 0, env.pressure), env.adjust)
    return replace(lens, surfaces=surfaces, mce=[], num_configs=1, current_config=1, environment=environment, _cells={}, _cells_size=-1)


def refresh(lens) -> None:
    """把拾取和热跟随格的数值更新成现在算出来的（存盘前用；追迹不读这些缓存值）。"""
    alphas: dict[int, list[float | None]] = {}
    for row in lens.mce:
        code = row.solve_code
        if code == lens.thermal_code and row.op not in ("GLSS", "CHZN"):
            row.value = thermal_value(lens, row, alphas.setdefault(row.source_config, _alphas(lens, row.source_config)))
            row.edited = True
        elif code == lens.thermal_code and row.op == "CHZN":
            row.value = 0.0


def _clear_radius(s) -> float:
    """机械半口径计入的半径：有圆孔径时取半口径和孔径里小的那个（iris 面 1：半口径 7.22、圆孔 6）。"""
    return min(s.semi_diameter, s.ap_max) if s.aperture_code == 1 and s.ap_max > 0 else s.semi_diameter


def _element_mech(ordered: list, alphas: list[float | None]) -> dict[int, float]:
    """一片（含胶合组）玻璃共用一个机械半口径：组内各面半径的最大值。没指定时 OpticStudio 这样算（存盘实测）。
    组内有非圆面孔径时各面取自己的半口径（只有 iris 面 7 矩形孔这一个样本）。"""
    out: dict[int, float] = {}
    group: list = []

    def close() -> None:
        if group:
            shared = not any(s.aperture_code not in (0, 1) for s in group)
            top = max(_clear_radius(s) for s in group)
            out.update({s.index: top if shared else s.semi_diameter for s in group})
            group.clear()

    for s in ordered[1:-1]:
        i = s.index
        if isinstance(s.material, (Catalog, Model)) or (isinstance(s.material, Mirror) and group):
            group.append(s)
        elif group:
            group.append(s)
            close()
        elif alphas[i] is not None:
            out[i] = _clear_radius(s)
    close()
    return out


def make(lens, temperatures: list[float], tce: float) -> None:
    """同 OpticStudio 的 MCE.RunTool_MakeThermal：结构 1 是现在的镜头，其余结构是给定温度下的热跟随。
    行序和求解码都按它存盘的样子：TEMP、PRES，然后逐面 CRVT、THIC、GLSS、SDIA、CHZN、MCSD、PRAM。
    前一面是空气的空气面只有 THIC。半口径和机械半口径变成固定值，边缘光线高度求解变成固定厚度，变量移到结构 1 的单元格。
    tce 是所有面的 TCED（1e-6/K）。"""
    from dataclasses import replace as _replace

    from .zmx import MCEOperand

    if lens.mce:
        raise ThermalUnsupported("镜头已经有多重结构行，不能再生成热结构")
    if not temperatures:
        raise ThermalUnsupported("至少要一个非标称温度")
    ordered = sorted(lens.surfaces, key=lambda s: s.index)
    env = lens.environment
    lens.num_configs, lens.current_config = 1 + len(temperatures), 1
    lens.environment = _replace(env, adjust=True)
    for s in ordered[1:-1]:
        s.raw["TCED"] = f"TCED {float(tce):.12E} 0 0"
        s.raw.pop("MAZH", None)
    alphas = _alphas(lens, 1)
    mechs = _element_mech(ordered, alphas)
    count = lens.num_configs
    rows: list = []
    row_number = 0

    def add(op, surface, values, *, param=0, text="", variable=False, follow=True):
        nonlocal row_number
        row_number += 1
        for c, value in enumerate(values, 1):
            code = (VARIABLE if variable else FIXED) if c == 1 or not follow else (CONFIG_PICKUP if op == "GLSS" else lens.thermal_code)
            scale = 0.0 if op == "GLSS" else 1.0
            source_row = row_number if c > 1 and follow else 1
            rows.append(MCEOperand(op=op, surface=surface, config=c, value=value, raw="", text=text, edited=True,
                                   solve=[code, param, 0, 1, source_row, scale, 0.0, 0, float("nan"), 0]))

    add("TEMP", 0, [env.temperature, *temperatures], follow=False)
    add("PRES", 0, [env.pressure] * count, follow=False)
    same = lambda v: [float(v)] * count  # noqa: E731
    if isfinite(ordered[0].thickness):
        add("THIC", 0, same(ordered[0].thickness))
    for s in ordered[1:-1]:
        i = s.index
        if alphas[i] is None:
            add("THIC", i, same(s.thickness), variable="VDSZ" in s.raw)
            s.raw.pop("VDSZ", None)
            s.semi_fixed = True
            continue
        add("CRVT", i, same(s.curvature), variable=s.solve_radius == 1)
        add("THIC", i, same(s.thickness), variable="VDSZ" in s.raw)
        if isinstance(s.material, Catalog):
            add("GLSS", i, same(float("nan")), text=s.material.name)
        add("SDIA", i, same(s.semi_diameter))
        add("CHZN", i, same(0.0))
        if not s.mech_fixed:
            s.mech_semi_diameter = mechs.get(i, s.semi_diameter)
        add("MCSD", i, same(s.mech_semi_diameter))
        if s.type.upper() == "EVENASPH":
            for n in range(1, 9):
                add("PRAM", i, same(s.params.get(n, 0.0)), param=n, variable=n in s.variable_params)
                s.params.setdefault(n, 0.0)
        s.solve_radius = 0
        s.raw.pop("VDSZ", None)
        s.variable_params = set()
        s.semi_fixed = s.mech_fixed = True
    lens.mce = rows
    lens._cells_size = -1
    refresh(lens)


# Make Thermal 给偶次非球面的 Par 行数（OpticStudio 24.1 存盘实测）
ASPHERE_ROWS = 8


def _renumber(lens, old_keys: list[tuple]) -> None:
    """行增减之后，跟随格的源行号改成新的行号。"""
    new_number = {key: j for j, key in enumerate(lens.row_keys(), 1)}
    for row in lens.mce:
        if row.solve_code in (CONFIG_PICKUP, lens.thermal_code) and len(row.solve) > 4:
            source = int(row.solve[4])
            if 1 <= source <= len(old_keys) and old_keys[source - 1] in new_number:
                row.solve[4] = new_number[old_keys[source - 1]]
                row.edited = True


def follows(lens, index: int) -> bool:
    """这一面的曲率有热跟随行（前一面是空气的空气面没有）。"""
    return any(r.op == "CRVT" and r.surface == index and r.solve_code == lens.thermal_code for r in lens.mce)


def add_asphere_rows(lens, index: int) -> None:
    """Standard 换成 Even Asphere 后补上 PRAM 行，位置和求解码同 Make Thermal：跟在这一面的 MCSD 之后，结构 1 固定、其余热跟随。"""
    from .zmx import MCEOperand

    old = lens.row_keys()
    at = max(k for k, r in enumerate(lens.mce) if r.op == "MCSD" and r.surface == index) + 1
    count = lens.num_configs
    rows = [MCEOperand(op="PRAM", surface=index, config=c, value=0.0, raw="", edited=True,
                       solve=[FIXED if c == 1 else lens.thermal_code, n, 0, 1, 1, 1.0, 0.0, 0, float("nan"), 0])
            for n in range(1, ASPHERE_ROWS + 1) for c in range(1, count + 1)]
    lens.mce[at:at] = rows
    lens._cells_size = -1
    _renumber(lens, old)
    for row in rows:
        if row.config > 1:
            row.solve[4] = lens.row_keys().index(("PRAM", index, row.param)) + 1


def drop_asphere_rows(lens, index: int) -> None:
    """Even Asphere 换回 Standard：去掉这一面的 PRAM 行。"""
    old = lens.row_keys()
    lens.mce[:] = [r for r in lens.mce if not (r.op == "PRAM" and r.surface == index)]
    lens._cells_size = -1
    _renumber(lens, old)
