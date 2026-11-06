"""微光学系统的测量原语（光纤探头、内窥镜、光纤端面打印透镜、微型物镜）：与具体布局无关，判定脚本只写自己的布局约定
（面注释、折转、视场与波长），其余用这里的函数（2026-11-02 用户决定）。
Measurement primitives for micro-optical systems (fibre probes, endoscopes, lenses printed on fibre tips, miniature
objectives), independent of any layout; a check keeps only its layout conventions (comments, folds, fields, waves).

约定 / conventions:
- 只经 saos.kit 与 ctx 读写，不导入后端；同一次判定内反复用到的量经 once() 只算一次。
  Reads and writes through saos.kit and ctx only; quantities reused within one evaluation go through once().
- 物理上不成立的状态（没有实焦点、光线到不了、面在口径内没有定义、零件伸进外壳）抛 DesignFailure，guarded() 把它变成 inf，
  记为不达标而不是检查出错（DESIGN.md「判定只读且物理自洽」）。
  Physically invalid states raise DesignFailure; guarded() turns it into inf, a failed item rather than a check error.
- 调用错误（采样数不是足够大的整数、面序号越界、没有后一面、空视场表）报 BAD_ARGS，guarded() 不拦截，检查本身出错。
  Caller errors (a sampling count too small or not an integer, a surface out of range, no following surface, no
  fields) raise BAD_ARGS, which guarded() lets through: the check itself is wrong.
- 长度为镜头单位（mm），名字带 _um 的返回 µm。/ Lengths in lens units (mm); names ending in _um return µm.
- 几何光线量（焦点、RMS、收集比例）不含衍射；微光学里菲涅耳数小、几何焦点和衍射焦点可以差得远，决定性的量用 Huygens PSF 复核。
  Geometric quantities ignore diffraction; at small Fresnel numbers the geometric and diffraction foci can differ a lot,
  so confirm deciding numbers with the Huygens PSF.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

import numpy as np

from saos import geometry, kit
from saos.protocol import ErrorCode, SaosError

GRID = 21
PSF_PUPIL = 64    # Huygens 光瞳采样 64×64 / pupil sampling
PSF_IMAGE = 128   # 像面采样 128×128 / image sampling
# 自动开池时给用户留下的许可证数（他们自己的窗口、别的实例）；凑不出这么多个 worker 就不并行
# licences left free for the user (their windows, other instances) when the pool starts itself; fewer workers: serial
POOL_KEEP_FREE = 2
POOL_MIN_WORKERS = 2


def _count(name: str, value: Any, least: int) -> int:
    """采样数等调用参数：须为不小于 least 的整数，否则 BAD_ARGS——调用错误不是设计失败，guarded 不能把它变成 inf。
    A sampling count must be an integer >= least, else BAD_ARGS: a caller error, never turned into inf by guarded."""
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)) or value < least:
        raise SaosError(ErrorCode.BAD_ARGS, f"{name} must be an integer >= {least}, got {value!r}")
    return int(value)


class DesignFailure(ValueError):
    """该设计给不出有效测量值（几何不成立、没有实焦点、光线到不了样品）。check 返回 inf，记为不达标而非检查失败。
    The design has no valid value here (invalid geometry, no real focus, no ray reaching the sample); the check gives inf."""


# ---- 判定基础 / evaluation basics ----

def once(ctx, key: tuple, compute: Callable[[], Any]) -> Any:
    """同一次判定内只算一次（ctx.memo）；DesignFailure 也记住，后面的判定直接得到同样的失败。key 由调用方带上自己的命名空间。
    Once per evaluation (ctx.memo); a DesignFailure is remembered too. The caller puts its own namespace in key."""
    def wrapped():
        try:
            return ("ok", compute())
        except DesignFailure as exc:
            return ("fail", exc)

    kind, value = kit.memo(ctx, key, wrapped)
    if kind == "fail":
        raise value
    return value


def guarded(fn: Callable[[Any], float]) -> Callable[[Any], float]:
    """check 的 run：设计无有效值（DesignFailure、None、非有限值）时返回 inf。/ A check's run: inf when the design has no valid value."""
    def run(ctx):
        try:
            v = fn(ctx)
        except DesignFailure:
            return math.inf
        return v if v is not None and math.isfinite(v) else math.inf
    return run


