"""在 OpticStudio 上录热结构真值（tests/thermal_variants.py），并验证它读得进 rtc 写的热结构文件。

录真值：SAOS_RUN_ZEMAX=1 SAOS_CASE_RECORD=1 uv run pytest tests/test_zemax_thermal.py -s -k record
读回：  SAOS_RUN_ZEMAX=1 uv run pytest tests/test_zemax_thermal.py -s -k reads
录完跑离线的 tests/rtc/test_thermal_truth.py。
"""

from __future__ import annotations

import math
import os
import tempfile
from datetime import date
from pathlib import Path

import pytest

from saos import prescription
from saos.backend import rtc_lens
from saos.backend.rtc import RtcBackend
from thermal_variants import ALL, TCE, Variant, save_truth, truth

pytestmark = [pytest.mark.zemax, pytest.mark.skipif(os.environ.get("SAOS_RUN_ZEMAX") != "1", reason="需要真实 OpticStudio，设置 SAOS_RUN_ZEMAX=1")]

PUPIL = [(0.0, 0.0), (0.95, 0.0), (-0.95, 0.0), (0.0, 0.95), (0.0, -0.95), (0.5, 0.5), (-0.5, 0.5)]
RAY_KEYS = ("error", "vignette", "x", "y", "z", "l", "m", "n")


@pytest.fixture(scope="module")
def zos():
    from saos.backend.zos import ZosBackend

    backend = ZosBackend()
    backend.start()
    yield backend
    backend.close()


def _op(z, name, int1=0, int2=0, data=()):
    try:
        return float(z.operand(name, int1, int2, list(data)))
    except Exception as exc:  # noqa: BLE001 - 真值里记下读不出的原因 / the reason goes into the truth
        return str(exc)


def _rays(z, count_waves, fields, image):
    rows = [(w, hx, hy, px, py) for w in range(1, count_waves + 1) for hx, hy in fields for px, py in PUPIL]
    rays = z.trace_batch(rows, image)
    columns = [getattr(rays, key).tolist() for key in RAY_KEYS]
    return [[*row, *cell] for row, cell in zip(rows, zip(*columns))]


def _field_points(z):
    from rtc import zmx
    from saos import fast

    with tempfile.TemporaryDirectory() as folder:
        saved = Path(folder) / "lens.zmx"
        z.export_native(saved)
        return fast.field_points(zmx.parse(saved)), zmx.decode_zmx_bytes(saved.read_bytes())


def record(z, variant: Variant) -> dict:
    z.build(prescription.normalize(variant.prescription()))
    with tempfile.TemporaryDirectory() as folder:
        saved = Path(folder) / "nominal.zmx"
        z.export_native(saved)
        from rtc import zmx

        nominal = zmx.decode_zmx_bytes(saved.read_bytes())
    z.make_thermal(list(variant.temperatures), TCE)
    if variant.after is not None:
        variant.after(z)
        z.system.UpdateStatus()
    fields, text = _field_points(z)
    image = z.surface_count() - 1
    waves = int(z.system.SystemData.Wavelengths.NumberOfWavelengths)
    count = z.configuration_count()
    per_config = {}
    for c in range(1, count + 1):
        z.select_configuration(c)
        item = {
            "index": {str(i): [_op(z, "INDX", i, w) for w in range(1, waves + 1)] for i in range(1, image)},
            "operands": {name: _op(z, name, 0, 0) for name in ("EFFL", "EPDI", "WFNO", "TOTR")},
            "semi": {str(s["index"]): float(s["semi_diameter"]) for s in z.surfaces()[1:]},
            "thickness": {str(s["index"]): float(s["thickness"]) for s in z.surfaces()[:-1]},
            "radius": {str(s["index"]): float(s["radius"]) for s in z.surfaces()[1:]},
        }
        if variant.rays:
            item["rays"] = _rays(z, waves, fields, image)
        per_config[str(c)] = item
    z.select_configuration(1)
    return {"source": f"OpticStudio 24.1 ZOS-API standalone, tests/test_zemax_thermal.py, {date.today().isoformat()}",
            "variant": variant.name, "case": variant.case, "serves": variant.serves, "temperatures": list(variant.temperatures), "tce": TCE,
            "nominal_zmx": nominal, "zmx": text, "image": image, "configurations": z._configurations(), "per_config": per_config}


@pytest.mark.skipif(os.environ.get("SAOS_CASE_RECORD") != "1", reason="设置 SAOS_CASE_RECORD=1 才重录真值")
@pytest.mark.parametrize("variant", ALL, ids=lambda v: v.name)
def test_record(zos, variant):
    save_truth(variant.name, record(zos, variant))


@pytest.mark.parametrize("variant", ALL, ids=lambda v: v.name)
def test_opticstudio_reads_rtc_file(zos, tmp_path, variant):
    """rtc 改过结构 1 再写盘：OpticStudio 读进来，每个单元格和 rtc 算的相同，说明行格式、求解码和热跟随都对。"""
    data = truth(variant.name)
    source = tmp_path / "source.zmx"
    source.write_bytes(rtc_lens.BOM + data["zmx"].encode("utf-16-le"))
    rtc = RtcBackend(None, standalone=True)
    try:
        rtc.load(source)
    except Exception as exc:  # noqa: BLE001
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


@pytest.mark.parametrize("name", ["seq_3", "iris_6", "reflective_3", "seq_asphere"])
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


@pytest.mark.parametrize("name", ["seq_3", "iris_6"])
def test_multi_configuration_merit_matches_opticstudio_row_by_row(zos, tmp_path, name):
    """rtc 写盘的多结构评价函数：OpticStudio 读进来行序相同，已对账的操作数逐行同值。
    向导的 TRAC 行只比行序：rtc 的 TRAC 没和 OpticStudio 对账过，结构 1 的第 14 行就差 3.7e-5 mm（seq_3）、1.2e-3 mm（iris_6）。"""
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
    theirs, mine = zos._configurations(), rtc._configurations()
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
