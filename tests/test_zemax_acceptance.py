"""真实 OpticStudio 的认可入口：Optiland 搭建与优化 → 报告 → 用户认可（zos begin）→ OpticStudio 复测、对比、封存。
不改用户已打开的窗口：会话是本测试 standalone 启动的，结束时只关闭它自己。

Real OpticStudio acceptance entry: Optiland build and optimisation → report → user acceptance (zos begin) → OpticStudio
measurement, comparison and sealing. Standalone session started by this test; only that session is closed.

运行：SAOS_RUN_ZEMAX=1 uv run pytest tests/test_zemax_acceptance.py -s
"""

from __future__ import annotations

import json
import os

import pytest
import yaml

from saos import demand, jobs, ledger, report, stages
from saos.ui import stage as window
from saos.backend.optiland import OptilandBackend
from saos.daemon import Daemon
from test_optiland_flow import DEMAND, NOTES, call
from test_lens_snapshot_files import PRESCRIPTION

pytestmark = [
    pytest.mark.zemax,
    pytest.mark.skipif(os.environ.get("SAOS_RUN_ZEMAX") != "1", reason="需要真实 OpticStudio，设置 SAOS_RUN_ZEMAX=1"),
]


def test_optiland_design_is_accepted_and_verified_in_opticstudio(tmp_path, monkeypatch):
    monkeypatch.setenv("SAOS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAOS_BACKEND", "optiland")
    monkeypatch.setenv("SAOS_UI", "0")
    backend = OptilandBackend()
    daemon = Daemon(backend, backend.start(), "standalone")
    try:
        job = jobs.create_job("accept")
        assert call(daemon, "lens.build", job=job.id, prescription=PRESCRIPTION, file="single.zmx")["ok"]
        (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(DEMAND, allow_unicode=True), encoding="utf-8")
        demand.confirm(job.root, "确认，开始吧")
        folder = call(daemon, "lens.describe", job=job.id, notes=NOTES)["result"]["folder"]
        assert call(daemon, "lens.approve", job=job.id, model=folder, quote="模型可以")["ok"]
        assert call(daemon, "round.record", job=job.id)["ok"]
        assert call(daemon, "opt.merit", job=job.id, action="wizard", settings={})["ok"]
        assert call(daemon, "opt.vary", job=job.id, surfaces=[2], param="thickness", min=60.0, max=95.0)["ok"]
        assert call(daemon, "opt.local", job=job.id, max_seconds=20)["ok"]
        assert call(daemon, "round.record", job=job.id, tier="local")["ok"]

        generated = report.write(job)
        assert generated["deliverable"] is True and generated["deliver"], generated
        reply = call(daemon, "zos.begin", job=job.id, quote="可以，接受这份报告，进入 OpticStudio", round=None)
        assert reply["ok"], reply

        entry = stages.read(job.root)
        assert entry is not None and stages.completed(job.root)
        rows = ledger.load(job.root / ledger.LEDGER_FILE)
        verify = [r for r in rows if r.get("verify_of") == entry["source_round"]]
        assert len(verify) == 1 and verify[0]["stage"] == "zos"
        assert verify[0]["evaluation"] and json.loads((job.root / verify[0]["evaluation"]).read_text(encoding="utf-8"))["environment"]["backend"] == "zos"
        comparison = json.loads((job.root / stages.RESULT).read_text(encoding="utf-8"))
        assert comparison["items"] and comparison["verify_of"] == entry["source_round"]
        assert daemon.backend.system_info()["backend"] == "zos"
        # 工作流窗口只读文件：zos 已完成，不再停在 Optiland 设计阶段 / the window reads files: zos is done, not Optiland design
        snap = window.snapshot(job)
        statuses = {row["id"]: row["status"] for row in snap["steps"]}
        assert statuses["zos"] == "done" and statuses["design"] == "done" and snap["current"] in ("zos", "final_report")
    finally:
        if daemon.zos is not None:
            daemon.zos.close()
        daemon.optiland.close()
