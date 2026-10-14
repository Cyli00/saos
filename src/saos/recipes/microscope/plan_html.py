"""单文件设计计划页面；所有外部文字转义，图形只由处方数值生成。"""

from __future__ import annotations

from html import escape
import json
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from saos.recipes.microscope.interview import ITEM_NAMES


def e(value: Any) -> str:
    return escape(str(value), quote=True)


def value_text(value: Any) -> str:
    if value is None:
        return "无盖玻片"
    if value is True:
        return "允许"
    if value is False:
        return "不允许"
    if isinstance(value, float):
        return f"{value:.6g}"
    if isinstance(value, list):
        return " / ".join(value_text(v) for v in value)
    if isinstance(value, dict):
        if set(value) == {"value", "scale"}:
            return f"间隔 {value_text(value['value'])}；差距尺度 {value_text(value['scale'])}"
        if set(value) == {"thickness", "nd", "vd"}:
            return f"厚度 {value_text(value['thickness'])}；nd {value_text(value['nd'])}；νd {value_text(value['vd'])}"
        return json.dumps(value, ensure_ascii=False)
    return {"achromat": "消色差", "plan_achromat": "平场消色差", "visual": "目视", "camera": "相机", "image": "一次像面", "object": "物方"}.get(value, str(value))


def section_svg(prescription: dict[str, Any]) -> str:
    surfaces = prescription.get("surfaces", [])
    x = float(prescription["object"]["thickness"])
    positions = []
    for s in surfaces:
        if s.get("comment") in ("saos:stop", "saos:tube"):
            break
        positions.append((x, s))
        x += float(s.get("thickness", 0))
    if len(positions) < 2:
        return '<p class="notice">此条目没有足够的面数据用于绘制剖面。</p>'
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
        material = value_text(s1["material"])
        paths.append(f'<path class="glass" d="{d}"><title>面 {i + 1} 起，{e(material)}</title></path>')
    return f'''<svg class="section-view" viewBox="0 0 800 250" role="img" aria-labelledby="section-title section-desc">
<title id="section-title">初始结构的处方示意剖面</title>
<desc id="section-desc">从物面向右看向筒镜。轴向位置和曲率来自库中处方，透镜高度为示意，没有进行光线追迹。</desc>
<line class="axis" x1="35" y1="115" x2="765" y2="115"/>
<line class="object-plane" x1="50" y1="55" x2="50" y2="175"/>
{''.join(paths)}<text x="50" y="210">物面</text><text x="625" y="210">向参考筒镜 →</text>
<text x="50" y="235">库中原始尺寸 · 轴向跨度 {end:.3g} mm</text></svg>'''


