from __future__ import annotations

import math
import json

import pytest
import yaml

from saos import harness, jobs, ledger, report
from saos.backend.fake import FakeBackend
from saos.daemon import Daemon
from saos.protocol import ErrorCode, SaosError

HARNESS = {
    "schema": 1,
    "request": "总长不超过 95 mm，越短越好",
    "targets": [
        {"id": "length", "kind": "constraint", "description": "total track", "check": {"operand": {"type": "TOTR"}}, "goal": {"max": 95.0}, "scale": 5.0},
        {"id": "short", "kind": "preference", "check": {"operand": {"type": "TOTR"}}},
    ],
    "assumptions": ["only the track length matters here"],
    "confirmed": {"quote": "确认，开始吧"},
}


def rounds_of(gs, tiers):
    return [{"round": k, "shortfall": g, "tier": t, "achieved": g == 0, "harness_sha": "a"} for k, (g, t) in enumerate(zip(gs, tiers))]


STOP = {"rel": 0.01, "max_rounds": 30, "tiers": list(harness.TIERS)}


def test_shortfall_and_validation():
    t = {"goal": {"equals": 50.0, "tol": 0.5}}
    assert harness.shortfall({**t}, 50.4) == 0 and harness.shortfall(t, 51.5) == pytest.approx(2.0)
    assert harness.shortfall({"goal": {"min": 0.3}, "scale": 0.1}, 0.25) == pytest.approx(0.5)
    assert harness.shortfall({"goal": {"max": 1.0}}, None) == math.inf
    with pytest.raises(SaosError):
        harness.validate({**HARNESS, "targets": [{"id": "x", "kind": "target", "check": {"operand": {"type": "EFFL"}}, "goal": {"max": 0.0}}]})
    with pytest.raises(SaosError):
        harness.validate({**HARNESS, "request": ""})
    with pytest.raises(SaosError):
        harness.validate({**HARNESS, "stop": {"tiers": ["magic"]}})
    assert harness.digest(HARNESS) == harness.digest({**HARNESS, "confirmed": {"quote": "另一次确认"}})


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
    # 需求框定只允许部分层级时，用完这些层级即可判停
    d = ledger.decide(rounds_of([10, 5, 5, 5], ["baseline", "local", "local", "variables"]), {**STOP, "tiers": ["local", "variables"]})
    assert d["status"] == "exhausted"


def test_stop_rule_achieved_limit_and_harness_change():
    assert ledger.decide(rounds_of([10, 0], ["baseline", "local"]), STOP)["status"] == "achieved"
    assert ledger.decide(rounds_of([10, 9, 8], ["baseline", "local", "local"]), {**STOP, "max_rounds": 2})["status"] == "limit"
    assert ledger.decide(rounds_of([math.inf, math.inf, 5], ["baseline", "local", "local"]), STOP)["last_gain_round"] == 2
    rows = rounds_of([10, 5, 5, 5, 5, 5, 5], ["baseline", "local", "variables", "materials", "global", "structure", "local"])
    rows[-1]["harness_sha"] = "b"  # 需求框定改过：从改动后重新算
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


