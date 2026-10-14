"""单文件设计计划页面，中英各一份；所有外部文字转义，图形只由处方数值生成。"""

from __future__ import annotations

from html import escape
import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from saos.recipes.microscope import library
from saos.recipes.microscope.interview import DEFAULT_BASES, FALLBACK_BASIS, FIELDS, ITEM_NAMES

PAGES = {"zh": "index.zh.html", "en": "index.en.html"}

T = {
    "zh": {
        "html_lang": "zh-CN", "other": "English", "no_cover": "无盖玻片", "yes": "允许", "no": "不允许",
        "gap": "间隔 {v}；差距尺度 {s}", "cover": "厚度 {t}；nd {nd}；νd {vd}",
        "values": {"achromat": "消色差", "plan_achromat": "平场消色差", "visual": "目视", "camera": "相机", "image": "一次像面", "object": "物方"},
        "no_section": "此条目没有足够的面数据用于绘制剖面。", "glass_from": "面 {i} 起，{m}",
        "svg_title": "初始结构的处方示意剖面",
        "svg_desc": "从物面向右看向筒镜。轴向位置和曲率来自库中处方，透镜高度为示意，没有进行光线追迹。",
        "object_plane": "物面", "to_tube": "向参考筒镜 →", "svg_scale": "库中原始尺寸 · 轴向跨度 {end} mm",
        "th_spec": "规格", "th_value": "数值与单位", "th_meaning": "含义与来源", "from_user": "用户给出", "assumed": "假设",
        "kinds": {"constraint": "约束", "target": "性能指标", "preference": "期望项", "unresolved": "待解决"},
        "chosen": "选定的初始结构",
        "caption_svg": "曲率与轴向位置来自库中原处方；通光口径仅作示意，未绘制光线。此图不是按本任务规格重新优化后的结果。",
        "todo": "从这个结构出发，还要处理什么",
        "no_todo": "查询未发现已检查参数的明显差异；像质及完整约束仍须按本任务规格验收。",
        "doubts": "原始资料中的假设与疑点",
        "no_structure": "尚未选定初始结构。本计划目前不能进入自动优化；请在对话中选择已收录结构，或决定是否先将候选专利离线入库。",
        "candidate": "{id}：NA {na}，{n} 片；{notes}", "candidate_sep": "；", "candidate_none": "仍需按用户规格评估",
        "no_candidates": "结构库中没有可列出的已收录候选。",
        "plan_ratio": "平场比 ≥ {r}", "range": "检查至相对视场半径 {r}。", "mtf_freq": "一次像面频率 {f} lp/mm。",
        "astig_removed": "平场比已启用，扣除离焦和像散。", "astig_kept": "计入固定焦面上的总波像差。",
        "units": {"µm object side": "µm 物方", "µm image": "µm 像面", "× δ′": "δ′ 份数", "items": "件", "elements": "片", "ratio": "比例"},
        "state_review": "待你审阅", "state_structure": "等待初始结构",
        "title": "设计计划 · {job}", "skip": "跳到设计计划", "brand": "显微物镜设计", "eyebrow": "设计计划 / 审阅副本",
        "heading": "{m}× {c}物镜",
        "f_na": "数值孔径", "f_field": "物方视场直径", "f_wd": "最小工作距离", "f_receiver": "观察方式",
        "check_notice": "请核对下方 {n} 项假设，以及初始结构需要调整的地方。页面仅供审阅；回到对话中确认这个版本，才进入后续设计。",
        "nav": "设计计划章节", "nav_needs": "用户需求", "nav_assumptions": "假设", "nav_structure": "初始结构", "nav_spec": "光学规格", "nav_acceptance": "验收口径",
        "h_needs": "你希望得到什么", "preference_note": "期望项会在报告中说明满足程度，不参与是否通过的判定。",
        "h_assumptions": "需要你确认的假设", "count": "{n} 项", "assumptions_intro": "以下数值并非用户明确给出；可在对话中逐项修改，再生成新版计划。",
        "assumption": "{label}：{value} {unit}",
        "calibration": "视场像质、倍率色差、MTF、长度、口径和最小厚度中的部分初始阈值仍待标定。它们是本次约定的要求，不是对最终性能的保证。",
        "h_structure": "从哪个结构开始", "h_spec": "光学规格", "h_conditions": "工作条件", "h_limits": "结构界限", "h_targets": "性能指标设置",
        "h_acceptance": "怎样判定完成",
        "acceptance_intro": "在固定验收焦面，按下面的约束和性能指标逐项检查。全部通过时规格差距 G = 0；无法可靠评估时不会判为通过。优化中不会自行放宽这些要求。",
        "th_item": "检查项", "th_bound": "达标线", "th_range": "范围与说明",
        "scope": "第一版范围：干物镜、无限远校正、球面或平面，评估使用理想参考筒镜。机械齐焦兼容性和公差分析不在本次验收内；不会保证当前初始结构已经满足以上要求。",
        "h_confirm": "回到对话中确认", "confirm": "认可时说明“确认这个版本”；需要修改时直接指出字段或描述。确认后保留本版本，后续要求变化另建任务。",
        "revision": "版本 {r}", "footer_meta": "任务 {job} · 生成时间 {time}。此页面是生成时的审阅副本，当前确认状态以对话中的记录为准。",
        "footer": "saos · 从用户需求到可检验的光学规格",
        "adj": {
            "optical_length": "按焦距缩放的光学长度估计超过规格上限，需要优化；该估计尚未重新追迹。",
            "clear_diameter": "按焦距缩放的通光直径估计超过规格上限，需要重新评估。",
            "cover_glass": "库中盖玻片条件与目标规格不同，需要重新适配并验收。",
            "glass": "需处理不符合目标目录或状态的玻璃：{m}。选择此结构不会自动放宽玻璃约束。", "glass_sep": "、",
        },
    },
    "en": {
        "html_lang": "en", "other": "中文", "no_cover": "no cover glass", "yes": "allowed", "no": "not allowed",
        "gap": "gap {v}; shortfall scale {s}", "cover": "thickness {t}; nd {nd}; νd {vd}",
        "values": {"achromat": "achromat", "plan_achromat": "plan achromat", "visual": "visual", "camera": "camera", "image": "intermediate image", "object": "object side"},
        "no_section": "This entry does not have enough surface data to draw a section.", "glass_from": "from surface {i}, {m}",
        "svg_title": "Schematic section of the starting design",
        "svg_desc": "From the object plane towards the tube lens. Axial positions and curvatures come from the library prescription; lens heights are schematic and no rays were traced.",
        "object_plane": "object", "to_tube": "to reference tube lens →", "svg_scale": "library scale · axial span {end} mm",
        "th_spec": "Spec", "th_value": "Value and unit", "th_meaning": "Meaning and source", "from_user": "from the user", "assumed": "assumption",
        "kinds": {"constraint": "constraint", "target": "target", "preference": "preference", "unresolved": "unresolved"},
        "chosen": "Chosen starting design",
        "caption_svg": "Curvatures and axial positions come from the library prescription; apertures are schematic and no rays are drawn. This is not the design re-optimised for this job's spec.",
        "todo": "What still needs work from this structure",
        "no_todo": "The query found no clear difference in the checked parameters; image quality and all constraints still need acceptance against this job's spec.",
        "doubts": "Assumptions and doubts in the source",
        "no_structure": "No starting design is chosen yet, so this plan cannot enter automatic optimisation. Choose an admitted structure in the conversation, or decide whether to admit a candidate patent offline first.",
        "candidate": "{id}: NA {na}, {n} elements; {notes}", "candidate_sep": "; ", "candidate_none": "still to be evaluated against the user's spec",
        "no_candidates": "The library has no admitted candidates to list.",
        "plan_ratio": "flatness ratio ≥ {r}", "range": "Checked to relative field radius {r}. ", "mtf_freq": "Intermediate-image frequency {f} lp/mm. ",
        "astig_removed": "Flatness ratio enabled; defocus and astigmatism removed.", "astig_kept": "Total wavefront error on the fixed focal plane.",
        "state_review": "awaiting your review", "state_structure": "awaiting a starting design",
        "title": "Design plan · {job}", "skip": "Skip to the design plan", "brand": "microscope objective design", "eyebrow": "Design plan / review copy",
        "heading": "{m}× {c} objective",
        "f_na": "Numerical aperture", "f_field": "Object field diameter", "f_wd": "Minimum working distance", "f_receiver": "Receiver",
        "check_notice": "Check the {n} assumptions below and what the starting design still needs. This page is for review only; confirm this revision in the conversation before design continues.",
        "nav": "Design plan sections", "nav_needs": "Needs", "nav_assumptions": "Assumptions", "nav_structure": "Starting design", "nav_spec": "Optical spec", "nav_acceptance": "Acceptance",
        "h_needs": "What you asked for", "preference_note": "Preferences are reported with how well they are met; they do not decide pass or fail.",
        "h_assumptions": "Assumptions to confirm", "count": "{n}", "assumptions_intro": "These values were not given by the user. Change any of them in the conversation and a new plan will be created.",
        "assumption": "{label}: {value} {unit}",
        "calibration": "Some initial thresholds for field image quality, lateral color, MTF, length, aperture and minimum thickness are still to be calibrated. They are the requirements agreed here, not a promise of final performance.",
        "h_structure": "Where the design starts", "h_spec": "Optical spec", "h_conditions": "Working conditions", "h_limits": "Structural limits", "h_targets": "Target settings",
        "h_acceptance": "How completion is judged",
        "acceptance_intro": "Each constraint and target below is checked on the fixed acceptance focal plane. The spec shortfall G = 0 when all pass; anything that cannot be evaluated reliably does not pass. Optimisation never relaxes these requirements on its own.",
        "th_item": "Check", "th_bound": "Pass line", "th_range": "Range and notes",
        "scope": "First-version scope: dry, infinity-corrected objectives with spherical or flat surfaces, evaluated with an ideal reference tube lens. Mechanical parfocal compatibility and tolerancing are outside this acceptance; the current starting design is not guaranteed to meet the requirements above.",
        "h_confirm": "Confirm in the conversation", "confirm": "To approve, say “confirm this revision”; to change something, name the field or describe the change. A confirmed revision is kept; later changes in requirements start a new job.",
        "revision": "Revision {r}", "footer_meta": "Job {job} · created {time}. This page is a review copy from creation time; the conversation holds the current confirmation state.",
        "footer": "saos · from user needs to a checkable optical spec",
        "adj": {
            "optical_length": "The optical length estimated by focal-length scaling exceeds the spec limit and needs optimisation; the estimate has not been re-traced.",
            "clear_diameter": "The clear diameter estimated by focal-length scaling exceeds the spec limit and needs re-evaluation.",
            "cover_glass": "The library's cover glass differs from the target spec; the design needs re-adapting and acceptance.",
            "glass": "Glasses outside the target catalogs or statuses need handling: {m}. Choosing this structure does not relax the glass constraints.", "glass_sep": ", ",
        },
    },
}


