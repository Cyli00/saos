"""布局图的 2D 剖面平面。轴对称系统画全局 YZ 面；带折转的系统（45° 反射镜、坐标断点）只要整体关于某个平面对称，
就画这个平面：每个面都有一条截线（子午或弧矢）整条落在平面内，并且有一个光线扇也在平面内。找不到时为 None，界面只给 3D。
只用 view.json / viz.json 里已有的全局坐标，不需要 OpticStudio。
The plane of the layout's 2D section. An axial system is drawn in the global YZ plane; a folded system (45° mirror,
coordinate breaks) is drawn in its plane of symmetry when every surface has one section line (tangential or sagittal)
lying in it and one ray fan does too. None when there is no such plane, and the window then offers 3D only.
Works on the global coordinates already in view.json / viz.json; no OpticStudio needed.
"""

from __future__ import annotations

import math
from typing import Any

Vec = tuple[float, float, float]
AXES = {"x": (1.0, 0.0, 0.0), "y": (0.0, 1.0, 0.0), "z": (0.0, 0.0, 1.0)}
# 点到平面的容差，相对于整个系统的尺寸 / tolerance of a point off the plane, relative to the system size
REL_TOL = 1e-6


def _sub(a, b) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a, b) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _unit(a) -> Vec | None:
    n = math.sqrt(_dot(a, a))
    return None if n < 1e-12 else (a[0] / n, a[1] / n, a[2] / n)


def _candidates(surfaces: list[dict[str, Any]], vertices: list[Vec]) -> list[Vec]:
    """先试全局 YZ、XZ 面，再试所有顶点张成的平面和各条弯曲截线所在的平面。"""
    out: list[Vec] = [AXES["x"], AXES["y"]]
    for k in range(2, len(vertices)):
        n = _unit(_cross(_sub(vertices[1], vertices[0]), _sub(vertices[k], vertices[0])))
        if n:
            out.append(n)
            break
    for s in surfaces:
        for key in ("profile_y", "profile_x"):
            p = s[key]
            chord = _sub(p[-1], p[0])
            mid = tuple((a + b) / 2 for a, b in zip(p[0], p[-1]))
            n = _unit(_cross(chord, _sub(p[len(p) // 2], mid)))
            if n:
                out.append(n)
    unique: list[Vec] = []
    for n in out:
        if all(abs(_dot(n, m)) < 1 - 1e-9 for m in unique):
            unique.append(n)
    return unique


def _in_plane(points, n: Vec, c: float, tol: float) -> bool:
    return all(abs(_dot(n, p) - c) <= tol for p in points)


def _name(axis: Vec, fallback: str) -> tuple[Vec, str]:
    """贴近某个全局轴时用它的名字，方向取正；否则用 fallback。"""
    for name, a in AXES.items():
        d = _dot(axis, a)
        if abs(d) > 1 - 1e-9:
            return (a, name)
    return (axis, fallback)


def section_plane(viz: dict[str, Any]) -> dict[str, Any] | None:
    """返回 {normal, offset, u, v, u_name, v_name, profiles: {面号: "profile_y"|"profile_x"}, fan: "px"|"py"}：
    u 为横轴、v 为纵轴（都在平面内），fan 为平面内那个光线扇在光瞳上为 0 的坐标（与 2D 只画 px = 0 的子午扇相同含义）。
    Returns the plane with the in-plane axes u (horizontal) and v (vertical), the section line of each surface, and
    the pupil coordinate that is 0 on the in-plane ray fan."""
    surfaces = [s for s in viz.get("surfaces") or [] if s.get("profile_y") and s.get("profile_x") and s.get("vertex")]
    if not surfaces:
        return None
    vertices = [tuple(s["vertex"]) for s in surfaces]
    points = [p for s in surfaces for p in s.get("rim") or s["profile_y"]] + vertices
    size = max((max(p[k] for p in points) - min(p[k] for p in points) for k in range(3)), default=0.0)
    tol = max(REL_TOL * size, 1e-12)
    rays = (viz.get("layout") or {}).get("rays") or []
    first_field = min((r["field"] for r in rays), default=None)
    for n in _candidates(surfaces, vertices):
        c = _dot(n, vertices[0])
        if not _in_plane(vertices, n, c, tol):
            continue
        profiles = {}
        for s in surfaces:
            key = next((k for k in ("profile_y", "profile_x") if _in_plane(s[k], n, c, tol)), None)
            if key is None:
                break
            profiles[s["index"]] = key
        else:
            # 第一个视场（通常是轴上）的某个扇整条在平面内 / one fan of the first field lies wholly in the plane
            fan = None
            for zero in ("px", "py"):
                traced = [r for r in rays if r["field"] == first_field and r[zero] == 0]
                if traced and all(_in_plane(r["points"], n, c, tol) for r in traced):
                    fan = zero
                    break
            if rays and fan is None:
                continue
            # 横轴取全局 z 在平面内的投影（光轴大致沿 z）；纵轴与它垂直、主分量取正
            # u: the global z axis projected into the plane; v: perpendicular to it, its largest component positive
            ref = AXES["z"] if abs(n[2]) < 0.9 else min(AXES["x"], AXES["y"], key=lambda a: abs(_dot(a, n)))
            u = _unit(_sub(ref, tuple(_dot(ref, n) * x for x in n)))
            v = _unit(_cross(n, u))
            if max(v, key=abs) < 0:
                v = (-v[0], -v[1], -v[2])
            u, u_name = _name(u, "u")
            v, v_name = _name(v, "v")
            return {"normal": list(n), "offset": c, "u": list(u), "v": list(v), "u_name": u_name, "v_name": v_name,
                    "profiles": profiles, "fan": fan or "px"}
    return None
