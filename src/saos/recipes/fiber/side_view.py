"""光纤侧视探头的判定量（配方草稿，由 fiber-printed-relay 任务的 checks/fiberlib.py 检查 v6 上收，2026-10-17）。
Measurements for side-viewing fibre probes (draft recipe, lifted from checks/fiberlib.py v6 of the fiber-printed-relay job).

数值定义与 fiberlib v6 相同；改动只有两处：追迹、操作数和材料查询改用 saos.kit，同一次判定内反复用到的量（几何前提、
轴上焦点、各视场光线、PSF）经 ctx.memo 只算一次。判定脚本只要一行：
The numbers are defined as in fiberlib v6; only two things changed: tracing, operands and material lookups go through
saos.kit, and quantities several checks reuse within one evaluation (geometric preconditions, the on-axis focus, the
field rays, the PSF) are computed once through ctx.memo. A check script is one line:

    from saos.recipes.fiber import side_view as sv
    def run(ctx):
        return sv.check(ctx, "spot")

镜头约定（建模时遵守，名字可以用 Layout 改）：光纤端面为物面（物方远心，物高 = 光纤芯圆半径）；经一次理想反射折转，
折转后厚度为负；面注释含 "prism exit"、"tube inner wall"、"tube outer wall"，像面为样品面；棱镜出射面上的透镜（若有）注释为
"lens plane"（贴合面）与 "lens dome"；打印透镜与胶的材料名以 "SAOS_" 开头。长度单位 mm，返回值 µm（另注明者除外）。
Lens conventions (names can be changed with Layout): the fibre face is the object (object-space telecentric, object
height = fibre core radius); one ideal fold, negative thicknesses after it; comments "prism exit", "tube inner wall",
"tube outer wall"; the image is the sample; an optional lens on the prism exit is "lens plane" / "lens dome"; printed
lens and glue materials start with "SAOS_". Lengths in mm, results in µm unless stated.

共同前提：零件几何成立（geometry_ok），否则 DesignFailure，判定返回 inf。只读：PSF 等需要临时改系统的测量在 finally 中恢复。
Common precondition: valid part geometry (geometry_ok), otherwise DesignFailure and the check returns inf. Read-only:
measurements that change the system temporarily (PSF plane and fields) restore it in finally.
"""

from __future__ import annotations

import math
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from saos import kit

W488, W532 = 1, 2
GRID = 21
FIELDS5 = ((0.0, 0.0), (0.7, 0.0), (-0.7, 0.0), (0.0, 0.7), (0.0, -0.7))
MID_FIELDS = ((0.7, 0.0), (-0.7, 0.0), (0.0, 0.7), (0.0, -0.7))
EDGE_FIELDS = ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0))
PSF_PUPIL = "S_64x64"
PSF_IMAGE = "S_128x128"


@dataclass(frozen=True)
class Layout:
    """一个探头模型的约定。默认值是 fiber-printed-relay 任务确认过的。/ One probe model's conventions; defaults from fiber-printed-relay."""

    object_height: float = 0.20          # 光纤中心 400 µm 圆的半径 / radius of the 400 µm centre circle of the fibre
    fiber_max_radius: float = 0.2375     # 规格表 450 ± 25 µm 的最大半径 / max radius of the 450 ± 25 µm datasheet value
    prism_half: float = 0.20
    prism_exit: str = "prism exit"
    tube_inner: str = "tube inner wall"
    tube_outer: str = "tube outer wall"
    lens_plane: str = "lens plane"
    lens_dome: str = "lens dome"
    wall_skip: tuple[str, ...] = ("prism entrance", "prism exit", "prism hypotenuse (ideal mirror)")
    excitation: int = W488
    emission: int = W532
    printed_prefix: str = "SAOS_"


DEFAULT = Layout()


class DesignFailure(ValueError):
    """该设计给不出有效测量值（几何不成立、没有实焦点、光线到不了样品）。check 返回 inf，记为不达标而非检查失败。"""


# ---- 基础 ----

def _once(ctx, key: tuple, compute: Callable[[], Any]) -> Any:
    """同一次判定内只算一次；DesignFailure 也记住，后面的判定直接得到同样的失败。
    Once per evaluation; a DesignFailure is remembered too, so later checks get the same failure."""
    def wrapped():
        try:
            return ("ok", compute())
        except DesignFailure as exc:
            return ("fail", exc)

    kind, value = kit.memo(ctx, ("saos.recipes.fiber",) + key, wrapped)
    if kind == "fail":
        raise value
    return value


def surface_index(ctx, comment: str, required: bool = True) -> int | None:
    names = kit.surfaces_by_comment(ctx)
    if comment in names:
        return names[comment]
    if required:
        raise ValueError(f"lens has no surface commented {comment!r}")
    return None


def trace(ctx, rays, surface=None) -> list[tuple[float, ...] | None]:
    """批量追迹实际光线到 surface（默认像面）。rays: (wave, hx, hy, px, py)。失败或渐晕返回 None，否则 (x,y,z,l,m,n)。"""
    r = kit.trace(ctx, rays, surface)
    ok = r.ok
    return [(float(r.x[k]), float(r.y[k]), float(r.z[k]), float(r.l[k]), float(r.m[k]), float(r.n[k])) if ok[k] else None
            for k in range(len(ok))]


def pupil_grid(n: int = GRID) -> list[tuple[float, float]]:
    return kit.pupil(n)


