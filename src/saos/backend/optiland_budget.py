"""优化预算与已验证镜头快照；退出时恢复快照，不再次追迹。"""

from __future__ import annotations

import copy
import math
import sys
import time
from contextlib import contextmanager

from saos.protocol import ErrorCode, SaosError


class Stop(BaseException):
    """穿过第三方的计算错误捕获，到优化调用边界退出。"""


class Snapshot:
    FIELDS = ("optic", "_prescription", "_effective", "_rows", "_scales", "_configuration", "_aiming")

    def __init__(self, backend):
        self.values = copy.deepcopy({name: getattr(backend, name) for name in self.FIELDS})

    def restore(self, backend):
        # 嵌套优化仍可能受外层预算跟踪；恢复是原子清理，不允许在后端配置中再次被打断。
        previous = sys.gettrace()
        sys.settrace(None)
        try:
            for name, value in copy.deepcopy(self.values).items():
                setattr(backend, name, value)
            backend._torch_state = None
            backend._torch_device = None
            backend._invalidate()
            backend._synchronized = (backend._revision, backend._configuration, backend._aiming)
            backend._merit_cache = None
            backend._configure()
        finally:
            sys.settrace(previous)


class Budget:
    def __init__(self, seconds, should_cancel=None):
        if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                                    or not math.isfinite(seconds) or seconds <= 0):
            raise SaosError(ErrorCode.BAD_ARGS, "max_seconds must be finite and positive")
        self.seconds = seconds
        self.should_cancel = should_cancel
        self.started = time.monotonic()
        self.stopped_by = None
        self.next_check = self.started
        self.result = {"initial_merit": None, "final_merit": None, "evaluations": 0,
                       "accepted_steps": 0, "rejected_steps": 0, "converged": False}
        self.best = None
        self.files = set()

    def watch(self, fn):
        paths = getattr(fn, "_saos_budget_files", ())
        if not paths and hasattr(fn, "__code__"):
            paths = (fn.__code__.co_filename,)
        self.files.update(path.replace("\\", "/").lower() for path in paths)

    def remaining(self):
        return None if self.seconds is None else max(0.0, self.seconds - (time.monotonic() - self.started))

    def check(self):
        if self.should_cancel is not None and self.should_cancel():
            self.stopped_by = "cancel"
        elif self.seconds is not None and self.remaining() <= 0:
            self.stopped_by = self.stopped_by or "timeout"
        if self.stopped_by:
            raise Stop

    @contextmanager
    def interruptible(self):
        # 只在第三方计算的 Python 检查点打断，避免异步异常落入快照提交或 finally。
        # 原生 C/CUDA 调用需返回到 Python 才能中断；不承诺硬实时期限。
        previous = sys.gettrace()
        def trace(frame, event, arg):
            filename = frame.f_code.co_filename.replace("\\", "/").lower()
            if "/optiland/" not in filename and "/scipy/" not in filename and filename not in self.files:
                return None
            if time.monotonic() >= self.next_check:
                self.next_check = time.monotonic() + 0.02
                self.check()
            return trace
        try:
            self.check()
            sys.settrace(trace)
            yield
            self.check()
        finally:
            sys.settrace(previous)


def outcome(initial, final, *, converged, stopped_by, admissible=True):
    finite = initial is not None and final is not None and math.isfinite(initial) and math.isfinite(final)
    improved = bool(finite and final < initial - 1e-12 * max(1.0, abs(initial)))
    return {"improved": improved, "converged": bool(converged),
            "succeeded": bool(admissible and improved and converged and stopped_by is None)}
