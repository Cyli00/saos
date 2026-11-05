"""R1–R8 候选实现的验收；调用底层入口，不改变后端准入名单。"""

from __future__ import annotations

import numpy as np
import pytest

from case_variants import ALL, BY_NAME, truth
from rtc import firstorder, psf, zmx
from rtc.operands import Operands
from rtc.system import OpticalSystem
from rtc.trace import trace
from saos.backend import rtc_lens

from test_case_truth import _compare, _systems, path

CANDIDATES = [v.name for v in ALL if v.serves in ("R1", "R3", "R7")]


@pytest.mark.parametrize("name", CANDIDATES)
def test_candidate_rays_match_recorded_opticstudio(name, path):
    record = truth(name)
    lens = BY_NAME[name].lens()
    systems = _systems(lens)
    tolerances = {"position_tol": 1e-6, "direction_tol": 1e-7} if lens.ray_aiming == 2 else {}
    _compare(systems, record["rays"], record["image_surface"], **tolerances)
    for surface, rows in record["surfaces"].items():
        _compare(systems, rows, int(surface), **tolerances)


@pytest.mark.parametrize("name", [v.name for v in ALL if v.serves in ("R1", "R7")])
def test_candidate_pupils_and_first_order_match_recorded_opticstudio(name):
    lens = BY_NAME[name].lens()
    system = _systems(lens)[lens.primary_wave]
    want = truth(name)["operands"]
    operands = Operands(system)
    assert firstorder.entrance_pupil_diameter(system) == pytest.approx(want["EPDI"], rel=1e-9)
    assert firstorder.effl(system) == pytest.approx(want["EFFL"], rel=1e-9)
    assert firstorder.wfno(system) == pytest.approx(want["WFNO"], rel=1e-8)
    assert operands.expp() == pytest.approx(want["EXPP"], rel=1e-6)
    assert operands.expd() == pytest.approx(want["EXPD"], rel=1e-6)


@pytest.mark.parametrize("name", ["iris_vignetting", "iris_set_vignetting"])
def test_off_table_fields_match_opticstudio(name, path):
    record = truth(name)
    _compare(_systems(BY_NAME[name].lens()), record["off_table"], record["image_surface"])


@pytest.mark.parametrize("variant", [v for v in ALL if v.serves == "R3"], ids=lambda v: v.name)
def test_aperture_roundtrip_uses_recorded_native_format(variant):
    original = variant.lens()
    loaded = zmx.parse_text(rtc_lens.dump(original))
    assert [(s.aperture_code, s.ap_min, s.ap_max, s.ap_dx, s.ap_dy) for s in loaded.surfaces] == [
        (s.aperture_code, s.ap_min, s.ap_max, s.ap_dx, s.ap_dy) for s in original.surfaces]
    _compare(_systems(loaded), truth(variant.name)["rays"], max(s.index for s in loaded.surfaces))


@pytest.mark.parametrize("name", [v.name for v in ALL if "R5" in v.serves])
def test_adaptive_semi_diameters_match_recorded_lde(name):
    lens = BY_NAME[name].lens()
    systems = _systems(lens)
    got = rtc_lens.auto_semi_diameters(
        systems[lens.primary_wave], lens, lens.current_config, len(systems),
        lambda wave, hx, hy, px, py: trace(systems[wave], px, py, hx, hy), experimental=True)
    for surface, (expected, solve) in truth(name)["semi_diameters"].items():
        if solve == "Automatic":
            assert got[int(surface)] == pytest.approx(expected, abs=1e-6, rel=0), (name, surface)


