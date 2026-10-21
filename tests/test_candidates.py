"""结构探索（candidates 计划）的离线测试：假后端，worker 子进程也用假后端。
Structure exploration (candidates plans) offline: fake backend, worker subprocesses included."""

from __future__ import annotations

import json

import pytest
import yaml

from saos import candidates, demand, jobs
from saos.backend.fake import FakeBackend
from saos.daemon import Daemon
from saos.protocol import ErrorCode, SaosError

LINE = demand.QUESTION_LINE["zh"]
LENGTH = {"id": "length", "description": "total track", "check": {"operand": {"type": "TOTR"}}, "origin": "user",
          "must": {"max": 95.0}, "scale": 5.0, "source": "user"}
DEMAND = {"schema": 2, "constants": [{"id": "lens", "description": "the sample singlet", "value": "a.zmx", "source": "user"}],
          "variables": [{"id": "radii", "description": "both radii are free"}], "objectives": [LENGTH],
          "summary": f"用户要总长不超过 95 mm。{LINE}"}


def call(daemon, op, **args):
    return daemon.handle({"op": op, "args": args})


def write_demand(job, data=DEMAND, confirm="确认，开始吧"):
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    if confirm:
        demand.confirm(job.root, confirm)


@pytest.fixture
def explore(saos_home, fake_lens_file):
    """三个互不相同的起始结构：两份镜头文件（半径不同）加一份处方。 / Three different starts: two lens files and a prescription."""
    backend = FakeBackend()
    daemon = Daemon(backend, backend.start(), "standalone")
    job = jobs.create_job("explore")
    data = json.loads(fake_lens_file.read_text(encoding="utf-8"))
    other = json.loads(json.dumps(data))
    other["surfaces"][1]["radius"] = 60.0
    # 候选路径相对任务目录（不是 lenses/）/ candidate paths are relative to the job folder, not lenses/
    (job.root / "explore").mkdir(parents=True, exist_ok=True)
    (job.root / "explore" / "a.zmx").write_text(json.dumps(data), encoding="utf-8")
    (job.root / "explore" / "b.zmx").write_text(json.dumps(other), encoding="utf-8")
    yield daemon, job
    daemon.pool.close()


def plan(stages, **extra):
    return {"candidates": [{"id": "a", "lens": "explore/a.zmx", "hypothesis": "单片，基线"},
                           {"id": "b", "lens": "explore/b.zmx", "hypothesis": "单片，较大曲率"}],
            "stages": stages, "measure": ["length"], **extra}


def test_candidates_need_a_stage_budget_or_say_they_only_screen(explore):
    daemon, job = explore
    write_demand(job)
    bad = call(daemon, "scan.run", job=job.id, plan=plan([{"local": {}}]))
    assert bad["error"]["code"] == ErrorCode.BAD_ARGS
    assert candidates.validate(job.root, plan([]), {"length": {"kind": "limit"}}, ["length"])["stages"] == []


def test_identical_candidates_do_not_count_twice(explore):
    daemon, job = explore
    write_demand(job)
    twin = plan([]) | {"candidates": [{"id": "a", "lens": "explore/a.zmx", "hypothesis": "x"},
                                       {"id": "copy", "lens": "explore/a.zmx", "hypothesis": "同一个结构换了名字"}]}
    with pytest.raises(SaosError) as refused:
        candidates.validate(job.root, twin, {"length": {"kind": "limit"}}, ["length"])
    assert refused.value.code == ErrorCode.BAD_ARGS and "identical" in refused.value.message


def test_screening_without_stages_makes_no_optimiser_call(explore, monkeypatch):
    """G03：没有优化阶段时，各候选仍保存证据与比较依据，优化调用次数为 0。
    Screening only: every candidate still leaves its evidence, and the optimiser is never called."""
    daemon, job = explore
    write_demand(job)
    calls = []
    monkeypatch.setattr(FakeBackend, "optimize", lambda self, *a, **kw: calls.append("local") or {}, raising=False)
    monkeypatch.setattr(FakeBackend, "optimize_search", lambda self, *a, **kw: calls.append("search") or {}, raising=False)
    out = call(daemon, "scan.run", job=job.id, plan=plan([]))
    assert out["ok"], out
    assert calls == []
    result = json.loads((job.root / out["result"]["folder"] / "result.json").read_text(encoding="utf-8"))
    rows = result["rows"]
    assert {r["id"] for r in rows} == {"a", "b"} and all(r["stages"] == [] and "start" in r for r in rows)


def _sealed_comparison(job, rows, demand_sha="d", method_sha="m", name="001"):
    folder = job.root / "scans" / name
    folder.mkdir(parents=True)
    (folder / "plan.json").write_text(json.dumps({"mode": "candidates", "demand_sha": demand_sha, "method_sha": method_sha}), encoding="utf-8")
    (folder / "result.json").write_text(json.dumps({"mode": "candidates", "rows": rows}), encoding="utf-8")
    job.seal(folder / "plan.json", folder / "result.json")


VALID = {"valid": True, "problems": [], "id": "ok"}
PHYSICS = {"valid": False, "problems": ["parts cross or interpenetrate"], "id": "cross"}
FAILED = {"valid": False, "problems": ["no finite shortfall"], "id": "broken"}


def test_two_physical_eliminations_and_one_valid_complete_the_comparison(explore):
    """G01：一个可行、两个有物理证据淘汰，比较成立，不死锁。 / One feasible, two physically eliminated: the comparison stands."""
    daemon, job = explore
    _sealed_comparison(job, [VALID, PHYSICS, PHYSICS])
    assert candidates.structure_compared(job, "d", "m")


def test_failed_computations_do_not_count_as_candidates(explore):
    daemon, job = explore
    _sealed_comparison(job, [VALID, PHYSICS, FAILED])
    assert not candidates.structure_compared(job, "d", "m")


def test_a_comparison_needs_at_least_one_valid_candidate(explore):
    daemon, job = explore
    _sealed_comparison(job, [PHYSICS, PHYSICS, PHYSICS])
    assert not candidates.structure_compared(job, "d", "m")


def test_changed_checks_or_demand_void_the_comparison(explore):
    daemon, job = explore
    _sealed_comparison(job, [VALID, VALID, VALID])
    assert candidates.structure_compared(job, "d", "m")
    assert not candidates.structure_compared(job, "d", "changed-checks")
    assert not candidates.structure_compared(job, "changed-demand", "m")
