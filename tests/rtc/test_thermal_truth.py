"""多重结构解析与热跟随对 OpticStudio 真值（tests/fixtures/cases/thermal.json，变体见 tests/thermal_variants.py）。"""

from __future__ import annotations

import copy
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rtc import thermal, zmx  # noqa: E402
from rtc.system import OpticalSystem  # noqa: E402
from rtc.trace import trace  # noqa: E402
from saos import prescription  # noqa: E402
from saos.backend import rtc_lens, rtc_merit  # noqa: E402
from saos.backend.rtc import RtcBackend  # noqa: E402
from saos.protocol import SaosError  # noqa: E402
from thermal_variants import ALL, TCE, TRUTH_FILE, base, truth  # noqa: E402

pytestmark = pytest.mark.skipif(not TRUTH_FILE.exists(), reason="还没有录热结构真值")
# OpticStudio 存盘的单元格只有 13 位有效数字
CELL_REL = 1e-11
RAY_TOL = 1e-7
ACCEPTED = [v.name for v in ALL]


def lens_of(name: str) -> zmx.Lens:
    return zmx.parse_text(truth(name)["zmx"])


def instance(tmp_path: Path, text: str) -> RtcBackend:
    path = tmp_path / "lens.zmx"
    path.write_bytes(rtc_lens.BOM + text.encode("utf-16-le"))
    backend = RtcBackend(None, standalone=True)
    backend.load(path)
    return backend


@pytest.mark.parametrize("name", ACCEPTED)
def test_solve_code_is_configuration_count_plus_two(name):
    lens = lens_of(name)
    codes = {row.solve_code for row in lens.mce if row.op == "THIC" and row.config > 1}
    assert lens.thermal_code == lens.num_configs + 2
    assert lens.thermal_code in codes


@pytest.mark.parametrize("name", ACCEPTED)
def test_every_thermal_cell_matches_opticstudio(name):
    lens = lens_of(name)
    checked = 0
    alphas = {}
    for row in lens.mce:
        if row.solve_code != lens.thermal_code or row.op in ("GLSS", "CHZN"):
            continue
        table = alphas.setdefault(row.source_config, thermal._alphas(lens, row.source_config))
        value = thermal.thermal_value(lens, row, table)
        assert math.isclose(value, row.value, rel_tol=CELL_REL, abs_tol=1e-10), (row.op, row.surface, row.param, row.config, value, row.value)
        checked += 1
    assert checked > 0


@pytest.mark.parametrize("name", ACCEPTED)
def test_tracing_each_configuration_matches_opticstudio(tmp_path, name):
    data = truth(name)
    if "rays" not in data["per_config"]["1"]:
        pytest.skip("这个变体只录单元格")
    backend = instance(tmp_path, data["zmx"])
    image = data["image"]
    for config, item in data["per_config"].items():
        backend.select_configuration(int(config))
        rows = [tuple(r[:5]) for r in item["rays"]]
        mine = backend.trace_batch(rows, image)
        theirs = np.array([r[5:] for r in item["rays"]], dtype=float)
        ok = theirs[:, 0] == 0
        assert (np.asarray(mine.error)[ok] == 0).all()
        for k, key in enumerate(("x", "y", "z", "l", "m", "n"), start=2):
            delta = np.abs(np.asarray(getattr(mine, key), dtype=float)[ok] - theirs[ok, k])
            assert delta.max(initial=0.0) < RAY_TOL, (config, key, float(delta.max()))


@pytest.mark.parametrize("name", ACCEPTED)
def test_indices_each_configuration_match_opticstudio(tmp_path, name):
    data = truth(name)
    backend = instance(tmp_path, data["zmx"])
    for config, item in data["per_config"].items():
        backend.select_configuration(int(config))
        for surface, values in item["index"].items():
            for w, value in enumerate(values, 1):
                assert abs(backend.operand("INDX", int(surface), w, []) - value) <= 2e-8, (config, surface, w)


@pytest.mark.parametrize("name", ["seq_3", "iris_6", "seq_asphere", "reflective_3"])
def test_configuration_equals_single_configuration_lens(name):
    """结构 c 的结果和把同样的值写进单结构镜头完全一致：configured 是唯一入口。"""
    lens = lens_of(name)
    for config in range(1, lens.num_configs + 1):
        view = lens.configured(config)
        single = copy.deepcopy(view)
        assert single.mce == [] and single.num_configs == 1
        a, b = OpticalSystem(lens, config=config), OpticalSystem(single)
        px, py = np.array([0.0, 0.5, 0.0]), np.array([0.0, 0.5, 0.9])
        ra, rb = trace(a, px, py, 0.0, 0.0), trace(b, px, py, 0.0, 0.0)
        for key in ("x", "y", "z", "l", "m", "n"):
            np.testing.assert_array_equal(getattr(ra, key), getattr(rb, key))
        assert [it.thickness for it in a.items] == [it.thickness for it in b.items]
        assert a.wavelength == b.wavelength


