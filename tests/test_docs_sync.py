"""工作流写在技能、界面和流程图里，几处都会提到命令：这里自动核对其中能机械核对的部分，减少靠人记得同步。

- 技能（SKILL.md 与 references）、DESIGN.md、各级命令帮助和 workflow.drawio 中出现的命令都存在于命令行；
  已删除的命令只能以历史说明出现。
- 反向：使用 saos 的 agent 能调用的命令和 Python 接口都查得到：写在命令帮助或技能里，或能经 saos api 读到 docstring，
  否则显式列为内部接口（INTERNAL、api_help.HIDDEN）。技能文档只放判断，接口说明放帮助（DESIGN.md）。
- 技能里的链接和帮助主题都指向存在的东西。
- workflow.drawio 能被 draw.io 官方查看器渲染（默认跳过，需要 Chrome 和网络：SAOS_RENDER_DRAWIO=1 uv run pytest tests/test_docs_sync.py -s，
  打印出 PNG 路径，供人看图核对）。
"""

from __future__ import annotations

import html
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from saos import api_help, cli, cli_help, config
from saos.protocol import SaosError

ROOT = config.REPO_ROOT
DOCS = ("DESIGN.md", *sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / ".agents" / "skills" / "saos").rglob("*.md")))
DIAGRAM = ROOT / "docs" / "workflow.drawio"
COMMAND_RE = re.compile(r"(?<!from )\bsaos ([a-z][a-z-]*)(?: ([a-z][a-z-]*))?(?: ([a-z][a-z-]*))?")
# 已删除的命令：只能出现在“原……并入 / 已取消”这类历史说明里 / removed commands may only appear in historical notes
REMOVED = {"probe", "reverify"}
HISTORY_MARKS = ("原", "并入", "取消", "旧", "removed", "folded", "former")
SKILL_DOCS = tuple(name for name in DOCS if name.startswith(".agents/"))
# agent 脚本里直接调用的 Python 包；包内新增的子模块自动纳入核对
# 查不到说明但有意保留的接口：内部辅助。Python 包里的内部名字在 api_help.HIDDEN
INTERNAL = {"ctx.child", "ctx.describe", "saos daemon run", "saos ui serve"} | api_help.HIDDEN


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
    if command == "saos":
        sources += [(f"saos {key} --help {slug}", text, text) for key, slug, text in _help_topics()]
    return sources


def _help_topics() -> list[tuple[str, str, str]]:
    return [(key, slug, cli_help.topic_help(key, slug)) for key, topics in cli_help.TOPICS.items() for slug in topics]


def _all_help() -> str:
    def walk(parser):
        yield parser.format_help()
        for child in _subcommands(parser).values():
            yield from walk(child)

    return "\n".join([*walk(cli.build_parser()), *(cli_help.topic_help(key, "all") for key in cli_help.TOPICS)])


def _diagram_boxes() -> list[str]:
    """drawio 各框的文字（框按计算后端着色，每框第二行是命令或接口）。/ The text of the diagram's boxes."""
    xml = DIAGRAM.read_text(encoding="utf-8")
    return [re.sub(r"<[^>]+>", " ", html.unescape(html.unescape(value)))
            for value in re.findall(r'<mxCell [^>]*value="([^"]*)" style="[^"]*"', xml)]


def _agent_interface() -> set[str]:
    """命令记作 `saos 组 子命令`，ctx 成员记作 `ctx.名字`，包内公开函数和类记作完整模块路径。"""
    names = set()
    for group, child in _subcommands(cli.build_parser()).items():
        names |= {f"saos {group} {sub}" for sub in _subcommands(child)} or {f"saos {group}"}
    from saos.scripting import ScriptContext

    names |= {f"ctx.{name}" for klass in ScriptContext.__mro__[:-1] for name in vars(klass) if not name.startswith("_")}
    return names | set(api_help.public_names(hidden=True))


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
    sources += [("docs/workflow.drawio", box, box) for box in _diagram_boxes()]
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


def _has_docstring(key: str) -> bool:
    if key.startswith("saos "):
        return False
    if key.startswith("ctx."):
        member = api_help._ctx_members()[key[4:]]
    else:
        member = api_help.public_names(hidden=True)[key]
    return bool(inspect.getdoc(member))


