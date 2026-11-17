"""ruff 按 pyproject.toml 的 [tool.ruff] 检查 src 与 tests；规则只开约束得住的几条，见那一节。
ruff checks src and tests with the rules in pyproject.toml's [tool.ruff]."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ruff_finds_nothing():
    run = subprocess.run([sys.executable, "-m", "ruff", "check", "--no-fix", "--output-format", "concise", "src", "tests"],
                         cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
