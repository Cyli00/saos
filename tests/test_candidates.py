"""结构探索（candidates 计划）的离线测试：假后端，worker 子进程也用假后端。
Structure exploration (candidates plans) offline: fake backend, worker subprocesses included."""

from __future__ import annotations

import json
import warnings

import pytest
import yaml

from saos import candidates, demand, jobs, parallel
from saos.backend.fake import FakeBackend
from saos.backend.optiland_materials import MaterialError
from saos.cli import build_parser
from saos.daemon import Daemon
from saos.protocol import ErrorCode, SaosError

LINE = demand.QUESTION_LINE["zh"]
LENGTH = {"id": "length", "description": "total track", "check": {"operand": {"type": "TOTR"}}, "origin": "user",
          "must": {"max": 95.0}, "scale": 5.0, "source": "user"}
# 假镜头总长 100 mm：LOOSE 通过，TIGHT 不通过 / the fake lens is 100 mm long: LOOSE passes, TIGHT fails
LOOSE = {**LENGTH, "must": {"max": 150.0}}
TIGHT = {**LENGTH, "must": {"max": 95.0}}
# 硬条件：总长不超过 50 mm，假镜头不满足 / a hard limit the fake lens does not meet
SHORT = {"id": "short", "description": "hard total limit", "check": {"operand": {"type": "TOTR"}}, "goal": {"max": 50.0}, "source": "user"}
CRASH = {"id": "crash", "description": "an operand that raises", "check": {"operand": {"type": "CRASH"}}, "origin": "user",
         "must": {"max": 1.0}, "scale": 1.0, "source": "user"}
WARNED = {"id": "warned", "description": "an operand that warns", "check": {"operand": {"type": "WARN"}}, "origin": "user",
          "must": {"max": 10.0}, "scale": 1.0, "source": "user"}
LENS = {"id": "lens", "description": "the sample singlet", "value": "a.zmx", "source": "user"}
# 硬条件（常量）：总长上限。
MAX_LENGTH = {"id": "max_length", "description": "hard total track limit", "check": {"operand": {"type": "TOTR"}}, "goal": {"max": 150.0}, "source": "user"}
MAX_LENGTH_TIGHT = {**MAX_LENGTH, "goal": {"max": 95.0}}
# 判定时出现材料数据错误的目标项 / an objective whose check raises a material error
MATERIAL_PROBE = {"id": "glass_probe", "description": "an operand that needs a glass without data", "check": {"operand": {"type": "MATERIAL"}},
                  "origin": "user", "must": {"max": 10.0}, "scale": 1.0, "source": "user"}
# 判定时出现非数值类警告的目标项 / an objective whose check raises a non-numeric warning
DEPRECATED = {"id": "dep", "description": "an operand that raises a deprecation warning", "check": {"operand": {"type": "DEPRECATED"}},
              "origin": "user", "must": {"max": 10.0}, "scale": 1.0, "source": "user"}
DEMAND = {"schema": 2, "constants": [LENS, MAX_LENGTH],
          "variables": [{"id": "radii", "description": "both radii are free"}], "objectives": [LENGTH],
          "summary": f"用户要总长不超过 95 mm。{LINE}"}


def with_limit(limit, **extra):
    """DEMAND 的总长硬条件换成 limit，其余字段用 extra 覆盖 / DEMAND with its total-track hard limit replaced by limit"""
    return {**DEMAND, "constants": [LENS, limit], **extra}
NOTES = {"surfaces": {"0": "object", "1": "lens front", "2": "lens back", "3": "image"}, "assumptions": ["test"], "first_order": ["f = 50 mm"]}


def call(daemon, op, **args):
    return daemon.handle({"op": op, "args": args})


def write_demand(job, data=DEMAND, confirm="确认，开始吧"):
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    demand._PREPARED.clear()  # 需求文件刚改过，丢掉按修改时间缓存的旧版本 / the demand file just changed: drop the cached old version
    if confirm:
        demand.confirm(job.root, confirm)


def cand(cid, lens, **extra):
    return {"id": cid, "lens": lens, "hypothesis": f"{cid}: 说明", **extra}


