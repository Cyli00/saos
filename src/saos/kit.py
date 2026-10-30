"""ZOS-API 常用操作的通用函数，省去每个任务重写同一批封装（ADR-0013：数值都由计算后端算，这里只做调用和统计）。
Reusable ZOS-API helpers, so that jobs stop rewriting the same wrappers (ADR-0013: the backend computes every number;
this module only calls it and does bookkeeping and statistics on what it returns).

exec 脚本的 ctx、ctx.map 的 worker 上下文和判定脚本的 ctx 都带同名方法（ctx.set(...)、ctx.spot(...)），也可以直接调用
kit.<函数>(ctx, ...)。/ ctx in exec scripts, ctx.map workers and check scripts carry these as methods (ctx.set(...),
ctx.spot(...)); kit.<function>(ctx, ...) works as well.

参数键 / parameter keys:
    "<面>.<参数>"   面是序号（"3"、"-1" 为像面）或 Lens Data 的注释（"lens1 back"）；
                    参数：radius、curvature（1/R，0 为平面）、thickness、conic、semi_diameter、mech_semi_diameter（零件外缘）、
                    material、comment、nd、vd、dpgf、par<N>
    "aperture"      系统孔径值 / the system aperture value
    "field.<n>.x"、"field.<n>.y"、"wave.<n>"   第 n 个视场坐标、第 n 个波长（µm）/ field n coordinates, wavelength n (µm)
    "config.<c>.<r>" 多重结构第 r 行在结构 c 的值（变焦间隔、温度等）/ multi-configuration row r in configuration c
    "object.<物体>.<参数>" 非序列物体：物体是序号或注释；参数为 x、y、z、tilt_x、tilt_y、tilt_z、material、comment、
                    par<N> 或参数列标题（"Z Length"）、mean_path（体散射平均自由程）、vp.<DLL 参数名>（"vp.g"）
                    / a non-sequential object by number or comment: position, tilt, material, comment, a parameter by
                    par<N> or column header, mean_path, or vp.<DLL parameter name>

非序列系统：ctx.nsc_trace(...) 追迹（默认分裂、散射、偏振都开，留 2 个逻辑核），ctx.detector(物体) 读探测器，
ctx.objects() 列出物体。/ Non-sequential systems: ctx.nsc_trace(...), ctx.detector(object), ctx.objects().

多重结构的判定按结构分别测量：with ctx.configuration(c): ...，块结束时回到原来的结构，判定结束时镜头不变。
Checks of a multi-configuration lens measure each configuration inside with ctx.configuration(c): ...; the original
configuration returns when the block ends, so the lens is unchanged after the checks.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Sequence

import numpy as np

from saos.backend.base import PAR_RE
from saos.protocol import ErrorCode, SaosError, finite

PARAMS = ("radius", "curvature", "thickness", "conic", "semi_diameter", "mech_semi_diameter", "material", "comment", "nd", "vd", "dpgf")
_ROW_ATTRS = {"radius": "Radius", "thickness": "Thickness", "conic": "Conic", "semi_diameter": "SemiDiameter",
              "mech_semi_diameter": "MechanicalSemiDiameter"}
_INT_RE = re.compile(r"^-?\d+$")
_SYSTEM_RE = re.compile(r"^(aperture|wave\.(\d+)|field\.(\d+)\.([xy])|config\.(\d+)\.(\d+))$")
_OBJECT_ATTRS = {"x": "XPosition", "y": "YPosition", "z": "ZPosition", "tilt_x": "TiltAboutX", "tilt_y": "TiltAboutY", "tilt_z": "TiltAboutZ"}
_FACE_KEY_RE = re.compile(r"^face(\d+)\.(is|coating|fraction|sigma|rays)$", re.IGNORECASE)
# ReadNextResult 的返回：(成功, 光线号, 错误, 渐晕, X, Y, Z, L, M, N, l2, m2, n2, opd, 强度)
# ReadNextResult returns (ok, ray number, error, vignette, X, Y, Z, L, M, N, l2, m2, n2, opd, intensity)
_READ_ARGS = (0, 0, 0, *([0.0] * 11))


# ---------- 系统对象 / system objects ----------

def system(ctx):
    """OpticStudio 的系统对象；假后端没有时返回 None。/ The OpticStudio system object; None on the fake backend."""
    backend = ctx.backend
    return getattr(backend, "system", None)


def zosapi(ctx):
    from saos.backend.zos import _load_zosapi

    return _load_zosapi(ctx.backend.root)


def _need_system(ctx, what: str):
    sysm = system(ctx)
    if sysm is None:
        # rtc 上算作当前后端算不了（判定的 backend_unsupported），其他后端仍是参数错 / on rtc a backend gap, elsewhere a bad call
        code = ErrorCode.BACKEND_UNSUPPORTED if ctx.backend.identity().get("backend") == "rtc" else ErrorCode.BAD_ARGS
        raise SaosError(code, f"{what} needs OpticStudio; {type(ctx.backend).__name__} has none")
    return sysm


# ---------- 面与参数 / surfaces and parameters ----------

def surfaces_by_comment(ctx) -> dict[str, int]:
    """注释 → 面序号（重名时取第一个）。/ comment → surface index (the first one when a comment repeats)."""
    sysm = system(ctx)
    found: dict[str, int] = {}
    if sysm is None:
        for s in ctx.backend.surfaces():
            found.setdefault(str(s.get("comment", "")), int(s["index"]))
    else:
        lde = sysm.LDE
        for i in range(lde.NumberOfSurfaces):
            found.setdefault(str(lde.GetSurfaceAt(i).Comment), i)
    found.pop("", None)
    return found


def _count(ctx) -> int:
    sysm = system(ctx)
    return int(sysm.LDE.NumberOfSurfaces) if sysm is not None else len(ctx.backend.surfaces())


def surface(ctx, ref: int | str, _names: dict[str, int] | None = None) -> int:
    """面序号：整数原样（负数从像面倒数，-1 为像面），字符串按注释查找。
    Surface index: an int as is (negative counts back from the image, -1 is the image), a string by its comment."""
    count = _count(ctx)
    if isinstance(ref, str) and _INT_RE.match(ref.strip()):
        ref = int(ref)
    if isinstance(ref, int) and not isinstance(ref, bool):
        index = ref + count if ref < 0 else ref
        if not 0 <= index < count:
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {ref} out of range 0–{count - 1}")
        return index
    names = surfaces_by_comment(ctx) if _names is None else _names
    if ref not in names:
        raise SaosError(ErrorCode.BAD_ARGS, f"no surface has the comment {ref!r}", {"comments": sorted(names)})
    return names[ref]


def info(ctx) -> dict[str, Any]:
    """系统设置（saos lens info 结果里的 system）：孔径、视场、波长、在用的玻璃目录等。/ The system part of saos lens info."""
    return ctx.backend.system_info()


def surface_params(ctx, ref: int | str) -> list[dict[str, Any]]:
    """该面型的参数列：列号、列标题、按单元格类型读出的值、是否只读，与 objects() 的 params 同形。数值列用 ctx.set({"<面>.par<N>": 值}) 写。
    The surface type's parameter columns with number, header, value by cell type and read-only flag, shaped like objects()'s params."""
    from saos.backend.zos import cell_value, param_cells

    sysm = _need_system(ctx, "surface_params")
    row = sysm.LDE.GetSurfaceAt(surface(ctx, ref))
    return [{"par": n, "name": str(c.Header), "value": cell_value(c), "read_only": bool(c.IsReadOnly)}
            for n, c in param_cells(zosapi(ctx), row).items()]


def parse_key(key: str) -> tuple[str, Any, str]:
    """("system", 名, 序号) 或 ("surface", 面, 参数)。/ ("system", name, n) or ("surface", ref, param)."""
    key = str(key).strip()
    if key.startswith("object."):
        ref, sep, param = key[len("object."):].partition(".")
        if not sep or not ref or not param:
            raise SaosError(ErrorCode.BAD_ARGS, f"parameter key {key!r}: use object.<number or comment>.<param>")
        return "object", ref, param
    m = _SYSTEM_RE.match(key)
    if m:
        if m.group(1) == "aperture":
            return "system", "aperture", ""
        if m.group(2):
            return "system", "wave", m.group(2)
        if m.group(5):
            return "system", "config", f"{m.group(5)}.{m.group(6)}"
        return "system", f"field_{m.group(4)}", m.group(3)
    ref, sep, param = key.rpartition(".")
    if not sep or not ref or not (param in PARAMS or PAR_RE.match(param)):
        raise SaosError(ErrorCode.BAD_ARGS, f"parameter key {key!r}: use <surface>.<param> with param in {PARAMS} or par<N>, "
                                            "or aperture, field.<n>.x|y, wave.<n>, config.<c>.<row>")
    return "surface", ref, param


def _par_cell(ctx, row, param: str, index: int):
    from saos.backend.zos import par_cell

    return par_cell(zosapi(ctx), row, index, param)


def _config_cell(sysm, param: str):
    """多重结构第 r 行在结构 c 的单元格。/ The cell of row r in configuration c."""
    c, r = (int(x) for x in param.split("."))
    mce = sysm.MCE
    count, rows = int(mce.NumberOfConfigurations), int(mce.NumberOfOperands)
    if count < 2 or not (1 <= c <= count and 1 <= r <= rows):
        raise SaosError(ErrorCode.BAD_ARGS, f"config.{c}.{r}: the lens has {count} configuration(s) and {rows} multi-configuration row(s)")
    return mce, c, mce.GetOperandAt(r).GetOperandCell(c)


def object_index(ctx, ref: int | str) -> int:
    """非序列物体的序号：整数原样，字符串按注释查找（重名时取第一个）。混合模式里写 <物体>@<NonSequential 面号> 选组，
    只有一个组时可以省略；查找前把该组设为当前组（NCE.SetActiveSurface）。
    An object number, or the first object with this comment. In mixed mode <object>@<NonSequential surface> picks the
    group (optional when there is only one); the group is made current before the lookup."""
    from saos.backend import nsc

    sysm = _need_system(ctx, "object")
    group = None
    if isinstance(ref, str) and "@" in ref:
        ref, _, g = ref.rpartition("@")
        if not _INT_RE.match(g.strip()):
            raise SaosError(ErrorCode.BAD_ARGS, f"object key: after @ comes the number of a NonSequential surface, got {g!r}")
        group = int(g)
    if not nsc.is_nsc(sysm):
        found = nsc.groups(sysm)
        if not found:
            raise SaosError(ErrorCode.BAD_ARGS, "the lens has no non-sequential objects (no NonSequential surface)")
        if group is None:
            if len(found) > 1:
                raise SaosError(ErrorCode.BAD_ARGS, f"the lens has several NonSequential surfaces {found}; write <object>@<surface>", {"groups": found})
            group = found[0]
        nsc.select_group(sysm, group)
    elif group is not None:
        raise SaosError(ErrorCode.BAD_ARGS, "a pure non-sequential system has one group; leave out @<surface>")
    nce = sysm.NCE
    count = int(nce.NumberOfObjects)
    if isinstance(ref, str) and _INT_RE.match(ref.strip()):
        ref = int(ref)
    if isinstance(ref, int) and not isinstance(ref, bool):
        if not 1 <= ref <= count:
            raise SaosError(ErrorCode.BAD_ARGS, f"object {ref} out of range 1–{count}")
        return ref
    names = {}
    for i in range(1, count + 1):
        names.setdefault(str(nce.GetObjectAt(i).Comment), i)
    if ref not in names:
        raise SaosError(ErrorCode.BAD_ARGS, f"no object has the comment {ref!r}", {"comments": sorted(n for n in names if n)})
    return names[ref]


def _vp_settings(obj, param: str):
    vp = obj.VolumePhysicsData
    if str(vp.Model) == "None":
        raise SaosError(ErrorCode.BAD_ARGS, f"object {obj.ObjectNumber} has no volume physics model for {param}")
    return getattr(vp.ModelSettings, "__implementation__", vp.ModelSettings)


def _vp_param(impl, name: str, number: int) -> int:
    names = [str(impl.GetParameterName(i)) for i in range(int(impl.NumberOfParameters))]
    for k, n in enumerate(names):
        if n.strip().lower() == name.strip().lower():
            return k
    raise SaosError(ErrorCode.BAD_ARGS, f"object {number}: {impl.DLL} has no parameter {name!r}", {"parameters": names})


def _glass(sysm, cell, material: str, label: str) -> dict[str, float]:
    """一种材料的 nd、vd、ΔPgF：模型玻璃读求解数据，目录玻璃查在用目录（ΔPgF 记 0）。空气、MIRROR、ABSORB 没有。
    nd, vd and ΔPgF of a material: a model glass from its solve, a catalog glass from the catalogs in use (ΔPgF 0)."""
    from saos.backend.zos import catalog_constants

    solve = cell.GetSolveData()
    if str(solve.Type) == "MaterialModel":
        d = solve._S_MaterialModel
        return {"nd": float(d.IndexNd), "vd": float(d.AbbeVd), "dpgf": float(d.dPgF)}
    name = material.strip().upper()
    found = catalog_constants(sysm, name) if name and name not in ("MIRROR", "ABSORB") else None
    if found is None:
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}: {name or 'air'} has no nd/vd to read or perturb")
    return {"nd": float(found[0]), "vd": float(found[1]), "dpgf": 0.0}


def _material_value(cell, material) -> str | dict[str, float]:
    """材料参数的读数：模型玻璃为 {nd, vd, dpgf}（可原样写回 material），其余为牌号文字。
    The material value: {nd, vd, dpgf} for a model glass (writable back as material), else the name."""
    solve = cell.GetSolveData()
    if str(solve.Type) == "MaterialModel":
        d = solve._S_MaterialModel
        return {"nd": float(d.IndexNd), "vd": float(d.AbbeVd), "dpgf": float(d.dPgF)}
    return str(material)


def _set_glass(ctx, sysm, cell, material: str, param: str, value: Any, label: str) -> None:
    """改 nd、vd 或 dpgf：把材料换成模型玻璃（其余两项取当前值；目录玻璃的 ΔPgF 记 0），用于折射率、阿贝数公差。
    Set nd, vd or dpgf by turning the material into a model glass (the other two keep their current values; a catalog
    glass's ΔPgF counts as 0): for index and Abbe tolerances."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}.{param} needs a number, got {value!r}")
    now = _glass(sysm, cell, material, label)
    now[param] = float(value)
    _set_model_material(ctx, cell, now, label)


def _set_model_material(ctx, cell, value: dict, label: str) -> None:
    if not {"nd", "vd"} <= value.keys() or value.keys() - {"nd", "vd", "dpgf"}:
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}: model material needs nd/vd and optional dpgf")
    values = {"nd": value["nd"], "vd": value["vd"], "dpgf": value.get("dpgf", 0.0)}
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values.values()) or values["nd"] <= 0:
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}: model material needs finite numbers and nd > 0")
    solve = cell.CreateSolveType(zosapi(ctx).Editors.SolveType.MaterialModel)
    data = solve._S_MaterialModel
    data.IndexNd, data.AbbeVd, data.dPgF = values["nd"], values["vd"], values["dpgf"]
    cell.SetSolveData(solve)
    got = cell.GetSolveData()._S_MaterialModel
    for name, attr in (("nd", "IndexNd"), ("vd", "AbbeVd"), ("dpgf", "dPgF")):
        actual = float(getattr(got, attr))
        if not math.isfinite(actual) or abs(actual - values[name]) > 1e-9 * max(1.0, abs(values[name])):
            raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"{label}: model material {name} did not take {values[name]}")


