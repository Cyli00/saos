"""rtc 探索后端：在内存里持有 OpticStudio 存出的 .zmx，供脚本做高频追迹（ADR-0014）。
The rtc exploration backend: an in-memory copy of a lens OpticStudio saved, for scripts that trace at high rates.

数值来自 rtc（ray tracing core，float64 序列追迹，与 saos 并列的 `../rtc`），只用于探索：判定、轮次、报告仍由 OpticStudio 计算。
建立时用 OpticStudio 自己的描述（describe_model）逐项核对：面型、参数、各波长折射率（INDX）、视场、波长、孔径和
瞄准方式；rtc 复现不了的设置一律报 BACKEND_UNSUPPORTED，不返回近似值。
Numbers come from rtc and are for exploration only; checks, rounds and reports stay on OpticStudio. Building one
compares it item by item with OpticStudio's own description; settings rtc cannot reproduce raise BACKEND_UNSUPPORTED.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from saos.backend.base import Backend, check_size, parse_numeric
from saos.protocol import ErrorCode, SaosError

# 求解类型里 rtc 当作普通数值的两种；其余求解（拾取、边缘光线高度、F 数…）改别的参数后 OpticStudio 会重算，rtc 不会
# the two solve types rtc treats as plain numbers; other solves are recomputed by OpticStudio after a change, not by rtc
PLAIN_SOLVES = ("Fixed", "Variable")
SURFACE_APERTURES = {"None": 0, "CircularAperture": 1, "FloatingAperture": 10}
# 与 OpticStudio 逐条追迹对过账的面型（.zmx 类型码）；rtc 另有的 BINARY_2、FRESNELS 尚未对账，一律拒绝
# surface types reconciled ray by ray with OpticStudio; rtc's BINARY_2 and FRESNELS are not, so they are refused
RECONCILED_TYPES = ("STANDARD", "EVENASPH", "BICONIC", "BICONICX", "BICONICZ", "TOROIDAL", "PARAXIAL")
GEOMETRY_TOL = 1e-9
INDEX_TOL = 1e-8
# 已与 OpticStudio 对过账的材料计算路径；见文件内说明
ADMITTED_FILE = Path(__file__).with_name("rtc_admitted.json")


@lru_cache(maxsize=1)
def admitted_paths() -> frozenset[str]:
    return frozenset(json.loads(ADMITTED_FILE.read_text(encoding="utf-8"))["paths"])


def catalog_indices(names, catalogs, glass_dir: Path | None, environment: tuple[float, float, bool],
                    wavelengths_um) -> tuple[dict[str, list[float]], dict[str, str]]:
    """不建镜头，按准入表在 rtc 材料层算目录玻璃在各波长的折射率（与 OpticStudio INDX 差 ≤ 1e-8 的路径才算）。
    environment 是 (温度 °C, 压力 atm, 是否按环境修正)。返回 {牌号: [n…]} 与 {牌号: 没算的原因}，后者交给 OpticStudio。
    Catalog indices from rtc's material layer for admitted paths only, without a lens; the rest is left to OpticStudio."""
    from rtc.material import Catalog, Environment, GlassCatalog, MaterialInvalid, MaterialUnsupported, resolve

    catalog = GlassCatalog(glass_dir) if glass_dir else GlassCatalog()
    env = _material_call(Environment, *environment)
    values, skipped = {}, {}
    for name in names:
        try:
            medium = resolve(Catalog(name), catalog, list(catalogs))
            path = medium.path(env)
            if path not in admitted_paths():
                skipped[name] = f"material path {path} is not reconciled with OpticStudio INDX"
                continue
            values[name] = [float(medium.index(w, env)) for w in wavelengths_um]
        except (MaterialInvalid, MaterialUnsupported) as exc:
            skipped[name] = str(exc)
    return values, skipped


def _unsupported(message: str, problems: list[str] | None = None) -> SaosError:
    return SaosError(ErrorCode.BACKEND_UNSUPPORTED, message, {"problems": problems} if problems else None)


