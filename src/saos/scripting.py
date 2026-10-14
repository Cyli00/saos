"""在后台进程内执行 agent 编写的脚本（ADR-0007）。

脚本定义 run(ctx)，返回可 JSON 化的结果。每次执行都把脚本副本和结果存进任务的 scripts/，
编号递增：scripts/NNN-<name>.py 与 scripts/NNN-<name>.json。
"""

from __future__ import annotations

import json
import re
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any

from saos import jobs
from saos.protocol import ErrorCode, SaosError, jsonable, write_json

if TYPE_CHECKING:
    from saos.daemon import Daemon

SCRIPTS_DIR = "scripts"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
TRACEBACK_CHARS = 4000


class ScriptContext:
    """脚本能用的全部入口。镜头切换和保存要经过这里，后台进程才能跟踪当前加载的文件和未保存状态。"""

    def __init__(self, daemon: Daemon, job: jobs.Job, args: dict[str, Any]):
        self._daemon = daemon
        self.job = job
        self.args = args
        self.backend = daemon.backend

    @property
    def system(self):
        """OpticStudio 的 PrimarySystem（只有真实后端才有）。"""
        system = getattr(self.backend, "system", None)
        if system is None:
            raise SaosError(ErrorCode.BAD_ARGS, f"{type(self.backend).__name__} has no OpticStudio system")
        return system

    @property
    def zosapi(self):
        """ZOSAPI 命名空间，例如 ctx.zosapi.Editors.LDE.SurfaceType。"""
        from saos.backend.zos import _load_zosapi

        return _load_zosapi(self.backend.root)

    @property
    def lens(self) -> Path | None:
        loaded = self._daemon.loaded
        return loaded.path if loaded is not None and loaded.job.id == self.job.id else None

    @property
    def dirty(self) -> bool:
        loaded = self._daemon.loaded
        return bool(loaded is not None and loaded.job.id == self.job.id and loaded.dirty)

    def describe(self) -> dict[str, Any]:
        loaded = self._daemon.loaded
        return loaded.describe() if loaded is not None and loaded.job.id == self.job.id else {"job": self.job.id, "file": None, "dirty": False}

    def mark_dirty(self) -> None:
        loaded = self._daemon.loaded
        if loaded is not None and loaded.job.id == self.job.id:
            loaded.dirty = True

    def load(self, name: str) -> str:
        """加载本任务 lenses/ 下的文件；不检查当前镜头的未保存修改，由脚本自己负责。"""
        from saos.daemon import Loaded

        path = self.job.resolve_lens(name, must_exist=True)
        self._daemon.loaded = None
        self.backend.load(path)
        self._daemon.loaded = Loaded(self.job, path)
        return self.job.relative_lens(path)

    def save_as(self, name: str, *, overwrite: bool = False) -> str:
        """把当前系统存为本任务 lenses/ 下的文件，并把它记为当前加载的镜头。"""
        from saos.daemon import Loaded

        target = self.job.resolve_lens(name, must_exist=False)
        if target.exists() and not overwrite and target != self.lens:
            raise SaosError(ErrorCode.FILE_EXISTS, f"job {self.job.id} already has {name}; pass overwrite=True")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.backend.save_as(target)
        self._daemon.loaded = Loaded(self.job, target)
        self._daemon.window_unsaved = False
        rel = self.job.relative_lens(target)
        self.job.update_state(current_lens=rel, last_saved=rel)
        return rel

    def path(self, rel: str) -> Path:
        """任务目录内的路径；越出任务目录时拒绝。"""
        path = (self.job.root / rel).resolve()
        if self.job.root.resolve() not in path.parents:
            raise SaosError(ErrorCode.PATH_OUTSIDE_JOB, f"path escapes the job directory: {rel}")
        return path


def next_copy(job: jobs.Job, name: str) -> Path:
    if not NAME_RE.match(name):
        raise SaosError(ErrorCode.BAD_ARGS, "script name: letters, digits, - and _, 1–48 chars, starting with a letter or digit")
    folder = job.root / SCRIPTS_DIR
    folder.mkdir(exist_ok=True)
    return folder / f"{jobs.next_archive_number(folder):03d}-{name}.py"


def execute(daemon: Daemon, job: jobs.Job, name: str, source: str, args: dict[str, Any]) -> dict[str, Any]:
    copy = next_copy(job, name)
    copy.write_text(source, encoding="utf-8")
    rel = copy.relative_to(job.root).as_posix()
    record: dict[str, Any] = {"script": rel, "args": args}
    started = time.perf_counter()
    try:
        namespace: dict[str, Any] = {"__name__": f"saos_script_{copy.stem.replace('-', '_')}", "__file__": str(copy)}
        exec(compile(source, str(copy), "exec"), namespace)
        run = namespace.get("run")
        if not callable(run):
            raise SaosError(ErrorCode.BAD_ARGS, "script must define run(ctx)")
        result = jsonable(run(ScriptContext(daemon, job, args)))
        try:
            json.dumps(result, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise SaosError(ErrorCode.BAD_ARGS, f"run(ctx) must return JSON-serializable data: {exc}") from exc
        record.update(ok=True, result=result)
        return {"script": rel, "result": result}
    except SaosError as exc:
        record.update(ok=False, error=exc.to_dict())
        raise
    except Exception as exc:
        tb = traceback.format_exc()[-TRACEBACK_CHARS:]
        record.update(ok=False, error={"exception": type(exc).__name__, "message": str(exc)[:2000], "traceback": tb})
        if not daemon.backend.healthy():
            raise  # 交给后台进程按连接中断处理
        raise SaosError(ErrorCode.SCRIPT_ERROR, f"script raised {type(exc).__name__}: {exc}", {"script": rel, "traceback": tb}) from exc
    finally:
        record["elapsed_s"] = round(time.perf_counter() - started, 3)
        try:
            write_json(copy.with_suffix(".json"), record)
        except (OSError, ValueError, TypeError):
            pass
