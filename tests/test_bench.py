from __future__ import annotations

import pytest

from zea import bench, rounds


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("ZEA_LIBRARY", str(tmp_path / "library"))
    return tmp_path / "library"


def test_replay_matches_decide_and_restores_parameters():
    traj = [10.0, 5.0, 4.98, 4.97, 4.96, 1.0]
    out = bench.replay(traj, stagnation_rounds=3, stagnation_rel=0.01, max_rounds=20)
    assert out == {"stop_round": 4, "status": "stagnated", "g_best": 4.96}
    # 放宽停滞轮数后能走到第 5 轮
    assert bench.replay(traj, 4, 0.01, 20)["stop_round"] is None
    assert (rounds.STAGNATION_ROUNDS, rounds.STAGNATION_REL, rounds.MAX_ROUNDS) == (3, 0.01, 20)


def test_replay_pass_and_limit():
    assert bench.replay([3.0, 1.0, 0.0], 3, 0.01, 20) == {"stop_round": 2, "status": "pass", "g_best": 0.0}
    assert bench.replay([3.0, 2.0, 1.0, 0.5], 3, 0.01, 2)["status"] == "limit"
    assert bench.first_pass([3.0, 1.0, 0.0]) == 2 and bench.first_pass([3.0]) is None


def test_oplt_references_tthi_row_after_reverse_insert(tmp_path, monkeypatch):
    case = object.__new__(bench.Case)
    sent = {}

    def req(op, **args):
        if op == "lens.operand":
            return {"value": -0.5}
        if args.get("action") == "add":
            sent["rows"] = args["rows"]
        return {}

    monkeypatch.setattr(case, "req", req, raising=False)
    monkeypatch.setattr(case, "merit_rows", lambda lay, failing: [{"type": "A"}, {"type": "TTHI"}, {"type": "OPLT", "ref": 1}, {"type": "B"}], raising=False)
    top_down = case.apply_merit({}, {})
    # merit add 逐行插到第 1 行：提交顺序的最后一行最终在最上面
    final = list(reversed(sent["rows"]))
    assert [r["type"] for r in final] == [r["type"] for r in top_down]
    oplt = next(r for r in final if r["type"] == "OPLT")
    assert final[oplt["int1"] - 1]["type"] == "TTHI"


@pytest.mark.parametrize("g0, expect", [(0.0, False), (0.5, False), (1.0, True), (float("inf"), True)])
def test_clearly_failing_start(g0, expect):
    assert bench.clearly_failing(g0) is expect


def test_calibrate_counts_false_stops_and_idle_rounds():
    trajs = {
        "late_pass": [10.0, 9.0, 8.99, 8.98, 8.97, 0.0],  # 3 轮停滞后第 5 轮通过
        "never": [10.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0],
        "quick": [10.0, 0.0],
    }
    rows = bench.calibrate(trajs, (3, 5), (0.01,), (20,))
    by_n = {r["stagnation_rounds"]: r for r in rows}
    assert by_n[3]["false_stops"] == ["late_pass"] and by_n[3]["passed"] == 1
    assert by_n[5]["false_stops"] == [] and by_n[5]["passed"] == 2
    # late_pass 停在第 4 轮、最后进步在第 1 轮；never 同样，各 3 轮
    assert by_n[3]["idle_rounds"] == 6


def test_idle_rounds_never_negative_and_later_gain_is_reported():
    # 审阅反例：停在第 4 轮，第 5 轮又有进步。旧算法用完整轨迹的最后进步（第 5 轮）相减得 −1
    traj = [10.0, 5.0, 5.0, 5.0, 5.0, 4.0, 4.0, 4.0, 4.0, 4.0]
    row = bench.calibrate({"x": traj}, (3,), (0.01,), (20,))[0]
    assert row["rounds_used"] == 4 and row["idle_rounds"] == 3
    assert row["missed"] == [{"case": "x", "stop_round": 4, "g_at_stop": 5.0, "g_later": 4.0}]


