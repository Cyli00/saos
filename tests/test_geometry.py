"""结构交叠检查的离线测试：球面镜片用解析矢高构造，不需要 OpticStudio。"""

from __future__ import annotations

import math

from saos import geometry

IDENTITY = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


def surface(index, z, radius=math.inf, sd=5.0, material="", rot=IDENTITY, off=None, comment=""):
    def sag(x, y):
        if math.isinf(radius):
            return 0.0
        r2 = x * x + y * y
        return r2 / (radius * (1 + math.sqrt(1 - r2 / radius ** 2)))

    return {"index": index, "type": "Standard", "comment": comment, "material": material, "sd": sd, "rot": rot,
            "off": off or [0.0, 0.0, z], "sags": [sag(x, y) for x, y in geometry.polar_points(sd)]}


def lens(index, z, thickness, r1=20.0, r2=-20.0, sd=5.0):
    return [surface(index, z, r1, sd, "N-BK7"), surface(index + 1, z + thickness, r2, sd)]


def test_separate_lenses_and_a_cemented_doublet_are_clean():
    cemented = [surface(5, 20.0, 30.0, 5, "N-BK7"), surface(6, 24.0, -15.0, 5, "F2"), surface(7, 26.0, -60.0, 5)]
    out = geometry.interference([*lens(1, 0.0, 4.0), *lens(3, 8.0, 4.0), *cemented, surface(8, 40.0)])
    assert out["crossings"] == [] and out["overlaps"] == [] and out["tolerance"] == geometry.TOL


def test_a_biconvex_lens_with_negative_edge_thickness_crosses_itself():
    # 两面矢高各约 0.64 mm（R 20、半口径 5），中心厚 1 mm 时边缘厚约 −0.28 mm / sag ≈ 0.64 mm each: edge ≈ −0.28 mm at 1 mm centre
    out = geometry.interference(lens(1, 0.0, 1.0))
    assert [c["surfaces"] for c in out["crossings"]] == [[1, 2]] and out["crossings"][0]["depth"] > 0.1


def test_a_negative_air_gap_that_buries_a_lens_inside_another_is_an_overlap():
    thin = [surface(3, 1.5, 200.0, 2.0, "N-BK7"), surface(4, 2.0, -200.0, 2.0)]  # 整片在第一片玻璃里 / wholly inside the first lens
    out = geometry.interference([*lens(1, 0.0, 4.0), *thin])
    assert out["crossings"] == [] and [o["lenses"] for o in out["overlaps"]] == [[[1, 2], [3, 4]]]


def test_a_tilted_wall_through_a_lens_is_a_crossing_and_coordinate_breaks_keep_the_medium():
    c, s = math.cos(math.radians(30)), math.sin(math.radians(30))
    tilted = [1.0, 0.0, 0.0, 0.0, c, -s, 0.0, s, c]  # 绕 x 轴转 30° / 30° about x
    wall = [surface(3, 2.0, sd=8.0, material="SILICA", rot=tilted, comment="tube inner wall"),
            {"index": 4, "type": "Coordinate Break", "material": "", "sd": 0.0},
            surface(5, 3.0, sd=8.0, rot=tilted, comment="tube outer wall")]
    out = geometry.interference([*lens(1, 0.0, 4.0), *wall])
    pairs = [c["surfaces"] for c in out["crossings"]]
    assert [1, 3] in pairs or [2, 3] in pairs
    assert all(4 not in p for p in pairs) and out["crossings"][0]["comments"][1] in ("tube inner wall", "tube outer wall")
    # 坐标断点之后仍在 SILICA 里，外壁是分界面 / still in SILICA after the break, so the outer wall is a boundary
    assert wall[2]["boundary"] and not wall[1]["boundary"]


def test_prism_faces_and_dummy_surfaces_do_not_raise_false_alarms():
    """玻璃里的反射面（棱镜斜面）半口径常比实际面大得多，不参与相交检查；两侧同材料的虚设面不当作镜片表面。"""
    c = math.cos(math.radians(45))
    hyp = [1.0, 0.0, 0.0, 0.0, c, -c, 0.0, c, c]
    prism = [surface(1, 0.0, sd=0.3, material="N-BK7", comment="prism entrance"),
             surface(2, 0.2, sd=0.85, material="MIRROR", rot=hyp, comment="prism hypotenuse"),
             surface(3, 0.2, sd=0.31, material="N-BK7", comment="dummy exit")]
    out = geometry.interference(prism)
    assert out["crossings"] == [] and out["overlaps"] == [] and out["tolerance"] == geometry.TOL
    assert prism[1]["in_glass_mirror"] and not prism[2]["boundary"]
    # 空气里的反射镜照常检查 / a mirror in air is still checked
    fold = surface(3, 2.0, sd=8.0, material="MIRROR", rot=hyp)
    assert [c["surfaces"] for c in geometry.interference([*lens(1, 0.0, 4.0), fold])["crossings"]]