def _face(o, number: int, n: int):
    cs = o.CoatScatterData
    if not bool(cs.IsCoatScatterAvailable) or not 0 <= n < int(cs.NumberOfFaces):
        count = int(cs.NumberOfFaces) if bool(cs.IsCoatScatterAvailable) else 0
        raise SaosError(ErrorCode.BAD_ARGS, f"object {number} ({o.TypeName}) has {count} face(s), no face {n}")
    return cs.GetFaceData(n)


def _get_face(o, number: int, n: int, what: str) -> Any:
    from saos.backend import nsc

    data = nsc.read_face(_face(o, number, n))
    if what == "is":
        return data["is"]
    if what == "coating":
        return data.get("coating", "")
    if what not in data["scatter"]:
        raise SaosError(ErrorCode.BAD_ARGS, f"object {number} face {n}: the {data['scatter']['model']} scatter model has no {what}")
    return data["scatter"][what]


def _set_face(ctx, o, number: int, n: int, what: str, value: Any) -> None:
    """改一个面的一项：is、coating 直接写；fraction、sigma、rays 在当前散射模型上改这一项，其余字段照旧。"""
    from saos.backend import nsc
    from saos.prescription import FACE_IS

    face = _face(o, number, n)
    label = f"object {number} face {n}"
    if what == "is":
        if value not in FACE_IS:
            raise SaosError(ErrorCode.BAD_ARGS, f"{label}.is must be one of {list(FACE_IS)}, got {value!r}")
        spec: dict[str, Any] = {"is": value}
    elif what == "coating":
        spec = {"coating": str(value or "NONE").strip().upper()}
    else:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise SaosError(ErrorCode.BAD_ARGS, f"{label}.{what} needs a number, got {value!r}")
        scatter = dict(nsc.read_face(face)["scatter"])
        if what not in scatter:
            raise SaosError(ErrorCode.BAD_ARGS, f"{label}: the {scatter['model']} scatter model has no {what}")
        scatter[what] = int(value) if what == "rays" else float(value)
        spec = {"scatter": scatter}
    nsc.set_face(zosapi(ctx), face, spec, label)


