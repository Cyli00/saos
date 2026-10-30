"""设计任务目录。

每个设计任务一个目录：

    <SAOS_HOME>/jobs/<job_id>/        （SAOS_HOME 默认 work/main，其他实例为 work/<实例名>）
        job.json     任务状态（原子写入）
        spec/        显微物镜配方的光学规格文件（只在用到时创建）
        sources/     输入材料，含用户需求访谈（interview.md）
        lenses/      镜头文件副本，所有 .zmx 读写都限制在这里
        log.jsonl    执行记录，每行一条
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from saos import config
from saos.protocol import ErrorCode, SaosError, write_json

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
LENS_SUFFIX = ".zmx"


def now_iso(*, timespec: str = "seconds") -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec=timespec)


SEALS_FILE = "seals.jsonl"
# 长时间计算的进度和取消请求：工具写、任何时候都能直接读，不经过可能正忙的后台进程
# progress of a long computation and a cancel request: written by the tools, readable at any time without the busy daemon
PROGRESS_FILE = "progress.json"
# 当前镜头的布局、Lens Data 和交叠检查，每次改动镜头后由后台进程重写，给 saos ui 显示 / the live view for saos ui
VIEW_FILE = "view.json"
# 当前镜头的评价函数值和需求各项的值，后台进程在回复之后自动测（saos.live）/ the live metrics, measured by the daemon after replying
METRICS_FILE = "metrics.json"
CANCEL_FILE = "cancel.request"
# lenses/ 下的归档目录：工具写入一次，之后不再改 / archive folders under lenses/: written once by the tools
ARCHIVE_LENS_DIRS = ("rounds", "captures")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def next_archive_number(folder: Path) -> int:
    numbers = [int(p.name.split("-", 1)[0]) for p in folder.iterdir() if p.name.split("-", 1)[0].isdigit()]
    return max(numbers, default=0) + 1


@dataclass(frozen=True)
class Job:
    id: str
    root: Path

    @property
    def mode(self) -> str:
        """interactive（用户在关卡确认、可要求修改）或 autonomous（agent 自行过关卡并记录理由）；旧任务没有这一项，按 interactive。
        interactive (the user confirms the gates and may ask for changes) or autonomous (the agent passes them with its
        reasons on record); older jobs lack the field and count as interactive."""
        return mode_of(self.root)

    @property
    def state_file(self) -> Path:
        return self.root / "job.json"

    @property
    def lenses_dir(self) -> Path:
        return self.root / "lenses"

    @property
    def log_file(self) -> Path:
        return self.root / "log.jsonl"

    def read_state(self) -> dict[str, Any]:
        return json.loads(self.state_file.read_text(encoding="utf-8"))

    def update_state(self, **fields: Any) -> dict[str, Any]:
        state = self.read_state()
        state.update(fields)
        state["updated_at"] = now_iso()
        write_json(self.state_file, state)
        return state

    def is_archive_lens(self, path: Path) -> bool:
        """lenses/rounds/、lenses/captures/ 下的镜头是证据，不能用 lens save 覆盖。
        Lenses under lenses/rounds/ and lenses/captures/ are evidence and must not be overwritten by lens save."""
        try:
            rel = path.resolve().relative_to(self.lenses_dir.resolve())
        except ValueError:
            return False
        return len(rel.parts) > 1 and rel.parts[0] in ARCHIVE_LENS_DIRS

    def seal(self, *paths: Path) -> None:
        """记下工具写入的证据文件的哈希；之后 verify_seals 能发现被改动或删除的文件。
        Record the hash of evidence files written by the tools so that verify_seals can detect later edits or deletions."""
        lines = []
        for p in paths:
            p = Path(p)
            for f in sorted(p.rglob("*")) if p.is_dir() else [p]:
                if f.is_file():
                    lines.append(json.dumps({"path": f.resolve().relative_to(self.root.resolve()).as_posix(),
                                             "sha256": sha256_file(f), "time": now_iso()}, ensure_ascii=False))
        if lines:
            # 多个候选 worker 会同时封存；Windows 的追加写入仍需互斥，避免一条记录覆盖另一条。
            with (self.root / SEALS_FILE).open("a+b") as fh:
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    fh.seek(0, 2)
                    fh.write("".join(line + "\n" for line in lines).encode("utf-8"))
                    fh.flush()
                finally:
                    fh.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def verify_seals(self) -> list[dict[str, Any]]:
        """返回被改动或缺失的证据文件（每个路径取最后一次封存）。 / Evidence files changed or missing since sealing."""
        path = self.root / SEALS_FILE
        if not path.is_file():
            return []
        latest: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                latest[row["path"]] = row["sha256"]
        problems = []
        for rel, sha in sorted(latest.items()):
            f = self.root / rel
            if not f.is_file():
                problems.append({"path": rel, "problem": "missing"})
            elif sha256_file(f) != sha:
                problems.append({"path": rel, "problem": "changed"})
        return problems

    def write_progress(self, **fields: Any) -> dict[str, Any]:
        """合并写入 progress.json（原子替换）。进度只是给人看的：重试后仍写不进去时记一笔日志、返回合并后的内容，
        不让显示故障打断正在跑的计算。
        Merge fields into progress.json (atomic replace). Progress is for display only: when it still cannot be written
        after the retries, log it and return the merged content, so a display fault never stops a running computation."""
        current = self.read_progress() or {}
        current.update(fields, updated=now_iso())
        try:
            write_json(self.root / PROGRESS_FILE, current)
        except OSError as exc:
            logging.getLogger("saos.jobs").warning("progress.json of job %s not written: %s", self.id, exc)
            current["write_error"] = f"{type(exc).__name__}: {exc}"[:300]
        return current

    def read_progress(self) -> dict[str, Any] | None:
        path = self.root / PROGRESS_FILE
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def request_cancel(self) -> dict[str, Any]:
        (self.root / CANCEL_FILE).write_text(now_iso(), encoding="utf-8")
        # 分离运行的扫描有自己的取消文件，同一条命令一起停 / detached scans have their own cancel file; stop them too
        detached = detached_runs(self.root)
        for run in detached:
            (self.root / run["folder"] / CANCEL_FILE).write_text(now_iso(), encoding="utf-8")
        return {"job": self.id, "cancel_requested": True, "progress": self.read_progress(), "detached": [r["folder"] for r in detached]}

    def cancel_requested(self) -> bool:
        return (self.root / CANCEL_FILE).is_file()

    def clear_cancel(self) -> None:
        (self.root / CANCEL_FILE).unlink(missing_ok=True)

    def append_log(self, record: dict[str, Any]) -> None:
        line = json.dumps({"time": now_iso(), **record}, ensure_ascii=False)
        with self.log_file.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def resolve_lens(self, name: str, *, must_exist: bool) -> Path:
        """把相对 lenses/ 的文件名解析成绝对路径，拒绝任何越出本任务目录的路径。"""
        if not name or Path(name).is_absolute() or Path(name).drive:
            raise SaosError(ErrorCode.PATH_OUTSIDE_JOB, f"lens files must be paths relative to lenses/: {name!r}")
        path = (self.lenses_dir / name).resolve()
        lenses = self.lenses_dir.resolve()
        if lenses not in path.parents:
            raise SaosError(
                ErrorCode.PATH_OUTSIDE_JOB,
                f"path escapes lenses/ of job {self.id}: {name!r}",
                {"job": self.id, "path": str(path)},
            )
        if path.suffix.lower() != LENS_SUFFIX:
            raise SaosError(ErrorCode.BAD_ARGS, f"lens files must be {LENS_SUFFIX}: {name!r}")
        if must_exist and not path.is_file():
            raise SaosError(ErrorCode.FILE_NOT_FOUND, f"job {self.id} has no lens file {name!r}")
        return path

    def relative_lens(self, path: Path) -> str:
        return path.resolve().relative_to(self.lenses_dir.resolve()).as_posix()


DETACHED_DIRS = ("scans",)


def inside(root: Path, rel: str) -> Path:
    """任务目录内的路径，越出时拒绝。先按字面规范化比较，再把已存在的部分按真实路径查一次（挡住指向任务外的链接）。
    不对还不存在的路径整体 resolve：Windows 上几个 worker 同时建同一目录时，那样偶尔会误报越界（2026-10-17 实测）。
    A path inside the job, refused when it escapes. Compared after normalising the text, then the existing part is checked
    by its real path (links out of the job). A not-yet-existing path is never resolved as a whole: on Windows, workers
    creating the same folder at once occasionally made that report an escape (measured 2026-10-17)."""
    import os

    base = Path(root).resolve()
    target = Path(os.path.normpath(base / rel))
    existing = target
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    real = existing.resolve()
    if base not in target.parents or (real != base and base not in real.parents):
        raise SaosError(ErrorCode.PATH_OUTSIDE_JOB, f"path escapes the job directory: {rel}")
    return target


def detached_runs(root: Path) -> list[dict[str, Any]]:
    """正在后台运行的分离任务（saos scan --detach）：进度为 running 且所属后台进程还活着。
    Detached runs in progress (saos scan --detach): progress says running and the owning daemon is alive."""
    from saos import client

    out = []
    for name in DETACHED_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for folder in sorted(base.iterdir()):
            # 写坏或被改过的进度文件只跳过，不让同一任务的其他查询失败 / a damaged progress file is skipped, not fatal for the job
            try:
                progress = json.loads((folder / PROGRESS_FILE).read_text(encoding="utf-8"))
                alive = (isinstance(progress, dict) and progress.get("status") == "running"
                         and client.pid_alive(int(progress.get("daemon_pid") or 0)))
            except (OSError, ValueError, TypeError, OverflowError, RecursionError):
                continue
            if alive:
                out.append({**progress, "folder": folder.relative_to(root).as_posix()})
    return out


def get_job(job_id: str) -> Job:
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise SaosError(ErrorCode.BAD_ARGS, f"malformed job id: {job_id!r}")
    root = config.jobs_dir() / job_id
    if not (root / "job.json").is_file():
        raise SaosError(ErrorCode.JOB_NOT_FOUND, f"no design job {job_id}")
    return Job(job_id, root)


# 任务的两种流程（ADR-0021）：做题、测试用 autonomous，从头设计用 interactive / the two workflows of a job
MODES = ("interactive", "autonomous")
# 用户在窗口里对关卡提出的修改意见 / change requests the user made at a gate in the window
REVISIONS_FILE = "revisions.jsonl"


def mode_of(root: Path) -> str:
    try:
        mode = json.loads((Path(root) / "job.json").read_text(encoding="utf-8")).get("mode")
    except (OSError, ValueError):
        mode = None
    return mode if mode in MODES else "interactive"


def require_autonomous(root: Path, what: str) -> None:
    """agent 自己过关卡、自己停止只在自主任务里可以。/ Only an autonomous job lets the agent pass a gate or stop by itself."""
    if mode_of(root) != "autonomous":
        raise SaosError(ErrorCode.BAD_ARGS, f"{what} by the agent alone is only for autonomous jobs; this job is interactive, so the user decides "
                        "(the workflow window's button, or --quote with their words when the window cannot be used)")


def make_interactive(job: Job) -> dict[str, Any]:
    """自主任务改为参与：只能由用户在窗口里做，反过来不行，agent 不能为了过关卡把任务切成自主（ADR-0021）。
    Turn an autonomous job interactive: only the user does it, in the window; never the other way round."""
    if job.mode == "interactive":
        return {"mode": "interactive", "changed": False}
    job.update_state(mode="interactive", mode_changed_at=now_iso())
    job.append_log({"op": "job.mode", "ok": True, "args": {"mode": "interactive"}})
    return {"mode": "interactive", "changed": True}


def record_revision(job: Job, gate: str, target: str, note: str) -> dict[str, Any]:
    """用户在关卡处要求修改：记入 revisions.jsonl，并在 sources/interview.md 末尾追加原话。target 是当时显示的版本
    （需求单的 sha 或建模说明的文件夹）；出了新版本，这条请求就算处理过了。
    The user asked for changes at a gate: appended to revisions.jsonl and, verbatim, to sources/interview.md. target is
    the version shown (the demand sha or the description folder); a new version settles the request."""
    note = str(note or "").strip()
    if gate not in ("demand", "model") or not target or not note:
        raise SaosError(ErrorCode.BAD_ARGS, "a change request needs the gate (demand or model), the version shown and the user's note")
    record = {"gate": gate, "target": target, "note": note[:4000], "time": now_iso()}
    with (job.root / REVISIONS_FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    label = {"demand": f"需求单 {target[:8]}", "model": f"建模说明 {target}"}[gate]
    interview = job.root / "sources" / "interview.md"
    interview.parent.mkdir(parents=True, exist_ok=True)
    with interview.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## {record['time']} 用户在窗口里要求修改（{label}）\n\n{record['note']}\n")
    return record


def revisions(root: Path) -> list[dict[str, Any]]:
    path = Path(root) / REVISIONS_FILE
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def create_job(name: str, mode: str = "interactive") -> Job:
    if not NAME_RE.match(name or ""):
        raise SaosError(ErrorCode.BAD_ARGS, "job names: letters, digits, - and _, 1–48 chars, starting with a letter or digit")
    if mode not in MODES:
        raise SaosError(ErrorCode.BAD_ARGS, f"mode must be one of {MODES}")

    job_id = f"{datetime.now():%Y%m%d-%H%M%S}-{name}"
    root = config.jobs_dir() / job_id
    try:
        root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise SaosError(ErrorCode.JOB_EXISTS, f"design job {job_id} already exists") from exc

    job = Job(job_id, root)
    job.lenses_dir.mkdir()
    (root / "sources").mkdir()

    created = now_iso()
    write_json(
        job.state_file,
        {
            "id": job_id,
            "name": name,
            "mode": mode,
            "status": "created",
            "created_at": created,
            "updated_at": created,
            "current_lens": None,
            "last_saved": None,
        },
    )
    job.append_log({"op": "job.create", "ok": True, "args": {"name": name, "mode": mode}})
    return job


def list_jobs() -> list[dict[str, Any]]:
    root = config.jobs_dir()
    if not root.is_dir():
        return []
    result = []
    for entry in sorted(root.iterdir()):
        state_file = entry / "job.json"
        if state_file.is_file():
            state = json.loads(state_file.read_text(encoding="utf-8"))
            result.append({k: state.get(k) for k in ("id", "status", "created_at", "current_lens")})
    return result


def sealed(root: Path, relative: str) -> bool:
    path, recorded = root / SEALS_FILE, None
    if not path.is_file() or not (root / relative).is_file():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            row = json.loads(line)
            if row.get("path") == relative:
                recorded = row.get("sha256")
    return recorded is not None and recorded == sha256_file(root / relative)


def require_zos_history(root: Path) -> None:
    """旧双后端任务只留作历史，不把旧结论接入新比较窗口。"""
    legacy = (root / "stages" / "zos.json").is_file()
    path = root / "ledger.jsonl"
    if path.is_file() and not legacy:
        with path.open(encoding="utf-8") as stream:
            legacy = any(json.loads(line).get("stage") == "exploration" for line in stream if line.strip())
    if legacy:
        raise SaosError(ErrorCode.BAD_ARGS,
                        "this is a legacy hybrid job; keep its evidence unchanged, create a new job, "
                        "import an OpticStudio .zmx or rebuild from the original prescription, then confirm and measure again")
