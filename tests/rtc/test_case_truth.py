"""rtc 与 OpticStudio 真值逐条比对；真值由 tests/test_zemax_cases.py 录入，这里只回放，不启动 OpticStudio。

比对的是位置、方向、错误码、渐晕码、光程、一阶量、自动半口径和波前。
通过不代表生产放行：准入由 saos.backend.rtc 的 _file_problems 决定。
覆盖自检断言真值本身有判别力，案例改坏了（比如视场全被挡、孔径碰不到光线）会直接报错。
"""

from __future__ import annotations

from collections import Counter

import numpy as np
import pytest

from case_variants import ALL, BY_NAME, CASES, truth as _truth
from rtc import firstorder, psf, zmx
from rtc.operands import Operands
from rtc.system import OpticalSystem
from rtc.trace import aperture_clipped, trace
from saos.backend import rtc_lens
from saos.backend.rtc import RtcBackend

# 没有瞄准的路径，内核和 OpticStudio 在 1e-13 量级上一致；实际瞄准收敛误差大，单独放宽
POSITION_TOL = 1e-9
DIRECTION_TOL = 1e-9
AIMED_POSITION_TOL = 1e-6
AIMED_DIRECTION_TOL = 1e-7
PARAXIAL_PATH_TOL = 2e-6
ALL_NAMES = [v.name for v in ALL]
RAY_VARIANTS = [v.name for v in ALL if v.waves != "none"]


def _lens(name: str):
    return BY_NAME[name].lens()


def _systems(lens) -> dict[int, OpticalSystem]:
    return {w: OpticalSystem(lens, wavelength_um=um) for w, (um, _) in enumerate(lens.active_wavelengths, 1)}


def _primary(lens) -> OpticalSystem:
    return OpticalSystem(lens, wavelength_um=lens.active_wavelengths[lens.primary_wave - 1][0])


def _tolerances(lens) -> dict[str, float]:
    if lens.ray_aiming == 2:
        return {"position_tol": AIMED_POSITION_TOL, "direction_tol": AIMED_DIRECTION_TOL}
    return {"position_tol": POSITION_TOL, "direction_tol": DIRECTION_TOL}


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


# ---------------------------------------------------------------- 光线、光程、一阶量


@pytest.mark.parametrize("name", RAY_VARIANTS)
def test_rays_match_opticstudio_at_the_image_and_surface_by_surface(name):
    record = _truth(name)
    lens = _lens(name)
    systems = _systems(lens)
    tolerances = _tolerances(lens)
    _compare(systems, record["rays"], record["image_surface"], **tolerances)
    for surface, rows in record["surfaces"].items():
        _compare(systems, rows, int(surface), **tolerances)


@pytest.mark.parametrize("name", RAY_VARIANTS)
def test_optical_path_matches_plen(name):
    lens = _lens(name)
    system = _primary(lens)
    rows = np.asarray(_truth(name)["plen"], dtype=float)
    block = trace(system, rows[:, 2], rows[:, 3], rows[:, 0], rows[:, 1])
    # OpticStudio 的 PLEN 对丢失的光线给 0，不比
    alive = block.error == 0
    # PLEN 含理想透镜相位；入射光束有像差时它的积分路径未公开（见 trace._paraxial_lens_phase），实测残差 ≤ 1.3e-6 mm
    has_lens = any(it.type == "PARAXIAL" for it in system.items)
    tolerance = PARAXIAL_PATH_TOL if has_lens else _tolerances(lens)["position_tol"]
    assert (block.opl + block.phase)[alive, -1] == pytest.approx(rows[alive, 4], abs=tolerance)


@pytest.mark.parametrize("name", ALL_NAMES)
def test_first_order_quantities_match_opticstudio(name):
    want = _truth(name)["operands"]
    system = _primary(_lens(name))
    operands = Operands(system)
    assert firstorder.entrance_pupil_diameter(system) == pytest.approx(want["EPDI"], rel=1e-9)
    assert firstorder.effl(system) == pytest.approx(want["EFFL"], rel=1e-9)
    assert firstorder.wfno(system) == pytest.approx(want["WFNO"], rel=1e-8)
    assert firstorder.entrance_pupil_position(system) == pytest.approx(want["ENPP"], rel=1e-9, abs=1e-9)
    assert firstorder.totr(system) == pytest.approx(want["TOTR"], rel=1e-12, abs=1e-12)
    assert firstorder.petzval_radius(system) == pytest.approx(want["PETZ"], rel=1e-9)
    assert operands.expp() == pytest.approx(want["EXPP"], rel=1e-6)
    assert operands.expd() == pytest.approx(want["EXPD"], rel=1e-6)


def test_real_aiming_changes_the_recorded_expd():
    """光阑在次镜上时近轴和实际边缘光线高度差 0.17%：真值要能分开瞄准 off 和 real，一阶比对才有判别力。"""
    off, real = _truth("reflective_stop_on_secondary"), _truth("reflective_stop_on_secondary_real")
    assert off["operands"]["EXPD"] != pytest.approx(real["operands"]["EXPD"], rel=1e-3)


@pytest.mark.parametrize("name", ["iris_vignetting", "iris_set_vignetting"])
def test_off_table_fields_match_opticstudio(name):
    record = _truth(name)
    _compare(_systems(_lens(name)), record["off_table"], record["image_surface"])


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


# ---------------------------------------------------------------- 孔径


