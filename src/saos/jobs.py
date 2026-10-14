"""设计任务目录。

每个设计任务一个目录：

    <SAOS_HOME>/jobs/<job_id>/        （SAOS_HOME 默认 work/main，其他实例为 work/<实例名>）
        job.json     任务状态（原子写入）
        spec/        光学规格文件
        sources/     输入材料，含用户需求访谈（interview.md、interview.yaml）
        lenses/      镜头文件副本，所有 .zmx 读写都限制在这里
        log.jsonl    执行记录，每行一条
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from saos import config
from saos.protocol import ErrorCode, SaosError, write_json

JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
LENS_SUFFIX = ".zmx"


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class Job:
    id: str
    root: Path

    @property
    def state_file(self) -> Path:
        return self.root / "job.json"

    @property
    def spec_dir(self) -> Path:
        return self.root / "spec"

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

    def append_log(self, record: dict[str, Any]) -> None:
        line = json.dumps({"time": now_iso(), **record}, ensure_ascii=False)
        with self.log_file.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def resolve_lens(self, name: str, *, must_exist: bool) -> Path:
        """把相对 lenses/ 的文件名解析成绝对路径，拒绝任何越出本任务目录的路径。"""
        if not name or Path(name).is_absolute() or Path(name).drive:
            raise SaosError(ErrorCode.PATH_OUTSIDE_JOB, f"镜头文件必须是相对 lenses/ 的路径：{name!r}")
        path = (self.lenses_dir / name).resolve()
        lenses = self.lenses_dir.resolve()
        if lenses not in path.parents:
            raise SaosError(
                ErrorCode.PATH_OUTSIDE_JOB,
                f"路径越出任务 {self.id} 的 lenses/ 目录：{name!r}",
                {"job": self.id, "path": str(path)},
            )
        if path.suffix.lower() != LENS_SUFFIX:
            raise SaosError(ErrorCode.BAD_ARGS, f"镜头文件必须是 {LENS_SUFFIX}：{name!r}")
        if must_exist and not path.is_file():
            raise SaosError(ErrorCode.FILE_NOT_FOUND, f"任务 {self.id} 中没有镜头文件 {name!r}")
        return path

    def relative_lens(self, path: Path) -> str:
        return path.resolve().relative_to(self.lenses_dir.resolve()).as_posix()


def get_job(job_id: str) -> Job:
    if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
        raise SaosError(ErrorCode.BAD_ARGS, f"任务编号格式不正确：{job_id!r}")
    root = config.jobs_dir() / job_id
    if not (root / "job.json").is_file():
        raise SaosError(ErrorCode.JOB_NOT_FOUND, f"没有设计任务 {job_id}")
    return Job(job_id, root)


def create_job(name: str, spec_path: str | None = None) -> Job:
    if not NAME_RE.match(name or ""):
        raise SaosError(ErrorCode.BAD_ARGS, "任务名只能包含字母、数字、- 和 _，长度 1–48，且以字母或数字开头")
    spec_src = None
    if spec_path is not None:
        spec_src = Path(spec_path)
        if not spec_src.is_file():
            raise SaosError(ErrorCode.FILE_NOT_FOUND, f"规格文件不存在：{spec_path}")

    job_id = f"{datetime.now():%Y%m%d-%H%M%S}-{name}"
    root = config.jobs_dir() / job_id
    try:
        root.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise SaosError(ErrorCode.JOB_EXISTS, f"设计任务 {job_id} 已存在") from exc

    job = Job(job_id, root)
    job.spec_dir.mkdir()
    job.lenses_dir.mkdir()
    (root / "sources").mkdir()
    spec_rel = None
    if spec_src is not None:
        target = job.spec_dir / spec_src.name
        shutil.copy2(spec_src, target)
        spec_rel = target.relative_to(root).as_posix()

    created = now_iso()
    write_json(
        job.state_file,
        {
            "id": job_id,
            "name": name,
            "status": "created",
            "created_at": created,
            "updated_at": created,
            "spec": spec_rel,
            "current_lens": None,
            "last_saved": None,
        },
    )
    job.append_log({"op": "job.create", "ok": True, "args": {"name": name, "spec": spec_path}})
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
