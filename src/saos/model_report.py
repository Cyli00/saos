"""建模说明：建好模型后、开始优化前给用户看的单文件 HTML。
Model description: a single HTML page the user reviews after modelling and before any optimisation.

    saos lens describe --job J [--notes NOTES.json] [--lang zh|en]

内容全部从当前打开的镜头读取（backend.describe_model）：逐面面型与参数、材料来源及各波长折射率（INDX 实测）、
孔径、视场、波长、光线瞄准，以及布局图。agent 在 NOTES.json 里写每个面代表的实物、建模假设和一阶分析：

    {"surfaces": {"3": "透镜 1 背面：打印树脂 IP-S 自由曲面"}, "assumptions": ["棱镜斜面按理想反射"],
     "first_order": ["倍率 0.75：400 µm 光纤圆 → 300 µm 视场", "NA 0.5 光锥在棱镜入射面的口径 …"]}

自动检查（warnings）只列确定的事实，例如模型玻璃、弯曲的菲涅尔基底、物方 NA 下未设远心；是否可以接受由用户判断。
Everything is read from the open lens (backend.describe_model). The agent's NOTES.json says what each surface
physically is, the assumptions and the first-order analysis. The automatic warnings state facts about how
OpticStudio will treat the model (model glass, a curved Fresnel substrate, object-space NA without telecentric
object space, …); whether they are acceptable is the user's call when approving (saos.model_gate).
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any

import yaml

from saos import demand, i18n, jobs, materials_record, model_gate
from saos.prescription import FACE_IS
from saos.protocol import ErrorCode, SaosError, jsonable
from saos.report import CSS, e, fmt, header_row

TEXT: dict[str, tuple[str, str]] = {
    "title": ("建模说明", "Model description"),
    "byline": ("任务 {job} · 镜头 {lens} · 生成于 {time}", "Job {job} · lens {lens} · generated {time}"),
    "intro": (
        "当前模型与需求值由打开镜头的计算后端（Optiland 或 OpticStudio）读取；候选预判和初筛值来自注明版本的存档。开始优化前请核对：每个面代表什么实物、用的什么材料、按什么光阑、视场和波长计算。",
        "Current model and requirement values come from the loaded backend; candidate predictions and screening values come from the identified archives. Before optimisation starts, check what each surface stands for, "
        "which material it uses, and which aperture, fields and wavelengths the evaluation uses.",
    ),
    "h.warnings": ("需要注意", "Check these"),
    "no_warnings": ("自动检查没有发现问题。", "The automatic checks found nothing."),
    "h.system": ("系统设置", "System settings"),
    "h.fields": ("视场", "Fields"),
    "h.waves": ("波长", "Wavelengths"),
    "h.materials": ("材料", "Materials"),
    "h.surfaces": ("逐面说明", "Surfaces"),
    "h.params": ("面型参数（非零的附加列）", "Surface parameters (non-zero extra columns)"),
    "h.layout": ("布局图", "Layout"),
    "h.assumptions": ("建模假设", "Modelling assumptions"),
    "h.first_order": ("一阶分析", "First-order analysis"),
    "none": ("无", "none"),
    "units": ("长度单位", "Lens units"),
    "aperture": ("系统孔径", "System aperture"),
    "telecentric": ("物方远心", "Telecentric object space"),
    "afocal": ("像方无焦", "Afocal image space"),
    "aiming": ("光线瞄准", "Ray aiming"),
    "aiming_options": ("光线瞄准选项", "Ray aiming options"),
    "afocal_units": ("无焦单位", "Afocal units"),
    "environment": ("环境", "Environment"),
    "env_value": ("{t} °C，{p} atm，按环境修正折射率：{adj}", "{t} °C, {p} atm, index adjusted to environment: {adj}"),
    "apodization": ("切趾", "Apodization"),
    "polarization": ("偏振", "Polarization"),
    "unpolarized": ("非偏振", "unpolarized"),
    "coating_file": ("膜层文件", "Coating file"),
    "h.configs": ("多重结构", "Multiple configurations"),
    "configs_note": ("共 {n} 个结构，当前为结构 {cur}。交叠检查逐个结构做；TEMP、PRES 的值和各行操作数属于确认的模型，其余值是设计变量。",
                     "{n} configurations, configuration {cur} is current. The interference check covers each configuration; TEMP and PRES values and the operand rows belong to the approved model, the other values are design variables."),
    "th.configs": ("行|操作数|参数|{cols}", "Row|Operand|Parameters|{cols}"),
    "stop": ("光阑面", "Stop surface"),
    "catalogs": ("使用的材料目录", "Material catalogs in use"),
    "yes": ("是", "yes"),
    "no": ("否", "no"),
    "field_type": ("视场类型：{type}；归一化：{norm}", "Field type: {type}; normalization: {norm}"),
    "th.fields": ("#|X|Y|权重|渐晕因子 VDX/VDY/VCX/VCY/VAN", "#|X|Y|Weight|Vignetting VDX/VDY/VCX/VCY/VAN"),
    "th.waves": ("#|波长 µm|权重|主波长", "#|Wavelength µm|Weight|Primary"),
    "th.materials": ("材料|来源|参数|{waves}|所在面", "Material|Source|Parameters|{waves}|Surfaces"),
    "th.surfaces": ("#|名称与说明|面型|半径|圆锥|厚度|之后的介质|孔径|求解|顶点全局位置 (x, y, z)",
                    "#|Name and description|Type|Radius|Conic|Thickness|Medium after|Aperture|Solves|Vertex, global (x, y, z)"),
    "src.model": ("模型玻璃（不是真实材料）", "Model glass (not a real material)"),
    "src.catalog": ("目录 {cats}", "catalog {cats}"),
    "src.status": ("，{status}", ", {status}"),
    "src.uncatalogued": ("在用目录里找不到", "not found in the catalogs in use"),
    "src.none": ("空气/真空（OpticStudio 环境）", "air/vacuum (OpticStudio environment)"),
    "src.mirror": ("反射面", "mirror"),
    "src.inherit": ("坐标断点：沿用前一介质", "coordinate break: keeps the previous medium"),
    "n_note": ("折射率是该面之后介质在各波长的值，用 INDX 操作数实测。", "Indices are of the medium after the surface, measured with the INDX operand."),
    "thick_note": ("折转（反射）之后厚度为负，表示沿局部 −z 前进。", "After a fold (mirror) thicknesses are negative: light travels along local −z."),
    "stop_mark": ("光阑", "stop"),
    "layout_missing": ("没有生成：{err}", "not generated: {err}"),
    "w.model_material": (
        "第 {s} 面用的是模型玻璃（nd {nd}，Vd {vd}），不对应任何真实材料。交付前应换成有来源的牌号（OpticStudio 目录或项目材料库 library/materials/）。",
        "Surface {s} uses a model glass (nd {nd}, Vd {vd}) that is no real material. Replace it with a sourced material (an OpticStudio catalog or library/materials/) before delivery.",
    ),
    "w.env_model_material": (
        "第 {s} 面是环境或外购件（不由本设计制造），用的是模型介质（nd {nd}，Vd {vd}）。它不阻止交付，但须在 sources/materials.yaml 写明数据来源，并覆盖各工作波长的色散。",
        "Surface {s} is environment or a bought-in part (not made by this design) and uses a model medium (nd {nd}, Vd {vd}). It does not block delivery, "
        "but record its data source in sources/materials.yaml, covering the dispersion at every working wavelength.",
    ),
    "w.curved_fresnel": (
        "第 {s} 面是 {type}，但基底半径 {r}、圆锥 {k} 不为平面。这类面的半径和圆锥定义的是实际基底形状（光线在弯曲基底上求交），多项式只改变局部折射斜率；若想要平板，应把半径设为无穷、圆锥设为 0，并按实际矢高检查厚度和相交。",
        "Surface {s} is {type} with base radius {r} and conic {k}, i.e. not flat. Radius and conic define the physical substrate (rays intersect the curved base); "
        "the polynomial only changes the local refraction slope. For a flat plate set radius to infinity and conic to 0, and check thickness and intersection with the real sag.",
    ),
    "w.non_telecentric": (
        "孔径按物方 {type} 给出，有离轴视场，但没有勾选物方远心：离轴物点的光锥以指向傍轴入瞳的主光线为中心。若光源沿自身轴线发光（光纤、LED 阵列），应勾选物方远心。",
        "The aperture is object-space {type} with off-axis fields but telecentric object space is off: off-axis cones are centred on chief rays aimed at the paraxial entrance pupil. "
        "If each source point emits along its own axis (fibre, LED array), turn telecentric object space on.",
    ),
    "w.no_dispersion": (
        "第 {s} 面之后的介质在各波长折射率相同（{n}），没有色散；只有确实无色散的介质才应这样建模。",
        "The medium after surface {s} has the same index at every wavelength ({n}); only a truly non-dispersive medium should be modelled this way.",
    ),
    "w.crossing": (
        "{cfg}第 {a} 面（{ca}）和第 {b} 面（{cb}）在口径内相交，最深约 {depth}：镜片边缘厚度为负，或两个零件互相穿过。",
        "{cfg}Surfaces {a} ({ca}) and {b} ({cb}) cross inside their apertures, by up to about {depth}: a negative edge thickness, or two parts passing through each other.",
    ),
    "w.overlap": (
        "{cfg}镜片 {l1}（{c1}）和镜片 {l2}（{c2}）相互穿插：一片的表面落在另一片的玻璃里（{n} 个采样点），多半是空气间隔为负。",
        "{cfg}Lenses {l1} ({c1}) and {l2} ({c2}) interpenetrate: {n} sample points of one lie inside the other's glass, usually a negative air gap.",
    ),
    "w.edges_from_clear": (
        "没有一片镜片的外缘大于净口径：交叠检查和边厚只按净口径算，没算装配余量。按表 17-1（李晓彤）给各镜片面设机械半口径"
        "（lens set --param mech_semi_diameter），检查才对应实物零件；优化后才加余量，正透镜可能没有边厚。",
        "No lens has an outer edge beyond its clear aperture: the interference check and edge thicknesses cover clear apertures only, "
        "without a mounting allowance. Set the lens faces' mechanical semi-diameters (lens set --param mech_semi_diameter) per table 17-1 "
        "of 李晓彤's textbook so the check sees the real parts; an allowance added after optimising can leave a positive lens without edge.",
    ),
    "w.thin_edge": (
        "第 {a}–{b} 面之间的玻璃在外缘附近最薄 {min}（离轴 {r} 处，外径 {d}），低于表 17-2（李晓彤）正透镜最小边厚 {need}。",
        "The glass between surfaces {a} and {b} is thinnest near its edge, {min} ({r} from the axis, outer diameter {d}), below the "
        "minimum positive-lens edge {need} of table 17-2 in 李晓彤's textbook.",
    ),
    "w.thin_centre": (
        "第 {a}–{b} 面之间的玻璃在中心附近最薄 {min}（离轴 {r} 处，外径 {d}），低于表 17-2（李晓彤）负透镜最小中心厚 {need}。",
        "The glass between surfaces {a} and {b} is thinnest near its centre, {min} ({r} from the axis, outer diameter {d}), below the "
        "minimum negative-lens centre {need} of table 17-2 in 李晓彤's textbook.",
    ),
    "w.no_exploration": (
        "没有结构探索记录；请在说明中写明起点依据或直接采用该结构的理由。",
        "No screening record; explain the source of this start or why this form was used directly.",
    ),
    "w.uncatalogued": ("第 {s} 面的材料 {name} 不在在用的材料目录里。", "Surface {s}: material {name} is not in the catalogs in use."),
    "w.obsolete_material": ("第 {s} 面的玻璃 {name} 在目录里标为 Obsolete（停产），通常买不到；交付前换成在产牌号或向厂家确认。",
                            "Surface {s}: glass {name} is marked Obsolete in its catalog and is usually unavailable; replace it with a glass in production or confirm with the maker before delivery."),
    "w.vignetting": ("第 {f} 个视场设置了渐晕因子，会改变该视场的光瞳形状和大小。", "Field {f} has vignetting factors, which change its pupil shape and size."),
    "w.no_first_order": (
        "没有一阶分析：开始优化前应写明倍率、光阑与光瞳位置、各孔径处的光束口径和渐晕预算、每个面需要的最大偏折角（写在 --notes 的 first_order 里）。",
        "No first-order analysis: before optimising, state the magnification, stop and pupil positions, beam footprints and vignetting budget at each aperture, "
        "and the largest bending each surface must do (add them under first_order in --notes).",
    ),
    "w.no_material_record": (
        "镜头用了真实材料，但任务里没有材料来源记录 sources/materials.yaml（格式见 saos.materials_record）。",
        "The lens uses real materials but the job has no material source record sources/materials.yaml (format: saos.materials_record).",
    ),
    "w.unrecorded_material": (
        "这些材料没有写进 sources/materials.yaml：{list}。",
        "These materials are missing from sources/materials.yaml: {list}.",
    ),
    "w.undescribed": ("这些面没有说明它代表什么实物：{list}（写在 --notes 的 surfaces 里）。", "These surfaces have no description of what they stand for: {list} (add them under surfaces in --notes)."),
    # 非序列系统 / non-sequential systems
    "h.nsc": ("非序列选项", "Non-sequential options"),
    "h.objects": ("物体", "Objects"),
    "h.object_params": ("物体参数与体散射", "Object parameters and volume physics"),
    "th.objects": ("#|名称与说明|类型|参考物体|Inside Of|位置 (x, y, z)|倾斜 (x, y, z)|材料|体散射",
                   "#|Name and description|Type|Ref object|Inside Of|Position (x, y, z)|Tilt (x, y, z)|Material|Volume physics"),
    "th.obj_materials": ("材料|来源|参数|{waves}|所在物体", "Material|Source|Parameters|{waves}|Objects"),
    "obj_n_note": ("折射率用 NCE.GetIndex 实测。", "Indices are measured with NCE.GetIndex."),
    "h.faces": ("面属性与散射去向", "Face properties and scatter targets"),
    "th.faces": ("#|面|反射/吸收|膜层|面散射", "#|Face|Reflective/absorbing|Coating|Surface scatter"),
    "faces_default": ("所有物体的面都是默认设置（随物体材料，无膜层，不散射），也没有指定散射去向。",
                      "Every face is at its default (follows the object's material, no coating, no scatter) and no scatter target is set."),
    "face_is.default": ("随材料", "object default"), "face_is.reflective": ("反射", "reflective"), "face_is.absorbing": ("吸收", "absorbing"),
    "scatter_to.objects": ("物体 {n} 的散射光只追向物体 {list}", "Object {n} scatters only towards objects {list}"),
    "scatter_to.importance": ("物体 {n} 的散射用重要性采样：{rows}", "Object {n} uses importance sampling: {rows}"),
    "file": ("文件", "file"),
    "h.tabs": ("光源、Type、衍射、折射率设置", "Source, Type, Diffraction and Index settings"),
    "tabs_default": ("没有物体改过光源页、Type 页、衍射页或折射率页的默认设置（光源只列出了它们的光源页）。",
                     "No object changes the Source, Type, Diffraction or Index tab from its default."),
    "tab.source": ("光源", "source"), "tab.type": ("Type", "type"), "tab.diffraction": ("衍射", "diffraction"), "tab.index_model": ("折射率", "index"),
    "h.nsc_group": ("非序列组（面 {s}）", "Non-sequential group (surface {s})"),
    "w.detector_spill": (
        "探测器 {d}（{dc}）伸出了它所在的物体 {v}（{vc}）之外（如点 {p}），又不在吸收体里：从物体 {v} 别处漏出的光会被记成到达探测器。"
        "把探测器缩到物体内，或在外面加 ABSORB 物体（杂散荧光任务实测：方形探测器四角伸出圆柱组织，多记 1.8 个百分点）",
        "Detector {d} ({dc}) reaches outside object {v} ({vc}) that holds its centre (e.g. at {p}) and not into an absorber: light leaking "
        "out of object {v} elsewhere is recorded as reaching the detector. Shrink the detector into the object or surround it with an ABSORB object"),
    "w.detectors_unchecked": ("探测器 {list} 所在的体算不出包含关系，没有检查它们是否伸出所在的物体",
                              "For detectors {list} the containing volume could not be worked out; whether they reach outside it was not checked"),
    "src.absorb": ("吸收体（ABSORB）", "absorber (ABSORB)"),
    "w.obj_model_material": (
        "第 {s} 个物体用的是模型玻璃（nd {nd}，Vd {vd}），不对应任何真实材料。交付前应换成有来源的材料。",
        "Object {s} uses a model glass (nd {nd}, Vd {vd}) that is no real material. Replace it with a sourced material before delivery.",
    ),
    "w.obj_env_model_material": (
        "第 {s} 个物体是环境（不由本设计制造），用的是模型介质（nd {nd}，Vd {vd}）。它不阻止交付，但须在 sources/materials.yaml 写明数据来源，并说明各工作波长的色散。",
        "Object {s} is environment (not made by this design) and uses a model medium (nd {nd}, Vd {vd}). It does not block delivery, "
        "but record its data source in sources/materials.yaml, covering the dispersion at every working wavelength.",
    ),
    "w.obj_uncatalogued": ("第 {s} 个物体的材料 {name} 不在在用的材料目录里。", "Object {s}: material {name} is not in the catalogs in use."),
    "w.nsc_overlap": (
        "第 {a} 个物体（{ca}）和第 {b} 个物体（{cb}）部分重叠，深约 {depth}，又没有嵌套关系（编号大的整个在编号小的里面）：OpticStudio 追迹时会出错。检查按轮廓的包围盒做，倾斜或弯曲的物体可能多报。",
        "Objects {a} ({ca}) and {b} ({cb}) overlap by about {depth} without nesting (the later one wholly inside the earlier one): OpticStudio's trace will report errors. "
        "The check uses the outlines' bounding boxes and may over-report tilted or curved objects.",
    ),
    "w.nsc_unchecked": ("这些物体的类型画不出轮廓，交叠检查没有覆盖：{list}。", "These objects have no outline, so the interference check does not cover them: {list}."),
    "w.no_source": ("系统里没有光源。", "The system has no source."),
    "w.no_detector": ("系统里没有探测器，判定要读的量需要另外安排（探测器或光线数据库）。", "The system has no detector; the checks need another way to read results (a detector or the ray database)."),
    "w.obj_undescribed": ("这些物体没有说明它代表什么实物：{list}（写在 --notes 的 objects 里）。", "These objects have no description of what they stand for: {list} (add them under objects in --notes)."),
    # 确认页的四节 / the four sections before approval
    "sel.matched": ("当前结构匹配候选：{ids}", "Current structure matches: {ids}"),
    "sel.secondary": ("二手转录，原书页尚未核验", "Secondary transcription; original book pages not checked"),
    "sel.extrapolation": ("超出来源示例，需外推试算", "Outside the source example; exploratory extrapolation"),
    "sel.unverified": ("起点来源已登记，数值验收未完成", "Source recorded; numerical validation pending"),
    "sel.versions": ("需求版本 / 判定版本", "Demand / checks version"),
    "metric.efl_mm": ("焦距 mm", "EFL mm"), "metric.half_field_deg": ("半视场 °", "Half field °"),
    "metric.f_number": ("f/#", "f/#"), "metric.wavelengths_um": ("波长 µm", "Wavelength µm"),
    "sel.tightest": ("最吃紧指标", "Tightest constraint"),
    "pred.first": ("一阶量（长度沿处方单位）", "First order (prescription length units)"),
    "pred.quantity": ("量", "Quantity"), "pred.value": ("值", "Value"),
    "pred.track": ("总光路长", "Total track"), "pred.stop": ("光阑面", "Stop surface"),
    "pred.stop_z": ("光阑轴向位置", "Stop axial position"), "pred.ep": ("入瞳（相对首面）", "Entrance pupil (from first surface)"),
    "pred.xp": ("出瞳（相对像面）", "Exit pupil (from image)"), "pred.epd": ("入瞳直径", "Entrance pupil diameter"),
    "pred.third": ("三阶像差与一阶色差", "Third order and first order colour"),
    "pred.petzval": ("Petzval 半径 / 焦距", "Petzval radius / EFL"),
    "pred.surface": ("面", "Surface"), "pred.total": ("合计", "Total"),
    "pred.incidence": ("各面采样最大入射角", "Sampled maximum incidence per surface"),
    "pred.samples": ("有效样本", "Valid samples"), "pred.invalid": ("失效光线 / 尝试数", "Invalid rays / attempted"),
    "pred.scope": ("预判使用初筛镜头快照；三阶量为 Optiland 原生口径，尚未与 OpticStudio SEID 对照。入射角为采样最大值。", "Prediction uses the screening snapshot and native Optiland conventions; OpticStudio SEID comparison is not run. Incidence maxima are sampled."),
    "sel.constraints": ("需求选型指标", "Selection constraints"),
    "sel.prediction": ("像差预判", "Aberration prediction"),
    "sel.no_prediction": ("未记录预判", "No prediction recorded"),
    "sel.columns": ("候选|形式与说明|来源|初筛结论|原因|预判", "Candidate|Form and notes|Source|Screening verdict|Reason|Prediction"),
    "verdict.not_screened": ("已生成，未初筛", "Generated, not screened"),
    "verdict.invalid": ("预算内无效", "invalid within budget"),
    "w.prescribed_changed": ("当前结构与最初认可的指定结构存在差异：{changed}", "The current structure differs from the first approved prescribed model: {changed}"),
    "w.no_screening": ("没有初筛记录；可确认模型，但选型依据尚缺。", "No screening record; approval is allowed, but comparison evidence is missing."),
    "w.approval_evidence": ("历史模型确认不可用或未封存：{paths}", "Historical model approvals unavailable or unsealed: {paths}"),
    "w.checks_changed": ("判定方法已变，已有结果使用旧判定。", "Check methods changed; recorded results use earlier checks."),
    "w.comparison_outdated": ("比较使用旧需求版本。", "The comparison uses an earlier demand version."),
    "w.comparison_changed": ("比较文件未封存或已变动。", "Comparison files are unsealed or changed."),
    "w.comparison_unmatched": ("当前模型未匹配初筛候选。", "The current model does not match a screened candidate."),
    "h.selection": ("选型依据", "Structure choice"),
    "h.requirements": ("需求逐项", "Requirements, item by item"),
    "h.validity": ("计算有效性（系统核对）", "Computation validity (checked by the system)"),
    "h.budget": ("下一阶段预算", "Budget for the next stage"),
    "sel.prescribed": ("结构由用户指定，没有经过比较选型。理由：{reason}", "The user fixed the structure, so no comparison chose it. Reason: {reason}"),
    "sel.no_demand": ("没有需求单，无法判断结构是否由用户指定。", "There is no demand file, so it cannot be told whether the user fixed the structure."),
    "sel.none": ("当前模型未匹配初筛候选，见上方提示。", "No screened candidate matches the current structure; see the notes above."),
    "sel.hypothesis": ("假设", "Hypothesis"),
    "sel.reason": ("选择理由", "Reason for the choice"),
    "sel.screening": ("仅初筛", "screening only"),
    "sel.optimised": ("优化后仍失败", "still failing after optimisation"),
    "sel.link": ("比较页", "comparison page"),
    "sel.others": ("同一比较中的其他候选", "Other candidates in the same comparison"),
    "th.others": ("候选|结论|原因", "Candidate|Verdict|Reason"),
    "verdict.valid": ("有效", "valid"),
    "verdict.material": ("未评估：材料", "not evaluated: material"),
    "verdict.library": ("未评估：库数据", "not evaluated: library data"),
    "verdict.configuration": ("未评估：配置", "not evaluated: configuration"),
    "verdict.computation": ("未评估：计算", "not evaluated: computation"),
    "verdict.unknown": ("未知", "unknown"),
    "th.requirements": ("条目|类型|当前值|界限|状态|依据来源", "Item|Type|Current value|Bound|Status|Source"),
    "type.constants": ("硬条件（常量）", "hard limit (constant)"),
    "type.variables": ("硬条件（变量范围）", "hard limit (variable range)"),
    "type.objectives": ("目标（{origin}）", "objective ({origin})"),
    "origin.user": ("用户", "user"),
    "origin.agent": ("agent 补充", "added by the agent"),
    "bound.min": ("≥ {v}", ">= {v}"),
    "bound.max": ("≤ {v}", "<= {v}"),
    "bound.equals": ("= {v} ± {tol}", "= {v} ± {tol}"),
    "bound.fixed": ("固定为 {v}", "fixed at {v}"),
    "bound.dir.min": ("越小越好，理想 {v}", "smaller is better, ideal {v}"),
    "bound.dir.max": ("越大越好，理想 {v}", "larger is better, ideal {v}"),
    "bound.none": ("无界限", "no bound"),
    "sep": ("；", "; "),
    "status.met": ("满足", "met"),
    "status.unmet": ("未满足", "not met"),
    "status.unknown": ("未知", "unknown"),
    "status.pursuit": ("无达标线，只比较", "no pass line, compared only"),
    "status.no_bound": ("无界限", "no bound"),
    "reason.no_check": ("没有判定方法", "no check defined"),
    "reason.not_measured": ("没有测出：{err}", "not measured: {err}"),
    "reason.check_error": ("判定出错：{err}", "check failed: {err}"),
    "reason.no_valid_value": ("没有有效值", "no valid value"),
    "src.assumed": ("agent 假设", "agent assumption"),
    "req_backend": ("当前值由后端 {backend} {version} 在当前镜头上用需求单的判定测得，没有存证据文件。",
                    "Current values were measured on the current lens by backend {backend} {version} with the demand's checks; no evidence files were kept."),
    "req_error": ("没有完成测量：{err}", "measurement did not complete: {err}"),
    "req_none": ("需求单里没有条目。", "The demand has no items."),
    "val.intro": ("本节由系统根据记录和测量核对。用户确认不担保这些内容，确认只记录用户看过它们。",
                  "The system checks this section from the records and measurements. Confirming does not vouch for it; it only records that the user saw it."),
    "val.numeric": ("所选候选的数值警告", "Numeric warnings of the chosen candidate"),
    "val.numeric_scope": ("检查脚本可通过 Python 警告过滤器屏蔽数值警告；零条警告不证明计算有效。",
                          "Check scripts can suppress numeric warnings through Python warning filters; a zero count does not establish validity."),
    "val.numeric_na": ("不适用：结构由用户指定，没有候选比较", "not applicable: the structure was fixed by the user, with no candidate comparison"),
    "val.numeric_count": ("{n} 次，第一条：{first}", "{n} time(s), first: {first}"),
    "val.unknown": ("无有效值或判定出错的条目", "Items with no valid value or a failed check"),
    "val.materials": ("材料，逐级核对", "Materials, level by level"),
    "th.levels": ("材料|所在|资料找到|数据完整|后端支持|实际加载", "Material|Where|Source found|Data complete|Backend supports|Actually loaded"),
    "state.yes": ("是", "yes"),
    "state.no": ("否", "no"),
    "state.unknown": ("未知", "unknown"),
    "state.na": ("不适用", "n/a"),
    "note.no_record": ("没有 sources/materials.yaml", "no sources/materials.yaml"),
    "note.record_unreadable": ("材料记录读不出：{err}", "material record unreadable: {err}"),
    "note.not_in_record": ("记录里没有这个牌号", "this glass is not in the record"),
    "note.in_record": ("{id}，来源：{source}", "{id}, source: {source}"),
    "note.src_library": ("材料库 {id}", "library {id}"),
    "note.src_sources": ("记录的来源 {ids}", "recorded sources {ids}"),
    "note.src_vendor": ("厂家目录 {catalog}", "vendor catalog {catalog}"),
    "note.no_dispersion": ("记录里没有色散数据，覆盖范围无法核对", "the record has no dispersion data, so coverage cannot be checked"),
    "note.no_range": ("色散数据没有给出有效范围", "the dispersion data give no valid range"),
    "note.covered": ("有效范围 {lo}–{hi} nm，覆盖全部工作波长", "valid range {lo}–{hi} nm covers every working wavelength"),
    "note.not_covered": ("有效范围 {lo}–{hi} nm，不覆盖 {missing} nm", "valid range {lo}–{hi} nm does not cover {missing} nm"),
    "note.not_found": ("没有资料，无从核对", "no source, nothing to check"),
    "note.catalog_in_use": ("OpticStudio 在用目录 {cats}", "OpticStudio catalogs in use: {cats}"),
    "note.not_in_use": ("在用的目录里找不到", "not in the catalogs in use"),
    "note.local_agf": ("本机 AGF 目录中有此牌号", "found in the local AGF catalogs"),
    "note.local_library": ("材料库 library/materials 中有此牌号", "found in the material library library/materials"),
    "note.not_local": ("本机没有对应的 AGF 或材料库条目，Optiland 无法加载", "no matching local AGF or library entry; Optiland cannot load it"),
    "note.backend_unknown": ("没有后端信息", "no backend information"),
    "note.model": ("模型玻璃，不对应真实材料", "model glass, not a real material"),
    "note.measured": ("INDX 实测 {n} 个波长", "INDX measured at {n} wavelengths"),
    "note.not_measured": ("没有实测折射率", "no measured index"),
    "note.non_finite": ("有的波长折射率无效", "an index is not finite at some wavelength"),
    "budget.none": ("notes 里没有 next_budget，见上方警告。", "notes has no next_budget; see the warning above."),
    "budget.line": ("最多 {s} 秒的计算", "at most {s} s of computation"),
    "budget.plan": ("准备：{plan}", "Plan: {plan}"),
    "w.no_next_budget": ("notes 里没有 next_budget：下一阶段的计算预算和计划没有写明，确认页上无法核对。",
                         "notes has no next_budget: the next stage's computation budget and plan are not written, so the page cannot show them."),
}


class Tr:
    def __init__(self, lang: str) -> None:
        self.lang = lang

    def __call__(self, key: str, **kw: Any) -> str:
        return i18n.text(self.lang, *TEXT[key]).format(**kw)


OBJECT_APERTURES = ("ObjectSpaceNA", "ObjectConeAngle")


def _flat(radius: float) -> bool:
    return math.isinf(radius) or abs(radius) > 1e9


# 表 17-2（李晓彤《几何光学·像差·光学设计》）的下限：(外径上限 mm, 正透镜最小边厚, 负透镜最小中心厚)；外径 3 mm 以下表里没有
# lower bounds of table 17-2 in that textbook: (outer diameter up to, mm; min edge of a positive lens; min centre of a negative lens)
MIN_GLASS = ((6, 0.4, 0.6), (10, 0.6, 0.8), (18, 0.8, 1.0), (30, 1.2, 1.5), (50, 1.8, 2.2), (80, 2.4, 3.5), (120, 3.0, 5.0), (math.inf, 4.0, 8.0))


def _min_glass(c: dict[str, Any]) -> tuple[float, str] | None:
    """一片玻璃最薄处的下限（毫米）：最薄处靠外缘按正透镜边厚，靠中心按负透镜中心厚。
    The lower bound for a glass's thinnest point (mm): near the edge the positive-lens edge, near the centre the negative-lens centre."""
    diameter = 2 * float(c["outer"])
    if diameter < 3:
        return None
    _, edge, centre = next(row for row in MIN_GLASS if diameter <= row[0])
    at_edge = float(c["radius"]) >= 0.5 * float(c["outer"])
    return (edge, "edge") if at_edge else (centre, "centre")


