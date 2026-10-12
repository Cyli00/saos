"""第 2 步的验收流程，在本机真实 OpticStudio 2024 R1 上运行。

默认跳过。运行方法：ZEA_RUN_ZEMAX=1 uv run pytest -m zemax -s

启动后台 → 连续执行命令 → 加载镜头副本 → 读取并修改参数 → 保存并重新加载核对 → 正常退出，
同时检查错误返回和任务隔离。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from zea.protocol import ErrorCode

pytestmark = [
    pytest.mark.zemax,
    pytest.mark.skipif(os.environ.get("ZEA_RUN_ZEMAX") != "1", reason="需要真实 OpticStudio，设置 ZEA_RUN_ZEMAX=1"),
]

SAMPLE = Path.home() / "Documents/Zemax/Samples/Sequential/Objectives/Double Gauss 28 degree field.zmx"


def zea(*args: str, env: dict[str, str]) -> tuple[int, dict, float]:
    started = time.perf_counter()
    proc = subprocess.run([sys.executable, "-m", "zea", *args], capture_output=True, env=env, timeout=300)
    elapsed = time.perf_counter() - started
    out = json.loads(proc.stdout.decode("utf-8"))
    print(f"[{elapsed:6.2f}s] zea {' '.join(args)} -> {'ok' if out['ok'] else out['error']['code']}")
    return proc.returncode, out, elapsed


def ok(call):
    code, out, _ = call
    assert code == 0 and out["ok"], out
    return out["result"]


def err(call):
    code, out, _ = call
    assert code == 1 and not out["ok"], out
    return out["error"]["code"]


def opticstudio_pids() -> set[int]:
    proc = subprocess.run(
        ["tasklist", "/FI", "IMAGENAME eq OpticStudio.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True
    )
    pids = set()
    for line in proc.stdout.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


def test_step2_acceptance(zea_home):
    assert SAMPLE.is_file(), f"找不到样例镜头 {SAMPLE}"
    env = {**os.environ, "ZEA_HOME": str(zea_home), "ZEA_BACKEND": "zos"}
    sample_hash = hashlib.sha256(SAMPLE.read_bytes()).hexdigest()
    before_pids = opticstudio_pids()

    started = ok(zea("daemon", "start", env=env))
    assert started["opticstudio"]["version"] == "24.1.0"
    try:
        job_a = ok(zea("job", "create", "--name", "e2e-a", env=env))["job"]
        job_b = ok(zea("job", "create", "--name", "e2e-b", env=env))["job"]

        # 加载镜头副本：导入到任务目录，原样例文件不变
        ok(zea("lens", "import", "--job", job_a, str(SAMPLE), "--as", "start.zmx", env=env))
        ok(zea("lens", "load", "--job", job_a, "start.zmx", env=env))

        # 连续读取
        info = ok(zea("lens", "info", "--job", job_a, env=env))
        assert info["system"]["num_surfaces"] == 13
        surfaces = ok(zea("lens", "surfaces", "--job", job_a, env=env))["surfaces"]
        r1 = surfaces[1]["radius"]
        effl_before = ok(zea("lens", "operand", "--job", job_a, "EFFL", "--int2", "2", env=env))["value"]

        # 修改参数：半径、玻璃
        new_r1 = round(r1 * 1.02, 6)
        changed = ok(zea("lens", "set", "--job", job_a, "--surface", "1", "--param", "radius", "--value", str(new_r1), env=env))
        assert changed["after"] == pytest.approx(new_r1, rel=1e-12) and changed["dirty"] is True
        ok(zea("lens", "set", "--job", job_a, "--surface", "3", "--param", "material", "--value", "N-SK16", env=env))
        effl_after = ok(zea("lens", "operand", "--job", job_a, "EFFL", "--int2", "2", env=env))["value"]
        assert effl_after != pytest.approx(effl_before, rel=1e-9)

        # 明确的错误返回
        assert err(zea("lens", "set", "--job", job_a, "--surface", "3", "--param", "material", "--value", "NOTAGLASS", env=env)) == ErrorCode.BAD_ARGS
        assert err(zea("lens", "set", "--job", job_a, "--surface", "99", "--param", "radius", "--value", "1", env=env)) == ErrorCode.BAD_ARGS
        assert err(zea("lens", "operand", "--job", job_a, "NOSUCHOP", env=env)) == ErrorCode.BAD_ARGS
        assert err(zea("lens", "load", "--job", job_a, "missing.zmx", env=env)) == ErrorCode.FILE_NOT_FOUND

        # 任务隔离
        assert err(zea("lens", "surfaces", "--job", job_b, env=env)) == ErrorCode.JOB_MISMATCH
        assert err(zea("lens", "save", "--job", job_a, "--as", f"../../{job_b}/lenses/x.zmx", env=env)) == ErrorCode.PATH_OUTSIDE_JOB
        ok(zea("lens", "import", "--job", job_b, str(SAMPLE), "--as", "b.zmx", env=env))
        assert err(zea("lens", "load", "--job", job_b, "b.zmx", env=env)) == ErrorCode.UNSAVED_CHANGES
        assert err(zea("daemon", "stop", env=env)) == ErrorCode.UNSAVED_CHANGES

        # 保存并重新加载核对
        saved = ok(zea("lens", "save", "--job", job_a, "--as", "r001.zmx", env=env))
        assert saved["verified"] is True and saved["surfaces_checked"] == 13
        ok(zea("lens", "load", "--job", job_b, "b.zmx", env=env))
        ok(zea("lens", "load", "--job", job_a, "r001.zmx", env=env))
        reloaded = ok(zea("lens", "surfaces", "--job", job_a, env=env))["surfaces"]
        assert reloaded[1]["radius"] == pytest.approx(new_r1, rel=1e-12)
        assert reloaded[3]["material"] == "N-SK16"
        assert ok(zea("lens", "operand", "--job", job_a, "EFFL", "--int2", "2", env=env))["value"] == pytest.approx(effl_after, rel=1e-12)

        # 文件互不混用
        lenses_a = sorted(p.name for p in (zea_home / "jobs" / job_a / "lenses").glob("*.zmx"))
        lenses_b = sorted(p.name for p in (zea_home / "jobs" / job_b / "lenses").glob("*.zmx"))
        assert lenses_a == ["r001.zmx", "start.zmx"] and lenses_b == ["b.zmx"]
        start_copy = (zea_home / "jobs" / job_a / "lenses" / "start.zmx").read_bytes()
        assert hashlib.sha256(start_copy).hexdigest() == sample_hash
        assert hashlib.sha256(SAMPLE.read_bytes()).hexdigest() == sample_hash

        log_ops = [json.loads(l)["op"] for l in (zea_home / "jobs" / job_a / "log.jsonl").read_text(encoding="utf-8").splitlines()]
        assert log_ops[0] == "job.create" and "lens.save" in log_ops
    finally:
        stopped = ok(zea("daemon", "stop", "--discard", env=env))

    assert stopped["forced"] is False and len(stopped["opticstudio_pids"]) == 1
    assert ok(zea("daemon", "status", env=env)) == {"running": False}
    # stop 返回时，后台进程启动的 OpticStudio 必须已经退出
    assert not (opticstudio_pids() - before_pids), "后台进程退出后仍有 OpticStudio 进程残留"