def write_harness(job, data):
    (job.root / "sources" / "harness.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def call(daemon, op, **args):
    return daemon.handle({"op": op, "args": args})


def test_workflow_rounds_until_achieved_and_report(setup):
    daemon, job = setup
    assert call(daemon, "harness.check", job=job.id)["error"]["code"] == ErrorCode.FILE_NOT_FOUND
    write_harness(job, {**HARNESS, "confirmed": None})
    check = call(daemon, "harness.check", job=job.id)["result"]  # 框定阶段可以先取值
    assert check["items"][0]["value"] == 100.0 and check["shortfall"] == pytest.approx(1.0) and not check["achieved"]
    assert call(daemon, "round.record", job=job.id)["error"]["code"] == ErrorCode.BAD_ARGS  # 未确认不能记录轮次
    write_harness(job, HARNESS)
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
    assert "达成" in html and "总长不超过 95 mm" in html and "TOTR" in html and "<svg" in html
    # 语言由需求框定的 lang 决定，--lang 优先；切换语言不改变需求框定的哈希
    write_harness(job, {**HARNESS, "lang": "en"})
    assert report.write(job)["lang"] == "en"
    html = (job.root / "report" / "index.html").read_text(encoding="utf-8")
    assert 'lang="en"' in html and "Achieved" in html and "达成" not in html and "总长不超过 95 mm" in html
    assert report.write(job, lang="zh")["lang"] == "zh"


def test_harness_change_needs_new_confirmation(setup):
    daemon, job = setup
    write_harness(job, HARNESS)
    call(daemon, "round.record", job=job.id)
    changed = {**HARNESS, "targets": [{**HARNESS["targets"][0], "goal": {"max": 99.0}}]}
    write_harness(job, changed)
    assert call(daemon, "round.record", job=job.id, tier="local")["error"]["code"] == ErrorCode.SPEC_CHANGED
    write_harness(job, {**changed, "confirmed": {"quote": "改成 99 mm 也可以"}})
    assert call(daemon, "round.record", job=job.id, tier="local")["ok"]


@pytest.mark.parametrize("bound", ["min", "max", "equals"])
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, "50", None])
def test_invalid_goal_cannot_pass(bound, value):
    target = {**HARNESS["targets"][0], "goal": {bound: value, "tol": 0.5}, "scale": 5}
    with pytest.raises(SaosError) as exc:
        harness.validate({**HARNESS, "targets": [target]})
    assert exc.value.code == ErrorCode.BAD_ARGS


def test_report_never_reuses_an_old_requirement_version(setup):
    daemon, job = setup
    loose = {**HARNESS, "targets": [{**HARNESS["targets"][0], "goal": {"max": 110}}]}
    write_harness(job, loose)
    assert call(daemon, "round.record", job=job.id)["result"]["record"]["achieved"]
    tight = {**loose, "targets": [{**loose["targets"][0], "goal": {"max": 50}}], "confirmed": {"quote": "改成50"}}
    write_harness(job, tight)
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
    target = {**HARNESS["targets"][0], "check": {"analysis": {"type": "FakeCurve", "path": "series[0].y.v[2]"}}}
    write_harness(job, {**HARNESS, "targets": [target]})
    first = call(daemon, "round.record", job=job.id)["result"]["evaluation"]
    text_path = job.root / first["items"][0]["evidence"]["text"]
    original = text_path.read_bytes()
    daemon.backend.lens["surfaces"][1]["thickness"] = 105
    second = call(daemon, "round.record", job=job.id, tier="local")["result"]["evaluation"]
    checked = call(daemon, "harness.check", job=job.id)["result"]
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
    write_harness(job, {**HARNESS, "targets": [{**HARNESS["targets"][0], "check": check}]})
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
    write_harness(job, {**HARNESS, "targets": [{**HARNESS["targets"][0], "check": {"script": "checks/check.py"}}],
                        "stop": {"tiers": ["local"]}})
    first = call(daemon, "round.record", job=job.id)["result"]
    # 同一镜头再记一轮被拒绝；改一个不影响总长的半径后再记 / the same lens again is refused; change a radius that leaves the track
    assert call(daemon, "round.record", job=job.id, tier="local")["error"]["code"] == ErrorCode.DUPLICATE_ROUND
    daemon.backend.lens["surfaces"][1]["radius"] = 45.0
    assert call(daemon, "round.record", job=job.id, tier="local")["result"]["decision"]["status"] == "exhausted"
    script.write_text(source.replace("[])", "[]) + 5"), encoding="utf-8")
    with pytest.raises(SaosError):
        report.build(job)
    changed = call(daemon, "round.record", job=job.id, tier="local")["result"]
    assert changed["record"]["harness_sha"] == first["record"]["harness_sha"]
    assert changed["record"]["method_sha"] != first["record"]["method_sha"]
    assert changed["decision"]["status"] == "continue" and changed["decision"]["last_gain_round"] == 2
    assert report.build(job)["deliver"]["round"] == 2
    archived = job.root / first["evaluation"]["items"][0]["evidence"]["script"]
    assert archived.read_text(encoding="utf-8") == source


