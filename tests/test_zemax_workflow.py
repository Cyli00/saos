"""真实 ZOS 工作流回归：建模确认、局部优化、轮次与报告。仅由用户运行。"""

from __future__ import annotations

import os

import pytest

from saos import ledger, report
from zemax_support import design_session

pytestmark = [
    pytest.mark.zemax,
    pytest.mark.skipif(os.environ.get("SAOS_RUN_ZEMAX") != "1", reason="需要真实 OpticStudio，设置 SAOS_RUN_ZEMAX=1"),
]


def ok(reply):
    assert reply["ok"], reply
    return reply["result"]


def test_accepted_design_is_improved_in_opticstudio_and_reported(tmp_path, monkeypatch):
    with design_session(tmp_path / "home", monkeypatch, "workflow") as (daemon, job, entry):
        assert entry["round"] == 0
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

        # 第零轮就是 ZOS 基线，不再追加跨后端复测轮。
        rows = ledger.load(job.root / ledger.LEDGER_FILE)
        zos_rows = [r for r in rows if r.get("stage") == "zos"]
        assert zos_rows[0]["round"] == 0 and zos_rows[0]["tier"] == "baseline"
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
