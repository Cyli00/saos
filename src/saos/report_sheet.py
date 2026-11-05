"""交付报告第 1 页：一页总览（纸面风格，打印为 A3 横向第 1 页）。

只画已经记录的数据：交付轮次的 viz.json（剖面、点列、MTF、处方）、账本里的各轮 G、逐项判定结果和确认记录；
不重新计算任何光学量。完整记录（需求原文、材料、逐项依据、公差、证据）在第 2 页，由 report.render 生成。
Page 1 of the delivery report: a one-page summary drawn from recorded data only (the delivered round's viz.json, the
ledger, the judged items and the confirmations); nothing optical is recomputed. The full record is page 2.
"""

from __future__ import annotations

import json
import math
from html import escape
from pathlib import Path
from typing import Any

from saos import i18n, jobs

TEXT: dict[str, tuple[str, str]] = {
    "kicker": ("saos 设计报告 · {job} · 第 {n} 轮 · {date}", "saos design report · {job} · round {n} · {date}"),
    "to_full": ("完整记录", "Full record"),
    "to_sheet": ("总览", "Summary"),
    "sign.demand": ("需求单", "Demand"),
    "sign.model": ("建模说明", "Model"),
    "sign.design": ("设计", "Design"),
    "sign.measure": ("OpticStudio 测量", "OpticStudio"),
    "sign.tolerance": ("公差", "Tolerances"),
    "via.window": ("用户在窗口确认", "user, in the window"),
    "via.cli": ("用户原话确认", "user's own words"),
    "via.auto": ("agent 自行确认", "agent, autonomous"),
    "via.none": ("未记录", "not recorded"),
    "rounds": ("{n} 轮", "{n} rounds"),
    "measured": ("交付轮已测量", "delivered round measured"),
    "fake": ("测试数据", "test data"),
    "tol.done": ("已扫描", "scanned"),
    "tol.other": ("扫描的不是交付镜头", "scan not on the delivered lens"),
    "tol.none": ("未做", "not run"),
    "fig.lens": ("镜头", "Lens"),
    "fig.lens.sub": ("子午剖面 · 实际比例", "meridional section · true scale"),
    "fig.lens.none": ("这个系统画不出子午剖面（非序列或各面不共面），见第 2 页与工作流窗口。", "This system has no meridional section (non-sequential or folded); see page 2 and the workflow window."),
    "fig.spot": ("点列图", "Spot diagram"),
    "fig.spot.sub": ("相对主波长主光线", "relative to the primary chief ray"),
    "fig.mtf": ("MTF", "MTF"),
    "fig.mtf.sub": ("实线子午 · 虚线弧矢 · 点线衍射极限", "solid T · dashed S · dotted diffraction limit"),
    "fig.none": ("交付轮次没有这张图的导出。", "The delivered round has no export for this figure."),
    "fig.g": ("收敛", "Convergence"),
    "fig.g.sub": ("目标差距 G", "shortfall G"),
    "fig.g.log": ("目标差距 G · 对数", "shortfall G · log"),
    "cap.lens": ("<b>图 1</b>颜色为视场；像面为右侧竖线。", "<b>Fig. 1</b> Colours are fields; the image plane is the line on the right."),
    "cap.spot": ("<b>图 2</b>颜色为波长；方框为 ±{half} {unit}。", "<b>Fig. 2</b> Colours are wavelengths; each box spans ±{half} {unit}."),
    "cap.mtf": ("<b>图 3</b>{source}", "<b>Fig. 3</b> {source}"),
    "h.judge": ("判定", "Checks"),
    "h.judge.sub": ("第 {n} 轮", "round {n}"),
    "th.judge": ("条目|界限|实测|结论", "Item|Bound|Measured|Verdict"),
    "h.rx": ("处方", "Prescription"),
    "th.rx": ("面|注释|材料|半径|厚度|半口径", "Surface|Comment|Material|Radius|Thickness|Semi-dia."),
    "rx.more": ("另 {n} 面见第 2 页", "{n} more surfaces on page 2"),
    "judge.more": ("另 {n} 项见第 2 页", "{n} more items on page 2"),
    "h.risks": ("未验证与风险", "Not verified and risks"),
    "risk.more": ("另 {n} 条见第 2 页", "{n} more on page 2"),
    "risk.none": ("记录里没有未验证项。", "The records list nothing unverified."),
    "risk.fake": ("数值来自假后端，不是光学实测。", "The numbers come from the fake backend, not optical measurement."),
    "risk.rtc": ("设计轮次在 rtc 上计算；交付轮的数值已换成 OpticStudio 复测值。", "Design rounds ran on rtc; the delivered round shows OpticStudio's re-measured numbers."),
    "risk.tol_none": ("没有做公差扫描：加工与装配误差的影响未知。", "No tolerance scan: the effect of manufacturing and assembly errors is unknown."),
    "risk.tol_other": ("最近的公差扫描不是在交付镜头上做的，良率只供参考。", "The latest tolerance scan did not run on the delivered lens; its yield is for reference only."),
    "risk.blocked": ("交付轮不可交付，原因见第 2 页。", "The shown round is not deliverable; see page 2 for why."),
    "risk.auto": ("自主运行：{n} 项决定未经用户确认（见第 2 页）。", "Autonomous run: {n} decision(s) not confirmed by the user (see page 2)."),
    "risk.errors": ("判定执行出错：{ids}。", "Checks failed to run: {ids}."),
    "risk.open": ("待确认问题：{q}", "Open question: {q}"),
    "risk.viz": ("交付轮次没有可视化导出，图缺失。", "The delivered round has no visual export; figures are missing."),
    "risk.viz_errors": ("图的导出有缺口：{parts}。", "Gaps in the visual export: {parts}."),
    "risk.unverified": ("未与 OpticStudio 对账：{items}。", "Not reconciled with OpticStudio: {items}."),
    "risk.continue": ("设计尚未结束，这是截至最后一轮的状态。", "The design is not finished; this is the state as of the last round."),
    "footer": ("生成于 {time} · 数值均取自任务记录，图由交付轮次的导出绘制", "generated {time} · every number comes from the job's records; figures are drawn from the delivered round's export"),
    "page": ("第 1 页 / 共 2 页", "page 1 of 2"),
    "lede.aperture": ("{type} {value}", "{type} {value}"),
    "lede.fields": ("视场 {items}", "fields {items}"),
    "lede.waves": ("波长 {items} µm", "wavelengths {items} µm"),
    "lede.materials": ("材料 {items}", "materials {items}"),
}

