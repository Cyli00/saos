"""需求的两层哈希（确认层、计算层）与升级前旧公式的兼容。
Two hash layers of the demand (confirmation, compute) and compatibility with the pre-upgrade formula."""

from __future__ import annotations

import copy
import json

import pytest
import yaml

from saos import demand, ledger, workflow
from saos.protocol import ErrorCode, SaosError

# 旧公式在升级前对这份需求算出的值（升级前的代码，demand.digest）。公式不得改动，这个值必须永远不变。
# The old formula's value for this demand, computed by the pre-upgrade code (demand.digest). The formula must not change.
FIXTURE = {
    "schema": 2,
    "constants": [{"id": "lens", "description": "the sample singlet", "value": "a.zmx", "source": "user"}],
    "variables": [{"id": "radii", "description": "both radii free", "range": {"min": 1.0}, "check": {"operand": {"type": "TOTR"}},
                   "source": "agent assumption: typical radius"}],
    "objectives": [{"id": "length", "description": "total track", "check": {"operand": {"type": "TOTR"}}, "origin": "user",
                    "must": {"max": 95.0}, "wish": {"max": 90.0}, "scale": 5.0, "source": "user"}],
    "structure": {"mode": "prescribed", "reason": "测试样品的结构由用户指定"},
    "summary": "总结不参与哈希。",
    "lang": "zh",
}
OLD_DIGEST = "ac64b08fc1699545a84bfc5d200ebf5137cbf7416cb9e096f0e7d2662442c3de"
STOP = {"rel": 0.01, "max_rounds": 30, "tiers": list(demand.TIERS)}


def valid(data):
    return demand.validate(copy.deepcopy(data))


def edit(section: str, entry_id: str, **changes):
    """改 FIXTURE 中一条条目的字段（字段值为 None 时删除该字段）。/ edit one entry of FIXTURE (None deletes the field)."""
    data = copy.deepcopy(FIXTURE)
    entry = next(e for e in data[section] if e["id"] == entry_id)
    for key, value in changes.items():
        if value is None:
            entry.pop(key, None)
        else:
            entry[key] = value
    return data


