from __future__ import annotations

import json

import pytest

from saos import jobs
from saos.backend.fake import FakeBackend
from saos.daemon import Daemon
from saos.protocol import ErrorCode


@pytest.fixture
def daemon(saos_home):
    backend = FakeBackend()
    return Daemon(backend, backend.start(), "standalone")


@pytest.fixture
def loaded(daemon, fake_lens_file):
    job = jobs.create_job("script")
    daemon.op_import({"job": job.id, "src": str(fake_lens_file), "name": "a.zmx"})
    daemon.op_load({"job": job.id, "file": "a.zmx"})
    return job


def run(daemon, job, source, name="probe", **args):
    return daemon.handle({"op": "script.exec", "args": {"job": job.id, "name": name, "source": source, "args": args}})


def test_script_result_and_archive(daemon, loaded):
    source = "def run(ctx):\n    return {'n': len(ctx.backend.surfaces()), 'lens': ctx.lens.name, 'x': ctx.args['x']}\n"
    out = run(daemon, loaded, source, x=3)
    assert out["ok"] and out["result"] == {"script": "scripts/001-probe.py", "result": {"n": 4, "lens": "a.zmx", "x": 3}}
    assert (loaded.root / "scripts" / "001-probe.py").read_text(encoding="utf-8") == source
    record = json.loads((loaded.root / "scripts" / "001-probe.json").read_text(encoding="utf-8"))
    assert record["ok"] and record["result"]["x"] == 3 and record["args"] == {"x": 3}
    assert run(daemon, loaded, source, x=4)["result"]["script"] == "scripts/002-probe.py"
    # 执行记录里不重复存脚本全文
    log = [json.loads(line) for line in loaded.log_file.read_text(encoding="utf-8").splitlines()]
    assert log[-1]["op"] == "script.exec" and log[-1]["args"]["source"].endswith("chars>")


def test_script_errors_keep_daemon_alive(daemon, loaded):
    out = run(daemon, loaded, "def run(ctx):\n    raise ValueError('boom')\n", name="bad")
    assert out["error"]["code"] == ErrorCode.SCRIPT_ERROR and "boom" in out["error"]["details"]["traceback"]
    assert not daemon.stopping
    record = json.loads((loaded.root / "scripts" / "001-bad.json").read_text(encoding="utf-8"))
    assert record["ok"] is False and record["error"]["exception"] == "ValueError"
    assert run(daemon, loaded, "x = 1\n")["error"]["code"] == ErrorCode.BAD_ARGS
    assert run(daemon, loaded, "def run(ctx):\n    return object()\n")["error"]["code"] == ErrorCode.BAD_ARGS
    assert run(daemon, loaded, "def run(ctx):\n    return 1\n", name="bad name")["error"]["code"] == ErrorCode.BAD_ARGS
    assert daemon.handle({"op": "ping"})["ok"]


def test_script_cannot_act_for_another_job(daemon, loaded):
    other = jobs.create_job("other")
    assert run(daemon, other, "def run(ctx):\n    return 1\n")["error"]["code"] == ErrorCode.JOB_MISMATCH


def test_context_save_and_load_track_the_loaded_lens(daemon, loaded):
    source = (
        "def run(ctx):\n"
        "    ctx.backend.set_surface(1, 'thickness', 6.0)\n"
        "    ctx.mark_dirty()\n"
        "    dirty = ctx.dirty\n"
        "    rel = ctx.save_as('b.zmx')\n"
        "    return {'dirty_before': dirty, 'saved': rel, 'dirty_after': ctx.dirty}\n"
    )
    out = run(daemon, loaded, source)["result"]["result"]
    assert out == {"dirty_before": True, "saved": "b.zmx", "dirty_after": False}
    assert daemon.loaded.path.name == "b.zmx" and loaded.read_state()["last_saved"] == "b.zmx"
    assert run(daemon, loaded, "def run(ctx):\n    return ctx.save_as('a.zmx')\n")["error"]["code"] == ErrorCode.FILE_EXISTS
    assert run(daemon, loaded, "def run(ctx):\n    return ctx.load('a.zmx')\n")["result"]["result"] == "a.zmx"
    assert run(daemon, loaded, "def run(ctx):\n    return str(ctx.path('../x'))\n")["error"]["code"] == ErrorCode.PATH_OUTSIDE_JOB


def test_needs_save_marks_dirty_after_script(daemon, loaded, monkeypatch):
    monkeypatch.setattr(daemon.backend, "needs_save", lambda: True)
    run(daemon, loaded, "def run(ctx):\n    return None\n")
    assert daemon.loaded.dirty
