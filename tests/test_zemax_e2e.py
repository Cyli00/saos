"""第 2 步的验收流程，在本机真实 OpticStudio 2024 R1 上运行。

默认跳过。运行方法：SAOS_RUN_ZEMAX=1 uv run pytest -m zemax -s

迁移说明（A3）：旧版通过 CLI 执行 `daemon start` 并以 SAOS_BACKEND=zos 直接启动，并导入 Double Gauss 样例。
该入口已按设计拒绝（OpticStudio 只能经 zos begin 启动），现在从经用户认可的 OpticStudio 会话开始：
- 镜头输入改为该会话写出的 zos-entry.zmx（经 lens.import 复制到另一名字）；
- 样例专属的断言（13 个面、样例的三条建模警告）改为对两片玻璃样品的断言，并在下方注明；
- `daemon start/stop/status` 的 CLI 断言改为 shutdown 对未保存镜头的拒绝，以及夹具结束时的残留进程检查。

连续执行命令 → 加载镜头副本 → 读取并修改参数 → 保存并重新加载核对 → 正常退出，同时检查错误返回和任务隔离。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from saos import jobs
from saos.protocol import ErrorCode
from test_optiland_flow import call
from zemax_support import NOTES, accepted_session

pytestmark = [
    pytest.mark.zemax,
    pytest.mark.skipif(os.environ.get("SAOS_RUN_ZEMAX") != "1", reason="需要真实 OpticStudio，设置 SAOS_RUN_ZEMAX=1"),
]


def ok(reply):
    assert reply["ok"], reply
    return reply["result"]


def refused(reply, code):
    assert not reply["ok"] and reply["error"]["code"] == code, reply
    return reply["error"]["code"]


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_accepted_lens_io_errors_isolation_scripts_and_analyses(saos_home, monkeypatch):
    with accepted_session(saos_home, monkeypatch, "e2e-a") as (daemon, job_a, _):
        entry = job_a.root / "lenses" / "zos-entry.zmx"
        assert entry.is_file()
        entry_hash = sha(entry)
        job_b = jobs.create_job("e2e-b")

        # 加载镜头副本：导入到任务目录，原文件不变
        ok(call(daemon, "lens.import", job=job_a.id, src=str(entry), name="start.zmx"))
        ok(call(daemon, "lens.load", job=job_a.id, file="start.zmx"))

        # 连续读取
        info = ok(call(daemon, "lens.info", job=job_a.id))
        surfaces = ok(call(daemon, "lens.surfaces", job=job_a.id))["surfaces"]
        assert info["system"]["num_surfaces"] == len(surfaces) == 6  # 物面 + 4 个折射面 + 像面 / object + 4 refracting + image
        r1 = surfaces[1]["radius"]
        effl_before = ok(call(daemon, "lens.operand", job=job_a.id, type="EFFL", int2=2))["value"]

        # 建模说明：样例的材料、面说明和一阶分析与 Double Gauss 不同，这里只核对结构与目录玻璃
        described = ok(call(daemon, "lens.describe", job=job_a.id, notes=NOTES, lang="en"))
        folder = job_a.root / described["folder"]
        assert (folder / "index.html").is_file()
        model = json.loads((folder / "model.json").read_text(encoding="utf-8"))["model"]
        glass = [s for s in model["surfaces"] if s["material_source"]["kind"] == "catalog"]
        # 样品只有一个波长（0.5876 µm），所以不检查色散（n 在各波长间不同）；只检查目录来源和折射率范围
        # the sample has one wavelength, so there is no dispersion to check; only the catalog source and the index range
        assert {s["material_source"]["name"] for s in glass} == {"N-BK7", "N-SF2"}
        assert all(s["material_source"]["catalogs"] and min(s["n"]) > 1.4 for s in glass)
        assert all(isinstance(v, str) and (folder / v).is_file() for v in described["layout"].values()), described["layout"]

        # 修改参数：半径、玻璃
        new_r1 = round(r1 * 1.02, 6)
        changed = ok(call(daemon, "lens.set", job=job_a.id, surface=1, param="radius", value=new_r1))
        assert changed["after"] == pytest.approx(new_r1, rel=1e-12) and changed["dirty"] is True
        ok(call(daemon, "lens.set", job=job_a.id, surface=3, param="material", value="N-SK16"))
        effl_after = ok(call(daemon, "lens.operand", job=job_a.id, type="EFFL", int2=2))["value"]
        assert effl_after != pytest.approx(effl_before, rel=1e-9)

        # 明确的错误返回
        refused(call(daemon, "lens.set", job=job_a.id, surface=3, param="material", value="NOTAGLASS"), ErrorCode.BAD_ARGS)
        refused(call(daemon, "lens.set", job=job_a.id, surface=99, param="radius", value=1), ErrorCode.BAD_ARGS)
        refused(call(daemon, "lens.operand", job=job_a.id, type="NOSUCHOP"), ErrorCode.BAD_ARGS)
        refused(call(daemon, "lens.load", job=job_a.id, file="missing.zmx"), ErrorCode.FILE_NOT_FOUND)

        # 任务隔离
        refused(call(daemon, "lens.surfaces", job=job_b.id), ErrorCode.JOB_MISMATCH)
        refused(call(daemon, "lens.save", job=job_a.id, file=f"../../{job_b.id}/lenses/x.zmx"), ErrorCode.PATH_OUTSIDE_JOB)
        ok(call(daemon, "lens.import", job=job_b.id, src=str(entry), name="b.zmx"))
        refused(call(daemon, "lens.load", job=job_b.id, file="b.zmx"), ErrorCode.UNSAVED_CHANGES)
        refused(call(daemon, "shutdown"), ErrorCode.UNSAVED_CHANGES)

        # 保存并重新加载核对
        saved = ok(call(daemon, "lens.save", job=job_a.id, file="r001.zmx"))
        assert saved["verified"] is True and saved["surfaces_checked"] == len(surfaces)
        # job_b 没有进入 OpticStudio 阶段，它的 .zmx（没有伴随快照）只能由 OpticStudio 打开，Optiland 不读它：这是按设计的拒绝
        # job_b never entered the OpticStudio stage; its .zmx has no companion, so only OpticStudio can open it and Optiland refuses it
        refused(call(daemon, "lens.load", job=job_b.id, file="b.zmx"), ErrorCode.BACKEND_UNSUPPORTED)
        ok(call(daemon, "lens.load", job=job_a.id, file="r001.zmx"))
        reloaded = ok(call(daemon, "lens.surfaces", job=job_a.id))["surfaces"]
        assert reloaded[1]["radius"] == pytest.approx(new_r1, rel=1e-12)
        assert reloaded[3]["material"] == "N-SK16"
        assert ok(call(daemon, "lens.operand", job=job_a.id, type="EFFL", int2=2))["value"] == pytest.approx(effl_after, rel=1e-12)

        # 脚本执行：直接用 ZOS-API，出错不影响后台进程
        probe = """
