"""镜头处方：从零搭建一个序列系统（或 "mode": "non_sequential" 的非序列系统）所需的数据（lens build 的输入格式，结构探索的候选也可以写成处方）。

处方只管怎么在 OpticStudio 里搭出镜头，不管怎么评价；评价在需求单的判定和配方里。
saos 自己校验骨架（系统设置、每个面的通用项、多重结构），并拒绝不成立的组合（如物在无穷远时用物方 NA）。
面型和面型参数不逐个写代码：面型写 OpticStudio 的面型名（ZOS-API 的 SurfaceType，如 EvenAspheric、
CoordinateBreak、Toroidal、XYPolynomial、Binary2），参数按 Par 列号（par4）或列标题（"4th Order Term"）填写，
名字或参数写错时由 OpticStudio 在搭建时拒绝。格式见 saos lens build --help。
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

TOP_KEYS = {"mode", "title", "units", "aperture", "telecentric_object", "fields", "wavelengths", "catalogs", "ray_aiming",
            "environment", "object", "surfaces", "image", "configurations", "afocal", "apodization", "polarization",
            "coating_file", "ray_aiming_options", "nsc", "source_units", "analysis_units"}
SURFACE_KEYS = {"type", "radius", "conic", "thickness", "material", "comment", "stop", "semi_diameter",
                "mech_semi_diameter", "params", "decenter_tilt", "aperture", "solves", "focal_length", "opd_mode", "coating", "objects"}
IMAGE_KEYS = SURFACE_KEYS - {"thickness", "material", "stop", "focal_length", "opd_mode", "objects"}
TYPE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
PAR_KEY_RE = re.compile(r"^par([1-9]\d{0,2})$", re.IGNORECASE)
# 非序列处方（"mode": "non_sequential"）
NSC_TOP_KEYS = {"mode", "title", "units", "wavelengths", "catalogs", "coating_file", "nsc", "objects", "source_units", "analysis_units"}
OBJECT_KEYS = {"type", "comment", "ref", "inside_of", "position", "tilt", "material", "params", "volume_physics",
               "file", "file2", "faces", "scatter_to", "source", "type_options", "diffraction", "index_model"}
VOLUME_MODELS = ("none", "angle", "dll", "photoluminescence")
# 光致发光模型的字段：(类型, 可选项)；file 类按 OpticStudio 列出的文件名选 / photoluminescence fields: (kind, choices)
PL_SPECTRA = ("absorption", "emission", "excitation", "quantum_yield")
PL_FIELDS = {"efficiency": ("quantum_yield", "excitation"), "pl_density": "number", "extinction_coefficient": "number",
             "extinction_wavelength": "number", "threshold": "number", "basic_algorithm": "flag", "consider_mie": "flag",
             "particle_index": "number", "particle_density": "number", "particle_radius": "number", "use_mie_density": "flag",
             "mie_mean_path": "number", "pl_mean_path": "number"}
# 光源页 / the Sources tab
SOURCE_KEYS = {"polarization", "initial_phase", "coherence_length", "reverse_rays", "pre_propagation", "sampling", "bulk_scatter", "array", "color"}
SAMPLING = ("random", "sobol")
BULK_SCATTER = ("many", "once", "never")
ARRAY_TYPES = ("none", "rectangular", "circular", "hexapolar", "hexagonal")
ARRAY_FIELDS = {"number_x": "int", "number_y": "int", "spacing_x": "number", "spacing_y": "number", "number": "int", "radius": "number",
                "rings": "int", "spacing": "number"}
COLOR_MODES = ("system_wavelengths", "uniform", "blackbody", "color_temperature", "d65", "spectrum_file", "cie1931_xyz", "cie1931_xy",
               "cie1931_rgb", "cie1976")
COLOR_FIELDS = {"temperature": "number", "from": "number", "to": "number", "count": "int", "file": "name", "x": "number", "y": "number",
                "z": "number", "cx": "number", "cy": "number", "r": "number", "g": "number", "b": "number", "u": "number", "v": "number"}
# Type 页 / the Type tab
TYPE_OPTIONS = {"rays_ignore": ("never", "always", "on_launch"), "consider": "objects", "ignore": "objects", "is_detector": "flag",
                "pixel_interpolation": "flag", "global_xyz_rotation": "flag", "consider_ignore_when_splitting": "flag"}
DIFFRACTION_SPLIT = ("none", "table", "dll")
INDEX_TYPES = ("isotropic", "birefringent", "grin")
SOURCE_UNITS = ("watts", "lumens", "joules")
ANALYSIS_UNITS = ("W/mm2", "W/cm2", "W/in2", "W/m2", "W/ft2")
FACE_KEYS = {"is", "coating", "profile", "scatter"}
FACE_IS = ("default", "reflective", "absorbing")
# 各面散射模型收的字段（2024 R1 的 IOSS_* 接口实测）；rays 是该面的散射光线数，各模型都收
# the fields each face scatter model takes (the IOSS_* interfaces in 2024 R1); rays is the face's scatter ray count
SCATTER_FIELDS = {"none": {}, "lambertian": {"fraction": "number"}, "gaussian": {"fraction": "number", "sigma": "number"},
                  "abg": {"reflect": "name", "transmit": "name"}, "abg_file": {"reflect": "name", "transmit": "name"},
                  "bsdf": {"reflect": "name", "transmit": "name"}, "user": {"dll": "name", "fraction": "number", "file": "name", "params": "params"}}
SCATTER_REQUIRED = {"lambertian": ("fraction",), "gaussian": ("fraction", "sigma"), "user": ("dll",)}
IMPORTANCE_ROWS = 6
NSC_SETTINGS = ("max_intersections", "max_segments", "max_nested", "min_relative_intensity", "min_absolute_intensity",
                "glue_distance", "simple_ray_splitting")


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
    if "objects" in s:
        # 混合模式：NonSequential 面（非序列组）里的物体，写法同纯非序列处方，物体号在组内从 1 起
        # mixed mode: the objects of a NonSequential surface (an NSC group), written as in a pure NSC prescription, numbered within the group
        if kind.lower() != "nonsequential":
            raise _bad(f"{name}: objects belong to a NonSequential surface")
        item["objects"] = _objects(s["objects"], f"{name}.objects")
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


def _wavelengths(value: Any) -> dict[str, Any]:
    waves = _dict(value, "wavelengths")
    _known(waves, {"um", "weights", "primary"}, "wavelengths")
    um = [_size(w, "wavelengths.um", positive=True) for w in _list(waves.get("um"), "wavelengths.um")]
    if not um:
        raise _bad("at least one wavelength is required")
    weights = [_size(w, "wavelengths.weights") for w in _list(waves.get("weights"), "wavelengths.weights") or [1.0] * len(um)]
    primary = waves.get("primary", 1)
    if len(weights) != len(um) or not isinstance(primary, int) or isinstance(primary, bool) or not 1 <= primary <= len(um):
        raise _bad("wavelengths: wrong number of weights or primary index")
    return {"um": um, "weights": weights, "primary": primary}


def _triple(value: Any, name: str) -> list[float]:
    v = _list(value, name) or [0.0, 0.0, 0.0]
    if len(v) != 3:
        raise _bad(f"{name} needs 3 numbers [x, y, z]")
    return [_finite(x, name) for x in v]


def _volume_physics(value: Any, name: str) -> dict[str, Any]:
    """体散射：{"model": "none"}、{"model": "dll", "dll", "mean_path", "params": {参数名: 值}} 或 {"model": "angle", "mean_path", "angle"}。"""
    d = _dict(value, name)
    model = _choice(d.get("model"), VOLUME_MODELS, f"{name}.model")
    if model == "none":
        _known(d, {"model"}, name)
        return {"model": model}
    if model == "photoluminescence":
        # 光致发光：吸收、发射、量子产率等写法同 OpticStudio 的体物理页，字段逐项回读
        # photoluminescence: absorption, emission, quantum yield … as on OpticStudio's volume physics tab, read back field by field
        # 2024 R1 实测：吸收、发射、激发、量子产率文件经 ZOS-API 写入被静默忽略（仍是 _sample_*），只能在 OpticStudio 界面里选
        # measured in 2024 R1: the absorption, emission, excitation and quantum-yield files ignore ZOS-API writes; pick them in the GUI
        files = sorted(k for k in PL_SPECTRA if k in d)
        if files:
            raise _bad(f"{name}: {files} cannot be set through the ZOS-API (OpticStudio 2024 R1 ignores the write and keeps its sample files); "
                       "choose the spectra in OpticStudio's Volume Physics tab, the other fields can be given here")
        _known(d, {"model", *PL_FIELDS}, name)
        out = {"model": model}
        for key, kind in PL_FIELDS.items():
            if key in d:
                out[key] = _typed(d[key], kind, f"{name}.{key}")
        return out
    out: dict[str, Any] = {"model": model, "mean_path": _size(d.get("mean_path"), f"{name}.mean_path", positive=True)}
    if model == "angle":
        _known(d, {"model", "mean_path", "angle"}, name)
        out["angle"] = _size(d.get("angle"), f"{name}.angle", positive=True)
        if out["angle"] > 180:
            raise _bad(f"{name}.angle is a scatter angle up to 180 degrees")
        return out
    _known(d, {"model", "dll", "mean_path", "params"}, name)
    dll = d.get("dll")
    if not isinstance(dll, str) or not dll.strip():
        raise _bad(f"{name}.dll needs the DLL file name, e.g. Henyey-Greenstein-bulk.dll")
    out["dll"] = dll.strip()
    out["params"] = {str(k).strip(): _finite(v, f"{name}.params.{k}") for k, v in _dict(d.get("params"), f"{name}.params").items()}
    return out


def _scatter(value: Any, name: str) -> dict[str, Any]:
    d = _dict(value, name)
    model = _choice(d.get("model"), SCATTER_FIELDS, f"{name}.model")
    fields = SCATTER_FIELDS[model]
    _known(d, {"model", "rays", *fields}, name)
    out: dict[str, Any] = {"model": model}
    for key, kind in fields.items():
        if key not in d:
            continue
        if kind == "number":
            out[key] = _size(d[key], f"{name}.{key}")
            if key == "fraction" and out[key] > 1:
                raise _bad(f"{name}.fraction is the scattered fraction of the energy, 0 to 1")
        elif kind == "params":
            out[key] = {str(k).strip(): _finite(v, f"{name}.params.{k}") for k, v in _dict(d[key], f"{name}.params").items()}
        else:
            out[key] = _name(d[key], f"{name}.{key}")
    missing = [k for k in SCATTER_REQUIRED.get(model, ()) if k not in out]
    if missing:
        raise _bad(f"{name}: the {model} model needs {', '.join(missing)}")
    if model in ("abg", "abg_file", "bsdf") and not ({"reflect", "transmit"} & set(out)):
        raise _bad(f"{name}: the {model} model needs reflect, transmit or both (the data names OpticStudio lists)")
    if "rays" in d:
        out["rays"] = _integer(d["rays"], f"{name}.rays", low=0)
    return out


def _face(value: Any, name: str) -> dict[str, Any]:
    """一个面的属性：{"is": "default|reflective|absorbing", "coating", "scatter": {...}} 或 {"profile": 名字}。"""
    d = _dict(value, name)
    _known(d, FACE_KEYS, name)
    if "profile" in d:
        if len(d) > 1:
            raise _bad(f"{name}: a profile sets the whole face; write either profile or is/coating/scatter")
        return {"profile": _name(d["profile"], f"{name}.profile")}
    out: dict[str, Any] = {}
    if "is" in d:
        out["is"] = _choice(d["is"], FACE_IS, f"{name}.is")
    if "coating" in d:
        out["coating"] = _name(d["coating"], f"{name}.coating")
        # 2024 R1 实测：面设为反射或吸收时 OpticStudio 清掉膜层 / OpticStudio clears the coating of a reflective or absorbing face
        if out.get("is", "default") != "default":
            raise _bad(f"{name}: a face set to {out['is']} takes no coating (OpticStudio clears it)")
    if "scatter" in d:
        out["scatter"] = _scatter(d["scatter"], f"{name}.scatter")
    if not out:
        raise _bad(f"{name}: write is, coating, scatter or profile")
    return out


def _faces(value: Any, name: str) -> dict[str, dict[str, Any]]:
    """各面属性：键是面号（从 0 起，见 lens surfaces 的 faces）或 "all"（先写，再由面号覆盖）。"""
    out = {}
    for key, face in _dict(value, name).items():
        k = str(key).strip().lower()
        if k != "all" and not re.fullmatch(r"\d{1,4}", k):
            raise _bad(f"{name}: keys are face numbers from 0 or \"all\", got {key!r}")
        out[k if k == "all" else str(int(k))] = _face(face, f"{name}.{key}")
    if not out:
        raise _bad(f"{name} is empty")
    return dict(sorted(out.items(), key=lambda kv: -1 if kv[0] == "all" else int(kv[0])))


def _scatter_to(value: Any, name: str) -> dict[str, Any]:
    """散射光往哪去：{"objects": [物体号, …]}（Scatter To 列表）或 {"importance": [{"towards", "size", "limit"}, …]}（重要性采样，最多 6 行）。"""
    d = _dict(value, name)
    _known(d, {"objects", "importance"}, name)
    if len(d) != 1:
        raise _bad(f"{name}: write objects (a scatter-to list) or importance (importance sampling), one of them")
    if "objects" in d:
        objects = [_integer(v, f"{name}.objects", low=1) for v in _list(d["objects"], f"{name}.objects")]
        if not objects:
            raise _bad(f"{name}.objects is empty")
        return {"objects": sorted(set(objects))}
    rows = []
    for i, r in enumerate(_list(d["importance"], f"{name}.importance")):
        label = f"{name}.importance[{i}]"
        r = _dict(r, label)
        _known(r, {"towards", "size", "limit"}, label)
        rows.append({"towards": _integer(r.get("towards"), f"{label}.towards", low=1), "size": _size(r.get("size"), f"{label}.size", positive=True),
                     "limit": _size(r.get("limit", 1.0), f"{label}.limit")})
    if not 1 <= len(rows) <= IMPORTANCE_ROWS:
        raise _bad(f"{name}.importance takes 1 to {IMPORTANCE_ROWS} rows")
    return {"importance": rows}


def _typed(value: Any, kind: Any, name: str) -> Any:
    """按字段类型解析：number、int、flag、name、file、objects（物体号列表）或可选项元组。"""
    if isinstance(kind, tuple):
        return _choice(value, kind, name)
    if kind == "number":
        return _finite(value, name)
    if kind == "int":
        return _integer(value, name)
    if kind == "flag":
        return _flag(value, name)
    if kind == "objects":
        out = [_integer(v, name, low=1) for v in _list(value, name)]
        if not out:
            raise _bad(f"{name} is empty")
        return sorted(set(out))
    return _name(value, name)


def _typed_fields(value: Any, spec: dict[str, Any], name: str, extra: set[str] = frozenset()) -> dict[str, Any]:
    d = _dict(value, name)
    _known(d, set(spec) | set(extra), name)
    return {k: _typed(v, spec[k], f"{name}.{k}") for k, v in d.items() if k in spec}


def _source(value: Any, name: str) -> dict[str, Any]:
    """光源页：偏振、初相位、相干长度、反向、预传播、采样、体散射次数、阵列、光源颜色（光谱）。"""
    d = _dict(value, name)
    _known(d, SOURCE_KEYS, name)
    out: dict[str, Any] = {}
    if "polarization" in d:
        p = d["polarization"]
        if p == "random":
            out["polarization"] = "random"
        else:
            pol = _typed_fields(p, {"jx": "number", "jy": "number", "x_phase": "number", "y_phase": "number"}, f"{name}.polarization")
            if "jx" not in pol and "jy" not in pol:
                raise _bad(f'{name}.polarization: "random" or a Jones vector {{"jx", "jy", "x_phase", "y_phase"}}')
            out["polarization"] = pol
    for key, kind in (("initial_phase", "number"), ("coherence_length", "number"), ("reverse_rays", "flag"),
                      ("pre_propagation", "number"), ("sampling", SAMPLING), ("bulk_scatter", BULK_SCATTER)):
        if key in d:
            out[key] = _typed(d[key], kind, f"{name}.{key}")
    if "array" in d:
        a = _dict(d["array"], f"{name}.array")
        out["array"] = {"type": _choice(a.get("type"), ARRAY_TYPES, f"{name}.array.type"),
                        **_typed_fields({k: v for k, v in a.items() if k != "type"}, ARRAY_FIELDS, f"{name}.array")}
    if "color" in d:
        c = _dict(d["color"], f"{name}.color")
        out["color"] = {"mode": _choice(c.get("mode"), COLOR_MODES, f"{name}.color.mode"),
                        **_typed_fields({k: v for k, v in c.items() if k != "mode"}, COLOR_FIELDS, f"{name}.color")}
    if not out:
        raise _bad(f"{name} is empty")
    return out


def _diffraction(value: Any, name: str) -> dict[str, Any]:
    d = _dict(value, name)
    _known(d, {"split", "dll", "start_order", "stop_order", "reflect", "transmit"}, name)
    out: dict[str, Any] = {"split": _choice(d.get("split", "none"), DIFFRACTION_SPLIT, f"{name}.split")}
    if "dll" in d:
        out["dll"] = _name(d["dll"], f"{name}.dll")
    for key in ("start_order", "stop_order"):
        if key in d:
            out[key] = _integer(d[key], f"{name}.{key}")
    for key in ("reflect", "transmit"):
        if key in d:
            out[key] = {str(k).strip(): _finite(v, f"{name}.{key}.{k}") for k, v in _dict(d[key], f"{name}.{key}").items()}
    if out["split"] == "dll" and "dll" not in out:
        raise _bad(f"{name}: split by DLL needs dll")
    return out


def _index(value: Any, name: str) -> dict[str, Any]:
    """折射率页：各向同性（默认）、双折射（光轴、追迹哪些光线）或 GRIN（DLL 及参数）。"""
    d = _dict(value, name)
    kind = _choice(d.get("type"), INDEX_TYPES, f"{name}.type")
    if kind == "isotropic":
        _known(d, {"type"}, name)
        return {"type": kind}
    if kind == "birefringent":
        _known(d, {"type", "axis", "mode", "reflections"}, name)
        out: dict[str, Any] = {"type": kind, "axis": _triple(d.get("axis"), f"{name}.axis")}
        if not any(out["axis"]):
            raise _bad(f"{name}.axis must not be zero")
        for key in ("mode", "reflections"):
            if key in d:
                out[key] = str(d[key]).strip()
        return out
    _known(d, {"type", "dll", "file", "max_step", "params"}, name)
    out = {"type": kind, "dll": _name(d.get("dll"), f"{name}.dll")}
    if "file" in d:
        out["file"] = _name(d["file"], f"{name}.file")
    if "max_step" in d:
        out["max_step"] = _size(d["max_step"], f"{name}.max_step", positive=True)
    out["params"] = {str(k).strip(): _finite(v, f"{name}.params.{k}") for k, v in _dict(d.get("params"), f"{name}.params").items()}
    return out


def _object(o: Any, number: int) -> dict[str, Any]:
    name = f"objects[{number}]"
    if not isinstance(o, dict):
        raise _bad(f"{name} must be a JSON object")
    _known(o, OBJECT_KEYS, name)
    kind = o.get("type")
    if not isinstance(kind, str) or not TYPE_NAME_RE.match(kind):
        raise _bad(f"{name}.type must be an OpticStudio object type name such as CylinderVolume, SourceVolumeCylindrical, DetectorRectangle")
    item: dict[str, Any] = {"type": kind, "comment": str(o.get("comment", ""))}
    for key in ("ref", "inside_of"):
        # 只能引用排在前面的物体（OpticStudio 的规则）/ only objects listed before this one
        item[key] = _integer(o.get(key, 0), f"{name}.{key}", low=0)
        if item[key] >= number:
            raise _bad(f"{name}.{key} must name an object listed before it (1-{number - 1}) or 0, got {item[key]}")
    item["position"] = _triple(o.get("position"), f"{name}.position")
    item["tilt"] = _triple(o.get("tilt"), f"{name}.tilt")
    # 要文件的类型（CAD、多边形物体、光线文件光源、DLL 物体……）在 OpticStudio 的数据目录里按文件名选
    # types that need a file (CAD, polygon objects, ray-file sources, DLL objects ...) pick it by name from OpticStudio's data folders
    for key in ("file", "file2"):
        if key in o:
            item[key] = _name(o[key], f"{name}.{key}")
    if "file2" in item and "file" not in item:
        raise _bad(f"{name}: file2 needs file")
    # 2024 R1 实测：这类物体的注释栏就是文件名，写注释会把文件改成 "<注释>.<扩展名>"，追迹时没有光线也不报错
    # in 2024 R1 the comment column of these objects is the file name; writing a comment renames the file and the trace silently has no rays
    if "file" in item and item["comment"]:
        raise _bad(f"{name}: an object with a file keeps the file name in the comment column; leave comment out "
                   "(describe the object in the model notes, refer to it by number or by the file name)")
    is_source = kind.startswith("Source")
    for key, parse in (("faces", _faces), ("scatter_to", _scatter_to)):
        if o.get(key) is not None:
            if is_source:
                raise _bad(f"{name}: sources have no faces")
            item[key] = parse(o[key], f"{name}.{key}")
    if o.get("source") is not None:
        if not is_source:
            raise _bad(f"{name}: source settings belong to source objects, not {kind}")
        item["source"] = _source(o["source"], f"{name}.source")
    if o.get("type_options") is not None:
        item["type_options"] = _typed_fields(o["type_options"], TYPE_OPTIONS, f"{name}.type_options")
    if o.get("diffraction") is not None:
        item["diffraction"] = _diffraction(o["diffraction"], f"{name}.diffraction")
    # 折射率页叫 index_model：读回的物体里 index 是物体号 / the Index tab is index_model: index is the object number in read-backs
    if o.get("index_model") is not None:
        if is_source or kind.startswith("Detector"):
            raise _bad(f"{name}: the index tab belongs to objects with a material")
        item["index_model"] = _index(o["index_model"], f"{name}.index_model")
    is_part = not kind.startswith(("Source", "Detector"))
    if "material" in o:
        if not is_part:
            raise _bad(f"{name}: sources and detectors take no material")
        item["material"] = normalize_material(o["material"], name)
    if o.get("params"):
        item["params"] = _params(o["params"], f"{name}.params")
    if o.get("volume_physics") is not None:
        if not is_part:
            raise _bad(f"{name}: volume physics belongs to volume objects, not {kind}")
        item["volume_physics"] = _volume_physics(o["volume_physics"], f"{name}.volume_physics")
    return item


def _nsc_settings(value: Any) -> dict[str, Any]:
    d = _dict(value, "nsc")
    _known(d, set(NSC_SETTINGS), "nsc")
    out: dict[str, Any] = {}
    for key, v in d.items():
        if key == "simple_ray_splitting":
            out[key] = _flag(v, f"nsc.{key}")
        elif key.startswith("max_"):
            out[key] = _integer(v, f"nsc.{key}", low=1)
            # 2024 R1 实测：交点数上限最大 4000，写更大的值被悄悄改回 4000；段数上限可到 200 万
            # measured in 2024 R1: the intersection limit tops out at 4000 (larger values silently become 4000)
            if key == "max_intersections" and out[key] > 4000:
                raise _bad("nsc.max_intersections: OpticStudio allows at most 4000 intersections per ray; raise max_segments instead")
        else:
            out[key] = _size(v, f"nsc.{key}")
    return out


def normalize_nsc(p: dict[str, Any]) -> dict[str, Any]:
    """非序列处方：系统设置、非序列选项和物体表（类型、位置、材料、参数、体散射）。光源和探测器也是物体。
    A non-sequential prescription: system settings, NSC options and the object list; sources and detectors are objects too."""
    _known(p, NSC_TOP_KEYS, "the prescription")
    title = p.get("title", "")
    if not isinstance(title, str):
        raise _bad(f"title must be a string, got {title!r}")
    coating_file = p.get("coating_file")
    if coating_file is not None and (not isinstance(coating_file, str) or not coating_file.strip()):
        raise _bad("coating_file must be a coating file name such as COATING.DAT")
    objects = _objects(p.get("objects"), "objects")
    return {"mode": "non_sequential", "title": title, "units": _choice(p.get("units", "mm"), UNITS, "units"),
            "wavelengths": _wavelengths(p.get("wavelengths")),
            "catalogs": [_name(c, "catalogs") for c in _list(p.get("catalogs"), "catalogs") or ["SCHOTT"]],
            "coating_file": coating_file.strip() if coating_file else None, "nsc": _nsc_settings(p.get("nsc")), "objects": objects,
            **_power_units(p)}


def _objects(value: Any, name: str) -> list[dict[str, Any]]:
    """一组非序列物体（纯非序列系统，或序列系统里一个 NonSequential 面的物体）；物体号在组内从 1 起。
    A group of non-sequential objects (a pure NSC system, or the objects of one NonSequential surface); numbered from 1 within the group."""
    objects = [_object(o, k) for k, o in enumerate(_list(value, name), start=1)]
    if not objects:
        raise _bad(f"{name}: at least one object is required")
    for k, item in enumerate(objects, start=1):
        st = item.get("scatter_to") or {}
        targets = st.get("objects", []) + [r["towards"] for r in st.get("importance", [])]
        targets += (item.get("type_options") or {}).get("consider", []) + (item.get("type_options") or {}).get("ignore", [])
        if any(t > len(objects) for t in targets):
            raise _bad(f"{name}[{k}] names object {max(targets)}, but there are {len(objects)} objects")
    return objects


def _power_units(p: dict[str, Any]) -> dict[str, Any]:
    """光源功率单位和分析（照度）单位；不写时用 OpticStudio 的设置（新系统为 W 和 W/cm²）。"""
    out = {}
    if "source_units" in p:
        out["source_units"] = _choice(p["source_units"], SOURCE_UNITS, "source_units")
    if "analysis_units" in p:
        out["analysis_units"] = _choice(p["analysis_units"], ANALYSIS_UNITS, "analysis_units")
    return out


def normalize(p: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(p, dict):
        raise _bad("the prescription must be a JSON object")
    mode = _choice(p.get("mode", "sequential"), ("sequential", "non_sequential"), "mode")
    if mode == "non_sequential":
        return normalize_nsc(p)
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
    wavelengths = _wavelengths(p.get("wavelengths"))
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
        "wavelengths": wavelengths,
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
        "nsc": _nsc_settings(p.get("nsc")),
        **_power_units(p),
    }
    if image is not None:
        out["image"] = image
    return out


HELP_EPILOG = """Input: sequential Prescription JSON
  Unknown keys are rejected. Switches accept JSON true/false only; enum values
  must be listed names. A prescription builds a system; checks and recipes
  define how to judge it. The title is written into OpticStudio.

  {
    "title": "Sequential example", "units": "mm",
    "aperture": {"type": "entrance_pupil_diameter", "value": 10},
    "telecentric_object": false,
    "fields": {"type": "angle", "y": [0, 10, 14], "x": [0, 0, 0],
               "weights": [1, 1, 1], "normalization": "radial"},
    "wavelengths": {"um": [0.486, 0.588, 0.656], "weights": [1, 1, 1], "primary": 2},
    "catalogs": ["CDGM"], "ray_aiming": "real",
    "environment": {"temperature": 20, "pressure": 1, "adjust_index": true},
    "polarization": {"unpolarized": true}, "coating_file": "COATING.DAT",
    "ray_aiming_options": {"robust": true},
    "object": {"thickness": "inf", "material": "", "comment": ""},
    "surfaces": [
      {"radius": 30, "thickness": 4, "material": "H-ZK9B",
       "mech_semi_diameter": 7, "coating": "AR"},
      {"type": "EvenAspheric", "radius": -80, "conic": -1.2, "thickness": 2,
       "params": {"par2": 1.2e-5, "6th Order Term": -3e-8},
       "solves": {"radius": "variable", "par2": "variable"}},
      {"radius": "inf", "thickness": 5, "stop": true, "semi_diameter": 4},
      {"type": "CoordinateBreak", "params": {"par3": 12}},
      {"radius": "inf", "thickness": 0, "material": "MIRROR",
       "aperture": {"type": "rectangular", "x_half": 8, "y_half": 6}},
      {"type": "CoordinateBreak", "params": {"par3": 12}, "thickness": -20},
      {"radius": -60, "thickness": 18,
       "decenter_tilt": {"before": {"decenter_y": 0.5, "tilt_x": 1}, "after": "reverse"},
       "solves": {"thickness": {"type": "marginal_ray_height", "height": 0}}}
    ],
    "image": {"radius": -120},
    "configurations": [{"operand": "THIC", "params": [3], "values": [5, 5.6]},
                       {"operand": "TEMP", "values": [20, -40]}]
  }

