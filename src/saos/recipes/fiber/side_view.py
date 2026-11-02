"""光纤侧视探头的判定量（配方草稿，由 fiber-printed-relay 任务的 checks/fiberlib.py 检查 v6 上收，2026-10-17）。
Measurements for side-viewing fibre probes (draft recipe, lifted from checks/fiberlib.py v6 of the fiber-printed-relay job).

数值定义与 fiberlib v6 相同。与布局无关的测量（焦点、RMS、口径内的面型、外壳间隙、收集比例、Huygens PSF、借 worker 池）在
saos.micro，这里只保留侧视探头的布局约定：面注释、一次折转、视场与波长。判定脚本只要一行：
The numbers are defined as in fiberlib v6. Layout-independent measurements (focus, RMS, surfaces inside their aperture,
housing clearance, collection, Huygens PSF, borrowing the worker pool) live in saos.micro; this module keeps only the
side-view layout conventions: comments, one fold, fields and waves. A check script is one line:

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
from dataclasses import dataclass, replace
from typing import Any, Callable

from saos import kit, micro
from saos.micro import GRID, PSF_IMAGE, PSF_PUPIL, DesignFailure, guarded  # noqa: F401 - 配方的公开名字 / the recipe's public names

W488, W532 = 1, 2
FIELDS5 = ((0.0, 0.0), (0.7, 0.0), (-0.7, 0.0), (0.0, 0.7), (0.0, -0.7))
MID_FIELDS = ((0.7, 0.0), (-0.7, 0.0), (0.0, 0.7), (0.0, -0.7))
EDGE_FIELDS = ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0))


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
    # psf_z 的离焦扫描（kit.psf_axial 的 scan）：window 为原定义；outward 算的离焦面少得多，但属于另一种定义，换用前与 window 对比
    # psf_z's focus scan: window is the original definition; outward computes far fewer planes but is another definition
    psf_z_scan: str = "window"


DEFAULT = Layout()


# ---- 基础 ----

def _once(ctx, key: tuple, compute: Callable[[], Any]) -> Any:
    return micro.once(ctx, ("saos.recipes.fiber",) + key, compute)


def surface_index(ctx, comment: str, required: bool = True) -> int | None:
    names = kit.surfaces_by_comment(ctx)
    if comment in names:
        return names[comment]
    if required:
        raise ValueError(f"lens has no surface commented {comment!r}")
    return None


trace = micro.trace


def pupil_grid(n: int = GRID) -> list[tuple[float, float]]:
    return kit.pupil(n)


def field_hits(ctx, fields, wave=W532, n=GRID):
    """各视场到达样品面的光线；同一次判定内相同参数只追迹一次。/ Rays reaching the sample per field, traced once per evaluation."""
    return micro.field_hits(ctx, fields, wave, n)


def _outer_key(ctx, layout: Layout) -> str:
    """外壁这一面的厚度键：它决定样品面（像面）的位置。/ The outer wall's thickness key: it places the sample (image) plane."""
    return f"{surface_index(ctx, layout.tube_outer)}.thickness"


def _tissue(ctx, layout: Layout) -> micro.Medium:
    """外壁之后的组织：折转一次后沿局部 -z 前进。/ The tissue after the outer wall: travelled along local -z after the fold."""
    return micro.Medium(_outer_key(ctx, layout), -1)


def sample_distance(ctx, layout: Layout = DEFAULT) -> float:
    """样品面到外壁的前进距离（折转一次后厚度为负）。"""
    return _tissue(ctx, layout).distance(ctx)


# ---- 几何前提 ----

def _rows(ctx) -> list[dict[str, Any]]:
    """面表，同一次判定只读一次：OpticStudio 上每面要读约 20 个属性。临时改像面的测量（PSF、波前）在 kept 里恢复后才结束，
    不会把改动中的面表留在缓存里。
    The surface table, read once per evaluation (about 20 properties per surface on OpticStudio). Measurements that move
    the image plane restore it inside kept before they end, so no table from the middle of a change is cached."""
    return _once(ctx, ("rows",), ctx.backend.surfaces)


