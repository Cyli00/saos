"""需求（demand）：读取、校验 sources/demand.yaml，确认记录，以及在 OpticStudio 中逐项取值并判定。
Demand: read and validate sources/demand.yaml, record the user's confirmation, measure and judge every check in OpticStudio.

文件放在任务的 sources/demand.yaml，字段用英文（给 agent 读写），格式见 skills/saos/demand.md：
The file is the job's sources/demand.yaml; keys are English (the agent writes it); format in skills/saos/demand.md:

    schema: 2
    constants:     [{id, description, value, unit, source, check?, goal?}]       # 固定条件 / fixed conditions
    variables:     [{id, description, range?, options?, check?}]                  # 可调自由度 / design freedoms
    objectives:    [{id, description, check, origin, must?, wish?, weight?}]      # 目标 / measured goals
    open_questions: [{question, blocking}]
    summary: 简短访谈总结，最后一句固定 / short interview summary, fixed last sentence
    lang: zh
    stop: {rel, max_rounds, tiers}

判定方法三选一：operand（任意 Zemax 优化操作数）、analysis（运行一个分析，按 path 取值）、
script（任务内的脚本，run(ctx) 返回数值）。每一项都在 OpticStudio 中实际计算，满足"实测依据"。
A check is exactly one of: operand, analysis (pick a value by path) or script (run(ctx) returns a number).
Every value is computed in OpticStudio, which is what "measured evidence" means.

常量和变量范围是硬条件：任何一项不满足的轮次不能交付，不能靠其他目标变好抵消。
Constants and variable ranges are hard: a round that breaks one is not deliverable, whatever the other goals do.

目标按来源加权：用户给出或认可的目标权重高，agent 自己补充的权重低，且不得高于任何用户目标。
一个结构往往达不到全部目标，加权后的目标差距决定取舍时优先照顾用户的目标。
Objectives are weighted by origin: the user's weigh more, the agent's less and never more than any of the user's.
One structure often cannot meet every objective; the weighted shortfall then favours what the user asked for.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from saos import i18n, jobs
from saos.protocol import ErrorCode, SaosError, write_json

if TYPE_CHECKING:
    from saos.scripting import ScriptContext

DEMAND_FILE = "sources/demand.yaml"
LEGACY_FILE = "sources/harness.yaml"
CONFIRMATIONS_FILE = "confirmations.jsonl"
SECTIONS = ("constants", "variables", "objectives")
# 每类条目在判定结果里的 kind / the kind of each section's items in a judgement
KIND = {"constants": "constant", "variables": "variable", "objectives": "objective"}
HARD_KINDS = ("constant", "variable")
# 总结的最后一句（用户指定原文）/ the summary's last sentence (wording fixed by the user)
QUESTION_LINE = {"zh": "发现任何这个demand可能存在的问题，要大胆质疑和询问。",
                 "en": "If you find any possible problem with this demand, question it boldly and ask."}
# notes：不影响设计和判定的背景（来源文件的位置、做过的决定），不进版本 / notes: context that changes neither design nor judgement, not versioned
TOP_KEYS = {"schema", *SECTIONS, "open_questions", "summary", "notes", "lang", "stop"}
ITEM_KEYS = {
    "constants": {"id", "description", "value", "unit", "source", "check", "goal", "scale"},
    "variables": {"id", "description", "range", "options", "unit", "check", "scale", "source"},
    "objectives": {"id", "description", "unit", "check", "origin", "must", "wish", "weight", "scale", "source"},
}
# 目标的来源和默认权重：user = 用户给出或明确认可的，agent = agent 补充、用户没有表态的
# origin of an objective and its default weight: user = stated or explicitly accepted by the user, agent = filled in by the agent
DEFAULT_WEIGHT = {"user": 1.0, "agent": 0.3}
# 手段层级，从低到高；含义见 ledger.py / strategy tiers, low to high; see ledger.py
TIERS = ("local", "variables", "materials", "global", "structure")
DEFAULT_STOP = {"rel": 0.01, "max_rounds": 30, "tiers": list(TIERS)}
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
PATH_RE = re.compile(r"\['[^']*'\]|\[\d+\]|[A-Za-z_][A-Za-z0-9_ ]*")
# 比较界限时的相对容差：超出量不到尺度的这个比例就算达标，避免 9.999999999999954 µm 被判为没达到 10 µm
# relative tolerance at a bound: an excess below this fraction of the scale counts as met (9.999999999999954 µm meets ≥ 10 µm)
BOUND_TOLERANCE = 1e-9
CHECKS_DIR = "checks"
SUPPORT_KEY = "_checks_tree"


def _bad(message: str, **details: Any) -> SaosError:
    return SaosError(ErrorCode.BAD_ARGS, f"demand: {message}", details or None)


def path_of(root: Path) -> Path:
    """任务的需求文件；只有旧格式 harness.yaml 时明确报错，不自动转换。
    The job's demand file; a job with only the old harness.yaml is refused with a pointer, never converted silently."""
    path = root / DEMAND_FILE
    if path.is_file():
        return path
    if (root / LEGACY_FILE).is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"{LEGACY_FILE} is the old format; rewrite it as {DEMAND_FILE} (skills/saos/demand.md) and get it confirmed with saos demand confirm")
    raise SaosError(ErrorCode.FILE_NOT_FOUND, f"no {DEMAND_FILE}; frame the need with the user first")