def e(value: Any) -> str:
    return escape(str(value), quote=True)


def value_text(value: Any, lang: str) -> str:
    t = T[lang]
    if value is None:
        return t["no_cover"]
    if value is True:
        return t["yes"]
    if value is False:
        return t["no"]
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, list):
        return " / ".join(value_text(v, lang) for v in value)
    if isinstance(value, dict):
        if set(value) == {"value", "scale"}:
            return t["gap"].format(v=value_text(value["value"], lang), s=value_text(value["scale"], lang))
        if set(value) == {"thickness", "nd", "vd"}:
            return t["cover"].format(t=value_text(value["thickness"], lang), nd=value_text(value["nd"], lang), vd=value_text(value["vd"], lang))
        return json.dumps(value, ensure_ascii=False)
    return t["values"].get(value, str(value))


def adjustment_text(flag: dict[str, Any], lang: str) -> str:
    adj = T[lang]["adj"]
    if flag["kind"] == "glass":
        return adj["glass"].format(m=adj["glass_sep"].join(flag["materials"]))
    if flag["kind"] in adj:
        return adj[flag["kind"]]
    return library.flag_text(flag, lang)


def section_svg(prescription: dict[str, Any], lang: str) -> str:
    t = T[lang]
    surfaces = prescription.get("surfaces", [])
    x = float(prescription["object"]["thickness"])
    positions = []
    for s in surfaces:
        if s.get("comment") in ("saos:stop", "saos:tube"):
            break
        positions.append((x, s))
        x += float(s.get("thickness", 0))
    if len(positions) < 2:
        return f'<p class="notice">{t["no_section"]}</p>'
    glasses = [(i, z, s, positions[i + 1][0], positions[i + 1][1]) for i, (z, s) in enumerate(positions[:-1]) if s.get("material")]
    end = max(z for z, _ in positions)
    scale = 700 / max(end, 1)
    height = min(6.0, max(end, 1) / 10)
    def radius(s):
        return float(s.get("radius", "inf"))
    def sag(r, y):
        return 0.0 if not math.isfinite(r) or r == 0 else y * y / (r * (1 + math.sqrt(max(0, 1 - (y / r) ** 2))))
    paths = []
    for i, z1, s1, z2, s2 in glasses:
        r1, r2 = radius(s1), radius(s2)
        h = min([height] + [abs(r) * 0.6 for r in (r1, r2) if math.isfinite(r) and r != 0])
        # 原处方没有逐面通光半口径；示意高度缩小到曲面不相交，不冒充实际光线包络。
        for _ in range(20):
            if z1 + sag(r1, h) < z2 + sag(r2, h):
                break
            h *= 0.8
        points = []
        for z, r, order in ((z1, r1, range(25)), (z2, r2, range(24, -1, -1))):
            for k in order:
                y = h * (2 * k / 24 - 1)
                points.append((50 + (z + sag(r, y)) * scale, 115 - y * scale))
        d = "M " + " L ".join(f"{a:.2f},{b:.2f}" for a, b in points) + " Z"
        material = value_text(s1["material"], lang)
        paths.append(f'<path class="glass" d="{d}"><title>{e(t["glass_from"].format(i=i + 1, m=material))}</title></path>')
    return f'''<svg class="section-view" viewBox="0 0 800 250" role="img" aria-labelledby="section-title section-desc">
<title id="section-title">{t["svg_title"]}</title>
<desc id="section-desc">{t["svg_desc"]}</desc>
<line class="axis" x1="35" y1="115" x2="765" y2="115"/>
<line class="object-plane" x1="50" y1="55" x2="50" y2="175"/>
{''.join(paths)}<text x="50" y="210">{t["object_plane"]}</text><text x="625" y="210">{t["to_tube"]}</text>
<text x="50" y="235">{t["svg_scale"].format(end=f"{end:.3g}")}</text></svg>'''


