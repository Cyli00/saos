"""一个后台进程持有 rtc 与 OpticStudio 两个后端（ADR-0018）的离线回归。

离线没有 OpticStudio：OpticStudio 的位置放第二个 rtc 实例，它的 describe_model(None) 与 trace_batch 和 ZOS 后端同字段，
所以交接比对走的是真实代码路径；它与 rtc 读同一个文件，结论应当一致，人为改动它的描述才出现差异。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from saos import jobs, prescription
from saos.backend.rtc import RtcBackend
from saos.daemon import Daemon
from saos.protocol import ErrorCode, SaosError

CASES = Path(__file__).parent / "fixtures" / "cases"


def _glasscat_ready() -> bool:
    from rtc.materials import catalog_dir

    return any(p.stem.upper() == "SCHOTT" for p in catalog_dir().glob("*.[Aa][Gg][Ff]"))


pytestmark = pytest.mark.skipif(not _glasscat_ready(), reason="需要本机 Documents/Zemax/Glasscat/SCHOTT.AGF")


def _daemon():
    started = []
    stand_in = RtcBackend(None, standalone=True)

    def start_zos():
        started.append(1)
        return stand_in, {"backend": "zos", "version": "stand-in"}

    rtc = RtcBackend(None, standalone=True)
    return Daemon(rtc, rtc.identity(), start_zos), stand_in, started


def call(daemon, op, **args):
    return daemon.handle({"op": op, "args": args})


def _built(daemon, job):
    p = prescription.normalize(json.loads((CASES / "sequential.json").read_text(encoding="utf-8")))
    reply = call(daemon, "lens.build", job=job.id, prescription=p, file="a.zmx")
    assert reply["ok"], reply


def test_opticstudio_starts_on_demand_and_the_lens_is_handed_over_both_ways(saos_home):
    daemon, stand_in, started = _daemon()
    job = jobs.create_job("both")
    # 访谈阶段在 rtc 上，不启动 OpticStudio / the interview runs on rtc without starting OpticStudio
    _built(daemon, job)
    assert daemon.active == "rtc" and not started and daemon.pool.backend == "rtc"

    pinned = call(daemon, "daemon.use", backend="zos", job=job.id)["result"]
    assert started == [1] and daemon.active == "zos" and daemon.backend is stand_in and daemon.pool.backend == "zos"
    switch = pinned["backend_switch"]
    assert switch["from"] == "rtc" and switch["to"] == "zos"
    assert switch["handover"]["agrees"], switch["handover"]
    assert switch["handover"]["rays"]["rays"] > 0 and switch["handover"]["rays"]["ok_one_side"] == 0

    # 放开后按阶段回到 rtc，OpticStudio 不再重启 / back on rtc by stage; OpticStudio is not started again
    call(daemon, "daemon.use", backend="auto")
    reply = call(daemon, "lens.operand", job=job.id, type="EFFL")
    assert reply["ok"] and reply["result"]["backend_switch"]["to"] == "rtc" and daemon.active == "rtc"
    assert reply["result"]["backend_switch"]["handover"]["agrees"]
    call(daemon, "daemon.use", backend="zos", job=job.id)
    assert started == [1]


def test_unsaved_changes_are_saved_before_a_handover(saos_home):
    daemon, _, _ = _daemon()
    job = jobs.create_job("unsaved")
    _built(daemon, job)
    assert call(daemon, "lens.set", job=job.id, surface=1, param="radius", value=70.0)["ok"]
    refused = call(daemon, "daemon.use", backend="zos", job=job.id)["error"]
    assert refused["code"] == ErrorCode.UNSAVED_CHANGES and daemon.active == "rtc"
    assert call(daemon, "lens.save", job=job.id)["ok"]
    assert call(daemon, "daemon.use", backend="zos", job=job.id)["ok"] and daemon.active == "zos"
    assert daemon.backend.surfaces()[1]["radius"] == pytest.approx(70.0)


def test_a_lens_rtc_cannot_reproduce_stays_on_opticstudio(saos_home, monkeypatch):
    daemon, _, _ = _daemon()
    job = jobs.create_job("stays")
    _built(daemon, job)
    call(daemon, "daemon.use", backend="zos", job=job.id)
    calls = []

    def refuse(path):
        calls.append(path)
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, "rtc does not trace mirrors")

    monkeypatch.setattr(daemon.rtc, "load", refuse)
    call(daemon, "daemon.use", backend="auto")
    for _ in range(2):
        reply = call(daemon, "lens.operand", job=job.id, type="EFFL")
        assert reply["ok"] and daemon.active == "zos"
        assert reply["result"]["backend_switch"]["stayed"] == "rtc does not trace mirrors"
    assert len(calls) == 1  # 同一文件不再重试 / the same file is not retried


def test_a_handover_difference_is_kept_as_unverified_provenance(saos_home, monkeypatch):
    daemon, stand_in, _ = _daemon()
    job = jobs.create_job("differs")
    _built(daemon, job)
    call(daemon, "daemon.use", backend="zos", job=job.id)
    describe = stand_in.describe_model

    def shifted(lens=None):
        model = describe(lens)
        model["surfaces"][1]["radius"] += 1e-3
        return model

    monkeypatch.setattr(stand_in, "describe_model", shifted)
    call(daemon, "daemon.use", backend="auto")
    switch = call(daemon, "lens.operand", job=job.id, type="EFFL")["result"]["backend_switch"]
    assert daemon.active == "rtc" and not switch["handover"]["agrees"]
    assert any("surface 1 radius" in m for m in switch["handover"]["mismatches"])
    assert any(u.startswith("differs from OpticStudio at handover: surface 1 radius") for u in daemon.rtc.provenance()["unverified"])
    # 换镜头后差异随之清空 / a new lens clears it
    assert call(daemon, "lens.load", job=job.id, file="a.zmx")["ok"]
    assert not any("handover" in u for u in daemon.rtc.unverified())


def test_the_object_semi_diameter_is_left_to_the_ray_comparison(saos_home, monkeypatch):
    # 真实 OpticStudio 在物距无穷时给物面半口径 inf，rtc 为 0；追迹不用它 / OpticStudio reports inf at an infinite object, rtc 0
    daemon, stand_in, _ = _daemon()
    job = jobs.create_job("object-sd")
    _built(daemon, job)
    surfaces = stand_in.surfaces

    def infinite_object():
        rows = surfaces()
        rows[0]["semi_diameter"] = float("inf")
        return rows

    monkeypatch.setattr(stand_in, "surfaces", infinite_object)
    switch = call(daemon, "daemon.use", backend="zos", job=job.id)["result"]["backend_switch"]
    assert switch["handover"]["agrees"], switch["handover"]["mismatches"]


NOTES = {"surfaces": {"0": "object"}, "assumptions": ["test"], "first_order": ["test"],
         "exploration": {"fixed_by_user": {"parts": "the test lens", "quote": "就用这个镜头"}}}


def test_an_rtc_only_daemon_describes_the_model_and_still_refuses_opticstudio_operations(saos_home):
    rtc = RtcBackend(None, standalone=True)
    daemon = Daemon(rtc, rtc.identity())
    assert not daemon.dual and daemon.rtc_instance
    job = jobs.create_job("rtc-only")
    _built(daemon, job)
    # 模型页不需要 OpticStudio：剖面图由 saos 用 viz 数据画 / the model page needs no OpticStudio: saos draws the section
    reply = call(daemon, "lens.describe", job=job.id, notes=NOTES)
    assert reply["ok"], reply
    folder = job.root / reply["result"]["folder"]
    assert reply["result"]["layout"] == {"cross-section": "layout-cross-section.svg"}
    svg = (folder / "layout-cross-section.svg").read_text(encoding="utf-8")
    assert svg.startswith('<svg xmlns="http://www.w3.org/2000/svg"') and 'class="glass"' in svg and 'class="ray"' in svg
    assert "layout-cross-section.svg" in (folder / "index.html").read_text(encoding="utf-8")
    assert call(daemon, "lens.approve", job=job.id, model=reply["result"]["folder"], quote="可以")["ok"]
    assert call(daemon, "round.confirm", job=job.id)["error"]["code"] == ErrorCode.BACKEND_UNSUPPORTED
    assert call(daemon, "daemon.use", backend="zos")["error"]["code"] == ErrorCode.BAD_ARGS


def test_the_model_page_no_longer_starts_opticstudio(saos_home):
    daemon, _, started = _daemon()
    job = jobs.create_job("both")
    _built(daemon, job)
    assert call(daemon, "lens.describe", job=job.id, notes=NOTES)["ok"]
    assert daemon.active == "rtc" and not started


def test_a_pin_holds_within_its_stage_and_is_released_when_the_stage_changes(saos_home, monkeypatch):
    # 10-30 的任务固定 zos 后没有放开，设计阶段 11 轮全在 OpticStudio 上 / a pin left in place ran 11 design rounds on OpticStudio
    daemon, _, _ = _daemon()
    job = jobs.create_job("pin")
    _built(daemon, job)
    stage = {"now": "design"}
    monkeypatch.setattr(daemon, "_stage_of", lambda args: stage["now"])
    pinned = call(daemon, "daemon.use", backend="zos", job=job.id)["result"]
    assert pinned["pinned_stage"] == "design" and daemon.active == "zos"

    held = call(daemon, "lens.operand", job=job.id, type="EFFL")["result"]
    assert daemon.active == "zos"
    assert held["backend_pin"] == {"backend": "zos", "pinned_at": "design", "stage": "design", "stage_rule": "rtc"}

    stage["now"] = "model_approve"
    released = call(daemon, "lens.operand", job=job.id, type="EFFL")["result"]
    assert released["backend_pin"] == {"released": "zos", "pinned_at": "design", "stage": "model_approve"}
    assert daemon.preference == "auto" and daemon.pinned_stage is None and daemon.active == "rtc"
    assert "backend_pin" not in call(daemon, "lens.operand", job=job.id, type="EFFL")["result"]


def test_a_prescription_rtc_cannot_build_is_built_on_opticstudio(saos_home):
    # 建模归 rtc 后（ADR-0020），带倾斜等项的处方不能卡在 rtc 上 / once modelling is on rtc, such prescriptions must not stall there
    daemon, _, started = _daemon()
    job = jobs.create_job("tilted")
    raw = json.loads((CASES / "sequential.json").read_text(encoding="utf-8"))
    raw["surfaces"][0]["decenter_tilt"] = {"before": {"tilt_x": 5.0}}
    reply = call(daemon, "lens.build", job=job.id, prescription=prescription.normalize(raw), file="t.zmx")
    # 离线的 OpticStudio 替身也是 rtc，建不了；要看的是路由 / the offline stand-in is rtc too; the routing is what counts
    switch = (reply.get("result") or reply["error"]["details"])["backend_switch"]
    assert started == [1] and daemon.active == "zos"
    assert switch["to"] == "zos" and "decenter_tilt" in switch["reason"]


def test_thermal_conditions_run_through_the_daemon_and_hand_over(saos_home):
    """处方声明工况、向导按结构展开、逐结构变量和优化都走后台进程；存盘后交接给另一个后端两边一致。"""
    daemon, stand_in, started = _daemon()
    job = jobs.create_job("thermal")
    raw = json.loads((CASES / "sequential.json").read_text(encoding="utf-8"))
    raw.setdefault("environment", {})["adjust_index"] = True
    raw["thermal"] = {"temperatures": [-40.0, 65.0], "housing_tce": 23.6}
    reply = call(daemon, "lens.build", job=job.id, prescription=raw, file="t.zmx")
    assert reply["ok"], reply
    assert not started
    rtc = daemon.backend
    assert rtc.configuration_count() == 3 and rtc.lens.thermal_code == 5
    reply = call(daemon, "opt.merit", job=job.id, action="wizard", settings={"data": "Spot Radius", "reference": "Centroid", "rings": 2, "arms": 6})
    assert reply["ok"], reply
    assert [r.int1 for r in rtc.merit if r.type == "CONF"] == [1, 1, 2, 3]
    rows = {(o["type"], tuple(o["params"])): o["row"] for o in rtc._configurations()["operands"]}
    reply = call(daemon, "opt.vary", job=job.id, config_rows=[rows[("THIC", (3,))]], configs=[1], on=True)
    assert reply["ok"], reply
    before = rtc.merit_value()
    # opt.local 还要过需求门禁，这里要看的是优化器和存盘 / opt.local also needs a demand; the optimiser and the save are what counts here
    rtc.optimize("10")
    assert rtc.merit_value() < before
    daemon.loaded.dirty = True
    saved = call(daemon, "lens.save", job=job.id)
    assert saved["ok"], saved
    pinned = call(daemon, "daemon.use", backend="zos", job=job.id)
    assert pinned["ok"], pinned
    assert stand_in.configuration_count() == 3
    assert rtc.handover["agrees"], rtc.handover
    assert [o["values"] for o in stand_in._configurations()["operands"] if o["type"] == "TEMP"] == [[20.0, -40.0, 65.0]]


def test_make_thermal_from_a_script_context(saos_home):
    from types import SimpleNamespace

    from saos import kit

    backend = RtcBackend(None, standalone=True)
    backend.build(prescription.normalize(json.loads((CASES / "sequential.json").read_text(encoding="utf-8"))))
    ctx = SimpleNamespace(backend=backend, cache={"x": 1})
    with pytest.raises(SaosError):
        kit.make_thermal(ctx, [20.0, 20.0], 23.6)
    with pytest.raises(SaosError):
        kit.make_thermal(ctx, [-40.0], "23.6")
    out = kit.make_thermal(ctx, [-40.0], 23.6)
    assert out["count"] == 2 and ctx.cache == {}
    assert backend.lens.environment.adjust is True
    with pytest.raises(SaosError) as exc:
        kit.make_thermal(ctx, [65.0], 23.6)
    assert exc.value.code == ErrorCode.BACKEND_UNSUPPORTED
