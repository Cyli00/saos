"""验收评估中不依赖 Zemax 的部分：视场波像差的扣除规则、无效测量值、验收配置冻结。"""

from __future__ import annotations

import math

import pytest

from zea import jobs, rounds
from zea.acceptance import Evaluator, acceptance_config, config_hash, field_wfe
from zea.protocol import ErrorCode, ZeaError
from zea.spec import Item, load_spec, parse_spec

SPEC_YAML = """\
spec_version: 1
correction_class: {cls}
magnification: 10
na: 0.25
limits:
  working_distance_min: {{value: 10.6, basis: "{basis}"}}
targets:
  field_rms_wfe: {{range: 0.6}}
"""


def spec_of(cls="achromat", basis="x"):
    import yaml

    return parse_spec(yaml.safe_load(SPEC_YAML.format(cls=cls, basis=basis)))


# ---- 1. 视场波像差：未启用平场比时计入离焦 ----


def test_pure_defocus_counts_when_plan_field_is_off():
    assert field_wfe(0.2, 0.2, 0.0, 0.0, remove_defocus_astig=False) == pytest.approx(0.2)
    assert field_wfe(0.2, 0.2, 0.0, 0.0, remove_defocus_astig=True) == pytest.approx(0.0)
    assert field_wfe(0.3, 0.2, 0.0, 0.1, remove_defocus_astig=True) == pytest.approx(0.2)
    assert field_wfe(0.3, 0.2, 0.0, 0.1, remove_defocus_astig=False) == pytest.approx(0.3)


def test_achromat_field_item_keeps_defocus_and_gives_expected_gap():
    item = spec_of("achromat").item("field_rms_wfe")
    assert item.params["remove_defocus_astig"] is False
    value = field_wfe(0.2, 0.2, 0.0, 0.0, remove_defocus_astig=item.params["remove_defocus_astig"])
    assert item.gap(value) == pytest.approx(0.2 / (1 / 14) - 1)  # 1.8
    assert spec_of("plan_achromat").item("field_rms_wfe").params["remove_defocus_astig"] is True


# ---- 3. 无效测量值不能判为通过 ----


def passing_values(spec):
    good = {
        "magnification": 0.0, "working_distance": 20.0, "optical_length": 30.0, "clear_diameter": 10.0,
        "glass_status": 0.0, "surface_types": 0.0, "center_thickness_glass": 1.0, "edge_thickness_glass": 1.0,
        "air_gap": 0.1, "axial_rms_wfe": 0.01, "axial_color": 0.0, "osc": 0.0, "plan_field": -0.5,
        "field_rms_wfe": 0.01, "lateral_color": 1.0,
    }
    return {i.id: good[i.id] for i in spec.items}


def report(spec, measured):
    return Evaluator(None, spec, []).report(measured, {}, None, 0.0)


def test_all_passing_values_pass():
    spec = spec_of("plan_achromat")
    out = report(spec, passing_values(spec))
    assert out["verdict"] == "pass" and out["spec_shortfall"] == 0


@pytest.mark.parametrize("bad", [math.nan, -math.inf, math.inf, None, "0.01"])
def test_invalid_measurement_is_invalid_not_pass(bad):
    spec = spec_of("plan_achromat")
    values = passing_values(spec)
    values["axial_rms_wfe"] = bad
    out = report(spec, values)
    assert out["verdict"] == "invalid"
    assert math.isinf(out["spec_shortfall"])
    assert "axial_rms_wfe" in out["invalid"]["items"]


def test_missing_item_is_invalid():
    spec = spec_of("plan_achromat")
    values = passing_values(spec)
    del values["osc"]
    assert report(spec, values)["verdict"] == "invalid"


def test_infinite_air_gap_means_no_gaps_and_is_valid():
    spec = spec_of("plan_achromat")
    values = passing_values(spec)
    values["air_gap"] = math.inf
    assert report(spec, values)["verdict"] == "pass"


def test_item_rejects_bad_threshold_or_scale_and_nan_gap():
    for threshold, scale in ((math.nan, 1.0), (1.0, 0.0), (1.0, math.inf), (math.inf, 1.0)):
        with pytest.raises(ZeaError):
            Item("x", "target", "upper", threshold, scale, "")
    with pytest.raises(ValueError):
        Item("x", "target", "upper", 1.0, 1.0, "").gap(math.nan)


def test_spec_with_nan_threshold_is_rejected():
    import yaml

    data = yaml.safe_load(SPEC_YAML.format(cls="achromat", basis="x"))
    data["limits"]["working_distance_min"] = float("nan")
    with pytest.raises(ZeaError):
        parse_spec(data)
    data["limits"]["working_distance_min"] = 10.6
    data["magnification"] = float("nan")
    with pytest.raises(ZeaError):
        parse_spec(data)


# ---- 2. 验收配置冻结 ----


def write_spec(job, text, name="spec.yaml"):
    (job.spec_dir / name).write_text(text, encoding="utf-8")
    return f"spec/{name}"


def frozen_check(job, name):
    spec = load_spec(job.root / name)
    config = acceptance_config(spec)
    rounds.check_frozen(job, name, config, config_hash(config))


def test_changed_threshold_is_detected(zea_home):
    job = jobs.create_job("freeze")
    name = write_spec(job, SPEC_YAML.format(cls="achromat", basis="first"))
    config = acceptance_config(load_spec(job.root / name))
    rounds.freeze(job, name, config, config_hash(config))
    frozen_check(job, name)  # 未改动

    # 只改说明文字：补全后的验收配置不变，允许
    write_spec(job, SPEC_YAML.format(cls="achromat", basis="edited note"))
    frozen_check(job, name)

    # 原地改阈值：必须拒绝
    write_spec(job, SPEC_YAML.format(cls="achromat", basis="x").replace("10.6", "11.0"))
    with pytest.raises(ZeaError) as exc:
        frozen_check(job, name)
    assert exc.value.code == ErrorCode.SPEC_CHANGED
    assert "items" in exc.value.details["changed_keys"]


