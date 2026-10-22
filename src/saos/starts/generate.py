"""从需求条目读取指标，筛选有来源的形式并缩放；结果供 candidates 扫描使用。"""
from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any

from saos import demand, jobs, prescription
from saos.protocol import ErrorCode, SaosError, read_json, write_json
from saos.starts.library import entries, entry_path, library_root
from saos.starts.scaling import scale_prescription

REQUIRED = {"efl_mm", "half_field_deg", "f_number", "wavelengths_um"}
UNITS = {"efl_mm": {"mm": 1, "cm": 10, "m": 1000, "in": 25.4},
         "half_field_deg": {"deg": 1, "degree": 1, "°": 1},
         "f_number": {"": 1, "1": 1, "f/#": 1},
         "wavelengths_um": {"um": 1, "µm": 1, "nm": .001}}


def requirements(d: dict, bindings: dict) -> dict:
    if set(bindings) != REQUIRED:
        raise SaosError(ErrorCode.BAD_ARGS, f"指标映射必须包含 {sorted(REQUIRED)}")
    indexed = {r["id"]: r for section in demand.SECTIONS for r in d.get(section, [])}
    values, evidence = {}, {}
    for metric, binding in bindings.items():
        binding = {"id": binding} if isinstance(binding, str) else binding
        item = indexed.get(binding.get("id"))
        if item is None:
            raise SaosError(ErrorCode.BAD_ARGS, f"未找到 {metric} 对应的需求条目")
        bound = item.get("goal") or item.get("must") or item.get("range") or {}
        value = binding.get("value", item.get("value", bound.get("equals")))
        if value is None:
            raise SaosError(ErrorCode.BAD_ARGS, f"{item['id']} 只有范围；请在映射中显式填写范围内的 value")
        unit = str(item.get("unit", "")).lower()
        if unit not in UNITS[metric]:
            raise SaosError(ErrorCode.BAD_ARGS, f"{item['id']} 的单位 {unit!r} 无法用于 {metric}")
        raw = value if isinstance(value, list) else [value]
        if not raw or (metric != "wavelengths_um" and len(raw) != 1):
            raise SaosError(ErrorCode.BAD_ARGS, f"{metric} 的数值数量不正确")
        for v in raw:
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 or (metric != "half_field_deg" and v == 0):
                raise SaosError(ErrorCode.BAD_ARGS, f"{metric} 需要有限正数，半视场允许零")
            if v < bound.get("min", -math.inf) or v > bound.get("max", math.inf) or ("equals" in bound and abs(v - bound["equals"]) > bound.get("tol", 0)):
                raise SaosError(ErrorCode.BAD_ARGS, f"{metric} 的起点值不在需求界限内")
        converted = [v * UNITS[metric][unit] for v in raw]
        values[metric] = sorted(set(converted)) if metric == "wavelengths_um" else converted[0]
        evidence[metric] = {"id": item["id"], "source": item.get("source"), "value": value, "unit": unit}
    if values["half_field_deg"] >= 90:
        raise SaosError(ErrorCode.BAD_ARGS, "当前起点库只支持小于 90° 的半视场")
    return {"values": values, "items": evidence}


