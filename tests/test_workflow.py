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
    return daemon, job


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
    assert (job.root / "lenses" / "rounds" / "r001.zmx").is_file() and (job.root / "rounds" / "r001.json").is_file()
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
    {"script": "missing.py"},
    {"script": "broken.py"},
])
def test_failed_measurement_still_generates_report(setup, check):
    daemon, job = setup
    (job.root / "broken.py").write_text("def run(ctx):\n    raise RuntimeError('<失败>')\n", encoding="utf-8")
    write_harness(job, {**HARNESS, "targets": [{**HARNESS["targets"][0], "check": check}]})
    result = call(daemon, "round.record", job=job.id)["result"]
    assert result["record"]["shortfall"] == math.inf and not result["record"]["achieved"]
    data = report.build(job)
    for lang in ("zh", "en"):
        html = report.render(job, data, lang)
        assert "无法取值" in html if lang == "zh" else "no value" in html
        assert "<失败>" not in html
    assert report.write(job)["status"] == "continue"


def test_script_versions_reset_comparison_and_preserve_source(setup):
    daemon, job = setup
    script = job.root / "check.py"
    source = "def run(ctx):\n    return ctx.backend.operand('TOTR', 0, 0, [])\n"
    script.write_text(source, encoding="utf-8")
    write_harness(job, {**HARNESS, "targets": [{**HARNESS["targets"][0], "check": {"script": "check.py"}}],
                        "stop": {"tiers": ["local"]}})
    first = call(daemon, "round.record", job=job.id)["result"]
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


def test_report_rejects_missing_evaluation(setup):
    daemon, job = setup
    write_harness(job, HARNESS)
    call(daemon, "round.record", job=job.id)
    (job.root / "rounds/r000.json").unlink()
    with pytest.raises(SaosError) as exc:
        report.build(job)
    assert exc.value.code == ErrorCode.FILE_NOT_FOUND


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
