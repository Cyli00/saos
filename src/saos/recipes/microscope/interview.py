"""需求访谈的结构化交接：保留来源，检查缺项，复用现有规格解析器。"""

from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
from typing import Any

import yaml

from saos.protocol import ErrorCode, SaosError
from saos.recipes.microscope.spec import REFERENCE_TUBE_MM, WAVELENGTHS_UM, parse_spec


FIELDS_ZH = {
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

ITEM_NAMES_ZH = {
    "magnification": "放大率公差", "working_distance": "工作距离", "optical_length": "光学长度",
    "clear_diameter": "通光直径", "glass_status": "玻璃状态", "surface_types": "面型",
    "center_thickness_glass": "玻璃中心厚", "edge_thickness_glass": "玻璃边缘厚", "air_gap": "空气间隔",
    "elements": "片数", "axial_rms_wfe": "轴上单色波像差", "axial_color": "轴向色差", "osc": "正弦差",
    "plan_field": "平场比", "field_rms_wfe": "视场波像差", "lateral_color": "倍率色差", "mtf": "MTF", "distortion": "畸变",
}

BASE_DEFAULTS = {
    "spec_version": 1, "tube_lens_focal_length": REFERENCE_TUBE_MM,
    "wavelengths": {"um": list(WAVELENGTHS_UM), "weights": [1.0, 1.0, 1.0]},
    "limits": {"magnification_tolerance": 0.05, "optical_length_max": 45.0, "max_clear_diameter": 16.0,
               "min_center_thickness_glass": 0.5, "min_edge_thickness_glass": 0.3,
               "min_air_gap": {"value": 0.0, "scale": 0.01},
               "glass": {"catalogs": ["CDGM"], "allowed_status": ["Standard", "Preferred"], "allow_model": False}},
    "targets": {"axial_rms_wfe": 1 / 14, "osc": 0.0025},
}

DEFAULT_BASES_ZH = {
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


FIELDS_EN = {
    "magnification": ("Nominal magnification", "×", "How much the image is magnified relative to the object with the reference tube lens."),
    "na": ("Object-side numerical aperture", "—", "Sets the cone of light the objective accepts, a key condition for resolving detail."),
    "correction_class": ("Correction class", "—", "An achromat mainly constrains on-axis image quality; a plan achromat also constrains performance across the field."),
    "receiver": ("Receiver", "—", "A camera needs a pixel size and enables the MTF check."),
    "pixel_size": ("Pixel size", "µm", "Edge length of one camera pixel; sets the default MTF check frequency."),
    "tube_lens_focal_length": ("Reference tube lens focal length", "mm", "The ideal tube lens used for evaluation; this job does not design a real tube lens."),
    "objective_field_number": ("Objective field number", "mm", "Diameter to cover in the intermediate image; it does not by itself guarantee sharpness over that range."),
    "cover_glass": ("Cover glass", "mm; nd, νd unitless", "null means no cover glass; otherwise its thickness and material are both modelled."),
    "wavelengths.um": ("Acceptance wavelengths", "µm", "Fixed to F′, e, C′ in the first version; e is the primary wavelength."),
    "wavelengths.weights": ("Wavelength weights", "—", "Relative weight of each wavelength in polychromatic calculations."),
    "limits.magnification_tolerance": ("Magnification tolerance", "ratio", "0.05 means the actual magnification may deviate from nominal by at most 5%."),
    "limits.working_distance_min": ("Minimum working distance", "mm", "Minimum air distance from the objective front to the top of the cover glass; from the object plane when there is no cover glass."),
    "limits.optical_length_max": ("Maximum optical length", "mm", "Axial distance from the object plane to the last lens vertex; not the mechanical parfocal distance."),
    "limits.max_clear_diameter": ("Maximum clear diameter", "mm", "Limits the diameter each surface needs to pass the rays; not the barrel's outer diameter."),
    "limits.min_center_thickness_glass": ("Minimum glass center thickness", "mm", "Keeps lenses from getting too thin at the center."),
    "limits.min_edge_thickness_glass": ("Minimum glass edge thickness", "mm", "Keeps lenses from getting too thin at the edge of the clear aperture."),
    "limits.min_air_gap": ("Minimum air gap", "mm", "value is the minimum gap at center and edge; scale only normalises the spec shortfall."),
    "limits.max_elements": ("Maximum elements", "elements", "A cemented lens counts each of its glass elements."),
    "limits.glass.catalogs": ("Allowed glass catalogs", "—", "Catalogs for the final design; the starting structure may need glass changes."),
    "limits.glass.allowed_status": ("Allowed glass status", "—", "Standard and Preferred are this project's default allowed statuses."),
    "limits.glass.allow_model": ("Allow model glass", "—", "A model glass has no definite grade that can be bought."),
    "targets.axial_rms_wfe": ("On-axis monochromatic wavefront error", "λe", "Limits the RMS wavefront error at the center."),
    "targets.osc": ("Offense against the sine condition", "—", "Limits the sine-condition deviation near the axis."),
    "targets.field_rms_wfe.range": ("Field wavefront check range", "relative field radius", "1 checks to the edge of the specified field; 0.85 checks to 85% of its radius."),
    "targets.field_rms_wfe.threshold": ("Field wavefront pass line", "λe", "Wavefront error required on the fixed acceptance focal plane; removed terms follow the acceptance rules."),
    "targets.lateral_color.range": ("Lateral color check range", "relative field radius", "Checks how far the image points of different wavelengths shift across the field."),
    "targets.lateral_color.threshold_um": ("Lateral color pass line", "µm intermediate image", "Limits the lateral distance between the F′ and C′ image points."),
    "targets.mtf.frequency": ("MTF check frequency", "lp/mm", "Line pairs per millimetre; read it together with the plane it refers to."),
    "targets.mtf.plane": ("Plane of the MTF frequency", "—", "object means object side; image means the intermediate image."),
    "targets.mtf.threshold": ("MTF pass line", "—", "Contrast transfer at this spatial frequency must not fall below this value."),
    "targets.mtf.range": ("MTF check range", "relative field radius", "Checked on the fixed acceptance focal plane, without refocusing per field."),
    "targets.distortion.threshold_pct": ("Distortion pass line", "%", "Upper limit on the deviation of image height from the ideal within the field."),
    "targets.distortion.range": ("Distortion check range", "relative field radius", "For measurement use, state the allowed distortion and the range it applies to."),
}

ITEM_NAMES_EN = {
    "magnification": "Magnification tolerance", "working_distance": "Working distance", "optical_length": "Optical length",
    "clear_diameter": "Clear diameter", "glass_status": "Glass status", "surface_types": "Surface types",
    "center_thickness_glass": "Glass center thickness", "edge_thickness_glass": "Glass edge thickness", "air_gap": "Air gap",
    "elements": "Elements", "axial_rms_wfe": "On-axis monochromatic WFE", "axial_color": "Axial color", "osc": "OSC",
    "plan_field": "Flatness ratio", "field_rms_wfe": "Field WFE", "lateral_color": "Lateral color", "mtf": "MTF", "distortion": "Distortion",
}

DEFAULT_BASES_EN = {
    "na": "Proposed from the first version's magnification/NA pairs; confirm it suits the specimen.",
    "correction_class": "Chosen from the receiver; whether the whole field must be sharp still needs checking against the use.",
    "tube_lens_focal_length": "The first version uses an ideal 200 mm reference tube lens; check your tube lens if existing equipment must fit.",
    "objective_field_number": "Keeps the first version's field number; an achromat's field number does not guarantee a sharp edge.",
    "wavelengths.um": "Uses the first version's fixed F′, e, C′ achromat acceptance lines.",
    "wavelengths.weights": "Equal weights are a project assumption, not weighted by the actual light source and camera response.",
    "limits.magnification_tolerance": "Keeps the first version's ±5% magnification tolerance.",
    "limits.working_distance_min": "Keeps the first version's reference value from commercial objectives of the same magnification; not given by the user.",
    "limits.optical_length_max": "45 mm is a length limit still to be calibrated; it does not imply mechanical parfocal compatibility.",
    "limits.max_clear_diameter": "16 mm is a clear-diameter limit still to be calibrated, not yet checked against a barrel design.",
    "limits.min_center_thickness_glass": "0.5 mm is a minimum center thickness assumption still to be calibrated.",
    "limits.min_edge_thickness_glass": "0.3 mm is a minimum edge thickness assumption still to be calibrated.",
    "limits.min_air_gap": "Lenses must not intersect; 0.01 mm only normalises the spec shortfall.",
    "limits.glass.catalogs": "The first version defaults to the CDGM catalog; the starting structure may need glass changes.",
    "limits.glass.allowed_status": "Keeps the first version's allowed catalog statuses, excluding obsolete glass.",
    "limits.glass.allow_model": "The final design needs definite glass grades by default.",
    "targets.axial_rms_wfe": "Keeps the first version's λ/14 on-axis RMS wavefront criterion.",
    "targets.osc": "Keeps the first version's OSC criterion.",
    "targets.mtf.frequency": "Intermediate-image frequency from the pixel Nyquist frequency, 500 / pixel size (µm).",
    "targets.mtf.threshold": "0.3 is an MTF pass line still to be calibrated; confirm it against the use.",
    "targets.mtf.plane": "The MTF frequency refers to the intermediate image by default.",
    "targets.mtf.range": "Checks to the edge of the specified field by default.",
    "targets.field_rms_wfe.range": "A plan achromat checks to relative field radius 0.85 by default.",
    "targets.field_rms_wfe.threshold": "λ/14 is an off-axis wavefront pass line still to be calibrated.",
    "targets.lateral_color.range": "Checks to the edge of the specified field by default.",
    "targets.lateral_color.threshold_um": "Proposed from the Airy radius in the intermediate image; still to be calibrated.",
    "targets.distortion.range": "Checks distortion over the whole specified field by default.",
}

FIELDS = {"zh": FIELDS_ZH, "en": FIELDS_EN}
ITEM_NAMES = {"zh": ITEM_NAMES_ZH, "en": ITEM_NAMES_EN}
DEFAULT_BASES = {"zh": DEFAULT_BASES_ZH, "en": DEFAULT_BASES_EN}
FALLBACK_BASIS = {"zh": "按第一版规格口径提出，需确认。", "en": "Proposed from the first version's spec conventions; please confirm."}
SCOPE_BASIS = {"zh": "第一版固定范围：干物镜、无限远校正、球面或平面、参考筒镜。",
               "en": "Fixed first-version scope: dry objective, infinity-corrected, spherical or flat surfaces, reference tube lens."}


def bad(message: str) -> None:
    raise SaosError(ErrorCode.BAD_ARGS, message)


def read_draft(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        bad(f"cannot read the interview record: {exc}")
    if not isinstance(data, dict):
        bad("the interview record must be a YAML mapping")
    return data


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    out = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if path in FIELDS_EN or path == "spec_version":
            out[path] = value
        elif isinstance(value, dict) and any(k.startswith(path + ".") for k in FIELDS_EN):
            out.update(flatten(value, path))
        else:
            bad(f"unsupported spec field {path}; clarify it or record it as unresolved, never drop it silently")
    return out


def assign(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    for key in parts[:-1]:
        data = data.setdefault(key, {})
    data[parts[-1]] = deepcopy(value)


def number(value: Any, path: str, *, zero: bool = False) -> None:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or (value < 0 if zero else value <= 0):
        bad(f"{path} must be a {'non-negative' if zero else 'positive'} finite number")


def check(draft: dict[str, Any]) -> dict[str, Any]:
    allowed = {"schema", "request", "spec", "provenance", "requirements", "questions", "scope"}
    if set(draft) - allowed:
        bad(f"unknown interview fields: {sorted(set(draft) - allowed)}")
    if draft.get("schema") != 1 or not isinstance(draft.get("request"), str) or not draft["request"].strip():
        bad("the interview record needs schema: 1 and a non-empty request (the user's words)")
    if not isinstance(draft.get("spec", {}), dict) or not isinstance(draft.get("provenance", {}), dict):
        bad("spec and provenance must be mappings")
    supplied = flatten(draft.get("spec", {}))
    for path in ("magnification", "na", "pixel_size"):
        if path in supplied:
            number(supplied[path], path)
    provenance = draft.get("provenance", {})
    if set(provenance) - (set(supplied) - {"spec_version"}):
        bad("provenance lists spec fields that were not supplied")
    for path, value in supplied.items():
        if path == "spec_version":
            continue
        p = provenance.get(path)
        if not isinstance(p, dict) or set(p) != {"source", "basis"} or p.get("source") not in ("user", "assumption") or not isinstance(p.get("basis"), str) or not p["basis"].strip():
            bad(f"{path} needs provenance: source user or assumption, basis with the user's words or the reason for the assumption")
        if isinstance(value, dict) and path not in ("cover_glass", "limits.min_air_gap"):
            bad(f"{path} takes the value directly; its source goes in provenance")
    questions = draft.get("questions", [])
    if not isinstance(questions, list) or any(not isinstance(q, str) or not q.strip() for q in questions):
        bad("questions must be a list of strings")
    issues = list(questions)
    if "magnification" in supplied and provenance["magnification"]["source"] != "user":
        issues.append("The nominal magnification must come from the user, not from an agent assumption.")
    scope = draft.get("scope", {"medium": "air", "conjugate": "infinity"})
    if scope != {"medium": "air", "conjugate": "infinity"}:
        issues.append("The first version supports only infinity-corrected objectives in air; clarify the use or open an extension task.")
    effective = deepcopy(BASE_DEFAULTS)
    for path, value in supplied.items():
        assign(effective, path, value)
    for group in draft.get("spec", {}).get("targets", {}):
        effective["targets"].setdefault(group, {})
    for path in ("magnification", "receiver", "cover_glass"):
        if path not in supplied:
            issues.append({"magnification": "What magnification is needed?", "receiver": "Visual observation or camera capture?", "cover_glass": "Is there a cover glass on the specimen? If so, how thick?"}[path])
    m = supplied.get("magnification")
    if m is not None:
        number(m, "magnification")
    if "na" not in supplied:
        na = {4: 0.1, 10: 0.25, 20: 0.4, 40: 0.65}.get(m)
        if na is None:
            issues.append("The object-side NA must be stated; there is no agreed default for this magnification.")
        else:
            effective["na"] = na
    if "limits.working_distance_min" not in supplied:
        wd = {10: 10.6, 20: 1.2, 40: 0.6}.get(m)
        if wd is None:
            issues.append("How far must the objective front stay from the specimen or cover glass (mm)? There is no verified default for this magnification.")
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
                issues.append("Over what range (relative field radius) must the field wavefront requirement hold?")
    if "lateral_color" in targets:
        lc = targets["lateral_color"]
        lc.setdefault("range", 1.0)
        if effective.get("na") and m:
            lc.setdefault("threshold_um", 0.61 * WAVELENGTHS_UM[1] * m / effective["na"])
    if effective.get("receiver") == "camera":
        if not effective.get("pixel_size"):
            issues.append("What is the camera pixel size in µm?")
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
                    issues.append("MTF is on the object side; state the object-side frequency (lp/mm).")
                else:
                    mtf["frequency"] = 500 / effective["pixel_size"]
        for key in ("frequency", "plane", "threshold", "range"):
            if key not in mtf:
                issues.append(f"State the MTF {key}.")
    if "distortion" in targets:
        targets["distortion"].setdefault("range", 1.0)
        if "threshold_pct" not in targets["distortion"]:
            issues.append("What distortion limit in % is allowed? An assumption may be proposed, but a measurement use must not silently become a zero-distortion requirement.")
    flat = flatten(effective)
    for path, value in flat.items():
        if path in {"spec_version", "correction_class", "receiver", "cover_glass", "wavelengths.um", "wavelengths.weights", "limits.glass.catalogs", "limits.glass.allowed_status", "limits.glass.allow_model", "limits.min_air_gap", "targets.mtf.plane"}:
            continue
        number(value, path)
        if path.endswith(".range") and value > 1:
            bad(f"{path} cannot exceed 1; enlarge the field through the objective field number")
    if effective.get("na", 0) > 0.65:
        issues.append("The NA is outside the first version's dry-objective range NA ≤ 0.65.")
    if effective.get("receiver") not in (None, "visual", "camera"):
        bad("receiver must be visual or camera")
    if effective["correction_class"] not in ("achromat", "plan_achromat"):
        issues.append("The correction class is outside the first version's achromat / plan achromat range.")
    if targets.get("mtf", {}).get("plane", "image") not in ("image", "object"):
        bad("MTF plane must be image or object")
    if targets.get("mtf", {}).get("threshold", 0) > 1:
        bad("the MTF threshold cannot exceed 1")
    if "max_elements" in effective["limits"] and (not isinstance(effective["limits"]["max_elements"], int) or isinstance(effective["limits"]["max_elements"], bool)):
        bad("maximum elements must be a positive integer")
    cover = effective.get("cover_glass")
    if cover is not None:
        if not isinstance(cover, dict) or set(cover) != {"thickness", "nd", "vd"}:
            bad("cover_glass must be null or a mapping with thickness, nd and vd")
        for k, v in cover.items():
            number(v, f"cover_glass.{k}")
        if cover["nd"] <= 1:
            bad("cover glass nd must exceed 1")
    gap = effective["limits"]["min_air_gap"]
    if not isinstance(gap, dict) or set(gap) != {"value", "scale"}:
        bad("min_air_gap needs value and scale")
    number(gap["value"], "min_air_gap.value", zero=True)
    number(gap["scale"], "min_air_gap.scale")
    glass = effective["limits"]["glass"]
    for key in ("catalogs", "allowed_status"):
        if not isinstance(glass[key], list) or not glass[key] or any(not isinstance(v, str) or not v for v in glass[key]):
            bad(f"glass.{key} must be a non-empty list of strings")
    if not isinstance(glass["allow_model"], bool):
        bad("glass.allow_model must be a boolean")
    weights = effective["wavelengths"]["weights"]
    if not isinstance(weights, list) or len(weights) != 3:
        bad("wavelength weights need 3 values")
    for w in weights:
        number(w, "wavelengths.weights", zero=True)
    if not any(weights):
        bad("at least one wavelength weight must be positive")
    requirements = draft.get("requirements", [])
    if not isinstance(requirements, list):
        bad("requirements must be a list")
    if not requirements:
        issues.append("Map each condition in the user's words to a constraint, target or preference.")
    for r in requirements:
        if not isinstance(r, dict) or set(r) - {"text", "kind", "fields", "note"} or not isinstance(r.get("text"), str) or not r["text"].strip():
            bad("each requirement needs text, kind and fields; note is optional")
        kind, paths = r.get("kind"), r.get("fields", [])
        if kind not in ("constraint", "target", "preference", "unresolved") or not isinstance(paths, list) or any(not isinstance(p, str) for p in paths):
            bad("requirement kind or fields are invalid")
        if any(p not in flat or p == "spec_version" for p in paths):
            bad(f"a requirement refers to an undefined field: {r['text']}")
        if kind == "unresolved" or (kind in ("constraint", "target") and not paths):
            issues.append(f"Not yet resolved: {r['text']}")
        if kind == "target" and paths and not any(p.startswith("targets.") or p == "correction_class" for p in paths):
            issues.append(f"Target not yet mapped to an acceptance item: {r['text']}")
    rows = []
    for path, value in flat.items():
        if path == "spec_version":
            continue
        # 自动补全的假设记下 default，计划页按语言取依据；agent 看到的是英文
        origin = provenance.get(path) or {"source": "assumption", "basis": DEFAULT_BASES_EN.get(path, FALLBACK_BASIS["en"]), "default": True}
        label, unit, explanation = FIELDS_EN[path]
        rows.append({"path": path, "label": label, "unit": unit, "value": value, "explanation": explanation, **origin})
    spec = None
    if not issues:
        try:
            spec = parse_spec(effective)
        except (ValueError, TypeError, KeyError) as exc:
            bad(f"invalid spec: {exc}")
    return {"ready": not issues, "questions": list(dict.fromkeys(issues)), "request": draft["request"], "requirements": requirements,
            "spec": effective, "fields": rows, "derived": spec.derived() if spec else {},
            "scope": {"medium": "air", "conjugate": "infinity"},
            "scope_basis": SCOPE_BASIS["en"]}