def job_root(tmp_path, data=FIXTURE, confirmations=(), stops=(), rounds=(), rule_outs=()):
    """一个只含文件的任务目录（不启动后台进程）。/ a job folder holding only files (no daemon)."""
    (tmp_path / "sources").mkdir(exist_ok=True)
    (tmp_path / "sources" / "demand.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    for name, rows in (("confirmations.jsonl", confirmations), ("stops.jsonl", stops), ("ruled_out.jsonl", rule_outs), ("ledger.jsonl", rounds)):
        if rows:
            (tmp_path / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return tmp_path


def rounds_of(shortfalls, tiers, sha):
    return [{"round": k, "shortfall": g, "tiers": [t], "tier": t, "achieved": False, "values": {}, "measurement_errors": [],
             "hard_failing": [], "failing": {}, "demand_sha": sha, "method_sha": "m", "time": "t"}
            for k, (g, t) in enumerate(zip(shortfalls, tiers))]


def test_old_formula_is_frozen():
    """旧公式不变：升级前的记录靠它认出来。/ the old formula is unchanged: pre-upgrade records are recognised by it."""
    assert demand.legacy_digest(valid(FIXTURE)) == OLD_DIGEST


def test_description_only_change_keeps_both_layers():
    base = valid(FIXTURE)
    reworded = copy.deepcopy(FIXTURE)
    reworded["constants"][0]["description"] = "换个说法"
    reworded["variables"][0]["description"] = "两面半径都放开"
    reworded["objectives"][0]["description"] = "总长（轨迹）"
    reworded = valid(reworded)
    assert demand.digest(reworded) == demand.digest(base)
    assert demand.compute_digest(reworded) == demand.compute_digest(base)


def test_source_only_change_needs_confirmation_but_keeps_compute():
    base = valid(FIXTURE)
    basis = valid(edit("variables", "radii", source="user"))  # 依据从“agent 假设”改为“用户”
    assert demand.digest(basis) != demand.digest(base)
    assert demand.compute_digest(basis) == demand.compute_digest(base)


@pytest.mark.parametrize("changed", [
    edit("constants", "lens", value="b.zmx"),                                       # 常量数值
    edit("variables", "radii", range={"min": 2.0}),                                 # 变量范围
    edit("objectives", "length", must={"max": 90.0}),                               # 达标线
    edit("objectives", "length", check={"operand": {"type": "EFFL"}}),              # 判定
    edit("objectives", "length", weight=2.0),                                       # 权重
    edit("objectives", "length", unit="mm"),                                        # 单位
    edit("objectives", "length", scale=4.0),                                        # 尺度
    {**FIXTURE, "stop": {**STOP, "max_rounds": 20}},                                # 终止规则
])
def test_value_range_check_weight_changes_both_layers(changed):
    base = valid(FIXTURE)
    after = valid(changed)
    assert demand.digest(after) != demand.digest(base)
    assert demand.compute_digest(after) != demand.compute_digest(base)


def test_as_current_maps_only_this_demands_legacy_value():
    base = valid(FIXTURE)
    assert demand.as_current(OLD_DIGEST, base, "compute") == demand.compute_digest(base)
    assert demand.as_current(OLD_DIGEST, base, "confirm") == demand.digest(base)
    new = demand.compute_digest(base)
    assert demand.as_current(new, base, "compute") == new  # 新哈希原样返回 / a new hash comes back unchanged
    assert demand.as_current(None, base) is None
    other = valid(edit("constants", "lens", value="b.zmx"))
    assert demand.as_current(OLD_DIGEST, other, "compute") == OLD_DIGEST  # 旧值属于另一版需求，不认 / another version's old value is not recognised
    with pytest.raises(ValueError):
        demand.as_current(OLD_DIGEST, base, "bogus")


def test_legacy_confirmation_still_confirms_and_preflight_returns_the_compute_layer(tmp_path):
    root = job_root(tmp_path, confirmations=[{"demand_sha": OLD_DIGEST, "quote": "好，就这样", "time": "t"}])
    d = demand.load_job(root)
    assert demand.confirmation_for(root, demand.digest(d), d) is not None
    _, sha, _ = workflow.preflight(root)
    assert sha == demand.compute_digest(d)


def test_confirmation_is_bound_to_the_confirmation_layer(tmp_path):
    """确认记录绑定确认层：只改 source 后，旧确认失效，preflight 要求重新确认。
    Confirmation is bound to the confirmation layer: after a source-only change the old confirmation is void."""
    root = job_root(tmp_path, confirmations=[{"demand_sha": demand.digest(valid(FIXTURE)), "quote": "好", "time": "t"}])
    _, before, _ = workflow.preflight(root)
    changed = edit("variables", "radii", source="user")
    job_root(tmp_path, data=changed)  # 覆盖需求文件 / overwrite the demand file
    with pytest.raises(SaosError) as exc:
        workflow.preflight(root)
    assert exc.value.code == ErrorCode.DEMAND_NOT_CONFIRMED and exc.value.details["demand_sha"] == demand.digest(valid(changed))
    # 同样的计算层哈希：已有的数值证据仍对得上 / the same compute hash: existing numerical evidence still matches
    assert demand.compute_digest(valid(changed)) == before


def test_description_only_edit_keeps_confirmation_and_plan_version(tmp_path):
    """只改描述：确认仍有效，preflight 给出的版本（计划、比较、轮次都用它）不变。/ a description-only edit keeps the confirmation and the version."""
    root = job_root(tmp_path, confirmations=[{"demand_sha": demand.digest(valid(FIXTURE)), "quote": "好", "time": "t"}])
    _, recorded, _ = workflow.preflight(root)  # 计划或轮次写下的版本 / the version a plan or round recorded
    job_root(tmp_path, data=edit("objectives", "length", description="总长，越短越好"))
    _, now, _ = workflow.preflight(root)
    assert now == recorded


def test_legacy_rounds_are_current_after_upgrade(tmp_path):
    """升级前写下的轮次（旧公式哈希）在新代码下仍是当前版本：同一个比较窗口、同样的终止判断。
    Rounds written before the upgrade are still the current version: the same window and the same stop decision."""
    d = valid(FIXTURE)
    legacy = ledger_path(tmp_path, rounds_of([10, 5, 5], ["baseline", "local", "local"], OLD_DIGEST))
    new = rounds_of([10, 5, 5, 3], ["baseline", "local", "local", "variables"], demand.compute_digest(d))
    loaded = ledger.load(legacy, d)
    assert {r["demand_sha"] for r in loaded} == {demand.compute_digest(d)}
    assert ledger.current_version(loaded) == loaded  # 同一版本，轮次一个不少 / one version, no round dropped
    assert ledger.decide(loaded, STOP) == ledger.decide(new[:3], STOP)
    old_tail = {**rounds_of([5], ["local"], OLD_DIGEST)[0], "round": 2}  # 升级前的第 2 轮，与新轮次混在一份 ledger 里 / a pre-upgrade round 2 among new rounds
    mixed = ledger.load(ledger_path(tmp_path / "m", new[:2] + [old_tail]), d)
    assert ledger.decide(ledger.current_window(mixed), STOP)["last_gain_round"] == ledger.decide(new[:3], STOP)["last_gain_round"]
    # 不传需求时不做换算：调用方必须传 demand / without demand nothing is converted: callers must pass the demand
    assert ledger.version(ledger.load(legacy)[0])[0] == OLD_DIGEST


def test_description_only_edit_keeps_rounds_stops_and_rule_outs(tmp_path):
    base = valid(FIXTURE)
    rounds = rounds_of([10, 5], ["baseline", "local"], demand.compute_digest(base))
    root = job_root(tmp_path, confirmations=[{"demand_sha": demand.digest(base), "quote": "好", "time": "t"}],
                    stops=[{"round": 1, "demand_sha": demand.compute_digest(base), "reason": "r", "quote": "q", "time": "t"}],
                    rule_outs=[{"round": 1, "demand_sha": demand.compute_digest(base), "tiers": ["structure"], "reason": "r", "time": "t"}],
                    rounds=rounds)
    job_root(tmp_path, data=edit("objectives", "length", description="总长（轨迹）"))
    d = demand.load_job(root)
    assert demand.confirmation_for(root, demand.digest(d), d) is not None
    sha = demand.compute_digest(d)
    assert ledger.stop_for(root, sha, d) is not None
    assert [r["tiers"] for r in ledger.ruled_out_for(root, sha, d)] == [["structure"]]
    assert all(ledger.version(r)[0] == sha for r in ledger.load(root / ledger.LEDGER_FILE, d))


def test_source_only_edit_keeps_compute_but_not_the_confirmation(tmp_path):
    base = valid(FIXTURE)
    root = job_root(tmp_path, confirmations=[{"demand_sha": demand.digest(base), "quote": "好", "time": "t"}],
                    stops=[{"round": 1, "demand_sha": demand.compute_digest(base), "reason": "r", "quote": "q", "time": "t"}])
    job_root(tmp_path, data=edit("variables", "radii", source="user"))
    d = demand.load_job(root)
    assert demand.confirmation_for(root, demand.digest(d), d) is None
    assert ledger.stop_for(root, demand.compute_digest(d), d) is not None


def test_changed_value_voids_rounds_stops_and_confirmation(tmp_path):
    base = valid(FIXTURE)
    root = job_root(tmp_path, confirmations=[{"demand_sha": demand.digest(base), "quote": "好", "time": "t"}],
                    stops=[{"round": 1, "demand_sha": demand.compute_digest(base), "reason": "r", "quote": "q", "time": "t"}],
                    rounds=rounds_of([10, 5], ["baseline", "local"], demand.compute_digest(base)))
    job_root(tmp_path, data=edit("objectives", "length", must={"max": 90.0}))
    d = demand.load_job(root)
    assert demand.confirmation_for(root, demand.digest(d), d) is None
    assert ledger.stop_for(root, demand.compute_digest(d), d) is None
    assert all(ledger.version(r)[0] != demand.compute_digest(d) for r in ledger.load(root / ledger.LEDGER_FILE, d))


def test_legacy_stop_and_rule_out_are_recognised(tmp_path):
    root = job_root(tmp_path, stops=[{"round": 1, "demand_sha": OLD_DIGEST, "reason": "r", "quote": "q", "time": "t"}],
                    rule_outs=[{"round": 1, "demand_sha": OLD_DIGEST, "tiers": ["global"], "reason": "r", "time": "t"}])
    d = demand.load_job(root)
    sha = demand.compute_digest(d)
    assert ledger.stop_for(root, sha, d)["quote"] == "q"
    assert [r["tiers"] for r in ledger.ruled_out_for(root, sha, d)] == [["global"]]
    assert ledger.stop_for(root, sha) is None  # 不传需求时认不出旧值 / without the demand the old value is not recognised


def test_record_stop_after_upgrade_accepts_the_legacy_round(tmp_path):
    """升级后用户停止：最后一轮是升级前写下的，记录停止必须成功。/ a stop after the upgrade refers to a pre-upgrade round."""
    d = valid(FIXTURE)
    root = job_root(tmp_path, rounds=rounds_of([10], ["baseline"], OLD_DIGEST))
    record = ledger.record_stop(root, demand.compute_digest(d), "no real focus", "保持现结构", d)
    assert record["round"] == 0 and ledger.stop_for(root, demand.compute_digest(d), d) == record


def ledger_path(folder, rows):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / ledger.LEDGER_FILE
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    return path


def test_known_limit_a_pre_upgrade_record_is_recognised_only_for_its_own_demand():
    """已知限制：升级前的记录只认得写下它时的那份需求。升级后第一次改需求（哪怕只改描述），升级前的记录不再认出，
    与旧代码的行为相同；之后的记录按两层规则工作。消除它需要用记录里的需求全文（result.json、confirmations 的 content）重算。
    Known limit: a pre-upgrade record is recognised only for the demand it came from. The first edit after the upgrade,
    even a description-only one, no longer recognises it, as the old code did; later records follow the two layers."""
    before = valid(FIXTURE)
    after = valid(edit("objectives", "length", description="总长（轨迹）"))
    assert demand.as_current(OLD_DIGEST, after, "compute") == OLD_DIGEST
    assert demand.compute_digest(after) == demand.compute_digest(before)


def test_legacy_archive_keeps_confirmation_rounds_and_stop_after_rewording(tmp_path):
    base = valid(FIXTURE)
    root = job_root(tmp_path, confirmations=[{"demand_sha": OLD_DIGEST, "quote": "同意停止", "content": yaml.safe_dump(FIXTURE)}],
                    rounds=rounds_of([10, 5], ["baseline", "local"], OLD_DIGEST),
                    stops=[{"round": 1, "demand_sha": OLD_DIGEST, "quote": "停止"}],
                    rule_outs=[{"round": 1, "demand_sha": OLD_DIGEST, "tiers": ["global"]}])
    job_root(root, data=edit("objectives", "length", description="总长换一种说法"))
    current = demand.load_job(root)
    _, sha, _ = workflow.preflight(root)
    assert sha == demand.compute_digest(base)
    assert ledger.stop_for(root, sha, current)["quote"] == "停止"
    assert ledger.ruled_out_for(root, sha, current)[0]["tiers"] == ["global"]
    assert all(ledger.version(r)[0] == sha for r in ledger.load(root / ledger.LEDGER_FILE, current))
    changed = valid(edit("objectives", "length", must={"max": 80.0}))
    assert demand.as_current(OLD_DIGEST, changed, root=root) == OLD_DIGEST


def test_legacy_evaluation_full_text_is_used_when_no_confirmation_archive(tmp_path):
    root = job_root(tmp_path, rounds=rounds_of([10], ["baseline"], OLD_DIGEST))
    folder = root / "evaluations" / "001"
    folder.mkdir(parents=True)
    (folder / "result.json").write_text(json.dumps({"demand": valid(FIXTURE)}), encoding="utf-8")
    current = valid(edit("objectives", "length", description="新版文字"))
    assert ledger.version(ledger.load(root / ledger.LEDGER_FILE, current)[0])[0] == demand.compute_digest(current)


def test_structure_reason_changes_only_confirmation_and_preserves_old_compute_hash(tmp_path):
    before = valid(FIXTURE)
    old_compute = demand._hash(demand._body(before, frozenset({"description", "source"})))
    root = job_root(tmp_path, confirmations=[{"demand_sha": demand.digest(before), "quote": "同意", "content": yaml.safe_dump(FIXTURE)}])
    after = copy.deepcopy(before)
    after["structure"]["reason"] = "重写理由文字"
    assert demand.digest(after) != demand.digest(before)
    assert demand.compute_digest(after) == demand.compute_digest(before)
    assert demand.as_current(old_compute, after, root=root) == demand.compute_digest(after)
