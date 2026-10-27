"""只识别能确定映射到 ctx.get/set 的源码写法；提示不参与执行许可。"""

from __future__ import annotations

import ast
from collections import Counter

ATTRS = {"Radius": "radius", "Thickness": "thickness", "Conic": "conic", "SemiDiameter": "semi_diameter",
         "MechanicalSemiDiameter": "mech_semi_diameter", "Material": "material", "Comment": "comment"}


def direct_api_hints(source):
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return []
    hints = []
    for function in tree.body:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)) or function.name != "run" or not function.args.args:
            continue
        context = function.args.args[0].arg
        assignments = Counter(n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store))
        if assignments[context]:
            continue
        aliases = {}

        def expand(node):
            if isinstance(node, ast.Name) and node.id in aliases:
                return aliases[node.id]
            return node

        def is_lde(node):
            node = expand(node)
            if not isinstance(node, ast.Attribute) or node.attr != "LDE":
                return False
            system = expand(node.value)
            return (isinstance(system, ast.Attribute) and system.attr == "system"
                    and isinstance(system.value, ast.Name) and system.value.id == context)

        def surface(node):
            node = expand(node)
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) or node.func.attr != "GetSurfaceAt":
                return None
            if len(node.args) != 1 or node.keywords or not is_lde(node.func.value):
                return None
            index = node.args[0]
            if isinstance(index, ast.Constant) and isinstance(index.value, int) and not isinstance(index.value, bool) and index.value >= 0:
                return index.value
            return None

        # 仅跟踪函数顶层、唯一赋值的别名，跳过分支、循环和闭包中的不确定绑定。
        for statement in function.body:
            if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
                target = statement.targets[0]
                if isinstance(target, ast.Name) and assignments[target.id] == 1:
                    aliases[target.id] = expand(statement.value)
            if not isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign, ast.Expr, ast.Return)):
                continue
            for node in ast.walk(statement):
                if not isinstance(node, ast.Attribute) or node.attr not in ATTRS:
                    continue
                index = surface(node.value)
                if index is None:
                    continue
                key = f"{index}.{ATTRS[node.attr]}"
                write = isinstance(node.ctx, ast.Store)
                replacement = f'{context}.set("{key}", value)' if write else f'{context}.get("{key}")'
                hints.append({"line": node.lineno, "member": node.attr, "suggestion": replacement,
                              "message": f"saos already wraps this: {replacement}; direct ZOS-API access stays allowed"})
                if len(hints) == 100:
                    return hints
    return hints
