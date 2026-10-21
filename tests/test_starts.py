"""G01–G03 的实施用例；容差见 docs/recipes/microscope/starts-extraction.md。"""
from __future__ import annotations

import copy
import json
import math

import pytest

from saos import prescription
from saos.protocol import SaosError
from saos.starts import query, requirements, scale_prescription, screen
from saos.starts.library import entries, library_root


def test_six_forms_have_sources_and_valid_prescriptions():
    root = library_root()
    found = entries(root)
    assert {e["id"] for e in found} == {"cooke", "petzval", "double_gauss", "telephoto", "reverse_telephoto", "cassegrain"}
    for entry in found:
        assert entry["source"].get("patent") or (entry["source"].get("book") and entry["source"].get("page"))
        assert entry["source"]["url"].startswith("https://")
        prescription.normalize(json.loads((root / "entries" / entry["id"] / "prescription.json").read_text(encoding="utf-8")))


def test_scaling_preserves_normalized_shape_with_aspheres_and_decentres():
    p = {"aperture": {"type": "image_f_number", "value": 4}, "fields": {"type": "object_height", "x": [0], "y": [2]},
         "object": {"thickness": 100}, "surfaces": [{"type": "EvenAspheric", "radius": 20, "thickness": 3,
         "conic": -1, "semi_diameter": 5, "mech_semi_diameter": 6, "params": {"par2": .001},
         "decenter_tilt": {"before": {"decenter_x": 1, "tilt_y": 15}, "after": "reverse"}}]}
    original = copy.deepcopy(p)
    scaled = scale_prescription(p, 2)
    assert p == original
    s = scaled["surfaces"][0]
    assert (s["radius"], s["thickness"], s["semi_diameter"], s["mech_semi_diameter"]) == (40, 6, 10, 12)
    assert s["conic"] == -1 and scaled["aperture"]["value"] == 4
    assert s["decenter_tilt"]["before"] == {"decenter_x": 2, "tilt_y": 15}
    assert scaled["fields"]["y"] == [4] and scaled["object"]["thickness"] == 200
    radius = 2.5
    assert s["params"]["par2"] * (radius * 2)**4 == pytest.approx(2 * .001 * radius**4, rel=1e-12)


@pytest.mark.parametrize("factor", [0, -1, math.nan, math.inf, True])
def test_scaling_rejects_undefined_length_scale(factor):
    with pytest.raises(SaosError):
        scale_prescription({"surfaces": []}, factor)


def test_unknown_parameter_dimensions_are_not_silently_scaled():
    with pytest.raises(SaosError):
        scale_prescription({"surfaces": [{"type": "Unknown", "params": {"par1": 2}}]}, 2)


def test_query_retains_exclusion_reasons_and_finite_conjugate():
    target = {"efl_mm": 50, "half_field_deg": 5, "f_number": 8, "wavelengths_um": [.55]}
    chosen, excluded = query(target)
    assert "double_gauss" not in {e["id"] for e in chosen}
    assert any(e["id"] == "double_gauss" and e["reasons"] for e in excluded)
    finite, _ = query(target, conjugate="finite")
    assert [e["id"] for e in finite] == ["double_gauss"]
    assert all(e["tightest"] in e["pressure"] for e in chosen)


def test_requirements_convert_units_and_refuse_ambiguous_bounds():
    d = {"constants": [{"id": "f", "value": 5, "unit": "cm"}, {"id": "h", "value": 5, "unit": "deg"},
                       {"id": "n", "value": 8}, {"id": "w", "value": [500, 600], "unit": "nm"}]}
    bindings = {"efl_mm": "f", "half_field_deg": "h", "f_number": "n", "wavelengths_um": "w"}
    r = requirements(d, bindings)
    assert r["values"] == {"efl_mm": 50, "half_field_deg": 5, "f_number": 8, "wavelengths_um": [.5, .6]}
    d["constants"][0] = {"id": "f", "goal": {"min": 4, "max": 6}, "unit": "cm"}
    with pytest.raises(SaosError, match="范围"):
        requirements(d, bindings)
    assert requirements(d, {**bindings, "efl_mm": {"id": "f", "value": 5}})["values"]["efl_mm"] == 50


