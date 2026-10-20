"""镜头处方：从零搭建一个序列系统所需的数据（lens build 的输入格式，结构探索的候选也可以写成处方）。

处方只管怎么在 OpticStudio 里搭出镜头，不管怎么评价；评价在需求单的判定和配方里。
saos 自己校验骨架（系统设置、每个面的通用项、多重结构），并拒绝不成立的组合（如物在无穷远时用物方 NA）。
面型和面型参数不逐个写代码：面型写 OpticStudio 的面型名（ZOS-API 的 SurfaceType，如 EvenAspheric、
CoordinateBreak、Toroidal、XYPolynomial、Binary2），参数按 Par 列号（par4）或列标题（"4th Order Term"）填写，
名字或参数写错时由 OpticStudio 在搭建时拒绝。格式见 docs/cli.md 的「处方格式」。
"""


from __future__ import annotations

import math
import re
from typing import Any

from saos.protocol import ErrorCode, SaosError

RAY_AIMING = ("off", "paraxial", "real")
# 处方里的写法 → ZOS-API 枚举名（2024 R1 实测枚举）
UNITS = {"mm": "Millimeters", "cm": "Centimeters", "in": "Inches", "m": "Meters"}
APERTURES = {
    "object_na": "ObjectSpaceNA",
    "entrance_pupil_diameter": "EntrancePupilDiameter",
    "image_f_number": "ImageSpaceFNum",
    "working_f_number": "ParaxialWorkingFNum",
    "object_cone_angle": "ObjectConeAngle",
    "float_by_stop": "FloatByStopSize",
}
FIELDS = {
    "object_height": "ObjectHeight",
    "angle": "Angle",
    "paraxial_image_height": "ParaxialImageHeight",
    "real_image_height": "RealImageHeight",
    "theodolite_angle": "TheodoliteAngle",
}
NORMALIZATIONS = {"radial": "Radial", "rectangular": "Rectangular"}
SURFACE_APERTURES = {
    "circular": "CircularAperture",
    "circular_obscuration": "CircularObscuration",
    "rectangular": "RectangularAperture",
    "rectangular_obscuration": "RectangularObscuration",
    "elliptical": "EllipticalAperture",
    "elliptical_obscuration": "EllipticalObscuration",
    "spider": "Spider",
}
ORDERS = {"decenter_tilt": "Decenter_Tilt", "tilt_decenter": "Tilt_Decenter"}
AFOCAL_UNITS = {"microradians": "Microradians", "milliradians": "Milliradians", "radians": "Radians",
                "arcseconds": "ArcSeconds", "arcminutes": "ArcMinutes", "degrees": "Degrees"}
APODIZATIONS = {"uniform": "Uniform", "gaussian": "Gaussian", "cosine_cubed": "CosineCubed"}
POLARIZATION_METHODS = {"x_axis": "XAxisMethod", "y_axis": "YAxisMethod", "z_axis": "ZAxisMethod"}
# 光线瞄准的开关项 → ISDRayAimingData 的属性名
RAY_AIMING_FLAGS = {"robust": "UseRobustRayAiming", "enhanced": "UseEnhancedRayAiming", "cache": "UseRayAimingCache",
                    "auto_pupil_shift": "AutomaticallyCalculatePupilShiftsIsChecked",
                    "scale_pupil_shift_by_field": "ScalePupilShiftFactorsByField"}
SOLVES = ("variable", "pickup", "marginal_ray_height", "f_number")
SOLVE_COLUMNS = ("radius", "thickness", "conic", "material")
# 只按物方给定的孔径和视场：物在无穷远时不成立
OBJECT_SIDE_APERTURES = ("object_na", "object_cone_angle")
ANGLE_FIELDS = ("angle", "theodolite_angle")
# 无焦像空间的像在无穷远：像高视场和像方 F 数都没有意义
IMAGE_HEIGHT_FIELDS = ("paraxial_image_height", "real_image_height")
IMAGE_F_APERTURES = ("image_f_number", "working_f_number")

TOP_KEYS = {"title", "units", "aperture", "telecentric_object", "fields", "wavelengths", "catalogs", "ray_aiming",
            "environment", "object", "surfaces", "image", "configurations", "afocal", "apodization", "polarization",
            "coating_file", "ray_aiming_options"}