def _field_hits_raw(ctx, fields, wave, n):
    grid = pupil_grid(n)
    hits = trace(ctx, [(wave, hx, hy, px, py) for hx, hy in fields for px, py in grid])
    per = len(grid)
    return {f: [h for h in hits[k * per:(k + 1) * per] if h] for k, f in enumerate(fields)}, per


def field_hits(ctx, fields, wave=W532, n=GRID):
    """各视场到达样品面的光线；同一次判定内相同参数只追迹一次。/ Rays reaching the sample per field, traced once per evaluation."""
    fields = tuple(tuple(float(v) for v in f) for f in fields)
    return _once(ctx, ("field_hits", fields, int(wave), int(n)), lambda: _field_hits_raw(ctx, fields, wave, n))


def _arrays(hits, extrapolate: bool):
    """到达光线的 x、y 与斜率 l/n、m/n。要沿 z 外推时去掉 |n| ≤ 1e-12 的光线（几乎平行于样品面，外推不了；与 kit.spot 相同）。
    x, y and slopes l/n, m/n of the arriving rays; for extrapolation along z rays with |n| <= 1e-12 are dropped, as in kit.spot."""
    bad = TypeError("hits must be (x, y, z, l, m, n) rows: a sequence of 6-number tuples")
    try:
        if len(hits) == 0:   # 没有光线：交给调用方判为 DesignFailure / no rays: the callers turn this into DesignFailure
            return (np.empty(0),) * 4
        raw = np.array(hits)   # 不带 dtype：参差、None、字符串行不会被悄悄转成数字 / no dtype, so ragged, None or string rows are not coerced
    except (TypeError, ValueError) as e:
        raise bad from e
    if raw.ndim != 2 or raw.shape[1] != 6 or raw.dtype.kind not in "iuf":
        raise bad
    a = raw.astype(float)
    if extrapolate:
        a = a[np.abs(a[:, 5]) > 1e-12]
    with np.errstate(divide="ignore", invalid="ignore"):
        return a[:, 0], a[:, 1], a[:, 3] / a[:, 5], a[:, 4] / a[:, 5]


def _best_focus(hits) -> float:
    """像方是均匀介质，光线是直线：沿局部 z 平移 dz 后弥散斑 RMS 最小的 dz（闭式解，kit.focus_shift）。
    平行光或可用光线不足 3 条时没有焦点，记为设计给不出有效值（原先返回 0，把焦点当成在样品面上）。"""
    dz = kit.focus_shift(*_arrays(hits, True))
    if dz is None:
        raise DesignFailure("the rays reaching the sample are parallel (or fewer than 3 usable): there is no focus")
    if not math.isfinite(dz):   # 坐标含 NaN/inf 时闭式解会给出非有限值 / NaN or inf coordinates give a non-finite shift
        raise DesignFailure("the traced rays have no valid focus (non-finite focus shift)")
    return dz


def _rms(hits, dz: float = 0.0) -> float:
    x, y, u, v = _arrays(hits, dz != 0.0)
    if len(x) == 0:   # 空输入或外推时全部光线被滤掉 / empty input, or every ray dropped before extrapolating
        raise DesignFailure("no ray reaching the sample can be used (none arrive, or all are parallel to the sample plane)")
    r = kit.spread(x + u * dz, y + v * dz)["rms_radius"] if dz != 0.0 else kit.spread(x, y)["rms_radius"]
    if not math.isfinite(r):   # 坐标或 dz 含 NaN/inf / NaN or inf coordinates, or a non-finite dz
        raise DesignFailure("the traced rays give a non-finite RMS spot radius (NaN/inf coordinates or focus shift)")
    return r


def _centroid(h):
    x, y, _, _ = _arrays(h, False)
    if len(x) == 0:
        raise DesignFailure("no ray reaches the sample: the centroid is undefined")
    c = kit.spread(x, y)["centroid"]
    if not (math.isfinite(c[0]) and math.isfinite(c[1])):
        raise DesignFailure("the traced rays give a non-finite centroid (NaN/inf coordinates)")
    return c[0], c[1]


def sample_distance(ctx, layout: Layout = DEFAULT) -> float:
    """样品面到外壁的前进距离（折转一次后厚度为负）。"""
    return -float(ctx.system.LDE.GetSurfaceAt(surface_index(ctx, layout.tube_outer)).Thickness)


# ---- 几何前提 ----

def _is_cb(s) -> bool:
    return "Coordinate" in str(s.TypeName)