def environment_surfaces(notes: dict[str, Any] | None) -> list[int]:
    """notes 里声明为环境或外购件的面（样品、浸没介质、买来的管子等），只有要制造的零件受模型玻璃的交付限制。
    Surfaces the notes declare as environment or bought-in parts (sample, immersion medium, a bought tube);
    only the parts this design makes are bound by the model-glass delivery rule."""
    return [int(i) for i in (notes or {}).get("environment") or []]


def checks(desc: dict[str, Any], notes: dict[str, Any] | None = None, recorded: set[str] | None | bool = False) -> list[tuple[str, dict[str, Any]]]:
    """确定性的建模检查，返回 (代码, 参数)。recorded：材料来源记录里的牌号；None 表示没有记录文件，False 表示不检查。
    Deterministic model checks, returning (code, arguments). recorded: names in the material source record;
    None means there is no record file, False skips the check."""
    if desc.get("mode") == "non_sequential":
        return checks_nsc(desc, notes, recorded)
    out: list[tuple[str, dict[str, Any]]] = []
    for s in desc["surfaces"]:
        # 混合模式里非序列组的物体：模型玻璃和目录外材料同样要说明 / objects of a mixed-mode group: model glasses and uncatalogued materials
        for o in s.get("objects") or []:
            src = o.get("material_source") or {}
            if o["kind"] == "object" and src.get("kind") == "model":
                out.append(("obj_model_material", {"s": f"{s['index']}:{o['index']}", "nd": fmt(src.get("nd")), "vd": fmt(src.get("vd"))}))
            if o["kind"] == "object" and src.get("kind") == "catalog" and not src.get("catalogs"):
                out.append(("obj_uncatalogued", {"s": f"{s['index']}:{o['index']}", "name": src.get("name")}))
    if recorded is not False:
        used = sorted({str((s.get("material_source") or {}).get("name", "")).upper() for s in desc["surfaces"]
                       if (s.get("material_source") or {}).get("kind") == "catalog"})
        if used and recorded is None:
            out.append(("no_material_record", {}))
        elif used:
            missing = [n for n in used if n not in recorded]
            if missing:
                out.append(("unrecorded_material", {"list": ", ".join(missing)}))
    surfaces = desc["surfaces"]
    last = len(surfaces) - 1
    environment = set(environment_surfaces(notes))
    for s in surfaces:
        src = s.get("material_source") or {}
        if src.get("kind") == "model":
            code = "env_model_material" if s["index"] in environment else "model_material"
            out.append((code, {"s": s["index"], "nd": fmt(src.get("nd")), "vd": fmt(src.get("vd"))}))
        if src.get("kind") == "catalog" and not src.get("catalogs"):
            out.append(("uncatalogued", {"s": s["index"], "name": src.get("name")}))
        if src.get("kind") == "catalog" and src.get("status") == "Obsolete" and s["index"] not in environment:
            out.append(("obsolete_material", {"s": s["index"], "name": src.get("name")}))
        if "Fresnel" in str(s.get("type")) and (not _flat(float(s["radius"])) or float(s.get("conic", 0.0)) != 0.0):
            out.append(("curved_fresnel", {"s": s["index"], "type": s["type"], "r": fmt(float(s["radius"])), "k": fmt(float(s.get("conic", 0.0)))}))
        n = s.get("n") or []
        if s["index"] < last and len(n) >= 2 and src.get("kind") in ("model", "catalog") and max(n) - min(n) < 1e-5:
            out.append(("no_dispersion", {"s": s["index"], "n": fmt(n[0])}))
    ap = desc["aperture"]
    items = desc["fields"]["items"]
    if ap["type"] in OBJECT_APERTURES and not ap.get("telecentric_object") and any(f["x"] or f["y"] for f in items):
        out.append(("non_telecentric", {"type": ap["type"]}))
    for k, f in enumerate(items, 1):
        if any(abs(v) > 0 for v in (f.get("vignetting") or {}).values()):
            out.append(("vignetting", {"f": k}))
    clash = desc.get("interference") or {}
    # 多重结构时标出结构号；警告文字给 agent 读，统一用英文写结构号 / the configuration, when there are several
    cfg = lambda x: f"[configuration {x['configuration']}] " if "configuration" in x else ""  # noqa: E731
    for c in clash.get("crossings") or []:
        out.append(("crossing", {"a": c["surfaces"][0], "b": c["surfaces"][1], "ca": c["comments"][0] or "-", "cb": c["comments"][1] or "-",
                                 "depth": fmt(c["depth"]), "cfg": cfg(c)}))
    for o in clash.get("overlaps") or []:
        out.append(("overlap", {"l1": "–".join(map(str, o["lenses"][0])), "l2": "–".join(map(str, o["lenses"][1])),
                                "c1": o["comments"][0] or "-", "c2": o["comments"][1] or "-", "n": o["points"], "cfg": cfg(o)}))
    if clash.get("edges_from_clear"):
        out.append(("edges_from_clear", {}))
    if str(desc.get("lens_units", "")).lower().startswith("millimet"):
        for c in clash.get("clearances") or []:
            need = _min_glass(c)
            if c.get("glass") and need is not None and c["min"] < need[0]:
                out.append((f"thin_{need[1]}", {"a": c["surfaces"][0], "b": c["surfaces"][1], "min": fmt(c["min"]), "r": fmt(c["radius"]),
                                                 "d": fmt(2 * c["outer"]), "need": fmt(need[0])}))
    # LDE 的注释只是名字；说明要写清这个面代表什么实物，所以每个面都要在 notes 里有一条
    # an LDE comment is only a name; the notes must say what each surface physically is
    missing = [str(s["index"]) for s in surfaces if not str(((notes or {}).get("surfaces") or {}).get(str(s["index"]), "")).strip()]
    if missing:
        out.append(("undescribed", {"list": ", ".join(missing)}))
    if not (notes or {}).get("first_order"):
        out.append(("no_first_order", {}))
    return out


