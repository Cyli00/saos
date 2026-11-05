"""已放行的路径与 OpticStudio 真值逐条比对；真值由 tests/test_zemax_cases.py 录入。

比对的是位置、方向、错误码和渐晕码，内核和 RTC_KERNEL=numpy 两条路径都跑。另有覆盖自检：
断言真值本身有判别力，案例改坏了（比如视场全被挡、孔径碰不到光线）会直接报错。
"""

from __future__ import annotations

from collections import Counter

import numpy as np
import pytest

from case_variants import ADMITTED, BY_NAME, CASES, truth as _truth
from rtc import firstorder
from rtc.operands import Operands
from rtc.system import OpticalSystem
from rtc.trace import trace
from saos.backend.rtc import RtcBackend

# 没有瞄准的路径，内核和 OpticStudio 在 1e-13 量级上一致；留到 1e-9，瞄准放行时再按第 3.1 节放宽
POSITION_TOL = 1e-9
DIRECTION_TOL = 1e-9
RAY_VARIANTS = [name for name in ADMITTED if BY_NAME[name].waves != "none"]


def _lens(name: str):
    return BY_NAME[name].lens()


def _systems(lens) -> dict[int, OpticalSystem]:
    return {w: OpticalSystem(lens, wavelength_um=um) for w, (um, _) in enumerate(lens.active_wavelengths, 1)}


def _trace_all(systems, rows: np.ndarray, surface: int):
    """按波长分组追迹，结果放回 rows 的顺序：错误码、渐晕码、x, y, z, l, m, n。"""
    out = np.full((len(rows), 8), np.nan)
    for wave in np.unique(rows[:, 0]).astype(int):
        chosen = rows[:, 0] == wave
        block = trace(systems[wave], rows[chosen, 3], rows[chosen, 4], rows[chosen, 1], rows[chosen, 2])
        k = block.surface_index.index(surface)
        out[chosen] = np.c_[block.error, block.vig, block.x[:, k], block.y[:, k], block.z[:, k], block.l[:, k], block.m[:, k], block.n[:, k]]
    return out


def _compare(systems, truth_rows, surface: int, *, position_tol=POSITION_TOL, direction_tol=DIRECTION_TOL) -> None:
    rows = np.asarray(truth_rows, dtype=float)
    got = _trace_all(systems, rows, surface)
    want = rows[:, 5:]
    # OpticStudio 追到 surface 为止，只报到这一面为止的错误和挡光；rtc 的是整条光线的，这里截到同一面
    error = np.where(np.abs(got[:, 0]) > surface, 0.0, got[:, 0])
    vignette = np.where(got[:, 1] > surface, 0.0, got[:, 1])
    assert error == pytest.approx(want[:, 0]), "错误码"
    assert vignette == pytest.approx(want[:, 1]), "渐晕码"
    alive = want[:, 0] == 0
    assert np.abs(got[alive, 2:5] - want[alive, 2:5]).max(initial=0.0) < position_tol
    assert np.abs(got[alive, 5:8] - want[alive, 5:8]).max(initial=0.0) < direction_tol


@pytest.fixture(params=["kernel", "numpy"])
def path(request, monkeypatch):
    if request.param == "numpy":
        monkeypatch.setenv("RTC_KERNEL", "numpy")
    else:
        monkeypatch.delenv("RTC_KERNEL", raising=False)
    return request.param


@pytest.mark.parametrize("name", RAY_VARIANTS)
def test_rays_at_the_image_match_opticstudio(name, path):
    truth = _truth(name)
    _compare(_systems(_lens(name)), truth["rays"], truth["image_surface"])


@pytest.mark.parametrize("name", RAY_VARIANTS)
def test_rays_surface_by_surface_match_opticstudio(name, path):
    truth = _truth(name)
    systems = _systems(_lens(name))
    for surface, rows in truth["surfaces"].items():
        _compare(systems, rows, int(surface))


@pytest.mark.parametrize("name", RAY_VARIANTS)
def test_optical_path_matches_plen(name):
    truth = _truth(name)
    lens = _lens(name)
    system = OpticalSystem(lens, wavelength_um=lens.active_wavelengths[lens.primary_wave - 1][0])
    rows = np.asarray(truth["plen"], dtype=float)
    block = trace(system, rows[:, 2], rows[:, 3], rows[:, 0], rows[:, 1])
    # OpticStudio 的 PLEN 对丢失的光线给 0，不比
    alive = block.error == 0
    assert block.opl[alive, -1] == pytest.approx(rows[alive, 4], abs=POSITION_TOL)


@pytest.mark.parametrize("name", ADMITTED)
def test_first_order_quantities_match_opticstudio(name):
    want = _truth(name)["operands"]
    lens = _lens(name)
    system = OpticalSystem(lens, wavelength_um=lens.active_wavelengths[lens.primary_wave - 1][0])
    operands = Operands(system)
    assert firstorder.entrance_pupil_diameter(system) == pytest.approx(want["EPDI"], rel=1e-9)
    assert firstorder.effl(system) == pytest.approx(want["EFFL"], rel=1e-9)
    assert firstorder.wfno(system) == pytest.approx(want["WFNO"], rel=1e-8)
    assert operands.expp() == pytest.approx(want["EXPP"], rel=1e-6)
    assert operands.expd() == pytest.approx(want["EXPD"], rel=1e-6)