@pytest.mark.parametrize("name", ["sequential_opd", "reflective", "reflective_one_mirror", "reflective_no_obscuration"])
def test_experimental_opdc_matches_opticstudio(name, path):
    lens = BY_NAME[name].lens()
    system = OpticalSystem(lens)
    for field in truth(name)["opd"]:
        rows = np.asarray(field["rows"], dtype=float)
        chief = trace(system, [0.0], [0.0], field["hx"], field["hy"])
        if chief.error[0] or chief.vig[0]:
            with pytest.raises(ValueError, match="主光线"):
                psf.opd_rays(system, rows[:, 0], rows[:, 1], field["hx"], field["hy"], experimental=True)
            continue
        opd, ok = psf.opd_rays(system, rows[:, 0], rows[:, 1], field["hx"], field["hy"], experimental=True)
        assert ok.any(), (name, field["hx"], field["hy"])
        assert opd[ok] / (system.wavelength * 1e-3) == pytest.approx(rows[ok, 2], abs=1e-5, rel=0)


def test_experimental_adaptive_sampling_is_bounded():
    lens = BY_NAME["iris_fast_off"].lens()
    systems = _systems(lens)
    counts = []

    def measured(wave, hx, hy, px, py):
        counts.append(len(px))
        return trace(systems[wave], px, py, hx, hy)

    rtc_lens.auto_semi_diameters(systems[lens.primary_wave], lens, lens.current_config, len(systems), measured, experimental=True)
    assert sum(counts) <= len(lens.fields) * len(systems) * 256 * 20


def test_spider_has_four_opposite_arms_in_both_paths(path):
    lens = BY_NAME["iris_spider"].lens()
    system = OpticalSystem(lens)
    item = next(it for it in system.items if it.surface.aperture_code == 3)
    from rtc.trace import aperture_clipped

    x = np.array([1.0, -1.0, 0.0, 0.0, 1.0, -1.0])
    y = np.array([0.0, 0.0, 1.0, -1.0, 1.0, -1.0])
    if path == "numpy":
        clipped = aperture_clipped(item, x, y).tolist()
    else:
        from rtc.kernel import _clipped

        s = item.surface
        ap = np.array([s.aperture_code, s.ap_min, s.ap_max, s.ap_dx, s.ap_dy, item.semi, 1.0 - 1e-8])
        clipped = [bool(_clipped(ap, a, b)) for a, b in zip(x, y)]
    assert clipped == [True, True, True, True, False, False]


@pytest.mark.parametrize("aiming", [False, True])
@pytest.mark.parametrize("code", [1, 2, 10])
def test_circular_edges_follow_gold_standard_squared_radius_tolerance(code, aiming, path):
    from rtc.trace import aperture_clipped

    lens = BY_NAME["iris"].lens()
    lens.ray_aiming = 2 if aiming else 0
    stop = lens.surface(lens.stop_index)
    stop.aperture_code, stop.ap_min, stop.ap_max = code, 1.0, 2.0
    stop.semi_diameter, stop.ap_dx, stop.ap_dy = 2.0, 0.0, 0.0
    system = OpticalSystem(lens)
    item = system.items[system.stop]
    tolerance = 1e-7 if aiming else 1e-8
    x = np.array([1.0, 1.0 + tolerance, 2.0, 2.0 * (1 + tolerance / 4), 2.0 * (1 + tolerance)])
    expected = {1: [True, False, False, False, True],
                2: [False, True, True, True, False],
                10: [False, False, False, False, True]}[code]
    if path == "numpy":
        got = aperture_clipped(item, x, np.zeros_like(x), aiming=aiming).tolist()
    else:
        from rtc.kernel import _clipped, pack

        ap = pack(system).AP[system.stop]
        got = [bool(_clipped(ap, value, 0.0)) for value in x]
    assert got == expected


def test_elliptical_obscuration_includes_its_boundary(path):
    from rtc.trace import aperture_clipped

    lens = BY_NAME["iris"].lens()
    stop = lens.surface(lens.stop_index)
    stop.aperture_code, stop.ap_min, stop.ap_max = 7, 2.0, 1.0
    system = OpticalSystem(lens)
    if path == "numpy":
        assert aperture_clipped(system.items[system.stop], [2.0], [0.0])[0]
    else:
        from rtc.kernel import _clipped, pack

        assert _clipped(pack(system).AP[system.stop], 2.0, 0.0)
