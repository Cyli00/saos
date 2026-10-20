"""ZOS-API 常用操作的通用函数，省去每个任务重写同一批封装（ADR-0008：数值都由 OpticStudio 计算，这里只做调用和统计）。
Reusable ZOS-API helpers, so that jobs stop rewriting the same wrappers (ADR-0008: OpticStudio computes every number;
this module only calls it and does bookkeeping and statistics on what it returns).

exec 脚本的 ctx、ctx.map 的 worker 上下文和判定脚本的 ctx 都带同名方法（ctx.set(...)、ctx.spot(...)），也可以直接调用
kit.<函数>(ctx, ...)。/ ctx in exec scripts, ctx.map workers and check scripts carry these as methods (ctx.set(...),
ctx.spot(...)); kit.<function>(ctx, ...) works as well.

参数键 / parameter keys:
    "<面>.<参数>"   面是序号（"3"、"-1" 为像面）或 Lens Data 的注释（"lens1 back"）；
                    参数：radius、curvature（1/R，0 为平面）、thickness、conic、semi_diameter、mech_semi_diameter（零件外缘）、
                    material、comment、par<N>
    "aperture"      系统孔径值 / the system aperture value
    "field.<n>.x"、"field.<n>.y"、"wave.<n>"   第 n 个视场坐标、第 n 个波长（µm）/ field n coordinates, wavelength n (µm)
    "config.<c>.<r>" 多重结构第 r 行在结构 c 的值（变焦间隔、温度等）/ multi-configuration row r in configuration c

多重结构的判定按结构分别测量：with ctx.configuration(c): ...，块结束时回到原来的结构，判定结束时镜头不变。
Checks of a multi-configuration lens measure each configuration inside with ctx.configuration(c): ...; the original
configuration returns when the block ends, so the lens is unchanged after the checks.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import numpy as np

from saos.backend.base import PAR_RE
from saos.protocol import ErrorCode, SaosError

PARAMS = ("radius", "curvature", "thickness", "conic", "semi_diameter", "mech_semi_diameter", "material", "comment")
_ROW_ATTRS = {"radius": "Radius", "thickness": "Thickness", "conic": "Conic", "semi_diameter": "SemiDiameter",
              "mech_semi_diameter": "MechanicalSemiDiameter"}
_INT_RE = re.compile(r"^-?\d+$")
_SYSTEM_RE = re.compile(r"^(aperture|wave\.(\d+)|field\.(\d+)\.([xy])|config\.(\d+)\.(\d+))$")
# ReadNextResult 的返回：(成功, 光线号, 错误, 渐晕, X, Y, Z, L, M, N, l2, m2, n2, opd, 强度)
# ReadNextResult returns (ok, ray number, error, vignette, X, Y, Z, L, M, N, l2, m2, n2, opd, intensity)
_READ_ARGS = (0, 0, 0, *([0.0] * 11))


# ---------- 系统对象 / system objects ----------

def system(ctx):
    """OpticStudio 的系统对象；假后端没有时返回 None。/ The OpticStudio system object; None on the fake backend."""
    backend = ctx.backend
    return backend.system if hasattr(type(backend), "system") else None


def zosapi(ctx):
    from saos.backend.zos import _load_zosapi

    return _load_zosapi(ctx.backend.root)


def _need_system(ctx, what: str):
    sysm = system(ctx)
    if sysm is None:
        raise SaosError(ErrorCode.BAD_ARGS, f"{what} needs OpticStudio; {type(ctx.backend).__name__} has none")
    return sysm


# ---------- 面与参数 / surfaces and parameters ----------

def surfaces_by_comment(ctx) -> dict[str, int]:
    """注释 → 面序号（重名时取第一个）。/ comment → surface index (the first one when a comment repeats)."""
    sysm = system(ctx)
    found: dict[str, int] = {}
    if sysm is None:
        for s in ctx.backend.surfaces():
            found.setdefault(str(s.get("comment", "")), int(s["index"]))
    else:
        lde = sysm.LDE
        for i in range(lde.NumberOfSurfaces):
            found.setdefault(str(lde.GetSurfaceAt(i).Comment), i)
    found.pop("", None)
    return found


def _count(ctx) -> int:
    sysm = system(ctx)
    return int(sysm.LDE.NumberOfSurfaces) if sysm is not None else len(ctx.backend.surfaces())


def surface(ctx, ref: int | str, _names: dict[str, int] | None = None) -> int:
    """面序号：整数原样（负数从像面倒数，-1 为像面），字符串按注释查找。
    Surface index: an int as is (negative counts back from the image, -1 is the image), a string by its comment."""
    count = _count(ctx)
    if isinstance(ref, str) and _INT_RE.match(ref.strip()):
        ref = int(ref)
    if isinstance(ref, int) and not isinstance(ref, bool):
        index = ref + count if ref < 0 else ref
        if not 0 <= index < count:
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {ref} out of range 0–{count - 1}")
        return index
    names = surfaces_by_comment(ctx) if _names is None else _names
    if ref not in names:
        raise SaosError(ErrorCode.BAD_ARGS, f"no surface has the comment {ref!r}", {"comments": sorted(names)})
    return names[ref]


def parse_key(key: str) -> tuple[str, Any, str]:
    """("system", 名, 序号) 或 ("surface", 面, 参数)。/ ("system", name, n) or ("surface", ref, param)."""
    key = str(key).strip()
    m = _SYSTEM_RE.match(key)
    if m:
        if m.group(1) == "aperture":
            return "system", "aperture", ""
        if m.group(2):
            return "system", "wave", m.group(2)
        if m.group(5):
            return "system", "config", f"{m.group(5)}.{m.group(6)}"
        return "system", f"field_{m.group(4)}", m.group(3)
    ref, sep, param = key.rpartition(".")
    if not sep or not ref or not (param in PARAMS or PAR_RE.match(param)):
        raise SaosError(ErrorCode.BAD_ARGS, f"parameter key {key!r}: use <surface>.<param> with param in {PARAMS} or par<N>, "
                                            "or aperture, field.<n>.x|y, wave.<n>, config.<c>.<row>")
    return "surface", ref, param


def _par_cell(ctx, row, param: str, index: int):
    from saos.backend.zos import par_cell

    return par_cell(zosapi(ctx), row, index, param)


def _config_cell(sysm, param: str):
    """多重结构第 r 行在结构 c 的单元格。/ The cell of row r in configuration c."""
    c, r = (int(x) for x in param.split("."))
    mce = sysm.MCE
    count, rows = int(mce.NumberOfConfigurations), int(mce.NumberOfOperands)
    if count < 2 or not (1 <= c <= count and 1 <= r <= rows):
        raise SaosError(ErrorCode.BAD_ARGS, f"config.{c}.{r}: the lens has {count} configuration(s) and {rows} multi-configuration row(s)")
    return mce, c, mce.GetOperandAt(r).GetOperandCell(c)


def _get_zos(ctx, sysm, kind: str, ref: Any, param: str, names: dict[str, int]) -> Any:
    if kind == "system":
        sd = sysm.SystemData
        if ref == "config":
            from saos.backend.zos import cell_value

            return cell_value(_config_cell(sysm, param)[2])
        if ref == "aperture":
            return float(sd.Aperture.ApertureValue)
        if ref == "wave":
            return float(sd.Wavelengths.GetWavelength(int(param)).Wavelength)
        field = sd.Fields.GetField(int(param))
        return float(field.X if ref == "field_x" else field.Y)
    index = surface(ctx, ref, names)
    row = sysm.LDE.GetSurfaceAt(index)
    if param == "curvature":
        r = float(row.Radius)
        return 0.0 if r == 0 or math.isinf(r) else 1.0 / r
    if PAR_RE.match(param):
        return float(_par_cell(ctx, row, param, index).DoubleValue)
    if param in ("material", "comment"):
        return str(getattr(row, param.capitalize()))
    return float(getattr(row, _ROW_ATTRS[param]))


def _set_zos(ctx, sysm, kind: str, ref: Any, param: str, value: Any, names: dict[str, int]) -> None:
    if kind == "system":
        sd = sysm.SystemData
        if ref == "config":
            from saos.backend.zos import set_cell

            mce, c, cell = _config_cell(sysm, param)
            if isinstance(value, str) and mce.GetOperandAt(int(param.split(".")[1])).TypeName == "GLSS":
                value = ctx.backend._check_material(value)
            set_cell(cell, value, f"config.{param}")
            if c == int(mce.CurrentConfiguration):
                # 改的是当前结构：重新套用，镜头才跟着变 / the current configuration: re-apply it so the lens follows
                mce.SetCurrentConfiguration(c)
            return
        if ref == "aperture":
            sd.Aperture.ApertureValue = float(value)
        elif ref == "wave":
            sd.Wavelengths.GetWavelength(int(param)).Wavelength = float(value)
        else:
            field = sd.Fields.GetField(int(param))
            setattr(field, "X" if ref == "field_x" else "Y", float(value))
        return
    index = surface(ctx, ref, names)
    row = sysm.LDE.GetSurfaceAt(index)
    if param == "material":
        # 材料走后端的目录校验 / materials go through the backend's catalog check
        ctx.backend.set_surface(index, "material", value)
        return
    if param == "comment":
        row.Comment = "" if value is None else str(value)
        return
    target = float(value)
    if param == "curvature":
        target, param = (math.inf if target == 0 else 1.0 / target), "radius"
    if PAR_RE.match(param):
        cell = _par_cell(ctx, row, param, index)
        cell.DoubleValue = target
        actual = float(cell.DoubleValue)
    else:
        attr = _ROW_ATTRS[param]
        setattr(row, attr, target)
        actual = float(getattr(row, attr))
    if not (actual == target or abs(actual - target) <= 1e-12 * max(1.0, abs(target))):
        raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"surface {index} {param} did not take {target}; a solve or pickup may override it",
                        {"requested": target, "actual": actual})


def get(ctx, key: str) -> Any:
    """一个参数的当前值。/ The current value of one parameter."""
    return values(ctx, [key])[key]


def values(ctx, keys: Iterable[str]) -> dict[str, Any]:
    """几个参数的当前值。/ The current values of several parameters."""
    keys = list(keys)
    sysm = system(ctx)
    names = surfaces_by_comment(ctx) if any(not _INT_RE.match(parse_key(k)[1]) for k in keys if parse_key(k)[0] == "surface") else {}
    out: dict[str, Any] = {}
    for key in keys:
        kind, ref, param = parse_key(key)
        if sysm is not None:
            out[key] = _get_zos(ctx, sysm, kind, ref, param, names)
        elif kind == "system":
            raise SaosError(ErrorCode.BAD_ARGS, f"{type(ctx.backend).__name__} has no system settings to read ({key})")
        else:
            index = surface(ctx, ref, names)
            v = ctx.backend.surface_value(index, "radius" if param == "curvature" else param)
            out[key] = (0.0 if v == 0 or math.isinf(v) else 1.0 / v) if param == "curvature" else v
    return out


def set(ctx, changes: dict[str, Any] | None = None, **kw: Any) -> None:  # noqa: A001 - 与 ctx.set 同名 / mirrors ctx.set
    """一次写入几个参数，写不进去（被求解覆盖等）时报 VALUE_NOT_APPLIED。
    Write several parameters at once; VALUE_NOT_APPLIED when one does not take (a solve overrides it, …)."""
    changes = {**(changes or {}), **kw}
    if not changes:
        return
    sysm = system(ctx)
    names = surfaces_by_comment(ctx) if any(parse_key(k)[0] == "surface" and not _INT_RE.match(parse_key(k)[1]) for k in changes) else {}
    for key, value in changes.items():
        kind, ref, param = parse_key(key)
        if sysm is not None:
            _set_zos(ctx, sysm, kind, ref, param, value, names)
        elif kind == "system":
            raise SaosError(ErrorCode.BAD_ARGS, f"{type(ctx.backend).__name__} has no system settings to write ({key})")
        else:
            index = surface(ctx, ref, names)
            if param == "curvature":
                param, value = "radius", (math.inf if float(value) == 0 else 1.0 / float(value))
            ctx.backend.set_surface(index, param, value)
    if hasattr(ctx, "mark_dirty"):
        ctx.mark_dirty()


@contextmanager
def kept(ctx, keys: Iterable[str]) -> Iterator[dict[str, Any]]:
    """with 块结束时把这些参数恢复原值（出错也恢复）。/ Restore these parameters when the with block ends, errors included."""
    before = values(ctx, keys)
    try:
        yield dict(before)
    finally:
        set(ctx, before)


def configurations(ctx) -> int:
    """结构数（单结构为 1）。/ The number of configurations (1 for a single one)."""
    return int(_need_system(ctx, "configurations").MCE.NumberOfConfigurations)


@contextmanager
def configuration(ctx, n: int) -> Iterator[None]:
    """with 块内切到第 n 个结构，结束时回到原来的结构（出错也回）。按结构分别测量的判定用它，判定结束时镜头不变；
    ctx.memo 的键要带上结构号。
    Switch to configuration n inside the block and back to the original one when it ends, errors included. Checks that
    measure each configuration use it, so the lens is unchanged after them; include the configuration in ctx.memo keys."""
    mce = _need_system(ctx, "configuration").MCE
    count = int(mce.NumberOfConfigurations)
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= count:
        raise SaosError(ErrorCode.BAD_ARGS, f"configuration must be 1–{count}, got {n!r}")
    before = int(mce.CurrentConfiguration)
    mce.SetCurrentConfiguration(n)
    try:
        yield
    finally:
        mce.SetCurrentConfiguration(before)


@contextmanager
def restored(ctx) -> Iterator[None]:
    """with 块结束时整个镜头回到进入时的内容（存一份临时副本再加载，约 0.1 s），用于会增删面的试探。
    The whole lens returns to its content at entry when the block ends (a temporary copy is saved and reloaded,
    about 0.1 s); for trials that insert or remove surfaces."""
    handle, name = tempfile.mkstemp(prefix="saos-keep-", suffix=".zmx")
    os.close(handle)
    path = Path(name)
    ctx.backend.save_as(path)
    try:
        yield
    finally:
        ctx.backend.load(path)
        for p in path.parent.glob(path.stem + ".*"):
            p.unlink(missing_ok=True)


# ---------- 操作数与分析 / operands and analyses ----------

def op(ctx, name: str, int1: int = 0, int2: int = 0, *data: float) -> float:
    """任意优化操作数的值，例如 op(ctx, "EFFL", 0, 1)、op(ctx, "SSAG", 3, 0, 0.0, 0.2)。
    The value of any merit operand, e.g. op(ctx, "EFFL", 0, 1), op(ctx, "SSAG", 3, 0, 0.0, 0.2)."""
    return ctx.backend.operand(str(name), int(int1), int(int2), [float(x) for x in data])


def analysis(ctx, name: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """运行一个分析并返回导出的数据（序列、网格、表头），不留文件；要存证据用 saos lens analysis。
    Run an analysis and return its exported data (series, grids, header) without keeping files; use saos lens analysis for evidence."""
    folder = Path(tempfile.mkdtemp(prefix="saos-analysis-"))
    try:
        return ctx.backend.analysis(str(name), dict(settings or {}), folder / "out.txt")
    finally:
        for p in folder.glob("*"):
            p.unlink(missing_ok=True)
        folder.rmdir()


def quick_focus(ctx) -> dict[str, Any]:
    return ctx.backend.quick_focus()


def primary_wave(ctx) -> int:
    sysm = _need_system(ctx, "primary_wave")
    waves = sysm.SystemData.Wavelengths
    for n in range(1, waves.NumberOfWavelengths + 1):
        if waves.GetWavelength(n).IsPrimary:
            return n
    return 1


def material_nd(ctx, ref: int | str) -> float:
    """面材料的 nd：模型玻璃取求解里的 nd，目录材料从启用的目录里查（材料目录工具）。
    The nd of a surface's material: a model glass from its solve, a catalog material from the catalogs in use."""
    sysm = _need_system(ctx, "material_nd")
    row = sysm.LDE.GetSurfaceAt(surface(ctx, ref))
    solve = row.MaterialCell.GetSolveData()
    if str(solve.Type) == "MaterialModel":
        return float(solve._S_MaterialModel.IndexNd)
    from saos.backend.zos import catalog_constants

    name = str(row.Material)
    constants = catalog_constants(sysm, name)
    if constants is not None:
        return constants[0]
    raise SaosError(ErrorCode.BAD_ARGS, f"material {name!r} of surface {ref} is not in the catalogs in use")