def test_stretched_spec_scales_measured_values():
    spec = {"limits": {"working_distance_min": 11.65, "optical_length_max": 53}}
    out = bench.stretched_spec(spec, {"wd_mech_mm": 12.27, "optical_length_mm": 47.6}, {"wd": 1.25, "length": 0.85})
    assert out["limits"]["working_distance_min"] == pytest.approx(15.338, abs=1e-3)
    assert out["limits"]["optical_length_max"] == pytest.approx(40.46)
    assert spec["limits"]["working_distance_min"] == 11.65  # 原规格不变
    with pytest.raises(ValueError):
        bench.stretched_spec(spec, {}, {"na": 1.1})


def test_first_targeted_round_already_escalates(monkeypatch):
    case = object.__new__(bench.Case)
    case.spec = __import__("zea.spec", fromlist=["parse_spec"]).parse_spec(
        {"spec_version": 1, "correction_class": "achromat", "magnification": 20, "na": 0.4, "limits": {"working_distance_min": 10}}
    )
    monkeypatch.setattr(case, "req", lambda op, **a: {"value": 0.0}, raising=False)
    lay = {"first": 1, "last": 10, "gap": 0, "block": list(range(1, 12))}
    base = {r["type"]: r["weight"] for r in case.merit_rows(lay, {})}
    first = {r["type"]: r["weight"] for r in case.merit_rows(lay, {"working_distance": 0})}
    assert first["CTGT"] == 3 * base["CTGT"]  # 否则针对的第一轮与上一轮完全相同
    capped = {r["type"]: r["weight"] for r in case.merit_rows(lay, {"working_distance": 99})}
    assert capped["CTGT"] == base["CTGT"] * bench.ESCALATION**bench.MAX_ESCALATION_STEPS


def test_resume_restores_escalation_like_continuous_run():
    # 审阅反例：连续运行到第 5 轮应为 ×3³ = 27，旧代码续跑时清空进度只剩 ×3
    failing = {"working_distance": 0.1}
    streak = {}
    for number in range(1, 6):
        streak = bench.next_streak(streak, failing, number)
    assert streak == {"working_distance": 2}
    resumed = bench.next_streak({"working_distance": 1}, failing, 5)  # 第 4 轮记录的 targeted
    assert resumed == streak


def _case_with_history(tmp_path, history, recorded):
    import json

    case = object.__new__(bench.Case)
    case.job = type("J", (), {"id": "j", "root": tmp_path})()
    case.log_path = tmp_path / "bench.jsonl"
    case.log_path.write_text("".join(json.dumps(h) + "\n" for h in history), encoding="utf-8")
    (tmp_path / "rounds.jsonl").write_text("".join(json.dumps({"round": r}) + "\n" for r in recorded), encoding="utf-8")
    return case


def test_resume_rejects_other_policy_and_out_of_sync_rounds(tmp_path):
    from zea.protocol import ZeaError

    old = [{"round": 0, "policy": "reference-v2", "spec_shortfall": 3.0, "items": {"osc": 3.0}, "file": "rounds/r000.zmx"}]
    with pytest.raises(ZeaError, match="其他版本"):
        _case_with_history(tmp_path, old, [0]).run(5)
    cur = [{**old[0], "policy": bench.POLICY}]
    with pytest.raises(ZeaError, match="不一致"):
        _case_with_history(tmp_path, cur, [0, 1]).run(5)


def test_prepare_does_not_discard_by_default(lib, zea_home, monkeypatch):
    from zea import library as lib_mod

    lib_mod.save_entry("X", {"id": "X", "status": "admitted", "metrics": {}})
    lib_mod.write_file("X", "prescription.json", {"title": "x"})
    (lib_mod.entry_dir("X") / "benchmark.yaml").write_text("spec_version: 1\nlimits: {}\n", encoding="utf-8")
    sent = []
    monkeypatch.setattr(bench.client, "request", lambda op, args, timeout: sent.append((op, args)) or {})
    monkeypatch.setattr(bench.Case, "__init__", lambda self, *a, **k: None)
    monkeypatch.setattr(bench.Case, "start", lambda self, *a, **k: None)
    bench.prepare("X", 1, 0.01, 0.03, 10)
    assert sent[0][0] == "lens.build" and sent[0][1]["discard"] is False


SPEC = """spec_version: 1
correction_class: achromat
magnification: 20
na: 0.4
limits:
  working_distance_min: {wd}
"""