def checks_nsc(desc: dict[str, Any], notes: dict[str, Any] | None = None, recorded: set[str] | None | bool = False) -> list[tuple[str, dict[str, Any]]]:
    """非序列系统的建模检查：材料来源记录、模型玻璃、目录外材料、物体交叠、有没有光源和探测器、物体说明、一阶分析。
    Model checks for a non-sequential system: material records, model glasses, uncatalogued materials, object overlaps,
    whether a source and a detector exist, object descriptions and the first-order analysis."""
    out: list[tuple[str, dict[str, Any]]] = []
    objects = desc["objects"]
    parts = [o for o in objects if o["kind"] == "object"]
    if recorded is not False:
        used = sorted({str((o.get("material_source") or {}).get("name", "")).upper() for o in parts
                       if (o.get("material_source") or {}).get("kind") == "catalog"})
        if used and recorded is None:
            out.append(("no_material_record", {}))
        elif used:
            missing = [n for n in used if n not in recorded]
            if missing:
                out.append(("unrecorded_material", {"list": ", ".join(missing)}))
    environment = set(environment_surfaces(notes))
    for o in parts:
        src = o.get("material_source") or {}
        if src.get("kind") == "model":
            code = "obj_env_model_material" if o["index"] in environment else "obj_model_material"
            out.append((code, {"s": o["index"], "nd": fmt(src.get("nd")), "vd": fmt(src.get("vd"))}))
        if src.get("kind") == "catalog" and not src.get("catalogs"):
            out.append(("obj_uncatalogued", {"s": o["index"], "name": src.get("name")}))
        if src.get("kind") == "catalog" and src.get("status") == "Obsolete" and o["index"] not in environment:
            out.append(("obsolete_material", {"s": o["index"], "name": src.get("name")}))
    clash = desc.get("interference") or {}
    for c in clash.get("overlaps") or []:
        out.append(("nsc_overlap", {"a": c["objects"][0], "b": c["objects"][1], "ca": c["comments"][0] or "-", "cb": c["comments"][1] or "-",
                                    "depth": fmt(c["depth"])}))
    if clash.get("unchecked"):
        out.append(("nsc_unchecked", {"list": ", ".join(map(str, clash["unchecked"]))}))
    for s in clash.get("detector_spill") or []:
        out.append(("detector_spill", {"d": s["detector"], "dc": s["comment"] or "-", "v": s["volume"], "vc": s["volume_comment"] or "-",
                                       "p": ", ".join(fmt(c) for c in s["point"])}))
    if clash.get("detectors_unchecked"):
        out.append(("detectors_unchecked", {"list": ", ".join(map(str, clash["detectors_unchecked"]))}))
    if not any(o["kind"] == "source" for o in objects):
        out.append(("no_source", {}))
    if not any(o["kind"] == "detector" for o in objects):
        out.append(("no_detector", {}))
    described = {str(k): v for k, v in ((notes or {}).get("objects") or {}).items()}
    missing = [str(o["index"]) for o in objects if not str(described.get(str(o["index"]), "")).strip()]
    if missing:
        out.append(("obj_undescribed", {"list": ", ".join(missing)}))
    if not (notes or {}).get("first_order"):
        out.append(("no_first_order", {}))
    return out


