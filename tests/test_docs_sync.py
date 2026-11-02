"""工作流写在技能、界面和流程图里，几处都会提到命令：这里自动核对其中能机械核对的部分，减少靠人记得同步。

- 技能（SKILL.md 与 references）、DESIGN.md、各级命令帮助和 workflow.drawio 中出现的命令都存在于命令行；
  已删除的命令只能以历史说明出现。
- 反向：使用 saos 的 agent 能调用的命令和 Python 接口都写进技能文档，或显式列为内部接口（INTERNAL）。
- workflow.drawio 能被 draw.io 官方查看器渲染（默认跳过，需要 Chrome 和网络：SAOS_RENDER_DRAWIO=1 uv run pytest tests/test_docs_sync.py -s，
  打印出 PNG 路径，供人看图核对）。
"""

from __future__ import annotations

import html
import importlib
import inspect
import json
import os
import pkgutil
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from saos import cli, config

ROOT = config.REPO_ROOT
DOCS = ("DESIGN.md", *sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / ".agents" / "skills" / "saos").rglob("*.md")))
DIAGRAM = ROOT / "docs" / "workflow.drawio"
COMMAND_RE = re.compile(r"(?<!from )\bsaos ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?(?: ([a-z][a-z-]*))?")
# 已删除的命令：只能出现在“原……并入 / 已取消”这类历史说明里 / removed commands may only appear in historical notes
REMOVED = {"probe", "reverify"}
HISTORY_MARKS = ("原", "并入", "取消", "旧", "removed", "folded", "former")
SKILL_DOCS = tuple(name for name in DOCS if name.startswith(".agents/"))
# agent 脚本里直接调用的 Python 包；包内新增的子模块自动纳入核对
AGENT_PACKAGES = ("saos.ctx", "saos.starts", "saos.search", "saos.micro")
# 有意不写进技能的接口：内部辅助、只靠命令帮助说明的命令。新增接口要么写进技能文档，要么加在这里
INTERNAL = {
    "ctx.analysis", "ctx.child", "ctx.configurations", "ctx.describe", "ctx.detector", "ctx.dirty", "ctx.lens", "ctx.lib", "ctx.mark_dirty",
    "ctx.memo", "ctx.nsc_trace", "ctx.object_index", "ctx.objects", "ctx.op", "ctx.path", "ctx.quick_focus", "ctx.ray_hits", "ctx.save_as",
    "ctx.show", "ctx.spot", "ctx.surface", "ctx.trace", "ctx.values", "ctx.zosapi",
    "saos daemon run", "saos job list", "saos lens analysis", "saos lens capture", "saos lens info", "saos lens operand",
    "saos lens surfaces", "saos lens unload", "saos microscope adapt", "saos microscope perturb", "saos microscope repro", "saos ui serve",
    "saos.ctx.experts.asphere.mfe_residual", "saos.ctx.experts.asphere.topology",
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
}


def _subcommands(parser) -> dict:
    for action in parser._actions:
        if action.__class__.__name__ == "_SubParsersAction":
            return action.choices
    return {}


def _code_spans(text: str) -> list[str]:
    return re.findall(r"`([^`\n]+)`", text)


def _help_sources(parser, command="saos") -> list[tuple[str, str, str]]:
    sources = []
    for field in ("description", "epilog"):
        value = getattr(parser, field) or ""
        sources.append((f"{command} --help ({field})", value, value))
    for name, child in _subcommands(parser).items():
        sources.extend(_help_sources(child, f"{command} {name}"))
    return sources


def _diagram_boxes(fill: str | None = None) -> list[str]:
    """drawio 各框的文字；fill 给定时只取该填充色的框（#d5e8d4 是 saos 命令 / 工具行为）。
    The text of the diagram's boxes; with fill, only boxes of that colour (#d5e8d4: saos commands / tool behaviour)."""
    xml = DIAGRAM.read_text(encoding="utf-8")
    out = []
    for value, style in re.findall(r'<mxCell [^>]*value="([^"]*)" style="([^"]*)"', xml):
        if fill is None or f"fillColor={fill}" in style:
            out.append(re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(value))))
    return out


def _agent_interface() -> set[str]:
    """命令记作 `saos 组 子命令`，ctx 成员记作 `ctx.名字`，包内公开函数和类记作完整模块路径。"""
    names = set()
    for group, child in _subcommands(cli.build_parser()).items():
        names |= {f"saos {group} {sub}" for sub in _subcommands(child)} or {f"saos {group}"}
    from saos.scripting import ScriptContext

    names |= {f"ctx.{name}" for klass in ScriptContext.__mro__[:-1] for name in vars(klass) if not name.startswith("_")}
    for package in AGENT_PACKAGES:
        root = importlib.import_module(package)
        modules = [root]
        if hasattr(root, "__path__"):
            modules += [importlib.import_module(info.name) for info in pkgutil.walk_packages(root.__path__, f"{package}.")
                        if not any(part.startswith("_") for part in info.name.split("."))]
        for module in modules:
            names |= {f"{module.__name__}.{name}" for name, obj in vars(module).items()
                      if not name.startswith("_") and (inspect.isfunction(obj) or inspect.isclass(obj))
                      and obj.__module__ == module.__name__}
    return names


