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
# rtc 能算的操作数；参数约定同 OpticStudio 的评价函数（Int1、Int2、Hx、Hy、Px、Py…）
# operands rtc computes, with OpticStudio's merit-function argument conventions
RAY_OPERANDS = frozenset({"REAX", "REAY", "REAZ", "REAR", "REAA", "REAB", "REAC", "RANG", "RAID",
                          "RAGX", "RAGY", "RAGZ", "RAGA", "RAGB", "RAGC"})
SYSTEM_OPERANDS = frozenset({"EFFL", "WFNO", "ISFN", "TOTR", "TTHI", "EPDI", "ENPP", "EXPP", "EXPD", "PETZ", "INDX", "SSAG", "CONF"})
# 其中已与 OpticStudio 批量追迹或操作数实测逐值对过账的（rtc.operands 的说明）；其余按手册定义实现
# those reconciled value by value with OpticStudio (see rtc.operands); the rest follow the manual's definitions
RECONCILED_OPERANDS = frozenset({"REAX", "REAY", "REAC", "INDX", "SSAG", "EXPP", "EXPD", "CONF"})
IDENTITY = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]


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

    def __init__(self, path: Path | None, model: dict[str, Any] | None = None, *, glass_dir: Path | None = None,
                 freeze_solves: bool = False, standalone: bool = False):
        from rtc import materials, zmx
        from rtc.material import MaterialInvalid, MaterialUnsupported

        # standalone：SAOS_BACKEND=rtc 的实例自己持有镜头（处方搭建、读写 .zmx），没有 OpticStudio 可对照；
        # 按手册定义实现、未对账的路径照算，用到的列进 unverified()
        # standalone: the SAOS_BACKEND=rtc instance owns its lens (built from prescriptions, .zmx read and written) with no
        # OpticStudio to compare against; paths implemented from the manual but not reconciled run and are listed by unverified()
        self.standalone = bool(standalone)
        # standalone 镜头改过、还没存盘 / a standalone lens changed since it was last saved or loaded
        self._dirty = False
        # 本次脚本用到的操作数，provenance() 取走后清空 / operands used since provenance() was last taken
        self._used: set[str] = set()
        self.glasses = materials.Catalog(glass_dir) if glass_dir else None
        self.model = model
        self.freeze_solves = bool(freeze_solves)
        self._systems: dict[tuple[int, int], Any] = {}
        # 介质与折射率缓存，跨改参数后的系统重建复用（环境与玻璃库在副本里不变）
        self._media: dict = {}
        # 建立时 OpticStudio INDX 逐值确认过的 (材料描述, 波长)：未准入的材料路径只在这些点上可用
        self.confirmed: set = set()
        # 被求解锁住的参数：键 → 求解类型 / parameters held by a solve: key → solve type
        self.held: dict[str, str] = {}
        # standalone 时用到的未准入材料路径 / material paths outside the admitted table used by a standalone instance
        self.unverified_paths: set[str] = set()
        self.path = Path(path) if path is not None else None
        self.lens = None
        self.config = 1
        if path is None:
            if not self.standalone:
                raise SaosError(ErrorCode.BAD_ARGS, "an rtc copy needs a lens file")
            return
        try:
            lens = zmx.parse(self.path)
        except OSError as exc:
            raise SaosError(ErrorCode.FILE_NOT_FOUND, f"cannot read {self.path}: {exc}") from exc
        except (MaterialInvalid, MaterialUnsupported) as exc:
            # 文件内容不是脚本参数：材料或环境行读不了一律算 rtc 复现不了
            raise _unsupported(f"rtc cannot read the materials or environment of {self.path}: {exc}") from exc
        self._adopt(lens)

    def _adopt(self, lens) -> None:
        """核对并接手一份 rtc 镜头：适用范围、与 OpticStudio 描述的逐项比较（有 model 时）、逐波长建系统；
        standalone 时再重算求解与自动半口径。失败时不留下半接手的镜头。
        Check and take over an rtc lens: scope, item-by-item comparison with OpticStudio's description (with a model),
        one system per wavelength; standalone also recomputes solves and automatic semi-diameters. Nothing is kept on failure."""
        previous = (self.lens, self.config, dict(self.held), set(self.unverified_paths))
        self.lens, self.config, self.held = lens, lens.current_config, {}
        self.unverified_paths.clear()
        self._systems.clear()
        try:
            self._check_lens()
            if self.standalone:
                self._recompute()
        except BaseException:
            self.lens, self.config, self.held, self.unverified_paths = previous
            self._systems.clear()
            raise

    def _check_lens(self) -> None:
        model = self.model
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
        finite = math.isfinite(lens.surface(0).thickness)
        if self.standalone:
            if lens.aperture_type not in ("EPD", "OBNA"):
                out.append(f"system aperture {lens.aperture_type} (rtc has entrance pupil diameter and object space NA)")
            if lens.field_type not in ("ANGLE", "OP1") or (lens.field_type == "OP1" and not finite):
                out.append(f"field type {lens.field_type} (rtc has angles, and object heights with a finite object)")
            if lens.aperture_type == "OBNA" and not finite:
                out.append("object space NA with the object at infinity")
        else:
            if lens.aperture_type != "EPD":
                # 2026-10-26 实测：piB（物方 NA + 实际瞄准）10 条光线 9 条瞄准不收敛，位置差 0.096 mm
                out.append("system aperture other than entrance pupil diameter (rtc's object space NA launch and aiming do not "
                           "match OpticStudio)")
            if lens.field_type != "ANGLE":
                out.append(f"field type {lens.field_type} (rtc traces angle fields only)")
            if lens.ray_aiming == 1:
                out.append("paraxial ray aiming (rtc has off and real)")
            if lens.aperture_type == "EPD" and finite:
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
        if path not in admitted_paths() and (spec, wavelength) not in self.confirmed and self.standalone:
            self.unverified_paths.add(path)
            return
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
                                                glasses=self.glasses, media=self._media, primary=primary,
                                                allow_unverified=self.standalone)
        return self._systems[key]

    def _changed(self, edit: bool = True) -> None:
        self._systems.clear()
        if self.standalone and self.lens is not None:
            if edit:
                self._dirty = True
            self._recompute()

    # ---- standalone：OpticStudio 会重算的量与来源记录 / what OpticStudio recomputes, and provenance ----

    def _need_lens(self):
        if self.lens is None:
            raise SaosError(ErrorCode.NO_LENS_LOADED, "the rtc instance holds no lens; build or load one first")
        return self.lens

    def _recompute(self) -> None:
        """改参数后 OpticStudio 会替用户重算的量：近轴边缘光线高度求解（按面序，前一个改了后面的光线就变），
        再按各视场、各波长的光瞳边缘光线定自动半口径（机械半口径未固定时跟随）。
        What OpticStudio recomputes after a change: paraxial marginal-ray-height solves in surface order, then automatic
        semi-diameters from the pupil-rim rays of every field and wavelength (and unfixed mechanical ones with them)."""
        from rtc.trace import trace

        from saos.backend import rtc_lens

        lens = self.lens
        for index, height in rtc_lens.marginal_height_solves(lens):
            if any(r.op == "THIC" and r.surface == index for r in lens.mce):
                raise _unsupported(f"surface {index} has both a marginal ray height solve and a multi-configuration THIC row")
            self._systems.clear()
            system = self._system(lens.primary_wave)
            k = next(k for k, it in enumerate(system.items) if it.index == index)
            value = rtc_lens.solve_thickness(rtc_lens.paraxial_marginal(system), k, height)
            if value is None:
                raise _unsupported(f"surface {index}: the paraxial marginal ray is parallel to the axis, so its height solve has no solution")
            lens.surface(index).thickness = value
            self.held[f"{index}.thickness"] = "MarginalRayHeight"
        self._systems.clear()
        auto = [s for s in lens.surfaces if s.index > 0 and not s.semi_fixed]
        if not auto:
            return

        def rays(wave, hx, hy, px, py):
            try:
                return trace(self._system(wave), px, py, hx, hy)
            except NotImplementedError as exc:
                raise _unsupported(f"rtc: {exc}") from exc

        best = rtc_lens.auto_semi_diameters(self._system(lens.primary_wave), lens, self.config, len(lens.active_wavelengths), rays)
        for surface in auto:
            surface.semi_diameter = best.get(surface.index, 0.0)
            if not surface.mech_fixed:
                surface.mech_semi_diameter = surface.semi_diameter
        self._systems.clear()

    def unverified(self) -> list[str]:
        """本镜头用到的、只按手册定义实现而未与 OpticStudio 对账的计算路径（standalone 才有）。
        Calculation paths of this lens implemented from the manual but not reconciled with OpticStudio (standalone only)."""
        from rtc import geometry

        lens = self.lens
        if lens is None or not self.standalone:
            return []
        out = []
        if math.isfinite(lens.surface(0).thickness):
            out.append("finite object distance: launch from the object and aiming per manual 2.1.1.7")
        if lens.aperture_type == "OBNA":
            out.append("object space NA aperture")
        if lens.field_type == "OP1":
            out.append("object height fields")
        if lens.ray_aiming == 1:
            out.append("paraxial ray aiming")
        for surface in lens.surfaces[1:]:
            shape = geometry.make(surface)
            if not getattr(shape, "verified", False):
                out.append(f"surface {surface.index} type {surface.type}")
        if any(s.index > 0 and not s.semi_fixed for s in lens.surfaces):
            out.append("automatic semi-diameters (pupil-rim rays of every field and wavelength)")
        if any("MAZH" in s.raw for s in lens.surfaces):
            out.append("paraxial marginal ray height solves")
        out += [f"material path {path}" for path in sorted(self.unverified_paths)]
        return out

    def provenance(self) -> dict[str, Any]:
        """脚本结果的来源记录：后端身份、未对账的计算路径、用到的未对账操作数；取走后清空操作数记录。
        Provenance for a script result: backend identity, unreconciled paths, unreconciled operands used (then cleared)."""
        used, self._used = sorted(self._used), set()
        return {**self.identity(), "unverified": self.unverified(),
                "operands_unverified": [op for op in used if op not in RECONCILED_OPERANDS]}

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
        if not self.standalone:
            self.__init__(path, None, glass_dir=self.glasses.folder if self.glasses is not None else None,
                          freeze_solves=self.freeze_solves)
            return
        from rtc import zmx
        from rtc.material import MaterialInvalid, MaterialUnsupported

        try:
            lens = zmx.parse(Path(path))
        except OSError as exc:
            raise SaosError(ErrorCode.FILE_NOT_FOUND, f"cannot read {path}: {exc}") from exc
        except (MaterialInvalid, MaterialUnsupported) as exc:
            raise _unsupported(f"rtc cannot read the materials or environment of {path}: {exc}") from exc
        self._adopt(lens)
        self.path, self._dirty = Path(path), False

    def save_as(self, path: Path) -> None:
        if not self.standalone:
            raise _unsupported("rtc never writes lens files; push the values to OpticStudio with fast.push(ctx) and save there")
        from saos.backend import rtc_lens

        rtc_lens.write(self._need_lens(), Path(path))
        self.path, self._dirty = Path(path), False

    def new_system(self) -> None:
        if not self.standalone:
            raise _unsupported("rtc only holds a copy of an OpticStudio lens")
        self.lens, self.path, self._dirty = None, None, False
        self.held, self._systems = {}, {}

    def build(self, prescription: dict[str, Any]) -> None:
        """standalone：按规范化处方搭建镜头（rtc 复现不了或写不出的项一次全部列出，BACKEND_UNSUPPORTED）。"""
        if not self.standalone:
            raise _unsupported("an rtc copy cannot be rebuilt; build the lens on OpticStudio")
        from saos.backend import rtc_lens

        self._adopt(rtc_lens.from_prescription(prescription))
        self.path, self._dirty = None, True

    def needs_save(self) -> bool:
        return self._dirty

    def has_unsaved_changes(self) -> bool:
        return False

    def describe_model(self, folder: Path | None, prefix: str = "layout") -> dict[str, Any]:
        raise _unsupported("model descriptions are measured on OpticStudio; open the lens in an OpticStudio instance for saos lens describe")

    def analysis_types(self) -> list[str]:
        raise _unsupported("rtc has no OpticStudio analyses; use ctx.spot, ctx.trace and the operands rtc computes")

    def analysis(self, name: str, settings: dict[str, Any], text_path: Path, keep_open: bool = False) -> dict[str, Any]:
        raise _unsupported(f"rtc has no OpticStudio analysis {name}; use ctx.spot, ctx.trace and the operands rtc computes")

    def system_info(self) -> dict[str, Any]:
        lens = self._need_lens()
        ap = (self.model or {}).get("aperture", {})
        return {
            **self.identity(), "file": str(self.path) if self.path else None, "lens_units": "Millimeters", "title": lens.name,
            "mode": "sequential", "object_thickness": lens.surface(0).thickness,
            "aperture": {"type": {"EPD": "EntrancePupilDiameter", "OBNA": "ObjectSpaceNA"}[lens.aperture_type],
                         "value": lens.aperture_value, "telecentric_object": False, "afocal_image": bool(ap.get("afocal_image"))},
            "fields": {"type": {"ANGLE": "Angle", "OP1": "ObjectHeight"}.get(lens.field_type, lens.field_type),
                       "normalization": lens.field_normalization.capitalize(),
                       "items": [{"x": x, "y": y, "weight": w} for x, y, w in lens.fields]},
            "wavelengths": [{"um": um, "weight": w, "primary": k == lens.primary_wave} for k, (um, w) in enumerate(lens.active_wavelengths, 1)],
            "num_surfaces": len(lens.surfaces), "stop_surface": lens.stop_index,
            "environment": {"temperature": lens.environment.temperature, "pressure": lens.environment.pressure,
                            "adjust_index": lens.environment.adjust},
        }

    def surfaces(self) -> list[dict[str, Any]]:
        self._need_lens()
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
        """rtc 能算的操作数（RAY_OPERANDS、SYSTEM_OPERANDS），参数约定同 OpticStudio：光线类 Int1 = 面、Int2 = 波长、
        数据 = Hx、Hy、Px、Py；EFFL、PETZ 的 Int2 = 波长（0 为主波长）；TTHI 的 Int1、Int2 = 首末面；INDX 的 Int1 = 面、
        Int2 = 波长；SSAG 的 Int1 = 面，数据 = X、Y。其余报 BACKEND_UNSUPPORTED。
        The operands rtc computes, with OpticStudio's argument conventions; others raise BACKEND_UNSUPPORTED."""
        from rtc import firstorder
        from rtc.operands import Operands

        lens = self._need_lens()
        op = str(name).upper()
        if op not in RAY_OPERANDS and op not in SYSTEM_OPERANDS:
            raise _unsupported(f"rtc does not compute operand {op}; it has {sorted(RAY_OPERANDS | SYSTEM_OPERANDS)}. "
                               "Measure other operands on an OpticStudio instance")
        self._used.add(op)

        def wave(w: int) -> int:
            w = int(w) or lens.primary_wave
            if not 1 <= w <= len(lens.active_wavelengths):
                raise SaosError(ErrorCode.BAD_ARGS, f"{op}: wavelength {w} out of range 1–{len(lens.active_wavelengths)}")
            return w

        try:
            if op in RAY_OPERANDS:
                return self._ray_operand(op, int(int1), wave(int2), list(data))
            primary = self._system(lens.primary_wave)
            if op == "EFFL":
                return firstorder.effl(self._system(wave(int2)))
            if op == "WFNO":
                return firstorder.wfno(primary)
            if op == "ISFN":
                return firstorder.isfn(primary)
            if op == "TOTR":
                return firstorder.totr(primary)
            if op == "TTHI":
                return firstorder.tthi(lens, int1, int2, self.config)
            if op == "EPDI":
                return firstorder.entrance_pupil_diameter(primary)
            if op == "ENPP":
                return firstorder.entrance_pupil_position(primary)
            if op == "EXPP":
                return float(Operands(primary).expp())
            if op == "EXPD":
                return float(Operands(primary).expd())
            if op == "PETZ":
                return firstorder.petzval_radius(self._system(wave(int2)))
            if op == "INDX":
                if int(int1) == 0:
                    return 1.0
                system = self._system(wave(int2))
                return float(next(it.n_after for it in system.items if it.index == int(self._index(int(int1)))))
            if op == "SSAG":
                x, y = (list(data) + [0.0, 0.0])[:2]
                value = self.sag(self._index(int(int1)), float(x), float(y))
                if value is None:
                    raise SaosError(ErrorCode.BAD_ARGS, f"SSAG: surface {int1} has no sag at ({x}, {y})")
                return value
            if op == "CONF":
                self.select_configuration(int(int1))
                return 0.0
        except NotImplementedError as exc:
            raise _unsupported(f"rtc: {exc}") from exc
        except (ValueError, ZeroDivisionError) as exc:
            raise SaosError(ErrorCode.BAD_ARGS, f"{op}: {exc}") from exc
        raise AssertionError(op)

    def _ray_operand(self, op: str, surf: int, wave: int, data: list[float]) -> float:
        from rtc.trace import _launch, trace

        hx, hy, px, py = (list(data) + [0.0] * 4)[:4]
        index = self._index(surf)
        if index < 1:
            raise SaosError(ErrorCode.BAD_ARGS, f"{op}: rays start at surface 1, got {surf}")
        system = self._system(wave)
        block = trace(system, [px], [py], [hx], [hy])
        error = int(block.error[0])
        if error and abs(error) <= index:
            how = "is totally internally reflected" if error < 0 else "misses"
            raise SaosError(ErrorCode.BAD_ARGS, f"{op}: the ray (Hx {hx}, Hy {hy}, Px {px}, Py {py}) {how} at surface {abs(error)}")
        k = block.surface_index.index(index)
        x, y, z = (float(v[0, k]) for v in (block.x, block.y, block.z))
        l, m, n = (float(v[0, k]) for v in (block.l, block.m, block.n))
        vertex = system.items[k].vertex_z
        values = {"REAX": x, "REAY": y, "REAZ": z, "REAR": math.hypot(x, y), "REAA": l, "REAB": m, "REAC": n,
                  "RAGX": x, "RAGY": y, "RAGZ": z + vertex, "RAGA": l, "RAGB": m, "RAGC": n,
                  # 手册：RANG 是相对该面局部 z 轴的光线角（弧度）/ the ray angle in radians to the local z axis
                  "RANG": math.atan2(math.hypot(l, m), n)}
        if op != "RAID":
            return values[op]
        if k > 0:
            d = np.array([block.l[0, k - 1], block.m[0, k - 1], block.n[0, k - 1]])
        elif system.lens.ray_aiming == 0 or system.stop is None:
            d = np.asarray(_launch(system, np.array([px]), np.array([py]), np.array([hx]), np.array([hy]))[3]).reshape(3)
        else:
            raise _unsupported("RAID at surface 1 with ray aiming needs the aimed launch direction, which rtc does not expose")
        _, zx, zy = system.items[k].shape.sag(np.array([x]), np.array([y]))
        normal = np.array([-float(np.asarray(zx).ravel()[0]), -float(np.asarray(zy).ravel()[0]), 1.0])
        cos = abs(float(np.dot(d, normal))) / (float(np.linalg.norm(d)) * float(np.linalg.norm(normal)))
        # 手册：RAID 是入射光线与面法线的夹角（度），总为正 / the angle of incidence in degrees, always positive
        return math.degrees(math.acos(min(1.0, cos)))

    def sag(self, index: int, x: float, y: float) -> float | None:
        system = self._system(self._need_lens().primary_wave)
        item = next((it for it in system.items if it.index == int(index)), None)
        if item is None:
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {index} out of range 1–{len(self.lens.surfaces) - 1}")
        z, _, _ = item.shape.sag(np.array([float(x)]), np.array([float(y)]))
        value = float(np.asarray(z, dtype=float).ravel()[0])
        return value if math.isfinite(value) else None

    def _vertex_z(self) -> dict[int, float]:
        return {it.index: it.vertex_z for it in self._system(self._need_lens().primary_wave).items}

    def global_matrix(self, index: int):
        """rtc 的面都沿光轴排列（不支持倾斜偏心）：旋转是单位阵，平移只有顶点 z。"""
        return np.eye(3), np.array([0.0, 0.0, self._vertex_z()[int(index)]])

    def quick_focus(self) -> dict[str, Any]:
        """按 RMS 光斑半径调整像面前一面的厚度（全部视场与波长按权重，各视场以自身质心为参考），与 ZOS 后端调的是同一个厚度。
        像面前是均匀介质时光线是直线，RMS² 对厚度改变量是二次函数，极小值有闭式解；像面弯曲时再迭代至多 5 次。光瞳采样与
        OpticStudio 的 Quick Focus 不同，未对账。
        Adjust the thickness before the image for the smallest RMS spot radius over every field and wavelength (weighted,
        each field about its own centroid); closed form per step for straight rays, up to 5 steps for a curved image."""
        from rtc.trace import trace

        from saos import kit

        lens = self._need_lens()
        index = len(lens.surfaces) - 2
        if f"{index}.thickness" in self.held:
            raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"surface {index} thickness is held by a {self.held[f'{index}.thickness']} solve")
        if any(r.op == "THIC" and r.surface == index for r in lens.mce):
            raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"surface {index} thickness comes from a multi-configuration row")
        before = lens.thickness(index, self.config)
        grid = np.asarray(kit.pupil(21), dtype=float)
        sx, sy = self._system(lens.primary_wave)._field_scale()
        for _ in range(5):
            num = den = 0.0
            for fx, fy, fw in lens.fields:
                hx, hy = (fx / sx if sx else 0.0), (fy / sy if sy else 0.0)
                pos, slope, weight = [], [], []
                for w, (_, ww) in enumerate(lens.active_wavelengths, start=1):
                    block = trace(self._system(w), grid[:, 0], grid[:, 1], hx, hy)
                    ok = (block.error == 0) & (np.abs(block.n[:, -2]) > 1e-12)
                    pos.append(np.column_stack([block.x[ok, -1], block.y[ok, -1]]))
                    slope.append(np.column_stack([block.l[ok, -2] / block.n[ok, -2], block.m[ok, -2] / block.n[ok, -2]]))
                    weight.append(np.full(int(ok.sum()), float(ww)))
                p, u, wt = np.vstack(pos), np.vstack(slope), np.concatenate(weight)
                if wt.sum() <= 0:
                    continue
                wt = wt / wt.sum()
                dp = p - (wt[:, None] * p).sum(axis=0)
                du = u - (wt[:, None] * u).sum(axis=0)
                num += fw * float((wt[:, None] * dp * du).sum())
                den += fw * float((wt[:, None] * du * du).sum())
            if den <= 0.0:
                raise SaosError(ErrorCode.BAD_ARGS, "no rays reach the image surface, so there is no focus to find")
            shift = -num / den
            surface = lens.surface(index)
            surface.thickness += shift
            self._changed()
            if abs(shift) <= 1e-9 * max(1.0, abs(surface.thickness)):
                break
        return {"surface": index, "thickness_before": before, "thickness_after": lens.thickness(index, self.config), "backend": "rtc"}

    # ---- 流程图窗口 / the workflow window ----

    @staticmethod
    def _aperture_spec(surface) -> dict[str, Any]:
        """面孔径写成 ZOS 后端 _aperture_dict 的形式，交给 saos.geometry。"""
        if surface.aperture_code == 1:
            return {"type": "CircularAperture", "MinimumRadius": surface.ap_min, "MaximumRadius": surface.ap_max,
                    "ApertureXDecenter": surface.ap_dx, "ApertureYDecenter": surface.ap_dy}
        return {"type": "FloatingAperture" if surface.aperture_code == 10 else "None"}

    def _geometry_items(self) -> list[dict[str, Any]]:
        """交叠检查的面，取法同 ZosBackend._geometry_items：网格到零件外缘，净口径外面形照常延伸。"""
        from saos import geometry

        lens = self._need_lens()
        vertex = self._vertex_z()
        items = []
        for surface in lens.surfaces[1:-1]:
            item = {"index": surface.index, "type": surface.type, "comment": surface.comment,
                    "material": self._material_text(surface), "sd": surface.semi_diameter}
            if surface.semi_diameter > 0:
                ap = geometry.aperture(self._aperture_spec(surface), surface.semi_diameter)
                item.update(clear=geometry.extent(ap), edge=max(surface.semi_diameter, surface.mech_semi_diameter), ap=ap,
                            rot=IDENTITY, off=[0.0, 0.0, vertex[surface.index]])
            items.append(item)
        radii = geometry.part_radii(items)
        for item in items:
            if "clear" not in item:
                continue
            ap, clear = item["ap"], item["clear"]
            outer = radii.get(item["index"], clear)
            if not ap["explicit"] and outer > clear:
                ap = {**ap, "hx": outer, "hy": outer}
            grid = geometry.polar_points(geometry.extent(ap))
            sags = geometry.fill_beyond(grid, [self.sag(item["index"], x, y) for x, y in grid], clear)
            item.update(sd=geometry.extent(ap), ap=ap, sags=geometry.fill_outside(grid, sags, ap))
        return items

    def interference(self) -> dict[str, Any]:
        from saos import geometry

        count = self.configuration_count()
        if count < 2:
            return geometry.interference(self._geometry_items())
        current, results = self.config, []
        try:
            for c in range(1, count + 1):
                self.select_configuration(c)
                results.append(geometry.interference(self._geometry_items()))
        finally:
            self.select_configuration(current)
        return geometry.merge_configurations(results)

    def viz_data(self, folder: Path | None, *, fan: int = 7, full: bool = True, rays: bool = True,
                 plots: frozenset[str] | None = None) -> dict[str, Any]:
        """流程图窗口的当前视图，结构同 ZosBackend.viz_data（不带 full 的部分）：逐面几何、布局光线、Lens Data、交叠检查，
        另带 backend 与 unverified，窗口据此标明数值来自 rtc。
        The live view for the workflow window, shaped like ZosBackend.viz_data without full, plus backend and unverified."""
        import time

        from rtc.trace import trace

        from saos import geometry

        lens = self._need_lens()
        info = self.system_info()
        image = len(lens.surfaces) - 1
        primary = lens.primary_wave
        system = self._system(primary)
        sx, sy = system._field_scale()
        norm = [((x / sx) if sx else 0.0, (y / sy) if sy else 0.0) for x, y, _ in lens.fields]
        vertex = self._vertex_z()
        timings: dict[str, float] = {}
        errors: dict[str, str] = {}
        started = time.monotonic()
        surfaces = []
        for surface in lens.surfaces[1:]:
            i, sd = surface.index, surface.semi_diameter
            item = {"index": i, "type": surface.type, "comment": surface.comment, "material": self._material_text(surface),
                    "semi_diameter": sd, "is_stop": surface.is_stop}
            if sd > 0:
                off = [0.0, 0.0, vertex[i]]
                item["vertex"] = off
                ap = geometry.aperture(self._aperture_spec(surface), sd)
                if ap["explicit"]:
                    item["aperture"] = {k: ap[k] for k in ("shape", "hx", "hy", "dx", "dy")}
                y_lo, y_hi, x_at = geometry.chord(ap, "y")
                x_lo, x_hi, y_at = geometry.chord(ap, "x")
                lines = {"profile_y": [(x_at, y_lo + (y_hi - y_lo) * k / 20) for k in range(21)],
                         "profile_x": [(x_lo + (x_hi - x_lo) * k / 20, y_at) for k in range(21)],
                         "rim": geometry.outline(ap, 32)}
                for key, points in lines.items():
                    sags = [self.sag(i, x, y) for x, y in points]
                    if any(v is None for v in sags):
                        errors.setdefault("sag", f"no sag on surface {i}; drawn flat")
                    item[key] = [[x, y, (v or 0.0) + off[2]] for (x, y), v in zip(points, sags)]
            surfaces.append(item)
        timings["geometry"] = round(time.monotonic() - started, 3)
        started = time.monotonic()
        clash = geometry.interference(self._geometry_items())
        timings["interference"] = round(time.monotonic() - started, 3)
        started = time.monotonic()
        paths = []
        if rays:
            steps = [2 * k / (fan - 1) - 1 for k in range(fan)]
            layout = [(f, hx, hy, 0.0, p) for f, (hx, hy) in enumerate(norm) for p in steps] +                      [(f, hx, hy, p, 0.0) for f, (hx, hy) in enumerate(norm) for p in steps if p != 0.0]
            arr = np.asarray([r[1:] for r in layout], dtype=float)
            try:
                block = trace(system, arr[:, 2], arr[:, 3], arr[:, 0], arr[:, 1])
            except NotImplementedError as exc:
                errors["layout_rays"] = str(exc)
                block = None
            for r, (f, _, _, px, py) in enumerate(layout if block is not None else []):
                points, first = [], None
                for k, index in enumerate(block.surface_index):
                    error, vig = int(block.error[r]), int(block.vig[r])
                    if error and abs(error) <= index:
                        break
                    points.append([float(block.x[r, k]), float(block.y[r, k]), float(block.z[r, k]) + vertex[index]])
                    if first is None:
                        first = [float(block.l[r, k]), float(block.m[r, k]), float(block.n[r, k])]
                    if vig and vig <= index:
                        break
                if points:
                    paths.append({"field": f, "px": px, "py": py, "points": points, "start_dir": first})
        timings["layout_rays"] = round(time.monotonic() - started, 3)
        stop = next((s for s in surfaces if s["is_stop"]), None)
        return {
            "backend": "rtc", "unverified": self.unverified(),
            "system": {**{k: info[k] for k in ("title", "lens_units", "aperture", "fields", "wavelengths", "stop_surface")},
                       "primary_wave": primary, "normalization": lens.field_normalization.capitalize(), "normalized_fields": norm,
                       "stop_semi_diameter": stop["semi_diameter"] if stop else None, "image_surface": image,
                       "non_axial": any(s.type in ("BICONICX", "BICONIC", "BICONICZ", "TOROIDAL") for s in lens.surfaces)},
            "surfaces": surfaces,
            "layout": {"wave": primary, "rays": paths, **({} if rays else {"rays_paused": True})},
            "lens_data": self.surfaces(),
            "interference": clash,
            "timings_s": timings, "errors": errors,
        }

    def configuration_count(self) -> int:
        return max(1, self.lens.num_configs)

    def current_configuration(self) -> int:
        return self.config

    def select_configuration(self, n: int) -> None:
        if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= self.configuration_count():
            raise SaosError(ErrorCode.BAD_ARGS, f"configuration must be 1–{self.configuration_count()}, got {n!r}")
        if n != self.config:
            self.config = n
            self._changed(edit=False)

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
            if index == 0 and math.isfinite(number) and lens.aperture_type == "EPD" and not self.standalone:
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
        # standalone 自己重算求解（_recompute），不需要冻结 / a standalone instance recomputes its solves itself
        if self.held and not self.freeze_solves and not self.standalone:
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
