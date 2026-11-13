"""每个数的可信等级（trust）：命令结果、判定的每一项、账本里没有这个字段的旧记录。
Trust levels of numbers: command results, each check item, and old ledger records without the field."""

from __future__ import annotations

import json

import pytest
import yaml

from saos import demand, jobs, ledger, prescription
from saos.backend.rtc import RtcBackend
from saos.daemon import Daemon
from saos.protocol import ErrorCode, SaosError

# 模型玻璃单透镜，半口径全部固定、物在无穷远、不追光线瞄准：rtc 在它上面不经过任何未对账的路径
# a model-glass singlet with every semi-diameter fixed, the object at infinity and no ray aiming: rtc takes no unreconciled path
CLEAN = {"title": "clean singlet", "aperture": {"type": "entrance_pupil_diameter", "value": 10},
         "fields": {"type": "angle", "y": [0, 3]}, "wavelengths": {"um": [0.5876]}, "ray_aiming": "off",
         "object": {"thickness": "inf"},
         "surfaces": [{"radius": 50, "thickness": 5, "material": {"model": {"nd": 1.5168, "vd": 64.17}}, "stop": True, "semi_diameter": 6},
                      {"radius": -50, "thickness": 47, "semi_diameter": 6}],
         "image": {"semi_diameter": 5}}
# REAY 已对账，TOTR 没有（rtc.RECONCILED_OPERANDS）/ REAY is reconciled, TOTR is not
REAY = {"type": "REAY", "int1": 2, "int2": 1, "data": [0, 1, 0, 1]}
TOTR = {"type": "TOTR"}
# 两个脚本判定经 ctx.memo 共用同一次 TOTR：第二个从缓存取值，也要算用了 TOTR
# two script checks share one TOTR through ctx.memo: the second reads it from the cache and must still count as using TOTR
MEMO_SCRIPT = "def run(ctx):\n    return ctx.memo('length', lambda: ctx.op('TOTR'))\n"


def call(daemon, op, **args):
    return daemon.handle({"op": op, "args": args})


def _rtc_job(raw=CLEAN):
    rtc = RtcBackend(None, standalone=True)
    daemon = Daemon(rtc, rtc.identity())
    job = jobs.create_job("trust")
    reply = call(daemon, "lens.build", job=job.id, prescription=prescription.normalize(raw), file="a.zmx")
    assert reply["ok"], reply
    return daemon, job


def _bounded(item_id, check):
    return {"id": item_id, "description": item_id, "check": check, "goal": {"max": 1e6}, "source": "user"}


def test_a_command_result_says_how_far_its_number_can_be_trusted(saos_home):
    daemon, job = _rtc_job()
    ray = call(daemon, "lens.operand", job=job.id, **REAY)["result"]
    assert ray["trust"] == "reconciled" and ray["computed_by"]["operands_unverified"] == []
    focal = call(daemon, "lens.operand", job=job.id, **TOTR)["result"]
    assert focal["trust"] == "unreconciled" and focal["computed_by"]["operands_unverified"] == ["TOTR"]


def test_an_operand_used_by_one_request_does_not_mark_the_next(saos_home):
    daemon, job = _rtc_job()
    assert call(daemon, "lens.operand", job=job.id, **TOTR)["ok"]
    source = "def run(ctx):\n    return ctx.op('REAY', 2, 1, 0, 1, 0, 1)\n"
    out = call(daemon, "script.exec", job=job.id, name="ray", source=source)["result"]
    assert out["trust"] == "reconciled" and out["computed_by"]["operands_unverified"] == []


def test_a_path_the_lens_takes_makes_every_number_unreconciled(saos_home):
    # 像面半口径不固定：自动半口径未对账，已对账的 REAY 也随之降档 / an automatic image semi-diameter is unreconciled
    daemon, job = _rtc_job({k: v for k, v in CLEAN.items() if k != "image"})
    ray = call(daemon, "lens.operand", job=job.id, **REAY)["result"]
    assert ray["trust"] == "unreconciled" and any("semi-diameters" in u for u in ray["computed_by"]["unverified"])


