"""选型交接中的篡改、并发与失效探针的仓库回归。"""

from __future__ import annotations

import json

import pytest

from saos import model_gate, parallel
from saos.protocol import ErrorCode, SaosError
from test_candidates import DEMAND, LOOSE, NOTES, call, cand, plan, write_demand
from test_candidates import explore as explore


def compared(explore, **extra):
    daemon, job = explore
    write_demand(job, {**DEMAND, "objectives": [LOOSE]})
    spec = plan([]) | {"candidates": [cand("a", "explore/a.zmx"), cand("c", "explore/c.zmx"), cand("d", "explore/d.zmx")], **extra}
    out = call(daemon, "scan.run", job=job.id, plan=spec)
    assert out["ok"], out
    return daemon, job, job.root / out["result"]["folder"]










@pytest.mark.parametrize("attack", ["structure", "candidate", "delete"])
def test_tampering_before_final_seal_is_rejected(explore, monkeypatch, attack):
    dispatch = parallel.dispatch

    def tamper(*args, **kwargs):
        result = dispatch(*args, **kwargs)
        job = args[1]
        folder = job.root / "scans" / "001" / "candidates" / "a"
        path = folder / ("structure.json" if attack == "structure" else "a.zmx")
        if attack == "delete":
            path.unlink()
        else:
            path.write_text("{}", encoding="utf-8")
        return result

    monkeypatch.setattr(parallel, "dispatch", tamper)
    daemon, job = explore
    write_demand(job)
    out = call(daemon, "scan.run", job=job.id, plan=plan([]))
    assert out["error"]["code"] == ErrorCode.EVIDENCE_TAMPERED
    assert not (job.root / "scans/001/result.json").exists()










@pytest.mark.parametrize("remove_all", [False, True])
def test_changed_model_cannot_be_approved_as_a_legacy_model(explore, fake_lens_file, remove_all):
    daemon, job = explore
    assert call(daemon, "lens.import", job=job.id, src=str(fake_lens_file), name="a.zmx")["ok"]
    assert call(daemon, "lens.load", job=job.id, file="a.zmx")["ok"]
    result = call(daemon, "lens.describe", job=job.id, notes=NOTES)["result"]
    path = job.root / result["folder"] / "model.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("shown_sha")
    if remove_all:
        for key in ("shown", "unknowns", "approval_schema", "evidence_context"):
            data.pop(key, None)
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(SaosError) as refused:
        model_gate.approve(job, result["folder"], "确认")
    assert refused.value.code == ErrorCode.EVIDENCE_TAMPERED




def _seal_process(root, worker):
    from pathlib import Path
    from saos import jobs
    job = jobs.Job("parallel-seals", Path(root))
    for i in range(8):
        path = job.root / f"worker-{worker}-{i}.json"
        path.write_text(str(i), encoding="utf-8")
        job.seal(path)


def test_seals_from_multiple_processes_are_all_retained(tmp_path):
    from concurrent.futures import ProcessPoolExecutor
    import multiprocessing
    from saos import jobs
    with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
        futures = [pool.submit(_seal_process, str(tmp_path), worker) for worker in range(4)]
        for future in futures:
            future.result(timeout=60)
    rows = [json.loads(line) for line in (tmp_path / "seals.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 32
    assert jobs.Job("parallel-seals", tmp_path).verify_seals() == []


def test_method_changes_are_notes_and_structure_changes_require_rebuild_reason(explore, fake_lens_file):
    daemon, job = explore
    write_demand(job)
    assert call(daemon, "lens.import", job=job.id, src=str(fake_lens_file), name="a.zmx")["ok"]
    assert call(daemon, "lens.load", job=job.id, file="a.zmx")["ok"]
    folder = call(daemon, "lens.describe", job=job.id, notes=NOTES)["result"]["folder"]
    model_gate.approve(job, folder, "自动化测试夹具：确认模型")
    (job.root / "checks").mkdir(exist_ok=True)
    (job.root / "checks" / "changed.py").write_text("VALUE = 2\n", encoding="utf-8")
    model_gate.require_approved(job, daemon.backend.describe_model(None))
    assert model_gate.evidence_notes(job, daemon.backend.describe_model(None), "changed")
    daemon.backend.lens["surfaces"][1]["type"] = "Even Asphere"
    folder = call(daemon, "lens.describe", job=job.id, notes=NOTES)["result"]["folder"]
    with pytest.raises(SaosError, match="rebuild-reason"):
        model_gate.approve(job, folder, "自动化测试夹具：确认重建")
    approved = model_gate.approve(job, folder, "自动化测试夹具：确认重建", "原结构像差余量不足")
    assert approved["rebuild_reason"] == "原结构像差余量不足"