def test_observe_rounds_keep_freeze_but_do_not_stop(zea_home, fake_lens_file, monkeypatch):
    from zea import jobs
    from zea.backend.fake import FakeBackend
    from zea.daemon import Daemon
    from zea.protocol import ErrorCode, ZeaError

    backend = FakeBackend()
    daemon = Daemon(backend, backend.start(), "standalone")
    job = jobs.create_job("observe")
    (job.spec_dir / "b.yaml").write_text(SPEC.format(wd=10), encoding="utf-8")
    daemon.op_import({"job": job.id, "src": str(fake_lens_file), "name": "a.zmx"})
    daemon.op_load({"job": job.id, "file": "a.zmx"})
    gs = iter([5.0, 5.0, 5.0, 5.0, 5.0, 5.0])
    monkeypatch.setattr(backend, "merit_value", lambda: 0.0, raising=False)
    monkeypatch.setattr(daemon, "_accept", lambda job, loaded, name, spec: {"verdict": "fail", "spec_shortfall": next(gs), "items": []})
    for k in range(4):
        out = daemon.op_round({"job": job.id, "spec": "spec/b.yaml", "observe": True})
    assert out["decision"]["status"] == "stagnated" and out["record"]["round"] == 3
    out = daemon.op_round({"job": job.id, "spec": "spec/b.yaml", "observe": True})  # 停机后仍可观察
    assert out["record"]["round"] == 4 and out["record"]["observe"] is True
    with pytest.raises(ZeaError) as exc:  # 不加 observe 时照常拒绝
        daemon.op_round({"job": job.id})
    assert exc.value.code == ErrorCode.BAD_ARGS
    (job.spec_dir / "b.yaml").write_text(SPEC.format(wd=12), encoding="utf-8")
    with pytest.raises(ZeaError) as exc:  # observe 也不能绕过冻结
        daemon.op_round({"job": job.id, "spec": "spec/b.yaml", "observe": True})
    assert exc.value.code == ErrorCode.SPEC_CHANGED