def global_matrix(ctx, ref: int | str) -> tuple[np.ndarray, np.ndarray]:
    """面局部坐标 → 全局坐标：(3×3 旋转, 平移)，全局点 = R @ 局部点 + t。
    Local → global coordinates of a surface: (3×3 rotation, translation); global = R @ local + t."""
    sysm = _need_system(ctx, "global_matrix")
    m = sysm.LDE.GetGlobalMatrix(surface(ctx, ref), *([0.0] * 12))
    if not m[0]:
        raise SaosError(ErrorCode.ZOS_ERROR, f"GetGlobalMatrix failed for surface {ref}")
    return np.array([float(v) for v in m[1:10]]).reshape(3, 3), np.array([float(v) for v in m[10:13]])


def sag(ctx, ref: int | str, x: float, y: float) -> float | None:
    """面在 (x, y) 处的矢高（LDE.GetSag）；该点没有定义时为 None。/ Surface sag at (x, y); None where undefined."""
    sysm = _need_system(ctx, "sag")
    r = sysm.LDE.GetSag(surface(ctx, ref), float(x), float(y), 0.0, 0.0)
    return float(r[1]) if r[0] and math.isfinite(float(r[1])) else None


# ---------- 光线 / rays ----------

@dataclass
class Rays:
    """批量追迹的结果，每个量是一维数组；坐标在目标面的局部坐标系、镜头单位。ok = 没有错误、没有渐晕、数值有限。
    Batch trace results, one array per quantity; coordinates in the target surface's local frame, lens units.
    ok = no error, not vignetted, finite."""

    wave: np.ndarray
    hx: np.ndarray
    hy: np.ndarray
    px: np.ndarray
    py: np.ndarray
    error: np.ndarray
    vignette: np.ndarray
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    l: np.ndarray  # noqa: E741 - 方向余弦沿用 ZOS-API 的名字 / direction cosines keep the ZOS-API names
    m: np.ndarray
    n: np.ndarray
    intensity: np.ndarray
    surface: int

    @property
    def ok(self) -> np.ndarray:
        finite = np.isfinite(self.x) & np.isfinite(self.y) & np.isfinite(self.z) & np.isfinite(self.l) & np.isfinite(self.m) & np.isfinite(self.n)
        return (self.error == 0) & (self.vignette == 0) & finite

    def select(self, mask: np.ndarray) -> Rays:
        return Rays(**{k: (getattr(self, k)[mask] if k != "surface" else self.surface) for k in self.__dataclass_fields__})


