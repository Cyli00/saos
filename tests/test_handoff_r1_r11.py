"""B0/B2 交接里 R1–R11 的定向用例：旧指纹、历史确认、时间排序、错误分类、快照别名与零候选归档。"""

from __future__ import annotations

import copy
import inspect
import json
import math
from pathlib import Path

import pytest
import yaml

from saos import demand, jobs, milestones, model_gate, stages, starts
from saos.backend.fake import FakeBackend
from saos.backend.optiland_materials import MaterialError
from saos.daemon import Daemon
from saos.protocol import ErrorCode, SaosError
from saos.ui import stage


def demand_doc():
    return {"schema": 2, "constants": [{"id": "lens", "description": "sample", "value": "a.zmx", "source": "user"}],
            "variables": [], "objectives": [{"id": "length", "description": "total track",
                                             "check": {"operand": {"type": "TOTR"}}, "origin": "user",
                                             "must": {"max": 150.0}, "scale": 5.0, "source": "user"}],
            "summary": "测试"}


def desc(**overrides):
    """一份带后加指纹项的结构描述，用来检查旧确认的对齐规则。"""
    d = {
        "lens_units": "Millimeters",
        "aperture": {"type": "ObjectSpaceNA", "value": 0.5, "telecentric_object": False,
                     "afocal_image": False, "afocal_units": "Radians"},
        "ray_aiming": "Off",
        "fields": {"type": "ObjectHeight", "normalization": "Radial",
                   "items": [{"x": 0.0, "y": 0.0, "weight": 1.0}, {"x": 0.0, "y": 0.2, "weight": 1.0}]},
        "wavelengths": [{"um": 0.488, "weight": 1.0, "primary": False},
                        {"um": 0.532, "weight": 1.0, "primary": True}],
        "surfaces": [
            {"index": 0, "type": "Standard", "radius": math.inf, "conic": 0.0, "material": "",
             "is_stop": False, "material_source": {"kind": "none"}, "aperture": {"type": "None"}, "params": []},
            {"index": 1, "type": "Standard", "radius": 10.0, "conic": 0.0, "material": "N-BK7",
             "is_stop": True, "material_source": {"kind": "catalog", "name": "N-BK7", "catalogs": ["SCHOTT"]},
             "aperture": {"type": "None"}, "params": []},
            {"index": 2, "type": "Standard", "radius": -10.0, "conic": 0.0, "material": "",
             "is_stop": False, "material_source": {"kind": "none"}, "aperture": {"type": "None"}, "params": []},
            {"index": 3, "type": "Standard", "radius": math.inf, "conic": 0.0, "material": "",
             "is_stop": False, "material_source": {"kind": "none"}, "aperture": {"type": "None"}, "params": []},
        ],
        "configurations": {"count": 1, "operands": [{"type": "TEMP", "params": [], "values": [20.0]}]},
        "environment": {"temperature": 20.0},
        "apodization": {"type": "None", "value": 0.0},
        "polarization": {"mode": "None"},
        "coating_file": "coating.dat",
    }
    d.update(overrides)
    return d


def test_r1_old_fingerprint_and_appended_sampling_need_no_reason():
    current = desc()
    old = copy.deepcopy(model_gate.fingerprint(current))
    for key in model_gate.LATER_KEYS:
        old.pop(key, None)
    old["aperture"].pop("afocal_units", None)
    assert model_gate.fingerprint_changes(old, model_gate.fingerprint(current), current) == []

    appended = desc()
    appended["fields"]["items"].append({"x": 0.0, "y": 0.4, "weight": 1.0})
    appended["wavelengths"].append({"um": 0.65, "weight": 1.0, "primary": False})
    assert model_gate.structure_changes(current, appended) == []

    changed = desc()
    changed["fields"]["items"][0]["y"] = 0.1
    assert model_gate.structure_changes(current, changed) == ["fields.items[0][1]"]

    structural = desc()
    structural["surfaces"][1]["type"] = "Even Asphere"
    assert model_gate.structure_changes(current, structural) == ["surfaces[1].type"]


