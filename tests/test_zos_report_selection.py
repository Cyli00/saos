"""Z06：OpticStudio 的测量比 Optiland 差（总长多 3 mm）时，最终报告仍只选择合资格的 ZOS 轮次，并保留两端差异与来源。

沿用 test_zos_begin_recovery 的夹具：OpticStudio 会话由测试替身 FakeZos 代替，替身的几何比 Optiland 多 3 mm。
达标之后不再记录新轮次（round.record 拒绝），所以 ZOS 阶段只有验证轮；它就是唯一的 ZOS 轮次。
被测的是报告的选择与比较记录，不是光学结果。
"""

from __future__ import annotations

import json

from saos import ledger, report, stages
from test_zos_begin_recovery import FakeZos, begin, ready  # noqa: F401  (ready 是夹具 / ready is a fixture)


def test_report_delivers_the_eligible_zos_round_and_keeps_both_ends(ready, monkeypatch):  # noqa: F811
    daemon, job = ready
    real_load = FakeZos.load

    def longer_in_opticstudio(self, path):
        # 测试替身：OpticStudio 读入的镜头最后一面比 Optiland 厚 3 mm / the double: the last surface is 3 mm thicker than in Optiland
        real_load(self, path)
        self.lens["surfaces"][4]["thickness"] += 3.0

    monkeypatch.setattr(FakeZos, "load", longer_in_opticstudio)
    accepted = json.loads((job.root / "report" / "latest.json").read_text(encoding="utf-8"))["archived"]
    assert begin(daemon, job)["ok"]
    entry = stages.read(job.root)
    optiland_round = entry["source_round"]
    rows = ledger.load(job.root / ledger.LEDGER_FILE)
    zos_rounds = [r for r in rows if r.get("stage") == "zos"]
    assert len(zos_rounds) == 1 and zos_rounds[0]["verify_of"] == optiland_round
    verify = zos_rounds[0]
    optiland = next(r for r in rows if r["round"] == optiland_round)
    assert verify["shortfall"] >= optiland["shortfall"] and ledger.deliverable(verify)  # 比 Optiland 差，但仍合资格 / worse than Optiland, still eligible

    report.write(job)
    meta = json.loads((job.root / "report" / "latest.json").read_text(encoding="utf-8"))
    # 报告交付唯一合资格的 ZOS 轮次，而不是 Optiland 轮次 / the report delivers the only eligible ZOS round, not the Optiland one
    assert meta["round"] == verify["round"] and meta["round"] != optiland_round

    # 两端差异与来源都保留：比较记录指向 Optiland 来源轮次，报告归档与认可记录一致
    # Both ends and their sources are kept: the comparison points at the Optiland source round; the archived report matches the acceptance
    comparison = json.loads((job.root / stages.RESULT).read_text(encoding="utf-8"))
    assert comparison["verify_of"] == optiland_round and comparison["round"] == verify["round"]
    # 认可时的报告（Optiland 轮次）仍是来源；认可之后生成的报告是另一份归档 / the accepted report (Optiland) stays the source; the later report is a new archive
    assert entry["report"] == accepted and meta["archived"] != accepted
    assert entry["source_file"] and entry["quote"]
    assert comparison["items"], "the comparison must keep the Optiland and OpticStudio values"
