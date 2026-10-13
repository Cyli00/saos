from __future__ import annotations

import json

import pytest
import yaml

from zea import acceptance
from zea import calibration as cal
from zea.spec import parse_spec

CODE = "c0de"
MANIFEST = {"batch": "B", "policy": "reference-v3", "code_sha256": CODE, "mode": "standalone", "observe_rounds": 20}
SPEC_TEXT = "spec_version: 1\ncorrection_class: achromat\nmagnification: 20\nna: 0.4\nlimits:\n  working_distance_min: {wd}\n"
CONFIG = acceptance.acceptance_config(parse_spec(yaml.safe_load(SPEC_TEXT.format(wd=10))))
PRESCRIPTION = b'{"title": "p"}'
INPUTS = {"prescription_sha256": cal.hashlib.sha256(PRESCRIPTION).hexdigest(), "benchmark_sha256": "b", "acceptance_config_sha256": acceptance.config_hash(CONFIG)}


def task(name="t1", entry="E1", seed=1, group="tuning", **kw):
    return {"task": name, "group": group, "class": "main", "tier": "default", "entry": entry, "patent": "P" + entry, "seed": seed,
            "radius_rel": 0.01, "thickness_rel": 0.03, "amplify": True, "stretch": {}, "inputs": INPUTS, **kw}


def make_job(root, name, t, traj, *, mode="standalone", code=CODE, frozen=True, lens=b"lens", rounds_override=None, times=True):
    d = root / "jobs" / name
    for sub in ("lenses/rounds", "spec", "sources"):
        (d / sub).mkdir(parents=True)
    (d / "spec" / "benchmark.yaml").write_text(SPEC_TEXT.format(wd=10), encoding="utf-8")
    (d / "sources" / "prescription.json").write_bytes(PRESCRIPTION)
    state = {"bench": {"batch": "B", "task": t["task"], "entry": t["entry"], "seed": t["seed"], "radius_rel": t["radius_rel"], "thickness_rel": t["thickness_rel"],
                       "stretch": t["stretch"], "policy": "reference-v3", "client_code": code}}
    if frozen:
        (d / "spec" / "frozen_config.json").write_text(json.dumps(CONFIG, indent=2), encoding="utf-8")
        state["frozen"] = {"spec": "spec/benchmark.yaml", "config": "spec/frozen_config.json", "sha256": acceptance.config_hash(CONFIG)}
    (d / "job.json").write_text(json.dumps(state), encoding="utf-8")
    bench_lines, round_lines = [], []
    for k, g in enumerate(traj):
        f = f"rounds/r{k:03d}.zmx"
        (d / "lenses" / f).write_bytes(lens if k == 0 else f"{name}-{k}".encode())
        verdict = "pass" if g == 0 else "fail"
        extra = {"opt_s": 10.0, "eval_s": 5.0} if times else {"opt_s": 10.0}
        bench_lines.append({"round": k, "policy": "reference-v3", "spec_shortfall": g, "verdict": verdict, "file": f, "mode": mode, "daemon_code": code, **extra})
        round_lines.append({"round": k, "spec_shortfall": g, "verdict": verdict, "file": f, "config_sha256": state.get("frozen", {}).get("sha256")})
    for k, patch in (rounds_override or {}).items():
        round_lines[k].update(patch)
    (d / "bench.jsonl").write_text("".join(json.dumps(x) + "\n" for x in bench_lines), encoding="utf-8")
    (d / "rounds.jsonl").write_text("".join(json.dumps(x) + "\n" for x in round_lines), encoding="utf-8")
    return d


def sample(name, traj, entry="E1", seed=1):
    passed = next((k for k, g in enumerate(traj) if g == 0), None)
    return {"task": name, "entry": entry, "patent": "P" + entry, "seed": seed, "trajectory": traj, "passed_at": passed, "times": [0.0] + [15.0] * (len(traj) - 1)}


READY = {"complete": True}


# ---- 准入 ----