def _geometry(ctx, layout: Layout) -> bool:
    rows = _rows(ctx)
    inner = surface_index(ctx, layout.tube_inner)
    # 厚度方向：折转（理想反射面）之前沿 +z，之后沿 -z；反向的厚度表示零件长回前一个零件里
    mirror = next(s["index"] for s in rows if str(s["material"]).upper() == "MIRROR")
    for s in rows[:-1]:
        if micro.is_coordinate_break(s):
            continue
        i, t = s["index"], float(s["thickness"])
        if (i < mirror and t < -1e-6) or (i > mirror and t > 1e-6):   # 容差 1 nm
            raise DesignFailure(f"surface {i} ({s['comment']}) has thickness {t * 1000:.1f} um against the direction of propagation")
    # 面在其半口径内必须有定义（例如球面半口径不能超过半径）
    micro.require_defined(ctx, rows[1:inner + 1])
    for s, nxt in zip(rows[:inner - 1], rows[1:inner]):
        i, t = s["index"], float(s["thickness"])
        if abs(t) < 1e-9 or micro.is_coordinate_break(s) or micro.is_coordinate_break(nxt) or str(s["material"]).upper() in ("N-BK7", "MIRROR") or i + 1 == inner:
            continue
        e = kit.op(ctx, "ETVA", i, 0)
        if not math.isfinite(e) or e * (1 if t > 0 else -1) < 0:
            raise DesignFailure(f"edge thickness after surface {i} ({s['comment']}) is {e * 1000:.1f} um against centre {t * 1000:.1f} um")
    return True


def geometry_ok(ctx, layout: Layout = DEFAULT) -> bool:
    """各透镜与间隙的边缘厚度（ETVA）须与中心厚度同号：曲面不穿过贴合面、间隙不交叠；跳过坐标断点、零厚度行、棱镜内部与
    紧邻管内壁的行（管壁间隙由 wall_clearance 按实际矢高检查）。同一次判定内只查一次。"""
    return _once(ctx, ("geometry", layout), lambda: _geometry(ctx, layout))


part_radius = micro.part_radius


def _wall_clearance(ctx, layout: Layout) -> float:
    geometry_ok(ctx, layout)
    rows = _rows(ctx)
    axis = micro.axis_through(ctx, 0, 1)
    inner = surface_index(ctx, layout.tube_inner)
    r_in = abs(float(rows[inner]["radius"]))
    if abs(axis.radial(micro.transform(ctx, inner)[9:12]) - r_in) > 1e-6:
        raise ValueError("tube inner wall vertex is not on the tube radius")
    far = layout.fiber_max_radius
    d_exit = axis.radial(micro.transform(ctx, surface_index(ctx, layout.prism_exit))[9:12])
    far = max(far, math.hypot(d_exit, layout.prism_half), math.hypot(d_exit - 2 * layout.prism_half, layout.prism_half))
    parts = [s for s in rows[1:inner]
             if not (micro.is_coordinate_break(s) or str(s["comment"]) in layout.wall_skip or str(s["material"]).upper() == "MIRROR")]
    far = max(far, micro.radial_extent(ctx, parts, axis))
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
    """打印透镜与胶（材料名以 printed_prefix 开头，或模型玻璃）中最大的 nd。"""
    rows = [s["index"] for s in _rows(ctx)
            if str(s["material"]).upper().startswith(layout.printed_prefix) or isinstance(kit.get(ctx, f"{s['index']}.material"), dict)]
    if not rows:
        raise ValueError("no printed lens or glue material found")
    return max(kit.material_nd(ctx, i) for i in rows)


def lens_base_um(ctx, layout: Layout = DEFAULT) -> float:
    """棱镜出射面上透镜的底径：贴合面与穹顶实际半径（part_radius）的较大者 × 2；没有该透镜时为 0。"""
    i = surface_index(ctx, layout.lens_plane, required=False)
    if i is None:
        return 0.0
    rows = _rows(ctx)
    if abs(float(rows[i]["thickness"])) < 1e-9:
        return 0.0
    j = surface_index(ctx, layout.lens_dome)
    return 2 * max(part_radius(ctx, rows[i]), part_radius(ctx, rows[j])) * 1000


# ---- 焦点、光斑、通过率、视场 ----

axis_hits = micro.axis_hits


def _focus_positions(ctx, layout: Layout, n: int) -> dict[int, float]:
    # 折转一次后沿局部 -z 前进 / travelled along local -z after the fold
    return {wave: micro.focus_depth(ctx, _tissue(ctx, layout), wave, n) for wave in (layout.excitation, layout.emission)}