System fields
  units: mm (default), cm, in, m.
  aperture.type: object_na, entrance_pupil_diameter, image_f_number,
    working_f_number, object_cone_angle (half angle in degrees), float_by_stop.
    float_by_stop forbids value and requires semi_diameter on the stop surface.
  fields.type: object_height, angle (<90 degrees), paraxial_image_height,
    real_image_height, theodolite_angle. x defaults to 0 and weights to 1;
    normalization is radial (default) or rectangular.
  wavelengths: um and weights arrays; primary is a 1-based wavelength number.
  environment defaults: temperature 20 C, pressure 1 atm, adjust_index false.
  ray_aiming: off, paraxial, real.
  Rejected combinations: infinite object with object_na, object_cone_angle or
    object_height; telecentric_object with infinite object, angular fields or
    entrance_pupil_diameter; TEMP/PRES configurations without adjust_index true.
    Aperture, field type and telecentric settings are read back after building;
    OpticStudio changing them is BAD_ARGS.

Surfaces and image
  type defaults to standard; paraxial uses focal_length and opd_mode (default 1).
  Other types use case-insensitive ZOS-API SurfaceType names: EvenAspheric,
    OddAsphere, QTypeAsphere, Toroidal, Biconic, XYPolynomial, ZernikeStandardSag,
    Binary2, CoordinateBreak, Gradient1 through Gradient12, Gradium, etc.
  Common fields: radius ("inf" for flat), conic, thickness, comment, stop (at most
    one), semi_diameter (fixed if given, otherwise automatic), mech_semi_diameter
    (part outer edge used by interference checks), material, coating.
  material: catalog name, MIRROR, or {"model": {"nd": 1.5, "vd": 60, "dpgf": 0}}.
  params: {"par2": 1e-5, "6th Order Term": -3e-8}. Keys are Par column numbers or
    case-insensitive column headers; values follow the cell's number/integer/text
    type. Entries are applied in order (some types expose columns after setting
    a term count), read back individually, and unused columns cannot be set.
  decenter_tilt.before: decenter_x, decenter_y, tilt_x, tilt_y, tilt_z, order
    (decenter_tilt or tilt_decenter). after is "reverse" or the same explicit
    fields. CoordinateBreak uses params par1 through par6 instead.
  aperture: circular/circular_obscuration with min,max;
    rectangular/rectangular_obscuration/elliptical/elliptical_obscuration with
    x_half,y_half; spider with arms,width. All allow x_decenter,y_decenter.
    Use two surfaces for a central obscuration plus support arms.
  solves maps radius/thickness/conic/material/par<N> to:
    "variable" (material variables use saos opt vary --mode instead),
    {"type": "pickup", "surface": 3, "scale": 1, "offset": 0},
    {"type": "marginal_ray_height", "height": 0, "zone": 0} (thickness only),
    {"type": "f_number", "value": 4} (radius only).
    Material pickups omit scale/offset; solves are applied after all surfaces
    exist and may pick up a later surface.
  object (required): thickness (a number or "inf"), material, comment.
  image accepts surface fields except thickness, material, stop, focal_length
    and opd_mode. Use it for curved or tilted image surfaces.

