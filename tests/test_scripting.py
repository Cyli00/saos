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


def test_analysis_export_is_archived(daemon, loaded):
    out = daemon.handle({"op": "lens.analysis", "args": {"job": loaded.id, "type": "FakeCurve", "settings": {"Field": "all"}}})["result"]
    assert out["file"] == "analyses/001-FakeCurve.json" and out["text"] == "analyses/001-FakeCurve.txt"
    assert out["series"][0]["y"]["v"][-1] == 100.0 and out["settings"] == {"Field": "all"}
    saved = json.loads((loaded.root / out["file"]).read_text(encoding="utf-8"))
    assert saved["lens"]["file"] == "a.zmx" and saved["type"] == "FakeCurve"
    again = daemon.handle({"op": "lens.analysis", "args": {"job": loaded.id, "type": "FakeCurve"}})["result"]
    assert again["file"] == "analyses/002-FakeCurve.json"
    assert daemon.handle({"op": "lens.analysis", "args": {"job": loaded.id, "type": "Nope"}})["error"]["code"] == ErrorCode.BAD_ARGS
    assert daemon.handle({"op": "lens.analysis", "args": {"job": loaded.id, "list": True}})["result"] == {"types": ["FakeCurve"]}


def test_archives_keep_incrementing_past_three_digits(daemon, loaded, monkeypatch):
    folder = loaded.root / "scripts"
    folder.mkdir()
    (folder / "999-probe.py").write_text("旧存档", encoding="utf-8")
    first = run(daemon, loaded, "def run(ctx):\n    return 1000\n")["result"]
    second = run(daemon, loaded, "def run(ctx):\n    return 1001\n")["result"]
    assert first["script"] == "scripts/1000-probe.py" and second["script"] == "scripts/1001-probe.py"
    assert json.loads((loaded.root / first["script"]).with_suffix(".json").read_text())["result"] == 1000
    analyses = loaded.root / "analyses"
    analyses.mkdir()
    (analyses / "1000-FakeCurve.txt").write_text("保留失败请求的证据", encoding="utf-8")
    result = daemon.handle({"op": "lens.analysis", "args": {"job": loaded.id, "type": "FakeCurve"}})["result"]
    assert result["file"] == "analyses/1001-FakeCurve.json"
    screens = loaded.root / "screens"
    screens.mkdir()
    (screens / "1000-window-1.png").write_bytes(b"old")

    def screenshot(folder, prefix):
        name = f"{prefix}-1.png"
        (folder / name).write_bytes(b"new")
        return [name]

    monkeypatch.setattr(daemon.backend, "screenshots", screenshot)
    for number in (1001, 1002):
        result = daemon.handle({"op": "lens.screenshot", "args": {"job": loaded.id}})["result"]
        assert result["files"] == [f"screens/{number}-window-1.png"]
    assert (screens / "1000-window-1.png").read_bytes() == b"old"
