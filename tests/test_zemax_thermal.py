"""OpticStudio 读得进 rtc 写的热结构文件、两个后端交接与多结构评价函数一致：这几项要真机现算，录不成离线真值。

SAOS_RUN_ZEMAX=1 uv run pytest tests/test_zemax_thermal.py -s
真值由 tests/record_truth.py thermal 录入，离线回放在 tests/rtc/test_thermal_truth.py。
"""

from __future__ import annotations

import math
import os

import pytest

from saos import prescription
from saos.backend import rtc_lens
from saos.backend.rtc import RtcBackend
from thermal_variants import ALL, TCE, truth

pytestmark = [pytest.mark.zemax, pytest.mark.skipif(os.environ.get("SAOS_RUN_ZEMAX") != "1", reason="需要真实 OpticStudio，设置 SAOS_RUN_ZEMAX=1")]

@pytest.fixture(scope="module")
def zos():
    from saos.backend.zos import ZosBackend

    backend = ZosBackend()
    backend.start()
    yield backend
    backend.close()


# seq_asphere 一个变体就含全部行类型（含 PRAM）；iris_6 换一种结构数，并带空气光阑、面孔径和 Float By Stop Size。
# 其余变体只差求解码的数值（结构数 + 2），规则由离线的 tests/rtc/test_thermal_truth.py 逐个对真值核对
@pytest.mark.parametrize("name", ["seq_asphere", "iris_6"])
def test_opticstudio_reads_rtc_file(zos, tmp_path, name):
    """rtc 改过结构 1 再写盘：OpticStudio 读进来，每个单元格和 rtc 算的相同，说明行格式、求解码和热跟随都对。"""
    data = truth(name)
    source = tmp_path / "source.zmx"
    source.write_bytes(rtc_lens.BOM + data["zmx"].encode("utf-16-le"))
    rtc = RtcBackend(None, standalone=True)
    try:
        rtc.load(source)
    except Exception as exc:  # noqa: BLE001 - rtc 拒绝的变体跳过
        pytest.skip(f"rtc 拒绝这个变体：{exc}")
    rtc.set_param("1.thickness", float(rtc.get_param("1.thickness")) * 1.01)
    written = tmp_path / "written.zmx"
    rtc.save_as(written)
    zos.load(written)
    zos.system.UpdateStatus()
    saved = tmp_path / "back.zmx"
    zos.export_native(saved)
    from rtc import zmx

    theirs, mine = zmx.parse(saved), zmx.parse(written)
    assert [(r.op, r.surface, r.param, r.config, r.solve_code) for r in theirs.mce] == [(r.op, r.surface, r.param, r.config, r.solve_code) for r in mine.mce]
    for a, b in zip(theirs.mce, mine.mce):
        if a.op == "GLSS":
            assert a.text.upper() == b.text.upper()
        else:
            assert math.isclose(a.value, b.value, rel_tol=2e-11, abs_tol=2e-9), (a.op, a.surface, a.param, a.config, a.value, b.value)


def test_opticstudio_computes_a_config_pickup_as_rtc_does(zos, tmp_path):
    """CRVT 结构拾取（比例 2、偏移 0.001）：rtc 写盘时缓存值故意留错，OpticStudio 读进来重算，和 rtc 求的值相同。"""
    source = tmp_path / "source.zmx"
    source.write_bytes(rtc_lens.BOM + truth("seq_3")["zmx"].encode("utf-16-le"))
    rtc = RtcBackend(None, standalone=True)
    rtc.load(source)
    lens = rtc.lens
    nominal, cell = lens.cell("CRVT", 1, 1), lens.cell("CRVT", 1, 2)
    nominal.value, nominal.edited = 0.02, True
    cell.value, cell.edited = 0.03, True
    cell.solve = [2.0, 0.0, 0.0, 1.0, cell.solve[4], 2.0, 0.001, 0.0, float("nan"), 0.0]
    rtc._changed()
    written = tmp_path / "written.zmx"
    rtc.save_as(written)
    zos.load(written)
    zos.system.UpdateStatus()
    saved = tmp_path / "back.zmx"
    zos.export_native(saved)
    from rtc import zmx

    theirs = zmx.parse(saved).cell("CRVT", 1, 2)
    assert theirs.solve_code == 2, theirs.raw
    assert math.isclose(theirs.value, 0.041, rel_tol=1e-11), theirs.raw
    assert math.isclose(lens.configured(2).surface(1).curvature, theirs.value, rel_tol=1e-11)


@pytest.mark.parametrize("name", ["iris_6"])
def test_handover_agrees_in_every_configuration(zos, tmp_path, name):
    """rtc 和 OpticStudio 打开同一份热结构文件，每个结构的面数据和光线都一致（交付前复测走的路径）。"""
    from saos import handover

    source = tmp_path / "source.zmx"
    source.write_bytes(rtc_lens.BOM + truth(name)["zmx"].encode("utf-16-le"))
    rtc = RtcBackend(None, standalone=True)
    rtc.load(source)
    zos.load(source)
    for c in range(1, rtc.configuration_count() + 1):
        rtc.select_configuration(c)
        zos.select_configuration(c)
        result = handover.compare(rtc, zos)
        assert result["rays_agree"], (c, result["rays"])
        # 像面半口径两边各自自动算，INDX 的差和固定值的差都不应该出现
        others = [m for m in result["mismatches"] if not m.startswith(f"surface {rtc.surface_count() - 1} semi_diameter")]
        assert not others, (c, others)
    zos.select_configuration(1)


