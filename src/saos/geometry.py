"""结构交叠检查：面与面相交、镜片之间相互穿插。只用计算后端给出的面变换（global_matrix）和面形（矢高），
这里只做坐标变换和比较，不涉及光学计算。
Interference check: surfaces crossing each other and lenses interpenetrating. Uses only the surface transforms
(global_matrix) and shapes (sags) from the backend; this module does coordinate transforms and comparisons, no optics.

每个面在自身局部坐标里按极坐标网格取矢高：RINGS 圈（含边缘）× SPOKES 条辐线，加中心点。网格半径取面孔径的外接圆，
只有落在孔径里的点参与比较；没有显式孔径（矩形、圆形、椭圆）的面按半口径的圆处理。
Each surface is sampled in its local frame on a polar grid: RINGS rings (the rim included) × SPOKES spokes, plus the centre.
The grid covers the surface aperture's circumscribed circle and only points inside the aperture are compared; a surface
without an explicit aperture (rectangular, circular, elliptical) is a circle of its semi-diameter.
"""

from __future__ import annotations

import math
from typing import Any

RINGS = 4
SPOKES = 16
# 越过另一面超过这个距离（镜头单位）才算相交，挡住数值噪声和贴合的胶合面 / beyond numerical noise and cemented contact
TOL = 1e-4


def aperture(spec: dict[str, Any] | None, sd: float) -> dict[str, Any]:
    """面孔径（backend 的 aperture 字典，ZOS-API 的 CurrentType 与设置）统一成 shape、hx、hy、dx、dy、rmin；
    遮拦、浮动孔径、用户孔径和没有孔径时按半口径的圆，explicit 为 False。
    The surface aperture as shape, hx, hy, dx, dy, rmin; obscurations, floating, user or no apertures give the
    semi-diameter circle with explicit False."""
    spec = spec or {}
    kind = str(spec.get("type", ""))
    dx, dy = float(spec.get("ApertureXDecenter") or 0.0), float(spec.get("ApertureYDecenter") or 0.0)
    if "RectangularAperture" in kind and spec.get("XHalfWidth") and spec.get("YHalfWidth"):
        return {"shape": "rect", "hx": float(spec["XHalfWidth"]), "hy": float(spec["YHalfWidth"]), "dx": dx, "dy": dy, "rmin": 0.0, "explicit": True}
    if "EllipticalAperture" in kind and spec.get("XHalfWidth") and spec.get("YHalfWidth"):
        return {"shape": "ellipse", "hx": float(spec["XHalfWidth"]), "hy": float(spec["YHalfWidth"]), "dx": dx, "dy": dy, "rmin": 0.0, "explicit": True}
    if "CircularAperture" in kind and spec.get("MaximumRadius"):
        r = float(spec["MaximumRadius"])
        return {"shape": "circle", "hx": r, "hy": r, "dx": dx, "dy": dy, "rmin": float(spec.get("MinimumRadius") or 0.0), "explicit": True}
    return {"shape": "circle", "hx": sd, "hy": sd, "dx": 0.0, "dy": 0.0, "rmin": 0.0, "explicit": False}


def extent(ap: dict[str, Any]) -> float:
    """孔径外接圆半径（以面顶点为圆心）。/ Radius of the aperture's circumscribed circle about the vertex."""
    if ap["shape"] == "rect":
        return math.hypot(ap["hx"] + abs(ap["dx"]), ap["hy"] + abs(ap["dy"]))
    return max(ap["hx"], ap["hy"]) + math.hypot(ap["dx"], ap["dy"])


def inside(ap: dict[str, Any], x: float, y: float) -> bool:
    u, v = x - ap["dx"], y - ap["dy"]
    eps = 1e-9 * max(ap["hx"], ap["hy"])
    if ap["shape"] == "rect":
        return abs(u) <= ap["hx"] + eps and abs(v) <= ap["hy"] + eps
    if ap["shape"] == "ellipse":
        return (u / ap["hx"]) ** 2 + (v / ap["hy"]) ** 2 <= 1 + 1e-9
    r = math.hypot(u, v)
    return ap["rmin"] - eps <= r <= ap["hx"] + eps