def load(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise _bad(f"cannot read {path}: {exc}") from exc
    return validate(data)


def load_job(root: Path) -> dict[str, Any]:
    return load(path_of(root))


def digest(demand: dict[str, Any]) -> str:
    """需求实质内容的哈希：三类条目和终止规则。总结、待确认问题和语言不参与，改写它们不影响差距的可比性。
    Hash of what the demand means: the three sections and the stop rule. The summary, open questions and language
    are left out, so rewording them keeps G comparable."""
    body = {k: demand.get(k) for k in ("schema", *SECTIONS, "stop")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def checked(demand: dict[str, Any]) -> list[dict[str, Any]]:
    """所有带判定方法的条目，按 constants → variables → objectives 的顺序，统一成判定用的形式。
    Every entry with a check, in section order, in the form used for judging."""
    items = []
    for section in SECTIONS:
        for entry in demand.get(section) or []:
            if "check" not in entry:
                continue
            bound = {"constants": entry.get("goal"), "variables": entry.get("range"), "objectives": entry.get("must")}[section]
            item = {"id": entry["id"], "kind": KIND[section], "description": entry.get("description", ""), "check": entry["check"], "goal": bound,
                    "weight": weight(entry) if section == "objectives" else 1.0}
            if section == "objectives":
                item["origin"] = entry["origin"]
            if entry.get("wish") is not None:
                item["wish"] = entry["wish"]
            if "scale" in entry:
                item["scale"] = entry["scale"]
            items.append(item)
    return items


# 来源写成 "agent assumption: …" 表示没有外部依据 / a source starting with "agent assumption" has no outside basis
ASSUMPTION_RE = re.compile(r"^\s*agent\s+assumption", re.I)


def weak_basis(demand: dict[str, Any]) -> list[dict[str, Any]]:
    """依据薄弱的界限：常量的值或判定、变量范围、目标达标线中，没有来源或只是 agent 假设的。确认前要摆给用户看。
    Limits on a weak basis: constant values or goals, variable ranges and pass lines without a source or resting
    only on an agent assumption. The user sees them before confirming."""
    out = []
    for section in SECTIONS:
        for entry in demand.get(section) or []:
            bound = {"constants": entry.get("goal", entry.get("value")), "variables": entry.get("range"), "objectives": entry.get("must")}[section]
            if bound is None:
                continue
            source = str(entry.get("source") or "").strip()
            if not source or ASSUMPTION_RE.match(source):
                out.append({"id": entry["id"], "section": section, "bound": bound, "source": source or None})
    return out


def weight(entry: dict[str, Any]) -> float:
    return float(entry.get("weight", DEFAULT_WEIGHT[entry["origin"]]))


def blocking_questions(demand: dict[str, Any]) -> list[str]:
    return [q["question"] for q in demand.get("open_questions") or [] if q.get("blocking")]


def support_files(root: Path) -> dict[str, str]:
    """checks/ 下全部文件（判定脚本及其辅助模块、数据）的哈希；判定脚本只能依赖这里的文件。
    Hashes of every file under checks/ (scripts, helper modules, data); check scripts may depend only on these."""
    base = root / CHECKS_DIR
    if not base.is_dir():
        return {}
    return {f.relative_to(root).as_posix(): jobs.sha256_file(f) for f in sorted(base.rglob("*"))
            if f.is_file() and "__pycache__" not in f.parts}


def prepare_scripts(root: Path, demand: dict[str, Any]) -> dict[str, dict[str, Any]]:
    scripts = {}
    for item in checked(demand):
        if "script" not in item["check"]:
            continue
        rel = str(item["check"]["script"])
        path = (root / rel).resolve()
        if root.resolve() not in path.parents:
            raise SaosError(ErrorCode.PATH_OUTSIDE_JOB, f"path escapes the job directory: {rel}")
        if (root / CHECKS_DIR).resolve() not in path.parents:
            # 判定脚本及其依赖都放在 checks/ 下，才能整体留档和复现 / scripts and what they use live in checks/, archived as a whole
            raise SaosError(ErrorCode.BAD_ARGS, f"{item['id']}: check scripts live under {CHECKS_DIR}/ (got {rel})")
        try:
            source = path.read_text(encoding="utf-8")
            scripts[item["id"]] = {"source": source, "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest()}
        except (OSError, UnicodeError) as exc:
            scripts[item["id"]] = {"source": None, "sha256": None, "error": f"{type(exc).__name__}: {exc}"}
    # 辅助文件整体算进判定方法的版本：改了辅助模块或数据，比较窗口也重新开始
    # supporting files count towards the method version: editing a helper or a data file starts a new window
    files = support_files(root)
    scripts[SUPPORT_KEY] = {"source": None, "sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(), "files": files}
    return scripts


def method_digest(scripts: dict[str, dict[str, Any]]) -> str:
    versions = {key: value["sha256"] for key, value in scripts.items()}
    return hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()


def _check_goal(where: str, goal: Any) -> None:
    if not isinstance(goal, dict) or not ({"max", "min", "equals"} & set(goal)) or set(goal) - {"max", "min", "equals", "tol"}:
        raise _bad(f"{where} needs max, min or equals (with tol), nothing else")
    for key in ("min", "max", "equals"):
        if key in goal and not _finite_number(goal[key]):
            raise _bad(f"{where}.{key} must be a finite number")
    if "equals" in goal and ("min" in goal or "max" in goal):
        raise _bad(f"{where}: equals cannot be combined with min or max")
    if "min" in goal and "max" in goal and goal["min"] > goal["max"]:
        raise _bad(f"{where}: min must not exceed max")
    if "equals" in goal and not _positive(goal.get("tol")):
        raise _bad(f"{where}: equals needs a positive tol")


def _check_method(where: str, check: Any) -> None:
    if not isinstance(check, dict) or len(check) != 1 or next(iter(check)) not in ("operand", "analysis", "script"):
        raise _bad(f"{where}: check must have exactly one of operand / analysis / script")


def validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema") != 2:
        raise _bad("must be a mapping with schema: 2")
    unknown = set(data) - TOP_KEYS
    if unknown:
        # 只收需求本身；文献细节、过程指令放在 sources/ 的其他文件里引用 / the demand only; literature and process notes live elsewhere in sources/
        raise _bad(f"unknown fields {sorted(unknown)}; allowed: {sorted(TOP_KEYS)}")
    notes = data.get("notes")
    if notes is not None and not (isinstance(notes, list) and all(isinstance(n, str) and n.strip() for n in notes)):
        raise _bad("notes must be a list of non-empty strings")
    seen: set[str] = set()
    for section in SECTIONS:
        entries = data.get(section) or []
        if not isinstance(entries, list):
            raise _bad(f"{section} must be a list")
        for e in entries:
            if not isinstance(e, dict) or not ID_RE.match(str(e.get("id", ""))):
                raise _bad(f"each entry of {section} needs an id (letters, digits, _)", entry=e)
            where = f"{section}.{e['id']}"
            if e["id"] in seen:
                raise _bad(f"duplicate id {e['id']} (ids are unique across all sections)")
            seen.add(e["id"])
            extra = set(e) - ITEM_KEYS[section]
            if extra:
                raise _bad(f"{where}: unknown fields {sorted(extra)}; allowed: {sorted(ITEM_KEYS[section])}")
            if not str(e.get("description", "")).strip():
                raise _bad(f"{where}: description is required")
            if "check" in e:
                _check_method(where, e["check"])
            bounds = {"constants": ("goal",), "variables": ("range",), "objectives": ("must", "wish")}[section]
            for key in bounds:
                if e.get(key) is not None:
                    _check_goal(f"{where}.{key}", e[key])
            if section == "constants" and not str(e.get("source", "")).strip():
                raise _bad(f"{where}: source is required (the user, a datasheet or paper, or 'agent assumption: <why>')")
            if section != "objectives" and ("check" in e) != (e.get(bounds[0]) is not None):
                raise _bad(f"{where}: give {bounds[0]} and check together (a bound must be measured; a check needs a bound)")
            if section == "objectives":
                if "check" not in e:
                    raise _bad(f"{where}: an objective needs a check")
                if e.get("origin") not in DEFAULT_WEIGHT:
                    raise _bad(f"{where}: origin must be user (stated or explicitly accepted by the user) or agent (filled in by you)")
                if "weight" in e and (e.get("must") is None or not _positive(e["weight"])):
                    raise _bad(f"{where}: weight must be a positive number and needs must (only pass lines enter the shortfall)")
            if "options" in e and not (isinstance(e["options"], list) and e["options"]):
                raise _bad(f"{where}: options must be a non-empty list")
            for key in bounds[:1]:
                if e.get(key) is not None:
                    scale = e.get("scale", _default_scale(e[key]))
                    if not _positive(scale):
                        raise _bad(f"{where}: scale must be a positive number (the bound is 0, so give one explicitly)")
    if not data.get("objectives"):
        raise _bad("objectives must be a non-empty list")
    scored = [o for o in data["objectives"] if o.get("must") is not None]
    user = [weight(o) for o in scored if o["origin"] == "user"]
    over = [o["id"] for o in scored if o["origin"] == "agent" and user and weight(o) > min(user)]
    if over:
        # agent 补充的目标不能比用户的目标更重 / an objective you added never outweighs one the user gave
        raise _bad(f"agent objectives {over} weigh more than the lightest user objective ({min(user)})")
    questions = data.get("open_questions") or []
    if not isinstance(questions, list) or any(not isinstance(q, dict) or not str(q.get("question", "")).strip()
                                              or set(q) - {"question", "blocking"} or not isinstance(q.get("blocking", False), bool) for q in questions):
        raise _bad("open_questions must be a list of {question, blocking: true|false}")
    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        raise _bad("summary is required: a short account of the interview")
    if data.get("lang") is not None and data["lang"] not in i18n.LANGS:
        raise _bad(f"lang must be one of {i18n.LANGS} (the language the user writes in)")
    stop = {**DEFAULT_STOP, **(data.get("stop") or {})}
    if not (_positive(stop["rel"]) and isinstance(stop["max_rounds"], int) and stop["max_rounds"] > 0):
        raise _bad("stop.rel must be > 0 and stop.max_rounds a positive integer")
    if not stop["tiers"] or any(t not in TIERS for t in stop["tiers"]):
        raise _bad(f"stop.tiers must be a non-empty subset of {TIERS}")
    return {**data, "stop": stop}


def _finite_number(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _positive(x: Any) -> bool:
    return _finite_number(x) and x > 0


def _default_scale(goal: dict[str, Any]) -> float | None:
    if "equals" in goal:
        return float(goal["tol"])
    bound = goal.get("max", goal.get("min"))
    return abs(float(bound)) if bound is not None and float(bound) != 0 else None


def excess(goal: dict[str, Any], value: float) -> float:
    if "equals" in goal:
        return abs(value - float(goal["equals"])) - float(goal["tol"])
    if "max" in goal and "min" in goal:
        return max(value - float(goal["max"]), float(goal["min"]) - value)
    if "max" in goal:
        return value - float(goal["max"])
    return float(goal["min"]) - value


def shortfall(item: dict[str, Any], value: float | None) -> float:
    """单项差距：超出达标线的量除以尺度，达标为 0；没有有效数值时为无穷大。
    Per-item shortfall: the excess over the pass line divided by the scale; 0 when met, infinity without a valid value."""
    if value is None or not math.isfinite(value):
        return math.inf
    goal = item["goal"]
    scale = float(item.get("scale", _default_scale(goal)))
    over = excess(goal, value)
    return 0.0 if over <= BOUND_TOLERANCE * scale else over / scale


# ---------- 确认记录 / confirmations ----------

def confirmations(root: Path) -> list[dict[str, Any]]:
    path = root / CONFIRMATIONS_FILE
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def confirmation_for(root: Path, sha: str) -> dict[str, Any] | None:
    """用户对这一版需求内容的最近一次确认；确认绑定内容哈希，改了内容就要重新确认。
    The user's latest confirmation of this demand version; it is bound to the content hash."""
    return next((c for c in reversed(confirmations(root)) if c.get("demand_sha") == sha), None)


def confirm(root: Path, quote: str) -> dict[str, Any]:
    """记录用户对当前需求的确认原话。确认前须校验通过；没有 blocking 问题时才算可以开工。
    Record the user's words of approval for the current demand; it must validate first."""
    quote = str(quote or "").strip()
    if not quote:
        raise SaosError(ErrorCode.BAD_ARGS, "--quote must hold the user's own words of approval")
    path = path_of(root)
    d = load(path)
    sha = digest(d)
    weak = weak_basis(d)
    missing = [w["id"] for w in weak if w["source"] is None]
    if missing:
        raise SaosError(ErrorCode.BAD_ARGS, f"every limit needs a source (the user, a datasheet, a paper, a textbook table, or 'agent assumption: <why>'); missing for {missing}",
                        {"missing_source": missing})
    # 存下用户确认时看到的全文（含总结和待确认问题），之后改写总结也能追溯
    # keep the full text the user saw (summary and open questions included), so later rewording stays traceable
    # weak_basis：确认时依据只是 agent 假设的界限，应当已经逐条摆给用户 / limits resting on an agent assumption, which the user should have seen one by one
    record = {"demand_sha": sha, "quote": quote, "time": jobs.now_iso(), "weak_basis": weak, "content": path.read_text(encoding="utf-8")}
    with (root / CONFIRMATIONS_FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {**{k: v for k, v in record.items() if k != "content"}, "blocking_questions": blocking_questions(d)}


def show(root: Path) -> dict[str, Any]:
    """校验需求并给出版本、确认状态和待确认问题，不连接 OpticStudio。
    Validate the demand and return its version, confirmation state and open questions; no OpticStudio needed."""
    d = load_job(root)
    sha = digest(d)
    confirmed = confirmation_for(root, sha)
    return {"demand_sha": sha, "confirmed": confirmed, "blocking_questions": blocking_questions(d),
            "open_questions": [q["question"] for q in d.get("open_questions") or [] if not q.get("blocking")],
            "counts": {s: len(d.get(s) or []) for s in SECTIONS}, "checks": [i["id"] for i in checked(d)],
            "summary": d["summary"], "notes": d.get("notes") or [],
            # 确认前逐条告诉用户这些界限依据薄弱 / tell the user, item by item, that these limits rest on a weak basis before confirming
            "weak_basis": weak_basis(d)}


# ---------- 取值与判定 / measuring and judging ----------

def pick(data: Any, path: str) -> Any:
    """按 a.b[0].c 形式的路径取值，用于从分析导出结果中挑出一个数；键里有点号时写成 ['0.48']。
    Pick a value by a path like a.b[0].c from an analysis export; write keys containing dots as ['0.48']."""
    tokens = PATH_RE.findall(path)
    current = data
    for token in (t.strip() for t in tokens):
        if not token:
            continue
        if token.startswith("['"):
            current = current[token[2:-2]]
        elif token.startswith("["):
            current = current[int(token[1:-1])]
        else:
            current = current[token]
    return current


def measure(ctx: ScriptContext, item: dict[str, Any], folder: Path, scripts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """在 OpticStudio 中取一项的值；返回值和取值依据。
    Measure one item in OpticStudio; return the value and its evidence."""
    kind, spec = next(iter(item["check"].items()))
    evidence: dict[str, Any] = {}
    try:
        if kind == "operand":
            value = ctx.backend.operand(str(spec["type"]), int(spec.get("int1", 0)), int(spec.get("int2", 0)), [float(x) for x in spec.get("data", [])])
            evidence = {"operand": spec}
        elif kind == "analysis":
            text_path = folder / f"{item['id']}.txt"
            data = ctx.backend.analysis(str(spec["type"]), dict(spec.get("settings") or {}), text_path)
            write_json(folder / f"{item['id']}.json", data)
            evidence = {"analysis": spec["type"], "settings": spec.get("settings") or {}, "path": spec["path"],
                        "text": text_path.relative_to(ctx.job.root).as_posix() if data.get("text_file") else None}
            value = pick(data, str(spec["path"]))
        else:
            from saos import scripting

            script = scripts[item["id"]]
            if script["source"] is None:
                return {"value": None, "error": script["error"], "evidence": {"source": str(spec)}}
            copy = folder / f"{item['id']}.py"
            copy.write_text(script["source"], encoding="utf-8")
            evidence = {"script": copy.relative_to(ctx.job.root).as_posix(), "source": str(spec), "sha256": script["sha256"]}
            namespace: dict[str, Any] = {"__name__": f"saos_check_{item['id']}", "__file__": str(ctx.path(str(spec)))}
            exec(compile(script["source"], str(copy), "exec"), namespace)
            value = namespace["run"](scripting.ScriptContext(ctx._daemon, ctx.job, {"target": item["id"]}))
        value = float(value)
    except Exception as exc:  # noqa: BLE001 - 见下 / see below
        # 判定失败是可报告的结果，连接中断和阻塞仍交给后台进程处理。
        # a failed measurement is a reportable result; a lost connection or a blocked window still goes to the daemon
        if not ctx.backend.healthy() or (isinstance(exc, SaosError) and exc.code in (ErrorCode.ZOS_CONNECTION_LOST, ErrorCode.UI_BLOCKED)):
            raise
        return {"value": None, "error": f"{type(exc).__name__}: {exc}", "evidence": evidence}
    if not math.isfinite(value):
        # 判定正常执行、但这个设计给不出有效数值（例如光线到不了像面）：是设计结果，不是测量失败
        # the check ran but this design has no valid value (e.g. no ray reaches the image): a design result, not a failed measurement
        return {"value": None, "design_failure": True, "evidence": evidence}
    return {"value": value, "evidence": evidence}


def evaluate(ctx: ScriptContext, demand: dict[str, Any], scripts: dict[str, dict[str, Any]] | None = None,
             only: list[str] | None = None) -> dict[str, Any]:
    """逐项取值和判定。目标差距 G 是所有带界限条目的单项差距按权重的加权和；常量或变量范围不满足时记入 hard_failing。
    only 只测其中几项（探索用）：结果标为 partial，不宣布达成，G 只是这几项之和。
    Measure and judge every check. G is the weighted sum of the shortfalls of every bounded item; broken constants or
    variable ranges are listed in hard_failing. With only, measure just those items (exploration): the result is
    partial, never achieved, and G covers those items alone."""
    specs = checked(demand)
    if only is not None:
        unknown = sorted(set(only) - {s["id"] for s in specs})
        if unknown or not only:
            raise SaosError(ErrorCode.BAD_ARGS, f"--only needs ids of checks in the demand; unknown: {unknown}",
                            {"known": [s["id"] for s in specs]})
        specs = [s for s in specs if s["id"] in set(only)]
    scripts = prepare_scripts(ctx.job.root, demand) if scripts is None else scripts
    root = ctx.path("evaluations")
    root.mkdir(exist_ok=True)
    folder = root / f"{jobs.next_archive_number(root):03d}"
    folder.mkdir()
    # 判定所依赖的 checks/ 整体存一份副本，历史结果可以复现 / keep a copy of checks/ so that results can be reproduced
    support = scripts.get(SUPPORT_KEY, {}).get("files") or {}
    for rel in support:
        target_file = folder / "support" / rel
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_bytes((ctx.job.root / rel).read_bytes())
    items = []
    total = 0.0
    for index, spec in enumerate(specs):
        item_folder = folder / f"{index:03d}-{spec['id']}"
        item_folder.mkdir()
        started = time.monotonic()
        measured = measure(ctx, spec, item_folder, scripts)
        # 每项耗时写进结果，便于看出哪几项判定最慢、探索时值得单独测哪几项
        item = {k: v for k, v in spec.items() if k != "check"} | measured | {"duration_s": round(time.monotonic() - started, 2)}
        value = measured["value"]
        if spec["goal"] is not None:
            g = shortfall(spec, value)
            item.update(shortfall=g, passed=g == 0)
            total += spec["weight"] * g
        if "wish" in spec:
            item["wish_met"] = value is not None and excess(spec["wish"], value) <= BOUND_TOLERANCE * (_default_scale(spec["wish"]) or 1.0)
        items.append(item)
    judged = [i for i in items if i["goal"] is not None]
    daemon = getattr(ctx, "_daemon", None)
    result = {"shortfall": total,
              # 没有任何带界限的条目时不能宣布达成 / nothing with a pass line means nothing can be declared achieved
              "achieved": only is None and bool(judged) and all(i["passed"] for i in judged), "items": items,
              "partial": only is not None,
              "hard_failing": [i["id"] for i in judged if i["kind"] in HARD_KINDS and not i["passed"]],
              # 执行出错的判定（脚本异常、读不到文件）：这一轮的结论不可信，不能当作设计不达标
              # checks that failed to run: the round says nothing about the design and must not count as a shortfall result
              "measurement_errors": [i["id"] for i in items if i.get("error")],
              "demand": demand, "demand_sha": digest(demand), "method_sha": method_digest(scripts),
              "support_files": support,
              "environment": {"saos_code_sha256": getattr(daemon, "code_sha256", None),
                              "opticstudio": (getattr(daemon, "zos_info", None) or {}).get("version")},
              "lens": ctx.describe(), "evidence_dir": folder.relative_to(ctx.job.root).as_posix(),
              "evidence_file": (folder / "result.json").relative_to(ctx.job.root).as_posix()}
    write_json(folder / "result.json", result)
    ctx.job.seal(folder)
    return result
