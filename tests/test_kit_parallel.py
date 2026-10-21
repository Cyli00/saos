"""kit、search、ctx.measure、worker 池和 saos scan 的离线测试（假后端；worker 子进程也用假后端）。"""

from __future__ import annotations

import json
import math
from types import SimpleNamespace

import pytest
import yaml

from saos import demand, jobs, kit, search
from saos.backend.fake import FakeBackend
from saos.daemon import Daemon
from saos.protocol import ErrorCode, SaosError

LINE = demand.QUESTION_LINE["zh"]
LENGTH = {"id": "length", "description": "total track", "check": {"operand": {"type": "TOTR"}}, "origin": "user",
          "must": {"max": 95.0}, "scale": 5.0, "source": "user"}
DEMAND = {"schema": 2, "constants": [{"id": "lens", "description": "the sample singlet", "value": "a.zmx", "source": "user"}],
          "variables": [{"id": "radii", "description": "both radii are free"}], "objectives": [LENGTH],
          "summary": f"用户要总长不超过 95 mm。{LINE}"}


@pytest.fixture
def setup(saos_home, fake_lens_file):
    backend = FakeBackend()
    daemon = Daemon(backend, backend.start(), "extension")
    job = jobs.create_job("kit")
    daemon.handle({"op": "lens.import", "args": {"job": job.id, "src": str(fake_lens_file), "name": "a.zmx"}})
    daemon.handle({"op": "lens.load", "args": {"job": job.id, "file": "a.zmx"}})
    yield daemon, job
    daemon.pool.close()


MODEL_NOTES = {"surfaces": {"0": "object", "1": "lens front", "2": "lens back", "3": "image"}, "assumptions": ["test"], "first_order": ["f = 50 mm"]}


def write_demand(job, data=DEMAND, confirm=None):
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    if confirm:
        demand.confirm(job.root, confirm)


def call(daemon, op, **args):
    return daemon.handle({"op": op, "args": args})


def exec_script(daemon, job, source, name="s"):
    return call(daemon, "script.exec", job=job.id, name=name, source=source, args={})


# ---------- kit ----------

def test_kit_reads_writes_and_restores_by_comment_or_index(fake_lens_file):
    backend = FakeBackend()
    backend.load(fake_lens_file)
    backend.set_surface(1, "comment", "front")
    ctx = SimpleNamespace(backend=backend, cache={})
    assert kit.surface(ctx, "front") == 1 and kit.surface(ctx, -1) == 3 and kit.surface(ctx, "2") == 2
    assert kit.get(ctx, "front.radius") == 50.0 and kit.get(ctx, "front.curvature") == pytest.approx(0.02)
    with kit.kept(ctx, ["front.radius", "2.radius"]) as before:
        kit.set(ctx, {"front.curvature": 0.04, "2.radius": -10.0})
        assert kit.values(ctx, ["1.radius", "2.radius"]) == {"1.radius": 25.0, "2.radius": -10.0}
        assert before == {"front.radius": 50.0, "2.radius": -50.0}
    assert kit.values(ctx, ["1.radius", "2.radius"]) == {"1.radius": 50.0, "2.radius": -50.0}
    kit.set(ctx, {"1.curvature": 0})
    assert math.isinf(kit.get(ctx, "1.radius")) and kit.get(ctx, "1.curvature") == 0.0
    with pytest.raises(SaosError) as exc:
        kit.set(ctx, {"2.thickness": 1.0})  # 被求解占着 / held by a solve
    assert exc.value.code == ErrorCode.VALUE_NOT_APPLIED
    for bad in ("nowhere.radius", "1.colour", "radius"):
        with pytest.raises(SaosError):
            kit.get(ctx, bad)
    assert kit.parse_key("field.2.y") == ("system", "field_y", "2") and kit.parse_key("wave.3") == ("system", "wave", "3")
    assert kit.parse_key("tube.wall 1.par12") == ("surface", "tube.wall 1", "par12")
    assert len(kit.pupil(21)) == 317 and kit.pupil(1) == [(0.0, 0.0)]


