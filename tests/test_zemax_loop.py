"""第 3 步最小闭环的回归测试（优化基准），在本机真实 OpticStudio 2024 R1 上运行。

默认跳过。运行方法：SAOS_RUN_ZEMAX=1 uv run pytest -m zemax -s

US 4521083 实施例 1：提取结果 → 适配 → 搭建 → 复现校验 → 验收（应通过）
→ 受控扰动（seed 1）→ 第 0 轮（应未通过）→ 第 1 轮优化 → 验收通过。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.zemax,
    pytest.mark.skipif(os.environ.get("SAOS_RUN_ZEMAX") != "1", reason="需要真实 OpticStudio，设置 SAOS_RUN_ZEMAX=1"),
]

FIXTURES = Path(__file__).parent / "fixtures" / "us4521083"


def saos(*args: str, env: dict[str, str]) -> dict:
    started = time.perf_counter()
    proc = subprocess.run([sys.executable, "-m", "saos", *map(str, args)], capture_output=True, env=env, timeout=900)
    out = json.loads(proc.stdout.decode("utf-8"))
    print(f"[{time.perf_counter() - started:6.2f}s] saos {' '.join(map(str, args[:3]))} -> {'ok' if out['ok'] else out['error']['code']}")
    assert out["ok"], out
    return out["result"]


def test_minimal_loop(saos_home):
    env = {**os.environ, "SAOS_HOME": str(saos_home), "SAOS_BACKEND": "zos"}
    saos("daemon", "start", env=env)
    try:
        job = saos("job", "create", "--name", "loop", env=env)["job"]
        root = saos_home / "jobs" / job
        for name in ("extract.json", "merit_wizard.json", "merit_extra.json"):
            shutil.copy(FIXTURES / name, root / "sources" / name)
        shutil.copy(FIXTURES / "benchmark.yaml", root / "spec" / "benchmark.yaml")
        src = root / "sources"

        adapted = saos("adapt", "patent", src / "extract.json", "--focal-length", 10, "--tube-lens", 200,
                      "--field-number", 27, "--catalogs", "OHARA", "SCHOTT", "HOYA", "SUMITA", "--out", src, env=env)
        assert adapted["report"]["scale"] == pytest.approx(10.0)

        saos("lens", "build", "--job", job, src / "prescription.json", "--as", "repro.zmx", env=env)
        repro = saos("lens", "repro", "--job", job, src / "expect.json", env=env)
        assert repro["pass"] is True
        assert all(m["rel_error"] < 0.002 for m in repro["metrics"])

        accepted = saos("eval", "accept", "--job", job, "--spec", "spec/benchmark.yaml", env=env)
        assert accepted["verdict"] == "pass" and accepted["spec_shortfall"] == 0

        saos("lens", "perturb", "--job", job, "--seed", 1, "--radius-rel", 0.01, "--thickness-rel", 0.03, env=env)
        r0 = saos("round", "record", "--job", job, "--spec", "spec/benchmark.yaml", "--note", "round 0", env=env)
        assert r0["record"]["verdict"] == "fail"
        assert r0["record"]["spec_shortfall"] == pytest.approx(12.873, abs=0.01)
        assert set(r0["record"]["items"]) == {"axial_rms_wfe", "osc", "field_rms_wfe"}

        saos("opt", "vary", "--job", job, "--param", "radius", "--surfaces", "1-16", env=env)
        saos("opt", "vary", "--job", job, "--param", "thickness", "--surfaces", "0", env=env)
        saos("opt", "merit", "--job", job, "clear", env=env)
        saos("opt", "merit", "--job", job, "wizard", "--file", src / "merit_wizard.json", env=env)
        saos("opt", "merit", "--job", job, "add", "--file", src / "merit_extra.json", env=env)
        saos("opt", "local", "--job", job, "--cycles", "auto", env=env)
        r1 = saos("round", "record", "--job", job, "--note", "round 1", env=env)
        assert r1["record"]["verdict"] == "pass"
        assert r1["decision"]["status"] == "pass"

        rounds = [json.loads(l) for l in (root / "rounds.jsonl").read_text(encoding="utf-8").splitlines()]
        assert [r["round"] for r in rounds] == [0, 1]
        assert json.loads((root / "job.json").read_text(encoding="utf-8"))["spec"] == "spec/benchmark.yaml"
    finally:
        saos("daemon", "stop", "--discard", env=env)
