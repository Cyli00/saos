"""交付报告：由任务目录中的需求框定和 ledger 生成单文件 HTML（给用户看）。

    saos report --job J [--lang zh|en]     # 写到任务的 report/index.html

语言按 i18n.resolve 选择：--lang > 需求框定的 lang > SAOS_LANG > 系统界面语言。
只读取已有记录，不重新计算，也不连接 OpticStudio；所有数值都能追溯到 ledger、analyses/、scripts/。
"""

from __future__ import annotations

import json
import math
from html import escape
from typing import Any

from saos import harness, i18n, jobs, ledger
from saos.protocol import ErrorCode, SaosError

# 每条文本为 (中文, 英文)，带 {name} 的按 str.format 填参数
TEXT: dict[str, tuple[str, str]] = {
    "status.achieved": ("达成", "Achieved"),
    "status.exhausted": ("无法再优化", "Cannot improve further"),
    "status.limit": ("达到轮次上限", "Round limit reached"),
    "status.continue": ("进行中", "In progress"),
    "summary.achieved": ("所有约束和性能指标都已达标。", "Every constraint and performance target is met."),
    "summary.exhausted": (
        "自最后一次明显进步以来，允许的每一级手段都试过，目标差距没有再明显缩小。下表列出未达标的项和差距。",
        "Since the last clear gain, every allowed tier of changes was tried and the shortfall did not shrink noticeably. The table lists the unmet goals and their gaps.",
    ),
    "summary.limit": ("轮次用完时仍有未达标的项。下表列出差距。", "Some goals were still unmet when the rounds ran out. The table lists the gaps."),
    "summary.continue": ("设计尚未结束，以下是截至最后一轮的状态。", "The design is not finished; this is the state as of the last round."),
    "tier.baseline": ("初始", "Start"),
    "tier.local": ("局部优化", "Local optimisation"),
    "tier.variables": ("放开变量", "More variables"),
    "tier.materials": ("换材料", "Materials"),
    "tier.global": ("全局优化", "Global optimisation"),
    "tier.structure": ("改结构", "Structure"),
    "kind.constraint": ("约束", "Constraint"),
    "kind.target": ("性能指标", "Target"),
    "kind.preference": ("期望项", "Preference"),
    "sep": ("，", ", "),
    "list_sep": ("、", ", "),
    "none": ("无", "none"),
    "no_value": ("无法取值", "no value"),
    "met": ("达标", "met"),
    "unmet": ("未达标", "not met"),
    "over": ("超出 {gap}", "off by {gap}"),
    "ev.operand": ("操作数", "Operand"),
    "ev.analysis": ("分析 <code>{name}</code> 取 <code>{path}</code>，全文 {link}", "Analysis <code>{name}</code>, value <code>{path}</code>, full text {link}"),
    "ev.script": ("脚本", "Script"),
    "chart.label": ("目标差距随轮次的变化", "Shortfall by round"),
    "chart.round": ("第 {n} 轮", "Round {n}"),
    "chart.x": ("轮次", "Round"),
    "judge.achieved": ("第 {n} 轮所有约束和性能指标达标", "every constraint and target is met in round {n}"),
    "judge.limit": ("已用完轮次上限 max_rounds = {cap}", "used up max_rounds = {cap}"),
    "judge.exhausted": (
        "自第 {n} 轮以来 G 没有缩小 {rel:.0%} 以上，允许的手段（{tiers}）都已试过",
        "G has not shrunk by {rel:.0%} or more since round {n}, and every allowed tier ({tiers}) was tried",
    ),
    "judge.continue": ("尚未终止；当前手段不再见效时，下一个要试的是{tier}", "not finished; when the current approach stops gaining, try {tier} next"),
    "progress": (
        "<b>最后一次明显进步</b>：第 {n} 轮；之后 {k} 轮没有明显进步，还没试过的手段：{untried}",
        "<b>Last clear gain</b>: round {n}; {k} round(s) since without a clear gain; tiers not yet tried: {untried}",
    ),
    "title": ("设计报告", "Design report"),
    "byline": ("saos 设计报告 · 任务 {job} · 生成于 {time}", "saos design report · job {job} · generated {time}"),
    "deliver": ("<b>交付镜头</b>：{lens}（第 {n} 轮，目标差距 G = {g}）", "<b>Delivered lens</b>: {lens} (round {n}, shortfall G = {g})"),
    "best_so_far": ("<b>目前最好的镜头</b>：{lens}（第 {n} 轮，目标差距 G = {g}）", "<b>Best lens so far</b>: {lens} (round {n}, shortfall G = {g})"),
    "judgement": ("<b>终止判断</b>：{text}", "<b>Stop decision</b>: {text}"),
    "h.request": ("需求", "Request"),
    "confirmed": ("用户确认：{quote}", "Confirmed by the user: {quote}"),
    "not_recorded": ("（未记录）", "(not recorded)"),
    "h.assumptions": ("假设", "Assumptions"),
    "h.items": ("逐项结果（第 {n} 轮）", "Results by goal (round {n})"),
    "th.items": ("目标|类别|达标线|实测值|结论|差多少|实测依据", "Goal|Kind|Pass line|Measured|Verdict|Gap|Evidence"),
    "note.g": (
        "目标差距 G 是约束和性能指标的单项差距之和，每项按其尺度归一化；期望项只报告数值，不决定是否达成。",
        "The shortfall G is the sum of the normalised gaps of all constraints and targets; preferences are reported only and do not decide whether the need is met.",
    ),
    "h.process": ("调试过程", "Rounds"),
    "th.rounds": ("轮次|手段|说明|G|评价函数|镜头", "Round|Tier|Note|G|Merit function|Lens"),
    "note.merit": (
        "评价函数每轮可能被调整，不能跨轮比较；判断进展只看 G。",
        "The merit function may change between rounds and is not comparable across them; progress is judged by G only.",
    ),
    "h.evidence": ("实测依据", "Evidence"),
    "ev.analyses": ("分析导出（{n}）", "Analysis exports ({n})"),
    "ev.scripts": ("脚本执行（{n}）", "Script runs ({n})"),
    "ev.screens": ("窗口截图", "Window screenshots"),
    "h.next": ("后续", "Next steps"),
    "next": (
        "如果需要评估加工和装配误差的影响，可以进一步做公差分析。",
        "A tolerance analysis can follow to estimate the effect of manufacturing and assembly errors.",
    ),
}


