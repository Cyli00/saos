"""结构交叠检查：面与面相交、镜片之间相互穿插。只用 ZOS-API 给出的面变换（GetGlobalMatrix）和面形（GetSag），
这里只做坐标变换和比较，不涉及光学计算。
Interference check: surfaces crossing each other and lenses interpenetrating. Uses only the surface transforms
(GetGlobalMatrix) and shapes (GetSag) from ZOS-API; this module does coordinate transforms and comparisons, no optics.

每个面在自身局部坐标里按极坐标网格取矢高：RINGS 圈（含边缘）× SPOKES 条辐线，加中心点。
Each surface is sampled in its local frame on a polar grid: RINGS rings (the rim included) × SPOKES spokes, plus the centre.
"""

from __future__ import annotations

import math
from typing import Any

RINGS = 4
SPOKES = 16
# 越过另一面超过这个距离（镜头单位）才算相交，挡住数值噪声和贴合的胶合面 / beyond numerical noise and cemented contact
TOL = 1e-4


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
    """在极坐标网格上双线性插值；口径之外返回 None。/ Bilinear interpolation on the polar grid; None outside the aperture."""
    sd, sags = s["sd"], s["sags"]
    r = math.hypot(x, y)
    if r >= sd:
        return None
    u = r / sd * RINGS
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
        s["global"] = [_to_global(s, x, y, z) for (x, y), z in zip(polar_points(s["sd"]), s["sags"])]
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
    # 玻璃里的反射面（棱镜斜面）的实际范围由棱镜其他面决定，半口径常远大于它（实测光纤探头 0.85 对半宽 0.2），不参与检查
    # a reflecting face inside glass (a prism hypotenuse) is bounded by the prism's other faces, not by its semi-diameter
    # (0.85 against a half-width of 0.2 on the fibre probe), so it is left out
    bounds = [s for s in surfaces if s.get("boundary") and not s.get("in_glass_mirror")]
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


def interference(surfaces: list[dict[str, Any]]) -> dict[str, Any]:
    """surfaces：按序号排列，每个带 index、type、comment、material（面后的材料）、sd、rot（行优先 3×3）、off、sags（与 polar_points 对应）。
    material 之前的介质取上一面的材料；两侧材料不同（或是反射镜）的面才是分界面。
    surfaces in index order, each with index, type, comment, material (after the surface), sd, rot (row-major 3×3), off, and
    sags matching polar_points. A surface is a boundary when the media on its two sides differ, or it is a mirror."""
    before = ""
    for s in surfaces:
        if "Coordinate" in str(s.get("type", "")):
            # 坐标断点不换介质 / a coordinate break does not change the medium
            s["boundary"] = False
            continue
        after = (s.get("material") or "").upper()
        s["boundary"] = after == "MIRROR" or after != before
        s["in_glass_mirror"] = after == "MIRROR" and before not in ("", "AIR")
        if after != "MIRROR":
            before = after
    usable = [s for s in surfaces if s.get("sd") and s.get("rot") and s.get("sags") and None not in s["sags"]]
    return {"crossings": crossings(usable), "overlaps": overlaps(usable), "tolerance": TOL}