def test_each_check_item_is_graded_by_what_it_used(saos_home):
    daemon, job = _rtc_job()
    (job.root / "checks").mkdir(exist_ok=True)
    (job.root / "checks" / "focal.py").write_text(MEMO_SCRIPT, encoding="utf-8")
    data = {"schema": 2, "constants": [_bounded("ray", {"operand": REAY}), _bounded("focal", {"operand": TOTR}),
                                       _bounded("memo_a", {"script": "checks/focal.py"}), _bounded("memo_b", {"script": "checks/focal.py"}),
                                       _bounded("mtf", {"operand": {"type": "MTFT"}})],
            "objectives": [{"id": "height", "description": "height", "check": {"operand": REAY}, "origin": "user",
                            "must": {"max": 1e6}, "scale": 1.0, "source": "user"}],
            "summary": "trust levels"}
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    demand._PREPARED.clear()
    reply = call(daemon, "demand.check", job=job.id)
    assert reply["ok"], reply
    result = reply["result"]
    items = {i["id"]: i for i in result["items"]}
    assert items["ray"]["trust"] == "reconciled" and "operands_unverified" not in items["ray"]
    assert items["focal"]["trust"] == "unreconciled" and items["focal"]["operands_unverified"] == ["TOTR"]
    assert items["memo_a"]["trust"] == items["memo_b"]["trust"] == "unreconciled"
    assert items["memo_b"]["operands_unverified"] == ["TOTR"]
    # rtc 算不了的项没有数 / an item rtc cannot compute has no number
    assert items["mtf"]["trust"] == "refused" and result["backend_unsupported"] == ["mtf"]
    assert result["trust"] == "unreconciled"
    assert result["environment"]["provenance"] == {"unverified": [], "operands_unverified": ["TOTR"]}


def test_old_ledger_records_get_their_level_on_reading_and_the_file_stays(tmp_path):
    rows = [{"round": 1, "tier": "local", "shortfall": 1.0, "demand_sha": "d", "backend": "rtc",
             "provenance": {"unverified": [], "operands_unverified": []}},
            # 来源记录出现之前的 rtc 轮次：不知道经过了什么，按未对账算 / an rtc round from before provenance existed
            {"round": 2, "tier": "local", "shortfall": 0.8, "demand_sha": "d", "backend": "rtc"},
            {"round": 3, "tier": "local", "shortfall": 0.5, "demand_sha": "d"},
            # 第 1 轮在 OpticStudio 上复测 / round 1 re-measured on OpticStudio
            {"round": 4, "tier": "confirm", "confirm_of": 1, "shortfall": 1.0, "demand_sha": "d", "backend": "zos", "agrees": True}]
    path = tmp_path / "ledger.jsonl"
    text = "".join(json.dumps(r) + "\n" for r in rows)
    path.write_text(text, encoding="utf-8")
    rounds = {r["round"]: r for r in ledger.load(path)}
    assert rounds[1]["trust"] == "measured" and rounds[1]["rtc"]["trust"] == "reconciled"
    assert rounds[2]["trust"] == "unreconciled" and rounds[3]["trust"] == "measured"
    assert path.read_text(encoding="utf-8") == text


def _clean_file(tmp_path):
    backend = RtcBackend(None, standalone=True)
    backend.build(prescription.normalize(CLEAN))
    path = tmp_path / "clean.zmx"
    backend.save_as(path)
    return path


def test_measure_grades_each_item_in_its_own_process_without_the_daemon(tmp_path):
    from saos import measure

    out = measure.run(_clean_file(tmp_path), [{"operand": REAY}, {"operand": TOTR}, {"operand": {"type": "MTFT"}}])
    ray, length, mtf = out["items"]
    assert out["computed_by"]["unverified"] == [] and ray["trust"] == "reconciled"
    assert length["trust"] == "unreconciled" and length["operands_unverified"] == ["TOTR"]
    assert mtf["trust"] == "refused" and mtf["value"] is None and "MTFT" in mtf["error"]
    with pytest.raises(SaosError) as err:
        measure.run(_clean_file(tmp_path), [{"script": "checks/x.py"}])
    assert err.value.code == ErrorCode.BAD_ARGS


def test_measure_runs_a_jobs_demand_checks_item_by_item(saos_home, tmp_path):
    from saos import measure

    job = jobs.create_job("measure")
    data = {"schema": 2, "constants": [], "objectives": [
        {"id": "height", "description": "height", "check": {"operand": REAY}, "origin": "user", "must": {"max": 1e6}, "scale": 1.0, "source": "user"},
        {"id": "length", "description": "length", "check": {"operand": TOTR}, "origin": "user", "must": {"max": 1e6}, "scale": 1.0, "source": "user"}],
        "summary": "measure"}
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    demand._PREPARED.clear()
    rows = {r["id"]: r for r in measure.run(_clean_file(tmp_path), [], job_root=job.root, demand=True)["demand"]}
    assert rows["height"]["trust"] == "reconciled" and rows["length"]["trust"] == "unreconciled"
    assert rows["length"]["value"] == pytest.approx(52.0)