@pytest.fixture
def explore(saos_home, fake_lens_file):
    """四个镜头：a 是基线；b 只改半径（结构与 a 相同）；c 第一面换成空气；d 第一面不是光阑。
    Four lenses: a the baseline; b only a different radius (same structure as a); c air at the first surface; d no stop."""
    backend = FakeBackend()
    daemon = Daemon(backend, backend.start(), "standalone")
    job = jobs.create_job("explore")
    base = json.loads(fake_lens_file.read_text(encoding="utf-8"))

    def variant(**edit):
        data = json.loads(json.dumps(base))
        data["surfaces"][1].update(edit)
        return data

    (job.root / "explore").mkdir(parents=True, exist_ok=True)
    for name, data in {"a": base, "b": variant(radius=60.0), "c": variant(material=""), "d": variant(is_stop=False),
                       "e": variant(material="F2"), "m": variant(radius=70.0), "x": variant(radius=80.0),
                       "f": variant(radius=90.0)}.items():
        (job.root / "explore" / f"{name}.zmx").write_text(json.dumps(data), encoding="utf-8")
    yield daemon, job
    daemon.pool.close()


def plan(stages, **extra):
    return {"candidates": [cand("a", "explore/a.zmx", hypothesis="单片，基线"),
                           cand("b", "explore/b.zmx", hypothesis="单片，较大曲率")],
            "stages": stages, "measure": ["length"], **extra}


def judge(job, cands, stages=(), folder_name="001"):
    """在本进程里按真实的候选流程逐个判定（无 worker 子进程），返回各候选的行；用来测结论，不测比较。
    Judge candidates in-process along the real candidate path (no worker subprocess); returns the rows, for verdicts."""
    d = demand.load_job(job.root)
    items = {i["id"]: i for i in demand.checked(d)}
    padded = list(cands)
    # 所测项与 scan 的默认一致：所有带界限的条目 / the measured items as scan defaults them: every bounded check
    measure = [i for i, item in items.items() if item["goal"] is not None]
    spec = candidates.validate(job.root, {"candidates": padded, "stages": list(stages)}, items, measure)
    folder = job.root / "scans" / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    snapshot = candidates.snapshot(job.root, spec, folder)
    backend = FakeBackend()
    backend.start()
    w = parallel.WorkerContext(backend, job.root, None, 0, {"plan": spec, "lens_dir": None})
    return [candidates.candidate(w, item) for item in snapshot][:len(cands)]


def test_candidates_need_a_stage_budget_or_say_they_only_screen(explore):
    daemon, job = explore
    write_demand(job)
    bad = call(daemon, "scan.run", job=job.id, plan=plan([{"local": {}}]))
    assert bad["error"]["code"] == ErrorCode.BAD_ARGS
    items = {"max_length": {"id": "max_length", "kind": "constant", "goal": {"max": 150.0}},
             "length": {"id": "length", "kind": "objective", "goal": {"max": 95.0}}}
    assert candidates.validate(job.root, plan([]), items, ["length"])["stages"] == []


def test_identical_candidates_do_not_count_twice(explore):
    daemon, job = explore
    write_demand(job)
    twin = plan([]) | {"candidates": [cand("a", "explore/a.zmx", hypothesis="x"),
                                       cand("copy", "explore/a.zmx", hypothesis="同一个结构换了名字")]}
    with pytest.raises(SaosError) as refused:
        candidates.validate(job.root, twin, {"max_length": {"id": "max_length", "kind": "constant", "goal": {"max": 150.0}}}, ["length"])
    assert refused.value.code == ErrorCode.BAD_ARGS and "identical" in refused.value.message


def test_screening_without_stages_makes_no_optimiser_call(explore, monkeypatch):
    """G03：没有优化阶段时，各候选仍保存证据与比较依据，优化调用次数为 0。
    Screening only: every candidate still leaves its evidence, and the optimiser is never called."""
    daemon, job = explore
    write_demand(job, with_limit(MAX_LENGTH_TIGHT))
    calls = []
    monkeypatch.setattr(FakeBackend, "optimize", lambda self, *a, **kw: calls.append("local") or {}, raising=False)
    monkeypatch.setattr(FakeBackend, "optimize_search", lambda self, *a, **kw: calls.append("search") or {}, raising=False)
    out = call(daemon, "scan.run", job=job.id, plan=plan([]))
    assert out["ok"], out
    assert calls == []
    result = json.loads((job.root / out["result"]["folder"] / "result.json").read_text(encoding="utf-8"))
    rows = result["rows"]
    assert {r["id"] for r in rows} == {"a", "b"} and all(r["stages"] == [] and "start" in r for r in rows)
    assert {r["verdict"] for r in rows} == {"invalid"} and all(r["structure"]["as_built"]["fingerprint"] for r in rows)