# ---- 光线 / rays ----

def trace(ctx, rays, surface=None) -> list[tuple[float, ...] | None]:
    """批量追迹实际光线到 surface（默认像面）。rays: (wave, hx, hy, px, py)。失败或渐晕为 None，否则 (x, y, z, l, m, n)。
    Batch real-ray trace to surface (default the image); None for a failed or vignetted ray, else (x, y, z, l, m, n)."""
    r = kit.trace(ctx, rays, surface)
    ok = r.ok
    return [(float(r.x[k]), float(r.y[k]), float(r.z[k]), float(r.l[k]), float(r.m[k]), float(r.n[k])) if ok[k] else None
            for k in range(len(ok))]


def field_hits_raw(ctx, fields, wave: int, n: int = GRID) -> tuple[dict[tuple[float, float], list], int]:
    """各视场（归一化）n×n 光瞳网格到达像面的光线，不缓存（移动像面后用）；第二项是每个视场发出的光线数。
    Rays of an n×n pupil grid reaching the image per normalised field, uncached (for a moved plane); also the rays launched per field."""
    grid = kit.pupil(n)
    hits = trace(ctx, [(wave, hx, hy, px, py) for hx, hy in fields for px, py in grid])
    per = len(grid)
    return {f: [h for h in hits[k * per:(k + 1) * per] if h] for k, f in enumerate(fields)}, per


def field_hits(ctx, fields, wave: int, n: int = GRID):
    """同 field_hits_raw，同一次判定内相同参数只追迹一次。/ As field_hits_raw, traced once per evaluation."""
    fields = tuple(tuple(float(v) for v in f) for f in fields)
    return once(ctx, ("saos.micro", "field_hits", fields, int(wave), int(n)), lambda: field_hits_raw(ctx, fields, wave, n))


def axis_hits(ctx, wave: int, n: int = GRID) -> list[tuple[float, ...]]:
    """轴上到达像面的光线，同一次判定只追迹一次。/ On-axis rays reaching the image, traced once per evaluation."""
    return once(ctx, ("saos.micro", "axis", int(wave), int(n)),
                lambda: [h for h in trace(ctx, [(wave, 0.0, 0.0, px, py) for px, py in kit.pupil(n)]) if h])


def ray_arrays(hits, extrapolate: bool):
    """到达光线的 x、y 与斜率 l/n、m/n。要沿 z 外推时去掉 |n| ≤ 1e-12 的光线（几乎平行于像面，外推不了；与 kit.spot 相同）。
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


def best_focus_dz(hits) -> float:
    """像方是均匀介质、光线是直线时，沿像面局部 z 平移 dz 后 RMS 最小的 dz（闭式解，kit.focus_shift）。
    平行光或可用光线不足 3 条时没有焦点，是 DesignFailure（不能当成焦点在像面上）。
    The shift along the image's local z that minimises the RMS spot (closed form). A parallel beam or fewer than 3
    usable rays has no focus: DesignFailure, never a focus on the image plane."""
    dz = kit.focus_shift(*ray_arrays(hits, True))
    if dz is None:
        raise DesignFailure("the rays reaching the sample are parallel (or fewer than 3 usable): there is no focus")
    if not math.isfinite(dz):   # 坐标含 NaN/inf 时闭式解会给出非有限值 / NaN or inf coordinates give a non-finite shift
        raise DesignFailure("the traced rays have no valid focus (non-finite focus shift)")
    return dz


def rms_radius(hits, dz: float = 0.0) -> float:
    """沿局部 z 外推 dz 后的 RMS 半径（相对质心）。/ RMS radius about the centroid after extrapolating dz along local z."""
    x, y, u, v = ray_arrays(hits, dz != 0.0)
    if len(x) == 0:   # 空输入或外推时全部光线被滤掉 / empty input, or every ray dropped before extrapolating
        raise DesignFailure("no ray reaching the sample can be used (none arrive, or all are parallel to the sample plane)")
    r = kit.spread(x + u * dz, y + v * dz)["rms_radius"] if dz != 0.0 else kit.spread(x, y)["rms_radius"]
    if not math.isfinite(r):   # 坐标或 dz 含 NaN/inf / NaN or inf coordinates, or a non-finite dz
        raise DesignFailure("the traced rays give a non-finite RMS spot radius (NaN/inf coordinates or focus shift)")
    return r


def centroid(hits) -> tuple[float, float]:
    x, y, _, _ = ray_arrays(hits, False)
    if len(x) == 0:
        raise DesignFailure("no ray reaches the sample: the centroid is undefined")
    c = kit.spread(x, y)["centroid"]
    if not (math.isfinite(c[0]) and math.isfinite(c[1])):
        raise DesignFailure("the traced rays give a non-finite centroid (NaN/inf coordinates)")
    return c[0], c[1]


# ---- 像面所在的介质 / the medium the image plane sits in ----

@dataclass(frozen=True)
class Medium:
    """像面前最后一段均匀介质（组织、水、浸没液）：key 是决定像面位置的厚度键，sign 是光沿该面局部 z 前进的方向
    （折转一次后厚度为负，sign = -1）。distance 为该面到像面的前进距离，depth(dz) 为到最佳焦点的前进距离。
    The last homogeneous medium before the image (tissue, water, immersion): key is the thickness that places the image
    plane, sign the direction light travels along that surface's local z (-1 after one fold)."""

    key: str
    sign: int

    def distance(self, ctx) -> float:
        return self.sign * float(kit.get(ctx, self.key))

    def place(self, ctx, distance: float) -> None:
        """把像面放到前进距离 distance 处（调用方在 kit.kept 里恢复）。/ Put the image plane at distance (restore in kit.kept)."""
        kit.set(ctx, {self.key: self.sign * distance})

    def depth(self, ctx, dz: float) -> float:
        """像面局部 z 平移 dz 处到该面的前进距离。/ The travelled distance to the point dz along the image's local z."""
        return self.distance(ctx) + self.sign * dz


