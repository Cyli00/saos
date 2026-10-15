from __future__ import annotations

import math
import json

import pytest
import yaml

from saos import demand, jobs, ledger, report
from saos.backend.fake import FakeBackend
from saos.daemon import Daemon
from saos.protocol import ErrorCode, SaosError

LINE = demand.QUESTION_LINE["zh"]
LENGTH = {"id": "length", "description": "total track", "check": {"operand": {"type": "TOTR"}}, "origin": "user", "must": {"max": 95.0}, "wish": {"max": 90.0}, "scale": 5.0}
DEMAND = {
    "schema": 2,
    "constants": [{"id": "lens", "description": "the sample singlet", "value": "a.zmx", "source": "user"}],
    "variables": [{"id": "radii", "description": "both radii are free"}],
    "objectives": [LENGTH],
    "summary": f"用户要总长不超过 95 mm，越短越好。{LINE}",
}


def demand_with(*objectives, **top):
    return {**DEMAND, "objectives": list(objectives), **top}


def rounds_of(gs, tiers):
    return [{"round": k, "shortfall": g, "tier": t, "achieved": g == 0, "demand_sha": "a"} for k, (g, t) in enumerate(zip(gs, tiers))]


STOP = {"rel": 0.01, "max_rounds": 30, "tiers": list(demand.TIERS)}


def test_shortfall_and_validation():
    t = {"goal": {"equals": 50.0, "tol": 0.5}}
    assert demand.shortfall({**t}, 50.4) == 0 and demand.shortfall(t, 51.5) == pytest.approx(2.0)
    assert demand.shortfall({"goal": {"min": 0.3}, "scale": 0.1}, 0.25) == pytest.approx(0.5)
    assert demand.shortfall({"goal": {"max": 1.0}}, None) == math.inf
    demand.validate(DEMAND)
    bad = [
        demand_with({k: v for k, v in LENGTH.items() if k != "scale"} | {"must": {"max": 0.0}}),  # 界限为 0 时必须给尺度
        {**DEMAND, "summary": "没有最后那句话"},
        {**DEMAND, "stop": {"tiers": ["magic"]}},
        {**DEMAND, "tissue": {"source": "paper"}},                          # 不收需求以外的字段
        {**DEMAND, "request": "用户原话"},
        demand_with({**LENGTH, "goal": {"max": 1.0}}),                      # 目标用 must / wish，不用 goal
        demand_with({k: v for k, v in LENGTH.items() if k != "check"}),     # 目标必须能测
        {**DEMAND, "constants": [{"id": "na", "description": "fiber NA", "value": 0.5}]},             # 常量要有来源
        {**DEMAND, "variables": [{"id": "n", "description": "lens index", "range": {"max": 1.6}}]},  # 范围要能测
        {**DEMAND, "variables": [{"id": "length", "description": "same id as an objective"}]},
        {**DEMAND, "objectives": []},
        {**DEMAND, "open_questions": [{"question": "脑区？", "blocking": "yes"}]},
        demand_with({k: v for k, v in LENGTH.items() if k != "origin"}),   # 目标要写明来源
        demand_with({**LENGTH, "weight": 0}),
        demand_with({k: v for k, v in LENGTH.items() if k not in ("must", "scale")} | {"weight": 2.0}),  # 没有达标线就没有权重
        demand_with(LENGTH, {**LENGTH, "id": "extra", "origin": "agent", "weight": 1.5}),                # agent 的目标不能比用户的重
    ]
    for data in bad:
        with pytest.raises(SaosError):
            demand.validate(data)


def test_digest_covers_meaning_not_wording():
    """总结、待确认问题和语言不改变需求版本；三类条目变了才变。"""
    base = demand.digest(demand.validate(DEMAND))
    reworded = {**DEMAND, "summary": f"换个说法。{LINE}", "lang": "en", "open_questions": [{"question": "染料？", "blocking": False}]}
    assert demand.digest(demand.validate(reworded)) == base
    assert demand.digest(demand.validate(demand_with({**LENGTH, "must": {"max": 99.0}}))) != base
    assert demand.digest(demand.validate({**DEMAND, "constants": [{**DEMAND["constants"][0], "source": "datasheet"}]})) != base


