"""Optiland 阶段的 torch 自动微分优化（torch-lbfgs、torch-adam，Plan 2 §2.2、§2.3）。

只对可微子集求梯度：RSCE、RWCE、REAY、EFFL、TOTR、CTVA、ETVA 以及由 CTVA / ETVA 构成的边界行（CTGT、CTLT、ETGT、ETLT），
再加上返回 Tensor 的损失脚本（在脚本里用 ctx.backend.torch_operand 取可微的操作数值）。评价函数里有别的操作数时直接报错并列出，
不静默退回有限差分。变量坐标与 scipy 路径相同（optiland_opt.Problem 的缩放）。

防 NaN：Optiland 内部的开方（折射、求交）在全反射或脱靶时给出 NaN，反向传播会污染整个梯度，而 saos 改不了 Optiland 的内部，
所以每组光线先不带梯度追一遍找出失败或被裁的光线，带梯度时只追其余光线；失败光线按所占比例计一个常数惩罚（不带梯度），
线搜索看到损失变大自然退回。Plan 2 设想的“对 sinθ − 1 连续可导的惩罚”要改 Optiland 的追迹核心，没有做。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

import numpy as np

from saos.backend.base import is_par
from saos.backend.optiland_opt import BOUNDARY, CYCLES, PENALTY, REPORT_EVERY_S, Problem, _Stop, evaluate, merit
from saos.backend.optiland_state import array
from saos.protocol import ErrorCode, SaosError

TORCH_OPERANDS = {"RSCE", "RWCE", "REAY", "EFFL", "TOTR", "CTVA", "ETVA", *BOUNDARY}
# 曲率恰为 0（平面）时 1/c 的梯度是无穷大；取一个不影响矢高的极小曲率
FLAT = 1e-12


class TorchState:
    """一次 torch 运行里的可微量：变量（缩放坐标的 Parameter 换算成的物理值）、受其影响的厚度与求解结果、各面的基准位置。"""

    def __init__(self, b, problem: Problem):
        self.b, self.problem = b, problem
        self.base = [tuple(float(np.asarray(array(v)).ravel()[0]) for v in (s.geometry.cs.x, s.geometry.cs.y, s.geometry.cs.z))
                     for s in b.optic.surfaces]
        self.thickness0 = {i: float(b._rows[i].get("thickness", 0.0)) for i in range(len(b._rows))}
        self.values: dict[tuple[int, str], Any] = {}

    def write(self, i: int, column: str, value) -> None:
        """在 torch Optic 上写入一个有效值（可带梯度）；厚度只记下，位置在 place() 里一次算完。"""
        b = self.b
        self.values[(i, column)] = value
        surface = b.optic.surfaces[i]
        if column == "radius":
            b.optic.updater.set_radius(value, i)
        elif column == "conic":
            surface.geometry.k = value
        elif is_par(column):
            surface.geometry.coefficients[int(column[3:]) - 1] = value
        elif column != "thickness":
            raise SaosError(ErrorCode.BAD_ARGS, f"{column} cannot vary on the torch path")

    def thickness(self, i: int):
        return self.values.get((i, "thickness"), self.thickness0[i])

    def place(self) -> None:
        """各面位置 = 基准位置 + Σ（其前各面厚度变化 × 该面厚度方向），可对厚度求导。"""
        import torch

        b = self.b
        changed = sorted(i for (i, column) in self.values if column == "thickness")
        if not changed:
            return
        for i in changed:
            if i == 0:
                b.optic.surfaces[0].geometry.cs.z = -self.values[(0, "thickness")]
        for j, s in enumerate(b.optic.surfaces):
            if j == 0:
                continue
            shift = None
            for i in changed:
                if 0 < i < j:
                    step = (self.values[(i, "thickness")] - self.thickness0[i]) * torch.as_tensor(b.optic.saos_axes[i], dtype=torch.float64,
                                                                                                device=_device())
                    shift = step if shift is None else shift + step
            if shift is not None:
                x, y, z = self.base[j]
                s.geometry.cs.x, s.geometry.cs.y, s.geometry.cs.z = x + shift[0], y + shift[1], z + shift[2]

    def settle(self) -> None:
        """拾取、F 数（Optiland 的求解器在 torch 后端上可微）和零区域边缘光线高度（闭式）的可微版本。"""
        b = self.b
        for i, row in enumerate(b._rows):
            for column, spec in row.get("solves", {}).items():
                kind_ = spec.get("type")
                if kind_ == "pickup" and column != "material":
                    source = self.values.get((spec["surface"], column))
                    if source is None:
                        source = self.thickness0[spec["surface"]] if column == "thickness" else b.surface_value(spec["surface"], column)
                    scale = spec.get("scale", 1.0)
                    self.write(i, column, (0.0 if scale == 0 else source * scale) + spec.get("offset", 0.0))
                elif kind_ == "f_number":
                    from optiland.solves.curvature import MarginalRayAngleCurvatureSolve

                    self.place()
                    MarginalRayAngleCurvatureSolve(b.optic, i, -1 / (2 * spec["value"])).apply()
                    self.values[(i, "radius")] = b.optic.surfaces[i].geometry.radius
                elif kind_ == "marginal_ray_height":
                    # 近轴边缘光线在第 i 面之后沿 y + u·t 传播，高度对该厚度是线性的：一步就解出
                    self.place()
                    y, u = b.optic.paraxial.marginal_ray()
                    y, u = y.reshape(-1), u.reshape(-1)
                    t = self.thickness(i)
                    self.write(i, "thickness", t + (spec["height"] - y[i + 1]) / u[i])
        self.place()


def _clean(param) -> None:
    """梯度为空（损失与变量无关）时补零；个别分量非有限时置零，不让一步更新把变量带到 NaN。"""
    import torch

    if param.grad is None:
        param.grad = torch.zeros_like(param)
    elif not torch.isfinite(param.grad).all():
        param.grad = torch.nan_to_num(param.grad, nan=0.0, posinf=0.0, neginf=0.0)


def _device() -> str:
    import optiland.backend as be

    return str(be.get_device()) if hasattr(be, "get_device") else "cpu"


def _check(b, rows: list[dict[str, Any]]) -> None:
    others = sorted({row["type"] for row in rows} - TORCH_OPERANDS)
    if others:
        raise SaosError(ErrorCode.BAD_ARGS, f"the torch algorithms differentiate only {sorted(TORCH_OPERANDS)}; the merit function also has "
                                            f"{others}. Use --algorithm trf (finite differences over every mapped operand)")
    for i, row in enumerate(b._rows):
        for column, spec in row.get("solves", {}).items():
            if spec.get("type") == "marginal_ray_height" and spec.get("zone", 0):
                raise SaosError(ErrorCode.BAD_ARGS, f"surface {i}: a marginal-ray-height solve with a nonzero zone is not differentiable "
                                                    "here; use --algorithm trf")


def _rays(b, wave: int, hx: float, hy: float, px: np.ndarray, py: np.ndarray, target: int):
    """带梯度地追迹到 target 面，返回 (rays, 有效掩码)；无效光线先不带梯度找出、再从这一组里去掉，NaN 不进计算图。"""
    import torch

    import optiland.backend as be

    nhx, nhy = b._native_fields(hx, hy)
    um = b._effective["wavelengths"]["um"][wave - 1]

    def trace(x, y, grad: bool):
        with be.grad_mode.temporary_enable() if grad else torch.no_grad():
            rays = b.optic.ray_tracer.ray_generator.generate_rays(float(nhx), float(nhy), be.array(x), be.array(y), um)
            for i, surface in enumerate(b.optic.surfaces):
                if i > target:
                    break
                surface.trace(rays, record=False)
            b.optic.surfaces[target].geometry.cs.localize(rays)
        return rays

    try:
        probe = trace(px, py, grad=False)
    except (ValueError, ZeroDivisionError, FloatingPointError, RuntimeError):
        return None, np.zeros(len(px), dtype=bool)
    values = np.column_stack([np.asarray(array(getattr(probe, k)), dtype=float).ravel() for k in ("x", "y", "z", "L", "M", "N")])
    valid = np.isfinite(values).all(axis=1) & (np.asarray(array(probe.i), dtype=float).ravel() > 0)
    if not valid.any():
        return None, valid
    try:
        return trace(px[valid], py[valid], grad=True), valid
    except (ValueError, ZeroDivisionError, FloatingPointError, RuntimeError):
        return None, np.zeros(len(px), dtype=bool)


def operand(b, state: TorchState, name: str, int1: int, int2: int, data: list[float], target: float = 0.0):
    """可微操作数的 torch 值，返回 (Tensor 或 float, 失败光线比例)。参数位置同 optiland_operands.operand。"""
    import torch

    from saos.backend.optiland_operands import _gq_pupil, _surface, _wave

    name = name.upper()
    if name in BOUNDARY:
        base, sign = BOUNDARY[name]
        raw, bad = operand(b, state, base, int1, 0, [0.0, 0.0] if base == "ETVA" else [])
        return torch.where(torch.as_tensor(sign * (raw - target) >= 0), torch.as_tensor(target, dtype=torch.float64), raw), bad
    if name == "TOTR":
        return sum(state.thickness(i) for i in range(1, len(b._rows) - 1)), 0.0
    if name == "CTVA":
        return state.thickness(_surface(b, int1)), 0.0
    if name == "ETVA":
        zone, mode = (list(data) + [0.0, 0.0])[:2]
        surface = _surface(b, int1)
        if zone or int2 or int(mode) not in (0, 1) or not 1 <= surface < len(b._rows) - 1:
            raise SaosError(ErrorCode.BAD_ARGS, "ETVA on the torch path takes Code 0, zone 0 and Mode 0 or 1 on a surface with a successor")
        r = float(b.surface_value(surface, "semi_diameter" if int(mode) else "mech_semi_diameter"))
        zero, edge = torch.zeros(1, dtype=torch.float64), torch.full((1,), r, dtype=torch.float64)
        sag = lambda k: b.optic.surfaces[k].geometry.sag(zero, edge).reshape(-1)[0]
        return state.thickness(surface) + sag(surface + 1) - sag(surface), 0.0
    if name == "EFFL":
        wave = _wave(b, int2)
        before = b.optic.wavelengths.primary_index
        try:
            b.optic.wavelengths.primary_index = wave - 1
            return b.optic.paraxial.f2().reshape(-1)[0], 0.0
        finally:
            b.optic.wavelengths.primary_index = before
    if name == "REAY":
        hx, hy, px, py = [float(v) for v in (list(data) + [0.0] * 4)[:4]]
        rays, valid = _rays(b, _wave(b, int2), hx, hy, np.array([px]), np.array([py]), _surface(b, int1))
        if rays is None:
            return math.nan, 1.0
        return rays.y.reshape(-1)[0], 0.0
    if name in ("RSCE", "RWCE"):
        hx, hy = [float(v) for v in (list(data) + [0.0, 0.0])[:2]]
        px, py, weights = _gq_pupil(int1)
        waves = [_wave(b, int2)] if int2 else list(range(1, b.wavelength_count() + 1))
        wave_weights = b._effective["wavelengths"]["weights"]
        if name == "RSCE":
            xs, ys, ws, bad = [], [], [], 0.0
            for w in waves:
                rays, valid = _rays(b, w, hx, hy, px, py, len(b._rows) - 1)
                bad += float((~valid).mean()) / len(waves)
                if rays is None:
                    continue
                xs.append(rays.x.reshape(-1)), ys.append(rays.y.reshape(-1))
                ws.append(torch.as_tensor(weights[valid] * (wave_weights[w - 1] if not int2 else 1.0), dtype=torch.float64))
            if not xs:
                return math.nan, 1.0
            x, y, wt = torch.cat(xs), torch.cat(ys), torch.cat(ws)
            wt = wt / wt.sum()
            cx, cy = (wt * x).sum(), (wt * y).sum()
            return torch.sqrt((wt * ((x - cx) ** 2 + (y - cy) ** 2)).sum() + 1e-30), bad
        return _rwce(b, waves, int2, hx, hy, px, py, weights, wave_weights)
    raise SaosError(ErrorCode.BAD_ARGS, f"{name} has no torch implementation")


def _rwce(b, waves, int2, hx, hy, px, py, weights, wave_weights):
    """RWCE：去掉加权活塞与倾斜后的 RMS 波前（波），按 Wavefront 的主光线参考球面；无效光线先剔除。"""
    import torch

    from optiland.distribution import create_distribution
    from optiland.wavefront import Wavefront

    import optiland.backend as be

    nhx, nhy = b._native_fields(hx, hy)
    total, norm, bad = 0.0, 0.0, 0.0
    for w in waves:
        um = b._effective["wavelengths"]["um"][w - 1]

        def opd(x, y, grad: bool):
            distribution = create_distribution("uniform")
            distribution.x, distribution.y = be.array(x), be.array(y)
            with be.grad_mode.temporary_enable() if grad else torch.no_grad():
                data = Wavefront(b.optic, fields=[(float(nhx), float(nhy))], wavelengths=[um], distribution=distribution,
                                 strategy="chief_ray").get_data((float(nhx), float(nhy)), um)
            return data.opd.reshape(-1), data.intensity.reshape(-1)

        try:
            probe, intensity = opd(px, py, grad=False)
        except (ValueError, ZeroDivisionError, FloatingPointError, RuntimeError):
            bad += 1.0 / len(waves)
            continue
        valid = np.isfinite(np.asarray(array(probe), dtype=float)) & (np.asarray(array(intensity), dtype=float) > 0)
        bad += float((~valid).mean()) / len(waves)
        if not valid.any():
            continue
        values, _ = opd(px[valid], py[valid], grad=True)
        wt = torch.as_tensor(weights[valid], dtype=torch.float64)
        design = torch.as_tensor(np.column_stack([np.ones(valid.sum()), px[valid], py[valid]]), dtype=torch.float64)
        normal = design.T @ (design * wt[:, None])
        coef = torch.linalg.solve(normal, design.T @ (wt * values))
        residual = values - design @ coef
        weight = wave_weights[w - 1] if not int2 else 1.0
        total = total + weight * (wt * residual ** 2).sum() / wt.sum()
        norm += weight
    if norm <= 0:
        return math.nan, 1.0
    return torch.sqrt(total / norm + 1e-30), bad


def optimize(b, *, algorithm: str, cycles: str, max_seconds: float | None, on_progress, should_cancel,
             script: Callable[[], Any] | None, script_weight: float, device: str | None) -> dict[str, Any]:
    import torch

    if cycles not in CYCLES:
        raise SaosError(ErrorCode.BAD_ARGS, f"cycles must be one of {list(CYCLES)}")
    if device == "cuda" and not torch.cuda.is_available():
        raise SaosError(ErrorCode.BAD_ARGS, "--device cuda: torch sees no CUDA device")
    rows = list(b._merit)
    if not rows and script is None:
        raise SaosError(ErrorCode.BAD_ARGS, "the merit function is empty; use saos opt merit wizard, add or script first")
    _check(b, rows)
    b._sync_optical_state()
    problem = Problem(b, rows, script, script_weight)
    if not problem.live:
        raise SaosError(ErrorCode.BAD_ARGS, "the torch path sets radius, curvature, thickness, conic and even/odd asphere terms in place; "
                                            "this lens has another kind of variable or pickup (or a varying model glass). Use trf")
    values, _ = evaluate(b, rows)
    initial = merit(rows, values, float(script()) if script is not None else None, script_weight)
    weights = [row["weight"] for row in rows] + ([script_weight] if script is not None else [])
    total = sum(weights) or 1.0
    scale = [math.sqrt(w / total) for w in weights]
    unit = torch.as_tensor(problem.unit, dtype=torch.float64)
    lo, hi = torch.as_tensor(problem.lo, dtype=torch.float64), torch.as_tensor(problem.hi, dtype=torch.float64)
    started = time.monotonic()
    out_state: dict[str, Any] = {"evaluations": 0, "stopped_by": None, "best": (math.inf, problem.x0.copy()), "reported": started,
                                 "bad": 0.0}
    b._torch_device = "cuda" if device == "cuda" else "cpu"
    try:
        b._synchronized = None
        b._sync_optical_state()
        state = TorchState(b, problem)
        b._torch_state = state
        param = torch.nn.Parameter(torch.as_tensor(problem.x0, dtype=torch.float64))

        def loss_of(x):
            if should_cancel is not None and should_cancel():
                out_state["stopped_by"] = "cancel"
                raise _Stop
            if max_seconds is not None and time.monotonic() - started > max_seconds:
                out_state["stopped_by"] = "time"
                raise _Stop
            state.values.clear()
            physical = x / unit
            for k, (i, column) in enumerate(problem.keys):
                v = physical[k]
                if column == "radius":
                    c = torch.where(v.abs() < FLAT, torch.full_like(v, FLAT), v)
                    v = 1.0 / c
                v = v * b._scales.get((i, column), 1.0)
                state.write(i, column, v)
            state.settle()
            terms, bad = [], 0.0
            for k, row in enumerate(rows):
                value, failed = operand(b, state, row["type"], row["int1"], row["int2"], row["data"], row["target"])
                bad += failed
                # 与变量无关的行是常数（float），照常计入
                value = value if isinstance(value, torch.Tensor) else torch.as_tensor(float(value), dtype=torch.float64)
                if not torch.isfinite(value):
                    terms.append(torch.as_tensor(scale[k] * PENALTY, dtype=torch.float64))
                else:
                    terms.append(scale[k] * (value - row["target"]))
            if script is not None:
                value = script()
                if not isinstance(value, torch.Tensor):
                    raise SaosError(ErrorCode.BAD_ARGS, "the merit script returned a float; only a Tensor (from ctx.backend.torch_operand or "
                                                        "ctx.backend.optic) works with the torch algorithms. Use trf")
                terms.append(scale[-1] * value.reshape(-1)[0] if torch.isfinite(value).all() else torch.as_tensor(scale[-1] * PENALTY))
            r = torch.stack([t.reshape(()) for t in terms])
            # 失败光线的比例按常数惩罚计入（不带梯度），线搜索看到损失上升会退回
            loss = (r * r).sum() + (PENALTY * bad) ** 2
            out_state["evaluations"] += 1
            out_state["bad"] = bad
            value = float(loss.detach().sqrt())
            if value < out_state["best"][0]:
                out_state["best"] = (value, x.detach().cpu().numpy().copy())
            now = time.monotonic()
            if on_progress is not None and now - out_state["reported"] >= REPORT_EVERY_S:
                out_state["reported"] = now
                on_progress({"merit": out_state["best"][0], "evaluations": out_state["evaluations"], "elapsed_s": round(now - started, 2)})
            return loss

        limit = CYCLES[cycles]
        message = ""
        import optiland.backend as be

        with be.grad_mode.temporary_enable():
            try:
                if algorithm == "torch-lbfgs":
                    optimizer = torch.optim.LBFGS([param], lr=1.0, max_iter=20, history_size=20, line_search_fn="strong_wolfe",
                                                  tolerance_grad=1e-12, tolerance_change=1e-15)

                    def closure():
                        optimizer.zero_grad()
                        loss = loss_of(param)
                        loss.backward()
                        _clean(param)
                        return loss

                    previous = math.inf
                    for _ in range(limit or 100):
                        optimizer.step(closure)
                        with torch.no_grad():
                            param.data = torch.maximum(torch.minimum(param.data, hi), lo)
                        current = out_state["best"][0]
                        if previous - current <= 1e-12 * max(1.0, current):
                            message = "converged"
                            break
                        previous = current
                else:
                    optimizer = torch.optim.Adam([param], lr=1e-2)
                    previous, flat = math.inf, 0
                    for _ in range((limit or 20) * 50):
                        optimizer.zero_grad()
                        loss = loss_of(param)
                        loss.backward()
                        _clean(param)
                        optimizer.step()
                        with torch.no_grad():
                            param.data = torch.maximum(torch.minimum(param.data, hi), lo)
                        current = float(loss.detach().sqrt())
                        flat = flat + 1 if abs(previous - current) <= 1e-10 * max(1.0, current) else 0
                        if flat >= 20:
                            message = "converged"
                            break
                        previous = current
                message = message or "step budget reached"
            except _Stop:
                message = {"cancel": "cancelled", "time": "max_seconds reached"}[out_state["stopped_by"]]
    finally:
        b._torch_device = None
        b._torch_state = None
        best = out_state["best"][1]
        try:
            b._commit(problem.physical(best))
        except SaosError:
            b._commit(problem.physical(problem.x0))
            message = "the best point failed the full rebuild, so the start was restored"
    values, errors = evaluate(b, rows)
    final = merit(rows, values, float(script()) if script is not None else None, script_weight)
    out = {"initial_merit": initial, "final_merit": final, "variables": len(problem.keys), "targets": len(rows) + (script is not None),
           "succeeded": out_state["stopped_by"] is None, "stopped_by": out_state["stopped_by"],
           "elapsed_s": round(time.monotonic() - started, 3), "algorithm": algorithm, "evaluations": out_state["evaluations"],
           "device": "cuda" if device == "cuda" else "cpu", "inner_loop": "torch", "message": message}
    failed = [{"row": k, "type": row["type"], "error": e} for k, (row, e) in enumerate(zip(rows, errors), 1) if e]
    if failed:
        out["failed_rows"] = failed
    return out
