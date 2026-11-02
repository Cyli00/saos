"""OpticStudio 上按参数键读写镜头（ZosBackend.get_param / set_param）与非序列物体的定位。原先在 saos.kit 里，
测量库因此直接摸 ZOS-API 单元格、后端又反过来导入 kit；现在归后端所有，kit 只经 Backend 协议调用。
Reading and writing the lens by parameter key on OpticStudio (ZosBackend.get_param / set_param) and locating
non-sequential objects. This used to live in saos.kit, which made the measurement library touch ZOS-API cells and the
backend import kit back; it now belongs to the backend, and kit goes through the Backend protocol only.

b 是 ZosBackend：b.system 为系统对象，b.root 为 OpticStudio 安装目录。/ b is the ZosBackend.
"""

from __future__ import annotations

import math
import re
from typing import Any

from saos import lensdata
from saos.backend import nsc
from saos.backend.zos import _load_zosapi, catalog_constants, cell_value, par_cell, set_cell
from saos.lensdata import INT_RE, PAR_RE
from saos.protocol import ErrorCode, SaosError

_ROW_ATTRS = {"radius": "Radius", "thickness": "Thickness", "conic": "Conic", "semi_diameter": "SemiDiameter",
              "mech_semi_diameter": "MechanicalSemiDiameter"}
_OBJECT_ATTRS = {"x": "XPosition", "y": "YPosition", "z": "ZPosition", "tilt_x": "TiltAboutX", "tilt_y": "TiltAboutY", "tilt_z": "TiltAboutZ"}
_FACE_KEY_RE = re.compile(r"^face(\d+)\.(is|coating|fraction|sigma|rays)$", re.IGNORECASE)


def _config_cell(sysm, param: str):
    """多重结构第 r 行在结构 c 的单元格。/ The cell of row r in configuration c."""
    c, r = (int(x) for x in param.split("."))
    mce = sysm.MCE
    count, rows = int(mce.NumberOfConfigurations), int(mce.NumberOfOperands)
    if count < 2 or not (1 <= c <= count and 1 <= r <= rows):
        raise SaosError(ErrorCode.BAD_ARGS, f"config.{c}.{r}: the lens has {count} configuration(s) and {rows} multi-configuration row(s)")
    return mce, c, mce.GetOperandAt(r).GetOperandCell(c)


def object_index(b, ref: int | str) -> int:
    """非序列物体的序号：整数原样，字符串按注释查找（重名时取第一个）。混合模式里写 <物体>@<NonSequential 面号> 选组，
    只有一个组时可以省略；查找前把该组设为当前组（NCE.SetActiveSurface）。
    An object number, or the first object with this comment. In mixed mode <object>@<NonSequential surface> picks the
    group (optional when there is only one); the group is made current before the lookup."""
    sysm = b.system
    group = None
    if isinstance(ref, str) and "@" in ref:
        ref, _, g = ref.rpartition("@")
        if not INT_RE.match(g.strip()):
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
    if isinstance(ref, str) and INT_RE.match(ref.strip()):
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