def focus_positions(ctx, layout: Layout = DEFAULT, n: int = GRID) -> dict[int, float]:
    """轴上激发、发射波长最佳焦点到外壁的前进距离（mm，正值在组织中、为实焦点）。"""
    return _once(ctx, ("focus", layout, n), lambda: _focus_positions(ctx, layout, n))


def _best_focus_dz(ctx, layout: Layout) -> float:
    hits = axis_hits(ctx, layout.emission)
    if len(hits) < 8:
        raise DesignFailure("too few on-axis rays reach the sample")
    dz = micro.best_focus_dz(hits)
    if _tissue(ctx, layout).depth(ctx, dz) <= 0:
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
    return max(2 * micro.rms_radius(h, dz) * 1000 for h in hits.values())


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
    ax = micro.centroid(axis[(0.0, 0.0)])
    radii = []
    for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        fields = [(dx * s / 20, dy * s / 20) for s in range(20, 0, -1)]
        hits, _ = field_hits(ctx, fields, layout.emission, n=11)
        r = 0.0
        for f in fields:
            f = (float(f[0]), float(f[1]))
            if len(hits[f]) >= 4:
                cx, cy = micro.centroid(hits[f])
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
    if len(a) < 4 or 2 * micro.rms_radius(a, dz) * 1000 > limit_um:
        return 0.0
    ax = micro.centroid(a)
    radii = []
    for dx, dy in ((0, 1), (0, -1), (1, 0), (-1, 0)):
        fields = [(float(dx * s / 20), float(dy * s / 20)) for s in range(1, 21)]
        hits, _ = field_hits(ctx, fields, layout.emission)
        r = 0.0
        for f in fields:
            h = hits[f]
            if len(h) < 4 or 2 * micro.rms_radius(h, dz) * 1000 > limit_um:
                break
            cx, cy = micro.centroid(h)
            r = math.hypot(cx - ax[0], cy - ax[1])
        radii.append(r)
    return 2 * min(radii) * 1000


def _collection(ctx, fields, n: int, layout: Layout) -> float:
    geometry_ok(ctx, layout)
    return micro.collection_fraction(ctx, fields, layout.emission, n)


def collection(ctx, fields, n: int = 61, layout: Layout = DEFAULT) -> float:
    """几何收集比例：按光路可逆，样品点能送回光纤（物方 NA、端面圆内）的方向就是正向光束在样品侧覆盖的方向。
    光瞳网格上四角都通过的格子，把像方单位方向向量围成的四边形面积累加为立体角，除以 4π；多个物点取平均。"""
    fields = tuple(tuple(float(v) for v in f) for f in fields)
    return _once(ctx, ("collection", fields, n, layout), lambda: _collection(ctx, fields, n, layout))


# ---- Huygens PSF（发射波长，样品面移到轴上最佳焦面） ----

def _field_at(layout: Layout, hx: float, hy: float) -> tuple[float, float]:
    """归一化视场 → 视场表坐标（物高 h·hx, h·hy）；PSF 与波前分析按坐标临时追加视场。"""
    return layout.object_height * hx, layout.object_height * hy


def _psf_widths(ctx, layout: Layout) -> list[dict[str, Any]]:
    geometry_ok(ctx, layout)
    dz = best_focus_dz(ctx, layout)
    tissue = _tissue(ctx, layout)
    focus = tissue.depth(ctx, dz)
    out = []
    with kit.kept(ctx, [tissue.key]), kit.huygens(ctx, layout.emission, PSF_PUPIL, PSF_IMAGE) as hug:
        tissue.place(ctx, focus)
        for hx, hy in FIELDS5:
            delta = micro.psf_pixel_um(ctx, hx, hy, layout.emission)
            w = micro.lateral_widths(micro.psf(hug, _field_at(layout, hx, hy), delta))
            out.append({"field": (hx, hy), "fwhm_x": w["fwhm_x"], "fwhm_y": w["fwhm_y"], "ee80": w["ee_diameter"], "delta_um": delta})
    return out


def psf_widths_um(ctx, layout: Layout = DEFAULT) -> list[dict[str, Any]]:
    """5 个视场的 X、Y 半高全宽与 80% 包围能量直径（µm），在发射波长轴上最佳焦面计算 Huygens PSF（kit.psf_widths）。
    psf_x、psf_y、ee80 共用一次计算。"""
    return _once(ctx, ("psf", layout), lambda: _psf_widths(ctx, layout))