def test_all_candidates_screen_before_equal_short_budget(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from saos import scan
    zero = {"candidates": [{"id": "a"}, {"id": "b"}], "stages": []}
    short = {**zero, "stages": [{"local": {"max_seconds": 12}}], "prepare": "prepare.py"}
    for name, data in (("screen.json", zero), ("short.json", short)):
        (tmp_path / name).write_text(json.dumps(data), encoding="utf-8")
    plans = []
    def run(ctx, plan, workers):
        plans.append(copy.deepcopy(plan))
        return {"folder": f"scans/{len(plans):03d}"}
    monkeypatch.setattr(scan, "run", run)
    ctx = SimpleNamespace(job=SimpleNamespace(root=tmp_path), cancel_requested=lambda: False)
    screen(ctx, {"screen_plan": "screen.json", "short_plan": "short.json"}, workers=2)
    assert plans[0]["stages"] == [] and plans[1]["stages"] == short["stages"]
    assert plans[0]["candidates"] == plans[1]["candidates"] and plans[1]["screening_scan"] == "scans/001"


@pytest.mark.parametrize("factor", [.5, 2.0, 10.0])
def test_optiland_first_order_scales_with_prescription(factor):
    from saos.backend.optiland import OptilandBackend
    b = OptilandBackend()
    b.start()
    data = {"aperture": {"type": "entrance_pupil_diameter", "value": 2}, "fields": {"type": "angle", "y": [0, .1]},
            "wavelengths": {"um": [.55]}, "object": {"thickness": "inf"},
            "surfaces": [{"radius": 50, "thickness": 5, "material": "N-BK7", "stop": True},
                         {"radius": -50, "thickness": 48, "material": ""}]}
    try:
        b.build(prescription.normalize(data))
        before = b.operand("EFFL", 0, 0, [])
        b.build(prescription.normalize(scale_prescription(data, factor)))
        assert b.operand("EFFL", 0, 0, []) == pytest.approx(before * factor, rel=1e-9, abs=1e-9)
    finally:
        b.close()


def test_small_aperture_prediction_against_real_marginal_ray():
    from saos.backend.optiland import OptilandBackend
    from saos.backend.optiland_operands import trace_record
    from saos.starts import predict
    b = OptilandBackend()
    b.start()
    data = {"aperture": {"type": "entrance_pupil_diameter", "value": 1}, "fields": {"type": "angle", "y": [0, .1]},
            "wavelengths": {"um": [.55]}, "object": {"thickness": "inf"},
            "surfaces": [{"radius": 50, "thickness": 5, "material": "N-BK7", "stop": True},
                         {"radius": -50, "thickness": 48, "material": ""}]}
    try:
        b.build(prescription.normalize(data))
        b.set_surface(2, "thickness", 48 + float(b.optic.paraxial.F2()))
        prediction = predict(b, rings=1, arms=4)
        assert not prediction["errors"]
        actual = trace_record(b, 1, 0, 0, 0, 1, 3)[-1]["position"][1]
        expected = prediction["third_order"]["totals"]["TSC"]
        assert expected == pytest.approx(actual, rel=.02, abs=1e-7)
        assert prediction["incidence"]["invalid"] == 0
        import numpy as np
        assert np.sum(prediction["seidel"]["surfaces"], axis=0) == pytest.approx(prediction["seidel"]["totals"], rel=1e-12, abs=1e-12)
    finally:
        b.close()


@pytest.mark.parametrize("data", [None, {}, {"surfaces": []}, {"surfaces": [None]},
    {"surfaces": [{"radius": "1"}]}, {"surfaces": [{"radius": True}]}, {"surfaces": [{"radius": math.nan}]},
    {"surfaces": [{"params": {"par2": True}, "type": "EvenAspheric"}]},
    {"surfaces": [{"params": [], "type": "EvenAspheric"}]},
    {"surfaces": [{}], "aperture": {"type": "entrance_pupil_diameter", "value": "1"}},
    {"surfaces": [{}], "fields": {"type": "object_height", "y": [True]}},
    {"surfaces": [{"decenter_tilt": {"before": {"decenter_x": "1"}}}]},
    {"surfaces": [{"solves": {"thickness": {"type": "pickup", "offset": math.nan}}}]},
])
def test_bad_scaling_inputs_report_saos_error_without_modifying_source(data):
    from saos.protocol import ErrorCode
    original = repr(data)
    with pytest.raises(SaosError) as error:
        scale_prescription(data, 2)
    assert error.value.code == ErrorCode.BAD_ARGS and repr(data) == original


def test_empty_params_and_explicit_infinity_are_supported():
    from saos.starts import scale_surface
    assert scale_surface({"radius": "inf", "params": None}, 2) == {"radius": math.inf, "params": {}}
    assert scale_surface({"params": {}}, 2) == {"params": {}}


def test_generate_requires_prepare_before_backend_or_artifacts(saos_home):
    from saos import jobs, starts
    job = jobs.create_job("starts-config")
    def forbidden():
        raise AssertionError("不能启动后端")
    with pytest.raises(SaosError, match="prepare"):
        starts.generate(job, {}, short_seconds=1, prepare="", backend_factory=forbidden)
    assert not (job.root / "explore" / "starts").exists()


def test_generate_passes_normalized_prescriptions_without_a_shim(saos_home, tmp_path, monkeypatch):
    import shutil
    import yaml
    from saos import jobs, starts, demand
    from saos.starts import prediction
    root = tmp_path / "starts-library"
    shutil.copytree(library_root() / "entries" / "cooke", root / "entries" / "cooke")
    job = jobs.create_job("starts-canonical")
    doc = {"schema": 2, "constants": [
        {"id": "f", "description": "焦距", "value": 50, "unit": "mm", "source": "自动化测试夹具"},
        {"id": "h", "description": "半视场", "value": 1, "unit": "deg", "source": "自动化测试夹具"},
        {"id": "n", "description": "f数", "value": 8, "source": "自动化测试夹具"},
        {"id": "w", "description": "波长", "value": [.4861327, .5875618, .6562725], "unit": "um", "source": "自动化测试夹具"}],
        "variables": [], "objectives": [{"id":"length","description":"总长","check":{"operand":{"type":"TOTR"}},"origin":"user","must":{"max":200},"source":"自动化测试夹具"}], "summary": demand.QUESTION_LINE["zh"]}
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(doc,allow_unicode=True),encoding="utf-8")
    (job.root / "prepare.py").write_text("def run(w):\n    return None\n",encoding="utf-8")
    built = []
    class Backend:
        def start(self): pass
        def close(self): pass
        def build(self, data):
            assert len(data["fields"]["x"]) == len(data["fields"]["y"]) == len(data["fields"]["weights"])
            assert len(data["wavelengths"]["um"]) == len(data["wavelengths"]["weights"])
            assert "environment" in data and "ray_aiming" in data
            built.append(copy.deepcopy(data))
        def operand(self, *args): return 50.0
    monkeypatch.setattr(prediction,"predict",lambda backend: {"first_order": {"efl": 50}})
    out = starts.generate(job, {"efl_mm":"f","half_field_deg":"h","f_number":"n","wavelengths_um":"w"},
                          short_seconds=1, prepare="prepare.py", root=root, backend_factory=Backend)
    assert out["count"] == 1 and out["not_evaluated"] == [] and len(built) == 2
    saved = json.loads((job.root / out["folder"] / "cooke.json").read_text(encoding="utf-8"))
    assert saved["fields"]["x"] == [0,0,0] and saved["wavelengths"]["weights"] == [1,1,1]


def test_extrapolation_is_explicit_and_preserves_source_limits():
    target = {"efl_mm":50,"half_field_deg":20,"f_number":4,"wavelengths_um":[.4861327,.5875618,.6562725]}
    strict, _ = query(target)
    assert not strict
    exploratory, _ = query(target, allow_extrapolation=True)
    cooke = next(e for e in exploratory if e["id"] == "cooke")
    assert cooke["extrapolation"] and cooke["applicability"]["f_number_min"] == 5
    assert "double_gauss" not in {e["id"] for e in exploratory}