Configurations and optional system settings
  configurations: one row per ZOS-API MultiConfigOperandType (THIC, GLSS, TEMP,
    PRES, PRAM, COTN, etc.); params has up to 3 integer operand parameters;
    values has one value per configuration (at least 2, same count in every row).
    vary:true makes the row variable. Building ends in configuration 1.
    TEMP/PRES each allow at most one row; configuration 1 sets the corresponding
    environment value. An explicit conflicting environment value is rejected;
    temperature and pressure are read back after building.
  afocal: true or {"units": "milliradians"}; units: microradians, milliradians,
    radians, arcseconds, arcminutes, degrees. Omitted means off. Rejects image
    height fields and image_f_number/working_f_number apertures; read back after
    building. Analyses then report angles, not image-plane spot positions.
  apodization: {"type": "uniform"}, {"type": "gaussian", "factor": 1}, or
    {"type": "cosine_cubed"}; factor is required only for gaussian.
  polarization: unpolarized, jx, jy, x_phase, y_phase (degrees), method
    (x_axis/y_axis/z_axis), convert_thin_film_phase. Only given fields are set
    and read back. unpolarized:true forbids Jones-vector fields; Jones fields
    imply unpolarized:false when omitted. jx and jy cannot both be zero.
  coating_file must be an OpticStudio-listed file. A surface coating names a
    coating in it (case-insensitive); unknown names return details.choices.
    Define custom coating layers in that file, not in the prescription.
  ray_aiming_options: robust, enhanced, cache, auto_pupil_shift,
    scale_pupil_shift_by_field (booleans), pupil_shift [x,y,z], pupil_compress
    [x,y]. Requires ray_aiming other than off; explicit pupil_shift conflicts
    with auto_pupil_shift:true and otherwise disables automatic shift. Read back.
  Gratings, holograms and binary optics use ordinary types (DiffractionGrating,
    EllipticalGrating1, VariableLineSpaceGrating, ToroidalGrat, Hologram1, Binary2)
    and params for groove density, order and phase coefficients.
  Mixed mode: a surface of type NonSequential may carry "objects" (written as
    in the non-sequential prescription below; object numbers start at 1 within
    the group). Its parameters use column headers: Exit Loc X/Y/Z, Exit Tilt
    X/Y/Z, Order, Reverse Rays, Back Propagation Distance. The build selects
    the group with NCE.SetActiveSurface; objects survive saving and sequential
    rays pass through the group (2024 R1). A sequential prescription also takes
    nsc, source_units and analysis_units. The workflow window's sequential
    layout does not draw group objects yet.
  Physical optics and coating layer definitions require a script:
    uv run saos exec --help. Tolerancing is a scan tolerance plan:
    uv run saos scan --help.