def test_measure_and_the_daemon_give_the_same_numbers_for_the_same_lens(saos_home, tmp_path):
    # 同一镜头经 daemon 的 demand.check 与无状态的 saos measure：每项数值和等级逐项一致（操作数、度量目录、脚本）
    # the same lens through the daemon's demand.check and the stateless saos measure: every value and grade agrees
    from saos import measure

    daemon, job = _rtc_job()
    (job.root / "checks").mkdir(exist_ok=True)
    (job.root / "checks" / "focal.py").write_text(MEMO_SCRIPT, encoding="utf-8")
    spot = {"metric": {"id": "spot", "params": {"fields": [[0, 0], [0, 1]]}, "pick": "rms_radius"}}
    data = {"schema": 2, "constants": [_bounded("ray", {"operand": REAY}), _bounded("focal", {"operand": TOTR}),
                                       _bounded("script", {"script": "checks/focal.py"}), _bounded("spot", spot),
                                       _bounded("efl", {"metric": {"id": "efl"}}), _bounded("mtf", {"operand": {"type": "MTFT"}})],
            "objectives": [{"id": "height", "description": "height", "check": {"operand": REAY}, "origin": "user",
                            "must": {"max": 1e6}, "scale": 1.0, "source": "user"}],
            "summary": "two paths"}
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    demand._PREPARED.clear()
    reply = call(daemon, "demand.check", job=job.id)
    assert reply["ok"], reply
    via_daemon = {i["id"]: i for i in reply["result"]["items"]}
    demand._PREPARED.clear()
    via_measure = {r["id"]: r for r in measure.run(job.resolve_lens("a.zmx", must_exist=True), [], job_root=job.root, demand=True)["demand"]}
    assert set(via_measure) == set(via_daemon) == {"ray", "focal", "script", "spot", "efl", "mtf", "height"}
    for item_id, row in via_measure.items():
        assert row["trust"] == via_daemon[item_id]["trust"], item_id
        assert row["value"] == via_daemon[item_id]["value"], item_id
    assert via_measure["spot"]["value"] is not None and via_measure["mtf"]["value"] is None


def test_spot_statistics_and_quick_focus_are_unreconciled_but_a_single_ray_trace_is_not(saos_home):
    # 点列取 31×31 方格之类的光瞳采样，与 OpticStudio 的 Standard Spot 不同；快速对焦同样没对账；单根光线等同 REAX/REAY
    # spot statistics and quick focus sample the pupil differently from OpticStudio; a single ray is as reconciled as REAY
    daemon, job = _rtc_job()
    source = "def run(ctx):\n    return float(ctx.trace([[1, 0, 1, 0, 1]]).y[0])\n"
    traced = call(daemon, "script.exec", job=job.id, name="ray", source=source)["result"]
    assert traced["trust"] == "reconciled" and traced["computed_by"]["operands_unverified"] == []
    source = "def run(ctx):\n    return ctx.spot([(0, 0), (0, 1)])[0]['rms_radius']\n"
    spot = call(daemon, "script.exec", job=job.id, name="spot", source=source)["result"]
    assert spot["trust"] == "unreconciled" and spot["computed_by"]["operands_unverified"] == ["SPOT"]
    source = "def run(ctx):\n    return ctx.quick_focus()\n"
    focus = call(daemon, "script.exec", job=job.id, name="focus", source=source)["result"]
    assert focus["trust"] == "unreconciled" and focus["computed_by"]["operands_unverified"] == ["QUICK_FOCUS"]


def test_the_spot_metric_and_a_cached_spot_carry_the_mark_in_a_demand_check(saos_home):
    daemon, job = _rtc_job()
    (job.root / "checks").mkdir(exist_ok=True)
    cached = "def run(ctx):\n    return ctx.memo('spot', lambda: ctx.spot([(0, 0)]))[0]['rms_radius']\n"
    (job.root / "checks" / "spot.py").write_text(cached, encoding="utf-8")
    spot = {"metric": {"id": "spot", "params": {"fields": [[0, 0]]}, "pick": "rms_radius"}}
    data = {"schema": 2, "constants": [_bounded("ray", {"operand": REAY}), _bounded("metric", spot),
                                       _bounded("first", {"script": "checks/spot.py"}), _bounded("second", {"script": "checks/spot.py"})],
            "objectives": [{"id": "height", "description": "height", "check": {"operand": REAY}, "origin": "user",
                            "must": {"max": 1e6}, "scale": 1.0, "source": "user"}],
            "summary": "spot marks"}
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    demand._PREPARED.clear()
    items = {i["id"]: i for i in call(daemon, "demand.check", job=job.id)["result"]["items"]}
    assert items["ray"]["trust"] == "reconciled"
    for item_id in ("metric", "first", "second"):
        assert items[item_id]["trust"] == "unreconciled" and items[item_id]["operands_unverified"] == ["SPOT"], item_id


def test_the_capability_table_lists_spot_and_quick_focus_as_unreconciled():
    from saos import capability

    measurements = capability.table()["rtc"]["measurements"]
    assert measurements["spot"] == {"status": "unreconciled"} and measurements["quick_focus"] == {"status": "unreconciled"}