def outline(ap: dict[str, Any], n: int = 32) -> list[tuple[float, float]]:
    """孔径边界上 n+1 个点（首尾相同），第 k 个点在方位角 2πk/n 方向上，布局图据此画边缘。
    n+1 points on the aperture edge (closed), the k-th in the direction 2πk/n from the aperture centre."""
    out = []
    for k in range(n + 1):
        a = 2 * math.pi * k / n
        c, sn = math.cos(a), math.sin(a)
        if ap["shape"] == "rect":
            t = min(ap["hx"] / abs(c) if abs(c) > 1e-12 else math.inf, ap["hy"] / abs(sn) if abs(sn) > 1e-12 else math.inf)
            out.append((ap["dx"] + t * c, ap["dy"] + t * sn))
        else:
            out.append((ap["dx"] + ap["hx"] * c, ap["dy"] + ap["hy"] * sn))
    return out


def chord(ap: dict[str, Any], axis: str) -> tuple[float, float, float]:
    """子午（axis="y"，x=0）或弧矢（axis="x"，y=0）截线在孔径里的范围 (lo, hi, 另一坐标)；截线不过孔径时取过孔径中心的那条。
    The meridional (axis "y", at x=0) or sagittal (axis "x", at y=0) section inside the aperture as (lo, hi, other
    coordinate); when the section misses the aperture, the one through the aperture centre."""
    across, along = ("dx", "dy") if axis == "y" else ("dy", "dx")
    half_across, half_along = (ap["hx"], ap["hy"]) if axis == "y" else (ap["hy"], ap["hx"])
    at = 0.0 if abs(ap[across]) <= half_across else ap[across]
    u = (at - ap[across]) / half_across
    half = half_along if ap["shape"] == "rect" else half_along * math.sqrt(max(0.0, 1 - u * u))
    return ap[along] - half, ap[along] + half, at


def fill_outside(points: list[tuple[float, float]], sags: list[float | None], ap: dict[str, Any]) -> list[float | None]:
    """孔径外的网格点读不到矢高时（例如球面在矩形孔径的角外），用同一辐线内一圈的值补上，只供插值用。
    Grid points outside the aperture without a sag (a sphere beyond a rectangle's corner) take the next inner ring's
    value on the same spoke; they only serve interpolation."""
    out = list(sags)
    for i, ((x, y), v) in enumerate(zip(points, out)):
        if v is None and i > 0 and not inside(ap, x, y):
            out[i] = out[i - SPOKES] if i > SPOKES else out[0]
    return out


def fill_beyond(points: list[tuple[float, float]], sags: list[float | None], clear: float) -> list[float | None]:
    """净口径之外读不到矢高的点（例如球面在机械口径处已过半球）取同一辐线内一圈的值，即在净口径外接一圈平台。
    Points beyond the clear aperture without a sag (a sphere past its hemisphere at the mechanical edge) take the next
    inner ring's value on the same spoke, i.e. a flat annulus outside the clear aperture."""
    out = list(sags)
    for i, ((x, y), v) in enumerate(zip(points, out)):
        if v is None and i > 0 and math.hypot(x, y) > clear:
            out[i] = out[i - SPOKES] if i > SPOKES else out[0]
    return out


def polar_points(sd: float) -> list[tuple[float, float]]:
    """局部 (x, y) 采样点，顺序与 sags 一致：中心，然后逐圈逐辐线。/ Local (x, y) samples, centre first, then ring by ring."""
    out = [(0.0, 0.0)]
    for k in range(1, RINGS + 1):
        r = sd * k / RINGS
        out += [(r * math.cos(2 * math.pi * m / SPOKES), r * math.sin(2 * math.pi * m / SPOKES)) for m in range(SPOKES)]
    return out


def _to_global(s: dict[str, Any], x: float, y: float, z: float) -> tuple[float, float, float]:
    r, o = s["rot"], s["off"]
    return (r[0] * x + r[1] * y + r[2] * z + o[0], r[3] * x + r[4] * y + r[5] * z + o[1], r[6] * x + r[7] * y + r[8] * z + o[2])