@pytest.mark.parametrize("variant", [v for v in ALL if v.serves == "R3"], ids=lambda v: v.name)
def test_aperture_roundtrip_uses_recorded_native_format(variant):
    original = variant.lens()
    loaded = zmx.parse_text(rtc_lens.dump(original))
    assert [(s.aperture_code, s.ap_min, s.ap_max, s.ap_dx, s.ap_dy) for s in loaded.surfaces] == [
        (s.aperture_code, s.ap_min, s.ap_max, s.ap_dx, s.ap_dy) for s in original.surfaces]
    _compare(_systems(loaded), _truth(variant.name)["rays"], max(s.index for s in loaded.surfaces))


def test_spider_has_four_opposite_arms():
    system = OpticalSystem(_lens("iris_spider_decentred"))
    item = next(it for it in system.items if it.surface.aperture_code == 3)
    x = np.array([1.0, -1.0, 0.0, 0.0, 1.0, -1.0]) + item.surface.ap_dx
    y = np.array([0.0, 0.0, 1.0, -1.0, 1.0, -1.0]) + item.surface.ap_dy
    assert aperture_clipped(item, x, y).tolist() == [True, True, True, True, False, False]


@pytest.mark.parametrize("aiming", [False, True])
@pytest.mark.parametrize("code", [1, 2, 10])
def test_circular_edges_follow_gold_standard_squared_radius_tolerance(code, aiming):
    lens = _lens("iris")
    lens.ray_aiming = 2 if aiming else 0
    stop = lens.surface(lens.stop_index)
    stop.aperture_code, stop.ap_min, stop.ap_max = code, 1.0, 2.0
    stop.semi_diameter, stop.ap_dx, stop.ap_dy = 2.0, 0.0, 0.0
    system = OpticalSystem(lens)
    tolerance = 1e-7 if aiming else 1e-8
    x = np.array([1.0, 1.0 + tolerance, 2.0, 2.0 * (1 + tolerance / 4), 2.0 * (1 + tolerance)])
    expected = {1: [True, False, False, False, True],
                2: [False, True, True, True, False],
                10: [False, False, False, False, True]}[code]
    assert aperture_clipped(system.items[system.stop], x, np.zeros_like(x), aiming=aiming).tolist() == expected


def test_elliptical_obscuration_includes_its_boundary():
    lens = _lens("iris")
    stop = lens.surface(lens.stop_index)
    stop.aperture_code, stop.ap_min, stop.ap_max = 7, 2.0, 1.0
    system = OpticalSystem(lens)
    assert aperture_clipped(system.items[system.stop], [2.0], [0.0])[0]


# ---------------------------------------------------------------- 自动半口径


@pytest.mark.parametrize("name", [v.name for v in ALL if "R5" in v.serves])
def test_adaptive_semi_diameters_match_recorded_lde(name):
    lens = _lens(name)
    systems = _systems(lens)
    got = rtc_lens.auto_semi_diameters(
        systems[lens.primary_wave], lens, lens.current_config, len(systems),
        lambda wave, hx, hy, px, py: trace(systems[wave], px, py, hx, hy), experimental=True)
    for surface, (expected, solve) in _truth(name)["semi_diameters"].items():
        if solve == "Automatic":
            assert got[int(surface)] == pytest.approx(expected, abs=1e-6, rel=0), (name, surface)


def test_experimental_adaptive_sampling_is_bounded():
    lens = _lens("iris_fast_off")
    systems = _systems(lens)
    counts = []

    def measured(wave, hx, hy, px, py):
        counts.append(len(px))
        return trace(systems[wave], px, py, hx, hy)

    rtc_lens.auto_semi_diameters(systems[lens.primary_wave], lens, lens.current_config, len(systems), measured, experimental=True)
    assert sum(counts) <= len(lens.fields) * len(systems) * 256 * 20


# ---------------------------------------------------------------- 波前


@pytest.mark.parametrize("name", [v.name for v in ALL if v.opd])
def test_experimental_opdc_matches_opticstudio(name):
    system = OpticalSystem(_lens(name))
    for field in _truth(name)["opd"]:
        rows = np.asarray(field["rows"], dtype=float)
        chief = trace(system, [0.0], [0.0], field["hx"], field["hy"])
        if chief.error[0] or chief.vig[0]:
            with pytest.raises(ValueError, match="主光线"):
                psf.opd_rays(system, rows[:, 0], rows[:, 1], field["hx"], field["hy"], experimental=True)
            continue
        opd, ok = psf.opd_rays(system, rows[:, 0], rows[:, 1], field["hx"], field["hy"], experimental=True)
        assert ok.any(), (name, field["hx"], field["hy"])
        assert opd[ok] / (system.wavelength * 1e-3) == pytest.approx(rows[ok, 2], abs=1e-5, rel=0)


# ---------------------------------------------------------------- 覆盖自检


def _first_clips(record: dict) -> Counter:
    return Counter(int(r[6]) for r in record["rays"])


def _usable_by_field(record: dict) -> dict[tuple[float, float], float]:
    out = {}
    for hx, hy in record["fields"]:
        rows = [r for r in record["rays"] if r[1] == hx and r[2] == hy]
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
    block = trace(_primary(_lens("iris")), [ray[2]], [ray[3]], [ray[0]], [ray[1]])
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
    system = _primary(_lens("reflective"))
    assert system.items[-1].n_after == pytest.approx(1.0)
    assert system.items[-1].vertex_z > 0.0


def test_one_mirror_variant_has_an_odd_number_of_reflections():
    assert _primary(_lens("reflective_one_mirror")).items[-1].sign == -1.0


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
    assert _truth("iris")["semi_settings"]["fast"] is True
    assert _truth("iris_fast_off")["semi_settings"]["fast"] is False
    settings = _truth("iris_margin_both")["semi_settings"]
    assert (settings["margin_mm"], settings["margin_percent"]) == pytest.approx((0.5, 10.0))
