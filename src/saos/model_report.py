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
        "没有一片镜片的外缘大于净口径：交叠检查和边厚只按净口径算，没算装配余量。按 design-practice.md 表 17-1 给各镜片面设机械半口径"
        "（lens set --param mech_semi_diameter），检查才对应实物零件；优化后才加余量，正透镜可能没有边厚。",
        "No lens has an outer edge beyond its clear aperture: the interference check and edge thicknesses cover clear apertures only, "
        "without a mounting allowance. Set the lens faces' mechanical semi-diameters (lens set --param mech_semi_diameter) per table 17-1 "
        "in design-practice.md so the check sees the real parts; an allowance added after optimising can leave a positive lens without edge.",
    ),
    "w.thin_edge": (
        "第 {a}–{b} 面之间的玻璃在外缘附近最薄 {min}（离轴 {r} 处，外径 {d}），低于 design-practice.md 表 17-2 正透镜最小边厚 {need}。",
        "The glass between surfaces {a} and {b} is thinnest near its edge, {min} ({r} from the axis, outer diameter {d}), below the "
        "minimum positive-lens edge {need} of table 17-2 in design-practice.md.",
    ),
    "w.thin_centre": (
        "第 {a}–{b} 面之间的玻璃在中心附近最薄 {min}（离轴 {r} 处，外径 {d}），低于 design-practice.md 表 17-2 负透镜最小中心厚 {need}。",
        "The glass between surfaces {a} and {b} is thinnest near its centre, {min} ({r} from the axis, outer diameter {d}), below the "
        "minimum negative-lens centre {need} of table 17-2 in design-practice.md.",
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
}


class Tr:
    def __init__(self, lang: str) -> None:
        self.lang = lang

    def __call__(self, key: str, **kw: Any) -> str:
        return i18n.text(self.lang, *TEXT[key]).format(**kw)


OBJECT_APERTURES = ("ObjectSpaceNA", "ObjectConeAngle")


def _flat(radius: float) -> bool:
    return math.isinf(radius) or abs(radius) > 1e9


# design-practice.md 表 17-2 的下限：(外径上限 mm, 正透镜最小边厚, 负透镜最小中心厚)；外径 3 mm 以下表里没有
# lower bounds of table 17-2 in design-practice.md: (outer diameter up to, mm; min edge of a positive lens; min centre of a negative lens)
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
{configs_html}
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
    fp = model_gate.fingerprint(desc, (notes or {}).get("environment") or ())
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