def render(payload: dict[str, Any], revision: str, lang: str) -> str:
    t = T[lang]
    result = payload["review"]
    spec = result["spec"]
    starting = payload["starting_design"]
    rows = result["fields"]
    assumption_rows = [r for r in rows if r["source"] == "assumption"]

    def vt(value):
        return value_text(value, lang)

    def field(r):
        # 自动补全的假设按语言取依据；用户或 agent 写的依据保持原文
        label, unit, explanation = FIELDS[lang][r["path"]]
        basis = DEFAULT_BASES[lang].get(r["path"], FALLBACK_BASIS[lang]) if r.get("default") else r["basis"]
        return label, unit, explanation, basis

    def listing(values):
        return "<ul>" + "".join(f"<li>{e(v)}</li>" for v in values) + "</ul>"

    def field_table(selected):
        body = []
        for r in selected:
            label, unit, explanation, basis = field(r)
            source = t["from_user"] if r["source"] == "user" else t["assumed"]
            body.append(f'<tr><th scope="row">{e(label)}</th><td class="numeric">{e(vt(r["value"]))}<small>{e(unit)}</small></td>'
                        f'<td>{e(explanation)}<small><span class="source {r["source"]}">{source}</span> {e(basis)}</small></td></tr>')
        return (f'<div class="table-wrap"><table><thead><tr><th scope="col">{t["th_spec"]}</th><th scope="col">{t["th_value"]}</th>'
                f'<th scope="col">{t["th_meaning"]}</th></tr></thead><tbody>' + "".join(body) + "</tbody></table></div>")

    requirements = "".join(f'<li><span class="source">{e(t["kinds"][r["kind"]])}</span> {e(r["text"])}'
                           + (f'<small>{e(r["note"])}</small>' if r.get("note") else "") + "</li>" for r in result["requirements"])
    if starting:
        entry = starting["entry"]
        source = entry["source"]
        url = source.get("url", "")
        source_label = e(source.get("patent", entry["id"]))
        if urlsplit(url).scheme in ("http", "https") and urlsplit(url).netloc:
            source_label = f'<a href="{e(url)}" target="_blank" rel="noopener noreferrer">{source_label}</a>'
        adjustments = [adjustment_text(f, lang) for f in starting["adjustments"]]
        structure = f'''<div class="structure-heading"><div><p class="eyebrow">{t["chosen"]}</p><h3>{e(entry['id'])}</h3></div><p>{source_label}<br>{e(entry.get('embodiment',''))}</p></div>
<p>{e(starting['rationale'])}</p>{section_svg(starting['prescription'], lang)}
<p class="caption">{t["caption_svg"]}</p>
<h3>{t["todo"]}</h3>{listing(adjustments) if adjustments else f'<p>{t["no_todo"]}</p>'}
<details><summary>{t["doubts"]}</summary>{listing(entry.get('assumptions',[])+entry.get('doubts',[]))}</details>'''
    else:
        structure = f'<p class="notice">{t["no_structure"]}</p>'
        candidates = [t["candidate"].format(id=c["id"], na=c["na"], n=c["elements"],
                                            notes=t["candidate_sep"].join(library.flag_text(f, lang) for f in c["flags"]) or t["candidate_none"])
                      for c in payload["candidates"]]
        structure += listing(candidates) if candidates else f'<p>{t["no_candidates"]}</p>'
    thresholds = []
    for item in payload["acceptance_config"]["items"]:
        label = ITEM_NAMES[lang].get(item["id"], item["id"])
        if item["id"] == "plan_field":
            bound = t["plan_ratio"].format(r=vt(item["params"]["ratio"]))
        else:
            unit = t.get("units", {}).get(item["unit"], item["unit"])
            bound = ("≥ " if item["kind"] == "lower" else "≤ ") + vt(item["threshold"]) + " " + unit
        detail = ""
        if "range" in item["params"]:
            detail += t["range"].format(r=f"{item['params']['range']:.3g}")
        if item["id"] == "mtf":
            detail += t["mtf_freq"].format(f=f"{item['params']['frequency']:.6g}")
        if item["id"] == "field_rms_wfe":
            detail += t["astig_removed"] if item["params"]["remove_defocus_astig"] else t["astig_kept"]
        thresholds.append(f'<tr><th scope="row">{e(label)}</th><td>{e(bound)}</td><td>{e(detail.strip())}</td></tr>')
    css = Path(__file__).with_name("plan.css").read_text(encoding="utf-8")
    state = t["state_review"] if starting else t["state_structure"]
    other = "en" if lang == "zh" else "zh"
    sections = (("needs", "nav_needs"), ("assumptions", "nav_assumptions"), ("structure", "nav_structure"),
                ("spec", "nav_spec"), ("acceptance", "nav_acceptance"))
    assumptions = "".join(f'<li><strong>{e(t["assumption"].format(label=label, value=vt(r["value"]), unit=unit))}</strong><p>{e(basis)}</p></li>'
                          for r in assumption_rows for label, unit, _, basis in [field(r)])
    return f'''<!doctype html>
<html lang="{t["html_lang"]}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
<title>{e(t["title"].format(job=payload['job']))}</title><style>{css}</style></head><body>
<a class="skip" href="#content">{t["skip"]}</a>
<header><a class="brand" href="#content">saos <span>{t["brand"]}</span></a><span class="status">{state}</span><a class="lang" href="{PAGES[other]}" hreflang="{T[other]["html_lang"]}">{t["other"]}</a></header>
<main id="content"><section class="intro"><p class="eyebrow">{t["eyebrow"]}</p>
<h1>{e(t["heading"].format(m=vt(spec['magnification']), c=vt(spec['correction_class'])))}</h1>
<p class="lead">{e(result['request'])}</p>
<div class="facts"><div><small>{t["f_na"]}</small><strong>{e(vt(spec['na']))}</strong></div><div><small>{t["f_field"]}</small><strong>{result['derived']['object_field_diameter_mm']:.3g} <span>mm</span></strong></div><div><small>{t["f_wd"]}</small><strong>{spec['limits']['working_distance_min']:.3g} <span>mm</span></strong></div><div><small>{t["f_receiver"]}</small><strong>{e(vt(spec['receiver']))}</strong></div></div>
<p class="notice">{t["check_notice"].format(n=len(assumption_rows))}</p>
<nav aria-label="{t["nav"]}">{"".join(f'<a href="#{anchor}">{t[key]}</a>' for anchor, key in sections)}</nav></section>
<section id="needs"><h2>{t["h_needs"]}</h2><ul class="requirements">{requirements}</ul><p class="caption">{t["preference_note"]}</p></section>
<section id="assumptions"><h2>{t["h_assumptions"]} <span>{t["count"].format(n=len(assumption_rows))}</span></h2><p>{t["assumptions_intro"]}</p>
<ul class="assumptions">{assumptions}</ul>
<p class="notice">{t["calibration"]}</p></section>
<section id="structure"><h2>{t["h_structure"]}</h2>{structure}</section>
<section id="spec"><h2>{t["h_spec"]}</h2><h3>{t["h_conditions"]}</h3>{field_table([r for r in rows if not r['path'].startswith(('limits.','targets.'))])}
<h3>{t["h_limits"]}</h3>{field_table([r for r in rows if r['path'].startswith('limits.')])}<h3>{t["h_targets"]}</h3>{field_table([r for r in rows if r['path'].startswith('targets.')])}</section>
<section id="acceptance"><h2>{t["h_acceptance"]}</h2><p>{t["acceptance_intro"]}</p>
<div class="table-wrap"><table><thead><tr><th scope="col">{t["th_item"]}</th><th scope="col">{t["th_bound"]}</th><th scope="col">{t["th_range"]}</th></tr></thead><tbody>{''.join(thresholds)}</tbody></table></div>
<p class="caption">{t["scope"]}</p></section>
<section class="confirmation"><h2>{t["h_confirm"]}</h2><p>{t["confirm"]}</p><p class="revision">{e(t["revision"].format(r=revision))}</p><p class="caption">{e(t["footer_meta"].format(job=payload['job'], time=payload['created_at']))}</p></section></main>
<footer>{t["footer"]}</footer></body></html>'''