def _geometry(ctx, layout: Layout) -> bool:
    lde = ctx.system.LDE
    inner = surface_index(ctx, layout.tube_inner)
    # 厚度方向：折转（理想反射面）之前沿 +z，之后沿 -z；反向的厚度表示零件长回前一个零件里
    mirror = next(i for i in range(lde.NumberOfSurfaces) if str(lde.GetSurfaceAt(i).Material).upper() == "MIRROR")
    for i in range(0, lde.NumberOfSurfaces - 1):
        s = lde.GetSurfaceAt(i)
        if _is_cb(s):
            continue
        t = float(s.Thickness)
        if (i < mirror and t < -1e-6) or (i > mirror and t > 1e-6):   # 容差 1 nm
            raise DesignFailure(f"surface {i} ({s.Comment}) has thickness {t * 1000:.1f} um against the direction of propagation")
    # 面在其半口径内必须有定义（例如球面半口径不能超过半径）
    for i in range(1, inner + 1):
        s = lde.GetSurfaceAt(i)
        if _is_cb(s):
            continue
        sd = float(s.SemiDiameter)
        for x, y in ((sd, 0.0), (0.0, sd), (-sd, 0.0), (0.0, -sd)):
            if not math.isfinite(kit.op(ctx, "SSAG", i, 0, x, y)):
                raise DesignFailure(f"surface {i} ({s.Comment}) is undefined at its semi-diameter {sd * 1000:.1f} um")
        r = float(s.Radius)
        if str(s.TypeName) == "Standard" and math.isfinite(r) and r != 0 and abs(float(s.Conic)) < 1e-12 and sd > abs(r) + 1e-9:
            raise DesignFailure(f"surface {i} ({s.Comment}) semi-diameter {sd * 1000:.1f} um exceeds its radius {abs(r) * 1000:.1f} um")
    for i in range(0, inner - 1):
        s, nxt = lde.GetSurfaceAt(i), lde.GetSurfaceAt(i + 1)
        t = float(s.Thickness)
        if abs(t) < 1e-9 or _is_cb(s) or _is_cb(nxt) or str(s.Material).upper() in ("N-BK7", "MIRROR") or i + 1 == inner:
            continue
        e = kit.op(ctx, "ETVA", i, 0)
        if not math.isfinite(e) or e * (1 if t > 0 else -1) < 0:
            raise DesignFailure(f"edge thickness after surface {i} ({s.Comment}) is {e * 1000:.1f} um against centre {t * 1000:.1f} um")
    return True


def geometry_ok(ctx, layout: Layout = DEFAULT) -> bool:
    """各透镜与间隙的边缘厚度（ETVA）须与中心厚度同号：曲面不穿过贴合面、间隙不交叠；跳过坐标断点、零厚度行、棱镜内部与
    紧邻管内壁的行（管壁间隙由 wall_clearance 按实际矢高检查）。同一次判定内只查一次。"""
    return _once(ctx, ("geometry", layout), lambda: _geometry(ctx, layout))


def part_radius(s) -> float:
    """零件实际的半径：面上定义了圆形孔径时取孔径半径，否则取半口径。"""
    try:
        if "Circular" in str(s.ApertureData.CurrentType):
            return float(s.ApertureData.CurrentTypeSettings._S_CircularAperture.MaximumRadius)
    except Exception:  # noqa: BLE001 - 读不到孔径设置时按半口径
        pass
    return float(s.SemiDiameter)


def _global(ctx, i) -> list[float]:
    rot, shift = kit.global_matrix(ctx, i)
    return [float(v) for v in rot.reshape(-1)] + [float(v) for v in shift]


def _wall_clearance(ctx, layout: Layout) -> float:
    geometry_ok(ctx, layout)
    lde = ctx.system.LDE
    o = _global(ctx, 0)[9:12]
    p1 = _global(ctx, 1)[9:12]
    axis = [p1[c] - o[c] for c in range(3)]
    norm = math.sqrt(sum(c * c for c in axis))
    axis = [c / norm for c in axis]

    def radial(p):
        v = [p[c] - o[c] for c in range(3)]
        along = sum(v[c] * axis[c] for c in range(3))
        return math.sqrt(max(0.0, sum(c * c for c in v) - along * along))

    inner = surface_index(ctx, layout.tube_inner)
    r_in = abs(float(lde.GetSurfaceAt(inner).Radius))
    if abs(radial(_global(ctx, inner)[9:12]) - r_in) > 1e-6:
        raise ValueError("tube inner wall vertex is not on the tube radius")
    far = layout.fiber_max_radius
    d_exit = radial(_global(ctx, surface_index(ctx, layout.prism_exit))[9:12])
    far = max(far, math.hypot(d_exit, layout.prism_half), math.hypot(d_exit - 2 * layout.prism_half, layout.prism_half))
    for i in range(1, inner):
        s = lde.GetSurfaceAt(i)
        if _is_cb(s) or str(s.Comment) in layout.wall_skip or str(s.Material).upper() == "MIRROR":
            continue
        tr = _global(ctx, i)
        sd = part_radius(s)
        for ring in range(21):
            r = sd * ring / 20
            for k in range(36 if ring else 1):
                a = 2 * math.pi * k / 36
                x, y = r * math.cos(a), r * math.sin(a)
                z = kit.op(ctx, "SSAG", i, 0, x, y)
                # 取不到矢高的点原先被 max 悄悄跳过（NaN）；前提不成立应判无效 / an undefined sag was silently skipped by max; it voids the check
                if not math.isfinite(z):
                    raise DesignFailure(f"surface {i} ({s.Comment}) is undefined at r = {r * 1000:.1f} um inside its aperture")
                p = [tr[9 + j] + tr[3 * j] * x + tr[3 * j + 1] * y + tr[3 * j + 2] * z for j in range(3)]
                far = max(far, radial(p))
    clearance = (r_in - far) * 1000
    if clearance < 0:
        raise DesignFailure(f"a part extends {-clearance:.2f} um into the tube wall")
    return clearance


def wall_clearance_um(ctx, layout: Layout = DEFAULT) -> float:
    """所有透镜面、光纤与棱镜到管内壁的最小间隙。管轴过光纤端面中心、沿第 0→1 面方向；透镜面在其半口径内按极坐标网格取
    实际矢高（SSAG）变换到全局坐标，棱镜按方截面的角点。"""
    return _once(ctx, ("wall", layout), lambda: _wall_clearance(ctx, layout))


# ---- 材料与底径 ----

