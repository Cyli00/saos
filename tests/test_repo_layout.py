"""目录规定的检查，规定本身就写在这里：仓库里的文件，以及本机 work/ 下的运行目录。
Layout rules, stated here and nowhere else: files in the repository and the local work/ tree.

仓库部分检查已入库和未忽略的新文件（git ls-files -co --exclude-standard）；work/ 不入库，只在本机存在时检查。
The repository part covers tracked and new non-ignored files; work/ is not versioned and is checked only when present.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from saos import jobs as jobs_mod

ROOT = Path(__file__).resolve().parents[1]

ROOT_FILES = {"README.md", "TODO.md", "AGENTS.md", "pyproject.toml", "uv.lock", ".gitignore"}
ROOT_DIRS = {"src", "docs", "library", "tests", ".agents", ".claude"}
JUNK = re.compile(r"\.(log|tmp|bak|orig|zda|pyc)$|(^|/)(~\$|\.~)|\.zmx$", re.I)

INSTANCE_ENTRIES = {"daemon", "jobs"}
JOB_ENTRIES = {
    # 工具写入
    "job.json", "log.jsonl", "ledger.jsonl", "seals.jsonl", "confirmations.jsonl", "stops.jsonl", "ruled_out.jsonl", "progress.json", "view.json", "metrics.json", "cancel.request", "sources", "lenses", "models", "scripts", "analyses", "screens", "evaluations", "rounds", "report", "probes", "parallel", "scans", "runs",
    # agent 写入
    "notes", "checks", "explore",
    # 已删除的显微物镜配方留在旧任务里的目录（2026-11-06 前）
    "spec", "eval",
}


def repo_files() -> list[PurePosixPath]:
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True)
    # 已删除但还没提交的文件仍在索引里
    return [PurePosixPath(p) for p in out.stdout.splitlines() if p and (ROOT / p).exists()]


def test_root_holds_only_the_listed_entries():
    stray = sorted({p.parts[0] for p in repo_files() if (p.parts[0] not in ROOT_DIRS if len(p.parts) > 1 else p.name not in ROOT_FILES)})
    assert stray == [], f"not allowed at the repository root: {stray}"


def test_no_lens_files_logs_or_temp_files_outside_their_places():
    bad = [str(p) for p in repo_files() if JUNK.search(str(p))
           and not (p.suffix.lower() in (".zmx", ".zda") and p.parts[0] in ("library", "tests"))]
    assert bad == [], f"lens/log/temp files belong in work/ or the system temp directory: {bad}"


def test_docs_are_cross_job():
    bad = []
    for p in repo_files():
        if p.parts[0] != "docs":
            continue
        rel = p.parts[1:]
        if rel != ("workflow.drawio",):
            bad.append(str(p))
    assert bad == [], f"docs/ holds workflow.drawio only; decisions live in code, tests, help and the skill, open work in TODO.md: {bad}"


def test_markdown_lives_only_where_code_cannot_say_it():
    """Markdown 只有：根目录的 README、TODO、AGENTS；技能（.agents/）；数据目录说明来源与许可的 README（library/、tests/ 的夹具）。
    其余说明写进 docstring 或命令帮助。"""
    bad = [str(p) for p in repo_files() if p.suffix.lower() == ".md"
           and not (len(p.parts) == 1 or p.parts[0] in (".agents", ".claude") or (p.parts[0] in ("library", "tests") and p.name == "README.md"))]
    assert bad == [], f"write this into a docstring or the command help instead: {bad}"


@pytest.mark.parametrize("top", ["library"])
def test_every_library_has_a_readme(top):
    files = [p for p in repo_files() if p.parts[0] == top]
    loose = [str(p) for p in files if len(p.parts) == 2 and p.name != "README.md"]
    assert loose == [], f"{top}/ has loose files; put them in a subdirectory: {loose}"
    missing = sorted({p.parts[1] for p in files if len(p.parts) > 2} - {p.parts[1] for p in files if len(p.parts) == 3 and p.name == "README.md"})
    assert missing == [], f"{top}/<name>/ needs a README.md: {missing}"


def test_local_work_tree_follows_the_job_layout():
    """只检查 SAOS_INSTANCE 指定的实例（如 verify）。不读取 work/main 等其他实例的用户任务，也不替它们做封存校验。
    Checks only the instance named by SAOS_INSTANCE (e.g. verify). Other instances, including the user's work/main, are
    never read: their task data is not this test's to judge (plans §12.1)."""
    name = os.environ.get("SAOS_INSTANCE")
    if not name:
        pytest.skip("SAOS_INSTANCE is not set; the default instance holds the user's tasks and is not read by tests")
    instance = ROOT / "work" / name
    if not instance.is_dir():
        pytest.skip(f"no local work/{name} directory")
    bad = [f"{instance.name}/{p.name}" for p in instance.iterdir() if p.name not in INSTANCE_ENTRIES]
    jobs = instance / "jobs"
    for job in jobs.iterdir() if jobs.is_dir() else []:
        bad += [f"{instance.name}/jobs/{job.name}/{p.name}" for p in job.iterdir() if p.name not in JOB_ENTRIES]
        for p in jobs_mod.Job(job.name, job).verify_seals():
            bad.append(f"{instance.name}/jobs/{job.name}/{p['path']} ({p['problem']} after sealing)")
        explore = job / "explore"
        for topic in explore.iterdir() if explore.is_dir() else []:
            # __pycache__ 是导入 explore 里的脚本时 Python 自动生成的，不是探索主题
            if topic.is_dir() and topic.name != "__pycache__" and not (topic / "README.md").is_file():
                bad.append(f"{instance.name}/jobs/{job.name}/explore/{topic.name} (no README.md)")
    assert bad == [], "work/ layout: instance = daemon/ + jobs/; job extras only notes/, checks/, explore/<topic>/ with README.md: " + ", ".join(bad)


def test_relative_markdown_links_resolve():
    bad = []
    for p in repo_files():
        if p.suffix != ".md":
            continue
        path = ROOT / p
        for m in re.finditer(r"\]\(([^)#\s]+)(#[^)]*)?\)", path.read_text(encoding="utf-8")):
            target = m.group(1)
            if not re.match(r"[a-z]+:", target) and not (path.parent / target).exists():
                bad.append(f"{p}: {target}")
    assert bad == [], f"broken relative links (fix them after moving files): {bad}"