@pytest.mark.parametrize("name", ["seq_3"])
def test_multi_configuration_merit_matches_opticstudio_row_by_row(zos, tmp_path, name):
    """rtc 写盘的多结构评价函数：OpticStudio 读进来行序相同，已对账的操作数逐行同值。"""
    source = tmp_path / "source.zmx"
    source.write_bytes(rtc_lens.BOM + truth(name)["zmx"].encode("utf-16-le"))
    rtc = RtcBackend(None, standalone=True)
    rtc.load(source)
    image = rtc.surface_count() - 1
    rtc.merit_wizard({"data": "Spot Radius", "reference": "Centroid", "rings": 2, "arms": 6, "air": [0.1, 1000.0, 0.1], "glass": [0.5, 50.0, 0.1]})
    written = tmp_path / "wizard.zmx"
    rtc.save_as(written)
    zos.load(written)
    assert [(r["row"], r["type"]) for r in rtc.merit_rows()] == [(r["row"], r["type"]) for r in zos.merit_rows()]
    rtc.merit_clear()
    rows = []
    for c in range(1, rtc.configuration_count() + 1):
        rows.append({"type": "CONF", "int1": c})
        rows += [{"type": "REAY", "int1": image, "int2": 1, "data": [0.0, 1.0, 0.0, p], "weight": 1.0} for p in (-0.7, 0.0, 0.7)]
        rows += [{"type": "REAX", "int1": image, "int2": 1, "data": [0.0, 1.0, 0.7, 0.0], "weight": 1.0},
                 {"type": "EFFL", "int2": 1, "weight": 1.0}, {"type": "TTHI", "int1": 1, "int2": image - 1, "weight": 1.0},
                 {"type": "INDX", "int1": 1, "int2": 1, "weight": 1.0}]
    rtc.merit_add(rows)
    written = tmp_path / "rows.zmx"
    rtc.save_as(written)
    zos.load(written)
    mine, theirs = rtc.merit_rows(), zos.merit_rows()
    assert [(r["row"], r["type"]) for r in mine] == [(r["row"], r["type"]) for r in theirs]
    for a, b in zip(mine, theirs):
        if a["type"] != "CONF":
            assert abs(float(a["value"]) - float(b["value"])) <= 1e-6, (a, b)
    assert math.isclose(rtc.merit_value(), zos.merit_value(), rel_tol=1e-6)


def test_prescription_thermal_builds_the_same_rows_on_both_backends(zos):
    """同一份处方的 thermal 块，两个后端建出的行、求解一致；标称半口径两边各自算，所以数值只比温度。"""
    raw = ALL[1].prescription()
    raw["thermal"] = {"temperatures": [-40.0, 65.0], "housing_tce": TCE}
    normalized = prescription.normalize(raw)
    zos.build(normalized)
    rtc = RtcBackend(None, standalone=True)
    rtc.build(normalized)
    theirs, mine = zos.configurations(), rtc.configurations()
    assert [(o["type"], o["params"], o["solves"]) for o in theirs["operands"]] == [(o["type"], o["params"], o["solves"]) for o in mine["operands"]]
    assert next(o for o in theirs["operands"] if o["type"] == "TEMP")["values"] == [20.0, -40.0, 65.0]


def test_opticstudio_reads_a_surface_converted_on_rtc(zos, tmp_path):
    """rtc 上把热结构的一面换成非球面、补 PRAM 行、写盘：OpticStudio 读进来，单元格和求解码都和 rtc 相同。"""
    from rtc import zmx

    raw = ALL[1].prescription()
    raw["thermal"] = {"temperatures": [-40.0, 65.0], "housing_tce": TCE}
    rtc = RtcBackend(None, standalone=True)
    rtc.build(prescription.normalize(raw))
    rtc.change_surface_type(1, "EvenAspheric")
    rtc.set_param("1.par2", -3e-5)
    rtc.set_param("1.par3", 2e-7)
    written = tmp_path / "written.zmx"
    rtc.save_as(written)
    zos.load(written)
    zos.system.UpdateStatus()
    saved = tmp_path / "back.zmx"
    zos.export_native(saved)
    theirs, mine = zmx.parse(saved), zmx.parse(written)
    assert [(r.op, r.surface, r.param, r.config, r.solve_code) for r in theirs.mce] == [(r.op, r.surface, r.param, r.config, r.solve_code) for r in mine.mce]
    for a, b in zip(theirs.mce, mine.mce):
        if a.op != "GLSS":
            assert math.isclose(a.value, b.value, rel_tol=2e-11, abs_tol=2e-9), (a.op, a.surface, a.param, a.config, a.value, b.value)