# ---------- search ----------

def test_root_brackets_skips_invalid_points_and_reports_failure():
    res = search.root(lambda x: x * x, 0.0, 3.0, target=2.0, xtol=1e-10)
    assert res["ok"] and res["x"] == pytest.approx(math.sqrt(2), abs=1e-9) and res["evals"] < 25
    res = search.root(lambda x: None if x < 1.2 else x - 2.0, 0.0, 3.0)  # 左半段无效 / the left part is invalid
    assert res["ok"] and res["x"] == pytest.approx(2.0)
    res = search.root(lambda x: x + 5.0, 0.0, 3.0)
    assert not res["ok"] and res["x"] == 0.0 and "no sign change" in res["reason"]
    assert search.root(lambda x: None, 0.0, 1.0)["reason"] == "no valid point"


def test_minimize_with_bounds_budget_and_invalid_points():
    f = lambda p: (p["a"] - 1.0) ** 2 + (p["b"] + 2.0) ** 2  # noqa: E731
    res = search.minimize(f, {"a": 0.0, "b": 0.0}, {"a": 0.5, "b": 0.5}, xtol=1e-6, ftol=1e-12, max_evals=400)
    assert res["best"]["a"] == pytest.approx(1.0, abs=1e-3) and res["best"]["b"] == pytest.approx(-2.0, abs=1e-3)
    res = search.minimize(f, {"a": 0.0, "b": 0.0}, {"a": 0.5, "b": 0.5}, bounds={"b": (-1.0, None)}, max_evals=400)
    assert res["best"]["b"] >= -1.0 - 1e-9 and res["best"]["b"] == pytest.approx(-1.0, abs=1e-2)
    res = search.minimize(lambda p: None if p["a"] > 0.2 else f(p), {"a": 0.0, "b": 0.0}, {"a": 0.5, "b": 0.5}, max_evals=7)
    assert res["stopped"] == "max_evals" and res["evals"] == 7 and res["best"] is not None and res["best"]["a"] <= 0.2
    assert search.grid({"x": [1, 2], "y": {"from": 0, "to": 1, "steps": 3}})[-1] == {"x": 2, "y": 1.0}
    assert search.axis({"from": 0, "to": 1, "step": 0.25}) == [0.0, 0.25, 0.5, 0.75, 1.0]


# ---------- ctx.measure 与判定共享 cache / ctx.measure and the shared check cache ----------

def test_checks_share_one_cache_and_measure_matches_check(setup):
    daemon, job = setup
    checks = job.root / "checks"
    checks.mkdir(exist_ok=True)
    (checks / "helper.py").write_text(
        "def total(ctx):\n"
        "    def compute():\n"
        "        with open(ctx.path('explore/calls.txt'), 'a') as fh:\n"
        "            fh.write('x')\n"
        "        return ctx.backend.operand('TOTR', 0, 0, [])\n"
        "    return ctx.memo('totr', compute)\n", encoding="utf-8")
    (job.root / "explore").mkdir(exist_ok=True)
    for name in ("a", "b"):
        (checks / f"{name}.py").write_text("def run(ctx):\n    return ctx.lib('helper').total(ctx)\n", encoding="utf-8")
    items = [{**LENGTH, "id": f"len_{n}", "check": {"script": f"checks/{n}.py"}} for n in ("a", "b")]
    write_demand(job, {**DEMAND, "objectives": [LENGTH, *items]})
    check = call(daemon, "demand.check", job=job.id)["result"]
    assert [i["value"] for i in check["items"]] == [100.0, 100.0, 100.0]
    assert (job.root / "explore" / "calls.txt").read_text() == "x"  # 两个判定只算一次 / two checks, one computation
    out = exec_script(daemon, job, "def run(ctx):\n    return ctx.measure()\n")["result"]["result"]
    assert out["values"] == {"length": 100.0, "len_a": 100.0, "len_b": 100.0}, (out["errors"], out["invalid"])
    assert out["shortfall"] == pytest.approx(3.0) and out["invalid"] == [] and out["hard_failing"] == []
    bad = exec_script(daemon, job, "def run(ctx):\n    return ctx.measure(['nope'])\n")
    assert bad["error"]["code"] == ErrorCode.BAD_ARGS


