"""用真实虚拟环境子进程检查后台启动时的控制台与日志。"""

from __future__ import annotations

import json
import subprocess

import pytest

from saos import client, config


@pytest.mark.parametrize("deny_breakaway", [False, True])
def test_spawn_without_console_preserves_output(saos_home, monkeypatch, deny_breakaway):
    original_popen = subprocess.Popen
    attempts = 0
    probe = """
import ctypes
import json
import sys

kernel = ctypes.WinDLL("kernel32")
kernel.GetConsoleWindow.restype = ctypes.c_void_p
print(json.dumps({"console_window": kernel.GetConsoleWindow()}), flush=True)
print("stderr-ok", file=sys.stderr, flush=True)
"""

    def launch_probe(cmd, **kwargs):
        nonlocal attempts
        attempts += 1
        if deny_breakaway and attempts == 1:
            raise PermissionError("模拟宿主作业对象拒绝脱离")
        return original_popen([cmd[0], "-c", probe], **kwargs)

    monkeypatch.setattr(client.subprocess, "Popen", launch_probe)
    proc = client._spawn("console-test")
    try:
        assert proc.wait(timeout=15) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=15)

    lines = config.daemon_log_file().read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[0])["console_window"] is None
    assert lines[1] == "stderr-ok"
    assert attempts == (2 if deny_breakaway else 1)


def test_spawn_resolves_relative_home_before_changing_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('SAOS_HOME', 'relative-work')
    captured = {}

    def launch(cmd, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(client.subprocess, 'Popen', launch)
    client._spawn('relative-home-test')
    expected = str((tmp_path / 'relative-work').resolve())
    assert captured['cwd'] == expected
    assert captured['env']['SAOS_HOME'] == expected


def test_instance_daemons_reads_state_files_only(tmp_path, monkeypatch):
    import json
    import os

    from saos import client, config

    work = tmp_path / "work"
    for name, pid in (("a", os.getpid()), ("b", 0)):
        (work / name / "daemon").mkdir(parents=True)
        (work / name / "daemon" / "state.json").write_text(
            json.dumps({"pid": pid, "token": "secret", "started_at": "t"}), encoding="utf-8")
    (work / "c").mkdir()
    job = work / "a" / "jobs" / "j1"
    job.mkdir(parents=True)
    (job / "progress.json").write_text(json.dumps({"op": "script.exec", "detail": "opt", "status": "running",
                                                   "daemon_pid": os.getpid(), "started": "s"}), encoding="utf-8")
    done = work / "a" / "jobs" / "j2"
    done.mkdir()
    (done / "progress.json").write_text(json.dumps({"status": "done", "daemon_pid": os.getpid()}), encoding="utf-8")
    monkeypatch.setattr(config, "WORK_ROOT", work)
    monkeypatch.setenv("SAOS_HOME", str(work / "a"))

    found = client.instance_daemons()
    assert [d["instance"] for d in found] == ["a", "b"]
    a, b = found
    assert a["current"] and a["alive"] and not b["current"] and not b["alive"]
    assert "token" not in a
    assert [j["job"] for j in a["running_jobs"]] == ["j1"] and b["running_jobs"] == []


def _machine(tmp_path, monkeypatch):
    """work/ 下实例 a（活着，pid 为本进程）、b（进程已不在）；登记目录里 work/ 之外的一个活着、一个进程号被复用（状态文件已没了）。"""
    import os

    work = tmp_path / "work"
    registry = tmp_path / "registry"
    registry.mkdir()
    for name, pid in (("a", os.getpid()), ("b", 0)):
        (work / name / "daemon").mkdir(parents=True)
        (work / name / "daemon" / "state.json").write_text(json.dumps({"pid": pid}), encoding="utf-8")
    elsewhere = tmp_path / "pytest-home"
    (elsewhere / "daemon").mkdir(parents=True)
    # work/ 之外那个用父进程的 pid：同样活着，又与 work/a 的不同
    (elsewhere / "daemon" / "state.json").write_text(json.dumps({"pid": os.getppid()}), encoding="utf-8")
    (registry / f"{os.getppid()}.json").write_text(json.dumps({"pid": os.getppid(), "home": str(elsewhere)}), encoding="utf-8")
    (registry / "4.json").write_text(json.dumps({"pid": 4, "home": str(tmp_path / "gone")}), encoding="utf-8")
    # 与 work/a 同一 home 的登记不应重复列出
    (registry / "1.json").write_text(json.dumps({"pid": os.getpid(), "home": str(work / "a")}), encoding="utf-8")
    monkeypatch.setattr(config, "WORK_ROOT", work)
    monkeypatch.setenv("SAOS_HOME", str(work / "a"))
    monkeypatch.setenv("SAOS_DAEMON_REGISTRY", str(registry))
    return work, registry, elsewhere


def test_all_daemons_adds_registered_homes_outside_work(tmp_path, monkeypatch):
    work, registry, elsewhere = _machine(tmp_path, monkeypatch)
    found = client.all_daemons()
    # 登记按 pid 数值排：4 总在父进程之前，与父进程 pid 的位数、首位无关
    assert [(d["instance"], d["alive"]) for d in found] == [("a", True), ("b", False), (None, False), (None, True)]
    assert found[3]["home"] == str(elsewhere) and not found[3]["current"]
    # pid 4 是系统进程：仍然存在，但它的 home 没有指向它的状态文件，所以不算活着
    assert found[2]["pid"] == 4 and not found[2]["alive"]


def test_stop_all_stops_live_ones_and_clears_leftovers(tmp_path, monkeypatch):
    from saos.protocol import ErrorCode, SaosError

    work, registry, elsewhere = _machine(tmp_path, monkeypatch)
    calls = []

    def fake_stop(discard=False, force=False):
        home = config.saos_home()
        calls.append((home.name, discard, force))
        if home == elsewhere.resolve() and not force:
            raise SaosError(ErrorCode.UNSAVED_CHANGES, "unsaved")
        return {"stopped": True, "forced": force}

    monkeypatch.setattr(client, "stop", fake_stop)
    with pytest.raises(SaosError) as exc:
        client.stop_all()
    summary = exc.value.details
    assert exc.value.code == ErrorCode.UNSAVED_CHANGES
    assert [s["instance"] for s in summary["stopped"]] == ["a"]
    assert [f["home"] for f in summary["failed"]] == [str(elsewhere)]
    assert sorted(s["pid"] for s in summary["stale_removed"]) == [0, 4]
    assert not (work / "b" / "daemon" / "state.json").exists() and not (registry / "4.json").exists()
    # 每次调用都落在对应实例上，之后 SAOS_HOME 恢复
    assert calls == [("a", False, False), ("pytest-home", False, False)] and config.saos_home() == (work / "a").resolve()
    calls.clear()
    assert client.stop_all(force=True)["failed"] == []
    assert calls[-2:] == [("pytest-home", False, False), ("pytest-home", False, True)]
