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
_SYSTEM_RE = re.compile(r"^(aperture|wave\.(\d+)|field\.(\d+)\.([xy]))$")
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
        return "system", f"field_{m.group(4)}", m.group(3)
    ref, sep, param = key.rpartition(".")
    if not sep or not ref or not (param in PARAMS or PAR_RE.match(param)):
        raise SaosError(ErrorCode.BAD_ARGS, f"parameter key {key!r}: use <surface>.<param> with param in {PARAMS} or par<N>, "
                                            "or aperture, field.<n>.x|y, wave.<n>")
    return "surface", ref, param


def _par_cell(ctx, row, param: str, index: int):
    from saos.backend.zos import par_cell

    return par_cell(zosapi(ctx), row, index, param)


def _get_zos(ctx, sysm, kind: str, ref: Any, param: str, names: dict[str, int]) -> Any:
    if kind == "system":
        sd = sysm.SystemData
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

    def memo(self, key, compute):
        return memo(self, key, compute)

    def lib(self, name):
        return lib(self, name)
