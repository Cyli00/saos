from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from saos import jobs

from saos.recipes.microscope import acceptance, interview, plan
from saos.cli import main
from saos.protocol import SaosError
from saos.recipes.microscope.spec import parse_spec


FIXTURE = Path(__file__).parent / "fixtures" / "interview" / "visual.yaml"


def draft():
    return interview.read_draft(FIXTURE)


def set_field(data, path, value, source="user"):
    interview.assign(data["spec"], path, value)
    data["provenance"][path] = {"source": source, "basis": "情景用户明确给出"}


def test_visual_interview_preserves_needs_and_discloses_defaults():
    data = draft()
    result = interview.check(data)
    assert result["ready"]
    assert data == draft()
    assert result["spec"]["na"] == 0.4
    fields = {r["path"]: r for r in result["fields"]}
    assert fields["na"]["source"] == "assumption"
    assert fields["limits.working_distance_min"]["source"] == "user"
    assert result["requirements"][-1]["kind"] == "preference"
    assert parse_spec(result["spec"]).item("elements") is None
    canonical = deepcopy(data["spec"])
    canonical.update(spec_version=1, na=0.4, correction_class="achromat")
    assert acceptance.acceptance_config(parse_spec(result["spec"])) == acceptance.acceptance_config(parse_spec(canonical))


def test_camera_measurement_keeps_frequency_plane_and_enables_targets():
    data = draft()
    set_field(data, "receiver", "camera")
    set_field(data, "pixel_size", 3.45)
    set_field(data, "targets.distortion.threshold_pct", 0.5)
    set_field(data, "targets.mtf.frequency", 1000)
    set_field(data, "targets.mtf.plane", "object")
    result = interview.check(data)
    spec = parse_spec(result["spec"])
    assert spec.correction_class == "plan_achromat"
    assert spec.item("mtf").params["frequency"] == 50
    assert spec.item("distortion").threshold == 0.5
    assert spec.item("lateral_color").threshold == pytest.approx(16.655135)


def test_missing_camera_pixel_or_four_times_wd_remains_a_question():
    data = draft()
    set_field(data, "receiver", "camera")
    assert not interview.check(data)["ready"]
    data = draft()
    set_field(data, "magnification", 4)
    del data["spec"]["limits"]["working_distance_min"]
    del data["provenance"]["limits.working_distance_min"]
    data["requirements"] = data["requirements"][:2]
    assert not interview.check(data)["ready"]


def test_unsupported_scope_and_unresolved_requests_block_plan():
    data = draft()
    data["scope"]["medium"] = "oil"
    assert not interview.check(data)["ready"]
    data = draft()
    data["requirements"].append({"text": "要兼容机械齐焦肩", "kind": "unresolved", "fields": []})
    assert not interview.check(data)["ready"]


@pytest.mark.parametrize("path,value", [("na", float("nan")), ("pixel_size", -3), ("targets.mtf.threshold", 1.1), ("targets.field_rms_wfe.range", 1.2), ("limits.glass.allow_model", "false")])
def test_invalid_numeric_and_boolean_input_is_not_silently_accepted(path, value):
    data = draft()
    set_field(data, path, value)
    with pytest.raises(SaosError):
        interview.check(data)


def test_unknown_or_unattributed_fields_are_rejected():
    data = draft()
    data["spec"]["numerical_aperture"] = 0.5
    with pytest.raises(SaosError, match="不支持"):
        interview.check(data)
    data = draft()
    del data["provenance"]["magnification"]
    with pytest.raises(SaosError, match="provenance"):
        interview.check(data)


@pytest.fixture
def example_library(tmp_path, monkeypatch):
    root = tmp_path / "library"
    d = root / "entries" / "TEST-e1"
    d.mkdir(parents=True)
    monkeypatch.setenv("SAOS_LIBRARY", str(root))
    entry = {"id": "TEST-e1", "status": "admitted", "source": {"patent": "测试专利", "url": "javascript:alert(1)"}, "embodiment": "例 1",
             "metrics": {"focal_length_mm": 10, "na": 0.4, "field_number": 22, "elements": 1, "claimed_class": "plan",
                         "wd_mech_mm": 2, "optical_length_mm": 50, "clear_diameter_mm": 20,
                         "cover_glass": None, "glasses": [{"material": "OLD", "catalog": "OHARA", "status": "Obsolete"}]},
             "repro": {"pass": True}, "assumptions": [], "doubts": []}
    prescription = {"object": {"thickness": 2}, "surfaces": [
        {"radius": 10, "thickness": 3, "material": "OLD", "comment": "patent r1"},
        {"radius": -10, "thickness": 5, "material": "", "comment": "patent r2"},
        {"radius": "inf", "thickness": 0, "comment": "saos:stop"}]}
    (d / "entry.json").write_text(json.dumps(entry), encoding="utf-8")
    (d / "prescription.json").write_text(json.dumps(prescription), encoding="utf-8")
    return d


