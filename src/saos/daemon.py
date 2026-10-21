"""常驻后台进程：持有 OpticStudio 连接，依次处理命令行发来的请求（ADR-0002）。

同一时间只加载一个任务的一个镜头文件。所有镜头操作都必须带上任务编号，
与当前加载的任务不一致时直接拒绝，保证不同设计任务的文件不会混用。
"""

from __future__ import annotations

import glob
import json
import logging
import math
import os
import secrets
import shutil
import sys
import time
import traceback
from dataclasses import dataclass
from datetime import datetime
from multiprocessing.connection import Connection, Listener
from pathlib import Path
from typing import Any, Callable

from saos import config, geometry, i18n, jobs, lens_files, parallel, prescription, scripting, stages, timing, workflow
from saos.backend import Backend, make_backend
from saos.backend.base import same_number
from saos.protocol import ErrorCode, SaosError, decode, encode, fail, ok, write_json

log = logging.getLogger("saos.daemon")


def _t(zh: str, en: str) -> str:
    """日志给人看，按 SAOS_LANG 或系统界面语言选中英文。"""
    return i18n.text(i18n.resolve(), zh, en)


REQUEST_READ_TIMEOUT_S = 30
# 显性调试：这些操作可能改变镜头，结束后刷新已打开的分析窗口
# 可能跑很久的操作：开始和结束都写进 progress.json，开始前清掉旧的取消请求
# operations that may run long: their start and end go to progress.json; a stale cancel request is cleared first
LONG_OPS = frozenset({"opt.local", "opt.hammer", "opt.global", "opt.glass", "script.exec", "round.record", "demand.check", "scan.run", "zos.begin"})
REFRESH_AFTER = frozenset({"lens.load", "lens.set", "lens.build", "opt.local", "opt.hammer", "opt.global", "opt.glass", "script.exec", "round.record", "zos.begin"})
# 优化运行中多久给流程图窗口推一次当前镜头（只有几何，不带光线）/ how often a running optimisation updates the live view
VIEW_DURING_OPT_S = 5.0
# Hammer、全局搜索运行中在镜头文件旁写出的系统（文件名模式）/ systems Hammer and global search write next to the lens file
SEARCH_FILES = {"hammer": "{stem}_*_HAMMER.zmx", "global": "GLOPT_*.zmx"}
# 距上次记轮次累计到这么多次优化（或脚本）时，结果里提醒记一轮：执行记录很多而账本几乎空着，停止判断就没有依据
# (nightvision 任务 59 个脚本后账本只有第 0 轮，2026-10-19)
# remind to record a round once this many optimisations (or scripts) ran since the last one: a long run history with an
# almost empty ledger leaves the stop rule without evidence (nightvision: one round after 59 scripts, 2026-10-19)
ROUND_REMINDER = {"opt": 2, "exec": 15}
RUN_KINDS = {"opt.local": "opt", "opt.hammer": "opt", "opt.global": "opt", "opt.glass": "opt", "script.exec": "exec", "scan.run": "scan"}
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
        # 认可前的任务只用 Optiland；OpticStudio 会话只在 zos begin 认可后启动（见 _zos_session）
        self.optiland = backend
        self.zos: Backend | None = None
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
        # ctx.map / saos scan 的常驻 worker 池，第一次用时启动 / the worker pool of ctx.map and saos scan, started on first use
        self.pool = parallel.Pool()
        # 分离运行的扫描线程，键为任务内的 scans/NNN / threads of detached scans, keyed by scans/NNN in the job
        self.background: dict[str, Any] = {}
        self.view_seq = 0
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
            "lens.export": self.op_export,
            "lens.operand": self.op_operand,
            "lens.build": self.op_build,
            "lens.view": self.op_view,
            "lens.analysis": self.op_analysis,
            "lens.screenshot": self.op_screenshot,
            "lens.capture": self.op_capture,
            "opt.vary": self.op_vary,
            "opt.merit": self.op_merit,
            "opt.local": self.op_optimize,
            "opt.hammer": lambda args: self.op_search({**args, "kind": "hammer"}),
            "opt.global": lambda args: self.op_search({**args, "kind": "global"}),
            "opt.glass": self.op_glass,
            "script.exec": self.op_exec,
            "demand.check": self.op_check,
            "scan.run": self.op_scan,
            "round.record": self.op_round,
            "zos.begin": self.op_zos_begin,
        }

    # ---- 请求分发 ----

    def handle(self, request: dict[str, Any]) -> dict[str, Any]:
        op = request.get("op")
        args = request.get("args") or {}
        started = time.perf_counter()
        phases_before = timing.snapshot()
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
            # 连接手动打开的窗口时，长操作期间默认暂停界面刷新，结束后窗口显示最终状态（实测刷新会让一次验收评估从 22 s 变成 98 s）；
            # 请求带 live 或 SAOS_UI_LIVE=1 时全程刷新
            # with a connected window, long operations pause UI updates by default and the window shows the final state afterwards
            # (live updates once slowed an acceptance evaluation from 22 s to 98 s); live or SAOS_UI_LIVE=1 keeps them
            live = bool(args.get("live")) or os.environ.get("SAOS_UI_LIVE") == "1"
            quiet = op in LONG_OPS and not live
            self._progress_start(op, args)
            with self.backend.activity(f"saos {op}", quiet):
                result = handler(args)
            if op in REFRESH_AFTER or op == "lens.describe":
                self._refresh_windows(args)
            if op in REFRESH_AFTER:
                self.export_view()
        except SaosError as exc:
            error = exc
        except Exception as exc:  # noqa: BLE001 - 后台进程不能因单个请求崩溃
            log.exception(_t("操作 %s 出现未预期异常", "unexpected error in op %s"), op)
            error = self._unexpected(exc)
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        if op in LONG_OPS:
            self._progress_end(args, error, duration_ms)
        self._log_to_job(op, args, error, duration_ms, timing.delta(phases_before, timing.snapshot()))
        if error is None and op in RUN_KINDS and isinstance(result, dict):
            reminder = self._since_round(args)
            if reminder:
                result["since_round"] = reminder
        return fail(error) if error else ok(result)

    def _since_round(self, args: dict[str, Any]) -> dict[str, Any] | None:
        """设计阶段（需求已确认、有获确认的建模说明）里，上次成功记轮次之后跑了多少次优化、脚本和扫描；到 ROUND_REMINDER 时附一句提醒。
        In the design phase (demand confirmed, a model approved), how many optimisations, scripts and scans ran since the
        last recorded round; a note is added once ROUND_REMINDER is reached."""
        from saos import demand, model_gate

        try:
            job = jobs.get_job(str(args.get("job")))
            if not demand.confirmations(job.root) or not any((job.root / "models").glob(f"*/{model_gate.APPROVAL_FILE}")):
                return None
            lines = job.log_file.read_text(encoding="utf-8").splitlines() if job.log_file.is_file() else []
        except (SaosError, OSError):
            return None
        counts = {"opt": 0, "exec": 0, "scan": 0}
        rounds = False
        for line in reversed(lines):
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("op") == "round.record" and row.get("ok"):
                rounds = True
                break
            kind = RUN_KINDS.get(row.get("op"))
            if kind and row.get("ok"):
                counts[kind] += 1
        out: dict[str, Any] = {**counts, "round_recorded": rounds}
        if counts["opt"] >= ROUND_REMINDER["opt"] or counts["exec"] >= ROUND_REMINDER["exec"]:
            since = "since the last round" if rounds else "and no round is recorded yet"
            out["note"] = (f"{counts['opt']} optimisation runs, {counts['exec']} scripts and {counts['scan']} scans {since}: once the lens is "
                           "worth judging, record it with saos round so that the ledger and the stop rule see the progress")
        return out

    def _progress_start(self, op: str, args: dict[str, Any]) -> jobs.Job | None:
        if op not in LONG_OPS or not isinstance(args.get("job"), str):
            return None
        try:
            job = jobs.get_job(args["job"])
        except SaosError:
            return None
        job.clear_cancel()
        detail = args.get("name") if op == "script.exec" else args.get("tier") if op == "round.record" else None
        (job.root / jobs.PROGRESS_FILE).unlink(missing_ok=True)
        job.write_progress(op=op, detail=detail, status="running", started=jobs.now_iso(), daemon_pid=os.getpid())
        return job

    def _progress_end(self, args: dict[str, Any], error: SaosError | None, duration_ms: float) -> None:
        try:
            job = jobs.get_job(str(args.get("job")))
        except SaosError:
            return
        if job.read_progress() is None:
            return
        status = "cancelled" if error is not None and error.code == ErrorCode.CANCELLED else "failed" if error else "done"
        job.write_progress(status=status, duration_s=round(duration_ms / 1000, 1), error=error.code if error else None)
        job.clear_cancel()

    def _window_edited(self) -> bool:
        # 窗口是 OpticStudio 会话；它的未保存内容与当前后端无关。部分面参数无法完整读取，宁可要求保存也不误判为安全。
        # The window is the OpticStudio session, whatever backend is current. Parameters that cannot be read in full make
        # the window count as edited: better to ask for a copy than to judge it safe.
        return self.window_unsaved or (self.zos is not None and self.zos.has_unsaved_changes())

    def _modeling(self, args: dict[str, Any]) -> bool:
        """建模阶段：需求已经确认过、还没有任何获确认的建模说明。
        The modelling phase: the demand was confirmed and no model description is approved yet."""
        from saos import demand, model_gate

        job_id = args.get("job")
        if not isinstance(job_id, str):
            return False
        try:
            root = jobs.get_job(job_id).root
        except SaosError:
            return False
        return bool(demand.confirmations(root)) and not any((root / "models").glob(f"*/{model_gate.APPROVAL_FILE}"))

    def export_view(self, *, rays: bool = True) -> dict[str, Any] | None:
        """把当前镜头的布局、Lens Data 和交叠检查写进任务的 view.json，流程图窗口据此显示；失败只记在文件里，不影响操作。
        Write the current lens's layout, Lens Data and interference check to the job's view.json for the workflow window;
        a failure is noted in the file and never fails the operation."""
        loaded = self.loaded
        if loaded is None or os.environ.get("SAOS_VIEW") == "0":
            return None
        started = time.perf_counter()
        try:
            data: dict[str, Any] = {"ok": True, **self.backend.viz_data(None, full=False, rays=rays)}
        except Exception as exc:  # noqa: BLE001 - 视图只是给人看的 / the view is for people only
            log.exception(_t("导出当前视图失败", "exporting the live view failed"))
            data = {"ok": False, "error": f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"[:500]}
        described_at = time.perf_counter()
        try:
            data["model"] = self.backend.describe_model(None)
        except Exception as exc:
            data["model_error"] = f"{type(exc).__name__}: {exc}"[:500]
        data["timing"] = {"viz_s": round(described_at - started, 6),
                          "model_s": round(time.perf_counter() - described_at, 6)}
        self.view_seq += 1
        data.update(lens=loaded.job.relative_lens(loaded.path), dirty=loaded.dirty, updated=jobs.now_iso(), seq=self.view_seq,
                    elapsed_s=round(time.perf_counter() - started, 3))
        write_json(loaded.job.root / jobs.VIEW_FILE, data)
        clash = data.get("interference") or {}
        return {"elapsed_s": data["elapsed_s"], "timing": data["timing"], "ok": data["ok"],
                "interference": {k: clash.get(k) for k in ("crossings", "overlaps")} if clash else None}

    def _refresh_windows(self, args: dict[str, Any]) -> None:
        """改动镜头后刷新窗口；建模阶段还要保证 Lens Data 和布局窗口开着，被关掉的重新打开。"""
        if self.mode != "extension":
            return
        try:
            if self._modeling(args):
                self.backend.show_windows()
            else:
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

    def _log_to_job(self, op: Any, args: dict[str, Any], error: SaosError | None, duration_ms: float,
                    phases: dict[str, dict[str, float]] | None = None) -> None:
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
        if phases:
            # 包含式分项，见 saos.timing：分项之间会重叠，不能相加
            record["phases"] = phases
        if error is not None:
            record["error"] = {"code": error.code, "message": error.message}
        try:
            job.append_log(record)
        except OSError:
            log.exception(_t("写入任务 %s 的执行记录失败", "writing the run record of job %s failed"), job_id)

    # ---- 辅助 ----

    def _job(self, args: dict[str, Any]) -> jobs.Job:
        return jobs.get_job(_arg(args, "job", str))

    def _select_backend(self, job: jobs.Job) -> None:
        """已认可的任务用 OpticStudio 会话，其余用 Optiland。会话刚连上时，窗口里的未保存内容先存副本，再允许换掉。
        A job past acceptance uses the OpticStudio session, others Optiland. A freshly connected window keeps its unsaved
        content as a copy before anything replaces it."""
        if stages.backend(job.root) == "zos":
            self._zos_session()
            self.capture_window(job)
            self.backend = self.zos
        else:
            self.backend = self.optiland

    def _zos_session(self) -> Backend:
        """OpticStudio 会话，没有就启动。只能在认可之后调用。"""
        if self.zos is None:
            from saos.backend.zos import ZosBackend

            zos = ZosBackend(mode=self.mode)
            try:
                info = zos.start()
            except BaseException:
                zos.close()
                raise
            self.zos = zos
            # 用户窗口里已有未保存内容时，换掉窗口内容前要先保护（ADR-0011）
            self.window_unsaved = self.window_unsaved or bool(info.get("initial_needs_save"))
        return self.zos

    def _optiland_only(self, name: str, value: Any) -> None:
        """--device、--method 只有 Optiland 认识；OpticStudio 会话里给了就拒绝，不静默忽略。"""
        if value is not None and self.backend is self.zos:
            raise SaosError(ErrorCode.BAD_ARGS, f"--{name} applies to Optiland only; the accepted OpticStudio stage does not take it")

    def enter_zos(self, job: jobs.Job, path: Path) -> tuple[Backend, str | None]:
        """zos begin 认可后调用：连接 OpticStudio → 先保存窗口里的未保存内容 → 加载入口镜头 → 之后这个任务用它。
        保存副本失败时加载不会发生。返回会话和副本路径（窗口没有未保存内容时为 None）。
        Called after acceptance: connect OpticStudio, keep the window's unsaved content as a copy, then load the entry lens.
        When the copy fails nothing is loaded. Returns the session and the copy's path (None when nothing was unsaved)."""
        zos = self._zos_session()
        captured = self.capture_window(job)
        zos.load(path)
        self.backend = zos
        return zos, captured

    def close(self) -> None:
        if self.zos is not None:
            self.zos.close()
        self.optiland.close()

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
        """窗口的未保存标志无法区分用户修改和分析窗口的副作用，置位时一律把窗口内容存成任务内的副本，返回副本路径。
        保存由窗口所属的 OpticStudio 会话完成；失败时异常向上抛出，调用方不得继续替换窗口内容。"""
        if self.zos is None or not self._window_edited():
            return None
        folder = job.lenses_dir / "captures"
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{jobs.next_archive_number(folder):03d}-unsaved{jobs.LENS_SUFFIX}"
        self.zos.save_as(target)
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
            "pool": self.pool.status(),
            "background": self.running_background(),
            "home": str(config.saos_home()),
        }

    def op_shutdown(self, args: dict[str, Any]) -> dict[str, Any]:
        running = self.running_background()
        if running:
            raise SaosError(ErrorCode.BUSY, f"detached scans are still running ({', '.join(running)}); cancel them with saos job cancel "
                                            "or wait for them (saos scan --show), then stop the daemon", {"background": running})
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
        if lens_files.exists(dest) and not args.get("overwrite", False):
            raise SaosError(ErrorCode.FILE_EXISTS, f"job {job.id} already has {name}; pass overwrite to replace it")
        dest.parent.mkdir(parents=True, exist_ok=True)
        from saos.lens_files import copy_lens

        copy_lens(src, dest)
        return {"job": job.id, "file": job.relative_lens(dest), "source": str(src)}

    def op_load(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        path = job.resolve_lens(_arg(args, "file", str), must_exist=True)
        captured = self._before_replace(job, bool(args.get("discard", False)), "loading another lens")
        from saos.lens_files import read_companion

        # 镜头与伴随文件不配对时在动当前镜头之前报错，当前镜头保持加载 / refuse a mismatched pair before the current lens is touched
        read_companion(path)
        self._select_backend(job)
        self.loaded = None
        self.backend.load(path)
        self.loaded = Loaded(job, path)
        rel = job.relative_lens(path)
        job.update_state(status="active", current_lens=rel)
        return {"job": job.id, "file": rel, "num_surfaces": len(self.backend.surfaces()), **self._object_count(), "captured": captured}

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
        objects = self.backend.objects()
        return {**loaded.describe(), "surfaces": self.backend.surfaces(), **({"objects": objects} if objects else {})}

    def _object_count(self) -> dict[str, Any]:
        # 非序列系统没有面，报物体数 / a non-sequential system has objects instead of surfaces
        info = self.backend.system_info()
        return {"num_objects": len(info["objects"])} if info.get("mode") == "non_sequential" else {}

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
        try:
            desc["interference"] = self.backend.interference()
        except SaosError as exc:
            desc["interference"] = {"error": exc.message}
        lens = loaded.path.relative_to(job.lenses_dir).as_posix()
        # 确认页的“需求逐项”用需求单现有的判定入口测当前镜头（不存证据）/ the requirements section measures the lens with the demand's own checks (no evidence kept)
        measured = model_report.measure_requirements(scripting.ScriptContext(self, job, {}))
        result = model_report.write(job, desc, folder, notes, lens, _arg(args, "lang", str, default=None), measured=measured)
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

        return model_gate.approve(self._job(args), _arg(args, "model", str), _arg(args, "quote", str), args.get("rebuild_reason") or "")

    def op_set(self, args: dict[str, Any]) -> dict[str, Any]:
        loaded = self._require_loaded(self._job(args))
        surface = _arg(args, "surface", int)
        param = _arg(args, "param", str)
        if "value" not in args:
            raise SaosError(ErrorCode.BAD_ARGS, "missing argument value")
        current = self.backend.surfaces()
        before = self.backend.surface_value(surface, param) if 0 <= surface < len(current) else None
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
        if target != loaded.path and lens_files.exists(target) and not args.get("overwrite", False):
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

    def op_export(self, args: dict[str, Any]) -> dict[str, Any]:
        """显式导出 .zmx，与原生快照成对；lens save 不会自动导出。表达不了时返回具体缺口，快照不变。
        Explicit .zmx export, paired with the native snapshot; lens save never exports. When the writer cannot express the
        model the gap is reported and the snapshot is left unchanged."""
        job = self._job(args)
        loaded = self._require_loaded(job)
        export = getattr(self.backend, "export_zmx", None)
        if export is None:
            raise SaosError(ErrorCode.BAD_ARGS, "the loaded lens is already an OpticStudio .zmx; there is nothing to export")
        target = job.resolve_lens(_arg(args, "file", str), must_exist=False)
        if job.is_archive_lens(target):
            raise SaosError(ErrorCode.BAD_ARGS, "lenses/rounds/ and lenses/captures/ hold evidence written by the tools; export under another name")
        if target == loaded.path:
            raise SaosError(ErrorCode.BAD_ARGS, "export writes a new file; name a file other than the loaded lens")
        if lens_files.exists(target) and not args.get("overwrite", False):
            raise SaosError(ErrorCode.FILE_EXISTS, f"job {job.id} already has {job.relative_lens(target)}; pass overwrite to replace it")
        export(target)
        return {"job": job.id, "file": job.relative_lens(target), "snapshot": lens_files.companion(target).name, "exported": True}

    def op_build(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        presc = prescription.normalize(_arg(args, "prescription", dict))
        target = job.resolve_lens(_arg(args, "file", str), must_exist=False)
        if job.is_archive_lens(target):
            raise SaosError(ErrorCode.BAD_ARGS, "lenses/rounds/ and lenses/captures/ hold evidence written by the tools; save under another name")
        if lens_files.exists(target) and not args.get("overwrite", False):
            raise SaosError(ErrorCode.FILE_EXISTS, f"job {job.id} already has {job.relative_lens(target)}; pass overwrite to replace it")
        captured = self._before_replace(job, bool(args.get("discard", False)), "building a new lens")
        self._select_backend(job)
        self.loaded = None
        self.backend.build(presc)
        target.parent.mkdir(parents=True, exist_ok=True)
        self.backend.save_as(target)
        self.loaded = Loaded(job, target)
        rel = job.relative_lens(target)
        job.update_state(status="active", current_lens=rel, last_saved=rel)
        return {"job": job.id, "file": rel, "num_surfaces": len(self.backend.surfaces()), **self._object_count(), "captured": captured}

    def op_capture(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        if self.loaded is not None:
            self._require_loaded(job)
        target = job.resolve_lens(_arg(args, "file", str), must_exist=False)
        if lens_files.exists(target):
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
        rel = (folder / f"{stem}.json").relative_to(job.root).as_posix()
        # 返回值省略网格本体，只留统计量；完整数据在存档文件里
        brief = {**record, "grids": [{k: v for k, v in g.items() if k != "values"} for g in data["grids"]]}
        return {"file": rel, "text": f"analyses/{stem}.txt" if data.get("text_file") else None, **brief}

    def op_view(self, args: dict[str, Any]) -> dict[str, Any]:
        """显性调试：打开 Lens Data 和布局窗口，按需再开一个分析，并按当前镜头刷新。"""
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

    # ---- 优化与调试轮次 ----

    def op_vary(self, args: dict[str, Any]) -> dict[str, Any]:
        loaded = self._require_loaded(self._job(args))
        if args.get("config_rows") is not None:
            return self._vary_config(loaded, args)
        if args.get("objects") is not None:
            return self._vary_objects(loaded, args)
        param = _arg(args, "param", str)
        surfaces = _arg(args, "surfaces", list)
        # 面号先校验成整数：否则 int() 的异常出在循环里，会被当成改动途中的错误而把镜头标为未保存
        # surface numbers are checked up front; an int() failure inside the loop would mark the lens unsaved
        if any(isinstance(s, bool) or not isinstance(s, int) for s in surfaces):
            raise SaosError(ErrorCode.BAD_ARGS, f"surfaces must be integers, got {surfaces!r}")
        on = bool(args.get("on", True))
        mode = _arg(args, "mode", str, default=None)
        catalog = _arg(args, "catalog", str, default=None)
        substitute = param == "material" and on and mode == "substitute"
        extra: dict[str, Any] = {}
        if substitute:
            catalog = self.backend.substitute_catalog(catalog)
            # 现在的玻璃不在所选目录里的面：替换会把它换成别家的玻璃，提醒 agent 必要时按面给 --catalog
            # surfaces whose glass is not in the chosen catalog: substitution moves them to another maker's glasses
            rows = self.backend.surfaces()
            glass = {int(s): str(rows[int(s)].get("material") or "") for s in surfaces if 0 <= int(s) < len(rows)}
            outside = [{"surface": s, "material": m} for s, m in glass.items() if m and self.backend.catalog_has(m, catalog) is False]
            extra = {"catalog": catalog or "all catalogs in use", "outside_catalog": outside}
            if outside:
                extra["note"] = (f"the current glass of surfaces {[o['surface'] for o in outside]} is not in {catalog}; substitution will "
                                 "replace it with glasses of that catalog. Pass --catalog per surface to stay with a maker")
        minimum = _arg(args, "min", (int, float), default=None)
        maximum = _arg(args, "max", (int, float), default=None)
        bounded = minimum is not None or maximum is not None
        if bounded:
            # 边界只有 Optiland 的优化器用；OpticStudio 没有逐变量边界，要用评价函数里的边界操作数
            # bounds are used by the Optiland optimiser only; OpticStudio has no per-variable bounds
            if not on or param == "material":
                raise SaosError(ErrorCode.BAD_ARGS, "--min / --max go with a numeric variable, not with --off or --param material")
            if getattr(self.backend, "set_bounds", None) is None:
                raise SaosError(ErrorCode.BAD_ARGS, "OpticStudio has no per-variable bounds; constrain the value with boundary operands "
                                                    "in the merit function instead (saos opt merit add, e.g. CTGT / CTLT / ETGT)")
        solves: dict[int, str] = {}
        try:
            for s in surfaces:
                solves[int(s)] = self.backend.vary(int(s), param, on, mode, catalog)
            if bounded:
                extra["bounds"] = {int(s): self.backend.set_bounds(int(s), param, minimum, maximum) for s in surfaces}
        except Exception as exc:
            # BAD_ARGS 是改动前的校验；其他错误可能出在加目录或设 solve 之后，保守地当作镜头已改
            # BAD_ARGS comes from checks before any change; any other error may follow an added catalog or a set solve
            if not (isinstance(exc, SaosError) and exc.code == ErrorCode.BAD_ARGS):
                loaded.dirty = True
            raise
        finally:
            # 中途某面失败时，前面的面（和替换目录）已经改了 / when a later surface fails, the earlier ones are already changed
            if solves:
                loaded.dirty = True
        return {**loaded.describe(), "param": param, "solves": solves, **extra}

    def _vary_objects(self, loaded, args: dict[str, Any]) -> dict[str, Any]:
        """非序列物体的参数设为变量或固定。/ Parameters of non-sequential objects variable or fixed."""
        objects = _arg(args, "objects", list)
        param = _arg(args, "param", str)
        group = _arg(args, "group", int, default=None)
        if any(isinstance(x, bool) or not isinstance(x, int) for x in objects):
            raise SaosError(ErrorCode.BAD_ARGS, f"objects must be integers, got {objects!r}")
        solves: dict[int, str] = {}
        try:
            for n in objects:
                solves[n] = self.backend.vary_object(group, n, param, bool(args.get("on", True)))
        finally:
            if solves:
                loaded.dirty = True
        return {**loaded.describe(), "param": param, "object_solves": solves}

    def _vary_config(self, loaded, args: dict[str, Any]) -> dict[str, Any]:
        """多重结构的格设为变量或固定（变焦间隔、调焦量等）。/ Multi-configuration cells variable or fixed (zoom spacings, focus, …)."""
        rows = _arg(args, "config_rows", list)
        configs = _arg(args, "configs", list, default=None)
        if any(isinstance(x, bool) or not isinstance(x, int) for x in rows + (configs or [])):
            raise SaosError(ErrorCode.BAD_ARGS, "config_rows and configs must be integers")
        if args.get("param") is not None or args.get("surfaces"):
            raise SaosError(ErrorCode.BAD_ARGS, "give either --param with --surfaces or --config-rows, not both")
        try:
            solves = self.backend.vary_config(rows, configs, bool(args.get("on", True)))
        except SaosError as exc:
            if exc.code != ErrorCode.BAD_ARGS:
                loaded.dirty = True
            raise
        except Exception:
            loaded.dirty = True
            raise
        loaded.dirty = True
        return {**loaded.describe(), "config_solves": solves}

    def op_merit(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        loaded = self._require_loaded(job)
        action = _arg(args, "action", str)
        if action == "clear":
            self.backend.merit_clear()
        elif action == "wizard":
            self.backend.merit_wizard(_arg(args, "settings", dict, default={}))
        elif action == "add":
            self.backend.merit_add(_arg(args, "rows", list))
        elif action == "script":
            setter = getattr(self.backend, "set_merit_script", None)
            if setter is None:
                raise SaosError(ErrorCode.BAD_ARGS, "a merit loss script runs on the Optiland backend only; the lens is open in OpticStudio")
            source = _arg(args, "source", str)
            namespace: dict[str, Any] = {"__name__": "saos_merit_script_check"}
            try:
                exec(compile(source, "<merit script>", "exec"), namespace)
            except Exception as exc:
                raise SaosError(ErrorCode.SCRIPT_ERROR, f"the merit script does not load: {type(exc).__name__}: {exc}") from exc
            if not callable(namespace.get("loss")):
                raise SaosError(ErrorCode.BAD_ARGS, "the merit script must define loss(ctx)")
            copy = scripting.next_copy(job, _arg(args, "name", str))
            copy.write_text(source, encoding="utf-8")
            setter(source, copy.relative_to(job.root).as_posix(), _arg(args, "weight", (int, float), default=1.0))
        elif action != "show":
            raise SaosError(ErrorCode.BAD_ARGS, f"unknown merit action {action}")
        if action != "show":
            loaded.dirty = True
        kw = self._merit_script_kw(job)
        return {**loaded.describe(), "merit": self.backend.merit_value(**kw), "rows": self.backend.merit_rows(**kw)}

    def _merit_script_kw(self, job: jobs.Job) -> dict[str, Any]:
        """评价函数里有损失脚本时，编译出调用它的函数（每次用同一个 ctx），以 {"script": fn} 交给后端；没有时为空。
        With a loss script in the merit function, compile it into a callable (one ctx for every call) passed as script."""
        get = getattr(self.backend, "merit_script", None)
        stored = get() if get is not None else None
        if not stored:
            return {}
        path = job.root / stored["name"]
        namespace: dict[str, Any] = {"__name__": "saos_merit_script", "__file__": str(path)}
        try:
            exec(compile(stored["source"], str(path), "exec"), namespace)
        except Exception as exc:
            raise SaosError(ErrorCode.SCRIPT_ERROR, f"merit script {stored['name']} does not compile: {type(exc).__name__}: {exc}") from exc
        loss = namespace.get("loss")
        if not callable(loss):
            raise SaosError(ErrorCode.BAD_ARGS, f"merit script {stored['name']} must define loss(ctx)")
        ctx = scripting.ScriptContext(self, job, {}, script=path)

        def run() -> Any:
            try:
                return loss(ctx)
            except SaosError:
                raise
            except Exception as exc:
                raise SaosError(ErrorCode.SCRIPT_ERROR, f"merit script {stored['name']} raised {type(exc).__name__}: {exc}",
                                {"traceback": traceback.format_exc()[-4000:]}) from exc

        return {"script": run}

    def _optimizer_hooks(self, job: jobs.Job, stage: str) -> Callable[[dict[str, Any]], None]:
        last_view = [time.monotonic()]

        def on_progress(data: dict[str, Any]) -> None:
            job.write_progress(stage=stage, **data)
            # 运行中只读得到几何：优化器占着工具，追迹不了光线；实测读几何不改变优化结果（Double Gauss，2026-10-17）
            # only geometry can be read mid-run: the optimiser holds the tool; reading it left the result unchanged
            if time.monotonic() - last_view[0] >= VIEW_DURING_OPT_S:
                self.export_view(rays=False)
                last_view[0] = time.monotonic()

        return on_progress

    def _measure_ids(self, job: jobs.Job, args: dict[str, Any]) -> list[str] | None:
        """--measure：hard 为常量和变量范围，all 为所有进 G 的项，否则逗号分隔的判定 id；没给时 None。
        --measure: hard means the constants and variable ranges, all every item entering G, else comma-separated ids."""
        from saos import demand

        spec = _arg(args, "measure", str, default=None)
        if not spec:
            return None
        specs = demand.checked(demand.load_job(job.root))
        if spec == "hard":
            return [s["id"] for s in specs if s["kind"] in demand.HARD_KINDS]
        if spec == "all":
            return [s["id"] for s in specs if s["goal"] is not None or "direction" in s]
        ids = list(dict.fromkeys(x.strip() for x in spec.split(",") if x.strip()))
        unknown = [i for i in ids if i not in {s["id"] for s in specs}]
        if unknown or not ids:
            raise SaosError(ErrorCode.BAD_ARGS, f"--measure: unknown check ids {unknown}" if unknown else "--measure lists no check id",
                            {"known": sorted(s["id"] for s in specs)})
        return ids

    def _measure_before(self, job: jobs.Job, ids: list[str] | None) -> dict[str, Any] | None:
        from saos import demand

        if ids is None:
            return None
        job.write_progress(stage="measure before")
        return demand.quick(scripting.ScriptContext(self, job, {}), ids)

    def _review(self, job: jobs.Job, ids: list[str] | None, before: dict[str, Any] | None) -> dict[str, Any]:
        """优化之后复核：交叠检查（零件相交、穿插、最薄玻璃和最小空气间隔），给了 measure 时正式判定的前后对比。
        merit 变小不等于设计变好（nightvision 出现过 merit 下降而轴外像质恶化的候选），所以把变差的项和无效状态直接列出来。
        Review after an optimisation: the interference check (crossing or interpenetrating parts, thinnest glass, smallest
        air gap) and, with measure, the demand's own checks before and after. A smaller merit is no proof of a better design
        (nightvision had a candidate whose merit fell while off-axis image quality got worse), so worse items and invalid
        states are listed outright."""
        from saos import demand

        out: dict[str, Any] = {}
        warnings: list[str] = []
        valid = True
        try:
            clash = self.backend.interference()
            out["interference"] = geometry.summary(clash)
            if clash.get("crossings") or clash.get("overlaps"):
                valid = False
                warnings.append("parts cross or interpenetrate: the editor holds a physically invalid system")
        except Exception as exc:  # noqa: BLE001 - 复核出错不能吞掉已经完成的优化结果 / a failing review must not lose a finished run
            log.exception("interference check after the optimisation failed")
            out["interference"] = {"error": f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"[:500]}
            # 没检查完不能说没问题 / an unfinished check is no clean bill
            valid = False
            warnings.append("the interference check after the run failed; check the lens before you trust it")
        if ids is not None and before is not None:
            job.write_progress(stage="measure after")
            try:
                after = demand.quick(scripting.ScriptContext(self, job, {}), ids)
            except Exception as exc:  # noqa: BLE001 - 同上 / as above
                log.exception("measuring after the optimisation failed")
                out["measured"] = {"error": f"{type(exc).__name__}: {getattr(exc, 'message', exc)}"[:500]}
                out["valid"] = False
                out["warnings"] = [*warnings, "measuring the demand's checks after the run failed; the result is not judged"]
                return out

            def score(r: dict[str, Any], i: str) -> float:
                return float(r["gaps"].get(i, 0.0)) + float(r["pursuit"].get(i, 0.0))

            items = {i: {"before": before["values"].get(i), "after": after["values"].get(i),
                         "gap_before": score(before, i), "gap_after": score(after, i)} for i in ids}
            tol = 1e-9
            worse = [i for i, v in items.items() if v["gap_after"] > v["gap_before"] + tol * max(1.0, abs(v["gap_before"]))]
            better = [i for i, v in items.items() if v["gap_after"] < v["gap_before"] - tol * max(1.0, abs(v["gap_before"]))]
            out["measured"] = {"items": items, "shortfall": {"before": before["shortfall"], "after": after["shortfall"]},
                               "better": better, "worse": worse, "hard_failing": after["hard_failing"], "invalid": after["invalid"],
                               "errors": after["errors"]}
            if worse:
                warnings.append(f"the demand's checks got worse on {', '.join(worse)} whatever the merit function did")
            if after["hard_failing"] or after["invalid"] or after["errors"]:
                valid = False
                warnings.append("after the run, hard limits fail or checks give no valid value: "
                                f"{', '.join(after['hard_failing'] + after['invalid'] + sorted(after['errors']))}")
        out["valid"] = valid
        out["warnings"] = warnings
        return out

    def op_optimize(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        stages.require_ready(job.root)
        loaded = self._require_loaded(job)
        # 优化可能跑很久：需求未确认或有 blocking 问题时先问用户，不进入优化
        # an optimisation may run for a long time: with an unconfirmed demand or a blocking question, ask the user first
        workflow.require_optimise(job, self.backend)
        ids = self._measure_ids(job, args)
        before = self._measure_before(job, ids)
        kw = self._merit_script_kw(job)
        device = _arg(args, "device", str, default=None)
        self._optiland_only("device", device)
        if device is not None:
            kw["device"] = device
        try:
            result = self.backend.optimize(str(args.get("cycles", "auto")), args.get("cores") or config.compute_cores(),
                                           max_seconds=_arg(args, "max_seconds", (int, float), default=None),
                                           algorithm=_arg(args, "algorithm", str, default=None),
                                           on_progress=self._optimizer_hooks(job, "opt.local"), should_cancel=job.cancel_requested, **kw)
        finally:
            # 出错时编辑器里也是优化器走到的系统，不能当作未改动 / after an error the editor still holds what the optimiser reached
            loaded.dirty = True
        if result.get("stopped_by"):
            job.write_progress(stopped_by=result["stopped_by"])
        return {**loaded.describe(), **result, "review": self._review(job, ids, before)}

    def op_search(self, args: dict[str, Any]) -> dict[str, Any]:
        """Hammer 或全局搜索。两者都不会自己结束，必须给 max_seconds。OpticStudio 运行中把找到的系统写在镜头文件旁边，
        结束后移到 lenses/<kind>/NNN/，不留在工作镜头的目录里。
        Hammer or global search. Neither ends on its own, so max_seconds is required. OpticStudio writes the systems it
        finds next to the lens file while running; they are moved to lenses/<kind>/NNN/ afterwards."""
        job = self._job(args)
        stages.require_ready(job.root)
        loaded = self._require_loaded(job)
        kind = _arg(args, "kind", str)
        if kind not in SEARCH_FILES:
            raise SaosError(ErrorCode.BAD_ARGS, f"kind must be one of {list(SEARCH_FILES)}")
        max_seconds = _arg(args, "max_seconds", (int, float))
        if max_seconds <= 0:
            raise SaosError(ErrorCode.BAD_ARGS, "max_seconds must be positive")
        workflow.require_optimise(job, self.backend)
        lens_dir = loaded.path.parent
        pattern = SEARCH_FILES[kind].format(stem=glob.escape(loaded.path.stem))
        before = self._search_stamps(lens_dir, pattern)
        # GLOPT 的编号跟文件夹里已有的文件无关（实测每次运行前文件夹为空，编号仍是 0000、0001、0002），同名直接覆盖：
        # 镜头取自 lenses/global/NNN/ 时会冲掉上一次的结果，甚至当前镜头本身
        # GLOPT numbering ignores the files already there and overwrites same-named ones, e.g. a lens loaded from lenses/global/NNN/
        if kind == "global" and before:
            raise SaosError(ErrorCode.BAD_ARGS, f"{len(before)} GLOPT_*.zmx files already lie next to {job.relative_lens(loaded.path)}; "
                            "global search would overwrite them. Save the lens elsewhere first (saos lens save --as NAME)")
        ids = self._measure_ids(job, args)
        measured = self._measure_before(job, ids)
        kw = self._merit_script_kw(job)
        method = _arg(args, "method", str, default=None)
        self._optiland_only("method", method)
        if method is not None:
            kw["method"] = method
        try:
            result = self.backend.optimize_search(kind, args.get("cores") or config.compute_cores(), max_seconds=float(max_seconds),
                                                  on_progress=self._optimizer_hooks(job, f"opt.{kind}"), should_cancel=job.cancel_requested, **kw)
        finally:
            loaded.dirty = True
            after = self._search_stamps(lens_dir, pattern)
            written = [lens_dir / name for name in sorted(after) if before.get(name) != after[name]]
            files = self._archive_search_files(job, kind, written)
        if result.get("stopped_by"):
            job.write_progress(stopped_by=result["stopped_by"])
        return {**loaded.describe(), **result, "files": files, "review": self._review(job, ids, measured)}

    @staticmethod
    def _search_stamps(folder: Path, pattern: str) -> dict[str, int]:
        """搜索结果的文件名 → 最新修改时间。只有快照的系统也算结果，所以按镜头的全部文件取最新时间。
        Result file name -> newest modification time. Snapshot-only systems count as results too, so every file of a lens counts."""
        return {p.name: max(f.stat().st_mtime_ns for f in lens_files.files(p)) for p in lens_files.listing(folder, pattern)}

    @staticmethod
    def _archive_search_files(job: jobs.Job, kind: str, written: list[Path]) -> list[str]:
        if not written:
            return []
        root = job.lenses_dir / kind
        root.mkdir(parents=True, exist_ok=True)
        folder = root / f"{jobs.next_archive_number(root):03d}"
        folder.mkdir()
        out = []
        for p in written:
            # 系统的 .zmx 与快照（若有）一起移走 / a system's .zmx and its snapshot, when present, move together
            for src in lens_files.files(p):
                shutil.move(str(src), folder / src.name)
            out.append(job.relative_lens(folder / p.name))
        return out

    def op_glass(self, args: dict[str, Any]) -> dict[str, Any]:
        """Optiland 阶段的在产玻璃替换（贪心邻域搜索，每种候选都重新局部优化），结束后同 opt local 复核。"""
        job = self._job(args)
        stages.require_ready(job.root)
        loaded = self._require_loaded(job)
        run = getattr(self.backend, "optimize_glass", None)
        if run is None:
            raise SaosError(ErrorCode.BAD_ARGS, "opt glass is the Optiland stage's glass substitution; in OpticStudio use opt hammer "
                                                "or opt global with opt vary --param material --mode substitute")
        workflow.require_optimise(job, self.backend)
        ids = self._measure_ids(job, args)
        before = self._measure_before(job, ids)
        surfaces = _arg(args, "surfaces", list, default=None)
        if surfaces is not None and any(isinstance(s, bool) or not isinstance(s, int) for s in surfaces):
            raise SaosError(ErrorCode.BAD_ARGS, f"surfaces must be integers, got {surfaces!r}")
        catalog = _arg(args, "catalog", str, default=None) or self.backend.substitute_catalog(None) or "SAOS_CDGM_INPROD"
        try:
            result = run(surfaces=surfaces, catalog=catalog, neighbours=_arg(args, "neighbours", int, default=7),
                         cycles=str(args.get("cycles") or "auto"), max_seconds=_arg(args, "max_seconds", (int, float), default=None),
                         algorithm=_arg(args, "algorithm", str, default=None), on_progress=self._optimizer_hooks(job, "opt.glass"),
                         should_cancel=job.cancel_requested, **self._merit_script_kw(job))
        finally:
            loaded.dirty = True
        if result.get("stopped_by"):
            job.write_progress(stopped_by=result["stopped_by"])
        return {**loaded.describe(), **result, "review": self._review(job, ids, before)}

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

    def op_scan(self, args: dict[str, Any]) -> dict[str, Any]:
        """并发扫描或多起点寻优（saos.scan），不改动当前镜头。"""
        from saos import scan

        job = self._job(args)
        plan = _arg(args, "plan", dict)
        # 结构探索的候选是各自的文件，不用当前镜头，所以不要求加载镜头 / exploration candidates are files of their own
        if "candidates" not in plan:
            self._require_loaded(job)
        return scan.run(scripting.ScriptContext(self, job, {}), plan, _arg(args, "workers", int, default=None),
                        detach=_arg(args, "detach", bool, default=False))

    def running_background(self) -> list[str]:
        return [name for name, thread in self.background.items() if thread.is_alive()]

    def op_check(self, args: dict[str, Any]) -> dict[str, Any]:
        """按需求对当前镜头逐项取值判定，不记录轮次。"""
        job = self._job(args)
        self._require_loaded(job)
        return workflow.check(scripting.ScriptContext(self, job, {}), only=_arg(args, "only", list, default=None))

    def op_round(self, args: dict[str, Any]) -> dict[str, Any]:
        """记录一个调试轮次并给出终止判断（DESIGN.md「终止规则」）。"""
        job = self._job(args)
        self._require_loaded(job)
        ctx = scripting.ScriptContext(self, job, {})
        return workflow.record_round(ctx, _arg(args, "tier", str, default=None), str(args.get("note", "")),
                                     merit=_arg(args, "merit", (int, float), default=None), viz=_arg(args, "viz", bool, default=True))

    def op_zos_begin(self, args: dict[str, Any]) -> dict[str, Any]:
        job = self._job(args)
        if self.running_background():
            raise SaosError(ErrorCode.BUSY, "wait for or cancel detached scans before entering OpticStudio")
        # 窗口副本在连接 OpticStudio 之后、加载之前由 enter_zos 保存；这里只检查 agent 自己的未保存修改
        self._check_not_dirty(False, "entering OpticStudio")
        previous = self.loaded
        try:
            return stages.begin(scripting.ScriptContext(self, job, {}), _arg(args, "quote", str),
                                _arg(args, "round", int, default=None), _arg(args, "max_rounds", int, default=None))
        except BaseException:
            # 阶段尚未封存时恢复此前的任务与镜头；封存后保留复测现场供同一入口续跑。
            if stages.read(job.root) is None:
                if previous is not None:
                    self._select_backend(previous.job)
                    self.backend.load(previous.path)
                else:
                    self.backend = self.optiland
                self.loaded = previous
            raise

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
    log.info(_t("后台进程 %s 启动，后端 %s（%s）", "daemon %s starting, backend %s (%s)"), os.getpid(), config.backend_name(), mode)
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
    log.info(_t("就绪：%s，%s %s", "ready: %s, %s %s"), config.pipe_address(), zos_info.get("backend", "zos"), zos_info.get("version"))
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
        daemon.pool.close()
        daemon.close()
        listener.close()
        config.daemon_state_file().unlink(missing_ok=True)
        config.daemon_authkey_file().unlink(missing_ok=True)
        log.info(_t("已退出", "stopped"))
    return 0