def _documented(text: str, key: str) -> bool:
    if key.startswith("saos "):
        words = key.split()[1:]
        pattern = rf"\b{' '.join(words)}\b" if len(words) == 2 else rf"\bsaos {words[0]}\b|`{words[0]}`"
    elif key.startswith("ctx."):
        pattern = rf"\b{re.escape(key)}\b"
    else:
        # 模块函数在文档里常写成 `patents.search`、`generate(...)` 或单独的 `GlassExpert`
        name = re.escape(key.rsplit(".", 1)[1])
        pattern = rf"\.{name}\b|\b{name}\(|`{name}`"
    return re.search(pattern, text) is not None


def test_every_saos_command_in_the_docs_exists():
    parser = cli.build_parser()
    sources = [(name, span, text) for name in DOCS for text in [(ROOT / name).read_text(encoding="utf-8")] for span in _code_spans(text)]
    sources += [("docs/workflow.drawio", box, box) for box in _diagram_boxes("#d5e8d4")]
    sources += _help_sources(parser)
    assert len(sources) > 200
    unknown = set()
    for name, span, text in sources:
        for m in COMMAND_RE.finditer(span):
            cmd = m.group(1)
            if cmd in REMOVED:
                at = text.find(m.group(0))
                context = text[max(0, at - 40): at + 60]
                if not any(mark in context for mark in HISTORY_MARKS):
                    unknown.add(f"{name}: removed command {m.group(0)!r} outside a historical note")
                continue
            current = parser
            for token in m.groups():
                choices = _subcommands(current)
                if token is None or not choices:
                    break
                if token not in choices:
                    unknown.add(f"{name}: {m.group(0)!r}")
                    break
                current = choices[token]
    assert not unknown, sorted(unknown)


def test_the_diagram_names_every_workflow_step_command():
    """流程图窗口的每个步骤都靠一条命令推进，图上都要有这条命令。/ Each step of the window advances by a command the diagram shows."""
    from saos.ui import stage

    boxes = " ".join(_diagram_boxes())
    step_commands = {"interview": "saos job create", "demand_confirm": "saos demand confirm", "modeling": "saos lens describe",
                     "model_approve": "saos lens approve", "design": "saos round", "deliver": "saos report"}
    assert set(step_commands) == set(stage.STEPS)
    assert [c for c in step_commands.values() if c not in boxes] == []


def test_every_agent_interface_is_in_the_skill_or_marked_internal():
    text = "\n".join((ROOT / name).read_text(encoding="utf-8") for name in SKILL_DOCS)
    interface = _agent_interface()
    missing = sorted(key for key in interface - INTERNAL if not _documented(text, key))
    assert not missing, f"写进 .agents/skills/saos/，或在本文件 INTERNAL 里标为内部接口：{missing}"
    assert not INTERNAL - interface, f"INTERNAL 里已不存在的接口：{sorted(INTERNAL - interface)}"


@pytest.mark.skipif(os.environ.get("SAOS_RENDER_DRAWIO") != "1", reason="需要 Chrome 和网络（draw.io 官方查看器），设置 SAOS_RENDER_DRAWIO=1")
def test_the_diagram_renders(tmp_path):
    from saos import ui

    chrome = ui._chrome()
    assert chrome, "Chrome not found"
    config_json = json.dumps({"xml": DIAGRAM.read_text(encoding="utf-8"), "nav": False, "resize": True, "toolbar": "", "lightbox": False})
    page = tmp_path / "diagram.html"
    page.write_text('<!doctype html><html><head><meta charset="utf-8"></head><body style="margin:0;background:#fff">'
                    f'<div class="mxgraph" data-mxgraph="{html.escape(config_json, quote=True)}"></div>'
                    '<script src="https://viewer.diagrams.net/js/viewer-static.min.js"></script></body></html>', encoding="utf-8")
    png = tmp_path / "workflow.png"
    subprocess.run([chrome, "--headless=new", f"--screenshot={png}", "--window-size=1920,2800", "--virtual-time-budget=20000",
                    "--hide-scrollbars", f"--user-data-dir={tmp_path / 'profile'}", page.as_uri()], capture_output=True, timeout=120)
    assert png.is_file() and png.stat().st_size > 100_000, "the viewer did not render the diagram"
    keep = Path(os.environ.get("TEMP", tmp_path)) / "saos-workflow.png"
    shutil.copy2(png, keep)
    print(f"rendered: {keep}")