def _vp_text(vp: dict[str, Any] | None) -> str:
    if not vp or vp.get("model") in (None, "None"):
        return "—"
    if vp["model"] == "photoluminescence":
        keys = ("absorption", "emission", "excitation", "quantum_yield", "efficiency", "pl_density", "extinction_coefficient",
                "extinction_wavelength", "consider_mie", "particle_index", "particle_radius", "particle_density")
        return "photoluminescence; " + "; ".join(f"{k} {fmt(vp[k]) if isinstance(vp[k], float) else vp[k]}" for k in keys if vp.get(k) not in (None, ""))
    parts = [vp["model"]]
    if vp.get("dll"):
        parts.append(vp["dll"])
    if "mean_path" in vp:
        parts.append(f"mean path {fmt(vp['mean_path'])}")
    if "angle" in vp:
        parts.append(f"angle {fmt(vp['angle'])}")
    parts += [f"{k} = {fmt(v)}" for k, v in (vp.get("params") or {}).items()]
    return "; ".join(parts)


def _face_set(f: dict[str, Any]) -> bool:
    """面有没有改过默认设置。/ Whether a face differs from the default."""
    return f.get("is") != "default" or bool(f.get("coating")) or bool(f.get("profile")) or (f.get("scatter") or {}).get("model") != "none"


def _scatter_text(f: dict[str, Any]) -> str:
    if f.get("profile"):
        return f"profile {f['profile']}"
    sc = f.get("scatter") or {}
    if sc.get("model") in (None, "none"):
        return "—"
    parts = [sc["model"]]
    parts += [f"{k} {sc[k]}" for k in ("reflect", "transmit", "dll", "file") if sc.get(k)]
    parts += [f"{k} {fmt(sc[k])}" for k in ("fraction", "sigma") if k in sc]
    parts += [f"{k} = {fmt(v)}" for k, v in (sc.get("params") or {}).items()]
    if "rays" in sc:
        parts.append(f"rays {sc['rays']}")
    return "; ".join(parts)


TYPE_DEFAULTS = {"rays_ignore": "never", "consider": [], "ignore": [], "is_detector": False, "global_xyz_rotation": False,
                 "consider_ignore_when_splitting": False}


def _plain_text(v: Any) -> str:
    if isinstance(v, dict):
        return ", ".join(f"{k} {_plain_text(x)}" for k, x in v.items())
    if isinstance(v, list):
        return " ".join(_plain_text(x) for x in v)
    return fmt(v) if isinstance(v, float) else str(v)


def _object_rows(objects: list[dict[str, Any]], descr: dict[str, str], t: Tr, key_prefix: str = "") -> dict[str, Any]:
    """一组物体的表格行：物体表、参数、面属性、散射去向、其余各页（光源、Type、衍射、折射率），以及按材料归并的物体。
    key_prefix 是混合模式里 notes.objects 的键前缀（"<面号>:"）。/ Table rows for one group of objects."""
    out: dict[str, Any] = {"objects": [], "params": [], "faces": [], "targets": [], "tabs": [], "materials": {}}
    for o in objects:
        i = o["index"]
        src = o.get("material_source") or {}
        if o["kind"] == "object":
            key = f"model nd={src.get('nd')} vd={src.get('vd')}" if src.get("kind") == "model" else str(o.get("material") or "") or "(air)"
            out["materials"].setdefault(key, {"o": o, "objects": []})["objects"].append(i)
        note = descr.get(f"{key_prefix}{i}", "")
        name = e(o.get("comment") or "") + (f"<div class='muted'>{e(note)}</div>" if note else "")
        material = "—" if o["kind"] != "object" else (o.get("material") or "—")
        out["objects"].append(
            f"<tr><td class='num'>{i}</td><td>{name}</td><td>{e(o['type'])}</td><td class='num'>{o['ref']}</td><td class='num'>{o['inside_of']}</td>"
            f"<td class='num'>{e(', '.join(f'{v:.4g}' for v in o['position']))}</td><td class='num'>{e(', '.join(f'{v:.4g}' for v in o['tilt']))}</td>"
            f"<td>{e(material)}</td><td>{e(_vp_text(o.get('volume_physics')))}</td></tr>")
        vals = "; ".join([f"{t('file')} {o[k]}" for k in ("file", "file2") if o.get(k)]
                         + [f"par{q['par']} {q['name']} = {fmt(q['value']) if isinstance(q['value'], float) else q['value']}" for q in o.get("params") or []])
        for f in o.get("faces") or []:
            if _face_set(f):
                out["faces"].append(f"<tr><td class='num'>{i}</td><td>{e(f['name'])}</td><td>{e(t('face_is.' + f['is']) if f['is'] in FACE_IS else f['is'])}</td>"
                                 f"<td>{e(f.get('coating') or '—')}</td><td>{e(_scatter_text(f))}</td></tr>")
        st = o.get("scatter_to")
        if st and st.get("objects"):
            out["targets"].append(t("scatter_to.objects", n=i, list=", ".join(map(str, st["objects"]))))
        elif st and st.get("importance"):
            rows = "; ".join(f"→ {r['towards']} (size {fmt(r['size'])}, limit {fmt(r['limit'])})" for r in st["importance"])
            out["targets"].append(t("scatter_to.importance", n=i, rows=rows))
        if vals:
            out["params"].append(f"<tr><td class='num'>{i}</td><td>{e(o['type'])}</td><td>{e(vals)}</td></tr>")
        tabs = []
        if o.get("source"):
            tabs.append(f"{t('tab.source')}: {_plain_text(o['source'])}")
        changed = {k: v for k, v in (o.get("type_options") or {}).items() if k in TYPE_DEFAULTS and v != TYPE_DEFAULTS[k]}
        if changed:
            tabs.append(f"{t('tab.type')}: {_plain_text(changed)}")
        for key in ("diffraction", "index_model"):
            if o.get(key):
                tabs.append(f"{t('tab.' + key)}: {_plain_text(o[key])}")
        if tabs:
            out["tabs"].append(f"<tr><td class='num'>{i}</td><td>{e(o['type'])}</td><td>{e(' | '.join(tabs))}</td></tr>")
    return out


def _objects_html(rows: dict[str, Any], t: Tr, h: str) -> str:
    faces = (f"<div class='wrap'><table><thead><tr>{header_row(t('th.faces'))}</tr></thead><tbody>{''.join(rows['faces'])}</tbody></table></div>"
             if rows["faces"] else "")
    faces += "".join(f"<p>{e(r)}</p>" for r in rows["targets"])
    faces = faces or f"<p class='muted'>{t('faces_default')}</p>"
    tabs = f"<div class='wrap'><table><tbody>{''.join(rows['tabs'])}</tbody></table></div>" if rows["tabs"] else f"<p class='muted'>{t('tabs_default')}</p>"
    return (f"<{h}>{t('h.objects')}</{h}><div class='wrap'><table><thead><tr>{header_row(t('th.objects'))}</tr></thead><tbody>{''.join(rows['objects'])}</tbody></table></div>"
            f"<{h}>{t('h.object_params')}</{h}><div class='wrap'><table><tbody>{''.join(rows['params'])}</tbody></table></div>"
            f"<{h}>{t('h.faces')}</{h}>{faces}<{h}>{t('h.tabs')}</{h}>{tabs}")