def _to_local(s: dict[str, Any], p: tuple[float, float, float]) -> tuple[float, float, float]:
    r, o = s["rot"], s["off"]
    d = (p[0] - o[0], p[1] - o[1], p[2] - o[2])
    return (r[0] * d[0] + r[3] * d[1] + r[6] * d[2], r[1] * d[0] + r[4] * d[1] + r[7] * d[2], r[2] * d[0] + r[5] * d[1] + r[8] * d[2])


def _sag_at(s: dict[str, Any], x: float, y: float) -> float | None:
    """在极坐标网格上双线性插值；孔径之外返回 None。/ Bilinear interpolation on the polar grid; None outside the aperture."""
    sd, sags = s["sd"], s["sags"]
    r = math.hypot(x, y)
    # 外缘那一圈（r = sd）要算进来：正透镜最薄在那里，排除它会把边厚报成内一圈的值
    # the rim ring (r = sd) counts: a positive lens is thinnest there, and leaving it out reports the next ring's thickness
    if r > sd * (1 + 1e-9) or ("ap" in s and not inside(s["ap"], x, y)):
        return None
    u = min(r / sd, 1.0) * RINGS
    k = min(int(u), RINGS - 1)
    fr = u - k
    v = (math.atan2(y, x) % (2 * math.pi)) / (2 * math.pi) * SPOKES
    m = int(v) % SPOKES
    ft = v - int(v)

    def ring(kk: int, mm: int) -> float:
        return sags[0] if kk == 0 else sags[1 + (kk - 1) * SPOKES + mm % SPOKES]

    inner = ring(k, m) * (1 - ft) + ring(k, m + 1) * ft
    outer = ring(k + 1, m) * (1 - ft) + ring(k + 1, m + 1) * ft
    return inner * (1 - fr) + outer * fr


def _points(s: dict[str, Any]) -> list[tuple[float, float, float]]:
    if "global" not in s:
        ap = s.get("ap")
        s["global"] = [_to_global(s, x, y, z) for (x, y), z in zip(polar_points(s["sd"]), s["sags"]) if ap is None or inside(ap, x, y)]
    return s["global"]


def _signed(s: dict[str, Any], p: tuple[float, float, float]) -> float | None:
    """点在面 s 的哪一侧：局部 z 减去该处矢高；不在口径内为 None。/ Which side of s a point lies on; None outside its aperture."""
    x, y, z = _to_local(s, p)
    sag = _sag_at(s, x, y)
    return None if sag is None else z - sag


def _near(a: dict[str, Any], b: dict[str, Any]) -> bool:
    reach = a["sd"] + b["sd"] + max(map(abs, a["sags"])) + max(map(abs, b["sags"])) + TOL
    return math.dist(a["off"], b["off"]) <= reach


