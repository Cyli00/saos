"""rtc.firstorder 与有限物距发射：解析恒等式（只能抓实现错误，不替代 oracle 回归）。

模型玻璃不依赖本机 AGF。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from rtc import firstorder, zmx
from rtc.material.types import Model
from rtc.system import OpticalSystem
from rtc.trace import trace

N_D, V_D = 1.5168, 64.17


def _lens(r1=50.0, r2=-50.0, t=5.0, back=45.0, object_thickness=math.inf, aperture=("EPD", 10.0), kind="STANDARD",
          params=None, field_type="ANGLE"):
    lens = zmx.Lens(units="MM")
    lens.raw["MODE"] = "MODE SEQ"
    lens.aperture_type, lens.aperture_value = aperture
    lens.field_type = field_type
    lens.fields = [(0.0, 0.0, 1.0), (0.0, 1.0, 1.0)]
    lens.num_fields = 2
    lens.wavelengths = [(0.5876, 1.0)]
    lens.num_waves = 1
    front = zmx.Surface(index=1, type=kind, curvature=1 / r1, thickness=t, is_stop=True)
    front.material = Model(N_D, V_D, 0.0)
    front.params.update(params or {})
    lens.surfaces = [zmx.Surface(index=0, thickness=object_thickness), front,
                     zmx.Surface(index=2, curvature=1 / r2, thickness=back), zmx.Surface(index=3)]
    return lens


def test_effl_is_the_thick_lens_focal_length():
    system = OpticalSystem(_lens())
    n = system.items[0].n_after
    c1, c2, t = 1 / 50.0, -1 / 50.0, 5.0
    power = (n - 1) * (c1 - c2 + (n - 1) * t * c1 * c2 / n)
    assert firstorder.effl(system) == pytest.approx(1 / power, rel=1e-8)
    assert firstorder.isfn(system) == pytest.approx(firstorder.effl(system) / 10.0, rel=1e-14)


def test_even_asphere_r2_term_adds_twice_its_value_to_the_paraxial_curvature():
    alpha = 2e-3
    asphere = OpticalSystem(_lens(kind="EVENASPH", params={1: alpha}))
    standard = OpticalSystem(_lens(r1=1 / (1 / 50.0 + 2 * alpha)))
    assert firstorder.effl(asphere) == pytest.approx(firstorder.effl(standard), rel=1e-7)
    assert asphere.curvature_axis(asphere.items[0], "y") == pytest.approx(1 / 50.0 + 2 * alpha, rel=1e-15)


def test_totr_and_tthi_sum_the_vertex_spacings():
    lens = _lens(t=5.0, back=45.0)
    system = OpticalSystem(lens)
    assert firstorder.totr(system) == pytest.approx(50.0, abs=1e-14)
    assert firstorder.tthi(lens, 1, 2) == pytest.approx(50.0, abs=1e-14)


def test_wfno_on_axis_is_one_over_twice_the_image_space_na():
    system = OpticalSystem(_lens())
    block = trace(system, [0.0], [1.0])
    sin = math.hypot(block.l[0, -2], block.m[0, -2])
    assert firstorder.wfno(system) == pytest.approx(1 / (2 * sin), rel=1e-12)


def test_finite_object_launch_needs_the_unverified_switch():
    lens = _lens(object_thickness=100.0, aperture=("EPD", 10.0))
    with pytest.raises(NotImplementedError):
        trace(OpticalSystem(lens), [0.0], [1.0])
    system = OpticalSystem(lens, allow_unverified=True)
    block = trace(system, [0.0], [1.0])
    # 瞄准关：轴上边缘光线从物点 (0, 0, −100) 射向近轴入瞳边缘 (0, 5, 0)（光阑在第 1 面，入瞳半径 = EPD/2），
    # 所以它与第 1 面的交点在这条直线上
    y1, z1 = float(block.y[0, 0]), float(block.z[0, 0])
    assert y1 / (100.0 + z1) == pytest.approx(5.0 / 100.0, rel=1e-12)


def test_object_height_field_starts_at_that_height():
    lens = _lens(object_thickness=100.0, aperture=("OBNA", 0.05), field_type="OP1")
    system = OpticalSystem(lens, allow_unverified=True)
    from rtc.trace import _launch

    x0, y0, z0, _ = _launch(system, np.array([0.0]), np.array([0.0]), np.array([0.0]), np.array([1.0]))
    assert (float(x0[0]), float(y0[0]), float(z0[0])) == (0.0, 1.0, -100.0)
