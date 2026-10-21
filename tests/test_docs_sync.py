"""工作流写在四处（AGENTS.md），DESIGN.md 也会提到命令：这里自动核对其中能机械核对的部分，减少靠人记得同步。

- SKILL.md、demand.md、DESIGN.md、docs/cli.md、各级命令帮助和 workflow.drawio 中出现的命令都存在于命令行；
  已删除的命令只能以历史说明出现。
- workflow.drawio 能被 draw.io 官方查看器渲染（默认跳过，需要 Chrome 和网络：SAOS_RENDER_DRAWIO=1 uv run pytest tests/test_docs_sync.py -s，
  打印出 PNG 路径，供人看图核对）。
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from saos import cli, config

ROOT = config.REPO_ROOT
DOCS = ("skills/saos/SKILL.md", "skills/saos/demand.md", "DESIGN.md", "docs/cli.md")
DIAGRAM = ROOT / "docs" / "workflow.drawio"
COMMAND_RE = re.compile(r"(?<!from )\bsaos ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?(?: ([a-z][a-z-]*))?")
# 已删除的命令：只能出现在“原……并入 / 已取消”这类历史说明里 / removed commands may only appear in historical notes
REMOVED = {"probe", "reverify"}
HISTORY_MARKS = ("原", "并入", "取消", "旧", "removed", "folded", "former")


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
                     "model_approve": "saos lens approve", "design": "saos round", "deliver": "saos report",
                     "zos": "saos zos begin", "final_report": "saos report"}
    assert set(step_commands) == set(stage.STEPS)
    assert [c for c in step_commands.values() if c not in boxes] == []


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
