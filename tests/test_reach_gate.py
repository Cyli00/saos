"""差距门禁：离需求太远的结构，自主任务不能自己批准；用户指定的结构和用户本人的确认不受限。
The reach gate: an autonomous job cannot approve a structure too far from the demand; a prescribed structure and the user's own approval pass."""

from __future__ import annotations

import copy
import itertools
import json
import math

import pytest
import yaml

from saos import demand, jobs, ledger, model_gate, report, workflow
from saos.backend.fake import FakeBackend
from saos.daemon import Daemon
from saos.protocol import ErrorCode, SaosError
from saos.ui import stage

BASE = {
    "schema": 2,
    "constants": [{"id": "lens", "description": "the sample singlet", "value": "a.zmx", "source": "user"}],
    "variables": [{"id": "radii", "description": "both radii are free"}],
    "objectives": [{"id": "length", "description": "total track", "check": {"operand": {"type": "TOTR"}}, "origin": "user",
                    "class": "quality", "must": {"max": 95.0}, "scale": 5.0, "source": "user"}],
    "summary": "总长。",
}
NOTES = {"surfaces": {"0": "object", "1": "front", "2": "back", "3": "image"}, "assumptions": ["test"], "first_order": ["f = 50 mm"],
         "exploration": {"fixed_by_user": {"parts": "the test lens", "quote": "就用这个镜头"}}}


def goal(**changes):
    data = copy.deepcopy(BASE)
    data["objectives"][0].update(changes)
    return data


@pytest.mark.parametrize("bound, value, multiple", [
    ({"max": 13.0}, 358.0, 358.0 / 13.0),
    ({"max": 13.0}, 13.0, 1.0),
    ({"min": 0.3}, 0.15, 2.0),
    ({"min": 60.0}, -1.0, math.inf),
    ({"equals": 50.0, "tol": 0.5}, 52.0, 4.0),
    ({"max": -1.0}, 1.0, 3.0),
])
def test_miss_multiple(bound, value, multiple):
    assert demand.miss_multiple(bound, value) == pytest.approx(multiple)


def test_reach_applies_a_class_to_each_must():
    data = goal()
    data["objectives"] += [
        {"id": "f_number", "description": "F", "check": {"operand": {"type": "WFNO"}}, "origin": "user", "class": "first_order", "must": {"max": 1.2}, "source": "user"},
        {"id": "spot", "description": "spot", "check": {"operand": {"type": "TOTR"}}, "origin": "user", "must": {"max": 13.0}, "source": "user"},
        {"id": "mtf", "description": "mtf", "check": {"operand": {"type": "TOTR"}}, "origin": "user", "class": "quality", "must": {"min": 0.3}, "source": "user"},
    ]
    out = demand.reach(data, {"length": 94.0, "f_number": 1.21, "spot": 358.0, "mtf": 0.165})
    # 一阶量差一点也拦；没分类的按像质算；MTF 差 1.8 倍不拦 / a first-order miss blocks however small; unclassified counts as quality
    assert out["blocking"] == ["f_number", "spot"] and out["unclassified"] == ["spot"] and out["measured"] == 4
    assert demand.reach(data, {"length": "inf"})["blocking"] == ["length"]
    assert demand.reach(data, {})["blocking"] == [] and demand.reach(data, {})["measured"] == 0


def test_class_is_validated_and_keeps_rounds_comparable():
    with pytest.raises(SaosError):
        demand.validate(goal(**{"class": "optical"}))
    plain = copy.deepcopy(BASE)
    del plain["objectives"][0]["class"]
    # 改类别要重新确认，但数值没变，以前的轮次仍可比较 / a new class is reconfirmed, while earlier rounds stay comparable
    assert demand.digest(plain) != demand.digest(BASE) and demand.compute_digest(plain) == demand.compute_digest(BASE)


