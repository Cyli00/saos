"""调试轮次记录与终止判断（DESIGN.md「终止规则」）。
Round ledger and stop decision (DESIGN.md, "终止规则" / stop rule).

每个设计任务一份 ledger.jsonl：第 0 轮是初始状态（tier 为 baseline），之后每轮一条，记录本轮使用的
手段层级、各项的实测值和目标差距 G。
One ledger.jsonl per design job: round 0 is the starting state (tier "baseline"); every later round records
its strategy tier, the measured value of every check and the shortfall G.

终止判断的依据（参考李晓彤《几何光学·像差·光学设计》第 17、18 章）：阻尼最小二乘只收敛到局部极小；
变量要分批放开；全局搜索和 Hammer 优化能跳出局部极小，但会一直运行到人为中断。所以"几轮没进步"
不足以说明无法再优化，还要看是否已经逐级升级过手段。层级从低到高：
Why tiers (after Li Xiaotong, Geometrical Optics, Aberrations and Optical Design, ch. 17–18): damped least
squares only reaches a local minimum, variables are freed in batches, and global search / Hammer escape
local minima but run until stopped. "No gain for a few rounds" therefore says nothing unless the strategy
has also been escalated. Tiers, low to high:

    local      在现有变量下局部优化 / local optimisation on the current variables
    variables  放开更多变量或调整评价函数的构成与权重 / more variables, or a different merit construction
    materials  在有来源的真实材料之间替换 / substitute among sourced real materials
    global     全局搜索或 Hammer，也包括调用 ZOS-API 的多起点扫描 / global search or Hammer, incl. a multi-start scan through ZOS-API
    structure  改结构 / change the structure

判定（按顺序）/ Decision, in this order:
    achieved   本轮所有带界限的条目（常量、变量范围、目标的 must）都达标，且不含模型玻璃；需求里有越小/越大越好的目标
               （minimize / maximize）时，还要自最后一次明显进步以来允许的每个层级都试过或排除，或者轮次数到了 max_rounds
               every bounded item (constants, variable ranges, objectives' must) is met and the lens has no model glass; with
               objectives to minimise or maximise, also every allowed tier tried or ruled out since the last significant gain,
               or max_rounds reached
    limit      当前需求版本下的轮次数达到 max_rounds / rounds of the current demand version reached max_rounds
    exhausted  自最后一次明显进步（G 比此前最好值至少再小 rel）以来，允许的每个层级都试过一轮或带证据排除（saos rule-out），仍没有明显进步
               since the last significant gain (G at least rel below the best so far) every allowed tier was tried or ruled out with evidence;
               只说明试过的手段不再见效，不证明物理上不可达 / says the strategies stopped working, not that the goal is physically out of reach
    stopped    用户决定停止（saos stop，带理由和用户原话）；停止也是一种结论
               the user decided to stop (saos stop, with a reason and the user's words); stopping is a conclusion too
    continue   其他情况；给出最后一次进步以来还没试过、也没排除的最低层级 / otherwise; suggests the lowest tier neither tried nor ruled out
一轮可以同时用几个层级（tiers）；排除某个层级要写明证据，只对最后一次进步之后有效，有了新进步就要重新判断。
A round may use several tiers at once; ruling a tier out needs the evidence and holds only until the next gain.
需求的实质内容一变（哈希不同），G 的定义就变了，进步从改动后的第一轮重新算起。
When the demand content changes (different hash), G means something else and progress is counted afresh.
判定代码改了，saos round 先用新判定重测此前最好的一轮（tier 为 rebase），之后的轮次和它比较：数值不变则窗口延续，
变了就从这条重测记录开始新窗口。
When the check code changes, saos round first re-measures the best earlier round with the new checks (tier "rebase")
and later rounds compare with it: unchanged numbers keep the window, changed ones start a new window at that record.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from saos import jobs
from saos.demand import TIERS
from saos.protocol import ErrorCode, SaosError, jsonable

LEDGER_FILE = "ledger.jsonl"
STOPS_FILE = "stops.jsonl"
RULED_OUT_FILE = "ruled_out.jsonl"
# 判定代码改动后对此前最好一轮的重测记录 / the re-measurement of the best earlier round after the check code changed
REBASE = "rebase"


def load(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    for r in rows:
        r["shortfall"] = float(r["shortfall"])
    return rows


def append(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(jsonable(record), ensure_ascii=False) + "\n")


def stops(root: Path) -> list[dict[str, Any]]:
    path = root / STOPS_FILE
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def stop_for(root: Path, demand_sha: str) -> dict[str, Any] | None:
    """这一版需求下最近一次停止决定；需求改了版本就不再适用。
    The latest stop decision under this demand version; a new version starts afresh."""
    return next((s for s in reversed(stops(root)) if s.get("demand_sha") == demand_sha), None)


def record_stop(root: Path, demand_sha: str, reason: str, quote: str) -> dict[str, Any]:
    """记录用户的停止决定。停止等于接受达不到需求，所以必须有用户原话。
    Record the user's decision to stop. Stopping accepts that the demand is not met, so it needs the user's words."""
    reason, quote = str(reason or "").strip(), str(quote or "").strip()
    if not reason or not quote:
        raise SaosError(ErrorCode.BAD_ARGS, "a stop needs --reason (why, with the evidence) and --quote (the user's own words deciding to stop)")
    rounds = load(root / LEDGER_FILE)
    if not rounds or version(rounds[-1])[0] != demand_sha:
        raise SaosError(ErrorCode.BAD_ARGS, "record a round under the current demand before stopping; the stop refers to the last round")
    record = {"round": int(rounds[-1]["round"]), "demand_sha": demand_sha, "reason": reason, "quote": quote, "time": jobs.now_iso()}
    append(root / STOPS_FILE, record)
    return record


def tiers_of(record: dict[str, Any]) -> list[str]:
    """本轮用到的层级；旧记录只有 tier 一个字段。/ the tiers a round used; older records have a single tier."""
    return [t for t in (record.get("tiers") or [record.get("tier")]) if t]


def ruled_out_for(root: Path, demand_sha: str) -> list[dict[str, Any]]:
    path = root / RULED_OUT_FILE
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("demand_sha") == demand_sha]


def record_rule_out(root: Path, demand_sha: str, tiers: list[str], reason: str) -> dict[str, Any]:
    """带证据排除层级，免得为了走完阶梯去试与诊断结论无关的手段。只对最后一次进步之后有效。
    Rule tiers out with the evidence, so no round is spent on a tier the diagnosis says cannot help. Holds until the next gain."""
    reason = str(reason or "").strip()
    unknown = [t for t in tiers if t not in TIERS]
    if not tiers or unknown:
        raise SaosError(ErrorCode.BAD_ARGS, f"--tier must name tiers of the ladder ({', '.join(TIERS)}); got {unknown or tiers}")
    if not reason:
        raise SaosError(ErrorCode.BAD_ARGS, "--reason must say why the tier cannot help, citing the evidence (e.g. scans/NNN, a first-order result)")
    rounds = load(root / LEDGER_FILE)
    if not rounds or version(rounds[-1])[0] != demand_sha:
        raise SaosError(ErrorCode.BAD_ARGS, "record a round under the current demand first; a tier is ruled out after a given round")
    record = {"round": int(rounds[-1]["round"]), "demand_sha": demand_sha, "tiers": list(dict.fromkeys(tiers)), "reason": reason, "time": jobs.now_iso()}
    append(root / RULED_OUT_FILE, record)
    return record


def significant(best: float, g: float, rel: float) -> bool:
    if not math.isfinite(g):
        return False
    if not math.isfinite(best):
        return True
    return best > 0 and (best - g) / best >= rel


def version(record: dict[str, Any]) -> tuple[Any, Any]:
    # 旧记录的字段名是 harness_sha / older records call it harness_sha
    return record.get("demand_sha", record.get("harness_sha")), record.get("method_sha")


def is_rebase(record: dict[str, Any]) -> bool:
    return record.get("tier") == REBASE


def _continues(prev: dict[str, Any], record: dict[str, Any]) -> bool:
    if version(record) == version(prev):
        return True
    # 判定改了，但在旧镜头上重测的数值不变：仍是同一个比较窗口 / new checks, same numbers on the old lens: the same window
    return is_rebase(record) and bool(record.get("equivalent")) and version(record)[0] == version(prev)[0]


def current_window(rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    start = max((k for k, r in enumerate(rounds) if k == 0 or not _continues(rounds[k - 1], r)), default=0)
    return rounds[start:]


def current_version(rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """当前需求版本的轮次。判定改动开的新比较窗口仍在同一版本内：轮次上限和用户停止按版本算，改判定解除不了。
    The rounds of the current demand version. A new comparison window opened by changed checks stays inside it:
    the round budget and the user's stop hold per demand version, so changing the checks does not lift them."""
    start = max((k for k, r in enumerate(rounds) if k == 0 or version(rounds[k - 1])[0] != version(r)[0]), default=0)
    return rounds[start:]