def _get_object(ctx, sysm, ref: Any, param: str) -> Any:
    from saos.backend import nsc
    from saos.backend.zos import cell_value

    number = object_index(ctx, ref)
    o = sysm.NCE.GetObjectAt(number)
    m = _FACE_KEY_RE.match(param)
    if m:
        return _get_face(o, number, int(m.group(1)), m.group(2).lower())
    if param in _OBJECT_ATTRS:
        return float(getattr(o, _OBJECT_ATTRS[param]))
    if param == "material":
        return _material_value(o.MaterialCell, o.Material)
    if param == "comment":
        return str(o.Comment)
    if param in ("nd", "vd", "dpgf"):
        return _glass(sysm, o.MaterialCell, str(o.Material), f"object {number}")[param]
    if param == "mean_path":
        return float(_vp_settings(o, param).MeanPath)
    if param.startswith("vp."):
        impl = _vp_settings(o, param)
        return float(impl.GetParameterValue(_vp_param(impl, param[3:], number)))
    return cell_value(nsc.param_cell(zosapi(ctx), o, f"object {number}", param))


def _set_object(ctx, sysm, ref: Any, param: str, value: Any) -> None:
    from saos.backend import nsc
    from saos.backend.zos import set_cell

    number = object_index(ctx, ref)
    o = sysm.NCE.GetObjectAt(number)
    m = _FACE_KEY_RE.match(param)
    if m:
        _set_face(ctx, o, number, int(m.group(1)), m.group(2).lower(), value)
        return
    if param == "comment":
        if nsc.type_files(o):
            raise SaosError(ErrorCode.BAD_ARGS, f"object {number} ({o.TypeName}) keeps its file name in the comment column; writing it would change the file")
        o.Comment = "" if value is None else str(value)
        return
    if param == "material":
        if isinstance(value, dict):
            _set_model_material(ctx, o.MaterialCell, value, f"object {number}")
            return
        name = str(value).strip().upper()
        o.Material = name if name in ("", "ABSORB") else ctx.backend._check_material(name)
        return
    if param in ("nd", "vd", "dpgf"):
        _set_glass(ctx, sysm, o.MaterialCell, str(o.Material), param, value, f"object {number}")
        return
    if param in _OBJECT_ATTRS or param == "mean_path" or param.startswith("vp."):
        target = float(value)
        if param in _OBJECT_ATTRS:
            setattr(o, _OBJECT_ATTRS[param], target)
            actual = float(getattr(o, _OBJECT_ATTRS[param]))
        elif param == "mean_path":
            impl = _vp_settings(o, param)
            impl.MeanPath = target
            actual = float(impl.MeanPath)
        else:
            impl = _vp_settings(o, param)
            k = _vp_param(impl, param[3:], number)
            impl.SetParameterValue(k, target)
            actual = float(impl.GetParameterValue(k))
        if not (actual == target or abs(actual - target) <= 1e-12 * max(1.0, abs(target))):
            raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"object {number} {param} did not take {target}", {"requested": target, "actual": actual})
        return
    set_cell(nsc.param_cell(zosapi(ctx), o, f"object {number}", param), value, f"object {number} {param}")


def _get_zos(ctx, sysm, kind: str, ref: Any, param: str, names: dict[str, int]) -> Any:
    if kind == "object":
        return _get_object(ctx, sysm, ref, param)
    if kind == "system":
        sd = sysm.SystemData
        if ref == "config":
            from saos.backend.zos import cell_value

            return cell_value(_config_cell(sysm, param)[2])
        if ref == "aperture":
            return float(sd.Aperture.ApertureValue)
        if ref == "wave":
            return float(sd.Wavelengths.GetWavelength(int(param)).Wavelength)
        field = sd.Fields.GetField(int(param))
        return float(field.X if ref == "field_x" else field.Y)
    index = surface(ctx, ref, names)
    row = sysm.LDE.GetSurfaceAt(index)
    if param == "curvature":
        r = float(row.Radius)
        return 0.0 if r == 0 or math.isinf(r) else 1.0 / r
    if PAR_RE.match(param):
        return float(_par_cell(ctx, row, param, index).DoubleValue)
    if param == "material":
        return _material_value(row.MaterialCell, row.Material)
    if param == "comment":
        return str(row.Comment)
    if param in ("nd", "vd", "dpgf"):
        return _glass(sysm, row.MaterialCell, str(row.Material), f"surface {index}")[param]
    return float(getattr(row, _ROW_ATTRS[param]))


def _set_zos(ctx, sysm, kind: str, ref: Any, param: str, value: Any, names: dict[str, int]) -> None:
    if kind == "object":
        _set_object(ctx, sysm, ref, param, value)
        return
    if kind == "system":
        sd = sysm.SystemData
        if ref == "config":
            from saos.backend.zos import set_cell

            mce, c, cell = _config_cell(sysm, param)
            if isinstance(value, str) and mce.GetOperandAt(int(param.split(".")[1])).TypeName == "GLSS":
                value = ctx.backend._check_material(value)
            set_cell(cell, value, f"config.{param}")
            if c == int(mce.CurrentConfiguration):
                # 改的是当前结构：重新套用，镜头才跟着变 / the current configuration: re-apply it so the lens follows
                mce.SetCurrentConfiguration(c)
            return
        if ref == "aperture":
            sd.Aperture.ApertureValue = float(value)
        elif ref == "wave":
            sd.Wavelengths.GetWavelength(int(param)).Wavelength = float(value)
        else:
            field = sd.Fields.GetField(int(param))
            setattr(field, "X" if ref == "field_x" else "Y", float(value))
        return
    index = surface(ctx, ref, names)
    row = sysm.LDE.GetSurfaceAt(index)
    if param == "material":
        if isinstance(value, dict):
            _set_model_material(ctx, row.MaterialCell, value, f"surface {index}")
            return
        # 材料走后端的目录校验 / materials go through the backend's catalog check
        ctx.backend.set_surface(index, "material", value)
        return
    if param == "comment":
        row.Comment = "" if value is None else str(value)
        return
    if param in ("nd", "vd", "dpgf"):
        _set_glass(ctx, sysm, row.MaterialCell, str(row.Material), param, value, f"surface {index}")
        return
    target = float(value)
    if param == "curvature":
        target, param = (math.inf if target == 0 else 1.0 / target), "radius"
    if PAR_RE.match(param):
        cell = _par_cell(ctx, row, param, index)
        cell.DoubleValue = target
        actual = float(cell.DoubleValue)
    else:
        attr = _ROW_ATTRS[param]
        setattr(row, attr, target)
        actual = float(getattr(row, attr))
    if not (actual == target or abs(actual - target) <= 1e-12 * max(1.0, abs(target))):
        raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"surface {index} {param} did not take {target}; a solve or pickup may override it",
                        {"requested": target, "actual": actual})


def get(ctx, key: str) -> Any:
    """一个参数的当前值。/ The current value of one parameter."""
    return values(ctx, [key])[key]


def values(ctx, keys: Iterable[str]) -> dict[str, Any]:
    """几个参数的当前值。/ The current values of several parameters."""
    return {key: ctx.backend.get_param(key) for key in keys}


