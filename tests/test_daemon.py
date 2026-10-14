"""在本进程内直接调用 Daemon.handle，检查请求处理逻辑，不启动子进程。"""

from __future__ import annotations

import json

import pytest

from saos import jobs
from saos.backend.fake import FakeBackend
from saos.daemon import Daemon, compare_surfaces
from saos.protocol import ErrorCode


@pytest.fixture
def daemon(saos_home):
    backend = FakeBackend()
    info = backend.start()
    return Daemon(backend, info, "standalone")


def call(daemon: Daemon, op: str, **args):
    return daemon.handle({"op": op, "args": args})


def ok_result(response):
    assert response["ok"], response
    return response["result"]


def err_code(response):
    assert not response["ok"], response
    return response["error"]["code"]


@pytest.fixture
def loaded(daemon, fake_lens_file):
    job = jobs.create_job("a")
    ok_result(call(daemon, "lens.import", job=job.id, src=str(fake_lens_file), name="start.zmx"))
    ok_result(call(daemon, "lens.load", job=job.id, file="start.zmx"))
    return job


def test_import_copies_without_touching_source(daemon, fake_lens_file):
    job = jobs.create_job("a")
    original = fake_lens_file.read_bytes()
    result = ok_result(call(daemon, "lens.import", job=job.id, src=str(fake_lens_file)))
    assert result["file"] == fake_lens_file.name
    assert (job.lenses_dir / fake_lens_file.name).read_bytes() == original
    assert err_code(call(daemon, "lens.import", job=job.id, src=str(fake_lens_file))) == ErrorCode.FILE_EXISTS
    ok_result(call(daemon, "lens.import", job=job.id, src=str(fake_lens_file), overwrite=True))
    assert err_code(call(daemon, "lens.import", job=job.id, src="relative.zmx")) == ErrorCode.BAD_ARGS


def test_set_marks_dirty_and_save_verifies(daemon, loaded):
    result = ok_result(call(daemon, "lens.set", job=loaded.id, surface=1, param="radius", value=48.5))
    assert (result["before"], result["after"], result["dirty"]) == (50.0, 48.5, True)

    saved = ok_result(call(daemon, "lens.save", job=loaded.id, file="r001.zmx"))
    assert saved == {"job": loaded.id, "file": "r001.zmx", "verified": True, "surfaces_checked": 4}
    state = loaded.read_state()
    assert state["current_lens"] == "r001.zmx" and state["last_saved"] == "r001.zmx"
    # 原文件不受影响
    start = json.loads((loaded.lenses_dir / "start.zmx").read_text(encoding="utf-8"))
    assert start["surfaces"][1]["radius"] == 50.0

    info = ok_result(call(daemon, "lens.info", job=loaded.id))
    assert info["dirty"] is False and info["file"] == "r001.zmx"


def test_save_as_existing_requires_overwrite(daemon, loaded, fake_lens_file):
    ok_result(call(daemon, "lens.import", job=loaded.id, src=str(fake_lens_file), name="other.zmx"))
    assert err_code(call(daemon, "lens.save", job=loaded.id, file="other.zmx")) == ErrorCode.FILE_EXISTS
    ok_result(call(daemon, "lens.save", job=loaded.id, file="other.zmx", overwrite=True))
    ok_result(call(daemon, "lens.save", job=loaded.id))  # 覆盖当前文件不需要 overwrite
    assert err_code(call(daemon, "lens.import", job=loaded.id, src=str(fake_lens_file), name="other.zmx", overwrite=True)) == ErrorCode.BAD_ARGS


def test_set_errors(daemon, loaded):
    assert err_code(call(daemon, "lens.set", job=loaded.id, surface=2, param="thickness", value=1)) == ErrorCode.VALUE_NOT_APPLIED
    assert err_code(call(daemon, "lens.set", job=loaded.id, surface=1, param="material", value="NOTAGLASS")) == ErrorCode.BAD_ARGS
    assert err_code(call(daemon, "lens.set", job=loaded.id, surface=9, param="radius", value=1)) == ErrorCode.BAD_ARGS
    assert err_code(call(daemon, "lens.set", job=loaded.id, surface=1, param="radius", value="abc")) == ErrorCode.BAD_ARGS
    assert err_code(call(daemon, "lens.set", job=loaded.id, surface=1, param="tilt", value=1)) == ErrorCode.BAD_ARGS
    assert err_code(call(daemon, "lens.set", job=loaded.id, surface="1", param="radius", value=1)) == ErrorCode.BAD_ARGS
    # 失败的修改不把镜头标为已修改
    assert ok_result(call(daemon, "lens.info", job=loaded.id))["dirty"] is False


