"""反射面、Float By Stop Size 与视场渐晕因子的解析恒等式和文件读取。

和 OpticStudio 逐条比对的用例在 tests/rtc/test_case_truth.py。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from case_variants import BY_NAME
from rtc import adtrace, firstorder, psf, zmx
from rtc.material.types import Mirror
from rtc.system import OpticalSystem
from rtc.trace import trace

CASES = Path(__file__).parent.parent / "fixtures" / "cases"


def single_mirror(radius: float = -800.0, epd: float = 40.0) -> OpticalSystem:
    """一个抛物面主镜，焦距 |R|/2，像面放在焦点上。"""
    lens = BY_NAME["reflective_one_mirror"].lens()
    lens.aperture_value = epd
    lens.fields, lens.num_fields = [(0.0, 0.0, 1.0)], 1
    lens.vignetting = [(0.0,) * 5]
    mirror = lens.surface(1)
    mirror.curvature, mirror.thickness, mirror.conic = 1.0 / radius, radius / 2.0, -1.0
    mirror.aperture_code = 0
    mirror.is_stop = True
    mirror.semi_diameter = abs(radius) / 20.0
    return OpticalSystem(lens, wavelength_um=0.5876)


# ---------------------------------------------------------------- 反射面


def test_concave_mirror_has_half_the_radius_as_focal_length():
    system = single_mirror()
    assert firstorder.effl(system) == pytest.approx(400.0, rel=1e-6)
    # 抛物面把轴上无穷远的平行光聚到焦点：各光瞳高度的像面交点都是 0
    block = trace(system, [0.0, 0.5, 1.0], [0.0, 0.0, 0.0])
    assert np.allclose(block.x[:, -1], 0.0, atol=1e-12)
    assert np.allclose(block.y[:, -1], 0.0, atol=1e-12)


def test_signed_index_and_direction_flip_at_each_mirror():
    system = single_mirror()
    mirror, image = system.items[0], system.items[1]
    assert mirror.reflect and mirror.n_after == -1.0 and mirror.sign == -1.0
    assert image.n_after == -1.0 and image.sign == -1.0
    block = trace(system, [0.6], [0.0])
    # 反射后光线实际向 −z 走；OpticStudio 报的方向余弦乘了 σ，所以 n 为正、与几何方向相反
    assert block.n[0, 0] > 0.0
    assert block.n[0, 1] > 0.0


def test_optical_path_through_a_mirror_stays_positive():
    system = single_mirror()
    block = trace(system, [1.0], [0.0])
    # 发射面到第 1 面 −0.25 mm（抛物面边缘在顶点面之前），第 1 面到像面 400.25 mm
    assert block.opl[0, -1] == pytest.approx(400.25, abs=1e-9)


def test_wavefront_of_a_mirror_is_refused_until_reconciled():
    with pytest.raises(NotImplementedError, match="反射"):
        psf.rms_wavefront(single_mirror(), n=8)


def test_ad_trace_follows_the_float_trace_through_a_mirror():
    lens = zmx.parse(CASES / "reflective.zmx")
    system = OpticalSystem(lens, wavelength_um=0.5876)
    image = system.items[-1].index
    k = len(system.items) - 2
    variable = adtrace.Variable(k, "thickness")
    last = system.items[k].index
    theta = [lens.thickness(last)]
    ad = adtrace.trace_ad(system, [0.0, 0.6], [0.0, 0.3], [variable], theta)
    assert float(np.ravel(ad.at(image)["y"][1].v)[0]) == pytest.approx(float(trace(system, [0.6], [0.3]).y[0, -1]), abs=1e-12)
    derivative = float(np.ravel(ad.at(image)["y"][1].d)[0])
    step = 1e-6

    def image_y(thickness: float) -> float:
        changed = zmx.parse(CASES / "reflective.zmx")
        changed.surface(last).thickness = thickness
        return float(trace(OpticalSystem(changed, wavelength_um=0.5876), [0.6], [0.3]).y[0, -1])

    finite = (image_y(theta[0] + step) - image_y(theta[0] - step)) / (2 * step)
    assert derivative == pytest.approx(finite, rel=1e-6)


def test_zmx_reads_mirrors_and_surface_apertures():
    lens = zmx.parse(CASES / "reflective.zmx")
    assert lens.surface(1).mirror and isinstance(lens.surface(1).material, Mirror)
    assert (lens.surface(1).aperture_code, lens.surface(1).ap_max) == (2, 15.0)
    assert lens.surface(3).mirror and lens.surface(2).glass == "N-BK7" and lens.surface(4).glass == ""
    assert lens.thickness(1) == pytest.approx(-300.0)
    iris = zmx.parse(CASES / "iris.zmx")
    assert iris.aperture_type == "FLOA"
    assert iris.surface(1).aperture_code == 1 and iris.surface(1).ap_max == pytest.approx(6.0)
    rect = iris.surface(7)
    assert (rect.aperture_code, rect.ap_min, rect.ap_max, rect.ap_dx) == (4, 2.6, 2.2, 0.5)
    assert all(not any(row) for row in iris.vignetting)
    handwritten = BY_NAME["iris_vignetting"].lens()
    assert handwritten.vignetting[1] == pytest.approx((0.04, -0.03, 0.12, 0.10, 3.0))


def test_obscuration_clips_the_central_rays_only():
    lens = zmx.parse(CASES / "reflective.zmx")
    system = OpticalSystem(lens, wavelength_um=0.5876)
    block = trace(system, [0.0, 0.0, 0.0], [0.0, 0.1, 0.9])
    # 主镜的中心遮挡半径 15 mm：光瞳 0.1 处（4 mm）被挡，0.9 处（36 mm）通过
    assert block.vig[1] == 1 and block.vig[2] == 0


# ---------------------------------------------------------------- Float By Stop Size


def test_float_by_stop_aims_at_the_stop_semi_diameter():
    lens = zmx.parse(CASES / "iris.zmx")
    system = OpticalSystem(lens, wavelength_um=0.5876)
    assert system.aim_target(1.0, 0.0) == (2.5, 0.0)


# ---------------------------------------------------------------- 视场渐晕因子


@pytest.mark.parametrize("px, py, ex, ey", [
    (1.0, 0.0, 0.8799254, 0.2138488),   # 偏心 + 压缩 + 旋转 20°
    (0.0, 1.0, 0.0169284, 0.5382505),
    (0.0, 0.0, 0.2221406, -0.0255652),  # 纯偏心
])
def test_vignetting_factors_map_the_pupil(px, py, ex, ey):
    """先偏心压缩后旋转：(0.2, −0.1, 0.3, 0.4, 20°) 下的期望值由公式手算，真值比对在 test_case_truth 的 iris_vignetting。"""
    lens = zmx.parse(CASES / "iris.zmx")
    lens.vignetting = [(0.2, -0.1, 0.3, 0.4, 20.0)] * len(lens.fields)
    lens.fields = [(0.0, 0.0, 1.0)]
    lens.num_fields = 1
    system = OpticalSystem(lens, wavelength_um=0.5876)
    qx, qy = system.vignetted_pupil(np.array([px]), np.array([py]), 0.0, 0.0)
    assert float(qx[0]) == pytest.approx(ex, abs=1e-6)
    assert float(qy[0]) == pytest.approx(ey, abs=1e-6)


def test_vignetting_factors_come_from_the_field_row():
    system = OpticalSystem(BY_NAME["iris_vignetting"].lens(), wavelength_um=0.5876)
    assert system.vignetting_factors(0.0, 0.0)[0] == pytest.approx((0.06, 0.0, 0.10, 0.08, 0.0))


@pytest.mark.parametrize("name", ["iris", "iris_vignetting", "sequential_opd", "reflective"])
@pytest.mark.parametrize("wave", [1, 2, 3])
def test_ad_values_and_derivatives_follow_changed_float_system(name, wave):
    """覆盖非主波长、表外视场及离开入口参数后的求交值，差分独立走浮点追迹。"""
    lens = BY_NAME[name].lens()
    system = OpticalSystem(lens, wavelength_um=lens.active_wavelengths[wave - 1][0])
    variables = [adtrace.Variable(0, "curvature"), adtrace.Variable(len(system.items) - 2, "thickness")]
    if lens.aperture_type == "FLOA":
        variables.append(adtrace.Variable(system.stop, "semi_diameter"))
    values = np.array([v.value(system) for v in variables])
    values[0] *= 1.001
    px, py = np.array([0.3, -0.45]), np.array([0.2, 0.3])
    changed = adtrace.system_with_variables(system, variables, values)
    got = adtrace.trace_ad(system, px, py, variables, values, 0.0, 0.7).at(system.items[-1].index)
    float_result = trace(changed, px, py, 0.0, 0.7)
    assert np.all(float_result.error == 0)
    for key in ("x", "y", "z", "l", "m", "n", "opl"):
        assert got[key].v == pytest.approx(getattr(float_result, key)[:, -1], abs=1e-9)
    for j, variable in enumerate(variables):
        step = 1e-7 if variable.field == "curvature" else 1e-5
        a, b = values.copy(), values.copy()
        a[j] += step
        b[j] -= step
        plus = trace(adtrace.system_with_variables(system, variables, a), px, py, 0.0, 0.7)
        minus = trace(adtrace.system_with_variables(system, variables, b), px, py, 0.0, 0.7)
        for key in ("x", "y", "l", "m", "n"):
            expected = (getattr(plus, key)[:, -1] - getattr(minus, key)[:, -1]) / (2 * step)
            assert got[key].d[j] == pytest.approx(expected, rel=3e-5, abs=2e-7), (name, wave, variable, key)


def test_invalidating_nonprimary_system_rebuilds_primary_from_current_items():
    from dataclasses import replace

    lens = BY_NAME["iris"].lens()
    system = OpticalSystem(lens, wavelength_um=lens.active_wavelengths[0][0])
    old = system.pupil_reference()
    k = system.stop
    system.items[k] = replace(system.items[k], semi=2.0)
    system.invalidate()
    new = system.pupil_reference()
    assert new[0] == old[0]
    assert new[1] == pytest.approx(old[1] * 2.0 / 2.5)
    assert system.primary.items[k].semi == 2.0
    assert lens.surface(system.items[k].index).semi_diameter == 2.5


def test_parabolic_mirror_experimental_wavefront_has_constant_phase():
    system = single_mirror()
    opd, ok = psf.opd_rays(system, [0.0, 0.4, 0.8], [0.0, 0.0, 0.0], experimental=True)
    assert ok.all()
    assert opd == pytest.approx(np.zeros(3), abs=1e-10)


def test_experimental_wavefront_refuses_an_obscured_chief():
    system = single_mirror()
    system.items[0].surface.aperture_code = 2
    system.items[0].surface.ap_max = 5.0
    with pytest.raises(ValueError, match="主光线"):
        psf.opd_rays(system, [0.0, 0.4, 0.8], [0.0, 0.0, 0.0], experimental=True)