Input: pure non-sequential Prescription JSON
  {
    "mode": "non_sequential", "title": "Volume scattering", "units": "mm",
    "wavelengths": {"um": [0.5, 0.515, 0.53, 0.545],
                    "weights": [1, 1, 1, 1], "primary": 2},
    "catalogs": ["SCHOTT", "MISC"],
    "nsc": {"max_segments": 20000, "max_intersections": 4000,
            "simple_ray_splitting": true},
    "objects": [
      {"type": "CylinderVolume", "comment": "Cortex", "position": [0, 0, 1.15],
       "material": {"model": {"nd": 1.36, "vd": 65}},
       "params": {"Front R": 5, "Z Length": 0.7, "Back R": 5},
       "volume_physics": {"model": "dll", "dll": "Henyey-Greenstein-bulk.dll",
                          "mean_path": 0.05, "params": {"Transmission": 0.99, "g": 0.9}}},
      {"type": "SourceVolumeCylindrical", "comment": "GFP", "inside_of": 1,
       "params": {"# Analysis Rays": 100000, "Power(Watts)": 1,
                  "X Half Width": 5, "Y Half Width": 5, "Z Half Width": 0.5}},
      {"type": "DetectorRectangle", "comment": "top", "position": [0, 0, 2.02],
       "params": {"X Half Width": 5, "Y Half Width": 5,
                  "# X Pixels": 100, "# Y Pixels": 100}}
    ]
  }