@pytest.fixture
def lens_job(saos_home, fake_lens_file):
    backend = FakeBackend()
    daemon = Daemon(backend, backend.start())
    names = itertools.count()

    def make(data, mode="autonomous"):
        job = jobs.create_job(f"reach{next(names)}", mode=mode)
        for op, args in (("lens.import", {"src": str(fake_lens_file), "name": "a.zmx"}), ("lens.load", {"file": "a.zmx"})):
            assert daemon.handle({"op": op, "args": {"job": job.id, **args}})["ok"]
        (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        if mode == "autonomous":
            demand.confirm(job.root, "", via="auto", reason="题目给定")
        else:
            demand.confirm(job.root, "可以")
        out = daemon.handle({"op": "lens.describe", "args": {"job": job.id, "notes": NOTES}})
        assert out["ok"], out
        return job, out["result"]

    make.daemon = daemon
    return make


def test_autonomous_approval_is_refused_when_a_quality_must_is_far_off(lens_job):
    job, described = lens_job(goal(must={"max": 5.0}, scale=1.0))
    assert any("Too far" in w for w in described["warnings"])
    with pytest.raises(SaosError) as refused:
        model_gate.approve(job, described["folder"], "", via="auto", reason="先做着看")
    assert [b["id"] for b in refused.value.details["blocking"]] == ["length"]
    assert model_gate.approvals(job) == []


def test_autonomous_approval_is_refused_for_a_first_order_miss_or_a_missing_class(lens_job):
    job, described = lens_job(goal(must={"max": 90.0}, **{"class": "first_order"}))
    with pytest.raises(SaosError, match="too far"):
        model_gate.approve(job, described["folder"], "", via="auto", reason="理由")
    data = copy.deepcopy(BASE)
    del data["objectives"][0]["class"]
    job, described = lens_job(data)
    with pytest.raises(SaosError, match="class"):
        model_gate.approve(job, described["folder"], "", via="auto", reason="理由")


def test_a_prescribed_structure_is_recorded_but_not_refused(lens_job):
    data = goal(must={"max": 5.0}, scale=1.0)
    data["structure"] = {"mode": "prescribed", "reason": "用户指定"}
    job, described = lens_job(data)
    assert model_gate.approve(job, described["folder"], "", via="auto", reason="结构由用户指定")["via"] == "auto"


def test_the_user_may_overrule_the_gate_and_the_window_shows_it(lens_job):
    job, described = lens_job(goal(must={"max": 5.0}, scale=1.0), mode="interactive")
    shown = model_gate.approval_gate(job)["reach_blocking"]
    assert [b["id"] for b in shown] == ["length"] and shown[0]["multiple"] > 5
    record = model_gate.approve(job, described["folder"], "我知道差得远，先继续")
    assert any("Too far" in w for w in record["warnings_acknowledged"])


def _step(job, step_id):
    return {s["id"]: s["status"] for s in stage.snapshot(job)["steps"]}[step_id]


def test_a_job_no_structure_can_pass_may_still_stop_and_report(lens_job):
    job, _ = lens_job(goal(must={"max": 5.0}, scale=1.0))
    d = demand.load_job(job.root)
    sha = demand.compute_digest(d)
    with pytest.raises(SaosError, match="existing file"):
        ledger.record_stop(job.root, sha, "试过了，做不到", "", d, via="auto")
    with pytest.raises(SaosError, match="no recorded rounds"):
        report.write(job)
    (job.root / "notes" / "gaps.md").write_text("length x20", encoding="utf-8")
    stop = ledger.record_stop(job.root, sha, "见notes/gaps.md：所有结构都差 20 倍", "", d, via="auto")
    assert stop["round"] == ledger.BEFORE_ROUNDS and ledger.stopped_before_rounds(job.root, sha, d) == stop
    assert stage.snapshot(job)["current"] == "deliver" and _step(job, "deliver") == "active"
    assert lens_job.daemon._stage_of({"job": job.id}) == "modeling"  # 不为它启动 OpticStudio / no OpticStudio for it
    out = report.write(job)
    assert out["headline"] == "no_design" and out["deliver"] is None and not out["deliverable"]
    html = (job.root / "report" / "index.html").read_text(encoding="utf-8")
    assert "所有结构都差 20 倍" in html and "未经用户确认" in html
    assert not job.verify_seals() and (job.root / out["archived"]).is_file()
    assert _step(job, "deliver") == "done"


def test_the_no_design_report_shows_the_latest_screening(lens_job):
    job, _ = lens_job(goal())
    d = demand.load_job(job.root)
    (job.root / "scans" / "001").mkdir(parents=True)
    row = {"id": "doublet", "verdict": "valid", "stopped_by": "timeout", "shortfall": 3.5, "reach_blocking": [{"id": "spot", "multiple": 27.5}]}
    (job.root / "scans" / "001" / "result.json").write_text(json.dumps({"mode": "candidates", "rows": [row]}), encoding="utf-8")
    ledger.record_stop(job.root, demand.compute_digest(d), "scans/001 全部被拦", "", d, via="auto")
    report.write(job)
    html = (job.root / "report" / "index.html").read_text(encoding="utf-8")
    assert "doublet" in html and "spot ×27.5" in html and "OpticStudio" in html


def test_a_later_round_voids_a_stop_before_rounds(lens_job):
    job, _ = lens_job(goal())
    d = demand.load_job(job.root)
    sha = demand.compute_digest(d)
    (job.root / "notes" / "gaps.md").write_text("x", encoding="utf-8")
    stop = ledger.record_stop(job.root, sha, "notes/gaps.md", "", d, via="auto")
    row = {"round": 0, "shortfall": 1.0, "tier": "baseline", "demand_sha": sha, "method_sha": "m", "hard_failing": [], "measurement_errors": []}
    assert ledger.decide([row], d["stop"], stop)["status"] != "stopped"
    ledger.append(job.root / ledger.LEDGER_FILE, row)
    assert ledger.stopped_before_rounds(job.root, sha, d) is None


def test_a_stop_before_rounds_needs_a_confirmed_demand(saos_home):
    job = jobs.create_job("unconfirmed", mode="interactive")
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(BASE, allow_unicode=True), encoding="utf-8")
    d = demand.load_job(job.root)
    with pytest.raises(SaosError, match="confirm the demand"):
        ledger.record_stop(job.root, demand.compute_digest(d), "sources/demand.yaml", "停吧", d)