def test_stop_rule_requires_escalation_before_exhausted():
    tiers = ["baseline", "local", "local", "local", "local", "local", "local"]
    d = ledger.decide(rounds_of([10, 5, 5, 5, 5, 5, 5], tiers), STOP)
    # 只在同一层级上停滞，不算无法再优化，建议升级到放开变量
    assert d["status"] == "continue" and d["next_tier"] == "variables" and d["rounds_without_gain"] == 5
    tiers = ["baseline", "local", "local", "variables", "materials", "global", "structure"]
    d = ledger.decide(rounds_of([10, 5, 5, 5, 5, 5, 5], tiers), STOP)
    assert d["status"] == "exhausted" and d["untried_tiers"] == []
    # 升级后又有明显进步：重新计数，不判停
    d = ledger.decide(rounds_of([10, 5, 5, 5, 5, 3, 3], tiers), STOP)
    assert d["status"] == "continue" and d["last_gain_round"] == 5
    # 需求只允许部分层级时，用完这些层级即可判停
    d = ledger.decide(rounds_of([10, 5, 5, 5], ["baseline", "local", "local", "variables"]), {**STOP, "tiers": ["local", "variables"]})
    assert d["status"] == "exhausted"


def test_stop_rule_achieved_limit_and_demand_change():
    assert ledger.decide(rounds_of([10, 0], ["baseline", "local"]), STOP)["status"] == "achieved"
    assert ledger.decide(rounds_of([10, 9, 8], ["baseline", "local", "local"]), {**STOP, "max_rounds": 2})["status"] == "limit"
    assert ledger.decide(rounds_of([math.inf, math.inf, 5], ["baseline", "local", "local"]), STOP)["last_gain_round"] == 2
    rows = rounds_of([10, 5, 5, 5, 5, 5, 5], ["baseline", "local", "variables", "materials", "global", "structure", "local"])
    rows[-1]["demand_sha"] = "b"  # 需求改过：从改动后重新算
    assert ledger.decide(rows, STOP)["status"] == "continue"


@pytest.fixture
def setup(saos_home, fake_lens_file):
    backend = FakeBackend()
    daemon = Daemon(backend, backend.start(), "standalone")
    job = jobs.create_job("flow")
    daemon.handle({"op": "lens.import", "args": {"job": job.id, "src": str(fake_lens_file), "name": "a.zmx"}})
    daemon.handle({"op": "lens.load", "args": {"job": job.id, "file": "a.zmx"}})
    approve_model(daemon, job)
    return daemon, job


def approve_model(daemon, job, quote="模型可以"):
    """建模说明并记录确认 / describe the model and record the user's approval."""
    folder = daemon.handle({"op": "lens.describe", "args": {"job": job.id, "notes": NOTES}})["result"]["folder"]
    assert daemon.handle({"op": "lens.approve", "args": {"job": job.id, "model": folder, "quote": quote}})["ok"]
    return folder


NOTES = {"surfaces": {"0": "object", "1": "lens front", "2": "lens back", "3": "image"}, "assumptions": ["test"], "first_order": ["f = 50 mm"]}


def write_demand(job, data, confirm="确认，开始吧"):
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    if confirm:
        demand.confirm(job.root, confirm)


def call(daemon, op, **args):
    return daemon.handle({"op": op, "args": args})


def test_workflow_rounds_until_achieved_and_report(setup):
    daemon, job = setup
    assert call(daemon, "demand.check", job=job.id)["error"]["code"] == ErrorCode.FILE_NOT_FOUND
    write_demand(job, DEMAND, confirm=None)
    check = call(daemon, "demand.check", job=job.id)["result"]  # 框定阶段可以先取值
    assert check["items"][0]["value"] == 100.0 and check["shortfall"] == pytest.approx(1.0) and not check["achieved"]
    assert not check["items"][0]["wish_met"]
    assert call(daemon, "round.record", job=job.id)["error"]["code"] == ErrorCode.DEMAND_NOT_CONFIRMED  # 未确认不能记录轮次
    assert call(daemon, "opt.local", job=job.id)["error"]["code"] == ErrorCode.DEMAND_NOT_CONFIRMED  # 也不能开始优化
    demand.confirm(job.root, "确认，开始吧")
    r0 = call(daemon, "round.record", job=job.id, note="start")["result"]
    assert r0["record"]["tier"] == "baseline" and r0["decision"]["status"] == "continue"
    assert call(daemon, "round.record", job=job.id, tier="magic")["error"]["code"] == ErrorCode.BAD_ARGS
    call(daemon, "lens.set", job=job.id, surface=2, param="thickness", value=85.0)  # 由 MarginalRayHeight 求解覆盖
    daemon.backend.lens["surfaces"][2]["thickness"] = 85.0
    r1 = call(daemon, "round.record", job=job.id, tier="local", note="shorter")["result"]
    assert r1["record"]["achieved"] and r1["decision"]["status"] == "achieved"
    assert call(daemon, "round.record", job=job.id, tier="local")["error"]["code"] == ErrorCode.BAD_ARGS  # 已停止
    assert (job.root / "lenses" / "rounds" / "r001.zmx").is_file() and (job.root / r1["record"]["evaluation"]).is_file()
    out = report.write(job, lang="zh")
    html = (job.root / "report" / "index.html").read_text(encoding="utf-8")
    assert out["status"] == "achieved" and out["deliver"] == "rounds/r001.zmx" and out["lang"] == "zh"
    assert "达成" in html and "总长不超过 95 mm" in html and "TOTR" in html and "<svg" in html and LINE in html
    assert "确认，开始吧" in html and "a.zmx" in html and "期望 ≤ 90" in html
    # 语言由需求的 lang 决定，--lang 优先；切换语言不改变需求的哈希，不需要重新确认
    write_demand(job, {**DEMAND, "lang": "en"}, confirm=None)
    assert report.write(job)["lang"] == "en"
    html = (job.root / "report" / "index.html").read_text(encoding="utf-8")
    assert 'lang="en"' in html and "Achieved" in html and "达成" not in html and "总长不超过 95 mm" in html
    assert report.write(job, lang="zh")["lang"] == "zh"