def pupil(n: int = 21) -> list[tuple[float, float]]:
    """单位圆内的 n×n 方格光瞳采样点（归一化坐标）。/ An n×n square grid of normalised pupil points inside the unit circle."""
    if n < 2:
        return [(0.0, 0.0)]
    # 与各任务原有的写法逐位一致：-1 + 2i/(n-1)，外层 px / bit-identical to the jobs' own grids: -1 + 2i/(n-1), px outer
    grid = [-1 + 2 * i / (n - 1) for i in range(n)]
    return [(px, py) for px in grid for py in grid if px * px + py * py <= 1 + 1e-12]


def trace(ctx, rays: Iterable[Iterable[float]], to: int | str | None = None) -> Rays:
    """归一化坐标的批量实际光线追迹。rays：(wave, hx, hy, px, py) 的序列；to：目标面，默认像面。
    Batch real-ray trace in normalised coordinates. rays: (wave, hx, hy, px, py) rows; to: target surface, default the image."""
    sysm = _need_system(ctx, "trace")
    arr = np.asarray(list(rays), dtype=float).reshape(-1, 5)
    count = len(arr)
    target = int(sysm.LDE.NumberOfSurfaces) - 1 if to is None else surface(ctx, to)
    out = np.zeros((count, 9))
    if count:
        rt = zosapi(ctx).Tools.RayTrace
        tool = sysm.Tools.OpenBatchRayTrace()
        if tool is None:
            raise SaosError(ErrorCode.ZOS_ERROR, "cannot open the batch ray trace tool; another tool (an optimiser?) is still open")
        try:
            batch = tool.CreateNormUnpol(count, rt.RaysType.Real, target)
            add, mode = batch.AddRay, getattr(rt.OPDMode, "None")
            for wave, hx, hy, px, py in arr.tolist():
                add(int(wave), hx, hy, px, py, mode)
            tool.RunAndWaitForCompletion()
            batch.StartReadingResults()
            read = batch.ReadNextResult
            for k in range(count):
                r = read(*_READ_ARGS)
                # 读取失败的行记为错误 -1 / a row that could not be read counts as error -1
                out[k] = (r[2] if r[0] else -1, r[3], r[4], r[5], r[6], r[7], r[8], r[9], r[14])
        finally:
            tool.Close()
    return Rays(wave=arr[:, 0].astype(int), hx=arr[:, 1], hy=arr[:, 2], px=arr[:, 3], py=arr[:, 4],
                error=out[:, 0].astype(int), vignette=out[:, 1].astype(int), x=out[:, 2], y=out[:, 3], z=out[:, 4],
                l=out[:, 5], m=out[:, 6], n=out[:, 7], intensity=out[:, 8], surface=target)


