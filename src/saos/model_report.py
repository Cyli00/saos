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

import math
from pathlib import Path
from typing import Any

from saos import i18n, jobs, materials_record, model_gate
from saos.protocol import ErrorCode, SaosError
from saos.report import CSS, e, fmt, header_row

TEXT: dict[str, tuple[str, str]] = {
    "title": ("建模说明", "Model description"),
    "byline": ("任务 {job} · 镜头 {lens} · 生成于 {time}", "Job {job} · lens {lens} · generated {time}"),
    "intro": (
        "下面的数值都直接从 OpticStudio 读取，不是手写的。开始优化前请核对：每个面代表什么实物、用的什么材料、按什么光阑、视场和波长计算。",
        "Every value below is read from OpticStudio, not typed by hand. Before optimisation starts, check what each surface stands for, "
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
    "w.uncatalogued": ("第 {s} 面的材料 {name} 不在在用的材料目录里。", "Surface {s}: material {name} is not in the catalogs in use."),
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
}


class Tr:
    def __init__(self, lang: str) -> None:
        self.lang = lang

    def __call__(self, key: str, **kw: Any) -> str:
        return i18n.text(self.lang, *TEXT[key]).format(**kw)


OBJECT_APERTURES = ("ObjectSpaceNA", "ObjectConeAngle")


def _flat(radius: float) -> bool:
    return math.isinf(radius) or abs(radius) > 1e9


def environment_surfaces(notes: dict[str, Any] | None) -> list[int]:
    """notes 里声明为环境或外购件的面（样品、浸没介质、买来的管子等），只有要制造的零件受模型玻璃的交付限制。
    Surfaces the notes declare as environment or bought-in parts (sample, immersion medium, a bought tube);
    only the parts this design makes are bound by the model-glass delivery rule."""
    return [int(i) for i in (notes or {}).get("environment") or []]


def checks(desc: dict[str, Any], notes: dict[str, Any] | None = None, recorded: set[str] | None | bool = False) -> list[tuple[str, dict[str, Any]]]:
    """确定性的建模检查，返回 (代码, 参数)。recorded：材料来源记录里的牌号；None 表示没有记录文件，False 表示不检查。
    Deterministic model checks, returning (code, arguments). recorded: names in the material source record;
    None means there is no record file, False skips the check."""
    out: list[tuple[str, dict[str, Any]]] = []
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
    # LDE 的注释只是名字；说明要写清这个面代表什么实物，所以每个面都要在 notes 里有一条
    # an LDE comment is only a name; the notes must say what each surface physically is
    missing = [str(s["index"]) for s in surfaces if not str(((notes or {}).get("surfaces") or {}).get(str(s["index"]), "")).strip()]
    if missing:
        out.append(("undescribed", {"list": ", ".join(missing)}))
    if not (notes or {}).get("first_order"):
        out.append(("no_first_order", {}))
    return out


def _source_text(src: dict[str, Any], t: Tr) -> tuple[str, str]:
    kind = src.get("kind")
    if kind == "model":
        return t("src.model"), f"nd {fmt(src.get('nd'))}, Vd {fmt(src.get('vd'))}, ΔPgF {fmt(src.get('dpgf'))}"
    if kind == "catalog":
        cats = src.get("catalogs") or []
        return (t("src.catalog", cats=", ".join(cats)) if cats else t("src.uncatalogued")), ""
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


def render(desc: dict[str, Any], notes: dict[str, Any] | None, meta: dict[str, Any], lang: str,
           found: list[tuple[str, dict[str, Any]]] | None = None) -> str:
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
        if s.get("params"):
            vals = "; ".join(f"{p['name']} = {fmt(p['value'])}" + ("" if p.get("solve") in (None, "Fixed") else f" ({p['solve']})") for p in s["params"])
            param_rows.append(f"<tr><td class='num'>{i}</td><td>{e(s['type'])}</td><td>{e(vals)}</td></tr>")
    layouts = []
    for key, value in (desc.get("layout") or {}).items():
        if isinstance(value, str):
            layouts.append(f"<h3>{e(key)}</h3><p><img src='{e(value)}' alt='{e(key)}'></p>")
        else:
            layouts.append(f"<h3>{e(key)}</h3><p class='muted'>{e(t('layout_missing', err=value.get('error', '')))}</p>")
    assumptions = "".join(f"<li>{e(a)}</li>" for a in notes.get("assumptions") or []) or f"<li>{t('none')}</li>"
    first_order = "".join(f"<li>{e(a)}</li>" for a in notes.get("first_order") or []) or f"<li>{t('none')}</li>"
    params_html = (
        f"<h2>{t('h.params')}</h2><div class='wrap'><table><tbody>{''.join(param_rows)}</tbody></table></div>" if param_rows else ""
    )
    return f"""<!doctype html><html lang="{'zh-CN' if lang == 'zh' else 'en'}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{t('title')} · {e(meta['job'])}</title><style>{CSS}</style></head><body><main>
<p class="muted">{t('byline', job=e(meta['job']), lens=e(meta['lens']), time=e(meta['time']))}</p>
<h1>{t('title')}</h1><p>{t('intro')}</p>
<h2>{t('h.warnings')}</h2><div class="card">{f'<ul>{warnings}</ul>' if warnings else t('no_warnings')}</div>
<h2>{t('h.system')}</h2><div class="wrap"><table><tbody>{system}</tbody></table></div>
<h3>{t('h.fields')}</h3><p class="muted">{e(t('field_type', type=desc['fields']['type'], norm=desc['fields'].get('normalization', '—')))}</p>
<div class="wrap"><table><thead><tr>{header_row(t('th.fields'))}</tr></thead><tbody>{fields}</tbody></table></div>
<h3>{t('h.waves')}</h3><div class="wrap"><table><thead><tr>{header_row(t('th.waves'))}</tr></thead><tbody>{wave_rows}</tbody></table></div>
<h2>{t('h.materials')}</h2><div class="wrap"><table><thead><tr>{header_row(t('th.materials', waves=wave_heads))}</tr></thead><tbody>{''.join(mat_rows)}</tbody></table></div>
<p class="muted">{t('n_note')}</p>
<h2>{t('h.surfaces')}</h2><div class="wrap"><table><thead><tr>{header_row(t('th.surfaces'))}</tr></thead><tbody>{''.join(surf_rows)}</tbody></table></div>
<p class="muted">{t('thick_note')}</p>
{params_html}
<h2>{t('h.first_order')}</h2><ul>{first_order}</ul>
<h2>{t('h.assumptions')}</h2><ul>{assumptions}</ul>
<h2>{t('h.layout')}</h2>{''.join(layouts)}
</main></body></html>"""


def write(job: jobs.Job, desc: dict[str, Any], folder: Path, notes: dict[str, Any] | None, lens: str, lang: str | None) -> dict[str, Any]:
    env = (notes or {}).get("environment")
    if env is not None and not (isinstance(env, list) and all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(desc["surfaces"]) for i in env)):
        raise SaosError(ErrorCode.BAD_ARGS, f"notes.environment must list surface indices 0–{len(desc['surfaces']) - 1} (environment or bought-in parts)")
    from saos.protocol import write_json

    chosen = i18n.resolve(lang, _demand_lang(job))
    meta = {"job": job.id, "lens": lens, "time": jobs.now_iso()}
    found = checks(desc, notes, materials_record.recorded_names(job))
    warnings = [i18n.text("en", *TEXT["w." + code]).format(**args) for code, args in found]
    fp = model_gate.fingerprint(desc)
    write_json(folder / "model.json", {**meta, "notes": notes, "warnings": warnings, "fingerprint": fp,
                                        "fingerprint_sha": model_gate.digest(fp), "model": desc})
    out = folder / "index.html"
    out.write_text(render(desc, notes, meta, chosen, found), encoding="utf-8")
    job.seal(folder)
    return {
        "page": str(out),
        "lang": chosen,
        "warnings": warnings,
        "layout": desc.get("layout"),
        "next": "show the page to the user; after they approve, run lens approve --model <folder> --quote \"<their words>\"",
    }


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