def medium(ctx, surface: int | str, sign: int | None = None) -> Medium:
    """surface（序号或注释）的厚度决定像面位置；sign 省略时取厚度的符号（厚度为 0 时按 +1）。
    The medium whose thickness after surface places the image; sign defaults to the thickness's sign (+1 at zero)."""
    key = f"{kit.surface(ctx, surface)}.thickness"
    if sign is None:
        sign = -1 if float(kit.get(ctx, key)) < 0 else 1
    if sign not in (1, -1):
        raise SaosError(ErrorCode.BAD_ARGS, f"sign must be 1 or -1, got {sign!r}")
    return Medium(key, int(sign))


def focus_depth(ctx, med: Medium, wave: int, n: int = GRID, min_rays: int = 8) -> float:
    """轴上最佳焦点到介质起始面的前进距离（mm，正值为实焦点在介质中）；到达光线少于 min_rays 为 DesignFailure。
    The travelled distance from the medium's surface to the on-axis best focus (mm; positive is a real focus inside it)."""
    hits = axis_hits(ctx, wave, n)
    if len(hits) < min_rays:
        raise DesignFailure(f"too few on-axis rays reach the sample at wave {wave}: {len(hits)}")
    return med.depth(ctx, best_focus_dz(hits))


def marginal_na(ctx, wave: int, to: int | str | None = None, rays: int = 32) -> float:
    """轴上边缘光线在目标面（默认像面）所在介质里的最小 n·sin u：一圈 rays 条光瞳边缘光线，sin u 取方向余弦的 hypot(l, m)，
    n 为目标面之前那一面的 INDX。目标面局部 z 须沿光轴（没有倾斜）；任一条光线到不了是 DesignFailure。
    The smallest n·sin u of a ring of on-axis marginal rays in the medium of the target surface (default the image):
    sin u = hypot(l, m), n = INDX of the surface before it. The target's local z must be the axis (no tilt)."""
    rays = _count("rays", rays, 1)
    target = kit.surface(ctx, to) if to is not None else kit.surface(ctx, -1)
    index = abs(float(kit.op(ctx, "INDX", target - 1, int(wave))))
    if not (math.isfinite(index) and index >= 1.0):
        raise DesignFailure(f"no valid refractive index before surface {target}: {index}")
    ring = [(int(wave), 0.0, 0.0, math.cos(2 * math.pi * k / rays), math.sin(2 * math.pi * k / rays)) for k in range(rays)]
    r = kit.trace(ctx, ring, target)
    if not bool(np.all(r.ok)):
        raise DesignFailure("a marginal ray does not reach the sample")
    return float(min(index * math.hypot(float(lx), float(my)) for lx, my in zip(r.l, r.m)))


