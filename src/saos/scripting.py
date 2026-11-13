"""在后台进程内执行 agent 编写的脚本。

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
from saos.ctx.experts import ExpertMethods
from saos.kit import KitMethods
from saos.metrics import MetricMethods
from saos.lens_files import companion
from saos.protocol import ErrorCode, SaosError, finite, jsonable, stamp, write_json

if TYPE_CHECKING:
    from saos.daemon import Daemon

SCRIPTS_DIR = "scripts"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$")
TRACEBACK_CHARS = 4000
# ctx.solve 的求解及其参数默认值；None 表示必须给出 / ctx.solve kinds and parameter defaults; None means required
SOLVE_PARAMETERS: dict[str, dict[str, Any]] = {
    "fixed": {}, "variable": {}, "pickup": {"surface": None, "scale": 1.0, "offset": 0.0},
    "marginal_ray_height": {"height": 0.0, "zone": 0.0}, "f_number": {"value": None}}
REQUIRED_SOLVE_PARAMETERS = {kind: {k for k, v in d.items() if v is None} for kind, d in SOLVE_PARAMETERS.items()}


class ScriptContext(KitMethods, MetricMethods, ExpertMethods):
    """脚本能用的全部入口。镜头切换和保存要经过这里，后台进程才能跟踪当前加载的文件和未保存状态。
    kit 的方法（set、get、spot、trace、op、analysis、kept、restored、lib、memo）也挂在这里，见 saos.kit。"""

    def __init__(self, daemon: Daemon, job: jobs.Job, args: dict[str, Any], cache: dict[Any, Any] | None = None,
                 script: Path | None = None):
        self._daemon = daemon
        self.job = job
        self.args = args
        self.backend = daemon.backend
        # 同一次判定内各判定脚本共享，见 kit.memo / shared by the check scripts of one evaluation, see kit.memo
        self.cache: dict[Any, Any] = {} if cache is None else cache
        # 本次执行的脚本副本，ctx.map("fn", …) 在 worker 里从这里找函数 / the running script copy; ctx.map("fn", …) finds fn here
        self.script = script
        # 判定用的上下文不给 rtc 副本/ check contexts get no rtc copy
        self.in_check = False

    def __getattr__(self, name: str) -> Any:
        # 只在正常查找失败时调用。脚本常按 ZOS-API 的习惯猜 ctx.zos、ctx.lde 一类名字（2026-10 的任务日志里 6 次），报错时指明原生入口
        # called only when normal lookup fails; scripts guessed ZOS-API style names such as ctx.zos or ctx.lde, so point to the native entries
        if name.startswith("_"):
            raise AttributeError(name)
        if hasattr(type(self), name):
            # 属性（property）内部抛出的 AttributeError 也会落到这里；重新求值，让原错误照常抛出
            # an AttributeError raised inside a property also lands here; evaluate it again so the original error surfaces
            return object.__getattribute__(self, name)
        raise AttributeError(f"ctx has no attribute {name!r}. List the ctx methods with uv run saos api ctx and uv run saos exec --help; "
                             "on OpticStudio only, ctx.system (the IOpticalSystem: LDE, MFE, SystemData) and ctx.zosapi (the ZOSAPI "
                             "namespace) cover the rest")

    def child(self, args: dict[str, Any], cache: dict[Any, Any] | None = None) -> ScriptContext:
        """给判定脚本用的上下文：同一个任务和后台进程，默认共享 cache。/ A context for a check script; shares the cache by default."""
        ctx = ScriptContext(self._daemon, self.job, args, self.cache if cache is None else cache, self.script)
        ctx.in_check = True
        return ctx

    def fast(self, freeze_solves: bool = False):
        """当前镜头（含未保存修改）的 rtc 内存副本，用于高频追迹的探索（saos.fast）。
        An rtc in-memory copy of the current lens, unsaved changes included, for high-rate exploratory tracing
        (saos.fast); w.fast() in a worker. What the copy does and refuses: uv run saos exec --help fast."""
        from saos import fast

        return fast.from_context(self, freeze_solves)

    def _editing(self, what: str) -> None:
        if self.in_check:
            raise SaosError(ErrorCode.BAD_ARGS, f"check scripts are read-only: {what} is not available in a check")

    def merit_add(self, rows: list[dict[str, Any]]) -> list[int]:
        """按输入顺序把评价函数行插在第一个 DMFS 之前（没有时接在末尾），返回新行号。行写 type、params（或 int1/int2/data）、
        target、weight；id 与 {"ref": id} 引用同批前序行，行号在插入位置上解析。saos.ctx.rows 生成常用约束。
        Insert merit rows, in order, before the first DMFS (at the end without one) and return their row numbers. A row
        has type, params (or int1/int2/data), target and weight; id and {"ref": id} refer to an earlier row of the same
        batch, resolved at the insertion point. The batch loads as one merit file and is read back: 1501 rows take about
        65 ms instead of 9.8 s row by row, and a value OpticStudio would silently change refuses the whole batch and
        leaves the merit function as it was. Rows after the insertion point move down. saos.ctx.rows builds common
        constraints."""
        self._editing("merit_add")
        added = self.backend.merit_append(rows)
        self.mark_dirty()
        return added

    def merit_jacobian(self) -> dict[str, Any]:
        """当前镜头处评价函数对各变量的雅可比（rtc 上）：variables、rows（残差对应的评价函数行号）、residuals、
        jacobian[行][变量]、constraints（边界类行的 labels、values、jacobian）、steps、merit。镜头不变。
        用来在优化前后看结构：对 jacobian 按列归一后做奇异值分解，接近 0 的奇异值是变量之间的简并（放开再多也只算一个自由度），
        某一行整行接近 0 表示没有变量管这个目标；constraints 里 values 接近 0 的行是当前顶住的边界。
        The Jacobian of the merit function at the current lens, the one each opt local step uses: variables, rows (the
        merit row of each residual), residuals (sqrt(weight) x (value - target)), jacobian[row][variable], steps, merit,
        and constraints (labels, values >= 0 when met, jacobian) for the boundary rows. The lens is unchanged. rtc only.

        Use it to see the structure of a problem: after normalising the columns, singular values near zero are variables
        that act as one, a row near zero is a target no variable moves, and constraints with values near zero are the
        bounds now holding the design."""
        return self.backend.merit_jacobian()

    def merit_clear(self) -> None:
        """清空评价函数的所有行。/ Delete all merit function rows."""
        self._editing("merit_clear")
        self.backend.merit_clear()
        self.mark_dirty()

    def solve(self, key: str, kind: str, **parameters: Any) -> dict[str, Any]:
        """设一个面参数单元格的求解：fixed、variable、pickup(surface, scale=1, offset=0)、
        marginal_ray_height(height=0, zone=0)（厚度）、f_number(value)（半径）。返回回读的求解。
        Set the solve of one surface cell (<surface>.radius, thickness, conic, semi_diameter or par<N>) and return it
        read back: fixed, variable, pickup(surface, scale=1, offset=0), marginal_ray_height(height=0, zone=0) on
        thicknesses, f_number(value) on radii. rtc takes fixed and variable only."""
        from saos import kit
        from saos.lensdata import PAR_RE, parse_key

        self._editing("solve")
        what, ref, param = parse_key(key)
        if what != "surface" or not (param in ("radius", "thickness", "conic", "semi_diameter") or PAR_RE.match(param)):
            raise SaosError(ErrorCode.BAD_ARGS, f"求解参数应为 <surface>.radius|thickness|conic|semi_diameter|par<N>，收到 {key!r}")
        allowed = SOLVE_PARAMETERS.get(kind)
        if allowed is None or set(parameters) - set(allowed) or not REQUIRED_SOLVE_PARAMETERS.get(kind, set()) <= set(parameters):
            raise SaosError(ErrorCode.BAD_ARGS, f"solve kinds and their parameters: {SOLVE_PARAMETERS}; "
                                                f"required: {REQUIRED_SOLVE_PARAMETERS}; got {kind!r} {sorted(parameters)}")
        solve = {"type": kind, **allowed, **parameters}
        for name, value in solve.items():
            if name in ("type", "surface"):
                continue
            if finite(value) is None:
                raise SaosError(ErrorCode.BAD_ARGS, f"solve {kind}: {name} must be a finite number, got {value!r}")
        if kind == "pickup":
            solve["surface"] = kit.surface(self, solve["surface"])
        out = self.backend.set_solve(kit.surface(self, ref), param, solve)
        self.mark_dirty()
        return out

    def insert_surface(self, index: int, surface_type: str = "Standard", **values: Any) -> dict[str, Any]:
        """在 index 处（像面之前）插入一个面，换成 surface_type，再按 ctx.set 写 values（radius=…、thickness=…、par2=…）。
        Insert a surface at index (before the image), give it surface_type and set values as ctx.set would (radius=...,
        thickness=..., par2=...); returns index and type."""
        self._editing("insert_surface")
        kind = self.backend.insert_surface(index, surface_type)
        self.mark_dirty()
        if values:
            self.set({f"{index}.{k}": v for k, v in values.items()})
        return {"index": index, "type": kind}

    def change_surface_type(self, ref: int | str, surface_type: str) -> str:
        """改面型（OpticStudio 的 SurfaceType 名，不分大小写，如 EvenAspheric）；返回改后的面型。
        Change a surface's type to an OpticStudio SurfaceType name (any case, e.g. EvenAspheric) and return the new
        type; rtc takes Standard and EvenAspheric."""
        from saos import kit

        self._editing("change_surface_type")
        kind = self.backend.change_surface_type(kit.surface(self, ref), surface_type)
        self.mark_dirty()
        return kind

    def measure(self, ids: list[str] | None = None) -> dict[str, Any]:
        """按需求单的判定测当前镜头（与 check、round 同一套定义），不存证据、不记录；返回 values、shortfall（只含所测各项）、
        hard_failing、invalid、errors。改镜头—测量的循环用它，不必在脚本里重写判定。
        Measure the current lens with the demand's checks (the same definitions as check and round) without evidence or
        a record; returns values, shortfall (of the measured items only), gaps, hard_failing, invalid and errors. Use it
        as the objective of a change-and-measure loop rather than reimplementing the checks."""
        from saos import demand

        return demand.quick(self, ids)

    @property
    def system(self):
        """OpticStudio 的 PrimarySystem（只有真实后端才有）。
        OpticStudio's PrimarySystem (the IOpticalSystem: LDE, MFE, SystemData). OpticStudio only: on rtc it raises
        BACKEND_UNSUPPORTED, so read the lens through ctx.get, ctx.op and ctx.trace."""
        system = getattr(self.backend, "system", None)
        if system is None:
            if self.backend.identity().get("backend") == "rtc":
                # 检查与脚本经 ctx 读写才能在两个后端上跑/ checks and scripts run on both backends only through ctx
                raise SaosError(ErrorCode.BACKEND_UNSUPPORTED,
                                "ctx.system is OpticStudio's object and rtc has none; read the lens through "
                                "ctx.get (with <surface>.par<N> for a surface type's parameters), ctx.op (SSAG, INDX, ...), "
                                "ctx.configuration and ctx.analysis")
            raise SaosError(ErrorCode.BAD_ARGS, f"{type(self.backend).__name__} has no OpticStudio system")
        return system

    @property
    def zosapi(self):
        """ZOSAPI 命名空间，例如 ctx.zosapi.Editors.LDE.SurfaceType。
        The ZOSAPI namespace, e.g. ctx.zosapi.Editors.LDE.SurfaceType. OpticStudio only."""
        root = getattr(self.backend, "root", None)
        if root is None:
            raise SaosError(ErrorCode.BAD_ARGS, f"{type(self.backend).__name__} has no ZOS-API; ctx.zosapi exists only in the approved OpticStudio stage")
        from saos.backend.zos import _load_zosapi

        return _load_zosapi(root)

    @property
    def lens(self) -> Path | None:
        """The file of this job's loaded lens (a Path), or None when this job's lens is not loaded."""
        loaded = self._daemon.loaded
        return loaded.path if loaded is not None and loaded.job.id == self.job.id else None

    @property
    def dirty(self) -> bool:
        """Whether this job's loaded lens has unsaved changes."""
        loaded = self._daemon.loaded
        return bool(loaded is not None and loaded.job.id == self.job.id and loaded.dirty)

    def describe(self) -> dict[str, Any]:
        """The loaded lens's state: job, file (relative to lenses/) and dirty; file None when this job's lens is not
        loaded."""
        loaded = self._daemon.loaded
        return loaded.describe() if loaded is not None and loaded.job.id == self.job.id else {"job": self.job.id, "file": None, "dirty": False}

    def mark_dirty(self) -> None:
        """Record that this job's loaded lens has unsaved changes."""
        loaded = self._daemon.loaded
        if loaded is not None and loaded.job.id == self.job.id:
            loaded.dirty = True

    def _note_rtc(self, unclosed: bool) -> None:
        """fast.push 写回 rtc 结果时记为未收尾；在 OpticStudio 上优化过后清掉（saos round / check 据此提醒）。
        Mark rtc results as unclosed on fast.push; clear the mark after an OpticStudio optimisation."""
        loaded = self._daemon.loaded
        if loaded is not None and loaded.job.id == self.job.id:
            loaded.mark_rtc(unclosed)

    def show(self) -> dict[str, Any] | None:
        """在长脚本中途立即把当前镜头推给流程图窗口（布局、Lens Data、交叠检查）；ctx.progress 和命令结束时会自动推送。
        Push the current lens to the workflow window now (layout, Lens Data, interference); ctx.progress and the end of
        every command push it on their own."""
        return self._daemon.export_view()

    def load(self, name: str) -> str:
        """加载本任务 lenses / 下的文件；agent 自己的未保存修改由脚本负责。
        Load a file under this job's lenses/ (name relative to it) as the current lens. It replaces the current lens
        without an unsaved-changes prompt: save first."""
        from saos.daemon import Loaded

        path = self.job.resolve_lens(name, must_exist=True)
        from saos.lens_files import require_native

        jobs.require_zos_history(self.job.root)
        require_native(path)
        self.backend = self._daemon.backend
        self._daemon.loaded = None
        self.backend.load(path)
        self._daemon.loaded = Loaded(self.job, path)
        return self.job.relative_lens(path)

    def save_as(self, name: str, *, overwrite: bool = False, archive: bool = False) -> str:
        """把当前系统存为本任务 lenses / 下的文件，并把它记为当前加载的镜头。归档目录只有工具（archive=True）能写。
        Save the system as a file under the job's lenses/ (name relative to it) and make it the loaded lens; an existing
        file needs overwrite=True (FILE_EXISTS otherwise). Only the tools (archive=True) write into the archive folders
        lenses/rounds/ and lenses/captures/."""
        from saos.daemon import Loaded

        target = self.job.resolve_lens(name, must_exist=False)
        if self.job.is_archive_lens(target) and not archive:
            raise SaosError(ErrorCode.BAD_ARGS, "lenses/rounds/ and lenses/captures/ hold evidence written by the tools; save under another name")
        if (target.exists() or companion(target).exists()) and not overwrite and target != self.lens:
            raise SaosError(ErrorCode.FILE_EXISTS, f"job {self.job.id} already has {name}; pass overwrite=True")
        target.parent.mkdir(parents=True, exist_ok=True)
        self.backend.save_as(target)
        # 另存不改变镜头内容：未收尾的 rtc 结果仍未收尾 / saving does not change the lens, so unclosed rtc results stay unclosed
        previous = self._daemon.loaded
        self._daemon.loaded = Loaded(self.job, target, rtc_unclosed=bool(previous is not None and previous.rtc_unclosed))
        rel = self.job.relative_lens(target)
        self.job.update_state(current_lens=rel, last_saved=rel)
        return rel

    def progress(self, stage: str, **data: Any) -> bool:
        """长脚本报告进度（写进任务的 progress.json，saos job show 可见），并隔一段时间刷新流程图窗口的当前镜头；返回是否收到了取消请求。
        Report progress from a long script (the job's progress.json, shown by saos job show) and now and then refresh
        the workflow window's current lens; returns True when a cancel was requested."""
        self.job.write_progress(stage=str(stage), data=jsonable(data))
        self._daemon.view_tick(self.job)
        return self.job.cancel_requested()

    def map(self, fn: str, items: list[Any], workers: int | None = None, reset: bool = True, args: dict[str, Any] | None = None) -> dict[str, Any]:
        """在常驻的 standalone 实例池里并发执行 fn(w, item)，按条目顺序返回结果（saos.parallel）。fn 是本脚本里的函数名、
        "explore/x.py:fn" 或 "saos.scan:point"；args 原样交给每个 w.args。
        Run fn(w, item) over items on the daemon's pool of standalone instances (saos.parallel). fn is a function of
        this script, "explore/x.py:fn" in the job, or "saos.scan:point"; args reaches every w.args. Workers must return
        JSON-compatible data. A saved current lens is copied per call and reset reloads it before every item (about 0.08
        s); the current lens stays unchanged. w exposes the kit methods, measure, args, path, job, worker, reload and
        cache; its cache is fresh per item. Returns ordered results, per-item errors, worker counts and busy times, and
        startup (new workers, unavailability); every worker unavailable is ZOS_ERROR. Plan, script copy and results are
        sealed in parallel/NNN/, and job cancel waits for the current items."""
        from saos import parallel

        return parallel.run_map(self, fn, items, workers, reset, args)

    def spare_map(self, fn: str, items: list[Any], keep_free: int = 2, min_workers: int = 2, label: str = "spare") -> dict[str, Any] | None:
        """有空闲许可证才在 worker 池上跑 "saos.<模块>:<函数>"（当前内存状态的副本，不必先保存）；借不到时为 None，自己串行算。
        判定库用它把可拆开的慢测量（逐视场 PSF）分出去（saos.micro.pooled）。
        Run "saos.<module>:<function>" on the worker pool only when licences are free (on a copy of the in-memory state,
        no save needed); None when it cannot borrow the pool, so compute serially. Measurement libraries use it through
        saos.micro.pooled for slow measurements that split by field."""
        from saos import parallel

        return parallel.spare_map(self, fn, items, keep_free=keep_free, min_workers=min_workers, label=label)

    def pool_close(self) -> dict[str, Any]:
        """立即关闭 worker 池、释放许可证（否则空闲 SAOS_POOL_IDLE_S 秒后自动关闭）。
        Close the worker pool and free the licences now (otherwise it closes after SAOS_POOL_IDLE_S idle seconds);
        returns the pool status before closing."""
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
        Whether saos job cancel was run, without reporting progress. Long scripts must stop at the next safe point and
        keep the work they completed."""
        return self.job.cancel_requested()

    def path(self, rel: str) -> Path:
        """任务目录内的路径；越出任务目录时拒绝。
        A path inside the job directory; one that leaves it raises PATH_OUTSIDE_JOB."""
        return jobs.inside(self.job.root, rel)


def next_copy(job: jobs.Job, name: str) -> Path:
    if not NAME_RE.match(name):
        raise SaosError(ErrorCode.BAD_ARGS, "script name: letters, digits, - and _, 1–48 chars, starting with a letter or digit")
    folder = job.root / SCRIPTS_DIR
    folder.mkdir(exist_ok=True)
    return folder / f"{jobs.next_archive_number(folder):03d}-{name}.py"


def execute(daemon: Daemon, job: jobs.Job, name: str, source: str, args: dict[str, Any]) -> dict[str, Any]:
    from saos.ctx.hints import direct_api_hints

    copy = next_copy(job, name)
    copy.write_text(source, encoding="utf-8")
    rel = copy.relative_to(job.root).as_posix()
    record: dict[str, Any] = {"script": rel, "args": args}
    hints = direct_api_hints(source)
    if hints:
        record["hints"] = hints
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
        # 结果标明哪个后端算的、可信等级；rtc 实例上另带未对账的路径与操作数
        # the result names its backend and trust level; on the rtc instance also the unreconciled paths and operands
        stamped = stamp(daemon.backend)
        record.update(stamped)
        out.update(stamped)
        if hints:
            out["hints"] = hints
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
