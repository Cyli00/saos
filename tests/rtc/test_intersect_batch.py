"""求交的两种独立解法互相核对：各面型自己的求交方式（二次曲面闭式解、非球面从闭式解起的 Newton）与通用的括号保护
Newton，并检查交点满足矢高方程。只能抓实现错误，不替代 oracle 回归。

与 OpticStudio 的对照见 tools/compare_golden.py（三件历史镜头）和 saos 的 tests/test_zemax_rtc.py（4 面双胶合）。
"""

from __future__ import annotations

import numpy as np
import pytest

from rtc import geometry, kernel
from rtc.zmx import Surface


def _rays(n: int = 400, seed: int = 7):
    rng = np.random.default_rng(seed)
    x0 = rng.uniform(-6, 6, n)
    y0 = rng.uniform(-6, 6, n)
    z0 = rng.uniform(-30, -1, n)
    d = np.stack([rng.normal(0, 0.08, n), rng.normal(0, 0.08, n), np.ones(n)], axis=-1)
    d /= np.linalg.norm(d, axis=-1, keepdims=True)
    return x0, y0, z0, d[:, 0], d[:, 1], d[:, 2]


@pytest.mark.parametrize("surface", [
    Surface(index=1, type="STANDARD", curvature=1 / 50.0, conic=-0.5),
    Surface(index=1, type="STANDARD", curvature=-1 / 20.0),
    Surface(index=1, type="EVENASPH", curvature=1 / 40.0, conic=0.3, params={2: 1e-5, 3: -2e-8}),
    Surface(index=1, type="BICONICX", curvature=1 / 30.0, conic=-1.2, params={1: 80.0, 2: 0.5}),
], ids=["conic", "concave", "evenasph", "biconic"])
def test_shape_solver_matches_the_general_solver(surface):
    shape = geometry.make(surface)
    rays = _rays()
    t_batch = kernel.intersect(shape.mode, shape.row(), *rays)
    t_general = kernel.intersect(kernel.BRACKET, shape.row(), *rays)
    hit = np.isfinite(t_batch)
    assert hit.mean() > 0.9 and np.isfinite(t_general[hit]).all()
    assert np.max(np.abs(t_batch[hit] - t_general[hit])) < 1e-10
    # 交点满足矢高方程 / the intersection satisfies the sag equation
    x = rays[0] + t_batch * rays[3]
    y = rays[1] + t_batch * rays[4]
    z = rays[2] + t_batch * rays[5]
    sag = np.asarray(shape.sag(x[hit], y[hit])[0])
    assert np.max(np.abs(sag - z[hit])) < 1e-11


def test_conic_miss_is_nan():
    shape = geometry.make(Surface(index=1, type="STANDARD", curvature=1 / 5.0))
    # 半径 5 的球，离轴 8 mm 平行入射：错过曲面 / a ray 8 mm off axis misses a sphere of radius 5
    t = kernel.intersect(shape.mode, shape.row(), [8.0, 1.0], [0.0, 0.0], [-10.0, -10.0], [0.0, 0.0], [0.0, 0.0], [1.0, 1.0])
    assert np.isnan(t[0]) and np.isfinite(t[1])