def test_r1_environment_declaration_change_needs_a_rebuild_reason(saos_home, fake_lens_file):
    backend = FakeBackend()
    daemon = Daemon(backend, backend.start(), "extension")
    job = jobs.create_job("r1-environment")
    call = lambda op, **args: daemon.handle({"op": op, "args": args})
    call("lens.import", job=job.id, src=str(fake_lens_file), name="a.zmx")
    call("lens.load", job=job.id, file="a.zmx")
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(demand_doc(), allow_unicode=True), encoding="utf-8")
    demand.confirm(job.root, "确认需求")
    notes = {"surfaces": {"0": "object", "1": "front", "2": "back", "3": "image"},
             "assumptions": ["test"], "first_order": ["f = 50 mm"], "environment": []}
    folder = call("lens.describe", job=job.id, notes=notes)["result"]["folder"]
    assert call("lens.approve", job=job.id, model=folder, quote="确认模型")["ok"]
    notes["environment"] = [1]
    folder2 = call("lens.describe", job=job.id, notes=notes)["result"]["folder"]
    with pytest.raises(SaosError) as refused:
        model_gate.approve(job, folder2, "确认模型")
    assert refused.value.code == ErrorCode.BAD_ARGS and "重建原因" in refused.value.message
    assert model_gate.approve(job, folder2, "确认模型", "样品面现在声明为环境")["rebuild_reason"]


