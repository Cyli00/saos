"""需求访谈的结构化交接：保留来源，检查缺项，复用现有规格解析器。"""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
from typing import Any

import yaml

from zea.protocol import ErrorCode, ZeaError
from zea.spec import WAVELENGTHS_UM, parse_spec


FIELDS = {
    "magnification": ("标称放大倍率", "×", "配合参考筒镜时，像相对于物体放大的倍数。"),
    "na": ("物方数值孔径", "—", "决定物镜接收光线的角度，也是分辨细节能力的重要条件。"),
    "correction_class": ("校正等级", "—", "消色差主要约束轴上像质；平场消色差另外约束视场内的性能。"),
    "receiver": ("接收器", "—", "相机需要像素尺寸，并启用 MTF 验收。"),
    "pixel_size": ("像素尺寸", "µm", "相机单个像素的边长，用于确定默认 MTF 检查频率。"),
    "tube_lens_focal_length": ("参考筒镜焦距", "mm", "评估使用的理想筒镜；本任务不设计真实筒镜。"),
    "objective_field_number": ("物镜视场数", "mm", "一次像面上需要覆盖的直径；它本身不保证整个范围都清晰。"),
    "cover_glass": ("盖玻片", "mm；nd、νd 无量纲", "无盖玻片写 null；有盖玻片时厚度和材料一起参与建模。"),
    "wavelengths.um": ("验收波长", "µm", "第一版固定采用 F′、e、C′，主波长为 e 线。"),
    "wavelengths.weights": ("波长权重", "—", "各条波长在多色计算中的相对权重。"),
    "limits.magnification_tolerance": ("放大率公差", "比例", "0.05 表示实际放大倍率相对标称值最多偏离 5%。"),
    "limits.working_distance_min": ("最小工作距离", "mm", "物镜前端与盖玻片上表面之间的最小空气距离；无盖玻片时从物面量起。"),
    "limits.optical_length_max": ("光学长度上限", "mm", "物面到物镜最后一个透镜面顶点的轴向距离，不等于机械齐焦距离。"),
    "limits.max_clear_diameter": ("最大通光直径", "mm", "限制各面通过光线所需的直径，不是镜筒外径。"),
    "limits.min_center_thickness_glass": ("玻璃最小中心厚", "mm", "避免透镜中心过薄。"),
    "limits.min_edge_thickness_glass": ("玻璃最小边缘厚", "mm", "避免透镜在通光口径边缘过薄。"),
    "limits.min_air_gap": ("最小空气间隔", "mm", "value 是中心与边缘的最小间隔；scale 只用于计算规格差距。"),
    "limits.max_elements": ("最多片数", "片", "胶合透镜按组成它的玻璃元件数计数。"),
    "limits.glass.catalogs": ("允许的玻璃目录", "—", "最终设计使用的玻璃目录；候选结构可能需要换玻璃。"),
    "limits.glass.allowed_status": ("允许的玻璃状态", "—", "Standard、Preferred 为本项目默认允许的状态。"),
    "limits.glass.allow_model": ("允许模型玻璃", "—", "模型玻璃没有可直接采购的确定牌号。"),
    "targets.axial_rms_wfe": ("轴上单色波像差", "λe", "限制中心位置的均方根波像差。"),
    "targets.osc": ("正弦差", "—", "限制近轴视场的正弦条件偏差。"),
    "targets.field_rms_wfe.range": ("视场波像差检查范围", "相对视场半径", "1 表示检查到整个规定视场边缘；0.85 表示半径的 85%。"),
    "targets.field_rms_wfe.threshold": ("视场波像差达标线", "λe", "固定验收焦面上的波像差要求；扣除项沿用验收口径。"),
    "targets.lateral_color.range": ("倍率色差检查范围", "相对视场半径", "检查各波长形成的像点在视场中的偏移。"),
    "targets.lateral_color.threshold_um": ("倍率色差达标线", "µm 一次像面", "限制 F′ 与 C′ 像点之间的横向距离。"),
    "targets.mtf.frequency": ("MTF 检查频率", "lp/mm", "每毫米线对数，必须与频率所在的面一起理解。"),
    "targets.mtf.plane": ("MTF 频率所在的面", "—", "object 为物方；image 为一次像面。"),
    "targets.mtf.threshold": ("MTF 达标线", "—", "该空间频率的对比度传递不得低于此值。"),
    "targets.mtf.range": ("MTF 检查范围", "相对视场半径", "在固定验收焦面检查，不能逐视场重新调焦。"),
    "targets.distortion.threshold_pct": ("畸变达标线", "%", "视场内像高相对理想像高的偏差上限。"),
    "targets.distortion.range": ("畸变检查范围", "相对视场半径", "测量用途需明确允许的畸变及检查范围。"),
}