def test_jobs_cannot_touch_each_others_lens(daemon, loaded, fake_lens_file):
    other = jobs.create_job("b")
    for op, extra in [("lens.info", {}), ("lens.surfaces", {}), ("lens.save", {}), ("lens.operand", {"type": "TOTR"})]:
        assert err_code(call(daemon, op, job=other.id, **extra)) == ErrorCode.JOB_MISMATCH
    assert err_code(call(daemon, "lens.set", job=other.id, surface=1, param="radius", value=1)) == ErrorCode.JOB_MISMATCH
    assert err_code(call(daemon, "lens.load", job=other.id, file="start.zmx")) == ErrorCode.FILE_NOT_FOUND
    assert err_code(call(daemon, "lens.save", job=loaded.id, file=f"../../{other.id}/lenses/x.zmx")) == ErrorCode.PATH_OUTSIDE_JOB
    assert not list(other.lenses_dir.iterdir())


def test_unsaved_changes_block_switching(daemon, loaded, fake_lens_file):
    other = jobs.create_job("b")
    ok_result(call(daemon, "lens.import", job=other.id, src=str(fake_lens_file), name="b.zmx"))
    ok_result(call(daemon, "lens.set", job=loaded.id, surface=1, param="radius", value=40))
    assert err_code(call(daemon, "lens.load", job=other.id, file="b.zmx")) == ErrorCode.UNSAVED_CHANGES
    assert err_code(call(daemon, "shutdown")) == ErrorCode.UNSAVED_CHANGES
    assert daemon.stopping is False
    ok_result(call(daemon, "lens.load", job=other.id, file="b.zmx", discard=True))
    assert ok_result(call(daemon, "ping"))["loaded"] == {"job": other.id, "file": "b.zmx", "dirty": False}


def test_requires_loaded_lens(daemon):
    job = jobs.create_job("a")
    assert err_code(call(daemon, "lens.info", job=job.id)) == ErrorCode.NO_LENS_LOADED
    assert err_code(call(daemon, "lens.info", job="20260101-000000-none")) == ErrorCode.JOB_NOT_FOUND
    assert err_code(call(daemon, "lens.info")) == ErrorCode.BAD_ARGS
    assert err_code(call(daemon, "no.such.op")) == ErrorCode.UNKNOWN_OP


def test_unexpected_exception_keeps_daemon_alive(daemon, loaded):
    response = call(daemon, "lens.operand", job=loaded.id, type="CRASH")
    assert err_code(response) == ErrorCode.ZOS_ERROR
    assert response["error"]["details"]["exception"] == "RuntimeError"
    assert ok_result(call(daemon, "lens.operand", job=loaded.id, type="TOTR"))["value"] == 100.0


def test_connection_lost_stops_daemon(daemon, loaded):
    daemon.backend.started = False  # 模拟 OpticStudio 进程已退出
    response = call(daemon, "lens.surfaces", job=loaded.id)
    assert err_code(response) == ErrorCode.ZOS_CONNECTION_LOST
    assert response["error"]["details"]["lost_lens"]["job"] == loaded.id
    assert response["error"]["details"]["daemon_exiting"] is True
    assert daemon.stopping is True and daemon.loaded is None
    # ping 仍可回答，并如实报告连接状态
    assert ok_result(call(daemon, "ping"))["backend_healthy"] is False


def test_connection_lost_during_operation(daemon, loaded):
    original = daemon.backend.operand

    def die(*args):
        daemon.backend.started = False
        return original("CRASH", 0, 0, [])

    daemon.backend.operand = die
    response = call(daemon, "lens.operand", job=loaded.id, type="TOTR")
    assert err_code(response) == ErrorCode.ZOS_CONNECTION_LOST
    assert response["error"]["details"]["exception"] == "RuntimeError"
    assert daemon.stopping is True


