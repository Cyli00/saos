"""模块分层（DESIGN.md「模块分层」）：下层不导入上层，函数内的延迟导入也算。
Module layers: a lower layer never imports a higher one, imports inside functions included.

L0 基础约定（协议、参数键、几何、处方、文件）→ L1 计算后端 → L2 测量库（kit、指标目录、micro、rtc 副本、搜索、专家类、fiber 配方库）→ 其余
（任务、需求、轮次、扫描、worker 池、后台进程、窗口、配方命令）。只约束 L0–L2：下层不导入上层，L0–L2 之间也不成环（只豁免完全在同一包内的环），相对导入也算；
其余模块相当于 ripgrep 的 core，彼此的方向不在这里检查。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "saos"

# saos.ctx.rows 是评价函数行的构造与校验，没有依赖，后端编译评价函数行时要用 / no dependencies; backends compile merit rows with it
BASE = {"protocol", "config", "budget", "lensdata", "geometry", "prescription", "lens_files", "glasscat", "library", "native_ui", "ctx", "ctx.rows"}
MEASURE = {"kit", "fast", "search", "micro", "metrics"}
# 只含测量函数的配方库与测量库同层：判定脚本导入它们，它们不得伸进执行层 / measurement-only recipe libraries sit with kit
MEASURE_RECIPES = ("recipes.fiber",)
TOP = 9


def layer(module: str) -> int:
    if module in BASE:
        return 0
    if module == "backend" or module.startswith("backend."):
        return 1
    if module in MEASURE or module.startswith("ctx.") or module.startswith(MEASURE_RECIPES) or module == "recipes":
        return 2
    return TOP


def modules() -> dict[str, Path]:
    out = {}
    for path in ROOT.rglob("*.py"):
        parts = list(path.relative_to(ROOT).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        out[".".join(parts)] = path
    return out


def _absolute(path: Path, node: ast.ImportFrom) -> str | None:
    """from 导入的绝对模块名（去掉 saos. 前缀，saos 本身为 ""）；相对导入按文件所在包解析，包外导入为 None。
    The absolute module of a from-import without the saos. prefix; relative imports resolve against the file's package."""
    if node.level == 0:
        module = node.module or ""
        return None if module != "saos" and not module.startswith("saos.") else module[len("saos."):]
    package = list(path.relative_to(ROOT).with_suffix("").parts)[:-1]
    if node.level > 1:
        package = package[:-(node.level - 1)]
    return ".".join(package + ([node.module] if node.module else []))


def imports(path: Path, known: dict[str, Path]):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        base = _absolute(path, node) if isinstance(node, ast.ImportFrom) else None
        if base is not None:
            for alias in node.names:
                name = f"{base}.{alias.name}" if base else alias.name
                # from saos.x import y：y 是子模块时按子模块算，否则按 x 算 / y counts when it is a submodule, else x
                yield node.lineno, name if name in known else base
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("saos."):
                    yield node.lineno, alias.name[len("saos."):]


def test_lower_layers_never_import_higher_ones():
    known = modules()
    found = [f"{m} (L{layer(m)}) imports {t} (L{layer(t)}) at {path.name}:{line}"
             for m, path in sorted(known.items()) if layer(m) < TOP
             for line, t in imports(path, known) if t and layer(t) > layer(m)]
    assert not found, "\n".join(found)


def test_every_named_lower_module_exists():
    # 名单里的模块改名或删掉后，这里的约束会悄悄失效 / a renamed module would silently drop out of the rules
    known = modules()
    assert (BASE | MEASURE) <= set(known), sorted((BASE | MEASURE) - set(known))


def test_lower_layers_form_no_import_cycle():
    # 参照 ripgrep：库 crate 之间没有环，最上层（ripgrep 的 core）内部不管。L0–L2 是库，同层之间也不许成环；
    # 一个环完全落在同一个包内（backend.*、ctx.*）才相当于 crate 内部的模块而豁免。图里保留同包的边：
    # 经过同包模块绕回来的跨包环（metrics → ctx.a → ctx.b → metrics）照样要报。
    # like ripgrep's library crates; only a cycle entirely inside one package is exempt, and same-package edges stay in
    # the graph so a cross-package cycle through them is still caught
    known = modules()
    low = sorted(m for m in known if layer(m) < TOP)
    graph = {m: sorted({t for _, t in imports(known[m], known) if t in known and layer(t) < TOP and t != m}) for m in low}
    index: dict[str, int] = {}
    lowest: dict[str, int] = {}
    stack: list[str] = []
    components = []

    def connect(node):  # Tarjan 强连通分量 / Tarjan's strongly connected components
        index[node] = lowest[node] = len(index)
        stack.append(node)
        for nxt in graph[node]:
            if nxt not in index:
                connect(nxt)
                lowest[node] = min(lowest[node], lowest[nxt])
            elif nxt in stack:
                lowest[node] = min(lowest[node], index[nxt])
        if lowest[node] == index[node]:
            component = []
            while True:
                member = stack.pop()
                component.append(member)
                if member == node:
                    break
            components.append(sorted(component))

    for node in low:
        if node not in index:
            connect(node)
    crossing = [c for c in components if len({m.split(".")[0] for m in c}) > 1]
    assert not crossing, "; ".join(" ↔ ".join(c) for c in crossing)