def test_verdicts_follow_what_failed(explore):
    """硬条件失败属于预算内无效，通过者有效。"""
    daemon, job = explore
    write_demand(job, {**DEMAND, "objectives": [LOOSE]})
    rows = judge(job, [cand("ok", "explore/a.zmx")])
    assert rows[0]["verdict"] == "valid" and rows[0]["valid"] is True and rows[0]["problems"] == []
    write_demand(job, with_limit(MAX_LENGTH_TIGHT, objectives=[LOOSE]))
    rows = judge(job, [cand("out", "explore/a.zmx")], folder_name="002")
    assert rows[0]["verdict"] == "invalid" and rows[0]["valid"] is False


def test_unmet_objective_keeps_valid_geometry_and_records_gap(explore):
    """目标项未达标仍记录真实差距，几何有效的候选可以继续优化。"""
    daemon, job = explore
    write_demand(job, {**DEMAND, "objectives": [TIGHT]})
    rows = judge(job, [cand("g", "explore/a.zmx")])
    assert rows[0]["verdict"] == "valid" and rows[0]["shortfall"] > 0 and rows[0]["problems"] == []


def test_failed_hard_limits_are_invalid_within_budget(explore):
    """所有硬条件都参与判定，同结构的无效结果只计一次。"""
    daemon, job = explore
    write_demand(job, {**DEMAND, "constants": [*DEMAND["constants"], SHORT], "objectives": [LOOSE]})
    rows = judge(job, [cand("p1", "explore/a.zmx"), cand("p2", "explore/b.zmx")])
    assert [r["verdict"] for r in rows] == ["invalid", "invalid"]
    assert all(r["problems"] == ["hard limits fail: short"] for r in rows)
    assert [r["id"] for r in candidates.counted(rows)] == ["p1"]


def test_crossing_parts_are_invalid_within_budget(explore, monkeypatch):
    daemon, job = explore
    write_demand(job, {**DEMAND, "objectives": [LOOSE]})
    monkeypatch.setattr(FakeBackend, "interference", lambda self: {"crossings": [{"surfaces": [1, 2], "depth": 0.5}], "overlaps": []})
    rows = judge(job, [cand("x1", "explore/x.zmx"), cand("x2", "explore/m.zmx")])
    assert [r["verdict"] for r in rows] == ["invalid", "invalid"]
    assert rows[1]["problems"] == ["parts cross or interpenetrate"]


def test_material_and_computation_failures_are_not_evaluated(explore, monkeypatch):
    daemon, job = explore
    write_demand(job, {**DEMAND, "objectives": [LOOSE, CRASH]})
    rows = judge(job, [cand("ok", "explore/a.zmx")])
    assert rows[0]["verdict"] == "computation" and "crash" in rows[0]["errors"]
    write_demand(job, {**DEMAND, "objectives": [LOOSE]})
    real_load = FakeBackend.load

    def load(self, path):
        if path.name == "m.zmx":
            raise MaterialError(ErrorCode.BACKEND_UNSUPPORTED, "glass F2 has no exact sourced dispersion definition", {"backend": "optiland"})
        return real_load(self, path)

    monkeypatch.setattr(FakeBackend, "load", load)
    rows = judge(job, [cand("m", "explore/m.zmx")], folder_name="002")
    assert rows[0]["verdict"] == "material" and "dispersion" in rows[0]["error"]
    assert candidates.counted(rows) == []
    # 比较页把它单列为未评估，并写出原因 / the comparison page lists it apart as not evaluated, with its reason
    page = candidates.page(job, job.root / "scans" / "002", {"stages": [], "measure": ["length"]}, rows, "zh").read_text(encoding="utf-8")
    assert "未评估" in page and "dispersion" in page and "说明" in page