def test_demand_change_needs_new_confirmation(setup):
    daemon, job = setup
    write_demand(job, DEMAND)
    call(daemon, "round.record", job=job.id)
    changed = demand_with({**LENGTH, "must": {"max": 99.0}})
    write_demand(job, changed, confirm=None)
    refused = call(daemon, "round.record", job=job.id, tier="local")["error"]
    assert refused["code"] == ErrorCode.DEMAND_NOT_CONFIRMED and refused["details"]["demand_sha"] == demand.digest(demand.validate(changed))
    # 只改写总结不需要重新确认 / rewording the summary needs no new confirmation
    write_demand(job, {**changed, "summary": f"放宽到 99 mm。{LINE}"}, confirm="改成 99 mm 也可以")
    assert call(daemon, "round.record", job=job.id, tier="local")["ok"]
    write_demand(job, {**changed, "summary": f"换个说法。{LINE}"}, confirm=None)
    daemon.backend.lens["surfaces"][1]["radius"] = 45.0
    assert call(daemon, "round.record", job=job.id, tier="local")["ok"]


@pytest.mark.parametrize("bound", ["min", "max", "equals"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, "50", None])
def test_invalid_goal_cannot_pass(bound, value):
    target = {**LENGTH, "must": {bound: value, "tol": 0.5}, "scale": 5}
    with pytest.raises(SaosError) as exc:
        demand.validate(demand_with(target))
    assert exc.value.code == ErrorCode.BAD_ARGS


def test_report_never_reuses_an_old_requirement_version(setup):
    daemon, job = setup
    loose = demand_with({**LENGTH, "must": {"max": 110}})
    write_demand(job, loose)
    assert call(daemon, "round.record", job=job.id)["result"]["record"]["achieved"]
    tight = demand_with({**LENGTH, "must": {"max": 50}})
    write_demand(job, tight, confirm="改成50")
    with pytest.raises(SaosError) as exc:
        report.build(job)
    assert exc.value.code == ErrorCode.SPEC_CHANGED
    assert call(daemon, "round.record", job=job.id, tier="local")["ok"]
    data = report.build(job)
    assert data["deliver"]["round"] == 1 and not data["items"][0]["passed"]
    assert data["items"][0]["goal"] == {"max": 50}
    assert [r["round"] for r in data["rounds"]] == [1]


def test_analysis_evidence_survives_later_rounds_and_checks(setup):
    daemon, job = setup
    target = {**LENGTH, "check": {"analysis": {"type": "FakeCurve", "path": "series[0].y.v[2]"}}}
    write_demand(job, demand_with(target))
    first = call(daemon, "round.record", job=job.id)["result"]["evaluation"]
    text_path = job.root / first["items"][0]["evidence"]["text"]
    original = text_path.read_bytes()
    daemon.backend.lens["surfaces"][1]["thickness"] = 105
    second = call(daemon, "round.record", job=job.id, tier="local")["result"]["evaluation"]
    checked = call(daemon, "demand.check", job=job.id)["result"]
    paths = {item["items"][0]["evidence"]["text"] for item in (first, second, checked)}
    assert len(paths) == 3 and text_path.read_bytes() == original
    assert json.loads(text_path.with_suffix(".json").read_text())["series"][0]["y"]["v"][-1] == 100
    data = report.build(job)
    assert data["deliver"]["round"] == 0 and data["items"][0]["value"] == 100