def spread(x: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    """点列的质心、RMS 半径和最大半径（相对质心）/ centroid, RMS radius and largest radius about the centroid"""
    cx, cy = float(x.mean()), float(y.mean())
    d2 = (x - cx) ** 2 + (y - cy) ** 2
    return {"centroid": [cx, cy], "rms_radius": float(math.sqrt(d2.mean())), "geo_radius": float(math.sqrt(d2.max()))}


def best_focus(rays: Rays) -> float | None:
    """沿目标面局部 z 平移多少时弥散斑 RMS 最小（闭式解）。前提：目标面之后是均匀介质，光线是直线；
    这只是把 OpticStudio 追迹出的位置和方向沿直线外推，不是追迹。
    The shift along the target surface's local z that minimises the RMS spot (closed form). Precondition: a homogeneous
    medium after the target surface, so rays are straight; this extrapolates OpticStudio's traced rays, it traces nothing."""
    good = rays.ok & (np.abs(rays.n) > 1e-12)
    return focus_shift(rays.x[good], rays.y[good], rays.l[good] / rays.n[good], rays.m[good] / rays.n[good])


def focus_shift(x: np.ndarray, y: np.ndarray, u: np.ndarray, v: np.ndarray) -> float | None:
    """best_focus 的闭式解本身：位置 (x, y)、斜率 (u, v) = (l/n, m/n) 的直线束沿 z 平移多少时 RMS 最小。
    少于 3 条光线，或各光线斜率相同（平行光，没有焦点）时为 None。
    The closed form behind best_focus for straight rays at (x, y) with slopes (u, v) = (l/n, m/n). None with fewer
    than 3 rays or when every slope is the same (a collimated beam has no focus)."""
    if len(x) < 3:
        return None
    du, dv = u - u.mean(), v - v.mean()
    den = float((du * du + dv * dv).sum())
    return None if den == 0 else float(-((x - x.mean()) * du + (y - y.mean()) * dv).sum() / den)


def spot(ctx, fields: Iterable[Iterable[float]] = ((0.0, 0.0),), wave: int | Iterable[int] | None = None, grid: int = 21,
         to: int | str | None = None, refocus: bool = False, min_rays: int = 3) -> list[dict[str, Any]]:
    """各视场（和波长）的点列统计，一次批量追迹完成。每行：field、wave、launched、arrived、pass_rate（%）、
    chief（主光线落点，到不了时 None）、centroid、rms_radius、geo_radius（相对质心，镜头单位）；refocus=True 时另给
    best_dz（见 best_focus）和该处的 rms_radius_best。到达光线少于 min_rays 的行 valid 为 False，统计量为 None。
    Spot statistics per field (and wavelength) from one batch trace. Each row: field, wave, launched, arrived,
    pass_rate (%), chief (None when it does not arrive), centroid, rms_radius, geo_radius (about the centroid, lens
    units); with refocus=True also best_dz (see best_focus) and rms_radius_best there. Rows with fewer than min_rays
    arriving rays have valid False and None statistics."""
    sysm = system(ctx)
    if to is None and sysm is not None and bool(sysm.SystemData.Aperture.AFocalImageSpace):
        # 无焦系统的像在无穷远：像面上的落点不反映像质，量会被当成镜头单位误读
        # an afocal system images to infinity: landing points on the image surface say nothing and would be read as lengths
        raise SaosError(ErrorCode.BAD_ARGS, "the system is afocal (image at infinity), so spot sizes on the image surface mean nothing; "
                        "measure the angular spread from the ray directions (ctx.trace gives l, m, n) or an operand/analysis "
                        "that reports afocal units, or pass to=<a surface> deliberately")
    fields = [tuple(float(v) for v in f) for f in fields]
    waves = [primary_wave(ctx)] if wave is None else [int(wave)] if isinstance(wave, (int, np.integer)) else [int(w) for w in wave]
    grid_pts = pupil(grid)
    rays, rows = [], []
    for hx, hy in fields:
        for w in waves:
            rays.append((w, hx, hy, 0.0, 0.0))
            rays.extend((w, hx, hy, px, py) for px, py in grid_pts)
            rows.append({"field": [hx, hy], "wave": w})
    result = trace(ctx, rays, to)
    per = len(grid_pts) + 1
    ok = result.ok
    for k, row in enumerate(rows):
        start = k * per
        chief_ok = bool(ok[start])
        row["chief"] = [float(result.x[start]), float(result.y[start])] if chief_ok else None
        mask = np.zeros(len(ok), bool)
        mask[start + 1:start + per] = True
        sub = result.select(mask & ok)
        arrived = len(sub.x)
        row.update(launched=len(grid_pts), arrived=arrived, pass_rate=100.0 * arrived / len(grid_pts), valid=arrived >= min_rays)
        if arrived >= min_rays:
            row.update(spread(sub.x, sub.y))
            if refocus:
                dz = best_focus(sub)
                row["best_dz"] = dz
                if dz is not None:
                    keep = np.abs(sub.n) > 1e-12
                    row["rms_radius_best"] = spread(sub.x[keep] + sub.l[keep] / sub.n[keep] * dz,
                                                     sub.y[keep] + sub.m[keep] / sub.n[keep] * dz)["rms_radius"]
                else:
                    row["rms_radius_best"] = None
        else:
            row.update(centroid=None, rms_radius=None, geo_radius=None)
            if refocus:
                row.update(best_dz=None, rms_radius_best=None)
    return rows


# ---------- 加密采样与极值 / dense sampling and extremes ----------
#
# 一次任务里补全视场 CRA、加密光谱透过率时，边缘一点或五个波长都漏掉过极值（nightvision，2026-10-19：中间视场的 CRA
# 达 19.88°，加密光谱后透过率出现五波长之间的低谷）。这些函数按同一个格式交回：量的定义和单位、采样范围与步长、极值和
# 它的位置、所有局部极值、读不出的点、加密后极值移动了多少，以及覆盖是否够（coverage）。
# Filling in the full-field CRA and a denser transmission spectrum on one job, an edge point or five wavelengths missed the
# extreme (nightvision, 2026-10-19: 19.88° CRA in mid field, a transmission dip between the five wavelengths). These
# functions all return the quantity's definition and unit, the sampled range and step, the extreme and where it is, every
# local extreme, unreadable points, how far refining moved the extreme, and whether the coverage is enough.

# profile 一次最多等距取的点数 / the most equally spaced points one profile takes
PROFILE_MAX_N = 10001


def _number(v: Any) -> float | None:
    """有限实数原样，其他（None、非有限、复数、文字）算读不出。/ A finite real number, else None (unreadable)."""
    if isinstance(v, bool) or not isinstance(v, (int, float, np.integer, np.floating)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def profile(evaluate: Callable[[list[float]], Iterable[float | None]], lo: float, hi: float, n: int = 33, *, find: str = "max",
            refine: int = 2, points: int = 8, rel: float = 1e-3) -> dict[str, Any]:
    """在 [lo, hi] 上等距取 n 点求极值，再在当前极值两侧相邻样点之间加密 refine 次（每次 points 点）。evaluate 一次收到一批
    x、按序返回值（None 或非有限值算读不出）；一批一次调用，便于批量追迹。coverage.ok 为假的情形：有读不出的点、加密让极值
    变化超过 rel（相对）、或有效样点少于 3 个。at_edge 表示极值在区间端点，区间外可能更极端。
    Find the extreme over n equally spaced points on [lo, hi], then refine `refine` times between the samples next to the
    current extreme (`points` each time). evaluate gets a batch of x and returns values in order (None or non-finite means
    unreadable), one call per batch so that rays can be traced in batches. coverage.ok is False when points were unreadable,
    refining changed the extreme by more than rel (relative), or fewer than 3 samples are valid. at_edge: the extreme lies at
    an end of the range and may be exceeded outside it."""
    if find not in ("max", "min"):
        raise SaosError(ErrorCode.BAD_ARGS, "find must be max or min")
    if isinstance(n, bool) or not isinstance(n, (int, np.integer)) or not 2 <= n <= PROFILE_MAX_N:
        raise SaosError(ErrorCode.BAD_ARGS, f"profile needs an integer n in 2–{PROFILE_MAX_N} (got {n!r})")
    if isinstance(refine, bool) or not isinstance(refine, (int, np.integer)) or not isinstance(points, (int, np.integer)) or points < 1:
        raise SaosError(ErrorCode.BAD_ARGS, f"profile needs integer refine and points >= 1 (got {refine!r}, {points!r})")
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError) as exc:
        raise SaosError(ErrorCode.BAD_ARGS, f"profile needs numeric lo and hi (got {lo!r}, {hi!r})") from exc
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        raise SaosError(ErrorCode.BAD_ARGS, f"profile needs finite lo < hi (got {lo}, {hi})")
    sign = 1.0 if find == "max" else -1.0
    samples: dict[float, float | None] = {}

    def run(xs: list[float]) -> None:
        xs = [x for x in xs if x not in samples]
        if not xs:
            return
        values = list(evaluate(xs))
        if len(values) != len(xs):
            raise SaosError(ErrorCode.BAD_ARGS, f"evaluate returned {len(values)} values for {len(xs)} points")
        for x, v in zip(xs, values):
            samples[x] = _number(v)

    def best() -> tuple[float, float] | None:
        valid = [(x, v) for x, v in samples.items() if v is not None]
        return max(valid, key=lambda xv: sign * xv[1]) if valid else None

    run([lo + (hi - lo) * k / (n - 1) for k in range(n)])
    coarse = best()
    for _ in range(max(0, refine)):
        current = best()
        if current is None:
            break
        xs = sorted(samples)
        k = xs.index(current[0])
        a, b = xs[max(0, k - 1)], xs[min(len(xs) - 1, k + 1)]
        run([a + (b - a) * (j + 1) / (points + 1) for j in range(points)])
    final = best()
    ordered = sorted(samples.items())
    valid = [(x, v) for x, v in ordered if v is not None]
    local = [[x, v] for k, (x, v) in enumerate(valid)
             if all(sign * v >= sign * w for _, w in valid[max(0, k - 1):k + 2])]
    local.sort(key=lambda xv: -sign * xv[1])
    missing = [x for x, v in ordered if v is None]
    notes = []
    if missing:
        notes.append(f"{len(missing)} of {len(ordered)} points gave no value")
    if len(valid) < 3:
        notes.append("fewer than 3 valid samples")
    shift = None
    if coarse is not None and final is not None:
        shift = final[1] - coarse[1]
        if abs(shift) > rel * max(1e-12, abs(final[1])):
            notes.append(f"refining moved the {find} by {shift:.4g}: the coarse step {(hi - lo) / (n - 1):.4g} is too wide")
    return {"find": find, "value": final[1] if final else None, "at": final[0] if final else None, "range": [lo, hi],
            "step": (hi - lo) / (n - 1), "evaluations": len(ordered), "refine_shift": shift,
            "at_edge": bool(final and final[0] in (lo, hi)), "local": local[:10], "missing": missing,
            "samples": [[x, v] for x, v in ordered], "coverage": {"ok": not notes, "notes": notes}}


def cra(ctx, direction: Iterable[float] = (0.0, 1.0), wave: int | Iterable[int] | None = None, n: int = 33, refine: int = 2,
        to: int | str | None = None) -> dict[str, Any]:
    """主光线角沿视场方向的分布和最大值：实际主光线（px = py = 0）与目标面（默认像面）局部 z 轴的夹角，取所测各波长中的最大，
    单位度。direction 是归一化视场坐标里的终点 (hx, hy)：径向归一化时 (0, 1) 是 +y 全视场；矩形归一化时对角是 (1, 1)。
    wave 默认全部波长。某个视场点有任一波长的主光线到不了，这一点记为读不出（coverage 会说明）。
    The chief ray angle along a field direction and its maximum: the angle between the real chief ray (px = py = 0) and
    the local z axis of the target surface (default the image), the largest over the wavelengths measured, in degrees.
    direction is the end point (hx, hy) in normalised field coordinates: (0, 1) is the +y full field with radial
    normalisation; the diagonal is (1, 1) with rectangular normalisation. wave defaults to every wavelength. A field point
    where any wavelength's chief ray fails counts as unreadable (coverage says so)."""
    sysm = _need_system(ctx, "cra")
    dx, dy = (float(v) for v in direction)
    waves = (list(range(1, int(sysm.SystemData.Wavelengths.NumberOfWavelengths) + 1)) if wave is None
             else [int(wave)] if isinstance(wave, (int, np.integer)) else [int(w) for w in wave])
    per_wave: dict[float, list[float | None]] = {}

    def evaluate(qs: list[float]) -> list[float | None]:
        result = trace(ctx, [(w, q * dx, q * dy, 0.0, 0.0) for q in qs for w in waves], to)
        angles = np.degrees(np.arccos(np.clip(np.abs(result.n), 0.0, 1.0)))
        out = []
        for k, q in enumerate(qs):
            row = [float(angles[k * len(waves) + j]) if result.ok[k * len(waves) + j] else None for j in range(len(waves))]
            per_wave[q] = row
            out.append(None if None in row else max(row))
        return out

    found = profile(evaluate, 0.0, 1.0, n, find="max", refine=refine)
    return {"quantity": "chief ray angle", "unit": "deg",
            "definition": "angle between the real chief ray (px=py=0) and the target surface's local z axis, largest over the wavelengths",
            "direction": [dx, dy], "waves": waves, "surface": surface(ctx, to) if to is not None else int(sysm.LDE.NumberOfSurfaces) - 1,
            "per_wave_at_extreme": dict(zip(waves, per_wave.get(found["at"], []))) if found["at"] is not None else None, **found}


def field_profile(ctx, fn: Callable[..., float | None], direction: Iterable[float] = (0.0, 1.0), n: int = 17, *, find: str = "max",
                  refine: int = 1, unit: str = "", quantity: str = "") -> dict[str, Any]:
    """任意量沿视场方向的分布和极值：对 q ∈ [0, 1] 调 fn(ctx, hx, hy)（hx, hy = q·direction，归一化视场坐标），格式同 profile。
    fn 不能改动镜头。用于相对照度、畸变、像质等逐视场量；主光线角用 cra（一次批量追迹，快得多）。
    Any quantity along a field direction and its extreme: fn(ctx, hx, hy) at q in [0, 1] (hx, hy = q·direction in
    normalised field coordinates), returned like profile. fn must leave the lens unchanged. For relative illumination,
    distortion, image quality per field; for the chief ray angle use cra (one batch trace, much faster)."""
    dx, dy = (float(v) for v in direction)
    found = profile(lambda qs: [fn(ctx, q * dx, q * dy) for q in qs], 0.0, 1.0, n, find=find, refine=refine)
    return {"quantity": quantity, "unit": unit, "axis": "normalised field q", "direction": [dx, dy], **found}


def wave_profile(ctx, fn: Callable[..., float | None], lo: float, hi: float, n: int = 33, *, find: str = "min", refine: int = 2,
                 wave: int | None = None, unit: str = "", quantity: str = "") -> dict[str, Any]:
    """任意量沿波长的分布和极值（透过率的低谷、色差的峰值）：把第 wave 个波长（默认主波长）依次设为 [lo, hi] µm 内的各点，
    调 fn(ctx, wave) 取值，结束后恢复原波长；格式同 profile。几个离散波长之间的窄低谷只有这样加密才看得见。
    Any quantity across wavelength and its extreme (a transmission dip, a colour peak): set wavelength number `wave`
    (default the primary) to points in [lo, hi] µm in turn, read fn(ctx, wave), restore the wavelength afterwards; returned
    like profile. A narrow dip between a few discrete wavelengths only shows up this way."""
    slot = primary_wave(ctx) if wave is None else int(wave)
    key = f"wave.{slot}"

    def evaluate(xs: list[float]) -> list[float | None]:
        out = []
        for x in xs:
            set(ctx, {key: x})
            out.append(fn(ctx, slot))
        return out

    with kept(ctx, [key]):
        found = profile(evaluate, float(lo), float(hi), n, find=find, refine=refine)
    return {"quantity": quantity, "unit": unit, "axis": "wavelength (µm)", "wave": slot, **found}


# ---------- 缓存与任务内模块 / cache and job modules ----------

def memo(ctx, key: Any, compute: Callable[[], Any]) -> Any:
    """在 ctx.cache 里按 key 记住 compute() 的结果。一次判定（check、round、measure）内所有判定脚本共享同一个 cache，
    镜头在判定期间不变；exec 脚本自己改了镜头之后要 ctx.cache.clear()。
    Remember compute() under key in ctx.cache. All check scripts of one evaluation (check, round, measure) share one
    cache and the lens does not change during it; an exec script that changes the lens must call ctx.cache.clear()."""
    cache = ctx.cache
    if key not in cache:
        cache[key] = compute()
    return cache[key]


def lib(ctx, name: str):
    """加载任务里的辅助模块（"fiberlib" 即 checks/fiberlib.py，也可以写任务内相对路径），同一个 cache 内只加载一次。
    Load a helper module of the job ("fiberlib" means checks/fiberlib.py; a job-relative path also works), once per cache."""
    import importlib.util

    rel = name if name.endswith(".py") else f"checks/{name}.py"
    path = ctx.path(rel)
    if not path.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"cannot find {rel} in the job")
    key = ("lib", str(path), path.stat().st_mtime_ns)

    def load():
        spec = importlib.util.spec_from_file_location(f"saos_lib_{path.stem}", str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module

    return memo(ctx, key, load)


class KitMethods:
    """把本模块的函数挂到上下文对象上（ctx.set(...)、wctx.spot(...)）。/ Expose this module's functions as context methods."""

    cache: dict[Any, Any]

    def surface(self, ref):
        return surface(self, ref)

    def get(self, key):
        return get(self, key)

    def values(self, keys):
        return values(self, keys)

    def set(self, changes=None, **kw):  # noqa: A003
        return set(self, changes, **kw)

    def kept(self, keys):
        return kept(self, keys)

    def restored(self):
        return restored(self)

    def configurations(self):
        return configurations(self)

    def configuration(self, n):
        return configuration(self, n)

    def op(self, name, int1=0, int2=0, *data):
        return op(self, name, int1, int2, *data)

    def analysis(self, name, settings=None):
        return analysis(self, name, settings)

    def quick_focus(self):
        return quick_focus(self)

    def trace(self, rays, to=None):
        return trace(self, rays, to)

    def spot(self, fields=((0.0, 0.0),), wave=None, grid=21, to=None, refocus=False, min_rays=3):
        return spot(self, fields, wave, grid, to, refocus, min_rays)

    def cra(self, direction=(0.0, 1.0), wave=None, n=33, refine=2, to=None):
        return cra(self, direction, wave, n, refine, to)

    def field_profile(self, fn, direction=(0.0, 1.0), n=17, **kw):
        return field_profile(self, fn, direction, n, **kw)

    def wave_profile(self, fn, lo, hi, n=33, **kw):
        return wave_profile(self, fn, lo, hi, n, **kw)

    def memo(self, key, compute):
        return memo(self, key, compute)

    def lib(self, name):
        return lib(self, name)