class Tr:
    def __init__(self, lang: str) -> None:
        self.lang = lang

    def __call__(self, key: str, **kw: Any) -> str:
        template = i18n.text(self.lang, *TEXT[key])
        return template.format(**kw) if kw else template

    def tier(self, tier: Any) -> str:
        return self(f"tier.{tier}") if f"tier.{tier}" in TEXT else str(tier)


CSS = """
:root{--bg:#fbfaf7;--fg:#1f2328;--muted:#5d646d;--line:#d9dde3;--card:#ffffff;--ok:#1a7f37;--bad:#c4321b;--warn:#9a6700;--accent:#2f5d8a;}
@media (prefers-color-scheme: dark){:root{--bg:#15171a;--fg:#e6e8eb;--muted:#a0a7b1;--line:#30353c;--card:#1d2024;--ok:#4cc26a;--bad:#ff7b65;--warn:#d9a733;--accent:#7fb0e0;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,"Microsoft YaHei","PingFang SC",sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 64px}h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;margin:12px 0}
.status{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;border:1px solid currentColor}
.achieved{color:var(--ok)}.exhausted,.limit{color:var(--bad)}.continue{color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600}.num{font-variant-numeric:tabular-nums;white-space:nowrap}.nowrap,th{white-space:nowrap}.pass{color:var(--ok);font-weight:600}.fail{color:var(--bad);font-weight:600}
blockquote{margin:8px 0;padding:8px 12px;border-left:3px solid var(--accent);background:var(--card)}code{font-size:13px}
.wrap{overflow-x:auto}svg text{fill:var(--muted);font-size:11px}svg .line{stroke:var(--accent);fill:none;stroke-width:2}svg .axis{stroke:var(--line)}
svg .dot{fill:var(--accent)}svg .dot.ok{fill:var(--ok)}img{max-width:100%;border:1px solid var(--line);border-radius:6px}
"""


def e(value: Any) -> str:
    return escape(str(value), quote=True)


def fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        if math.isinf(value):
            return "∞"
        return f"{value:.6g}"
    return e(value)


def goal_text(goal: dict[str, Any] | None, t: Tr) -> str:
    if not goal:
        return "—"
    if "equals" in goal:
        return f"= {fmt(float(goal['equals']))} ± {fmt(float(goal['tol']))}"
    parts = []
    if "min" in goal:
        parts.append(f"≥ {fmt(float(goal['min']))}")
    if "max" in goal:
        parts.append(f"≤ {fmt(float(goal['max']))}")
    return t("sep").join(parts)


def gap_text(goal: dict[str, Any] | None, value: float | None, t: Tr) -> str:
    """差多少：用目标本身的单位写出超出达标线的量。"""
    if not goal or value is None:
        return t("no_value") if goal else "—"
    if "equals" in goal:
        excess = abs(value - float(goal["equals"])) - float(goal["tol"])
    elif "max" in goal and value > float(goal["max"]):
        excess = value - float(goal["max"])
    elif "min" in goal and value < float(goal["min"]):
        excess = float(goal["min"]) - value
    else:
        excess = 0.0
    return t("met") if excess <= 0 else t("over", gap=fmt(excess))