def with_aperture(s, spec):
    """按面孔径重取网格（与 ZosBackend._geometry_items 相同的做法）。/ Re-sample on the aperture grid as the backend does."""
    ap = geometry.aperture(spec, s["sd"])
    s = {k: v for k, v in s.items() if k != "global"}  # 上一次检查缓存的采样点 / sample points cached by an earlier check
    return {**s, "ap": ap, "sd": geometry.extent(ap), "sags": [0.0] * len(geometry.polar_points(geometry.extent(ap)))}


def test_square_prism_faces_are_checked_by_their_rectangular_apertures():
    """0.4 mm 立方棱镜：入射面和出射面相互垂直，在棱边相接。按半口径的圆盘会在角上互相穿过（旧模型实测误报 0.07 mm），
    按矩形孔径不相交；斜面有显式孔径后照常参与检查。
    A 0.4 mm cube prism: entrance and exit faces meet at an edge. Semi-diameter discs pass through each other at the corner
    (a 0.07 mm false alarm on an old model); the rectangular apertures do not, and the hypotenuse with an explicit aperture
    is checked."""
    c = math.cos(math.radians(45))
    hyp = [1.0, 0.0, 0.0, 0.0, c, -c, 0.0, c, c]
    down = [1.0, 0.0, 0.0, 0.0, 0.0, -1.0, 0.0, 1.0, 0.0]  # 出射面法线沿 -y / exit face normal along -y
    square = {"type": "SurfaceApertureTypes.RectangularAperture", "XHalfWidth": 0.2, "YHalfWidth": 0.2}
    entrance = surface(1, 0.0, sd=0.26, material="N-BK7", comment="prism entrance")
    mirror = surface(2, 0.2, sd=0.85, material="MIRROR", rot=hyp, comment="hypotenuse")
    exit_ = surface(3, 0.2, sd=0.27, material="", rot=down, off=[0.0, -0.2, 0.2], comment="prism exit")
    discs = geometry.interference([entrance, mirror, exit_])
    assert [c["surfaces"] for c in discs["crossings"]] == [[1, 3]]
    squared = [with_aperture(entrance, square), with_aperture(mirror, {**square, "XHalfWidth": 0.2, "YHalfWidth": 0.2 * math.sqrt(2)}),
               with_aperture(exit_, square)]
    assert geometry.interference(squared)["crossings"] == []
    # 斜面沿光轴错开 0.1 mm 就穿过入射面 / a hypotenuse shifted 0.1 mm along the axis passes through the entrance face
    shifted = with_aperture(surface(2, 0.1, sd=0.85, material="MIRROR", rot=hyp), {**square, "YHalfWidth": 0.2 * math.sqrt(2)})
    assert [c["surfaces"] for c in geometry.interference([squared[0], shifted, squared[2]])["crossings"]] == [[1, 2]]


def test_aperture_outline_chord_and_extent():
    rect = geometry.aperture({"type": "RectangularAperture", "XHalfWidth": 0.3, "YHalfWidth": 0.2, "ApertureYDecenter": 0.1}, 1.0)
    rim = geometry.outline(rect, 32)
    assert len(rim) == 33 and rim[0] == (0.3, 0.1) and abs(rim[8][1] - 0.3) < 1e-12
    assert geometry.chord(rect, "y") == (-0.1, 0.30000000000000004, 0.0) and geometry.chord(rect, "x") == (-0.3, 0.3, 0.0)
    assert abs(geometry.extent(rect) - math.hypot(0.3, 0.3)) < 1e-12
    circle = geometry.aperture({"type": "CircularAperture", "MinimumRadius": 0.0, "MaximumRadius": 0.25}, 1.0)
    assert circle["explicit"] and geometry.extent(circle) == 0.25
    assert geometry.aperture({"type": "None"}, 0.4) == {"shape": "circle", "hx": 0.4, "hy": 0.4, "dx": 0.0, "dy": 0.0, "rmin": 0.0, "explicit": False}
