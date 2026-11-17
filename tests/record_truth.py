"""在真机 OpticStudio 上录离线回放用的真值。它是工具不是测试，pytest 不收集；改了处方、变体或录入项之后再跑。

    uv run python tests/record_truth.py cases [变体 ...]    → tests/fixtures/cases/truth.json，回放：tests/rtc/test_case_truth.py
    uv run python tests/record_truth.py thermal [变体 ...]  → tests/fixtures/cases/thermal.json，回放：tests/rtc/test_thermal_truth.py
    uv run python tests/record_truth.py doublet             → tests/fixtures/rtc/doublet.*，回放：tests/test_rtc_backend.py

不写变体名时录全部；变体名见 tests/case_variants.py、tests/thermal_variants.py。会启动一个 standalone OpticStudio。
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import tempfile
from collections import Counter
from datetime import date
from pathlib import Path

import case_variants
import thermal_variants
from rtc import zmx
from saos import fast as fast_mod
from saos import prescription
from saos.kit import KitMethods, pupil

FIXTURES = Path(__file__).parent / "fixtures" / "rtc"
GRID = 11
OPD_GRID = 7
RAY_KEYS = ("error", "vignette", "x", "y", "z", "l", "m", "n")
THERMAL_PUPIL = [(0.0, 0.0), (0.95, 0.0), (-0.95, 0.0), (0.0, 0.95), (0.0, -0.95), (0.5, 0.5), (-0.5, 0.5)]
DOUBLET_FIELDS = [(0.0, 0.0), (0.0, 0.5), (0.0, 1.0), (0.3, -0.7)]


class Ctx(KitMethods):
    """kit 方法直接作用在 ZosBackend 上的最小上下文。/ A minimal context: kit methods on a ZosBackend."""

    in_check = False

    def __init__(self, backend):
        self.backend = backend
        self.cache = {}

    def fast(self, freeze_solves=False):
        return fast_mod.from_context(self, freeze_solves)


def doublet(stop_behind: bool) -> dict:
    """f ≈ 100 mm 的双胶合（N-BK7 / N-SF2），第 3 面带 4 次非球面项；视场含 x 分量，检验径向归一化。"""
    lens = [
        {"radius": 61.47, "thickness": 6.0, "material": "N-BK7"},
        {"radius": -44.64, "thickness": 2.5, "material": "N-SF2"},
        {"type": "EvenAspheric", "radius": -129.94, "thickness": 5.0 if stop_behind else 94.0, "material": "", "params": {"par2": 2e-7}},
    ]
    if stop_behind:
        surfaces = [*lens, {"radius": "inf", "thickness": 89.0, "material": "", "stop": True}]
    else:
        surfaces = [{**lens[0], "stop": True}, *lens[1:]]
    return {
        "title": "rtc doublet (test fixture)",
        "aperture": {"type": "entrance_pupil_diameter", "value": 10.0},
        "fields": {"type": "angle", "y": [0.0, 3.5, 5.0], "x": [0.0, 0.0, 2.0]},
        "wavelengths": {"um": [0.4861, 0.5876, 0.6563], "weights": [1.0, 1.0, 1.0], "primary": 2},
        "ray_aiming": "real" if stop_behind else "off",
        "object": {"thickness": "inf"},
        "surfaces": surfaces,
    }


# ---- iris、reflective 及其变体 ----

def _cells(rays) -> list[list[float]]:
    columns = [getattr(rays, key).tolist() for key in RAY_KEYS]
    return [list(row) for row in zip(*columns)]


def _trace_rows(ctx, rows: list[tuple], to: int) -> list[list[float]]:
    """每行 [wave, hx, hy, px, py, error, vignette, x, y, z, l, m, n]。"""
    return [[*row, *cell] for row, cell in zip(rows, _cells(ctx.trace(rows, to)))]


def record_case(zos, variant: case_variants.Variant) -> dict:
    zos.build(prescription.normalize(variant.prescription()))
    if variant.after is not None:
        variant.after(zos)
    ctx = Ctx(zos)
    with tempfile.TemporaryDirectory() as folder:
        saved = Path(folder) / "lens.zmx"
        zos.export_native(saved)
        text = zmx.decode_zmx_bytes(saved.read_bytes())
        if variant.is_base:
            shutil.copyfile(saved, case_variants.CASES / f"{variant.case}.zmx")

    lens = zmx.parse_text(text)
    image = ctx.surface(-1)
    primary = lens.primary_wave
    waves = list(range(1, len(lens.active_wavelengths) + 1))
    points = fast_mod.field_points(lens)
    aperture = zos.system.SystemData.Aperture
    table = zos.surfaces()
    data = {
        "source": f"OpticStudio 24.1 ZOS-API standalone, tests/record_truth.py, {date.today().isoformat()}",
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
        grid = case_variants.pupil_grid(GRID)
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
        off = [(primary, hx, hy, px, py) for hx, hy in variant.off_table for px, py in case_variants.pupil_grid(5)]
        data["off_table"] = _trace_rows(ctx, off, image)
    if variant.opd:
        grid = case_variants.pupil_grid(OPD_GRID)
        data["opd"] = [{"hx": hx, "hy": hy,
                        "rows": [[px, py, ctx.op("OPDC", 0, primary, hx, hy, px, py), ctx.op("OPDX", 0, primary, hx, hy, px, py)]
                                 for px, py in grid]} for hx, hy in points]
        data["wavefront_rms"] = ctx.wavefront_rms([tuple(p) for p in points], primary)
    return data


def _case_summary(data: dict) -> str:
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


# ---- 热结构变体 ----

def _op(z, name, int1=0, int2=0, data=()):
    try:
        return float(z.operand(name, int1, int2, list(data)))
    except Exception as exc:  # noqa: BLE001 - 真值里记下读不出的原因
        return str(exc)


def _thermal_rays(z, count_waves, fields, image):
    rows = [(w, hx, hy, px, py) for w in range(1, count_waves + 1) for hx, hy in fields for px, py in THERMAL_PUPIL]
    rays = z.trace_batch(rows, image)
    columns = [getattr(rays, key).tolist() for key in RAY_KEYS]
    return [[*row, *cell] for row, cell in zip(rows, zip(*columns))]


def _field_points(z):
    with tempfile.TemporaryDirectory() as folder:
        saved = Path(folder) / "lens.zmx"
        z.export_native(saved)
        return fast_mod.field_points(zmx.parse(saved)), zmx.decode_zmx_bytes(saved.read_bytes())


def record_thermal(z, variant: thermal_variants.Variant) -> dict:
    z.build(prescription.normalize(variant.prescription()))
    with tempfile.TemporaryDirectory() as folder:
        saved = Path(folder) / "nominal.zmx"
        z.export_native(saved)
        nominal = zmx.decode_zmx_bytes(saved.read_bytes())
    z.make_thermal(list(variant.temperatures), thermal_variants.TCE)
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
            item["rays"] = _thermal_rays(z, waves, fields, image)
        per_config[str(c)] = item
    z.select_configuration(1)
    return {"source": f"OpticStudio 24.1 ZOS-API standalone, tests/record_truth.py, {date.today().isoformat()}",
            "variant": variant.name, "case": variant.case, "serves": variant.serves, "temperatures": list(variant.temperatures),
            "tce": thermal_variants.TCE, "nominal_zmx": nominal, "zmx": text, "image": image, "configurations": z.configurations(),
            "per_config": per_config}


# ---- rtc 副本的双胶合基准 ----

def _spot_rms(ctx_like) -> list[float]:
    rows = ctx_like.spot(DOUBLET_FIELDS, wave=[1, 2, 3], grid=21)
    return [r["rms_radius"] for r in rows]


def record_doublet(zos) -> None:
    zos.build(prescription.normalize(doublet(False)))
    ctx = Ctx(zos)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    model = zos.export_rtc(FIXTURES / "doublet.zmx")
    (FIXTURES / "doublet.ZDA").unlink(missing_ok=True)
    for local in ("glass_dir", "file"):
        model.pop(local, None)
    rows = [(w, hx, hy, px, py) for hx, hy in DOUBLET_FIELDS for w in (1, 2, 3) for px, py in [(0.0, 0.0), *pupil(7)]]
    traces = {}
    for to in (2, ctx.surface(-1)):
        r = ctx.trace(rows, to)
        traces[str(to)] = {k: getattr(r, k).tolist() for k in RAY_KEYS}
    data = {"source": "OpticStudio 24.1 ZOS-API standalone, tests/record_truth.py doublet",
            "model": model, "rays": rows, "traces": traces, "spot_rms": _spot_rms(ctx)}
    (FIXTURES / "doublet.json").write_text(json.dumps(data, indent=1, allow_nan=True), encoding="utf-8")


def _chosen(variants, names: list[str]):
    known = {v.name: v for v in variants}
    unknown = [n for n in names if n not in known]
    if unknown:
        sys.exit(f"unknown variants: {unknown}; known: {sorted(known)}")
    return [known[n] for n in names] if names else list(variants)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("what", choices=["cases", "thermal", "doublet"])
    parser.add_argument("variants", nargs="*", help="variant names (default: all)")
    args = parser.parse_args(argv)
    plan = (_chosen(case_variants.ALL, args.variants) if args.what == "cases"
            else _chosen(thermal_variants.ALL, args.variants) if args.what == "thermal" else [])
    from saos.backend.zos import ZosBackend

    zos = ZosBackend()
    zos.start()
    try:
        if args.what == "doublet":
            record_doublet(zos)
            print(f"wrote {FIXTURES / 'doublet.json'}")
        for variant in plan:
            if args.what == "cases":
                data = record_case(zos, variant)
                case_variants.save_truth(variant.name, data)
                print(f"[{variant.name}] {data['operands']}\n  {_case_summary(data)}")
            else:
                thermal_variants.save_truth(variant.name, record_thermal(zos, variant))
                print(f"[{variant.name}] recorded")
    finally:
        zos.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
