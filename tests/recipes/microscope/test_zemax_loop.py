"""显微物镜配方最小闭环的回归测试，在本机真实 OpticStudio 2024 R1 上运行。

默认跳过。运行方法：SAOS_RUN_ZEMAX=1 uv run pytest tests/recipes/microscope/test_zemax_loop.py -s

US 4521083 实施例 1：提取结果 → 适配 → 搭建 → 复现校验 → 验收（应通过）
→ 受控扰动（seed 1）→ 验收（应未通过）→ 局部优化 → 验收通过。轮次记录走通用工作流（saos round），这里不涉及。
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
import yaml

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
        (root / "spec").mkdir()
        shutil.copy(FIXTURES / "benchmark.yaml", root / "spec" / "benchmark.yaml")
        src = root / "sources"

        adapted = saos("microscope", "adapt", src / "extract.json", "--focal-length", 10, "--tube-lens", 200,
                      "--field-number", 27, "--catalogs", "OHARA", "SCHOTT", "HOYA", "SUMITA", "--out", src, env=env)
        assert adapted["report"]["scale"] == pytest.approx(10.0)

        saos("lens", "build", "--job", job, src / "prescription.json", "--as", "repro.zmx", env=env)
        repro = saos("microscope", "repro", "--job", job, src / "expect.json", env=env)
        assert repro["pass"] is True
        assert all(m["rel_error"] < 0.002 for m in repro["metrics"])

        accepted = saos("microscope", "accept", "--job", job, "--spec", "spec/benchmark.yaml", env=env)
        assert accepted["verdict"] == "pass" and accepted["spec_shortfall"] == 0

        saos("microscope", "perturb", "--job", job, "--seed", 1, "--radius-rel", 0.01, "--thickness-rel", 0.03, env=env)
        saos("lens", "save", "--job", job, "--as", "perturbed.zmx", env=env)
        before = saos("microscope", "accept", "--job", job, "--spec", "spec/benchmark.yaml", env=env)
        assert before["verdict"] == "fail"
        assert before["spec_shortfall"] == pytest.approx(12.873, abs=0.01)

        # 优化前要有已确认的需求单；验收口径仍是配方的规格 / optimisation needs a confirmed demand; acceptance stays with the spec
        (src / "demand.yaml").write_text(yaml.safe_dump({
            "schema": 2,
            "objectives": [{"id": "efl", "description": "objective focal length (mm)", "check": {"operand": {"type": "EFFL"}}, "origin": "user",
                            "must": {"min": 9.0, "max": 11.0}, "source": "user: the 10 mm objective of the recipe spec"}],
            "summary": "按配方规格把扰动后的物镜优化回去。发现任何这个demand可能存在的问题，要大胆质疑和询问。",
            "stop": {"rel": 0.01, "max_rounds": 5, "tiers": ["local"]},
        }, allow_unicode=True, sort_keys=False), encoding="utf-8")
        saos("demand", "confirm", "--job", job, "--quote", "按规格优化", env=env)
        saos("opt", "vary", "--job", job, "--param", "radius", "--surfaces", "1-16", env=env)
        saos("opt", "vary", "--job", job, "--param", "thickness", "--surfaces", "0", env=env)
        saos("opt", "merit", "--job", job, "clear", env=env)
        saos("opt", "merit", "--job", job, "wizard", "--file", src / "merit_wizard.json", env=env)
        saos("opt", "merit", "--job", job, "add", "--file", src / "merit_extra.json", env=env)
        saos("opt", "local", "--job", job, "--cycles", "auto", env=env)
        saos("lens", "save", "--job", job, "--as", "optimised.zmx", env=env)
        after = saos("microscope", "accept", "--job", job, "--spec", "spec/benchmark.yaml", env=env)
        assert after["verdict"] == "pass" and after["spec_shortfall"] == 0
        assert json.loads((root / "job.json").read_text(encoding="utf-8"))["last_evaluation"]["file"] == "optimised.zmx"
    finally:
        saos("daemon", "stop", "--discard", env=env)