# ---- 零件几何 / part geometry ----

def is_coordinate_break(row: dict[str, Any]) -> bool:
    # OpticStudio 叫 "Coordinate Break"，rtc 叫 "COORDBRK" / "Coordinate Break" in OpticStudio, "COORDBRK" in rtc
    return "COORD" in str(row["type"]).upper()


def sag(ctx, i: int, x: float, y: float) -> float | None:
    """SSAG；该点没有矢高时为 None（OpticStudio 给非有限值，rtc 报 BAD_ARGS）。/ SSAG; None where the surface has no sag."""
    try:
        z = kit.op(ctx, "SSAG", i, 0, x, y)
    except SaosError as exc:
        if exc.code != ErrorCode.BAD_ARGS:
            raise
        return None
    return z if math.isfinite(z) else None


def require_defined(ctx, rows: Iterable[dict[str, Any]]) -> None:
    """这些面在半口径处必须有矢高（例如球面半口径不能超过半径），坐标断点跳过；不成立是 DesignFailure。rows 是 surfaces() 的行。
    Each surface must have a sag at its semi-diameter (a sphere's semi-diameter cannot exceed its radius); coordinate
    breaks are skipped. rows come from surfaces()."""
    for s in rows:
        if is_coordinate_break(s):
            continue
        i, sd = s["index"], float(s["semi_diameter"])
        # 旋转对称面型在这四点的 x²+y² 都正好是 sd²：查一点就够 / for a rotational type all four points have x²+y² = sd² exactly
        points = ((sd, 0.0),) if geometry.rotational(s["type"]) else ((sd, 0.0), (0.0, sd), (-sd, 0.0), (0.0, -sd))
        for x, y in points:
            if sag(ctx, i, x, y) is None:
                raise DesignFailure(f"surface {i} ({s['comment']}) is undefined at its semi-diameter {sd * 1000:.1f} um")
        r = float(s["radius"])
        if str(s["type"]).upper() == "STANDARD" and math.isfinite(r) and r != 0 and abs(float(s["conic"])) < 1e-12 and sd > abs(r) + 1e-9:
            raise DesignFailure(f"surface {i} ({s['comment']}) semi-diameter {sd * 1000:.1f} um exceeds its radius {abs(r) * 1000:.1f} um")


def part_radius(ctx, row: dict[str, Any], radii: dict[int, float] | None = None) -> float:
    """零件外缘半径，与交叠检查同一定义（kit.part_radii / saos.geometry.part_radii）；交叠检查不含这个面时取半口径。
    多个面一起量时先取一次 kit.part_radii(ctx) 传进来，免得每个面都跑一遍交叠检查。
    The part's edge radius as the interference check defines it; the semi-diameter for a surface it leaves out."""
    radii = kit.part_radii(ctx) if radii is None else radii
    return radii.get(int(row["index"]), float(row["semi_diameter"]))


def transform(ctx, i: int) -> list[float]:
    """面的全局变换：前 9 个为行优先的旋转，后 3 个为顶点位置。/ The surface's global transform: 9 rotation terms, then the vertex."""
    rot, shift = kit.global_matrix(ctx, i)
    return [float(v) for v in rot.reshape(-1)] + [float(v) for v in shift]


@dataclass(frozen=True)
class Axis:
    """全局坐标里的一条轴（外壳、套管的中心线）：origin 为轴上一点，direction 为单位方向。/ An axis in global coordinates."""

    origin: tuple[float, float, float]
    direction: tuple[float, float, float]

    def radial(self, p: Sequence[float]) -> float:
        """点到轴的距离。/ The distance of a point from the axis."""
        o, axis = self.origin, self.direction
        v = [p[c] - o[c] for c in range(3)]
        along = sum(v[c] * axis[c] for c in range(3))
        return math.sqrt(max(0.0, sum(c * c for c in v) - along * along))


