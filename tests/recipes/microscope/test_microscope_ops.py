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
    job.spec_dir.mkdir(exist_ok=True)
    (job.spec_dir / "b.yaml").write_text(SPEC.format(wd=10), encoding="utf-8")
    daemon.op_import({"job": job.id, "src": str(fake_lens_file), "name": "a.zmx"})
    daemon.op_load({"job": job.id, "file": "a.zmx"})
    gs = iter([5.0, 5.0, 5.0, 5.0, 5.0, 5.0])
    monkeypatch.setattr(backend, "merit_value", lambda: 0.0, raising=False)
    monkeypatch.setattr(ops, "_accept", lambda ctx, name, spec: {"verdict": "fail", "spec_shortfall": next(gs), "items": []})
    for _ in range(4):
        resp = record(daemon, job, spec="spec/b.yaml", observe=True)
        assert resp["ok"], resp
        out = resp["result"]["result"]
    assert out["decision"]["status"] == "stagnated" and out["record"]["round"] == 3
    out = record(daemon, job, spec="spec/b.yaml", observe=True)["result"]["result"]  # 停机后仍可观察
    assert out["record"]["round"] == 4 and out["record"]["observe"] is True
    # 记录轮次后当前镜头仍是工作文件，归档不会被之后的保存改写 / the working file stays current after a round
    assert daemon.loaded.path.name == "a.zmx" and not daemon.loaded.dirty and (job.lenses_dir / "rounds" / "r004.zmx").is_file()
    assert record(daemon, job)["error"]["code"] == ErrorCode.BAD_ARGS  # 不加 observe 时照常拒绝
    (job.spec_dir / "b.yaml").write_text(SPEC.format(wd=12), encoding="utf-8")
    assert record(daemon, job, spec="spec/b.yaml", observe=True)["error"]["code"] == ErrorCode.SPEC_CHANGED  # observe 也不能绕过冻结


def test_calls_only_expose_known_ops():
    with pytest.raises(ValueError):
        calls.call("anything", "job")


def test_acceptance_measures_the_saved_file_after_capturing_the_window(saos_home, fake_lens_file, monkeypatch):
    from saos.scripting import ScriptContext

    backend = FakeBackend()
    daemon = Daemon(backend, backend.start(), "extension")
    job = jobs.create_job("capture")
    daemon.op_import({"job": job.id, "src": str(fake_lens_file), "name": "a.zmx"})
    daemon.op_load({"job": job.id, "file": "a.zmx"})
    backend.lens["surfaces"][1]["thickness"] = 9.0  # 用户在窗口里改了但没保存
    unsaved = [True]
    monkeypatch.setattr(backend, "has_unsaved_changes", lambda: unsaved[0])
    load = backend.load
    monkeypatch.setattr(backend, "load", lambda path: (load(path), unsaved.__setitem__(0, False)))
    ctx = ScriptContext(daemon, job, {})
    assert ops._saved_lens_in_window(ctx, ctx.lens) == {"captured": "captures/001-unsaved.zmx"}
    assert backend.surfaces()[1]["thickness"] != 9.0
    assert ops._saved_lens_in_window(ctx, ctx.lens) == {}
