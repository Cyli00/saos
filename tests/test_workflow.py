from __future__ import annotations

import math
import json
import re

import pytest
import yaml

from saos import config, demand, jobs, ledger, report, workflow
from saos.backend.fake import FakeBackend
from saos import daemon as daemon_mod
from saos.daemon import Daemon
from saos.protocol import ErrorCode, SaosError

LINE = demand.QUESTION_LINE["zh"]
LENGTH = {"id": "length", "description": "total track", "check": {"operand": {"type": "TOTR"}}, "origin": "user", "must": {"max": 95.0}, "wish": {"max": 90.0}, "scale": 5.0, "source": "user"}
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
        {**DEMAND, "summary": " "},                                          # 总结不能为空
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
    daemon = Daemon(backend, backend.start(), "extension")
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
    glass = next(s for s in daemon.backend.lens["surfaces"] if s.get("material"))
    glass["material_source"] = {"kind": "catalog", "name": glass["material"], "catalogs": ["FAKE"], "status": "Obsolete"}
    r1 = call(daemon, "round.record", job=job.id, tier="local", note="shorter")["result"]
    assert r1["record"]["achieved"] and r1["decision"]["status"] == "achieved"
    assert [m["status"] for m in r1["record"]["materials"] if m["kind"] == "catalog"] == ["Obsolete"]  # 目录状态随轮记录
    assert call(daemon, "round.record", job=job.id, tier="local")["error"]["code"] == ErrorCode.BAD_ARGS  # 已停止
    assert (job.root / "lenses" / "rounds" / "r001.zmx").is_file() and (job.root / r1["record"]["evaluation"]).is_file()
    out = report.write(job, lang="zh")
    html = (job.root / "report" / "index.html").read_text(encoding="utf-8")
    assert out["status"] == "achieved" and out["deliver"] == "rounds/r001.zmx" and out["lang"] == "zh"
    assert "达成" in html and "总长不超过 95 mm" in html and "TOTR" in html and "<svg" in html and LINE in html
    assert "确认，开始吧" in html and "a.zmx" in html and "期望 ≤ 90" in html and "Obsolete（停产" in html
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
    # 新窗口从对最好一轮（r0）的重测开始，本轮试了 local 仍没有进步 / the new window starts at the re-measured r0; local again gave nothing
    assert changed["rebase"]["rebase_of"] == 0 and not changed["rebase"]["equivalent"] and changed["record"]["round"] == 3
    assert changed["decision"]["status"] == "exhausted" and changed["decision"]["last_gain_round"] == 2
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
    assert report.build(job)["deliver"]["round"] == 2  # r1 是对 r0 的重测 / r1 re-measures r0


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
    daemon = Daemon(backend, backend.start(), "extension")
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
    daemon.backend.optimize = lambda cycles, cores, **kw: {"merit": 1.0}  # 假后端没有优化器 / the fake backend has no optimiser
    assert call(daemon, "opt.local", job=job.id)["ok"]
    result = call(daemon, "round.record", job=job.id)["result"]
    assert result["brief"]["open_questions"] == ["染料？"] and "reminder" not in result["brief"]  # 没有证据就不提醒


