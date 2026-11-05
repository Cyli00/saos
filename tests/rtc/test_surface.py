"""面型矢高回归：只用已经实测/可直接推导的对照值。

ORACLE.md 记录的两条 SSAG 实测（R=50, c=0.02, k=0）：
  h=1 → 0.010001000200050014
  h=6 → 0.36130541603657362
其余是解析恒等式（对称性/退化），不是 OpticStudio 实测——它们只能抓实现错误，
不能替代 SSAG 回归。
"""

from __future__ import annotations

import numpy as np
import pytest

from rtc import geometry
from rtc.zmx import Surface


def sag(s, x, y):
    return geometry.make(s).sag(x, y)


# ---------------------------------------------------------------- 旋转对称


def test_conic_matches_recorded_ssag():
    s = Surface(index=1, type="STANDARD", curvature=0.02, conic=0.0)
    for h, expected in ((1.0, 0.010001000200050014), (6.0, 0.36130541603657362)):
        z, _, _ = sag(s, h, 0.0)
        assert z == pytest.approx(expected, abs=5e-16)
        # 旋转对称：沿 x 与沿 y 同值
        assert sag(s, 0.0, h)[0] == pytest.approx(z, abs=0.0)


def test_standard_gradient_matches_finite_difference():
    s = Surface(index=1, type="STANDARD", curvature=0.05, conic=-1.2)
    x, y = np.array([0.3, -0.7]), np.array([0.2, 0.4])
    _, gx, gy = sag(s, x, y)
    h = 1e-7
    fd_x = (sag(s, x + h, y)[0] - sag(s, x - h, y)[0]) / (2 * h)
    fd_y = (sag(s, x, y + h)[0] - sag(s, x, y - h)[0]) / (2 * h)
    assert np.allclose(gx, fd_x, atol=1e-8)
    assert np.allclose(gy, fd_y, atol=1e-8)


def test_even_asph_coefficient_semantics():
    # 手册 / SSAG 差分实测：参数 1 是 ρ² 上的系数，无归一化半径
    s = Surface(index=1, type="EVENASPH", curvature=0.02, conic=0.0, params={1: 0.01})
    h = 6.0
    conic_only = sag(Surface(index=1, curvature=0.02), h, 0.0)[0]
    z, _, _ = sag(s, h, 0.0)
    assert z - conic_only == pytest.approx(0.01 * h**2, rel=1e-13)  # 0.36，实测值
    # 只有偶数次项：z(ρ) 关于原点对称
    assert sag(s, -h, 0.0)[0] == pytest.approx(z, abs=0.0)


# ---------------------------------------------------------------- 定向面形


def _biconic(rx, ky, ry=None, kx=None, **params):
    ry = rx if ry is None else ry
    kx = ky if kx is None else kx
    p = {1: rx, 2: kx}
    p.update(params)
    return Surface(index=1, type="BICONICX", curvature=0.0 if ry in (0, np.inf) else 1.0 / ry, conic=ky, params=p)


def test_biconic_degenerates_to_rotationally_symmetric_conic():
    rx = ry = 5.0
    k = -0.5
    s = _biconic(rx, k)
    ref = Surface(index=1, type="STANDARD", curvature=1.0 / ry, conic=k)
    for x, y in ((0.0, 0.0), (0.4, 0.3), (-0.9, 0.2)):
        assert sag(s, x, y)[0] == pytest.approx(sag(ref, x, y)[0], rel=1e-15)
        assert sag(s, x, y)[1] == pytest.approx(sag(ref, x, y)[1], rel=1e-12)


def test_biconic_has_independent_x_and_y_curvature():
    # 参数 1 = Rx，参数 2 = kx；LDE 半径/圆锥作用于 Y
    s = _biconic(2.0, -1.0, ry=4.0, kx=-2.0)
    cx, kx, cy, ky = 1 / 2.0, -2.0, 1 / 4.0, -1.0
    zx, _, _ = sag(s, 1.0, 0.0)
    zy, _, _ = sag(s, 0.0, 1.0)
    # 各自只受本方向曲率/圆锥影响：z = c·u²/(1+√(1−(1+k)c²u²))
    assert zx == pytest.approx(cx / (1 + np.sqrt(1 - (1 + kx) * cx**2)), rel=1e-15)
    assert zy == pytest.approx(cy / (1 + np.sqrt(1 - (1 + ky) * cy**2)), rel=1e-15)
    assert zx != pytest.approx(zy, rel=1e-3)


def test_biconic_zernike_polynomial_terms_use_x1_and_y1_slots():
    # 参数 13 = x¹，14 = x²，29 = y¹，30 = y²
    s = _biconic(np.inf, 0.0, ry=np.inf, kx=0.0)
    s.type = "BICONICZ"
    s.params.update({13: 0.5, 14: -0.25, 29: 0.125, 30: 0.75})
    z, _, _ = sag(s, 0.2, 0.4)
    expected = 0.5 * 0.2 + (-0.25) * 0.04 + 0.125 * 0.4 + 0.75 * 0.16
    assert z == pytest.approx(expected, rel=1e-15)


def test_biconic_zernike_refuses_active_zernike_terms():
    s = _biconic(np.inf, 0.0, ry=np.inf, kx=0.0)
    s.type = "BICONICZ"
    s.params.update({3: 9, 4: 1.0})
    with pytest.raises(NotImplementedError):
        sag(s, 0.1, 0.1)


def test_toroid_with_zero_rotation_radius_is_a_cylinder():
    # 三件镜头的参数 1 全是 0 → 旋转半径无穷 → 圆柱面：只随 y 变
    s = Surface(index=1, type="TOROIDAL", curvature=1.0 / -0.3, conic=0.0, params={1: 0.0})
    y = np.array([0.05, -0.1, 0.2])
    z1, _, _ = sag(s, np.zeros_like(y), y)
    z2, _, _ = sag(s, np.full_like(y, 0.15), y)
    assert np.allclose(z1, z2, atol=0.0)
    assert np.allclose(z1, sag(Surface(index=1, curvature=1.0 / -0.3), np.zeros_like(y), y)[0], atol=0.0)


def test_toroid_refuses_finite_rotation_radius():
    with pytest.raises(NotImplementedError):
        sag(Surface(index=1, type="TOROIDAL", curvature=0.1, params={1: 100.0}), 0.0, 0.0)


def test_paraxial_surface_is_plane():
    s = Surface(index=1, type="PARAXIAL", curvature=0.0, params={1: 4.135, 2: 1.0})
    z, gx, gy = sag(s, np.array([0.1, -0.2]), np.array([0.3, 0.0]))
    assert np.all(z == 0.0) and np.all(gx == 0.0) and np.all(gy == 0.0)
