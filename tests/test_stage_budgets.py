"""Z05：两阶段各自计数的预算；旧的停止与层级排除不进入 OpticStudio 阶段；循环重测（rebase）不占用轮次。

只用真实的 ledger 与阶段文件（不启动任何程序）：探索阶段的记录 → 认可入口（stages/zos.json）→ OpticStudio 阶段的轮次。
"""

from __future__ import annotations

import json

from saos import ledger, stages
from saos.demand import TIERS

POLICY = {"rel": 0.01, "max_rounds": 3, "tiers": list(TIERS)}


def rnd(number, stage, tier, shortfall, **extra):
    return {"round": number, "stage": stage, "tier": tier, "tiers": [tier] if tier else [], "shortfall": shortfall,
            "demand_sha": "d", "method_sha": "m", "achieved": False, "measurement_errors": [], "model_glass": [],
            "hard_failing": False, "parts_clash": False, **extra}


def write_entry(root, first_round, max_rounds):
    (root / "stages").mkdir(exist_ok=True)
    (root / "stages" / "zos.json").write_text(json.dumps({"stage": "zos", "first_round": first_round, "max_rounds": max_rounds}), encoding="utf-8")


def exploration(root):
    """第 0 轮，再用 3 个层级各一轮，都没有明显进步：探索阶段的轮次数已到 max_rounds = 3。
    Round 0 and one round per tier, none with a significant gain: the exploration budget of 3 is used up."""
    rows = [rnd(0, "exploration", None, 10.0), rnd(1, "exploration", TIERS[0], 9.99), rnd(2, "exploration", TIERS[1], 9.98),
            rnd(3, "exploration", TIERS[2], 9.97)]
    for row in rows:
        ledger.append(root / ledger.LEDGER_FILE, row)
    return rows


def add_zos(root, *rows):
    for row in rows:
        ledger.append(root / ledger.LEDGER_FILE, row)


def zos_rounds(root):
    return stages.rounds(root, ledger.load(root / ledger.LEDGER_FILE))


def test_each_stage_counts_its_own_budget(tmp_path):
    exploration(tmp_path)
    assert ledger.decide(ledger.load(tmp_path / ledger.LEDGER_FILE), POLICY)["status"] == "limit"  # 探索阶段用完了 3 轮 / exploration budget used
    write_entry(tmp_path, first_round=4, max_rounds=2)
    add_zos(tmp_path, rnd(4, "zos", "verify", 9.97, verify_of=3))
    budget = stages.stop(tmp_path, POLICY)
    assert budget["max_rounds"] == 2  # OpticStudio 阶段用认可入口记下的上限，不沿用探索阶段的 3 / the ZOS stage uses the cap recorded at acceptance
    first = ledger.decide(zos_rounds(tmp_path), budget)
    assert first["status"] == "continue" and first["rounds_in_version"] == 0  # 验证轮不计入 / the verify round is not counted
    add_zos(tmp_path, rnd(5, "zos", TIERS[0], 9.97))
    second = ledger.decide(zos_rounds(tmp_path), budget)
    assert second["status"] == "continue" and second["rounds_in_version"] == 1 and second["next_tier"] == TIERS[1]
    add_zos(tmp_path, rnd(6, "zos", TIERS[1], 9.97))
    third = ledger.decide(zos_rounds(tmp_path), budget)
    assert third["status"] == "limit" and third["rounds_in_version"] == 2 and "max_rounds = 2" in third["reason"]


def test_a_stop_before_acceptance_does_not_end_the_zos_stage(tmp_path):
    exploration(tmp_path)
    ledger.append(tmp_path / ledger.STOPS_FILE, ledger.record_stop(tmp_path, "d", "探索阶段已无进步", "停，就到这里"))
    stop = ledger.stop_for(tmp_path, "d")
    assert ledger.decide(ledger.load(tmp_path / ledger.LEDGER_FILE), POLICY, stop)["status"] == "stopped"
    write_entry(tmp_path, first_round=4, max_rounds=5)
    add_zos(tmp_path, rnd(4, "zos", "verify", 9.97, verify_of=3), rnd(5, "zos", TIERS[0], 9.97))
    result = ledger.decide(zos_rounds(tmp_path), stages.stop(tmp_path, POLICY), stop)
    assert result["status"] == "continue", result  # 旧的停止（第 3 轮）不适用于 OpticStudio 阶段 / the old stop does not apply to the ZOS stage


def test_a_rule_out_before_acceptance_does_not_cover_zos_tiers(tmp_path):
    exploration(tmp_path)
    ledger.record_rule_out(tmp_path, "d", [TIERS[1]], "诊断结果显示这个层级无效")
    ruled = ledger.ruled_out_for(tmp_path, "d")
    write_entry(tmp_path, first_round=4, max_rounds=5)
    add_zos(tmp_path, rnd(4, "zos", "verify", 9.97, verify_of=3), rnd(5, "zos", TIERS[0], 9.97))
    result = ledger.decide(zos_rounds(tmp_path), stages.stop(tmp_path, POLICY), None, ruled)
    assert result["next_tier"] == TIERS[1]  # 探索阶段的排除不生效，OpticStudio 阶段仍可以试这个层级 / exploration rule-out does not carry over
    # 在 OpticStudio 阶段内的排除才生效 / a rule-out made inside the ZOS stage does hold
    ledger.record_rule_out(tmp_path, "d", [TIERS[1]], "OpticStudio 中的一阶结果证明这个层级无效")
    ruled = ledger.ruled_out_for(tmp_path, "d")
    result = ledger.decide(zos_rounds(tmp_path), stages.stop(tmp_path, POLICY), None, ruled)
    assert result["next_tier"] == TIERS[2] and TIERS[1] in result["tiers_ruled_out_since_gain"]


def test_a_checks_rebase_does_not_use_the_zos_budget(tmp_path):
    write_entry(tmp_path, first_round=0, max_rounds=2)
    add_zos(tmp_path, rnd(0, "zos", "verify", 9.97, verify_of=0), rnd(1, "zos", TIERS[0], 9.97),
            rnd(2, "zos", ledger.REBASE, 9.97))
    result = ledger.decide(zos_rounds(tmp_path), stages.stop(tmp_path, POLICY))
    assert result["status"] == "continue" and result["rounds_in_version"] == 1  # 重测不计入上限 2 / the re-measurement does not count toward 2
