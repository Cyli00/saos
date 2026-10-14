"""交付报告：由任务目录中的需求框定和 ledger 生成单文件 HTML（给用户看，中文）。

    saos report --job J      # 写到任务的 report/index.html

只读取已有记录，不重新计算，也不连接 OpticStudio；所有数值都能追溯到 ledger、analyses/、scripts/。
"""

from __future__ import annotations

import json
import math
from html import escape
from typing import Any


from saos import harness, jobs, ledger
from saos.protocol import ErrorCode, SaosError

STATUS_TEXT = {
    "achieved": ("达成", "所有约束和性能指标都已达标。"),
    "exhausted": ("无法再优化", "自最后一次明显进步以来，允许的每一级手段都试过，目标差距没有再明显缩小。下表列出未达标的项和差距。"),
    "limit": ("达到轮次上限", "轮次用完时仍有未达标的项。下表列出差距。"),
    "continue": ("进行中", "设计尚未结束，以下是截至最后一轮的状态。"),
}
TIER_TEXT = {"baseline": "初始", "local": "局部优化", "variables": "放开变量", "materials": "换材料", "global": "全局优化", "structure": "改结构"}
KIND_TEXT = {"constraint": "约束", "target": "性能指标", "preference": "期望项"}

CSS = """
:root{--bg:#fbfaf7;--fg:#1f2328;--muted:#5d646d;--line:#d9dde3;--card:#ffffff;--ok:#1a7f37;--bad:#c4321b;--warn:#9a6700;--accent:#2f5d8a;}
@media (prefers-color-scheme: dark){:root{--bg:#15171a;--fg:#e6e8eb;--muted:#a0a7b1;--line:#30353c;--card:#1d2024;--ok:#4cc26a;--bad:#ff7b65;--warn:#d9a733;--accent:#7fb0e0;}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.6 "Microsoft YaHei","PingFang SC",system-ui,sans-serif}
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


def goal_text(goal: dict[str, Any] | None) -> str:
    if not goal:
        return "—"
    if "equals" in goal:
        return f"= {fmt(float(goal['equals']))} ± {fmt(float(goal['tol']))}"
    parts = []
    if "min" in goal:
        parts.append(f"≥ {fmt(float(goal['min']))}")
    if "max" in goal:
        parts.append(f"≤ {fmt(float(goal['max']))}")
    return "，".join(parts)


def gap_text(goal: dict[str, Any] | None, value: float | None) -> str:
    """差多少：用目标本身的单位写出超出达标线的量。"""
    if not goal or value is None:
        return "无法取值" if goal else "—"
    if "equals" in goal:
        excess = abs(value - float(goal["equals"])) - float(goal["tol"])
    elif "max" in goal and value > float(goal["max"]):
        excess = value - float(goal["max"])
    elif "min" in goal and value < float(goal["min"]):
        excess = float(goal["min"]) - value
    else:
        excess = 0.0
    return "达标" if excess <= 0 else f"超出 {fmt(excess)}"


def evidence_text(item: dict[str, Any]) -> str:
    ev = item.get("evidence") or {}
    if "operand" in ev:
        op = ev["operand"]
        return f"操作数 <code>{e(op.get('type'))}</code> int1={e(op.get('int1', 0))} int2={e(op.get('int2', 0))} data={e(op.get('data', []))}"
    if "analysis" in ev:
        return f"分析 <code>{e(ev['analysis'])}</code> 取 <code>{e(ev['path'])}</code>，全文 <a href=\"../{e(ev['text'])}\">{e(ev['text'])}</a>"
    if "script" in ev:
        return f"脚本 <a href=\"../{e(ev['script'])}\">{e(ev['script'])}</a>"
    return e(item.get("error", "—"))


def chart(rounds: list[dict[str, Any]]) -> str:
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
        f'<circle class="dot{" ok" if r.get("achieved") else ""}" cx="{x(k):.1f}" cy="{y(r["shortfall"]):.1f}" r="4"><title>第 {r["round"]} 轮 · {e(TIER_TEXT.get(r.get("tier"), r.get("tier")))} · G = {fmt(r["shortfall"])}</title></circle>'
        f'<text x="{x(k):.1f}" y="{height - 18}" text-anchor="middle">{r["round"]}</text>'
        for k, r in enumerate(rounds)
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" role="img" aria-label="目标差距随轮次的变化">'
        f'<line class="axis" x1="{left}" y1="{height - bottom}" x2="{width - 8}" y2="{height - bottom}"/>'
        f'<line class="axis" x1="{left}" y1="{top}" x2="{left}" y2="{height - bottom}"/>'
        f'<text x="{left - 6}" y="{top + 4}" text-anchor="end">{fmt(peak)}</text><text x="{left - 6}" y="{height - bottom}" text-anchor="end">0</text>'
        f'<polyline class="line" points="{points}"/>{dots}<text x="{(width + left) / 2}" y="{height - 2}" text-anchor="middle">轮次</text></svg>'
    )


def judgement(decision: dict[str, Any], stop: dict[str, Any]) -> str:
    """ledger 的 reason 是给 agent 看的英文，报告里按状态重写成中文。"""
    status = decision["status"]
    if status == "achieved":
        return f"第 {decision['round']} 轮所有约束和性能指标达标"
    if status == "limit":
        return f"已用完轮次上限 max_rounds = {stop['max_rounds']}"
    tiers = "、".join(TIER_TEXT[t] for t in ledger.TIERS if t in stop["tiers"])
    if status == "exhausted":
        return f"自第 {decision['last_gain_round']} 轮以来 G 没有缩小 {stop['rel']:.0%} 以上，允许的手段（{tiers}）都已试过"
    return f"尚未终止；当前手段不再见效时升级到{TIER_TEXT[decision['next_tier']]}"


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


def render(job: jobs.Job, data: dict[str, Any]) -> str:
    h, rounds, decision, deliver = data["harness"], data["rounds"], data["decision"], data["deliver"]
    status = decision["status"]
    title, summary = STATUS_TEXT[status]
    targets = {t["id"]: t for t in h["targets"]}
    rows = []
    for item in data["items"]:
        target = targets.get(item["id"], {})
        goal = target.get("goal")
        passed = item.get("passed")
        mark = "—" if item["kind"] == "preference" else ('<span class="pass">达标</span>' if passed else '<span class="fail">未达标</span>')
        rows.append(
            f"<tr><td>{e(item['id'])}<div class=\"muted\">{e(item.get('description', ''))}</div></td><td class=\"nowrap\">{e(KIND_TEXT[item['kind']])}</td>"
            f"<td class=\"num\">{goal_text(goal)}</td><td class=\"num\">{fmt(item.get('value'))}</td><td class=\"nowrap\">{mark}</td>"
            f"<td class=\"num\">{gap_text(goal, item.get('value')) if item['kind'] != 'preference' else '—'}</td><td>{evidence_text(item)}</td></tr>"
        )
    round_rows = "".join(
        f"<tr><td class=\"num\">{r['round']}</td><td>{e(TIER_TEXT.get(r.get('tier'), r.get('tier')))}</td><td>{e(r.get('note', ''))}</td>"
        f"<td class=\"num\">{fmt(r['shortfall'])}</td><td class=\"num\">{fmt(r.get('merit'))}</td><td><a href=\"../lenses/{e(r['file'])}\">{e(r['file'])}</a></td></tr>"
        for r in rounds
    )
    assumptions = "".join(f"<li>{e(a)}</li>" for a in h.get("assumptions") or []) or "<li>无</li>"
    confirmed = (h.get("confirmed") or {}).get("quote", "")
    evidence = []
    for folder, label in (("analyses", "分析导出"), ("scripts", "脚本执行")):
        files = sorted((job.root / folder).glob("*.json")) if (job.root / folder).is_dir() else []
        if files:
            links = "".join(f'<li><a href="../{folder}/{e(p.name)}">{e(p.name)}</a></li>' for p in files)
            evidence.append(f"<h3>{label}（{len(files)}）</h3><ul>{links}</ul>")
    shots = sorted((job.root / "screens").glob("*.png")) if (job.root / "screens").is_dir() else []
    if shots:
        evidence.append("<h3>窗口截图</h3>" + "".join(f'<p><img src="../screens/{e(p.name)}" alt="{e(p.name)}"></p>' for p in shots[-4:]))
    untried = "、".join(TIER_TEXT[t] for t in decision["untried_tiers"]) or "无"
    progress = (
        ""
        if status == "achieved"
        else f"<br><b>最后一次明显进步</b>：第 {decision['last_gain_round']} 轮；之后 {decision['rounds_without_gain']} 轮没有明显进步，还没试过的手段：{e(untried)}"
    )
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>设计报告 · {e(job.id)}</title><style>{CSS}</style></head><body><main>
<p class="muted">saos 设计报告 · 任务 {e(job.id)} · 生成于 {e(jobs.now_iso())}</p>
<h1>{e(job.read_state().get('name', job.id))} <span class="status {status}">{title}</span></h1>
<p>{summary}</p>
<div class="card"><b>交付镜头</b>：<a href="../lenses/{e(deliver['file'])}">{e(deliver['file'])}</a>（第 {deliver['round']} 轮，目标差距 G = {fmt(deliver['shortfall'])}）
<br><b>终止判断</b>：{e(judgement(decision, h['stop']))}{progress}</div>
<h2>需求</h2><blockquote>{e(h['request'])}</blockquote>
<p class="muted">用户确认：{e(confirmed) or '（未记录）'}</p><h3>假设</h3><ul>{assumptions}</ul>
<h2>逐项结果（第 {deliver['round']} 轮）</h2><div class="wrap"><table><thead><tr><th>目标</th><th>类别</th><th>达标线</th><th>实测值</th><th>结论</th><th>差多少</th><th>实测依据</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div>
<p class="muted">目标差距 G 是约束和性能指标的单项差距之和，每项按其尺度归一化；期望项只报告数值，不决定是否达成。</p>
<h2>调试过程</h2><div class="card">{chart(rounds)}</div>
<div class="wrap"><table><thead><tr><th>轮次</th><th>手段</th><th>说明</th><th>G</th><th>评价函数</th><th>镜头</th></tr></thead><tbody>{round_rows}</tbody></table></div>
<p class="muted">评价函数每轮可能被调整，不能跨轮比较；判断进展只看 G。</p>
<h2>实测依据</h2>{''.join(evidence) or '<p>无</p>'}
<h2>后续</h2><p>如果需要评估加工和装配误差的影响，可以进一步做公差分析。</p>
</main></body></html>"""


def write(job: jobs.Job) -> dict[str, Any]:
    data = build(job)
    out = job.root / "report" / "index.html"
    out.parent.mkdir(exist_ok=True)
    out.write_text(render(job, data), encoding="utf-8")
    return {"report": str(out), "status": data["decision"]["status"], "deliver": data["deliver"]["file"], "shortfall": data["deliver"]["shortfall"]}