def run(ctx):
    lde = ctx.system.LDE
    standard = ctx.zosapi.Editors.LDE.SurfaceType.Standard
    return {'surfaces': int(lde.NumberOfSurfaces), 'standard': str(standard), 'lens': ctx.lens.name, 'k': ctx.args['k']}
"""
        ran = ok(call(daemon, "script.exec", job=job_a.id, name="probe", source=probe, args={"k": 2}))
        assert ran["result"] == {"surfaces": len(surfaces), "standard": "Standard", "lens": "r001.zmx", "k": 2}
        assert (job_a.root / ran["script"]).is_file()
        bad = "def run(ctx):\n    return ctx.system.LDE.GetSurfaceAt(999).Radius\n"
        refused(call(daemon, "script.exec", job=job_a.id, name="probe_bad", source=bad, args={}), ErrorCode.SCRIPT_ERROR)
        assert ok(call(daemon, "lens.info", job=job_a.id))["dirty"] is False

        # 分析导出：序列、网格、点列图数据和文本全文都存进任务目录
        mtf = ok(call(daemon, "lens.analysis", job=job_a.id, type="FftMtf", settings={"MaximumFrequency": "50", "Field": "all"}))
        assert mtf["series"] and all(len(s["x"]) > 10 for s in mtf["series"]) and mtf["settings"]["MaximumFrequency"].startswith("50")
        assert (job_a.root / mtf["file"]).is_file() and (job_a.root / mtf["text"]).is_file()
        spot = ok(call(daemon, "lens.analysis", job=job_a.id, type="StandardSpot"))
        assert spot["spot"]["rms_radius"][0][0] > 0
        wave = ok(call(daemon, "lens.analysis", job=job_a.id, type="WavefrontMap"))
        assert wave["grids"][0]["stats"] is not None and "values" not in wave["grids"][0]
        refused(call(daemon, "lens.analysis", job=job_a.id, type="FftMtf", settings={"Bogus": "1"}), ErrorCode.BAD_ARGS)
        assert "StandardSpot" in ok(call(daemon, "lens.analysis", job=job_a.id, list=True))["types"]
        assert ok(call(daemon, "lens.info", job=job_a.id))["dirty"] is False

        # 文件互不混用：各任务的 lenses/ 顶层只有自己导入或保存的镜头
        lenses_a = {p.name for p in (job_a.root / "lenses").glob("*.zmx")}
        lenses_b = sorted(p.name for p in (job_b.root / "lenses").glob("*.zmx"))
        assert {"start.zmx", "r001.zmx"} <= lenses_a and lenses_b == ["b.zmx"]
        assert sha(job_a.root / "lenses" / "start.zmx") == entry_hash
        assert sha(entry) == entry_hash

        log_ops = [json.loads(line)["op"] for line in (job_a.root / "log.jsonl").read_text(encoding="utf-8").splitlines()]
        assert "lens.save" in log_ops and "zos.begin" in log_ops