def _material_call(operation, *args, **kwargs):
    """材料层错误只在这里转成 saos 错误：参数本身无效报 BAD_ARGS，rtc 复现不了报 BACKEND_UNSUPPORTED。"""
    from rtc.material import MaterialInvalid, MaterialUnsupported

    try:
        return operation(*args, **kwargs)
    except MaterialInvalid as exc:
        raise SaosError(ErrorCode.BAD_ARGS, str(exc)) from exc
    except MaterialUnsupported as exc:
        raise _unsupported(str(exc)) from exc


def _same(a: float, b: float, tol: float = GEOMETRY_TOL) -> bool:
    if math.isinf(a) or math.isinf(b):
        return a == b
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


class RtcBackend(Backend):
    """一份镜头在 rtc 里的内存模型。model 是同一镜头的 ZosBackend.describe_model(None)；给出时逐项核对。
    freeze_solves=False 时镜头带有 rtc 不重算的求解就拒绝改参数，True 时这些求解保持存盘时的值。
    An in-memory rtc model of one lens. model is ZosBackend.describe_model(None) of the same lens and is compared
    item by item. With freeze_solves False, parameter changes are refused while the lens holds solves rtc does not
    recompute; with True those solved values stay as saved."""

    def __init__(self, path: Path, model: dict[str, Any] | None = None, *, glass_dir: Path | None = None,
                 freeze_solves: bool = False):
        from rtc import materials, zmx
        from rtc.material import MaterialInvalid, MaterialUnsupported

        self.path = Path(path)
        try:
            self.lens = zmx.parse(self.path)
        except OSError as exc:
            raise SaosError(ErrorCode.FILE_NOT_FOUND, f"cannot read {self.path}: {exc}") from exc
        except (MaterialInvalid, MaterialUnsupported) as exc:
            # 文件内容不是脚本参数：材料或环境行读不了一律算 rtc 复现不了
            raise _unsupported(f"rtc cannot read the materials or environment of {self.path}: {exc}") from exc
        self.glasses = materials.Catalog(glass_dir) if glass_dir else None
        self.model = model
        self.freeze_solves = bool(freeze_solves)
        self.config = self.lens.current_config
        self._systems: dict[tuple[int, int], Any] = {}
        # 介质与折射率缓存，跨改参数后的系统重建复用（环境与玻璃库在副本里不变）
        self._media: dict = {}
        # 建立时 OpticStudio INDX 逐值确认过的 (材料描述, 波长)：未准入的材料路径只在这些点上可用
        self.confirmed: set = set()
        # 被求解锁住的参数：键 → 求解类型 / parameters held by a solve: key → solve type
        self.held: dict[str, str] = {}
        problems = self._file_problems()
        if model is not None:
            problems += self._model_problems(model)
        if problems:
            raise _unsupported("rtc cannot reproduce this lens: " + "; ".join(problems), problems)
        if model is not None:
            mismatches = self._compare(model)
            if mismatches:
                raise SaosError(ErrorCode.VERIFY_FAILED, "the rtc copy does not match OpticStudio: " + "; ".join(mismatches[:8]),
                                {"mismatches": mismatches})
        # 材料准入要用到上面的逐值确认，所以各波长系统最后才建
        problems = self._system_problems()
        if problems:
            raise _unsupported("rtc cannot reproduce this lens: " + "; ".join(problems), problems)

    # ---- 适用范围 / what rtc reproduces ----

    def _file_problems(self) -> list[str]:
        from rtc import geometry

        lens = self.lens
        out = []
        if lens.units.upper() != "MM":
            out.append(f"lens units {lens.units} (rtc works in mm)")
        mode = lens.raw.get("MODE", "MODE SEQ").split()
        if len(mode) > 1 and mode[1].upper() != "SEQ":
            out.append("not a sequential system")
        if lens.aperture_type != "EPD":
            # 2026-10-26 实测：piB（物方 NA + 实际瞄准）10 条光线 9 条瞄准不收敛，位置差 0.096 mm
            out.append("system aperture other than entrance pupil diameter (rtc's object space NA launch and aiming do not "
                       "match OpticStudio)")
        if lens.field_type != "ANGLE":
            out.append(f"field type {lens.field_type} (rtc traces angle fields only)")
        if lens.ray_aiming == 1:
            out.append("paraxial ray aiming (rtc has off and real)")
        if lens.aperture_type == "EPD" and math.isfinite(lens.surface(0).thickness):
            out.append("finite object distance with an entrance pupil aperture")
        if "ENVD" not in lens.raw:
            out.append("missing ENVD (the environment cannot be checked)")
        if not lens.active_wavelengths:
            out.append("no active wavelengths")
        if lens.surface(0).material is not None:
            out.append("rtc assumes air in object space")
        others = sorted({row.op for row in lens.mce if row.op not in ("THIC", "MOFF")})
        if others:
            out.append(f"multi-configuration operands {others} (rtc applies THIC only)")
        for s in lens.surfaces[1:]:
            if s.glass.upper() == "MIRROR":
                out.append(f"surface {s.index} is a mirror")
            if s.type.upper() not in RECONCILED_TYPES:
                out.append(f"surface {s.index} type {s.type} (not reconciled with OpticStudio)")
                continue
            try:
                geometry.make(s)
            except NotImplementedError as exc:
                out.append(f"surface {s.index} type {s.type}: {exc}")
        return out

    def _system_problems(self) -> list[str]:
        """逐波长建 rtc 系统：材料准入、折射率、面型参数等在这里暴露。"""
        for wave in range(1, len(self.lens.active_wavelengths) + 1):
            try:
                self._system(wave)
            except SaosError as exc:
                return [f"wavelength {wave}: {exc.message}"]
            except (NotImplementedError, KeyError, ValueError) as exc:
                return [f"wavelength {wave}: {exc}"]
        return []

    def _model_problems(self, model: dict[str, Any]) -> list[str]:
        out = []
        if model.get("mode") == "non_sequential":
            return ["non-sequential system"]
        ap = model.get("aperture", {})
        if ap.get("type") != "EntrancePupilDiameter":
            out.append(f"system aperture {ap.get('type')}")
        if ap.get("telecentric_object"):
            out.append("telecentric object space")
        if model.get("ray_aiming") not in ("Off", "Real"):
            out.append(f"ray aiming {model.get('ray_aiming')}")
        fields = model.get("fields", {})
        if fields.get("type") != "Angle":
            out.append(f"field type {fields.get('type')}")
        if any(any(v for v in f.get("vignetting", {}).values()) for f in fields.get("items", [])):
            out.append("field vignetting factors (VDX/VDY/VCX/VCY/VAN)")
        operands = {op.get("type") for op in model.get("configurations", {}).get("operands", [])}
        if operands - {"THIC"}:
            out.append(f"multi-configuration operands {sorted(operands - {'THIC'})}")
        for s in model.get("surfaces", []):
            i = int(s["index"])
            if s.get("tilt_decenter"):
                out.append(f"surface {i} tilt/decenter")
            kind = (s.get("aperture") or {}).get("type", "None")
            if kind not in SURFACE_APERTURES:
                out.append(f"surface {i} aperture {kind}")
            elif (s.get("aperture") or {}).get("picked_up_from"):
                out.append(f"surface {i} aperture picked up")
            solves = {**(s.get("solves") or {}), **{f"par{p['par']}": p.get("solve", "Fixed") for p in s.get("params", [])}}
            for name, solve in solves.items():
                if name in ("material", "semi_diameter", "mech_semi_diameter"):
                    continue
                if solve not in PLAIN_SOLVES:
                    self.held[f"{i}.{name}"] = solve
            if kind == "FloatingAperture" and (s.get("solves") or {}).get("semi_diameter") not in PLAIN_SOLVES:
                self.held[f"{i}.semi_diameter"] = str((s.get("solves") or {}).get("semi_diameter"))
            if (s.get("solves") or {}).get("material") not in (None, "Fixed", "Variable", "MaterialModel", "MaterialSubstitute"):
                out.append(f"surface {i} material solve {s['solves']['material']}")
        return out

    def _compare(self, model: dict[str, Any]) -> list[str]:
        """逐项比较存盘副本与 OpticStudio 的描述；返回不一致的条目。/ Compare the copy with OpticStudio's description."""
        lens = self.lens
        out = []
        environment = model.get("environment")
        if not isinstance(environment, dict):
            out.append("OpticStudio environment is missing")
        else:
            actual = lens.environment
            for name in ("temperature", "pressure"):
                value = environment.get(name)
                if not isinstance(value, (int, float)) or not _same(float(value), getattr(actual, name)):
                    out.append(f"environment {name}: OpticStudio {value}, file {getattr(actual, name)}")
            if environment.get("adjust_index") is not actual.adjust:
                out.append(f"environment adjust_index: OpticStudio {environment.get('adjust_index')}, file {actual.adjust}")
        ap = model.get("aperture", {})
        want_ap = {"EntrancePupilDiameter": "EPD", "ObjectSpaceNA": "OBNA"}.get(ap.get("type"))
        if want_ap != lens.aperture_type or not _same(float(ap.get("value", 0.0)), lens.aperture_value):
            out.append(f"aperture {ap.get('type')} {ap.get('value')} vs file {lens.aperture_type} {lens.aperture_value}")
        if {"Off": 0, "Real": 2}.get(model.get("ray_aiming")) != lens.ray_aiming:
            out.append(f"ray aiming {model.get('ray_aiming')} vs file {lens.ray_aiming}")
        fields = model.get("fields", {})
        if str(fields.get("normalization", "")).lower() != lens.field_normalization:
            out.append(f"field normalization {fields.get('normalization')} vs file {lens.field_normalization}")
        items = fields.get("items", [])
        if len(items) != len(lens.fields) or not all(_same(f["x"], x) and _same(f["y"], y) for f, (x, y, _) in zip(items, lens.fields)):
            out.append("field points differ from the file")
        waves = model.get("wavelengths", [])
        if len(waves) != len(lens.active_wavelengths) or not all(_same(w["um"], um) for w, (um, _) in zip(waves, lens.active_wavelengths)):
            out.append("wavelengths differ from the file")
        primary = next((k for k, w in enumerate(waves, 1) if w.get("primary")), 1)
        if primary != lens.primary_wave:
            out.append(f"primary wavelength {primary} vs file {lens.primary_wave}")
        surfaces = model.get("surfaces", [])
        if len(surfaces) != len(lens.surfaces):
            return out + [f"{len(surfaces)} surfaces in OpticStudio, {len(lens.surfaces)} in the file"]
        stop = model.get("stop_surface")
        if stop is not None and stop != lens.stop_index:
            out.append(f"stop surface {stop} vs file {lens.stop_index}")
        for s in surfaces:
            i = int(s["index"])
            mine = lens.surface(i)
            # 像面之后没有介质，存盘时写 INFINITY、OpticStudio 读作 0，不参与追迹
            # nothing follows the image surface: saved as INFINITY, read as 0 by OpticStudio, never traced
            names = ("radius", "conic") if i == len(surfaces) - 1 else ("radius", "thickness", "conic")
            if mine.type == "PARAXIAL":
                # 近轴面没有圆锥系数：ZOS-API 读作 inf、存盘为 0，追迹也不用它
                # a paraxial surface has no conic: the ZOS-API reads inf, the file holds 0, and tracing ignores it
                names = tuple(n for n in names if n != "conic")
            for name in names:
                got, want = float(self.get_param(f"{i}.{name}")), float(s[name])
                if name == "radius":
                    # 平面在 OpticStudio 里读作 inf 或 0 / a flat reads as inf or 0 in OpticStudio
                    got, want = (math.inf if v == 0 else v for v in (got, want))
                if not _same(want, got):
                    out.append(f"surface {i} {name} {s[name]} vs {got}")
            for p in s.get("params", []):
                if isinstance(p.get("value"), (int, float)) and not _same(float(p["value"]), float(mine.params.get(int(p["par"]), 0.0))):
                    out.append(f"surface {i} par{p['par']} {p['value']} vs {mine.params.get(int(p['par']), 0.0)}")
            kind = (s.get("aperture") or {}).get("type", "None")
            if SURFACE_APERTURES.get(kind, -1) != mine.aperture_code:
                out.append(f"surface {i} aperture {kind} vs file code {mine.aperture_code}")
            if i == 0 or i == len(surfaces) - 1:
                continue
            indices = s.get("n", [])
            if len(indices) != len(lens.active_wavelengths):
                out.append(f"surface {i}: INDX data does not cover every active wavelength")
                continue
            out += self._confirm_indices(mine, indices)
        object_n = (surfaces[0].get("n") or [1.0])[0] if surfaces else 1.0
        if abs(float(object_n) - 1.0) > INDEX_TOL:
            out.append(f"object space index {object_n} (rtc assumes air)")
        return out

    def _confirm_indices(self, s, indices) -> list[str]:
        """一面的面后介质逐波长与 OpticStudio 的 INDX 比较；一致的 (材料, 波长) 记入 confirmed。
        rtc 算不了的材料不算不一致，留给 _system_problems 报 BACKEND_UNSUPPORTED。"""
        from rtc.material import MaterialInvalid, MaterialUnsupported, resolve

        env = self.lens.environment
        out = []
        try:
            medium = resolve(s.material, self._catalog(), self.lens.catalogs)
        except (MaterialInvalid, MaterialUnsupported):
            return out
        for w, ((um, _), n_zos) in enumerate(zip(self.lens.active_wavelengths, indices), 1):
            try:
                n_rtc = float(medium.index(um, env))
            except (MaterialInvalid, MaterialUnsupported):
                continue
            if abs(float(n_zos) - n_rtc) <= INDEX_TOL:
                self.confirmed.add((s.material, um))
            else:
                out.append(f"surface {s.index} index at wavelength {w}: OpticStudio {n_zos}, rtc {n_rtc}")
        return out

    # ---- rtc 系统 / rtc systems ----

    def _catalog(self):
        from rtc.system import catalog

        return self.glasses if self.glasses is not None else catalog()

    def _admit(self, medium, spec, wavelength: float) -> None:
        """材料路径已准入，或这一 (材料, 波长) 建立时经 OpticStudio INDX 确认过，才允许参与追迹。"""
        path = medium.path(self.lens.environment)
        if path not in admitted_paths() and (spec, wavelength) not in self.confirmed:
            raise _unsupported(f"material path {path} ({spec}) at {wavelength} µm is not reconciled with OpticStudio INDX yet; "
                               "run saos.rtc_golden for it or use OpticStudio")

    def _system(self, wave: int):
        from rtc.material import resolve
        from rtc.system import OpticalSystem

        key = (wave, self.config)
        if key not in self._systems:
            um = self.lens.active_wavelengths[wave - 1][0]
            for s in self.lens.surfaces[1:]:
                self._admit(_material_call(resolve, s.material, self._catalog(), self.lens.catalogs), s.material, um)
            # 非主波长系统借主波长系统取光阑半径，免得每次各自重建一份
            primary = self._system(self.lens.primary_wave) if wave != self.lens.primary_wave else None
            self._systems[key] = _material_call(OpticalSystem, self.lens, wavelength_um=um, config=self.config,
                                                glasses=self.glasses, media=self._media, primary=primary)
        return self._systems[key]

    def _changed(self) -> None:
        self._systems.clear()

    def optical_system(self, wave: int):
        """第 wave 个波长的 rtc 光学系统（材料已过准入），给 rtc.adtrace 这类直接用内核的探索代码。
        The rtc optical system of wavelength wave (materials admitted), for exploration code using the kernel directly."""
        return self._system(wave)

    # ---- Backend 接口 / the Backend interface ----

    def start(self) -> dict[str, Any]:
        return self.identity()

    def close(self) -> None:
        self._systems.clear()

    def identity(self) -> dict[str, Any]:
        import rtc

        return {"backend": "rtc", "version": rtc.__version__, "precision": "float64"}

    def healthy(self) -> bool:
        return True

    def load(self, path: Path) -> None:
        self.__init__(path, None, glass_dir=self.glasses.folder if self.glasses is not None else None,
                      freeze_solves=self.freeze_solves)

    def save_as(self, path: Path) -> None:
        raise _unsupported("rtc never writes lens files; push the values to OpticStudio with fast.push(ctx) and save there")

    def new_system(self) -> None:
        raise _unsupported("rtc only holds a copy of an OpticStudio lens")

    def system_info(self) -> dict[str, Any]:
        lens = self.lens
        ap = (self.model or {}).get("aperture", {})
        return {
            **self.identity(), "file": str(self.path), "lens_units": "Millimeters",
            "aperture": {"type": {"EPD": "EntrancePupilDiameter", "OBNA": "ObjectSpaceNA"}[lens.aperture_type],
                         "value": lens.aperture_value, "telecentric_object": False, "afocal_image": bool(ap.get("afocal_image"))},
            "fields": {"type": "Angle", "normalization": lens.field_normalization.capitalize(),
                       "items": [{"x": x, "y": y, "weight": w} for x, y, w in lens.fields]},
            "wavelengths": [{"um": um, "weight": w, "primary": k == lens.primary_wave} for k, (um, w) in enumerate(lens.active_wavelengths, 1)],
            "num_surfaces": len(lens.surfaces), "stop_surface": lens.stop_index,
            "environment": {"temperature": lens.environment.temperature, "pressure": lens.environment.pressure,
                            "adjust_index": lens.environment.adjust},
        }

    def surfaces(self) -> list[dict[str, Any]]:
        return [{"index": s.index, "type": s.type, "comment": s.comment, "radius": self._radius(s),
                 "thickness": self.lens.thickness(s.index, self.config), "material": self._material_text(s), "semi_diameter": s.semi_diameter,
                 "mech_semi_diameter": s.mech_semi_diameter, "conic": s.conic, "is_stop": s.is_stop} for s in self.lens.surfaces]

    @staticmethod
    def _radius(s) -> float:
        return math.inf if s.curvature == 0 else 1.0 / s.curvature

    @staticmethod
    def _material(s) -> str | dict[str, float]:
        """get_param 的材料值：目录牌号、空气空串，模型玻璃为 {nd, vd, dpgf}（与 ZOS 后端相同）。"""
        from rtc.material import Model

        spec = s.material
        return {"nd": spec.nd, "vd": spec.vd, "dpgf": spec.dpgf} if isinstance(spec, Model) else s.glass

    @staticmethod
    def _material_text(s) -> str:
        """surfaces() 里的材料文字：模型玻璃写成 "MODEL nd vd dpgf"，调用方可以一律当字符串处理。"""
        from rtc.material import Model

        spec = s.material
        return f"MODEL {spec.nd:.10g} {spec.vd:.10g} {spec.dpgf:.10g}" if isinstance(spec, Model) else s.glass

    def set_surface(self, index: int, param: str, value: Any) -> dict[str, Any]:
        self.set_param(f"{index}.{param}", value)
        return self.surfaces()[index]

    def operand(self, name: str, int1: int, int2: int, data: list[float]) -> float:
        raise _unsupported(f"rtc has no operands ({name}); measure operands on OpticStudio through ctx")

    def configuration_count(self) -> int:
        return max(1, self.lens.num_configs)

    def current_configuration(self) -> int:
        return self.config

    def select_configuration(self, n: int) -> None:
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= self.configuration_count():
            raise SaosError(ErrorCode.BAD_ARGS, f"configuration must be 1–{self.configuration_count()}, got {n!r}")
        if n != self.config:
            self.config = n
            self._changed()

    # ---- 参数 / parameters ----

    def _index(self, ref: Any) -> int:
        from saos.kit import surface

        return surface(SimpleNamespace(backend=self), ref)

    def get_param(self, key: str) -> Any:
        from saos.kit import PAR_RE, parse_key

        kind, ref, param = parse_key(key)
        lens = self.lens
        if kind == "system":
            if ref == "aperture":
                return lens.aperture_value
            if ref == "wave":
                return lens.active_wavelengths[self._slot(param, len(lens.active_wavelengths), "wave")][0]
            if ref in ("field_x", "field_y"):
                return lens.fields[self._slot(param, len(lens.fields), "field")][0 if ref == "field_x" else 1]
            raise _unsupported(f"rtc cannot read {key}")
        if kind != "surface":
            raise _unsupported(f"rtc cannot read {key}")
        s = lens.surface(self._index(ref))
        if param == "radius":
            return self._radius(s)
        if param == "curvature":
            return s.curvature
        if param == "thickness":
            return lens.thickness(s.index, self.config)
        if param in ("conic", "semi_diameter", "mech_semi_diameter", "comment"):
            return getattr(s, param)
        if param == "material":
            return self._material(s)
        if param in ("nd", "vd", "dpgf"):
            from rtc.material import as_model

            spec = _material_call(as_model, s.material, self._catalog(), lens.catalogs)
            return getattr(spec, param)
        m = PAR_RE.match(param)
        if m:
            return float(s.params.get(int(m.group(1)), 0.0))
        raise _unsupported(f"rtc cannot read {param}")

    def set_param(self, key: str, value: Any) -> None:
        from saos.kit import PAR_RE, parse_key

        kind, ref, param = parse_key(key)
        lens = self.lens
        if kind == "system":
            self._guard(key)
            number = parse_numeric(key, value)
            if ref == "aperture":
                lens.aperture_value = number
            elif ref == "wave":
                k = self._slot(param, len(lens.active_wavelengths), "wave")
                self._validate_wavelength(number)
                lens.wavelengths[k] = (number, lens.wavelengths[k][1])
            elif ref in ("field_x", "field_y"):
                k = self._slot(param, len(lens.fields), "field")
                x, y, w = lens.fields[k]
                lens.fields[k] = (number, y, w) if ref == "field_x" else (x, number, w)
            else:
                raise _unsupported(f"rtc cannot write {key} (multi-configuration cells are not supported yet)")
            self._changed()
            return
        if kind != "surface":
            raise _unsupported(f"rtc cannot write {key}")
        index = self._index(ref)
        s = lens.surface(index)
        name = "radius" if param == "curvature" else param
        if f"{index}.{name}" in self.held:
            raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"surface {index} {name} is held by a {self.held[f'{index}.{name}']} solve")
        if param == "comment":
            s.comment = "" if value is None else str(value)
            return
        self._guard(key)
        if param == "material":
            self._set_material(s, value)
        elif param in ("nd", "vd", "dpgf"):
            from rtc.material import as_model

            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise SaosError(ErrorCode.BAD_ARGS, f"{key} needs a finite number")
            spec = _material_call(as_model, s.material, self._catalog(), lens.catalogs)
            self._set_material(s, _material_call(replace, spec, **{param: float(value)}))
        elif param in ("radius", "curvature"):
            number = parse_numeric(param, value)
            s.curvature = (0.0 if math.isinf(number) or number == 0 else 1.0 / number) if param == "radius" else number
        elif param == "thickness":
            if any(r.op == "THIC" and r.surface == index and r.config == self.config for r in lens.mce):
                raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"surface {index} thickness comes from a multi-configuration row in configuration {self.config}")
            number = parse_numeric(param, value)
            if index == 0 and math.isfinite(number) and lens.aperture_type == "EPD":
                raise _unsupported("rtc traces an entrance pupil aperture with the object at infinity only")
            s.thickness = number
        elif param in ("conic", "semi_diameter", "mech_semi_diameter"):
            number = parse_numeric(param, value)
            check_size(param, number)
            setattr(s, param, number)
        elif PAR_RE.match(param):
            n = int(PAR_RE.match(param).group(1))
            s.params[n] = parse_numeric(param, value)
            if n > 12:
                s.extra[n - 12] = s.params[n]
        else:
            raise _unsupported(f"rtc cannot write {param}")
        self._changed()

    def _guard(self, key: str) -> None:
        if self.held and not self.freeze_solves:
            raise _unsupported(f"cannot change {key}: the lens has solves rtc does not recompute ({self.held}); "
                               "pass freeze_solves=True to keep their saved values fixed in rtc, or make them fixed")

    def _set_material(self, s, value: Any) -> None:
        from rtc.material import material_spec, resolve

        spec = _material_call(material_spec, value)
        if s.index == 0 and spec is not None:
            raise _unsupported("rtc assumes air in object space")
        medium = _material_call(resolve, spec, self._catalog(), self.lens.catalogs)
        # 先查准入、算全部有效波长再提交，失败时材料和系统缓存都不变
        for wavelength, _ in self.lens.active_wavelengths:
            self._admit(medium, spec, wavelength)
            _material_call(medium.index, wavelength, self.lens.environment)
        s.material = spec

    def _validate_wavelength(self, wavelength: float) -> None:
        from rtc.material import resolve

        for s in self.lens.surfaces[1:]:
            medium = _material_call(resolve, s.material, self._catalog(), self.lens.catalogs)
            self._admit(medium, s.material, wavelength)
            _material_call(medium.index, wavelength, self.lens.environment)

    @staticmethod
    def _slot(param: str, count: int, label: str) -> int:
        k = int(param)
        if not 1 <= k <= count:
            raise SaosError(ErrorCode.BAD_ARGS, f"{label} {k} out of range 1–{count}")
        return k - 1

    # ---- 追迹 / tracing ----

    def trace_batch(self, rays, to: int):
        from rtc.trace import trace

        from saos.kit import Rays

        arr = np.asarray(list(rays) if not isinstance(rays, np.ndarray) else rays, dtype=float).reshape(-1, 5)
        if not 1 <= to < len(self.lens.surfaces):
            raise SaosError(ErrorCode.BAD_ARGS, f"rtc traces to surfaces 1–{len(self.lens.surfaces) - 1}, got {to}")
        out = np.zeros((len(arr), 9))
        waves = arr[:, 0].astype(int)
        count = len(self.lens.active_wavelengths)
        for w in np.unique(waves):
            if not 1 <= w <= count:
                raise SaosError(ErrorCode.BAD_ARGS, f"wavelength {w} out of range 1–{count}")
            sel = waves == w
            system = self._system(int(w))
            try:
                block = trace(system, arr[sel, 3], arr[sel, 4], arr[sel, 1], arr[sel, 2])
            except NotImplementedError as exc:
                raise _unsupported(f"rtc: {exc}") from exc
            k = block.surface_index.index(to)
            # 只有目标面及其之前的错误和渐晕才算数，与 OpticStudio 追到目标面为止一致
            # only errors and vignetting up to the target surface count, as OpticStudio traces no further
            error = np.where((np.abs(block.error) > 0) & (np.abs(block.error) <= to), block.error, 0)
            vig = np.where((block.vig > 0) & (block.vig <= to), block.vig, 0)
            rows = np.column_stack([error, vig, block.x[:, k], block.y[:, k], block.z[:, k], block.l[:, k], block.m[:, k],
                                    block.n[:, k], np.where(error == 0, 1.0, 0.0)])
            rows[error != 0, 2:8] = np.nan
            out[sel] = rows
        return Rays.from_rows(arr, out, to)