def query(targets: dict, *, root: Path | None = None, conjugate: str = "infinite", allow_extrapolation: bool = False) -> tuple[list[dict], list[dict]]:
    if not isinstance(allow_extrapolation, bool) or conjugate not in ("finite", "infinite"):
        raise SaosError(ErrorCode.BAD_ARGS, "allow_extrapolation 必须为布尔值，conjugate 必须为 finite 或 infinite")
    selected, excluded = [], []
    try:
        available = entries(root or library_root())
        for entry in available:
            app = entry["applicability"]
            entry_path(root or library_root(), entry["id"])
            if not isinstance(entry["source"], dict) or not isinstance(entry["form"], str):
                raise ValueError("source/form 格式错误")
            for key in ("version", "status"):
                if key not in entry:
                    raise ValueError(f"缺少 {key}")
            values = [app["half_field_deg_max"], app["f_number_min"], *app["wavelength_um"], entry["metrics"]["elements"]]
            if (len(app["wavelength_um"]) != 2 or app["wavelength_um"][0] > app["wavelength_um"][1]
                    or app["conjugate"] not in ("finite", "infinite")
                    or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in values)):
                raise ValueError("applicability/metrics 数值错误")
    except (OSError, ValueError, KeyError, TypeError, OverflowError, SaosError) as exc:
        raise SaosError(ErrorCode.BAD_ARGS, f"起点库元数据错误：{exc}", {"kind": "library", "root": str(root or library_root())}) from exc
    for entry in available:
        app = entry["applicability"]
        reasons = []
        if app["conjugate"] != conjugate:
            reasons.append("物像共轭与原始处方不一致")
        if targets["half_field_deg"] > app["half_field_deg_max"]:
            reasons.append("半视场超过有来源示例的范围")
        if targets["f_number"] < app["f_number_min"]:
            reasons.append("f/# 快于有来源示例的范围")
        lo, hi = app["wavelength_um"]
        low, high = min(targets["wavelengths_um"]), max(targets["wavelengths_um"])
        if (low < lo and not math.isclose(low, lo, rel_tol=1e-12)) or (high > hi and not math.isclose(high, hi, rel_tol=1e-12)):
            reasons.append("波段超出示例范围，材料和色差尚需另行评估")
        if reasons and (not allow_extrapolation or app["conjugate"] != conjugate):
            excluded.append({"id": entry["id"], "reasons": reasons, "source": entry["source"]})
            continue
        pressure = {"half_field_deg": targets["half_field_deg"] / app["half_field_deg_max"],
                    "f_number": app["f_number_min"] / targets["f_number"],
                    "wavelengths_um": (max(targets["wavelengths_um"]) - min(targets["wavelengths_um"])) / (hi-lo) if hi != lo else 1.0}
        selected.append({**entry, "extrapolation": reasons, "pressure": pressure, "tightest": max(pressure, key=pressure.get)})
    selected.sort(key=lambda e: (bool(e["extrapolation"]), max(e["pressure"].values()), e["metrics"]["elements"], e["id"]))
    return selected, excluded


