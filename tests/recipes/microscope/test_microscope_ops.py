from __future__ import annotations

import pytest

from saos import jobs
from saos.backend.fake import FakeBackend
from saos.daemon import Daemon
from saos.recipes.microscope import calls, ops



def test_calls_only_expose_known_ops():
    with pytest.raises(ValueError):
        calls.call("anything", "job")


def test_acceptance_measures_the_saved_file_after_capturing_the_window(saos_home, fake_lens_file, monkeypatch):
    from saos.scripting import ScriptContext

    backend = FakeBackend()
    daemon = Daemon(backend, backend.start(), "extension")
    daemon.zos = backend  # 窗口即已连接的 OpticStudio 会话 / the window is the connected OpticStudio session
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
