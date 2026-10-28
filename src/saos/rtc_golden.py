"""显式运行的材料 INDX 采集器与准入写入。

采集：新开 standalone 会话（不连接用户桌面镜头），对每个样品材料、每个系统环境存一份 ZMX 与同源 describe_model，
逐波长比较 OpticStudio 的 INDX 与 rtc 材料层，结果写 indx.jsonl 和 summary.json。summary 按材料计算路径
（rtc.material.Medium.path）汇总：只统计材料 LD 范围内的点，路径上每一点都 ≤ 1e-8 才记为通过；LD 外的点单独记下
OpticStudio 给出的值，不参与判定。

准入：``--admit SUMMARY`` 把 summary 里通过的路径写进 saos/backend/rtc_admitted.json，并注明证据。
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
from dataclasses import asdict
from datetime import date
from pathlib import Path

WAVELENGTHS = (.36, .4358343, .4861327, .55, .5875618, .6562725, 1.0, 2.5)
TEMPERATURES = (-40.0, 20.0, 25.0, 60.0)
PRESSURES = (0.0, .5, 1.0)
MODELS = ((1.3, 40.0, 0.0), (1.637, 57.3, -.02), (1.8, 20.0, .03), (1.5168, 0.0, 0.0))
TOLERANCE = 1e-8
# 有一个波长短于材料 LD 下限时，OpticStudio 不采用该材料，各波长 INDX 都是 1（2026-10-28：ORMOCLAD、ZNO、LZ_LK1、J-BAF3；
# 只长于上限的 BASF5、QUARTZ 不受影响），所以目录玻璃只取不短于其下限的波长
# 修正关闭时温度、压力不起作用，OpticStudio 也写不进去，只取默认的 20 °C、1 atm
ENVIRONMENTS = [(20.0, 1.0, False)] + [(t, p, True) for t, p in itertools.product(TEMPERATURES, PRESSURES)]


def _samples(directory, catalogs):
    """每个 (公式, 参考温度 20/25 °C) 取一种目录玻璃，LD 覆盖采样波长最多的那种。
    20、25 °C 都没有的公式（如只有 HIKARI 23 °C 的公式 13），从其它参考温度里取一种：准入路径的键不含参考温度。"""
    selected, other = {}, {}
    for stem in catalogs:
        for glass in directory.glasses(stem):
            # 名字含非 ASCII 字符（如 ORMOCLEAR®30）的不取：考的是公式，不是 ZMX 文件的编码；主波长取 0.55 µm，LD 须含它
            if (glass.formula not in range(1, 14) or glass.negative_index or glass.data_error or not glass.name.isascii()
                    or glass.wavelength_range[0] > .55 * 1.001):
                continue
            bucket, key = (selected, (glass.formula, glass.tref)) if glass.tref in (20.0, 25.0) else (other, glass.formula)
            lo, hi = glass.wavelength_range
            score = sum(lo <= w <= hi for w in WAVELENGTHS)
            if key not in bucket or score > bucket[key][0]:
                bucket[key] = (score, glass)
    covered = {formula for formula, _ in selected}
    for formula, item in other.items():
        if formula not in covered:
            selected[(formula, item[1].tref)] = item
    return [item[1] for _, item in sorted(selected.items())]


def _in_range(medium, wavelength: float) -> bool:
    glass = getattr(medium, "glass", None)
    if glass is None:
        return True
    lo, hi = glass.wavelength_range
    return not (wavelength < lo * 0.999 or wavelength * 0.999 > hi)


def collect(output: Path, catalogs: list[str], *, max_cases: int = 20000) -> dict:
    from types import SimpleNamespace

    from rtc import zmx
    from rtc.material import Catalog, Environment, GlassCatalog, MaterialInvalid, MaterialUnsupported, Model, resolve

    from saos import prescription
    from saos.backend.zos import ZosBackend
    from saos.kit import _set_model_material

    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    backend = ZosBackend("standalone")
    report = {"status": "incomplete", "source": "OpticStudio application INDX", "date": date.today().isoformat(),
              "catalogs": list(catalogs), "samples": [], "missing": [], "tolerance": TOLERANCE, "paths": {}, "failed_environments": [],
              "cases": 0, "failed_cases": 0, "out_of_range_cases": 0, "unsupported_cases": 0}
    completed = False
    try:
        backend.start()
        report["backend"] = backend.identity()
        directory = GlassCatalog(backend._glass_dir())
        glasses = _samples(directory, catalogs)
        coverage = {g.formula for g in glasses}
        report["missing"] = [{"formula": kind} for kind in range(1, 14) if kind not in coverage]
        choices = [(Catalog(g.name), g.catalog, {"glass": g.name, "catalog": g.catalog, "formula": g.formula, "tref": g.tref},
                    [w for w in WAVELENGTHS if w >= g.wavelength_range[0] * 0.999])
                   for g in glasses]
        choices += [(Model(*values), None, {"model": list(values)}, list(WAVELENGTHS)) for values in MODELS]
        cases = len(ENVIRONMENTS) * sum(len(waves) for *_, waves in choices)
        if cases > max_cases:
            raise ValueError(f"采样需要 {cases} 个点，超过 --max-cases={max_cases}")
        with (output / "indx.jsonl").open("x", encoding="utf-8") as stream:
            for sample_number, (spec, stem, metadata, waves) in enumerate(choices):
                build = {"title": "saos rtc material oracle", "aperture": {"type": "entrance_pupil_diameter", "value": 1.0},
                         "fields": {"type": "angle", "y": [0.0]},
                         "wavelengths": {"um": waves, "primary": waves.index(.55) + 1},
                         "object": {"thickness": "inf"}, "catalogs": [stem] if stem else catalogs,
                         "surfaces": [{"radius": "inf", "thickness": 1.0, "material": spec.name if isinstance(spec, Catalog) else "", "stop": True}]}
                backend.build(prescription.normalize(build))
                if isinstance(spec, Model):
                    _set_model_material(SimpleNamespace(backend=backend), backend.system.LDE.GetSurfaceAt(1).MaterialCell,
                                        asdict(spec), "surface 1")
                report["samples"].append({"material": asdict(spec), **metadata})
                for env_number, (t, p, adjust) in enumerate(ENVIRONMENTS):
                    snapshot = output / f"sample-{sample_number:02d}-environment-{env_number:02d}.zmx"
                    try:
                        env = backend.system.SystemData.Environment
                        # 修正关闭时 OpticStudio 不接受温度、压力的写入，所以先开关、后数值
                        env.AdjustIndexToEnvironment = adjust
                        env.Temperature, env.Pressure = t, p
                        description = backend.export_rtc(snapshot)
                        if isinstance(spec, Catalog) and all(n == 1.0 for n in description["surfaces"][1]["n"]):
                            raise ValueError("OpticStudio gives n = 1 at every wavelength: it did not apply the material")
                        snapshot.with_suffix(".json").write_text(json.dumps(description, ensure_ascii=False, indent=2), encoding="utf-8")
                        lens = zmx.parse(snapshot)
                        api_env = description["environment"]
                        if (api_env["temperature"], api_env["pressure"], api_env["adjust_index"]) != (t, p, adjust):
                            raise ValueError(f"OpticStudio kept environment {api_env}, not the requested {t} C, {p} atm, adjust {adjust}")
                        environment_equal = lens.environment == Environment(api_env["temperature"], api_env["pressure"], api_env["adjust_index"])
                        medium = resolve(lens.surface(1).material, directory, lens.catalogs)
                    except Exception as exc:  # noqa: BLE001 - 一个环境失败只记下，不放弃其余样品
                        report["failed_environments"].append({"sample": sample_number, **metadata, "temperature": t,
                                                              "pressure": p, "adjust": adjust, "error": f"{type(exc).__name__}: {exc}"})
                        continue
                    path = medium.path(lens.environment)
                    entry = report["paths"].setdefault(path, {"passed": True, "points": 0, "worst": 0.0, "samples": []})
                    if sample_number not in entry["samples"]:
                        entry["samples"].append(sample_number)
                    for wave, (w, _) in enumerate(lens.active_wavelengths, 1):
                        actual = description["surfaces"][1]["n"][wave - 1]
                        row = {"sample": sample_number, "snapshot": snapshot.name, **metadata, "path": path,
                               "environment": api_env, "wavelength": w, "indx": actual, "environment_equal": environment_equal}
                        report["cases"] += 1
                        if not _in_range(medium, w):
                            row.update(out_of_range=True)
                            report["out_of_range_cases"] += 1
                        else:
                            try:
                                candidate = float(medium.index(w, lens.environment))
                                error = abs(float(actual) - candidate)
                                passed = environment_equal and math.isfinite(error) and error <= TOLERANCE
                                row.update(rtc=candidate, absolute_error=error, passed=passed)
                                entry["worst"] = max(entry["worst"], error) if math.isfinite(error) else math.inf
                            except (MaterialUnsupported, MaterialInvalid) as exc:
                                passed = False
                                row.update(passed=False, unsupported=str(exc))
                                report["unsupported_cases"] += 1
                            entry["points"] += 1
                            entry["passed"] = entry["passed"] and passed
                            report["failed_cases"] += not passed
                        stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False, default=str) + "\n")
                    stream.flush()
        report["status"] = "recorded"
        completed = True
        return report
    finally:
        try:
            backend.close()
        finally:
            if not completed:
                report["status"] = "incomplete"
            for entry in report["paths"].values():
                entry["worst"] = entry["worst"] if math.isfinite(entry["worst"]) else None
            (output / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")


def admit(summary_path: Path) -> list[str]:
    """把 summary 里通过的路径（每点 ≤ 1e-8、至少一个点、采集完整）写入准入表；返回新加的路径。"""
    from saos.backend import rtc as rtc_backend

    summary = json.loads(Path(summary_path).read_text(encoding="utf-8"))
    if summary.get("status") != "recorded" or summary.get("tolerance") != TOLERANCE:
        raise ValueError(f"{summary_path}: 采集未完成或容差不是 {TOLERANCE}，不能用于准入")
    registry = json.loads(rtc_backend.ADMITTED_FILE.read_text(encoding="utf-8"))
    version = (summary.get("backend") or {}).get("version", "?")
    added = []
    for path, entry in sorted(summary["paths"].items()):
        if entry["passed"] and entry["points"] > 0 and path not in registry["paths"]:
            registry["paths"][path] = (f"OpticStudio {version} INDX：{entry['points']} 个 LD 内点，最大差 {entry['worst']:.1e}，"
                                       f"样品 {entry['samples']}（{summary.get('date')}，{Path(summary_path).resolve()}）")
            added.append(path)
    rtc_backend.ADMITTED_FILE.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    rtc_backend.admitted_paths.cache_clear()
    return added


def main(argv=None):
    parser = argparse.ArgumentParser(description="采集材料 INDX（创建独立 ZOS 会话，不连接用户窗口），或把通过的路径写入准入表")
    parser.add_argument("--output", type=Path, help="必须尚不存在的输出目录")
    parser.add_argument("--catalogs", nargs="+", help="OpticStudio 在用目录名（不含 .AGF）")
    parser.add_argument("--max-cases", type=int, default=20000)
    parser.add_argument("--admit", type=Path, metavar="SUMMARY", help="把这份 summary.json 里通过的路径写入准入表")
    args = parser.parse_args(argv)
    if args.admit:
        print(json.dumps({"admitted": admit(args.admit)}, ensure_ascii=False, indent=2))
        return 0
    if not args.output or not args.catalogs:
        parser.error("采集需要 --output 与 --catalogs")
    if args.max_cases <= 0:
        parser.error("--max-cases 必须为正数")
    report = collect(args.output, args.catalogs, max_cases=args.max_cases)
    keys = ("status", "cases", "failed_cases", "out_of_range_cases", "unsupported_cases", "missing", "failed_environments")
    print(json.dumps({k: report[k] for k in keys}, ensure_ascii=False, indent=2))
    return int(bool(report["failed_cases"] or report["missing"] or report["failed_environments"]))


if __name__ == "__main__":
    raise SystemExit(main())