def test_changed_spec_path_is_detected(zea_home):
    job = jobs.create_job("freeze")
    name = write_spec(job, SPEC_YAML.format(cls="achromat", basis="x"))
    other = write_spec(job, SPEC_YAML.format(cls="achromat", basis="x"), "other.yaml")
    config = acceptance_config(load_spec(job.root / name))
    rounds.freeze(job, name, config, config_hash(config))
    with pytest.raises(ZeaError) as exc:
        frozen_check(job, other)
    assert exc.value.code == ErrorCode.SPEC_CHANGED


def test_config_includes_evaluator_constants():
    config = acceptance_config(spec_of())
    assert config["evaluator"]["zern_max_term"] == 66
    assert config["derived"]["delta_ob_um"] == pytest.approx(4.369, abs=1e-3)
    assert config_hash(config) == config_hash(acceptance_config(spec_of()))


# ---- 3b. 原始测量值在参与运算之前检查 ----

from zea.acceptance import InvalidEvaluation, focus_on_axis, residual_rms  # noqa: E402


@pytest.mark.parametrize("plan", [False, True])
def test_raw_nan_rms_does_not_become_zero(plan):
    with pytest.raises(InvalidEvaluation):
        field_wfe(math.nan, 0.0, 0.0, 0.0, remove_defocus_astig=plan)


def test_axial_residual_rejects_nan_inputs():
    with pytest.raises(InvalidEvaluation):
        residual_rms(math.nan, 0.0)
    with pytest.raises(InvalidEvaluation):
        residual_rms(0.1, math.nan)
    with pytest.raises(InvalidEvaluation):
        residual_rms(math.inf, 0.0)
    assert residual_rms(0.1, 0.1000001) == 0.0  # 拟合噪声造成的微小负值按 0 处理


class NanBackend:
    """返回指定数值的桩后端，用来模拟 OpticStudio 给出 NaN 或无穷。"""

    def __init__(self, operand_value=0.0, zernike_values=None):
        self.operand_value = operand_value
        self.zernike_values = zernike_values or {}

    def operand(self, name, int1, int2, data):
        return self.operand_value

    def zernike(self, *, wave, field, samp, max_term, terms):
        return {t: self.zernike_values.get(t, 0.0) for t in terms}

    def surfaces(self):
        return [{"thickness": 13.0}]

    def set_surface(self, index, param, value):
        return {}


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_operand_values_are_checked(value):
    evaluator = Evaluator(NanBackend(operand_value=value), spec_of("plan_achromat"), [])
    with pytest.raises(InvalidEvaluation):
        evaluator.op("OSCD", 0, 2, 0.7)


@pytest.mark.parametrize("term", [-1, -4, 4, 6])
def test_zernike_values_are_checked_including_fit_residual(term):
    evaluator = Evaluator(NanBackend(zernike_values={term: math.nan}), spec_of("plan_achromat"), [])
    with pytest.raises(InvalidEvaluation):
        evaluator.checked_zern(2, 0.0, (-4, 4, 5, 6))


def test_focus_rejects_nan_defocus():
    with pytest.raises(InvalidEvaluation):
        focus_on_axis(
            NanBackend(zernike_values={4: math.nan}), gap=0, wave=2, field=1,
            lam_um=0.546, na_image=0.02, magnification=20, tol_mm=1e-5,
        )


# ---- 3c. +inf 只对“没有空气间隔”有效 ----


@pytest.mark.parametrize("item_id", ["working_distance", "edge_thickness_glass", "center_thickness_glass"])
def test_infinite_lower_bound_values_are_invalid(item_id):
    spec = spec_of("plan_achromat")
    values = passing_values(spec)
    values[item_id] = math.inf
    out = report(spec, values)
    assert out["verdict"] == "invalid" and item_id in out["invalid"]["items"]


def test_infinite_mtf_is_invalid():
    import yaml

    data = yaml.safe_load(SPEC_YAML.format(cls="plan_achromat", basis="x"))
    data.update(receiver="camera", pixel_size=6.5)
    spec = parse_spec(data)
    values = {**passing_values(spec_of("plan_achromat")), "mtf": math.inf}
    values = {i.id: values[i.id] for i in spec.items}
    out = report(spec, values)
    assert out["verdict"] == "invalid" and "mtf" in out["invalid"]["items"]


# ---- 模型玻璃许可 ----


def test_model_glass_counts_as_violation_unless_allowed():
    import yaml

    data = yaml.safe_load(SPEC_YAML.format(cls="achromat", basis="x"))
    data["limits"]["glass"] = {"catalogs": ["SCHOTT"], "allowed_status": ["Standard", "Preferred"]}
    rows = [
        {"index": 1, "material": {"model": {"nd": 1.81, "vd": 25.5, "dpgf": 0.0}}},
        {"index": 3, "material": "N-BK7"},
    ]
    strict = Evaluator(None, parse_spec(data), [])
    assert strict.glass_violations(rows)[0] == 1.0
    data["limits"]["glass"]["allow_model"] = True
    lenient = Evaluator(None, parse_spec(data), [])
    assert lenient.glass_violations(rows)[0] == 0.0
    assert acceptance_config(lenient.spec)["glass_allow_model"] is True