@pytest.mark.parametrize("check", [
    {"analysis": {"type": "FakeCurve", "path": "missing"}},
    {"analysis": {"type": "NoSuchAnalysis", "path": "missing"}},
    {"script": "checks/missing.py"},
    {"script": "checks/broken.py"},
])
def test_failed_measurement_still_generates_report(setup, check):
    daemon, job = setup
    (job.root / "checks").mkdir()
    (job.root / "checks" / "broken.py").write_text("def run(ctx):\n    raise RuntimeError('<失败>')\n", encoding="utf-8")
    write_demand(job, demand_with({**LENGTH, "check": check}))
    result = call(daemon, "round.record", job=job.id)["result"]
    assert result["record"]["shortfall"] == math.inf and not result["record"]["achieved"]
    # 判定执行出错：本轮无效，不是设计结论 / the check failed to run: an invalid round, not a design result
    assert result["record"]["measurement_errors"] == ["length"] and "fix them" in result["decision"]["reason"]
    data = report.build(job)
    for lang in ("zh", "en"):
        html = report.render(job, data, lang)
        assert "无法取值" in html if lang == "zh" else "no value" in html
        assert "<失败>" not in html
    assert report.write(job)["status"] == "continue"


def test_script_versions_reset_comparison_and_preserve_source(setup):
    daemon, job = setup
    (job.root / "checks").mkdir()
    script = job.root / "checks" / "check.py"
    source = "def run(ctx):\n    return ctx.backend.operand('TOTR', 0, 0, [])\n"
    script.write_text(source, encoding="utf-8")
    write_demand(job, demand_with({**LENGTH, "check": {"script": "checks/check.py"}},
                        stop={"tiers": ["local"]}))
    first = call(daemon, "round.record", job=job.id)["result"]
    # 同一镜头再记一轮被拒绝；改一个不影响总长的半径后再记 / the same lens again is refused; change a radius that leaves the track
    assert call(daemon, "round.record", job=job.id, tier="local")["error"]["code"] == ErrorCode.DUPLICATE_ROUND
    daemon.backend.lens["surfaces"][1]["radius"] = 45.0
    assert call(daemon, "round.record", job=job.id, tier="local")["result"]["decision"]["status"] == "exhausted"
    script.write_text(source.replace("[])", "[]) + 5"), encoding="utf-8")
    with pytest.raises(SaosError):
        report.build(job)
    changed = call(daemon, "round.record", job=job.id, tier="local")["result"]
    assert changed["record"]["demand_sha"] == first["record"]["demand_sha"]
    assert changed["record"]["method_sha"] != first["record"]["method_sha"]
    assert changed["decision"]["status"] == "continue" and changed["decision"]["last_gain_round"] == 2
    assert report.build(job)["deliver"]["round"] == 2
    archived = job.root / first["evaluation"]["items"][0]["evidence"]["script"]
    assert archived.read_text(encoding="utf-8") == source


def test_report_refuses_missing_or_changed_evidence(setup):
    daemon, job = setup
    write_demand(job, DEMAND)
    record = call(daemon, "round.record", job=job.id)["result"]["record"]
    (job.root / record["evaluation"]).unlink()
    with pytest.raises(SaosError) as exc:
        report.build(job)
    assert exc.value.code == ErrorCode.EVIDENCE_TAMPERED
    assert {"path": record["evaluation"], "problem": "missing"} in exc.value.details["problems"]


def test_legacy_stopped_round_can_be_remeasured_without_reconfirmation(setup):
    daemon, job = setup
    h = demand_with({**LENGTH, "must": {"max": 110}})
    write_demand(job, h)
    first = call(daemon, "round.record", job=job.id)["result"]["record"]
    legacy = {k: v for k, v in first.items() if k != "method_sha"}
    (job.root / ledger.LEDGER_FILE).write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    with pytest.raises(SaosError):
        report.build(job)
    assert call(daemon, "round.record", job=job.id, tier="local")["result"]["record"]["achieved"]
    assert report.build(job)["deliver"]["round"] == 1


def test_evidence_names_cannot_collide_on_windows(setup):
    daemon, job = setup
    target = {**LENGTH, "check": {"analysis": {"type": "FakeCurve", "path": "series[0].y.v[2]"}}}
    write_demand(job, demand_with(*({**target, "id": name} for name in ("result", "Result"))))
    evaluation = call(daemon, "round.record", job=job.id)["result"]["evaluation"]
    paths = [(job.root / item["evidence"]["text"]).with_suffix(".json") for item in evaluation["items"]]
    assert len(set(paths)) == 2
    assert all(json.loads(p.read_text())["series"][0]["y"]["v"][-1] == 100 for p in paths)
    assert len(report.build(job)["items"]) == 2


def test_connection_loss_during_check_does_not_record_an_infinite_round(setup, monkeypatch):
    daemon, job = setup
    write_demand(job, DEMAND)

    def disconnected(*args):
        daemon.backend.started = False
        raise RuntimeError("连接中断")

    monkeypatch.setattr(daemon.backend, "operand", disconnected)
    response = call(daemon, "round.record", job=job.id)
    assert response["error"]["code"] == ErrorCode.ZOS_CONNECTION_LOST
    assert not ledger.load(job.root / ledger.LEDGER_FILE)