def lens_index(ctx, layout: Layout = DEFAULT) -> float:
    lde = ctx.system.LDE
    rows = [i for i in range(lde.NumberOfSurfaces)
            if str(lde.GetSurfaceAt(i).Material).upper().startswith(layout.printed_prefix)
            or str(lde.GetSurfaceAt(i).MaterialCell.GetSolveData().Type) == "MaterialModel"]
    if not rows:
        raise ValueError("no printed lens or glue material found")
    return max(kit.material_nd(ctx, i) for i in rows)


def lens_base_um(ctx, layout: Layout = DEFAULT) -> float:
    """棱镜出射面上透镜的底径：贴合面与穹顶实际半径（part_radius）的较大者 × 2；没有该透镜时为 0。"""
    lde = ctx.system.LDE
    i = surface_index(ctx, layout.lens_plane, required=False)
    if i is None or abs(float(lde.GetSurfaceAt(i).Thickness)) < 1e-9:
        return 0.0
    j = surface_index(ctx, layout.lens_dome)
    return 2 * max(part_radius(lde.GetSurfaceAt(i)), part_radius(lde.GetSurfaceAt(j))) * 1000


# ---- 焦点、光斑、通过率、视场 ----

def axis_hits(ctx, wave: int, n: int = GRID) -> list[tuple[float, ...]]:
    """轴上到达样品面的光线；焦点位置和最佳焦面共用，同一次判定只追迹一次。/ On-axis rays, shared by focus and best-focus."""
    return _once(ctx, ("axis", int(wave), int(n)),
                 lambda: [h for h in trace(ctx, [(wave, 0.0, 0.0, px, py) for px, py in pupil_grid(n)]) if h])


def _focus_positions(ctx, layout: Layout, n: int) -> dict[int, float]:
    out = {}
    for wave in (layout.excitation, layout.emission):
        hits = axis_hits(ctx, wave, n)
        if len(hits) < 8:
            raise DesignFailure(f"too few on-axis rays reach the sample at wave {wave}: {len(hits)}")
        # 折转一次后沿局部 -z 前进
        out[wave] = sample_distance(ctx, layout) - _best_focus(hits)
    return out


def focus_positions(ctx, layout: Layout = DEFAULT, n: int = GRID) -> dict[int, float]:
    """轴上激发、发射波长最佳焦点到外壁的前进距离（mm，正值在组织中、为实焦点）。"""
    return _once(ctx, ("focus", layout, n), lambda: _focus_positions(ctx, layout, n))


def _best_focus_dz(ctx, layout: Layout) -> float:
    hits = axis_hits(ctx, layout.emission)
    if len(hits) < 8:
        raise DesignFailure("too few on-axis rays reach the sample")
    dz = _best_focus(hits)
    if sample_distance(ctx, layout) - dz <= 0:
        raise DesignFailure("no real 532 nm focus outside the tube")
    return dz


def best_focus_dz(ctx, layout: Layout = DEFAULT) -> float:
    """发射波长轴上最佳焦面相对样品面的局部 z 平移量，要求是实焦点。"""
    return _once(ctx, ("best_dz", layout), lambda: _best_focus_dz(ctx, layout))


def working_distance_um(ctx, layout: Layout = DEFAULT) -> float:
    geometry_ok(ctx, layout)
    f = focus_positions(ctx, layout)[layout.emission] * 1000
    if f <= 0:
        raise DesignFailure(f"532 nm focus is {f:.1f} um inside the outer wall (virtual in tissue)")
    return f


def focus_shift_um(ctx, layout: Layout = DEFAULT) -> float:
    geometry_ok(ctx, layout)
    f = focus_positions(ctx, layout)
    if min(f.values()) <= 0:
        raise DesignFailure("a focus is virtual")
    return abs(f[layout.excitation] - f[layout.emission]) * 1000


def spot_fields_um(ctx, fields, layout: Layout = DEFAULT) -> float:
    """所列视场发射波长 RMS 直径的最大值，在轴上最佳焦面测量（与 PSF 同一像面）；任一视场光线不足 4 条即无效。
    光斑按到达的全部光线计，截光比例由 pass_rate 报告。"""
    geometry_ok(ctx, layout)
    dz = best_focus_dz(ctx, layout)
    hits, _ = field_hits(ctx, fields, layout.emission)
    missing = [f for f, h in hits.items() if len(h) < 4]
    if missing:
        raise DesignFailure(f"too few rays reach the sample at fields {missing}")
    return max(2 * _rms(h, dz) * 1000 for h in hits.values())


def pass_rate_pct(ctx, layout: Layout = DEFAULT) -> float:
    """轴上与 0.7 视场（5 个视场，21×21 光瞳网格，发射波长）到达样品面的光线比例，取最差；失败、渐晕、全反射都算未通过。"""
    geometry_ok(ctx, layout)
    hits, per = field_hits(ctx, FIELDS5, layout.emission)
    return 100.0 * min(len(h) for h in hits.values()) / per


def _fov(ctx, layout: Layout) -> float:
    geometry_ok(ctx, layout)
    axis, _ = field_hits(ctx, [(0.0, 0.0)], layout.emission, n=11)
    if len(axis[(0.0, 0.0)]) < 4:
        raise DesignFailure("too few on-axis rays reach the sample")
    ax = _centroid(axis[(0.0, 0.0)])
    radii = []
    for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        fields = [(dx * s / 20, dy * s / 20) for s in range(20, 0, -1)]
        hits, _ = field_hits(ctx, fields, layout.emission, n=11)
        r = 0.0
        for f in fields:
            f = (float(f[0]), float(f[1]))
            if len(hits[f]) >= 4:
                cx, cy = _centroid(hits[f])
                r = math.hypot(cx - ax[0], cy - ax[1])
                break
        radii.append(r)
    return 2 * min(radii) * 1000


