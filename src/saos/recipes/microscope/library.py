"""初始结构库：条目的存储、指标、基准规格推导、查询与优化基准分组。

    <SAOS_LIBRARY>/
        entries/<id>/
            entry.json          元数据、状态（extracted / admitted / rejected）、标度无关指标
            extract.json        子 agent 的提取结果（schema 2）
            prescription.json   适配后的处方（原始焦距）
            expect.json         复现校验期望值
            adapt_report.json   适配报告（玻璃匹配、假设）
            repro.json          复现校验结果
            probe_accept.json   格式默认规格下的验收结果（标定证据）
            benchmark.yaml      基准规格（原结构在它下面 G = 0；受控扰动后的验收口径）
            bench_accept.json   基准规格下的验收结果（应为 G = 0）
        candidates.json         检索到的候选及处理结论
        benchmarks.json         优化基准案例与调参组 / 验证组划分
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

import yaml

from saos import config
from saos.starts import library as starting_library
from saos.protocol import write_json
from saos.recipes.microscope.spec import REFERENCE_TUBE_MM

ENTRY_ID_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.")
# 第 1 步格式默认值；基准规格从测量值推导时的出发点
DEFAULT_LENGTH_MM = 45.0
DEFAULT_DIAMETER_MM = 16.0
DEFAULT_CENTER_MM = 0.5
DEFAULT_EDGE_MM = 0.3
CALIBRATION_MARGIN = 1.2
# 有权威依据、不允许为基准放宽的项目（第 1 步文档 2.3 节标为“已定”），以及物理上必须满足的空气间隔非负
FIXED_ITEMS = ("axial_rms_wfe", "axial_color", "osc", "plan_field", "magnification", "glass_status", "surface_types", "air_gap")
PERTURBATION = {"seed": 1, "radius_rel": 0.01, "thickness_rel": 0.03}


def library_dir() -> Path:
    return Path(os.environ.get("SAOS_LIBRARY", config.REPO_ROOT / "library" / "microscope")).resolve()


def entry_dir(entry_id: str) -> Path:
    return starting_library.entry_path(library_dir(), entry_id)


def load_entry(entry_id: str) -> dict[str, Any]:
    return starting_library.load_entry(library_dir(), entry_id)


def save_entry(entry_id: str, entry: dict[str, Any]) -> None:
    write_json(entry_dir(entry_id) / "entry.json", entry)


def write_file(entry_id: str, name: str, data: Any) -> Path:
    path = entry_dir(entry_id) / name
    if name.endswith(".yaml"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data if isinstance(data, str) else yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    else:
        write_json(path, data)
    return path


def all_entries() -> list[dict[str, Any]]:
    return starting_library.entries(library_dir())


# ---- 指标 ----


def scale_free_metrics(entry: dict[str, Any]) -> dict[str, Any]:
    """与焦距无关的指标；缩放到焦距 f 时，长度类指标乘以 f / f_entry。"""
    m = entry["metrics"]
    f = m["focal_length_mm"]
    return {
        "na": m["na"],
        "ofn_at_reference_tube": m["field_number"],
        "wd_mech_over_f": m.get("wd_mech_mm", math.nan) / f,
        "length_over_f": m.get("optical_length_mm", math.nan) / f,
        "diameter_over_f": m.get("clear_diameter_mm", math.nan) / f,
        "elements": m["elements"],
    }


# ---- 基准规格推导 ----


def probe_spec(entry: dict[str, Any], correction_class: str) -> dict[str, Any]:
    """格式默认规格：只把与该结构固有参数相关的条目（NA、倍率、视场、盖玻片、玻璃目录）填上，
    界限类取宽松值，用来测量各项数值，作为标定证据。"""
    m = entry["metrics"]
    cover = m.get("cover_glass")
    return {
        "spec_version": 1,
        "correction_class": correction_class,
        "magnification": REFERENCE_TUBE_MM / m["focal_length_mm"],
        "na": m["na"],
        "tube_lens_focal_length": REFERENCE_TUBE_MM,
        "objective_field_number": m["field_number"],
        "receiver": "visual",
        "cover_glass": None if not cover else {"thickness": cover["thickness_mm"], "nd": cover["n"], "vd": cover["nu"]},
        "limits": {
            "magnification_tolerance": 0.05,
            "working_distance_min": 0.01,
            "optical_length_max": 1e6,
            "max_clear_diameter": 1e6,
            "glass": {
                "catalogs": m["catalogs"],
                "allowed_status": ["Standard", "Preferred", "Obsolete", "Special"],
                # 原文折射率精度不足、入库时用了模型玻璃的结构，基准规格允许模型玻璃
                "allow_model": any(g.get("status") == "Model" for g in m.get("glasses", [])),
            },
            "min_center_thickness_glass": 1e-3,
            "min_edge_thickness_glass": 1e-3,
            "min_air_gap": {"value": 0.0, "scale": 0.01},
        },
    }


def derive_benchmark_spec(entry: dict[str, Any], probe: dict[str, Any], correction_class: str) -> dict[str, Any]:
    """从格式默认规格下的验收结果推导基准规格。

    - 有权威依据的项目（FIXED_ITEMS）不放宽：不通过则该条目不能做优化基准。
    - 结构界限按测量值收紧到“刚好通过”附近，避免太宽而失去约束力：
      工作距离下限取实测的 95%；长度、口径上限取默认值与实测 ×1.1 中较大者；厚度下限取默认值与实测 ×0.9 中较小者。
    - 待标定的像质阈值：默认值能通过就用默认值；不能通过就取实测 ×1.2，并记录为标定证据。
    """
    items = {i["id"]: i for i in probe["items"]}
    failed_fixed = [i for i in FIXED_ITEMS if i in items and not items[i]["pass"]]
    m = entry["metrics"]
    overrides: dict[str, Any] = {}
    targets: dict[str, Any] = {}
    evidence = []
    for item_id in ("field_rms_wfe", "lateral_color"):
        it = items.get(item_id)
        if it is None:
            continue
        if it["pass"]:
            continue
        value = float(it["value"])
        new = math.ceil(value * CALIBRATION_MARGIN * 1000) / 1000
        key = "threshold_um" if item_id == "lateral_color" else "threshold"
        targets[item_id] = {key: new, "basis": f"benchmark only: reproduced value {value:.4g} fails the format default {it['threshold']:.4g}; using ×{CALIBRATION_MARGIN}"}
        evidence.append({"item": item_id, "default": it["threshold"], "measured": value, "benchmark_threshold": new})

    wd = float(items["working_distance"]["value"])
    length = float(items["optical_length"]["value"])
    diameter = float(items["clear_diameter"]["value"])
    center = float(items["center_thickness_glass"]["value"])
    edge = float(items["edge_thickness_glass"]["value"])
    overrides["working_distance_min"] = round(math.floor(wd * 0.95 * 100) / 100, 2)
    overrides["optical_length_max"] = max(DEFAULT_LENGTH_MM, math.ceil(length * 1.1))
    overrides["max_clear_diameter"] = max(DEFAULT_DIAMETER_MM, math.ceil(diameter * 1.1))
    overrides["min_center_thickness_glass"] = min(DEFAULT_CENTER_MM, math.floor(center * 0.9 * 100) / 100)
    overrides["min_edge_thickness_glass"] = min(DEFAULT_EDGE_MM, math.floor(edge * 0.9 * 100) / 100)
    for key, default, measured in (
        ("optical_length_max", DEFAULT_LENGTH_MM, length),
        ("max_clear_diameter", DEFAULT_DIAMETER_MM, diameter),
    ):
        if measured > default:
            evidence.append({"item": key, "default": default, "measured": measured, "benchmark_threshold": overrides[key]})
    for key, default, measured in (
        ("min_center_thickness_glass", DEFAULT_CENTER_MM, center),
        ("min_edge_thickness_glass", DEFAULT_EDGE_MM, edge),
    ):
        if measured < default:
            evidence.append({"item": key, "default": default, "measured": measured, "benchmark_threshold": overrides[key]})

    spec = probe_spec(entry, correction_class)
    spec["limits"].update(overrides)
    spec["limits"]["glass"]["allowed_status"] = ["Standard", "Preferred", "Obsolete", "Special"]
    if targets:
        spec["targets"] = targets
    return {
        "spec": spec,
        "eligible": not failed_fixed,
        "failed_fixed_items": {i: {"value": items[i]["value"], "threshold": items[i]["threshold"]} for i in failed_fixed},
        "calibration_evidence": evidence,
        "note": f"benchmark-only spec derived from the acceptance of {entry['id']} under the format default spec (saos.recipes.microscope.library.derive_benchmark_spec)",
        "perturbation": PERTURBATION,
        "_metrics_at_derivation": {"focal_length_mm": m["focal_length_mm"]},
    }


# ---- 查询 ----


def flag_text(flag: dict[str, Any], lang: str) -> str:
    """候选结构与规格的差异；agent 读英文，计划页按语言显示。"""
    kind, zh = flag["kind"], lang == "zh"
    if kind == "na_low":
        return f"NA {flag['value']:.3g} 低于要求 {flag['required']:.3g}" if zh else f"NA {flag['value']:.3g} below the required {flag['required']:.3g}"
    if kind == "field_number_low":
        return f"视场数 {flag['value']:.3g} 小于要求 {flag['required']:.3g}" if zh else f"field number {flag['value']:.3g} below the required {flag['required']:.3g}"
    if kind == "correction_class":
        return f"声称的校正等级为 {flag['claimed'] or '未注明'}" if zh else f"claimed correction class: {flag['claimed'] or 'not stated'}"
    if kind == "working_distance_low":
        return (f"缩放后机械工作距离 {flag['value']:.3g} mm 小于要求 {flag['required']:.3g} mm" if zh
                else f"scaled mechanical working distance {flag['value']:.3g} mm below the required {flag['required']:.3g} mm")
    raise ValueError(f"unknown flag {kind}")


def query(
    *,
    na: float,
    magnification: float,
    correction_class: str,
    objective_field_number: float | None = None,
    working_distance_min: float | None = None,
    tube_lens_mm: float = REFERENCE_TUBE_MM,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """按光学规格从已收录条目中选初始结构。

    排序：先看 NA（低于要求的扣分重）、视场、校正等级，再看缩放到目标焦距后的工作距离、长度和口径。
    分数越小越合适；各项扣分的系数是经验值，需要用基准案例检验。
    """
    f_target = tube_lens_mm / magnification
    out = []
    for e in all_entries():
        if e.get("status") != "admitted":
            continue
        sf = scale_free_metrics(e)
        s = f_target / e["metrics"]["focal_length_mm"]
        score = 0.0
        flags = []
        if sf["na"] < na:
            score += 10 * (na - sf["na"]) / na
            flags.append({"kind": "na_low", "value": sf["na"], "required": na})
        else:
            score += (sf["na"] - na) / na
        if objective_field_number and sf["ofn_at_reference_tube"] < objective_field_number:
            score += 3 * (objective_field_number - sf["ofn_at_reference_tube"]) / objective_field_number
            flags.append({"kind": "field_number_low", "value": sf["ofn_at_reference_tube"], "required": objective_field_number})
        claimed = e["metrics"].get("claimed_class") or ""
        if correction_class == "plan_achromat" and "plan" not in claimed.lower():
            score += 1.0
            flags.append({"kind": "correction_class", "claimed": claimed or None})
        wd_scaled = sf["wd_mech_over_f"] * f_target
        if working_distance_min and wd_scaled < working_distance_min:
            score += 3 * (working_distance_min - wd_scaled) / working_distance_min
            flags.append({"kind": "working_distance_low", "value": wd_scaled, "required": working_distance_min})
        out.append(
            {
                "id": e["id"],
                "score": round(score, 4),
                "scale": s,
                "scaled": {
                    "focal_length_mm": f_target,
                    "working_distance_mech_mm": wd_scaled,
                    "optical_length_mm": sf["length_over_f"] * f_target,
                    "clear_diameter_mm": sf["diameter_over_f"] * f_target,
                },
                "na": sf["na"],
                "elements": sf["elements"],
                "claimed_class": claimed or None,
                "flags": flags,
                "notes": [flag_text(f, "en") for f in flags],
            }
        )
    out.sort(key=lambda r: r["score"])
    return out[:limit]