ITEM_NAMES = {
    "magnification": "放大率公差", "working_distance": "工作距离", "optical_length": "光学长度",
    "clear_diameter": "通光直径", "glass_status": "玻璃状态", "surface_types": "面型",
    "center_thickness_glass": "玻璃中心厚", "edge_thickness_glass": "玻璃边缘厚", "air_gap": "空气间隔",
    "elements": "片数", "axial_rms_wfe": "轴上单色波像差", "axial_color": "轴向色差", "osc": "正弦差",
    "plan_field": "平场比", "field_rms_wfe": "视场波像差", "lateral_color": "倍率色差", "mtf": "MTF", "distortion": "畸变",
}

BASE_DEFAULTS = {
    "spec_version": 1, "tube_lens_focal_length": 200.0,
    "wavelengths": {"um": list(WAVELENGTHS_UM), "weights": [1.0, 1.0, 1.0]},
    "limits": {"magnification_tolerance": 0.05, "optical_length_max": 45.0, "max_clear_diameter": 16.0,
               "min_center_thickness_glass": 0.5, "min_edge_thickness_glass": 0.3,
               "min_air_gap": {"value": 0.0, "scale": 0.01},
               "glass": {"catalogs": ["CDGM"], "allowed_status": ["Standard", "Preferred"], "allow_model": False}},
    "targets": {"axial_rms_wfe": 1 / 14, "osc": 0.0025},
}

DEFAULT_BASES = {
    "na": "按第一版约定的倍率／NA 组合提出，需确认是否适合观察对象。",
    "correction_class": "根据接收器选择校正等级；是否要求整个视场清晰仍需对照用户用途。",
    "tube_lens_focal_length": "第一版使用 200 mm 理想参考筒镜；若需兼容现有设备，请核对其筒镜。",
    "objective_field_number": "沿用第一版视场数假设；消色差的视场数不保证边缘清晰。",
    "wavelengths.um": "采用第一版固定的 F′、e、C′ 消色差验收谱线。",
    "wavelengths.weights": "三条波长等权是项目假设，未按实际光源和相机响应加权。",
    "limits.magnification_tolerance": "沿用第一版 ±5% 的放大率验收公差。",
    "limits.working_distance_min": "沿用第一版同倍率商品物镜的工作距离参考值，并非用户指定。",
    "limits.optical_length_max": "45 mm 是待标定的结构长度上限，不表示机械齐焦兼容。",
    "limits.max_clear_diameter": "16 mm 是待标定的通光直径上限，尚未结合镜筒设计确认。",
    "limits.min_center_thickness_glass": "0.5 mm 是待标定的最小中心厚假设。",
    "limits.min_edge_thickness_glass": "0.3 mm 是待标定的最小边缘厚假设。",
    "limits.min_air_gap": "要求镜片不相交；0.01 mm 仅用于规格差距的归一化。",
    "limits.glass.catalogs": "第一版默认选成都光明 CDGM 目录；初始结构可能需要换玻璃。",
    "limits.glass.allowed_status": "沿用第一版允许的目录状态，排除停产玻璃。",
    "limits.glass.allow_model": "最终设计默认要求确定的玻璃牌号。",
    "targets.axial_rms_wfe": "沿用第一版 λ/14 的轴上均方根波像差判据。",
    "targets.osc": "沿用第一版正弦差判据。",
    "targets.mtf.frequency": "按像素奈奎斯特频率 500 / 像素尺寸（µm）计算一次像面频率。",
    "targets.mtf.threshold": "0.3 是待标定的 MTF 达标线，需要结合用途确认。",
    "targets.mtf.plane": "默认把 MTF 频率定义在一次像面。",
    "targets.mtf.range": "默认检查到规定视场的边缘。",
    "targets.field_rms_wfe.range": "平场消色差默认检查到相对视场半径 0.85。",
    "targets.field_rms_wfe.threshold": "λ/14 是待标定的离轴波像差达标线。",
    "targets.lateral_color.range": "默认检查到规定视场的边缘。",
    "targets.lateral_color.threshold_um": "按一次像面艾里斑半径提出，属于待标定阈值。",
    "targets.distortion.range": "默认在整个规定视场检查畸变。",
}


