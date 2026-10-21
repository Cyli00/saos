from __future__ import annotations

import json
from types import SimpleNamespace

from saos import demand, milestones, model_gate, stages


def test_elapsed_uses_recorded_milestones_and_keeps_missing_as_unknown(tmp_path, monkeypatch):
    doc = {"constants": [], "variables": [], "objectives": []}
    monkeypatch.setattr(demand, "load_job", lambda root: doc)
    monkeypatch.setattr(model_gate, "approvals", lambda job: [{"time": "2026-10-21T00:02:00+00:00"}])
    monkeypatch.setattr(stages, "read", lambda root: {"time": "2026-10-21T00:07:00+00:00"})
    (tmp_path / "confirmations.jsonl").write_text(json.dumps({"demand_sha": demand.digest(doc), "time": "2026-10-21T00:00:00+00:00"}), encoding="utf-8")
    job = SimpleNamespace(root=tmp_path)
    result = milestones.elapsed(job)
    assert result["to_first_model_approval_s"] == 120 and result["to_report_acceptance_s"] == 420
    monkeypatch.setattr(stages, "read", lambda root: None)
    assert milestones.elapsed(job)["to_report_acceptance_s"] is None
