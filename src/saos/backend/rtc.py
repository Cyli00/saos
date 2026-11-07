"""rtc 后端：OpticStudio 镜头的内存副本（ctx.fast，ADR-0014），以及 rtc 实例（SAOS_BACKEND=rtc）自己持有的镜头。
The rtc backend: an in-memory copy of an OpticStudio lens (ctx.fast, ADR-0014), and the lens an rtc instance owns.

数值来自 rtc（ray tracing core，float64 序列追迹，同仓库的 `src/rtc`）。副本只用于探索；rtc 实例在需求阶段做可行性
核算（ADR-0016），在设计阶段做变量、评价函数、优化、判定和轮次（ADR-0017），交付前在 OpticStudio 上复测。副本建立时用
OpticStudio 自己的描述（describe_model）逐项核对：面型、参数、各波长折射率（INDX）、视场、波长、孔径和瞄准方式；rtc 复现
不了的设置一律报 BACKEND_UNSUPPORTED，不返回近似值。
Numbers come from rtc. A copy is for exploration; the rtc instance checks need-stage feasibility (ADR-0016) and runs
variables, merit functions, optimisation, checks and rounds in the design stage (ADR-0017), re-measured on OpticStudio
before delivery. A copy is compared item by item with OpticStudio's own description; settings rtc cannot reproduce raise
BACKEND_UNSUPPORTED.
"""

from __future__ import annotations

import copy
import json
import math
import time
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from saos.backend.base import DEFAULT_SUBSTITUTE_CATALOG, MATERIAL_MODES, Backend, check_size, parse_numeric
from saos.protocol import ErrorCode, SaosError

# 求解类型里 rtc 当作普通数值的两种；其余求解（拾取、边缘光线高度、F 数…）改别的参数后 OpticStudio 会重算，rtc 不会
# the two solve types rtc treats as plain numbers; other solves are recomputed by OpticStudio after a change, not by rtc
PLAIN_SOLVES = ("Fixed", "Variable")
SURFACE_APERTURES = {"None": 0, "CircularAperture": 1, "CircularObscuration": 2, "RectangularAperture": 4, "FloatingAperture": 10}
# 系统孔径类型：OpticStudio 的名字 ↔ .zmx 的写法。float_by_stop 的孔径值由光阑半口径定，文件里没有数值。
APERTURE_TYPES = {"EntrancePupilDiameter": "EPD", "ImageSpaceFNum": "FNO", "ObjectSpaceNA": "OBNA", "FloatByStopSize": "FLOA"}
APERTURE_NAMES = {v: k for k, v in APERTURE_TYPES.items()}
# 与 OpticStudio 逐条追迹对过账的面型（.zmx 类型码）；rtc 另有的 BINARY_2、FRESNELS 只在几个小样本上对过，没有真值回归，一律拒绝
# surface types reconciled ray by ray with OpticStudio; rtc's BINARY_2 and FRESNELS were only spot-checked and have no truth regression, so they are refused
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
# rtc 实例能解析的多重结构操作数（rtc.thermal.SUPPORTED_OPS）；CHZN 只有 0
THERMAL_OPS = frozenset({"THIC", "CRVT", "CONN", "PRAM", "SDIA", "MCSD", "CHZN", "GLSS", "TEMP", "PRES", "MOFF"})
# .zmx 面型 → OpticStudio 的 TypeName（结构指纹按它比较）；BICONICZ 的名字没有实测过
# .zmx surface type → OpticStudio's TypeName, which the structural fingerprint compares; BICONICZ's name is not measured
TYPE_NAMES = {"STANDARD": "Standard", "EVENASPH": "Even Asphere", "PARAXIAL": "Paraxial", "BICONIC": "Biconic",
              "BICONICX": "Biconic", "TOROIDAL": "Toroidal", "BICONICZ": "Biconic Zernike"}
# rtc 读不出、也改不了的系统设置：存盘时原样带过去，结构指纹不比较，交付前在 OpticStudio 上复测时才核对
# system settings rtc can neither read nor change: carried over on save, left out of the fingerprint comparison, checked
# when the round is re-measured on OpticStudio before delivery
UNREAD_SETTINGS = ("apodization", "polarization", "coating_file")
# 局部优化 cycles 与迭代次数；auto 也有上限 / local optimisation cycles to iterations; auto has a cap too
CYCLES = {"auto": 200, "1": 1, "5": 5, "10": 10, "50": 50}
# 换料时每面在目录里取 n、V 最近的这么多种 / glasses per surface, nearest in n and V, that substitution chooses from
SUBSTITUTE_NEIGHBOURS = 8
# FFT MTF 的光瞳采样上限（每边点数）/ the largest pupil sampling of the FFT MTF (points per side)
MTF_MAX_SAMPLING = 256


@lru_cache(maxsize=1)
def admitted_paths() -> frozenset[str]:
    return frozenset(json.loads(ADMITTED_FILE.read_text(encoding="utf-8"))["paths"])


def catalog_indices(names, catalogs, glass_dir: Path | None, environment: tuple[float, float, bool],
                    wavelengths_um, admitted_only: bool = True) -> tuple[dict[str, list[float]], dict[str, str]]:
    """不建镜头，按准入表在 rtc 材料层算目录玻璃在各波长的折射率（与 OpticStudio INDX 差 ≤ 1e-8 的路径才算；admitted_only=False
    时 rtc 实例照算未准入的路径）。environment 是 (温度 °C, 压力 atm, 是否按环境修正)。返回 {牌号: [n…]} 与 {牌号: 没算的原因}。
    Catalog indices from rtc's material layer, admitted paths only unless admitted_only is False (the rtc instance)."""
    from rtc.material import Catalog, Environment, GlassCatalog, MaterialInvalid, MaterialUnsupported, resolve

    catalog = GlassCatalog(glass_dir) if glass_dir else GlassCatalog()
    env = _material_call(Environment, *environment)
    values, skipped = {}, {}
    for name in names:
        try:
            medium = resolve(Catalog(name), catalog, list(catalogs))
            path = medium.path(env)
            if admitted_only and path not in admitted_paths():
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


