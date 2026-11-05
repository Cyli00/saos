"""在 OpticStudio 上建 iris、reflective 及其变体（tests/case_variants.py），录真值到 tests/fixtures/cases/truth.json。

SAOS_RUN_ZEMAX=1 SAOS_CASE_RECORD=1 uv run pytest tests/test_zemax_cases.py -s

只改变案例或变体时可以加 -k 只录那几个。录完跑离线的 tests/rtc/test_case_truth.py。
"""

from __future__ import annotations

import math
import os
import shutil
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from case_variants import ALL, CASES, Variant, pupil_grid, save_truth
from rtc import zmx
from saos import fast as fast_mod
from saos import prescription
from saos.kit import KitMethods

pytestmark = [
    pytest.mark.zemax,
    pytest.mark.skipif(os.environ.get("SAOS_RUN_ZEMAX") != "1", reason="需要真实 OpticStudio，设置 SAOS_RUN_ZEMAX=1"),
    pytest.mark.skipif(os.environ.get("SAOS_CASE_RECORD") != "1", reason="设置 SAOS_CASE_RECORD=1 才重录真值"),
]

GRID = 11
OPD_GRID = 7
RAY_KEYS = ("error", "vignette", "x", "y", "z", "l", "m", "n")


class Ctx(KitMethods):
    in_check = False

    def __init__(self, backend):
        self.backend = backend
        self.cache = {}


@pytest.fixture(scope="module")
def zos():
    from saos.backend.zos import ZosBackend

    backend = ZosBackend()
    backend.start()
    yield backend
    backend.close()


def _cells(rays) -> list[list[float]]:
    columns = [getattr(rays, key).tolist() for key in RAY_KEYS]
    return [list(row) for row in zip(*columns)]


def _trace_rows(ctx, rows: list[tuple], to: int) -> list[list[float]]:
    """每行 [wave, hx, hy, px, py, error, vignette, x, y, z, l, m, n]。"""
    return [[*row, *cell] for row, cell in zip(rows, _cells(ctx.trace(rows, to)))]


def record(zos, variant: Variant) -> dict:
    zos.build(prescription.normalize(variant.prescription()))
    if variant.after is not None:
        variant.after(zos)
    ctx = Ctx(zos)
    with tempfile.TemporaryDirectory() as folder:
        saved = Path(folder) / "lens.zmx"
        zos.export_native(saved)
        text = zmx.decode_zmx_bytes(saved.read_bytes())
        if variant.is_base:
            shutil.copyfile(saved, CASES / f"{variant.case}.zmx")

    lens = zmx.parse_text(text)
    image = ctx.surface(-1)
    primary = lens.primary_wave
    waves = list(range(1, len(lens.active_wavelengths) + 1))
    points = fast_mod.field_points(lens)
    aperture = zos.system.SystemData.Aperture
    table = zos.surfaces()
    data = {
        "source": f"OpticStudio 24.1 ZOS-API standalone, tests/test_zemax_cases.py, {date.today().isoformat()}",
        "variant": variant.name, "case": variant.case, "serves": variant.serves,
        "fields": points, "primary_wave": primary, "image_surface": image,
        "vignetting": [list(row) for row in lens.vignetting],
        "semi_settings": {"fast": bool(aperture.FastSemiDiameters), "margin_mm": float(aperture.SemiDiameterMargin),
                          "margin_percent": float(aperture.SemiDiameterMarginPct)},
        "semi_diameters": {str(s["index"]): [float(s["semi_diameter"]), str(s["solves"]["semi_diameter"])] for s in table[1:]},
        "material": {str(s["index"]): str(s["material"]) for s in table[1:]},
        "index": {str(i): [ctx.op("INDX", i, w) for w in waves] for i in range(1, image)},
        "operands": {name: ctx.op(name, 0, primary) for name in ("EPDI", "EFFL", "WFNO", "ENPP", "EXPP", "EXPD", "PETZ", "TOTR")},
    }
    if not variant.is_base:
        data["zmx"] = text
    if variant.waves != "none":
        chosen = waves if variant.waves == "all" else [primary]
        fields = points if variant.fields is None else [points[k] for k in variant.fields]
        grid = pupil_grid(GRID)
        rows = [(w, hx, hy, px, py) for w in chosen for hx, hy in fields for px, py in grid]
        rows += [(w, hx, hy, px, py) for w in chosen for hx, hy, px, py in variant.named]
        if variant.ring is not None:
            rows += [(primary, 0.0, 0.0, variant.ring * math.cos(math.radians(a)), variant.ring * math.sin(math.radians(a)))
                     for a in range(0, 360, 5)]
        data["rays"] = _trace_rows(ctx, rows, image)
        # 逐面数据：每个视场的主光线和四条边缘光线，选 0.95 避开圆孔正好在边上的情形
        probes = [(w, hx, hy, px, py) for w in chosen for hx, hy in fields
                  for px, py in ((0.0, 0.0), (0.95, 0.0), (-0.95, 0.0), (0.0, 0.95), (0.0, -0.95))]
        data["surfaces"] = {str(to): _trace_rows(ctx, probes, to) for to in range(1, image + 1)}
        data["plen"] = [[hx, hy, px, py, ctx.op("PLEN", 1, image, hx, hy, px, py)]
                        for w, hx, hy, px, py in rows if w == primary]
    if variant.off_table:
        off = [(primary, hx, hy, px, py) for hx, hy in variant.off_table for px, py in pupil_grid(5)]
        data["off_table"] = _trace_rows(ctx, off, image)
    if variant.opd:
        grid = pupil_grid(OPD_GRID)
        data["opd"] = [{"hx": hx, "hy": hy,
                        "rows": [[px, py, ctx.op("OPDC", 0, primary, hx, hy, px, py), ctx.op("OPDX", 0, primary, hx, hy, px, py)]
                                 for px, py in grid]} for hx, hy in points]
        data["wavefront_rms"] = ctx.wavefront_rms([tuple(p) for p in points], primary)
    return data


def _summary(data: dict) -> str:
    if "rays" not in data:
        return "semi " + ", ".join(f"{k}:{v[0]:.4f}" for k, v in data["semi_diameters"].items())
    lines = []
    for hx, hy in data["fields"]:
        rows = [r for r in data["rays"] if r[1] == hx and r[2] == hy]
        if rows:
            usable = sum(1 for r in rows if r[5] == 0 and r[6] == 0)
            lines.append(f"({hx:.3f},{hy:.3f}) 通过 {100 * usable / len(rows):.1f}% 首挡面 {dict(sorted(Counter(int(r[6]) for r in rows).items()))} "
                         f"错误 {dict(sorted(Counter(int(r[5]) for r in rows).items()))}")
    return "\n  ".join(lines)


@pytest.mark.parametrize("variant", ALL, ids=[v.name for v in ALL])
def test_record_case_truth(zos, variant):
    data = record(zos, variant)
    save_truth(variant.name, data)
    print(f"\n[{variant.name}] {data['operands']}\n  {_summary(data)}")