def render(payload: dict[str, Any], revision: str) -> str:
    result = payload["review"]
    spec = result["spec"]
    starting = payload["starting_design"]
    rows = result["fields"]
    assumption_rows = [r for r in rows if r["source"] == "assumption"]
    def listing(values):
        return "<ul>" + "".join(f"<li>{e(v)}</li>" for v in values) + "</ul>"
    def field_table(selected):
        return '<div class="table-wrap"><table><thead><tr><th scope="col">规格</th><th scope="col">数值与单位</th><th scope="col">含义与来源</th></tr></thead><tbody>' + ''.join(
            f'<tr><th scope="row">{e(r["label"])}</th><td class="numeric">{e(value_text(r["value"]))}<small>{e(r["unit"])}</small></td><td>{e(r["explanation"])}<small><span class="source {r["source"]}">{"用户给出" if r["source"] == "user" else "假设"}</span> {e(r["basis"])}</small></td></tr>'
            for r in selected) + '</tbody></table></div>'
    requirements = ''.join(f'<li><span class="source">{e({"constraint":"约束","target":"性能指标","preference":"期望项","unresolved":"待解决"}[r["kind"]])}</span> {e(r["text"])}'
                           + (f'<small>{e(r["note"])}</small>' if r.get('note') else '') + '</li>' for r in result["requirements"])
    if starting:
        entry = starting["entry"]
        source = entry["source"]
        url = source.get("url", "")
        source_label = e(source.get("patent", entry["id"]))
        if urlsplit(url).scheme in ("http", "https") and urlsplit(url).netloc:
            source_label = f'<a href="{e(url)}" target="_blank" rel="noopener noreferrer">{source_label}</a>'
        structure = f'''<div class="structure-heading"><div><p class="eyebrow">选定的初始结构</p><h3>{e(entry['id'])}</h3></div><p>{source_label}<br>{e(entry.get('embodiment',''))}</p></div>
<p>{e(starting['rationale'])}</p>{section_svg(starting['prescription'])}
<p class="caption">曲率与轴向位置来自库中原处方；通光口径仅作示意，未绘制光线。此图不是按本任务规格重新优化后的结果。</p>
<h3>从这个结构出发，还要处理什么</h3>{listing(starting['adjustments']) if starting['adjustments'] else '<p>查询未发现已检查参数的明显差异；像质及完整约束仍须按本任务规格验收。</p>'}
<details><summary>原始资料中的假设与疑点</summary>{listing(entry.get('assumptions',[])+entry.get('doubts',[]))}</details>'''
    else:
        structure = '<p class="notice">尚未选定初始结构。本计划目前不能进入自动优化；请在对话中选择已收录结构，或决定是否先将候选专利离线入库。</p>'
        structure += listing([f"{c['id']}：NA {c['na']}，{c['elements']} 片；" + ('；'.join(c['notes']) or '仍需按用户规格评估') for c in payload['candidates']]) if payload['candidates'] else '<p>结构库中没有可列出的已收录候选。</p>'
    thresholds = []
    for item in payload["acceptance_config"]["items"]:
        label = ITEM_NAMES.get(item["id"], item["id"])
        if item["id"] == "plan_field":
            bound = '平场比 ≥ ' + value_text(item['params']['ratio'])
        else:
            bound = ('≥ ' if item['kind'] == 'lower' else '≤ ') + value_text(item['threshold']) + ' ' + item['unit']
        detail = ''
        if 'range' in item['params']:
            detail += f"检查至相对视场半径 {item['params']['range']:.3g}。"
        if item['id'] == 'mtf':
            detail += f"一次像面频率 {item['params']['frequency']:.6g} lp/mm。"
        if item['id'] == 'field_rms_wfe':
            detail += '平场比已启用，扣除离焦和像散。' if item['params']['remove_defocus_astig'] else '计入固定焦面上的总波像差。'
        thresholds.append(f'<tr><th scope="row">{e(label)}</th><td>{e(bound)}</td><td>{e(detail)}</td></tr>')
    css = Path(__file__).with_name('plan.css').read_text(encoding='utf-8')
    state = '待你审阅' if starting else '等待初始结构'
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'">
<title>设计计划 · {e(payload['job'])}</title><style>{css}</style></head><body>
<a class="skip" href="#content">跳到设计计划</a>
<header><a class="brand" href="#content">saos <span>显微物镜设计</span></a><span class="status">{state}</span></header>
<main id="content"><section class="intro"><p class="eyebrow">设计计划 / 审阅副本</p>
<h1>{e(value_text(spec['magnification']))}× {e(value_text(spec['correction_class']))}物镜</h1>
<p class="lead">{e(result['request'])}</p>
<div class="facts"><div><small>数值孔径</small><strong>{e(value_text(spec['na']))}</strong></div><div><small>物方视场直径</small><strong>{result['derived']['object_field_diameter_mm']:.3g} <span>mm</span></strong></div><div><small>最小工作距离</small><strong>{spec['limits']['working_distance_min']:.3g} <span>mm</span></strong></div><div><small>观察方式</small><strong>{e(value_text(spec['receiver']))}</strong></div></div>
<p class="notice">请核对下方 {len(assumption_rows)} 项假设，以及初始结构需要调整的地方。页面仅供审阅；回到对话中确认这个版本，才进入后续设计。</p>
<nav aria-label="设计计划章节"><a href="#needs">用户需求</a><a href="#assumptions">假设</a><a href="#structure">初始结构</a><a href="#spec">光学规格</a><a href="#acceptance">验收口径</a></nav></section>
<section id="needs"><h2>你希望得到什么</h2><ul class="requirements">{requirements}</ul><p class="caption">期望项会在报告中说明满足程度，不参与是否通过的判定。</p></section>
<section id="assumptions"><h2>需要你确认的假设 <span>{len(assumption_rows)} 项</span></h2><p>以下数值并非用户明确给出；可在对话中逐项修改，再生成新版计划。</p>
<ul class="assumptions">{''.join(f'<li><strong>{e(r["label"])}：{e(value_text(r["value"]))} {e(r["unit"])}</strong><p>{e(r["basis"])}</p></li>' for r in assumption_rows)}</ul>
<p class="notice">视场像质、倍率色差、MTF、长度、口径和最小厚度中的部分初始阈值仍待标定。它们是本次约定的要求，不是对最终性能的保证。</p></section>
<section id="structure"><h2>从哪个结构开始</h2>{structure}</section>
<section id="spec"><h2>光学规格</h2><h3>工作条件</h3>{field_table([r for r in rows if not r['path'].startswith(('limits.','targets.'))])}
<h3>结构界限</h3>{field_table([r for r in rows if r['path'].startswith('limits.')])}<h3>性能指标设置</h3>{field_table([r for r in rows if r['path'].startswith('targets.')])}</section>
<section id="acceptance"><h2>怎样判定完成</h2><p>在固定验收焦面，按下面的约束和性能指标逐项检查。全部通过时规格差距 G = 0；无法可靠评估时不会判为通过。优化中不会自行放宽这些要求。</p>
<div class="table-wrap"><table><thead><tr><th scope="col">检查项</th><th scope="col">达标线</th><th scope="col">范围与说明</th></tr></thead><tbody>{''.join(thresholds)}</tbody></table></div>
<p class="caption">第一版范围：干物镜、无限远校正、球面或平面，评估使用理想参考筒镜。机械齐焦兼容性和公差分析不在本次验收内；不会保证当前初始结构已经满足以上要求。</p></section>
<section class="confirmation"><h2>回到对话中确认</h2><p>认可时说明“确认这个版本”；需要修改时直接指出字段或描述。确认后保留本版本，后续要求变化另建任务。</p><p class="revision">版本 {e(revision)}</p><p class="caption">任务 {e(payload['job'])} · 生成时间 {e(payload['created_at'])}。此页面是生成时的审阅副本，当前确认状态以对话中的记录为准。</p></section></main>
<footer>saos · 从用户需求到可检验的光学规格</footer></body></html>'''
