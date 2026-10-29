"""脚本编辑接口：评价函数行的构造与引用、ctx.merit_add / solve / insert_surface（假后端），以及 exec 的直接访问提示。"""

from __future__ import annotations

import pytest

from saos import jobs
from saos.backend.fake import FakeBackend
from saos.ctx import rows
from saos.ctx.hints import direct_api_hints
from saos.daemon import Daemon


@pytest.fixture
def daemon(saos_home):
    backend = FakeBackend()
    return Daemon(backend, backend.start(), "standalone")


@pytest.fixture
def loaded(daemon, fake_lens_file):
    job = jobs.create_job("edit")
    daemon.op_import({"job": job.id, "src": str(fake_lens_file), "name": "a.zmx"})
    daemon.op_load({"job": job.id, "file": "a.zmx"})
    return job


def run(daemon, job, source):
    return daemon.handle({"op": "script.exec", "args": {"job": job.id, "name": "edit", "source": source, "args": {}}})


def test_sag_gap_reads_the_thickness_and_resolves_references_at_the_insertion_point():
    specs = rows.compile_rows(rows.sag_gap(4, 5, [(0.1, 0)], minimum=0.2), 37)
    assert [r["type"] for r in specs] == ["TTHI", "SSAG", "SSAG", "DIFF", "SUMM", "OPGT"]
    assert specs[0]["params"] == [4, 4]
    assert specs[3]["params"] == [39, 38]          # 后面矢高 − 前面矢高 / back sag − front sag
    assert specs[4]["params"] == [37, 40]          # 厚度 + 差 / thickness + difference
    assert specs[5]["params"] == [41] and specs[5]["target"] == 0.2


@pytest.mark.parametrize("specs", [
    [{"type": "DIFF", "params": [{"ref": "missing"}]}],
    [{"id": "x", "type": "EFFL"}, {"id": "x", "type": "EFFL"}],
    [{"type": "EFFL", "params": [float("nan")]}],
    [{"type": "EFFL", "weight": -1}],
    [{"type": "EFFL", "params": [], "int1": 1}],
    [{"type": "EFFL", "data": [0] * 7}],
])
def test_ambiguous_or_invalid_rows_are_refused(specs):
    with pytest.raises(ValueError):
        rows.compile_rows(specs, 1)


def test_helpers_keep_their_physical_assumptions_explicit():
    assert rows.thickness_bounds(2, minimum=1, maximum=3) == [
        {"type": "CTGT", "params": [2], "target": 1, "weight": 1},
        {"type": "CTLT", "params": [2], "target": 3, "weight": 1},
    ]
    na = rows.na_proxy(12, 1, index=1.5, target=.3, minimum=.18, axis="x")
    assert na[0]["type"] == "RAGA" and na[0]["params"] == [12, 1, 0, 0, 1, 0]
    assert na[0]["target"] == pytest.approx(.2) and na[1]["target"] == pytest.approx(.12)
    with pytest.raises(ValueError):
        rows.thickness_bounds(2, minimum=3, maximum=1)
    with pytest.raises(ValueError):
        rows.sag_gap(1, 3, [(0, 0)])


def test_merit_add_keeps_order_before_dmfs_and_marks_the_lens_dirty(daemon, loaded):
    daemon.backend.lens["merit"] = [{"type": "EFFL", "params": [0, 1], "target": 50, "weight": 1},
                                    {"type": "DMFS", "params": [], "target": 0, "weight": 0}]
    source = ("def run(ctx):\n"
              "    return ctx.merit_add([{'id': 'a', 'type': 'TTHI', 'params': [1, 1]},\n"
              "                          {'type': 'OPGT', 'params': [{'ref': 'a'}], 'target': 2}])\n")
    out = run(daemon, loaded, source)
    assert out["ok"], out
    assert out["result"]["result"] == [2, 3]
    assert [r["type"] for r in daemon.backend.lens["merit"]] == ["EFFL", "TTHI", "OPGT", "DMFS"]
    assert daemon.backend.lens["merit"][2]["params"] == [2]
    assert daemon.loaded.dirty


def test_merit_add_reports_bad_rows_as_bad_args(daemon, loaded):
    out = run(daemon, loaded, "def run(ctx):\n    return ctx.merit_add([{'type': 'DIFF', 'params': [{'ref': 'x'}]}])\n")
    assert not out["ok"] and out["error"]["code"] == "BAD_ARGS"