def test_report_refuses_missing_or_changed_evidence(setup):
    daemon, job = setup
    write_harness(job, HARNESS)
    record = call(daemon, "round.record", job=job.id)["result"]["record"]
    (job.root / record["evaluation"]).unlink()
    with pytest.raises(SaosError) as exc:
        report.build(job)
    assert exc.value.code == ErrorCode.EVIDENCE_TAMPERED
    assert {"path": record["evaluation"], "problem": "missing"} in exc.value.details["problems"]


def test_legacy_stopped_round_can_be_remeasured_without_reconfirmation(setup):
    daemon, job = setup
    h = {**HARNESS, "targets": [{**HARNESS["targets"][0], "goal": {"max": 110}}]}
    write_harness(job, h)
    first = call(daemon, "round.record", job=job.id)["result"]["record"]
    legacy = {k: v for k, v in first.items() if k != "method_sha"}
    (job.root / ledger.LEDGER_FILE).write_text(json.dumps(legacy) + "\n", encoding="utf-8")
    with pytest.raises(SaosError):
        report.build(job)
    assert call(daemon, "round.record", job=job.id, tier="local")["result"]["record"]["achieved"]
    assert report.build(job)["deliver"]["round"] == 1


def test_evidence_names_cannot_collide_on_windows(setup):
    daemon, job = setup
    target = {**HARNESS["targets"][0], "check": {"analysis": {"type": "FakeCurve", "path": "series[0].y.v[2]"}}}
    write_harness(job, {**HARNESS, "targets": [{**target, "id": name} for name in ("result", "Result")]})
    evaluation = call(daemon, "round.record", job=job.id)["result"]["evaluation"]
    paths = [(job.root / item["evidence"]["text"]).with_suffix(".json") for item in evaluation["items"]]
    assert len(set(paths)) == 2
    assert all(json.loads(p.read_text())["series"][0]["y"]["v"][-1] == 100 for p in paths)
    assert len(report.build(job)["items"]) == 2


def test_connection_loss_during_check_does_not_record_an_infinite_round(setup, monkeypatch):
    daemon, job = setup
    write_harness(job, HARNESS)

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
    write_harness(job, HARNESS)
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
    rounds.append({"round": 2, "shortfall": 0.0, "tier": "materials", "achieved": True, "harness_sha": "a", "model_glass": []})
    assert ledger.decide(rounds, STOP)["status"] == "achieved"
    assert ledger.deliverable(rounds[2]) and not ledger.deliverable(rounds[1])


def test_report_marks_exploration_only_rounds(setup):
    daemon, job = setup
    write_harness(job, HARNESS)
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
    write_harness(job, HARNESS)
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
    write_harness(job, {**HARNESS, "targets": [{**HARNESS["targets"][0], "check": {"script": "checks/edit.py"}}]})
    assert call(daemon, "harness.check", job=job.id)["result"]["lens_modified_by_checks"]
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
    write_harness(job, {**HARNESS, "targets": [{**HARNESS["targets"][0], "check": {"script": "checks/track.py"}}]})
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
    write_harness(job, HARNESS)
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
    # 轮次上限按需求框定版本计 / the round budget counts per harness version
    many = rounds_of([5.0] * 4, ["baseline", "local", "local", "local"])
    for r in many[2:]:
        r["harness_sha"] = "b"
    assert ledger.decide(many, {**STOP, "max_rounds": 2})["status"] != "limit"
    assert ledger.decide(many + [{**many[-1], "round": 4}], {**STOP, "max_rounds": 2})["status"] == "limit"