@pytest.mark.parametrize("name", ACCEPTED)
def test_save_and_reload_keeps_every_cell(tmp_path, name):
    data = truth(name)
    backend = instance(tmp_path, data["zmx"])
    first = backend._configurations()
    path = tmp_path / "written.zmx"
    backend.save_as(path)
    again = RtcBackend(None, standalone=True)
    again.load(path)
    second = again._configurations()
    assert [(o["type"], o["params"], o["solves"]) for o in first["operands"]] == [(o["type"], o["params"], o["solves"]) for o in second["operands"]]
    for a, b in zip(first["operands"], second["operands"]):
        assert a["values"] == pytest.approx(b["values"], rel=1e-14) if a["type"] != "GLSS" else a["values"] == b["values"]


@pytest.mark.parametrize("name", ACCEPTED)
def test_describe_rows_are_the_ones_opticstudio_describes(tmp_path, name):
    """结构指纹比较的字段（类型、参数、求解）两边一致；交付前换后端不会误判换了模型。"""
    data = truth(name)
    mine = instance(tmp_path, data["zmx"])._configurations()
    theirs = data["configurations"]
    assert [(o["type"], o["params"], o["solves"]) for o in mine["operands"]] == [(o["type"], o["params"], o["solves"]) for o in theirs["operands"]]


@pytest.mark.parametrize("name", ["seq_3", "seq_asphere", "seq_model", "iris_6", "reflective_3", "seq_air_after_air", "seq_finite_object", "reflective_air_after_mirror", "seq_curved_image"])
def test_make_thermal_builds_what_opticstudio_builds(tmp_path, name):
    """不启动 OpticStudio 生成的热结构：行、求解码和数值都与 Make Thermal 相同（半口径用同一份标称值）。"""
    data = truth(name)
    backend = instance(tmp_path, data["nominal_zmx"])
    ref = zmx.parse_text(data["zmx"])
    # OpticStudio 的自动半口径与 rtc 差到 1e-5 mm 量级，这里比的是热跟随，所以先用它存盘的标称半口径
    for s, r in zip(backend.lens.surfaces, ref.surfaces):
        if s.index and s.index < len(ref.surfaces) - 1:
            s.semi_diameter, s.semi_fixed = r.semi_diameter, True
    backend.make_thermal(data["temperatures"], data["tce"])
    backend._configurations()
    assert [(r.op, r.surface, r.param, r.config, r.solve_code, r.source_config, int(r.solve[4])) for r in backend.lens.mce] ==            [(r.op, r.surface, r.param, r.config, r.solve_code, r.source_config, int(r.solve[4])) for r in ref.mce]
    # 弯曲像面的矢高取自像面自动半口径，两边算的半口径差约 1e-5 mm，间隔差到 1e-6 mm
    tol = 2e-6 if name == "seq_curved_image" else 2e-9
    for a, b in zip(backend.lens.mce, ref.mce):
        if a.op == "GLSS":
            assert a.text.upper() == b.text.upper()
        else:
            assert math.isclose(a.value, b.value, rel_tol=1e-9, abs_tol=tol), (a.op, a.surface, a.param, a.config, a.value, b.value)


def thermal_instance(temperatures=(-40.0, 65.0), tce=TCE) -> RtcBackend:
    p = base("sequential")
    p.setdefault("environment", {})["adjust_index"] = True
    p["thermal"] = {"temperatures": list(temperatures), "housing_tce": tce}
    backend = RtcBackend(None, standalone=True)
    backend.build(prescription.normalize(p))
    return backend


def test_prescription_declares_conditions_without_opticstudio():
    backend = thermal_instance()
    assert backend.configuration_count() == 3
    temps = next(o for o in backend._configurations()["operands"] if o["type"] == "TEMP")
    assert temps["values"] == [20.0, -40.0, 65.0]
    backend.select_configuration(2)
    assert backend.lens.environment.temperature == -40.0
    cold = backend.get_param("1.radius")
    backend.select_configuration(3)
    hot = backend.get_param("1.radius")
    assert cold < 61.5 < hot  # 冷缩时半径变小


def test_nominal_edit_follows_in_other_configurations():
    backend = thermal_instance()
    before = backend._configurations()
    backend.set_param("1.radius", 70.0)
    backend.select_configuration(2)
    assert backend.get_param("1.radius") == pytest.approx(70.0 / (1.0 + 7.1e-6 * -60.0) ** -1, rel=1e-9)
    after = backend._configurations()
    assert before != after