# ---------- worker 池 / the worker pool ----------

MAP_SCRIPT = '''
def run(ctx):
    a = ctx.map("point", [{"1.thickness": 1.0}, {"1.thickness": 2.0}, {"1.thickness": 3.0}], workers=2, args={"k": 7})
    b = ctx.map("point", [{"1.thickness": 4.0}], workers=2)
    return {"a": a["results"], "b": b["results"], "a_started": len(a["startup"]["started"]),
            "b_started": len(b["startup"]["started"]), "folder": a["folder"], "errors": a["errors"]}


def point(w, p):
    w.set(p)
    return {"t": w.get("1.thickness"), "len": w.measure(["length"])["values"]["length"], "k": w.args.get("k")}
'''


def test_map_runs_script_functions_on_a_reused_pool(setup):
    daemon, job = setup
    write_demand(job)
    out = exec_script(daemon, job, MAP_SCRIPT, name="mapper")
    assert out["ok"], out
    res = out["result"]["result"]
    assert res["errors"] == {} and [r["len"] for r in res["a"]] == [96.0, 97.0, 98.0] and res["a"][0]["k"] == 7
    assert res["b"][0] == {"t": 4.0, "len": 99.0, "k": None}
    assert res["a_started"] == 2 and res["b_started"] == 0  # 第二次调用复用池 / the second call reuses the pool
    assert daemon.pool.status()["workers"] == 2
    folder = job.root / res["folder"]
    assert (folder / "work.py").is_file() and json.loads((folder / "plan.json").read_text(encoding="utf-8"))["fn"] == "point"
    # 当前镜头不受 worker 影响 / workers do not touch the current lens
    assert daemon.backend.surfaces()[1]["thickness"] == 5.0
    closed = exec_script(daemon, job, "def run(ctx):\n    return ctx.pool_close()\n")["result"]["result"]
    assert closed["workers"] == 2 and daemon.pool.status()["workers"] == 0


def test_map_refuses_unsaved_changes_and_unknown_functions(setup):
    daemon, job = setup
    write_demand(job)
    assert exec_script(daemon, job, "def run(ctx):\n    return ctx.map('nothing', [1])\n")["error"]["code"] == ErrorCode.BAD_ARGS
    dirty = "def run(ctx):\n    ctx.set({'1.thickness': 6.0})\n    return ctx.map('f', [1])\n\ndef f(w, i):\n    return i\n"
    assert exec_script(daemon, job, dirty)["error"]["code"] == ErrorCode.UNSAVED_CHANGES


# ---------- saos scan ----------

def test_scan_grid_ranks_points_keeps_best_lenses_and_leaves_the_lens(setup):
    daemon, job = setup
    write_demand(job)
    before = json.dumps(daemon.backend.surfaces(), sort_keys=True)
    plan = {"vars": {"1.thickness": [4.0, 0.0, 2.0]}, "measure": ["length"], "operands": {"totr": ["TOTR"]}, "keep": 1, "workers": 2}
    out = call(daemon, "scan.run", job=job.id, plan=plan)
    assert out["ok"], out
    res = out["result"]
    assert res["points"] == 3 and res["valid"] == 3, (res["errors"], res["top"])
    assert [r["set"]["1.thickness"] for r in res["top"]] == [0.0, 2.0, 4.0]
    assert [r["shortfall"] for r in res["top"]] == pytest.approx([0.0, 0.4, 0.8])
    assert res["top"][0]["operands"] == {"totr": 95.0} and res["kept_lenses"] == ["scans/001/p001.zmx"]
    assert (job.lenses_dir / "scans" / "001" / "p001.zmx").is_file()
    assert not (job.lenses_dir / "scans" / "001" / "p000.zmx").exists()
    assert json.dumps(daemon.backend.surfaces(), sort_keys=True) == before and job.verify_seals() == []
    assert call(daemon, "lens.load", job=job.id, file="scans/001/p001.zmx")["ok"]


