"""Optiland 0.6.3 后端：进程内建模、追迹、操作数、Zernike 与分析。

镜头状态以规范化处方为准（saos.prescription.normalize），每次改动后由 OpticalState 重建 Optic：
多重结构 → 环境折射率 → 求解链 → 自动半口径。保存时 .zmx 与同名 .optiland.json 一起写出。
"""

from __future__ import annotations

import copy
import hashlib
import math
import tempfile
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any

import numpy as np

from saos import config, geometry, lens_files, timing
from saos.backend import optiland_analysis, optiland_operands, optiland_opt
from saos.backend.base import Backend
from saos.backend.optiland_materials import MaterialStore, unsupported
from saos.backend.optiland_state import OpticalState, array
from saos.backend.optiland_surfaces import kind
from saos.protocol import ErrorCode, SaosError

RING = [(math.cos(2 * math.pi * k / 16), math.sin(2 * math.pi * k / 16)) for k in range(16)] + [(0.0, 0.0)]


class OptilandBackend(OpticalState, Backend):
    def __init__(self):
        self.device = config.compute_device()
        self._initialize_state()

    def start(self) -> dict[str, Any]:
        installed = version("optiland")
        if installed != config.OPTILAND_VERSION:
            raise SaosError(ErrorCode.DAEMON_START_FAILED, f"Optiland {config.OPTILAND_VERSION} is required; installed {installed}")
        import threading

        # 导入 optiland（连带 torch）约 5 s（2026-10-20 实测），放到后台线程预热，后台进程不必等它就绪；
        # 第一次用到镜头时 _configure / _require 先等预热结束，导入出错也在那里报出
        self._warmer = threading.Thread(target=self._warm, name="optiland-import", daemon=True)
        self._warmer.start()
        self.started = True
        self._identity = {"backend": "optiland", "version": installed, "precision": config.COMPUTE_PRECISION}
        return {"backend": "optiland", "mode": "in_process", "version": installed, "license": None,
                "device": self.device, "precision": config.COMPUTE_PRECISION, "program_dir": ""}

    def identity(self) -> dict[str, Any]:
        return dict(getattr(self, "_identity", None) or {"backend": "optiland", "version": None, "precision": None})

    def _warm(self) -> None:
        try:
            self._configure_now()
            import optiland.optic  # noqa: F401 - 预热 / warm-up
            import optiland.wavefront  # noqa: F401
        except Exception as exc:  # noqa: BLE001 - 记下，第一次使用时报 / reported on first use
            self._warm_error = exc

    def _wait_warm(self) -> None:
        warmer = getattr(self, "_warmer", None)
        if warmer is not None:
            warmer.join()
            self._warmer = None
            error = getattr(self, "_warm_error", None)
            if error is not None:
                raise SaosError(ErrorCode.OPTILAND_ERROR, f"cannot configure Optiland float64 on {self.device}: {error}") from error

    def _configure(self):
        self._wait_warm()
        return self._configure_now()

    def _require(self):
        self._wait_warm()
        return super()._require()

    # ---- 追迹 ----

    def _trace(self, arr: np.ndarray, target: int) -> np.ndarray:
        """批量实际光线追迹到 target 面，返回 (error, vignette, x, y, z, l, m, n, intensity)，坐标在 target 局部系。
        error：正数为未击中该面，负数为该面全反射（同 OpticStudio）；vignette：第一个裁掉光线的面。"""
        self._require()
        self._configure()
        if not 1 <= target < len(self._rows):
            raise SaosError(ErrorCode.BAD_ARGS, f"trace target must be 1–{len(self._rows) - 1}")
        if len(arr) and (not np.isfinite(arr).all() or np.any(arr[:, 0] != arr[:, 0].astype(int))):
            raise SaosError(ErrorCode.BAD_ARGS, "rays need finite coordinates and integer wavelength indices")
        if np.any((arr[:, 0] < 1) | (arr[:, 0] > self.wavelength_count())):
            raise SaosError(ErrorCode.BAD_ARGS, "wavelength index is outside the current system")
        out = np.full((len(arr), 9), np.nan, dtype=np.float64)
        for wave, hx, hy in dict.fromkeys(tuple(row[:3]) for row in arr):
            selected = np.flatnonzero(np.all(arr[:, :3] == (wave, hx, hy), axis=1))
            nhx, nhy = self._native_fields(float(hx), float(hy))
            out[selected] = self._trace_group(arr[selected, 3], arr[selected, 4], int(wave), float(nhx), float(nhy), target)
        return out

    def _trace_group(self, px: np.ndarray, py: np.ndarray, wave: int, hx: float, hy: float, target: int,
                     collect: dict[int, list] | None = None, path: list | None = None) -> np.ndarray:
        """collect 不为空时另记下每个面（1…target）上的局部 (x, y) 和是否到达，供自动半口径一次追迹算完全部面。
        path 不为空时逐面（1…target）追加 (全局落点, 全局出射方向, error, vignette)，供布局光线一次追迹画完全部面。"""
        be = self._configure()
        try:
            from saos.backend.optiland_guard import aiming_without_records
            with aiming_without_records(self.optic):
                rays = self.optic.ray_tracer.ray_generator.generate_rays(hx, hy, be.array(px), be.array(py),
                                                                         self._effective["wavelengths"]["um"][wave - 1])
        except (ValueError, ZeroDivisionError, FloatingPointError, RuntimeError):
            # 瞄准对整组光线一起迭代，组里有一根瞄不上就整组失败；逐根重试，单根仍瞄不上时用后备瞄准，再不行才记为第 1 面错误
            # the aimer iterates the whole group at once, so one unaimable ray fails all; retry ray by ray, then a fallback aimer
            rays = self._aim_fallback(float(px[0]), float(py[0]), wave, hx, hy) if len(px) == 1 else None
        if rays is None:
            if len(px) == 1:
                row = np.full((1, 9), np.nan)
                row[0, :2] = (1, 0)
                if collect is not None:
                    for i in range(1, target + 1):
                        collect.setdefault(i, []).append((np.full(1, np.nan), np.full(1, np.nan), np.zeros(1, bool)))
                if path is not None:
                    path.extend((np.full((1, 3), np.nan), np.full((1, 3), np.nan), np.ones(1, int), np.zeros(1, int)) for _ in range(target))
                return row
            parts = [[] if path is not None else None for _ in range(len(px))]
            out = np.vstack([self._trace_group(px[k:k + 1], py[k:k + 1], wave, hx, hy, target, collect, parts[k]) for k in range(len(px))])
            if path is not None:
                path.extend(tuple(np.concatenate([part[i][j] for part in parts]) for j in range(4)) for i in range(target))
            return out
        error = np.zeros(len(px), dtype=int)
        vignette = np.zeros(len(px), dtype=int)
        for i, optical_surface in enumerate(self.optic.surfaces):
            if i > target:
                break
            with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
                optical_surface.trace(rays, record=False)
            position = np.isfinite(np.column_stack([array(getattr(rays, k)).ravel() for k in ("x", "y", "z")])).all(axis=1)
            direction = np.isfinite(np.column_stack([array(getattr(rays, k)).ravel() for k in ("L", "M", "N")])).all(axis=1)
            fresh = error == 0
            error[fresh & ~position] = max(1, i)
            error[fresh & position & ~direction] = -max(1, i)
            vignette[(vignette == 0) & (error == 0) & (array(rays.i).ravel() <= 0)] = max(1, i)
            if path is not None and i >= 1:
                path.append((np.column_stack([array(getattr(rays, k)).ravel() for k in ("x", "y", "z")]),
                             np.column_stack([array(getattr(rays, k)).ravel() for k in ("L", "M", "N")]), error.copy(), vignette.copy()))
            if collect is not None and i >= 1:
                rotation, translation = self.global_matrix(i)
                points = np.column_stack([array(getattr(rays, k)).ravel() for k in ("x", "y", "z")])
                with np.errstate(invalid="ignore"):
                    local = (points - translation) @ rotation
                collect.setdefault(i, []).append((local[:, 0], local[:, 1], error == 0))
        self.optic.surfaces[target].geometry.cs.localize(rays)
        return np.column_stack([error, vignette, *[array(getattr(rays, k)).ravel() for k in ("x", "y", "z", "L", "M", "N", "i")]])

    def _aim_fallback(self, px: float, py: float, wave: int, hx: float, hy: float):
        """Optiland 的瞄准器对个别离轴光线不收敛时的后备（2026-10-20 双高斯 21° 视场 149 根里 5 根，OpticStudio 照常追到像面）：
        从近轴瞄准的起点出发，物在无穷远时平移入射点、有限物距时转动出射方向，用二维求根让光线落在光阑局部坐标
        (px, py)·近轴光阑半径处，即瞄准器自己的目标。返回未追迹的光线，求不出时返回 None。"""
        from optiland.rays import RealRays
        from scipy.optimize import root

        if self._aiming == "paraxial":
            return None
        be = self._configure()
        um = self._effective["wavelengths"]["um"][wave - 1]
        stop = int(self.optic.surfaces.stop_index)
        try:
            self.optic.set_ray_aiming("paraxial")
            seed = self.optic.ray_tracer.ray_generator.generate_rays(hx, hy, be.array([px]), be.array([py]), um)
            ym, _ = self.optic.paraxial.marginal_ray()
        except (ValueError, ZeroDivisionError, FloatingPointError, RuntimeError):
            return None
        finally:
            self.optic.set_ray_aiming(self._aiming, max_iter=50 if self._aiming == "robust" else 20, tol=1e-10)
        radius = abs(float(array(ym).ravel()[stop]))
        start = {k: float(array(getattr(seed, k)).ravel()[0]) for k in ("x", "y", "z", "L", "M", "N")}
        infinite = math.isinf(float(self.surface_value(0, "thickness")))
        rotation, translation = self.global_matrix(stop)
        goal = np.array([px * radius, py * radius])

        def launch(v):
            if infinite:
                return RealRays(*(be.array([value]) for value in (start["x"] + v[0], start["y"] + v[1], start["z"],
                                                                     start["L"], start["M"], start["N"])), be.array([1.0]), be.array([um]))
            L, M = start["L"] + v[0], start["M"] + v[1]
            N = math.copysign(math.sqrt(max(0.0, 1.0 - L * L - M * M)), start["N"])
            return RealRays(*(be.array([value]) for value in (start["x"], start["y"], start["z"], L, M, N)), be.array([1.0]), be.array([um]))

        def residual(v):
            rays = launch(v)
            with np.errstate(invalid="ignore", divide="ignore", over="ignore"):
                for optical_surface in self.optic.surfaces[:stop + 1]:
                    optical_surface.trace(rays, record=False)
            point = np.array([float(array(getattr(rays, k)).ravel()[0]) for k in ("x", "y", "z")])
            local = (point - translation) @ rotation
            return local[:2] - goal if np.isfinite(local).all() else np.full(2, 1e6)

        solution = root(residual, np.zeros(2), method="hybr", options={"xtol": 1e-13})
        if not solution.success or np.max(np.abs(residual(solution.x))) > 1e-9 * max(1.0, radius):
            return None
        return launch(solution.x)

    @timing.timed("optiland.trace")
    def trace_batch(self, rays, to: int):
        from saos.kit import Rays

        arr = np.asarray(list(rays), dtype=np.float64).reshape(-1, 5)
        return Rays.from_rows(arr, self._trace(arr, to), to)

    def trace_check(self, rays) -> list[dict[str, Any]]:
        traced = self.trace_batch(rays, len(self._rows) - 1)
        return [{"ray": list(ray), "error": int(error), "vignette": int(vignette)}
                for ray, error, vignette in zip(rays, traced.error, traced.vignette) if error or vignette]

    def _update_apertures(self) -> None:
        """未给定半口径的面：全视场、全波长的边缘光线环（16 根）加主光线的最大径向落点，同 OpticStudio 的自动半口径。"""
        # 优化内循环里重算时，上一次算出的自动半口径也要重算
        wanted = [i for i, row in enumerate(self._rows) if i and ("semi_diameter" not in row or row.get("_auto_semi_diameter"))]
        if not wanted:
            self._check_object_aperture()
            return
        # 一次追迹到最后一个要算的面、逐面记下落点：逐面各追一遍时耗时随面数平方增长（1000 面超过 10 分钟）
        px, py = np.array([p[0] for p in RING]), np.array([p[1] for p in RING])
        collect: dict[int, list] = {}
        for w in range(1, self.wavelength_count() + 1):
            for hx, hy in self._normalized_fields():
                nhx, nhy = self._native_fields(hx, hy)
                self._trace_group(px, py, w, float(nhx), float(nhy), wanted[-1], collect)
        for i in wanted:
            row = self._rows[i]
            x, y, reached = (np.concatenate([part[k] for part in collect[i]]) for k in range(3))
            usable = reached & np.isfinite(x) & np.isfinite(y)
            if not usable.any():
                raise SaosError(ErrorCode.OPTILAND_ERROR, f"no marginal or chief ray reaches surface {i}, so its automatic semi-diameter "
                                                          "is undefined; check the thicknesses, radii and apertures up to it")
            row["semi_diameter"] = float(np.max(np.hypot(x[usable], y[usable])))
            row["_auto_semi_diameter"] = True
            self.optic.surfaces[i].semi_aperture = row["semi_diameter"]
        self._check_object_aperture()

    def _check_object_aperture(self) -> None:
        """物方孔径（NA、锥角）开光线瞄准时，Optiland 的瞄准器在光束超出镜片时会退回近轴估计的光阑半径，实际物方 NA 变小
        （2026-10-20：规定 0.05，Optiland 0.0415，OpticStudio 保持 0.05 并让部分光线失败）。对不上就交给 OpticStudio。"""
        ap = self._effective["aperture"]
        if ap["type"] not in ("object_na", "object_cone_angle") or self._aiming == "paraxial"                 or math.isinf(float(self._rows[0].get("thickness", 0.0))):
            return
        n0 = float(array(self.optic.surfaces[0].material_post.n(self._effective["wavelengths"]["um"][self.primary_wave() - 1])).ravel()[0])
        wanted = ap["value"] if ap["type"] == "object_na" else n0 * math.sin(math.radians(ap["value"]))
        try:
            actual = optiland_operands.operand(self, "OBSN", 0, 0, [])
        except SaosError:
            actual = math.nan
        if not (math.isfinite(actual) and abs(actual - wanted) <= 1e-6 * max(1.0, wanted)):
            raise unsupported(f"with ray aiming, Optiland reaches object-space NA {actual:.6g} instead of {wanted:.6g} "
                              "(its aimer falls back to a paraxial stop size when the beam overfills the lens)")

    # ---- 几何 ----

    def global_matrix(self, index: int):
        self._require()
        translation, rotation = self.optic.surfaces[index].geometry.cs.get_effective_transform()
        return array(rotation).reshape(3, 3), array(translation).reshape(3)

    def sag(self, index: int, x: float, y: float) -> float | None:
        self._require()
        with np.errstate(invalid="ignore", divide="ignore"):
            value = float(array(self.optic.surfaces[index].geometry.sag(float(x), float(y))).ravel()[0])
        return value if math.isfinite(value) else None

    def snapshot(self) -> list[dict[str, Any]]:
        self._require()
        output = []
        for i, row in enumerate(self._rows):
            item = {k: copy.deepcopy(v) for k, v in row.items() if not k.startswith("_")}
            item.update(index=i, stop=bool(self.optic.surfaces[i].is_stop),
                        semi_diameter=float(self.surface_value(i, "semi_diameter")), thickness=float(self.surface_value(i, "thickness")),
                        material=self.surface_value(i, "material"))
            if kind(row) == "standard":
                item.update(radius=float(self.surface_value(i, "radius")), conic=float(self.surface_value(i, "conic")))
            output.append(item)
        return output

    def surfaces(self) -> list[dict[str, Any]]:
        rows = super().surfaces()
        for row, raw in zip(rows, self._rows):
            if raw.get("_auto_semi_diameter"):
                row["solves"] = {**row["solves"], "semi_diameter": "automatic"}
        return rows

    # ---- 计算 ----

    def operand(self, name: str, int1: int, int2: int, data: list[float]) -> float:
        self._require()
        return optiland_operands.operand(self, str(name), int(int1), int(int2), [float(v) for v in data])

    def zernike(self, *, wave: int, field: int, samp: int, max_term: int, terms: tuple[int, ...]) -> dict[int, float]:
        self._require()
        return optiland_analysis.zernike(self, wave=wave, field=field, samp=samp, max_term=max_term, terms=tuple(terms))

    # ---- 优化（变量与边界见 OpticalState.vary / set_bounds）----

    def merit_wizard(self, settings: dict[str, Any]) -> int:
        self._require()
        rows, start_at = optiland_opt.wizard_rows(self, settings)
        self._merit = self._merit[:start_at - 1] + rows
        return len(self._merit)

    def merit_add(self, rows: list[dict[str, Any]]) -> int:
        self._require()
        rows = optiland_opt.normalize_rows(rows)
        # 未映射的操作数现在就报出，混合后端据此把会话切到 OpticStudio 再加；计算失败（光线不通）不算
        optiland_opt.evaluate(self, rows)
        # 同 OpticStudio：新行插在开头 / new rows go first, as in OpticStudio
        self._merit = rows + self._merit
        return len(self._merit)

    def merit_clear(self) -> None:
        self._merit, self._merit_script = [], None

    def merit_script(self) -> dict[str, Any] | None:
        """评价函数的损失脚本 {name, source, weight}；没有时为 None。"""
        return dict(self._merit_script) if self._merit_script else None

    def set_merit_script(self, source: str, name: str, weight: float = 1.0) -> None:
        if not isinstance(weight, (int, float)) or isinstance(weight, bool) or not math.isfinite(weight) or weight <= 0:
            raise SaosError(ErrorCode.BAD_ARGS, f"the script weight must be a positive number, got {weight!r}")
        self._require()
        self._merit_script = {"name": name, "source": source, "weight": float(weight)}

    def _merit_evaluation(self, script=None) -> dict[str, Any]:
        self._require()
        key = (self._revision, self._configuration, repr(self._merit), script is not None)
        if self._merit_cache is not None and self._merit_cache[0] == key:
            return self._merit_cache[1]
        values, errors = optiland_opt.evaluate(self, self._merit)
        weight = (self._merit_script or {}).get("weight", 1.0)
        script_value = None
        if script is not None:
            try:
                script_value = float(script())
            except SaosError as exc:
                if exc.code in (ErrorCode.BACKEND_UNSUPPORTED, ErrorCode.SCRIPT_ERROR):
                    raise
                script_value, script_error = math.nan, exc.message
            else:
                script_error = None if math.isfinite(script_value) else "the loss is not finite"
        result = {"values": values, "errors": errors, "script_value": script_value, "weight": weight,
                  "merit": optiland_opt.merit(self._merit, values, script_value, weight)}
        if script is not None:
            result["script_error"] = script_error
        self._merit_cache = (key, result)
        return result

    @timing.timed("optiland.merit")
    def merit_value(self, script=None) -> float:
        return self._merit_evaluation(script)["merit"]

    @timing.timed("optiland.merit")
    def merit_rows(self, script=None) -> list[dict[str, Any]]:
        ev = self._merit_evaluation(script)
        terms = [row["weight"] * (v - row["target"]) ** 2 if math.isfinite(v) else math.inf for row, v in zip(self._merit, ev["values"])]
        if ev["script_value"] is not None:
            v = ev["script_value"]
            terms.append(ev["weight"] * v * v if math.isfinite(v) else math.inf)
        total = sum(terms)
        share = lambda t: 100.0 * t / total if 0 < total < math.inf else (100.0 if t == math.inf else 0.0)
        rows = []
        for k, (row, value, error) in enumerate(zip(self._merit, ev["values"], ev["errors"]), 1):
            item = {"row": k, "type": row["type"], "int1": row["int1"], "int2": row["int2"], "data": list(row["data"]),
                    "target": row["target"], "weight": row["weight"], "value": value, "contribution": share(terms[k - 1])}
            if error:
                item["error"] = error
            rows.append(item)
        if self._merit_script:
            item = {"row": len(rows) + 1, "type": "SCRIPT", "script": self._merit_script["name"], "target": 0.0,
                    "weight": self._merit_script["weight"], "value": ev["script_value"]}
            if ev["script_value"] is not None:
                item["contribution"] = share(terms[-1])
                if ev.get("script_error"):
                    item["error"] = ev["script_error"]
            else:
                item["note"] = "evaluated only by the daemon (opt merit show / opt local)"
            rows.append(item)
        return rows

    @timing.timed("optiland.optimize")
    def optimize(self, cycles: str, cores: int | None = None, *, max_seconds: float | None = None, on_progress=None,
                 should_cancel=None, algorithm: str | None = None, script=None, device: str | None = None) -> dict[str, Any]:
        """默认优先 Torch 自动微分，能力不支持时回退 SciPy。脚本损失按 set_merit_script 的权重计。"""
        self._require()
        if self._merit_script and script is None:
            raise SaosError(ErrorCode.BAD_ARGS, "the merit function has a loss script but the daemon passed no callable for it")
        weight = (self._merit_script or {}).get("weight", 1.0)
        result = optiland_opt.optimize(self, algorithm=algorithm, cycles=cycles, max_seconds=max_seconds, on_progress=on_progress,
                                       should_cancel=should_cancel, script=script, script_weight=weight, device=device)
        if self._bounds:
            result["bounds"] = copy.deepcopy(self._bounds)
        return result

    def torch_operand(self, name: str, int1: int = 0, int2: int = 0, data: list[float] | None = None, target: float = 0.0):
        """供损失脚本用：torch 优化运行中返回可微的 Tensor（只有 RSCE、RWCE、REAY、EFFL、TOTR、CTVA、ETVA 和边界行），
        其余时候返回与 operand 相同的数值，同一个脚本在 scipy 与 torch 算法下都能用。"""
        from saos.backend import optiland_torch

        if self._torch_state is None:
            return optiland_opt.row_value(self, {"type": str(name).upper(), "int1": int(int1), "int2": int(int2),
                                                 "data": [float(v) for v in data or []], "target": float(target)})
        value, _ = optiland_torch.operand(self, self._torch_state, str(name), int(int1), int(int2), [float(v) for v in data or []], float(target))
        return value

    def _script_weight(self, script) -> float:
        if self._merit_script and script is None:
            raise SaosError(ErrorCode.BAD_ARGS, "the merit function has a loss script but the daemon passed no callable for it")
        return (self._merit_script or {}).get("weight", 1.0)

    @timing.timed("optiland.search")
    def optimize_search(self, kind: str, cores: int | None = None, *, max_seconds: float, on_progress=None, should_cancel=None,
                        method: str | None = None, script=None) -> dict[str, Any]:
        """Optiland 上只有全局搜索（scipy）；Hammer 是 OpticStudio 的。候选系统写在当前镜头旁边，由后台进程归档。"""
        self._require()
        if kind != "global":
            raise unsupported("Hammer runs in OpticStudio")
        if self.file is None:
            raise SaosError(ErrorCode.BAD_ARGS, "save the lens before a global search; its systems are written next to the lens file")
        return optiland_opt.global_search(self, method=method, max_seconds=max_seconds, on_progress=on_progress, should_cancel=should_cancel,
                                          folder=Path(self.file).parent, script=script, script_weight=self._script_weight(script))

    def optimize_glass(self, *, surfaces: list[int] | None, catalog: str, neighbours: int = 7, cycles: str = "auto",
                       max_seconds: float | None = None, on_progress=None, should_cancel=None, algorithm: str | None = None,
                       script=None) -> dict[str, Any]:
        """在产玻璃替换（saos opt glass），见 optiland_opt.glass_search。"""
        self._require()
        return optiland_opt.glass_search(self, surfaces=surfaces, catalog=catalog, neighbours=neighbours, cycles=cycles,
                                         max_seconds=max_seconds, on_progress=on_progress, should_cancel=should_cancel,
                                         script=script, script_weight=self._script_weight(script), algorithm=algorithm)

    def analysis_types(self) -> list[str]:
        return list(optiland_analysis.ANALYSES)

    @timing.timed("optiland.analysis")
    def analysis(self, name: str, settings: dict[str, Any], text_path: Path, keep_open: bool = False) -> dict[str, Any]:
        self._require()
        return optiland_analysis.analysis(self, name, settings, text_path)

    def quick_focus(self) -> dict[str, Any]:
        """按 RMS 光斑半径（质心参考、全视场与波长加权）的闭式最佳焦移调整像面前一面的厚度；只用于平像面。
        与 OpticStudio Quick Focus 的判据相同，解法不同：线性离焦模型一次求出，不迭代。"""
        from saos.kit import pupil

        self._require()
        image = len(self._rows) - 1
        if kind(self._rows[image]) != "standard" or not math.isinf(float(self.surface_value(image, "radius"))) \
                or self.surface_value(image, "conic") != 0:
            raise unsupported("closed-form quick focus needs a plane image surface")
        p = self._effective
        sample = pupil(21)
        wave_weights = np.repeat(np.asarray(p["wavelengths"]["weights"], dtype=float), len(sample))
        if wave_weights.sum() <= 0:
            raise SaosError(ErrorCode.BAD_ARGS, "quick focus needs a positive wavelength weight")
        numerator, denominator = 0.0, 0.0
        for (hx, hy), weight in zip(self._normalized_fields(), p["fields"]["weights"]):
            if weight <= 0:
                continue
            rays = self.trace_batch([(w, hx, hy, px, py) for w in range(1, self.wavelength_count() + 1) for px, py in sample], image)
            active = (wave_weights > 0) & rays.ok & (np.abs(rays.n) > 1e-12)
            if active.sum() < 3:
                raise SaosError(ErrorCode.OPTILAND_ERROR, "quick focus: fewer than 3 rays reach the image for a field")
            weights = wave_weights[active] / wave_weights[active].sum()
            x, y = rays.x[active], rays.y[active]
            u, v = rays.l[active] / rays.n[active], rays.m[active] / rays.n[active]
            x, y, u, v = [values - np.dot(weights, values) for values in (x, y, u, v)]
            numerator += weight * float(np.dot(weights, x * u + y * v))
            denominator += weight * float(np.dot(weights, u * u + v * v))
        shift = -numerator / denominator if denominator > 0 else math.nan
        if not math.isfinite(shift):
            raise SaosError(ErrorCode.OPTILAND_ERROR, "quick focus has no finite solution")
        before = float(self.surface_value(image - 1, "thickness"))
        self.set_surface(image - 1, "thickness", before + shift)
        return {"surface": image - 1, "thickness_before": before, "thickness_after": float(self.surface_value(image - 1, "thickness"))}

    # ---- 建模说明、交叠与界面数据 ----

    @timing.timed("optiland.describe")
    def describe_model(self, folder: Path | None, prefix: str = "layout") -> dict[str, Any]:
        from saos.backend.optiland_surfaces import aperture_description, parameters

        p = self._require()
        rows = self.surfaces()
        for i, row in enumerate(rows):
            raw = self._rows[i]
            material = self.optic.surfaces[i].material_post
            metadata = getattr(material, "metadata", {}) or {}
            name = row["material"]
            if isinstance(raw.get("material"), dict):
                # 模型玻璃：Optiland 阶段为 Abbe 近似，记入每轮材料和报告 / a model glass, approximated by an Abbe material here
                source = {"kind": "model", **{k: metadata.get(k) for k in ("nd", "vd", "dpgf")}, "approximation": metadata.get("approximation")}
            else:
                source = {"kind": "none"} if not name else {"kind": "mirror"} if str(name).upper() == "MIRROR" else \
                    {"kind": "catalog", "name": name, "catalogs": [metadata.get("catalog") or getattr(material, "reference", "")],
                     "status": metadata.get("status")}
            row.update(material_source=source,
                       n=[self.operand("INDX", i, w, []) for w in range(1, self.wavelength_count() + 1)] if str(name).upper() != "MIRROR" else [],
                       aperture=aperture_description(raw.get("aperture")), solve_detail=copy.deepcopy(raw.get("solves", {})),
                       params=[{"par": int(k[3:]) if k[3:].isdigit() else None, "name": k, "value": v} for k, v in parameters(raw).items()],
                       global_vertex=self.global_matrix(i)[1].tolist() if i else None,
                       tilt_decenter=copy.deepcopy(raw.get("decenter_tilt") or {}), coating="")
        # 光线瞄准、多重结构按 OpticStudio 的写法给出：结构指纹在两端之间比较，写法不同会被当成结构变了
        operands = [{"row": j, "type": row["operand"], "params": list(row["params"]), "values": copy.deepcopy(row["values"]),
                     "solves": ["Variable" if row.get("vary") else "Fixed"] * len(row["values"])}
                    for j, row in enumerate(p.get("configurations", []), 1)]
        desc = {**self.system_info(), "surfaces": rows,
                "ray_aiming": {"off": "Off", "paraxial": "Paraxial", "real": "Real"}[p.get("ray_aiming", "real")],
                "optiland_aiming": self._aiming, "environment": copy.deepcopy(self._effective["environment"]),
                "configurations": {"count": self.configuration_count(), "current": self._configuration, "operands": operands},
                "coating_file": "COATING.DAT", "layout": {}}
        if p.get("apodization"):
            desc["apodization"] = {"type": {"uniform": "Uniform", "gaussian": "Gaussian", "cosine_cubed": "CosineCubed"}[p["apodization"]["type"]],
                                   **({"factor": p["apodization"]["factor"]} if "factor" in p["apodization"] else {})}
        return desc

    def _geometry_items(self) -> list[dict]:
        items = []
        for row in self.surfaces()[1:-1]:
            i, clear = row["index"], row["semi_diameter"]
            if kind(self._rows[i]) == "coordinatebreak":
                continue
            rot, off = self.global_matrix(i)
            ap = geometry.aperture(self._aperture_dict(i), clear)
            items.append({"index": i, "type": row["type"], "comment": row["comment"], "material": row["material"],
                          "sd": clear, "clear": clear, "edge": row["mech_semi_diameter"], "ap": ap,
                          "rot": rot.ravel().tolist(), "off": off.tolist()})
        radii = geometry.part_radii(items)
        for item in items:
            outer = radii.get(item["index"], item["clear"])
            item["sd"] = outer
            if not item["ap"]["explicit"]:
                item["ap"] = {**item["ap"], "hx": outer, "hy": outer}
            grid = geometry.polar_points(outer)
            item["sags"] = geometry.fill_beyond(grid, [self.sag(item["index"], x, y) for x, y in grid], item["clear"])
            if None in item["sags"]:
                raise SaosError(ErrorCode.OPTILAND_ERROR, f"surface {item['index']}: sag undefined at the part edge; cannot check interference")
        return items

    def _aperture_dict(self, index: int) -> dict | None:
        from saos.backend.optiland_surfaces import aperture_description

        spec = self._rows[index].get("aperture")
        return aperture_description(spec) if spec else None

    def interference(self) -> dict[str, Any]:
        self._require()
        return geometry.interference(self._geometry_items())

    @timing.timed("optiland.viz")
    def viz_data(self, folder: Path | None, *, pupil_grid: int = 31, fan: int = 7, full: bool = True, rays: bool = True,
                 plots: frozenset[str] | None = None) -> dict[str, Any]:
        self._require()
        # plots 为 None 时导出全部按轮分析图；否则只算其中列出的（saos ui open --tabs 选定）/ None exports every per-round plot, otherwise only those listed
        want = lambda name: plots is None or name in plots  # noqa: E731
        info = self.system_info()
        image = len(self._rows) - 1
        primary = self.primary_wave()
        norm = self._normalized_fields()
        timings: dict[str, float] = {}
        errors: dict[str, str] = {}
        started = time.monotonic()
        surfaces = []
        for row in self.surfaces()[1:]:
            i, sd = row["index"], row["semi_diameter"]
            item = {"index": i, "type": row["type"], "comment": row["comment"], "material": row["material"],
                    "semi_diameter": sd, "is_stop": row["is_stop"]}
            if sd > 0 and kind(self._rows[i]) != "coordinatebreak":
                rot, off = self.global_matrix(i)
                item["vertex"] = off.tolist()
                ap = geometry.aperture(self._aperture_dict(i), sd)
                if ap["explicit"]:
                    item["aperture"] = {k: ap[k] for k in ("shape", "hx", "hy", "dx", "dy")}
                y_lo, y_hi, x_at = geometry.chord(ap, "y")
                x_lo, x_hi, y_at = geometry.chord(ap, "x")
                curves = {"profile_y": [(x_at, y_lo + (y_hi - y_lo) * k / 20) for k in range(21)],
                          "profile_x": [(x_lo + (x_hi - x_lo) * k / 20, y_at) for k in range(21)],
                          "rim": geometry.outline(ap, 32)}
                for name, points in curves.items():
                    values = [self.sag(i, x, y) for x, y in points]
                    if None in values:
                        errors.setdefault("sag", f"sag undefined on surface {i}; drawn flat")
                    item[name] = [(rot @ np.array([x, y, value or 0.0]) + off).tolist() for (x, y), value in zip(points, values)]
            surfaces.append(item)
        timings["geometry"] = round(time.monotonic() - started, 3)
        started = time.monotonic()
        clash = self.interference()
        timings["interference"] = round(time.monotonic() - started, 3)
        started = time.monotonic()
        steps = [2 * k / (fan - 1) - 1 for k in range(fan)]
        layout = [(f, hx, hy, 0.0, p) for f, (hx, hy) in enumerate(norm) for p in steps] + \
                 [(f, hx, hy, p, 0.0) for f, (hx, hy) in enumerate(norm) for p in steps if p != 0.0]
        paths = []
        if rays:
            # 每个视场一次追迹到像面、逐面记全局落点：逐面各追一遍时耗时随面数平方增长（1000 面 1033 s）
            for f, (hx, hy) in enumerate(norm):
                chosen = [(px, py) for g, _, _, px, py in layout if g == f]
                nhx, nhy = self._native_fields(hx, hy)
                record: list = []
                self._trace_group(np.array([c[0] for c in chosen]), np.array([c[1] for c in chosen]), primary, float(nhx), float(nhy),
                                  image, path=record)
                for k, (px, py) in enumerate(chosen):
                    points, direction = [], None
                    for i, (where, heading, error, vignette) in enumerate(record, 1):
                        if error[k]:
                            break
                        points.append(where[k].tolist())
                        if direction is None:
                            direction = heading[k].tolist()
                        if vignette[k] and vignette[k] <= i:
                            break
                    if points:
                        paths.append({"field": f, "px": px, "py": py, "points": points, "start_dir": direction})
        timings["layout_rays"] = round(time.monotonic() - started, 3)
        stop = next((s for s in surfaces if s["is_stop"]), None)
        base = {"system": {**{k: info[k] for k in ("title", "lens_units", "aperture", "fields", "wavelengths", "stop_surface")},
                           "primary_wave": primary, "normalization": info["fields"]["normalization"], "normalized_fields": norm,
                           "stop_semi_diameter": stop["semi_diameter"] if stop else None, "image_surface": image,
                           "non_axial": any(self._rows[i].get("decenter_tilt") or kind(self._rows[i]) == "coordinatebreak"
                                            for i in range(len(self._rows))),
                           "backend": "optiland"},
                "surfaces": surfaces, "layout": {"wave": primary, "rays": paths, **({} if rays else {"rays_paused": True})},
                "lens_data": self.surfaces(), "interference": clash}
        if not full:
            return {**base, "timings_s": timings, "errors": errors}
        started = time.monotonic()
        grid = [2 * k / (pupil_grid - 1) - 1 for k in range(pupil_grid)]
        pupil = [(px, py) for py in grid for px in grid if px * px + py * py <= 1.0]
        spots = []
        for f, (hx, hy) in enumerate(norm if want("spot") else []):
            chief = self.trace_batch([(primary, hx, hy, 0.0, 0.0)], image)
            cx, cy = (chief.x[0], chief.y[0]) if chief.ok[0] else (math.nan, math.nan)
            per_wave = []
            for w in range(1, self.wavelength_count() + 1):
                hits = self.trace_batch([(w, hx, hy, px, py) for px, py in pupil], image)
                per_wave.append([[round(float(x - cx), 7), round(float(y - cy), 7)] for x, y, ok in zip(hits.x, hits.y, hits.ok)
                                 if ok and chief.ok[0]])
            spots.append({"field": f, "chief": [float(cx), float(cy)] if chief.ok[0] else None, "points": per_wave})
        timings["spot_rays"] = round(time.monotonic() - started, 3)

        def run(name: str, settings: dict[str, Any], keep: tuple[str, ...]) -> dict[str, Any] | None:
            started = time.monotonic()
            scratch = None if folder is not None else Path(tempfile.mkdtemp(prefix="saos-viz-"))
            target = (folder or scratch) / f"{name}.txt"
            try:
                out = self.analysis(name, settings, target)
                return {"series": out["series"], "spot": out.get("spot"), "settings": {s: out["settings"].get(s) for s in keep}}
            except SaosError as exc:
                errors[name] = exc.message
            finally:
                timings[name] = round(time.monotonic() - started, 3)
                if scratch is not None:
                    target.unlink(missing_ok=True)
                    scratch.rmdir()
            return None

        # 光线像差扇形图：子午扇 (0, py) 的 Δy 与弧矢扇 (px, 0) 的 Δx，相对主波长主光线，同点列图的参考
        # ray fan: Δy over the tangential fan (0, py) and Δx over the sagittal fan (px, 0), relative to the primary chief ray as in the spot diagram
        started = time.monotonic()
        fan_steps = [2 * k / 20 - 1 for k in range(21)]
        fans = []
        for f, (hx, hy) in enumerate(norm if want("fan") else []):
            chief = self.trace_batch([(primary, hx, hy, 0.0, 0.0)], image)
            if not chief.ok[0]:
                fans.append({"field": f, "tangential": None, "sagittal": None})
                continue
            cx, cy = float(chief.x[0]), float(chief.y[0])
            tangential, sagittal = [], []
            for w in range(1, self.wavelength_count() + 1):
                ty = self.trace_batch([(w, hx, hy, 0.0, p) for p in fan_steps], image)
                sx = self.trace_batch([(w, hx, hy, p, 0.0) for p in fan_steps], image)
                tangential.append([round(float(y - cy), 7) if ok else None for y, ok in zip(ty.y, ty.ok)])
                sagittal.append([round(float(x - cx), 7) if ok else None for x, ok in zip(sx.x, sx.ok)])
            fans.append({"field": f, "tangential": tangential, "sagittal": sagittal})
        timings["ray_fan"] = round(time.monotonic() - started, 3)
        field_plots = self._field_plots(info, errors, timings) if want("field") else None

        spot_analysis = run("StandardSpot", {"ReferTo": "ChiefRay"}, ("ReferTo", "Pattern", "RayDensity")) if want("spot") else None
        mtf = run("FftMtf", {"ShowDiffractionLimit": True, "Wavelength": primary}, ("MaximumFrequency", "SampleSize", "Type", "Wavelength")) if want("mtf") else None
        spot_stats = (spot_analysis or {}).get("spot") or {}
        out = {**base, "timings_s": timings, "errors": errors, "plots": sorted(plots) if plots is not None else None}
        if want("spot"):
            out["spot"] = {"reference": "chief ray, primary wavelength", "pupil_grid": pupil_grid, "fields": spots,
                           "rms_radius": spot_stats.get("rms_radius"), "geo_radius": spot_stats.get("geo_radius"),
                           "analysis_settings": (spot_analysis or {}).get("settings")}
        if want("fan"):
            out["ray_fan"] = {"reference": "chief ray, primary wavelength", "pupil": fan_steps, "fields": fans}
        if want("field"):
            out["field_plots"] = field_plots
        if want("mtf"):
            out["mtf"] = mtf
        return out

    def _field_plots(self, info: dict[str, Any], errors: dict[str, str], timings: dict[str, float], points: int = 21) -> dict[str, Any] | None:
        """场曲与畸变：直接用 Optiland 的 FieldCurvature、Distortion（与 Optiland GUI 的分析列表同一实现），只取数据不画图。
        Field curvature and distortion from Optiland's FieldCurvature and Distortion (the same classes the Optiland GUI lists); data only."""
        import optiland.backend as be
        from optiland.analysis import Distortion, FieldCurvature

        try:
            max_field = float(np.asarray(be.to_numpy(self.optic.fields.max_field)).reshape(-1)[0])
        except Exception as exc:  # noqa: BLE001 - 读不到视场就两张图都不给 / no field extent: neither plot
            errors["field_plots"] = f"{type(exc).__name__}: {exc}"[:300]
            return None
        out: dict[str, Any] = {"max_field": max_field, "field_type": info["fields"]["type"], "points": points}
        started = time.monotonic()
        try:
            curvature = FieldCurvature(self.optic, num_points=points)
            out["curvature"] = [{"um": float(wp.value), "tangential": np.asarray(be.to_numpy(d[0]), dtype=float).round(7).tolist(),
                                 "sagittal": np.asarray(be.to_numpy(d[1]), dtype=float).round(7).tolist()}
                                for wp, d in zip(curvature.wavelengths, curvature.data)]
        except Exception as exc:  # noqa: BLE001 - 非轴对称等情况 Optiland 算不出，界面显示缺口 / shown as a gap
            errors["field_curvature"] = f"{type(exc).__name__}: {exc}"[:300]
        timings["field_curvature"] = round(time.monotonic() - started, 3)
        started = time.monotonic()
        try:
            distortion = Distortion(self.optic, num_points=points)
            out["distortion"] = [{"um": float(wp.value), "percent": np.asarray(be.to_numpy(d), dtype=float).round(7).tolist()}
                                 for wp, d in zip(distortion.wavelengths, distortion.data)]
        except Exception as exc:  # noqa: BLE001
            errors["distortion"] = f"{type(exc).__name__}: {exc}"[:300]
        timings["distortion"] = round(time.monotonic() - started, 3)
        return out

    # ---- 文件 ----

    def _zemax_lines(self) -> list[str]:
        from optiland.fileio.zemax.writer.encoder import ZemaxFileEncoder
        from optiland.fileio.zemax.writer.formatter import OpticToZemaxConverter
        from optiland.materials.data import DataMaterial

        unmapped = [i for i, row in enumerate(self._rows) if kind(row) not in ("standard", "evenaspheric", "oddaspheric", "paraxial")
                    or row.get("decenter_tilt") or row.get("aperture")]
        if unmapped or self.configuration_count() > 1:
            raise unsupported(f"the Optiland .zmx writer cannot express surfaces {unmapped} or multiple configurations")

        class CatalogExporter(OpticToZemaxConverter):
            def _format_glass(exporter, mat, surf_idx, glass_catalogs, is_reflective=False):
                base = getattr(mat, "base", mat)
                model = getattr(base, "metadata", {}) or {}
                if model.get("approximation"):
                    # 模型玻璃按处方的 nd、Vd 写（写出器默认按主波长重算 V）；OpticStudio 读这一行的结果没有核对过
                    return {"name": "MODEL", "n": model["nd"], "V": model["vd"]}
                if isinstance(base, DataMaterial) and base.metadata.get("catalog"):
                    metadata = base.metadata
                    glass_catalogs.append(metadata["catalog"])
                    return {"name": base.name, "catalog": metadata["catalog"], "n": metadata.get("nd"), "V": metadata.get("vd")}
                return super()._format_glass(base, surf_idx, glass_catalogs, is_reflective)

        try:
            model = CatalogExporter(self.optic).convert()
            model.fields["weights"] = list(self._effective["fields"]["weights"])
            lines = ZemaxFileEncoder(model).encode()
        except (ValueError, TypeError, KeyError, AttributeError, NotImplementedError) as exc:
            raise unsupported(f"the Optiland .zmx writer failed: {exc}") from exc
        # Optiland 的写出器不写光线瞄准和环境、波长权重全写 1、不写矩形视场归一化；按 OpticStudio 2024 R1 自己保存的格式补上
        # （2026-10-20 逐项设置后读 OpticStudio 存出的文件确认各列含义），否则 OpticStudio 重开时瞄准变成 Off，结构指纹判为变化
        p = self._effective
        options = p.get("ray_aiming_options") or {}
        env = p["environment"]
        weights = p["wavelengths"]["weights"]
        mode = {"off": 0, "paraxial": 1, "real": 2}[p.get("ray_aiming", "real")]
        raim = f"RAIM 0 {mode} 1 {0 if options.get('cache') is False else 1} 0 {1 if options.get('robust') else 0} 0 0 0 1"
        exported = []
        surface = None
        floating = p["aperture"]["type"] == "float_by_stop"
        for line in lines:
            parts = line.split()
            head = parts[0] if parts and not line.startswith(" ") else ""
            if head in ("RAIM", "ENVD"):
                continue
            if head == "SURF":
                surface = int(parts[1])
            elif parts and parts[0] in ("CLAP", "DIAM") and surface is not None and line.startswith(" "):
                # 写出器把处方给定的半口径（Optic 上是 RadialAperture）写成 CLAP，OpticStudio 重开后多出圆形孔径，结构指纹判为变化。
                # OpticStudio 自己保存固定半口径是 DIAM <值> 1 0 0 1 ""，不带 CLAP；处方带面孔径的面写出器本来就不接，这里的 CLAP 都来自半口径
                if parts[0] == "CLAP":
                    continue
                row = self._rows[surface]
                if "semi_diameter" in row and not row.get("_auto_semi_diameter") and not (floating and self.optic.surfaces[surface].is_stop):
                    line = f'  DIAM {float(row["semi_diameter"])!r} 1 0 0 1 ""'
            if head == "WAVM" and len(parts) >= 3 and 1 <= int(parts[1]) <= len(weights):
                line = f"WAVM {parts[1]} {parts[2]} {weights[int(parts[1]) - 1]!r}"
            elif head == "FTYP" and len(parts) >= 6:
                parts[5] = "1" if p["fields"].get("normalization", "radial") == "rectangular" else "0"
                line = " ".join(parts)
                exported.extend([f"ENVD {env['temperature']!r} {env['pressure']!r} {1 if env['adjust_index'] else 0}", raim])
            exported.append(line)
            if line.lstrip().startswith("SURF "):
                index = int(line.split()[1])
                exported.append(f"  MEMA {self.surface_value(index, 'mech_semi_diameter'):.16E}")
        return exported

    def companion_payload(self) -> dict[str, Any]:
        self._require()
        return {"optiland_version": config.OPTILAND_VERSION, "prescription": copy.deepcopy(self._prescription),
                "configuration": self._configuration, "aiming": self._aiming, "materials": copy.deepcopy(self._store.records),
                "optimization": {"bounds": copy.deepcopy(self._bounds), "merit": copy.deepcopy(self._merit),
                                 "merit_script": copy.deepcopy(self._merit_script), "model_glass_variables": list(self._glass_vary)}}

    @timing.timed("optiland.save")
    def save_as(self, path: Path) -> None:
        """保存原生快照（同名 .optiland.json）。保存不写 .zmx：导出件由 export_zmx 显式生成，表达不了时只保留快照。"""
        self._require()
        if path.suffix.lower() != ".zmx":
            raise SaosError(ErrorCode.BAD_ARGS, "save requires a .zmx file")
        lens_files.write_snapshot(path, lens_files.companion_bytes(None, self.companion_payload()))
        self.file = path

    def export_zmx(self, path: Path) -> None:
        """显式导出 .zmx（与快照成对，哈希绑定）。Optiland 的写出器表达不了时抛出 BACKEND_UNSUPPORTED，快照保持不变。
        Explicit .zmx export, paired with the snapshot by hash. When Optiland's writer cannot express the model it raises
        BACKEND_UNSUPPORTED and the snapshot is left untouched."""
        self._require()
        if path.suffix.lower() != ".zmx":
            raise SaosError(ErrorCode.BAD_ARGS, "export requires a .zmx file")
        payload = self.companion_payload()
        lines = self._zemax_lines()
        digest = hashlib.sha256(lens_files.payload_bytes(payload)).hexdigest()
        lines.insert(1, f"NOTE 0 {lens_files.CONTENT_MARKER} {digest}")
        primary = ("\n".join(lines) + "\n").encode("utf-8")
        lens_files.write_pair(path, primary, lens_files.companion_bytes(primary, payload))

    @timing.timed("optiland.load")
    def load(self, path: Path) -> None:
        payload = lens_files.read_companion(path)
        if payload is None:
            if not path.is_file():
                raise unsupported(f"{path.name} has no lens file or companion")
            # 没有伴随文件的 .zmx：用 Optiland 自带的读取器；Optic 到 saos 处方的转换尚未实现，所以这里只到读取为止
            from optiland.fileio import load_zemax_file

            try:
                load_zemax_file(str(path))
            except ValueError as exc:
                raise unsupported(f"Optiland cannot read {path.name}: {exc}") from exc
            raise unsupported(f"Optiland read {path.name}, but its Optic is not yet converted to a saos prescription; "
                              "give the prescription data or an .optiland.json companion")
        if payload.get("optiland_version") != config.OPTILAND_VERSION:
            raise unsupported(f"the companion was written by Optiland {payload.get('optiland_version')}, not {config.OPTILAND_VERSION}")
        previous = self.__dict__.copy()
        try:
            self._store = MaterialStore(payload.get("materials") or {})
            from saos.prescription import snapshot_prescription

            self.build(snapshot_prescription(payload["prescription"]))
            if payload.get("aiming") in ("paraxial", "iterative", "robust") and payload["aiming"] != self._aiming:
                self._aiming = payload["aiming"]
                self._sync_optical_state()
            if payload.get("configuration", 1) != 1:
                self.select_configuration(int(payload["configuration"]))
            saved = payload.get("optimization") or {}
            self._bounds = copy.deepcopy(saved.get("bounds") or {})
            self._merit = copy.deepcopy(saved.get("merit") or [])
            self._merit_script = copy.deepcopy(saved.get("merit_script"))
            self._glass_vary = [int(s) for s in saved.get("model_glass_variables") or []]
        except BaseException:
            self.__dict__.update(previous)
            raise
        self.file = path

    def set_aiming(self, mode: str) -> None:
        """改光线瞄准方式（paraxial/iterative/robust）。"""
        if mode not in ("paraxial", "iterative", "robust"):
            raise SaosError(ErrorCode.BAD_ARGS, f"unknown ray aiming {mode!r}")
        before = self._aiming
        self._aiming = mode
        try:
            self._sync_optical_state()
        except BaseException:
            self._aiming = before
            self._synchronized = None
            raise
