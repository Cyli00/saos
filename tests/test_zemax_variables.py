"""变量类型在真实 OpticStudio 2024 R1 上的回归：面型参数列 par<N>、模型玻璃、目录内替换、非轴对称判断，
以及 3 个 worker 并发与主实例逐个计算的一致性。

迁移说明（A3）：旧版从 SAOS_BACKEND=zos 直接启动守护进程并导入 Cooke 样例（该入口已按设计拒绝）。现在在经用户认可
进入的 OpticStudio 会话上执行同样的断言；样品为两片玻璃（N-BK7 与 N-SF2），替代旧版的 Cooke 样例。
默认跳过。运行：SAOS_RUN_ZEMAX=1 uv run pytest tests/test_zemax_variables.py -s
"""

from __future__ import annotations

import os

import pytest

from saos import glasscat
from saos.protocol import ErrorCode
from zemax_support import accepted_session

pytestmark = [
    pytest.mark.zemax,
    pytest.mark.skipif(os.environ.get("SAOS_RUN_ZEMAX") != "1", reason="需要真实 OpticStudio，设置 SAOS_RUN_ZEMAX=1"),
]

ASPHERE = """
def run(ctx):
    # 第 1 面改成偶次非球面，读出各参数列的表头
    lde = ctx.zosapi.Editors.LDE
    row = ctx.system.LDE.GetSurfaceAt(1)
    row.ChangeType(row.GetSurfaceTypeSettings(lde.SurfaceType.EvenAspheric))
    ctx.mark_dirty()
    return {n: str(row.GetSurfaceCell(getattr(lde.SurfaceColumn, f"Par{n}")).Header) for n in range(1, 4)}
"""

FOLD = """
def run(ctx):
    before = bool(ctx.system.IsNonAxial)
    lde = ctx.zosapi.Editors.LDE
    row = ctx.system.LDE.InsertNewSurfaceAt(1)
    row.ChangeType(row.GetSurfaceTypeSettings(lde.SurfaceType.CoordinateBreak))
    row.GetSurfaceCell(lde.SurfaceColumn.Par3).DoubleValue = 10.0
    ctx.mark_dirty()
    return {"before": before, "after": bool(ctx.system.IsNonAxial), "mode": str(ctx.system.Mode)}
"""

CATALOGS = """
def run(ctx):
    return [str(c) for c in ctx.system.SystemData.MaterialCatalogs.GetCatalogsInUse() if str(c)]
"""

MODEL = """
def run(ctx):
    m = ctx.system.LDE.GetSurfaceAt({surface}).MaterialCell.GetSolveData()._S_MaterialModel
    return {{'nd': m.IndexNd, 'vd': m.AbbeVd, 'vary': [m.VaryIndex, m.VaryAbbe]}}
"""


def ok(reply):
    assert reply["ok"], reply
    return reply["result"]


def script(daemon, job, source, name="probe", **args):
    """运行脚本，返回脚本的返回值。 / Run a script and return its value."""
    return ok(daemon.handle({"op": "script.exec", "args": {"job": job.id, "name": name, "source": source, "args": args}}))["result"]