Non-sequential fields and constraints
  Top-level keys only: mode, title, units, wavelengths, catalogs, coating_file,
    nsc, objects, source_units, analysis_units. No aperture, fields or surfaces.
  source_units: watts, lumens, joules. analysis_units: W/mm2, W/cm2, W/in2,
    W/m2, W/ft2. Both are also accepted by sequential prescriptions.
  nsc: max_intersections (at most 4000 in 2024 R1), max_segments, max_nested,
    min_relative_intensity, min_absolute_intensity, glue_distance,
    simple_ray_splitting. Only given settings are written. Too few segments
    truncate scattered rays; checks must inspect nsc_trace.dead_ray_errors.
  objects: ordered ZOS-API ObjectType names (CylinderVolume, RectangularVolume,
    SourceVolumeCylindrical, SourceTwoAngle, DetectorRectangle, DetectorVolume).
    ref and inside_of refer only to earlier objects or 0; position and tilt
    have 3 numbers each (default zeros). material accepts catalog names, MIRROR,
    ABSORB or model glass as above; sources and detectors do not accept it.
    Model glass is read back with NCE.GetIndex; a primary-wave index differing
    from nd by more than 0.05 is rejected. Do not encode it as a string of numbers.
    params uses par<N> or NCE column headers (e.g. "Z Length", "# Analysis Rays").
    Object keys: type, comment, ref, inside_of, position, tilt, material, params,
    volume_physics, file, file2, faces, scatter_to, source, type_options,
    diffraction, index_model.
  file (and file2): file-based types (CADPartSTEPIGESSAT, CADPartSTL,
    PolygonObject, SourceFile, SourceDLL, UserDefinedObject, GridSagLens, etc.)
    pick a file OpticStudio lists (case-insensitive); a missing or wrong name
    returns details.choices. Put your own files in the matching folder under
    Documents/Zemax/Objects first. Their comment column is the file name
    (2024 R1: writing a comment renames the file to <comment>.<ext> and the
    trace silently finds no rays), so comment is rejected; refer to them by
    number or file name (e.g. block.STL). A ray-file source's # Analysis Rays
    cannot exceed the rays in the file (sample.SDF has 11).
  faces: keys are face numbers (from 0; lens surfaces lists numbers and names,
    e.g. Standard Lens 0 Side Faces, 1 Front Face, 2 Back Face) or "all"
    (written first, then overridden by numbers). Each face takes is (default,
    reflective, absorbing), coating (a name in the coating file) and scatter,
    or only profile (a stored OpticStudio face profile applied whole).
    Reflective/absorbing clears the coating, so is and coating together are
    rejected. scatter by model, extra fields rejected: {"model": "none"},
    lambertian (fraction), gaussian (fraction, sigma), abg (reflect, transmit:
    ABg data names), abg_file, bsdf (reflect, transmit: file names), user (dll,
    fraction, file, params); every model accepts rays (scatter rays for that
    face). Wrong names list the choices; each face is read back. Sources have
    no faces.
  scatter_to, one of: {"objects": [n, ...]} is the Scatter To list; scattered
    rays not heading for them are dropped (2024 R1: with 100% Lambertian and
    only a small off-axis detector listed, a near large detector fell from
    0.917 W to 0). Written space-separated and read back.
    {"importance": [{"towards": n, "size": r, "limit": 1}, ...]} (at most 6
    rows) aims scattered rays at a sphere of radius size around the target's
    origin, weighting energy by the scatter distribution. size must enclose
    the whole target: for a +/-2 mm square detector (half diagonal 2.83 mm),
    size 3 or 5 agreed with no importance sampling within about 2 standard
    errors with 200-400x more hits; size 2 read 17% low (experiments/nsc_probe).
  source (sources only): polarization ("random" or {"jx", "jy", "x_phase",
    "y_phase"}), initial_phase, coherence_length, reverse_rays, pre_propagation,
    sampling (random, sobol), bulk_scatter (many, once, never), array
    ({"type": "rectangular|circular|hexapolar|hexagonal", "number_x",
    "number_y", "spacing_x", "spacing_y", "number", "radius", "rings",
    "spacing"}), color ({"mode": "system_wavelengths|uniform|blackbody|
    color_temperature|d65|spectrum_file|cie1931_xyz|cie1931_xy|cie1931_rgb|
    cie1976", ...} with temperature, from, to (um), count, file (a listed
    .spcd etc.), x/y/z, cx/cy, r/g/b, u/v; fields a mode lacks are rejected
    with the usable ones).
  type_options (Type tab): rays_ignore (never, always, on_launch), consider,
    ignore (object number lists), is_detector, pixel_interpolation,
    global_xyz_rotation, consider_ignore_when_splitting.
  diffraction (types with a Diffraction tab: gratings, Binary, ...): split
    (none, table, dll), dll, start_order, stop_order, reflect, transmit (DLL
    parameter name to value).
  index_model (Index tab; not "index", which is the object number in
    read-backs): {"type": "isotropic"}, {"type": "birefringent", "axis":
    [x, y, z], "mode", "reflections"} (OpticStudio enum names, listed when
    wrong) or {"type": "grin", "dll", "file"?, "max_step"?, "params"}.
  Every tab is read back after writing; a mismatch is BAD_ARGS with both
    values. lens surfaces also lists source and type_options, and diffraction
    and index_model when changed from default.
  volume_physics: {"model": "none"},
    {"model": "angle", "mean_path": 0.05, "angle": 10}, or
    {"model": "dll", "dll": "Henyey-Greenstein-bulk.DLL", "mean_path": 0.05,
     "params": {"Transmission": 0.99, "g": 0.9}}.
    DLL/parameter names are case-insensitive and read back. Mie.dll exposes
    Particle index, Size (microns), Density (cm^-3), Min. thresh., Transmission.
    {"model": "photoluminescence", ...}: efficiency (quantum_yield or
    excitation), pl_density, extinction_coefficient, extinction_wavelength,
    threshold, basic_algorithm, consider_mie, particle_index,
    particle_density, particle_radius, use_mie_density, mie_mean_path,
    pl_mean_path, each read back (no generic mean_path). Absorption, emission,
    excitation and quantum-yield spectrum files written through ZOS-API are
    silently ignored (2024 R1), so the prescription rejects them: choose them
    on OpticStudio's Volume Physics tab; read-backs and the model description
    list the current files.
    With scatter:false, paths still attenuate by mean_path without changing
    direction. Partial volume overlaps cause trace errors; a later object fully
    inside an earlier object is valid nesting and takes precedence.
  Model approval locks types, files, ref, inside_of and medium categories;
    for sources and detectors their positions, tilts, parameters (except ray
    counts) and every tab (faces, scatter_to, source, type_options,
    diffraction, index_model); for notes.environment objects also material,
    parameters, placement, volume scattering and every tab; for designed parts
    only the volume scattering model/DLL, each face's reflective/absorbing
    state and scatter model, the diffraction split and the index_model type
    (coatings, scatter fractions and importance sampling may change between
    rounds). Wavelengths, coating_file
    and simple_ray_splitting are also locked. Notes use objects rather than surfaces:
    uv run saos lens describe --help.

Results and files
  Saves and loads lenses/<--as>.zmx. Extension mode first captures unsaved window
  content in lenses/captures/; captured names that copy. --overwrite is required
  for an existing destination; --discard drops the agent's own unsaved changes.

Common errors
  BAD_ARGS: invalid combination, unknown field/type/parameter; details.choices
    lists type/operand/coating names, details.parameters lists parameter columns.
  FILE_EXISTS, UNSAVED_CHANGES, JOB_MISMATCH, ZOS_SAVE_FAILED, ZOS_ERROR.
"""