def fov_um(ctx, layout: Layout = DEFAULT) -> float:
    """光纤芯圆在样品面上的像：四个方向边缘像点质心相对轴上质心的最小距离 × 2；边缘点没有光线通过时，沿该方向找能通过的最大物高。"""
    return _once(ctx, ("fov", layout), lambda: _fov(ctx, layout))


def fov_resolved_um(ctx, limit_um: float = 3.7, layout: Layout = DEFAULT) -> float:
    """RMS 直径 ≤ limit 的视场直径：沿 ±X、±Y 每 0.05 视场一步，在轴上最佳焦面测量；轴上不达标为 0。只报告。"""
    geometry_ok(ctx, layout)
    dz = best_focus_dz(ctx, layout)
    axis, _ = field_hits(ctx, [(0.0, 0.0)], layout.emission)
    a = axis[(0.0, 0.0)]
    if len(a) < 4 or 2 * _rms(a, dz) * 1000 > limit_um:
        return 0.0
    ax = _centroid(a)
    radii = []
    for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        fields = [(float(dx * s / 20), float(dy * s / 20)) for s in range(1, 21)]
        hits, _ = field_hits(ctx, fields, layout.emission)
        r = 0.0
        for f in fields:
            h = hits[f]
            if len(h) < 4 or 2 * _rms(h, dz) * 1000 > limit_um:
                break
            cx, cy = _centroid(h)
            r = math.hypot(cx - ax[0], cy - ax[1])
        radii.append(r)
    return 2 * min(radii) * 1000


def _collection(ctx, fields, n: int, layout: Layout) -> float:
    geometry_ok(ctx, layout)
    # 只追单位圆内的点：圆外的点原先也追，但随后就丢掉了；坐标与 kit.pupil 逐位相同，各光线独立追迹
    # trace only points inside the unit circle: outer ones were traced and then dropped; same coordinates as kit.pupil
    coords = kit.pupil(n)
    step = 2 / (n - 1)
    fractions = []
    for hx, hy in fields:
        hits = trace(ctx, [(layout.emission, hx, hy, px, py) for px, py in coords])
        dirs = {}
        for (px, py), h in zip(coords, hits):
            if h and px * px + py * py <= 1 + 1e-12:
                dirs[(round(px, 9), round(py, 9))] = h[3:6]
        omega = 0.0
        for i in range(n - 1):
            for j in range(n - 1):
                keys = [(round(-1 + step * (i + di), 9), round(-1 + step * (j + dj), 9)) for di, dj in ((0, 0), (1, 0), (1, 1), (0, 1))]
                if all(k in dirs for k in keys):
                    v = [dirs[k] for k in keys]
                    d1 = [v[2][c] - v[0][c] for c in range(3)]
                    d2 = [v[3][c] - v[1][c] for c in range(3)]
                    cross = (d1[1] * d2[2] - d1[2] * d2[1], d1[2] * d2[0] - d1[0] * d2[2], d1[0] * d2[1] - d1[1] * d2[0])
                    omega += 0.5 * math.sqrt(sum(c * c for c in cross))
        fractions.append(omega / (4 * math.pi))
    return sum(fractions) / len(fractions)


def collection(ctx, fields, n: int = 61, layout: Layout = DEFAULT) -> float:
    """几何收集比例：按光路可逆，样品点能送回光纤（物方 NA、端面圆内）的方向就是正向光束在样品侧覆盖的方向。
    光瞳网格上四角都通过的格子，把像方单位方向向量围成的四边形面积累加为立体角，除以 4π；多个物点取平均。"""
    fields = tuple(tuple(float(v) for v in f) for f in fields)
    return _once(ctx, ("collection", fields, n, layout), lambda: _collection(ctx, fields, n, layout))


# ---- Huygens PSF（发射波长，样品面移到轴上最佳焦面） ----

class _Temporary:
    """测量期间临时改动样品面位置与视场表，退出时逐项恢复（检查只读）。期间不用 field_hits 的缓存。"""

    def __init__(self, ctx, layout: Layout):
        self.ctx = ctx
        self.layout = layout
        self.outer = ctx.system.LDE.GetSurfaceAt(surface_index(ctx, layout.tube_outer))
        self.t0 = float(self.outer.Thickness)
        self.fields = ctx.system.SystemData.Fields
        self.n0 = int(self.fields.NumberOfFields)

    def __enter__(self):
        return self

    def set_plane(self, forward_from_wall_mm: float) -> None:
        self.outer.Thickness = -float(forward_from_wall_mm)

    def field_number(self, hx: float, hy: float) -> int:
        """在视场表末尾追加物高 (h·hx, h·hy) 的视场，返回其编号。"""
        f = self.fields.AddField(self.layout.object_height * hx, self.layout.object_height * hy, 1.0)
        return int(f.FieldNumber)

    def __exit__(self, *exc):
        while int(self.fields.NumberOfFields) > self.n0:
            self.fields.RemoveField(int(self.fields.NumberOfFields))
        self.outer.Thickness = self.t0
        return False