def test_variable_kinds_on_the_accepted_lens(tmp_path, monkeypatch):
    with accepted_session(tmp_path / "home", monkeypatch, "kinds") as (daemon, job, _):
        surfaces = ok(daemon.handle({"op": "lens.surfaces", "args": {"job": job.id}}))["surfaces"]
        glass = next(s for s in surfaces if s["material"] and s["index"] > 0)  # N-BK7

        # 模型玻璃：起点取目录玻璃的 nd、νd；--off 保留数值、停止变化 / model glass starts from the catalog glass's nd and Vd
        varied = ok(daemon.handle({"op": "opt.vary", "args": {"job": job.id, "param": "material", "mode": "model", "surfaces": [glass["index"]]}}))
        assert list(varied["solves"].values()) == ["MaterialModel"]
        model_src = MODEL.format(surface=glass["index"])
        model = script(daemon, job, model_src, "model")
        assert 1.4 < model["nd"] < 2.1 and 15 < model["vd"] < 100 and model["vary"] == [True, True], model
        ok(daemon.handle({"op": "opt.vary", "args": {"job": job.id, "param": "material", "surfaces": [glass["index"]], "on": False}}))
        fixed = script(daemon, job, model_src, "model_fixed")
        assert fixed["nd"] == pytest.approx(model["nd"]) and fixed["vary"] == [False, False]

        # 替换：不给 --catalog 时用在产目录；对空气面报错且不改在用目录，成功时才把目录加进去
        air = next(s for s in surfaces if not s["material"] and 0 < s["index"] < len(surfaces) - 1)
        before = script(daemon, job, CATALOGS, "catalogs")
        refused = daemon.handle({"op": "opt.vary", "args": {"job": job.id, "param": "material", "mode": "substitute", "surfaces": [air["index"]]}})
        assert refused["error"]["code"] == ErrorCode.BAD_ARGS
        assert script(daemon, job, CATALOGS, "catalogs") == before
        sub = ok(daemon.handle({"op": "opt.vary", "args": {"job": job.id, "param": "material", "mode": "substitute", "surfaces": [glass["index"]]}}))
        assert list(sub["solves"].values()) == ["MaterialSubstitute"]
        assert sub["outside_catalog"] == []  # 这一面现在是模型玻璃，不是哪家的牌号 / a model glass belongs to no maker
        if (glasscat.DEFAULT_GLASSCAT_DIR / "SAOS_CDGM_INPROD.AGF").is_file():
            assert sub["catalog"] == "SAOS_CDGM_INPROD" and "SAOS_CDGM_INPROD" in script(daemon, job, CATALOGS, "catalogs")
            # 第 3 面 N-SF2 是 SCHOTT 牌号，不在成都光明的目录里 / N-SF2 (SCHOTT) is not in CDGM's catalog
            other = next(s for s in surfaces if s["material"] and s["index"] > glass["index"])
            sub = ok(daemon.handle({"op": "opt.vary", "args": {"job": job.id, "param": "material", "mode": "substitute", "surfaces": [other["index"]]}}))
            assert sub["outside_catalog"] == [{"surface": other["index"], "material": other["material"]}] and "note" in sub, sub
        assert daemon.handle({"op": "opt.vary", "args": {"job": job.id, "param": "material", "mode": "model", "surfaces": [air["index"]]}})["error"]["code"] \
            == ErrorCode.BAD_ARGS

        # 面型参数列：偶次非球面的系数可以写入、读回、设为变量；没有的列报错 / asphere coefficients: write, read back, vary; missing columns refused
        headers = script(daemon, job, ASPHERE, "asphere")
        assert set(headers) == {1, 2, 3}, headers
        set_ = ok(daemon.handle({"op": "lens.set", "args": {"job": job.id, "surface": 1, "param": "par2", "value": 1e-6}}))
        assert set_["after"] == pytest.approx(1e-6) and set_["before"] == 0.0
        assert ok(daemon.handle({"op": "opt.vary", "args": {"job": job.id, "param": "par2", "surfaces": [1]}}))["solves"] == {1: "Variable"}
        assert daemon.handle({"op": "lens.set", "args": {"job": job.id, "surface": 2, "param": "par2", "value": 1}})["error"]["code"] == ErrorCode.BAD_ARGS

        # 插入带倾斜的坐标断点后系统变为非轴对称，剖面图窗口据此改用 3D / a tilted break makes the system non-axial
        fold = script(daemon, job, FOLD, "fold")
        assert fold == {"before": False, "after": True, "mode": "Sequential"}, fold


WORK = """
def work(wctx, item):
    wctx.backend.set_surface(1, "radius", item["r"])
    return wctx.backend.operand("RSCE", 6, 0, [0.0, 1.0])
"""

ONE = """
def run(ctx):
    ctx.backend.set_surface(1, "radius", ctx.args["r"])
    value = ctx.backend.operand("RSCE", 6, 0, [0.0, 1.0])
    ctx.load(ctx.args["lens"])
    return value
"""

SCAN = """
def run(ctx):
    return ctx.map("explore/work.py:work", [{"r": r} for r in ctx.args["radii"]], workers=3)
"""


def test_parallel_matches_the_main_instance_on_the_accepted_lens(tmp_path, monkeypatch):
    """3 个 standalone worker 并发算出的 RSCE 与主实例逐个算的相同。 / Three standalone workers match the main instance one by one."""
    with accepted_session(tmp_path / "home", monkeypatch, "parallel") as (daemon, job, _):
        lens = "zos-entry.zmx"
        r0 = ok(daemon.handle({"op": "lens.surfaces", "args": {"job": job.id}}))["surfaces"][1]["radius"]
        radii = [r0 * (1 + 0.002 * k) for k in range(30)]
        explore = job.root / "explore"
        explore.mkdir(parents=True, exist_ok=True)
        (explore / "work.py").write_text(WORK, encoding="utf-8")
        res = script(daemon, job, SCAN, "scan", radii=radii)
        assert res["workers_available"] >= 1 and not res["errors"] and not res["not_run"]
        for k in (0, 17, 29):
            single = script(daemon, job, ONE, f"one{k}", r=radii[k], lens=lens)
            assert res["results"][k] == pytest.approx(single, rel=1e-12), (k, res["results"][k], single)
