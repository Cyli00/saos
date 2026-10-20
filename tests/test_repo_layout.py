"""目录规定（DESIGN.md「仓库目录」「任务目录」）的检查：仓库里的文件，以及本机 work/ 下的运行目录。
Layout rules (DESIGN.md, repository and job directories): files in the repository and the local work/ tree.

仓库部分检查已入库和未忽略的新文件（git ls-files -co --exclude-standard）；work/ 不入库，只在本机存在时检查。
The repository part covers tracked and new non-ignored files; work/ is not versioned and is checked only when present.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path, PurePosixPath

import pytest

from saos import jobs as jobs_mod

ROOT = Path(__file__).resolve().parents[1]

ROOT_FILES = {"README.md", "DESIGN.md", "GLOSSARY.md", "TODO.md", "AGENTS.md", "pyproject.toml", "uv.lock", ".gitignore"}
ROOT_DIRS = {"src", "skills", "docs", "library", "tests", "experiments", ".claude"}
JUNK = re.compile(r"\.(log|tmp|bak|orig|zda|pyc)$|(^|/)(~\$|\.~)|\.zmx$", re.I)

INSTANCE_ENTRIES = {"daemon", "jobs"}
JOB_ENTRIES = {
    # 工具写入
    "job.json", "log.jsonl", "ledger.jsonl", "seals.jsonl", "confirmations.jsonl", "stops.jsonl", "ruled_out.jsonl", "progress.json", "view.json", "cancel.request", "sources", "lenses", "models", "scripts", "analyses", "screens", "evaluations", "rounds", "report", "probes", "parallel", "scans", "runs",
    # agent 写入
    "notes", "checks", "explore",
    # 显微物镜配方（规格与验收评估）
    "spec", "eval",
}


def repo_files() -> list[PurePosixPath]:
    out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], cwd=ROOT, capture_output=True, text=True, check=True)
    # 已删除但还没提交的文件仍在索引里
    return [PurePosixPath(p) for p in out.stdout.splitlines() if p and (ROOT / p).exists()]


def recipes() -> set[str]:
    return {p.name for p in (ROOT / "skills" / "recipes").iterdir() if p.is_dir()}


def test_root_holds_only_the_listed_entries():
    stray = sorted({p.parts[0] for p in repo_files() if (p.parts[0] not in ROOT_DIRS if len(p.parts) > 1 else p.name not in ROOT_FILES)})
    assert stray == [], f"not allowed at the repository root (DESIGN.md 仓库目录): {stray}"


def test_no_lens_files_logs_or_temp_files_outside_their_places():
    bad = [str(p) for p in repo_files() if JUNK.search(str(p))
           and not (p.suffix.lower() in (".zmx", ".zda") and p.parts[0] in ("library", "tests"))]
    assert bad == [], f"lens/log/temp files belong in work/ or the system temp directory: {bad}"


def test_docs_are_cross_job_or_per_recipe():
    known = recipes()
    bad = []
    for p in repo_files():
        if p.parts[0] != "docs":
            continue
        rel = p.parts[1:]
        ok = rel in (("cli.md",), ("workflow.drawio",)) or (rel[0] == "adr" and len(rel) == 2 and re.match(r"\d{4}-[a-z0-9-]+\.md$", rel[1])) \
            or (rel[0] == "recipes" and len(rel) >= 3 and rel[1] in known)
        if not ok:
            bad.append(str(p))
    assert bad == [], f"docs/ holds cli.md, workflow.drawio, adr/NNNN-*.md and recipes/<recipe>/ only: {bad}"


@pytest.mark.parametrize("top", ["library", "experiments"])
def test_every_library_and_experiment_has_a_readme(top):
    files = [p for p in repo_files() if p.parts[0] == top]
    loose = [str(p) for p in files if len(p.parts) == 2 and p.name != "README.md"]
    assert loose == [], f"{top}/ has loose files; put them in a subdirectory: {loose}"
    missing = sorted({p.parts[1] for p in files if len(p.parts) > 2} - {p.parts[1] for p in files if len(p.parts) == 3 and p.name == "README.md"})
    assert missing == [], f"{top}/<name>/ needs a README.md: {missing}"


def _imports_recipe(path: Path) -> bool:
    return bool(re.search(r"^\s*(from|import)\s+saos\.recipes\b", path.read_text(encoding="utf-8"), re.M))


def test_core_does_not_depend_on_recipes():
    core = [p for p in repo_files() if p.suffix == ".py" and p.parts[:2] == ("src", "saos") and p.parts[2] != "recipes" and p.name != "cli.py"]
    tests = [p for p in repo_files() if p.suffix == ".py" and p.parts[0] == "tests" and p.parts[1] != "recipes"]
    bad = [str(p) for p in core + tests if _imports_recipe(ROOT / p)]
    assert bad == [], f"core code and core tests must not import saos.recipes (recipe tests go to tests/recipes/<recipe>/): {bad}"


def test_recipe_parts_match_a_recipe_skill():
    known = recipes()
    bad = [str(p) for p in repo_files()
           if (p.parts[:3] == ("src", "saos", "recipes") and len(p.parts) > 4 and p.parts[3] not in known)
           or (p.parts[:2] == ("tests", "recipes") and len(p.parts) > 3 and p.parts[2] not in known)]
    assert bad == [], f"recipe code and tests need skills/recipes/<recipe>/SKILL.md: {bad}"
    assert all((ROOT / "skills" / "recipes" / r / "SKILL.md").is_file() for r in known)


def test_local_work_tree_follows_the_job_layout():
    work = ROOT / "work"
    if not work.is_dir():
        pytest.skip("no local work/ directory")
    bad = []
    for instance in work.iterdir():
        if not instance.is_dir():
            bad.append(f"{instance.name} (file in work/)")
            continue
        bad += [f"{instance.name}/{p.name}" for p in instance.iterdir() if p.name not in INSTANCE_ENTRIES]
        jobs = instance / "jobs"
        for job in jobs.iterdir() if jobs.is_dir() else []:
            bad += [f"{instance.name}/jobs/{job.name}/{p.name}" for p in job.iterdir() if p.name not in JOB_ENTRIES]
            for p in jobs_mod.Job(job.name, job).verify_seals():
                bad.append(f"{instance.name}/jobs/{job.name}/{p['path']} ({p['problem']} after sealing)")
            explore = job / "explore"
            for topic in explore.iterdir() if explore.is_dir() else []:
                # __pycache__ 是导入 explore 里的脚本时 Python 自动生成的，不是探索主题 / Python's import cache, not a topic
                if topic.is_dir() and topic.name != "__pycache__" and not (topic / "README.md").is_file():
                    bad.append(f"{instance.name}/jobs/{job.name}/explore/{topic.name} (no README.md)")
    assert bad == [], "work/ layout (DESIGN.md 任务目录): instance = daemon/ + jobs/; job extras only notes/, checks/, explore/<topic>/ with README.md: " + ", ".join(bad)


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
