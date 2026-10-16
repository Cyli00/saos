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
    proc = client._spawn("standalone", "console-test")
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
    client._spawn('standalone', 'relative-home-test')
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
            json.dumps({"pid": pid, "token": "secret", "mode": "extension", "started_at": "t"}), encoding="utf-8")
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
