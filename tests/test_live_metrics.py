"""后台进程在回复之后自动刷新当前镜头视图和指标（ADR-0019）的离线回归。
Offline regressions for the live view and metrics the daemon refreshes after replying (ADR-0019)."""

from __future__ import annotations

import json
import time

import pytest
import yaml

from saos import demand, jobs
from saos.backend.fake import FakeBackend
from saos.daemon import Daemon
from saos.ui import stage

LINE = demand.QUESTION_LINE["zh"]
LENGTH = {"id": "length", "description": "total track", "check": {"operand": {"type": "TOTR"}}, "origin": "user",
          "must": {"max": 95.0}, "scale": 5.0, "source": "user"}
DEMAND = {
    "schema": 2,
    "constants": [{"id": "lens", "description": "the sample singlet", "value": "a.zmx", "source": "user"}],
    "variables": [{"id": "radii", "description": "both radii are free"}],
    "objectives": [LENGTH],
    "summary": f"用户要总长不超过 95 mm。{LINE}",
}


def call(daemon, op, **args):
    return daemon.handle({"op": op, "args": args})


def read(job, name):
    return json.loads((job.root / name).read_text(encoding="utf-8"))


@pytest.fixture
def loaded(saos_home, fake_lens_file):
    backend = FakeBackend()
    daemon = Daemon(backend, backend.start(), "standalone")
    job = jobs.create_job("live")
    assert call(daemon, "lens.import", job=job.id, src=str(fake_lens_file), name="a.zmx")["ok"]
    assert call(daemon, "lens.load", job=job.id, file="a.zmx")["ok"]
    daemon.idle()
    return daemon, job


def write_demand(job, data=DEMAND):
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_a_lens_change_refreshes_the_view_and_measures_after_the_reply(loaded):
    daemon, job = loaded
    write_demand(job)
    first = read(job, jobs.VIEW_FILE)["seq"]
    # 刷新在回复之后（serve 循环调用 idle）/ the refresh comes after the reply (the serve loop calls idle)
    reply = call(daemon, "lens.set", job=job.id, surface=1, param="thickness", value=6.0)
    assert reply["ok"], reply
    assert read(job, jobs.VIEW_FILE)["seq"] == first  # 回复时还没刷新 / not yet at reply time
    daemon.idle()
    view = read(job, jobs.VIEW_FILE)
    metrics = read(job, jobs.METRICS_FILE)
    assert view["seq"] == first + 1 and view["view"] == metrics["view"] == daemon.view_token
    total = daemon.backend.operand("TOTR", 0, 0, [])
    item = metrics["items"]["length"]
    assert item["value"] == pytest.approx(total) and item["source"] == "live" and item["view"] == daemon.view_token
    assert metrics["shortfall"]["view"] == daemon.view_token and metrics["skipped"] == [] and metrics["slow"] == []

    # 只读操作不刷新，也不重测 / read-only ops neither refresh nor measure
    assert call(daemon, "lens.operand", job=job.id, type="TOTR")["ok"]
    daemon.idle()
    assert read(job, jobs.VIEW_FILE)["seq"] == first + 1

    # 窗口按需求单默认显示所有带判定方法的条目 / the window shows every checked item by default
    snap = stage.snapshot(job)
    assert [m["id"] for m in snap["metric_items"]] == ["length"] and snap["metrics"]["items"]["length"]["value"] == pytest.approx(total)


def test_check_results_become_the_metrics_without_measuring_again(loaded, monkeypatch):
    daemon, job = loaded
    write_demand(job)
    checked = call(daemon, "demand.check", job=job.id)["result"]

    def no_measuring(*_args, **_kwargs):
        raise AssertionError("check results must not be measured again")

    monkeypatch.setattr(demand, "quick", no_measuring)
    daemon.idle()
    metrics = read(job, jobs.METRICS_FILE)
    assert metrics["items"]["length"]["source"] == "check" and metrics["evidence"] == checked["evidence_file"]
    assert metrics["shortfall"]["value"] == pytest.approx(checked["shortfall"])


def test_slow_items_wait_for_check_and_the_budget_cuts_a_pass(loaded, monkeypatch):
    daemon, job = loaded
    fast = {**LENGTH, "id": "fast"}
    write_demand(job, {**DEMAND, "objectives": [LENGTH, fast]})
    # 上一次单测就超出预算的项不再自动测 / an item that alone exceeded the budget is not measured again
    (job.root / jobs.METRICS_FILE).write_text(json.dumps({"items": {"length": {"value": 1.0, "view": "old", "source": "check", "duration_s": 10.0}}}),
                                              encoding="utf-8")
    assert call(daemon, "lens.set", job=job.id, surface=1, param="thickness", value=6.0)["ok"]
    daemon.idle()
    metrics = read(job, jobs.METRICS_FILE)
    assert metrics["slow"] == ["length"] and metrics["items"]["length"]["view"] == "old"
    assert metrics["items"]["fast"]["view"] == daemon.view_token
    # 没测全进 G 的项就不给当前的 G / no current G unless every item in G was measured
    assert "shortfall" not in metrics

    # 预算用完时剩下的项记为 skipped，排到下次最前 / items past the budget are skipped and go first next time
    monkeypatch.setenv("SAOS_LIVE_S", "0.01")
    monkeypatch.setattr(daemon.backend, "merit_value", lambda: time.sleep(0.05) or 1.0, raising=False)
    (job.root / jobs.METRICS_FILE).unlink()
    assert call(daemon, "lens.set", job=job.id, surface=1, param="thickness", value=7.0)["ok"]
    daemon.idle()
    assert read(job, jobs.METRICS_FILE)["skipped"] == ["length", "fast"]


def test_checks_that_change_the_lens_pause_live_measurement(loaded):
    daemon, job = loaded
    (job.root / "checks").mkdir()
    (job.root / "checks" / "edit.py").write_text(
        "def run(ctx):\n    ctx.backend.lens['surfaces'][1]['radius'] += 1.0\n    return ctx.backend.operand('TOTR', 0, 0, [])\n", encoding="utf-8")
    write_demand(job, {**DEMAND, "objectives": [{**LENGTH, "check": {"script": "checks/edit.py"}}]})
    assert call(daemon, "lens.set", job=job.id, surface=1, param="thickness", value=6.0)["ok"]
    daemon.loaded.dirty = False
    daemon.idle()
    metrics = read(job, jobs.METRICS_FILE)
    assert any("radius" in c for c in metrics["paused"]["changes"]) and daemon.loaded.dirty
    radius = daemon.backend.lens["surfaces"][1]["radius"]
    assert call(daemon, "lens.set", job=job.id, surface=1, param="thickness", value=7.0)["ok"]
    daemon.idle()
    assert daemon.backend.lens["surfaces"][1]["radius"] == radius  # 暂停后不再运行判定 / paused: the checks no longer run


def test_script_progress_refreshes_the_view(loaded):
    daemon, job = loaded
    before = read(job, jobs.VIEW_FILE)["seq"]
    daemon._view_at = 0.0
    source = "def run(ctx):\n    ctx.set({'1.thickness': 6.0})\n    ctx.progress('moved')\n    return ctx.job.root.joinpath('view.json').read_text(encoding='utf-8')\n"
    reply = call(daemon, "script.exec", job=job.id, source=source, name="tick", args={})
    assert reply["ok"], reply
    assert json.loads(reply["result"]["result"])["seq"] == before + 1