def crossings(surfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """两两检查材料分界面（含反射镜）：一面在另一面口径内的采样点同时落在它两侧，说明两面相交。
    Pairwise over material boundaries (mirrors included): when one surface's samples inside another's aperture lie on
    both sides of it, the two surfaces cross."""
    out = []
    # 玻璃里的反射面（棱镜斜面）没有显式孔径时，实际范围由棱镜其他面决定，半口径常远大于它（实测光纤探头 0.85 对半宽 0.2），
    # 不参与检查；有矩形等显式孔径时照常检查
    # a reflecting face inside glass (a prism hypotenuse) without an explicit aperture is bounded by the prism's other faces,
    # not by its semi-diameter (0.85 against a half-width of 0.2 on the fibre probe), so it is left out; with an explicit
    # aperture it is checked like any other
    bounds = [s for s in surfaces if s.get("boundary") and not (s.get("in_glass_mirror") and not (s.get("ap") or {}).get("explicit"))]
    for i, a in enumerate(bounds):
        for b in bounds[i + 1:]:
            if not _near(a, b):
                continue
            depth = 0.0
            for one, other in ((a, b), (b, a)):
                d = [v for v in (_signed(one, p) for p in _points(other)) if v is not None]
                if d and max(d) > TOL and min(d) < -TOL:
                    depth = max(depth, min(max(d), -min(d)))
            if depth:
                out.append({"surfaces": [a["index"], b["index"]], "comments": [a.get("comment", ""), b.get("comment", "")], "depth": depth})
    return out


def elements(surfaces: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """两个相邻的折射分界面夹着玻璃的普通镜片（中间没有反射镜；两侧介质相同的虚设面跳过）。
    Plain lenses: glass between two consecutive refracting boundaries, no mirror in between (dummy surfaces skipped)."""
    out = []
    bounds = [s for s in surfaces if s.get("boundary")]
    for front, back in zip(bounds, bounds[1:]):
        material = (front.get("material") or "").upper()
        if material and material != "MIRROR" and (back.get("material") or "").upper() != "MIRROR" and front.get("sd") and back.get("sd"):
            out.append((front, back))
    return out


def _inside(element: tuple[dict[str, Any], dict[str, Any]], p: tuple[float, float, float]) -> bool:
    front, back = element
    df, db = _signed(front, p), _signed(back, p)
    if df is None or db is None:
        return False
    # 玻璃在前表面朝后表面顶点的一侧、后表面朝前表面顶点的一侧 / the glass lies on the side of each surface facing the other vertex
    sf = _signed(front, tuple(back["off"]))
    sb = _signed(back, tuple(front["off"]))
    if not sf or not sb:
        return False
    return df * math.copysign(1, sf) > TOL and db * math.copysign(1, sb) > TOL


def overlaps(surfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """镜片之间相互穿插：一片的表面采样点落在另一片的玻璃里（例如负空气间隔把整片镜片塞进另一片）。
    Lenses interpenetrating: samples of one lens's surfaces inside another lens's glass (for example a negative air gap
    that puts a whole lens inside another one)."""
    out = []
    els = elements(surfaces)
    for i, e1 in enumerate(els):
        for e2 in els[i + 1:]:
            if {id(s) for s in e1} & {id(s) for s in e2} or not _near(e1[0], e2[0]):
                continue
            hit = sum(_inside(e1, p) for s in e2 for p in _points(s)) + sum(_inside(e2, p) for s in e1 for p in _points(s))
            if hit:
                out.append({"lenses": [[e1[0]["index"], e1[1]["index"]], [e2[0]["index"], e2[1]["index"]]],
                            "comments": [e1[0].get("comment", ""), e2[0].get("comment", "")], "points": hit})
    return out


def mark_boundaries(surfaces: list[dict[str, Any]]) -> None:
    """给每个面标上 boundary（两侧材料不同，或是反射镜）和 in_glass_mirror（玻璃里的反射面）。material 是面后的材料，
    面前的介质取上一面的材料；坐标断点不换介质。
    Mark each surface with boundary (the media on its two sides differ, or it is a mirror) and in_glass_mirror. material
    is the medium after the surface, the medium before it is the previous surface's; a coordinate break changes nothing."""
    before = ""
    for s in surfaces:
        if "Coordinate" in str(s.get("type", "")):
            s["boundary"] = False
            continue
        after = (s.get("material") or "").upper()
        s["boundary"] = after == "MIRROR" or after != before
        s["in_glass_mirror"] = after == "MIRROR" and before not in ("", "AIR")
        if after != "MIRROR":
            before = after


def part_radii(surfaces: list[dict[str, Any]]) -> dict[int, float]:
    """每个面的零件外缘半径。surfaces 带 index、type、material，能画的面另带 clear（净口径外接圆半径）、edge（机械半口径与
    净口径加崩边区中的较大者）和 ap。没有显式孔径的面取 max(clear, edge)；一片镜片两面取两者中较大的外缘（镜片是一个外径），
    胶合面取它所在各片中最大的。显式孔径（棱镜的矩形面等）保持原样。
    The outer edge radius of each surface's part. surfaces carry index, type, material and, when drawable, clear (the
    clear aperture's circumscribed radius), edge (the larger of the mechanical semi-diameter and clear plus chip zone) and
    ap. A surface without an explicit aperture takes max(clear, edge); both faces of a lens take the larger outer edge of
    the two (a lens has one outer diameter), a cemented face the largest of its lenses. Explicit apertures stay as they are."""
    mark_boundaries(surfaces)
    drawable = [s for s in surfaces if "clear" in s]
    explicit = {s["index"] for s in drawable if (s.get("ap") or {}).get("explicit")}
    radius = {s["index"]: s["clear"] if s["index"] in explicit else max(s["clear"], s.get("edge") or 0.0) for s in drawable}
    for front, back in elements([dict(s, sd=s["clear"]) for s in drawable]):
        a, b = front["index"], back["index"]
        if a in explicit or b in explicit:
            continue
        outer = max(radius[a], radius[b])
        radius[a] = max(radius[a], outer)
        radius[b] = max(radius[b], outer)
    return radius


def clearances(surfaces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """相邻两个折射分界面之间的最小间隔：后一面在前一面口径内的采样点，沿前一面局部 z 到前一面的距离取最小，
    朝后一面顶点的方向为正。介质是玻璃时是这片镜片的最小厚度（正透镜在外缘、负透镜在中心），是空气时是最小空气间隔；
    radius 是最小值所在处离前一面轴线的距离，outer 是两面中较大的外缘半径。负值说明两面已经相交。
    The smallest separation between consecutive refracting boundaries: over the samples of the second surface inside the
    first one's aperture, the distance along the first surface's local z, positive toward the second vertex. In glass it is
    the lens's smallest thickness (at the edge of a positive lens, the centre of a negative one), in air the smallest air
    gap; radius is where it occurs, measured from the first surface's axis; outer is the larger outer edge of the two.
    A negative value means the surfaces cross."""
    out = []
    bounds = [s for s in surfaces if s.get("boundary")]
    for a, b in zip(bounds, bounds[1:]):
        if "MIRROR" in ((a.get("material") or "").upper(), (b.get("material") or "").upper()) or not _near(a, b):
            continue
        toward = _signed(a, tuple(b["off"]))
        sign = math.copysign(1.0, toward) if toward else 1.0
        best = None
        for p in _points(b):
            d = _signed(a, p)
            if d is None:
                continue
            x, y, _ = _to_local(a, p)
            if best is None or d * sign < best[0]:
                best = (d * sign, math.hypot(x, y))
        if best is not None:
            medium = (a.get("material") or "").upper() or "AIR"
            out.append({"surfaces": [a["index"], b["index"]], "comments": [a.get("comment", ""), b.get("comment", "")],
                        "medium": medium, "glass": medium != "AIR", "min": best[0], "radius": best[1], "outer": max(a["sd"], b["sd"])})
    return out


def summary(clash: dict[str, Any]) -> dict[str, Any]:
    """interference() 结果的摘要：相交、穿插、最薄玻璃、最小空气间隔、是否只按净口径算。
    A digest of interference(): crossings, overlaps, thinnest glass, smallest air gap, whether only clear apertures counted."""
    out: dict[str, Any] = {"crossings": clash.get("crossings") or [], "overlaps": clash.get("overlaps") or [],
                           "edges_from_clear": clash.get("edges_from_clear")}
    for glass, name in ((True, "thinnest_glass"), (False, "smallest_air_gap")):
        rows = [c for c in clash.get("clearances") or [] if bool(c.get("glass")) == glass]
        if rows:
            c = min(rows, key=lambda r: r["min"])
            out[name] = {k: c[k] for k in ("surfaces", "min", "radius", "outer")}
    return out


def merge_configurations(results: list[dict[str, Any]]) -> dict[str, Any]:
    """几个结构的 interference() 结果合成一份：相交和穿插逐条标上结构号（从 1 起），相邻面间隔取各结构中最小的并标出结构，
    镜片的净口径和外缘取各结构中最大的。
    Merge interference() results of several configurations: crossings and overlaps tagged with their configuration (from 1),
    each clearance the smallest over the configurations (tagged), each lens's clear and outer radius the largest."""
    out: dict[str, Any] = {"crossings": [], "overlaps": [], "configurations": len(results), "tolerance": TOL}
    gaps: dict[tuple[int, ...], dict[str, Any]] = {}
    lenses: dict[tuple[int, ...], dict[str, Any]] = {}
    for c, r in enumerate(results, start=1):
        out["crossings"] += [{**x, "configuration": c} for x in r.get("crossings") or []]
        out["overlaps"] += [{**x, "configuration": c} for x in r.get("overlaps") or []]
        for gap in r.get("clearances") or []:
            key = tuple(gap["surfaces"])
            if key not in gaps or gap["min"] < gaps[key]["min"]:
                gaps[key] = {**gap, "configuration": c}
        for e in r.get("lenses") or []:
            key = tuple(e["surfaces"])
            old = lenses.get(key)
            lenses[key] = e if old is None else {**e, "clear": max(e["clear"], old["clear"]), "outer": max(e["outer"], old["outer"])}
    out["clearances"] = list(gaps.values())
    out["lenses"] = list(lenses.values())
    out["edges_from_clear"] = bool(out["lenses"]) and all(e["outer"] <= e["clear"] + TOL for e in out["lenses"])
    return out


def nsc_overlaps(items: list[dict[str, Any]]) -> dict[str, Any]:
    """非序列物体的交叠检查：两个体物体（不含光源、探测器）的轮廓包围盒在三个方向都重叠超过 TOL，彼此没有 Inside Of 关系，
    且编号大的不是整个在编号小的里面（OpticStudio 的嵌套规则），就记为穿插。包围盒对倾斜或弯曲的物体偏保守（可能多报）；画不出轮廓的类型列在 unchecked。items 是 backend.nsc.objects 的结果。
    Interference of non-sequential objects: two volume objects (not sources or detectors) whose outline bounding boxes
    overlap by more than TOL on all three axes, and neither nested in the other via Inside Of. Boxes are conservative
    for tilted or curved objects; types without an outline are listed as unchecked."""
    from saos.backend import nsc

    parents = {it["index"]: it.get("inside_of", 0) for it in items}

    def ancestors(i: int) -> set[int]:
        out: set[int] = set()
        while parents.get(i):
            i = parents[i]
            if i in out:
                break
            out.add(i)
        return out

    boxes, unchecked = {}, []
    for it in items:
        if it["kind"] != "object":
            continue
        shape = nsc.outline(it)
        if (shape is None or not shape.get("closed") or not it.get("global")) and it.get("mesh_box"):
            # 没有解析轮廓的物体用导出网格的包围盒 / objects without an analytic outline use their exported mesh's box
            lo, hi = it["mesh_box"]
            boxes[it["index"]] = (list(lo), list(hi), it.get("comment", ""))
            continue
        if shape is None or not shape.get("closed") or not it.get("global"):
            unchecked.append(it["index"])
            continue
        pts = [p for e in shape["edges"] for p in nsc.to_global(e, it["global"])]
        boxes[it["index"]] = ([min(p[k] for p in pts) for k in range(3)], [max(p[k] for p in pts) for k in range(3)], it.get("comment", ""))
    overlaps = []
    keys = sorted(boxes)
    for n, a in enumerate(keys):
        for b in keys[n + 1:]:
            if a in ancestors(b) or b in ancestors(a):
                continue
            (lo1, hi1, c1), (lo2, hi2, c2) = boxes[a], boxes[b]
            # 编号大的物体整个在编号小的里面是 OpticStudio 的嵌套规则（后列的优先），不算穿插
            # a later object wholly inside an earlier one is OpticStudio's nesting rule (the later one wins), not a clash
            if all(lo1[k] - TOL <= lo2[k] and hi2[k] <= hi1[k] + TOL for k in range(3)):
                continue
            depth = min(min(hi1[k], hi2[k]) - max(lo1[k], lo2[k]) for k in range(3))
            if depth > TOL:
                overlaps.append({"objects": [a, b], "comments": [c1, c2], "depth": depth})
    spill, unchecked_detectors = detector_spill(items)
    return {"mode": "non_sequential", "crossings": [], "overlaps": overlaps, "clearances": [], "lenses": [], "edges_from_clear": False,
            "tolerance": TOL, "method": "bounding boxes of the object outlines (exported meshes for types without one); conservative for tilted or curved objects",
            "unchecked": unchecked, "detector_spill": spill, "detectors_unchecked": unchecked_detectors}


def _local(point: list[float], tf: dict[str, Any]) -> list[float]:
    """系统坐标的点到物体局部坐标（旋转矩阵正交，逆即转置）。/ A system point in the object's local frame."""
    r, o = tf["rotation"], tf["offset"]
    d = [point[k] - o[k] for k in range(3)]
    return [r[0] * d[0] + r[3] * d[1] + r[6] * d[2], r[1] * d[0] + r[4] * d[1] + r[7] * d[2], r[2] * d[0] + r[5] * d[1] + r[8] * d[2]]


def _in_mesh(tris: list, p: list[float]) -> bool:
    """射线法判断点是否在闭合网格内：沿一个略斜的方向数交点（Möller–Trumbore）。/ Ray-casting point-in-mesh test."""
    d = (0.5773, 0.5774, 0.5775)
    count = 0
    for a, b, c in tris:
        e1 = [b[i] - a[i] for i in range(3)]
        e2 = [c[i] - a[i] for i in range(3)]
        h = [d[1] * e2[2] - d[2] * e2[1], d[2] * e2[0] - d[0] * e2[2], d[0] * e2[1] - d[1] * e2[0]]
        det = sum(e1[i] * h[i] for i in range(3))
        if abs(det) < 1e-14:
            continue
        s = [p[i] - a[i] for i in range(3)]
        u = sum(s[i] * h[i] for i in range(3)) / det
        if u < 0 or u > 1:
            continue
        q = [s[1] * e1[2] - s[2] * e1[1], s[2] * e1[0] - s[0] * e1[2], s[0] * e1[1] - s[1] * e1[0]]
        v = sum(d[i] * q[i] for i in range(3)) / det
        if v < 0 or u + v > 1:
            continue
        if sum(e2[i] * q[i] for i in range(3)) / det > 0:
            count += 1
    return count % 2 == 1


def _contains(it: dict[str, Any], point: list[float]) -> bool | None:
    """点是否在体物体内：圆柱体、矩形体、环形体按参数算，其他按导出网格；都没有时 None。
    Whether a point lies inside a volume object: cylinders, boxes and annuli from their parameters, others from the mesh."""
    p = {str(q["name"]).strip().lower(): q["value"] for q in it.get("params") or [] if isinstance(q["value"], (int, float))}
    t = it["type"]
    if t in ("CylinderVolume", "RectangularVolume", "AnnularVolume") and it.get("global"):
        x, y, z = _local(point, it["global"])
        length = p.get("z length", 0.0)
        if not -TOL <= z <= length + TOL:
            return False
        f = z / length if length else 0.0
        if t == "CylinderVolume":
            rf, rb = p.get("front r", 0.0), p.get("back r", p.get("front r", 0.0))
            return math.hypot(x, y) <= rf + (rb - rf) * f + TOL
        if t == "RectangularVolume":
            hx = p.get("x1 half width", 0.0) + (p.get("x2 half width", p.get("x1 half width", 0.0)) - p.get("x1 half width", 0.0)) * f
            hy = p.get("y1 half width", 0.0) + (p.get("y2 half width", p.get("y1 half width", 0.0)) - p.get("y1 half width", 0.0)) * f
            return abs(x) <= hx + TOL and abs(y) <= hy + TOL

        def lerp(front: str, rear: str) -> float:
            return p.get(front, 0.0) + (p.get(rear, p.get(front, 0.0)) - p.get(front, 0.0)) * f

        ax_min, ay_min = lerp("front x min", "rear x min"), lerp("front y min", "rear y min")
        ax_max, ay_max = lerp("front x max", "rear x max"), lerp("front y max", "rear y max")
        outer = ax_max > 0 and ay_max > 0 and (x / ax_max) ** 2 + (y / ay_max) ** 2 <= 1 + 1e-9
        inner = ax_min > 0 and ay_min > 0 and (x / ax_min) ** 2 + (y / ay_min) ** 2 < 1 - 1e-9
        return outer and not inner
    if it.get("mesh_tris"):
        return _in_mesh(it["mesh_tris"], point)
    return None


def detector_spill(items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[int]]:
    """探测器超出它所在的体：探测器中心所在的体（编号最大的那个，OpticStudio 嵌套时后列的优先）之外、又不在吸收体里的探测器轮廓点，
    说明从这个体别处漏出的光可能被记下（杂散荧光任务实测：方形探测器四角伸出圆柱组织，多记了 1.8 个百分点）。
    中心不在任何体里的探测器不查；包含关系算不出时列在第二个返回值。
    Detectors reaching outside the volume that holds their centre (the highest-numbered one) at a point not inside an
    absorber: light leaking out of that volume elsewhere can land on them. Returns the findings and the detectors that
    could not be checked."""
    from saos.backend import nsc

    volumes = [it for it in items if it["kind"] == "object"]
    found, unchecked = [], []
    for det in items:
        if det["kind"] != "detector" or not det.get("global"):
            continue
        shape = nsc.outline(det)
        if shape is None:
            unchecked.append(det["index"])
            continue
        pts = [q for e in shape["edges"] for q in nsc.to_global(e, det["global"])]
        center = nsc.to_global([[0.0, 0.0, 0.0]], det["global"])[0]
        holders = []
        unknown = False
        for v in volumes:
            inside = _contains(v, center)
            if inside is None:
                unknown = True
            elif inside:
                holders.append(v)
        if not holders:
            if unknown:
                unchecked.append(det["index"])
            continue
        holder = max(holders, key=lambda v: v["index"])
        absorbers = [v for v in volumes if str(v.get("material", "")).upper() == "ABSORB"]
        for q in pts:
            if _contains(holder, q) is False and not any(_contains(a, q) for a in absorbers):
                found.append({"detector": det["index"], "comment": det.get("comment", ""), "volume": holder["index"],
                              "volume_comment": holder.get("comment", ""), "point": [round(c, 6) for c in q]})
                break
    return found, unchecked


def interference(surfaces: list[dict[str, Any]]) -> dict[str, Any]:
    """surfaces：按序号排列，每个带 index、type、comment、material（面后的材料）、sd（网格半径，即零件外缘）、rot（行优先 3×3）、off、
    sags（与 polar_points(sd) 对应），可选 ap（aperture() 的结果）和 clear（净口径半径，缺省同 sd）。两侧材料不同（或是反射镜）的面
    才是分界面（mark_boundaries）。返回相交（crossings）、穿插（overlaps）、相邻面最小间隔（clearances）和各镜片的净口径与外缘（lenses）。
    surfaces in index order, each with index, type, comment, material (after the surface), sd (grid radius: the part's
    outer edge), rot (row-major 3×3), off, sags matching polar_points(sd), and optionally ap (from aperture()) and clear
    (the clear radius, default sd). A surface is a boundary when the media on its two sides differ, or it is a mirror
    (mark_boundaries). Returns crossings, overlaps, the smallest separation of consecutive surfaces (clearances) and each
    lens's clear and outer radius (lenses)."""
    mark_boundaries(surfaces)
    usable = [s for s in surfaces if s.get("sd") and s.get("rot") and s.get("sags") and None not in s["sags"]]
    lenses = [{"surfaces": [f["index"], b["index"]], "clear": max(f.get("clear", f["sd"]), b.get("clear", b["sd"])), "outer": max(f["sd"], b["sd"])}
              for f, b in elements(usable)]
    return {"crossings": crossings(usable), "overlaps": overlaps(usable), "clearances": clearances(usable), "lenses": lenses,
            # 没有一片镜片的外缘大于净口径：检查只覆盖净口径，装配余量和机械外径都没算进去
            # no lens edge beyond its clear aperture: the check covers clear apertures only, no allowance or mechanical diameter
            "edges_from_clear": bool(lenses) and all(e["outer"] <= e["clear"] + TOL for e in lenses), "tolerance": TOL}