class _Huygens:
    """整个测量只开一个 Huygens PSF 分析，换视场、像元间距或像面后重新计算。
    extension 模式下每次新建分析都会在界面开关一个窗口，连续上百次会使 OpticStudio 崩溃（2026-10-16 实测）。"""

    def __init__(self, ctx, wave: int):
        self.run = ctx.system.Analyses.New_Analysis(ctx.zosapi.Analysis.AnalysisIDM.HuygensPsf)
        s = self.run.GetSettings()
        self.s = getattr(s, "__implementation__", s)
        self.s.Wavelength.SetWavelengthNumber(wave)
        self.s.PupilSampleSize = getattr(type(self.s.PupilSampleSize), PSF_PUPIL)
        self.s.ImageSampleSize = getattr(type(self.s.ImageSampleSize), PSF_IMAGE)
        self.s.Normalize = False

    def __enter__(self):
        return self

    def grid(self, field_no: int, delta_um: float) -> dict[str, Any]:
        self.s.Field.SetFieldNumber(int(field_no))
        self.s.ImageDelta = float(delta_um)
        self.run.ApplyAndWaitForCompletion()
        r = self.run.GetResults()
        if r is None or int(r.NumberOfDataGrids) < 1:
            raise DesignFailure("Huygens PSF returned no data")
        g = r.GetDataGrid(0)
        nx, ny, values = int(g.Nx), int(g.Ny), g.Values
        rows = [[float(values[iy, ix]) for ix in range(nx)] for iy in range(ny)]
        return {"values": rows, "dx": float(g.Dx), "dy": float(g.Dy), "step": 1}

    def __exit__(self, *exc):
        self.run.Close()
        return False


def _fwhm_1d(values, step: float) -> float:
    peak = max(values)
    if not peak > 0:
        raise DesignFailure("PSF has no positive peak")
    k = values.index(peak)
    half = peak / 2
    left = right = None
    for i in range(k, 0, -1):
        if values[i - 1] < half <= values[i]:
            left = (i - 1) + (half - values[i - 1]) / (values[i] - values[i - 1])
            break
    for i in range(k, len(values) - 1):
        if values[i + 1] < half <= values[i]:
            right = i + (values[i] - half) / (values[i] - values[i + 1])
            break
    if left is None or right is None:
        raise DesignFailure("PSF half maximum not inside the sampled window")
    return (right - left) * step


def _delta_for(ctx, hx: float, hy: float, dz: float, layout: Layout) -> float:
    """像元间距：让 128 点窗口约为该视场几何光斑 RMS 直径的 4 倍，最小 0.05 µm。在临时像面上追迹，不用缓存。"""
    hits, _ = _field_hits_raw(ctx, [(hx, hy)], layout.emission, GRID)
    h = hits[(hx, hy)]
    if len(h) < 4:
        raise DesignFailure(f"too few rays reach the sample at field {(hx, hy)}")
    return max(0.05, 4 * 2 * _rms(h, dz) * 1000 / 128)


def _psf_widths(ctx, layout: Layout) -> list[dict[str, Any]]:
    geometry_ok(ctx, layout)
    dz = best_focus_dz(ctx, layout)
    focus = sample_distance(ctx, layout) - dz
    out = []
    with _Temporary(ctx, layout) as tmp, _Huygens(ctx, layout.emission) as hug:
        tmp.set_plane(focus)
        for hx, hy in FIELDS5:
            delta = _delta_for(ctx, hx, hy, 0.0, layout)
            g = hug.grid(tmp.field_number(hx, hy), delta)
            rows = [[v or 0.0 for v in row] for row in g["values"]]
            dx, dy = g["dx"] * g["step"], g["dy"] * g["step"]
            iy, ix = max(((r, c) for r in range(len(rows)) for c in range(len(rows[0]))), key=lambda rc: rows[rc[0]][rc[1]])
            fx = _fwhm_1d(rows[iy], dx)
            fy = _fwhm_1d([row[ix] for row in rows], dy)
            total = sum(map(sum, rows))
            cx = sum(v * c for row in rows for c, v in enumerate(row)) / total
            cy = sum(v * r for r, row in enumerate(rows) for v in row) / total
            pix = sorted(((math.hypot((c - cx) * dx, (r - cy) * dy), v) for r, row in enumerate(rows) for c, v in enumerate(row)))
            acc, ee80 = 0.0, None
            for rad, v in pix:
                acc += v
                if acc >= 0.8 * total:
                    ee80 = 2 * rad
                    break
            out.append({"field": (hx, hy), "fwhm_x": fx, "fwhm_y": fy, "ee80": ee80, "delta_um": delta})
    return out


def psf_widths_um(ctx, layout: Layout = DEFAULT) -> list[dict[str, Any]]:
    """5 个视场的 X、Y 半高全宽与 80% 包围能量直径（µm），在发射波长轴上最佳焦面计算 Huygens PSF。psf_x、psf_y、ee80 共用一次计算。"""
    return _once(ctx, ("psf", layout), lambda: _psf_widths(ctx, layout))