def axis_through(ctx, a: int, b: int) -> Axis:
    """过面 a、b 顶点的轴，方向 a → b。/ The axis through the vertices of surfaces a and b, pointing from a to b."""
    o = transform(ctx, a)[9:12]
    p1 = transform(ctx, b)[9:12]
    axis = [p1[c] - o[c] for c in range(3)]
    norm = math.sqrt(sum(c * c for c in axis))
    if not norm > 0:
        raise SaosError(ErrorCode.BAD_ARGS, f"surfaces {a} and {b} share a vertex; they define no axis")
    return Axis((o[0], o[1], o[2]), (axis[0] / norm, axis[1] / norm, axis[2] / norm))


def radial_extent(ctx, rows: Iterable[dict[str, Any]], axis: Axis, rings: int = 21, spokes: int = 36) -> float:
    """这些面的零件（part_radius 内）离 axis 最远的距离：极坐标网格上取实际矢高（SSAG）变换到全局坐标。没有面时为 0；
    口径内取不到矢高是 DesignFailure。旋转对称面型每圈只取一次矢高（与逐点取只差 x²+y² 的末位舍入）。
    The farthest distance from axis of these surfaces' parts (within part_radius), from real sags on a polar grid
    transformed to global coordinates. 0 without surfaces; an undefined sag inside the aperture is DesignFailure."""
    rings, spokes = _count("rings", rings, 2), _count("spokes", spokes, 1)
    far = 0.0
    radii = kit.part_radii(ctx)
    for s in rows:
        i = s["index"]
        tr = transform(ctx, i)
        sd = part_radius(ctx, s, radii)
        symmetric = geometry.rotational(s["type"])
        for ring in range(rings):
            r = sd * ring / (rings - 1)
            ring_z = sag(ctx, i, r, 0.0) if symmetric else None
            for k in range(spokes if ring else 1):
                a = 2 * math.pi * k / spokes
                x, y = r * math.cos(a), r * math.sin(a)
                z = ring_z if symmetric else sag(ctx, i, x, y)
                if z is None:
                    raise DesignFailure(f"surface {i} ({s['comment']}) is undefined at r = {r * 1000:.1f} um inside its aperture")
                p = [tr[9 + j] + tr[3 * j] * x + tr[3 * j + 1] * y + tr[3 * j + 2] * z for j in range(3)]
                far = max(far, axis.radial(p))
    return far


def min_gap(ctx, front: int | str, radius: float | None = None, rings: int = 17, spokes: int = 32) -> float:
    """相邻两面（front 与 front + 1，同轴、中间没有坐标断点）在 radius（默认两面 part_radius 的较小者）内的最小轴向间隔：
    厚度 + 后面矢高 − 前面矢高，极坐标网格取样；为负表示交叠。取不到矢高是 DesignFailure。多重结构按当前结构，
    逐结构测用 with ctx.configuration(c)。
    The smallest axial gap between adjacent coaxial surfaces front and front + 1 within radius (default the smaller
    part_radius): thickness + back sag − front sag on a polar grid; negative means they overlap."""
    rings, spokes = _count("rings", rings, 2), _count("spokes", spokes, 1)
    i = kit.surface(ctx, front)
    rows = ctx.backend.surfaces()
    if i + 1 >= len(rows):
        raise SaosError(ErrorCode.BAD_ARGS, f"min_gap needs a surface after the front surface {front!r} (index {i}); the image has none")
    a, b = rows[i], rows[i + 1]
    if is_coordinate_break(a) or is_coordinate_break(b):
        raise SaosError(ErrorCode.BAD_ARGS, f"min_gap needs two optical surfaces; {i} or {i + 1} is a coordinate break")
    radii = kit.part_radii(ctx) if radius is None else {}
    edge = min(part_radius(ctx, a, radii), part_radius(ctx, b, radii)) if radius is None else float(radius)
    thickness = float(kit.get(ctx, f"{i}.thickness"))
    smallest = math.inf
    for ring in range(rings):
        r = edge * ring / (rings - 1)
        for k in range(spokes if ring else 1):
            t = 2 * math.pi * k / spokes
            x, y = r * math.cos(t), r * math.sin(t)
            za, zb = sag(ctx, i, x, y), sag(ctx, i + 1, x, y)
            if za is None or zb is None:
                raise DesignFailure(f"surface {i} or {i + 1} is undefined at r = {r * 1000:.1f} um")
            smallest = min(smallest, thickness + zb - za)
    return smallest


# ---- 收集 / collection ----