def test_broken_constant_or_variable_range_is_never_delivered(setup):
    """常量、变量范围是硬条件：G 再小也不能交付。"""
    daemon, job = setup
    hard = {**DEMAND, "variables": [{"id": "track_cap", "description": "track must stay under 99", "check": {"operand": {"type": "TOTR"}},
                                     "range": {"max": 99.0}, "source": "user"}],
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


def test_stop_needs_the_users_words_and_ends_the_rounds(setup):
    daemon, job = setup
    write_demand(job, DEMAND)
    with pytest.raises(SaosError):
        ledger.record_stop(job.root, demand.digest(demand.validate(DEMAND)), "no real focus", "")  # 没有轮次、没有原话
    call(daemon, "round.record", job=job.id)
    sha = demand.digest(demand.validate(DEMAND))
    with pytest.raises(SaosError):
        ledger.record_stop(job.root, sha, "no real focus", " ")
    stop = ledger.record_stop(job.root, sha, "F20: no real focus even on axis", "c：保持现结构")
    assert stop["round"] == 0
    daemon.backend.lens["surfaces"][1]["radius"] = 45.0
    refused = call(daemon, "round.record", job=job.id, tier="local")["error"]
    assert refused["code"] == ErrorCode.BAD_ARGS and refused["details"]["status"] == "stopped"
    report.write(job, lang="zh")
    html = (job.root / "report" / "index.html").read_text(encoding="utf-8")
    assert "按用户决定停止" in html and "F20: no real focus even on axis" in html and "c：保持现结构" in html
    # 新版本需求重新开始，停止不再适用 / a new demand version starts afresh
    write_demand(job, demand_with({**LENGTH, "must": {"max": 99.0}}), confirm="改成 99")
    assert call(daemon, "round.record", job=job.id, tier="local")["ok"]


def test_report_says_no_valid_design_and_draws_infinity_apart(setup):
    daemon, job = setup
    (job.root / "checks").mkdir()
    (job.root / "checks" / "virtual.py").write_text("def run(ctx):\n    return float('inf')\n", encoding="utf-8")
    write_demand(job, demand_with({**LENGTH, "check": {"script": "checks/virtual.py"}}))
    call(daemon, "round.record", job=job.id)
    data = report.build(job)
    assert data["deliver"]["shortfall"] == math.inf
    html = report.render(job, data, "zh")
    assert "没有有效的设计" in html and "目前最好的镜头" not in html
    svg = report.chart(rounds_of([math.inf, 4.0, 2.0], ["baseline", "local", "local"]), report.Tr("en"))
    assert "∞: no valid value" in svg and 'class="dot inf"' in svg
    # ∞ 的点在顶部带里，比最大有限值高 / the ∞ dot sits in the top band, above the largest finite value
    ys = [float(v) for v in re.findall(r'cy="([0-9.]+)"', svg)]
    assert ys[0] < ys[1] < ys[2]


def test_environment_surfaces_do_not_block_delivery(setup):
    """建模说明中声明为环境的面用模型介质，不阻止交付；要制造的零件用模型玻璃仍然阻止。"""
    daemon, job = setup
    write_demand(job, demand_with({**LENGTH, "must": {"max": 120.0}}))
    daemon.backend.lens["surfaces"][2]["material_source"] = {"kind": "model", "nd": 1.36, "vd": 50.0}
    notes = {**NOTES, "environment": [2]}
    folder = daemon.handle({"op": "lens.describe", "args": {"job": job.id, "notes": notes}})["result"]
    assert any("外购件" in w or "bought-in" in w for w in folder["warnings"])
    assert call(daemon, "lens.approve", job=job.id, model=folder["folder"], quote="样品介质按模型")["ok"]
    record = call(daemon, "round.record", job=job.id)["result"]["record"]
    assert record["model_glass"] == [] and record["environment"] == [2] and ledger.deliverable(record)
    bad = daemon.handle({"op": "lens.describe", "args": {"job": job.id, "notes": {**NOTES, "environment": [99]}}})
    assert bad["error"]["code"] == ErrorCode.BAD_ARGS


def test_confirmation_keeps_what_the_user_saw(setup):
    daemon, job = setup
    write_demand(job, DEMAND)
    record = demand.confirmations(job.root)[-1]
    assert "content" in record and "总长不超过 95 mm" in record["content"] and LINE in record["content"]


def test_agent_reported_merit_is_marked(setup):
    daemon, job = setup
    write_demand(job, DEMAND)
    r0 = call(daemon, "round.record", job=job.id)["result"]["record"]
    assert "merit_source" not in r0
    daemon.backend.lens["surfaces"][1]["radius"] = 45.0
    r1 = call(daemon, "round.record", job=job.id, tier="local", merit=0.25)["result"]["record"]
    assert r1["merit"] == 0.25 and r1["merit_source"] == "agent"
    report.write(job, lang="zh")
    assert "（agent 报告）" in (job.root / "report" / "index.html").read_text(encoding="utf-8")


def test_long_operations_report_progress_and_honour_cancel(setup, monkeypatch):
    """长操作写 progress.json；旧的取消请求在开始时清掉；脚本和优化都能收到取消（scan 的取消见 test_kit_parallel）。"""
    daemon, job = setup
    write_demand(job, DEMAND)
    job.request_cancel()  # 上一次留下的请求，不该影响新操作 / a stale request must not affect a new operation
    assert call(daemon, "round.record", job=job.id)["ok"]
    progress = job.read_progress()
    assert progress["op"] == "round.record" and progress["status"] == "done" and not job.cancel_requested()

    source = "def run(ctx):\n    first = ctx.progress('step', i=1)\n    ctx.job.request_cancel()\n    return [first, ctx.progress('step', i=2)]\n"
    assert call(daemon, "script.exec", job=job.id, name="steps", source=source, args={})["result"]["result"] == [False, True]
    assert job.read_progress()["data"] == {"i": 2} and job.read_progress()["status"] == "done"

    seen = {}

    def optimize(cycles, cores, *, max_seconds=None, on_progress=None, should_cancel=None):
        on_progress({"progress": 10, "merit": 2.0, "elapsed_s": 0.5})
        # 运行中的视图只有几何 / the mid-run view has geometry only
        seen["mid_view"] = json.loads((job.root / jobs.VIEW_FILE).read_text(encoding="utf-8"))["layout"]
        job.request_cancel()
        seen.update(cores=cores, max_seconds=max_seconds, cancel=should_cancel())
        return {"stopped_by": "cancel"}

    daemon.backend.optimize = optimize
    monkeypatch.setattr(daemon_mod, "VIEW_DURING_OPT_S", 0.0)
    assert call(daemon, "opt.local", job=job.id, max_seconds=30)["result"]["stopped_by"] == "cancel"
    mid = seen.pop("mid_view")
    assert mid["rays"] == [] and mid["rays_paused"]
    assert seen == {"cores": config.compute_cores(), "max_seconds": 30, "cancel": True}
    final = json.loads((job.root / jobs.VIEW_FILE).read_text(encoding="utf-8"))["layout"]
    assert final["rays"] and "rays_paused" not in final  # 结束后带光线 / with rays once it ends
    assert job.read_progress()["merit"] == 2.0


def test_hammer_and_global_need_a_budget_and_archive_the_systems_they_write(setup):
    """Hammer / 全局搜索：必须给 max_seconds，过需求单检查；OpticStudio 写在镜头旁的系统移到 lenses/<kind>/NNN/。"""
    daemon, job = setup
    lens_dir = job.lenses_dir
    seen = {}

    def search(kind, cores, *, max_seconds, on_progress=None, should_cancel=None):
        on_progress({"progress": 0, "merit": 0.5, "systems": 1000, "elapsed_s": 1.0})
        names = [f"GLOPT_0001_{k:03d}.zmx" for k in (1, 2)] if kind == "global" else ["a_0001_HAMMER.zmx"]
        for name in names:
            (lens_dir / name).write_text("lens", encoding="utf-8")
        seen.update(kind=kind, cores=cores, max_seconds=max_seconds)
        return {"final_merit": 0.5, "stopped_by": "timeout"}

    daemon.backend.optimize_search = search
    write_demand(job, DEMAND, confirm=None)
    assert call(daemon, "opt.global", job=job.id, max_seconds=5)["error"]["code"] == ErrorCode.DEMAND_NOT_CONFIRMED
    demand.confirm(job.root, "确认，开始吧")
    assert call(daemon, "opt.global", job=job.id)["error"]["code"] == ErrorCode.BAD_ARGS  # 没有预算 / no budget
    assert call(daemon, "opt.hammer", job=job.id, max_seconds=0)["error"]["code"] == ErrorCode.BAD_ARGS
    # 镜头旁已有 GLOPT 文件（如镜头取自 lenses/global/NNN/）会被同名覆盖：拒绝 / same-named GLOPT files would be overwritten
    (lens_dir / "GLOPT_0001_009.zmx").write_text("old", encoding="utf-8")
    assert call(daemon, "opt.global", job=job.id, max_seconds=5)["error"]["code"] == ErrorCode.BAD_ARGS and seen == {}
    (lens_dir / "GLOPT_0001_009.zmx").unlink()

    out = call(daemon, "opt.global", job=job.id, max_seconds=5)["result"]
    assert out["files"] == ["global/001/GLOPT_0001_001.zmx", "global/001/GLOPT_0001_002.zmx"] and out["dirty"]
    assert seen == {"kind": "global", "cores": config.compute_cores(), "max_seconds": 5.0}
    assert not list(lens_dir.glob("GLOPT_*.zmx"))
    progress = job.read_progress()
    assert progress["op"] == "opt.global" and progress["stage"] == "opt.global" and progress["systems"] == 1000
    assert progress["stopped_by"] == "timeout" and progress["status"] == "done"

    (lens_dir / "a_0009_HAMMER.zmx").write_text("old", encoding="utf-8")  # 运行前就有的不动 / left as it was
    assert call(daemon, "opt.hammer", job=job.id, max_seconds=5)["result"]["files"] == ["hammer/001/a_0001_HAMMER.zmx"]
    assert (lens_dir / "a_0009_HAMMER.zmx").read_text(encoding="utf-8") == "old"
    assert call(daemon, "opt.global", job=job.id, max_seconds=5)["result"]["files"][0].startswith("global/002/")



def test_job_show_and_cancel_work_without_the_daemon(saos_home):
    from saos import cli

    job = jobs.create_job("watch")
    job.write_progress(op="script.exec", status="running", stage="search 3/10")
    shown = cli.cmd_job(cli.build_parser().parse_args(["job", "show", job.id]))
    assert shown["progress"]["stage"] == "search 3/10" and shown["cancel_requested"] is False
    assert cli.cmd_job(cli.build_parser().parse_args(["job", "cancel", job.id]))["cancel_requested"] and job.cancel_requested()


def test_bounds_tolerate_float_noise():
    """9.999999999999954 µm 满足 ≥ 10 µm；真正的不足仍然判为不达标。"""
    item = {"goal": {"min": 10.0}}
    assert demand.shortfall(item, 9.999999999999954) == 0.0
    assert demand.shortfall(item, 9.99) == pytest.approx(0.001)
    assert demand.shortfall({"goal": {"max": 0.0}, "scale": 3.7}, 1e-12) == 0.0


def test_modelling_runs_in_standalone_and_the_live_view_shows_interference(saos_home, tmp_path):
    """建模不再要求 extension：standalone 下改动镜头后写 view.json（布局、Lens Data、交叠检查），建模说明列出交叠。"""
    from saos.backend.fake import fake_surface

    fixed = {"radius": "Fixed", "thickness": "Fixed", "material": "Fixed", "semi_diameter": "Fixed", "conic": "Fixed"}
    rows = [fake_surface(0, thickness=float("inf")), fake_surface(1, radius=50.0, thickness=5.0, material="N-BK7", semi_diameter=10.0, is_stop=True, solves=fixed),
            fake_surface(2, radius=-50.0, thickness=2.0, semi_diameter=10.0, solves=fixed),
            fake_surface(3, radius=80.0, thickness=4.0, material="N-BK7", semi_diameter=6.0, solves=fixed),
            fake_surface(4, radius=-80.0, thickness=80.0, semi_diameter=6.0, solves=fixed), fake_surface(5, semi_diameter=1.0)]
    source = tmp_path / "two.zmx"
    source.write_text(json.dumps({"title": "two lenses", "surfaces": rows}), encoding="utf-8")
    backend = FakeBackend()
    daemon = Daemon(backend, backend.start(), "standalone")
    job = jobs.create_job("background")
    write_demand(job, DEMAND)
    assert call(daemon, "lens.import", job=job.id, src=str(source), name="a.zmx")["ok"]
    assert call(daemon, "lens.load", job=job.id, file="a.zmx")["ok"]
    view = json.loads((job.root / jobs.VIEW_FILE).read_text(encoding="utf-8"))
    assert view["ok"] and view["lens"] == "a.zmx" and len(view["lens_data"]) == 6 and view["interference"]["overlaps"] == []
    first = view["seq"]
    # 负的空气间隔把第二片镜片塞进第一片 / a negative air gap pushes the second lens into the first
    assert call(daemon, "lens.set", job=job.id, surface=2, param="thickness", value=-3.0)["ok"]
    view = json.loads((job.root / jobs.VIEW_FILE).read_text(encoding="utf-8"))
    assert view["seq"] > first and view["dirty"] and [o["lenses"] for o in view["interference"]["overlaps"]] == [[[1, 2], [3, 4]]]
    out = call(daemon, "lens.describe", job=job.id, notes=NOTES)
    assert out["ok"], out
    assert any("interpenetrate" in w for w in out["result"]["warnings"])
    assert call(daemon, "lens.approve", job=job.id, model=out["result"]["folder"], quote="好")["ok"]


def test_modelling_keeps_lens_data_and_layout_open(saos_home, fake_lens_file, tmp_path):
    """建模阶段每次改动镜头后都要打开 Lens Data 和布局窗口；剖面图画不了的系统改用 3D 视图；模型确认后只刷新。"""
    backend = FakeBackend()
    daemon = Daemon(backend, backend.start(), "extension")
    job = jobs.create_job("windows")
    write_demand(job, DEMAND)
    folded = tmp_path / "folded.zmx"
    folded.write_text(json.dumps({**json.loads(fake_lens_file.read_text(encoding="utf-8")), "non_axial": True}), encoding="utf-8")
    call(daemon, "lens.import", job=job.id, src=str(fake_lens_file), name="a.zmx")
    call(daemon, "lens.import", job=job.id, src=str(folded), name="folded.zmx")
    assert call(daemon, "lens.load", job=job.id, file="a.zmx")["ok"]
    assert backend.shown[-1] == {"lens_data": True, "viewer": "cross-section", "fallback_reason": None, "analysis": None}
    assert call(daemon, "lens.load", job=job.id, file="folded.zmx")["ok"]
    assert backend.shown[-1]["viewer"] == "3d" and backend.shown[-1]["fallback_reason"] == "non-axial system"
    view = call(daemon, "lens.view", job=job.id, analysis="FakeCurve")["result"]
    assert view["layout"]["viewer"] == "3d" and view["watch"]["analysis"] == "FakeCurve"
    call(daemon, "lens.load", job=job.id, file="a.zmx")
    approve_model(daemon, job)
    shown = len(backend.shown)
    assert call(daemon, "lens.set", job=job.id, surface=1, param="radius", value=60.0)["ok"]
    assert len(backend.shown) == shown  # 模型确认后不再强制打开窗口


def test_rounds_may_use_several_tiers_and_tiers_can_be_ruled_out():
    """一轮可以同时用几个层级；带证据排除的层级算作处理过，但只到下一次明显进步为止。"""
    rows = rounds_of([10, 5, 5], ["baseline", "local", "variables"])
    rows[2]["tiers"] = ["variables", "materials"]
    d = ledger.decide(rows, STOP)
    assert d["tiers_tried_since_gain"] == ["variables", "materials"] and d["next_tier"] == "local"
    ruled = [{"round": 2, "demand_sha": "a", "tiers": ["global", "structure"], "reason": "probes/001"}]
    rows = rounds_of([10, 5, 5, 5], ["baseline", "local", "local", "variables"])
    rows[3]["tiers"] = ["variables", "materials"]
    d = ledger.decide(rows, STOP, ruled=ruled)
    assert d["status"] == "exhausted" and d["tiers_ruled_out_since_gain"] == ["global", "structure"]
    # 排除记在最后一次进步之前就不再有效 / a rule-out made before the last gain no longer holds
    rows = rounds_of([10, 9, 5, 5], ["baseline", "local", "local", "variables"])
    d = ledger.decide(rows, STOP, ruled=[{**ruled[0], "round": 1}])
    assert d["last_gain_round"] == 2 and d["tiers_ruled_out_since_gain"] == [] and d["status"] == "continue"


def test_rule_out_needs_a_reason_and_feeds_the_round_decision(setup):
    daemon, job = setup
    write_demand(job, DEMAND)
    sha = demand.digest(demand.load_job(job.root))
    with pytest.raises(SaosError):
        ledger.record_rule_out(job.root, sha, ["structure"], "probes/001")  # 还没有轮次 / no round yet
    assert call(daemon, "round.record", job=job.id)["ok"]
    with pytest.raises(SaosError):
        ledger.record_rule_out(job.root, sha, ["structure"], " ")
    with pytest.raises(SaosError):
        ledger.record_rule_out(job.root, sha, ["fancy"], "x")
    ledger.record_rule_out(job.root, sha, ["materials", "global", "structure"], "probes/001: no variable moves the target")
    call(daemon, "lens.set", job=job.id, surface=1, param="radius", value=60.0)
    out = call(daemon, "round.record", job=job.id, tier="local,variables")["result"]
    assert out["record"]["tiers"] == ["local", "variables"] and out["record"]["tier"] == "local"
    # 改半径不改变假后端的 TOTR，没有进步，排除的层级算作处理过 / the radius does not change the fake TOTR: no gain, the rule-out holds
    d = out["decision"]
    assert d["last_gain_round"] == 0 and d["tiers_ruled_out_since_gain"] == ["materials", "global", "structure"] and d["status"] == "exhausted"
    assert call(daemon, "round.record", job=job.id, tier="local,bogus")["error"]["code"] == ErrorCode.BAD_ARGS


def test_surface_parameters_and_materials_can_vary(setup):
    """面型参数 par<N>（非球面系数、倾斜等）可以读写、设变量、做扰动；材料可以设为模型玻璃或目录内替换。"""
    daemon, job = setup
    daemon.backend.lens["surfaces"][1]["par4"] = 0.0
    out = call(daemon, "lens.set", job=job.id, surface=1, param="par4", value=1e-5)["result"]
    assert out["before"] == 0.0 and out["after"] == 1e-5
    assert call(daemon, "lens.set", job=job.id, surface=2, param="par4", value=1.0)["error"]["code"] == ErrorCode.BAD_ARGS  # 该面没有这一列
    assert call(daemon, "lens.set", job=job.id, surface=1, param="parx", value=1.0)["error"]["code"] == ErrorCode.BAD_ARGS
    assert call(daemon, "opt.vary", job=job.id, param="par4", surfaces=[1])["result"]["solves"] == {1: "Variable"}
    solves = call(daemon, "opt.vary", job=job.id, param="material", surfaces=[1], mode="model")["result"]["solves"]
    assert list(solves.values()) == ["MaterialModel"]
    assert call(daemon, "opt.vary", job=job.id, param="material", surfaces=[1])["error"]["code"] == ErrorCode.BAD_ARGS  # 缺 mode
    assert call(daemon, "opt.vary", job=job.id, param="material", surfaces=[2], mode="substitute")["error"]["code"] == ErrorCode.BAD_ARGS  # 空气面
    assert call(daemon, "opt.vary", job=job.id, param="semi_diameter", surfaces=[1])["error"]["code"] == ErrorCode.BAD_ARGS


def test_demand_notes_are_free_and_ranges_can_cite_a_source(saos_home):
    """notes 不进版本；变量范围和目标可以写来源（例如教材表格）。"""
    job = jobs.create_job("notes")
    data = {**DEMAND, "variables": [{"id": "edge", "description": "min edge thickness of lens 1 (mm)", "range": {"min": 0.6},
                                    "check": {"operand": {"type": "TOTR"}}, "source": "李晓彤 表17-2, 6–10 mm lenses"}]}
    write_demand(job, data, confirm=None)
    sha = demand.digest(demand.load_job(job.root))
    write_demand(job, {**data, "notes": ["mounting method still open; see sources/mount.md"]}, confirm=None)
    assert demand.digest(demand.load_job(job.root)) == sha and demand.show(job.root)["notes"]
    write_demand(job, {**data, "notes": "one string"}, confirm=None)
    with pytest.raises(SaosError):
        demand.load_job(job.root)


def test_confirm_needs_a_source_for_every_limit_and_keeps_the_weak_ones(saos_home):
    """每个界限都要有来源；只是 agent 假设的界限在 demand show 中列出，并随确认留档。"""
    job = jobs.create_job("basis")
    guess = {**LENGTH, "id": "guess", "origin": "agent", "weight": 0.3, "source": "agent assumption: typical tube length"}
    write_demand(job, demand_with(LENGTH, {k: v for k, v in guess.items() if k != "source"}), confirm=None)
    with pytest.raises(SaosError) as exc:
        demand.confirm(job.root, "好")
    assert exc.value.details["missing_source"] == ["guess"]
    write_demand(job, demand_with(LENGTH, guess), confirm=None)
    assert [w["id"] for w in demand.show(job.root)["weak_basis"]] == ["guess"]
    assert demand.confirm(job.root, "好")["demand_sha"] and demand.confirmations(job.root)[-1]["weak_basis"][0]["id"] == "guess"
    write_demand(job, {**demand_with(LENGTH, guess), "summary": "不再需要固定结尾。"}, confirm=None)  # 总结不再要求固定句


def test_doubts_come_from_evidence_not_every_round(setup):
    """没有证据时 brief 不提醒；硬条件连续两轮不满足、目标连续两轮无有效值、未达标界限只是假设时提出质疑。"""
    daemon, job = setup
    (job.root / "checks").mkdir()
    (job.root / "checks" / "virtual.py").write_text("def run(ctx):\n    return float('inf')\n", encoding="utf-8")
    focus = {**LENGTH, "id": "focus", "check": {"script": "checks/virtual.py"}, "origin": "agent", "weight": 0.3, "source": "agent assumption: guess"}
    hard = {**DEMAND, "variables": [{"id": "track_cap", "description": "track under 99", "check": {"operand": {"type": "TOTR"}},
                                     "range": {"max": 99.0}, "source": "user"}],
            "objectives": [{**LENGTH, "must": {"max": 120.0}}, focus]}
    write_demand(job, hard)
    first = call(daemon, "round.record", job=job.id)["result"]["brief"]
    # 需求是中文的，给 agent 的提醒仍是英文 / a Chinese demand still gets the English reminder
    assert any("focus fails and its limit rests on agent assumption" in x for x in first["doubts"])
    assert first["reminder"] == demand.QUESTION_LINE["en"]
    call(daemon, "lens.set", job=job.id, surface=1, param="radius", value=60.0)
    second = call(daemon, "round.record", job=job.id, tier="local")["result"]["brief"]
    assert any(x.startswith("focus has had no valid value for 2 rounds") for x in second["doubts"])
    assert any(x.startswith("track_cap has not been met in any of the 2 rounds") for x in second["doubts"])
    clean = workflow.brief(demand.validate(DEMAND), {"items": [], "hard_failing": []}, {"status": "continue", "next_tier": "local"}, [])
    assert clean["doubts"] == [] and "reminder" not in clean


WORK = """
def work(wctx, item):
    if item.get("boom"):
        raise ValueError("bad item")
    wctx.backend.set_surface(1, "thickness", item["t"])
    return {"t": item["t"], "totr": wctx.backend.operand("TOTR", 0, 0, []), "worker": wctx.worker}
"""


def test_parallel_runs_items_in_several_workers_from_the_saved_lens(setup):
    """并发评估：每个条目从保存的镜头副本出发，结果按条目顺序返回，出错的条目单独记录，结果封存。"""
    daemon, job = setup
    (job.root / "explore").mkdir()
    (job.root / "explore" / "work.py").write_text(WORK, encoding="utf-8")
    run = "def run(ctx):\n    return ctx.map('explore/work.py:work', [{'t': 5.0}, {'t': 6.0}, {'boom': True}, {'t': 7.0}], workers=2)\n"
    out = call(daemon, "script.exec", job=job.id, name="scan", source=run, args={})
    assert out["ok"], out
    res = out["result"]["result"]
    totr = [r["totr"] if r else None for r in res["results"]]
    # 起点 TOTR = 5 + 95；每个条目只改第 1 面厚度，互不累积 / each item changes only surface 1 from the same start
    assert totr == [100.0, 101.0, None, 102.0] and "bad item" in res["errors"]["2"] and res["workers_available"] == 2
    assert {r["worker"] for r in res["results"] if r} <= {0, 1} and job.verify_seals() == []
    assert (job.root / res["folder"] / "result.json").is_file() and (job.root / res["folder"] / "work.py").is_file()
    # 有未保存修改时拒绝，worker 只能加载已保存的文件 / refused with unsaved changes: workers load the saved file
    call(daemon, "lens.set", job=job.id, surface=1, param="radius", value=60.0)
    assert call(daemon, "script.exec", job=job.id, name="scan", source=run, args={})["error"]["code"] == ErrorCode.UNSAVED_CHANGES


def test_check_only_measures_the_named_items_and_never_achieves(setup):
    daemon, job = setup
    other = {**LENGTH, "id": "length2", "must": {"max": 999.0}}
    write_demand(job, demand_with(LENGTH, other))
    full = call(daemon, "demand.check", job=job.id)["result"]
    assert full["partial"] is False and [i["id"] for i in full["items"]] == ["length", "length2"]
    assert all(i["duration_s"] >= 0 for i in full["items"])
    part = call(daemon, "demand.check", job=job.id, only=["length2"])["result"]
    assert part["partial"] is True and part["achieved"] is False
    assert [i["id"] for i in part["items"]] == ["length2"] and part["items"][0]["passed"] is True
    bad = call(daemon, "demand.check", job=job.id, only=["nope"])
    assert bad["ok"] is False and bad["error"]["code"] == ErrorCode.BAD_ARGS


def test_changed_checks_re_measure_the_best_round_first(setup):
    """判定代码改了：先用新判定重测此前最好的一轮。数值不变则窗口延续；变了就从重测记录开新窗口，旧的最好镜头仍可交付。"""
    daemon, job = setup
    checks = job.root / "checks"
    checks.mkdir(exist_ok=True)
    (checks / "length.py").write_text("def run(ctx):\n    return ctx.backend.operand('TOTR', 0, 0, [])\n", encoding="utf-8")
    write_demand(job, demand_with({**LENGTH, "check": {"script": "checks/length.py"}}))
    call(daemon, "round.record", job=job.id, note="start")                     # r0: TOTR 100
    call(daemon, "lens.set", job=job.id, surface=1, param="thickness", value=3.0)
    call(daemon, "lens.save", job=job.id)
    assert call(daemon, "round.record", job=job.id, tier="local")["ok"]         # r1: TOTR 98，最好 / best
    # 改写成 kit 的写法，数值相同 / rewritten with kit, same numbers
    (checks / "length.py").write_text("def run(ctx):\n    return ctx.op('TOTR')\n", encoding="utf-8")
    call(daemon, "lens.set", job=job.id, surface=1, param="thickness", value=4.0)
    call(daemon, "lens.save", job=job.id)
    out = call(daemon, "round.record", job=job.id, tier="variables")["result"]
    assert out["rebase"] == {"round": 2, "rebase_of": 1, "equivalent": True, "diffs": {}, "shortfall": out["rebase"]["shortfall"],
                             "evaluation": out["rebase"]["evaluation"]}
    assert out["record"]["round"] == 3 and out["record"]["file"] == "rounds/r003.zmx"
    assert out["decision"]["rounds_in_version"] == 2 and out["decision"]["last_gain_round"] == 1  # 同一窗口 / same window
    evaluation = json.loads((job.root / out["rebase"]["evaluation"]).read_text(encoding="utf-8"))
    assert evaluation["lens"]["file"] == "rounds/r001.zmx" and "src/saos/kit.py" in evaluation["saos_code_files"]
    assert daemon.backend.surfaces()[1]["thickness"] == 4.0 and job.verify_seals() == []  # 当前镜头回来了 / the current lens is back
    rows = ledger.load(job.root / ledger.LEDGER_FILE)
    assert [r["tier"] for r in rows] == ["baseline", "local", "rebase", "variables"] and rows[2]["file"] == "rounds/r001.zmx"
    # 口径变了：重测数值不同，从重测记录开新窗口 / the measurement changed: a new window starts at the rebase record
    (checks / "length.py").write_text("def run(ctx):\n    return ctx.op('TOTR') + 1.0\n", encoding="utf-8")
    call(daemon, "lens.set", job=job.id, surface=1, param="thickness", value=2.0)
    call(daemon, "lens.save", job=job.id)
    out = call(daemon, "round.record", job=job.id, tier="local")["result"]
    assert not out["rebase"]["equivalent"] and out["rebase"]["rebase_of"] == 1
    assert out["rebase"]["diffs"]["length"] == {"recorded": 98.0, "now": 99.0}
    assert out["decision"]["rounds_in_version"] == 1 and out["decision"]["last_gain_round"] == 5
    window = ledger.current_window(ledger.load(job.root / ledger.LEDGER_FILE))
    assert [r["round"] for r in window] == [4, 5] and window[0]["file"] == "rounds/r001.zmx"
    report.write(job, lang="zh")
    assert "新判定重测" in (job.root / "report" / "index.html").read_text(encoding="utf-8")


def test_saos_code_used_by_checks_joins_the_method_version(setup):
    """判定用到 kit 或配方库时，它们的源码哈希进入判定版本，并随证据留副本；没用到时版本不变。"""
    daemon, job = setup
    checks = job.root / "checks"
    checks.mkdir(exist_ok=True)
    (checks / "length.py").write_text("def run(ctx):\n    return ctx.backend.operand('TOTR', 0, 0, [])\n", encoding="utf-8")
    d = demand.validate(demand_with({**LENGTH, "check": {"script": "checks/length.py"}}))
    plain = demand.prepare_scripts(job.root, d)
    assert demand.CODE_KEY not in plain
    (checks / "helper.py").write_text("from saos.recipes import fiber  # noqa\n", encoding="utf-8")
    with_recipe = demand.prepare_scripts(job.root, d)
    files = with_recipe[demand.CODE_KEY]["files"]
    assert "src/saos/kit.py" in files and any(f.startswith("src/saos/recipes/fiber/") for f in files)
    assert demand.method_digest(with_recipe) != demand.method_digest(plain)
