"""常驻后台进程：持有 OpticStudio 连接，依次处理命令行发来的请求（ADR-0002）。

同一时间只加载一个任务的一个镜头文件。所有镜头操作都必须带上任务编号，
与当前加载的任务不一致时直接拒绝，保证不同设计任务的文件不会混用。
"""

from __future__ import annotations

import logging
import math
import os
import secrets
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from multiprocessing.connection import Connection, Listener
from pathlib import Path
from typing import Any, Callable

from saos import config, i18n, jobs, prescription, scripting, workflow
from saos.backend import Backend, make_backend
from saos.backend.base import same_number
from saos.protocol import ErrorCode, SaosError, decode, encode, fail, ok, write_json

log = logging.getLogger("saos.daemon")


def _t(zh: str, en: str) -> str:
    """日志给人看，按 SAOS_LANG 或系统界面语言选中英文。"""
    return i18n.text(i18n.resolve(), zh, en)


REQUEST_READ_TIMEOUT_S = 30
# 显性调试：这些操作可能改变镜头，结束后刷新已打开的分析窗口
REFRESH_AFTER = frozenset({"lens.load", "lens.set", "lens.build", "opt.local", "script.exec", "round.record"})
COMPARED_TEXT_KEYS = ("type", "comment", "material", "is_stop", "solves")
COMPARED_NUMBER_KEYS = ("radius", "thickness", "conic", "semi_diameter")


@dataclass
class Loaded:
    job: jobs.Job
    path: Path
    dirty: bool = False

    def describe(self) -> dict[str, Any]:
        return {"job": self.job.id, "file": self.job.relative_lens(self.path), "dirty": self.dirty}


def _arg(args: dict[str, Any], name: str, kind: type | tuple[type, ...], *, default: Any = ..., allow_none=False):
    if name not in args or (args[name] is None and not allow_none):
        if default is ...:
            raise SaosError(ErrorCode.BAD_ARGS, f"missing argument {name}")
        return default
    value = args[name]
    if value is None and allow_none:
        return None
    if kind is int and isinstance(value, bool) or not isinstance(value, kind):
        raise SaosError(ErrorCode.BAD_ARGS, f"argument {name} has the wrong type: {value!r}")
    return value