def collection_fraction(ctx, fields, wave: int, n: int = 61) -> float:
    """几何收集比例：按光路可逆，像点能送回物方（物方 NA、物面圆内）的方向就是正向光束在像侧覆盖的方向。光瞳网格上四角都通过的
    格子，把像方单位方向向量围成的四边形面积累加为立体角，除以 4π；多个视场取平均。不含菲涅耳损失与吸收。
    Geometric collection fraction by reversibility: on the pupil grid, cells whose four corners pass add the area of the
    quadrilateral their image-side unit directions span, as solid angle over 4π; averaged over fields. No Fresnel loss or absorption."""
    # 只追单位圆内的点：坐标与 kit.pupil 逐位相同，各光线独立追迹 / inside the unit circle only, same coordinates as kit.pupil
    # n ≤ 3 的网格没有四角都在单位圆内的格子，积不出面积，结果恒为 0；n = 4 起中心格可积
    # n <= 3 has no cell with all four corners inside the unit circle, so the result would always be 0
    n = _count("n", n, 4)
    fields = list(fields)
    if not fields:
        raise SaosError(ErrorCode.BAD_ARGS, "collection_fraction needs at least one field")
    coords = kit.pupil(n)
    step = 2 / (n - 1)
    fractions = []
    for hx, hy in fields:
        hits = trace(ctx, [(wave, hx, hy, px, py) for px, py in coords])
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


# ---- 衍射 PSF / diffraction PSF ----

def psf(hug, field: tuple[float, float], delta_um: float) -> kit.Psf:
    """一张 Huygens PSF；没有数据是 DesignFailure。/ One Huygens PSF; no data is DesignFailure."""
    p = hug.psf(field, delta_um)
    if p is None:
        raise DesignFailure("Huygens PSF returned no data")
    return p


def psf_pixel_um(ctx, hx: float, hy: float, wave: int, image: int = PSF_IMAGE) -> float:
    """像元间距：让 image 点窗口约为该视场（归一化）几何光斑 RMS 直径的 4 倍，最小 0.05 µm。在当前像面上追迹，不用缓存。
    Pixel pitch so that the image window spans about 4 RMS diameters of the field's geometric spot, at least 0.05 µm."""
    hits, _ = field_hits_raw(ctx, [(hx, hy)], wave, GRID)
    h = hits[(hx, hy)]
    if len(h) < 4:
        raise DesignFailure(f"too few rays reach the sample at field {(hx, hy)}")
    return max(0.05, 4 * 2 * rms_radius(h, 0.0) * 1000 / image)


def lateral_widths(p: kit.Psf, n_photon: int = 1) -> dict[str, float]:
    """kit.psf_widths，取不到半高全宽是 DesignFailure。/ kit.psf_widths, with DesignFailure when a width is missing."""
    w = kit.psf_widths(p, n_photon)
    if w["fwhm_x"] is None or w["fwhm_y"] is None:
        raise DesignFailure("PSF has no positive peak" if not p.peak() > 0 else "PSF half maximum not inside the sampled window")
    return w


def axial_fwhm_um(ctx, hug, med: Medium, focus: float, field: tuple[float, float], delta_um: float, step_um: float,
                  reaches: Sequence[int], scan: str = "window", n_photon: int = 1) -> float:
    """PSF 峰值随像面离焦（相对前进距离 focus 处，步长 step_um）的半高全宽（kit.psf_axial）；像面不进入介质起始面以内，
    那些面记为 0。像面由 med 移动，调用方在 kit.kept([med.key]) 里恢复。半高不在窗口内是 DesignFailure。
    The FWHM of the PSF peak against defocus about the travelled distance focus (kit.psf_axial); planes behind the
    medium's surface count as 0. The plane is moved through med; restore it with kit.kept([med.key])."""
    def peak_at(k: int) -> float:
        plane = focus + k * step_um / 1000
        if plane <= 0:
            return 0.0
        med.place(ctx, plane)
        return psf(hug, field, delta_um).peak()

    axial = kit.psf_axial(peak_at, step_um, reaches, n_photon=n_photon, scan=scan)
    if axial is None:
        raise DesignFailure("PSF half maximum not inside the sampled window")
    return axial["fwhm_um"]