def psf_z_um(ctx, step_um: float = 4.0, half_range_um: float = 60.0, layout: Layout = DEFAULT) -> float:
    """各视场 Huygens PSF 峰值随像面离焦的曲线的半高全宽（µm），5 个视场取最差。离焦面相对轴上最佳焦面，像元间距按最佳焦面定且
    各离焦面相同；离焦面不得进入外壁以内。曲线两侧都须降到峰值一半以下，否则扩大一次范围，仍不够则无效。
    在后台进程里时各视场分给 worker 池并行（_psf_z_parallel，池没开就启动），池忙或 worker 没算成的视场在主实例里串行算。"""
    geometry_ok(ctx, layout)
    dz = best_focus_dz(ctx, layout)
    focus = sample_distance(ctx, layout) - dz
    widths = _psf_z_parallel(ctx, focus, step_um, half_range_um) if layout == DEFAULT else {}
    worst = max(widths.values(), default=0.0)
    rest = [f for f in FIELDS5 if f not in widths]
    if rest:
        with _Temporary(ctx, layout) as tmp, _Huygens(ctx, layout.emission) as hug:
            for hx, hy in rest:
                worst = max(worst, _psf_z_field(ctx, tmp, hug, hx, hy, focus, step_um, half_range_um, layout))
    return worst


def _psf_z_field(ctx, tmp: _Temporary, hug: _Huygens, hx: float, hy: float, focus: float, step_um: float, half_range_um: float,
                 layout: Layout) -> float:
    """一个视场的离焦曲线半高全宽（µm）。/ The axial FWHM of one field (µm)."""
    tmp.set_plane(focus)
    delta = _delta_for(ctx, hx, hy, 0.0, layout)
    fno = tmp.field_number(hx, hy)
    cache: dict[int, float] = {}

    def peak_at(k: int) -> float:
        if k not in cache:
            plane = focus + k * step_um / 1000
            if plane <= 0:
                cache[k] = 0.0
            else:
                tmp.set_plane(plane)
                g = hug.grid(fno, delta)
                cache[k] = max(v or 0.0 for row in g["values"] for v in row)
        return cache[k]

    for half in (half_range_um, 2 * half_range_um):
        try:
            # 窗口内全部取值，与原定义相同；扩到 ±2×half 时已算过的离焦面直接复用
            # every plane in the window, as before; widening reuses the planes already computed
            reach = int(half / step_um)
            return _fwhm_1d([peak_at(k) for k in range(-reach, reach + 1)], step_um)
        except DesignFailure:
            if half != half_range_um:
                raise
    raise AssertionError("unreachable")


# 自动开池时给用户留下的许可证数（他们自己的窗口、别的实例）；凑不出这么多个 worker 就不并行
# licences left free for the user (their windows, other instances) when the pool starts itself; fewer workers than this: serial
PSF_Z_KEEP_FREE = 2
PSF_Z_MIN_WORKERS = 2


def _psf_z_parallel(ctx, focus: float, step_um: float, half_range_um: float) -> dict[tuple[float, float], float]:
    """把 FIELDS5 分给后台进程的 worker 池（没开就按需启动；新开 worker 前给用户留 PSF_Z_KEEP_FREE 个许可证，凑不出 2 个 worker 就串行），返回算成的各视场半高全宽；任一视场
    设计失败就抛 DesignFailure，worker 没算成的视场不在返回值里，由调用方串行补算。池正被分离扫描占用或不在后台进程里时返回空。
    worker 拿到的是主实例当前内存状态的快照（CopySystem 另存），所以未保存的修改、rebase 时装着的归档镜头都照样测。
    Share FIELDS5 out to the daemon's worker pool, starting it if needed while leaving PSF_Z_KEEP_FREE licences free (serial when fewer than 2 workers fit), and return
    the widths it got; a design failure in any field raises DesignFailure, fields no worker finished are left out for the
    caller to compute serially. Empty when the pool is busy with a detached scan or there is no daemon. Workers get a
    snapshot of the main instance's state in memory (CopySystem, saved aside), so unsaved changes and the archived lens
    of a rebase are measured as they are."""
    from saos import client, config, parallel

    pool = getattr(getattr(ctx, "_daemon", None), "pool", None)
    if pool is None:
        return {}
    # 已开着的 worker 照用；新开的只用许可证余量减去留给用户的部分，凑不出 2 个 worker 就串行
    # workers already up are used; new ones only from the free licences minus the share kept for the user; fewer than 2: serial
    alive = sum(1 for w in pool.workers if w.alive)
    free = config.LICENSED_INSTANCES - len(client.process_pids("OpticStudio.exe"))
    count = min(len(FIELDS5), config.compute_cores(), alive + max(0, free - PSF_Z_KEEP_FREE))
    if count < PSF_Z_MIN_WORKERS or not pool.lock.acquire(blocking=False):
        return {}
    folder: Path | None = None
    try:
        # 取得锁之后的每一步都在 try 里，出任何错都会释放锁，池不会一直被占着
        # everything after taking the lock sits in the try, so any failure releases it and the pool is never left held
        folder = Path(tempfile.mkdtemp(prefix="saos-psfz-"))
        snapshot = folder / "lens.zmx"
        try:
            copy = ctx.system.CopySystem()
            try:
                copy.SaveAs(str(snapshot))
            finally:
                copy.Close(False)
        except Exception:  # noqa: BLE001 - 存不出快照就在主实例串行 / no snapshot: measure serially in the main instance
            return {}
        if not snapshot.is_file():
            return {}
        items = [{"hx": hx, "hy": hy, "focus": focus, "step": step_um, "half": half_range_um} for hx, hy in FIELDS5]
        out = parallel.dispatch(pool, ctx.job, snapshot, "saos.recipes.fiber.side_view:psf_z_field", None, items, count, True, None,
                                progress=lambda stage: ctx.job.write_progress(stage=f"psf_z {stage}"), cancelled=lambda: False)
    finally:
        pool.lock.release()
        if folder is not None:
            shutil.rmtree(folder, ignore_errors=True)
    widths: dict[tuple[float, float], float] = {}
    for i, field in enumerate(FIELDS5):
        error = out["errors"].get(str(i))
        if error and "DesignFailure:" in error:
            raise DesignFailure(error.split("DesignFailure:", 1)[1].strip())
        if error is None and i not in out["not_run"] and out["results"][i] is not None:
            widths[field] = float(out["results"][i])
    return widths