def valid(record: dict[str, Any]) -> bool:
    """有判定执行出错的轮次不算数：不计进步、不算试过某层级。
    A round whose checks failed to run counts for nothing: no progress, no tier tried."""
    return not record.get("measurement_errors")


def deliverable(record: dict[str, Any]) -> bool:
    """含模型玻璃、测量出错、或常量/变量范围不满足的轮次不能交付。
    Rounds with a model glass, failed checks or a broken constant / variable range are not deliverable."""
    return not record.get("model_glass") and valid(record) and not record.get("hard_failing")


def decide(rounds: list[dict[str, Any]], stop: dict[str, Any], stopped: dict[str, Any] | None = None,
           ruled: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """rounds 为 ledger 中的全部轮次（含第 0 轮）；stopped 为这一版需求下用户的停止决定；ruled 为这一版需求下的层级排除记录。
    返回状态、理由和建议的下一层级。
    rounds: every round in the ledger (round 0 included); stopped: the user's stop decision under this demand version;
    ruled: the tiers ruled out under this demand version.
    Returns the status, the reason and the suggested next tier."""
    if not rounds:
        raise ValueError("ledger has no rounds")
    rel, cap, tiers = float(stop["rel"]), int(stop["max_rounds"]), [t for t in TIERS if t in stop["tiers"]]
    # 需求最后一次改动之后的轮次才可比 / only rounds since the last demand change are comparable
    window = current_window(rounds)
    best, last_gain = (window[0]["shortfall"] if valid(window[0]) else math.inf), 0
    for k, r in enumerate(window[1:], start=1):
        if not valid(r):
            continue
        if significant(best, r["shortfall"], rel):
            last_gain = k
        best = min(best, r["shortfall"])
    since = [r for r in window[last_gain + 1 :] if not is_rebase(r)]
    tried = {t for r in since if valid(r) for t in tiers_of(r)}
    gain_round = int(window[last_gain]["round"])
    excluded = {t for x in ruled or [] if int(x["round"]) >= gain_round for t in x["tiers"]}
    untried = [t for t in tiers if t not in tried and t not in excluded]
    # 重测记录不计入轮次上限 / re-measurements (rebase) do not count against max_rounds
    in_version = current_version(rounds)
    counted = sum(1 for r in in_version[1:] if not is_rebase(r))
    current = rounds[-1]
    number = int(current["round"])
    base = {
        "round": number,
        "best_shortfall": best,
        "last_gain_round": int(window[last_gain]["round"]),
        "rounds_without_gain": len(since),
        "tiers_tried_since_gain": sorted(tried & set(tiers), key=TIERS.index),
        "tiers_ruled_out_since_gain": sorted((excluded - tried) & set(tiers), key=TIERS.index),
        "untried_tiers": untried,
        # 轮次预算按需求版本计：用户确认新版本后重新计数 / the round budget counts per demand version
        "rounds_in_version": counted,
    }
    if current.get("achieved") and deliverable(current):
        if not current.get("pursuing"):
            return {**base, "status": "achieved", "reason": "every constant, variable range and objective pass line is met"}
        if current["shortfall"] <= 0:
            # 越小/越大越好的目标都已到理想值，G 为 0，不可能再有明显进步 / every pursued objective is at its ideal: G is 0 and cannot gain
            return {**base, "status": "achieved", "reason": "every pass line is met and every objective to minimise or maximise is at its ideal value"}
        # 达标后继续压越小/越大越好的目标，直到各手段都不再明显进步 / after the pass lines, keep pursuing until no tier gains
        if not untried or counted >= cap:
            return {**base, "status": "achieved",
                    "reason": "every pass line is met and the objectives to minimise or maximise gained less than "
                              f"{rel:.0%} since round {base['last_gain_round']} " + ("after trying or ruling out every allowed tier" if not untried
                                                                                     else f"within max_rounds = {cap}")}
        return {**base, "status": "continue", "next_tier": untried[0],
                "reason": f"every pass line is met; keep improving the objectives to minimise or maximise, next escalate to {untried[0]} "
                          "if the current tier stops gaining"}
    if stopped is not None and int(stopped["round"]) >= int(in_version[0]["round"]):
        return {**base, "status": "stopped", "stopped": stopped,
                "reason": f"the user decided to stop after round {stopped['round']}: {stopped['reason']}"}
    if counted >= cap:
        return {**base, "status": "limit", "reason": f"reached max_rounds = {cap} for this demand version"}
    if not valid(current):
        return {**base, "status": "continue", "next_tier": untried[0] if untried else None,
                "reason": f"checks failed to run ({', '.join(current['measurement_errors'])}); fix them and measure again; this round is not a design result"}
    if current.get("achieved"):
        # 指标达到了，但材料还不是真实材料：继续，换成有来源的材料
        # targets met with a model glass: keep going and replace it with a sourced material
        return {**base, "status": "continue", "next_tier": "materials",
                "reason": f"targets met, but surfaces {current['model_glass']} still use a model glass; replace it with a sourced material"}
    if not untried:
        return {
            **base,
            "status": "exhausted",
            "reason": f"no gain of at least {rel:.0%} since round {base['last_gain_round']} after trying or ruling out every allowed tier ({', '.join(tiers)})",
        }
    return {**base, "status": "continue", "reason": f"next escalate to {untried[0]} if the current tier stops gaining", "next_tier": untried[0]}