def create_plan(saos_home, example_library, tmp_path, data=None):
    job = jobs.create_job("review")
    path = tmp_path / "interview.json"
    path.write_text(json.dumps(data or draft(), ensure_ascii=False), encoding="utf-8")
    result = plan.create(job.id, path, "TEST-e1", "按 NA 与倍率选取，后续需换玻璃并收紧长度。")
    return job, result


def test_plan_confirm_handoff_and_immutable_approved_revision(saos_home, example_library, tmp_path):
    job, result = create_plan(saos_home, example_library, tmp_path)
    assert not job.read_state().get("spec")
    with pytest.raises(SaosError, match="尚未"):
        plan.handoff(job.id)
    confirmed = plan.confirm(job.id, result["revision"], "我确认这个版本")
    assert confirmed["state"] == "confirmed"
    handoff = plan.handoff(job.id)
    assert handoff["config_sha256"] == acceptance.config_hash(acceptance.acceptance_config(parse_spec(plan.inspect(job.id)["payload"]["review"]["spec"])))
    assert plan.confirm(job.id, result["revision"], "确认")["already_confirmed"]
    with pytest.raises(SaosError, match="已确认"):
        plan.create(job.id, FIXTURE, "TEST-e1", "理由")
    spec_path = job.root / confirmed["spec"]
    spec_data = json.loads(spec_path.read_text(encoding="utf-8"))
    spec_data["na"] = 0.3
    spec_path.write_text(json.dumps(spec_data), encoding="utf-8")
    with pytest.raises(SaosError, match="偏离"):
        plan.handoff(job.id)


def test_plan_html_escapes_input_and_discloses_glass_mismatch(saos_home, example_library, tmp_path):
    data = draft()
    data["request"] = '<img src=x onerror="alert(1)"> & 用户原话'
    job, result = create_plan(saos_home, example_library, tmp_path, data)
    page = Path(result["page"]).read_text(encoding="utf-8")
    assert '<img src=x' not in page and '&lt;img src=x' in page
    assert 'href="javascript:' not in page
    assert "OLD" in page and "不会自动放宽玻璃约束" in page
    assert 'aria-labelledby="section-title section-desc"' in page


@pytest.mark.parametrize("file", ["index.html", "spec.json", "plan.json"])
def test_changed_artifacts_cannot_be_confirmed(saos_home, example_library, tmp_path, file):
    job, result = create_plan(saos_home, example_library, tmp_path)
    p = Path(result["page"]).with_name(file)
    p.write_text(p.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(SaosError, match="变化"):
        plan.confirm(job.id, result["revision"], "确认")


def test_new_version_supersedes_old_and_missing_structure_blocks_confirmation(saos_home, example_library):
    job = jobs.create_job("versions")
    first = plan.create(job.id, FIXTURE, "TEST-e1", "理由一")
    second = plan.create(job.id, FIXTURE, None)
    with pytest.raises(SaosError, match="最新版本"):
        plan.confirm(job.id, first["revision"], "确认")
    with pytest.raises(SaosError, match="尚未选定"):
        plan.confirm(job.id, second["revision"], "确认")


def test_evaluator_change_requires_new_review(saos_home, example_library, tmp_path, monkeypatch):
    job, result = create_plan(saos_home, example_library, tmp_path)
    monkeypatch.setattr(acceptance, "ZERN_MAX_TERM", acceptance.ZERN_MAX_TERM + 1)
    with pytest.raises(SaosError, match="评估器"):
        plan.confirm(job.id, result["revision"], "确认")


def test_cli_interview_and_plan_help_are_available(capsys):
    assert main(["microscope", "interview", "check", str(FIXTURE)]) == 0
    assert json.loads(capsys.readouterr().out)["result"]["ready"]