def render_nsc(desc: dict[str, Any], notes: dict[str, Any] | None, meta: dict[str, Any], lang: str,
               found: list[tuple[str, dict[str, Any]]] | None = None, shown: dict[str, Any] | None = None) -> str:
    """非序列系统的建模说明页。/ The model page of a non-sequential system."""
    t = Tr(lang)
    notes = notes or {}
    descr = {str(k): v for k, v in (notes.get("objects") or {}).items()}
    waves = desc["wavelengths"]
    wave_heads = "|".join(f"n({w['um'] * 1000:.0f} nm)" for w in waves)
    found = checks(desc, notes) if found is None else found
    warnings = "".join(f"<li>{e(t('w.' + code, **args))}</li>" for code, args in found)
    system_rows = [(t("units"), desc.get("lens_units")), (t("catalogs"), ", ".join(desc.get("material_catalogs") or []) or t("none"))]
    if desc.get("coating_file"):
        system_rows.append((t("coating_file"), desc["coating_file"]))
    system = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in system_rows)
    nsc_rows = "".join(f"<tr><th>{e(k)}</th><td>{e(fmt(v) if isinstance(v, float) else v)}</td></tr>" for k, v in (desc.get("nsc") or {}).items())
    wave_rows = "".join(
        f"<tr><td class='num'>{k}</td><td class='num'>{fmt(w['um'])}</td><td class='num'>{fmt(w['weight'])}</td><td>{'★' if w['primary'] else ''}</td></tr>"
        for k, w in enumerate(waves, 1)
    )
    rows = _object_rows(desc["objects"], descr, t)
    materials = rows["materials"]
    mat_rows = []
    for key, m in materials.items():
        o = m["o"]
        src = o.get("material_source") or {}
        if src.get("kind") == "absorb":
            source, params = t("src.absorb"), ""
        else:
            source, params = _source_text(src, t)
        name = "—" if key == "(air)" else key if not key.startswith("model") else "MODEL"
        ns = "".join(f"<td class='num'>{v:.6f}</td>" for v in o.get("n") or [])
        mat_rows.append(f"<tr><td>{e(name)}</td><td>{e(source)}</td><td>{e(params)}</td>{ns}<td>{e(', '.join(map(str, m['objects'])))}</td></tr>")
    layouts = []
    for key, value in (desc.get("layout") or {}).items():
        if isinstance(value, str):
            layouts.append(f"<h3>{e(key)}</h3><p><img src='{e(value)}' alt='{e(key)}'></p>")
        else:
            layouts.append(f"<h3>{e(key)}</h3><p class='muted'>{e(t('layout_missing', err=value.get('error', '')))}</p>")
    assumptions = "".join(f"<li>{e(a)}</li>" for a in notes.get("assumptions") or []) or f"<li>{t('none')}</li>"
    first_order = "".join(f"<li>{e(a)}</li>" for a in notes.get("first_order") or []) or f"<li>{t('none')}</li>"
    return f"""<!doctype html><html lang="{'zh-CN' if lang == 'zh' else 'en'}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{t('title')} · {e(meta['job'])}</title><style>{CSS}</style></head><body><main>
<p class="muted">{t('byline', job=e(meta['job']), lens=e(meta['lens']), time=e(meta['time']))}</p>
<h1>{t('title')}</h1><p>{t('intro')}</p>
<h2>{t('h.warnings')}</h2><div class="card">{f'<ul>{warnings}</ul>' if warnings else t('no_warnings')}</div>
{_shown_html(shown, t)}
<h2>{t('h.system')}</h2><div class="wrap"><table><tbody>{system}</tbody></table></div>
<h3>{t('h.nsc')}</h3><div class="wrap"><table><tbody>{nsc_rows}</tbody></table></div>
<h3>{t('h.waves')}</h3><div class="wrap"><table><thead><tr>{header_row(t('th.waves'))}</tr></thead><tbody>{wave_rows}</tbody></table></div>
<h2>{t('h.materials')}</h2><div class="wrap"><table><thead><tr>{header_row(t('th.obj_materials', waves=wave_heads))}</tr></thead><tbody>{''.join(mat_rows)}</tbody></table></div>
<p class="muted">{t('obj_n_note')}</p>
{_objects_html(rows, t, 'h2')}
<h2>{t('h.first_order')}</h2><ul>{first_order}</ul>
<h2>{t('h.assumptions')}</h2><ul>{assumptions}</ul>
<h2>{t('h.layout')}</h2>{''.join(layouts)}
</main></body></html>"""


def _source_text(src: dict[str, Any], t: Tr) -> tuple[str, str]:
    kind = src.get("kind")
    if kind == "model":
        return t("src.model"), f"nd {fmt(src.get('nd'))}, Vd {fmt(src.get('vd'))}, ΔPgF {fmt(src.get('dpgf'))}"
    if kind == "catalog":
        cats = src.get("catalogs") or []
        status = t("src.status", status=src["status"]) if src.get("status") else ""
        return (t("src.catalog", cats=", ".join(cats)) + status if cats else t("src.uncatalogued")), ""
    return t("src." + (kind if kind in ("mirror", "none", "inherit") else "none")), ""


def _material_key(s: dict[str, Any]) -> str:
    src = s.get("material_source") or {}
    if src.get("kind") == "model":
        return f"model nd={src.get('nd')} vd={src.get('vd')} dpgf={src.get('dpgf')}"
    if src.get("kind") == "inherit":
        return "(inherit)"
    return str(s.get("material") or "") or "(air)"


def _aperture_text(ap: dict[str, Any]) -> str:
    kind = ap.get("type", "None")
    vals = [f"{k} {fmt(v)}" for k, v in ap.items() if k not in ("type", "picked_up_from") and isinstance(v, float) and v]
    picked = f" ← {ap['picked_up_from']}" if "picked_up_from" in ap else ""
    return e(kind) + (f"<div class='muted'>{e(', '.join(vals))}</div>" if vals else "") + picked


def _solve_text(s: dict[str, Any]) -> str:
    parts = []
    for name, d in (s.get("solve_detail") or {}).items():
        kind = d.get("type", "Fixed")
        if kind in ("Fixed", "None", ""):
            continue
        extra = f" ← {d['surface']}×{fmt(d['scale'])}+{fmt(d['offset'])}" if kind == "SurfacePickup" else ""
        parts.append(f"{name}: {kind}{extra}")
    return e("; ".join(parts)) or "—"


def _extra_rows(desc: dict[str, Any], t: Tr, yn) -> list[tuple[str, Any]]:
    """环境、切趾、偏振、膜层文件、光线瞄准选项；旧的描述里没有的项不列。/ Rows for the later system settings, when present."""
    rows: list[tuple[str, Any]] = []
    env = desc.get("environment")
    if env:
        rows.append((t("environment"), t("env_value", t=fmt(env["temperature"]), p=fmt(env["pressure"]), adj=yn(env["adjust_index"]))))
    apod = desc.get("apodization")
    if apod:
        rows.append((t("apodization"), apod["type"] + (f" (G = {fmt(apod['factor'])})" if "factor" in apod else "")))
    pol = desc.get("polarization")
    if pol:
        rows.append((t("polarization"), t("unpolarized") if pol["unpolarized"] else
                     f"Jx = {fmt(pol['jx'])}, Jy = {fmt(pol['jy'])}, X {fmt(pol['x_phase'])}°, Y {fmt(pol['y_phase'])}°, {pol['method']}"))
    if desc.get("coating_file"):
        rows.append((t("coating_file"), desc["coating_file"]))
    opts = desc.get("ray_aiming_options")
    if opts:
        on = [k for k, v in opts.items() if v is True]
        shift = ", ".join(f"{k} = {' / '.join(fmt(x) for x in opts[k])}" for k in ("pupil_shift", "pupil_compress") if any(opts.get(k) or []))
        rows.append((t("aiming_options"), "; ".join(x for x in (", ".join(on), shift) if x) or t("none")))
    return rows


def _configs_html(cfg: dict[str, Any], t: Tr) -> str:
    if (cfg.get("count") or 1) < 2:
        return ""
    cols = "|".join(str(c) for c in range(1, cfg["count"] + 1))
    rows = []
    for o in cfg.get("operands") or []:
        cells = "".join(f"<td class='num'>{e(fmt(v) if isinstance(v, (int, float)) else v)}{'' if s in ('Fixed', 'None') else f' ({e(s)})'}</td>"
                        for v, s in zip(o["values"], o.get("solves") or [""] * len(o["values"])))
        rows.append(f"<tr><td class='num'>{o['row']}</td><td>{e(o['type'])}</td><td>{e(', '.join(map(str, o.get('params') or [])))}</td>{cells}</tr>")
    return (f"<h2>{t('h.configs')}</h2><p class='muted'>{e(t('configs_note', n=cfg['count'], cur=cfg.get('current', 1)))}</p>"
            f"<div class='wrap'><table><thead><tr>{header_row(t('th.configs', cols=cols))}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>")


def render(desc: dict[str, Any], notes: dict[str, Any] | None, meta: dict[str, Any], lang: str,
           found: list[tuple[str, dict[str, Any]]] | None = None, shown: dict[str, Any] | None = None) -> str:
    if desc.get("mode") == "non_sequential":
        return render_nsc(desc, notes, meta, lang, found, shown)
    t = Tr(lang)
    notes = notes or {}
    descr = {str(k): v for k, v in (notes.get("surfaces") or {}).items()}
    waves = desc["wavelengths"]
    wave_heads = "|".join(f"n({w['um'] * 1000:.0f} nm)" for w in waves)
    found = checks(desc, notes) if found is None else found
    warnings = "".join(f"<li>{e(t('w.' + code, **args))}</li>" for code, args in found)
    ap = desc["aperture"]
    yn = lambda v: t("yes") if v else t("no")  # noqa: E731
    system_rows = [
        (t("units"), desc.get("lens_units")),
        (t("aperture"), f"{ap['type']} = {fmt(ap['value'])}"),
        (t("telecentric"), yn(ap.get("telecentric_object"))),
        (t("afocal"), yn(ap.get("afocal_image"))),
        (t("aiming"), desc.get("ray_aiming")),
        (t("stop"), desc.get("stop_surface")),
        (t("catalogs"), ", ".join(desc.get("material_catalogs") or []) or t("none")),
    ]
    if "afocal_units" in ap:
        system_rows.insert(4, (t("afocal_units"), ap["afocal_units"]))
    system_rows += _extra_rows(desc, t, yn)
    system = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in system_rows)
    fields = "".join(
        f"<tr><td class='num'>{k}</td><td class='num'>{fmt(f['x'])}</td><td class='num'>{fmt(f['y'])}</td><td class='num'>{fmt(f['weight'])}</td>"
        f"<td class='num'>{' / '.join(fmt(v) for v in (f.get('vignetting') or {}).values())}</td></tr>"
        for k, f in enumerate(desc["fields"]["items"], 1)
    )
    wave_rows = "".join(
        f"<tr><td class='num'>{k}</td><td class='num'>{fmt(w['um'])}</td><td class='num'>{fmt(w['weight'])}</td><td>{'★' if w['primary'] else ''}</td></tr>"
        for k, w in enumerate(waves, 1)
    )
    materials: dict[str, dict[str, Any]] = {}
    for s in desc["surfaces"][:-1]:
        key = _material_key(s)
        entry = materials.setdefault(key, {"s": s, "surfaces": []})
        entry["surfaces"].append(s["index"])
    mat_rows = []
    for key, m in materials.items():
        s = m["s"]
        source, params = _source_text(s.get("material_source") or {}, t)
        if key == "(inherit)":
            continue
        name = "—" if key == "(air)" else key if not key.startswith("model") else "MODEL"
        ns = "".join(f"<td class='num'>{v:.6f}</td>" for v in s.get("n") or [])
        mat_rows.append(f"<tr><td>{e(name)}</td><td>{e(source)}</td><td>{e(params)}</td>{ns}<td>{e(', '.join(map(str, m['surfaces'])))}</td></tr>")
    surf_rows = []
    param_rows = []
    for s in desc["surfaces"]:
        i = s["index"]
        note = descr.get(str(i), "")
        name = e(s.get("comment") or "") + (f"<div class='muted'>{e(note)}</div>" if note else "")
        stop = f" <span class='muted'>({t('stop_mark')})</span>" if s.get("is_stop") else ""
        gv = s.get("global_vertex")
        pos = ", ".join(f"{v:.4g}" for v in gv) if gv else "—"
        medium = _material_key(s)
        surf_rows.append(
            f"<tr><td class='num'>{i}</td><td>{name}{stop}</td><td>{e(s['type'])}</td><td class='num'>{fmt(float(s['radius']))}</td>"
            f"<td class='num'>{fmt(float(s.get('conic', 0.0)))}</td><td class='num'>{fmt(float(s['thickness']))}</td>"
            f"<td>{e('—' if medium == '(air)' else '↑' if medium == '(inherit)' else 'MODEL' if medium.startswith('model') else medium)}</td><td>{_aperture_text(s.get('aperture') or {})}</td>"
            f"<td>{_solve_text(s)}</td><td class='num'>{e(pos)}</td></tr>"
        )
        if s.get("tilt_decenter"):
            param_rows.append(f"<tr><td class='num'>{i}</td><td>Tilt/Decenter</td><td>{e('; '.join(f'{k} = {fmt(v)}' for k, v in s['tilt_decenter'].items()))}</td></tr>")
        if s.get("coating"):
            param_rows.append(f"<tr><td class='num'>{i}</td><td>Coating</td><td>{e(s['coating'])}</td></tr>")
        if s.get("params"):
            vals = "; ".join(f"par{p['par']} {p['name']} = {fmt(p['value'])}" + ("" if p.get("solve") in (None, "Fixed") else f" ({p['solve']})") for p in s["params"])
            param_rows.append(f"<tr><td class='num'>{i}</td><td>{e(s['type'])}</td><td>{e(vals)}</td></tr>")
    layouts = []
    for key, value in (desc.get("layout") or {}).items():
        if isinstance(value, str):
            layouts.append(f"<h3>{e(key)}</h3><p><img src='{e(value)}' alt='{e(key)}'></p>")
        else:
            layouts.append(f"<h3>{e(key)}</h3><p class='muted'>{e(t('layout_missing', err=value.get('error', '')))}</p>")
    assumptions = "".join(f"<li>{e(a)}</li>" for a in notes.get("assumptions") or []) or f"<li>{t('none')}</li>"
    first_order = "".join(f"<li>{e(a)}</li>" for a in notes.get("first_order") or []) or f"<li>{t('none')}</li>"
    configs_html = _configs_html(desc.get("configurations") or {}, t)
    # 混合模式：每个 NonSequential 面一组物体，notes.objects 的键写 "<面号>:<物体号>"
    # mixed mode: one object group per NonSequential surface; notes.objects keys are "<surface>:<object>"
    obj_descr = {str(k): v for k, v in (notes.get("objects") or {}).items()}
    groups_html = "".join(f"<h2>{t('h.nsc_group', s=s['index'])}</h2>" + _objects_html(_object_rows(s["objects"], obj_descr, t, f"{s['index']}:"), t, "h3")
                          for s in desc["surfaces"] if s.get("objects"))
    params_html = (
        f"<h2>{t('h.params')}</h2><div class='wrap'><table><tbody>{''.join(param_rows)}</tbody></table></div>" if param_rows else ""
    )
    return f"""<!doctype html><html lang="{'zh-CN' if lang == 'zh' else 'en'}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{t('title')} · {e(meta['job'])}</title><style>{CSS}</style></head><body><main>
<p class="muted">{t('byline', job=e(meta['job']), lens=e(meta['lens']), time=e(meta['time']))}</p>
<h1>{t('title')}</h1><p>{t('intro')}</p>
<h2>{t('h.warnings')}</h2><div class="card">{f'<ul>{warnings}</ul>' if warnings else t('no_warnings')}</div>
{_shown_html(shown, t)}
<h2>{t('h.system')}</h2><div class="wrap"><table><tbody>{system}</tbody></table></div>
<h3>{t('h.fields')}</h3><p class="muted">{e(t('field_type', type=desc['fields']['type'], norm=desc['fields'].get('normalization', '—')))}</p>
<div class="wrap"><table><thead><tr>{header_row(t('th.fields'))}</tr></thead><tbody>{fields}</tbody></table></div>
<h3>{t('h.waves')}</h3><div class="wrap"><table><thead><tr>{header_row(t('th.waves'))}</tr></thead><tbody>{wave_rows}</tbody></table></div>
<h2>{t('h.materials')}</h2><div class="wrap"><table><thead><tr>{header_row(t('th.materials', waves=wave_heads))}</tr></thead><tbody>{''.join(mat_rows)}</tbody></table></div>
<p class="muted">{t('n_note')}</p>
<h2>{t('h.surfaces')}</h2><div class="wrap"><table><thead><tr>{header_row(t('th.surfaces'))}</tr></thead><tbody>{''.join(surf_rows)}</tbody></table></div>
<p class="muted">{t('thick_note')}</p>
{params_html}
{groups_html}
{configs_html}
<h2>{t('h.first_order')}</h2><ul>{first_order}</ul>
<h2>{t('h.assumptions')}</h2><ul>{assumptions}</ul>
<h2>{t('h.layout')}</h2>{''.join(layouts)}
</main></body></html>"""


