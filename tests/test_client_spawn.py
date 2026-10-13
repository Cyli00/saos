"""用真实虚拟环境子进程检查后台启动时的控制台与日志。"""

from __future__ import annotations

import json
import subprocess

import pytest

from zea import client, config


@pytest.mark.parametrize("deny_breakaway", [False, True])
def test_spawn_without_console_preserves_output(zea_home, monkeypatch, deny_breakaway):
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