def test_short_unpassed_trajectory_is_not_eligible_even_if_job_limit_was_small(tmp_path):
    # 审阅反例：任务上限为 5、只观察到第 5 轮且未通过，旧入口把它当作完整轨迹并得出"误停 0"
    t = task()
    a = cal.audit(make_job(tmp_path, "j", t, [10.0, 9, 8, 7, 6, 5]), MANIFEST, t)
    assert not a["eligible"] and any("观察不足" in r for r in a["reasons"])
    assert cal.audit(make_job(tmp_path, "k", t, [10.0] + [5.0] * 20), MANIFEST, t)["eligible"]
    passed = cal.audit(make_job(tmp_path, "m", t, [10.0, 0.0]), MANIFEST, t)
    assert passed["eligible"] and passed["passed_at"] == 1


@pytest.mark.parametrize("kw, reason", [
    ({"mode": "extension"}, "运行模式"),
    ({"code": "other"}, "代码版本"),
    ({"frozen": False}, "没有冻结"),
])
def test_audit_rejects_wrong_mode_code_or_unfrozen(tmp_path, kw, reason):
    t = task()
    a = cal.audit(make_job(tmp_path, "j", t, [10.0, 0.0], **kw), MANIFEST, t)
    assert not a["eligible"] and any(reason in r for r in a["reasons"])


def test_audit_checks_round_results_match_between_logs(tmp_path):
    # 审阅反例：bench.jsonl 写 G = 0 通过，rounds.jsonl 同轮写 G = 9 未通过，旧准入判合格
    t = task()
    d = make_job(tmp_path, "j", t, [10.0, 0.0], rounds_override={1: {"spec_shortfall": 9.0, "verdict": "fail"}})
    a = cal.audit(d, MANIFEST, t)
    assert not a["eligible"] and any("第 1 轮" in r and "不一致" in r for r in a["reasons"])


def test_audit_requires_evidence_files(tmp_path):
    t = task()
    d = make_job(tmp_path, "j", t, [10.0, 0.0])
    (d / "spec" / "benchmark.yaml").unlink()
    (d / "lenses" / "rounds" / "r000.zmx").unlink()
    reasons = cal.audit(d, MANIFEST, t)["reasons"]
    assert "规格文件缺失" in reasons and "第 0 轮的镜头文件缺失" in reasons


def test_audit_verifies_frozen_file_and_manifest_inputs(tmp_path):
    t = task()
    d = make_job(tmp_path, "j", t, [10.0, 0.0])
    (d / "spec" / "frozen_config.json").write_text(json.dumps({**CONFIG, "na": 0.5}), encoding="utf-8")
    assert any("实际哈希" in r for r in cal.audit(d, MANIFEST, t)["reasons"])
    d2 = make_job(tmp_path, "k", t, [10.0, 0.0])
    (d2 / "sources" / "prescription.json").write_bytes(b"changed")
    assert any("处方" in r for r in cal.audit(d2, MANIFEST, t)["reasons"])
    other = task(inputs={**INPUTS, "acceptance_config_sha256": "other"})
    assert any("清单记录" in r for r in cal.audit(make_job(tmp_path, "m", other, [10.0, 0.0]), MANIFEST, other)["reasons"])


def test_unknown_eval_time_is_not_counted_as_zero(tmp_path):
    t = task()
    a = cal.audit(make_job(tmp_path, "j", t, [10.0, 5.0, 0.0], times=False), MANIFEST, t)
    assert a["eligible"] and a["times"] == [0.0, None, None]
    s = {**t, **a}
    c = cal.evaluate([s], 3, 0.01, 20)
    assert c["time_s"] == 0.0 and c["time_unknown_rounds"] == 2


# ---- 收集、去重与冲突 ----


def test_collect_dedups_same_start_and_uses_earliest_job(tmp_path):
    t1, t2 = task("t1"), task("t2", tier="hard")
    make_job(tmp_path, "20261013-100000-a", t1, [10.0, 0.0])
    make_job(tmp_path, "20261013-090000-b", t1, [10.0, 5.0, 0.0])  # 同一任务较早的一次运行为正式运行
    make_job(tmp_path, "20261013-110000-c", t2, [10.0, 5.0, 0.0])  # 起点相同（结构、冻结配置、r000 都相同）
    manifest = {**MANIFEST, "tasks": [t1, t2]}
    got = cal.collect(manifest, [tmp_path])
    assert [s["task"] for s in got["samples"]] == ["t1"] and got["samples"][0]["job"] == "20261013-090000-b"
    assert got["duplicates"] == [{"task": "t2", "same_start_as": "t1"}]
    assert cal.readiness(manifest, got, "tuning", "main")["complete"] is True


