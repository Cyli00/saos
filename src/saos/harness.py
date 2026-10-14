"""需求框定（harness）：读取、校验，以及在 OpticStudio 中逐项取值并判定。
Harness: read and validate the framed need, then measure and judge every target in OpticStudio.

文件放在任务的 sources/harness.yaml，字段用英文（给 agent 读写）：
The file is the job's sources/harness.yaml; keys are English (the agent reads and writes it):

    schema: 1
    request: 用户原话
    targets:
      - id: efl
        kind: constraint            # constraint | target | preference
        description: focal length 50 mm ± 1 %
        check: {operand: {type: EFFL, int2: 2}}
        goal: {equals: 50.0, tol: 0.5}   # 或 / or {max: v}, {min: v}; preference 可以没有 / may omit goal
        scale: 0.5                  # 单项差距的归一化尺度，默认取 tol 或界限的绝对值 / normalising scale, default tol or |bound|
    assumptions: [...]
    recipes: []
    lang: zh                        # 用户使用的语言，决定报告语言；可省略 / the user's language, sets the report language
    stop: {rel: 0.01, max_rounds: 30, tiers: [local, variables, materials, global, structure]}
    confirmed: {quote: 用户确认的原话 / the user's words of approval, time: ...}

判定方法三选一：operand（任意 Zemax 优化操作数）、analysis（运行一个分析，按 path 取值）、
script（任务内的脚本，run(ctx) 返回数值）。每一项都在 OpticStudio 中实际计算，满足"实测依据"。
A check is exactly one of: operand (any Zemax optimisation operand), analysis (run an analysis and pick a
value by path) or script (a job script whose run(ctx) returns a number). Every value is computed in
OpticStudio, which is what "measured evidence" means. A check must measure the real thing; it never
special-cases a surface type or setting to skip a measurement.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from saos import i18n, jobs
from saos.protocol import ErrorCode, SaosError, write_json

if TYPE_CHECKING:
    from saos.scripting import ScriptContext

HARNESS_FILE = "sources/harness.yaml"
KINDS = ("constraint", "target", "preference")
# 手段层级，从低到高；含义见 ledger.py / strategy tiers, low to high; see ledger.py
TIERS = ("local", "variables", "materials", "global", "structure")
DEFAULT_STOP = {"rel": 0.01, "max_rounds": 30, "tiers": list(TIERS)}
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,47}$")
PATH_RE = re.compile(r"\['[^']*'\]|\[\d+\]|[A-Za-z_][A-Za-z0-9_ ]*")


def _bad(message: str, **details: Any) -> SaosError:
    return SaosError(ErrorCode.BAD_ARGS, f"harness: {message}", details or None)


def load(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise _bad(f"cannot read {path}: {exc}") from exc
    return validate(data)


def digest(harness: dict[str, Any]) -> str:
    """需求框定内容的哈希，不含确认信息和报告语言：这两项换了、内容没变，差距仍然可比。
    Hash of the harness content without the confirmation and the language: changing only those keeps G comparable."""
    body = {k: v for k, v in harness.items() if k not in ("confirmed", "lang")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


CHECKS_DIR = "checks"
SUPPORT_KEY = "_checks_tree"


def support_files(root: Path) -> dict[str, str]:
    """checks/ 下全部文件（判定脚本及其辅助模块、数据）的哈希；判定脚本只能依赖这里的文件。
    Hashes of every file under checks/ (scripts, helper modules, data); check scripts may depend only on these."""
    base = root / CHECKS_DIR
    if not base.is_dir():
        return {}
    return {f.relative_to(root).as_posix(): jobs.sha256_file(f) for f in sorted(base.rglob("*"))
            if f.is_file() and "__pycache__" not in f.parts}


def prepare_scripts(root: Path, h: dict[str, Any]) -> dict[str, dict[str, Any]]:
    scripts = {}
    for target in h["targets"]:
        if "script" not in target["check"]:
            continue
        rel = str(target["check"]["script"])
        path = (root / rel).resolve()
        if root.resolve() not in path.parents:
            raise SaosError(ErrorCode.PATH_OUTSIDE_JOB, f"path escapes the job directory: {rel}")
        if (root / CHECKS_DIR).resolve() not in path.parents:
            # 判定脚本及其依赖都放在 checks/ 下，才能整体留档和复现 / scripts and what they use live in checks/, archived as a whole
            raise SaosError(ErrorCode.BAD_ARGS, f"{target['id']}: check scripts live under {CHECKS_DIR}/ (got {rel})")
        try:
            source = path.read_text(encoding="utf-8")
            scripts[target["id"]] = {"source": source, "sha256": hashlib.sha256(source.encode("utf-8")).hexdigest()}
        except (OSError, UnicodeError) as exc:
            scripts[target["id"]] = {"source": None, "sha256": None, "error": f"{type(exc).__name__}: {exc}"}
    # 辅助文件整体算进判定方法的版本：改了辅助模块或数据，比较窗口也重新开始
    # supporting files count towards the method version: editing a helper or a data file starts a new window
    files = support_files(root)
    scripts[SUPPORT_KEY] = {"source": None, "sha256": hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest(), "files": files}
    return scripts


def method_digest(scripts: dict[str, dict[str, Any]]) -> str:
    versions = {key: value["sha256"] for key, value in scripts.items()}
    return hashlib.sha256(json.dumps(versions, sort_keys=True).encode()).hexdigest()


def validate(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema") != 1:
        raise _bad("must be a mapping with schema: 1")
    if not isinstance(data.get("request"), str) or not data["request"].strip():
        raise _bad("request must keep the user's own words")
    targets = data.get("targets")
    if not isinstance(targets, list) or not targets:
        raise _bad("targets must be a non-empty list")
    seen = set()
    for t in targets:
        if not isinstance(t, dict) or not ID_RE.match(str(t.get("id", ""))):
            raise _bad("each target needs an id (letters, digits, _)", target=t)
        if t["id"] in seen:
            raise _bad(f"duplicate target id {t['id']}")
        seen.add(t["id"])
        if t.get("kind") not in KINDS:
            raise _bad(f"{t['id']}: kind must be one of {KINDS}")
        check = t.get("check")
        if not isinstance(check, dict) or len(check) != 1 or next(iter(check)) not in ("operand", "analysis", "script"):
            raise _bad(f"{t['id']}: check must have exactly one of operand / analysis / script")
        goal = t.get("goal")
        if t["kind"] != "preference":
            if not isinstance(goal, dict) or not ({"max", "min", "equals"} & set(goal)):
                raise _bad(f"{t['id']}: goal needs max, min or equals (with tol)")
            for key in ("min", "max", "equals"):
                if key in goal and not _finite_number(goal[key]):
                    raise _bad(f"{t['id']}: goal.{key} must be a finite number")
            if "equals" in goal and ("min" in goal or "max" in goal):
                raise _bad(f"{t['id']}: equals cannot be combined with min or max")
            if "min" in goal and "max" in goal and goal["min"] > goal["max"]:
                raise _bad(f"{t['id']}: goal.min must not exceed goal.max")
            if "equals" in goal and not _positive(goal.get("tol")):
                raise _bad(f"{t['id']}: equals needs a positive tol")
            scale = t.get("scale", _default_scale(goal))
            if not _positive(scale):
                raise _bad(f"{t['id']}: scale must be a positive number (the bound is 0, so give one explicitly)")
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


def shortfall(target: dict[str, Any], value: float | None) -> float:
    """单项差距：超出达标线的量除以尺度，达标为 0；没有有效数值时为无穷大。
    Per-target shortfall: the excess over the pass line divided by the scale; 0 when met, infinity without a valid value."""
    if value is None or not math.isfinite(value):
        return math.inf
    goal = target["goal"]
    scale = float(target.get("scale", _default_scale(goal)))
    if "equals" in goal:
        excess = abs(value - float(goal["equals"])) - float(goal["tol"])
    elif "max" in goal and "min" in goal:
        excess = max(value - float(goal["max"]), float(goal["min"]) - value)
    elif "max" in goal:
        excess = value - float(goal["max"])
    else:
        excess = float(goal["min"]) - value
    return max(0.0, excess) / scale


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


def measure(ctx: ScriptContext, target: dict[str, Any], folder: Path, scripts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """在 OpticStudio 中取一个目标的值；返回值和取值依据。
    Measure one target in OpticStudio; return the value and its evidence."""
    kind, spec = next(iter(target["check"].items()))
    evidence: dict[str, Any] = {}
    try:
        if kind == "operand":
            value = ctx.backend.operand(str(spec["type"]), int(spec.get("int1", 0)), int(spec.get("int2", 0)), [float(x) for x in spec.get("data", [])])
            evidence = {"operand": spec}
        elif kind == "analysis":
            text_path = folder / f"{target['id']}.txt"
            data = ctx.backend.analysis(str(spec["type"]), dict(spec.get("settings") or {}), text_path)
            write_json(folder / f"{target['id']}.json", data)
            evidence = {"analysis": spec["type"], "settings": spec.get("settings") or {}, "path": spec["path"],
                        "text": text_path.relative_to(ctx.job.root).as_posix() if data.get("text_file") else None}
            value = pick(data, str(spec["path"]))
        else:
            from saos import scripting

            script = scripts[target["id"]]
            if script["source"] is None:
                return {"value": None, "error": script["error"], "evidence": {"source": str(spec)}}
            copy = folder / f"{target['id']}.py"
            copy.write_text(script["source"], encoding="utf-8")
            evidence = {"script": copy.relative_to(ctx.job.root).as_posix(), "source": str(spec), "sha256": script["sha256"]}
            namespace: dict[str, Any] = {"__name__": f"saos_check_{target['id']}", "__file__": str(ctx.path(str(spec)))}
            exec(compile(script["source"], str(copy), "exec"), namespace)
            value = namespace["run"](scripting.ScriptContext(ctx._daemon, ctx.job, {"target": target["id"]}))
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


def evaluate(ctx: ScriptContext, harness: dict[str, Any], scripts: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """逐项取值和判定。目标差距 G 是约束和性能指标的单项差距之和；期望项只报告数值。
    Measure and judge every target. G is the sum of the shortfalls of constraints and performance targets;
    preferences are reported only."""
    scripts = prepare_scripts(ctx.job.root, harness) if scripts is None else scripts
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
    for index, target in enumerate(harness["targets"]):
        target_folder = folder / f"{index:03d}-{target['id']}"
        target_folder.mkdir()
        measured = measure(ctx, target, target_folder, scripts)
        item = {"id": target["id"], "kind": target["kind"], "description": target.get("description", ""), **measured}
        if target["kind"] != "preference":
            g = shortfall(target, measured["value"])
            item.update(goal=target["goal"], shortfall=g, passed=g == 0)
            total += g
        items.append(item)
    judged = [i for i in items if i["kind"] != "preference"]
    daemon = getattr(ctx, "_daemon", None)
    result = {"shortfall": total, "achieved": all(i["passed"] for i in judged), "items": items,
              # 执行出错的判定（脚本异常、读不到文件）：这一轮的结论不可信，不能当作设计不达标
              # checks that failed to run: the round says nothing about the design and must not count as a shortfall result
              "measurement_errors": [i["id"] for i in items if i.get("error")],
              "harness": harness, "harness_sha": digest(harness), "method_sha": method_digest(scripts),
              "support_files": support,
              "environment": {"saos_code_sha256": getattr(daemon, "code_sha256", None),
                              "opticstudio": (getattr(daemon, "zos_info", None) or {}).get("version")},
              "lens": ctx.describe(), "evidence_dir": folder.relative_to(ctx.job.root).as_posix(),
              "evidence_file": (folder / "result.json").relative_to(ctx.job.root).as_posix()}
    write_json(folder / "result.json", result)
    ctx.job.seal(folder)
    return result