def test_rounds_need_an_approved_model_matching_the_structure(saos_home, fake_lens_file):
    """模型确认关卡 / the model gate."""
    backend = FakeBackend()
    daemon = Daemon(backend, backend.start(), "standalone")
    job = jobs.create_job("gate")
    daemon.handle({"op": "lens.import", "args": {"job": job.id, "src": str(fake_lens_file), "name": "a.zmx"}})
    daemon.handle({"op": "lens.load", "args": {"job": job.id, "file": "a.zmx"}})
    write_demand(job, DEMAND)
    refused = call(daemon, "round.record", job=job.id)["error"]
    assert refused["code"] == ErrorCode.MODEL_NOT_APPROVED and "latest_approved" not in refused["details"]
    bare = daemon.handle({"op": "lens.describe", "args": {"job": job.id}})["result"]["folder"]
    assert call(daemon, "lens.approve", job=job.id, model=bare, quote="ok")["error"]["code"] == ErrorCode.FILE_NOT_FOUND  # 没有 notes
    folder = daemon.handle({"op": "lens.describe", "args": {"job": job.id, "notes": NOTES}})["result"]["folder"]
    assert (job.root / "notes" / f"model-{folder.split('/')[1][:3]}.json").is_file()
    assert call(daemon, "lens.approve", job=job.id, model=folder, quote=" ")["error"]["code"] == ErrorCode.BAD_ARGS
    assert call(daemon, "lens.approve", job=job.id, model="lenses", quote="ok")["error"]["code"] == ErrorCode.BAD_ARGS
    approval = call(daemon, "lens.approve", job=job.id, model=folder, quote="可以")["result"]
    assert approval["quote"] == "可以" and (job.root / folder / "approval.json").is_file()
    assert call(daemon, "round.record", job=job.id)["result"]["record"]["model"] == folder
    # 换真实材料、改半径不需要重新确认 / swapping a real material or a radius needs no new approval
    call(daemon, "lens.set", job=job.id, surface=1, param="material", value="SF2")
    call(daemon, "lens.set", job=job.id, surface=1, param="radius", value=40.0)
    assert call(daemon, "round.record", job=job.id, tier="local")["ok"]
    # 改面型（结构）要重新确认 / changing a surface type is a structural change
    daemon.backend.lens["surfaces"][1]["type"] = "Even Asphere"
    refused = call(daemon, "round.record", job=job.id, tier="structure")["error"]
    assert refused["code"] == ErrorCode.MODEL_NOT_APPROVED and refused["details"]["latest_approved"] == folder
    assert any(c.startswith("surfaces[1].type") for c in refused["details"]["changed"])
    approve_model(daemon, job, "加非球面可以")
    assert call(daemon, "round.record", job=job.id, tier="structure")["ok"]


def test_model_glass_is_never_achieved_or_delivered():
    """含模型玻璃：指标达标也只算继续，报告不交付 / a model glass never counts as achieved or delivered."""
    rounds = rounds_of([1.0, 0.0], ["baseline", "local"])
    rounds[1]["model_glass"] = [3]
    d = ledger.decide(rounds, STOP)
    assert d["status"] == "continue" and d["next_tier"] == "materials" and "[3]" in d["reason"]
    rounds.append({"round": 2, "shortfall": 0.0, "tier": "materials", "achieved": True, "demand_sha": "a", "model_glass": []})
    assert ledger.decide(rounds, STOP)["status"] == "achieved"
    assert ledger.deliverable(rounds[2]) and not ledger.deliverable(rounds[1])


def test_report_marks_exploration_only_rounds(setup):
    daemon, job = setup
    write_demand(job, DEMAND)
    call(daemon, "round.record", job=job.id)
    # 手工把这一轮标成含模型玻璃，模拟只做了探索 / mark the round as using a model glass
    path = job.root / ledger.LEDGER_FILE
    rows = ledger.load(path)
    rows[0]["model_glass"] = [1]
    rows[0]["materials"] = [{"surface": 1, "kind": "model", "nd": 1.6, "vd": 50.0, "dpgf": 0.0, "n": [1.6]}]
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    report.write(job, lang="en")
    html = (job.root / "report" / "index.html").read_text(encoding="utf-8")
    assert "No deliverable lens" in html and "model glass nd 1.6" in html