def _set_glass(zosapi, sysm, cell, material: str, param: str, value: Any, label: str) -> None:
    """改 nd、vd 或 dpgf：把材料换成模型玻璃（其余两项取当前值；目录玻璃的 ΔPgF 记 0），用于折射率、阿贝数公差。
    Set nd, vd or dpgf by turning the material into a model glass (the other two keep their current values; a catalog
    glass's ΔPgF counts as 0): for index and Abbe tolerances."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}.{param} needs a number, got {value!r}")
    now = _glass(sysm, cell, material, label)
    now[param] = float(value)
    set_model_material(zosapi, cell, now, label)


def set_model_material(zosapi, cell, value: dict, label: str) -> None:
    """把材料单元格设为模型玻璃求解（nd、vd，可选 dpgf）并回读核对。/ Make a material cell a model-glass solve and read it back."""
    if not {"nd", "vd"} <= value.keys() or value.keys() - {"nd", "vd", "dpgf"}:
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}: model material needs nd/vd and optional dpgf")
    values = {"nd": value["nd"], "vd": value["vd"], "dpgf": value.get("dpgf", 0.0)}
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values.values()) or values["nd"] <= 0:
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}: model material needs finite numbers and nd > 0")
    solve = cell.CreateSolveType(zosapi.Editors.SolveType.MaterialModel)
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
    data = nsc.read_face(_face(o, number, n))
    if what == "is":
        return data["is"]
    if what == "coating":
        return data.get("coating", "")
    if what not in data["scatter"]:
        raise SaosError(ErrorCode.BAD_ARGS, f"object {number} face {n}: the {data['scatter']['model']} scatter model has no {what}")
    return data["scatter"][what]


def _set_face(zosapi, o, number: int, n: int, what: str, value: Any) -> None:
    """改一个面的一项：is、coating 直接写；fraction、sigma、rays 在当前散射模型上改这一项，其余字段照旧。"""
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
    nsc.set_face(zosapi, face, spec, label)


def _get_object(b, ref: Any, param: str) -> Any:
    sysm, zosapi = b.system, _load_zosapi(b.root)
    number = object_index(b, ref)
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
    return cell_value(nsc.param_cell(zosapi, o, f"object {number}", param))


def _set_object(b, ref: Any, param: str, value: Any) -> None:
    sysm, zosapi = b.system, _load_zosapi(b.root)
    number = object_index(b, ref)
    o = sysm.NCE.GetObjectAt(number)
    m = _FACE_KEY_RE.match(param)
    if m:
        _set_face(zosapi, o, number, int(m.group(1)), m.group(2).lower(), value)
        return
    if param == "comment":
        if nsc.type_files(o):
            raise SaosError(ErrorCode.BAD_ARGS, f"object {number} ({o.TypeName}) keeps its file name in the comment column; writing it would change the file")
        o.Comment = "" if value is None else str(value)
        return
    if param == "material":
        if isinstance(value, dict):
            set_model_material(zosapi, o.MaterialCell, value, f"object {number}")
            return
        name = str(value).strip().upper()
        o.Material = name if name in ("", "ABSORB") else b._check_material(name)
        return
    if param in ("nd", "vd", "dpgf"):
        _set_glass(zosapi, sysm, o.MaterialCell, str(o.Material), param, value, f"object {number}")
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
    set_cell(nsc.param_cell(zosapi, o, f"object {number}", param), value, f"object {number} {param}")


def _names(b, kind: str, ref: Any) -> dict[str, int] | None:
    # 按注释找面时先读一次注释表；序号不用读 / read the comments once for a surface named by comment, never for a number
    return b.surface_comments() if kind == "surface" and not str(ref).lstrip("-").isdigit() else {}


def get(b, key: str) -> Any:
    kind, ref, param = lensdata.parse_key(key)
    sysm = b.system
    if kind == "object":
        return _get_object(b, ref, param)
    if kind == "system":
        sd = sysm.SystemData
        if ref == "config":
            return cell_value(_config_cell(sysm, param)[2])
        if ref == "aperture":
            return float(sd.Aperture.ApertureValue)
        if ref == "wave":
            return float(sd.Wavelengths.GetWavelength(int(param)).Wavelength)
        field = sd.Fields.GetField(int(param))
        return float(field.X if ref == "field_x" else field.Y)
    index = lensdata.surface(b, ref, _names(b, kind, ref))
    row = sysm.LDE.GetSurfaceAt(index)
    if param == "curvature":
        r = float(row.Radius)
        return 0.0 if r == 0 or math.isinf(r) else 1.0 / r
    if PAR_RE.match(param):
        return float(par_cell(_load_zosapi(b.root), row, index, param).DoubleValue)
    if param == "material":
        return _material_value(row.MaterialCell, row.Material)
    if param == "comment":
        return str(row.Comment)
    if param in ("nd", "vd", "dpgf"):
        return _glass(sysm, row.MaterialCell, str(row.Material), f"surface {index}")[param]
    return float(getattr(row, _ROW_ATTRS[param]))


def put(b, key: str, value: Any) -> None:
    kind, ref, param = lensdata.parse_key(key)
    sysm = b.system
    if kind == "object":
        _set_object(b, ref, param, value)
        return
    if kind == "system":
        sd = sysm.SystemData
        if ref == "config":
            mce, c, cell = _config_cell(sysm, param)
            if isinstance(value, str) and mce.GetOperandAt(int(param.split(".")[1])).TypeName == "GLSS":
                value = b._check_material(value)
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
    index = lensdata.surface(b, ref, _names(b, kind, ref))
    row = sysm.LDE.GetSurfaceAt(index)
    if param == "material":
        if isinstance(value, dict):
            set_model_material(_load_zosapi(b.root), row.MaterialCell, value, f"surface {index}")
            return
        # 材料走后端的目录校验 / materials go through the backend's catalog check
        b.set_surface(index, "material", value)
        return
    if param == "comment":
        row.Comment = "" if value is None else str(value)
        return
    if param in ("nd", "vd", "dpgf"):
        _set_glass(_load_zosapi(b.root), sysm, row.MaterialCell, str(row.Material), param, value, f"surface {index}")
        return
    target = float(value)
    if param == "curvature":
        target, param = (math.inf if target == 0 else 1.0 / target), "radius"
    if PAR_RE.match(param):
        cell = par_cell(_load_zosapi(b.root), row, index, param)
        cell.DoubleValue = target
        actual = float(cell.DoubleValue)
    else:
        attr = _ROW_ATTRS[param]
        setattr(row, attr, target)
        actual = float(getattr(row, attr))
    if not (actual == target or abs(actual - target) <= 1e-12 * max(1.0, abs(target))):
        raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"surface {index} {param} did not take {target}; a solve or pickup may override it",
                        {"requested": target, "actual": actual})
