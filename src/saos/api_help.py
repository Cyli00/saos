"""saos api：从代码取签名和 docstring，说明脚本可以 import 的 Python 接口，不另存一份文档。
saos api: signatures and docstrings straight from the code, so the Python interface of scripts has no second copy."""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import pkgutil
from typing import Any

from saos.protocol import ErrorCode, SaosError

PACKAGES = ("saos.ctx", "saos.starts", "saos.search", "saos.micro", "saos.library", "saos.kit", "saos.materials_record")

# 公开但不给 agent 用的内部辅助，不列出 / public names that are internal helpers, left out of the listing
HIDDEN = frozenset({
    "saos.library.root",
    "saos.ctx.experts.ExpertMethods", "saos.ctx.experts.asphere.mfe_residual", "saos.ctx.experts.asphere.topology",
    "saos.ctx.experts.glass.GlassCandidate", "saos.ctx.experts.glass.candidate_window", "saos.ctx.experts.glass.special_glasses",
    "saos.ctx.experts.ranking.SurfaceSensitivity", "saos.ctx.experts.ranking.usable_fraction",
    "saos.ctx.hints.direct_api_hints",
    "saos.ctx.rows.compile_rows", "saos.ctx.rows.integer", "saos.ctx.rows.number",
    "saos.search.Budget", "saos.search.Stop", "saos.search.axis", "saos.search.ranked",
    "saos.starts.generate.query", "saos.starts.generate.requirements",
    "saos.starts.library.entries", "saos.starts.library.entry_path", "saos.starts.library.library_root", "saos.starts.library.load_entry",
    "saos.starts.paraxial.Surface", "saos.starts.paraxial.back_focal_distance", "saos.starts.paraxial.effective_focal_length",
    "saos.starts.paraxial.front_focal_distance", "saos.starts.paraxial.system_matrix",
    "saos.starts.patents.build_index", "saos.starts.patents.describe", "saos.starts.patents.library_root", "saos.starts.patents.load_index",
    "saos.starts.patents.start_entries",
    "saos.starts.prediction.predict",
    "saos.starts.scaling.scale_surface",
    "saos.starts.seq.elements", "saos.starts.seq.geometry", "saos.starts.seq.paraxial", "saos.starts.seq.parse", "saos.starts.seq.read",
    "saos.starts.seq.to_prescription",
})


def modules() -> list[str]:
    names = []
    for package in PACKAGES:
        root = importlib.import_module(package)
        names.append(package)
        if hasattr(root, "__path__"):
            names += [info.name for info in pkgutil.walk_packages(root.__path__, f"{package}.")
                      if not any(part.startswith("_") for part in info.name.split("."))]
    return names


def _public(module_name: str, *, hidden: bool = False) -> dict[str, Any]:
    module = importlib.import_module(module_name)
    return {name: obj for name, obj in vars(module).items()
            if not name.startswith("_") and (inspect.isfunction(obj) or inspect.isclass(obj))
            and obj.__module__ == module_name and (hidden or f"{module_name}.{name}" not in HIDDEN)}


def public_names(*, hidden: bool = False) -> dict[str, Any]:
    """所有列出的接口：完整路径 → 对象。/ Every listed interface: full path to object."""
    return {f"{m}.{name}": obj for m in modules() for name, obj in _public(m, hidden=hidden).items()}


def _signature(obj: Any) -> str:
    try:
        return str(inspect.signature(obj))
    except (TypeError, ValueError):
        return ""


def _entry(name: str, obj: Any, *, full: bool) -> dict[str, Any]:
    doc = inspect.getdoc(obj) or ""
    entry = {"name": name, "signature": _signature(obj), "doc": doc if full else doc.split("\n", 1)[0]}
    if full and inspect.isclass(obj):
        entry["methods"] = [_entry(k, v, full=True) for k, v in vars(obj).items()
                            if not k.startswith("_") and inspect.isfunction(v)]
    return entry


def _is_module(name: str) -> bool:
    """name 是不是列出的模块。只导入它的父包，不扫描目录。"""
    if not any(name == p or name.startswith(f"{p}.") for p in PACKAGES) or any(part.startswith("_") for part in name.split(".")):
        return False
    try:
        return importlib.util.find_spec(name) is not None
    except ModuleNotFoundError:
        # 父级是模块而不是包；父包自己导入失败时，错误路径的完整扫描会再报出来
        return False


def _ctx_members() -> dict[str, Any]:
    from saos.scripting import ScriptContext

    members: dict[str, Any] = {}
    for klass in ScriptContext.__mro__[:-1]:
        for name, member in vars(klass).items():
            if not name.startswith("_") and name not in members:
                members[name] = member.fget if isinstance(member, property) else member
    return members


def describe(target: str | None = None) -> dict[str, Any]:
    """无参数：模块表；模块或 ctx：其公开接口；完整路径或 ctx.名字：该接口的全文。
    No target lists modules; a module or ctx lists its interface; a full path or ctx.name gives one in full."""
    if target is None:
        rows = [{"module": m, "names": len(_public(m)), "doc": (inspect.getdoc(importlib.import_module(m)) or "").split("\n", 1)[0]}
                for m in modules()]
        return {"modules": [row for row in rows if row["names"]], "also": "ctx"}
    if target == "ctx":
        members = _ctx_members()
        return {"target": "ctx", "names": [_entry(k, v, full=False) for k, v in sorted(members.items()) if callable(v)]}
    if target.startswith("ctx."):
        members = _ctx_members()
        if target[4:] in members:
            return _entry(target, members[target[4:]], full=True)
    elif _is_module(target):
        return {"target": target, "doc": inspect.getdoc(importlib.import_module(target)) or "",
                "names": [_entry(k, v, full=False) for k, v in _public(target).items()]}
    else:
        module, _, name = target.rpartition(".")
        found = _public(module).get(name) if _is_module(module) else None
        if found is not None:
            return _entry(target, found, full=True)
    raise SaosError(ErrorCode.BAD_ARGS, f"no such interface: {target}", {"modules": modules(), "also": "ctx"})