def test_numeric_warnings_keep_a_valid_candidate_and_fail_an_invalid_one(explore, monkeypatch):
    """有效的候选出现数值警告仍有效（带警告记录）；无效的候选出现警告，结论记为计算出错。
    A valid candidate with a numeric warning stays valid and records it; an invalid one with a warning becomes computation."""
    daemon, job = explore
    real_operand = FakeBackend.operand

    def operand(self, name, int1, int2, data):
        if name.upper() == "WARN":
            warnings.warn("invalid value encountered in multiply", RuntimeWarning)
            return 1.0
        return real_operand(self, name, int1, int2, data)

    monkeypatch.setattr(FakeBackend, "operand", operand)
    write_demand(job, {**DEMAND, "objectives": [LOOSE, WARNED]})
    rows = judge(job, [cand("ok", "explore/a.zmx")])
    assert rows[0]["verdict"] == "valid" and rows[0]["valid"] is True
    assert rows[0]["numeric_warnings"]["count"] >= 1
    assert "invalid value encountered in multiply" in rows[0]["numeric_warnings"]["first"]
    # 无效的候选：硬条件失败，带警告的记为计算出错 / an invalid one: its hard limit fails, with a warning it is computation
    write_demand(job, with_limit(MAX_LENGTH_TIGHT, objectives=[LOOSE, WARNED]))
    rows = judge(job, [cand("bad", "explore/a.zmx")], folder_name="002")
    assert rows[0]["verdict"] == "computation" and rows[0]["numeric_warnings"]["count"] >= 1


def test_other_warnings_reach_their_output_and_are_not_counted(explore, monkeypatch):
    """非数值类警告（如弃用）原样交给原来的输出，不计入 numeric_warnings；只有 RuntimeWarning 计数。
    A non-numeric warning (deprecation) goes to the original output and is not counted; only RuntimeWarnings are."""
    daemon, job = explore
    real_operand = FakeBackend.operand

    def operand(self, name, int1, int2, data):
        if name.upper() == "DEPRECATED":
            warnings.warn("set_field_type is deprecated", DeprecationWarning)
            return 1.0
        return real_operand(self, name, int1, int2, data)

    monkeypatch.setattr(FakeBackend, "operand", operand)
    shown = []
    monkeypatch.setattr(warnings, "showwarning", lambda message, category, *args, **kwargs: shown.append(category))
    write_demand(job, {**DEMAND, "objectives": [LOOSE, DEPRECATED]})
    rows = judge(job, [cand("ok", "explore/a.zmx")])
    assert rows[0]["verdict"] == "valid" and "numeric_warnings" not in rows[0]
    assert shown and set(shown) == {DeprecationWarning}


def test_numeric_counter_keeps_only_runtime_warnings_and_forwards_the_rest():
    forwarded = []
    numeric = candidates._Numeric(lambda message, category, *args: forwarded.append(category))
    numeric("invalid value", RuntimeWarning, "f.py", 3)
    numeric("old api", DeprecationWarning, "g.py", 4)
    assert numeric.count == 1 and numeric.first.startswith("f.py:3") and forwarded == [DeprecationWarning]


def test_a_material_error_inside_a_check_is_filed_as_material(explore, monkeypatch):
    """判定里的材料错误（不是加载时的）也归为材料：类型来自 demand.measure 记下的 error_kind，不靠消息文字。
    A material error raised inside a check (not at load) is filed as material: the kind comes from measure's error_kind, not the text."""
    daemon, job = explore
    real_operand = FakeBackend.operand

    def operand(self, name, int1, int2, data):
        if name.upper() == "MATERIAL":
            raise MaterialError(ErrorCode.BACKEND_UNSUPPORTED, "glass F2 has no sourced dispersion", {"backend": "optiland"})
        return real_operand(self, name, int1, int2, data)

    monkeypatch.setattr(FakeBackend, "operand", operand)
    write_demand(job, {**DEMAND, "objectives": [LOOSE, MATERIAL_PROBE]})
    rows = judge(job, [cand("m", "explore/a.zmx")])
    assert rows[0]["verdict"] == "material" and "glass_probe" in rows[0]["errors"]
    assert candidates.counted(rows) == []


def test_twins_by_structure_count_once_even_with_different_glass(explore):
    """相同结构即使换了真实玻璃也只计一次。"""
    daemon, job = explore
    write_demand(job, {**DEMAND, "objectives": [LOOSE]})
    rows = judge(job, [cand("a", "explore/a.zmx"), cand("b", "explore/b.zmx"), cand("c", "explore/c.zmx"),
                       cand("d", "explore/d.zmx"), cand("e", "explore/e.zmx")])
    assert all(r["verdict"] == "valid" for r in rows)
    assert [r["id"] for r in candidates.counted(rows)] == ["a", "c", "d"]
    fingerprints = {r["id"]: r["structure"]["as_built"]["fingerprint"] for r in rows}
    assert fingerprints["a"] == fingerprints["b"] == fingerprints["e"] != fingerprints["c"] != fingerprints["d"]