def test_round_archive_is_write_once_and_sealed(setup):
    """归档镜头只由工具写一次，工作文件仍是当前镜头；封存能发现改动。
    Round archives are written once by the tools, the working file stays current, and seals detect edits."""
    daemon, job = setup
    write_demand(job, DEMAND)
    rec = call(daemon, "round.record", job=job.id)["result"]["record"]
    assert rec["file"] == "rounds/r000.zmx"
    assert daemon.loaded.path == job.lenses_dir / "a.zmx"  # 回到工作文件 / back on the working file
    saved = call(daemon, "lens.save", job=job.id)["result"]
    assert saved["file"] == "a.zmx"
    assert call(daemon, "lens.save", job=job.id, file="rounds/r000.zmx", overwrite=True)["error"]["code"] == ErrorCode.BAD_ARGS
    assert call(daemon, "lens.save", job=job.id, file="captures/x.zmx")["error"]["code"] == ErrorCode.BAD_ARGS
    assert job.verify_seals() == []
    (job.root / rec["evaluation"]).write_text("{}", encoding="utf-8")
    assert job.verify_seals() == [{"path": rec["evaluation"], "problem": "changed"}]
    (job.lenses_dir / "rounds" / "r000.zmx").unlink()
    assert {"path": "lenses/rounds/r000.zmx", "problem": "missing"} in job.verify_seals()


def test_check_that_changes_the_lens_blocks_the_round(setup):
    """判定改了镜头：check 给出提示，round 拒绝且不留轮次。 / A check that edits the lens: check warns, round refuses."""
    daemon, job = setup
    (job.root / "checks").mkdir()
    (job.root / "checks" / "edit.py").write_text(
        "def run(ctx):\n    ctx.backend.lens['surfaces'][1]['radius'] += 1.0\n    return ctx.backend.operand('TOTR', 0, 0, [])\n", encoding="utf-8")
    write_demand(job, demand_with({**LENGTH, "check": {"script": "checks/edit.py"}}))
    assert call(daemon, "demand.check", job=job.id)["result"]["lens_modified_by_checks"]
    refused = call(daemon, "round.record", job=job.id)["error"]
    assert refused["code"] == ErrorCode.LENS_MODIFIED_BY_CHECK and any("radius" in c for c in refused["details"]["changed"])
    assert not ledger.load(job.root / ledger.LEDGER_FILE)
    assert not (job.lenses_dir / "rounds" / "r000.zmx").exists()


def test_support_files_are_versioned_and_copied(setup):
    """checks/ 下的辅助文件计入判定版本并随每次判定留副本。 / Helper files under checks/ are versioned and copied."""
    daemon, job = setup
    (job.root / "checks").mkdir()
    (job.root / "checks" / "limits.json").write_text('{"extra": 0}', encoding="utf-8")
    (job.root / "checks" / "track.py").write_text(
        "import json, pathlib\n"
        "def run(ctx):\n"
        "    extra = json.loads(pathlib.Path(__file__).with_name('limits.json').read_text())['extra']\n"
        "    return ctx.backend.operand('TOTR', 0, 0, []) + extra\n", encoding="utf-8")
    write_demand(job, demand_with({**LENGTH, "check": {"script": "checks/track.py"}}))
    first = call(daemon, "round.record", job=job.id)["result"]
    folder = job.root / first["evaluation"]["evidence_dir"]
    assert (folder / "support" / "checks" / "limits.json").is_file()
    assert set(first["evaluation"]["support_files"]) == {"checks/limits.json", "checks/track.py"}
    assert "saos_code_sha256" in first["evaluation"]["environment"]
    (job.root / "checks" / "limits.json").write_text('{"extra": 1}', encoding="utf-8")
    second = call(daemon, "round.record", job=job.id, tier="local")["result"]  # 同一镜头，但判定版本变了 / same lens, new method
    assert second["record"]["method_sha"] != first["record"]["method_sha"]


def test_leftover_archive_is_set_aside_not_overwritten(setup):
    daemon, job = setup
    write_demand(job, DEMAND)
    leftover = job.lenses_dir / "rounds" / "r000.zmx"
    leftover.parent.mkdir(parents=True)
    leftover.write_text("residue of a failed attempt", encoding="utf-8")
    call(daemon, "round.record", job=job.id)
    moved = list((job.lenses_dir / "rounds" / "orphaned").glob("r000-*.zmx"))
    assert len(moved) == 1 and moved[0].read_text(encoding="utf-8") == "residue of a failed attempt"