def test_same_start_with_different_trajectories_is_a_conflict(tmp_path):
    # 审阅反例：同一起点一条第 1 轮通过、另一条第 10 轮通过，旧去重直接丢掉后者
    t1, t2 = task("t1"), task("t2", tier="hard")
    make_job(tmp_path, "a", t1, [10.0, 0.0])
    make_job(tmp_path, "b", t2, [10.0] * 10 + [0.0])
    manifest = {**MANIFEST, "tasks": [t1, t2]}
    got = cal.collect(manifest, [tmp_path])
    assert got["samples"] == [] and len(got["conflicts"]) == 1
    ready = cal.readiness(manifest, got, "tuning", "main")
    assert ready["complete"] is False and ready["counts"] == {"conflict": 2}


# ---- 选择与验证 ----


def test_constant_failures_do_not_make_evidence_sufficient():
    # 审阅反例：5 条始终为 10 的轨迹，所有候选都零误停，总轮数目标偏向 N=2，但没有检验防误停能力
    out = cal.select([sample(f"s{k}", [10.0] * 21, entry=f"E{k}") for k in range(5)], READY)
    assert out["chosen"]["N"] == 2 and out["status"].startswith("证据不足")
    assert out["categories"] == {"始终无改善": {"samples": 5, "structures": 5, "patents": 5, "seeds": 5, "tasks": ["s0", "s1", "s2", "s3", "s4"]}}


def test_late_success_forces_larger_window_and_refutes_initial():
    late = [10.0, 9.99, 9.98, 9.97, 9.96, 9.95, 0.0]  # 5 轮无明显改善后第 6 轮通过
    out = cal.select([sample("late", late), sample("quick", [10.0, 0.0], entry="E2")], READY)
    assert out["initial_status"].startswith("被否定") and out["initial"]["false_stops"] == ["late"]
    assert out["chosen"]["N"] == 6
    assert out["status"].startswith("找到满足调参准则的候选")
    assert set(out["categories"]) == {"停滞后成功", "快速成功"}


def test_selection_on_incomplete_manifest_is_only_interim():
    out = cal.select([sample("quick", [10.0, 0.0])], {"complete": False})
    assert out["status"].startswith("阶段性统计")
    assert cal.select([], READY)["status"].startswith("阶段性统计")


def test_no_candidate_is_reported_not_relaxed():
    late = [10.0] * 10 + [0.0]  # 第 10 轮通过，前 9 轮完全不动：任何候选（N ≤ 6）都会误停
    out = cal.select([sample("late", late)], READY)
    assert out["status"] == "没有合格候选" and out["chosen"] is None


def test_tie_break_prefers_initial_by_index_distance():
    cands = [{"N": n, "rel": r, "cap": c} for n in cal.GRID_N for r in cal.GRID_REL for c in cal.GRID_CAP]
    ordered = sorted(cands, key=cal.tie_key)
    assert (ordered[0]["N"], ordered[0]["rel"], ordered[0]["cap"]) == cal.INITIAL
    assert (ordered[1]["N"], ordered[1]["rel"], ordered[1]["cap"]) == (4, 0.01, 20)  # 距离相同时取较大 N


def test_validation_requires_complete_manifest_and_no_false_stop():
    # 审阅反例：空验证集、清单 2 个任务只完成 1 个，旧代码都返回"独立验证通过"
    assert cal.validate([], 3, 0.01, 20, READY)["passed"] is None
    partial = cal.validate([sample("quick", [10.0, 0.0])], 3, 0.01, 20, {"complete": False})
    assert partial["passed"] is None and partial["status"].startswith("阶段性统计")
    assert cal.validate([sample("quick", [10.0, 0.0])], 3, 0.01, 20, READY)["passed"] is True
    failed = cal.validate([sample("late", [10.0, 10.0, 10.0, 10.0, 0.0])], 3, 0.01, 20, {"complete": False})
    assert failed["passed"] is False and failed["status"].startswith("所选规则未通过验证")