def worst_wavefront(ctx, fields: list[tuple[float, float]], wave: int) -> float:
    """这些视场（镜头视场单位的坐标）在当前像面上 RMS 波前误差的最大值（波长）；任一视场算不出是 DesignFailure。
    The largest RMS wavefront error (waves) over these fields at the current image plane."""
    worst = 0.0
    for f, v in zip(fields, kit.wavefront_rms(ctx, list(fields), wave)):
        if v is None or not math.isfinite(v):
            raise DesignFailure(f"wavefront error not computable at field {f}")
        worst = max(worst, v)
    return worst


# ---- 借 worker 池 / borrowing the worker pool ----

def pooled(ctx, fn: str, items: list[Any], *, keep_free: int = POOL_KEEP_FREE, min_workers: int = POOL_MIN_WORKERS,
           label: str = "pooled") -> dict[int, Any]:
    """有空闲许可证时把 fn(w, item)（"saos.<模块>:<函数>"）分给后台进程的 worker 池（ctx.spare_map），返回算成的 {条目序号: 结果}；
    任一条目抛了 DesignFailure 就在这里抛出同样的失败。不在后台进程里（worker 内、离线）、池忙或凑不出 min_workers 个 worker
    时返回空，调用方串行补算缺的条目。
    Spread fn(w, item) over the daemon's worker pool when licences are free (ctx.spare_map) and return {item index:
    result} for the items finished; a DesignFailure in any item is raised here. Empty outside the daemon, when the pool
    is busy or fewer than min_workers fit: the caller computes the missing items itself."""
    spare = getattr(ctx, "spare_map", None)
    out = spare(fn, items, keep_free=keep_free, min_workers=min_workers, label=label) if callable(spare) else None
    if out is None:
        return {}
    done: dict[int, Any] = {}
    for i in range(len(items)):
        error = out["errors"].get(str(i))
        if error and "DesignFailure:" in error:
            raise DesignFailure(error.split("DesignFailure:", 1)[1].strip())
        if error is None and i not in out["not_run"] and out["results"][i] is not None:
            done[i] = out["results"][i]
    return done


# ---- 变焦行程 / focus travel ----

def focus_travel(ctx, apply: Callable[[float], None], lo: float, hi: float, depth: Callable[[Any], float], n: int = 17,
                 refine: int = 1, points: int = 8) -> dict[str, Any]:
    """运动量 q 在 [lo, hi] 上扫一遍：apply(q) 改镜头（调用方在 kit.kept 里恢复），depth(ctx) 给焦点深度（mm）；
    在 n 个等距点上取值，再分别在最浅、最深处加密（kit.profile，共用样点）。DesignFailure 的点记为读不出。
    返回 near、far、span（mm）、monotonic（样点上深度随 q 单调）、samples 与两侧 profile；有读不出的点或加密改变了极值时
    coverage_ok 为假，此时 near/far 不可当作判定值。
    Sweep the motion q over [lo, hi]: apply(q) changes the lens (restore it in kit.kept), depth(ctx) gives the focus
    depth (mm); n equal steps, then refined at the shallowest and deepest points (kit.profile, sharing samples). A
    DesignFailure counts as unreadable. coverage_ok is False when any point was unreadable or refining moved an extreme."""
    seen: dict[float, float | None] = {}

    def evaluate(qs: list[float]) -> list[float | None]:
        out = []
        for q in qs:
            if q not in seen:
                apply(q)
                try:
                    seen[q] = float(depth(ctx))
                except DesignFailure:
                    seen[q] = None
            out.append(seen[q])
        return out

    near = kit.profile(evaluate, lo, hi, n, find="min", refine=refine, points=points)
    far = kit.profile(evaluate, lo, hi, n, find="max", refine=refine, points=points)
    ordered = sorted(seen.items())
    values = [v for _, v in ordered if v is not None]
    rising = all(b >= a - 1e-9 for a, b in zip(values, values[1:]))
    falling = all(b <= a + 1e-9 for a, b in zip(values, values[1:]))
    ok = near["coverage"]["ok"] and far["coverage"]["ok"] and len(values) == len(ordered)
    return {"near": near["value"], "far": far["value"],
            "span": (far["value"] - near["value"]) if ok else None, "monotonic": bool(values) and (rising or falling),
            "coverage_ok": ok, "samples": [[q, v] for q, v in ordered], "near_profile": near, "far_profile": far}