def test_optimising_is_allowed_before_the_first_approval_only(lens_job):
    job, described = lens_job(goal())
    backend = lens_job.daemon.backend
    workflow.require_optimise(job, backend)
    model_gate.approve(job, described["folder"], "", via="auto", reason="理由")
    workflow.require_optimise(job, backend)
    backend.lens["surfaces"][1]["material"] = ""  # 结构变了 / the structure changed
    with pytest.raises(SaosError) as refused:
        workflow.require_optimise(job, backend)
    assert refused.value.code == ErrorCode.MODEL_NOT_APPROVED


def test_the_brief_flags_a_far_must_once():
    data = demand.validate(goal(must={"max": 5.0}, scale=1.0))
    evaluation = {"items": [{"id": "length", "kind": "objective", "value": 100.0}], "hard_failing": []}
    decision = {"status": "continue", "rounds_without_gain": 0, "next_tier": "local"}
    row = {"values": {"length": 100.0}}
    first = workflow.brief(data, evaluation, decision, [row])
    assert first["reach_blocking"] == ["length"] and "too far" in first["review"]
    later = workflow.brief(data, evaluation, decision, [row, row])
    assert later["reach_blocking"] == ["length"] and later["review"] is None
    newly = workflow.brief(data, evaluation, decision, [{"values": {"length": 6.0}}, row])
    assert "too far" in newly["review"]