def psf_z_um(ctx, step_um: float = 4.0, half_range_um: float = 60.0, layout: Layout = DEFAULT) -> float:
    """各视场 Huygens PSF 峰值随像面离焦的曲线的半高全宽（µm，kit.psf_axial），5 个视场取最差。离焦面相对轴上最佳焦面，像元间距
    按最佳焦面定且各离焦面相同；离焦面不得进入外壁以内。曲线两侧都须降到峰值一半以下，否则扩大一次范围，仍不够则无效。
    有空闲许可证时各视场分给 worker 池并行（micro.pooled），没算成的视场在主实例里串行算。"""
    geometry_ok(ctx, layout)
    dz = best_focus_dz(ctx, layout)
    tissue = _tissue(ctx, layout)
    focus = tissue.depth(ctx, dz)
    widths: dict[tuple[float, float], float] = {}
    # worker 按默认约定测，只换扫描方式：其余约定与默认相同时才能并行 / workers use the default conventions apart from the scan
    if layout == replace(DEFAULT, psf_z_scan=layout.psf_z_scan):
        items = [{"hx": hx, "hy": hy, "focus": focus, "step": step_um, "half": half_range_um, "scan": layout.psf_z_scan} for hx, hy in FIELDS5]
        done = micro.pooled(ctx, "saos.recipes.fiber.side_view:psf_z_field", items, label="psf_z")
        widths = {FIELDS5[i]: float(v) for i, v in done.items()}
    worst = max(widths.values(), default=0.0)
    rest = [f for f in FIELDS5 if f not in widths]
    if rest:
        with kit.kept(ctx, [tissue.key]), kit.huygens(ctx, layout.emission, PSF_PUPIL, PSF_IMAGE) as hug:
            for hx, hy in rest:
                worst = max(worst, _psf_z_field(ctx, hug, hx, hy, focus, step_um, half_range_um, layout))
    return worst


def _psf_z_field(ctx, hug, hx: float, hy: float, focus: float, step_um: float, half_range_um: float, layout: Layout) -> float:
    """一个视场的离焦曲线半高全宽（µm）；调用方负责恢复外壁厚度。/ The axial FWHM of one field (µm); the caller restores the wall."""
    tissue = _tissue(ctx, layout)
    tissue.place(ctx, focus)
    delta = micro.psf_pixel_um(ctx, hx, hy, layout.emission)
    reaches = (int(half_range_um / step_um), int(2 * half_range_um / step_um))
    return micro.axial_fwhm_um(ctx, hug, tissue, focus, _field_at(layout, hx, hy), delta, step_um, reaches, layout.psf_z_scan)


def psf_z_field(w, item: dict[str, Any]) -> float:
    """worker 里测一个视场的 psf_z（由 psf_z_um 经 micro.pooled 分派，默认 Layout，扫描方式随条目）。
    One field of psf_z inside a worker (dispatched by psf_z_um through micro.pooled; default Layout, the scan from the item)."""
    layout = replace(DEFAULT, psf_z_scan=str(item.get("scan") or "window"))
    with kit.kept(w, [_outer_key(w, layout)]), kit.huygens(w, layout.emission, PSF_PUPIL, PSF_IMAGE) as hug:
        return _psf_z_field(w, hug, float(item["hx"]), float(item["hy"]), float(item["focus"]), float(item["step"]),
                            float(item["half"]), layout)


def wfe_rms_waves(ctx, layout: Layout = DEFAULT) -> float:
    """发射波长 RMS 波前误差（波长，kit.wavefront_rms：光线积分、参考质心即扣除平移与倾斜），5 个视场取最差，像面移到轴上最佳焦面。"""
    geometry_ok(ctx, layout)
    dz = best_focus_dz(ctx, layout)
    tissue = _tissue(ctx, layout)
    focus = tissue.depth(ctx, dz)
    with kit.kept(ctx, [tissue.key]):
        tissue.place(ctx, focus)
        return micro.worst_wavefront(ctx, [_field_at(layout, hx, hy) for hx, hy in FIELDS5], layout.emission)


# ---- 判定入口 / check entry points ----

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