# ---------- 确认页的四节 / the four sections shown before approval ----------
# 数据只记代码能确定的事实；shown_sha 是这份数据（与语言无关）的摘要，页面文字由 TEXT 生成。
# The data holds only what the code can establish; shown_sha digests this data (language-free), and the page text comes from TEXT.

NEXT_BUDGET_KEYS = {"max_seconds", "plan"}
MEASURE_FAILURES = {"not_measured", "check_error", "no_valid_value"}


def next_budget(notes: dict[str, Any] | None) -> dict[str, Any] | None:
    """notes.next_budget：{"max_seconds": 正数, "plan": 非空文字}。没有时返回 None（页面给自动警告），格式不对时报错。
    notes.next_budget: {"max_seconds": positive number, "plan": text}. None when absent (the page warns); an error when malformed."""
    raw = (notes or {}).get("next_budget")
    if raw is None:
        return None
    seconds = raw.get("max_seconds") if isinstance(raw, dict) else None
    if (not isinstance(raw, dict) or set(raw) != NEXT_BUDGET_KEYS or isinstance(seconds, bool) or not isinstance(seconds, (int, float))
            or not math.isfinite(seconds) or seconds <= 0 or not str(raw.get("plan", "")).strip()):
        raise SaosError(ErrorCode.BAD_ARGS, 'notes.next_budget must be {"max_seconds": <positive number>, "plan": "<the optimisations and scans to run next>"}')
    return {"max_seconds": float(seconds), "plan": str(raw["plan"]).strip()}


def measure_requirements(ctx) -> dict[str, Any]:
    """用需求单现有的判定入口（demand.quick：不存证据、不进 evaluations/）测当前镜头上所有带判定方法的条目。
    返回各项的值、判定出错的项、没有有效值的项和后端名；需求单不存在或读不出时 error 写明原因。
    Measure the current lens's checked items with the demand's judging entry (demand.quick: no evidence kept, nothing in
    evaluations/). Returns values, failed checks, items with no valid value and the backend name; error says why when the
    demand is missing or unreadable."""
    identity = ctx.backend.identity()
    out: dict[str, Any] = {"backend": identity.get("backend"), "version": identity.get("version"), "error": None}
    try:
        d = demand.load_job(ctx.job.root)
        res = demand.quick(ctx, [s["id"] for s in demand.checked(d)])
    except SaosError as exc:
        out["error"] = exc.message
        return out
    return {**out, "values": res["values"], "errors": res["errors"], "invalid": res["invalid"]}


def _demand_or_error(job: jobs.Job) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return demand.load_job(job.root), None
    except SaosError as exc:
        return None, exc.message


def _bound(entry: dict[str, Any], section: str, direction: str | None) -> dict[str, Any]:
    """界限的结构化形式，只含有的键。/ The bound in structured form, keeping only the keys present."""
    out: dict[str, Any] = {}
    if section == "constants":
        if entry.get("goal") is not None:
            out.update(entry["goal"])
        elif entry.get("value") is not None:
            out["fixed"] = entry["value"]
    elif section == "variables":
        out.update(entry.get("range") or {})
    else:
        out.update(entry.get("must") or {})
        if direction:
            out.update(direction=direction, ideal=float(entry.get("ideal", 0.0)))
    return out


def requirement_rows(d: dict[str, Any], measured: dict[str, Any] | None) -> list[dict[str, Any]]:
    """需求单逐条的当前值与状态。满足/未满足按 demand.shortfall（与判定一致）；未知只指有判定但没有有效值或判定出错。
    没有判定方法的条目没有界限（固定条件、只定范围的自由度），记为无界限，不谈满足与否。
    Each demand item with its current value and status. met/unmet follow demand.shortfall, as the judging does; unknown means
    a check that gave no valid value or failed. An item without a check has no bound (a fixed condition, a free variable), so it
    is no bound rather than unknown."""
    specs = {s["id"]: s for s in demand.checked(d)}
    rows = []
    for section in demand.SECTIONS:
        for entry in d.get(section) or []:
            item = specs.get(entry["id"])
            direction = demand.pursuit_of(entry) if section == "objectives" else None
            value = None
            reason = detail = None
            if item is None:
                status, reason = "no_bound", "no_check"
            elif measured is None or measured.get("error"):
                status, reason = "unknown", "not_measured"
                detail = (measured or {}).get("error") or "no measurement"
            elif entry["id"] in measured.get("errors", {}):
                status, reason, detail = "unknown", "check_error", measured["errors"][entry["id"]]
            elif measured.get("values", {}).get(entry["id"]) is None:
                status, reason = "unknown", "no_valid_value"
            else:
                value = float(measured["values"][entry["id"]])
                if item["goal"] is None:
                    status = "pursuit" if direction else "no_bound"
                else:
                    status = "met" if demand.shortfall(item, value) == 0 else "unmet"
            source = str(entry.get("source") or "").strip()
            rows.append({"id": entry["id"], "section": section, "description": str(entry.get("description", "")),
                         "origin": entry.get("origin") if section == "objectives" else None,
                         "bound": _bound(entry, section, direction), "value": value, "status": status, "reason": reason,
                         "detail": detail, "source": source, "assumed": bool(demand.ASSUMPTION_RE.match(source))})
    return rows