def test_scan_solves_one_variable_per_point(setup):
    daemon, job = setup
    write_demand(job)
    plan = {"points": [{"1.radius": 40.0}, {"1.radius": 60.0}],
            "solve": {"var": "1.thickness", "lo": 0.0, "hi": 20.0, "target": {"item": "length"}, "value": 103.0}, "keep": 0}
    res = call(daemon, "scan.run", job=job.id, plan=plan)["result"]
    assert all(r["solve"]["ok"] and r["solve"]["x"] == pytest.approx(8.0) for r in res["top"])
    assert all(r["values"]["length"] == pytest.approx(103.0) for r in res["top"]) and res["kept_lenses"] == []
    plan["solve"]["value"] = 500.0  # 区间内够不着 / out of reach in the bracket
    res = call(daemon, "scan.run", job=job.id, plan=plan)["result"]
    assert res["valid"] == 0 and all("no sign change" in r["invalid"] for r in res["top"])


def test_scan_minimize_needs_a_confirmed_demand_and_descends(setup):
    daemon, job = setup
    # 与 opt local 同一判定：需求确认、结构有资格、当前模型已认可 / the same rule as opt local
    write_demand(job, {**DEMAND, "structure": {"mode": "prescribed", "reason": "测试样品的结构由用户指定"}})
    plan = {"minimize": {"vars": {"1.thickness": {"step": 1.0, "min": -3.0, "max": 10.0}}, "max_evals": 40}, "measure": ["length"]}
    assert call(daemon, "scan.run", job=job.id, plan=plan)["error"]["code"] == ErrorCode.DEMAND_NOT_CONFIRMED
    demand.confirm(job.root, "确认")
    assert call(daemon, "scan.run", job=job.id, plan=plan)["error"]["code"] == ErrorCode.MODEL_NOT_APPROVED
    folder = call(daemon, "lens.describe", job=job.id, notes=MODEL_NOTES)["result"]["folder"]
    assert call(daemon, "lens.approve", job=job.id, model=folder, quote="模型可以")["ok"]
    res = call(daemon, "scan.run", job=job.id, plan=plan)["result"]
    best = res["best"]
    assert best["shortfall"] == 0.0 and best["set"]["1.thickness"] <= 0.0 and best["start_f"] == pytest.approx(1.0)
    assert best["evals"] <= 40 and best["lens"]


@pytest.mark.parametrize("plan", [
    {},
    {"vars": {"1.thickness": [1]}, "points": [{"1.thickness": 1}]},
    {"vars": {"1.colour": [1]}},
    {"vars": {"1.thickness": [1]}, "measure": ["nope"]},
    {"points": []},
    {"minimize": {"vars": {"1.thickness": {"step": 0}}}},
    {"vars": {"1.thickness": [1]}, "solve": {"var": "1.radius", "lo": 0, "hi": 1, "target": {"item": "nope"}}},
    {"vars": {"1.thickness": [1]}, "compensate": "refocus"},
])
def test_scan_plan_is_checked(setup, plan):
    daemon, job = setup
    write_demand(job)
    assert call(daemon, "scan.run", job=job.id, plan=plan)["error"]["code"] == ErrorCode.BAD_ARGS



def probe_plan(*probes, **kw):
    return {"target": "length", "mechanism": "the track is the sum of the thicknesses", "compensate": "quick_focus", "probes": list(probes), **kw}