SURFACE_KEYS = {"type", "radius", "conic", "thickness", "material", "comment", "stop", "semi_diameter",
                "mech_semi_diameter", "params", "decenter_tilt", "aperture", "solves", "focal_length", "opd_mode", "coating"}
IMAGE_KEYS = SURFACE_KEYS - {"thickness", "material", "stop", "focal_length", "opd_mode"}
TYPE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
PAR_KEY_RE = re.compile(r"^par([1-9]\d{0,2})$", re.IGNORECASE)


def _bad(message: str) -> SaosError:
    return SaosError(ErrorCode.BAD_ARGS, f"prescription: {message}")


def _number(value: Any, name: str) -> float:
    if isinstance(value, str) and value.lower() in ("inf", "infinity"):
        return math.inf
    if isinstance(value, str) and value.lower() in ("-inf", "-infinity"):
        return -math.inf
    if isinstance(value, bool) or not isinstance(value, (int, float)) or math.isnan(value):
        raise _bad(f"{name} needs a number, got {value!r}")
    return float(value)


def _finite(value: Any, name: str) -> float:
    number = _number(value, name)
    if not math.isfinite(number):
        raise _bad(f"{name} must be finite, got {value!r}")
    return number


def _size(value: Any, name: str, positive: bool = False) -> float:
    number = _finite(value, name)
    if number < 0 or (positive and number == 0):
        raise _bad(f"{name} must be {'> 0' if positive else '>= 0'}, got {value!r}")
    return number


