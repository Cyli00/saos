"""启动真正的后台子进程（假后端），检查启动、重复启动、退出和未运行时的错误。"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from zea.protocol import ErrorCode


def zea(*args: str, env: dict[str, str]) -> tuple[int, dict]:
    proc = subprocess.run(
        [sys.executable, "-m", "zea", *args],
        capture_output=True,
        env=env,
        timeout=120,
    )
    return proc.returncode, json.loads(proc.stdout.decode("utf-8"))


@pytest.fixture
def env(zea_home):
    return {**os.environ, "ZEA_HOME": str(zea_home), "ZEA_BACKEND": "fake"}


def test_daemon_lifecycle(env, fake_lens_file):
    code, out = zea("lens", "info", "--job", "x", env=env)
    assert code == 1 and out["error"]["code"] == ErrorCode.DAEMON_NOT_RUNNING

    code, out = zea("daemon", "start", env=env)
    assert code == 0, out
    pid = out["result"]["pid"]
    try:
        code, out = zea("daemon", "start", env=env)
        assert code == 1 and out["error"]["code"] == ErrorCode.DAEMON_ALREADY_RUNNING

        code, out = zea("job", "create", "--name", "p", env=env)
        job = out["result"]["job"]
        assert zea("lens", "import", "--job", job, str(fake_lens_file), "--as", "a.zmx", env=env)[0] == 0
        assert zea("lens", "load", "--job", job, "a.zmx", env=env)[0] == 0
        code, out = zea("lens", "set", "--job", job, "--surface", "1", "--param", "radius", "--value", "42", env=env)
        assert code == 0 and out["result"]["after"] == 42.0

        code, out = zea("daemon", "stop", env=env)
        assert code == 1 and out["error"]["code"] == ErrorCode.UNSAVED_CHANGES

        code, out = zea("lens", "save", "--job", job, env=env)
        assert code == 0 and out["result"]["verified"] is True

        code, out = zea("daemon", "status", env=env)
        assert out["result"]["running"] is True and out["result"]["pid"] == pid
        assert out["result"]["loaded"] == {"job": job, "file": "a.zmx", "dirty": False}
    finally:
        code, out = zea("daemon", "stop", "--discard", "--force", env=env)
    assert code == 0 and out["result"]["stopped"] is True

    code, out = zea("daemon", "status", env=env)
    assert out["result"] == {"running": False}


def test_bad_cli_arguments_return_json(env):
    code, out = zea("lens", "set", "--job", "x", env=env)
    assert code == 2
    assert out["ok"] is False and out["error"]["code"] == ErrorCode.BAD_ARGS