def test_thermal_cell_refuses_direct_edit():
    backend = thermal_instance()
    backend.select_configuration(2)
    radius = backend.get_param("1.radius")
    with pytest.raises(SaosError):
        backend.set_param("1.radius", 70.0)
    assert backend.get_param("1.radius") == radius
    with pytest.raises(SaosError):
        backend.vary(1, "radius", True)


def test_nominal_variable_lives_in_the_cell_and_is_saved(tmp_path):
    backend = thermal_instance()
    assert backend.vary(1, "radius", True) == "Variable"
    assert backend.vary(3, "thickness", True) == "Variable"
    keys = backend.variables()
    assert all(k.startswith("config.1.") for k in keys) and len(keys) == 2
    path = tmp_path / "v.zmx"
    backend.save_as(path)
    again = RtcBackend(None, standalone=True)
    again.load(path)
    assert again.variables() == keys


def test_wizard_groups_each_configuration_and_optimiser_lowers_every_group():
    backend = thermal_instance()
    backend.merit_wizard({"data": "Spot Radius", "reference": "Centroid", "rings": 2, "arms": 6})
    confs = [r.int1 for r in backend.merit if r.type == "CONF"]
    assert confs == [1, 1, 2, 3]
    first = backend.merit_value()
    backend.vary(3, "thickness", True)
    backend.vary(2, "radius", True)
    result = backend.optimize("10")
    assert result["final_merit"] < first
    assert backend.current_configuration() == 1


def test_merit_value_is_the_sum_over_configurations_of_single_lenses(tmp_path):
    """多结构评价函数的每一段等于把那个结构单独写成镜头求的值。"""
    backend = thermal_instance()
    backend.merit_wizard({"data": "Spot Radius", "reference": "Centroid", "rings": 2, "arms": 6})
    rows = backend.merit_rows()
    backend.select_configuration(2)
    single = RtcBackend(None, standalone=True)
    view = backend.lens.configured(2)
    single._adopt(copy.deepcopy(view))
    group = [r for r in backend.merit]
    start = group.index(next(r for r in group if r.type == "CONF" and r.int1 == 2))
    stop = group.index(next(r for r in group if r.type == "CONF" and r.int1 == 3))
    single.merit = [r for r in group[start + 1:stop]]
    single_rows = single.merit_rows()
    multi = [r for r in rows if start + 1 < r["row"] <= stop and r["type"] != "CONF"]
    assert [r["type"] for r in multi] == [r["type"] for r in single_rows]
    for a, b in zip(multi, single_rows):
        if a["value"] is not None:
            assert a["value"] == pytest.approx(b["value"], abs=1e-12)


def test_config_cell_variable_for_zoom_lens(tmp_path):
    """只有厚度行的变焦镜头：单元格也能做变量，写盘后保留。"""
    p = base("sequential")
    p["surfaces"][2].pop("solves", None)
    p["surfaces"][2]["thickness"] = 90.0
    p["configurations"] = [{"operand": "THIC", "params": [3], "values": [90.0, 95.0]}]
    backend = RtcBackend(None, standalone=True)
    backend.build(prescription.normalize(p))
    assert backend.vary_config([1], [2], True) == {"1.2": "Variable"}
    assert backend.variables() == ["config.2.1"]
    backend.set_param("config.2.1", 96.0)
    backend.select_configuration(2)
    assert backend.get_param("3.thickness") == 96.0
    path = tmp_path / "zoom.zmx"
    backend.save_as(path)
    again = RtcBackend(None, standalone=True)
    again.load(path)
    assert again.variables() == ["config.2.1"]
    assert again.lens.thickness(3, 2) == 96.0


def test_prescription_refuses_conflicting_thermal():
    p = base("sequential")
    p["thermal"] = {"temperatures": [-40.0], "housing_tce": TCE}
    with pytest.raises(SaosError):
        prescription.normalize(p)  # 没开 adjust_index
    p.setdefault("environment", {})["adjust_index"] = True
    p["thermal"]["temperatures"] = [20.0]
    with pytest.raises(SaosError):
        prescription.normalize(p)
    p["thermal"]["temperatures"] = [-40.0]
    p["configurations"] = [{"operand": "THIC", "params": [3], "values": [90.0, 95.0]}]
    with pytest.raises(SaosError):
        prescription.normalize(p)


def test_wizard_single_configuration_has_no_conf_rows():
    p = base("sequential")
    backend = RtcBackend(None, standalone=True)
    backend.build(prescription.normalize(p))
    rows = rtc_merit.wizard_rows(backend.lens, {"data": "Spot Radius", "reference": "Centroid", "rings": 2, "arms": 6})
    assert not any(r.type == "CONF" for r in rows)
    assert rtc_lens.SURFACE_PASSTHROUGH[0] == "TCED"