def test_solve_and_insert_surface_validate_before_they_touch_the_lens(daemon, loaded):
    source = ("def run(ctx):\n"
              "    a = ctx.solve('2.thickness', 'marginal_ray_height', zone=0.7)\n"
              "    b = ctx.insert_surface(2, 'EvenAspheric', thickness=3.0)\n"
              "    return {'solve': a, 'insert': b, 'thickness': ctx.get('2.thickness'), 'n': len(ctx.backend.surfaces())}\n")
    out = run(daemon, loaded, source)
    assert out["ok"], out
    assert out["result"]["result"] == {"solve": {"type": "MarginalRayHeight"}, "insert": {"index": 2, "type": "EvenAspheric"},
                                       "thickness": 3.0, "n": 5}
    for bad in ("ctx.solve('2.thickness', 'pickup')", "ctx.solve('2.thickness', 'f_number', value=float('nan'))",
                "ctx.solve('2.material', 'variable')", "ctx.solve('2.radius', 'curvature')"):
        out = run(daemon, loaded, f"def run(ctx):\n    return {bad}\n")
        assert not out["ok"] and out["error"]["code"] == "BAD_ARGS", bad


def test_check_scripts_cannot_edit(daemon, loaded):
    from saos.scripting import ScriptContext

    ctx = ScriptContext(daemon, loaded, {}).child({})
    with pytest.raises(Exception, match="read-only"):
        ctx.merit_add([{"type": "EFFL"}])


def test_direct_api_hints_follow_only_certain_surface_numbers():
    source = ("def run(ctx):\n"
              "    lde = ctx.system.LDE\n"
              "    r = lde.GetSurfaceAt(3).Radius\n"
              "    ctx.system.LDE.GetSurfaceAt(4).Thickness = 2\n"
              "    for i in range(3):\n"
              "        lde.GetSurfaceAt(i).Conic\n")
    hints = direct_api_hints(source)
    assert [h["suggestion"] for h in hints] == ['ctx.get("3.radius")', 'ctx.set("4.thickness", value)']
    assert direct_api_hints("def run(:\n") == []


def test_constraint_builders_wire_their_references():
    bend = rows.compile_rows(rows.ray_bend([3], 1, [(0, 1, 0, 1)], maximum=30), 1)
    assert [r["type"] for r in bend] == ["RAID", "RAED", "DIFF", "ABSO", "OPLT"]
    assert bend[2]["params"] == [1, 2] and bend[3]["params"] == [3] and bend[4]["params"] == [4] and bend[4]["target"] == 30
    dist = rows.compile_rows(rows.distortion(2, [0.7, 1.0], maximum=1.5), 10)
    assert [r["type"] for r in dist] == ["DISG", "OPLT", "OPGT"] * 2
    assert dist[0]["params"][:2] == [1, 2] and [r["target"] for r in dist[1:3]] == [1.5, -1.5]
    assert [r["type"] for r in rows.clearances(1, 6, glass_center=(1, None), air_edge=(None, 5))] == ["MNCG", "MXEA"]
    track = rows.compile_rows(rows.total_track(minimum=20, maximum=60), 5)
    assert [(r["type"], r["params"], r["target"]) for r in track] == [("TOTR", [0, 0], 0), ("OPGT", [5], 20), ("OPLT", [5], 60)]
    for bad in (lambda: rows.clearances(2, 1, glass_center=(1, 2)), lambda: rows.clearances(1, 2),
                lambda: rows.chief_ray_angle(5, 1, [], maximum=30), lambda: rows.total_track(minimum=5, maximum=1)):
        with pytest.raises(ValueError):
            bad()


def test_unknown_ctx_names_point_to_the_native_entries(daemon, loaded):
    reply = run(daemon, loaded, "def run(ctx):\n    return ctx.zos\n")
    assert reply["error"]["code"] == "SCRIPT_ERROR"
    assert "ctx.system" in reply["error"]["message"] and "ctx.zosapi" in reply["error"]["message"]
    # 定义过的属性照常可用 / defined attributes still work
    assert run(daemon, loaded, "def run(ctx):\n    return ctx.dirty\n")["result"]["result"] is False


def test_ctx_info_is_the_system_part_of_lens_info(daemon, loaded):
    reply = run(daemon, loaded, "def run(ctx):\n    return ctx.info()\n")
    assert reply["result"]["result"] == daemon.handle({"op": "lens.info", "args": {"job": loaded.id}})["result"]["system"]
