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
from saos.kit import KitMethods
from saos.protocol import ErrorCode, SaosError, jsonable, write_json

if TYPE_CHECKING:
    from saos.daemon import Daemon

SCRIPTS_DIR = "scripts"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
TRACEBACK_CHARS = 4000


class ScriptContext(KitMethods):
    """脚本能用的全部入口。镜头切换和保存要经过这里，后台进程才能跟踪当前加载的文件和未保存状态。
    kit 的方法（set、get、spot、trace、op、analysis、kept、restored、lib、memo）也挂在这里，见 saos.kit。"""

    def __init__(self, daemon: Daemon, job: jobs.Job, args: dict[str, Any], cache: dict[Any, Any] | None = None,
                 script: Path | None = None):
        self._daemon = daemon
        self.job = job
        self.args = args
        self.backend = daemon.backend
        self.captured: list[str] = []
        # 同一次判定内各判定脚本共享，见 kit.memo / shared by the check scripts of one evaluation, see kit.memo
        self.cache: dict[Any, Any] = {} if cache is None else cache
        # 本次执行的脚本副本，ctx.map("fn", …) 在 worker 里从这里找函数 / the running script copy; ctx.map("fn", …) finds fn here
        self.script = script

    def child(self, args: dict[str, Any], cache: dict[Any, Any] | None = None) -> ScriptContext:
        """给判定脚本用的上下文：同一个任务和后台进程，默认共享 cache。/ A context for a check script; shares the cache by default."""
        return ScriptContext(self._daemon, self.job, args, self.cache if cache is None else cache, self.script)

    def measure(self, ids: list[str] | None = None) -> dict[str, Any]:
        """按需求单的判定测当前镜头（与 check、round 同一套定义），不存证据、不记录；返回 values、shortfall（只含所测各项）、
        hard_failing、invalid、errors。改镜头—测量的循环用它，不必在脚本里重写判定。
        Measure the current lens with the demand's checks (the same definitions as check and round) without evidence or a
        record; returns values, shortfall (of the measured items only), hard_failing, invalid, errors."""
        from saos import demand

        return demand.quick(self, ids)

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

    def capture_window(self) -> str | None:
        """换掉窗口内容之前调用：窗口有未保存内容时存成 lenses/captures/ 下的副本，返回其路径。"""
        rel = self._daemon.capture_window(self.job)
        if rel:
            self.captured.append(rel)
        return rel

    def load(self, name: str) -> str:
        """加载本任务 lenses/ 下的文件；agent 自己的未保存修改由脚本负责，窗口里的未保存内容先存副本。"""
        from saos.daemon import Loaded

        path = self.job.resolve_lens(name, must_exist=True)
        self.capture_window()
        self._daemon.loaded = None
        self.backend.load(path)
        self._daemon.loaded = Loaded(self.job, path)
        return self.job.relative_lens(path)

    def save_as(self, name: str, *, overwrite: bool = False, archive: bool = False) -> str:
        """把当前系统存为本任务 lenses/ 下的文件，并把它记为当前加载的镜头。归档目录只有工具（archive=True）能写。
        Save the system as a file under the job's lenses/ and make it the loaded lens. Only the tools (archive=True)
        write into the archive folders lenses/rounds/ and lenses/captures/."""
        from saos.daemon import Loaded

        target = self.job.resolve_lens(name, must_exist=False)
        if self.job.is_archive_lens(target) and not archive:
            raise SaosError(ErrorCode.BAD_ARGS, "lenses/rounds/ and lenses/captures/ hold evidence written by the tools; save under another name")
        if target.exists() and not overwrite and target != self.lens:
            raise SaosError(ErrorCode.FILE_EXISTS, f"job {self.job.id} already has {name}; pass overwrite=True")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.backend.save_as(target)
        self._daemon.loaded = Loaded(self.job, target)
        self._daemon.window_unsaved = False
        rel = self.job.relative_lens(target)
        self.job.update_state(current_lens=rel, last_saved=rel)
        return rel

    def progress(self, stage: str, **data: Any) -> bool:
        """长脚本报告进度（写进任务的 progress.json，saos job show 可见）；返回是否收到了取消请求。
        Report progress from a long script (job's progress.json, shown by saos job show); returns whether a cancel was requested."""
        self.job.write_progress(stage=str(stage), data=jsonable(data))
        return self.job.cancel_requested()

    def map(self, fn: str, items: list[Any], workers: int | None = None, reset: bool = True, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """在常驻的 standalone 实例池里并发执行 fn(w, item)，按条目顺序返回结果（saos.parallel）。fn 是本脚本里的函数名、
        "explore/x.py:fn" 或 "saos.scan:point"；args 原样交给每个 w.args。
        Run fn(w, item) over items on the daemon's pool of standalone instances, results in item order (saos.parallel).
        fn is a function of this script, "explore/x.py:fn" or "saos.scan:point"; args reaches every w.args."""
        from saos import parallel

        return parallel.run_map(self, fn, items, workers, reset, args)

    def pool_close(self) -> dict[str, Any]:
        """立即关闭 worker 池、释放许可证（否则空闲 SAOS_POOL_IDLE_S 秒后自动关闭）。
        Close the worker pool and free the licences now (otherwise it closes after SAOS_POOL_IDLE_S idle seconds)."""
        pool = self._daemon.pool
        before = pool.status()
        if not pool.lock.acquire(timeout=2):
            raise SaosError(ErrorCode.POOL_BUSY, "the worker pool is busy with a detached scan; cancel it first (saos job cancel)")
        try:
            pool.close()
        finally:
            pool.lock.release()
        return before

    def cancel_requested(self) -> bool:
        """用户或 agent 执行了 saos job cancel；脚本应在下一个安全点结束并保存已有结果。
        saos job cancel was run; the script should stop at the next safe point and keep what it has."""
        return self.job.cancel_requested()

    def path(self, rel: str) -> Path:
        """任务目录内的路径；越出任务目录时拒绝。"""
        return jobs.inside(self.job.root, rel)


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
        ctx = ScriptContext(daemon, job, args, script=copy)
        result = jsonable(run(ctx))
        try:
            json.dumps(result, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise SaosError(ErrorCode.BAD_ARGS, f"run(ctx) must return JSON-serializable data: {exc}") from exc
        record.update(ok=True, result=result)
        out = {"script": rel, "result": result}
        if ctx.captured:
            record["captured"] = out["captured"] = ctx.captured
        return out
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
            job.seal(copy, copy.with_suffix(".json"))
        except (OSError, ValueError, TypeError):
            pass
