"""默认后端：Optiland 优先，必要时借助 OpticStudio。

- 处方或镜头文件 Optiland 表达不了（can_handle 给出原因）时，整个会话换成 ZosBackend。
- Optiland 映射不了的单次计算（操作数、分析、Zernike）和写不出的 .zmx，由按同一处方搭建的 OpticStudio 孪生实例
  完成；孪生实例按镜头版本缓存，镜头一改就重建，最多占 1 个许可证。
- 优化器与显性调试窗口目前只有 OpticStudio 有：调用时按当前处方切换到 ZosBackend（Plan 2 接入 Optiland 优化器前）。
- 建模说明（describe_model）时做双端基准对齐，结果写进 parity_check。
"""

from __future__ import annotations

import math
import shutil
import tempfile
from pathlib import Path
from typing import Any

import numpy as np

from saos import lens_files
from saos.backend.optiland import OptilandBackend
from saos.protocol import ErrorCode, SaosError

# 只有 OpticStudio 能做的操作：调用时整个会话切换到 ZosBackend
# 多重结构变量、非序列物体变量、Hammer / 全局搜索和窗口操作只有 OpticStudio 能做，调用时整个会话切过去
ZOS_ONLY = ("vary_config", "vary_object", "merit_wizard_nsc", "show_windows", "screenshots", "clear_apertures")
# 先在 Optiland 上做；Optiland 报 BACKEND_UNSUPPORTED（材料变量、未映射的操作数、dls / od 算法等）时切到 OpticStudio 再做
NATIVE_FIRST = ("vary", "merit_wizard", "merit_add", "optimize", "optimize_search")
# 双端对齐的容差：一阶量相对 1e-4；主光线落点、RMS 光斑半径相对 0.5%（Plan 1 的验收指标）；通光率绝对 0.5%
TOLERANCES = {"first_order_rel": 1e-4, "chief_rel": 5e-3, "rms_rel": 5e-3, "transmission_abs": 5e-3, "length_abs": 1e-6}


def normalized_fields(info: dict[str, Any]) -> list[tuple[float, float]]:
    items = info["fields"]["items"]
    if str(info["fields"].get("normalization", "Radial")).lower() == "rectangular":
        mx, my = max(abs(f["x"]) for f in items), max(abs(f["y"]) for f in items)
        return [(f["x"] / mx if mx else 0.0, f["y"] / my if my else 0.0) for f in items]
    r = max(math.hypot(f["x"], f["y"]) for f in items)
    return [(f["x"] / r if r else 0.0, f["y"] / r if r else 0.0) for f in items]


def baseline(backend) -> dict[str, Any]:
    """两端用同一套调用取基准量：一阶量、像面前间隔、各视场主波长的主光线落点、通光率与 RMS 光斑半径（主光线参考）。"""
    info = backend.system_info()
    primary = backend.primary_wave()
    image = len(backend.surfaces()) - 1
    out: dict[str, Any] = {"errors": {}}
    for name, int2 in (("EFFL", primary), ("EPDI", 0), ("ENPP", 0), ("EXPD", 0), ("EXPP", 0), ("TOTR", 0)):
        try:
            out[name] = float(backend.operand(name, 0, int2, []))
        except SaosError as exc:
            out[name] = None
            out["errors"][name] = exc.message
    out["working_distance"] = float(backend.surface_value(image - 1, "thickness"))
    grid = [(px, py) for px in np.linspace(-1, 1, 15) for py in np.linspace(-1, 1, 15) if px * px + py * py <= 1]
    fields = []
    for f, (hx, hy) in enumerate(normalized_fields(info), 1):
        chief = backend.trace_batch([(primary, hx, hy, 0.0, 0.0)], image)
        rays = backend.trace_batch([(primary, hx, hy, px, py) for px, py in grid], image)
        ok = rays.ok
        rms = float(np.sqrt(np.mean((rays.x[ok] - chief.x[0]) ** 2 + (rays.y[ok] - chief.y[0]) ** 2))) if chief.ok[0] and ok.any() else None
        fields.append({"field": f, "chief": [float(chief.x[0]), float(chief.y[0])] if chief.ok[0] else None,
                       "transmission": float(ok.mean()), "rms_radius": rms})
    out["fields"] = fields
    return out