FIELD_INK = ("#a07a3c", "#3f7a57", "#b5432d", "#5a6f8f", "#7a5a8f", "#8f6a3c", "#3c7f8f", "#8f3c5a")
RX_ROWS = 12
JUDGE_ROWS = 12
RISK_ROWS = 4

SHEET_CSS = """
.sheet{--paper:#f4f1ea;--card:#fbf9f4;--ink:#1d1c1a;--ink-2:#57524a;--ink-3:#8f887c;--rule:#d8d1c3;--red:#b5432d;--green:#3f7a57;--gold:#a07a3c;
 --serif:"Noto Serif SC","Source Han Serif SC","Songti SC","SimSun",serif;--sans:"Noto Sans SC","Microsoft YaHei UI","Segoe UI",system-ui,sans-serif;--mono:"IBM Plex Mono","Cascadia Mono",Consolas,monospace;
 width:1480px;height:900px;margin:0 auto;background:var(--paper);color:var(--ink);font:12.5px/1.55 var(--sans);padding:28px 36px 18px;display:grid;grid-template-rows:auto minmax(0,1fr) auto;gap:14px;
 transform-origin:top center;box-shadow:0 30px 80px -30px #3b2f1a66}
.sheet .num{font-family:var(--mono);font-variant-numeric:tabular-nums}
.sheet header{display:grid;grid-template-columns:1fr auto;gap:26px;align-items:end;border-bottom:1.5px solid var(--ink);padding-bottom:10px;position:relative}
.sheet header::after{content:"";position:absolute;left:0;right:0;bottom:-5px;border-bottom:1px solid var(--rule)}
.sheet .kicker{font:11px var(--mono);color:var(--ink-3)}
.sheet h1{font:600 26px/1.2 var(--serif);margin:4px 0 2px;letter-spacing:.02em}
.sheet .lede{color:var(--ink-2);font-size:13px}
.sheet .signs{display:flex;gap:18px;align-items:flex-end}
.sheet .sign{min-width:104px}.sheet .sign b{display:block;font:600 12.5px var(--serif)}
.sheet .sign span{display:block;font:11px var(--mono);color:var(--ink-3)}
.sheet .sign i{display:block;font-style:normal;font-size:11px;color:var(--green)}.sheet .sign i.no{color:var(--red)}
.sheet .stamp{width:108px;height:108px;border-radius:50%;border:3px double var(--green);color:var(--green);display:grid;place-items:center;text-align:center;
 font:600 16px/1.25 var(--serif);letter-spacing:.12em;transform:rotate(-10deg);mix-blend-mode:multiply}
.sheet .stamp.no{border-color:var(--red);color:var(--red)}.sheet .stamp.mid{border-color:var(--gold);color:var(--gold)}
.sheet .stamp small{display:block;font:10px var(--mono);letter-spacing:0;margin-top:2px}
.sheet .cols{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr) minmax(0,.95fr);gap:20px;min-height:0}
.sheet .col{display:flex;flex-direction:column;gap:12px;min-height:0}
.sheet .fig{background:var(--card);border:1px solid var(--rule);padding:8px 12px 6px;display:flex;flex-direction:column;min-height:0}
.sheet .fig.grow{flex:1 1 auto}.sheet .fig svg{flex:1 1 auto;min-height:0;width:100%}
.sheet .cap{font-size:11px;color:var(--ink-2);margin-top:4px}.sheet .cap b{font-family:var(--serif);color:var(--ink);margin-right:5px}
.sheet h2{font:600 14px var(--serif);margin:0 0 4px;display:flex;gap:8px;align-items:baseline;border:0;padding:0}
.sheet h2 small{font:11px var(--mono);color:var(--ink-3);font-weight:400}
.sheet table.tbl{width:100%;border-collapse:collapse;font-size:11.5px}
.sheet .tbl th{font:500 11px var(--sans);color:var(--ink-2);text-align:right;padding:4px 6px;border-top:1.5px solid var(--ink);border-bottom:1px solid var(--ink);white-space:nowrap}
.sheet .tbl td{text-align:right;padding:3px 6px;border-bottom:1px solid var(--rule);vertical-align:top}
.sheet .tbl tr:last-child td{border-bottom:1.5px solid var(--ink)}
.sheet .tbl th:first-child,.sheet .tbl td:first-child,.sheet .tbl .l{text-align:left}
.sheet .ok{color:var(--green)}.sheet .no{color:var(--red)}
.sheet .mk{display:inline-block;width:8px;height:8px;border-radius:50%;border:1.2px solid currentColor;margin-right:5px}.sheet .mk.f{background:currentColor}
.sheet .more{font-size:11px;color:var(--ink-3);margin-top:2px}
.sheet .empty{flex:1;display:grid;place-items:center;color:var(--ink-3);font-size:12px;text-align:center;padding:12px}
.sheet ul.risks{margin:0;padding-left:16px;font-size:12px;color:var(--ink-2)}.sheet ul.risks li{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.sheet ul.risks li.more{list-style:none;color:var(--ink-3)}.sheet ul.risks li.open{color:var(--red)}
.sheet footer{display:flex;justify-content:space-between;color:var(--ink-3);font-size:10.5px;border-top:1px solid var(--rule);padding-top:5px}
.sheet svg text{font:10px var(--mono);fill:var(--ink-2)}.sheet svg .lab{font:italic 11px var(--serif);fill:var(--ink)}
.sheet svg .ax{stroke:var(--ink);stroke-width:.8}.sheet svg .gd{stroke:var(--rule);stroke-dasharray:1 3}
.sheet svg .glass{fill:#a07a3c12;stroke:var(--ink);stroke-width:.9}.sheet svg .axis{stroke:var(--ink-3);stroke-dasharray:10 3 2 3;stroke-width:.7}
.sheet svg .ray{fill:none;stroke-width:.6}.sheet svg .img{stroke:var(--ink);stroke-width:1.2}.sheet svg .c{fill:none;stroke-width:1.2}
"""


