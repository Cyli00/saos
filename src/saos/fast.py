"""脚本里的 rtc 快速副本：ctx.fast() 把当前镜头交给 rtc，在内存里高频改参数、追迹（ADR-0014）。
The rtc fast copy for scripts: ctx.fast() hands the current lens to rtc for high-rate changes and traces.

副本带 kit 的同名方法（trace、spot、set、get、values、kept、configuration、cra、profile 类），数值定义与 ctx 上的
完全相同（同一份 kit 代码），只是追迹由 rtc 完成。它的数值只用于探索：搜索收敛后用 push 写回 OpticStudio，
再用 ctx.measure / saos check / saos round 实测。判定脚本里不能用它。
The copy carries kit's methods with the same definitions as ctx (the same kit code); only the tracing is done by
rtc. Its numbers are for exploration: push the result to OpticStudio and measure it there. Checks cannot use it.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from saos.kit import KitMethods, parse_key, pupil
from saos.protocol import ErrorCode, SaosError


class FastLens(KitMethods):
    """rtc 内存副本。changed 记录经 set 写过的参数，push 时默认写回这些。
    An rtc in-memory copy. changed records the parameters written through set; push writes those back by default."""

    def __init__(self, backend):
        self.backend = backend
        self.cache: dict[Any, Any] = {}
        self.changed: dict[str, Any] = {}
        # 建立时与 OpticStudio 的追迹对照（from_context 填写）/ the trace comparison made when the copy was built
        self.agreement: dict[str, Any] | None = None

    @property
    def held(self) -> dict[str, str]:
        """被 rtc 不重算的求解锁住的参数。/ Parameters held by solves rtc does not recompute."""
        return dict(self.backend.held)

    def set(self, changes=None, **kw):  # noqa: A003 - 与 kit.set 同名 / mirrors kit.set
        changes = {**(changes or {}), **kw}
        super().set(changes)
        self.changed.update(changes)
        self.cache.clear()

    def select(self, n: int) -> None:
        """切到第 n 个结构并停在那里（kit.configuration 是块内临时切换）。/ Switch to configuration n and stay there."""
        self.backend.select_configuration(n)
        self.cache.clear()

    def push(self, ctx, keys: Iterable[str] | None = None) -> dict[str, Any]:
        """把副本里的参数写回 OpticStudio（ctx.set），返回写入的值。默认写回 set 改过的全部参数。
        之后先在 OpticStudio 上局部优化收尾，再用 saos check 或 saos round 判定；收尾前 check / round 的结果会带提醒。
        Write the copy's parameters to OpticStudio through ctx.set and return them; by default every parameter changed
        through set. Then close with an OpticStudio local optimisation before saos check or saos round judge the lens;
        until then their results carry a reminder."""
        names = []
        for key in (list(self.changed) if keys is None else list(keys)):
            kind, ref, param = parse_key(key)
            # nd/vd/dpgf 合成整块模型材料写回：只写一项时 OpticStudio 会用它自己目录里的另一项补齐，可能与副本不同
            key = f"{ref}.material" if kind == "surface" and param in ("nd", "vd", "dpgf") else key
            if key not in names:
                names.append(key)
        values = self.values(names)
        if values:
            ctx.set(values)
            note = getattr(ctx, "_note_rtc", None)  # worker 上下文没有轮次，不记 / worker contexts record no rounds
            if note is not None:
                note(True)
        return values

    def compare(self, ctx, fields: Iterable[Iterable[float]] = ((0.0, 0.0),), wave: int | Iterable[int] | None = None,
                grid: int = 7, to: int | str | None = None) -> dict[str, Any]:
        """同一批光线在 rtc 与 OpticStudio 上的差别：只在两边参数相同（刚建立副本或 push 之后）时有意义。
        返回逐项最大偏差（位置用镜头单位）、两边可用光线数和只在一边可用的光线数。
        How the same rays differ between rtc and OpticStudio; meaningful only while both hold the same parameters
        (right after the copy is made or after push). Returns the largest differences (positions in lens units), the
        usable rays on each side and the rays usable on one side only."""
        waves = ([ctx.backend.primary_wave()] if wave is None else [int(wave)] if isinstance(wave, (int, np.integer))
                 else [int(w) for w in wave])
        rows = [(w, float(hx), float(hy), px, py) for hx, hy in fields for w in waves for px, py in [(0.0, 0.0), *pupil(grid)]]
        return agreement(self.trace(rows, to), ctx.trace(rows, to))

    def op(self, name, int1=0, int2=0, *data):
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, f"the rtc copy has no operands ({name}); use ctx.op on OpticStudio")

    def analysis(self, name, settings=None):
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, f"the rtc copy has no analyses ({name}); use ctx.analysis on OpticStudio")

    def quick_focus(self):
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, "the rtc copy has no quick focus; use spot(..., refocus=True) or ctx.quick_focus")


def from_context(ctx, freeze_solves: bool = False) -> FastLens:
    """用 ctx 当前的 OpticStudio 镜头（含未保存修改）建立 rtc 副本。判定脚本里调用时拒绝。
    Build an rtc copy of ctx's current OpticStudio lens, unsaved changes included. Refused inside check scripts."""
    from saos.backend.rtc import RtcBackend

    if getattr(ctx, "in_check", False):
        raise SaosError(ErrorCode.BAD_ARGS, "checks measure on OpticStudio; rtc numbers are exploration only (ADR-0014)")
    if ctx.backend.identity().get("backend") == "rtc":
        raise SaosError(ErrorCode.BAD_ARGS, "this instance computes on rtc already; use ctx itself (ctx.trace, ctx.op, ctx.set) instead of ctx.fast")
    export = getattr(ctx.backend, "export_rtc", None)
    if export is None:
        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, f"{type(ctx.backend).__name__} cannot hand its lens to rtc")
    folder = Path(tempfile.mkdtemp(prefix="saos-rtc-"))
    try:
        path = folder / "lens.zmx"
        model = export(path)
        glass_dir = model.get("glass_dir")
        backend = RtcBackend(path, model, glass_dir=Path(glass_dir) if glass_dir else None, freeze_solves=freeze_solves)
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    fast = FastLens(backend)
    # 参数逐项一致还不够：每个视场、每个波长追一批光线与 OpticStudio 比，超差就拒绝
    # equal parameters are not enough: trace every field and wavelength on both sides and refuse beyond tolerance
    waves = list(range(1, backend.wavelength_count() + 1))
    diff = fast.compare(ctx, field_points(backend.lens), wave=waves, grid=5)
    fast.agreement = diff
    if not within_tolerance(diff):
        raise SaosError(ErrorCode.VERIFY_FAILED, f"rtc traces this lens differently from OpticStudio: {diff}", {"agreement": diff})
    return fast


