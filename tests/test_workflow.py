from __future__ import annotations

import math

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
    out = report.write(job)
    html = (job.root / "report" / "index.html").read_text(encoding="utf-8")
    assert out["status"] == "achieved" and out["deliver"] == "rounds/r001.zmx"
    assert "达成" in html and "总长不超过 95 mm" in html and "TOTR" in html and "<svg" in html


def test_harness_change_needs_new_confirmation(setup):
    daemon, job = setup
    write_harness(job, HARNESS)
    call(daemon, "round.record", job=job.id)
    changed = {**HARNESS, "targets": [{**HARNESS["targets"][0], "goal": {"max": 99.0}}]}
    write_harness(job, changed)
    assert call(daemon, "round.record", job=job.id, tier="local")["error"]["code"] == ErrorCode.SPEC_CHANGED
    write_harness(job, {**changed, "confirmed": {"quote": "改成 99 mm 也可以"}})
    assert call(daemon, "round.record", job=job.id, tier="local")["ok"]