def _e(value: Any) -> str:
    return escape(str(value), quote=True)


def _num(v: Any, digits: int = 4) -> str:
    if v is None:
        return "—"
    if isinstance(v, str):
        return "∞" if v.strip().lower() in ("inf", "infinity", "+inf") else "−∞" if v.strip().lower() in ("-inf", "-infinity") else _e(v)
    if not math.isfinite(v):
        return "∞"
    return f"{v:.{digits}g}"


def _nice(raw: float) -> float:
    p = 10 ** math.floor(math.log10(raw))
    m = raw / p
    return (1 if m < 1.5 else 2 if m < 3.5 else 5 if m < 7.5 else 10) * p


def _ticks(lo: float, hi: float, n: int = 4) -> list[float]:
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        return []
    step = _nice((hi - lo) / n)
    return [k * step for k in range(math.ceil(lo / step - 1e-9), math.floor(hi / step + 1e-9) + 1)]


class _T:
    def __init__(self, lang: str) -> None:
        self.lang = lang

    def __call__(self, key: str, **kw: Any) -> str:
        template = i18n.text(self.lang, *TEXT[key])
        return template.format(**kw) if kw else template


def load_viz(job: jobs.Job, deliver: dict[str, Any]) -> dict[str, Any] | None:
    """交付轮次的可视化导出（记轮次时写在判定结果旁）；没有或读不了时为 None。"""
    evaluation = deliver.get("evaluation")
    if not evaluation:
        return None
    path = job.root / Path(evaluation).parent / "viz.json"
    try:
        viz = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return viz if isinstance(viz, dict) and viz.get("ok", True) else None