T1 = {"id": "t1", "key": "1.thickness", "delta": 2.0, "prediction": "longer first gap, longer track", "expect": {"length": "up"}}
R1 = {"id": "r1", "key": "1.radius", "delta": 1.0, "control": True, "prediction": "radius does not enter the track", "expect": {"length": "none"}}


def test_probes_rank_variables_compare_predictions_and_leave_the_lens(setup):
    daemon, job = setup
    write_demand(job)  # 诊断不需要确认 / diagnosis needs no confirmation
    before = json.dumps(daemon.backend.surfaces(), sort_keys=True)
    wrong = {**T1, "id": "wrong", "delta": 1.0, "expect": {"length": "down"}}
    out = call(daemon, "scan.run", job=job.id, plan=probe_plan(T1, R1, wrong))
    assert out["ok"], out
    result = out["result"]
    assert result["points"] == 7 and result["valid"] == 7 and result["kept_lenses"] == []
    assert [r["id"] for r in result["ranking"]] == ["t1", "wrong"] and result["ranking"][0]["effect"] == pytest.approx(0.4)
    rows = {r["id"]: r for r in result["probes"]}
    assert rows["t1"]["observed"]["length"] == "up" and not rows["t1"]["mismatch"]
    assert rows["r1"]["observed"]["length"] == "none"
    assert rows["wrong"]["mismatch"] == {"length": {"expected": "down", "observed": "up"}}
    assert rows["t1"]["plus"]["values"]["length"] == pytest.approx(102.0) and rows["t1"]["plus"]["compensation"]["surface"] == 2
    # 假后端的 quick_focus 不动，提醒补偿没起作用 / the fake quick_focus never moves, so the review says nothing was compensated
    assert len(result["review"]) == 2 and "wrong" in result["review"][0] and "nothing was compensated" in result["review"][1]
    assert json.dumps(daemon.backend.surfaces(), sort_keys=True) == before
    folder = job.root / result["folder"]
    assert (folder / "plan.json").is_file() and (folder / "result.json").is_file() and job.verify_seals() == []


def test_probes_by_script(setup):
    daemon, job = setup
    write_demand(job)
    (job.root / "explore").mkdir(exist_ok=True)
    (job.root / "explore" / "gap.py").write_text("def run(w, delta):\n    w.set({'1.thickness': 5.0 + delta})\n", encoding="utf-8")
    script = {"id": "gap", "script": "explore/gap.py", "delta": 2.0, "prediction": "same as t1", "expect": {"length": "up"}}
    result = call(daemon, "scan.run", job=job.id, plan=probe_plan(script, R1))["result"]
    rows = {r["id"]: r for r in result["probes"]}
    assert rows["gap"]["plus"]["values"]["length"] == pytest.approx(102.0) and not rows["gap"]["mismatch"]
    assert result["ranking"][0]["id"] == "gap" and result["valid"] == 5


def test_probes_without_a_control_or_a_mover_say_so(setup):
    daemon, job = setup
    write_demand(job)
    review = call(daemon, "scan.run", job=job.id, plan=probe_plan(T1))["result"]["review"]
    assert any("no control probe" in line for line in review)
    r2 = {**R1, "id": "r2", "control": False, "expect": {}}
    review = call(daemon, "scan.run", job=job.id, plan=probe_plan(r2, R1))["result"]["review"]
    assert any("structure question" in line for line in review)


def test_probes_stop_at_a_target_without_a_valid_base_value(setup):
    """前提不成立（例如没有实焦点）时不读扰动，提示先确认可行性。"""
    daemon, job = setup
    (job.root / "checks").mkdir()
    (job.root / "checks" / "virtual.py").write_text("def run(ctx):\n    return float('inf')\n", encoding="utf-8")
    write_demand(job, {**DEMAND, "objectives": [{**LENGTH, "check": {"script": "checks/virtual.py"}}]})
    result = call(daemon, "scan.run", job=job.id, plan=probe_plan(T1, R1, measure=["length"]))["result"]
    assert result["probes"] == [] and "feasibility ladder" in result["review"][0]