def set(ctx, changes: dict[str, Any] | None = None, **kw: Any) -> None:  # noqa: A001 - 与 ctx.set 同名 / mirrors ctx.set
    """一次写入几个参数，写不进去（被求解覆盖等）时报 VALUE_NOT_APPLIED。
    Write several parameters at once; VALUE_NOT_APPLIED when one does not take (a solve overrides it, …)."""
    changes = {**(changes or {}), **kw}
    if not changes:
        return
    for key, value in changes.items():
        ctx.backend.set_param(key, value)
    if hasattr(ctx, "mark_dirty"):
        ctx.mark_dirty()


@contextmanager
def kept(ctx, keys: Iterable[str]) -> Iterator[dict[str, Any]]:
    """with 块结束时把这些参数恢复原值（出错也恢复）。/ Restore these parameters when the with block ends, errors included."""
    before = values(ctx, keys)
    try:
        yield dict(before)
    finally:
        set(ctx, before)


def configurations(ctx) -> int:
    """结构数（单结构为 1）。/ The number of configurations (1 for a single one)."""
    return ctx.backend.configuration_count()


@contextmanager
def configuration(ctx, n: int) -> Iterator[None]:
    """with 块内切到第 n 个结构，结束时回到原来的结构（出错也回）。按结构分别测量的判定用它，判定结束时镜头不变；
    ctx.memo 的键要带上结构号。
    Switch to configuration n inside the block and back to the original one when it ends, errors included. Checks that
    measure each configuration use it, so the lens is unchanged after them; include the configuration in ctx.memo keys."""
    count = ctx.backend.configuration_count()
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= count:
        raise SaosError(ErrorCode.BAD_ARGS, f"configuration must be 1–{count}, got {n!r}")
    before = ctx.backend.current_configuration()
    ctx.backend.select_configuration(n)
    try:
        yield
    finally:
        ctx.backend.select_configuration(before)


@contextmanager
def restored(ctx) -> Iterator[None]:
    """with 块结束时整个镜头回到进入时的内容（存一份临时副本再加载，约 0.1 s），用于会增删面的试探。
    之后后端的文件路径（SystemFile）停在副本上，Hammer 与全局搜索要等 saos lens save 之后才能跑。
    The whole lens returns to its content at entry when the block ends (a temporary copy is saved and reloaded,
    about 0.1 s); for trials that insert or remove surfaces. The backend's file is then that copy: save before
    Hammer or global search."""
    handle, name = tempfile.mkstemp(prefix="saos-keep-", suffix=".zmx")
    os.close(handle)
    path = Path(name)
    ctx.backend.save_as(path)
    try:
        yield
    finally:
        ctx.backend.load(path)
        for p in path.parent.glob(path.stem + ".*"):
            p.unlink(missing_ok=True)


# ---------- 非序列 / non-sequential ----------

def objects(ctx) -> list[dict[str, Any]]:
    """非序列物体表：序号、类型、注释、参考物体、Inside Of、位置、倾斜、材料、参数、体散射、各波长折射率、全局变换。
    The non-sequential objects with type, comment, references, placement, material, parameters, volume physics and indices."""
    from saos.backend import nsc

    sysm = _need_system(ctx, "objects")
    if not nsc.is_nsc(sysm):
        raise SaosError(ErrorCode.BAD_ARGS, "objects: the lens is sequential")
    return nsc.objects(zosapi(ctx), sysm)


def nsc_trace(ctx, *, split: bool = True, scatter: bool = True, polarization: bool = True, ignore_errors: bool = True,
              seed: int | None = None, cores: int | None = None, clear: bool = True, save_rays: str | None = None,
              zrd_format: str = "CompressedFullData", ray_filter: str = "", max_seconds: float | None = None) -> dict[str, Any]:
    """非序列追迹，光线数取各光源的 # Analysis Rays。默认开光线分裂、散射和偏振（界面的菲涅耳反射和全反射靠它们），
    默认留 2 个逻辑核给用户；seed 固定时结果可复现（2026-10-20 实测逐位相同），判定应固定种子。
    进度写进 ctx.progress，saos job cancel 时调用 OpticStudio 的 Cancel 并返回 stopped_by。返回值见 backend.nsc.trace：
    dead_ray_errors 是 OpticStudio 报的出错光线能量，段数上限截断光线时变大，判定应以它为前提条件。
    A non-sequential trace; ray counts come from each source's # Analysis Rays. Splitting, scattering and polarization
    are on by default (Fresnel reflection and TIR need them) and 2 logical cores are left to the user; a fixed seed makes
    the result reproducible, so checks should fix it. dead_ray_errors is the energy of rays OpticStudio reports as errors;
    it grows when rays hit the segment limit, so checks should treat it as a precondition."""
    from saos.backend import nsc

    sysm = _need_system(ctx, "nsc_trace")
    if not nsc.is_nsc(sysm):
        raise SaosError(ErrorCode.BAD_ARGS, "nsc_trace: the lens is sequential; use trace or spot")
    if cores is None:
        cores = max(1, (os.cpu_count() or 4) - 2)
    report = getattr(ctx, "progress", None)
    cancel = getattr(ctx, "cancel_requested", None)
    return nsc.trace(sysm, split=split, scatter=scatter, polarization=polarization, ignore_errors=ignore_errors, seed=seed, cores=cores,
                     clear=clear, save_rays=save_rays, zrd_format=zrd_format, ray_filter=ray_filter, max_seconds=max_seconds,
                     on_progress=(lambda pct: report("nsc_trace", percent=pct)) if callable(report) else None,
                     should_cancel=cancel if callable(cancel) else None)


def detector(ctx, ref: int | str, grid: bool = False) -> dict[str, Any]:
    """探测器读数（上一次追迹留下的）：power（W）、hits、peak_pixel_power、pixels；grid 时另有逐像素功率和照度。
    Detector readings left by the last trace: power (W), hits, peak_pixel_power, pixels; with grid also per-pixel power and irradiance."""
    from saos.backend import nsc

    sysm = _need_system(ctx, "detector")
    return nsc.detector(zosapi(ctx), sysm, object_index(ctx, ref), grid=grid)


def ray_hits(ctx, zrd: str, objects: Iterable[int | str], ray_filter: str = "", max_rays: int | None = None) -> dict[str, Any]:
    """读 nsc_trace(save_rays=...) 存下的光线数据库，取光线到达这些物体（序号或注释）的各段：x、y、z、l、m、n、intensity、
    wave（波长序号）、object、ray，均为 numpy 数组；rays_read 是读过的光线数。zrd 是文件名（在镜头文件旁边）或完整路径。
    Read a ray database saved by nsc_trace(save_rays=...) and return the segments where rays reach these objects as numpy arrays."""
    from saos.backend import nsc

    sysm = _need_system(ctx, "ray_hits")
    path = Path(zrd)
    if not path.is_absolute():
        path = Path(str(sysm.SystemFile)).parent / path
    if not path.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"no ray database {path}")
    raw = nsc.ray_hits(zosapi(ctx), sysm, str(path), [object_index(ctx, o) for o in objects], ray_filter, max_rays)
    rays_read = raw.pop("rays_read")[0]
    out: dict[str, Any] = {k: np.asarray(v, dtype=int if k in ("wave", "object", "ray") else float) for k, v in raw.items()}
    out["rays_read"] = rays_read
    return out


# ---------- 操作数与分析 / operands and analyses ----------

def op(ctx, name: str, int1: int = 0, int2: int = 0, *data: float) -> float:
    """任意优化操作数的值，例如 op(ctx, "EFFL", 0, 1)、op(ctx, "SSAG", 3, 0, 0.0, 0.2)。
    The value of any merit operand, e.g. op(ctx, "EFFL", 0, 1), op(ctx, "SSAG", 3, 0, 0.0, 0.2)."""
    return ctx.backend.operand(str(name), int(int1), int(int2), [float(x) for x in data])