def _aperture_value(lens) -> float:
    """系统孔径的数值：Float By Stop Size 由光阑半口径定（OpticStudio 的 ApertureValue 报的也是它）。"""
    if lens.aperture_type != "FLOA":
        return float(lens.aperture_value)
    stop = next((s for s in lens.surfaces if s.is_stop), None)
    return float(stop.semi_diameter) if stop is not None else 0.0


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
        # 与 OpticStudio 交接这份镜头时的逐值比对（saos.handover），换镜头时清空
        # the value-by-value comparison made when this lens was handed over with OpticStudio, cleared with the lens
        self.handover: dict[str, Any] | None = None
        # 被求解锁住的参数：键 → 求解类型 / parameters held by a solve: key → solve type
        self.held: dict[str, str] = {}
        # standalone 时用到的未准入材料路径 / material paths outside the admitted table used by a standalone instance
        self.unverified_paths: set[str] = set()
        self.path = Path(path) if path is not None else None
        self.lens = None
        self.config = 1
        # 设计阶段（ADR-0017）：评价函数行随镜头存盘；换料变量的 .zmx 写法没见过，只在内存里
        # design stage (ADR-0017): merit rows are saved with the lens; material variables only live in memory
        self.merit: list = []
        self.material_vars: dict[int, dict[str, str]] = {}
        # 读入文件里 rtc 不建模、存盘时原样带过去的行（rtc_lens.carried_lines）/ lines carried over from the file read
        self._carried: dict[str, list[str]] | None = None
        self._glass_tables: dict[str, list] = {}
        # 当前结构解析后的镜头（Lens.configured），改参数或换结构时清空；只有厚度行的镜头用不到
        # the current configuration resolved by Lens.configured, cleared on any change; unused by THIC-only lenses
        self._view = None
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
        from saos.backend import rtc_merit

        merit = rtc_merit.from_lens(lens)
        previous = (self.lens, self.config, dict(self.held), set(self.unverified_paths), set(self.confirmed), self.handover)
        self.lens, self.config, self.held = lens, lens.current_config, {}
        lens.glass_lookup = self._glass_lookup
        self._view = None
        # INDX 确认的键不含环境，只对确认时的那份镜头有效 / INDX confirmations carry no environment: valid for their lens only
        self.unverified_paths.clear()
        self.confirmed.clear()
        self.handover = None
        self._systems.clear()
        try:
            self._check_lens()
            if self.standalone:
                self._recompute()
        except BaseException:
            self.lens, self.config, self.held, self.unverified_paths, self.confirmed, self.handover = previous
            self._systems.clear()
            self._view = None
            raise
        self.merit, self.material_vars = merit, {}

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
            from saos.backend import rtc_lens

            # rtc 实例要写回 .zmx：不认识的面块行（拾取等求解、膜层）照算或丢掉都会让存盘的镜头与算的不同
            # the rtc instance writes .zmx back: unknown surface lines (pickups and other solves, coatings) would make the
            # saved lens differ from the one computed, whether ignored or dropped
            out += [f"{line} (a .zmx line rtc neither models nor carries over)" for line in rtc_lens.unknown_surface_lines(lens)]
            if lens.aperture_type not in ("EPD", "FNO", "OBNA", "FLOA"):
                out.append(f"system aperture {lens.aperture_type} (rtc has entrance pupil diameter, image space F/#, object space NA and "
                           "float by stop size)")
            if lens.field_type not in ("ANGLE", "OP1") or (lens.field_type == "OP1" and not finite):
                out.append(f"field type {lens.field_type} (rtc has angles, and object heights with a finite object)")
            if lens.aperture_type == "OBNA" and not finite:
                out.append("object space NA with the object at infinity")
            if lens.aperture_type == "FNO" and finite:
                out.append("image space F/# with a finite object distance")
        else:
            if lens.aperture_type not in ("EPD", "FNO", "FLOA"):
                # 2026-10-26 实测：piB（物方 NA + 实际瞄准）10 条光线 9 条瞄准不收敛，位置差 0.096 mm
                out.append("system aperture other than entrance pupil diameter, image space F/# or float by stop size (rtc's object space NA launch and aiming do not "
                           "match OpticStudio)")
            if lens.field_type != "ANGLE":
                out.append(f"field type {lens.field_type} (rtc traces angle fields only)")
            if lens.ray_aiming == 1:
                out.append("paraxial ray aiming (rtc has off and real)")
            if lens.aperture_type in ("EPD", "FNO", "FLOA") and finite:
                out.append("finite object distance with an entrance pupil aperture")
        if "ENVD" not in lens.raw:
            out.append("missing ENVD (the environment cannot be checked)")
        if not lens.active_wavelengths:
            out.append("no active wavelengths")
        if lens.surface(0).material is not None:
            out.append("rtc assumes air in object space")
        allowed = THERMAL_OPS if self.standalone else {"THIC", "MOFF"}
        others = sorted({row.op for row in lens.mce if row.op not in allowed})
        if others:
            out.append(f"multi-configuration operands {others} (rtc applies {sorted(allowed - {'MOFF'})})")
        if not lens.plain_configurations:
            if not self.standalone:
                out.append("thermal pickups (rtc copies apply fixed THIC rows only; open the lens on the rtc instance)")
            for s in lens.surfaces[1:]:
                if s.type.upper() not in ("STANDARD", "EVENASPH") and any(r.surface == s.index for r in lens.mce):
                    out.append(f"surface {s.index} type {s.type} in multi-configuration rows (only Standard and Even Asphere are reconciled)")
            for row in lens.mce:
                if row.solve_code not in (0, 1, 2, lens.thermal_code):
                    out.append(f"multi-configuration row {row.op} {row.surface} solve code {row.solve_code} (rtc knows fixed, variable, pickup and thermal)")
                    break
        # 下面几条路径没有 OpticStudio 真值，对账前拒绝（docs/plans/rtc-iris-mirror.md）
        # paths without OpticStudio truth yet are refused until reconciled
        mirrors = [s.index for s in lens.surfaces[1:] if s.mirror]
        for s in lens.surfaces[1:]:
            if s.aperture_code not in SURFACE_APERTURES.values():
                out.append(f"surface {s.index} aperture code {s.aperture_code} (not reconciled with OpticStudio)")
            if s.mirror and s.type.upper() != "STANDARD":
                out.append(f"surface {s.index} is a {s.type} mirror (only standard surfaces are reconciled as mirrors)")
            if s.type.upper() == "PARAXIAL" and mirrors and mirrors[0] < s.index:
                out.append(f"surface {s.index} is a paraxial surface after a mirror (not reconciled with OpticStudio)")
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
        for config in range(1, self.configuration_count() + 1):
            if not self.lens.plain_configurations:
                try:
                    self._configured(config)
                except (ValueError, KeyError) as exc:
                    return [f"configuration {config}: {exc}"]
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
        if ap.get("type") not in ("EntrancePupilDiameter", "ImageSpaceFNum", "FloatByStopSize"):
            out.append(f"system aperture {ap.get('type')}")
        if ap.get("telecentric_object"):
            out.append("telecentric object space")
        if model.get("ray_aiming") not in ("Off", "Real"):
            out.append(f"ray aiming {model.get('ray_aiming')}")
        fields = model.get("fields", {})
        if fields.get("type") != "Angle":
            out.append(f"field type {fields.get('type')}")
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
        want_ap = APERTURE_TYPES.get(ap.get("type"))
        if want_ap != lens.aperture_type or not _same(float(ap.get("value", 0.0)), _aperture_value(lens)):
            out.append(f"aperture {ap.get('type')} {ap.get('value')} vs file {lens.aperture_type} {_aperture_value(lens)}")
        if {"Off": 0, "Real": 2}.get(model.get("ray_aiming")) != lens.ray_aiming:
            out.append(f"ray aiming {model.get('ray_aiming')} vs file {lens.ray_aiming}")
        fields = model.get("fields", {})
        if str(fields.get("normalization", "")).lower() != lens.field_normalization:
            out.append(f"field normalization {fields.get('normalization')} vs file {lens.field_normalization}")
        items = fields.get("items", [])
        if len(items) != len(lens.fields) or not all(_same(f["x"], x) and _same(f["y"], y) for f, (x, y, _) in zip(items, lens.fields)):
            out.append("field points differ from the file")
        for k, (f, mine) in enumerate(zip(items, lens.vignetting or [])):
            theirs = f.get("vignetting") or {}
            for name, value in zip(("VDX", "VDY", "VCX", "VCY", "VAN"), mine):
                if not _same(float(theirs.get(name, 0.0)), float(value)):
                    out.append(f"field {k + 1} vignetting {name}: OpticStudio {theirs.get(name)}, file {value}")
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

    def reconcile(self, model: dict[str, Any]) -> list[str]:
        """rtc 实例与 OpticStudio 交接同一份镜头时（standalone）：与它的 describe_model(None) 逐项比较，返回不一致的条目，
        不报错。INDX 一致的材料点记入 confirmed，各波长系统随之重建，未对账的材料路径相应减少。
        On a handover of the same lens with OpticStudio (standalone): compare with its describe_model(None) and return
        the mismatches without raising. Materials whose INDX agrees are confirmed, so the systems are rebuilt and fewer
        material paths stay unverified."""
        mismatches = self._compare(model)
        self._systems.clear()
        self.unverified_paths.clear()
        problems = self._system_problems()
        if problems:
            raise _unsupported("rtc cannot reproduce this lens: " + "; ".join(problems), problems)
        return mismatches

    def _confirm_indices(self, s, indices) -> list[str]:
        """一面的面后介质逐波长与 OpticStudio 的 INDX 比较；一致的 (材料, 波长) 记入 confirmed。
        rtc 算不了的材料不算不一致，留给 _system_problems 报 BACKEND_UNSUPPORTED。"""
        from rtc.material import MaterialInvalid, MaterialUnsupported, resolve

        env = self.lens.environment
        out = []
        material = self._incident_surface(s).material
        try:
            medium = resolve(material, self._catalog(), self.lens.catalogs)
        except (MaterialInvalid, MaterialUnsupported):
            return out
        for w, ((um, _), n_zos) in enumerate(zip(self.lens.active_wavelengths, indices), 1):
            try:
                n_rtc = float(medium.index(um, env))
            except (MaterialInvalid, MaterialUnsupported):
                continue
            if abs(float(n_zos) - n_rtc) <= INDEX_TOL:
                self.confirmed.add((material, um))
            else:
                out.append(f"surface {s.index} index at wavelength {w}: OpticStudio {n_zos}, rtc {n_rtc}")
        return out

    def _incident_surface(self, s):
        """INDX 读的那一面：反射面报入射侧介质（reflective 实测，玻璃内的反射面报玻璃折射率），其余面报面后介质。"""
        while s.mirror:
            s = self.lens.surfaces[s.index - 1]
        return s

    # ---- rtc 系统 / rtc systems ----

    def _catalog(self):
        from rtc.system import catalog

        return self.glasses if self.glasses is not None else catalog()

    def _glass_lookup(self, name: str, catalogs: list[str]):
        return self._catalog().get(name, catalogs)

    def _configured(self, config: int):
        """Lens.configured，错误转成 saos 错误。/ Lens.configured with its errors turned into saos errors."""
        from rtc.thermal import ThermalUnsupported

        try:
            return self.lens.configured(config)
        except ThermalUnsupported as exc:
            raise _unsupported(f"rtc: {exc}") from exc

    def _sync(self) -> None:
        """让 lens.surfaces 和环境等于当前结构的值，同 OpticStudio 的 LDE：读镜头的地方都不用区分结构。只有厚度行的镜头不用同步。
        Make lens.surfaces and the environment hold the current configuration, as OpticStudio's LDE does."""
        lens = self.lens
        if lens.plain_configurations:
            self._view = None
            return
        view = self._configured(self.config)
        for s, v in zip(lens.surfaces, view.surfaces):
            s.curvature, s.conic, s.thickness = v.curvature, v.conic, v.thickness
            s.semi_diameter, s.mech_semi_diameter = v.semi_diameter, v.mech_semi_diameter
            s.semi_fixed, s.mech_fixed = v.semi_fixed, v.mech_fixed
            s.params.update(v.params)
            if s.material != v.material:
                s.material = v.material
        lens.environment = view.environment
        self._view = view

    def _lens_view(self):
        """建系统用的镜头：当前结构解析后的那份。/ The lens systems are built from: the current configuration, resolved."""
        if self.lens.plain_configurations:
            return self.lens
        if self._view is None:
            self._sync()
        return self._view

    def _admit(self, medium, spec, wavelength: float) -> None:
        """材料路径已准入，或这一 (材料, 波长) 建立时经 OpticStudio INDX 确认过，才允许参与追迹。"""
        # 反射面没有自己的折射率，入射侧的材料由入射侧那一面准入
        if medium.reflect:
            return
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
            self._lens_view()
            for s in self.lens.surfaces[1:]:
                self._admit(_material_call(resolve, s.material, self._catalog(), self.lens.catalogs), s.material, um)
            # 非主波长系统借主波长系统取光阑半径，免得每次各自重建一份
            primary = self._system(self.lens.primary_wave) if wave != self.lens.primary_wave else None
            self._systems[key] = _material_call(OpticalSystem, self._lens_view(), wavelength_um=um, config=self.config,
                                                glasses=self.glasses, media=self._media, primary=primary,
                                                allow_unverified=self.standalone)
        return self._systems[key]

    def _changed(self, edit: bool = True) -> None:
        self._systems.clear()
        self._view = None
        if getattr(self, "_batch", False):
            # apply_values 里逐项改值时不重算，全部改完再算一次 / apply_values recomputes once after all values are set
            if edit:
                self._dirty = True
            return
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
        self._sync()
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
        self._view = None

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
        if any(s.semi_variable for s in lens.surfaces):
            out.append("光阑半口径内存变量：AD 与优化终点待对账，导出只保留固定数值")
        if lens.ray_aiming == 1:
            out.append("paraxial ray aiming")
        if lens.ray_aiming == 2 and lens.aperture_type == "FLOA":
            out.append("float by stop size with real ray aiming (EXPP and EXPD differ from OpticStudio by about 4e-6)")
        if lens.ray_aiming == 2 and any(s.mirror for s in lens.surfaces[1:]):
            out.append("mirrors with real ray aiming (rays match OpticStudio on the truth cases only)")
        for surface in lens.surfaces[1:]:
            shape = geometry.make(surface)
            if not getattr(shape, "verified", False):
                out.append(f"surface {surface.index} type {surface.type}")
        if any(s.index > 0 and not s.semi_fixed for s in lens.surfaces):
            out.append("automatic semi-diameters (pupil-rim rays of every field and wavelength)")
        if any("MAZH" in s.raw for s in lens.surfaces):
            out.append("paraxial marginal ray height solves")
        out += [f"material path {path}" for path in sorted(self.unverified_paths)]
        if not lens.plain_configurations:
            out.append("thermal pickups: rule fitted to OpticStudio 24.1 cells on Standard and Even Asphere surfaces (tests/fixtures/cases/thermal.json)")
        if self.handover is not None and not self.handover["agrees"]:
            out += [f"differs from OpticStudio at handover: {m}" for m in self.handover["mismatches"]]
            if not self.handover["rays_agree"]:
                out.append(f"traces differently from OpticStudio at handover: {self.handover['rays']}")
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

        from saos.backend import rtc_lens

        try:
            text = zmx.decode_zmx_bytes(Path(path).read_bytes())
            lens = zmx.parse(Path(path))
        except OSError as exc:
            raise SaosError(ErrorCode.FILE_NOT_FOUND, f"cannot read {path}: {exc}") from exc
        except (MaterialInvalid, MaterialUnsupported) as exc:
            raise _unsupported(f"rtc cannot read the materials or environment of {path}: {exc}") from exc
        self._adopt(lens)
        self.path, self._dirty, self._carried = Path(path), False, rtc_lens.carried_lines(text)

    def save_as(self, path: Path) -> None:
        if not self.standalone:
            raise _unsupported("rtc never writes lens files; push the values to OpticStudio with fast.push(ctx) and save there")
        self._write(Path(path))
        self.path, self._dirty = Path(path), False

    def _write(self, path: Path) -> None:
        """当前镜头连同评价函数写成 .zmx，不改 self.path。/ Write the lens with its merit function, leaving self.path alone."""
        from rtc.zmx import MFERow

        from saos.backend import rtc_lens, rtc_merit

        lens = self._need_lens()
        if not lens.plain_configurations:
            # LDE 是当前结构的值（_sync），MNUM 要报同一个结构 / the LDE holds the current configuration, and MNUM must say so
            self._lens_view()
            lens.current_config = self.config
        lens.mfe = [MFERow(op=r.type, numbers=[] if r.type == "BLNK" else [*r.params[:6], r.target, r.weight, *r.params[6:8]], raw=line)
                    for r, line in zip(self.merit, rtc_merit.to_lines(self.merit))]
        rtc_lens.write(lens, path, carried=self._carried)

    def snapshot_to(self, path: Path) -> None:
        self._write(Path(path))

    def new_system(self) -> None:
        if not self.standalone:
            raise _unsupported("rtc only holds a copy of an OpticStudio lens")
        self.lens, self.path, self._dirty = None, None, False
        self.held, self._systems = {}, {}
        self.merit, self.material_vars, self._carried = [], {}, None

    def build(self, prescription: dict[str, Any]) -> None:
        """standalone：按规范化处方搭建镜头（rtc 复现不了或写不出的项一次全部列出，BACKEND_UNSUPPORTED）。"""
        if not self.standalone:
            raise _unsupported("an rtc copy cannot be rebuilt; build the lens on OpticStudio")
        from saos.backend import rtc_lens

        self._adopt(rtc_lens.from_prescription(prescription))
        if prescription.get("thermal"):
            self.make_thermal(prescription["thermal"]["temperatures"], prescription["thermal"]["housing_tce"])
        self.path, self._dirty, self._carried = None, True, None

    def native_file(self) -> Path | None:
        return self.path

    def needs_save(self) -> bool:
        return self._dirty

    def describe_model(self, folder: Path | None, prefix: str = "layout") -> dict[str, Any]:
        """rtc 实例上镜头的描述数据，字段与 ZosBackend.describe_model(None) 相同，供结构指纹、逐轮材料记录和镜头哈希用；
        UNREAD_SETTINGS 列在 unread 里、不给值。模型页的剖面图由后台进程用 viz 数据画（model_report.section_layout），这里不出图。
        The rtc instance's lens description with ZosBackend.describe_model(None)'s fields, for the structural fingerprint,
        per-round materials and the lens hash; UNREAD_SETTINGS are listed under unread without values. The daemon draws the
        model page's section from the viz data (model_report.section_layout); no pictures here."""
        if not self.standalone:
            raise _unsupported("model descriptions are measured on OpticStudio; open the lens in an OpticStudio instance for saos lens describe")
        if folder is not None:
            raise _unsupported("rtc writes no layout pictures; call describe_model(None) and draw the section with model_report.section_layout")
        lens = self._need_lens()
        info = self.system_info()
        vertex = self._vertex_z()
        surfaces = []
        for s in lens.surfaces:
            i = s.index
            surfaces.append({
                "index": i, "type": TYPE_NAMES.get(s.type.upper(), s.type), "comment": s.comment, "radius": self._radius(s),
                "thickness": lens.thickness(i, self.config), "material": self._material_text(s), "semi_diameter": s.semi_diameter,
                "mech_semi_diameter": s.mech_semi_diameter, "conic": self._conic(s), "is_stop": s.is_stop, "solves": self._solves(s),
                "material_source": self._material_source(s), "n": self._indices(i), "aperture": self._aperture_spec(s),
                "params": [{"par": n, "name": f"Par {n}", "value": v, "solve": "Variable" if n in s.variable_params else "Fixed"}
                           for n, v in sorted(s.params.items()) if v],
                "tilt_decenter": {}, "coating": "", "global_vertex": [0.0, 0.0, vertex[i]] if i in vertex else None,
            })
        fields = info["fields"]
        return {
            "backend": "rtc", "file": info["file"], "title": info["title"], "lens_units": "Millimeters", "aperture": info["aperture"],
            "ray_aiming": {0: "Off", 1: "Paraxial", 2: "Real"}[lens.ray_aiming],
            "fields": {"type": fields["type"], "normalization": fields["normalization"],
                       "items": fields["items"]},
            "wavelengths": info["wavelengths"], "material_catalogs": list(lens.catalogs), "stop_surface": lens.stop_index,
            "environment": info["environment"], "configurations": self._configurations(), "unread": list(UNREAD_SETTINGS),
            "surfaces": surfaces, "layout": {},
        }

    def _solves(self, s) -> dict[str, str]:
        from rtc.material import Model

        i = s.index
        material = {"model": "MaterialModel", "substitute": "MaterialSubstitute"}.get(self.material_vars.get(i, {}).get("mode"))
        cell_solve = {"radius": self._surface_cell(i, "radius"), "thickness": self._surface_cell(i, "thickness")}
        names = {0: "Fixed", 1: "Variable", 2: "ConfigPickup", self.lens.thermal_code: "ThermalPickup"}
        return {"radius": names.get(cell_solve["radius"].solve_code) if cell_solve["radius"] else "Variable" if s.solve_radius == 1 else "Fixed",
                "thickness": (names.get(cell_solve["thickness"].solve_code) if cell_solve["thickness"] else
                              self.held.get(f"{i}.thickness") or ("Variable" if "VDSZ" in s.raw else "Fixed")),
                "material": material or ("MaterialModel" if isinstance(s.material, Model) else "Fixed"),
                "semi_diameter": "Variable" if s.semi_variable else "Fixed" if s.semi_fixed else "Automatic",
                "mech_semi_diameter": "Fixed" if s.mech_fixed else "Automatic",
                "conic": "Variable" if "VCON" in s.raw else "Fixed"}

    def _material_source(self, s) -> dict[str, Any]:
        from rtc.material import Model

        spec = s.material
        if s.mirror:
            return {"kind": "mirror"}
        if spec is None:
            return {"kind": "none"}
        if isinstance(spec, Model):
            return {"kind": "model", "nd": spec.nd, "vd": spec.vd, "dpgf": spec.dpgf}
        name = s.glass.upper()
        catalogs = [c for c in self.lens.catalogs if any(g.name.upper() == name for g in self._glass_table(c))]
        return {"kind": "catalog", "name": name, "catalogs": catalogs, "status": self.glass_status(name, catalogs)}

    def medium_indices(self, ref: Any, wavelengths_um: list[float]) -> list[float]:
        """面后介质在任意波长的折射率，与 INDX 同一条材料路径，不改镜头的波长表。玻璃专家在 rtc 实例上取当前材料的坐标用；
        OpticStudio 那边只能临时改波长表再读 INDX。
        The index after a surface at any wavelengths, by INDX's material path, leaving the wavelength table alone."""
        from rtc.material import Environment, resolve

        s = self._incident_surface(self.lens.surfaces[self._index(ref)])
        medium = _material_call(resolve, s.material, self._catalog(), self.lens.catalogs)
        e = self.lens.environment
        env = _material_call(Environment, e.temperature, e.pressure, e.adjust)
        return [float(_material_call(medium.index, float(w), env)) for w in wavelengths_um]

    def _indices(self, index: int) -> list[float | None]:
        """面后介质在各波长的折射率（同 OpticStudio 的 INDX）；算不出的波长为 None。"""
        out: list[float | None] = []
        for w in range(1, len(self.lens.active_wavelengths) + 1):
            try:
                out.append(float(self.operand("INDX", index, w, [])))
            except SaosError:
                out.append(None)
        return out

    def _configurations(self) -> dict[str, Any]:
        """多重结构，形式同 ZosBackend._configurations。/ Multi-configuration data, shaped as ZosBackend._configurations."""
        lens = self.lens
        count = self.configuration_count()
        if not lens.plain_configurations:
            from rtc import thermal

            thermal.refresh(lens)
        names = {0: "Fixed", 1: "Variable", 2: "ConfigPickup", lens.thermal_code: "ThermalPickup"}
        operands = []
        for j, (op, surface, par) in enumerate(lens.row_keys(), 1):
            if op == "MOFF":
                continue
            cells = [lens.cell(op, surface, c, par) for c in range(1, count + 1)]
            if op == "GLSS":
                values: list[Any] = [cell.text for cell in cells]
            elif op == "THIC":
                values = [lens.thickness(surface, c) for c in range(1, count + 1)]
            else:
                values = [cell.value for cell in cells]
            params = [] if op in ("TEMP", "PRES") else [surface, par] if op == "PRAM" else [surface]
            operands.append({"row": j, "type": op, "params": params, "values": values,
                             "solves": [names.get(cell.solve_code, str(cell.solve_code)) for cell in cells]})
        return {"count": count, "current": self.config, "operands": operands}

    def _config_row(self, row: int) -> tuple[str, int, int]:
        keys = self.lens.row_keys()
        if not 1 <= row <= len(keys):
            raise SaosError(ErrorCode.BAD_ARGS, f"multi-configuration row {row} out of range 1–{len(keys)}")
        return keys[row - 1]

    def _config_cell(self, name: str):
        """config.<c>.<r> 的单元格。/ The cell of config.<c>.<r>."""
        config, row = (int(v) for v in name.split("."))
        if not 1 <= config <= self.configuration_count():
            raise SaosError(ErrorCode.BAD_ARGS, f"configuration {config} out of range 1–{self.configuration_count()}")
        op, surface, par = self._config_row(row)
        cell = self.lens.cell(op, surface, config, par)
        if cell is None:
            raise SaosError(ErrorCode.BAD_ARGS, f"multi-configuration row {row} has no cell in configuration {config}")
        return cell

    def config_cell_target(self, key: str) -> tuple[int, str]:
        """变量键 config.<c>.<r> 对应的 (面号, 面参数)，优化器按它定差分步长。"""
        _, config, row = key.split(".")
        op, surface, par = self._config_row(int(row))
        name = {"CRVT": "curvature", "THIC": "thickness", "CONN": "conic", "SDIA": "semi_diameter", "MCSD": "semi_diameter", "PRAM": f"par{par}"}.get(op)
        if name is None:
            raise SaosError(ErrorCode.BAD_ARGS, f"multi-configuration row {row} ({op}) cannot be a variable on rtc")
        return surface, name

    def _surface_cell(self, surface: int, param: str):
        """这个面参数在当前结构的单元格；没有这一行或旧式只有厚度行的镜头为 None。"""
        from saos.lensdata import PAR_RE

        lens = self.lens
        if lens.plain_configurations:
            return None
        match = PAR_RE.match(param)
        if match:
            return lens.cell("PRAM", surface, self.config, int(match.group(1)))
        op = {"radius": "CRVT", "curvature": "CRVT", "thickness": "THIC", "conic": "CONN", "semi_diameter": "SDIA",
              "mech_semi_diameter": "MCSD"}.get(param)
        return lens.cell(op, surface, self.config) if op else None

    def vary_config(self, rows: list[int], configs: list[int] | None, on: bool) -> dict[str, str]:
        """把多重结构的格设为变量或固定，同 ZosBackend.vary_config。热跟随格设固定时留下现在的值。
        Make cells variable or fixed, as ZosBackend.vary_config; a thermal cell made fixed keeps its current value."""
        lens = self._designing("saos opt vary")
        count = self.configuration_count()
        configs = configs or list(range(1, count + 1))
        bad = [c for c in configs if not 1 <= c <= count]
        if count < 2 or bad:
            raise SaosError(ErrorCode.BAD_ARGS, f"multi-configuration configurations 1–{count} (at least 2 configurations); got {bad}")
        keys = [self._config_row(r) for r in rows]
        if on and (fixed := sorted({k[0] for k in keys} & {"TEMP", "PRES", "GLSS", "CHZN", "MOFF"})):
            raise SaosError(ErrorCode.BAD_ARGS, f"multi-configuration rows {fixed} cannot be variables on rtc")
        out = {}
        for r, (op, surface, par) in zip(rows, keys):
            for c in configs:
                cell = lens.cell(op, surface, c, par)
                if cell is None:
                    raise SaosError(ErrorCode.BAD_ARGS, f"multi-configuration row {r} has no cell in configuration {c}")
                if op == "THIC" and cell.pickup is not None:
                    raise _unsupported(f"multi-configuration row {r} configuration {c} is a thickness pickup; rtc varies plain cells only")
                if not lens.plain_configurations:
                    from rtc import thermal

                    thermal.refresh(lens)
                cell.solve = [1.0 if on else 0.0, *cell.solve[1:]] if cell.solve else [1.0 if on else 0.0, float(par), 0.0, 1.0, 1.0, 1.0, 0.0]
                cell.edited = True
                out[f"{r}.{c}"] = "Variable" if on else "Fixed"
        self._dirty = True
        self._changed()
        return out

    def make_thermal(self, temperatures: list[float], tce: float) -> dict[str, Any]:
        """结构 1 是现在的镜头，其余结构是给定温度下的热跟随（同 OpticStudio 的 Make Thermal，不需要启动它）。tce 是镜筒的
        膨胀系数（1e-6/K），写进所有面的 TCED。结果形式同 describe_model 的 configurations。
        Configuration 1 is the lens as it is, the others follow it thermally at the given temperatures, as OpticStudio's
        Make Thermal does, without starting it. tce is the barrel's expansion (1e-6/K), written to every surface's TCED."""
        from rtc import thermal

        lens = self._designing("Make Thermal")
        # 单结构文件也可能带一行 MOFF（OpticStudio 的默认空行）/ a single-configuration file may hold OpticStudio's default MOFF row
        if any(row.op != "MOFF" for row in lens.mce) or lens.num_configs > 1:
            raise _unsupported("the lens already has multi-configuration rows; Make Thermal needs a single configuration")
        before = copy.deepcopy(lens)
        lens.mce = []
        try:
            thermal.make(lens, [float(t) for t in temperatures], float(tce))
        except thermal.ThermalUnsupported as exc:
            self.lens, self._view = before, None
            self._systems.clear()
            raise _unsupported(f"rtc: {exc}") from exc
        self.config = 1
        self.held = {k: v for k, v in self.held.items() if v != "MarginalRayHeight"}
        problems = self._file_problems()
        if problems:
            self.lens, self._view = before, None
            self._systems.clear()
            raise _unsupported("rtc cannot make this lens thermal: " + "; ".join(problems), problems)
        self._changed()
        return self._configurations()

    def metric_support(self, *, operand: str | None = None, analysis: str | None = None) -> str:
        if operand is not None and operand not in RAY_OPERANDS | SYSTEM_OPERANDS:
            return "unsupported"
        if analysis is not None and analysis not in self.analysis_types():
            return "unsupported"
        return "available"

    def analysis_types(self) -> list[str]:
        return ["FftMtf"]

    def analysis(self, name: str, settings: dict[str, Any], text_path: Path) -> dict[str, Any]:
        """rtc 能算的分析，结果形式同 ZosBackend.analysis（series、settings、status），不写文本导出。目前只有 FftMtf：
        设置 Field（0 为全部）、Wavelength（0 为全部，按权重）、MaximumFrequency（0 为到截止频率）、SampleSize（S_32x32…）、
        ShowDiffractionLimit；各波长以自己的主光线为参考（rtc.psf.mtf_fft），与 OpticStudio 未对账。
        Analyses rtc computes, shaped like ZosBackend.analysis without a text export. Only FftMtf for now."""
        if name != "FftMtf":
            raise _unsupported(f"rtc has no analysis {name}; it has FftMtf. Use ctx.spot, ctx.trace and the operands rtc computes, "
                               "or an OpticStudio instance")
        known = {"Field", "Wavelength", "MaximumFrequency", "SampleSize", "Type", "ShowDiffractionLimit"}
        extra = sorted(set(settings) - known)
        if extra:
            raise SaosError(ErrorCode.BAD_ARGS, f"rtc FftMtf settings {extra} unknown; allowed {sorted(known)}")
        if str(settings.get("Type", "Modulation")) != "Modulation":
            raise _unsupported("rtc FftMtf computes the modulation only (Type Modulation)")
        lens = self._need_lens()

        def integer(key: str, top: int) -> int:
            v = settings.get(key, 0)
            if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= top:
                raise SaosError(ErrorCode.BAD_ARGS, f"FftMtf {key} must be 0–{top}, got {v!r}")
            return v

        field, wave = integer("Field", len(lens.fields)), integer("Wavelength", len(lens.active_wavelengths))
        size = str(settings.get("SampleSize", "S_64x64"))
        try:
            n = int(size.split("_")[1].split("x")[0]) if size.startswith("S_") else int(size)
        except (IndexError, ValueError):
            raise SaosError(ErrorCode.BAD_ARGS, f"FftMtf SampleSize must look like S_64x64, got {size!r}") from None
        if not 16 <= n <= MTF_MAX_SAMPLING:
            raise _unsupported(f"rtc FftMtf samples 16–{MTF_MAX_SAMPLING} pupil points per side, got {n}")
        top = settings.get("MaximumFrequency", 0)
        if isinstance(top, bool) or not isinstance(top, (int, float)) or not math.isfinite(top) or top < 0:
            raise SaosError(ErrorCode.BAD_ARGS, f"FftMtf MaximumFrequency must be a number ≥ 0, got {top!r}")
        series, problems = self._mtf_series([field] if field else list(range(1, len(lens.fields) + 1)),
                                            [wave] if wave else None, n, float(top), bool(settings.get("ShowDiffractionLimit", False)))
        self._used.add("FFTMTF")
        out = {"header": [f"rtc FFT MTF, {n} x {n} pupil samples, each wavelength about its own chief ray"], "messages": [],
               "series": series, "grids": [], "problems": problems, "text_file": False, "message": None, "backend": "rtc",
               "settings": {"Field": field, "Wavelength": wave, "MaximumFrequency": float(top), "SampleSize": f"S_{n}x{n}",
                            "Type": "Modulation", "ShowDiffractionLimit": bool(settings.get("ShowDiffractionLimit", False))}}
        out["status"] = "ok" if series and not problems else "partial" if series else "empty"
        return out

    def _mtf_series(self, fields: list[int], waves: list[int] | None, n: int, top: float, diffraction: bool):
        """FFT MTF 的曲线（OpticStudio 导出的形式：每个视场一条，y 为 Tangential、Sagittal），算不出的视场记进 problems。"""
        from rtc.psf import mtf_fft

        lens = self.lens
        system = self._system(lens.primary_wave)
        sx, sy = system._field_scale()
        unit = "deg" if lens.field_type == "ANGLE" else "mm"
        chosen = waves or list(range(1, len(lens.active_wavelengths) + 1))
        systems = [(self._system(w), 1.0 if waves else lens.active_wavelengths[w - 1][1]) for w in chosen]
        freq = np.linspace(0.0, top, 41) if top > 0 else None
        series, problems = [], []

        def curve(label: str, hx: float, hy: float, aberrated: bool) -> dict[str, Any]:
            nonlocal freq
            result = mtf_fft(systems, hx, hy, frequencies=freq, n=n, aberrated=aberrated)
            freq = result["frequency"]
            return {"description": label, "x_label": "Spatial Frequency in cycles per mm", "x": [float(v) for v in freq],
                    "y": {"Tangential": [float(v) for v in result["tangential"]], "Sagittal": [float(v) for v in result["sagittal"]]}}

        if diffraction:
            try:
                series.append(curve("Diff. Limit", 0.0, 0.0, False))
            except (ValueError, NotImplementedError, FloatingPointError) as exc:
                problems.append({"part": "diffraction limit", "kind": "computation", "message": str(exc)})
        for f in fields:
            x, y, _ = lens.fields[f - 1]
            try:
                series.append(curve(f"Field: {x:.4f}, {y:.4f} ({unit})", x / sx if sx else 0.0, y / sy if sy else 0.0, True))
            except (ValueError, NotImplementedError, FloatingPointError) as exc:
                problems.append({"part": f"field {f}", "kind": "computation", "message": str(exc)})
        return series, problems

    def system_info(self) -> dict[str, Any]:
        lens = self._need_lens()
        ap = (self.model or {}).get("aperture", {})
        return {
            **self.identity(), "file": str(self.path) if self.path else None, "lens_units": "Millimeters", "title": lens.name,
            "mode": "sequential", "object_thickness": lens.surface(0).thickness,
            "aperture": {"type": APERTURE_NAMES[lens.aperture_type], "value": _aperture_value(lens),
                         "telecentric_object": False, "afocal_image": bool(ap.get("afocal_image"))},
            "fields": {"type": {"ANGLE": "Angle", "OP1": "ObjectHeight"}.get(lens.field_type, lens.field_type),
                       "normalization": lens.field_normalization.capitalize(),
                       "items": [{"x": x, "y": y, "weight": w,
                                  "vignetting": dict(zip(("VDX", "VDY", "VCX", "VCY", "VAN"), vig))}
                                 for (x, y, w), vig in zip(lens.fields, lens.vignetting or [(0.0,) * 5] * len(lens.fields))]},
            "wavelengths": [{"um": um, "weight": w, "primary": k == lens.primary_wave} for k, (um, w) in enumerate(lens.active_wavelengths, 1)],
            "num_surfaces": len(lens.surfaces), "stop_surface": lens.stop_index,
            "environment": {"temperature": lens.environment.temperature, "pressure": lens.environment.pressure,
                            "adjust_index": lens.environment.adjust},
        }

    def surface_count(self) -> int:
        return len(self._need_lens().surfaces)

    def surfaces(self) -> list[dict[str, Any]]:
        self._need_lens()
        return [{"index": s.index, "type": s.type, "comment": s.comment, "radius": self._radius(s),
                 "thickness": self.lens.thickness(s.index, self.config), "material": self._material_text(s), "semi_diameter": s.semi_diameter,
                 "mech_semi_diameter": s.mech_semi_diameter, "conic": self._conic(s), "is_stop": s.is_stop, "solves": self._solves(s)} for s in self.lens.surfaces]

    @staticmethod
    def _radius(s) -> float:
        return math.inf if s.curvature == 0 else 1.0 / s.curvature

    @staticmethod
    def _conic(s) -> float:
        # 近轴面没有圆锥系数：ZOS-API 读作 inf、存盘为 0；按 OpticStudio 的读数给出，结构指纹才与批准记录一致
        # a paraxial surface has no conic: the ZOS-API reads inf and the file holds 0; report OpticStudio's reading so
        # the structural fingerprint matches approvals recorded there
        return math.inf if s.type.upper() == "PARAXIAL" else s.conic

    @staticmethod
    def _material(s) -> str | dict[str, float]:
        """get_param 的材料值：目录牌号、空气空串，模型玻璃为 {nd, vd, dpgf}（与 ZOS 后端相同）。"""
        from rtc.material import Model

        spec = s.material
        if s.mirror:
            return "MIRROR"
        return {"nd": spec.nd, "vd": spec.vd, "dpgf": spec.dpgf} if isinstance(spec, Model) else s.glass

    @staticmethod
    def _material_text(s) -> str:
        """surfaces() 里的材料文字：模型玻璃写成 "MODEL nd vd dpgf"，调用方可以一律当字符串处理。"""
        from rtc.material import Model

        spec = s.material
        if s.mirror:
            return "MIRROR"
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
                return abs(float(next(it.n_after for it in system.items if it.index == int(self._index(int(int1))))))
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
        from rtc.trace import trace

        hx, hy, px, py = (list(data) + [0.0] * 4)[:4]
        system = self._system(wave)
        block = trace(system, [px], [py], [hx], [hy])
        return self._ray_value(op, self._index(surf), system, block, 0, (hx, hy, px, py))

    def _ray_value(self, op: str, index: int, system, block, ray: int, coords: tuple) -> float:
        """已追迹光线块中第 ray 条光线在面 index 上的光线操作数；coords 为 (Hx, Hy, Px, Py)。"""
        from rtc.trace import launch

        if index < 1:
            raise SaosError(ErrorCode.BAD_ARGS, f"{op}: rays start at surface 1, got {index}")
        hx, hy, px, py = coords
        error = int(block.error[ray])
        if error and abs(error) <= index:
            how = "is totally internally reflected" if error < 0 else "misses"
            raise SaosError(ErrorCode.BAD_ARGS, f"{op}: the ray (Hx {hx}, Hy {hy}, Px {px}, Py {py}) {how} at surface {abs(error)}")
        k = block.surface_index.index(index)
        x, y, z = (float(v[ray, k]) for v in (block.x, block.y, block.z))
        l, m, n = (float(v[ray, k]) for v in (block.l, block.m, block.n))
        vertex = system.items[k].vertex_z
        values = {"REAX": x, "REAY": y, "REAZ": z, "REAR": math.hypot(x, y), "REAA": l, "REAB": m, "REAC": n,
                  "RAGX": x, "RAGY": y, "RAGZ": z + vertex, "RAGA": l, "RAGB": m, "RAGC": n,
                  # 手册：RANG 是相对该面局部 z 轴的光线角（弧度）/ the ray angle in radians to the local z axis
                  "RANG": math.atan2(math.hypot(l, m), n)}
        if op != "RAID":
            return values[op]
        if k > 0:
            d = np.array([block.l[ray, k - 1], block.m[ray, k - 1], block.n[ray, k - 1]])
        elif system.lens.ray_aiming == 0 or system.stop is None:
            try:
                d = np.asarray(launch(system, np.array([px]), np.array([py]), np.array([hx]), np.array([hy]))[3]).reshape(3)
            except NotImplementedError as exc:
                raise _unsupported(f"rtc: {exc}") from exc
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

    def surface_aperture(self, index: int) -> dict[str, Any]:
        surfaces = self._need_lens().surfaces
        if not 0 <= int(index) < len(surfaces):
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {index} out of range 0–{len(surfaces) - 1}")
        return self._aperture_spec(surfaces[int(index)])

    def huygens(self, wave: int, pupil: int, image: int) -> Any:
        # rtc.psf.psf_huygens 已有，但出瞳取法与 OpticStudio 未对账（变形面不一致），对账通过前不给数（ADR-0020）
        # rtc.psf.psf_huygens exists, but its exit pupil is not reconciled with OpticStudio (anamorphic surfaces differ)
        raise _unsupported("rtc: the Huygens PSF is not reconciled with OpticStudio yet (ADR-0020); measure it on OpticStudio")

    def wavefront_rms(self, wave: int, fields: list[int | tuple[float, float]]) -> list[float | None]:
        raise _unsupported("rtc: the RMS wavefront error is not reconciled with OpticStudio yet (ADR-0020); measure it on OpticStudio")

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

        from saos.lensdata import pupil

        lens = self._need_lens()
        index = len(lens.surfaces) - 2
        if f"{index}.thickness" in self.held:
            raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"surface {index} thickness is held by a {self.held[f'{index}.thickness']} solve")
        cell = self._surface_cell(index, "thickness")
        if (cell is None or cell.solve_code not in (0, 1)) and any(r.op == "THIC" and r.surface == index for r in lens.mce):
            raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"surface {index} thickness comes from a multi-configuration row")
        before = lens.thickness(index, self.config)
        grid = np.asarray(pupil(21), dtype=float)
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
            if cell is not None:
                cell.value, cell.edited = surface.thickness, True
            self._changed()
            if abs(shift) <= 1e-9 * max(1.0, abs(surface.thickness)):
                break
        return {"surface": index, "thickness_before": before, "thickness_after": lens.thickness(index, self.config), "backend": "rtc"}

    # ---- 设计阶段：变量、评价函数与优化（ADR-0017）/ the design stage: variables, merit function, optimisation ----

    def _designing(self, what: str):
        if not self.standalone:
            raise _unsupported(f"{what} runs on the rtc instance (saos daemon start --backend rtc) or on OpticStudio, not on an rtc copy")
        return self._need_lens()

    def variables(self) -> list[str]:
        """当前的变量（get_param 的键），按面序：曲率、厚度、圆锥、Par 列、模型玻璃的 nd 与 vd。换料变量不在其中。
        The current variables as get_param keys, in surface order; substituted materials are not among them."""
        lens = self._designing("variables")
        out = []
        for s in lens.surfaces:
            i = s.index
            if s.solve_radius == 1:
                out.append(f"{i}.curvature")
            if "VDSZ" in s.raw:
                out.append(f"{i}.thickness")
            if "VCON" in s.raw:
                out.append(f"{i}.conic")
            if s.semi_variable:
                out.append(f"{i}.semi_diameter")
            out += [f"{i}.par{n}" for n in sorted(s.variable_params)]
            if self.material_vars.get(i, {}).get("mode") == "model":
                out += [f"{i}.nd", f"{i}.vd"]
        for j, (op, surface, par) in enumerate(lens.row_keys(), 1):
            out += [f"config.{c}.{j}" for c in range(1, self.configuration_count() + 1)
                    if (cell := lens.cell(op, surface, c, par)) is not None and cell.solve_code == 1]
        return out

    def vary(self, surface: int, param: str, on: bool, mode: str | None = None, catalog: str | None = None) -> str:
        from saos.lensdata import PAR_RE

        lens = self._designing("saos opt vary")
        if param not in ("radius", "curvature", "thickness", "conic", "material", "semi_diameter") and not PAR_RE.match(param):
            raise SaosError(ErrorCode.BAD_ARGS, "可调参数为 radius、thickness、conic、par<N>、material 或浮动孔径光阑的 semi_diameter")
        image = len(lens.surfaces) - 1
        if not (0 < surface < image or (surface == 0 and param == "thickness")):
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {surface} {param} cannot be a variable")
        s = lens.surface(surface)
        name = "radius" if param == "curvature" else param
        if on and f"{surface}.{name}" in self.held:
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {surface} {name} is held by a {self.held[f'{surface}.{name}']} solve")
        cell = self._surface_cell(surface, param)
        if cell is not None:
            # 多重结构行盖住了 LDE 的格：变量要设在单元格上 / a configuration row overrides the LDE cell: the variable goes on the row
            if cell.solve_code not in (0, 1):
                raise SaosError(ErrorCode.BAD_ARGS, f"surface {surface} {name} follows configuration {cell.source_config} in configuration {self.config}; "
                                f"select configuration {cell.source_config} to make it a variable")
            cell.solve = [1.0 if on else 0.0, *cell.solve[1:]]
            cell.edited = True
            self._dirty = True
            return "Variable" if on else "Fixed"
        if param == "material":
            out = self._vary_material(s, on, mode, catalog)
        elif param == "semi_diameter":
            if on and (lens.aperture_type != "FLOA" or not s.is_stop or s.semi_diameter <= 0):
                raise SaosError(ErrorCode.BAD_ARGS, "semi_diameter 变量仅用于 Float By Stop Size 的光阑，数值必须大于零")
            s.semi_variable, s.semi_fixed = bool(on), True
        elif name == "radius":
            s.solve_radius = 1 if on else 0
        elif param == "thickness":
            if on and any(r.op == "THIC" and r.surface == surface for r in lens.mce):
                raise SaosError(ErrorCode.BAD_ARGS, f"surface {surface} thickness comes from multi-configuration rows; rtc varies plain cells only")
            if on and not math.isfinite(s.thickness):
                raise SaosError(ErrorCode.BAD_ARGS, f"surface {surface} thickness is infinite and cannot be a variable")
            if on:
                s.raw["VDSZ"] = "VDSZ 0 0"
            else:
                s.raw.pop("VDSZ", None)
        elif param == "conic":
            if on:
                s.raw["VCON"] = "VCON"
            else:
                s.raw.pop("VCON", None)
        else:
            n = int(PAR_RE.match(param).group(1))
            if s.type.upper() == "STANDARD":
                raise SaosError(ErrorCode.BAD_ARGS, f"surface {surface} is Standard and has no {param}")
            if on:
                s.variable_params.add(n)
            else:
                s.variable_params.discard(n)
        self._dirty = True
        return out if param == "material" else ("Variable" if on else "Fixed")

    def _vary_material(self, s, on: bool, mode: str | None, catalog: str | None) -> str:
        """模型玻璃（nd、νd 可变，起点取目录玻璃的 nd、νd）或目录内替换（只在 Hammer、全局搜索里换）。同 ZosBackend._vary_material。"""
        from rtc.material import Model, as_model

        if not on:
            self.material_vars.pop(s.index, None)
            return "MaterialModel" if isinstance(s.material, Model) else "Fixed"
        if mode == "model" and any(r.op == "GLSS" and r.surface == s.index for r in self.lens.mce):
            raise _unsupported(f"surface {s.index} has GLSS rows; a model glass cannot follow them on rtc")
        if mode not in MATERIAL_MODES:
            raise SaosError(ErrorCode.BAD_ARGS, f"material needs --mode {' or '.join(MATERIAL_MODES)}")
        if s.material is None:
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {s.index} has no glass (air); set a material before making it vary")
        if mode == "model":
            if not isinstance(s.material, Model):
                spec = _material_call(as_model, s.material, self._catalog(), self.lens.catalogs)
                self._set_material(s, Model(spec.nd, spec.vd, 0.0))
                self._changed()
            self.material_vars[s.index] = {"mode": "model"}
            return "MaterialModel"
        if isinstance(s.material, Model):
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {s.index} has a model glass; set a catalog glass before substitution")
        name = (catalog or "").upper()
        self._use_catalog(name)
        self.material_vars[s.index] = {"mode": "substitute", "catalog": name}
        return "MaterialSubstitute"

    # 面型的 SurfaceType 名（ZosBackend.change_surface_type 的返回值）/ SurfaceType names, as ZosBackend.change_surface_type returns
    SURFACE_TYPES = {"STANDARD": "Standard", "EVENASPH": "EvenAspheric"}

    def change_surface_type(self, index: int, surface_type: str) -> str:
        """Standard 与 EvenAspheric 之间换面型：换成偶次非球面时系数为 0（光学上不变），换回标准面时去掉系数。
        Switch between Standard and EvenAspheric: zero coefficients going aspheric, coefficients dropped going back."""
        from saos.backend import rtc_lens

        lens = self._designing("changing a surface type")
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index < len(lens.surfaces) - 1:
            raise SaosError(ErrorCode.BAD_ARGS, f"only surfaces 1–{len(lens.surfaces) - 2} can change type, got {index!r}")
        kind = rtc_lens.TYPES.get(rtc_lens._type_key(str(surface_type)))
        s = lens.surface(index)
        if s.mirror:
            raise _unsupported(f"surface {index} is a mirror; only standard surfaces are reconciled as mirrors")
        if kind not in self.SURFACE_TYPES or s.type.upper() not in self.SURFACE_TYPES:
            raise _unsupported(f"rtc switches surfaces between Standard and EvenAspheric only; surface {index} is {s.type}, "
                               f"asked for {surface_type}")
        if kind != s.type.upper():
            if any(r.op == "CONN" and r.surface == index for r in lens.mce):
                raise _unsupported(f"surface {index} has CONN rows; change its type before adding them")
            if kind == "EVENASPH" and any(r.op == "PRAM" and r.surface == index for r in lens.mce):
                raise _unsupported(f"surface {index} already has PRAM rows")
            s.type = kind
            s.params, s.extra, s.variable_params = {}, {}, set()
            self._asphere_rows(index, kind)
            self._changed()
        return self.SURFACE_TYPES[kind]

    def _asphere_rows(self, index: int, kind: str) -> None:
        """热结构里换面型时同步 PRAM 行，系数才跟着温度走。/ Keep the PRAM rows in step, so the terms follow the temperature."""
        from rtc import thermal

        lens = self.lens
        if lens.plain_configurations:
            return
        if kind == "EVENASPH":
            if thermal.follows(lens, index):
                lens.surface(index).params = {n: 0.0 for n in range(1, thermal.ASPHERE_ROWS + 1)}
                thermal.add_asphere_rows(lens, index)
        else:
            thermal.drop_asphere_rows(lens, index)

    def set_solve(self, index: int, param: str, solve: dict[str, Any]) -> dict[str, Any]:
        """rtc 实例只有 fixed 与 variable 两种求解（同 saos opt vary）；材料只能 fixed。/ Fixed and variable solves only."""
        kind = solve.get("type")
        if kind not in ("fixed", "variable") or (param == "material" and kind != "fixed"):
            raise _unsupported(f"rtc sets fixed and variable solves only (material: fixed), got {kind!r} on surface {index} {param}")
        if param == "material":
            self.vary(index, "material", False)
            return {"type": self._solves(self.lens.surface(index))["material"]}
        self.vary(index, param, kind == "variable")
        return {"type": "Variable" if kind == "variable" else "Fixed"}

    def _glass_dir(self) -> Path:
        return Path(self._catalog().folder)

    def _glass_table(self, catalog: str) -> list:
        """目录的牌号表（saos.glasscat，取自 rtc 用的玻璃文件夹）；找不到或读不了时为空。"""
        from saos import glasscat

        key = catalog.upper()
        if key not in self._glass_tables:
            try:
                self._glass_tables[key] = glasscat.load_catalogs([catalog], self._glass_dir())
            except (OSError, ValueError):
                self._glass_tables[key] = []
        return self._glass_tables[key]

    def glass_status(self, name: str, catalogs: list[str]) -> str | None:
        for catalog in catalogs:
            for g in self._glass_table(catalog):
                if g.name.upper() == name.upper():
                    return g.status
        return None

    def substitute_catalog(self, catalog: str | None) -> str:
        """同 ZosBackend.substitute_catalog：指定的目录，否则成都光明在产目录（玻璃文件夹里有时），再否则全部在用目录。"""
        name = (catalog or "").upper()
        if not name and any(p.stem.upper() == DEFAULT_SUBSTITUTE_CATALOG for p in self._glass_dir().glob("*.[Aa][Gg][Ff]")):
            name = DEFAULT_SUBSTITUTE_CATALOG
        return name

    def catalog_has(self, name: str, catalog: str) -> bool | None:
        lens = self._need_lens()
        if not catalog or not name or not any(self.glass_status(name, [c]) is not None for c in lens.catalogs):
            return None
        table = self._glass_table(catalog)
        return None if not table else any(g.name.upper() == name.upper() for g in table)

    def _use_catalog(self, name: str) -> None:
        """替换只在在用目录里找牌号：不在就加进 GCAT（这是对镜头的改动）/ add the catalog to GCAT when not in use."""
        from saos.protocol import MaterialError

        lens = self.lens
        if not name or name in (c.upper() for c in lens.catalogs):
            return
        if not self._glass_table(name):
            raise MaterialError(ErrorCode.BAD_ARGS, f"cannot load glass catalog {name} from {self._glass_dir()}")
        lens.raw["GCAT"] = "GCAT " + " ".join([*lens.catalogs, name]) + " "
        self._systems.clear()
        self._dirty = True

    def _substitutes(self) -> dict[int, list[str]]:
        """换料变量的各面可换的牌号：所选目录（空为全部在用目录）里 n、V 离现在的玻璃最近的 SUBSTITUTE_NEIGHBOURS 种，含现在的。
        Per substituted surface, the glasses of its catalog nearest in n and V to the current one, the current one included."""
        from rtc.material import as_model

        from saos import glasscat

        out = {}
        for i, var in sorted(self.material_vars.items()):
            if var["mode"] != "substitute":
                continue
            s = self.lens.surface(i)
            spec = _material_call(as_model, s.material, self._catalog(), self.lens.catalogs)
            pool = [g for c in ([var["catalog"]] if var["catalog"] else self.lens.catalogs) for g in self._glass_table(c)]
            near = glasscat.nearest(spec.nd, spec.vd, pool, nd_tol=0.03, vd_tol=6.0, k=SUBSTITUTE_NEIGHBOURS)
            out[i] = list(dict.fromkeys([s.glass.upper(), *(g.name.upper() for _, g in near)]))
        return out

    def apply_values(self, values: dict[str, float]) -> None:
        """一次改多个参数、只重算一次（求解与自动半口径）；任何一项或重算失败时全部恢复，报 VALUE_NOT_APPLIED 或原错误。
        Set several parameters with one recomputation; on any failure everything is restored."""
        self._need_lens()
        before = {k: self.get_param(k) for k in values}
        self._batch = True
        try:
            for key, value in values.items():
                self.set_param(key, value)
        except BaseException:
            self._restore(before)
            raise
        finally:
            self._batch = False
        try:
            self._changed()
        except SaosError as exc:
            self._restore(before)
            raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"the lens cannot be computed with these values: {exc.message}",
                            {"values": values}) from exc

    def _restore(self, before: dict[str, Any]) -> None:
        self._batch = True
        try:
            for key, value in before.items():
                self.set_param(key, value)
        finally:
            self._batch = False
        self._changed()

    def _evaluate_merit(self):
        from saos.backend import rtc_merit

        self._designing("the merit function")
        self._used.update(r.type for r in self.merit if r.type not in rtc_merit.INERT)
        return rtc_merit.Evaluator(self, self.merit).evaluate()

    def merit_wizard(self, settings: dict[str, Any]) -> int:
        """rtc 的序列优化向导（rtc_merit.wizard_rows）；start_at 之前的行保留，其后换成向导的行（同 OpticStudio）。"""
        from saos.backend import rtc_merit

        lens = self._designing("the optimisation wizard")
        start_at = settings.get("start_at", 1)
        if isinstance(start_at, bool) or not isinstance(start_at, int) or not 1 <= start_at <= len(self.merit) + 1:
            raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard start_at must be 1–{len(self.merit) + 1}, got {start_at!r}")
        rows = self.merit[:start_at - 1] + rtc_merit.wizard_rows(lens, settings)
        rtc_merit.Evaluator.check(rows)
        self.merit, self._dirty = rows, True
        return len(self.merit)

    def merit_add(self, rows: list[dict[str, Any]]) -> int:
        self.merit_append(rows)
        return len(self.merit)

    def merit_append(self, rows: list[dict[str, Any]]) -> list[int]:
        """同 ZosBackend.merit_append：插在第一个 DMFS 之前；rtc 算不了的操作数整批拒绝，评价函数不变。"""
        from saos.backend import rtc_merit
        from saos.ctx.rows import compile_rows

        self._designing("merit rows")
        position = next((k for k, r in enumerate(self.merit) if r.type == "DMFS"), len(self.merit))
        try:
            specs = compile_rows(rows, position + 1)
        except ValueError as exc:
            raise SaosError(ErrorCode.BAD_ARGS, f"merit rows: {exc}") from exc
        added = rtc_merit.from_specs(specs)
        merged = self.merit[:position] + added + self.merit[position:]
        rtc_merit.Evaluator.check(merged)
        self.merit, self._dirty = merged, True
        return list(range(position + 1, position + 1 + len(added)))

    def merit_clear(self) -> None:
        self._designing("the merit function")
        self.merit, self._dirty = [], True

    def merit_value(self) -> float:
        """OpticStudio 定义的评价函数值；有正权重的行算不出时为 inf。/ The merit value; inf when a weighted row fails."""
        return self._evaluate_merit().merit if self.merit else 0.0

    def merit_rows(self) -> list[dict[str, Any]]:
        if not self.merit:
            return []
        result = self._evaluate_merit()
        return [{"row": k + 1, "type": r.type, "target": r.target, "weight": r.weight, "value": result.values[k],
                 "contribution": result.contributions[k], **({"error": result.failed[k]} if k in result.failed else {})}
                for k, r in enumerate(self.merit) if r.type != "BLNK"]

    def _problem(self):
        from saos.backend import rtc_merit, rtc_optim

        keys = self.variables()
        if not keys:
            raise SaosError(ErrorCode.BAD_ARGS, "the lens has no variables; make some with saos opt vary first")
        if not any(r.weight > 0 and r.type not in rtc_merit.INERT for r in self.merit):
            raise SaosError(ErrorCode.BAD_ARGS, "the merit function has no row with a positive weight; build it with saos opt merit")
        self._used.update(r.type for r in self.merit if r.type not in rtc_merit.INERT)
        return rtc_optim.Problem(self, self.merit, keys)

    def optimize(self, cycles: str, cores: int | None = None, *, max_seconds: float | None = None,
                 on_progress=None, should_cancel=None, algorithm: str | None = None) -> dict[str, Any]:
        """带精确约束的阻尼最小二乘（rtc_optim.local）；cycles 换成迭代次数（auto 到收敛，至多 CYCLES["auto"] 次）。
        结果字段同 ZosBackend.optimize，另有约束数、违背量和是否收敛。cores 不用（单线程）。
        Damped least squares with exact constraints; cycles become iterations. Result fields as ZosBackend.optimize plus
        constraint counts, violation and convergence. cores is unused (single thread)."""
        import time

        from saos.backend import rtc_optim

        self._designing("local optimisation")
        if cycles not in CYCLES:
            raise SaosError(ErrorCode.BAD_ARGS, f"cycles must be one of {list(CYCLES)}")
        if algorithm not in (None, "auto", "dls"):
            raise _unsupported(f"rtc optimises with damped least squares only (dls), got {algorithm!r}")
        problem = self._problem()
        deadline = time.monotonic() + float(max_seconds) if max_seconds is not None else None
        run = rtc_optim.local(problem, iterations=CYCLES[cycles], deadline=deadline, should_cancel=should_cancel, on_progress=on_progress)
        return {**rtc_optim.summary(run, problem), "backend": "rtc"}

    def optimize_search(self, kind: str, cores: int | None = None, *, max_seconds: float,
                        on_progress=None, should_cancel=None, rel: float | None = None) -> dict[str, Any]:
        """Hammer 或全局搜索（rtc_optim.search），到 max_seconds 或取消为止；结束后镜头是找到的最好解。找到的系统写在镜头文件旁
        （全局搜索 GLOPT_NNNN.zmx 按评价函数从小到大，Hammer 为 <镜头>_0000_HAMMER.zmx），由后台进程归档，同 OpticStudio。
        Hammer or global search until max_seconds or a cancel; the lens ends on the best solution. The systems found are
        written next to the lens file for the daemon to archive, as OpticStudio does."""
        from saos.backend import rtc_optim
        from saos.budget import outcome

        lens = self._designing("Hammer and global search")
        problem = self._problem()
        before = [self._material_text(s) for s in lens.surfaces]
        result = rtc_optim.search(problem, kind, max_seconds=max_seconds, substitutes=self._substitutes(),
                                  should_cancel=should_cancel, on_progress=on_progress, rel=rel, workers=max(1, int(cores or 1)))
        self._write_search(kind, result, problem)
        final = self.merit_value()
        initial = result["initial_merit"]
        improved = outcome(initial, final, converged=False, stopped_by=result["stopped_by"])["improved"]
        found = math.isfinite(result["best"].merit)
        out = {"initial_merit": initial, "final_merit": final, "systems": result["systems"], "variables": len(problem.keys),
               "targets": sum(1 for r in self.merit if r.weight > 0 and r.type not in ("BLNK", "DMFS")),
               "succeeded": improved and found and result["stopped_by"] is None, "solver_succeeded": found, "improved": improved,
               "converged": None, "stopped_by": result["stopped_by"], "elapsed_s": result["elapsed_s"], "seed": result["seed"],
               "best_at_s": result["best_at_s"], "checkpoints": result["checkpoints"], "workers": result["workers"],
               "substitutes": {str(s): len(names) for s, names in self._substitutes().items()}, "backend": "rtc"}
        if not found:
            out["note"] = "no solution met every constraint; the lens is back at its start"
        if kind == "global":
            out["best_merits"] = [c.merit for c in result["kept"]]
        out["materials_changed"] = [{"surface": i, "before": a, "after": b, "status": self.glass_status(b, list(lens.catalogs))}
                                    for i, (a, b) in enumerate(zip(before, (self._material_text(s) for s in lens.surfaces)))
                                    if a.upper() != b.upper()]
        return out

    def _write_search(self, kind: str, result: dict[str, Any], problem) -> None:
        """把搜索找到的系统写在镜头文件旁（没存过盘的镜头不写），最后回到最好解。"""
        if self.path is None:
            return
        best = result["best"]
        folder, stem = self.path.parent, self.path.stem
        chosen = result["kept"] if kind == "global" else ([best] if math.isfinite(best.merit) else [])
        try:
            for k, candidate in enumerate(chosen):
                for s, name in candidate.glasses.items():
                    self.set_param(f"{s}.material", name)
                problem.apply(candidate.x)
                self._write(folder / (f"GLOPT_{k:04d}.zmx" if kind == "global" else f"{stem}_{k:04d}_HAMMER.zmx"))
        finally:
            for s, name in best.glasses.items():
                self.set_param(f"{s}.material", name)
            problem.apply(best.x)

    # ---- 流程图窗口 / the workflow window ----

    @staticmethod
    def _aperture_spec(surface) -> dict[str, Any]:
        """面孔径写成 ZOS 后端 _aperture_dict 的形式，交给 saos.geometry。"""
        code = surface.aperture_code
        common = {"ApertureXDecenter": surface.ap_dx, "ApertureYDecenter": surface.ap_dy}
        if code == 1:
            return {"type": "CircularAperture", "MinimumRadius": surface.ap_min, "MaximumRadius": surface.ap_max, **common}
        if code == 2:
            return {"type": "CircularObscuration", "MinimumRadius": surface.ap_min, "MaximumRadius": surface.ap_max, **common}
        if code == 4:
            return {"type": "RectangularAperture", "XHalfWidth": surface.ap_min, "YHalfWidth": surface.ap_max, **common}
        if code in (5, 6, 7):
            kind = {5: "RectangularObscuration", 6: "EllipticalAperture", 7: "EllipticalObscuration"}[code]
            return {"type": kind, "XHalfWidth": surface.ap_min, "YHalfWidth": surface.ap_max, **common}
        if code == 3:
            return {"type": "Spider", "NumberOfArms": int(surface.ap_max), "WidthOfArms": surface.ap_min, **common}
        return {"type": "FloatingAperture" if code == 10 else "None"}

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

    def viz_data(self, folder: Path | None, *, fan: int = 7, full: bool = True, rays: bool = True) -> dict[str, Any]:
        """流程图窗口的数据，结构同 ZosBackend.viz_data：逐面几何、布局光线、Lens Data、交叠检查；full 时（每轮的 viz.json）
        另有点列图（spot）、光线扇图（fan）和 FFT MTF（mtf）。另带 backend 与 unverified，窗口据此标明数值来自 rtc。
        Data for the workflow window shaped like ZosBackend.viz_data; with full also the spot, ray fan and FFT MTF.
        backend and unverified tell the window the numbers come from rtc."""
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
                masks = geometry.obscuration_paths(self._aperture_spec(surface), sd)
                if any(masks.values()):
                    item["obscuration"] = {key: [[[x, y, (self.sag(i, x, y) or 0.0) + off[2]] for x, y in line]
                                                   for line in paths] for key, paths in masks.items()}
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
        out = {
            "backend": "rtc", "unverified": self.unverified(),
            "system": {**{k: info[k] for k in ("title", "lens_units", "aperture", "fields", "wavelengths", "stop_surface")},
                       "primary_wave": primary, "normalization": lens.field_normalization.capitalize(), "normalized_fields": norm,
                       "stop_semi_diameter": stop["semi_diameter"] if stop else None, "image_surface": image,
                       "non_axial": any(s.type in ("BICONICX", "BICONIC", "BICONICZ", "TOROIDAL") for s in lens.surfaces),
                       "backend": "rtc"},
            "surfaces": surfaces,
            "layout": {"wave": primary, "rays": paths, **({} if rays else {"rays_paused": True})},
            "lens_data": self.surfaces(),
            "interference": clash,
            "timings_s": timings, "errors": errors,
        }
        if not full:
            return out
        for key, make in (("spot", lambda: self._spot_plot(norm)), ("ray_fan", lambda: self._fan_plot(norm)), ("mtf", lambda: self._mtf_plot(errors))):
            started = time.monotonic()
            try:
                out[key] = make()
            except (SaosError, ValueError, NotImplementedError, FloatingPointError) as exc:
                errors[key if key != "mtf" else "FftMtf"] = getattr(exc, "message", None) or str(exc)
            timings[key] = round(time.monotonic() - started, 3)
        return out

    def _spot_plot(self, norm, pupil_grid: int = 31) -> dict[str, Any]:
        """点列图：方形网格取单位圆内的光瞳点，各视场各波长追到像面，相对主波长主光线（同 ZosBackend.viz_data）；
        RMS、GEO 为全部波长按权重合在一起、相对同一主光线的值（µm），每个波长一列同值，形式同 OpticStudio 的 Standard Spot。
        The spot diagram as ZosBackend.viz_data draws it; RMS and GEO over every wavelength by weight about the primary
        chief ray (µm), one equal value per wavelength column as OpticStudio's Standard Spot gives."""
        from rtc.trace import trace

        lens = self.lens
        grid = np.array([2 * k / (pupil_grid - 1) - 1 for k in range(pupil_grid)])
        px, py = np.meshgrid(grid, grid)
        inside = px * px + py * py <= 1.0
        px, py = px[inside], py[inside]
        waves = lens.active_wavelengths
        fields, rms, geo = [], [], []
        for f, (hx, hy) in enumerate(norm):
            chief = trace(self._system(lens.primary_wave), [0.0], [0.0], hx, hy)
            ok_chief = int(chief.error[0]) == 0 and int(chief.vig[0]) == 0
            cx, cy = (float(chief.x[0, -1]), float(chief.y[0, -1])) if ok_chief else (0.0, 0.0)
            per_wave, r2, wt, far = [], 0.0, 0.0, 0.0
            for w, (_, weight) in enumerate(waves, 1):
                block = trace(self._system(w), px, py, hx, hy)
                ok = (block.error == 0) & (block.vig == 0)
                dx, dy = block.x[ok, -1] - cx, block.y[ok, -1] - cy
                per_wave.append([[round(float(a), 7), round(float(b), 7)] for a, b in zip(dx, dy)])
                if ok.any():
                    r2 += float(weight) * float(np.mean(dx * dx + dy * dy))
                    wt += float(weight)
                    far = max(far, float(np.max(np.hypot(dx, dy))))
            fields.append({"field": f, "chief": [cx, cy] if ok_chief else None, "points": per_wave})
            value = 1000.0 * math.sqrt(r2 / wt) if wt > 0 and ok_chief else None
            rms.append([value] * len(waves))
            geo.append([1000.0 * far if ok_chief else None] * len(waves))
        return {"reference": "chief ray, primary wavelength", "pupil_grid": pupil_grid, "fields": fields, "rms_radius": rms,
                "geo_radius": geo, "analysis_settings": {"ReferTo": "ChiefRay", "Pattern": "square grid", "RayDensity": pupil_grid}}

    def _fan_plot(self, norm, points: int = 21) -> dict[str, Any]:
        """光线扇图：子午扇（Px = 0）的 ey 与弧矢扇（Py = 0）的 ex，相对主波长主光线，各波长一条，单位同镜头；
        追不到像面的点为 None。/ Ray fans about the primary chief ray: ey along Py and ex along Px, one curve per wavelength."""
        from rtc.trace import trace

        lens = self.lens
        pupil = [2 * k / (points - 1) - 1 for k in range(points)]
        zero = np.zeros(points)
        p = np.array(pupil)
        fields = []
        for f, (hx, hy) in enumerate(norm):
            chief = trace(self._system(lens.primary_wave), [0.0], [0.0], hx, hy)
            if int(chief.error[0]) or int(chief.vig[0]):
                fields.append({"field": f, "tangential": None, "sagittal": None})
                continue
            cx, cy = float(chief.x[0, -1]), float(chief.y[0, -1])
            tangential, sagittal = [], []
            for w in range(1, len(lens.active_wavelengths) + 1):
                system = self._system(w)
                t_block, s_block = trace(system, zero, p, hx, hy), trace(system, p, zero, hx, hy)
                t_ok = (t_block.error == 0) & (t_block.vig == 0)
                s_ok = (s_block.error == 0) & (s_block.vig == 0)
                tangential.append([float(v - cy) if k else None for v, k in zip(t_block.y[:, -1], t_ok)])
                sagittal.append([float(v - cx) if k else None for v, k in zip(s_block.x[:, -1], s_ok)])
            fields.append({"field": f, "tangential": tangential, "sagittal": sagittal})
        return {"pupil": pupil, "reference": "chief ray, primary wavelength", "fields": fields}

    def _mtf_plot(self, errors: dict[str, str]) -> dict[str, Any]:
        series, problems = self._mtf_series(list(range(1, len(self.lens.fields) + 1)), None, 64, 0.0, True)
        if problems:
            errors["FftMtf"] = "; ".join(f"{p['part']}: {p['message']}" for p in problems)
        if not any(s["description"] != "Diff. Limit" for s in series):
            raise ValueError(errors.get("FftMtf") or "no field gave an MTF")
        return {"series": series, "spot": None, "settings": {"SampleSize": "S_64x64", "Type": "Modulation", "ShowDiffractionLimit": True,
                                                              "backend": "rtc"}}

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
        from saos.lensdata import surface

        return surface(self, ref)

    def get_param(self, key: str) -> Any:
        from saos.lensdata import PAR_RE, parse_key

        kind, ref, param = parse_key(key)
        lens = self.lens
        if kind == "system":
            if ref == "aperture":
                return _aperture_value(lens)
            if ref == "wave":
                return lens.active_wavelengths[self._slot(param, len(lens.active_wavelengths), "wave")][0]
            if ref in ("field_x", "field_y"):
                return lens.fields[self._slot(param, len(lens.fields), "field")][0 if ref == "field_x" else 1]
            if ref == "config":
                cell = self._config_cell(param)
                if cell.op == "GLSS":
                    return cell.text
                if cell.op == "THIC":
                    return lens.thickness(cell.surface, cell.config)
                if not lens.plain_configurations and cell.solve_code not in (0, 1):
                    from rtc import thermal

                    thermal.refresh(lens)
                return cell.value
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
        if param == "conic":
            return self._conic(s)
        if param in ("semi_diameter", "mech_semi_diameter", "comment"):
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
        from saos.lensdata import PAR_RE, parse_key

        kind, ref, param = parse_key(key)
        lens = self.lens
        if kind == "system":
            self._guard(key)
            number = parse_numeric(key, value)
            if ref == "aperture":
                if lens.aperture_type == "FLOA":
                    raise _unsupported("float by stop size has no aperture value; the stop surface's semi_diameter sets the aperture")
                lens.aperture_value = number
            elif ref == "wave":
                k = self._slot(param, len(lens.active_wavelengths), "wave")
                self._validate_wavelength(number)
                lens.wavelengths[k] = (number, lens.wavelengths[k][1])
            elif ref in ("field_x", "field_y"):
                k = self._slot(param, len(lens.fields), "field")
                x, y, w = lens.fields[k]
                lens.fields[k] = (number, y, w) if ref == "field_x" else (x, number, w)
            elif ref == "config":
                cell = self._config_cell(param)
                if cell.solve_code not in (0, 1):
                    raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"{key} follows configuration {cell.source_config}; make it fixed first")
                if cell.op == "GLSS":
                    raise _unsupported(f"rtc cannot write {key}: set the material of the surface in its configuration")
                cell.value, cell.edited = number, True
            else:
                raise _unsupported(f"rtc cannot write {key}")
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
        cell = self._surface_cell(index, param)
        if cell is not None and cell.solve_code not in (0, 1):
            raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"surface {index} {param} follows configuration {cell.source_config} in configuration {self.config}; "
                            "change it there")
        if PAR_RE.match(param) and cell is None and not lens.plain_configurations and any(r.op == "PRAM" and r.surface == index for r in lens.mce):
            raise _unsupported(f"surface {index} {param}: multi-configuration rows hold Par 1–8 only, so this term would not follow the temperature")
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
            if any(r.op == "THIC" and r.surface == index and r.config == self.config for r in lens.mce) and self._surface_cell(index, param) is None:
                raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"surface {index} thickness comes from a multi-configuration row in configuration {self.config}")
            number = parse_numeric(param, value)
            if index == 0 and math.isfinite(number) and lens.aperture_type in ("EPD", "FNO", "FLOA") and not self.standalone:
                raise _unsupported("rtc traces an entrance pupil aperture with the object at infinity only")
            s.thickness = number
        elif param == "conic" and s.type.upper() == "PARAXIAL":
            # 读出的 inf 原样写回（kept 恢复时）不改任何东西；其他值无处可写
            if not math.isinf(parse_numeric(param, value)):
                raise SaosError(ErrorCode.BAD_ARGS, f"surface {index} is Paraxial and has no conic")
        elif param in ("conic", "semi_diameter", "mech_semi_diameter"):
            number = parse_numeric(param, value)
            check_size(param, number)
            if param == "semi_diameter" and lens.aperture_type == "FLOA" and s.is_stop and number <= 0:
                raise SaosError(ErrorCode.BAD_ARGS, "Float By Stop Size 的光阑半口径必须大于零")
            setattr(s, param, number)
            if param == "semi_diameter":
                s.semi_fixed = True
                if s.aperture_code == 10:
                    s.ap_max = number
        elif PAR_RE.match(param):
            n = int(PAR_RE.match(param).group(1))
            s.params[n] = parse_numeric(param, value)
            if n > 12:
                s.extra[n - 12] = s.params[n]
        else:
            raise _unsupported(f"rtc cannot write {param}")
        self._write_cell(index, param)
        self._changed()

    def _write_cell(self, index: int, param: str) -> None:
        """改了 LDE 的值：有这一行时同步写进当前结构的单元格（set_param 已挡住拾取和热跟随的格）。"""
        s = self.lens.surface(index)
        cell = self._surface_cell(index, param)
        if cell is None:
            if param == "material":
                for row in self.lens.mce:
                    # 当前结构和跟随它的格；别的结构固定写的玻璃不动 / this configuration and its pickups, not glasses fixed elsewhere
                    if row.op == "GLSS" and row.surface == index and (row.config == self.config or row.solve_code == 2):
                        row.text, row.edited = s.glass, True
            return
        from saos.lensdata import PAR_RE

        match = PAR_RE.match(param)
        value = s.params[int(match.group(1))] if match else s.curvature if param in ("radius", "curvature") else getattr(s, param)
        cell.value, cell.edited = float(value), True

    def _guard(self, key: str) -> None:
        # standalone 自己重算求解（_recompute），不需要冻结 / a standalone instance recomputes its solves itself
        if self.held and not self.freeze_solves and not self.standalone:
            raise _unsupported(f"cannot change {key}: the lens has solves rtc does not recompute ({self.held}); "
                               "pass freeze_solves=True to keep their saved values fixed in rtc, or make them fixed")

    def _set_material(self, s, value: Any) -> None:
        from rtc.material import Catalog, Mirror, material_spec, resolve

        spec = _material_call(material_spec, value)
        if s.index == 0 and spec is not None:
            raise _unsupported("rtc assumes air in object space")
        if not isinstance(spec, Catalog) and any(r.op == "GLSS" and r.surface == s.index for r in self.lens.mce):
            raise _unsupported(f"surface {s.index} has GLSS rows; they hold catalog glasses only")
        # 增删反射面会翻转后面所有面的符号和厚度，属于换结构，rtc 只接受建模时就有的反射面
        if isinstance(spec, Mirror) or s.mirror:
            raise _unsupported(f"surface {s.index}: rtc cannot add or remove a mirror on a built lens; rebuild the lens")
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

        from saos.lensdata import Rays

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