# 建立副本时追迹对照的容差：实际瞄准时 OpticStudio 自己的收敛误差约 6e-8 mm（ADR-0014），留一个数量级余量
# tolerances of the build-time comparison: OpticStudio's own real-aiming error is about 6e-8 mm, one decade of margin
POSITION_TOL = 1e-6
DIRECTION_TOL = 1e-7


def agreement(mine, ref) -> dict[str, Any]:
    """同一批光线两次追迹（rtc 在前、OpticStudio 在后）的逐项最大偏差、两边可用光线数和只在一边可用的光线数。
    The largest differences between two traces of the same rays (rtc first, OpticStudio second), the usable rays on
    each side and those usable on one side only."""
    both = mine.ok & ref.ok
    out: dict[str, Any] = {"rays": int(len(mine.ok)), "ok_rtc": int(mine.ok.sum()), "ok_opticstudio": int(ref.ok.sum()),
                           "ok_one_side": int((mine.ok != ref.ok).sum())}
    for k in ("x", "y", "z", "l", "m", "n"):
        diff = np.abs(getattr(mine, k)[both] - getattr(ref, k)[both])
        out[f"max_d{k}"] = float(diff.max()) if diff.size else None
    return out


def within_tolerance(diff: dict[str, Any]) -> bool:
    worst_pos = max(diff[k] or 0.0 for k in ("max_dx", "max_dy", "max_dz"))
    worst_dir = max(diff[k] or 0.0 for k in ("max_dl", "max_dm", "max_dn"))
    return not diff["ok_one_side"] and worst_pos <= POSITION_TOL and worst_dir <= DIRECTION_TOL


def field_points(lens) -> list[tuple[float, float]]:
    """镜头每个视场点的归一化坐标 (hx, hy)，按它的归一化方式。/ Each field point in normalised coordinates."""
    xs = [f[0] for f in lens.fields] or [0.0]
    ys = [f[1] for f in lens.fields] or [0.0]
    if lens.field_normalization == "rectangular":
        sx, sy = max(abs(v) for v in xs) or 1.0, max(abs(v) for v in ys) or 1.0
    else:
        sx = sy = max(float(np.hypot(x, y)) for x, y in zip(xs, ys)) or 1.0
    return [(x / sx, y / sy) for x, y in zip(xs, ys)]