def bad(message: str) -> None:
    raise ZeaError(ErrorCode.BAD_ARGS, message)


def read_draft(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        bad(f"无法读取访谈记录：{exc}")
    if not isinstance(data, dict):
        bad("访谈记录必须为 YAML 映射")
    return data


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if path in FIELDS or path == "spec_version":
            out[path] = value
        elif isinstance(value, dict) and any(k.startswith(path + ".") for k in FIELDS):
            out.update(flatten(value, path))
        else:
            bad(f"不支持的规格字段 {path}，请澄清或记录为待解决问题，不能静默忽略")
    return out


def assign(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    for key in parts[:-1]:
        data = data.setdefault(key, {})
    data[parts[-1]] = deepcopy(value)


def number(value: Any, path: str, *, zero: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or (value < 0 if zero else value <= 0):
        bad(f"{path} 必须是{'非负' if zero else '正的'}有限数")


def check(draft: dict[str, Any]) -> dict[str, Any]:
    allowed = {"schema", "request", "spec", "provenance", "requirements", "questions", "scope"}
    if set(draft) - allowed:
        bad(f"未知访谈字段：{sorted(set(draft) - allowed)}")
    if draft.get("schema") != 1 or not isinstance(draft.get("request"), str) or not draft["request"].strip():
        bad("访谈记录需要 schema: 1 和非空的 request（用户原话）")
    if not isinstance(draft.get("spec", {}), dict) or not isinstance(draft.get("provenance", {}), dict):
        bad("spec 和 provenance 必须是映射")
    supplied = flatten(draft.get("spec", {}))
    for path in ("magnification", "na", "pixel_size"):
        if path in supplied:
            number(supplied[path], path)
    provenance = draft.get("provenance", {})
    if set(provenance) - (set(supplied) - {"spec_version"}):
        bad("provenance 中存在未提供的规格字段")
    for path, value in supplied.items():
        if path == "spec_version":
            continue
        p = provenance.get(path)
        if not isinstance(p, dict) or set(p) != {"source", "basis"} or p.get("source") not in ("user", "assumption") or not isinstance(p.get("basis"), str) or not p["basis"].strip():
            bad(f"{path} 需要 provenance：source 为 user 或 assumption，basis 写用户原话或补全依据")
        if isinstance(value, dict) and path not in ("cover_glass", "limits.min_air_gap"):
            bad(f"{path} 应直接填写值；来源统一放在 provenance 中")
    questions = draft.get("questions", [])
    if not isinstance(questions, list) or any(not isinstance(q, str) or not q.strip() for q in questions):
        bad("questions 必须为待问问题的字符串列表")
    issues = list(questions)
    if "magnification" in supplied and provenance["magnification"]["source"] != "user":
        issues.append("标称放大倍率不能由 agent 自行假设，需要向用户问出。")
    scope = draft.get("scope", {"medium": "air", "conjugate": "infinity"})
    if scope != {"medium": "air", "conjugate": "infinity"}:
        issues.append("第一版只支持空气中的无限远校正显微物镜；当前用途需澄清或另立扩展任务。")
    effective = deepcopy(BASE_DEFAULTS)
    for path, value in supplied.items():
        assign(effective, path, value)
    for group in draft.get("spec", {}).get("targets", {}):
        effective["targets"].setdefault(group, {})
    for path in ("magnification", "receiver", "cover_glass"):
        if path not in supplied:
            issues.append({"magnification": "需要多少倍的物镜？", "receiver": "用于目视观察还是相机采集？", "cover_glass": "标本是否有盖玻片？有的话厚度是多少？"}[path])
    m = supplied.get("magnification")
    if m is not None:
        number(m, "magnification")
    if "na" not in supplied:
        na = {4: 0.1, 10: 0.25, 20: 0.4, 40: 0.65}.get(m)
        if na is None:
            issues.append("需要明确物方数值孔径；当前倍率没有已约定的补全值。")
        else:
            effective["na"] = na
    if "limits.working_distance_min" not in supplied:
        wd = {10: 10.6, 20: 1.2, 40: 0.6}.get(m)
        if wd is None:
            issues.append("物镜前端至少需要离标本或盖玻片多远（mm）？当前倍率没有已核实的补全值。")
        else:
            effective["limits"]["working_distance_min"] = wd
    effective.setdefault("correction_class", "plan_achromat" if effective.get("receiver") == "camera" else "achromat")
    plan = effective["correction_class"] == "plan_achromat"
    effective.setdefault("objective_field_number", 22.0 if plan else 18.0)
    targets = effective["targets"]
    if plan:
        targets.setdefault("field_rms_wfe", {})
        targets.setdefault("lateral_color", {})
    if "field_rms_wfe" in targets:
        fw = targets["field_rms_wfe"]
        fw.setdefault("threshold", 1 / 14)
        if "range" not in fw:
            if plan:
                fw["range"] = 0.85
            else:
                issues.append("视场波像差要求覆盖到多大范围（相对视场半径）？")
    if "lateral_color" in targets:
        lc = targets["lateral_color"]
        lc.setdefault("range", 1.0)
        if effective.get("na") and m:
            lc.setdefault("threshold_um", 0.61 * WAVELENGTHS_UM[1] * m / effective["na"])
    if effective.get("receiver") == "camera":
        if not effective.get("pixel_size"):
            issues.append("相机像素尺寸是多少 µm？")
        targets.setdefault("mtf", {})
    if "mtf" in targets:
        mtf = targets["mtf"]
        if effective.get("receiver") == "camera":
            mtf.setdefault("plane", "image")
            mtf.setdefault("threshold", 0.3)
            mtf.setdefault("range", 1.0)
            if "frequency" not in mtf and effective.get("pixel_size"):
                number(effective["pixel_size"], "pixel_size")
                if mtf["plane"] == "object":
                    issues.append("已指定物方 MTF，请明确物方频率（lp/mm）。")
                else:
                    mtf["frequency"] = 500 / effective["pixel_size"]
        for key in ("frequency", "plane", "threshold", "range"):
            if key not in mtf:
                issues.append(f"请明确 MTF 的 {key}。")
    if "distortion" in targets:
        targets["distortion"].setdefault("range", 1.0)
        if "threshold_pct" not in targets["distortion"]:
            issues.append("允许的畸变上限是多少 %？可提出假设，但不能把测量用途静默变成无畸变要求。")
    flat = flatten(effective)
    for path, value in flat.items():
        if path in {"spec_version", "correction_class", "receiver", "cover_glass", "wavelengths.um", "wavelengths.weights", "limits.glass.catalogs", "limits.glass.allowed_status", "limits.glass.allow_model", "limits.min_air_gap", "targets.mtf.plane"}:
            continue
        number(value, path)
        if path.endswith(".range") and value > 1:
            bad(f"{path} 不能超过 1；扩大视场请调整物镜视场数")
    if effective.get("na", 0) > 0.65:
        issues.append("NA 超出第一版干物镜 NA ≤ 0.65 的范围。")
    if effective.get("receiver") not in (None, "visual", "camera"):
        bad("receiver 必须为 visual 或 camera")
    if effective["correction_class"] not in ("achromat", "plan_achromat"):
        issues.append("校正等级超出第一版消色差／平场消色差的范围。")
    if targets.get("mtf", {}).get("plane", "image") not in ("image", "object"):
        bad("MTF plane 必须为 image 或 object")
    if targets.get("mtf", {}).get("threshold", 0) > 1:
        bad("MTF 阈值不能大于 1")
    if "max_elements" in effective["limits"] and (not isinstance(effective["limits"]["max_elements"], int) or isinstance(effective["limits"]["max_elements"], bool)):
        bad("最多片数必须是正整数")
    cover = effective.get("cover_glass")
    if cover is not None:
        if not isinstance(cover, dict) or set(cover) != {"thickness", "nd", "vd"}:
            bad("cover_glass 必须为 null 或包含 thickness、nd、vd 的映射")
        for k, v in cover.items():
            number(v, f"cover_glass.{k}")
        if cover["nd"] <= 1:
            bad("盖玻片 nd 必须大于 1")
    gap = effective["limits"]["min_air_gap"]
    if not isinstance(gap, dict) or set(gap) != {"value", "scale"}:
        bad("min_air_gap 必须含 value 和 scale")
    number(gap["value"], "min_air_gap.value", zero=True)
    number(gap["scale"], "min_air_gap.scale")
    glass = effective["limits"]["glass"]
    for key in ("catalogs", "allowed_status"):
        if not isinstance(glass[key], list) or not glass[key] or any(not isinstance(v, str) or not v for v in glass[key]):
            bad(f"glass.{key} 必须为非空字符串列表")
    if not isinstance(glass["allow_model"], bool):
        bad("glass.allow_model 必须为布尔值")
    weights = effective["wavelengths"]["weights"]
    if not isinstance(weights, list) or len(weights) != 3:
        bad("波长权重必须有 3 个值")
    for w in weights:
        number(w, "波长权重", zero=True)
    if not any(weights):
        bad("至少一个波长权重必须大于 0")
    requirements = draft.get("requirements", [])
    if not isinstance(requirements, list):
        bad("requirements 必须是列表")
    if not requirements:
        issues.append("请把用户原话中的条件逐条映射为约束、性能指标或期望项。")
    for r in requirements:
        if not isinstance(r, dict) or set(r) - {"text", "kind", "fields", "note"} or not isinstance(r.get("text"), str) or not r["text"].strip():
            bad("每条 requirement 需要 text、kind 和 fields；可选 note")
        kind, paths = r.get("kind"), r.get("fields", [])
        if kind not in ("constraint", "target", "preference", "unresolved") or not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
            bad("requirement 的 kind 或 fields 不正确")
        if any(p not in flat or p == "spec_version" for p in paths):
            bad(f"用户条件引用了未定义的字段：{r['text']}")
        if kind == "unresolved" or (kind in ("constraint", "target") and not paths):
            issues.append(f"尚未落实：{r['text']}")
        if kind == "target" and paths and not any(p.startswith("targets.") or p == "correction_class" for p in paths):
            issues.append(f"性能指标尚未对应到验收项：{r['text']}")
    rows = []
    for path, value in flat.items():
        if path == "spec_version":
            continue
        origin = provenance.get(path, {"source": "assumption", "basis": DEFAULT_BASES.get(path, "按第一版规格口径提出，需确认。")})
        label, unit, explanation = FIELDS[path]
        rows.append({"path": path, "label": label, "unit": unit, "value": value, "explanation": explanation, **origin})
    spec = None
    if not issues:
        try:
            spec = parse_spec(effective)
        except (ValueError, TypeError, KeyError) as exc:
            bad(f"规格格式错误：{exc}")
    return {"ready": not issues, "questions": list(dict.fromkeys(issues)), "request": draft["request"], "requirements": requirements,
            "spec": effective, "fields": rows, "derived": spec.derived() if spec else {},
            "scope": {"medium": "air", "conjugate": "infinity"},
            "scope_basis": "第一版固定范围：干物镜、无限远校正、球面或平面、参考筒镜。"}