def test_reconcile_recovers_round_recorded_before_crash(tmp_path):
    import json

    r0 = {"round": 0, "policy": bench.POLICY, "spec_shortfall": 3.0, "items": {"osc": 3.0}, "file": "rounds/r000.zmx"}
    pending = {"event": "pending", "round": 1, "policy": bench.POLICY, "from": "rounds/r000.zmx", "strategy": {"targeted": {}}, "opt_s": 12.0,
               "mode": "standalone", "daemon_code": "abc"}
    case = _case_with_history(tmp_path, [r0, pending], [0])
    rec1 = {"round": 1, "file": "rounds/r001.zmx", "verdict": "fail", "spec_shortfall": 1.0, "items": {"osc": 1.0}}
    with (tmp_path / "rounds.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec1) + "\n")
    history = case.reconcile(bench.load_history(case.log_path))
    assert [h["round"] for h in history] == [0, 1] and history[1]["opt_s"] == 12.0 and history[1]["recovered_from"] == "rounds.jsonl"
    # 运行信息来自中断前保存的 pending，不是续跑时的后台进程；验收耗时未知，不写成 0
    assert history[1]["mode"] == "standalone" and history[1]["daemon_code"] == "abc" and "eval_s" not in history[1]
    assert [h["round"] for h in bench.load_history(case.log_path)] == [0, 1]  # 补齐的记录已写回


def test_completeness_and_interruption(tmp_path):
    import json

    def write(lines):
        (tmp_path / "bench.jsonl").write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")

    base = {"policy": bench.POLICY, "items": {}, "file": "f"}
    write([{**base, "round": 0, "spec_shortfall": 3.0}, {**base, "round": 1, "spec_shortfall": 0.0}])
    assert bench.completeness(tmp_path, {"max_rounds": 20}) == "complete"
    write([{**base, "round": 0, "spec_shortfall": 3.0}, {**base, "round": 1, "spec_shortfall": 2.0}])
    assert bench.completeness(tmp_path, {"max_rounds": 1}) == "complete"
    assert bench.completeness(tmp_path, {"max_rounds": 20}) == "incomplete"
    write([{**base, "round": 0, "spec_shortfall": 3.0}, {"event": "interrupted", "round": 1, "error": "DAEMON_TIMEOUT"}])
    assert bench.completeness(tmp_path, {"max_rounds": 20}) == "interrupted"
    assert bench.load_trajectory(tmp_path) == [3.0]


class StubDaemon:
    """模拟后台进程：跟踪当前加载的镜头，记录每次局部优化时实际加载的文件；G 由（出发镜头，轮次）决定。"""

    G = {("rounds/r000.zmx", 1): 5.0, ("rounds/r001.zmx", 2): 3.0, ("rounds/r002.zmx", 3): 0.0}

    def __init__(self, root, loaded):
        self.root, self.loaded, self.opt_from = root, loaded, []

    def request(self, op, args=None, timeout=None):
        import json

        args = args or {}
        if op == "lens.load":
            self.loaded = args["file"]
            return {"file": self.loaded}
        if op == "opt.local":
            self.opt_from.append(self.loaded)
            return {"initial_merit": 1.0, "final_merit": 0.5}
        if op == "round.record":
            recorded = bench.rounds.load(self.root / "rounds.jsonl")
            n = len(recorded)
            g = self.G.get((self.opt_from[-1], n), 9.0)
            rec = {"round": n, "file": f"rounds/r{n:03d}.zmx", "verdict": "pass" if g == 0 else "fail", "spec_shortfall": g, "items": {} if g == 0 else {"osc": g}}
            with (self.root / "rounds.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
            self.loaded = rec["file"]
            return {"record": rec, "evaluation": {"invalid": None}, "decision": {"status": "continue"}}
        return {}


def _stub_case(tmp_path, monkeypatch, bench_lines, round_lines, loaded):
    case = _case_with_history(tmp_path, bench_lines, [])
    (tmp_path / "rounds.jsonl").write_text("".join(__import__("json").dumps(r) + "\n" for r in round_lines), encoding="utf-8")
    daemon = StubDaemon(tmp_path, loaded)
    case.cores, case.spec_name = None, "spec/benchmark.yaml"
    monkeypatch.setattr(case, "req", lambda op, **a: daemon.request(op, a), raising=False)
    monkeypatch.setattr(case, "layout", lambda: {"radii": [1], "gap": 0, "thickness": [1]}, raising=False)
    monkeypatch.setattr(case, "apply_merit", lambda lay, streak: [], raising=False)
    return case, daemon


R0 = {"round": 0, "policy": bench.POLICY, "file": "rounds/r000.zmx", "verdict": "fail", "spec_shortfall": 10.0, "items": {"osc": 10.0}}


def test_resume_after_recovered_round_optimizes_from_recovered_best(tmp_path, monkeypatch):
    # 连续运行
    (tmp_path / "a").mkdir()
    case, daemon = _stub_case(tmp_path / "a", monkeypatch, [R0], [R0], "rounds/r000.zmx")
    continuous = [h["spec_shortfall"] for h in case.run(20)]
    assert daemon.opt_from == ["rounds/r000.zmx", "rounds/r001.zmx", "rounds/r002.zmx"] and continuous == [10.0, 5.0, 3.0, 0.0]

    # 第 1 轮 round.record 已落盘、正式记录未写入时中断；续跑时后台进程里仍是 r000（审阅反例）
    (tmp_path / "b").mkdir()
    r1 = {"round": 1, "file": "rounds/r001.zmx", "verdict": "fail", "spec_shortfall": 5.0, "items": {"osc": 5.0}}
    pending = {"event": "pending", "round": 1, "policy": bench.POLICY, "from": "rounds/r000.zmx", "strategy": {"targeted": {}}}
    case, daemon = _stub_case(tmp_path / "b", monkeypatch, [R0, pending], [R0, r1], "rounds/r000.zmx")
    resumed = [h["spec_shortfall"] for h in case.run(20)]
    assert daemon.opt_from == ["rounds/r001.zmx", "rounds/r002.zmx"]  # 修复前第一项是 r000
    assert resumed == continuous
    froms = [h.get("from") for h in bench.load_history(tmp_path / "b" / "bench.jsonl")][2:]
    assert froms == daemon.opt_from  # 记录的出发镜头就是实际优化的镜头
