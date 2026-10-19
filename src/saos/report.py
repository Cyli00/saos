"""交付报告：由任务目录中的需求和 ledger 生成单文件 HTML（给用户看）。

    saos report --job J [--lang zh|en]     # 写到任务的 report/index.html

语言按 i18n.resolve 选择：--lang > 需求的 lang > SAOS_LANG > 系统界面语言。
只读取已有记录，不重新计算，也不连接 OpticStudio；所有数值都能追溯到 ledger、analyses/、scripts/。
"""

from __future__ import annotations

import json
import math
from html import escape
from typing import Any

from saos import demand, i18n, jobs, ledger
from saos.protocol import ErrorCode, SaosError

# 每条文本为 (中文, 英文)，带 {name} 的按 str.format 填参数
TEXT: dict[str, tuple[str, str]] = {
    "status.achieved": ("达成", "Achieved"),
    "status.exhausted": ("无法再优化", "Cannot improve further"),
    "status.limit": ("达到轮次上限", "Round limit reached"),
    "status.continue": ("进行中", "In progress"),
    "status.stopped": ("按用户决定停止", "Stopped by the user"),
    "summary.achieved": ("所有常量、变量范围和目标达标线都已满足。", "Every constant, variable range and objective pass line is met."),
    "summary.exhausted": (
        "自最后一次明显进步以来，允许的每一级手段都试过或带证据排除，目标差距没有再明显缩小。下表列出未达标的项和差距。",
        "Since the last clear gain, every allowed tier of changes was tried or ruled out with evidence and the shortfall did not shrink noticeably. The table lists the unmet goals and their gaps.",
    ),
    "summary.limit": ("轮次用完时仍有未达标的项。下表列出差距。", "Some goals were still unmet when the rounds ran out. The table lists the gaps."),
    "summary.continue": ("设计尚未结束，以下是截至最后一轮的状态。", "The design is not finished; this is the state as of the last round."),
    "summary.stopped": ("用户根据下面的理由决定停止，需求没有全部满足。下表列出未达标的项和差距。",
                        "The user decided to stop for the reason below; the demand is not fully met. The table lists the unmet items and their gaps."),
    "tier.baseline": ("初始", "Start"),
    "tier.rebase": ("新判定重测", "Re-measured with new checks"),
    "tier.local": ("局部优化", "Local optimisation"),
    "tier.variables": ("放开变量", "More variables"),
    "tier.materials": ("换材料", "Materials"),
    "tier.global": ("全局优化", "Global optimisation"),
    "tier.structure": ("改结构", "Structure"),
    "kind.constant": ("常量", "Constant"),
    "kind.variable": ("变量范围", "Variable range"),
    "kind.objective": ("目标", "Objective"),
    "origin.user": ("用户", "user"),
    "origin.agent": ("agent 补充", "added by agent"),
    "weighted": ("{origin}，权重 {w}", "{origin}, weight {w}"),
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
    "chart.inf": ("∞：无有效值", "∞: no valid value"),
    "judge.achieved": ("第 {n} 轮所有带达标线的项都已满足", "every item with a pass line is met in round {n}"),
    "judge.limit": ("已用完轮次上限 max_rounds = {cap}", "used up max_rounds = {cap}"),
    "judge.exhausted": (
        "自第 {n} 轮以来 G 没有缩小 {rel:.0%} 以上，允许的手段（{tiers}）都已试过或排除",
        "G has not shrunk by {rel:.0%} or more since round {n}, and every allowed tier ({tiers}) was tried or ruled out",
    ),
    "judge.continue": ("尚未终止；当前手段不再见效时，下一个要试的是{tier}", "not finished; when the current approach stops gaining, try {tier} next"),
    "judge.stopped": ("第 {n} 轮后按用户决定停止。理由：{reason}。用户原话：{quote}", "stopped after round {n} by the user's decision. Reason: {reason}. The user's words: {quote}"),
    "progress": (
        "<b>最后一次明显进步</b>：第 {n} 轮；之后 {k} 轮没有明显进步，还没试过的手段：{untried}",
        "<b>Last clear gain</b>: round {n}; {k} round(s) since without a clear gain; tiers not yet tried: {untried}",
    ),
    "title": ("设计报告", "Design report"),
    "byline": ("saos 设计报告 · 任务 {job} · 生成于 {time}", "saos design report · job {job} · generated {time}"),
    "deliver": ("<b>交付镜头</b>：{lens}（第 {n} 轮，目标差距 G = {g}）", "<b>Delivered lens</b>: {lens} (round {n}, shortfall G = {g})"),
    "best_so_far": ("<b>目前最好的镜头</b>：{lens}（第 {n} 轮，目标差距 G = {g}）", "<b>Best lens so far</b>: {lens} (round {n}, shortfall G = {g})"),
    "best_blocked": ("<b>差距最小但不可交付的镜头</b>：{lens}（第 {n} 轮，目标差距 G = {g}），原因见下。",
                     "<b>Smallest gap, not deliverable</b>: {lens} (round {n}, shortfall G = {g}); see below why."),
    "no_valid": ("<b>没有有效的设计</b>：所有轮次的目标差距都是 ∞，至少有一项测量的前提不成立（例如没有实焦点、零件交叠、光线到不了像面）。下面列出最后一轮 {lens}（第 {n} 轮）供诊断，它不是可用的设计。",
                 "<b>No valid design</b>: every round has an infinite shortfall; at least one measurement's precondition fails (no real focus, overlapping parts, rays missing the image). The last round {lens} (round {n}) is listed for diagnosis only; it is not a usable design."),
    "judgement": ("<b>终止判断</b>：{text}", "<b>Stop decision</b>: {text}"),
    "h.demand": ("需求", "Demand"),
    "h.summary": ("需求访谈总结", "Interview summary"),
    "h.constants": ("已知常量", "Constants"),
    "th.constants": ("项|内容|来源", "Item|Value|Source"),
    "h.variables": ("可调变量", "Variables"),
    "th.variables": ("项|内容|范围或候选", "Item|Description|Range or options"),
    "h.questions": ("待确认问题", "Open questions"),
    "blocking": ("（阻塞）", " (blocking)"),
    "wish": ("期望 {goal}", "wish {goal}"),
    "pursue.min": ("越小越好（理想值 {ideal}，尺度 {scale}）", "as small as possible (ideal {ideal}, scale {scale})"),
    "pursue.max": ("越大越好（理想值 {ideal}，尺度 {scale}）", "as large as possible (ideal {ideal}, scale {scale})"),
    "pursuit": ("离理想值 {g} 个尺度", "{g} scales from the ideal"),
    "h.materials": ("交付镜头的材料（第 {n} 轮）", "Materials of the delivered lens (round {n})"),
    "th.materials": ("面|材料|来源|各波长折射率", "Surface|Material|Source|Index per wavelength"),
    "mat.model": ("模型玻璃 nd {nd}，Vd {vd}（不是真实材料）", "model glass nd {nd}, Vd {vd} (not a real material)"),
    "mat.catalog": ("目录 {cats}", "catalog {cats}"),
    "mat.status": ("，{status}", ", {status}"),
    "mat.obsolete": ("，Obsolete（停产，通常买不到）", ", Obsolete (discontinued, usually unavailable)"),
    "mat.env": ("（环境或外购件）", " (environment or bought-in)"),
    "blocked": ("<b>没有可交付的镜头</b>：所有轮次都还含模型玻璃（第 {surfaces} 面），下面列的是探索中最好的一轮，不能作为设计交付。",
                "<b>No deliverable lens</b>: every round still uses a model glass (surfaces {surfaces}); the round below is the best exploration result and is not a deliverable design."),
    "blocked.hard": ("<b>没有可交付的镜头</b>：下面这一轮不满足常量或变量范围（{ids}），只是探索中最好的结果，不能作为设计交付。",
                     "<b>No deliverable lens</b>: the round below breaks a constant or variable range ({ids}); it is the best exploration result and is not a deliverable design."),
    "blocked.errors": ("<b>没有可交付的镜头</b>：下面这一轮有判定执行出错（{ids}），结论不可信。",
                       "<b>No deliverable lens</b>: checks of the round below failed to run ({ids}); its result cannot be trusted."),
    "blocked.clash": ("<b>没有可交付的镜头</b>：下面这一轮的零件交叠（{what}），物理上不成立，不能作为设计交付。",
                      "<b>No deliverable lens</b>: parts of the round below clash ({what}); it is physically impossible and is not a deliverable design."),
    "model_ok": ("建模说明：{model}（用户已确认）", "Model description: {model} (approved by the user)"),
    "confirmed": ("用户确认：{quote}", "Confirmed by the user: {quote}"),
    "not_recorded": ("（未记录）", "(not recorded)"),
    "h.items": ("逐项结果（第 {n} 轮）", "Results by item (round {n})"),
    "th.items": ("项|类别|达标线|实测值|结论|差多少|实测依据", "Item|Kind|Pass line|Measured|Verdict|Gap|Evidence"),
    "note.g": (
        "目标差距 G 是所有带达标线的项的单项差距、加上越小/越大越好的目标离理想值的距离，按权重的加权和，每项先按其尺度归一化；用户的目标权重高，agent 补充的低。是否达成只看达标线；越小/越大越好的目标在达标后继续改进，交付达成轮次中 G 最小的一轮。期望值只报告。常量或变量范围不满足的轮次不能交付。",
        "The shortfall G is the weighted sum of the normalised gaps of every item with a pass line plus the distance to the ideal of every objective to minimise or maximise; the user's objectives weigh more than those the agent added. Only the pass lines decide whether the demand is met; objectives to minimise or maximise keep improving afterwards, and the achieved round with the smallest G is delivered. Wishes are reported only. A round that breaks a constant or variable range is not deliverable.",
    ),
    "h.process": ("调试过程", "Rounds"),
    "merit.agent": ("（agent 报告）", " (reported by agent)"),
    "th.rounds": ("轮次|手段|说明|G|评价函数|镜头", "Round|Tier|Note|G|Merit function|Lens"),
    "note.merit": (
        "评价函数每轮可能被调整，不能跨轮比较；判断进展只看 G。标“agent 报告”的值是 agent 在 OpticStudio 评价函数之外实际优化的目标值，不是 OpticStudio 实测。",
        "The merit function may change between rounds and is not comparable across them; progress is judged by G only. Values marked \"reported by agent\" are the objective the agent actually optimised outside the OpticStudio merit function, not OpticStudio measurements.",
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

    def tiers(self, record: dict[str, Any]) -> str:
        return self("list_sep").join(self.tier(x) for x in ledger.tiers_of(record))


CSS = """
:root{--bg:#fbfaf7;--fg:#1f2328;--muted:#5d646d;--line:#d9dde3;--card:#ffffff;--ok:#1a7f37;--bad:#c4321b;--warn:#9a6700;--accent:#2f5d8a;}
@media (prefers-color-scheme: dark){:root{--bg:#15171a;--fg:#e6e8eb;--muted:#a0a7b1;--line:#30353c;--card:#1d2024;--ok:#4cc26a;--bad:#ff7b65;--warn:#d9a733;--accent:#7fb0e0;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 system-ui,"Microsoft YaHei","PingFang SC",sans-serif}
main{max-width:1080px;margin:0 auto;padding:24px 16px 64px}h1{font-size:24px;margin:0 0 4px}h2{font-size:18px;margin:32px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:16px;margin:12px 0}
.status{display:inline-block;padding:2px 10px;border-radius:999px;font-weight:600;border:1px solid currentColor}
.achieved{color:var(--ok)}.exhausted,.limit,.stopped{color:var(--bad)}.continue{color:var(--warn)}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600}.num{font-variant-numeric:tabular-nums;white-space:nowrap}.nowrap,th{white-space:nowrap}.pass{color:var(--ok);font-weight:600}.fail{color:var(--bad);font-weight:600}
blockquote{margin:8px 0;padding:8px 12px;border-left:3px solid var(--accent);background:var(--card)}code{font-size:13px}
.wrap{overflow-x:auto}svg text{fill:var(--muted);font-size:11px}svg .line{stroke:var(--accent);fill:none;stroke-width:2}svg .axis{stroke:var(--line)}
svg .dot{fill:var(--accent)}svg .dot.ok{fill:var(--ok)}svg .dot.inf{fill:var(--bg);stroke:var(--bad);stroke-width:2}img{max-width:100%;border:1px solid var(--line);border-radius:6px}
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
    # 只在判定为未达标时调用（判定带容差）；这里只把超出量写出来 / called for unmet items only; this only words the excess
    excess = demand.excess(goal, value)
    return t("met") if excess <= 0 else t("over", gap=fmt(excess))


def evidence_text(item: dict[str, Any], t: Tr) -> str:
    if item.get("error"):
        return e(item["error"])
    ev = item.get("evidence") or {}
    if "operand" in ev:
        op = ev["operand"]
        return f"{t('ev.operand')} <code>{e(op.get('type'))}</code> int1={e(op.get('int1', 0))} int2={e(op.get('int2', 0))} data={e(op.get('data', []))}"
    if "analysis" in ev:
        link = f'<a href="../{e(ev["text"])}">{e(ev["text"])}</a>' if ev.get("text") else "—"
        return t("ev.analysis", name=e(ev["analysis"]), path=e(ev["path"]), link=link)
    if "script" in ev:
        return f'{t("ev.script")} <a href="../{e(ev["script"])}">{e(ev["script"])}</a>'
    return e(item.get("error", "—"))


def chart(rounds: list[dict[str, Any]], t: Tr) -> str:
    """目标差距 G 随轮次的折线；G = ∞（无有效值）的轮次画在顶部单独的一条带里，空心点，不和有限值混在同一高度。"""
    if not rounds:
        return ""
    width, height, left, bottom, top, band = 760, 240, 52, 34, 14, 22
    finite = [r["shortfall"] for r in rounds if math.isfinite(r["shortfall"])]
    peak = max(finite) if finite else 1.0
    peak = peak if peak > 0 else 1.0
    n = max(len(rounds) - 1, 1)
    has_inf = len(finite) < len(rounds)
    # 有 ∞ 时顶部留出一条带 / reserve a band at the top when some rounds are infinite
    low = top + (band if has_inf else 0)

    def x(k: int) -> float:
        return left + (width - left - 16) * k / n

    def y(g: float) -> float:
        if not math.isfinite(g):
            return top
        return low + (height - low - bottom) * (1 - g / peak)

    segments, current = [], []
    for k, r in enumerate(rounds):
        if math.isfinite(r["shortfall"]):
            current.append(f"{x(k):.1f},{y(r['shortfall']):.1f}")
        elif current:
            segments.append(current)
            current = []
    if current:
        segments.append(current)
    lines = "".join(f'<polyline class="line" points="{" ".join(seg)}"/>' for seg in segments if len(seg) > 1)
    dots = "".join(
        f'<circle class="dot{" ok" if r.get("achieved") else ""}{" inf" if not math.isfinite(r["shortfall"]) else ""}" cx="{x(k):.1f}" cy="{y(r["shortfall"]):.1f}" r="4">'
        f'<title>{t("chart.round", n=r["round"])} · {e(t.tiers(r))} · G = {fmt(r["shortfall"])}</title></circle>'
        f'<text x="{x(k):.1f}" y="{height - 18}" text-anchor="middle">{r["round"]}</text>'
        for k, r in enumerate(rounds)
    )
    inf_axis = (f'<text x="{left - 6}" y="{top + 4}" text-anchor="end">∞</text>'
                f'<line class="axis" x1="{left}" y1="{low - band / 2:.1f}" x2="{width - 8}" y2="{low - band / 2:.1f}" stroke-dasharray="4 4"/>') if has_inf else ""
    legend = f'<text x="{width - 8}" y="{top + 4}" text-anchor="end">{t("chart.inf")}</text>' if has_inf else ""
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="{t("chart.label")}">'
        f'<line class="axis" x1="{left}" y1="{height - bottom}" x2="{width - 8}" y2="{height - bottom}"/>'
        f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}"/>{inf_axis}{legend}'
        f'<text x="{left - 6}" y="{low + 4}" text-anchor="end">{fmt(peak)}</text><text x="{left - 6}" y="{height - bottom}" text-anchor="end">0</text>'
        f'{lines}{dots}<text x="{(width + left) / 2}" y="{height - 2}" text-anchor="middle">{t("chart.x")}</text></svg>'
    )


def judgement(decision: dict[str, Any], stop: dict[str, Any], t: Tr) -> str:
    """ledger 的 reason 是给 agent 看的英文，报告里按状态重写。"""
    status = decision["status"]
    if status == "achieved":
        return t("judge.achieved", n=decision["round"])
    if status == "limit":
        return t("judge.limit", cap=stop["max_rounds"])
    if status == "stopped":
        s = decision["stopped"]
        return t("judge.stopped", n=s["round"], reason=s["reason"], quote=s["quote"])
    if status == "exhausted":
        tiers = t("list_sep").join(t.tier(x) for x in ledger.TIERS if x in stop["tiers"])
        return t("judge.exhausted", n=decision["last_gain_round"], rel=stop["rel"], tiers=tiers)
    return t("judge.continue", tier=t.tier(decision["next_tier"]))


def build(job: jobs.Job) -> dict[str, Any]:
    d = demand.load_job(job.root)
    # 报告只引用完好的证据：封存过的文件被改动或删除时拒绝生成
    # a report cites intact evidence only: refuse when a sealed file was changed or deleted
    problems = job.verify_seals()
    if problems:
        raise SaosError(ErrorCode.EVIDENCE_TAMPERED, f"{len(problems)} sealed evidence file(s) were changed or deleted; see details", {"problems": problems[:50]})
    rounds = ledger.load(job.root / ledger.LEDGER_FILE)
    if not rounds:
        raise SaosError(ErrorCode.BAD_ARGS, f"job {job.id} has no recorded rounds; run saos round first")
    current = (demand.digest(d), demand.method_digest(demand.prepare_scripts(job.root, d)))
    if ledger.version(rounds[-1]) != current:
        raise SaosError(ErrorCode.SPEC_CHANGED, "no round matches the current demand and check scripts; record a new round before reporting")
    rounds = ledger.current_window(rounds)
    decision = ledger.decide(rounds, d["stop"], ledger.stop_for(job.root, current[0]), ledger.ruled_out_for(job.root, current[0]))
    final = rounds[-1]
    # 只交付不含模型玻璃、满足硬条件的轮次；都不满足时取最好的一轮并标明不可交付
    # only rounds without a model glass that keep the hard conditions are deliverable; otherwise show the best one marked as not deliverable
    candidates = [r for r in rounds if ledger.deliverable(r)] or rounds
    best = min(candidates, key=lambda r: (r["shortfall"], r["round"]))
    # 达成的轮次里取 G 最小的（有越小/越大越好的目标时 G 在达标后还会变小）；相同取最新的，它用的是当前的判定
    # among achieved rounds the smallest G (it keeps shrinking after the pass lines with objectives to pursue); the latest on a
    # tie, judged with the current checks
    achieved = [r for r in rounds if r.get("achieved") and ledger.deliverable(r)]
    deliver = min(achieved, key=lambda r: (r["shortfall"], -r["round"])) if achieved else best
    if not math.isfinite(deliver["shortfall"]):
        # 没有任何有效轮次时，列最后一轮供诊断，而不是挑一个"最好"的 / with no valid round, show the last one for diagnosis instead of a "best"
        deliver = final
    eval_file = job.root / deliver["evaluation"]
    if not eval_file.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"missing evaluation for round {deliver['round']}")
    evaluation = json.loads(eval_file.read_text(encoding="utf-8"))
    # 文字部分（总结、待确认问题）取当前文件；三类条目取这一轮实际判定时的版本，两者内容哈希相同
    # the wording (summary, open questions) comes from the current file; the sections from the judged version, which hashes the same
    shown = {**evaluation["demand"], "summary": d["summary"], "open_questions": d.get("open_questions") or [], "lang": d.get("lang")}
    return {"demand": shown, "confirmed": demand.confirmation_for(job.root, current[0]), "rounds": rounds, "decision": decision,
            "deliver": deliver, "items": evaluation["items"], "blocked": not ledger.deliverable(deliver)}


def headline(status: str, deliver: dict[str, Any], blocked: bool | None) -> str:
    """报告开头那一行：交付、目前最好、不可交付或没有有效设计。"""
    if not math.isfinite(deliver["shortfall"]):
        return "no_valid"
    if blocked:
        return "best_blocked"
    return "best_so_far" if status == "continue" else "deliver"


def header_row(spec: str) -> str:
    return "".join(f"<th>{c}</th>" for c in spec.split("|"))


def kind_note(item: dict[str, Any], t: Tr) -> str:
    """目标的来源和权重（只对计入 G 的目标显示权重）。"""
    if item.get("origin") is None:
        return ""
    origin = t("origin." + item["origin"])
    text = t("weighted", origin=origin, w=fmt(item.get("weight"))) if item.get("goal") or item.get("direction") else origin
    return f'<div class="muted">{e(text)}</div>'


def demand_section(h: dict[str, Any], t: Tr) -> str:
    """需求：访谈总结、已知常量、可调变量、待确认问题；目标在逐项结果表里。"""
    summary = e(h.get("summary", "")).replace("\n", "<br>")
    parts = [f"<h3>{t('h.summary')}</h3><blockquote>{summary}</blockquote>"]
    constants = h.get("constants") or []
    if constants:
        rows = "".join(
            f'<tr><td>{e(c["id"])}<div class="muted">{e(c.get("description", ""))}</div></td>'
            f'<td>{e(c["value"]) if "value" in c else goal_text(c.get("goal"), t)} {e(c.get("unit", ""))}</td><td>{e(c.get("source", ""))}</td></tr>'
            for c in constants)
        parts.append(f'<h3>{t("h.constants")}</h3><div class="wrap"><table><thead><tr>{header_row(t("th.constants"))}</tr></thead><tbody>{rows}</tbody></table></div>')
    variables = h.get("variables") or []
    if variables:
        def bound(v: dict[str, Any]) -> str:
            text = (goal_text(v["range"], t) + (f" {e(v['unit'])}" if v.get("unit") else "")) if v.get("range") else ""
            options = t("list_sep").join(e(o) for o in v.get("options") or [])
            return "<br>".join(x for x in (text, options) if x) or "—"
        rows = "".join(f'<tr><td>{e(v["id"])}</td><td>{e(v.get("description", ""))}</td><td>{bound(v)}</td></tr>' for v in variables)
        parts.append(f'<h3>{t("h.variables")}</h3><div class="wrap"><table><thead><tr>{header_row(t("th.variables"))}</tr></thead><tbody>{rows}</tbody></table></div>')
    questions = h.get("open_questions") or []
    if questions:
        items = "".join(f"<li>{e(q['question'])}{t('blocking') if q.get('blocking') else ''}</li>" for q in questions)
        parts.append(f"<h3>{t('h.questions')}</h3><ul>{items}</ul>")
    return "".join(parts)


def render(job: jobs.Job, data: dict[str, Any], lang: str) -> str:
    t = Tr(lang)
    h, rounds, decision, deliver = data["demand"], data["rounds"], data["decision"], data["deliver"]
    status = decision["status"]
    rows = []
    for item in data["items"]:
        goal = item.get("goal")
        preference = goal is None
        mark = "—" if preference else (f'<span class="pass">{t("met")}</span>' if item.get("passed") else f'<span class="fail">{t("unmet")}</span>')
        # 达标与否以判定结果为准（含容差），差多少只对未达标的项写出 / the verdict (with tolerance) decides; gaps only for unmet items
        gap = "—" if preference else t("met") if item.get("passed") else gap_text(goal, item.get("value"), t)
        line = goal_text(goal, t) if goal else ""
        if item.get("direction"):
            scale = item.get("scale", demand._default_scale(goal) if goal else None)
            pursue = t("pursue." + item["direction"], ideal=fmt(item.get("ideal", 0.0)), scale=fmt(scale))
            line = (line + "<br>" if line else "") + e(pursue)
            far = t("pursuit", g=fmt(item.get("pursuit")))
            gap = far if preference else f"{gap}<br>{e(far)}"
        if item.get("wish"):
            line = (line + "<br>" if line else "") + t("wish", goal=goal_text(item["wish"], t))
        rows.append(
            f'<tr><td>{e(item["id"])}<div class="muted">{e(item.get("description", ""))}</div></td><td class="nowrap">{e(t("kind." + item["kind"]))}{kind_note(item, t)}</td>'
            f'<td class="num">{line or "—"}</td><td class="num">{fmt(item.get("value"))}</td><td class="nowrap">{mark}</td>'
            f'<td class="num">{gap}</td><td>{evidence_text(item, t)}</td></tr>'
        )
    round_rows = "".join(
        f'<tr><td class="num">{r["round"]}</td><td>{e(t.tiers(r))}</td><td>{e(r.get("note", ""))}</td>'
        f'<td class="num">{fmt(r["shortfall"])}</td><td class="num">{fmt(r.get("merit"))}{t("merit.agent") if r.get("merit_source") == "agent" else ""}</td><td><a href="../lenses/{e(r["file"])}">{e(r["file"])}</a></td></tr>'
        for r in rounds
    )
    confirmed = (data.get("confirmed") or {}).get("quote", "")
    demand_html = demand_section(h, t)
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
    blocked = ""
    if data.get("blocked"):
        if deliver.get("measurement_errors"):
            text = t("blocked.errors", ids=e(", ".join(deliver["measurement_errors"])))
        elif deliver.get("hard_failing"):
            text = t("blocked.hard", ids=e(", ".join(deliver["hard_failing"])))
        elif deliver.get("parts_clash"):
            clash = deliver["parts_clash"]
            what = [f"{a}–{b}" for a, b in (c["surfaces"] for c in clash.get("crossings") or [])]
            what += [" / ".join("–".join(map(str, lens)) for lens in o["lenses"]) for o in clash.get("overlaps") or []]
            text = t("blocked.clash", what=e("; ".join(what)))
        else:
            text = t("blocked", surfaces=e(", ".join(map(str, deliver.get("model_glass") or []))))
        blocked = f'<div class="card">{text}</div>'
    mat_rows = []
    for m in deliver.get("materials") or []:
        if m["kind"] == "model":
            name, source = "MODEL", t("mat.model", nd=fmt(m.get("nd")), vd=fmt(m.get("vd")))
        else:
            name, source = m.get("name") or "", t("mat.catalog", cats=", ".join(m.get("catalogs") or []))
            if m.get("status"):
                source += t("mat.obsolete") if m["status"] == "Obsolete" else t("mat.status", status=m["status"])
        n = " / ".join(f"{v:.6f}" for v in m.get("n") or [])
        if m["surface"] in (deliver.get("environment") or []):
            source += t("mat.env")
        mat_rows.append(f'<tr><td class="num">{m["surface"]}</td><td>{e(name)}</td><td>{e(source)}</td><td class="num">{e(n)}</td></tr>')
    model_line = f'<p class="muted">{t("model_ok", model=e(deliver["model"]))}</p>' if deliver.get("model") else ""
    materials = (f'<div class="wrap"><table><thead><tr>{header_row(t("th.materials"))}</tr></thead><tbody>{"".join(mat_rows)}</tbody></table></div>'
                 if mat_rows else f"<p>{t('none')}</p>") + model_line
    return f"""<!doctype html><html lang="{'zh-CN' if lang == 'zh' else 'en'}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{t('title')} · {e(job.id)}</title><style>{CSS}</style></head><body><main>
<p class="muted">{t('byline', job=e(job.id), time=e(jobs.now_iso()))}</p>
<h1>{e(job.read_state().get('name', job.id))} <span class="status {status}">{t('status.' + status)}</span></h1>
<p>{t('summary.' + status)}</p>
<div class="card">{t(headline(status, deliver, data.get('blocked')), lens=lens_link, n=deliver['round'], g=fmt(deliver['shortfall']))}
<br>{t('judgement', text=e(judgement(decision, h['stop'], t)))}{progress}</div>
{blocked}
<h2>{t('h.materials', n=deliver['round'])}</h2>{materials}
<h2>{t('h.demand')}</h2>{demand_html}
<p class="muted">{t('confirmed', quote=e(confirmed) or t('not_recorded'))}</p>
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
    chosen = i18n.resolve(lang, data["demand"].get("lang"))
    folder = job.root / "report"
    folder.mkdir(exist_ok=True)
    html = render(job, data, chosen)
    # 每次生成都留一份编号副本并封存；index.html 始终是最新一份
    # every generation is kept as a numbered, sealed copy; index.html is always the latest
    archived = folder / f"{jobs.next_archive_number(folder):03d}-r{data['deliver']['round']:03d}-{data['decision']['status']}.html"
    archived.write_text(html, encoding="utf-8")
    job.seal(archived)
    out = folder / "index.html"
    out.write_text(html, encoding="utf-8")
    return {
        "archived": archived.relative_to(job.root).as_posix(),
        "report": str(out),
        "lang": chosen,
        "status": data["decision"]["status"],
        "deliver": data["deliver"]["file"],
        "shortfall": data["deliver"]["shortfall"],
    }