def compare_surfaces(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保存前后的面数据逐项对比，返回不一致的条目。"""
    diffs = []
    if len(before) != len(after):
        return [{"field": "num_surfaces", "before": len(before), "after": len(after)}]
    for b, a in zip(before, after):
        for key in COMPARED_TEXT_KEYS:
            if b.get(key) != a.get(key):
                diffs.append({"surface": b["index"], "field": key, "before": b.get(key), "after": a.get(key)})
        for key in COMPARED_NUMBER_KEYS:
            x, y = float(b[key]), float(a[key])
            if not (same_number(x, y) or (math.isnan(x) and math.isnan(y))):
                diffs.append({"surface": b["index"], "field": key, "before": x, "after": y})
    return diffs


class Daemon:
    def __init__(self, backend: Backend, zos_info: dict[str, Any], mode: str):
        self.backend = backend
        self.zos_info = zos_info
        self.mode = mode
        # 启动时加载的代码版本；实验记录每轮核对，防止不同后台进程运行不同版本
        self.code_sha256 = config.code_sha256()
        self.started_at = time.time()
        self.loaded: Loaded | None = None
        self.stopping = False
        # 显性调试：连接时窗口里已有的未保存内容要保护，直到保存、捕获副本或显式放弃
        self.window_unsaved = bool(zos_info.get("initial_needs_save"))
        self.handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "ping": self.op_ping,
            "shutdown": self.op_shutdown,
            "lens.import": self.op_import,
            "lens.load": self.op_load,
            "lens.unload": self.op_unload,
            "lens.info": self.op_info,
            "lens.surfaces": self.op_surfaces,
            "lens.describe": self.op_describe,
            "lens.approve": self.op_approve,
            "lens.set": self.op_set,
            "lens.save": self.op_save,
            "lens.operand": self.op_operand,
            "lens.build": self.op_build,
            "lens.view": self.op_view,
            "lens.analysis": self.op_analysis,
            "lens.screenshot": self.op_screenshot,
            "lens.capture": self.op_capture,
            "opt.vary": self.op_vary,
            "opt.merit": self.op_merit,
            "opt.local": self.op_optimize,
            "script.exec": self.op_exec,
            "harness.check": self.op_check,
            "round.record": self.op_round,
        }

    # ---- 请求分发 ----

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        args = request.get("args") or {}
        started = time.perf_counter()
        error: SaosError | None = None
        result: Any = None
        try:
            if not isinstance(args, dict):
                raise SaosError(ErrorCode.BAD_ARGS, "args must be an object")
            handler = self.handlers.get(op)  # type: ignore[arg-type]
            if handler is None:
                raise SaosError(ErrorCode.UNKNOWN_OP, f"unknown operation {op!r}", {"available": sorted(self.handlers)})
            if op not in ("ping", "shutdown") and not self.backend.healthy():
                raise self._connection_lost({"detected": "connection check before the operation"})
            if op not in ("ping", "shutdown"):
                blockers = self.backend.blocking_dialogs()
                if blockers:
                    raise SaosError(
                        ErrorCode.UI_BLOCKED,
                        f"the OpticStudio window is blocked by a dialog ({', '.join(blockers)}); close it in the window and retry",
                        {"dialogs": blockers},
                    )
            # 连接手动打开的窗口时，带 quiet 的脚本执行期间暂停界面刷新，结束后窗口显示最终状态；SAOS_UI_LIVE=1 时全程刷新
            quiet = op == "script.exec" and bool(args.get("quiet")) and os.environ.get("SAOS_UI_LIVE") != "1"
            with self.backend.activity(f"saos {op}", quiet):
                result = handler(args)
            if op in REFRESH_AFTER:
                self._refresh_windows()
        except SaosError as exc:
            error = exc
        except Exception as exc:  # noqa: BLE001 - 后台进程不能因单个请求崩溃
            log.exception(_t("操作 %s 出现未预期异常", "unexpected error in op %s"), op)
            error = self._unexpected(exc)
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        self._log_to_job(op, args, error, duration_ms)
        return fail(error) if error else ok(result)

    def _window_edited(self) -> bool:
        # 部分面参数无法完整读取；宁可让只读分析的副作用要求保存，也不把用户修改误判为安全。
        return self.window_unsaved or self.backend.has_unsaved_changes()

    def _refresh_windows(self) -> None:
        try:
            self.backend.refresh_windows()
        except Exception:  # noqa: BLE001 - 刷新失败不影响操作结果
            log.exception(_t("刷新显性调试窗口失败", "refreshing the explicit debugging windows failed"))

    def _unexpected(self, exc: Exception) -> SaosError:
        detail = {"exception": type(exc).__name__, "message": str(exc)[:2000]}
        if self.backend.healthy():
            return SaosError(ErrorCode.ZOS_ERROR, f"backend operation failed: {exc}", detail)
        return self._connection_lost(detail)

    def _connection_lost(self, detail: dict[str, Any]) -> SaosError:
        # 实测（2024 R1）：OpticStudio 进程退出后，同一进程内无论复用还是新建 ZOSAPI_Connection，
        # CreateNewApplication 都报 RemotingException，无法重连。所以直接退出后台进程，由调用方重新启动。
        detail["lost_lens"] = self.loaded.describe() if self.loaded else None
        detail["daemon_exiting"] = True
        self.loaded = None
        self.stopping = True
        return SaosError(
            ErrorCode.ZOS_CONNECTION_LOST,
            "the OpticStudio connection is lost and the daemon is exiting; unsaved changes are gone. Run saos daemon start and load the lens again",
            detail,
        )

    def _log_to_job(self, op: Any, args: dict[str, Any], error: SaosError | None, duration_ms: float) -> None:
        job_id = args.get("job") if isinstance(args, dict) else None
        if not isinstance(job_id, str):
            return
        try:
            job = jobs.get_job(job_id)
        except SaosError:
            return
        if "source" in args:
            # 脚本全文已存进 scripts/，执行记录只留长度
            args = {**args, "source": f"<{len(str(args['source']))} chars>"}
        record: dict[str, Any] = {"op": op, "args": args, "ok": error is None, "duration_ms": duration_ms}
        if error is not None:
            record["error"] = {"code": error.code, "message": error.message}
        try:
            job.append_log(record)
        except OSError:
            log.exception(_t("写入任务 %s 的执行记录失败", "writing the run record of job %s failed"), job_id)

    # ---- 辅助 ----

    def _job(self, args: dict[str, Any]) -> jobs.Job:
        return jobs.get_job(_arg(args, "job", str))

    def _require_loaded(self, job: jobs.Job) -> Loaded:
        if self.loaded is None:
            raise SaosError(ErrorCode.NO_LENS_LOADED, f"job {job.id} has no lens loaded; run lens load first")
        if self.loaded.job.id != job.id:
            raise SaosError(
                ErrorCode.JOB_MISMATCH,
                f"the loaded lens belongs to job {self.loaded.job.id}; job {job.id} cannot act on it",
                self.loaded.describe(),
            )
        return self.loaded

    def _check_not_dirty(self, discard: bool, action: str) -> None:
        if self.loaded is not None and self.loaded.dirty and not discard:
            raise SaosError(
                ErrorCode.UNSAVED_CHANGES,
                f"the lens of job {self.loaded.job.id} has unsaved changes; save before {action}, or pass discard",
                self.loaded.describe(),
            )

    def _before_replace(self, job: jobs.Job, discard: bool, action: str) -> str | None:
        """换掉窗口内容之前：agent 自己的未保存修改要先保存或显式放弃；窗口里的未保存内容总是先存副本，
        discard 只放弃 agent 自己的修改，不能替用户放弃。"""
        self._check_not_dirty(discard, action)
        return self.capture_window(job)

    def capture_window(self, job: jobs.Job) -> str | None:
        """窗口的未保存标志无法区分用户修改和分析窗口的副作用，置位时一律把窗口内容存成任务内的副本，返回副本路径。"""
        if not self._window_edited():
            return None
        folder = job.lenses_dir / "captures"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{jobs.next_archive_number(folder):03d}-unsaved{jobs.LENS_SUFFIX}"
        self.backend.save_as(target)
        self.window_unsaved = False
        job.seal(target)
        return job.relative_lens(target)

    # ---- 操作 ----

    def op_ping(self, args: dict[str, Any]) -> dict[str, Any]:
        return {
            "pid": os.getpid(),
            "mode": self.mode,
            "code_sha256": self.code_sha256,
            "started_at": datetime.fromtimestamp(self.started_at).astimezone().isoformat(timespec="seconds"),
            "uptime_s": round(time.time() - self.started_at, 1),
            "opticstudio": self.zos_info,
            "backend_healthy": self.backend.healthy(),
            "loaded": self.loaded.describe() if self.loaded else None,
            "home": str(config.saos_home()),
        }

    def op_shutdown(self, args: dict[str, Any]) -> dict[str, Any]:
        self._check_not_dirty(bool(args.get("discard", False)), "exiting")
        self.stopping = True
        return {"stopping": True, "discarded": self.loaded.describe() if self.loaded and self.loaded.dirty else None}

    def op_import(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        src = Path(_arg(args, "src", str))
        if not src.is_absolute():
            raise SaosError(ErrorCode.BAD_ARGS, "src must be an absolute path")
        if not src.is_file():
            raise SaosError(ErrorCode.FILE_NOT_FOUND, f"source lens file does not exist: {src}")
        if src.suffix.lower() != jobs.LENS_SUFFIX:
            raise SaosError(ErrorCode.BAD_ARGS, f"only {jobs.LENS_SUFFIX} files can be imported")
        name = _arg(args, "name", str, default=src.name)
        dest = job.resolve_lens(name, must_exist=False)
        if job.is_archive_lens(dest):
            raise SaosError(ErrorCode.BAD_ARGS, "lenses/rounds/ and lenses/captures/ hold evidence written by the tools; import under another name")
        if self.loaded is not None and self.loaded.path == dest:
            raise SaosError(ErrorCode.BAD_ARGS, f"{name} is loaded; an import cannot overwrite it")
        if dest.exists() and not args.get("overwrite", False):
            raise SaosError(ErrorCode.FILE_EXISTS, f"job {job.id} already has {name}; pass overwrite to replace it")
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return {"job": job.id, "file": job.relative_lens(dest), "source": str(src)}

    def op_load(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        path = job.resolve_lens(_arg(args, "file", str), must_exist=True)
        captured = self._before_replace(job, bool(args.get("discard", False)), "loading another lens")
        self.loaded = None
        self.backend.load(path)
        self.loaded = Loaded(job, path)
        rel = job.relative_lens(path)
        job.update_state(status="active", current_lens=rel)
        return {"job": job.id, "file": rel, "num_surfaces": len(self.backend.surfaces()), "captured": captured}

    def op_unload(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        if self.loaded is None:
            return {"job": job.id, "unloaded": None}
        loaded = self._require_loaded(job)
        captured = self._before_replace(job, bool(args.get("discard", False)), "unloading")
        self.backend.new_system()
        self.loaded = None
        return {"job": job.id, "unloaded": loaded.describe(), "captured": captured}

    def op_info(self, args: dict[str, Any]) -> dict[str, Any]:
        loaded = self._require_loaded(self._job(args))
        return {**loaded.describe(), "system": self.backend.system_info()}

    def op_surfaces(self, args: dict[str, Any]) -> dict[str, Any]:
        loaded = self._require_loaded(self._job(args))
        return {**loaded.describe(), "surfaces": self.backend.surfaces()}

    def op_describe(self, args: dict[str, Any]) -> dict[str, Any]:
        """建模说明：models/NNN-<镜头>/ 下的 index.html、model.json 和布局图，给用户在优化前核对模型。"""
        from saos import model_report

        job = self._job(args)
        loaded = self._require_loaded(job)
        notes = _arg(args, "notes", dict, default=None)
        root = job.root / "models"
        root.mkdir(exist_ok=True)
        folder = root / f"{jobs.next_archive_number(root):03d}-{loaded.path.stem}"
        desc = self.backend.describe_model(folder, "layout")
        lens = loaded.path.relative_to(job.lenses_dir).as_posix()
        result = model_report.write(job, desc, folder, notes, lens, _arg(args, "lang", str, default=None))
        if notes is not None:
            notes_file = job.root / "notes" / f"model-{folder.name.split('-', 1)[0]}.json"
            write_json(notes_file, notes)
            job.seal(notes_file)
            result["notes"] = notes_file.relative_to(job.root).as_posix()
        return {**loaded.describe(), **result, "folder": folder.relative_to(job.root).as_posix()}

    def op_approve(self, args: dict[str, Any]) -> dict[str, Any]:
        """记录用户对某份建模说明的确认（原话）；之后结构一致的镜头才能记录调试轮次。
        Record the user's approval (their words) of a model description; rounds then require a matching structure."""
        from saos import model_gate

        return model_gate.approve(self._job(args), _arg(args, "model", str), _arg(args, "quote", str))

    def op_set(self, args: dict[str, Any]) -> dict[str, Any]:
        loaded = self._require_loaded(self._job(args))
        surface = _arg(args, "surface", int)
        param = _arg(args, "param", str)
        if "value" not in args:
            raise SaosError(ErrorCode.BAD_ARGS, "missing argument value")
        current = self.backend.surfaces()
        before = current[surface].get(param) if 0 <= surface < len(current) else None
        after = self.backend.set_surface(surface, param, args["value"])
        loaded.dirty = True
        return {**loaded.describe(), "surface": surface, "param": param, "before": before, "after": after.get(param)}

    def op_save(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        loaded = self._require_loaded(job)
        name = _arg(args, "file", str, default=None)
        target = loaded.path if name is None else job.resolve_lens(name, must_exist=False)
        if job.is_archive_lens(target):
            raise SaosError(ErrorCode.BAD_ARGS, "lenses/rounds/ and lenses/captures/ hold evidence written by the tools; save under another name")
        if target != loaded.path and target.exists() and not args.get("overwrite", False):
            raise SaosError(ErrorCode.FILE_EXISTS, f"job {job.id} already has {name}; pass overwrite to replace it")
        verify = bool(args.get("verify", True))
        snapshot = self.backend.surfaces() if verify else None
        target.parent.mkdir(parents=True, exist_ok=True)
        self.backend.save_as(target)
        loaded.path = target
        loaded.dirty = False
        self.window_unsaved = False
        rel = job.relative_lens(target)
        job.update_state(current_lens=rel, last_saved=rel)
        result: dict[str, Any] = {"job": job.id, "file": rel, "verified": False}
        if verify:
            # 从磁盘重新加载刚保存的文件，与保存前的内存数据逐项比对
            self.backend.load(target)
            diffs = compare_surfaces(snapshot or [], self.backend.surfaces())
            if diffs:
                raise SaosError(ErrorCode.VERIFY_FAILED, f"{rel} differs after reloading from the state before saving", {"diffs": diffs[:50]})
            result.update(verified=True, surfaces_checked=len(snapshot or []))
        return result

    def op_build(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        presc = prescription.normalize(_arg(args, "prescription", dict))
        target = job.resolve_lens(_arg(args, "file", str), must_exist=False)
        if job.is_archive_lens(target):
            raise SaosError(ErrorCode.BAD_ARGS, "lenses/rounds/ and lenses/captures/ hold evidence written by the tools; save under another name")
        if target.exists() and not args.get("overwrite", False):
            raise SaosError(ErrorCode.FILE_EXISTS, f"job {job.id} already has {job.relative_lens(target)}; pass overwrite to replace it")
        captured = self._before_replace(job, bool(args.get("discard", False)), "building a new lens")
        self.loaded = None
        self.backend.build(presc)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.backend.save_as(target)
        self.loaded = Loaded(job, target)
        rel = job.relative_lens(target)
        job.update_state(status="active", current_lens=rel, last_saved=rel)
        return {"job": job.id, "file": rel, "num_surfaces": len(self.backend.surfaces()), "captured": captured}

    def op_capture(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        if self.loaded is not None:
            self._require_loaded(job)
        target = job.resolve_lens(_arg(args, "file", str), must_exist=False)
        if target.exists():
            raise SaosError(ErrorCode.FILE_EXISTS, "a captured copy cannot overwrite an existing file")
        target.parent.mkdir(parents=True, exist_ok=True)
        original = self.backend.system_info()["file"]
        self.backend.save_as(target)
        self.loaded = Loaded(job, target)
        rel = job.relative_lens(target)
        self.window_unsaved = False
        job.update_state(current_lens=rel, last_saved=rel, captured_from=original)
        return {"file": rel, "captured_from": original}

    def op_analysis(self, args: dict[str, Any]) -> dict[str, Any]:
        """运行一个分析并导出数值：analyses/NNN-<类型>.json 与 OpticStudio 的文本导出 .txt。"""
        job = self._job(args)
        if args.get("list"):
            return {"types": self.backend.analysis_types()}
        loaded = self._require_loaded(job)
        name = _arg(args, "type", str)
        folder = job.root / "analyses"
        folder.mkdir(exist_ok=True)
        stem = f"{jobs.next_archive_number(folder):03d}-{name}"
        data = self.backend.analysis(name, _arg(args, "settings", dict, default={}), folder / f"{stem}.txt", bool(args.get("keep_open")))
        record = {"type": name, "lens": loaded.describe(), "time": jobs.now_iso(), **data}
        write_json(folder / f"{stem}.json", record)
        job.seal(*(p for p in (folder / f"{stem}.json", folder / f"{stem}.txt") if p.is_file()))
        if args.get("keep_open"):
            self.backend.tile_windows()
        rel = (folder / f"{stem}.json").relative_to(job.root).as_posix()
        # 返回值省略网格本体，只留统计量；完整数据在存档文件里
        brief = {**record, "grids": [{k: v for k, v in g.items() if k != "values"} for g in data["grids"]]}
        return {"file": rel, "text": f"analyses/{stem}.txt" if data.get("text_file") else None, **brief}

    def op_view(self, args: dict[str, Any]) -> dict[str, Any]:
        """显性调试：打开 Lens Data 或 System Viewer，以及一个像质分析，并按当前镜头刷新。"""
        loaded = self._require_loaded(self._job(args))
        shown = self.backend.show_windows(
            _arg(args, "viewer", str, default=None), _arg(args, "analysis", str, default=None), _arg(args, "settings", dict, default=None)
        )
        return {**loaded.describe(), **shown}

    def op_screenshot(self, args: dict[str, Any]) -> dict[str, Any]:
        """显性调试：截取连接窗口的 PNG，存进任务的 screens/。"""
        job = self._job(args)
        if self.loaded is not None:
            self._require_loaded(job)
        folder = job.root / "screens"
        folder.mkdir(exist_ok=True)
        label = _arg(args, "label", str, default="window")
        if not scripting.NAME_RE.match(label):
            raise SaosError(ErrorCode.BAD_ARGS, "label: letters, digits, - and _, 1–48 chars")
        prefix = f"{jobs.next_archive_number(folder):03d}-{label}"
        files = self.backend.screenshots(folder, prefix)
        job.seal(*(folder / name for name in files))
        return {"files": [f"screens/{name}" for name in files]}

    # ---- 优化与调整轮次 ----

    def op_vary(self, args: dict[str, Any]) -> dict[str, Any]:
        loaded = self._require_loaded(self._job(args))
        param = _arg(args, "param", str)
        surfaces = _arg(args, "surfaces", list)
        on = bool(args.get("on", True))
        solves = {int(s): self.backend.vary(int(s), param, on) for s in surfaces}
        loaded.dirty = True
        return {**loaded.describe(), "param": param, "solves": solves}

    def op_merit(self, args: dict[str, Any]) -> dict[str, Any]:
        loaded = self._require_loaded(self._job(args))
        action = _arg(args, "action", str)
        if action == "clear":
            self.backend.merit_clear()
        elif action == "wizard":
            self.backend.merit_wizard(_arg(args, "settings", dict, default={}))
        elif action == "add":
            self.backend.merit_add(_arg(args, "rows", list))
        elif action != "show":
            raise SaosError(ErrorCode.BAD_ARGS, f"unknown merit action {action}")
        if action != "show":
            loaded.dirty = True
        return {**loaded.describe(), "merit": self.backend.merit_value(), "rows": self.backend.merit_rows()}

    def op_optimize(self, args: dict[str, Any]) -> dict[str, Any]:
        loaded = self._require_loaded(self._job(args))
        result = self.backend.optimize(str(args.get("cycles", "auto")), args.get("cores"))
        loaded.dirty = True
        return {**loaded.describe(), **result}

    def op_exec(self, args: dict[str, Any]) -> dict[str, Any]:
        """执行 agent 编写的脚本（ADR-0007）。当前加载的是其他任务的镜头时拒绝。"""
        job = self._job(args)
        if self.loaded is not None:
            self._require_loaded(job)
        try:
            return scripting.execute(self, job, _arg(args, "name", str), _arg(args, "source", str), _arg(args, "args", dict, default={}))
        finally:
            # 脚本可能改动了镜头而没有声明；按 OpticStudio 的 NeedsSave 只升不降地标记。
            # 实测只读的 Zernike 拟合也会让 NeedsSave 置位，所以这样做偏保守
            if self.loaded is not None and self.backend.healthy() and self.backend.needs_save():
                self.loaded.dirty = True

    def op_check(self, args: dict[str, Any]) -> dict[str, Any]:
        """按需求框定对当前镜头逐项取值判定，不记录轮次。"""
        job = self._job(args)
        self._require_loaded(job)
        return workflow.check(scripting.ScriptContext(self, job, {}))

    def op_round(self, args: dict[str, Any]) -> dict[str, Any]:
        """记录一个调试轮次并给出终止判断（DESIGN.md「终止规则」）。"""
        job = self._job(args)
        self._require_loaded(job)
        ctx = scripting.ScriptContext(self, job, {})
        return workflow.record_round(ctx, _arg(args, "tier", str, default=None), str(args.get("note", "")))

    def op_operand(self, args: dict[str, Any]) -> dict[str, Any]:
        loaded = self._require_loaded(self._job(args))
        name = _arg(args, "type", str)
        int1 = _arg(args, "int1", int, default=0)
        int2 = _arg(args, "int2", int, default=0)
        data = _arg(args, "data", list, default=[])
        value = self.backend.operand(name, int1, int2, data)
        return {**loaded.describe(), "type": name.upper(), "int1": int1, "int2": int2, "data": data, "value": value}


# ---- 进程生命周期 ----


def _serve_one(daemon: Daemon, conn: Connection) -> None:
    if not conn.poll(REQUEST_READ_TIMEOUT_S):
        return
    try:
        request = decode(conn.recv_bytes())
        response = daemon.handle(request)
    except SaosError as exc:
        response = fail(exc)
    conn.send_bytes(encode(response))


def serve(mode: str, token: str = "") -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except AttributeError:
            pass
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ddir = config.daemon_dir()
    ddir.mkdir(parents=True, exist_ok=True)
    error_file = ddir / "start_error.json"
    error_file.unlink(missing_ok=True)

    backend = make_backend(mode)
    log.info(_t("后台进程 %s 启动，连接 OpticStudio（%s）", "daemon %s starting, connecting to OpticStudio (%s)"), os.getpid(), mode)
    try:
        zos_info = backend.start()
    except SaosError as exc:
        log.error(_t("启动失败：%s", "start failed: %s"), exc.message)
        write_json(error_file, exc.to_dict())
        return 1
    except Exception as exc:  # noqa: BLE001
        log.exception(_t("启动失败", "start failed"))
        write_json(error_file, SaosError(ErrorCode.DAEMON_START_FAILED, str(exc)).to_dict())
        return 1

    authkey = secrets.token_bytes(32)
    try:
        # AF_PIPE 以“首个实例”方式创建命名管道，同名管道已存在时会失败，防止重复启动
        listener = Listener(config.pipe_address(), family="AF_PIPE", authkey=authkey)
    except OSError as exc:
        backend.close()
        write_json(error_file, SaosError(ErrorCode.DAEMON_ALREADY_RUNNING, f"the named pipe is in use: {exc}").to_dict())
        return 1

    config.daemon_authkey_file().write_bytes(authkey)
    daemon = Daemon(backend, zos_info, mode)
    if mode == "extension":
        try:
            zos_info["windows"] = backend.show_windows()
        except Exception as exc:  # noqa: BLE001 - 窗口打不开不阻止连接，agent 可以再用 lens view
            log.warning(_t("打开显性调试窗口失败：%s", "opening the explicit debugging windows failed: %s"), exc)
    write_json(
        config.daemon_state_file(),
        {
            "pid": os.getpid(),
            "token": token,
            "address": config.pipe_address(),
            "mode": mode,
            "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "opticstudio": zos_info,
        },
    )
    log.info(_t("就绪：%s，OpticStudio %s", "ready: %s, OpticStudio %s"), config.pipe_address(), zos_info.get("version"))
    try:
        while not daemon.stopping:
            try:
                conn = listener.accept()
            except Exception as exc:  # noqa: BLE001 - 认证失败等只影响单个连接
                log.warning(_t("拒绝连接：%s", "connection refused: %s"), exc)
                continue
            try:
                _serve_one(daemon, conn)
            except Exception:  # noqa: BLE001
                log.exception(_t("处理请求时出错", "error while handling a request"))
            finally:
                conn.close()
    finally:
        log.info(_t("正在退出", "shutting down"))
        backend.close()
        listener.close()
        config.daemon_state_file().unlink(missing_ok=True)
        config.daemon_authkey_file().unlink(missing_ok=True)
        log.info(_t("已退出", "stopped"))
    return 0