def _wave_inks(waves: list[dict[str, Any]]) -> list[str]:
    # 按波长从短到长取蓝灰、绿、红（三谱线惯例），多于三个时按序插值 / short to long: blue-grey, green, red
    order = sorted(range(len(waves)), key=lambda i: waves[i]["um"])
    base = ["#5a6f8f", "#3f7a57", "#b5432d"]
    out = [""] * len(waves)
    for rank, i in enumerate(order):
        out[i] = base[min(2, round(rank * 2 / max(1, len(waves) - 1)))] if len(waves) > 1 else base[1]
    return out


def _section(viz: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """剖面坐标下的面与子午光线，与工作流窗口的 toSection 同一做法：轴对称系统就是全局 YZ 面，
    折转系统用 ui.section 找到的对称平面；找不到时为 None。点换成 (离面距离, 纵轴, 横轴)，之后按 (z, y) 画。
    Surfaces and meridional rays in section coordinates, as the workflow window's toSection does."""
    from saos.ui import section

    system = viz.get("system") or {}
    if system.get("mode") == "non_sequential":
        return None
    surfaces = [s for s in viz.get("surfaces") or [] if s.get("profile_y")]
    rays = (viz.get("layout") or {}).get("rays") or []
    if not system.get("non_axial"):
        return surfaces, [r for r in rays if abs(r.get("px", 0)) < 1e-12 and r.get("points")]
    sec = section.section_plane(viz)
    if not sec:
        return None
    dot = lambda a, p: a[0] * p[0] + a[1] * p[1] + a[2] * p[2]  # noqa: E731
    P = lambda p: [dot(sec["normal"], p) - sec["offset"], dot(sec["v"], p), dot(sec["u"], p)]  # noqa: E731
    profiles = {str(k): v for k, v in (sec.get("profiles") or {}).items()}
    out = []
    for s in surfaces:
        line = [P(p) for p in s.get(profiles.get(str(s["index"]), "profile_y")) or s["profile_y"]]
        if line and line[0][1] > line[-1][1]:
            line.reverse()
        mask_key = profiles.get(str(s["index"]), "profile_y")
        masks = (s.get("obscuration") or {}).get(mask_key, [])
        out.append({**s, "profile_y": line, "obscuration": {"profile_y": [[P(p) for p in row] for row in masks]}})
    fan = "py" if sec.get("fan") != "px" else "px"
    return out, [{**r, "points": [P(p) for p in r["points"]]} for r in rays if abs(r.get(fan, 0)) < 1e-12 and r.get("points")]


def lens_svg(viz: dict[str, Any], width: int = 560, height: int = 300) -> str | None:
    """子午剖面：面的截线与剖面内的光线扇；非序列或找不到对称平面时返回 None。"""
    shown = _section(viz)
    if shown is None:
        return None
    surfaces, rays = shown
    pts = [(p[2], p[1]) for s in surfaces for p in s["profile_y"]] + [(p[2], p[1]) for r in rays for p in r["points"]]
    if not pts:
        return None
    z0, z1 = min(p[0] for p in pts), max(p[0] for p in pts)
    y0, y1 = min(p[1] for p in pts), max(p[1] for p in pts)
    pad = 14
    scale = min((width - 2 * pad) / ((z1 - z0) or 1), (height - 2 * pad) / ((y1 - y0) or 1))
    ox = (width - (z1 - z0) * scale) / 2 - z0 * scale
    oy = (height + (y1 + y0) * scale) / 2
    X = lambda z: ox + z * scale  # noqa: E731
    Y = lambda y: oy - y * scale  # noqa: E731
    path = lambda ps: "M" + "L".join(f"{X(p[2]):.1f},{Y(p[1]):.1f}" for p in ps)  # noqa: E731
    out = [f'<line class="axis" x1="0" x2="{width}" y1="{Y(0):.1f}" y2="{Y(0):.1f}"/>']
    by_index = {s["index"]: s for s in surfaces}
    for s in surfaces:
        nxt = by_index.get(s["index"] + 1)
        material = (s.get("material") or "").upper()
        if nxt and material and material != "MIRROR":
            out.append(f'<path class="glass" d="{path(s["profile_y"])}L{path(list(reversed(nxt["profile_y"])))[1:]}Z"/>')
        if material == "MIRROR":
            out.append(f'<path class="img" d="{path(s["profile_y"])}"/>')
    for r in rays:
        out.append(f'<path class="ray" stroke="{FIELD_INK[r.get("field", 0) % len(FIELD_INK)]}" d="{path(r["points"])}"/>')
    for s in surfaces:
        for points in (s.get("obscuration") or {}).get("profile_y", []):
            out.append(f'<path class="img" stroke-dasharray="3 2" d="{path(points)}"/>')
    image = surfaces[-1] if surfaces else None
    if image:
        out.append(f'<path class="img" d="{path(image["profile_y"])}"/>')
    # 比例尺 / a scale bar
    bar = _nice(80 / scale)
    out.append(f'<path class="ax" d="M{pad},{height - 8}h{bar * scale:.1f}"/><text x="{pad}" y="{height - 12}">{_num(bar, 3)} mm</text>')
    return f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet">{"".join(out)}</svg>'


def spot_svg(viz: dict[str, Any], t: _T) -> tuple[str, str] | None:
    """各视场点列，一格一个视场，共用 ±half 的方框；点用零长线段加圆头画，所有点都画。"""
    spot, system = viz.get("spot"), viz.get("system") or {}
    if not spot or not spot.get("fields"):
        return None
    fields = spot["fields"]
    half = max((abs(v) for f in fields for pts in f.get("points") or [] for p in pts for v in p[:2]), default=0.0)
    half = float(f"{half * 1.08:.2g}") if half > 0 else 1e-3
    inks = _wave_inks(system.get("wavelengths") or [{"um": 0.55}])
    n = len(fields)
    cell, gap, label = 150, 10, 16
    width, height = n * cell + (n - 1) * gap, cell + label
    pw = (system.get("primary_wave") or 1) - 1
    out = []
    for k, f in enumerate(fields):
        x0 = k * (cell + gap)
        s = (cell - 2) / (2 * half)
        cx, cy = x0 + cell / 2, cell / 2
        out.append(f'<rect x="{x0 + 0.5}" y="0.5" width="{cell - 1}" height="{cell - 1}" fill="none" stroke="#1d1c1a" stroke-width=".8"/>'
                   f'<path class="gd" d="M{cx},0V{cell}M{x0},{cy}H{x0 + cell}"/>')
        for w, pts in enumerate(f.get("points") or []):
            d = "".join(f"M{cx + p[0] * s:.1f},{cy - p[1] * s:.1f}h0" for p in pts)
            out.append(f'<path d="{d}" stroke="{inks[w % len(inks)]}" stroke-width="1.6" stroke-linecap="round" opacity=".75"/>')
        rms = ((spot.get("rms_radius") or [[]])[k] or [None])[pw] if k < len(spot.get("rms_radius") or []) else None
        items = (system.get("fields") or {}).get("items") or []
        name = ""
        if f.get("field", k) < len(items):
            it = items[f.get("field", k)]
            name = f'{_num(it.get("y"), 3)}' if not it.get("x") else f'({_num(it.get("x"), 3)}, {_num(it.get("y"), 3)})'
        out.append(f'<text x="{cx}" y="{cell + 12}" text-anchor="middle">{_e(name)}{f" · RMS {_num(rms, 3)} µm" if rms is not None else ""}</text>')
    unit = "mm" if system.get("lens_units") == "Millimeters" else system.get("lens_units", "")
    caption = t("cap.spot", half=_num(half, 2), unit=_e(unit))
    return f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet">{"".join(out)}</svg>', caption


def _plot(curves: list[dict[str, Any]], xr: tuple[float, float], yr: tuple[float, float], xl: str, yl: str,
          width: int = 420, height: int = 230, log_y: bool = False) -> str:
    left, right, top, bottom = 40, 8, 10, 28
    X = lambda x: left + (x - xr[0]) / ((xr[1] - xr[0]) or 1) * (width - left - right)  # noqa: E731
    Y = lambda y: top + (1 - (y - yr[0]) / ((yr[1] - yr[0]) or 1)) * (height - top - bottom)  # noqa: E731
    out = []
    for v in _ticks(yr[0], yr[1]):
        label = f"1e{int(v)}" if log_y and float(v).is_integer() else _num(v, 3)
        out.append(f'<line class="gd" x1="{left}" x2="{width - right}" y1="{Y(v):.1f}" y2="{Y(v):.1f}"/><text x="{left - 5}" y="{Y(v) + 3:.1f}" text-anchor="end">{label}</text>')
    for v in _ticks(xr[0], xr[1], 5):
        out.append(f'<text x="{X(v):.1f}" y="{height - bottom + 13}" text-anchor="middle">{_num(v, 3)}</text>')
    out.append(f'<line class="ax" x1="{left}" x2="{left}" y1="{top}" y2="{height - bottom}"/><line class="ax" x1="{left}" x2="{width - right}" y1="{height - bottom}" y2="{height - bottom}"/>')
    for c in curves:
        segs, cur = [], []
        for x, y in zip(c["x"], c["y"]):
            if x is None or y is None or not (math.isfinite(x) and math.isfinite(y)):
                if cur:
                    segs.append(cur)
                cur = []
                continue
            cur.append(f"{X(x):.1f},{Y(max(yr[0], min(yr[1], y))):.1f}")
        if cur:
            segs.append(cur)
        dash = f' stroke-dasharray="{c["dash"]}"' if c.get("dash") else ""
        out += [f'<polyline class="c" stroke="{c["color"]}"{dash} points="{" ".join(s)}"/>' for s in segs if len(s) > 1]
        for x, y, fill in c.get("dots") or []:
            out.append(f'<circle cx="{X(x):.1f}" cy="{Y(y):.1f}" r="2.6" fill="{fill}" stroke="{c["color"]}"/>')
    out.append(f'<text class="lab" x="{width - right}" y="{height - 2}" text-anchor="end">{_e(xl)}</text><text class="lab" x="{left + 4}" y="{top + 9}">{_e(yl)}</text>')
    return f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet">{"".join(out)}</svg>'


def mtf_svg(viz: dict[str, Any]) -> str | None:
    mtf = viz.get("mtf") or {}
    series = mtf.get("series") or []
    if not series:
        return None
    curves, slot = [], -1
    for s in series:
        diff = "diff" in (s.get("description") or "").lower()
        if not diff:
            slot += 1
        color = "#8f887c" if diff else FIELD_INK[slot % len(FIELD_INK)]
        for name, ys in (s.get("y") or {}).items():
            curves.append({"x": s.get("x") or [], "y": ys, "color": color, "dash": "1.5 3" if diff else "5 3" if "sag" in name.lower() else None})
    xs = [x for c in curves for x in c["x"] if x is not None]
    if not xs:
        return None
    return _plot(curves, (0.0, max(xs)), (0.0, 1.0), series[0].get("x_label") or "cycles/mm", "MTF")


def g_svg(rounds: list[dict[str, Any]], t: _T) -> str | None:
    pts = [(r["round"], r["shortfall"], bool(r.get("achieved"))) for r in rounds if r.get("shortfall") is not None and math.isfinite(r["shortfall"])]
    if not pts:
        return None
    positive = [g for _, g, _ in pts if g > 0]
    log = bool(positive) and max(positive) / min(positive) > 100
    floor = min(positive) / 2 if positive else 1.0
    fy = (lambda g: math.log10(max(g, floor))) if log else (lambda g: g)  # noqa: E731
    ys = [fy(g) for _, g, _ in pts]
    lo, hi = (math.floor(min(ys)), math.ceil(max(ys))) if log else (0.0, max(ys) or 1.0)
    xr = (min(p[0] for p in pts), max(p[0] for p in pts))
    xr = xr if xr[1] > xr[0] else (xr[0], xr[0] + 1)
    curve = {"x": [p[0] for p in pts], "y": ys, "color": "#1d1c1a", "dots": [(p[0], fy(p[1]), "#3f7a57" if p[2] else "#fbf9f4") for p in pts]}
    return _plot([curve], xr, (lo, hi if hi > lo else lo + 1), "r", t("fig.g.log") if log else t("fig.g.sub"), height=170, log_y=log)


def _rx_table(viz: dict[str, Any], t: _T) -> str:
    rows = viz.get("lens_data") or []
    if not rows:
        return ""
    shown = rows[:RX_ROWS]
    head = "".join(f'<th class="{"l" if i in (1, 2) else ""}">{c}</th>' for i, c in enumerate(t("th.rx").split("|")))
    mark = lambda r, k: ("" if (r.get("solves") or {}).get(k) in (None, "Fixed", "Automatic", "None") else " V" if (r.get("solves") or {}).get(k) == "Variable" else f' {(r.get("solves") or {}).get(k)[0]}')  # noqa: E731
    body = "".join(
        f'<tr><td>{r.get("index")}{" STO" if r.get("is_stop") else ""}</td><td class="l">{_e(r.get("comment") or "")}</td><td class="l">{_e(r.get("material") or "")}</td>'
        f'<td class="num">{_num(r.get("radius"), 6)}{mark(r, "radius")}</td><td class="num">{_num(r.get("thickness"), 6)}{mark(r, "thickness")}</td>'
        f'<td class="num">{_num(r.get("semi_diameter"), 4)}</td></tr>' for r in shown)
    more = f'<div class="more">{t("rx.more", n=len(rows) - len(shown))}</div>' if len(rows) > len(shown) else ""
    return f'<div><h2>{t("h.rx")}</h2><table class="tbl"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>{more}</div>'


def risks(data: dict[str, Any], viz: dict[str, Any] | None, t: _T) -> list[tuple[str, bool]]:
    """第 1 页必须如实列出的未验证项与风险；(文字, 是否为未解决项)。"""
    out: list[tuple[str, bool]] = []
    deliver, decision = data["deliver"], data["decision"]
    if data["backend"]["name"] == "fake":
        out.append((t("risk.fake"), True))
    if any(r.get("backend") == "rtc" for r in data["rounds"]):
        out.append((t("risk.rtc"), False))
    tol = data.get("tolerance")
    if tol is None:
        out.append((t("risk.tol_none"), True))
    elif not tol.get("same_lens"):
        out.append((t("risk.tol_other"), True))
    if data.get("blocked"):
        out.append((t("risk.blocked"), True))
    if data.get("autonomy"):
        out.append((t("risk.auto", n=len(data["autonomy"])), True))
    errors = [i["id"] for i in data["items"] if i.get("error")]
    if errors:
        out.append((t("risk.errors", ids=", ".join(errors)), True))
    for q in data["demand"].get("open_questions") or []:
        out.append((t("risk.open", q=q.get("question", "")), bool(q.get("blocking"))))
    if viz is None:
        out.append((t("risk.viz"), True))
    else:
        if viz.get("errors"):
            out.append((t("risk.viz_errors", parts=", ".join(sorted(viz["errors"]))), True))
        if viz.get("unverified"):
            out.append((t("risk.unverified", items="; ".join(viz["unverified"])), True))
    if decision["status"] == "continue":
        out.append((t("risk.continue"), True))
    return out


def render_sheet(job: jobs.Job, data: dict[str, Any], lang: str, *, status_text: str, judged: list[dict[str, str]]) -> str:
    """第 1 页。judged 由 report 按它的规则写好：{id, bound, value, verdict, cls}。"""
    t = _T(lang)
    deliver, decision, rounds = data["deliver"], data["decision"], data["rounds"]
    viz = data.get("viz")
    system = (viz or {}).get("system") or {}
    now = jobs.now_iso()
    # 标题行：孔径、视场、波长、材料，全部取自导出与交付轮的材料记录 / the lede, from the export and the delivered materials
    lede = []
    ap = system.get("aperture") or {}
    if ap.get("type"):
        lede.append(t("lede.aperture", type=_e(ap["type"]), value=_num(ap.get("value"), 5)))
    fields = (system.get("fields") or {}).get("items") or []
    if fields:
        unit = "°" if (system.get("fields") or {}).get("type") == "Angle" else ""
        lede.append(t("lede.fields", items=", ".join(f'{_num(f.get("y"), 3)}{unit}' if not f.get("x") else f'({_num(f.get("x"), 3)}, {_num(f.get("y"), 3)}){unit}' for f in fields)))
    waves = system.get("wavelengths") or []
    if waves:
        lede.append(t("lede.waves", items=", ".join(_num(w.get("um"), 4) for w in waves)))
    mats = sorted({m.get("name") for m in deliver.get("materials") or [] if m.get("name")})
    if mats:
        lede.append(t("lede.materials", items=_e(" / ".join(mats))))
    # 签署栏 / the sign-offs
    confirmed = data.get("confirmed") or {}
    approval = data.get("approval") or {}
    via = lambda r: t("via." + (r.get("via") or "cli")) if r else t("via.none")  # noqa: E731
    stamp_time = lambda r: _e((r.get("time") or "")[:16].replace("T", " ")) if r else "—"  # noqa: E731
    backend = data["backend"]
    tol = data.get("tolerance")
    signs = [
        (t("sign.demand"), stamp_time(confirmed), via(confirmed), not confirmed or confirmed.get("via") == "auto"),
        (t("sign.model"), stamp_time(approval), via(approval), not approval or approval.get("via") == "auto"),
        (t("sign.design"), t("rounds", n=len(rounds)), _e(status_text), decision["status"] != "achieved"),
        (t("sign.measure"), _e(backend.get("version") or "—"), t("fake") if backend["name"] == "fake" else t("measured"), backend["name"] != "zos"),
        (t("sign.tolerance"), _e(tol["scan"]) if tol else "—", t("tol.none") if tol is None else t("tol.done") if tol.get("same_lens") else t("tol.other"),
         tol is None or not tol.get("same_lens")),
    ]
    signs_html = "".join(f'<div class="sign"><b>{a}</b><span>{b}</span><i class="{"no" if bad else ""}">{c}</i></div>' for a, b, c, bad in signs)
    g = deliver.get("shortfall")
    stamp_cls = "" if decision["status"] == "achieved" and not data.get("blocked") else "mid" if decision["status"] == "continue" else "no"
    stamp = f'<div class="stamp {stamp_cls}">{_e(status_text)}<small>G = {_num(g, 3)}</small></div>'
    # 图 / figures
    lens = lens_svg(viz) if viz else None
    spot = spot_svg(viz, t) if viz else None
    mtf = mtf_svg(viz) if viz else None
    gplot = g_svg(rounds, t)
    none = f'<div class="empty">{t("fig.none")}</div>'
    mtf_source = (viz or {}).get("backend") == "rtc" and "rtc FFT MTF" or "OpticStudio FFT MTF"
    judge_rows = "".join(f'<tr><td>{_e(j["id"])}</td><td class="num">{j["bound"]}</td><td class="num">{j["value"]}</td>'
                         f'<td class="{j["cls"]}"><span class="mk {"f" if j["cls"] == "ok" else ""}"></span>{_e(j["verdict"])}</td></tr>' for j in judged[:JUDGE_ROWS])
    judge_more = f'<div class="more">{t("judge.more", n=len(judged) - JUDGE_ROWS)}</div>' if len(judged) > JUDGE_ROWS else ""
    judge_head = "".join(f"<th>{c}</th>" for c in t("th.judge").split("|"))
    risk_items = risks(data, viz, t)
    shown_risks = sorted(risk_items, key=lambda r: not r[1])[:RISK_ROWS]
    risk_html = "".join(f'<li class="{"open" if open_ else ""}" title="{_e(text)}">{_e(text)}</li>' for text, open_ in shown_risks) or f"<li>{t('risk.none')}</li>"
    if len(risk_items) > RISK_ROWS:
        risk_html += f'<li class="more">{t("risk.more", n=len(risk_items) - RISK_ROWS)}</li>'
    title = _e(job.read_state().get("name", job.id))
    date = _e(now[:10])
    return f"""<article class="sheet" id="sheet">
<header><div><div class="kicker">{t("kicker", job=_e(job.id), n=deliver["round"], date=date)}</div><h1>{title}</h1><div class="lede">{" · ".join(lede)}</div></div>
<div class="signs">{signs_html}{stamp}</div></header>
<div class="cols">
 <div class="col">
  <div class="fig grow"><h2>{t("fig.lens")} <small>{t("fig.lens.sub")}</small></h2>{lens or f'<div class="empty">{t("fig.lens.none") if viz else t("fig.none")}</div>'}
   {f'<div class="cap">{t("cap.lens")}</div>' if lens else ""}</div>
  {_rx_table(viz, t) if viz else ""}
 </div>
 <div class="col">
  <div class="fig" style="flex:1 1 44%"><h2>{t("fig.spot")} <small>{t("fig.spot.sub")}</small></h2>{spot[0] if spot else none}{f'<div class="cap">{spot[1]}</div>' if spot else ""}</div>
  <div class="fig" style="flex:1 1 56%"><h2>{t("fig.mtf")} <small>{t("fig.mtf.sub")}</small></h2>{mtf or none}{f'<div class="cap">{t("cap.mtf", source=_e(mtf_source))}</div>' if mtf else ""}</div>
 </div>
 <div class="col">
  <div><h2>{t("h.judge")} <small>{t("h.judge.sub", n=deliver["round"])}</small></h2><table class="tbl"><thead><tr>{judge_head}</tr></thead><tbody>{judge_rows}</tbody></table>{judge_more}</div>
  <div class="fig" style="flex:0 0 190px"><h2>{t("fig.g")} <small>{t("fig.g.sub")}</small></h2>{gplot or none}</div>
  <div><h2>{t("h.risks")}</h2><ul class="risks">{risk_html}</ul></div>
 </div>
</div>
<footer><span>{t("footer", time=_e(now))}</span><span><a href="#full">{t("to_full")} →</a> · {t("page")}</span></footer>
</article>"""