def test_invalid_rounds_neither_count_as_tried_nor_as_progress():
    rounds = rounds_of([5.0, 5.0], ["baseline", "local"])
    rounds[1]["measurement_errors"] = ["length"]
    d = ledger.decide(rounds, {**STOP, "tiers": ["local"]})
    assert d["status"] == "continue" and d["untried_tiers"] == ["local"] and "fix them" in d["reason"]
    # 轮次上限按需求版本计 / the round budget counts per demand version
    many = rounds_of([5.0] * 4, ["baseline", "local", "local", "local"])
    for r in many[2:]:
        r["demand_sha"] = "b"
    assert ledger.decide(many, {**STOP, "max_rounds": 2})["status"] != "limit"
    assert ledger.decide(many + [{**many[-1], "round": 4}], {**STOP, "max_rounds": 2})["status"] == "limit"


def test_blocking_question_stops_rounds_and_optimisation(setup):
    """blocking 的待确认问题：不记录轮次、不跑优化；非阻塞问题不拦。"""
    daemon, job = setup
    asked = {**DEMAND, "open_questions": [{"question": "目标脑区是哪里？", "blocking": True}, {"question": "染料？", "blocking": False}]}
    write_demand(job, asked)
    for op in ("round.record", "opt.local"):
        refused = call(daemon, op, job=job.id)["error"]
        assert refused["code"] == ErrorCode.DEMAND_BLOCKED and refused["details"]["blocking_questions"] == ["目标脑区是哪里？"]
    # 问题不改变需求版本：用户回答后去掉 blocking，原确认仍有效
    write_demand(job, {**asked, "open_questions": asked["open_questions"][1:]}, confirm=None)
    daemon.backend.optimize = lambda cycles, cores: {"merit": 1.0}  # 假后端没有优化器 / the fake backend has no optimiser
    assert call(daemon, "opt.local", job=job.id)["ok"]
    result = call(daemon, "round.record", job=job.id)["result"]
    assert result["brief"]["open_questions"] == ["染料？"] and result["brief"]["reminder"] == LINE


def test_broken_constant_or_variable_range_is_never_delivered(setup):
    """常量、变量范围是硬条件：G 再小也不能交付。"""
    daemon, job = setup
    hard = {**DEMAND, "variables": [{"id": "track_cap", "description": "track must stay under 99", "check": {"operand": {"type": "TOTR"}},
                                     "range": {"max": 99.0}}],
            "objectives": [{**LENGTH, "must": {"max": 120.0}}]}
    write_demand(job, hard)
    result = call(daemon, "round.record", job=job.id)["result"]
    assert result["record"]["hard_failing"] == ["track_cap"] and not result["record"]["achieved"]
    assert result["brief"]["hard_failing"] == ["track_cap"]
    assert not ledger.deliverable(result["record"])
    report.write(job, lang="zh")
    html = (job.root / "report" / "index.html").read_text(encoding="utf-8")
    assert "不满足常量或变量范围（track_cap）" in html and "变量范围" in html


def test_nothing_with_a_pass_line_is_never_achieved(setup):
    daemon, job = setup
    write_demand(job, demand_with({k: v for k, v in LENGTH.items() if k not in ("must", "scale")}))
    result = call(daemon, "round.record", job=job.id)["result"]
    assert result["record"]["shortfall"] == 0 and not result["record"]["achieved"]


def test_brief_asks_for_review_when_rounds_stall(setup):
    """连续两轮没有明显进步时，提要要求先审视需求再加码。"""
    daemon, job = setup
    write_demand(job, DEMAND)
    r0 = call(daemon, "round.record", job=job.id)["result"]
    assert r0["brief"]["review"] is None and r0["brief"]["objectives"][0]["id"] == "length"
    for radius in (45.0, 46.0):
        daemon.backend.lens["surfaces"][1]["radius"] = radius
        brief = call(daemon, "round.record", job=job.id, tier="local")["result"]["brief"]
    assert "2 rounds without a clear gain" in brief["review"] and "ask the user" in brief["review"]


def test_old_harness_file_is_refused_with_a_pointer(setup):
    daemon, job = setup
    (job.root / "sources" / "harness.yaml").write_text("schema: 1\n", encoding="utf-8")
    refused = call(daemon, "demand.check", job=job.id)["error"]
    assert refused["code"] == ErrorCode.FILE_NOT_FOUND and "demand.yaml" in refused["message"]


def test_demand_show_and_confirm(saos_home):
    job = jobs.create_job("cli")
    write_demand(job, {**DEMAND, "open_questions": [{"question": "脑区？", "blocking": True}]}, confirm=None)
    shown = demand.show(job.root)
    assert shown["confirmed"] is None and shown["blocking_questions"] == ["脑区？"] and shown["checks"] == ["length"]
    with pytest.raises(SaosError):
        demand.confirm(job.root, "  ")
    confirmed = demand.confirm(job.root, "可以")
    assert confirmed["demand_sha"] == shown["demand_sha"] and confirmed["blocking_questions"] == ["脑区？"]
    assert demand.show(job.root)["confirmed"]["quote"] == "可以"


