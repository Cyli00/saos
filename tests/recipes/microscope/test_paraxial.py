from __future__ import annotations

import math

import pytest

from saos.recipes.microscope.paraxial import Surface, back_focal_distance, effective_focal_length, front_focal_distance


def test_thick_biconvex_lens():
    # r1 = 50, r2 = -50, d = 5, n = 1.5：
    # 1/f = (n-1)(1/r1 - 1/r2 + (n-1)d/(n r1 r2))，BFD = f(1 - (n-1)d/(n r1))
    lens = [Surface(50.0, 5.0, 1.5), Surface(-50.0, 0.0, 1.0)]
    f = 1 / (0.5 * (1 / 50 + 1 / 50 - 0.5 * 5 / (1.5 * 50 * -50) * -1))
    assert effective_focal_length(lens) == pytest.approx(f, rel=1e-12)
    bfd = f * (1 - 0.5 * 5 / (1.5 * 50))
    assert back_focal_distance(lens) == pytest.approx(bfd, rel=1e-12)
    assert front_focal_distance(lens) == pytest.approx(bfd, rel=1e-12)


def test_plano_convex_orientation():
    # 平凸透镜，平面朝物：前焦距 = f - d/n，后焦距 = f
    lens = [Surface(math.inf, 4.0, 1.6), Surface(-30.0, 0.0, 1.0)]
    f = 30 / 0.6
    assert effective_focal_length(lens) == pytest.approx(f)
    assert back_focal_distance(lens) == pytest.approx(f)
    assert front_focal_distance(lens) == pytest.approx(f - 4.0 / 1.6)