def evidence_text(item: dict[str, Any], t: Tr) -> str:
    ev = item.get("evidence") or {}
    if "operand" in ev:
        op = ev["operand"]
        return f"{t('ev.operand')} <code>{e(op.get('type'))}</code> int1={e(op.get('int1', 0))} int2={e(op.get('int2', 0))} data={e(op.get('data', []))}"
    if "analysis" in ev:
        link = f'<a href="../{e(ev["text"])}">{e(ev["text"])}</a>'
        return t("ev.analysis", name=e(ev["analysis"]), path=e(ev["path"]), link=link)
    if "script" in ev:
        return f'{t("ev.script")} <a href="../{e(ev["script"])}">{e(ev["script"])}</a>'
    return e(item.get("error", "—"))


def chart(rounds: list[dict[str, Any]], t: Tr) -> str:
    """目标差距 G 随轮次的折线；G = ∞（无法取值）的轮次画在顶部。"""
    if not rounds:
        return ""
    width, height, left, bottom, top = 760, 240, 52, 34, 14
    finite = [r["shortfall"] for r in rounds if math.isfinite(r["shortfall"])]
    peak = max(finite) if finite else 1.0
    peak = peak if peak > 0 else 1.0
    n = max(len(rounds) - 1, 1)

    def x(k: int) -> float:
        return left + (width - left - 16) * k / n

    def y(g: float) -> float:
        g = peak if not math.isfinite(g) else g
        return top + (height - top - bottom) * (1 - g / peak)

    points = " ".join(f"{x(k):.1f},{y(r['shortfall']):.1f}" for k, r in enumerate(rounds))
    dots = "".join(
        f'<circle class="dot{" ok" if r.get("achieved") else ""}" cx="{x(k):.1f}" cy="{y(r["shortfall"]):.1f}" r="4">'
        f'<title>{t("chart.round", n=r["round"])} · {e(t.tier(r.get("tier")))} · G = {fmt(r["shortfall"])}</title></circle>'
        f'<text x="{x(k):.1f}" y="{height - 18}" text-anchor="middle">{r["round"]}</text>'
        for k, r in enumerate(rounds)
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{t("chart.label")}">'
        f'<line class="axis" x1="{left}" y1="{height - bottom}" x2="{width - 8}" y2="{height - bottom}"/>'
        f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}"/>'
        f'<text x="{left - 6}" y="{top + 4}" text-anchor="end">{fmt(peak)}</text><text x="{left - 6}" y="{height - bottom}" text-anchor="end">0</text>'
        f'<polyline class="line" points="{points}"/>{dots}<text x="{(width + left) / 2}" y="{height - 2}" text-anchor="middle">{t("chart.x")}</text></svg>'
    )


def judgement(decision: dict[str, Any], stop: dict[str, Any], t: Tr) -> str:
    """ledger 的 reason 是给 agent 看的英文，报告里按状态重写。"""
    status = decision["status"]
    if status == "achieved":
        return t("judge.achieved", n=decision["round"])
    if status == "limit":
        return t("judge.limit", cap=stop["max_rounds"])
    if status == "exhausted":
        tiers = t("list_sep").join(t.tier(x) for x in ledger.TIERS if x in stop["tiers"])
        return t("judge.exhausted", n=decision["last_gain_round"], rel=stop["rel"], tiers=tiers)
    return t("judge.continue", tier=t.tier(decision["next_tier"]))


def build(job: jobs.Job) -> dict[str, Any]:
    harness_path = job.root / harness.HARNESS_FILE
    if not harness_path.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"job {job.id} has no {harness.HARNESS_FILE}")
    h = harness.load(harness_path)
    rounds = ledger.load(job.root / ledger.LEDGER_FILE)
    if not rounds:
        raise SaosError(ErrorCode.BAD_ARGS, f"job {job.id} has no recorded rounds; run saos round first")
    decision = ledger.decide(rounds, h["stop"])
    final = rounds[-1]
    best = min(rounds, key=lambda r: (r["shortfall"], r["round"]))
    deliver = final if final.get("achieved") else best
    eval_file = job.root / "rounds" / f"r{deliver['round']:03d}.json"
    items = json.loads(eval_file.read_text(encoding="utf-8"))["items"] if eval_file.is_file() else []
    return {"harness": h, "rounds": rounds, "decision": decision, "deliver": deliver, "items": items}


def header_row(spec: str) -> str:
    return "".join(f"<th>{c}</th>" for c in spec.split("|"))