def _integer(value: Any, name: str, low: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value != int(value):
        raise _bad(f"{name} needs an integer, got {value!r}")
    if low is not None and value < low:
        raise _bad(f"{name} must be >= {low}, got {value!r}")
    return int(value)


def _name(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _bad(f"{name} needs names, got {value!r}")
    return value.strip().upper()


def _flag(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise _bad(f"{name} must be true or false, got {value!r}")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise _bad(f"{name} must be a JSON list, got {value!r}")
    return value


def _dict(value: Any, name: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise _bad(f"{name} must be a JSON object")
    return value


def _known(d: dict[str, Any], keys: set[str], name: str) -> None:
    extra = sorted(set(d) - keys)
    if extra:
        raise _bad(f"{name} has unknown keys {extra}; allowed: {sorted(keys)}")


def _choice(value: Any, choices, name: str) -> str:
    if not isinstance(value, str) or value not in choices:
        raise _bad(f"{name} must be one of {list(choices)}, got {value!r}")
    return value


def normalize_material(value: Any, name: str) -> Any:
    """材料：'' 为空气；字符串为目录牌号（或 MIRROR）；{"model": {"nd", "vd", "dpgf"}} 为模型玻璃。"""
    if value is None or value == "":
        return ""
    if isinstance(value, str):
        return value.strip().upper()
    if isinstance(value, dict) and isinstance(value.get("model"), dict):
        model = value["model"]
        return {"model": {"nd": _number(model.get("nd"), f"{name}.nd"), "vd": _number(model.get("vd"), f"{name}.vd"), "dpgf": _finite(model.get("dpgf", 0.0), f"{name}.dpgf")}}
    raise _bad(f"{name} has a malformed material: {value!r}")


def _params(value: Any, name: str) -> dict[str, Any]:
    """面型参数：键为 par<N> 或 OpticStudio 的列标题，按给出的顺序写入（有的面型先要写项数，后面的列才出现）。"""
    out: dict[str, Any] = {}
    for key, v in _dict(value, name).items():
        key = str(key).strip()
        m = PAR_KEY_RE.match(key)
        if m:
            key = f"par{int(m.group(1))}"
        elif not key:
            raise _bad(f"{name}: empty parameter name")
        if isinstance(v, str) and v.lower() not in ("inf", "infinity", "-inf", "-infinity"):
            out[key] = v
        else:
            out[key] = _number(v, f"{name}.{key}")
    return out


def _decenter_tilt(value: Any, name: str) -> dict[str, Any]:
    d = _dict(value, name)
    _known(d, {"before", "after"}, name)

    def explicit(part: Any, label: str) -> dict[str, Any]:
        p = _dict(part, label)
        _known(p, {"decenter_x", "decenter_y", "tilt_x", "tilt_y", "tilt_z", "order"}, label)
        out = {k: _finite(p.get(k, 0.0), f"{label}.{k}") for k in ("decenter_x", "decenter_y", "tilt_x", "tilt_y", "tilt_z")}
        out["order"] = _choice(p.get("order", "decenter_tilt"), ORDERS, f"{label}.order")
        return out

    out: dict[str, Any] = {"before": explicit(d["before"], f"{name}.before") if d.get("before") is not None else None}
    after = d.get("after")
    if after is None or after == "reverse":
        out["after"] = after
    elif isinstance(after, dict):
        out["after"] = explicit(after, f"{name}.after")
    else:
        raise _bad(f"{name}.after must be \"reverse\" or an object of explicit values")
    return out


def _surface_aperture(value: Any, name: str) -> dict[str, Any]:
    d = _dict(value, name)
    kind = _choice(d.get("type"), SURFACE_APERTURES, f"{name}.type")
    out: dict[str, Any] = {"type": kind}
    if kind.startswith("circular"):
        _known(d, {"type", "min", "max", "x_decenter", "y_decenter"}, name)
        out["min"] = _size(d.get("min", 0.0), f"{name}.min")
        out["max"] = _size(d.get("max"), f"{name}.max", positive=True)
        if out["min"] >= out["max"]:
            raise _bad(f"{name}: min must be below max")
    elif kind == "spider":
        _known(d, {"type", "arms", "width", "x_decenter", "y_decenter"}, name)
        out["arms"] = _integer(d.get("arms"), f"{name}.arms", low=1)
        out["width"] = _size(d.get("width"), f"{name}.width", positive=True)
    else:
        _known(d, {"type", "x_half", "y_half", "x_decenter", "y_decenter"}, name)
        out["x_half"] = _size(d.get("x_half"), f"{name}.x_half", positive=True)
        out["y_half"] = _size(d.get("y_half"), f"{name}.y_half", positive=True)
    out["x_decenter"] = _finite(d.get("x_decenter", 0.0), f"{name}.x_decenter")
    out["y_decenter"] = _finite(d.get("y_decenter", 0.0), f"{name}.y_decenter")
    return out


def _solves(value: Any, name: str) -> dict[str, dict[str, Any]]:
    """求解：{"<列>": "variable" | {"type": ...}}，列为 radius、thickness、conic、material 或 par<N>。"""
    out: dict[str, dict[str, Any]] = {}
    for column, spec in _dict(value, name).items():
        label = f"{name}.{column}"
        m = PAR_KEY_RE.match(str(column))
        column = f"par{int(m.group(1))}" if m else str(column)
        if column not in SOLVE_COLUMNS and not m:
            raise _bad(f"{label}: solves go on {list(SOLVE_COLUMNS)} or par<N>")
        spec = {"type": spec} if isinstance(spec, str) else _dict(spec, label)
        kind = _choice(spec.get("type"), SOLVES, f"{label}.type")
        if kind == "variable":
            _known(spec, {"type"}, label)
            if column == "material":
                raise _bad(f"{label}: make a material vary with saos opt vary --mode model|substitute, not in the prescription")
            item: dict[str, Any] = {"type": kind}
        elif kind == "pickup":
            _known(spec, {"type", "surface", "scale", "offset"}, label)
            item = {"type": kind, "surface": _integer(spec.get("surface"), f"{label}.surface", low=0)}
            if column == "material":
                if "scale" in spec or "offset" in spec:
                    raise _bad(f"{label}: a material pickup has no scale or offset")
            else:
                item["scale"] = _finite(spec.get("scale", 1.0), f"{label}.scale")
                item["offset"] = _finite(spec.get("offset", 0.0), f"{label}.offset")
        elif kind == "marginal_ray_height":
            _known(spec, {"type", "height", "zone"}, label)
            if column != "thickness":
                raise _bad(f"{label}: marginal_ray_height is a thickness solve")
            item = {"type": kind, "height": _finite(spec.get("height", 0.0), f"{label}.height"),
                    "zone": _finite(spec.get("zone", 0.0), f"{label}.zone")}
        else:
            _known(spec, {"type", "value"}, label)
            if column != "radius":
                raise _bad(f"{label}: f_number is a radius solve")
            item = {"type": kind, "value": _finite(spec.get("value"), f"{label}.value")}
        out[column] = item
    return out


def _surface(s: Any, name: str, image: bool = False) -> dict[str, Any]:
    if not isinstance(s, dict):
        raise _bad(f"{name} must be a JSON object")
    _known(s, IMAGE_KEYS if image else SURFACE_KEYS, name)
    kind = s.get("type", "standard")
    if not isinstance(kind, str) or not TYPE_NAME_RE.match(kind):
        raise _bad(f"{name}.type must be standard, paraxial or an OpticStudio surface type name such as EvenAspheric")
    if kind.lower() in ("standard", "paraxial"):
        kind = kind.lower()
    if image and kind == "paraxial":
        raise _bad(f"{name}: the image surface cannot be paraxial")
    item: dict[str, Any] = {"type": kind, "comment": str(s.get("comment", ""))}
    if not image:
        item["thickness"] = _finite(s.get("thickness", 0.0), f"{name}.thickness")
        item["stop"] = _flag(s.get("stop", False), f"{name}.stop")
    if kind != "paraxial" and ("focal_length" in s or "opd_mode" in s):
        raise _bad(f"{name}: focal_length and opd_mode belong to paraxial surfaces; give other types' parameters in params")
    if kind == "standard":
        item["radius"] = _number(s.get("radius", math.inf), f"{name}.radius")
        item["conic"] = _number(s.get("conic", 0.0), f"{name}.conic")
        if not image:
            item["material"] = normalize_material(s.get("material", ""), name)
    elif kind == "paraxial":
        if any(k in s for k in ("radius", "conic")):
            raise _bad(f"{name}: a paraxial surface has no radius or conic; give focal_length")
        item["focal_length"] = _number(s.get("focal_length"), f"{name}.focal_length")
        item["opd_mode"] = _integer(s.get("opd_mode", 1), f"{name}.opd_mode", low=0)
        if "material" in s:
            item["material"] = normalize_material(s["material"], name)
    else:
        for key in ("radius", "conic"):
            if key in s:
                item[key] = _number(s[key], f"{name}.{key}")
        if "material" in s:
            item["material"] = normalize_material(s["material"], name)
    if s.get("semi_diameter") is not None:
        item["semi_diameter"] = _size(s["semi_diameter"], f"{name}.semi_diameter")
    if s.get("mech_semi_diameter") is not None:
        item["mech_semi_diameter"] = _size(s["mech_semi_diameter"], f"{name}.mech_semi_diameter")
    if s.get("params"):
        item["params"] = _params(s["params"], f"{name}.params")
    if s.get("decenter_tilt"):
        item["decenter_tilt"] = _decenter_tilt(s["decenter_tilt"], f"{name}.decenter_tilt")
    if s.get("aperture"):
        item["aperture"] = _surface_aperture(s["aperture"], f"{name}.aperture")
    if s.get("solves"):
        item["solves"] = _solves(s["solves"], f"{name}.solves")
    if s.get("coating"):
        if not isinstance(s["coating"], str) or not s["coating"].strip():
            raise _bad(f"{name}.coating must be a coating name from the coating file")
        item["coating"] = s["coating"].strip()
    return item


def _fields(f: Any) -> dict[str, Any]:
    f = _dict(f, "fields")
    _known(f, {"type", "y", "x", "weights", "normalization"}, "fields")
    kind = _choice(f.get("type"), FIELDS, "fields.type")
    ys = [_finite(y, "fields.y") for y in _list(f.get("y"), "fields.y")]
    if not ys:
        raise _bad("at least one field is required")
    xs = [_finite(x, "fields.x") for x in _list(f.get("x"), "fields.x") or [0.0] * len(ys)]
    weights = [_size(w, "fields.weights") for w in _list(f.get("weights"), "fields.weights") or [1.0] * len(ys)]
    if not len(xs) == len(weights) == len(ys):
        raise _bad("fields: x, y and weights need the same length")
    if kind == "angle" and any(abs(v) >= 90 for v in xs + ys):
        raise _bad("fields: angle fields must stay below 90 degrees; use theodolite_angle beyond")
    return {"type": kind, "y": ys, "x": xs, "weights": weights,
            "normalization": _choice(f.get("normalization", "radial"), NORMALIZATIONS, "fields.normalization")}


def _afocal(value: Any) -> dict[str, Any] | None:
    """无焦像空间：true 或 {"units": ...}；不写为关。/ Afocal image space: true or {"units": ...}; off when absent."""
    if value is None or value is False:
        return None
    d = {} if value is True else _dict(value, "afocal")
    _known(d, {"units"}, "afocal")
    return {"units": _choice(d["units"], AFOCAL_UNITS, "afocal.units")} if "units" in d else {}


def _apodization(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    d = _dict(value, "apodization")
    _known(d, {"type", "factor"}, "apodization")
    kind = _choice(d.get("type"), APODIZATIONS, "apodization.type")
    if kind == "gaussian":
        return {"type": kind, "factor": _size(d.get("factor"), "apodization.factor", positive=True)}
    if "factor" in d:
        raise _bad(f"apodization.factor belongs to gaussian apodization, not {kind}")
    return {"type": kind}


def _polarization(value: Any) -> dict[str, Any] | None:
    """偏振：只写给出的项，其余保持 OpticStudio 的默认。/ Only the given items are written."""
    if value is None:
        return None
    d = _dict(value, "polarization")
    _known(d, {"unpolarized", "jx", "jy", "x_phase", "y_phase", "method", "convert_thin_film_phase"}, "polarization")
    out: dict[str, Any] = {k: _flag(d[k], f"polarization.{k}") for k in ("unpolarized", "convert_thin_film_phase") if k in d}
    out.update({k: _finite(d[k], f"polarization.{k}") for k in ("jx", "jy", "x_phase", "y_phase") if k in d})
    if "method" in d:
        out["method"] = _choice(d["method"], POLARIZATION_METHODS, "polarization.method")
    jones = [k for k in ("jx", "jy", "x_phase", "y_phase") if k in out]
    if out.get("unpolarized") and jones:
        raise _bad(f"polarization: {jones} have no effect when unpolarized is true")
    if out.get("jx") == 0 and out.get("jy") == 0:
        raise _bad("polarization: jx and jy cannot both be 0")
    if jones and "unpolarized" not in out:
        # 给了琼斯矢量就是偏振光；OpticStudio 新系统默认非偏振，不关掉时这些值不生效（2026-10-19 验证实测）
        out["unpolarized"] = False
    return out


def _ray_aiming_options(value: Any, aiming: str) -> dict[str, Any] | None:
    if value is None:
        return None
    d = _dict(value, "ray_aiming_options")
    _known(d, {*RAY_AIMING_FLAGS, "pupil_shift", "pupil_compress"}, "ray_aiming_options")
    if aiming == "off":
        raise _bad("ray_aiming_options need ray_aiming paraxial or real")
    out: dict[str, Any] = {k: _flag(d[k], f"ray_aiming_options.{k}") for k in RAY_AIMING_FLAGS if k in d}
    for key, n in (("pupil_shift", 3), ("pupil_compress", 2)):
        if key in d:
            v = d[key]
            if not isinstance(v, list) or len(v) != n:
                raise _bad(f"ray_aiming_options.{key} needs {n} numbers")
            out[key] = [_finite(x, f"ray_aiming_options.{key}") for x in v]
    if out.get("auto_pupil_shift") and "pupil_shift" in out:
        raise _bad("ray_aiming_options: give pupil_shift or auto_pupil_shift, not both")
    if "pupil_shift" in out and "auto_pupil_shift" not in out:
        # OpticStudio 默认自动算光瞳偏移，会盖掉手动值（2026-10-19 验证实测）
        out["auto_pupil_shift"] = False
    return out


def _configurations(value: Any, adjust_index: bool) -> list[dict[str, Any]]:
    """多重结构：每行一个操作数，values 给每个结构的值；结构数是 values 的长度，各行相同且至少 2。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise _bad("configurations must be a list of {operand, params, values, vary}")
    if not value:
        return []
    rows = []
    for k, op in enumerate(value, start=1):
        name = f"configurations[{k}]"
        op = _dict(op, name)
        _known(op, {"operand", "params", "values", "vary"}, name)
        operand = op.get("operand")
        if not isinstance(operand, str) or not TYPE_NAME_RE.match(operand):
            raise _bad(f"{name}.operand must be a multi-configuration operand name such as THIC, GLSS, TEMP")
        params = [_integer(p, f"{name}.params", low=0) for p in _list(op.get("params"), f"{name}.params")]
        if len(params) > 3:
            raise _bad(f"{name}.params: at most 3 integers")
        values = []
        for v in _list(op.get("values"), f"{name}.values"):
            if isinstance(v, str) and v.lower() not in ("inf", "infinity", "-inf", "-infinity"):
                values.append(v)
            else:
                values.append(_number(v, f"{name}.values"))
        rows.append({"operand": operand.upper(), "params": params, "values": values, "vary": _flag(op.get("vary", False), f"{name}.vary")})
    counts = {len(r["values"]) for r in rows}
    if len(counts) != 1 or min(counts) < 2:
        raise _bad("configurations: every operand needs one value per configuration, at least 2, the same count in each row")
    if not adjust_index and any(r["operand"] in ("TEMP", "PRES") for r in rows):
        raise _bad("configurations: TEMP and PRES change nothing unless environment.adjust_index is true")
    return rows


def _conditions(rows: list[dict[str, Any]], environment: dict[str, Any], given: dict[str, Any]) -> list[dict[str, Any]]:
    """TEMP、PRES 行在结构 1 的值会覆盖系统的温度、气压（2026-10-20 验证实测），所以两者必须一致：environment 没写时取该行
    结构 1 的值，写了而不同就拒绝；每种至多一行。
    A TEMP or PRES row's configuration-1 value overrides the system temperature or pressure, so the two must agree: an
    unset environment value takes the row's, a different one is refused; at most one row of each."""
    for operand, key in (("TEMP", "temperature"), ("PRES", "pressure")):
        found = [r for r in rows if r["operand"] == operand]
        if len(found) > 1:
            raise _bad(f"configurations: at most one {operand} row")
        if not found:
            continue
        first = found[0]["values"][0]
        if not isinstance(first, float) or not math.isfinite(first):
            raise _bad(f"configurations: {operand} values must be finite numbers")
        if key in given and environment[key] != first:
            raise _bad(f"environment.{key} is {environment[key]} but the {operand} row sets {first} in configuration 1, "
                       f"which OpticStudio applies; make them equal or leave environment.{key} out")
        environment[key] = first
    return rows


def normalize(p: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(p, dict):
        raise _bad("the prescription must be a JSON object")
    _known(p, TOP_KEYS, "the prescription")
    units = _choice(p.get("units", "mm"), UNITS, "units")
    title = p.get("title", "")
    if not isinstance(title, str):
        raise _bad(f"title must be a string, got {title!r}")
    aperture = _dict(p.get("aperture"), "aperture")
    _known(aperture, {"type", "value"}, "aperture")
    a_kind = _choice(aperture.get("type"), APERTURES, "aperture.type")
    # 按光阑尺寸浮动时孔径由光阑面的半口径决定，没有数值
    if a_kind == "float_by_stop" and aperture.get("value") is not None:
        raise _bad("aperture.type float_by_stop takes no value: the stop surface's semi_diameter sets the aperture")
    a_value = None if a_kind == "float_by_stop" else _size(aperture.get("value"), "aperture.value", positive=True)
    if a_kind == "object_cone_angle" and a_value is not None and a_value >= 90:
        raise _bad("aperture.value: an object cone angle is a half angle below 90 degrees")
    fields = _fields(p.get("fields"))
    waves = _dict(p.get("wavelengths"), "wavelengths")
    _known(waves, {"um", "weights", "primary"}, "wavelengths")
    um = [_size(w, "wavelengths.um", positive=True) for w in _list(waves.get("um"), "wavelengths.um")]
    if not um:
        raise _bad("at least one wavelength is required")
    weights = [_size(w, "wavelengths.weights") for w in _list(waves.get("weights"), "wavelengths.weights") or [1.0] * len(um)]
    primary = waves.get("primary", 1)
    if len(weights) != len(um) or not isinstance(primary, int) or isinstance(primary, bool) or not 1 <= primary <= len(um):
        raise _bad("wavelengths: wrong number of weights or primary index")
    aiming = _choice(p.get("ray_aiming", "real"), RAY_AIMING, "ray_aiming")
    afocal = _afocal(p.get("afocal"))
    if afocal is not None and fields["type"] in IMAGE_HEIGHT_FIELDS:
        raise _bad("afocal: the image is at infinity, so fields cannot be image heights; use angle or object_height")
    if afocal is not None and a_kind in IMAGE_F_APERTURES:
        raise _bad("afocal: an afocal system has no image-space F-number; give the aperture as entrance_pupil_diameter, object_na, object_cone_angle or float_by_stop")
    coating_file = p.get("coating_file")
    if coating_file is not None and (not isinstance(coating_file, str) or not coating_file.strip()):
        raise _bad("coating_file must be a coating file name such as COATING.DAT")
    env = _dict(p.get("environment"), "environment")
    _known(env, {"temperature", "pressure", "adjust_index"}, "environment")
    # 默认值与 OpticStudio 新系统相同：20 °C、1 atm、不按环境修正折射率
    environment = {"temperature": _finite(env.get("temperature", 20.0), "environment.temperature"),
                   "pressure": _size(env.get("pressure", 1.0), "environment.pressure"),
                   "adjust_index": _flag(env.get("adjust_index", False), "environment.adjust_index")}

    obj = _dict(p.get("object"), "object")
    _known(obj, {"thickness", "material", "comment"}, "object")
    out_obj = {
        "thickness": _number(obj.get("thickness"), "object.thickness"),
        "material": normalize_material(obj.get("material", ""), "object"),
        "comment": str(obj.get("comment", "")),
    }
    if out_obj["thickness"] == -math.inf:
        raise _bad("object.thickness: finite or \"inf\" (object at infinity), not -inf")
    telecentric = _flag(p.get("telecentric_object", False), "telecentric_object")
    at_infinity = math.isinf(out_obj["thickness"])
    if at_infinity and a_kind in OBJECT_SIDE_APERTURES:
        raise _bad(f"aperture.type {a_kind} needs an object at a finite distance; with the object at infinity use entrance_pupil_diameter, image_f_number or float_by_stop")
    if at_infinity and fields["type"] == "object_height":
        raise _bad("fields.type object_height needs an object at a finite distance; with the object at infinity use angle or an image height")
    if telecentric:
        if at_infinity:
            raise _bad("telecentric_object needs an object at a finite distance")
        if fields["type"] in ANGLE_FIELDS:
            raise _bad("telecentric_object: every chief ray is parallel to the axis in object space, so field angles cannot be used")
        if a_kind == "entrance_pupil_diameter":
            raise _bad("telecentric_object puts the entrance pupil at infinity; give the aperture as object_na, object_cone_angle or a working F-number")

    surfaces_in = _list(p.get("surfaces"), "surfaces")
    surfaces = [_surface(s, f"surfaces[{k}]") for k, s in enumerate(surfaces_in, start=1)]
    if not surfaces:
        raise _bad("at least one surface is required")
    stops = [k for k, s in enumerate(surfaces, start=1) if s["stop"]]
    if len(stops) > 1:
        raise _bad("only one stop surface is allowed")
    if a_kind == "float_by_stop" and not (stops and "semi_diameter" in surfaces[stops[0] - 1]):
        raise _bad("aperture.type float_by_stop: mark the stop surface and give it a semi_diameter, which sets the aperture")
    image = _surface(p["image"], "image", image=True) if p.get("image") is not None else None
    last = len(surfaces) + 1
    for k, s in [*enumerate(surfaces, start=1), (last, image or {})]:
        for column, solve in (s.get("solves") or {}).items():
            if solve["type"] == "pickup" and not (0 <= solve["surface"] <= last and solve["surface"] != k):
                raise _bad(f"surface {k} {column}: a pickup reads another surface 0–{last}, got {solve['surface']}")

    out = {
        "title": title,
        "units": units,
        "aperture": {"type": a_kind, "value": a_value},
        "fields": fields,
        "wavelengths": {"um": um, "weights": weights, "primary": primary},
        "catalogs": [_name(c, "catalogs") for c in _list(p.get("catalogs"), "catalogs") or ["SCHOTT"]],
        "ray_aiming": aiming,
        "telecentric_object": telecentric,
        "environment": environment,
        "object": out_obj,
        "surfaces": surfaces,
        "configurations": _conditions(_configurations(p.get("configurations"), environment["adjust_index"]), environment, env),
        "afocal": afocal,
        "apodization": _apodization(p.get("apodization")),
        "polarization": _polarization(p.get("polarization")),
        "coating_file": coating_file.strip() if coating_file else None,
        "ray_aiming_options": _ray_aiming_options(p.get("ray_aiming_options"), aiming),
    }
    if image is not None:
        out["image"] = image
    return out