@pytest.mark.parametrize("plan", [
    probe_plan({**T1, "prediction": ""}, R1),                        # 没有预测 / no prediction
    probe_plan(T1, R1, target="nothing"),
    probe_plan(T1, R1, mechanism=" "),
    probe_plan({**T1, "key": "1.material"}, R1),
    probe_plan({**T1, "key": "1.colour"}, R1),
    probe_plan({**T1, "key": "1.par4"}, R1),                         # 这一面没有这一列 / no such column on this surface
    probe_plan({**T1, "delta": 0}, R1),
    probe_plan({**T1, "script": "explore/x.py"}, R1),                # key 和 script 只能给一个 / key or script
    probe_plan(T1, R1, compensate="refocus"),
    probe_plan(*({**T1, "id": f"p{k}"} for k in range(6)), R1),      # 最多 6 个 / at most 6
])
def test_probe_plan_is_checked(setup, plan):
    daemon, job = setup
    write_demand(job)
    assert call(daemon, "scan.run", job=job.id, plan=plan)["error"]["code"] == ErrorCode.BAD_ARGS


SLOW = "import time\n\n\ndef run(w):\n    time.sleep({secs})\n    return {{}}\n"

def test_detached_scan_runs_while_the_daemon_serves_other_commands(setup):
    import time

    from saos import scan
    from saos.ui import stage

    daemon, job = setup
    write_demand(job)
    (job.root / "explore").mkdir(exist_ok=True)
    (job.root / "explore" / "slow.py").write_text(SLOW.format(secs=0.4), encoding="utf-8")
    plan = {"vars": {"1.thickness": [0.0, 1.0, 2.0, 3.0, 4.0]}, "compensate": {"script": "explore/slow.py"}, "measure": ["length"], "workers": 1}
    started = call(daemon, "scan.run", job=job.id, plan=plan, detach=True)["result"]
    assert started["detached"] and started["folder"] == "scans/001"
    # 扫描在后台跑，后台进程照常处理其他命令 / the daemon keeps serving while the scan runs
    assert call(daemon, "lens.info", job=job.id)["ok"]
    assert call(daemon, "scan.run", job=job.id, plan=plan)["error"]["code"] == ErrorCode.POOL_BUSY
    assert call(daemon, "shutdown")["error"]["code"] == ErrorCode.BUSY
    assert stage.snapshot(job)["progress"]["op"] == "scan.run"
    running = scan.show(job)
    assert not running["finished"] and running["running"]
    deadline = time.monotonic() + 60
    while not (job.root / "scans" / "001" / "result.json").is_file() and time.monotonic() < deadline:
        time.sleep(0.1)
    daemon.background["scans/001"].join(timeout=30)
    done = scan.show(job, "1")
    assert done["finished"] and done["valid"] == 5 and done["best"]["set"] == {"1.thickness": 0.0}
    progress = json.loads((job.root / "scans" / "001" / "progress.json").read_text(encoding="utf-8"))
    assert progress["status"] == "done" and job.verify_seals() == [] and daemon.running_background() == []
    assert call(daemon, "shutdown")["ok"]


def test_job_cancel_stops_a_detached_scan(setup):
    import time

    from saos import scan

    daemon, job = setup
    write_demand(job)
    (job.root / "explore").mkdir(exist_ok=True)
    (job.root / "explore" / "slow.py").write_text(SLOW.format(secs=0.3), encoding="utf-8")
    plan = {"vars": {"1.thickness": [float(k) for k in range(30)]}, "compensate": {"script": "explore/slow.py"}, "measure": ["length"], "workers": 1}
    call(daemon, "scan.run", job=job.id, plan=plan, detach=True)
    time.sleep(1.0)
    assert job.request_cancel()["detached"] == ["scans/001"]
    daemon.background["scans/001"].join(timeout=60)
    done = scan.show(job)
    assert done["finished"] and done["cancelled"] and done["not_run"]
