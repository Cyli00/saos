"""AD 雅可比与中心差分互相核对：trace_ad 对曲率、零值面形系数、厚度的切向，应与 Design.system_with 改参数后普通追迹的
中心差分一致，值部分应与普通追迹逐位相同。只能抓实现错误，不替代 oracle 回归。

saos 的 AsphereExpert(engine="rtc") 用它给非球面排名（saos ADR-0015），一次追迹一批光线、在零系数处求导。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from rtc import dual as D
from rtc import zmx
from rtc.adtrace import Variable, trace_ad
from rtc.optimize import Design
from rtc.system import OpticalSystem
from rtc.trace import trace

FIX = Path(__file__).resolve().parent / "fixtures"


def test_constant_and_parameter_duals_combine_along_the_parameter_axis():
    rays = np.array([1.0, 2.0])
    c = D.Dual.param(0.5, 0, 2)          # 标量参数，切向 (2,)
    x = D.Dual(rays, np.zeros((2, 2)))   # 两条光线，参数个数恰好也是 2
    out = c * x + D.Dual.konst(1.0)
    assert out.v.tolist() == [1.5, 2.0]
    # 对参数 0 的导数是光线值本身，对参数 1 为 0；按尾部对齐会把参数轴错当成光线轴
    assert out.d.tolist() == [[1.0, 2.0], [0.0, 0.0]]
    assert (D.Dual.konst(3.0) + c).d.tolist() == [1.0, 0.0]


def test_ad_jacobian_matches_central_differences_on_a_batch_of_rays():
    # piA：EPD、不瞄准、光阑在第 1 面，变量都在光阑之后，发射不随变量变
    lens = zmx.parse(FIX / "lenses" / "piA-r012.zmx")
    system = OpticalSystem(lens, wavelength_um=lens.active_wavelengths[0][0], config=1)
    item = {it.index: k for k, it in enumerate(system.items)}
    image = system.items[-1].index
    variables = [Variable(item[4], "curvature"), Variable(item[5], "curvature"),
                 Variable(item[10], "param", 2), Variable(item[10], "param", 3),  # 两个 Toroidal 系数当前都是 0
                 Variable(item[image - 1], "thickness")]
    design = Design(system, variables)
    theta = design.values()
    assert theta[2] == 0.0 and theta[3] == 0.0
    grid = np.linspace(-1.0, 1.0, 5)
    px, py = (a.ravel() for a in np.meshgrid(grid, grid))
    inside = px * px + py * py <= 1.0
    px, py = px[inside], py[inside]

    got = trace_ad(system, px, py, variables, theta).at(image)
    real = trace(system, px, py).at(image)
    for axis in ("x", "y"):
        # 两条路径的运算顺序不同，只差舍入（实测 ≤ 2e-15 mm）
        np.testing.assert_allclose(got[axis].v, real[axis], rtol=0, atol=1e-12)
    for i in range(len(variables)):
        # 系数对像面的影响很小，步长 1e-6 时差分落在追迹舍入噪声里（par3 的 x 方向相对差 6.5e-4），系数用 1e-3
        h = 1e-3 if variables[i].field == "param" else 1e-6 * max(1.0, abs(theta[i]))
        up, down = theta.copy(), theta.copy()
        up[i] += h
        down[i] -= h
        plus, minus = trace(design.system_with(up), px, py).at(image), trace(design.system_with(down), px, py).at(image)
        for axis in ("x", "y"):
            fd = (plus[axis] - minus[axis]) / (2 * h)
            assert np.abs(fd).max() > 0, (variables[i], axis)
            np.testing.assert_allclose(got[axis].d[i], fd, rtol=0, atol=1e-4 * np.abs(fd).max())


def test_system_with_moves_the_vertices_after_a_changed_thickness():
    lens = zmx.parse(FIX / "lenses" / "piA-r012.zmx")
    system = OpticalSystem(lens, wavelength_um=lens.active_wavelengths[0][0], config=1)
    k = 3
    design = Design(system, [Variable(k, "thickness")])
    moved = design.system_with([system.items[k].thickness + 0.5])
    before = [it.vertex_z for it in system.items]
    after = [it.vertex_z for it in moved.items]
    assert after[: k + 1] == before[: k + 1]
    assert after[k + 1:] == pytest.approx([z + 0.5 for z in before[k + 1:]], abs=1e-12)
    assert moved.image_z == pytest.approx(system.image_z + 0.5, abs=1e-12)