def compare(mine: dict[str, Any], reference: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    t = TOLERANCES
    diffs, ok = [], True

    def check(item: str, a, b, limit: float, kind: str) -> None:
        nonlocal ok
        if a is None or b is None:
            passed = a is None and b is None
            diffs.append({"item": item, "optiland": a, "zos": b, "pass": passed, "kind": "missing"})
            ok &= passed
            return
        delta = abs(a - b) if math.isfinite(a) and math.isfinite(b) else (0.0 if a == b else math.inf)
        diffs.append({"item": item, "optiland": a, "zos": b, "delta": delta, "limit": limit, "kind": kind, "pass": delta <= limit})
        ok &= delta <= limit

    for name in ("EFFL", "EPDI", "ENPP", "EXPD", "EXPP", "TOTR"):
        b = reference.get(name)
        check(name, mine.get(name), b, max(t["length_abs"], t["first_order_rel"] * abs(b)) if b is not None and math.isfinite(b) else 0.0, "first_order")
    scale = max([math.hypot(*f["chief"]) for f in reference["fields"] if f["chief"]] + [0.0])
    for a, b in zip(mine["fields"], reference["fields"]):
        f = b["field"]
        if (a["chief"] is None) != (b["chief"] is None):
            diffs.append({"item": f"field {f} chief", "optiland": a["chief"], "zos": b["chief"], "pass": False, "kind": "chief"})
            ok = False
        elif a["chief"] is not None:
            # 表里两列给主光线落点的径向高度，偏差是两个落点的距离
            limit = max(t["length_abs"], t["chief_rel"] * scale)
            delta = math.dist(a["chief"], b["chief"])
            diffs.append({"item": f"field {f} chief", "optiland": math.hypot(*a["chief"]), "zos": math.hypot(*b["chief"]),
                          "delta": delta, "limit": limit, "kind": "chief", "pass": delta <= limit})
            ok &= delta <= limit
        check(f"field {f} transmission", a["transmission"], b["transmission"], t["transmission_abs"], "transmission")
        rb = b["rms_radius"]
        check(f"field {f} rms_radius", a["rms_radius"], rb, max(t["length_abs"], t["rms_rel"] * abs(rb)) if rb is not None else 0.0, "rms")
    return diffs, ok


class HybridBackend:
    def __init__(self, mode: str = "standalone"):
        self.mode = mode
        self._active = OptilandBackend()
        self._info: dict[str, Any] = {}
        self._twin = None
        self._twin_key = None
        self._twin_info: dict[str, Any] = {}
        # 会话里所有回退原因（换镜头时 fallback_reasons 只留当前镜头的，这里不清空），例如显微物镜验收中途切到 OpticStudio
        self._history: list[str] = []

    def __getattr__(self, name: str):
        if name in ZOS_ONLY and isinstance(self._active, OptilandBackend):
            def switched(*args, **kwargs):
                self.switch_to_zos(f"{name} runs in OpticStudio only")
                return getattr(self._active, name)(*args, **kwargs)
            return switched
        if name in NATIVE_FIRST and isinstance(self._active, OptilandBackend):
            def native_first(*args, **kwargs):
                try:
                    return getattr(self._active, name)(*args, **kwargs)
                except SaosError as exc:
                    if exc.code != ErrorCode.BACKEND_UNSUPPORTED or not isinstance(self._active, OptilandBackend):
                        raise
                    if name in ("optimize", "optimize_search") and kwargs.get("script") is not None:
                        raise SaosError(ErrorCode.BACKEND_UNSUPPORTED, f"{exc.message}; the merit function has a loss script, which only "
                                        "Optiland runs, so the session stays in Optiland (clear the script to optimise in OpticStudio)") from exc
                    self.switch_to_zos(f"{name}: {exc.message}")
                kwargs.pop("script", None)
                kwargs.pop("device", None)
                kwargs.pop("method", None)
                if name == "optimize" and kwargs.get("algorithm") not in ("dls", "od"):
                    # scipy 算法名 OpticStudio 不认识，用它的默认算法（结果里的 algorithm 写明实际用的）
                    kwargs["algorithm"] = None
                return getattr(self._active, name)(*args, **kwargs)
            return native_first
        return getattr(self._active, name)

    def start(self) -> dict[str, Any]:
        self._info.update(self._active.start(), requested_backend="optiland", fallback_reasons=[])
        return self._info

    def close(self) -> None:
        self._close_twin()
        self._active.close()

    def healthy(self) -> bool:
        return self._active.healthy()

    def _remember(self, reasons: list[str]) -> None:
        for reason in reasons:
            if reason not in self._history:
                self._history.append(reason)
        del self._history[:-100]

    def _note(self, reason: str) -> None:
        reasons = self._info.setdefault("fallback_reasons", [])
        if reason not in reasons:
            reasons.append(reason)
        self._remember([reason])

    def _session_zos(self):
        """会话要用的 ZosBackend。同一进程里关掉任何一个 standalone OpticStudio 都会断开全部 ZOS-API 连接
        （实测：两个实例里关掉一个，另一个的 PrimarySystem 抛 RemotingException），所以进程里始终只留一个：
        主会话已在 OpticStudio 就用它，有孪生实例就接管它，都没有才新开。返回 (后端, 启动信息)。"""
        from saos.backend.zos import ZosBackend

        if not isinstance(self._active, OptilandBackend):
            return self._active, {k: v for k, v in self._info.items() if k not in ("requested_backend", "fallback_reasons")}
        if self._twin is not None:
            twin, info = self._twin, dict(self._twin_info)
            self._twin, self._twin_key = None, None
            return twin, info
        backend = ZosBackend(mode=self.mode)
        try:
            return backend, backend.start()
        except BaseException:
            backend.close()
            raise

    def _replace(self, reasons: list[str], operation, *, native: bool) -> None:
        old = self._active
        if native:
            candidate = OptilandBackend()
            try:
                info = candidate.start()
                operation(candidate)
            except BaseException:
                candidate.close()
                raise
        else:
            candidate, info = self._session_zos()
            try:
                operation(candidate)
            except BaseException:
                # 主会话本来就在 OpticStudio 时留着它（与只用 OpticStudio 时一样）；接管来的孪生或新开的实例关掉，此时进程里没有别的 OpticStudio
                if candidate is not old:
                    candidate.close()
                raise
        self._active = candidate
        self._info.clear()
        self._info.update(info, requested_backend="optiland", fallback_reasons=list(reasons))
        self._remember(reasons)
        if old is candidate:
            return
        if native and not isinstance(old, OptilandBackend):
            # 从 OpticStudio 换回 Optiland：旧连接留作孪生实例，下次代算不必再等启动
            self._close_twin()
            self._twin, self._twin_key, self._twin_info = old, None, self._last_zos_info(old)
            return
        old.close()

    @staticmethod
    def _last_zos_info(backend) -> dict[str, Any]:
        try:
            return {"backend": "zos", "version": ".".join(map(str, (int(backend.app.ZOSMajorVersion), int(backend.app.ZOSMinorVersion),
                                                                     int(backend.app.ZOSSPVersion))))}
        except Exception:  # noqa: BLE001 - 只用于记录版本 / only used to record the version
            return {"backend": "zos"}

    def switch_to_zos(self, reason: str) -> None:
        """按 Optiland 当前的处方和结构在 OpticStudio 里重建镜头，之后整个会话用 ZosBackend。"""
        source = self._active
        if not isinstance(source, OptilandBackend):
            return
        prescription, configuration, file = source._require(), source.current_configuration(), source.file
        # 变量随处方（variable 求解）过去；评价函数行用的就是 Zemax 操作数名，逐行照译；变量边界和损失脚本 OpticStudio 没有对应
        untranslated = [f"variable bounds {key} {bound}" for key, bound in source._bounds.items()]
        glass_vary = list(source._glass_vary)
        if source._merit_script:
            untranslated.append(f"merit loss script {source._merit_script['name']}")

        def rebuild(backend) -> None:
            backend.build(prescription)
            if configuration != 1:
                backend.select_configuration(configuration)
            for surface in glass_vary:
                backend.vary(surface, "material", True, "model")
            if source._merit:
                backend.merit_clear()
            # OpticStudio 把新行插在开头，倒着逐行加回原来的顺序
            for k, row in reversed(list(enumerate(source._merit, 1))):
                try:
                    backend.merit_add([row])
                except SaosError as exc:
                    untranslated.append(f"merit row {k} {row['type']}: {exc.message}")

        self._replace([reason], rebuild, native=False)
        self._active.file = file
        if untranslated:
            self._info["untranslated"] = untranslated
            self._note("not translated to OpticStudio, set them again there: " + "; ".join(untranslated))

    # ---- OpticStudio 孪生实例 ----

    def _close_twin(self) -> None:
        if self._twin is not None:
            self._twin.close()
        self._twin, self._twin_key = None, None

    def twin(self, reason: str):
        """与当前 Optiland 镜头同处方、同结构的 ZosBackend；镜头版本不变时复用，变了就在同一个实例里重建镜头。"""
        source = self._active
        if not isinstance(source, OptilandBackend):
            raise SaosError(ErrorCode.BAD_ARGS, "the OpticStudio twin exists only while Optiland is active")
        key = (id(source), source._revision, source.current_configuration())
        if self._twin is None:
            from saos.backend.zos import ZosBackend

            twin = ZosBackend(mode="standalone")
            try:
                self._twin_info = twin.start()
            except BaseException:
                twin.close()
                raise
            self._twin, self._twin_key = twin, None
        if self._twin_key != key:
            self._twin_key = None
            self._twin.build(source._require())
            if key[2] != 1:
                self._twin.select_configuration(key[2])
            self._twin_key = key
        self._note(reason)
        return self._twin

    def _native_or_twin(self, method: str, reason: str, *args, **kwargs):
        try:
            return getattr(self._active, method)(*args, **kwargs)
        except SaosError as exc:
            if exc.code != ErrorCode.BACKEND_UNSUPPORTED or not isinstance(self._active, OptilandBackend):
                raise
            return getattr(self.twin(f"{reason}: {exc.message}"), method)(*args, **kwargs)

    def operand(self, name: str, int1: int, int2: int, data: list[float]) -> float:
        return self._native_or_twin("operand", f"operand {str(name).upper()} computed by the OpticStudio twin", name, int1, int2, data)

    def analysis(self, name: str, settings: dict[str, Any], text_path: Path, keep_open: bool = False) -> dict[str, Any]:
        return self._native_or_twin("analysis", f"analysis {name} computed by the OpticStudio twin", name, settings, text_path)

    def zernike(self, **kwargs) -> dict[int, float]:
        return self._native_or_twin("zernike", "Zernike fit computed by the OpticStudio twin", **kwargs)

    # ---- 文件 ----

    def build(self, prescription: dict[str, Any]) -> None:
        capability = OptilandBackend.can_handle(prescription)
        try:
            self._replace(capability["reasons"], lambda backend: backend.build(prescription), native=capability["supported"])
        except SaosError as exc:
            if not capability["supported"] or exc.code != ErrorCode.BACKEND_UNSUPPORTED:
                raise
            self._replace([exc.message], lambda backend: backend.build(prescription), native=False)

    def load(self, path: Path) -> None:
        payload = lens_files.read_companion(path)
        if payload is None:
            reasons = ["a .zmx without its .optiland.json companion is opened in OpticStudio"]
        else:
            reasons = OptilandBackend.can_handle(payload["prescription"])["reasons"]
        try:
            self._replace(reasons, lambda backend: backend.load(path), native=not reasons)
        except SaosError as exc:
            if reasons or exc.code != ErrorCode.BACKEND_UNSUPPORTED:
                raise
            self._replace([exc.message], lambda backend: backend.load(path), native=False)

    def save_as(self, path: Path) -> None:
        active = self._active
        if not isinstance(active, OptilandBackend):
            active.save_as(path)
            return
        try:
            active.save_as(path)
            return
        except SaosError as exc:
            if exc.code != ErrorCode.BACKEND_UNSUPPORTED:
                raise
            reason = f".zmx written by the OpticStudio twin: {exc.message}"
        folder = Path(tempfile.mkdtemp(prefix="saos-twin-save-"))
        try:
            scratch = folder / path.name
            self.twin(reason).save_as(scratch)
            active.save_as(path, primary=scratch.read_bytes())
        finally:
            shutil.rmtree(folder, ignore_errors=True)

    # ---- 建模说明与双端对齐 ----

    def parity_check(self) -> dict[str, Any]:
        source = self._active
        if not isinstance(source, OptilandBackend):
            return {"status": "not_applicable", "reason": "the lens is open in OpticStudio"}
        try:
            twin = self.twin("describe: dual-backend baseline check")
            reference = baseline(twin)
        except SaosError as exc:
            return {"status": "skipped", "reason": f"OpticStudio is unavailable for the comparison: {exc.message}"}
        start = source._aiming
        modes = [start] + (["iterative"] if start == "robust" else [])
        attempts, passed, mine = [], False, None
        for mode in modes:
            if mode != source._aiming:
                source.set_aiming(mode)
            mine = baseline(source)
            diffs, passed = compare(mine, reference)
            attempts.append({"aiming": mode, "pass": passed, "differences": diffs})
            if passed:
                break
        if not passed and source._aiming != start:
            source.set_aiming(start)
            mine = baseline(source)
        return {"status": "pass" if passed else "mismatch", "aiming": source._aiming, "tolerances": dict(TOLERANCES),
                "zos_version": self._twin_info.get("version"),
                "optiland": mine, "zos": reference, "attempts": attempts}

    def describe_model(self, folder: Path | None, prefix: str = "layout") -> dict[str, Any]:
        if not isinstance(self._active, OptilandBackend):
            desc = self._active.describe_model(folder, prefix)
            desc["parity_check"] = {"status": "not_applicable", "reason": "the lens is open in OpticStudio"}
            return desc
        parity = self.parity_check()
        desc = self._active.describe_model(folder, prefix)
        desc["parity_check"] = parity
        if folder is not None:
            # 布局图由 OpticStudio 孪生实例导出（Optiland 侧的图形导出在 Plan 4 的工作台里）
            try:
                desc["layout"] = self.twin("describe: layout pictures")._export_layouts(folder, prefix)
            except SaosError as exc:
                desc["layout"] = {"Layout": {"error": exc.message}}
        return desc

    def system_info(self) -> dict[str, Any]:
        info = {**self._active.system_info(), "backend": self._info.get("backend", "zos"),
                "fallback_reasons": list(self._info.get("fallback_reasons", [])), "fallback_history": list(self._history)}
        if self._info.get("untranslated"):
            info["untranslated"] = list(self._info["untranslated"])
        return info