def generate(job: jobs.Job, bindings: dict, *, short_seconds: float, prepare: str,
             root: Path | None = None, conjugate: str = "infinite", allow_extrapolation: bool = False, backend_factory=None) -> dict[str, Any]:
    if isinstance(short_seconds, bool) or not isinstance(short_seconds, (int, float)) or not math.isfinite(short_seconds) or short_seconds <= 0:
        raise SaosError(ErrorCode.BAD_ARGS, "短优化预算必须为有限正秒数")
    if not isinstance(prepare, str) or not prepare.strip():
        raise SaosError(ErrorCode.BAD_ARGS, "起点处方不带评价函数；生成短优化计划必须提供 prepare 脚本，设置变量和评价函数")
    if not jobs.inside(job.root, prepare).is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, "prepare 脚本不存在")
    d = demand.load_job(job.root)
    constraints = requirements(d, bindings)
    targets = constraints["values"]
    root = root or library_root()
    choices, excluded = query(targets, root=root, conjugate=conjugate, allow_extrapolation=allow_extrapolation)
    parent = job.root / "explore" / "starts"
    parent.mkdir(parents=True, exist_ok=True)
    folder = parent / f"{jobs.next_archive_number(parent):03d}"
    folder.mkdir()
    if backend_factory is None:
        from saos.backend.optiland import OptilandBackend
        backend_factory = OptilandBackend
    backend = backend_factory()
    candidates, failures = [], []
    try:
        if choices:
            backend.start()
        for entry in choices:
            phase = "library_read"
            try:
                data = read_json(entry_path(root, entry["id"]) / "prescription.json")
                if not isinstance(data, dict):
                    raise SaosError(ErrorCode.BAD_ARGS, "库处方必须为对象")
                reference = copy.deepcopy(data)
                reference["aperture"] = {"type": "image_f_number", "value": targets["f_number"]}
                field = targets["half_field_deg"]
                reference["fields"] = {"type": "angle", "y": [0, field * .7, field] if field else [0]}
                waves = targets["wavelengths_um"]
                reference["wavelengths"] = {"um": waves, "primary": (len(waves) + 1) // 2}
                phase = "library_normalize"
                reference = prescription.normalize(reference)
                phase = "reference_build"
                backend.build(reference)
                phase = "reference_efl"
                reference_efl = float(backend.operand("EFFL", 0, 0, []))
                if not math.isfinite(reference_efl) or reference_efl <= 0:
                    raise ValueError("原处方没有有限正焦距")
                factor = targets["efl_mm"] / reference_efl
                phase = "library_scale"
                scaled = prescription.normalize(scale_prescription(reference, factor))
                phase = "scaled_build"
                backend.build(scaled)
                from saos.starts.prediction import predict
                phase = "prediction"
                prediction = predict(backend)
                phase = "artifacts"
                path = folder / f"{entry['id']}.json"
                write_json(path, scaled)
                candidates.append({"id": entry["id"], "prescription": path.relative_to(job.root).as_posix(), "form": entry["form"],
                                   "provenance": {**entry["source"], "version": entry["version"], "validation": entry["status"], "reference_efl": reference_efl,
                                                  "scale": factor, "reference_wavelengths": reference["wavelengths"], "notes": entry.get("notes"), "entry_sha": jobs.sha256_file(entry_path(root, entry["id"]) / "entry.json"),
                                                  "prescription_sha": jobs.sha256_file(entry_path(root, entry["id"]) / "prescription.json")},
                                   "constraints": {"tightest": entry["tightest"], "pressure": entry["pressure"], "extrapolation": entry["extrapolation"]}, "prediction": prediction})
            except Exception as exc:
                from saos.backend.optiland_materials import MaterialError
                if phase == "artifacts":
                    raise
                verdict = ("library" if phase.startswith("library_") else "material" if isinstance(exc, MaterialError)
                           else "configuration" if isinstance(exc, SaosError) and exc.code in (ErrorCode.BAD_ARGS, ErrorCode.BACKEND_UNSUPPORTED)
                           else "computation")
                failures.append({"id": entry["id"], "verdict": verdict, "phase": phase,
                                 "code": getattr(exc, "code", ErrorCode.BAD_ARGS if verdict == "library" else None),
                                 "error": f"{type(exc).__name__}: {exc}", "source": entry["source"]})
    finally:
        if choices:
            backend.close()
    plan = {"candidates": candidates, "stages": [], "require_merit": True, "constraints": constraints, "generation": folder.relative_to(job.root).as_posix(), "prepare": prepare}
    result = {"folder": folder.relative_to(job.root).as_posix(), "demand_sha": demand.compute_digest(d), "constraints": constraints,
              "allow_extrapolation": allow_extrapolation, "excluded": excluded, "not_evaluated": failures, "count": len(candidates), "screen_plan": None, "short_plan": None}
    if candidates:
        write_json(folder / "screen.json", plan)
        write_json(folder / "short.json", {**plan, "stages": [{"local": {"max_seconds": short_seconds, "algorithm": "auto"}}]})
        result.update(screen_plan=(folder / "screen.json").relative_to(job.root).as_posix(),
                      short_plan=(folder / "short.json").relative_to(job.root).as_posix())
    write_json(folder / "generated.json", result)
    job.seal(folder)
    return result


def screen(ctx, generated: dict, *, workers: int | None = None) -> dict:
    from saos import scan
    if not generated.get("screen_plan") or not generated.get("short_plan"):
        raise SaosError(ErrorCode.BAD_ARGS, "没有可初筛的候选，查看 generated.json 的排除与未评估原因")
    plan = copy.deepcopy(read_json(jobs.inside(ctx.job.root, generated["short_plan"])))
    if not plan.get("prepare"):
        raise SaosError(ErrorCode.BAD_ARGS, "短优化计划缺 prepare；请设置变量和评价函数后重新生成")
    initial = scan.run(ctx, read_json(jobs.inside(ctx.job.root, generated["screen_plan"])), workers=workers)
    if ctx.cancel_requested():
        return {"initial": initial, "short": None, "cancelled": True}
    prepared = jobs.inside(ctx.job.root, initial["folder"] + "/prepare.py")
    if prepared.is_file():
        plan["prepare"] = prepared.relative_to(ctx.job.root).as_posix()
    plan["screening_scan"] = initial["folder"]
    short = scan.run(ctx, plan, workers=workers)
    return {"initial": initial, "short": short}
