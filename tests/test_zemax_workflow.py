"""通用工作流在 OpticStudio 阶段的真实回归：认可之后，在 OpticStudio 里改镜头 → 局部优化 → 记轮次 → 报告只选 ZOS 轮次。

迁移说明（A3）：旧版从 SAOS_BACKEND=zos 直接启动守护进程并导入 Cooke 样例。该入口已按设计拒绝（OpticStudio 只能经
zos begin 进入）。现在从 Optiland 搭建的两片样品开始，经用户认可进入 OpticStudio；保留原来的 EFFL 不超 1 % 与光斑下降断言。
旧版的第二段（无法再优化 → exhausted，含 Hammer 与全局搜索）未迁移，见计划第 13 节。

默认跳过。运行：SAOS_RUN_ZEMAX=1 uv run pytest tests/test_zemax_workflow.py -s
"""

from __future__ import annotations

import os

import pytest

from saos import ledger, report
from zemax_support import accepted_session

pytestmark = [
    pytest.mark.zemax,
    pytest.mark.skipif(os.environ.get("SAOS_RUN_ZEMAX") != "1", reason="需要真实 OpticStudio，设置 SAOS_RUN_ZEMAX=1"),
]


def ok(reply):
    assert reply["ok"], reply
    return reply["result"]


def test_accepted_design_is_improved_in_opticstudio_and_reported(tmp_path, monkeypatch):
    with accepted_session(tmp_path / "home", monkeypatch, "workflow") as (daemon, job, entry):
        assert entry["source_round"] == 0
        f0 = ok(daemon.handle({"op": "lens.operand", "args": {"job": job.id, "type": "EFFL", "int2": 2}}))["value"]
        spot0 = ok(daemon.handle({"op": "lens.analysis", "args": {"job": job.id, "type": "StandardSpot"}}))["spot"]["rms_radius"][0][0]

        # 放开全部曲率，RMS 光斑为评价函数并约束焦距，在 OpticStudio 中做阻尼最小二乘 / radii free, RMS spot merit held to f0
        ok(daemon.handle({"op": "opt.vary", "args": {"job": job.id, "param": "radius", "surfaces": [1, 2, 3, 4]}}))
        ok(daemon.handle({"op": "opt.merit", "args": {"job": job.id, "action": "wizard",
                                                       "settings": {"type": "RMS", "data": "Spot Radius", "reference": "Centroid"}}}))
        ok(daemon.handle({"op": "opt.merit", "args": {"job": job.id, "action": "add",
                                                       "rows": [{"type": "EFFL", "int2": 2, "target": f0, "weight": 10}]}}))
        ok(daemon.handle({"op": "opt.local", "args": {"job": job.id, "algorithm": "dls"}}))
        decision = ok(daemon.handle({"op": "round.record", "args": {"job": job.id, "tier": "local", "note": "radii + DLS in OpticStudio"}}))["decision"]

        # OpticStudio 阶段的轮次都有 verify 基线：第一轮 verify_of 指向认可的轮次 / every ZOS-stage round follows the verify baseline
        rows = ledger.load(job.root / ledger.LEDGER_FILE)
        zos_rows = [r for r in rows if r.get("stage") == "zos"]
        assert zos_rows[0]["verify_of"] == entry["source_round"] and zos_rows[0]["tier"] == "verify"
        assert zos_rows[-1]["round"] == rows[-1]["round"] and rows[-1]["stage"] == "zos"

        # EFFL 保持在 1 % 之内；光斑 RMS 从验证基线下降 / EFFL held within 1 %; spot RMS falls from the verified baseline
        effl = ok(daemon.handle({"op": "lens.operand", "args": {"job": job.id, "type": "EFFL", "int2": 2}}))["value"]
        assert effl == pytest.approx(f0, rel=0.01)
        spot = ok(daemon.handle({"op": "lens.analysis", "args": {"job": job.id, "type": "StandardSpot"}}))["spot"]["rms_radius"][0][0]
        assert spot < spot0 and decision["status"] in ("achieved", "continue")

        # 报告只选 OpticStudio 阶段的合资格轮次 / the report selects only deliverable OpticStudio-stage rounds
        generated = report.write(job)
        assert generated["deliverable"] is True and generated["deliver"], generated
        assert any(r["file"] == generated["deliver"] and r.get("stage") == "zos" for r in rows), generated["deliver"]