def test_each_candidate_records_its_structure_and_the_end_of_the_run(explore):
    daemon, job = explore
    write_demand(job, {**DEMAND, "objectives": [LOOSE]})
    rows = judge(job, [cand("a", "explore/a.zmx")])
    structure = json.loads((job.root / "scans" / "001" / "candidates" / "a" / candidates.STRUCTURE_FILE).read_text(encoding="utf-8"))
    assert set(structure) == {"as_built", "end"}
    assert rows[0]["structure"]["end"]["fingerprint"] == rows[0]["structure"]["as_built"]["fingerprint"]
    assert "file" not in structure["end"] and "layout" not in structure["end"]


def row(cid, verdict, fingerprint=None, materials=()):
    """结果里的一行（只含比较要用的字段）。/ One result row, with only the fields the comparison reads."""
    fp = fingerprint or cid
    built = {"fingerprint": fp, "materials": list(materials)}
    return {"id": cid, "verdict": verdict, "valid": verdict == "valid", "problems": [],
            "structure": {"as_built": built, "end": built}}


def test_the_plan_and_the_snapshot_are_sealed_before_the_screening(explore):
    """初筛开始前封存的计划和候选副本，之后改动会被 verify_seals 发现，也让结果不再是比较。
    The plan and the candidate copies are sealed before screening; a later change shows in verify_seals."""
    daemon, job = explore
    write_demand(job)
    out = call(daemon, "scan.run", job=job.id, plan=plan([]))
    assert out["ok"], out
    folder = job.root / out["result"]["folder"]
    assert candidates.unchanged_since_start(job, folder)
    assert job.verify_seals() == []
    (folder / "candidates" / "a" / "a.zmx").write_text("{}", encoding="utf-8")
    assert not candidates.unchanged_since_start(job, folder)
    assert any(p["path"].endswith("a.zmx") for p in job.verify_seals())


def _approve(daemon, job):
    folder = call(daemon, "lens.describe", job=job.id, notes=NOTES)["result"]["folder"]
    assert call(daemon, "lens.approve", job=job.id, model=folder, quote="模型可以")["ok"]


def test_single_candidate_and_optional_hypothesis(explore):
    _, job = explore
    spec = candidates.validate(job.root, {"candidates": [{"id": "a", "lens": "explore/a.zmx"}], "stages": []}, {}, [])
    assert len(spec["candidates"]) == 1 and spec["measure"] == []


def test_counting_is_diagnostic_without_quota():
    rows = [row("a", "valid"), row("b", "invalid"), row("c", "computation"), row("d", "material"), row("twin", "valid", fingerprint="a")]
    assert [r["id"] for r in candidates.counted(rows)] == ["a", "b"]


def test_scan_select_was_removed():
    parser = build_parser()
    assert parser.parse_args(["scan", "--job", "J", "--plan", "p.json"]).func.__name__ == "cmd_scan"
    with pytest.raises(SystemExit):
        parser.parse_args(["scan", "select", "--job", "J"])


def test_missing_merit_is_configuration_error_before_screening(explore, monkeypatch):
    _, job = explore
    write_demand(job)
    calls = []
    monkeypatch.setattr(FakeBackend,"merit_rows",lambda self:[],raising=False)
    monkeypatch.setattr(FakeBackend,"optimize",lambda *args,**kwargs:calls.append("optimise"),raising=False)
    rows = judge(job,[cand("a","explore/a.zmx")],stages=[{"local":{"max_seconds":1}}])
    assert rows[0]["error_kind"] == "configuration" and rows[0]["setup_error"]
    assert "start" not in rows[0] and "verdict" not in rows[0] and calls == []


def test_candidate_configuration_error_reaches_scan_entry(explore, monkeypatch):
    from saos import scan
    _, job = explore
    monkeypatch.setattr(parallel,"dispatch",lambda *a,**kw: {"workers_available":1,"results":[{"id":"a","setup_error":"empty merit"}],"errors":{}})
    with pytest.raises(SaosError) as error:
        scan._execute(job,job.root,{"mode":"candidates"},[{"i":0}],None,None,1,{},None,lambda:False)
    assert error.value.code == ErrorCode.BAD_ARGS
    assert error.value.details["configuration_errors"] == [{"candidate":"a","message":"empty merit"}]