def analysis(ctx, name: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """运行一个分析并返回导出的数据（序列、网格、表头），不留文件；要存证据用 saos lens analysis。
    Run an analysis and return its exported data (series, grids, header) without keeping files; use saos lens analysis for evidence."""
    folder = Path(tempfile.mkdtemp(prefix="saos-analysis-"))
    try:
        return ctx.backend.analysis(str(name), dict(settings or {}), folder / "out.txt")
    finally:
        for p in folder.glob("*"):
            p.unlink(missing_ok=True)
        folder.rmdir()


def quick_focus(ctx) -> dict[str, Any]:
    return ctx.backend.quick_focus()


def primary_wave(ctx) -> int:
    return ctx.backend.primary_wave()


def material_nd(ctx, ref: int | str) -> float:
    """面材料的 nd：模型玻璃取求解里的 nd，目录材料从启用的目录里查（材料目录工具）。
    The nd of a surface's material: a model glass from its solve, a catalog material from the catalogs in use."""
    return float(ctx.backend.get_param(f"{surface(ctx, ref)}.nd"))


def global_matrix(ctx, ref: int | str) -> tuple[np.ndarray, np.ndarray]:
    """面局部坐标 → 全局坐标：(3×3 旋转, 平移)，全局点 = R @ 局部点 + t。
    Local → global coordinates of a surface: (3×3 rotation, translation); global = R @ local + t."""
    return ctx.backend.global_matrix(surface(ctx, ref))


def sag(ctx, ref: int | str, x: float, y: float) -> float | None:
    """面在 (x, y) 处的矢高（LDE.GetSag）；该点没有定义时为 None。/ Surface sag at (x, y); None where undefined."""
    return ctx.backend.sag(surface(ctx, ref), float(x), float(y))


def aperture(ctx, ref: int | str) -> dict[str, Any]:
    """面孔径设置：type（CircularAperture、FloatingAperture、None …）及 MinimumRadius、MaximumRadius 等。
    A surface's aperture: type (CircularAperture, FloatingAperture, None, ...) with MinimumRadius, MaximumRadius, ..."""
    return ctx.backend.surface_aperture(surface(ctx, ref))


# ---------- 衍射 PSF 与波前 / diffraction PSF and wavefront ----------

@dataclass
class Psf:
    """一张 Huygens PSF：values[行 y][列 x] 为不归一化的强度（同一会话内可比），dx、dy 为像元间距（µm）。
    One Huygens PSF: values[row y][column x] are unnormalised intensities (comparable within a session); dx, dy in µm."""

    values: list[list[float]]
    dx: float
    dy: float

    def peak(self, n_photon: int = 1) -> float:
        return _photons(max(max(row) for row in self.values), n_photon)


def _photon_order(n_photon: Any) -> int:
    if isinstance(n_photon, bool) or not isinstance(n_photon, int) or n_photon < 1:
        raise SaosError(ErrorCode.BAD_ARGS, f"n_photon must be a positive integer (1 for linear, 2 for two-photon), got {n_photon!r}")
    return n_photon


def _photons(v: float, n_photon: int) -> float:
    # 逐次相乘而不是 v ** n：双光子与原先手写的 v * v 逐位相同 / repeated products, so two-photon matches a hand-written v * v bit for bit
    out = v
    for _ in range(_photon_order(n_photon) - 1):
        out *= v
    return out


class _PsfSession:
    def __init__(self, session):
        self._session = session

    def psf(self, field: int | tuple[float, float], delta_um: float) -> Psf | None:
        """field 为视场号或镜头视场单位下的 (x, y)；delta_um 为像元间距。分析没有数据时为 None。
        field is a field number or (x, y) in the lens's field units; delta_um the pixel pitch. None when there is no data."""
        got = self._session.psf(field, delta_um)
        return None if got is None else Psf(*got)


@contextmanager
def huygens(ctx, wave: int | None = None, pupil: int = 64, image: int = 128) -> Iterator[_PsfSession]:
    """Huygens PSF 会话（默认主波长，光瞳 64×64、像面 128×128 采样）。会话内换视场、像元间距或镜头参数（ctx.set、ctx.kept）后
    再调 psf() 就重新计算；按坐标给的视场在会话结束时从视场表删掉。离焦扫描、多视场都在一个会话里做。
    A Huygens PSF session (primary wave, 64×64 pupil and 128×128 image sampling by default). Calling psf() again after
    changing the field, pixel pitch or the lens (ctx.set, ctx.kept) recomputes; fields given as coordinates are removed
    when the session ends. Do focus scans and field sweeps inside one session."""
    session = ctx.backend.huygens(primary_wave(ctx) if wave is None else int(wave), int(pupil), int(image))
    try:
        yield _PsfSession(session)
    finally:
        session.close()


def fwhm(values: Sequence[float], step: float) -> float | None:
    """一维剖面的半高全宽：从首个最大值向两侧找第一次降到一半以下的位置，线性插值；最大值不为正或半高不在采样窗口内时为 None。
    Full width at half maximum of a profile: from the first maximum outwards to the first drop below half, linearly
    interpolated; None when the peak is not positive or a half-maximum crossing lies outside the window."""
    values = [float(v) for v in values]
    if not values:
        return None
    peak = max(values)
    if not peak > 0:
        return None
    k = values.index(peak)
    half = peak / 2
    left = right = None
    for i in range(k, 0, -1):
        if values[i - 1] < half <= values[i]:
            left = (i - 1) + (half - values[i - 1]) / (values[i] - values[i - 1])
            break
    for i in range(k, len(values) - 1):
        if values[i + 1] < half <= values[i]:
            right = i + (values[i] - half) / (values[i] - values[i + 1])
            break
    if left is None or right is None:
        return None
    return (right - left) * float(step)


def psf_widths(p: Psf, n_photon: int = 1, ee: float = 0.8) -> dict[str, float | None]:
    """横向宽度（µm）：强度取 n_photon 次方后，fwhm_x、fwhm_y 是过首个峰值像素的行、列剖面的半高全宽；ee_diameter 是以强度质心为圆心、
    包含 ee 份能量的最小像素圆直径。取不到的量为 None。
    Lateral widths (µm) of the intensity raised to n_photon: fwhm_x, fwhm_y across the row and column through the first
    peak pixel; ee_diameter is the diameter of the pixel circle about the intensity centroid holding the fraction ee.
    Values that cannot be found are None."""
    if not 0 < float(ee) <= 1:
        raise SaosError(ErrorCode.BAD_ARGS, f"ee must be a fraction in (0, 1], got {ee!r}")
    n = _photon_order(n_photon)
    rows = [[_photons(v, n) for v in row] for row in p.values]
    iy, ix = max(((r, c) for r in range(len(rows)) for c in range(len(rows[0]))), key=lambda rc: rows[rc[0]][rc[1]])
    out: dict[str, float | None] = {"fwhm_x": fwhm(rows[iy], p.dx), "fwhm_y": fwhm([row[ix] for row in rows], p.dy), "ee_diameter": None}
    total = sum(map(sum, rows))
    if not total > 0:
        return out
    cx = sum(v * c for row in rows for c, v in enumerate(row)) / total
    cy = sum(v * r for r, row in enumerate(rows) for v in row) / total
    pix = sorted(((math.hypot((c - cx) * p.dx, (r - cy) * p.dy), v) for r, row in enumerate(rows) for c, v in enumerate(row)))
    acc = 0.0
    for rad, v in pix:
        acc += v
        if acc >= float(ee) * total:
            out["ee_diameter"] = 2 * rad
            break
    return out


def psf_axial(peak_at: Callable[[int], float], step_um: float, reaches: Sequence[int] = (15, 30), *, n_photon: int = 1) -> dict[str, Any] | None:
    """轴向宽度：peak_at(k) 给出离焦 k 步（k·step_um）处 PSF 的峰值（单光子强度，调用方负责移像面并在之后恢复），
    取 n_photon 次方后在 −reach…reach 步上求半高全宽；半高不在窗口内时换下一个 reach（已算的离焦面复用），都不够时为 None。
    peak_offset_um 是采样中首个最大值的离焦量，分辨率为一步。
    Axial width: peak_at(k) is the PSF peak (single-photon intensity) k steps of step_um out of focus; the caller moves
    the plane and restores it afterwards. Raised to n_photon, the FWHM is taken over −reach…reach steps; when a
    half-maximum crossing falls outside, the next reach is tried, reusing planes already computed; None when none
    suffices. peak_offset_um is the defocus of the first sampled maximum, resolved to one step."""
    n = _photon_order(n_photon)
    if not (math.isfinite(float(step_um)) and float(step_um) > 0) or not reaches:
        raise SaosError(ErrorCode.BAD_ARGS, "psf_axial needs a positive step_um and at least one reach")
    cache: dict[int, float] = {}

    def value(k: int) -> float:
        if k not in cache:
            cache[k] = _photons(float(peak_at(k)), n)
        return cache[k]

    for reach in reaches:
        ks = list(range(-int(reach), int(reach) + 1))
        curve = [value(k) for k in ks]
        width = fwhm(curve, step_um)
        if width is not None:
            j = max(range(len(curve)), key=lambda i: curve[i])
            return {"fwhm_um": width, "peak_offset_um": ks[j] * float(step_um), "reach": int(reach),
                    "curve": [(k * float(step_um), v) for k, v in zip(ks, curve)]}
    return None


def wavefront_rms(ctx, field: int | tuple[float, float], wave: int | None = None) -> float | None:
    """光线积分的 RMS 波前误差（波长，参考质心：扣除平移与倾斜，默认主波长）；field 同 huygens 的 psf()；算不出时为 None。
    RMS wavefront error from ray integration (waves, to centroid: piston and tilt removed; primary wave by default);
    field as in huygens' psf(); None when not computable."""
    return ctx.backend.wavefront_rms(primary_wave(ctx) if wave is None else int(wave), field)


# ---------- 光线 / rays ----------

@dataclass
class Rays:
    """批量追迹的结果，每个量是一维数组；坐标在目标面的局部坐标系、镜头单位。ok = 没有错误、没有渐晕、数值有限。
    Batch trace results, one array per quantity; coordinates in the target surface's local frame, lens units.
    ok = no error, not vignetted, finite."""

    wave: np.ndarray
    hx: np.ndarray
    hy: np.ndarray
    px: np.ndarray
    py: np.ndarray
    error: np.ndarray
    vignette: np.ndarray
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    l: np.ndarray  # noqa: E741 - 方向余弦沿用 ZOS-API 的名字 / direction cosines keep the ZOS-API names
    m: np.ndarray
    n: np.ndarray
    intensity: np.ndarray
    surface: int

    @classmethod
    def from_rows(cls, rays: np.ndarray, rows: np.ndarray, surface: int) -> Rays:
        return cls(wave=rays[:, 0].astype(int), hx=rays[:, 1], hy=rays[:, 2], px=rays[:, 3], py=rays[:, 4],
                   error=rows[:, 0].astype(int), vignette=rows[:, 1].astype(int), x=rows[:, 2], y=rows[:, 3], z=rows[:, 4],
                   l=rows[:, 5], m=rows[:, 6], n=rows[:, 7], intensity=rows[:, 8], surface=surface)

    @property
    def ok(self) -> np.ndarray:
        finite = np.isfinite(self.x) & np.isfinite(self.y) & np.isfinite(self.z) & np.isfinite(self.l) & np.isfinite(self.m) & np.isfinite(self.n)
        return (self.error == 0) & (self.vignette == 0) & finite

    def select(self, mask: np.ndarray) -> Rays:
        return Rays(**{k: (getattr(self, k)[mask] if k != "surface" else self.surface) for k in self.__dataclass_fields__})


def pupil(n: int = 21) -> list[tuple[float, float]]:
    """单位圆内的 n×n 方格光瞳采样点（归一化坐标）。/ An n×n square grid of normalised pupil points inside the unit circle."""
    if n < 2:
        return [(0.0, 0.0)]
    # 与各任务原有的写法逐位一致：-1 + 2i/(n-1)，外层 px / bit-identical to the jobs' own grids: -1 + 2i/(n-1), px outer
    grid = [-1 + 2 * i / (n - 1) for i in range(n)]
    return [(px, py) for px in grid for py in grid if px * px + py * py <= 1 + 1e-12]


def trace(ctx, rays: Iterable[Iterable[float]], to: int | str | None = None) -> Rays:
    """归一化坐标的批量实际光线追迹。rays：(wave, hx, hy, px, py) 的序列；to：目标面，默认像面。
    Batch real-ray trace in normalised coordinates. rays: (wave, hx, hy, px, py) rows; to: target surface, default the image."""
    arr = np.asarray(rays if isinstance(rays, (np.ndarray, list, tuple)) else list(rays), dtype=float).reshape(-1, 5)
    target = _count(ctx) - 1 if to is None else surface(ctx, to)
    return ctx.backend.trace_batch(arr, target)


def spread(x: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    """点列的质心、RMS 半径和最大半径（相对质心）/ centroid, RMS radius and largest radius about the centroid"""
    cx, cy = float(x.mean()), float(y.mean())
    d2 = (x - cx) ** 2 + (y - cy) ** 2
    return {"centroid": [cx, cy], "rms_radius": float(math.sqrt(d2.mean())), "geo_radius": float(math.sqrt(d2.max()))}


def best_focus(rays: Rays) -> float | None:
    """沿目标面局部 z 平移多少时弥散斑 RMS 最小（闭式解）。前提：目标面之后是均匀介质，光线是直线；
    这只是把 OpticStudio 追迹出的位置和方向沿直线外推，不是追迹。
    The shift along the target surface's local z that minimises the RMS spot (closed form). Precondition: a homogeneous
    medium after the target surface, so rays are straight; this extrapolates OpticStudio's traced rays, it traces nothing."""
    good = rays.ok & (np.abs(rays.n) > 1e-12)
    return focus_shift(rays.x[good], rays.y[good], rays.l[good] / rays.n[good], rays.m[good] / rays.n[good])


def focus_shift(x: np.ndarray, y: np.ndarray, u: np.ndarray, v: np.ndarray) -> float | None:
    """best_focus 的闭式解本身：位置 (x, y)、斜率 (u, v) = (l/n, m/n) 的直线束沿 z 平移多少时 RMS 最小。
    少于 3 条光线，或各光线斜率相同（平行光，没有焦点）时为 None。
    The closed form behind best_focus for straight rays at (x, y) with slopes (u, v) = (l/n, m/n). None with fewer
    than 3 rays or when every slope is the same (a collimated beam has no focus)."""
    if len(x) < 3:
        return None
    du, dv = u - u.mean(), v - v.mean()
    den = float((du * du + dv * dv).sum())
    return None if den == 0 else float(-((x - x.mean()) * du + (y - y.mean()) * dv).sum() / den)


def spot(ctx, fields: Iterable[Iterable[float]] = ((0.0, 0.0),), wave: int | Iterable[int] | None = None, grid: int = 21,
         to: int | str | None = None, refocus: bool = False, min_rays: int = 3) -> list[dict[str, Any]]:
    """各视场（和波长）的点列统计，一次批量追迹完成。每行：field、wave、launched、arrived、pass_rate（%）、
    chief（主光线落点，到不了时 None）、centroid、rms_radius、geo_radius（相对质心，镜头单位）；refocus=True 时另给
    best_dz（见 best_focus）和该处的 rms_radius_best。到达光线少于 min_rays 的行 valid 为 False，统计量为 None。
    Spot statistics per field (and wavelength) from one batch trace. Each row: field, wave, launched, arrived,
    pass_rate (%), chief (None when it does not arrive), centroid, rms_radius, geo_radius (about the centroid, lens
    units); with refocus=True also best_dz (see best_focus) and rms_radius_best there. Rows with fewer than min_rays
    arriving rays have valid False and None statistics."""
    try:
        afocal = bool(ctx.backend.system_info().get("aperture", {}).get("afocal_image"))
    except (SaosError, AttributeError, TypeError):
        afocal = False
    if to is None and afocal:
        # 无焦系统的像在无穷远：像面上的落点不反映像质，量会被当成镜头单位误读
        # an afocal system images to infinity: landing points on the image surface say nothing and would be read as lengths
        raise SaosError(ErrorCode.BAD_ARGS, "the system is afocal (image at infinity), so spot sizes on the image surface mean nothing; "
                        "measure the angular spread from the ray directions (ctx.trace gives l, m, n) or an operand/analysis "
                        "that reports afocal units, or pass to=<a surface> deliberately")
    fields = [tuple(float(v) for v in f) for f in fields]
    waves = [primary_wave(ctx)] if wave is None else [int(wave)] if isinstance(wave, (int, np.integer)) else [int(w) for w in wave]
    grid_pts = pupil(grid)
    # 每个视场×波长一段：先主光线，再光瞳网格
    block = np.zeros((len(grid_pts) + 1, 5))
    block[1:, 3:] = grid_pts
    per = len(block)
    rows = [{"field": [hx, hy], "wave": w} for hx, hy in fields for w in waves]
    rays = np.tile(block, (len(rows), 1))
    rays[:, 0] = np.repeat([r["wave"] for r in rows], per)
    rays[:, 1] = np.repeat([r["field"][0] for r in rows], per)
    rays[:, 2] = np.repeat([r["field"][1] for r in rows], per)
    result = trace(ctx, rays, to)
    ok = result.ok
    for k, row in enumerate(rows):
        start = k * per
        chief_ok = bool(ok[start])
        row["chief"] = [float(result.x[start]), float(result.y[start])] if chief_ok else None
        sub = result.select(start + 1 + np.flatnonzero(ok[start + 1:start + per]))
        arrived = len(sub.x)
        row.update(launched=len(grid_pts), arrived=arrived, pass_rate=100.0 * arrived / len(grid_pts), valid=arrived >= min_rays)
        if arrived >= min_rays:
            row.update(spread(sub.x, sub.y))
            if refocus:
                dz = best_focus(sub)
                row["best_dz"] = dz
                if dz is not None:
                    keep = np.abs(sub.n) > 1e-12
                    row["rms_radius_best"] = spread(sub.x[keep] + sub.l[keep] / sub.n[keep] * dz,
                                                     sub.y[keep] + sub.m[keep] / sub.n[keep] * dz)["rms_radius"]
                else:
                    row["rms_radius_best"] = None
        else:
            row.update(centroid=None, rms_radius=None, geo_radius=None)
            if refocus:
                row.update(best_dz=None, rms_radius_best=None)
    return rows


# ---------- 加密采样与极值 / dense sampling and extremes ----------
#
# 一次任务里补全视场 CRA、加密光谱透过率时，边缘一点或五个波长都漏掉过极值（nightvision，2026-10-19：中间视场的 CRA
# 达 19.88°，加密光谱后透过率出现五波长之间的低谷）。这些函数按同一个格式交回：量的定义和单位、采样范围与步长、极值和
# 它的位置、所有局部极值、读不出的点、加密后极值移动了多少，以及覆盖是否够（coverage）。
# Filling in the full-field CRA and a denser transmission spectrum on one job, an edge point or five wavelengths missed the
# extreme (nightvision, 2026-10-19: 19.88° CRA in mid field, a transmission dip between the five wavelengths). These
# functions all return the quantity's definition and unit, the sampled range and step, the extreme and where it is, every
# local extreme, unreadable points, how far refining moved the extreme, and whether the coverage is enough.

# profile 一次最多等距取的点数 / the most equally spaced points one profile takes
PROFILE_MAX_N = 10001


def profile(evaluate: Callable[[list[float]], Iterable[float | None]], lo: float, hi: float, n: int = 33, *, find: str = "max",
            refine: int = 2, points: int = 8, rel: float = 1e-3) -> dict[str, Any]:
    """在 [lo, hi] 上等距取 n 点求极值，再在当前极值两侧相邻样点之间加密 refine 次（每次 points 点）。evaluate 一次收到一批
    x、按序返回值（None 或非有限值算读不出）；一批一次调用，便于批量追迹。coverage.ok 为假的情形：有读不出的点、加密让极值
    变化超过 rel（相对）、或有效样点少于 3 个。at_edge 表示极值在区间端点，区间外可能更极端。
    Find the extreme over n equally spaced points on [lo, hi], then refine `refine` times between the samples next to the
    current extreme (`points` each time). evaluate gets a batch of x and returns values in order (None or non-finite means
    unreadable), one call per batch so that rays can be traced in batches. coverage.ok is False when points were unreadable,
    refining changed the extreme by more than rel (relative), or fewer than 3 samples are valid. at_edge: the extreme lies at
    an end of the range and may be exceeded outside it."""
    if find not in ("max", "min"):
        raise SaosError(ErrorCode.BAD_ARGS, "find must be max or min")
    if isinstance(n, bool) or not isinstance(n, (int, np.integer)) or not 2 <= n <= PROFILE_MAX_N:
        raise SaosError(ErrorCode.BAD_ARGS, f"profile needs an integer n in 2–{PROFILE_MAX_N} (got {n!r})")
    if isinstance(refine, bool) or not isinstance(refine, (int, np.integer)) or not isinstance(points, (int, np.integer)) or points < 1:
        raise SaosError(ErrorCode.BAD_ARGS, f"profile needs integer refine and points >= 1 (got {refine!r}, {points!r})")
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError) as exc:
        raise SaosError(ErrorCode.BAD_ARGS, f"profile needs numeric lo and hi (got {lo!r}, {hi!r})") from exc
    if not (math.isfinite(lo) and math.isfinite(hi)) or hi <= lo:
        raise SaosError(ErrorCode.BAD_ARGS, f"profile needs finite lo < hi (got {lo}, {hi})")
    sign = 1.0 if find == "max" else -1.0
    samples: dict[float, float | None] = {}

    def run(xs: list[float]) -> None:
        xs = [x for x in xs if x not in samples]
        if not xs:
            return
        values = list(evaluate(xs))
        if len(values) != len(xs):
            raise SaosError(ErrorCode.BAD_ARGS, f"evaluate returned {len(values)} values for {len(xs)} points")
        for x, v in zip(xs, values):
            samples[x] = finite(v)

    def best() -> tuple[float, float] | None:
        valid = [(x, v) for x, v in samples.items() if v is not None]
        return max(valid, key=lambda xv: sign * xv[1]) if valid else None

    run([lo + (hi - lo) * k / (n - 1) for k in range(n)])
    coarse = best()
    for _ in range(max(0, refine)):
        current = best()
        if current is None:
            break
        xs = sorted(samples)
        k = xs.index(current[0])
        a, b = xs[max(0, k - 1)], xs[min(len(xs) - 1, k + 1)]
        run([a + (b - a) * (j + 1) / (points + 1) for j in range(points)])
    final = best()
    ordered = sorted(samples.items())
    valid = [(x, v) for x, v in ordered if v is not None]
    local = [[x, v] for k, (x, v) in enumerate(valid)
             if all(sign * v >= sign * w for _, w in valid[max(0, k - 1):k + 2])]
    local.sort(key=lambda xv: -sign * xv[1])
    missing = [x for x, v in ordered if v is None]
    notes = []
    if missing:
        notes.append(f"{len(missing)} of {len(ordered)} points gave no value")
    if len(valid) < 3:
        notes.append("fewer than 3 valid samples")
    shift = None
    if coarse is not None and final is not None:
        shift = final[1] - coarse[1]
        if abs(shift) > rel * max(1e-12, abs(final[1])):
            notes.append(f"refining moved the {find} by {shift:.4g}: the coarse step {(hi - lo) / (n - 1):.4g} is too wide")
    return {"find": find, "value": final[1] if final else None, "at": final[0] if final else None, "range": [lo, hi],
            "step": (hi - lo) / (n - 1), "evaluations": len(ordered), "refine_shift": shift,
            "at_edge": bool(final and final[0] in (lo, hi)), "local": local[:10], "missing": missing,
            "samples": [[x, v] for x, v in ordered], "coverage": {"ok": not notes, "notes": notes}}


def cra(ctx, direction: Iterable[float] = (0.0, 1.0), wave: int | Iterable[int] | None = None, n: int = 33, refine: int = 2,
        to: int | str | None = None) -> dict[str, Any]:
    """主光线角沿视场方向的分布和最大值：实际主光线（px = py = 0）与目标面（默认像面）局部 z 轴的夹角，取所测各波长中的最大，
    单位度。direction 是归一化视场坐标里的终点 (hx, hy)：径向归一化时 (0, 1) 是 +y 全视场；矩形归一化时对角是 (1, 1)。
    wave 默认全部波长。某个视场点有任一波长的主光线到不了，这一点记为读不出（coverage 会说明）。
    The chief ray angle along a field direction and its maximum: the angle between the real chief ray (px = py = 0) and
    the local z axis of the target surface (default the image), the largest over the wavelengths measured, in degrees.
    direction is the end point (hx, hy) in normalised field coordinates: (0, 1) is the +y full field with radial
    normalisation; the diagonal is (1, 1) with rectangular normalisation. wave defaults to every wavelength. A field point
    where any wavelength's chief ray fails counts as unreadable (coverage says so)."""
    dx, dy = (float(v) for v in direction)
    waves = (list(range(1, ctx.backend.wavelength_count() + 1)) if wave is None
             else [int(wave)] if isinstance(wave, (int, np.integer)) else [int(w) for w in wave])
    per_wave: dict[float, list[float | None]] = {}

    def evaluate(qs: list[float]) -> list[float | None]:
        result = trace(ctx, [(w, q * dx, q * dy, 0.0, 0.0) for q in qs for w in waves], to)
        angles = np.degrees(np.arccos(np.clip(np.abs(result.n), 0.0, 1.0)))
        out = []
        for k, q in enumerate(qs):
            row = [float(angles[k * len(waves) + j]) if result.ok[k * len(waves) + j] else None for j in range(len(waves))]
            per_wave[q] = row
            out.append(None if None in row else max(row))
        return out

    found = profile(evaluate, 0.0, 1.0, n, find="max", refine=refine)
    return {"quantity": "chief ray angle", "unit": "deg",
            "definition": "angle between the real chief ray (px=py=0) and the target surface's local z axis, largest over the wavelengths",
            "direction": [dx, dy], "waves": waves, "surface": surface(ctx, to) if to is not None else _count(ctx) - 1,
            "per_wave_at_extreme": dict(zip(waves, per_wave.get(found["at"], []))) if found["at"] is not None else None, **found}


def field_profile(ctx, fn: Callable[..., float | None], direction: Iterable[float] = (0.0, 1.0), n: int = 17, *, find: str = "max",
                  refine: int = 1, unit: str = "", quantity: str = "") -> dict[str, Any]:
    """任意量沿视场方向的分布和极值：对 q ∈ [0, 1] 调 fn(ctx, hx, hy)（hx, hy = q·direction，归一化视场坐标），格式同 profile。
    fn 不能改动镜头。用于相对照度、畸变、像质等逐视场量；主光线角用 cra（一次批量追迹，快得多）。
    Any quantity along a field direction and its extreme: fn(ctx, hx, hy) at q in [0, 1] (hx, hy = q·direction in
    normalised field coordinates), returned like profile. fn must leave the lens unchanged. For relative illumination,
    distortion, image quality per field; for the chief ray angle use cra (one batch trace, much faster)."""
    dx, dy = (float(v) for v in direction)
    found = profile(lambda qs: [fn(ctx, q * dx, q * dy) for q in qs], 0.0, 1.0, n, find=find, refine=refine)
    return {"quantity": quantity, "unit": unit, "axis": "normalised field q", "direction": [dx, dy], **found}


def wave_profile(ctx, fn: Callable[..., float | None], lo: float, hi: float, n: int = 33, *, find: str = "min", refine: int = 2,
                 wave: int | None = None, unit: str = "", quantity: str = "") -> dict[str, Any]:
    """任意量沿波长的分布和极值（透过率的低谷、色差的峰值）：把第 wave 个波长（默认主波长）依次设为 [lo, hi] µm 内的各点，
    调 fn(ctx, wave) 取值，结束后恢复原波长；格式同 profile。几个离散波长之间的窄低谷只有这样加密才看得见。
    Any quantity across wavelength and its extreme (a transmission dip, a colour peak): set wavelength number `wave`
    (default the primary) to points in [lo, hi] µm in turn, read fn(ctx, wave), restore the wavelength afterwards; returned
    like profile. A narrow dip between a few discrete wavelengths only shows up this way."""
    slot = primary_wave(ctx) if wave is None else int(wave)
    key = f"wave.{slot}"

    def evaluate(xs: list[float]) -> list[float | None]:
        out = []
        for x in xs:
            set(ctx, {key: x})
            out.append(fn(ctx, slot))
        return out

    with kept(ctx, [key]):
        found = profile(evaluate, float(lo), float(hi), n, find=find, refine=refine)
    return {"quantity": quantity, "unit": unit, "axis": "wavelength (µm)", "wave": slot, **found}


# ---------- 缓存与任务内模块 / cache and job modules ----------

def memo(ctx, key: Any, compute: Callable[[], Any]) -> Any:
    """在 ctx.cache 里按 key 记住 compute() 的结果。一次判定（check、round、measure）内所有判定脚本共享同一个 cache，
    镜头在判定期间不变；exec 脚本自己改了镜头之后要 ctx.cache.clear()。
    Remember compute() under key in ctx.cache. All check scripts of one evaluation (check, round, measure) share one
    cache and the lens does not change during it; an exec script that changes the lens must call ctx.cache.clear()."""
    cache = ctx.cache
    if key not in cache:
        cache[key] = compute()
    return cache[key]


def lib(ctx, name: str):
    """加载任务里的辅助模块（"fiberlib" 即 checks/fiberlib.py，也可以写任务内相对路径），同一个 cache 内只加载一次。
    Load a helper module of the job ("fiberlib" means checks/fiberlib.py; a job-relative path also works), once per cache."""
    import importlib.util

    rel = name if name.endswith(".py") else f"checks/{name}.py"
    path = ctx.path(rel)
    if not path.is_file():
        raise SaosError(ErrorCode.FILE_NOT_FOUND, f"cannot find {rel} in the job")
    key = ("lib", str(path), path.stat().st_mtime_ns)

    def load():
        spec = importlib.util.spec_from_file_location(f"saos_lib_{path.stem}", str(path))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # type: ignore[union-attr]
        return module

    return memo(ctx, key, load)


class KitMethods:
    """把本模块的函数挂到上下文对象上（ctx.set(...)、wctx.spot(...)）。/ Expose this module's functions as context methods."""

    cache: dict[Any, Any]

    def AsphereExpert(self, surfaces, **options):
        """当前上下文的非球面排名与 placement；接口见 saos.ctx.experts。"""
        from saos.ctx.experts import AsphereExpert

        return AsphereExpert(self, surfaces, **options)

    def GlassExpert(self, surfaces, **options):
        """当前上下文的工作波段材料搜索，复用当前计算后端。"""
        from saos.ctx.experts import GlassExpert

        return GlassExpert(self, surfaces, **options)

    def surface(self, ref):
        return surface(self, ref)

    def info(self):
        return info(self)

    def surface_params(self, ref):
        return surface_params(self, ref)

    def get(self, key):
        return get(self, key)

    def values(self, keys):
        return values(self, keys)

    def set(self, changes=None, **kw):  # noqa: A003
        return set(self, changes, **kw)

    def kept(self, keys):
        return kept(self, keys)

    def restored(self):
        return restored(self)

    def configurations(self):
        return configurations(self)

    def configuration(self, n):
        return configuration(self, n)

    def op(self, name, int1=0, int2=0, *data):
        return op(self, name, int1, int2, *data)

    def analysis(self, name, settings=None):
        return analysis(self, name, settings)

    def quick_focus(self):
        return quick_focus(self)

    def trace(self, rays, to=None):
        return trace(self, rays, to)

    def spot(self, fields=((0.0, 0.0),), wave=None, grid=21, to=None, refocus=False, min_rays=3):
        return spot(self, fields, wave, grid, to, refocus, min_rays)

    def cra(self, direction=(0.0, 1.0), wave=None, n=33, refine=2, to=None):
        return cra(self, direction, wave, n, refine, to)

    def huygens(self, wave=None, pupil=64, image=128):
        return huygens(self, wave, pupil, image)

    def wavefront_rms(self, field, wave=None):
        return wavefront_rms(self, field, wave)

    def aperture(self, ref):
        return aperture(self, ref)

    def field_profile(self, fn, direction=(0.0, 1.0), n=17, **kw):
        return field_profile(self, fn, direction, n, **kw)

    def wave_profile(self, fn, lo, hi, n=33, **kw):
        return wave_profile(self, fn, lo, hi, n, **kw)

    def objects(self):
        return objects(self)

    def object_index(self, ref):
        return object_index(self, ref)

    def nsc_trace(self, **kw):
        return nsc_trace(self, **kw)

    def detector(self, ref, grid=False):
        return detector(self, ref, grid)

    def ray_hits(self, zrd, objects, ray_filter="", max_rays=None):
        return ray_hits(self, zrd, objects, ray_filter, max_rays)

    def memo(self, key, compute):
        return memo(self, key, compute)

    def lib(self, name):
        return lib(self, name)
