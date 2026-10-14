"""需求框定（harness）：读取、校验，以及在 OpticStudio 中逐项取值并判定。

文件放在任务的 sources/harness.yaml，字段用英文（给 agent 读写）：

    schema: 1
    request: 用户原话
    targets:
      - id: efl
        kind: constraint            # constraint | target | preference
        description: focal length 50 mm ± 1 %
        check: {operand: {type: EFFL, int2: 2}}
        goal: {equals: 50.0, tol: 0.5}   # 或 {max: v}、{min: v}；preference 可以没有 goal
        scale: 0.5                  # 单项差距的归一化尺度，默认取 tol 或界限的绝对值
    assumptions: [...]
    recipes: []
    lang: zh                        # 用户使用的语言，决定交付报告的语言；可省略
    stop: {rel: 0.01, max_rounds: 30, tiers: [local, variables, materials, global, structure]}
    confirmed: {quote: 用户确认的原话, time: ...}

判定方法三选一：operand（任意 Zemax 优化操作数）、analysis（运行一个分析，按 path 取值）、
script（任务内的脚本，run(ctx) 返回数值）。每一项都在 OpticStudio 中实际计算，满足"实测依据"。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from saos import i18n
from saos.protocol import ErrorCode, SaosError

if TYPE_CHECKING:
    from saos.scripting import ScriptContext

HARNESS_FILE = "sources/harness.yaml"
KINDS = ("constraint", "target", "preference")
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
    """需求框定内容的哈希，不含确认信息和报告语言：这两项换了、内容没变，差距仍然可比。"""
    body = {k: v for k, v in harness.items() if k not in ("confirmed", "lang")}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


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


def _positive(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and x > 0


def _default_scale(goal: dict[str, Any]) -> float | None:
    if "equals" in goal:
        return float(goal["tol"])
    bound = goal.get("max", goal.get("min"))
    return abs(float(bound)) if bound is not None and float(bound) != 0 else None


def shortfall(target: dict[str, Any], value: float | None) -> float:
    """单项差距：超出达标线的量除以尺度，达标为 0；没有有效数值时为无穷大。"""
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
    """按 a.b[0].c 形式的路径取值，用于从分析导出结果中挑出一个数；键里有点号时写成 ['0.48']。"""
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


def measure(ctx: ScriptContext, target: dict[str, Any]) -> dict[str, Any]:
    """在 OpticStudio 中取一个目标的值；返回值和取值依据。"""
    kind, spec = next(iter(target["check"].items()))
    try:
        if kind == "operand":
            value = ctx.backend.operand(str(spec["type"]), int(spec.get("int1", 0)), int(spec.get("int2", 0)), [float(x) for x in spec.get("data", [])])
            evidence = {"operand": spec}
        elif kind == "analysis":
            folder = ctx.path("analyses")
            data = ctx.backend.analysis(str(spec["type"]), dict(spec.get("settings") or {}), folder / f"check-{target['id']}.txt")
            value = pick(data, str(spec["path"]))
            evidence = {"analysis": spec["type"], "settings": spec.get("settings") or {}, "path": spec["path"], "text": f"analyses/check-{target['id']}.txt"}
        else:
            from saos import scripting

            source = ctx.path(str(spec)).read_text(encoding="utf-8")
            namespace: dict[str, Any] = {"__name__": f"saos_check_{target['id']}"}
            exec(compile(source, str(ctx.path(str(spec))), "exec"), namespace)
            value = namespace["run"](scripting.ScriptContext(ctx._daemon, ctx.job, {"target": target["id"]}))
            evidence = {"script": str(spec)}
        value = float(value)
    except SaosError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, OSError) as exc:
        return {"value": None, "error": f"{type(exc).__name__}: {exc}", "evidence": target["check"]}
    return {"value": value if math.isfinite(value) else None, "evidence": evidence}


def evaluate(ctx: ScriptContext, harness: dict[str, Any]) -> dict[str, Any]:
    """逐项取值和判定。目标差距 G 是约束和性能指标的单项差距之和；期望项只报告数值。"""
    items = []
    total = 0.0
    for target in harness["targets"]:
        measured = measure(ctx, target)
        item = {"id": target["id"], "kind": target["kind"], "description": target.get("description", ""), **measured}
        if target["kind"] != "preference":
            g = shortfall(target, measured["value"])
            item.update(goal=target["goal"], shortfall=g, passed=g == 0)
            total += g
        items.append(item)
    judged = [i for i in items if i["kind"] != "preference"]
    return {"shortfall": total, "achieved": all(i["passed"] for i in judged), "items": items}