def test_operations_are_logged_per_job(daemon, loaded):
    call(daemon, "lens.set", job=loaded.id, surface=1, param="radius", value=45)
    call(daemon, "lens.set", job=loaded.id, surface=2, param="thickness", value=1)
    records = [json.loads(line) for line in loaded.log_file.read_text(encoding="utf-8").splitlines()]
    assert [r["op"] for r in records] == ["job.create", "lens.import", "lens.load", "lens.set", "lens.set"]
    assert records[-2]["ok"] is True and "duration_ms" in records[-2]
    assert records[-1]["ok"] is False and records[-1]["error"]["code"] == ErrorCode.VALUE_NOT_APPLIED


def test_compare_surfaces_detects_changes():
    a = [{"index": 0, "type": "Standard", "comment": "", "material": "", "is_stop": False, "solves": {}, "radius": 1.0, "thickness": float("inf"), "conic": 0.0, "semi_diameter": 2.0}]
    b = [dict(a[0], radius=1.0 + 1e-15)]
    assert compare_surfaces(a, b) == []
    c = [dict(a[0], radius=1.001, material="N-BK7")]
    fields = {d["field"] for d in compare_surfaces(a, c)}
    assert fields == {"radius", "material"}
    assert compare_surfaces(a, a + a)[0]["field"] == "num_surfaces"


def test_unmanaged_window_dirty_state_blocks_replacement(daemon, fake_lens_file, monkeypatch):
    job = jobs.create_job('protect-window')
    daemon.backend.load(fake_lens_file)
    monkeypatch.setattr(daemon.backend, 'has_unsaved_changes', lambda: True)
    ok_result(call(daemon, 'lens.import', job=job.id, src=str(fake_lens_file), name='copy.zmx'))
    assert err_code(call(daemon, 'lens.load', job=job.id, file='copy.zmx')) == ErrorCode.UNSAVED_CHANGES
    original = fake_lens_file.read_bytes()
    capture = ok_result(call(daemon, 'lens.capture', job=job.id, file='window-copy.zmx'))
    assert capture['file'] == 'window-copy.zmx'
    assert fake_lens_file.read_bytes() == original
    assert err_code(call(daemon, 'lens.capture', job=job.id, file='window-copy.zmx')) == ErrorCode.FILE_EXISTS


def test_window_edits_are_told_apart_from_saos_side_effects(saos_home, fake_lens_file, monkeypatch):
    # extension 模式：NeedsSave 置位但镜头内容与上次请求结束时相同，是 saos 自己的动作；内容变了才是用户修改
    backend = FakeBackend()
    daemon = Daemon(backend, {**backend.start(), "initial_needs_save": False}, "extension")
    monkeypatch.setattr(backend, "has_unsaved_changes", lambda: True)
    job = jobs.create_job("window")
    ok_result(call(daemon, "lens.import", job=job.id, src=str(fake_lens_file), name="a.zmx"))
    ok_result(call(daemon, "lens.import", job=job.id, src=str(fake_lens_file), name="b.zmx"))
    ok_result(call(daemon, "lens.load", job=job.id, file="a.zmx", discard=True))
    ok_result(call(daemon, "lens.load", job=job.id, file="b.zmx"))
    backend.lens["surfaces"][1]["thickness"] = 9.0  # 用户在窗口里手改
    assert err_code(call(daemon, "lens.load", job=job.id, file="a.zmx")) == ErrorCode.UNSAVED_CHANGES
    ok_result(call(daemon, "lens.load", job=job.id, file="a.zmx", discard=True))


def test_unsaved_window_content_at_connect_is_protected(saos_home, fake_lens_file):
    backend = FakeBackend()
    daemon = Daemon(backend, {**backend.start(), "initial_needs_save": True}, "extension")
    job = jobs.create_job("window")
    ok_result(call(daemon, "lens.import", job=job.id, src=str(fake_lens_file), name="a.zmx"))
    assert err_code(call(daemon, "lens.load", job=job.id, file="a.zmx")) == ErrorCode.UNSAVED_CHANGES
    backend.load(fake_lens_file)
    ok_result(call(daemon, "lens.capture", job=job.id, file="window.zmx"))
    ok_result(call(daemon, "lens.load", job=job.id, file="a.zmx"))


def test_blocking_dialog_fails_fast(daemon, loaded, monkeypatch):
    monkeypatch.setattr(daemon.backend, "blocking_dialogs", lambda: ["Zemax Error Message"])
    out = call(daemon, "lens.info", job=loaded.id)
    assert err_code(out) == ErrorCode.UI_BLOCKED and out["error"]["details"]["dialogs"] == ["Zemax Error Message"]
    assert call(daemon, "ping")["ok"]