def test_agent_objectives_weigh_less_in_the_shortfall(setup):
    """同样超出达标线，agent 补充的目标对 G 的贡献按默认权重 0.3 计，用户的按 1 计。"""
    daemon, job = setup
    agent = {**LENGTH, "id": "agent_track", "origin": "agent", "must": {"max": 95.0}}
    write_demand(job, demand_with({**LENGTH, "must": {"max": 120.0}}, agent))
    record = call(daemon, "round.record", job=job.id)["result"]["record"]
    assert record["shortfall"] == pytest.approx(0.3 * 1.0)  # (100 - 95) / 5 × 0.3
    write_demand(job, demand_with({**LENGTH, "weight": 2.0}, agent))
    daemon.backend.lens["surfaces"][1]["radius"] = 45.0
    result = call(daemon, "round.record", job=job.id, tier="local")["result"]
    assert result["record"]["shortfall"] == pytest.approx(2.0 * 1.0 + 0.3 * 1.0)
    assert {o["id"]: o["weight"] for o in result["brief"]["objectives"]} == {"length": 2.0, "agent_track": 0.3}
    report.write(job, lang="zh")
    html = (job.root / "report" / "index.html").read_text(encoding="utf-8")
    assert "用户，权重 2" in html and "agent 补充，权重 0.3" in html


def probe_plan(*probes, **kw):
    return {"target": "length", "mechanism": "the track is the sum of the thicknesses", "compensate": "quick_focus", "probes": list(probes), **kw}


T1 = {"id": "t1", "surface": 1, "param": "thickness", "delta": 2.0, "prediction": "longer first gap, longer track", "expect": {"length": "up"}}
R1 = {"id": "r1", "surface": 1, "param": "radius", "delta": 1.0, "control": True, "prediction": "radius does not enter the track", "expect": {"length": "none"}}


def test_probe_ranks_variables_compares_predictions_and_leaves_the_lens(setup):
    daemon, job = setup
    write_demand(job, DEMAND, confirm=None)  # 诊断不需要确认 / diagnosis needs no confirmation
    before = json.dumps(daemon.backend.surfaces(), sort_keys=True)
    wrong = {**T1, "id": "wrong", "delta": 1.0, "expect": {"length": "down"}}
    result = call(daemon, "probe.run", job=job.id, plan=probe_plan(T1, R1, wrong))["result"]
    assert [r["id"] for r in result["ranking"]] == ["t1", "wrong"] and result["ranking"][0]["effect"] == pytest.approx(0.4)
    rows = {r["id"]: r for r in result["probes"]}
    assert rows["t1"]["observed"]["length"] == "up" and not rows["t1"]["mismatch"]
    assert rows["r1"]["observed"]["length"] == "none"
    assert rows["wrong"]["mismatch"] == {"length": {"expected": "down", "observed": "up"}}
    assert rows["t1"]["plus"]["values"]["length"] == pytest.approx(102.0) and rows["t1"]["plus"]["compensation"]["surface"] == 2
    # 假后端的 quick_focus 不动，提醒补偿没起作用 / the fake quick_focus never moves, so the review says nothing was compensated
    assert len(result["review"]) == 2 and "wrong" in result["review"][0] and "nothing was compensated" in result["review"][1]
    assert result["lens_unchanged"] and json.dumps(daemon.backend.surfaces(), sort_keys=True) == before
    folder = job.root / result["folder"]
    assert (folder / "plan.json").is_file() and (folder / "result.json").is_file() and job.verify_seals() == []


def test_probe_flags_a_target_no_variable_can_move(setup):
    daemon, job = setup
    write_demand(job, DEMAND, confirm=None)
    r2 = {**R1, "id": "r2", "control": False, "expect": {}}
    review = call(daemon, "probe.run", job=job.id, plan=probe_plan(r2, R1))["result"]["review"]
    assert any("structure question" in line for line in review)


@pytest.mark.parametrize("plan", [
    probe_plan(T1),                                                  # 没有阴性对照
    probe_plan({**T1, "prediction": ""}, R1),                        # 没有预测
    probe_plan(T1, R1, target="nothing"),
    probe_plan(T1, R1, mechanism=" "),
    probe_plan({**T1, "param": "material"}, R1),
    probe_plan({**T1, "delta": 0}, R1),
    probe_plan(T1, R1, compensate="refocus"),
    probe_plan(*({**T1, "id": f"p{k}"} for k in range(6)), R1),      # 最多 6 个
])
def test_probe_plan_is_checked(setup, plan):
    daemon, job = setup
    write_demand(job, DEMAND, confirm=None)
    assert call(daemon, "probe.run", job=job.id, plan=plan)["error"]["code"] == ErrorCode.BAD_ARGS
