"""模块分层（DESIGN.md「模块分层」）：下层不导入上层，函数内的延迟导入也算。
Module layers: a lower layer never imports a higher one, imports inside functions included.

L0 基础约定（协议、参数键、几何、处方、文件）→ L1 计算后端 → L2 测量库（kit、micro、rtc 副本、搜索、专家类、fiber 配方库）→ 其余（任务、
需求、轮次、扫描、worker 池、后台进程、窗口、配方命令）。只约束 L0–L2；其余模块之间的方向不在这里检查。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src" / "saos"

# saos.ctx.rows 是评价函数行的构造与校验，没有依赖，后端编译评价函数行时要用 / no dependencies; backends compile merit rows with it
BASE = {"protocol", "config", "budget", "lensdata", "geometry", "prescription", "lens_files", "glasscat", "library", "native_ui", "ctx", "ctx.rows"}
MEASURE = {"kit", "fast", "search", "micro"}
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


def imports(path: Path, known: dict[str, Path]):
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module and (node.module == "saos" or node.module.startswith("saos.")):
            base = node.module[len("saos."):] if node.module != "saos" else ""
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