def test_every_agent_interface_can_be_looked_up_or_is_marked_internal():
    text = "\n".join([_all_help(), *((ROOT / name).read_text(encoding="utf-8") for name in SKILL_DOCS)])
    interface = _agent_interface()
    missing = sorted(key for key in interface - INTERNAL if not _documented(text, key) and not _has_docstring(key))
    assert not missing, f"写进命令帮助或 docstring（saos api 读得到），或标为内部接口：{missing}"
    assert not INTERNAL - interface, f"INTERNAL 里已不存在的接口：{sorted(INTERNAL - interface)}"


def test_every_help_topic_finds_its_section_and_keeps_all_the_text():
    for key, topics in cli_help.TOPICS.items():
        whole = cli_help.topic_help(key, "all")
        for slug in topics:
            assert cli_help.topic_help(key, slug).strip() in whole, (key, slug)
        overview = cli_help.topic_help(key)
        assert all(f"  {slug:<12}" in overview for slug in topics), key


def test_the_rtc_operand_lists_in_help_come_from_the_code():
    from saos.backend import rtc, rtc_merit

    listed = cli_help.topic_help("exec", "rtc")
    assert all(name in listed for name in rtc.RAY_OPERANDS | rtc.SYSTEM_OPERANDS)
    assert "<RTC_" not in listed
    merit = _subcommands(_subcommands(cli.build_parser())["opt"])["merit"].format_help()
    assert all(name in merit for name in rtc_merit.merit_operands() - rtc_merit.INERT)


def test_a_help_topic_prints_and_an_unknown_one_lists_the_choices(capsys):
    assert cli.main(["exec", "--help", "rtc"]) == 0
    assert "The rtc instance" in capsys.readouterr().out
    assert cli.main(["exec", "--help", "nosuch"]) == 2
    assert '"topics"' in capsys.readouterr().out


def test_saos_api_reads_the_code(capsys):
    assert cli.main(["api", "saos.ctx.rows"]) == 0
    listing = json.loads(capsys.readouterr().out)["result"]
    assert "sag_gap" in {row["name"] for row in listing["names"]}
    assert cli.main(["api", "ctx.make_thermal"]) == 0
    assert "temperatures" in json.loads(capsys.readouterr().out)["result"]["signature"]
    assert cli.main(["api", "saos.nothing"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "BAD_ARGS"


def test_saos_api_answers_every_listed_interface_as_the_full_scan_does():
    known = api_help.modules()
    assert {m: api_help.describe(m)["target"] for m in known} == {m: m for m in known}
    for name, obj in api_help.public_names().items():
        assert api_help.describe(name)["signature"] == api_help._signature(obj)
    for missing in ("saos.nothing", "saos.kit.nothing", "saos.kit.op.nothing", "os.path", "saos.ctx.experts._session", "saos.kit._nothing"):
        with pytest.raises(SaosError) as err:
            api_help.describe(missing)
        assert err.value.details["modules"] == known


def test_saos_api_imports_only_the_package_it_is_asked_about():
    script = ("import sys, saos.api_help as a; a.describe('saos.materials_record'); "
              "print([p for p in a.PACKAGES if p in sys.modules])")
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True).stdout
    assert json.loads(out.replace("'", '"')) == ["saos.materials_record"]


def test_skill_links_and_help_pointers_resolve():
    skill = ROOT / ".agents" / "skills" / "saos"
    broken = []
    pointer = re.compile(r"saos (exec|scan|lens build) --help ([a-z]+)")
    for path in skill.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        for target, anchor in re.findall(r"\]\(([^)#\s]*\.[a-z]+)?(?:#([^)\s]+))?\)", text):
            linked = (path.parent / target) if target else path
            if not linked.is_file():
                broken.append(f"{path.name}: {target}")
            elif anchor:
                headings = re.findall(r"^#+ (.+)$", linked.read_text(encoding="utf-8"), re.M)
                slugs = {re.sub(r"[^\w\- ]", "", h).strip().lower().replace(" ", "-") for h in headings}
                if anchor not in slugs:
                    broken.append(f"{path.name}: {target}#{anchor}")
        for command, topic in pointer.findall(text):
            if topic not in cli_help.TOPICS[command] and topic != "all":
                broken.append(f"{path.name}: {command} --help {topic}")
    for path in (ROOT / "src").rglob("*.py"):
        for name in re.findall(r"references/([a-z-]+\.md)", path.read_text(encoding="utf-8")):
            if not (skill / "references" / name).is_file():
                broken.append(f"{path.name}: references/{name}")
    assert not broken, broken


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