def _glass_items(desc: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """面或物体上的玻璃（真实或模型）：(所在, 项)。/ Glasses (real or model) on surfaces or objects: (where, item)."""
    if desc.get("mode") == "non_sequential":
        found = [(f"object {o['index']}", o) for o in desc.get("objects") or [] if o.get("kind") == "object"]
    else:
        found = []
        for s in desc["surfaces"]:
            found.append((f"surface {s['index']}", s))
            found += [(f"surface {s['index']} object {o['index']}", o) for o in s.get("objects") or [] if o.get("kind") == "object"]
    return [(where, item) for where, item in found if (item.get("material_source") or {}).get("kind") in ("model", "catalog")]


def _record(job: jobs.Job) -> tuple[list[dict[str, Any]] | None, str | None]:
    """材料来源记录里的牌号条目：没有文件时 (None, None)，读不出时 (None, 原因)。
    The record's material entries: (None, None) without the file, (None, reason) when it cannot be read."""
    path = job.root / materials_record.FILE
    if not path.is_file():
        return None, None
    try:
        return materials_record.validate(yaml.safe_load(path.read_text(encoding="utf-8")))["materials"], None
    except (OSError, yaml.YAMLError, SaosError) as exc:
        return None, exc.message if isinstance(exc, SaosError) else str(exc)


def _record_entry(entries: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    # 与 materials_record.recorded_names 相同的对应：牌号（zemax.name 或 name）大写相等
    # the same match as materials_record.recorded_names: the glass name (zemax.name or name), compared in upper case
    return next((m for m in entries if str((m.get("zemax") or {}).get("name") or m.get("name")).upper() == name.upper()), None)


def _record_source(m: dict[str, Any]) -> dict[str, Any]:
    if m.get("library_id"):
        return {"kind": "library", "id": m["library_id"]}
    if m.get("source_ids"):
        return {"kind": "sources", "ids": list(m["source_ids"])}
    return {"kind": "vendor", "catalog": (m.get("zemax") or {}).get("catalog")}


def _cell(state: str, code: str | None = None, **note: Any) -> dict[str, Any]:
    return {"state": state, "note": {"code": code, **note} if code else {}}


def _level_found(record: tuple[list[dict[str, Any]] | None, str | None], name: str) -> dict[str, Any]:
    entries, error = record
    if error:
        return _cell("unknown", "record_unreadable", err=error)
    if entries is None:
        return _cell("unknown", "no_record")
    m = _record_entry(entries, name)
    if m is None:
        return _cell("no", "not_in_record")
    return _cell("yes", "in_record", id=m["id"], source=_record_source(m))


def _level_complete(record: tuple[list[dict[str, Any]] | None, str | None], name: str, waves_nm: list[float]) -> dict[str, Any]:
    """数据完整：只有记录里的色散数据给出有效范围时才能判定；厂家目录里的牌号，记录没有范围，记为未知。
    Data complete: decided only when the record's dispersion gives a valid range; a vendor-catalogue glass has no range in the record, so unknown."""
    entries, error = record
    m = _record_entry(entries, name) if entries and not error else None
    if m is None:
        return _cell("unknown", "not_found")
    disp = m.get("dispersion")
    if not disp:
        return _cell("unknown", "no_dispersion")
    rng = disp.get("valid_range_nm")
    if not (isinstance(rng, list) and len(rng) == 2 and all(isinstance(x, (int, float)) for x in rng)):
        return _cell("unknown", "no_range")
    lo, hi = float(rng[0]), float(rng[1])
    missing = [w for w in waves_nm if not lo <= w <= hi]
    if missing:
        return _cell("no", "not_covered", lo=lo, hi=hi, missing=missing)
    return _cell("yes", "covered", lo=lo, hi=hi)


def _optiland_local(name: str, catalogs: list[str]) -> dict[str, Any]:
    """Optiland 能否从本机 AGF 或材料库读到这个牌号：只查 MaterialStore 查找的位置，不加载。
    Whether Optiland can read the glass from the local AGF files or the material library: looks where MaterialStore looks, loads nothing."""
    from saos.backend import optiland_materials as om

    try:
        store = om.MaterialStore()
        if any(store.catalog(cat).get(name.upper()) for cat in catalogs):
            return _cell("yes", "local_agf")
        lib = om.library("materials", "catalog.json").get("materials", [])
        if any(name.casefold() in [str(m.get(k, "")).casefold() for k in ("id", "name", "agf_name")] for m in lib):
            return _cell("yes", "local_library")
    except SaosError as exc:
        return _cell("unknown", "record_unreadable", err=exc.message)
    return _cell("no", "not_local")


def _level_backend(name: str, catalogs: list[str], backend: str | None) -> dict[str, Any]:
    if backend is None:
        return _cell("unknown", "backend_unknown")
    if backend == "zos":
        # OpticStudio 只能用在用目录里的牌号，目录列表由后端给出 / OpticStudio uses only glasses of the catalogues in use, as the backend lists them
        return _cell("yes", "catalog_in_use", catalogs=catalogs) if catalogs else _cell("no", "not_in_use")
    if backend == "optiland":
        return _optiland_local(name, catalogs)
    return _cell("unknown", "backend_unknown")


def _level_loaded(n: Any, waves_nm: list[float]) -> dict[str, Any]:
    if not isinstance(n, list) or len(n) != len(waves_nm):
        return _cell("unknown", "not_measured")
    if not all(isinstance(x, (int, float)) and math.isfinite(x) for x in n):
        return _cell("no", "non_finite")
    return _cell("yes", "measured", n=len(n))


def material_rows(job: jobs.Job, desc: dict[str, Any], backend: str | None) -> list[dict[str, Any]]:
    """每种玻璃的四级核对：资料找到、数据完整、后端支持、实际加载。模型玻璃不是真实材料，前三级记为不适用。
    Each glass at four levels: source found, data complete, backend supports, actually loaded. A model glass is not a real
    material, so its first three levels are n/a."""
    record = _record(job)
    waves = [float(w["um"]) * 1000 for w in desc["wavelengths"]]
    groups: dict[tuple, dict[str, Any]] = {}
    for where, item in _glass_items(desc):
        src = item["material_source"]
        if src["kind"] == "model":
            key = ("model", src.get("nd"), src.get("vd"))
            name = f"model nd {fmt(src.get('nd'))}, Vd {fmt(src.get('vd'))}"
        else:
            name = str(src.get("name") or "")
            key = ("catalog", name.upper(), tuple(src.get("catalogs") or []))
        groups.setdefault(key, {"name": name, "kind": src["kind"], "catalogs": list(src.get("catalogs") or []), "where": [],
                                "n": item.get("n")})["where"].append(where)
    rows = []
    for g in groups.values():
        if g["kind"] == "model":
            levels = {"found": _cell("na", "model"), "complete": _cell("na", "model"), "backend": _cell("na", "model")}
        else:
            levels = {"found": _level_found(record, g["name"]), "complete": _level_complete(record, g["name"], waves),
                      "backend": _level_backend(g["name"], g["catalogs"], backend)}
        levels["loaded"] = _level_loaded(g["n"], waves)
        rows.append({"name": g["name"], "kind": g["kind"], "where": g["where"], "levels": levels})
    return rows


def _generation_rows(job: jobs.Job, folder: Path) -> tuple[dict, list[dict]]:
    try:
        generated = json.loads((folder / "generated.json").read_text(encoding="utf-8"))
        screen = json.loads((folder / "screen.json").read_text(encoding="utf-8")) if generated.get("screen_plan") else {}
    except (OSError, ValueError):
        return {}, []
    rows = [{**row, "verdict": "not_screened", "structure": {"as_built": {"fingerprint": (row.get("prediction") or {}).get("fingerprint")}}}
            for row in screen.get("candidates", [])]
    rows += [{**row, "provenance": row.get("source")} for row in generated.get("not_evaluated", [])]
    return generated, rows


def _selection(job: jobs.Job, d: dict[str, Any], desc: dict[str, Any], folder: Path) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    """只读取已保存的比较，按结构匹配；旧版本显示提示，不阻断确认。"""
    from saos import candidates, stages

    if demand.structure_mode(d) == "prescribed":
        data = {"mode": "prescribed", "reason": str(d["structure"]["reason"]).strip()}
        previous = model_gate.approvals(job)
        if previous and previous[0].get("desc"):
            changed = model_gate.structure_changes(previous[0]["desc"], desc, previous[0]["environment"])
            if changed:
                data["differences"] = changed
                return data, [("prescribed_changed", {"changed": ", ".join(changed)})]
        return data, []
    method_sha = demand.method_digest(demand.prepare_scripts(job.root, d))
    fingerprint = model_gate.digest(model_gate.fingerprint(desc))
    fallback = None
    for scan in candidates._scan_folders(job):
        try:
            plan = json.loads((scan / "plan.json").read_text(encoding="utf-8"))
            result = json.loads((scan / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if plan.get("mode") != "candidates":
            continue
        rows = result.get("rows") or []
        if plan.get("generation"):
            try:
                generated, generation_rows = _generation_rows(job, jobs.inside(job.root, plan["generation"]))
            except SaosError:
                generation_rows = []
            rows = rows + [r for r in generation_rows if r.get("verdict") in ("material", "computation", "library", "configuration")]
        matched = [r for r in rows if any((r.get("structure") or {}).get(k, {}).get("fingerprint") == fingerprint
                                        for k in ("as_built", "end"))]
        issues = []
        if plan.get("method_sha") != method_sha:
            issues.append("checks_changed")
        if demand.as_current(plan.get("demand_sha"), d, "compute", job.root) != demand.compute_digest(d):
            issues.append("comparison_outdated")
        if not all(stages.sealed(job.root, (scan / f).relative_to(job.root).as_posix()) for f in ("plan.json", "result.json")):
            issues.append("comparison_changed")
        if not matched:
            issues.append("comparison_unmatched")
        rel = scan.relative_to(job.root).as_posix()
        link = os.path.relpath(scan / "index.html", folder).replace("\\", "/") if (scan / "index.html").is_file() else None
        chosen = {**matched[0], "candidate": matched[0]["id"], "scan": rel, "link": link} if matched else None
        data = {"mode": "free", "scan": rel, "link": link, "chosen": chosen, "matches": [r["id"] for r in matched],
                "rows": rows, "constraints": plan.get("constraints"), "issues": issues,
                "demand_sha": plan.get("demand_sha"), "method_sha": plan.get("method_sha")}
        result = data, [(code, {}) for code in issues]
        if matched:
            return result
        if fallback is None:
            fallback = result
    for path in sorted((job.root / "explore" / "starts").glob("*/generated.json"), reverse=True):
        generated, rows = _generation_rows(job, path.parent)
        if not generated:
            continue
        matched = [r for r in rows if (r.get("prediction") or {}).get("fingerprint") == fingerprint]
        if matched or fallback is None:
            chosen = {**matched[0], "candidate": matched[0]["id"]} if matched else None
            return {"mode": "free", "chosen": chosen, "matches": [r["id"] for r in matched], "rows": rows,
                    "constraints": generated.get("constraints"), "issues": ["no_screening"], "demand_sha": generated.get("demand_sha")}, [("no_screening", {})]
    return fallback or ({"mode": "free", "chosen": None, "rows": []}, [("no_screening", {})])


def _requirements(d: dict[str, Any] | None, measured: dict[str, Any] | None, error: str | None) -> dict[str, Any]:
    if d is None:
        return {"backend": None, "version": None, "error": error, "rows": []}
    if measured is None:
        return {"backend": None, "version": None, "error": "no measurement", "rows": requirement_rows(d, None)}
    return {"backend": measured.get("backend"), "version": measured.get("version"), "error": measured.get("error"),
            "rows": requirement_rows(d, measured)}


def _verdict_text(verdict: str | None, t: Tr) -> str:
    key = f"verdict.{verdict}"
    return t(key) if key in TEXT else t("verdict.unknown")


def _shown_html(shown: dict[str, Any] | None, t: Tr, h: str = "h2") -> str:
    """确认页的四节 / the four sections of the approval page."""
    shown = shown or {}
    return "".join([_selection_html(shown.get("selection"), t, h), _requirements_html(shown.get("requirements"), t, h),
                    _validity_html(shown.get("validity"), t, h), _budget_html(shown.get("budget"), t, h)])


def _selection_html(sel: dict[str, Any] | None, t: Tr, h: str) -> str:
    head = f"<{h}>{t('h.selection')}</{h}>"
    if not sel or sel.get("mode") == "unknown":
        return head + f"<p class='muted'>{e(t('sel.no_demand'))}</p>"
    if sel["mode"] == "prescribed":
        return head + f"<p>{e(t('sel.prescribed', reason=sel['reason']))}</p>"
    c = sel.get("chosen")
    text = t("sel.matched", ids=", ".join(sel.get("matches") or [c["candidate"]])) if c else t("sel.none")
    link = sel.get("link") or (c or {}).get("link")
    out = head + f"<p>{e(text)}</p>"
    if sel.get("demand_sha"):
        out += f"<p class='muted'>{e(t('sel.versions'))}: {e(sel['demand_sha'][:12])} / {e((sel.get('method_sha') or '—')[:12])}</p>"
    if link:
        out += f"<p><a href='{e(link)}'>{e(t('sel.link'))}</a></p>"
    if sel.get("constraints"):
        values = sel["constraints"].get("values") or {}
        text = "; ".join(f"{t('metric.' + key) if 'metric.' + key in TEXT else key}={value}" for key, value in values.items())
        out += f"<p>{e(t('sel.constraints'))}: {e(text)}</p>"
    rows = []
    for row in sel.get("rows") or []:
        origin = row.get("provenance") or row.get("source") or "—"
        origin_notes = list(origin.get("notes") or []) if isinstance(origin, dict) else []
        if isinstance(origin, dict) and (origin.get("review") or {}).get("level") == "secondary_transcription":
            origin_notes.insert(0, t("sel.secondary"))
        if isinstance(origin, dict) and origin.get("validation") == "sourced_unverified":
            origin_notes.insert(0, t("sel.unverified"))
        if isinstance(origin, dict):
            origin = " · ".join(str(origin[k]) for k in ("book", "patent", "page", "locator", "url", "version") if origin.get(k))
        origin_html = e(origin) + ("<p class='muted'>" + e("; ".join(origin_notes)) + "</p>" if origin_notes else "")
        why = "; ".join(row.get("problems") or []) or row.get("error") or "—"
        extrapolation = (row.get("constraints") or {}).get("extrapolation") or []
        if extrapolation:
            why += "; " + t("sel.extrapolation") + ": " + "; ".join(extrapolation)
        start = (row.get("start") or {}).get("shortfall")
        why += f"; G: {fmt(start)} → {fmt(row.get('shortfall'))}"
        tightest = (row.get("constraints") or {}).get("tightest")
        if tightest:
            why += f"; {t('sel.tightest')}: {t('metric.' + tightest) if 'metric.' + tightest in TEXT else tightest}"
        prediction = row.get("prediction")
        pred = prediction_html(prediction, t) if prediction else e(t("sel.no_prediction"))
        rows.append(f"<tr><td>{e(row.get('id'))}</td><td>{e(row.get('form') or row.get('hypothesis') or '—')}</td>"
                    f"<td>{origin_html}</td><td>{e(_verdict_text(row.get('verdict'), t))}</td>"
                    f"<td>{e(why)}</td><td>{pred}</td></tr>")
    if rows:
        out += f"<div class='wrap'><table><thead><tr>{header_row(t('sel.columns'))}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>"
    return out


def prediction_html(pred: dict, t: Tr) -> str:
    def table(caption, columns, rows):
        cells = "".join("<tr>" + "".join(f"<td>{e(v)}</td>" for v in row) + "</tr>" for row in rows)
        return f"<div class='wrap'><table><caption>{e(caption)}</caption><thead><tr>{header_row('|'.join(columns))}</tr></thead><tbody>{cells}</tbody></table></div>"
    first = pred.get("first_order") or {}
    labels = {"efl": "EFL", "bfl": "BFL", "total_track": t("pred.track"), "stop_surface": t("pred.stop"),
              "stop_z": t("pred.stop_z"), "entrance_pupil": t("pred.ep"), "exit_pupil": t("pred.xp"),
              "entrance_pupil_diameter": t("pred.epd"), "f_number": "f/#"}
    out = table(t("pred.first"), [t("pred.quantity"), t("pred.value")], [[labels.get(k,k), fmt(v)] for k,v in first.items()])
    petzval = pred.get("petzval") or {}
    out += f"<p>{e(t('pred.petzval'))}: {fmt(petzval.get('radius_over_efl'))}</p>"
    for section, title in (("seidel", "Seidel"), ("third_order", t("pred.third"))):
        terms = pred.get(section) or {}
        columns = terms.get("columns") or list((terms.get("totals") or {}).keys())
        rows = terms.get("surfaces") or []
        if columns:
            body = [[str(i+1)] + [fmt(row.get(k)) for k in columns] if isinstance(row, dict)
                    else [str(i+1)] + [fmt(v) for v in row] for i,row in enumerate(rows)]
            totals = terms.get("totals") or {}
            body.append([t("pred.total")] + ([fmt(totals.get(k)) for k in columns] if isinstance(totals, dict) else [fmt(v) for v in totals]))
            out += table(title, [t("pred.surface"), *columns], body)
    incidence = pred.get("incidence") or {}
    out += table(t("pred.incidence"), [t("pred.surface"), "deg", t("pred.samples")],
                 [[str(r["surface"]), fmt(r.get("max_deg")), str(r["valid_samples"])] for r in incidence.get("surfaces", [])])
    out += f"<p>{e(t('pred.invalid'))}: {incidence.get('invalid', '—')} / {incidence.get('attempted', '—')}</p>"
    out += f"<p class='muted'>{e(t('pred.scope'))}</p>"
    if pred.get("errors"):
        out += "<p>" + e("; ".join(f"{k}: {v}" for k,v in pred["errors"].items())) + "</p>"
    return f"<details><summary>{e(t('sel.prediction'))}</summary>{out}</details>"


def _bound_text(b: dict[str, Any], t: Tr) -> str:
    parts = []
    if "fixed" in b:
        parts.append(t("bound.fixed", v=fmt(b["fixed"])))
    if "equals" in b:
        parts.append(t("bound.equals", v=fmt(b["equals"]), tol=fmt(b["tol"])))
    if "min" in b:
        parts.append(t("bound.min", v=fmt(b["min"])))
    if "max" in b:
        parts.append(t("bound.max", v=fmt(b["max"])))
    if b.get("direction"):
        parts.append(t(f"bound.dir.{b['direction']}", v=fmt(b["ideal"])))
    return t("sep").join(parts) or t("bound.none")


def _status_text(r: dict[str, Any], t: Tr) -> str:
    if r["status"] in ("met", "unmet", "pursuit", "no_bound"):
        return t(f"status.{r['status']}")
    return f"{t('status.unknown')}：{t('reason.' + r['reason'], err=r.get('detail') or '')}"


def _requirements_html(req: dict[str, Any] | None, t: Tr, h: str) -> str:
    head = f"<{h}>{t('h.requirements')}</{h}>"
    req = req or {"rows": [], "error": None}
    if req.get("error") and not req["rows"]:
        return head + f"<p class='muted'>{e(t('req_error', err=req['error']))}</p>"
    if not req["rows"]:
        return head + f"<p class='muted'>{e(t('req_none'))}</p>"
    rows = []
    for r in req["rows"]:
        kind = t("type." + r["section"]) if r["section"] != "objectives" else t("type.objectives", origin=t("origin." + (r["origin"] or "agent")))
        source = e(r["source"] or "—") + (f" <span class='muted'>[{e(t('src.assumed'))}]</span>" if r["assumed"] else "")
        value = "—" if r["value"] is None else fmt(r["value"])
        rows.append(f"<tr><td>{e(r['id'])}<div class='muted'>{e(r['description'])}</div></td><td>{e(kind)}</td><td class='num'>{e(value)}</td>"
                    f"<td>{e(_bound_text(r['bound'], t))}</td><td>{e(_status_text(r, t))}</td><td>{source}</td></tr>")
    backend = t("req_backend", backend=req.get("backend") or "—", version=req.get("version") or "—") if req.get("backend") else ""
    return (f"{head}<p class='muted'>{e(backend)}</p><div class='wrap'><table><thead><tr>{header_row(t('th.requirements'))}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def _note_text(cell: dict[str, Any], t: Tr) -> str:
    note = cell.get("note") or {}
    code = note.get("code")
    if code == "in_record":
        src = note["source"]
        if src["kind"] == "library":
            source = t("note.src_library", id=src["id"])
        elif src["kind"] == "sources":
            source = t("note.src_sources", ids=", ".join(map(str, src["ids"])))
        else:
            source = t("note.src_vendor", catalog=src.get("catalog") or "—")
        return t("note.in_record", id=note["id"], source=source)
    if code == "covered":
        return t("note.covered", lo=fmt(note["lo"]), hi=fmt(note["hi"]))
    if code == "not_covered":
        return t("note.not_covered", lo=fmt(note["lo"]), hi=fmt(note["hi"]), missing=", ".join(fmt(x) for x in note["missing"]))
    if code == "catalog_in_use":
        return t("note.catalog_in_use", cats=", ".join(note["catalogs"]))
    if code == "measured":
        return t("note.measured", n=note["n"])
    if not code:
        return ""
    return t("note." + code, err=note.get("err", ""))


def _validity_html(val: dict[str, Any] | None, t: Tr, h: str) -> str:
    head = f"<{h}>{t('h.validity')}</{h}>"
    val = val or {"numeric": {"applicable": False}, "unknown": [], "materials": []}
    num = val.get("numeric") or {}
    if not num.get("applicable"):
        numeric = t("val.numeric_na")
    elif num.get("warnings"):
        numeric = t("val.numeric_count", n=num["warnings"]["count"], first=num["warnings"]["first"])
    else:
        numeric = t("none")
    unknown = ", ".join(f"{u['id']}（{t('reason.' + u['reason'], err=u.get('detail') or '')}）" for u in val.get("unknown") or []) or t("none")
    mats = []
    for m in val.get("materials") or []:
        cells = "".join(f"<td>{e(t('state.' + m['levels'][k]['state']))}<div class='muted'>{e(_note_text(m['levels'][k], t))}</div></td>"
                        for k in ("found", "complete", "backend", "loaded"))
        mats.append(f"<tr><td>{e(m['name'])}</td><td>{e(', '.join(m['where']))}</td>{cells}</tr>")
    mat_table = (f"<h3>{t('val.materials')}</h3><div class='wrap'><table><thead><tr>{header_row(t('th.levels'))}</tr></thead>"
                 f"<tbody>{''.join(mats)}</tbody></table></div>") if mats else ""
    return (f"{head}<p class='muted'>{e(t('val.intro'))}</p>"
            f"<ul><li>{e(t('val.numeric'))}: {e(numeric)}</li><li>{e(t('val.unknown'))}: {e(unknown)}</li></ul>"
            f"<p class='muted'>{e(t('val.numeric_scope'))}</p>{mat_table}")


def _budget_html(budget: dict[str, Any] | None, t: Tr, h: str) -> str:
    head = f"<{h}>{t('h.budget')}</{h}>"
    if not budget:
        return head + f"<p class='muted'>{e(t('budget.none'))}</p>"
    return head + f"<p>{e(t('budget.line', s=fmt(budget['max_seconds'])))}</p><p>{e(t('budget.plan', plan=budget['plan']))}</p>"


def write(job: jobs.Job, desc: dict[str, Any], folder: Path, notes: dict[str, Any] | None, lens: str, lang: str | None,
          measured: dict[str, Any] | None = None) -> dict[str, Any]:
    """写建模说明页、model.json 和四节的摘要。measured 是 measure_requirements 的结果（None 时需求项都记为未知）。
    Writes the page, model.json and the digest of the four sections. measured is the result of measure_requirements
    (None marks every requirement unknown)."""
    budget = next_budget(notes)
    env = (notes or {}).get("environment")
    if desc.get("mode") == "non_sequential":
        # 非序列系统按物体号（从 1 起）/ a non-sequential system lists object numbers, from 1
        n = len(desc["objects"])
        if env is not None and not (isinstance(env, list) and all(isinstance(i, int) and not isinstance(i, bool) and 1 <= i <= n for i in env)):
            raise SaosError(ErrorCode.BAD_ARGS, f"notes.environment must list object numbers 1–{n} (environment or bought-in parts)")
    elif env is not None and not (isinstance(env, list) and all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(desc["surfaces"]) for i in env)):
        raise SaosError(ErrorCode.BAD_ARGS, f"notes.environment must list surface indices 0–{len(desc['surfaces']) - 1} (environment or bought-in parts)")
    from saos.protocol import write_json

    chosen = i18n.resolve(lang, _demand_lang(job))
    meta = {"job": job.id, "lens": lens, "time": jobs.now_iso()}
    found = checks(desc, notes, materials_record.recorded_names(job))
    if not explorations(job) and not str((notes or {}).get("exploration", "")).strip():
        found.append(("no_exploration", {}))
    d, demand_error = _demand_or_error(job)
    selection, selection_found = _selection(job, d, desc, folder) if d is not None else ({"mode": "unknown"}, [])
    found += selection_found
    if d is not None:
        method = demand.method_digest(demand.prepare_scripts(job.root, d))
        problems: list[dict] = []
        records = model_gate.approvals(job, problems=problems)
        paths = [p["path"] for p in problems] + [r["model"] for r in records if r["legacy_unsealed"]]
        if paths:
            found.append(("approval_evidence", {"paths": ", ".join(paths)}))
        if records and (records[-1].get("evidence_context") or {}).get("method_sha") not in (None, method):
            found.append(("checks_changed", {}))
    if budget is None:
        found.append(("no_next_budget", {}))
    requirements = _requirements(d, measured, demand_error)
    materials = material_rows(job, desc, (measured or {}).get("backend") or desc.get("backend"))
    chosen_row = selection.get("chosen") if selection.get("mode") == "free" else None
    validity = {"numeric": {"applicable": chosen_row is not None, "warnings": (chosen_row or {}).get("numeric_warnings")},
                "unknown": [{"id": r["id"], "reason": r["reason"], "detail": r["detail"]} for r in requirements["rows"]
                            if r["status"] == "unknown" and r["reason"] in MEASURE_FAILURES],
                "materials": materials}
    # 四节的数据：摘要 shown_sha 由它算出，确认时记入 approval.json / the four sections' data; shown_sha digests it and approval records it
    shown = jsonable({"selection": selection, "requirements": requirements, "validity": validity, "budget": budget})
    shown_sha = model_gate.digest(shown)
    unknowns = [{"section": "requirements", "id": r["id"], "reason": r["reason"]} for r in requirements["rows"] if r["status"] == "unknown"]
    if d is None:
        unknowns.append({"section": "requirements", "reason": "not_checked"})
    unknowns += [{"section": "materials", "name": m["name"], "level": level, "reason": cell["note"].get("code")}
                 for m in materials for level, cell in m["levels"].items() if cell["state"] == "unknown"]
    warnings = [i18n.text("en", *TEXT["w." + code]).format(**args) for code, args in found]
    fp = model_gate.fingerprint(desc, (notes or {}).get("environment") or ())
    context = ({"method_sha": demand.method_digest(demand.prepare_scripts(job.root, d)),
                "scan": (selection.get("chosen") or {}).get("scan")} if d is not None else None)
    write_json(folder / "model.json", {**meta, "notes": notes, "warnings": warnings, "fingerprint": fp,
                                        "fingerprint_sha": model_gate.digest(fp), "model": desc,
                                        "approval_schema": 2, "evidence_context": context,
                                        "shown": shown, "shown_sha": shown_sha, "unknowns": unknowns})
    out = folder / "index.html"
    out.write_text(render(desc, notes, meta, chosen, found, shown), encoding="utf-8")
    job.seal(folder)
    return {
        "page": str(out),
        "lang": chosen,
        "warnings": warnings,
        "layout": desc.get("layout"),
        "shown_sha": shown_sha,
        "unknowns": unknowns,
        "next": "show the page to the user; after they approve, run lens approve --model <folder> --quote \"<their words>\"",
    }


def explorations(job: jobs.Job) -> list[str]:
    """任务里的结构探索（mode 为 candidates 的扫描）。/ The job's structure explorations (scans whose mode is candidates)."""
    out = []
    for plan in sorted((job.root / "scans").glob("*/plan.json")) if (job.root / "scans").is_dir() else []:
        try:
            if json.loads(plan.read_text(encoding="utf-8")).get("mode") == "candidates":
                out.append(plan.parent.relative_to(job.root).as_posix())
        except (OSError, ValueError):
            continue
    return out


def _demand_lang(job: jobs.Job) -> str | None:
    path = job.root / "sources" / "demand.yaml"
    if not path.is_file():
        return None
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 语言只是偏好，读不出来就用默认顺序
        return None
    return data.get("lang") if isinstance(data, dict) else None