def _write_model(job, number, model_desc, *, legacy=True, time="2026-10-21T00:00:00+00:00", sequence=1,
                 environment=(), corrupt=False, seal=True):
    folder = job.root / "models" / f"{number:03d}-a"
    folder.mkdir(parents=True, exist_ok=True)
    fp = model_gate.fingerprint(model_desc, environment)
    data = {"job": job.id, "lens": "a.zmx", "time": time, "notes": {"environment": list(environment)},
            "warnings": [], "fingerprint": fp, "fingerprint_sha": model_gate.digest(fp), "model": model_desc}
    if not legacy:
        data.update(approval_schema=2, shown={}, shown_sha=model_gate.digest({}), unknowns=[])
    (folder / "model.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    record = {"rebuild_reason": None, "model": folder.relative_to(job.root).as_posix(), "lens": "a.zmx",
              "quote": "确认", "time": time, "approval_sequence": sequence,
              "fingerprint_sha": data["fingerprint_sha"], "warnings_acknowledged": []}
    (folder / "approval.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    if seal and not legacy:
        job.seal(folder / "model.json", folder / "approval.json")
    if corrupt:
        (folder / "approval.json").write_text("{not json", encoding="utf-8")
    return folder


def test_r2_legacy_unsealed_approval_still_matches(saos_home):
    job = jobs.create_job("r2-legacy")
    _write_model(job, 1, desc(), legacy=True)
    records = model_gate.approvals(job)
    assert len(records) == 1 and records[0]["legacy_unsealed"] is True
    matched = model_gate.require_approved(job, desc())
    assert matched["model"] == "models/001-a" and matched["legacy_unsealed"] is True


def test_r2_modern_approval_without_seal_is_not_downgraded(saos_home):
    job = jobs.create_job("r2-modern")
    _write_model(job, 1, desc(), legacy=False, seal=False)
    problems = []
    assert model_gate.approvals(job, problems=problems) == []
    assert problems[0]["path"] == "models/001-a/model.json"
    with pytest.raises(SaosError) as refused:
        model_gate.require_approved(job, desc())
    assert refused.value.code == ErrorCode.EVIDENCE_TAMPERED
    assert refused.value.details["problems"][0]["path"] == "models/001-a/model.json"


def test_r2_corrupt_earliest_does_not_block_a_later_match(saos_home):
    job = jobs.create_job("r2-partial")
    _write_model(job, 1, desc(), legacy=True, corrupt=True)
    _write_model(job, 2, desc(), legacy=True, sequence=2)
    problems = []
    records = model_gate.approvals(job, problems=problems)
    assert [r["model"] for r in records] == ["models/002-a"]
    assert [p["path"] for p in problems] == ["models/001-a/approval.json"]
    matched = model_gate.require_approved(job, desc())
    assert matched["model"] == "models/002-a" and matched["evidence_problems"]


def test_r2_no_match_names_the_bad_file(saos_home):
    job = jobs.create_job("r2-none")
    _write_model(job, 1, desc(), legacy=True, corrupt=True)
    other = desc()
    other["surfaces"][1]["type"] = "Even Asphere"
    _write_model(job, 2, other, legacy=True, sequence=2)
    with pytest.raises(SaosError) as refused:
        model_gate.require_approved(job, desc())
    assert refused.value.code == ErrorCode.EVIDENCE_TAMPERED
    assert refused.value.details["problems"][0]["path"] == "models/001-a/approval.json"


def test_r2_modern_bad_json_is_reported(saos_home):
    job = jobs.create_job("r2-modern-bad")
    _write_model(job, 1, desc(), legacy=False, corrupt=True)
    problems = []
    assert model_gate.approvals(job, problems=problems) == []
    assert problems[0]["path"] == "models/001-a/approval.json"
    with pytest.raises(SaosError) as refused:
        model_gate.require_approved(job, desc())
    assert refused.value.code == ErrorCode.EVIDENCE_TAMPERED
    assert refused.value.details["problems"][0]["path"] == "models/001-a/approval.json"


def test_r2_corrupt_earliest_approval_keeps_round_and_opt_working(saos_home, fake_lens_file):
    """最早确认损坏、最新确认完整：round、opt 和窗口仍使用最新完整确认，同时保留坏路径提示。"""
    backend = FakeBackend()
    daemon = Daemon(backend, backend.start(), "extension")
    job = jobs.create_job("r2-integration")
    call = lambda op, **args: daemon.handle({"op": op, "args": args})
    call("lens.import", job=job.id, src=str(fake_lens_file), name="a.zmx")
    call("lens.load", job=job.id, file="a.zmx")
    doc = demand_doc()
    doc["objectives"][0]["must"] = {"max": 50.0}
    doc["structure"] = {"mode": "prescribed", "reason": "测试样品的结构由用户指定"}
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    demand.confirm(job.root, "确认需求")
    notes = {"surfaces": {"0": "object", "1": "front", "2": "back", "3": "image"},
             "assumptions": ["test"], "first_order": ["f = 50 mm"]}
    first = call("lens.describe", job=job.id, notes=notes)["result"]["folder"]
    assert call("lens.approve", job=job.id, model=first, quote="确认模型")["ok"]
    second = call("lens.describe", job=job.id, notes=notes)["result"]["folder"]
    assert call("lens.approve", job=job.id, model=second, quote="再次确认模型")["ok"]

    bad = job.root / first / "approval.json"
    bad.write_text("{not json", encoding="utf-8")
    snap = stage.snapshot(job)
    assert snap["current"] == "design"
    assert snap["model_evidence"] == [{"path": "models/001-a/approval.json",
                                       "message": "Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
                                       "code": ErrorCode.EVIDENCE_TAMPERED}]

    recorded = call("round.record", job=job.id)
    assert recorded["ok"] and recorded["result"]["record"]["model"] == second
    daemon.backend.optimize = lambda cycles, cores, **kw: {"merit": 1.0}
    assert call("opt.local", job=job.id)["ok"]


def test_r3_mixed_times_sort_and_unknown_stays_first(saos_home):
    job = jobs.create_job("r3-order")
    _write_model(job, 1, desc(), time="not-a-time", sequence=1)
    _write_model(job, 2, desc(), time="2026-10-21T00:00:02Z", sequence=2)
    _write_model(job, 3, desc(), time="2026-10-21T08:00:01.000000+08:00", sequence=3)
    _write_model(job, 4, desc(), time="2026-10-21T00:00:01+00:00", sequence=4)
    records = model_gate.approvals(job)
    assert [r["model"] for r in records] == ["models/001-a", "models/003-a", "models/004-a", "models/002-a"]
    assert [r["approval_sequence"] for r in records[1:3]] == [3, 4]


def test_r3_milestones_ignore_unparseable_times(saos_home):
    job = jobs.create_job("r3-milestones")
    doc = demand_doc()
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    loaded = demand.load_job(job.root)
    (job.root / "confirmations.jsonl").write_text(
        json.dumps({"demand_sha": demand.compute_digest(loaded), "time": "2026-10-21T00:00:00Z"}) + "\n",
        encoding="utf-8")
    _write_model(job, 1, desc(), time="bad", sequence=1)
    _write_model(job, 2, desc(), time="2026-10-21T00:01:00+00:00", sequence=2)
    _write_model(job, 3, desc(), time="2026-10-21T00:02:00.500000+00:00", sequence=3)
    result = milestones.elapsed(job)
    assert result["to_first_model_approval_s"] == 60.0
    assert result["to_report_acceptance_s"] is None


def _library(root: Path, entry_id="x", *, prescription_data=None, **entry_overrides):
    folder = root / "entries" / entry_id
    folder.mkdir(parents=True, exist_ok=True)
    entry = {"schema": 1, "id": entry_id, "form": "测试形式", "version": "1", "status": "sourced_unverified",
             "source": {"book": "测试来源", "page": 1, "url": "https://example.com"},
             "metrics": {"focal_length_mm": 50, "elements": 1, "half_field_deg": 10, "f_number": 5},
             "applicability": {"half_field_deg_max": 10, "f_number_min": 5,
                               "wavelength_um": [0.486, 0.656], "conjugate": "infinite"},
             "tightest": "half_field_deg", "pressure": {"half_field_deg": 1.0, "f_number": 1.0, "wavelengths_um": 1.0}}
    entry.update(entry_overrides)
    (folder / "entry.json").write_text(json.dumps(entry), encoding="utf-8")
    data = prescription_data or {"title": "test", "aperture": {"type": "image_f_number", "value": 5},
                                 "object": {"thickness": "inf"}, "fields": {"type": "angle", "y": [0, 5]},
                                 "wavelengths": {"um": [0.486, 0.656], "primary": 1},
                                 "surfaces": [{"radius": 20, "thickness": 3, "material": "N-BK7"},
                                              {"radius": -20, "thickness": 3}]}
    (folder / "prescription.json").write_text(json.dumps(data), encoding="utf-8")
    return root


def _generate_demand(job):
    doc = {"schema": 2, "constants": [
        {"id": "f", "description": "efl", "value": 50, "unit": "mm", "source": "test"},
        {"id": "h", "description": "field", "value": 5, "unit": "deg", "source": "test"},
        {"id": "n", "description": "fno", "value": 5, "source": "test"},
        {"id": "w", "description": "waves", "value": [0.486, 0.656], "unit": "um", "source": "test"}],
        "variables": [], "objectives": [{"id": "length", "description": "total track",
                                         "check": {"operand": {"type": "TOTR"}}, "origin": "user",
                                         "must": {"max": 150.0}, "scale": 5.0, "source": "user"}],
        "summary": "测试"}
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    (job.root / "prepare.py").write_text("def run(w):\n    return None\n", encoding="utf-8")
    return {"efl_mm": "f", "half_field_deg": "h", "f_number": "n", "wavelengths_um": "w"}


class _Stub:
    def __init__(self, error=None):
        self.error = error
        self.started = self.closed = False

    def start(self):
        self.started = True

    def close(self):
        self.closed = True

    def build(self, data):
        if self.error is not None:
            raise self.error

    def operand(self, *args):
        return 50.0


def test_r6_library_errors_are_not_computation(saos_home, tmp_path):
    job = jobs.create_job("r6-library")
    bindings = _generate_demand(job)
    bad = {"aperture": {"type": "epd", "value": 5}, "fields": {"type": "angle", "y": [0]},
           "wavelengths": {"um": [0.5], "primary": 1}, "surfaces": [{"radius": 10}]}
    root = _library(tmp_path / "lib1", prescription_data=bad)
    out = starts.generate(job, bindings, short_seconds=1, prepare="prepare.py", root=root, backend_factory=_Stub)
    assert out["not_evaluated"][0]["verdict"] == "library"
    assert out["not_evaluated"][0]["phase"] == "library_normalize"

    unknown = {"aperture": {"type": "image_f_number", "value": 5}, "object": {"thickness": "inf"},
               "fields": {"type": "angle", "y": [0, 5]}, "wavelengths": {"um": [0.486, 0.656], "primary": 1},
               "surfaces": [{"type": "Unknown", "radius": 20, "thickness": 3, "params": {"par1": 1.0}},
                            {"radius": -20, "thickness": 3}]}
    root2 = _library(tmp_path / "lib2", prescription_data=unknown)
    out2 = starts.generate(job, bindings, short_seconds=1, prepare="prepare.py", root=root2, backend_factory=_Stub)
    assert out2["not_evaluated"][0]["verdict"] == "library"
    assert out2["not_evaluated"][0]["phase"] == "library_scale"


def test_r6_backend_errors_keep_their_classes(saos_home, tmp_path):
    job = jobs.create_job("r6-backend")
    bindings = _generate_demand(job)
    root = _library(tmp_path / "lib")
    cases = [
        (SaosError(ErrorCode.BAD_ARGS, "bad backend option"), "configuration"),
        (MaterialError(ErrorCode.BAD_ARGS, "missing glass"), "material"),
        (RuntimeError("trace failed"), "computation"),
    ]
    for error, expected in cases:
        out = starts.generate(job, bindings, short_seconds=1, prepare="prepare.py", root=root,
                              backend_factory=lambda error=error: _Stub(error))
        assert out["not_evaluated"][0]["verdict"] == expected


def test_r6_library_metadata_error_precedes_artifacts(saos_home, tmp_path):
    job = jobs.create_job("r6-metadata")
    bindings = _generate_demand(job)
    root = _library(tmp_path / "lib")
    entry_file = root / "entries" / "x" / "entry.json"
    entry = json.loads(entry_file.read_text(encoding="utf-8"))
    entry.pop("version")
    entry_file.write_text(json.dumps(entry), encoding="utf-8")
    with pytest.raises(SaosError) as refused:
        starts.generate(job, bindings, short_seconds=1, prepare="prepare.py", root=root, backend_factory=_Stub)
    assert refused.value.code == ErrorCode.BAD_ARGS
    assert refused.value.details["kind"] == "library"
    assert not (job.root / "explore" / "starts").exists()


def test_r9_zero_candidates_are_archived_and_numbered(saos_home, tmp_path):
    job = jobs.create_job("r9-zero")
    bindings = _generate_demand(job)
    root = _library(tmp_path / "lib")
    entry_file = root / "entries" / "x" / "entry.json"
    entry = json.loads(entry_file.read_text(encoding="utf-8"))
    entry["applicability"]["half_field_deg_max"] = 1
    entry_file.write_text(json.dumps(entry), encoding="utf-8")

    class Forbidden(_Stub):
        def start(self):
            raise AssertionError("零候选不应启动后端")

        def close(self):
            raise AssertionError("零候选不应关闭后端")

    first = starts.generate(job, bindings, short_seconds=1, prepare="prepare.py", root=root, backend_factory=Forbidden)
    second = starts.generate(job, bindings, short_seconds=1, prepare="prepare.py", root=root, backend_factory=Forbidden)
    assert first["count"] == second["count"] == 0
    assert first["excluded"] and second["excluded"]
    assert Path(first["folder"]).name == "001" and Path(second["folder"]).name == "002"
    for out in (first, second):
        folder = job.root / out["folder"]
        assert sorted(p.name for p in folder.iterdir()) == ["generated.json"]
        assert stages.sealed(job.root, f"{out['folder']}/generated.json")


def test_r11_prepare_is_a_required_keyword(saos_home):
    assert inspect.signature(starts.generate).parameters["prepare"].default is inspect.Parameter.empty
    job = jobs.create_job("r11-signature")
    with pytest.raises(TypeError):
        starts.generate(job, {}, short_seconds=1)
    with pytest.raises(SaosError) as empty:
        starts.generate(job, {}, short_seconds=1, prepare="")
    assert empty.value.code == ErrorCode.BAD_ARGS
    with pytest.raises(SaosError) as missing:
        starts.generate(job, {}, short_seconds=1, prepare="missing.py")
    assert missing.value.code == ErrorCode.FILE_NOT_FOUND
    assert not (job.root / "explore" / "starts").exists()