def test_expd_uses_the_real_stop_height_only_with_real_aiming():
    """光阑在次镜上时近轴和实际边缘光线高度差 0.17%，瞄准 off 和 real 的 EXPD 正好差这么多。"""
    off, real = _truth("reflective_stop_on_secondary"), _truth("reflective_stop_on_secondary_real")
    assert off["operands"]["EXPD"] != pytest.approx(real["operands"]["EXPD"], rel=1e-3)
    lens = _lens("reflective_stop_on_secondary_real")
    system = OpticalSystem(lens, wavelength_um=lens.active_wavelengths[lens.primary_wave - 1][0])
    assert Operands(system).expd() == pytest.approx(real["operands"]["EXPD"], rel=1e-6)


@pytest.mark.parametrize("case", ["iris", "reflective"])
def test_indx_of_every_surface_matches_opticstudio(case):
    """含反射面：OpticStudio 报入射侧介质的折射率，符号恒正。"""
    backend = RtcBackend(CASES / f"{case}.zmx")
    for surface, values in _truth(case)["index"].items():
        for wave, want in enumerate(values, 1):
            assert backend.operand("INDX", int(surface), wave, []) == pytest.approx(want, abs=1e-8), (surface, wave)
    assert not backend.unverified_paths


def test_medium_indices_of_a_mirror_read_the_incident_side():
    backend = RtcBackend(CASES / "reflective.zmx")
    um = backend.lens.active_wavelengths[0][0]
    assert backend.medium_indices(1, [um]) == pytest.approx([1.0])
    assert backend.medium_indices(3, [um]) == pytest.approx(backend.medium_indices(2, [um]))


# ---------------------------------------------------------------- 覆盖自检


def _first_clips(truth: dict) -> Counter:
    return Counter(int(r[6]) for r in truth["rays"])


def _usable_by_field(truth: dict) -> dict[tuple[float, float], float]:
    out = {}
    for hx, hy in truth["fields"]:
        rows = [r for r in truth["rays"] if r[1] == hx and r[2] == hy]
        if rows:
            out[(hx, hy)] = sum(1 for r in rows if r[5] == 0 and r[6] == 0) / len(rows)
    return out


def test_iris_rays_exercise_every_aperture():
    clips = _first_clips(_truth("iris"))
    assert clips[1] > 0, "面 1 的圆孔没有首挡光线"
    assert clips[7] > 0, "矩形孔没有首挡光线"


def test_stop_semi_diameter_clips_only_with_a_floating_aperture():
    """(0.6, 0.8) 视场的 (Px, Py) = (1, 0) 越过光阑半口径 2.5：半口径本身不挡光，加上浮动孔径才挡。"""
    ray = (0.6, 0.8, 1.0, 0.0)
    lens = _lens("iris")
    system = OpticalSystem(lens, wavelength_um=lens.active_wavelengths[lens.primary_wave - 1][0])
    block = trace(system, [ray[2]], [ray[3]], [ray[0]], [ray[1]])
    k = block.surface_index.index(6)
    assert np.hypot(block.x[0, k], block.y[0, k]) > 2.5

    def first_clip(name):
        record = _truth(name)
        rows = [r for r in record["rays"] if r[0] == record["primary_wave"] and r[1:5] == pytest.approx(list(ray))]
        assert len(rows) == 1
        return rows[0][6]

    assert first_clip("iris") == 0
    assert first_clip("iris_floating") == 6


def test_iris_has_an_off_axis_x_field():
    assert any(hx != 0.0 for hx, _ in _truth("iris")["fields"])


def test_reflective_rays_hit_the_obscuration():
    assert _first_clips(_truth("reflective"))[1] > 0


def test_reflective_leaves_the_glass_and_passes_the_primary():
    lens = _lens("reflective")
    system = OpticalSystem(lens, wavelength_um=lens.active_wavelengths[lens.primary_wave - 1][0])
    assert system.items[-1].n_after == pytest.approx(1.0)
    assert system.items[-1].vertex_z > 0.0


def test_one_mirror_variant_has_an_odd_number_of_reflections():
    lens = _lens("reflective_one_mirror")
    system = OpticalSystem(lens, wavelength_um=lens.active_wavelengths[lens.primary_wave - 1][0])
    assert system.items[-1].sign == -1.0


@pytest.mark.parametrize("name", ["iris", "reflective"])
def test_most_rays_of_every_field_get_through(name):
    usable = _usable_by_field(_truth(name))
    assert usable and min(usable.values()) >= 0.5, usable


@pytest.mark.parametrize("name, against", [("iris_no_apertures", "iris"), ("iris_semi_2", "iris"), ("iris_vignetting", "iris"),
                                           ("iris_set_vignetting", "iris"), ("reflective_no_obscuration", "reflective")])
def test_variant_differs_from_its_base(name, against):
    """变体要真的改变结果，不然它验证不了那项能力。"""
    rows = {tuple(r[:5]): r[5:] for r in _truth(name)["rays"]}
    base = {tuple(r[:5]): r[5:] for r in _truth(against)["rays"]}
    shared = rows.keys() & base.keys()
    assert shared
    moved = sum(1 for key in shared if rows[key][1] != base[key][1] or abs(rows[key][2] - base[key][2]) > 1e-5 or abs(rows[key][3] - base[key][3]) > 1e-5)
    assert moved > 0


def test_semi_diameter_variants_recorded_their_settings():
    assert _truth("iris_fast_on")["semi_settings"]["fast"] is True
    assert _truth("iris_fast_off")["semi_settings"]["fast"] is False
    assert _truth("iris_margin_percent")["semi_settings"]["margin_percent"] == pytest.approx(10.0)
    assert _truth("iris_margin_mm")["semi_settings"]["margin_mm"] == pytest.approx(0.5)