def psf_z_field(w, item: dict[str, Any]) -> float:
    """worker 里测一个视场的 psf_z（由 _psf_z_parallel 分派，只用于默认 Layout）。
    One field of psf_z inside a worker (dispatched by _psf_z_parallel, default Layout only)."""
    with _Temporary(w, DEFAULT) as tmp, _Huygens(w, DEFAULT.emission) as hug:
        return _psf_z_field(w, tmp, hug, float(item["hx"]), float(item["hy"]), float(item["focus"]), float(item["step"]),
                            float(item["half"]), DEFAULT)


def wfe_rms_waves(ctx, layout: Layout = DEFAULT) -> float:
    """发射波长 RMS 波前误差（波长），5 个视场取最差：Zernike 标准系数分析文本中 "From integration of the rays" 下的
    RMS (to centroid)（扣除平移与倾斜），像面移到轴上最佳焦面。"""
    geometry_ok(ctx, layout)
    dz = best_focus_dz(ctx, layout)
    focus = sample_distance(ctx, layout) - dz
    worst = 0.0
    with _Temporary(ctx, layout) as tmp:
        tmp.set_plane(focus)
        for hx, hy in FIELDS5:
            # 读完就删；OpticStudio 偶尔还占着文件时不让清理失败打断测量 / removed once read; a file OpticStudio still holds must not fail the check
            with tempfile.TemporaryDirectory(prefix="saos-wfe-", ignore_cleanup_errors=True) as folder:
                text = Path(folder) / "z.txt"
                ctx.backend.analysis("ZernikeStandardCoefficients", {"Field": tmp.field_number(hx, hy), "Wavelength": layout.emission}, text)
                lines = text.read_text(encoding="utf-16", errors="ignore").splitlines()
            v = None
            for k, line in enumerate(lines):
                if line.startswith("From integration of the rays"):
                    for nxt in lines[k + 1:k + 4]:
                        if nxt.startswith("RMS (to centroid)"):
                            v = float(nxt.split(":")[1].split()[0])
                    break
            if v is None or not math.isfinite(v):
                raise DesignFailure(f"wavefront error not computable at field {(hx, hy)}")
            worst = max(worst, v)
    return worst


# ---- 判定入口 / check entry points ----

def guarded(fn: Callable[[Any], float]) -> Callable[[Any], float]:
    """check 的 run：设计无有效值时返回 inf。"""
    def run(ctx):
        try:
            v = fn(ctx)
        except DesignFailure:
            return math.inf
        return v if v is not None and math.isfinite(v) else math.inf
    return run


CHECKS: dict[str, Callable[..., float]] = {
    "fov": lambda ctx, lo: fov_um(ctx, lo),
    "working_distance": lambda ctx, lo: working_distance_um(ctx, lo),
    "focus_shift": lambda ctx, lo: focus_shift_um(ctx, lo),
    "lens_index": lambda ctx, lo: lens_index(ctx, lo),
    "lens_base": lambda ctx, lo: lens_base_um(ctx, lo),
    "wall_clearance": lambda ctx, lo: wall_clearance_um(ctx, lo),
    "spot": lambda ctx, lo: spot_fields_um(ctx, FIELDS5, lo),
    "spot_axis": lambda ctx, lo: spot_fields_um(ctx, [(0.0, 0.0)], lo),
    "spot_mid": lambda ctx, lo: spot_fields_um(ctx, MID_FIELDS, lo),
    "spot_edge": lambda ctx, lo: spot_fields_um(ctx, EDGE_FIELDS, lo),
    "pass_rate": lambda ctx, lo: pass_rate_pct(ctx, lo),
    "fov_resolved": lambda ctx, lo: fov_resolved_um(ctx, layout=lo),
    "psf_x": lambda ctx, lo: max(r["fwhm_x"] for r in psf_widths_um(ctx, lo)),
    "psf_y": lambda ctx, lo: max(r["fwhm_y"] for r in psf_widths_um(ctx, lo)),
    "ee80": lambda ctx, lo: max(r["ee80"] for r in psf_widths_um(ctx, lo)),
    "psf_z": lambda ctx, lo: psf_z_um(ctx, layout=lo),
    "wfe_rms": lambda ctx, lo: wfe_rms_waves(ctx, lo),
    "collect_center": lambda ctx, lo: collection(ctx, [(0.0, 0.0)], layout=lo),
    "collect_mid": lambda ctx, lo: collection(ctx, MID_FIELDS, layout=lo),
    "collect_edge": lambda ctx, lo: collection(ctx, EDGE_FIELDS, layout=lo),
}


def check(ctx, name: str, layout: Layout = DEFAULT) -> float:
    """按名字取一个判定量（见 CHECKS），设计无有效值时为 inf。/ One measurement by name (see CHECKS); inf when the design has no valid value."""
    if name not in CHECKS:
        raise ValueError(f"unknown fibre check {name!r}; known: {sorted(CHECKS)}")
    return guarded(lambda c: CHECKS[name](c, layout))(ctx)