def render(job: jobs.Job, data: dict[str, Any], lang: str) -> str:
    t = Tr(lang)
    h, rounds, decision, deliver = data["harness"], data["rounds"], data["decision"], data["deliver"]
    status = decision["status"]
    targets = {x["id"]: x for x in h["targets"]}
    rows = []
    for item in data["items"]:
        goal = targets.get(item["id"], {}).get("goal")
        preference = item["kind"] == "preference"
        mark = "—" if preference else (f'<span class="pass">{t("met")}</span>' if item.get("passed") else f'<span class="fail">{t("unmet")}</span>')
        gap = "—" if preference else gap_text(goal, item.get("value"), t)
        rows.append(
            f'<tr><td>{e(item["id"])}<div class="muted">{e(item.get("description", ""))}</div></td><td class="nowrap">{e(t("kind." + item["kind"]))}</td>'
            f'<td class="num">{goal_text(goal, t)}</td><td class="num">{fmt(item.get("value"))}</td><td class="nowrap">{mark}</td>'
            f'<td class="num">{gap}</td><td>{evidence_text(item, t)}</td></tr>'
        )
    round_rows = "".join(
        f'<tr><td class="num">{r["round"]}</td><td>{e(t.tier(r.get("tier")))}</td><td>{e(r.get("note", ""))}</td>'
        f'<td class="num">{fmt(r["shortfall"])}</td><td class="num">{fmt(r.get("merit"))}</td><td><a href="../lenses/{e(r["file"])}">{e(r["file"])}</a></td></tr>'
        for r in rounds
    )
    assumptions = "".join(f"<li>{e(a)}</li>" for a in h.get("assumptions") or []) or f"<li>{t('none')}</li>"
    confirmed = (h.get("confirmed") or {}).get("quote", "")
    evidence = []
    for folder, key in (("analyses", "ev.analyses"), ("scripts", "ev.scripts")):
        files = sorted((job.root / folder).glob("*.json")) if (job.root / folder).is_dir() else []
        if files:
            links = "".join(f'<li><a href="../{folder}/{e(p.name)}">{e(p.name)}</a></li>' for p in files)
            evidence.append(f"<h3>{t(key, n=len(files))}</h3><ul>{links}</ul>")
    shots = sorted((job.root / "screens").glob("*.png")) if (job.root / "screens").is_dir() else []
    if shots:
        evidence.append(f"<h3>{t('ev.screens')}</h3>" + "".join(f'<p><img src="../screens/{e(p.name)}" alt="{e(p.name)}"></p>' for p in shots[-4:]))
    untried = t("list_sep").join(t.tier(x) for x in decision["untried_tiers"]) or t("none")
    progress = (
        ""
        if status == "achieved"
        else "<br>" + t("progress", n=decision["last_gain_round"], k=decision["rounds_without_gain"], untried=e(untried))
    )
    lens_link = f'<a href="../lenses/{e(deliver["file"])}">{e(deliver["file"])}</a>'
    return f"""<!doctype html><html lang="{'zh-CN' if lang == 'zh' else 'en'}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{t('title')} · {e(job.id)}</title><style>{CSS}</style></head><body><main>
<p class="muted">{t('byline', job=e(job.id), time=e(jobs.now_iso()))}</p>
<h1>{e(job.read_state().get('name', job.id))} <span class="status {status}">{t('status.' + status)}</span></h1>
<p>{t('summary.' + status)}</p>
<div class="card">{t('best_so_far' if status == 'continue' else 'deliver', lens=lens_link, n=deliver['round'], g=fmt(deliver['shortfall']))}
<br>{t('judgement', text=e(judgement(decision, h['stop'], t)))}{progress}</div>
<h2>{t('h.request')}</h2><blockquote>{e(h['request'])}</blockquote>
<p class="muted">{t('confirmed', quote=e(confirmed) or t('not_recorded'))}</p><h3>{t('h.assumptions')}</h3><ul>{assumptions}</ul>
<h2>{t('h.items', n=deliver['round'])}</h2><div class="wrap"><table><thead><tr>{header_row(t('th.items'))}</tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p class="muted">{t('note.g')}</p>
<h2>{t('h.process')}</h2><div class="card">{chart(rounds, t)}</div>
<div class="wrap"><table><thead><tr>{header_row(t('th.rounds'))}</tr></thead><tbody>{round_rows}</tbody></table></div>
<p class="muted">{t('note.merit')}</p>
<h2>{t('h.evidence')}</h2>{''.join(evidence) or f"<p>{t('none')}</p>"}
<h2>{t('h.next')}</h2><p>{t('next')}</p>
</main></body></html>"""


def write(job: jobs.Job, lang: str | None = None) -> dict[str, Any]:
    data = build(job)
    chosen = i18n.resolve(lang, data["harness"].get("lang"))
    out = job.root / "report" / "index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(render(job, data, chosen), encoding="utf-8")
    return {
        "report": str(out),
        "lang": chosen,
        "status": data["decision"]["status"],
        "deliver": data["deliver"]["file"],
        "shortfall": data["deliver"]["shortfall"],
    }