# ---- 实验清单 ----


def _library(tmp_path, monkeypatch):
    monkeypatch.setenv("ZEA_LIBRARY", str(tmp_path / "lib"))
    from zea import library

    groups = {"tuning": [f"T{k}" for k in range(7)], "validation": [f"V{k}" for k in range(6)]}
    spec = "spec_version: 1\ncorrection_class: achromat\nmagnification: 20\nna: 0.4\nlimits:\n  working_distance_min: 10\n  optical_length_max: 50\n"
    for e in groups["tuning"] + groups["validation"]:
        library.save_entry(e, {"id": e, "status": "admitted", "source": {"patent": "P" + e}, "metrics": {"wd_mech_mm": 12.0, "optical_length_mm": 45.0}})
        library.write_file(e, "prescription.json", {"title": e})
        (library.entry_dir(e) / "benchmark.yaml").write_text(spec, encoding="utf-8")
    (library.library_dir() / "benchmarks.json").write_text(json.dumps({"groups": groups, "perturbation": {"seed": 1, "radius_rel": 0.01, "thickness_rel": 0.03}}), encoding="utf-8")
    return library


def test_plan_counts_task_ids_and_binds_inputs(tmp_path, monkeypatch):
    library = _library(tmp_path, monkeypatch)
    data = cal.plan("b1", "reference-v3")
    count = {}
    for t in data["tasks"]:
        count[(t["group"], t["class"])] = count.get((t["group"], t["class"]), 0) + 1
    assert count == {("tuning", "main"): 35, ("tuning", "stress"): 21, ("validation", "main"): 30, ("validation", "stress"): 18}
    assert len({t["task"] for t in data["tasks"]}) == len(data["tasks"])
    plain = next(t for t in data["tasks"] if t["entry"] == "T0" and not t["stretch"])
    wd = next(t for t in data["tasks"] if t["entry"] == "T0" and t["stretch"] == {"wd": 1.25})
    assert plain["inputs"]["acceptance_config_sha256"] != wd["inputs"]["acceptance_config_sha256"]
    # 库文件被改动后，同一任务的输入指纹不同，运行前核对会发现
    (library.entry_dir("T0") / "benchmark.yaml").write_text("spec_version: 1\ncorrection_class: achromat\nmagnification: 20\nna: 0.4\nlimits:\n  working_distance_min: 11\n", encoding="utf-8")
    assert cal.task_inputs("T0", {}) != plain["inputs"]


def test_run_task_refuses_when_inputs_changed(tmp_path, monkeypatch):
    from zea import bench, config
    from zea.protocol import ZeaError

    library = _library(tmp_path, monkeypatch)
    data = cal.write_plan("b1", bench.POLICY)
    monkeypatch.setattr(bench.client, "request", lambda op, args, timeout: {"mode": "standalone", "code_sha256": config.code_sha256()})
    library.write_file("T0", "prescription.json", {"title": "changed"})
    with pytest.raises(ZeaError) as exc:
        bench.run_task("b1", data["tasks"][0]["task"], 10)
    assert exc.value.details == {"inputs": True}


def test_audit_rederives_config_from_archived_spec(tmp_path):
    # 审阅反例：任务规格改为 999 mm，冻结文件和记录保持原样，旧准入仍放行
    t = task()
    d = make_job(tmp_path, "j", t, [10.0, 0.0])
    assert cal.audit(d, MANIFEST, t)["eligible"]
    (d / "spec" / "benchmark.yaml").write_text(SPEC_TEXT.format(wd=999), encoding="utf-8")
    reasons = cal.audit(d, MANIFEST, t)["reasons"]
    assert "任务规格生成的验收配置与冻结值不同" in reasons and "任务规格生成的验收配置与清单记录的不同" in reasons
    (d / "spec" / "benchmark.yaml").write_text("spec_version: 1\n", encoding="utf-8")
    assert any("无法解析" in r for r in cal.audit(d, MANIFEST, t)["reasons"])
