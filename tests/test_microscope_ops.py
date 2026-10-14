from __future__ import annotations

import pytest

from saos import jobs
from saos.backend.fake import FakeBackend
from saos.daemon import Daemon
from saos.protocol import ErrorCode
from saos.recipes.microscope import calls, ops

SPEC = """spec_version: 1
correction_class: achromat
magnification: 20
na: 0.4
limits:
  working_distance_min: {wd}
"""


def record(daemon, job, **kwargs):
    source = calls.SOURCE.format(fn="record_round")
    return daemon.handle({"op": "script.exec", "args": {"job": job.id, "name": "round", "source": source, "args": kwargs}})


def test_observe_rounds_keep_freeze_but_do_not_stop(saos_home, fake_lens_file, monkeypatch):
    backend = FakeBackend()
    daemon = Daemon(backend, backend.start(), "standalone")
    job = jobs.create_job("observe")
    (job.spec_dir / "b.yaml").write_text(SPEC.format(wd=10), encoding="utf-8")
    daemon.op_import({"job": job.id, "src": str(fake_lens_file), "name": "a.zmx"})
    daemon.op_load({"job": job.id, "file": "a.zmx"})
    gs = iter([5.0, 5.0, 5.0, 5.0, 5.0, 5.0])
    monkeypatch.setattr(backend, "merit_value", lambda: 0.0, raising=False)
    monkeypatch.setattr(ops, "_accept", lambda ctx, name, spec: {"verdict": "fail", "spec_shortfall": next(gs), "items": []})
    for _ in range(4):
        out = record(daemon, job, spec="spec/b.yaml", observe=True)["result"]["result"]
    assert out["decision"]["status"] == "stagnated" and out["record"]["round"] == 3
    out = record(daemon, job, spec="spec/b.yaml", observe=True)["result"]["result"]  # 停机后仍可观察
    assert out["record"]["round"] == 4 and out["record"]["observe"] is True
    assert daemon.loaded.path.name == "r004.zmx" and not daemon.loaded.dirty
    assert record(daemon, job)["error"]["code"] == ErrorCode.BAD_ARGS  # 不加 observe 时照常拒绝
    (job.spec_dir / "b.yaml").write_text(SPEC.format(wd=12), encoding="utf-8")
    assert record(daemon, job, spec="spec/b.yaml", observe=True)["error"]["code"] == ErrorCode.SPEC_CHANGED  # observe 也不能绕过冻结


def test_calls_only_expose_known_ops():
    with pytest.raises(ValueError):
        calls.call("anything", "job")
