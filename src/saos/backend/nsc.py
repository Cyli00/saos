"""非序列（NSC）系统：按处方搭建、读出物体、画轮廓、追迹和读探测器（只供 ZosBackend 和 saos.kit 使用）。

物体类型和参数不逐个写代码：类型写 ZOS-API 的 ObjectType 名（CylinderVolume、SourceVolumeCylindrical、DetectorRectangle），
参数按 Par 列号（par2）或非序列编辑器的列标题（"Z Length"）填写，名字写错时 OpticStudio 拒绝并列出可选项。
体散射（Volume Physics）按模型名、DLL 名、平均自由程和 DLL 参数名写。

2024 R1 实测（2026-10-20）：
- 不开散射（ScatterNSCRays 为假）时，体散射物体里的光线仍按平均自由程衰减，只是不改方向：散射组织里的光几乎全被扣掉。
- 同一随机种子（SetRandomSeed）两次追迹结果逐位相同。
- 每条光线的段数上限（MaximumSegmentsPerRay）从 4000 降到 300，探测器功率减半：段数不够时光线被截断，结果偏低；
  追迹工具的 DeadRayErrors 随之变化，作为截断的指标一并返回。
- 光线数据库（ZRD）只在镜头文件用反斜杠路径加载时写得出来；强散射时很大（2000 条光线不压缩 265 MB）。
"""

from __future__ import annotations

import math
import re
import time
from typing import Any, Callable

from saos.protocol import ErrorCode, SaosError

# 处方写法 → ZOS-API 的体散射模型名
VOLUME_MODELS = {"none": "None", "angle": "AngleScattering", "dll": "DLLDefinedScattering", "photoluminescence": "PhotoluminescenceModel"}
# 处方写法 → INSCSystemData 的属性名
NSC_SETTINGS = {"max_intersections": "MaximumIntersectionsPerRay", "max_segments": "MaximumSegmentsPerRay",
                "max_nested": "MaximumNestedTouchingObjects", "min_relative_intensity": "MinimumRelativeRayIntensity",
                "min_absolute_intensity": "MinimumAbsoluteRayIntensity", "glue_distance": "GlueDistanceInLensUnits",
                "simple_ray_splitting": "SimpleRaySplitting"}
ZRD_FORMATS = ("UncompressedFullData", "CompressedBasicData", "CompressedFullData")
# 处方写法 → FaceIsType 和 ObjectScatteringTypes 的成员名
FACE_IS = {"default": "ObjectDefault", "reflective": "Reflective", "absorbing": "Absorbing"}
SCATTER_MODELS = {"none": "None", "lambertian": "Lambertian", "gaussian": "Gaussian", "abg": "ABg", "abg_file": "ABgFile",
                  "bsdf": "BSDF", "user": "User"}
# 各模型的字段 → IOSS_<模型> 接口的属性；名字类字段另给列出可选名的方法（2024 R1 实测，ABgFile 与 BSDF 的方法名不同）
# per-model fields → IOSS_<model> properties; name fields also give the method listing the choices (measured in 2024 R1)
SCATTER_ATTRS = {"fraction": "ScatterFraction", "sigma": "Sigma", "dll": "DLLName", "file": "FileName"}
SCATTER_NAME_ATTRS = {"abg": ("ReflectName", "TransmitName", "GetAvailableABgNames"),
                      "abg_file": ("ReflectFile", "TransmitFile", "GetAvailableFiles"),
                      "bsdf": ("ReflectFile", "TransmitFile", "GetAvailableFileNames")}
SCATTER_LISTS = {"dll": "GetAvailableDLLNames", "file": "GetAvailableFileNames"}
# 光致发光模型（IVMPS_PhotoluminscenceModel）：处方字段 → 属性，文件类另给列出可选文件的方法
# the photoluminescence model: prescription field → property, plus the method listing the files for file fields
PL_ATTRS = {"absorption": ("AbsorptionFile", "GetAvailableAbsorptionFiles"), "emission": ("EmissionFile", "GetAvailableEmissionFiles"),
            "excitation": ("ExcitationFile", "GetAvailableExcitationFiles"), "quantum_yield": ("QuantumYield", "GetAvailableQuantumYieldFiles"),
            "efficiency": ("EfficiencySpectrum", None), "pl_density": ("PLDensity", None), "extinction_coefficient": ("ExtinctionCoefficient", None),
            "extinction_wavelength": ("ExtinctionWavelength", None), "threshold": ("Threshold", None), "basic_algorithm": ("BasicAlgorithm", None),
            "consider_mie": ("ConsiderMieScattering", None), "particle_index": ("ParticleIndex", None), "particle_density": ("ParticleDensity", None),
            "particle_radius": ("ParticleRadius", None), "use_mie_density": ("UseMieDensity", None), "mie_mean_path": ("MieMeanPath", None),
            "pl_mean_path": ("PLMeanPath", None)}
PL_EFFICIENCY = {"quantum_yield": "QuantumYield", "excitation": "Excitation"}
# 光源页 / Sources tab
SOURCE_ATTRS = {"initial_phase": "InitialPhaseDeg", "coherence_length": "CoherenceLength", "reverse_rays": "ReverseRays",
                "pre_propagation": "PrePropagation"}
SOURCE_ENUMS = {"sampling": ("SamplingMethod", {"random": "Random", "sobol": "Sobol"}),
                "bulk_scatter": ("BulkScatter", {"many": "Many", "once": "Once", "never": "Never"})}
ARRAY_TYPES = {"none": "None", "rectangular": "Rectangular", "circular": "Circular", "hexapolar": "Hexapolar", "hexagonal": "Hexagonal"}
ARRAY_ATTRS = {"number_x": "ArrayNumberX", "number_y": "ArrayNumberY", "spacing_x": "ArraySpacingX", "spacing_y": "ArraySpacingY",
               "number": "ArrayNumber", "radius": "ArrayRadius", "rings": "ArrayRings", "spacing": "ArraySpacing"}
COLOR_MODES = {"system_wavelengths": "SystemWavelengths", "uniform": "UniformPowerSpectrum", "blackbody": "BlackBodySpectrum",
               "color_temperature": "ColorTemperature", "d65": "D65White", "spectrum_file": "SpectrumFile", "cie1931_xyz": "CIE1931Tristimulus",
               "cie1931_xy": "CIE1931Chromaticity", "cie1931_rgb": "CIE1931RGB", "cie1976": "CIE1976"}
COLOR_ATTRS = {"temperature": "TemperatureK", "from": "WavelengthFrom", "to": "WavelengthTo", "count": "SpectrumCount", "file": "SpectrumFile",
               "x": "X", "y": "Y", "z": "Z", "cx": "cx", "cy": "cy", "r": "R", "g": "G", "b": "B", "u": "u", "v": "v"}
# Type 页 / Type tab
TYPE_ATTRS = {"rays_ignore": "RaysIgnoreObject", "consider": "ConsiderObjects", "ignore": "IgnoreObjects", "is_detector": "ObjectIsADetector",
              "pixel_interpolation": "UsePixelInterpolation", "global_xyz_rotation": "UseGlobalXYZRotationOrder",
              "consider_ignore_when_splitting": "UseConsiderIgnoreWhenSplitting"}
RAYS_IGNORE = {"never": "Never", "always": "Always", "on_launch": "OnLaunch"}
DIFFRACTION_SPLIT = {"none": "DontSplitByOrder", "table": "SplitByTable", "dll": "SplitByDLL"}
INDEX_TYPES = {"isotropic": "Isotropic", "birefringent": "Birefringent", "grin": "GRIN"}
SOURCE_UNITS = {"watts": "Watts", "lumens": "Lumens", "joules": "Joules"}
ANALYSIS_UNITS = {"W/mm2": "WattsPerMMSq", "W/cm2": "WattsPerCMSq", "W/in2": "WattsPerinSq", "W/m2": "WattsPerMSq", "W/ft2": "WattsPerftSq"}
# 采样数不是结构：改光线数不算改模型 / sampling is not structure
SAMPLING_HEADERS = ("# layout rays", "# analysis rays")
MODEL_RE = re.compile(r"^\s*(-?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)\s*,\s*(-?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)")


def is_nsc(system) -> bool:
    return str(system.Mode) == "NonSequential"


def kind_of(type_name: str) -> str:
    """source、detector 或 object（ObjectType 名的前缀）。"""
    return "source" if type_name.startswith("Source") else "detector" if type_name.startswith("Detector") else "object"


# ---------- 单元格 / cells ----------

def _cell(zosapi, obj, n: int):
    col = getattr(zosapi.Editors.NCE.ObjectColumn, f"Par{n}", None)
    if col is None:
        return None
    try:
        cell = obj.GetObjectCell(col)
    except Exception:  # noqa: BLE001 - 超出该类型的列 / beyond this type's columns
        return None
    # 没有标题的列读出来是 None（实测圆柱体的 Par4）/ a column without a header reads as None (Par4 of a cylinder)
    header = str(cell.Header) if cell is not None and cell.Header is not None else ""
    return cell if header and header != "None" and "unused" not in header.lower() else None


def param_cells(zosapi, obj) -> dict[int, Any]:
    """该物体类型可用的参数列 {列号: 单元格}。/ The usable parameter columns of the object's type."""
    out = {}
    for n in range(1, 255):
        if getattr(zosapi.Editors.NCE.ObjectColumn, f"Par{n}", None) is None:
            break
        cell = _cell(zosapi, obj, n)
        if cell is not None:
            out[n] = cell
    return out


def param_cell(zosapi, obj, label: str, key: str, cells: dict[int, Any] | None = None):
    """物体参数单元格：par<N> 按列号，其余按列标题（不分大小写）；没有时报错并列出该类型的参数。写了某些参数后会多出列，查不到时重扫一次。
    An object parameter cell by par<N> or column header; the error lists the type's parameters. Rescanned once on a miss."""
    m = re.match(r"^par([1-9]\d{0,2})$", key, re.IGNORECASE)
    if m:
        cell = _cell(zosapi, obj, int(m.group(1)))
        if cell is not None:
            return cell
    for attempt in (cells, None):
        if attempt is None:
            cells = param_cells(zosapi, obj)
        if not m:
            for c in cells.values():
                if str(c.Header).strip().lower() == key.strip().lower():
                    return c
        if attempt is None:
            break
    raise SaosError(ErrorCode.BAD_ARGS, f"{label} ({obj.TypeName}) has no parameter {key!r}",
                    {"parameters": {f"par{n}": str(c.Header) for n, c in cells.items()}})


# ---------- 读取 / reading ----------

def volume_physics(obj) -> dict[str, Any] | None:
    """体散射设置：模型、DLL、平均自由程、角度和 DLL 参数；该物体没有体散射页时为 None。"""
    vp = obj.VolumePhysicsData
    if not bool(vp.IsVolumePhysicsAvailable):
        return None
    model = str(vp.Model)
    out: dict[str, Any] = {"model": model}
    if model == "None":
        return out
    if model == "PhotoluminescenceModel":
        pl = vp.ModelSettings._S_PhotoluminescenceModel
        out["model"] = "photoluminescence"
        rev_eff = {v: k for k, v in PL_EFFICIENCY.items()}
        for key, (attr, _) in PL_ATTRS.items():
            v = getattr(pl, attr)
            out[key] = rev_eff.get(str(v), str(v)) if key == "efficiency" else (bool(v) if isinstance(v, bool) else
                                                                                 float(v) if isinstance(v, (int, float)) else str(v))
        return out
    impl = getattr(vp.ModelSettings, "__implementation__", vp.ModelSettings)
    if model == "DLLDefinedScattering":
        out["dll"] = str(impl.DLL)
        out["params"] = {str(impl.GetParameterName(i)): float(impl.GetParameterValue(i)) for i in range(int(impl.NumberOfParameters))}
    if model in ("DLLDefinedScattering", "AngleScattering"):
        out["mean_path"] = float(impl.MeanPath)
        if model == "AngleScattering" or bool(impl.AngleRequired):
            out["angle"] = float(impl.Angle)
    return out


def _scatter_settings(face):
    """面当前散射模型的 IOSS_<模型> 接口；模型为 None 时也返回（没有字段）。"""
    model = str(face.CurrentScatterModel)
    return model, getattr(face.CurrentScatterModelSettings, f"_S_{model}", None)


def read_face(face) -> dict[str, Any]:
    """一个面的属性：面号、名字、反射/吸收、膜层、散射模型及其字段、散射光线数；用了 profile 时另给 profile 名。"""
    rev_is = {v: k for k, v in FACE_IS.items()}
    rev_model = {v: k for k, v in SCATTER_MODELS.items()}
    out: dict[str, Any] = {"face": int(face.FaceNumber), "name": str(face.FaceName), "is": rev_is.get(str(face.FaceIs), str(face.FaceIs))}
    coating = str(face.Coating)
    if coating and coating.upper() != "NONE":
        out["coating"] = coating
    if bool(face.IsProfile):
        out["profile"] = str(face.ProfileName)
    model, impl = _scatter_settings(face)
    key = rev_model.get(model, model)
    scatter: dict[str, Any] = {"model": key}
    if impl is not None and key != "none":
        if key in SCATTER_NAME_ATTRS:
            reflect, transmit, _ = SCATTER_NAME_ATTRS[key]
            for k, attr in (("reflect", reflect), ("transmit", transmit)):
                v = str(getattr(impl, attr, "") or "")
                if v and v.upper() != "NONE":
                    scatter[k] = v
        for k, attr in SCATTER_ATTRS.items():
            if hasattr(impl, attr):
                v = getattr(impl, attr)
                if k in ("dll", "file"):
                    if v:
                        scatter[k] = str(v)
                else:
                    scatter[k] = float(v)
        if key == "user":
            scatter["params"] = {str(impl.GetParameterName(i)): float(impl.GetParameterValue(i)) for i in range(int(impl.NumberOfParameters))}
        scatter["rays"] = int(face.NumberOfRays)
    out["scatter"] = scatter
    return out


def read_faces(obj) -> list[dict[str, Any]] | None:
    cs = obj.CoatScatterData
    if not bool(cs.IsCoatScatterAvailable):
        return None
    return [read_face(cs.GetFaceData(k)) for k in range(int(cs.NumberOfFaces))]


def read_scatter_to(obj) -> dict[str, Any] | None:
    """Scatter To 页：列表 {"objects": [...]}、重要性采样 {"importance": [{towards, size, limit}]}；默认（空列表）为 None。"""
    st = obj.ScatterToData
    if not bool(st.IsScatterToAvailable):
        return None
    if str(st.ScatterToMethod) == "ImportanceSampling":
        rows = []
        for i in range(1, int(st.NumberOfRayData) + 1):
            r = st.GetRayData(i)
            if r is not None and int(r.Towards) > 0:
                rows.append({"row": i, "towards": int(r.Towards), "size": float(r.Size), "limit": float(r.Limit)})
        return {"importance": rows}
    listed = str(st.ScatterToList or "").strip()
    return {"objects": [int(x) for x in re.findall(r"\d+", listed)]} if listed else None


def _num(v: Any) -> Any:
    return bool(v) if isinstance(v, bool) else float(v) if isinstance(v, (int, float)) else str(v)


def read_source(obj) -> dict[str, Any] | None:
    """光源页：偏振、相位、相干长度、反向、预传播、采样、体散射次数、阵列和光源颜色（光谱）。"""
    sd = obj.SourcesData
    if not bool(sd.IsSourcesAvailable):
        return None
    out: dict[str, Any] = {"polarization": "random" if bool(sd.RandomPolarization) else
                           {"jx": float(sd.Jx), "jy": float(sd.Jy), "x_phase": float(sd.XPhase), "y_phase": float(sd.YPhase)}}
    out.update({k: _num(getattr(sd, a)) for k, a in SOURCE_ATTRS.items()})
    for key, (attr, names) in SOURCE_ENUMS.items():
        rev = {v: k for k, v in names.items()}
        out[key] = rev.get(str(getattr(sd, attr)), str(getattr(sd, attr)))
    array = {v: k for k, v in ARRAY_TYPES.items()}.get(str(sd.ArrayType), str(sd.ArrayType))
    if array != "none":
        out["array"] = {"type": array, **{k: _num(getattr(sd, a)) for k, a in ARRAY_ATTRS.items()}}
    mode = str(sd.SourceColor)
    color: dict[str, Any] = {"mode": {v: k for k, v in COLOR_MODES.items()}.get(mode, mode)}
    sub = getattr(sd.SourceColorSettings, f"_S_{mode}", None)
    if sub is not None:
        color.update({k: _num(getattr(sub, a)) for k, a in COLOR_ATTRS.items() if hasattr(sub, a)})
    out["color"] = color
    return out


def read_type_options(obj) -> dict[str, Any]:
    td = obj.TypeData
    out = {}
    for key, attr in TYPE_ATTRS.items():
        v = getattr(td, attr)
        if key == "rays_ignore":
            out[key] = {b: a for a, b in RAYS_IGNORE.items()}.get(str(v), str(v))
        elif key in ("consider", "ignore"):
            out[key] = [int(x) for x in re.findall(r"\d+", str(v or ""))]
        else:
            out[key] = bool(v)
    return out


def read_diffraction(obj) -> dict[str, Any] | None:
    dd = obj.DiffractionData
    if not bool(dd.IsDiffractionAvailable):
        return None
    split = {v: k for k, v in DIFFRACTION_SPLIT.items()}.get(str(dd.Split), str(dd.Split))
    out: dict[str, Any] = {"split": split, "start_order": int(dd.StartOrder), "stop_order": int(dd.StopOrder)}
    if split == "dll":
        out["dll"] = str(dd.DLL)
        n = int(dd.NumberOfParameters)
        # ZOS-API 的方法名就拼作 GetTransmitParamaterName / the ZOS-API method really is spelled GetTransmitParamaterName
        out["reflect"] = {str(dd.GetReflectParameterName(i)): float(dd.GetReflectParameterValue(i)) for i in range(n)}
        out["transmit"] = {str(dd.GetTransmitParamaterName(i)): float(dd.GetTransmitParameterValue(i)) for i in range(n)}
    return out


def read_index(obj) -> dict[str, Any] | None:
    idx = obj.IndexData
    if not bool(idx.IsIndexAvailable):
        return None
    kind = {v: k for k, v in INDEX_TYPES.items()}.get(str(idx.IndexType), str(idx.IndexType))
    out: dict[str, Any] = {"type": kind}
    sub = getattr(idx.ModelSettings, f"_S_{INDEX_TYPES.get(kind, kind)}", None)
    if kind == "birefringent" and sub is not None:
        out.update(axis=[float(sub.Ax), float(sub.Ay), float(sub.Az)], mode=str(sub.Mode), reflections=str(sub.Reflections))
    elif kind == "grin" and sub is not None:
        out.update(dll=str(sub.DLL), max_step=float(sub.MaximumStepSize),
                   params={str(sub.GetParameterName(i)): float(sub.GetParameterValue(i)) for i in range(int(sub.NumberOfParameters))})
        if bool(sub.UsesFile):
            out["file"] = str(sub.File)
    return out


def type_files(obj) -> dict[str, str]:
    """要文件的类型当前用的文件名（file、file2）。"""
    ts = obj.CurrentTypeSettings
    out = {}
    if ts is not None:
        for key, req, attr in (("file", "RequiresFile1", "FileName1"), ("file2", "RequiresFile2", "FileName2")):
            if bool(getattr(ts, req)):
                out[key] = str(getattr(ts, attr))
    return out


def indices(system, number: int, waves: int) -> list[float] | None:
    """物体材料在各波长的折射率（NCE.GetIndex）；读不出时为 None。"""
    import System  # pythonnet 在加载 ZOS-API 之后才有

    arr = System.Array.CreateInstance(System.Double, waves)
    try:
        if int(system.NCE.GetIndex(number, waves, arr)) == 0:
            return None
    except Exception:  # noqa: BLE001 - 光源、探测器没有材料 / sources and detectors have none
        return None
    return [float(v) for v in arr]


def matrix(system, number: int) -> tuple[list[float], list[float]] | None:
    """物体局部坐标到全局的旋转（行优先 3×3）和平移。"""
    m = system.NCE.GetMatrix(number, *([0.0] * 12))
    return ([float(v) for v in m[1:10]], [float(v) for v in m[10:13]]) if m[0] else None


def read_object(zosapi, system, number: int, waves: int) -> dict[str, Any]:
    o = system.NCE.GetObjectAt(number)
    type_name = str(o.Type)
    params = []
    for n, cell in param_cells(zosapi, o).items():
        kind = str(cell.DataType)
        value: Any = int(cell.IntegerValue) if kind == "Integer" else float(cell.DoubleValue) if kind == "Double" else str(cell.Value)
        params.append({"par": n, "name": str(cell.Header), "value": value})
    item: dict[str, Any] = {
        "index": number, "type": type_name, "type_label": str(o.TypeName), "kind": kind_of(type_name),
        "comment": str(o.Comment), "ref": int(o.RefObject), "inside_of": int(o.InsideOf),
        "position": [float(o.XPosition), float(o.YPosition), float(o.ZPosition)],
        "tilt": [float(o.TiltAboutX), float(o.TiltAboutY), float(o.TiltAboutZ)],
        "material": str(o.Material), "params": params,
    }
    item.update(type_files(o))
    if item["kind"] == "object":
        item["volume_physics"] = volume_physics(o)
        item["n"] = indices(system, number, waves)
    if item["kind"] != "source":
        item["faces"] = read_faces(o)
        scatter_to = read_scatter_to(o)
        if scatter_to:
            item["scatter_to"] = scatter_to
    else:
        item["source"] = read_source(o)
    item["type_options"] = read_type_options(o)
    for key, reader in (("diffraction", read_diffraction), ("index_model", read_index)):
        value = reader(o)
        # 默认（不分级次、各向同性）不列 / defaults (no order split, isotropic) are left out
        if value and value.get("split", value.get("type")) not in ("none", "isotropic"):
            item[key] = value
    tf = matrix(system, number)
    item["global"] = {"rotation": tf[0], "offset": tf[1]} if tf else None
    return item


def is_group(row) -> bool:
    """序列系统里的 NonSequential 面（非序列组）。/ A NonSequential surface (an NSC group) of a sequential lens."""
    return str(row.Type) == "NonSequential"


def groups(system) -> list[int]:
    """混合模式里各非序列组的面号；纯非序列系统为空。/ The surfaces holding NSC groups; empty for a pure NSC system."""
    if is_nsc(system):
        return []
    lde = system.LDE
    return [i for i in range(int(lde.NumberOfSurfaces)) if is_group(lde.GetSurfaceAt(i))]


def select_group(system, group: int | None) -> None:
    """选中一个非序列组；纯非序列系统不用选。/ Make an NSC group current; a pure NSC system has only one."""
    if group is None or is_nsc(system):
        return
    if group not in groups(system):
        raise SaosError(ErrorCode.BAD_ARGS, f"surface {group} is not a NonSequential surface", {"groups": groups(system)})
    if int(system.NCE.ActiveSurface) != group:
        system.NCE.SetActiveSurface(group)


def objects(zosapi, system, group: int | None = None) -> list[dict[str, Any]]:
    select_group(system, group)
    waves = int(system.SystemData.Wavelengths.NumberOfWavelengths)
    return [read_object(zosapi, system, i, waves) for i in range(1, int(system.NCE.NumberOfObjects) + 1)]


def settings(system) -> dict[str, Any]:
    nd = system.SystemData.NonSequentialData
    return {k: (bool(getattr(nd, a)) if k == "simple_ray_splitting" else int(getattr(nd, a)) if k.startswith("max_") else float(getattr(nd, a)))
            for k, a in NSC_SETTINGS.items()}


def model_material(name: str) -> dict[str, float] | None:
    """非序列编辑器里模型玻璃显示为 "nd,vd"（如 "1.39,65.0"）。"""
    m = MODEL_RE.match(name or "")
    return {"nd": float(m.group(1)), "vd": float(m.group(2))} if m else None


# ---------- 搭建 / building ----------

def apply_settings(system, values: dict[str, Any]) -> None:
    nd = system.SystemData.NonSequentialData
    for key, value in values.items():
        attr = NSC_SETTINGS[key]
        setattr(nd, attr, value if key == "simple_ray_splitting" else (int(value) if key.startswith("max_") else float(value)))
        actual = getattr(nd, attr)
        if (bool(actual) != value) if key == "simple_ray_splitting" else not math.isclose(float(actual), float(value), rel_tol=1e-9, abs_tol=1e-15):
            raise SaosError(ErrorCode.BAD_ARGS, f"nsc.{key} did not take {value!r} (now {actual!r})")


def set_volume_physics(zosapi, obj, spec: dict[str, Any], label: str) -> None:
    from saos.backend.zos import enum_member, pick_name

    vp = obj.VolumePhysicsData
    if not bool(vp.IsVolumePhysicsAvailable):
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}: {obj.TypeName} has no volume physics")
    vp.Model = enum_member(type(vp.Model), "None", VOLUME_MODELS[spec["model"]], f"{label}.model")
    if spec["model"] == "none":
        return
    if spec["model"] == "photoluminescence":
        pl = vp.ModelSettings._S_PhotoluminescenceModel
        for key, (attr, lister) in PL_ATTRS.items():
            if key not in spec:
                continue
            value = spec[key]
            if lister:
                value = pick_name([str(n) for n in getattr(pl, lister)()], value, f"{label}.{key}")
            elif key == "efficiency":
                value = enum_member(type(pl.EfficiencySpectrum), "QuantumYield", PL_EFFICIENCY[value], f"{label}.{key}")
            setattr(pl, attr, value)
        check_back(label, {k: v for k, v in spec.items() if k != "model"}, volume_physics(obj))
        return
    impl = getattr(vp.ModelSettings, "__implementation__", vp.ModelSettings)
    if spec["model"] == "dll":
        impl.DLL = pick_name([str(d) for d in impl.GetAvailableDLLs()], spec["dll"], f"{label}.dll")
        names = [str(impl.GetParameterName(i)) for i in range(int(impl.NumberOfParameters))]
        for name, value in spec.get("params", {}).items():
            k = next((i for i, n in enumerate(names) if n.strip().lower() == name.strip().lower()), None)
            if k is None:
                raise SaosError(ErrorCode.BAD_ARGS, f"{label}.params: {impl.DLL} has no parameter {name!r}", {"parameters": names})
            impl.SetParameterValue(k, float(value))
            if not math.isclose(float(impl.GetParameterValue(k)), float(value), rel_tol=1e-12, abs_tol=1e-15):
                raise SaosError(ErrorCode.BAD_ARGS, f"{label}.params.{name} did not take {value!r}")
    impl.MeanPath = float(spec["mean_path"])
    if "angle" in spec:
        impl.Angle = float(spec["angle"])
    got = volume_physics(obj)
    if not math.isclose(got.get("mean_path", math.nan), float(spec["mean_path"]), rel_tol=1e-12):
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}.mean_path did not take {spec['mean_path']!r} (now {got.get('mean_path')!r})")


def check_back(label: str, expect: Any, got: Any, path: str = "") -> None:
    """写入后回读核对：数值相对 1e-9，名字不分大小写，字典逐键、列表逐项；不一致时报 BAD_ARGS 并给出两边的值。"""
    problems: list[str] = []

    def walk(e: Any, g: Any, p: str) -> None:
        if isinstance(e, dict):
            for k, v in e.items():
                gv = g.get(k) if isinstance(g, dict) else None
                if gv is None and isinstance(g, dict):
                    # 名字类的键（DLL 参数名）不分大小写 / name-like keys (DLL parameters) are case-insensitive
                    gv = next((x for n, x in g.items() if str(n).strip().lower() == str(k).strip().lower()), None)
                walk(v, gv, f"{p}.{k}" if p else str(k))
        elif isinstance(e, list):
            if not isinstance(g, list) or len(g) != len(e):
                problems.append(p)
            else:
                for i, (a, b) in enumerate(zip(e, g)):
                    walk(a, b, f"{p}[{i}]")
        elif isinstance(e, bool) or e is None:
            if bool(g) != bool(e):
                problems.append(p)
        elif isinstance(e, (int, float)):
            if not isinstance(g, (int, float)) or not math.isclose(float(g), float(e), rel_tol=1e-9, abs_tol=1e-12):
                problems.append(p)
        elif str(g).strip().upper() != str(e).strip().upper():
            problems.append(p)

    walk(expect, got, path)
    if problems:
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}: OpticStudio did not take {', '.join(problems)}", {"requested": expect, "actual": got})


def set_source(zosapi, obj, spec: dict[str, Any], label: str) -> None:
    from saos.backend.zos import _named, enum_member, pick_name

    sd = obj.SourcesData
    if not bool(sd.IsSourcesAvailable):
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}: {obj.TypeName} has no Sources tab")
    with _named(label):
        pol = spec.get("polarization")
        if pol == "random":
            sd.RandomPolarization = True
        elif pol:
            sd.RandomPolarization = False
            for k, a in (("jx", "Jx"), ("jy", "Jy"), ("x_phase", "XPhase"), ("y_phase", "YPhase")):
                if k in pol:
                    setattr(sd, a, float(pol[k]))
        for k, a in SOURCE_ATTRS.items():
            if k in spec:
                setattr(sd, a, spec[k] if isinstance(spec[k], bool) else float(spec[k]))
        for k, (a, names) in SOURCE_ENUMS.items():
            if k in spec:
                setattr(sd, a, enum_member(type(getattr(sd, a)), names[next(iter(names))], names[spec[k]], f"{label}.{k}"))
        if "array" in spec:
            arr = spec["array"]
            sd.ArrayType = enum_member(type(sd.ArrayType), "None", ARRAY_TYPES[arr["type"]], f"{label}.array.type")
            for k, a in ARRAY_ATTRS.items():
                if k in arr:
                    setattr(sd, a, int(arr[k]) if isinstance(arr[k], int) else float(arr[k]))
        if "color" in spec:
            c = spec["color"]
            mode = COLOR_MODES[c["mode"]]
            sd.SourceColor = enum_member(type(sd.SourceColor), "SystemWavelengths", mode, f"{label}.color.mode")
            sub = getattr(sd.SourceColorSettings, f"_S_{mode}")
            allowed = [k for k, a in COLOR_ATTRS.items() if hasattr(sub, a)]
            for k, v in c.items():
                if k == "mode":
                    continue
                if k not in allowed:
                    raise SaosError(ErrorCode.BAD_ARGS, f"{label}.color: the {c['mode']} mode has no {k}", {"fields": allowed})
                if k == "file":
                    v = pick_name([str(n) for n in sub.GetAvailableSpectrumFiles()], v, f"{label}.color.file")
                setattr(sub, COLOR_ATTRS[k], v if isinstance(v, (str, int)) and not isinstance(v, bool) else float(v))
    check_back(label, spec, read_source(obj))


def set_type_options(zosapi, obj, spec: dict[str, Any], label: str) -> None:
    from saos.backend.zos import _named, enum_member

    td = obj.TypeData
    with _named(label):
        for k, v in spec.items():
            attr = TYPE_ATTRS[k]
            if k == "rays_ignore":
                v = enum_member(type(td.RaysIgnoreObject), "Never", RAYS_IGNORE[v], f"{label}.{k}")
            elif k in ("consider", "ignore"):
                v = " ".join(str(n) for n in v)
            setattr(td, attr, v)
    check_back(label, spec, read_type_options(obj))


def set_diffraction(zosapi, obj, spec: dict[str, Any], label: str) -> None:
    from saos.backend.zos import _named, enum_member, pick_name

    dd = obj.DiffractionData
    if not bool(dd.IsDiffractionAvailable):
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}: {obj.TypeName} has no Diffraction tab")
    with _named(label):
        dd.Split = enum_member(type(dd.Split), "DontSplitByOrder", DIFFRACTION_SPLIT[spec["split"]], f"{label}.split")
        if "dll" in spec:
            dd.DLL = pick_name([str(n) for n in dd.GetAvailableDLLs()], spec["dll"], f"{label}.dll")
        for k, a in (("start_order", "StartOrder"), ("stop_order", "StopOrder")):
            if k in spec:
                setattr(dd, a, int(spec[k]))
        n = int(dd.NumberOfParameters)
        for side, getname, setter in (("reflect", dd.GetReflectParameterName, dd.SetReflectParameterValue),
                                      ("transmit", dd.GetTransmitParamaterName, dd.SetTransmitParameterValue)):
            names = [str(getname(i)) for i in range(n)]
            for name, value in (spec.get(side) or {}).items():
                i = next((j for j, m in enumerate(names) if m.strip().lower() == name.strip().lower()), None)
                if i is None:
                    raise SaosError(ErrorCode.BAD_ARGS, f"{label}.{side}: {dd.DLL} has no parameter {name!r}", {"parameters": names})
                setter(i, float(value))
    check_back(label, spec, read_diffraction(obj))


def set_index(zosapi, obj, spec: dict[str, Any], label: str) -> None:
    from saos.backend.zos import _named, enum_member, pick_name

    idx = obj.IndexData
    if not bool(idx.IsIndexAvailable):
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}: {obj.TypeName} has no Index tab")
    kind = INDEX_TYPES[spec["type"]]
    with _named(label):
        idx.IndexType = enum_member(type(idx.IndexType), "Isotropic", kind, f"{label}.type")
        sub = getattr(idx.ModelSettings, f"_S_{kind}", None)
        if spec["type"] == "birefringent":
            sub.Ax, sub.Ay, sub.Az = (float(v) for v in spec["axis"])
            for k, a in (("mode", "Mode"), ("reflections", "Reflections")):
                if k in spec:
                    current = getattr(sub, a)
                    setattr(sub, a, enum_member(type(current), str(current), spec[k], f"{label}.{k}"))
        elif spec["type"] == "grin":
            sub.DLL = pick_name([str(n) for n in sub.GetAvailableDLLs()], spec["dll"], f"{label}.dll")
            if "file" in spec:
                sub.File = pick_name([str(n) for n in sub.GetAvailableFiles()], spec["file"], f"{label}.file")
            if "max_step" in spec:
                sub.MaximumStepSize = float(spec["max_step"])
            names = [str(sub.GetParameterName(i)) for i in range(int(sub.NumberOfParameters))]
            for name, value in spec["params"].items():
                i = next((j for j, m in enumerate(names) if m.strip().lower() == name.strip().lower()), None)
                if i is None:
                    raise SaosError(ErrorCode.BAD_ARGS, f"{label}.params: {sub.DLL} has no parameter {name!r}", {"parameters": names})
                sub.SetParameterValue(i, float(value))
    check_back(label, spec, read_index(obj))


def apply_units(zosapi, system, p: dict[str, Any]) -> None:
    """光源功率单位和分析单位（处方的 source_units、analysis_units）。"""
    from saos.backend.zos import enum_member

    u = system.SystemData.Units
    if "source_units" in p:
        u.SourceUnits = enum_member(type(u.SourceUnits), "Watts", SOURCE_UNITS[p["source_units"]], "source_units")
    if "analysis_units" in p:
        u.AnalysisUnits = enum_member(type(u.AnalysisUnits), "WattsPerCMSq", ANALYSIS_UNITS[p["analysis_units"]], "analysis_units")
    got = units(system)
    for k in ("source_units", "analysis_units"):
        if k in p and got[k] != p[k]:
            raise SaosError(ErrorCode.BAD_ARGS, f"{k} did not take {p[k]!r} (now {got[k]!r})")


def units(system) -> dict[str, str]:
    u = system.SystemData.Units
    return {"source_units": {v: k for k, v in SOURCE_UNITS.items()}.get(str(u.SourceUnits), str(u.SourceUnits)),
            "analysis_units": {v: k for k, v in ANALYSIS_UNITS.items()}.get(str(u.AnalysisUnits), str(u.AnalysisUnits))}


def _type_settings(o, target, spec: dict[str, Any], label: str):
    """类型设置；要文件的类型按名字从 OpticStudio 列出的文件里选，缺文件或多给文件时报错并列出可选文件。"""
    from saos.backend.zos import pick_name

    ts = o.GetObjectTypeSettings(target)
    for key, req, attr, lister in (("file", "RequiresFile1", "FileName1", "GetFileNames1"), ("file2", "RequiresFile2", "FileName2", "GetFileNames2")):
        needed = bool(getattr(ts, req))
        if not needed and key in spec:
            raise SaosError(ErrorCode.BAD_ARGS, f"{label}: {spec['type']} takes no {key}")
        if needed:
            names = [str(n) for n in (getattr(ts, lister)() or [])]
            if key not in spec:
                raise SaosError(ErrorCode.BAD_ARGS, f"{label}: {spec['type']} needs {key}, one of the files OpticStudio lists "
                                                    "(put your own file in the matching folder under Documents/Zemax/Objects first)",
                                {"choices": names})
            setattr(ts, attr, pick_name(names, spec[key], f"{label}.{key}"))
    return ts


def set_face(zosapi, face, spec: dict[str, Any], label: str) -> None:
    """写一个面的属性并回读核对。profile 整面套用；否则依次写反射/吸收、膜层、散射模型及字段、散射光线数。"""
    from saos.backend.zos import _named, enum_member, pick_name

    with _named(label):
        if "profile" in spec:
            face.UseProfile(pick_name([str(n) for n in face.GetAvailableProfiles()], spec["profile"], f"{label}.profile"))
        else:
            if bool(face.IsProfile):
                face.UseCustomSettings()
            if "is" in spec:
                face.FaceIs = enum_member(type(face.FaceIs), "ObjectDefault", FACE_IS[spec["is"]], f"{label}.is")
            if "coating" in spec:
                face.Coating = pick_name([str(n) for n in face.GetAvailableCoatings()], spec["coating"], f"{label}.coating")
            sc = spec.get("scatter")
            if sc is not None:
                model = SCATTER_MODELS[sc["model"]]
                settings = face.CreateScatterModelSettings(enum_member(zosapi.Editors.NCE.ObjectScatteringTypes, "None", model, f"{label}.scatter.model"))
                impl = getattr(settings, f"_S_{model}")
                if sc["model"] in SCATTER_NAME_ATTRS:
                    reflect, transmit, lister = SCATTER_NAME_ATTRS[sc["model"]]
                    names = [str(n) for n in getattr(impl, lister)()]
                    for k, attr in (("reflect", reflect), ("transmit", transmit)):
                        if k in sc:
                            setattr(impl, attr, pick_name(names, sc[k], f"{label}.scatter.{k}"))
                for k, attr in SCATTER_ATTRS.items():
                    if k not in sc:
                        continue
                    if k in SCATTER_LISTS:
                        setattr(impl, attr, pick_name([str(n) for n in getattr(impl, SCATTER_LISTS[k])()], sc[k], f"{label}.scatter.{k}"))
                    else:
                        setattr(impl, attr, float(sc[k]))
                if sc.get("params"):
                    names = [str(impl.GetParameterName(i)) for i in range(int(impl.NumberOfParameters))]
                    for name, value in sc["params"].items():
                        k = next((i for i, n in enumerate(names) if n.strip().lower() == name.strip().lower()), None)
                        if k is None:
                            raise SaosError(ErrorCode.BAD_ARGS, f"{label}.scatter.params: {impl.DLLName} has no parameter {name!r}", {"parameters": names})
                        impl.SetParameterValue(k, float(value))
                face.ChangeScatterModelSettings(settings)
                if "rays" in sc:
                    face.NumberOfRays = int(sc["rays"])
    got = read_face(face)
    expect = {k: spec[k] for k in ("is", "profile") if k in spec}
    if "coating" in spec:
        expect["coating"] = spec["coating"]
    problems = [k for k, v in expect.items() if str(got.get(k, "NONE" if k == "coating" else "")).upper() != str(v).upper()]
    sc = spec.get("scatter")
    if sc is not None:
        gs = got["scatter"]
        for k, v in sc.items():
            if k == "params":
                bad = [n for n, x in v.items() if not any(n.lower() == m.lower() and math.isclose(y, x, rel_tol=1e-9, abs_tol=1e-15) for m, y in gs.get("params", {}).items())]
                problems += [f"scatter.params.{n}" for n in bad]
            elif isinstance(v, str) and k != "model":
                if str(gs.get(k, "")).upper() != v.upper():
                    problems.append(f"scatter.{k}")
            elif k == "model" and gs.get("model") != v:
                problems.append("scatter.model")
            elif isinstance(v, (int, float)) and not math.isclose(float(gs.get(k, math.nan)), float(v), rel_tol=1e-9, abs_tol=1e-15):
                problems.append(f"scatter.{k}")
    if problems:
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}: OpticStudio did not take {', '.join(problems)}", {"requested": spec, "actual": got})


def set_faces(zosapi, obj, faces: dict[str, dict[str, Any]], label: str) -> None:
    """按面号写各面属性；"all" 先写到每个面，再由面号覆盖。面号越界时列出该物体的面。"""
    cs = obj.CoatScatterData
    if not bool(cs.IsCoatScatterAvailable):
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}: {obj.TypeName} has no face properties")
    count = int(cs.NumberOfFaces)
    names = [str(cs.GetFaceData(k).FaceName) for k in range(count)]
    for key, spec in faces.items():
        targets = range(count) if key == "all" else [int(key)]
        if key != "all" and int(key) >= count:
            raise SaosError(ErrorCode.BAD_ARGS, f"{label}: {obj.TypeName} has faces 0–{count - 1}, not {key}", {"faces": names})
        for k in targets:
            set_face(zosapi, cs.GetFaceData(k), spec, f"{label}.{key}" if key != "all" else f"{label}.all (face {k})")


def set_scatter_to(obj, spec: dict[str, Any], label: str) -> None:
    from saos.backend.zos import _named, enum_member

    st = obj.ScatterToData
    if not bool(st.IsScatterToAvailable):
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}: {obj.TypeName} has no Scatter To settings")
    with _named(label):
        if "objects" in spec:
            st.ScatterToMethod = enum_member(type(st.ScatterToMethod), "ScatterToList", "ScatterToList", label)
            # 2024 R1 实测：逗号分隔的 "2, 3" 只留下 3，所以用空格分隔并回读 / "2, 3" kept only 3, so separate by spaces and read back
            st.ScatterToList = " ".join(str(n) for n in spec["objects"])
        else:
            st.ScatterToMethod = enum_member(type(st.ScatterToMethod), "ScatterToList", "ImportanceSampling", label)
            for i in range(1, int(st.NumberOfRayData) + 1):
                row = st.GetRayData(i)
                r = spec["importance"][i - 1] if i <= len(spec["importance"]) else {"towards": 0, "size": 0.0, "limit": 1.0}
                row.Towards, row.Size, row.Limit = int(r["towards"]), float(r["size"]), float(r["limit"])
                st.SetRayData(i, row)
    got = read_scatter_to(obj) or {}
    if "objects" in spec:
        ok = sorted(got.get("objects", [])) == spec["objects"]
    else:
        rows = got.get("importance", [])
        ok = len(rows) == len(spec["importance"]) and all(
            a["towards"] == b["towards"] and math.isclose(a["size"], b["size"], rel_tol=1e-9) and math.isclose(a["limit"], b["limit"], rel_tol=1e-9, abs_tol=1e-15)
            for a, b in zip(rows, spec["importance"]))
    if not ok:
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}: OpticStudio did not take {spec}", {"actual": got})


def build_object(backend, zosapi, system, number: int, spec: dict[str, Any], prefix: str = "") -> None:
    from saos.backend.zos import _named, enum_member, set_cell

    nce = system.NCE
    label = f"{prefix}objects[{number}]"
    o = nce.GetObjectAt(number)
    with _named(f"{label}.type"):
        target = enum_member(zosapi.Editors.NCE.ObjectType, "CylinderVolume", spec["type"], f"{label}.type")
        o.ChangeType(_type_settings(o, target, spec, label))
    if str(o.Type) != str(target):
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}.type did not take {spec['type']!r} (now {o.Type})")
    files = type_files(o)
    for key in ("file", "file2"):
        if key in spec and files.get(key, "").upper() != spec[key].upper():
            raise SaosError(ErrorCode.BAD_ARGS, f"{label}.{key} did not take {spec[key]!r} (now {files.get(key)!r})")
    if not files:
        # 要文件的类型注释栏就是文件名，不能写 / the comment column of a file-based type is its file name
        o.Comment = spec["comment"]
    for attr, key in (("RefObject", "ref"), ("InsideOf", "inside_of")):
        with _named(f"{label}.{key}"):
            setattr(o, attr, spec[key])
        if int(getattr(o, attr)) != spec[key]:
            raise SaosError(ErrorCode.BAD_ARGS, f"{label}.{key} did not take {spec[key]} (now {getattr(o, attr)})")
    for attrs, key in ((("XPosition", "YPosition", "ZPosition"), "position"), (("TiltAboutX", "TiltAboutY", "TiltAboutZ"), "tilt")):
        for attr, value in zip(attrs, spec[key]):
            setattr(o, attr, value)
            if not math.isclose(float(getattr(o, attr)), value, rel_tol=1e-12, abs_tol=1e-15):
                raise SaosError(ErrorCode.BAD_ARGS, f"{label}.{key} did not take {spec[key]}")
    if "material" in spec:
        material = spec["material"]
        if isinstance(material, dict):
            model = material["model"]
            with _named(f"{label}.material"):
                solve = o.MaterialCell.CreateSolveType(zosapi.Editors.SolveType.MaterialModel)
                data = solve._S_MaterialModel
                data.IndexNd, data.AbbeVd, data.dPgF = model["nd"], model["vd"], model["dpgf"]
                o.MaterialCell.SetSolveData(solve)
        else:
            name = material if material in ("", "ABSORB") else backend._check_material(material)
            o.Material = name
            if str(o.Material).upper() != name:
                raise SaosError(ErrorCode.BAD_ARGS, f"{label}.material did not take {name!r} (now {o.Material!r})")
    cells = None
    for key, value in spec.get("params", {}).items():
        cell = param_cell(zosapi, o, label, key, cells)
        with _named(f"{label}.params.{key}"):
            set_cell(cell, value, f"{label}.params.{key}")
    if spec.get("volume_physics") is not None:
        set_volume_physics(zosapi, o, spec["volume_physics"], f"{label}.volume_physics")
    # 面属性放在参数之后：CAD 等物体的面数随参数和文件变
    # faces after the parameters: the face count of CAD and similar objects follows the parameters and file
    if spec.get("faces"):
        set_faces(zosapi, o, spec["faces"], f"{label}.faces")
    if spec.get("scatter_to"):
        set_scatter_to(o, spec["scatter_to"], f"{label}.scatter_to")
    for key, setter in (("source", set_source), ("type_options", set_type_options), ("diffraction", set_diffraction), ("index_model", set_index)):
        if spec.get(key):
            setter(zosapi, o, spec[key], f"{label}.{key}")


def check_model_index(system, number: int, spec: dict[str, Any], primary_um: float, prefix: str = "") -> None:
    """模型玻璃写进去后，用 GetIndex 核对主波长的折射率与 nd 相近（差不到 0.05；在 0.4–0.8 µm 内色散不会更大），
    防止写成一个 OpticStudio 不认的字符串（实测直接写 "1.39,65" 读回 1.0）。"""
    material = spec.get("material")
    if not isinstance(material, dict):
        return
    n = indices(system, number, int(system.SystemData.Wavelengths.NumberOfWavelengths))
    nd = material["model"]["nd"]
    if n is None or (0.4 <= primary_um <= 0.8 and abs(n[0] - nd) > 0.05) or n[0] <= 1.0:
        raise SaosError(ErrorCode.BAD_ARGS, f"{prefix}objects[{number}].material: the model glass nd {nd} reads back as index {n}")


# ---------- 轮廓 / outlines ----------

def _circle(r: float, z: float, n: int = 32, ry: float | None = None) -> list[list[float]]:
    ry = r if ry is None else ry
    return [[r * math.cos(2 * math.pi * k / n), ry * math.sin(2 * math.pi * k / n), z] for k in range(n + 1)]


def _rect(hx: float, hy: float, z: float) -> list[list[float]]:
    return [[-hx, -hy, z], [hx, -hy, z], [hx, hy, z], [-hx, hy, z], [-hx, -hy, z]]


def outline(item: dict[str, Any]) -> dict[str, Any] | None:
    """常见物体在局部坐标里的轮廓线：edges 给 3D，section 是局部 YZ 面（x = 0）上的截线。不认识的类型返回 None（列在表里，不画）。
    Outlines of common objects in local coordinates: edges for 3D, section on the local YZ plane. None for other types."""
    p = {str(q["name"]).strip().lower(): q["value"] for q in item["params"] if isinstance(q["value"], (int, float))}
    t = item["type"]
    if t in ("CylinderVolume", "CylinderPipe", "Cylinder2Volume", "Cylinder2Pipe"):
        rf, length, rb = p.get("front r", 0.0), p.get("z length", 0.0), p.get("back r", p.get("front r", 0.0))
        edges = [_circle(rf, 0.0), _circle(rb, length)] + [[[rf * c, rf * s, 0.0], [rb * c, rb * s, length]] for c, s in ((1, 0), (0, 1), (-1, 0), (0, -1))]
        section = [[[0.0, -rf, 0.0], [0.0, rf, 0.0], [0.0, rb, length], [0.0, -rb, length], [0.0, -rf, 0.0]]]
        return {"edges": edges, "section": section, "closed": True}
    if t in ("RectangularVolume", "DetectorVolume"):
        if t == "DetectorVolume":
            hx, hy, hz = p.get("x half width", 0.0), p.get("y half width", 0.0), p.get("z half width", 0.0)
            z0, z1, hx2, hy2 = -hz, hz, hx, hy
        else:
            hx, hy, z1 = p.get("x1 half width", 0.0), p.get("y1 half width", 0.0), p.get("z length", 0.0)
            hx2, hy2, z0 = p.get("x2 half width", hx), p.get("y2 half width", hy), 0.0
        front, back = _rect(hx, hy, z0), _rect(hx2, hy2, z1)
        edges = [front, back] + [[a, b] for a, b in zip(front[:4], back[:4])]
        section = [[[0.0, -hy, z0], [0.0, hy, z0], [0.0, hy2, z1], [0.0, -hy2, z1], [0.0, -hy, z0]]]
        return {"edges": edges, "section": section, "closed": True}
    if t in ("SourceVolumeCylindrical", "SourceVolumeRectangular", "SourceVolumeElliptical"):
        hx, hy, hz = p.get("x half width", 0.0), p.get("y half width", 0.0), p.get("z half width", 0.0)
        if t == "SourceVolumeRectangular":
            front, back = _rect(hx, hy, -hz), _rect(hx, hy, hz)
            edges = [front, back] + [[a, b] for a, b in zip(front[:4], back[:4])]
        else:
            edges = [_circle(hx, -hz, ry=hy), _circle(hx, hz, ry=hy)] + [[[hx * c, hy * s, -hz], [hx * c, hy * s, hz]] for c, s in ((1, 0), (0, 1), (-1, 0), (0, -1))]
        section = [[[0.0, -hy, -hz], [0.0, hy, -hz], [0.0, hy, hz], [0.0, -hy, hz], [0.0, -hy, -hz]]]
        return {"edges": edges, "section": section, "closed": True}
    if t in ("DetectorRectangle", "SourceRectangle", "Rectangle"):
        hx, hy = p.get("x half width", 0.0), p.get("y half width", 0.0)
        return {"edges": [_rect(hx, hy, 0.0)], "section": [[[0.0, -hy, 0.0], [0.0, hy, 0.0]]], "closed": False}
    if t in ("SourcePoint", "SourceTwoAngle", "SourceGaussian", "SourceDiode", "SourceEllipse"):
        hx, hy = p.get("x half width", 0.0), p.get("y half width", 0.0)
        if hx or hy:
            return {"edges": [_rect(hx, hy, 0.0)], "section": [[[0.0, -hy, 0.0], [0.0, hy, 0.0]]], "closed": False}
        return {"edges": [], "section": [], "point": [0.0, 0.0, 0.0], "closed": False}
    return None


def to_global(points: list[list[float]], tf: dict[str, Any]) -> list[list[float]]:
    rot, off = tf["rotation"], tf["offset"]
    return [[rot[0] * x + rot[1] * y + rot[2] * z + off[0], rot[3] * x + rot[4] * y + rot[5] * z + off[1],
             rot[6] * x + rot[7] * y + rot[8] * z + off[2]] for x, y, z in points]


def drawing(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """界面用的物体轮廓（全局坐标）。/ Object outlines for the window, in global coordinates."""
    out = []
    for item in items:
        shape = outline(item)
        entry = {"index": item["index"], "type": item["type"], "type_label": item["type_label"], "kind": item["kind"],
                 "comment": item["comment"], "material": item["material"]}
        if shape is not None and item.get("global"):
            entry["edges"] = [to_global(e, item["global"]) for e in shape["edges"]]
            entry["section"] = [to_global(e, item["global"]) for e in shape["section"]]
            entry["closed"] = shape["closed"]
            if "point" in shape:
                entry["point"] = to_global([shape["point"]], item["global"])[0]
        elif item.get("mesh_edges"):
            # 网格轮廓已在系统坐标里 / mesh outlines are already in system coordinates
            entry.update(edges=item["mesh_edges"], section=item["mesh_section"], closed=True, mesh=True)
        else:
            entry["undrawn"] = True
        out.append(entry)
    return out


# ---------- 追迹与探测器 / tracing and detectors ----------

def trace(system, *, split: bool = True, scatter: bool = True, polarization: bool = True, ignore_errors: bool = True,
          seed: int | None = None, cores: int | None = None, clear: bool = True, save_rays: str | None = None,
          zrd_format: str = "CompressedFullData", ray_filter: str = "", max_seconds: float | None = None,
          on_progress: Callable[[float], None] | None = None, should_cancel: Callable[[], bool] | None = None) -> dict[str, Any]:
    """非序列追迹（NSCRayTrace）。返回耗时、是否完成、停止原因，以及 OpticStudio 报的 DeadRayErrors、DeadRayThreshold、总能量。
    save_rays 是 ZRD 文件名（写在镜头文件旁边）。
    A non-sequential trace; returns the time, whether it finished, why it stopped, and OpticStudio's DeadRayErrors,
    DeadRayThreshold and total energy. save_rays is a ZRD file name, written next to the lens file."""
    tool = system.Tools.OpenNSCRayTrace()
    if tool is None:
        raise SaosError(ErrorCode.ZOS_ERROR, "cannot open the NSC ray trace tool (another tool is open, or the system is not non-sequential)")
    stopped_by = None
    try:
        tool.SplitNSCRays, tool.ScatterNSCRays, tool.UsePolarization, tool.IgnoreErrors = split, scatter, polarization, ignore_errors
        if seed is not None:
            tool.SetRandomSeed(int(seed))
        if cores:
            tool.NumberOfCores = int(cores)
        if save_rays:
            tool.SaveRays = True
            tool.SaveRaysFile = save_rays
            tool.ZRDFormat = getattr(type(tool.ZRDFormat), zrd_format)
            tool.Filter = ray_filter or ""
        if clear:
            tool.ClearDetectors(0)
        started = time.monotonic()
        tool.Run()
        last = -1.0
        while bool(tool.IsRunning):
            time.sleep(0.25)
            progress = float(tool.Progress)
            if on_progress is not None and progress != last:
                on_progress(progress)
                last = progress
            if should_cancel is not None and should_cancel():
                stopped_by = "cancel"
            elif max_seconds is not None and time.monotonic() - started > max_seconds:
                stopped_by = "max_seconds"
            if stopped_by:
                tool.Cancel()
                tool.WaitForCompletion()
                break
        return {"seconds": round(time.monotonic() - started, 2), "succeeded": bool(tool.Succeeded) and stopped_by is None,
                "stopped_by": stopped_by, "error": str(tool.ErrorMessage) if tool.ErrorMessage else None,
                "dead_ray_errors": float(tool.DeadRayErrors), "dead_ray_threshold": float(tool.DeadRayThreshold),
                "total_energy": float(tool.GetTotalRayEnergy()), "cores": int(tool.NumberOfCores),
                "options": {"split": split, "scatter": scatter, "polarization": polarization, "ignore_errors": ignore_errors, "seed": seed}}
    finally:
        tool.Close()


def ray_hits(zosapi, system, zrd: str, objects: list[int], ray_filter: str = "", max_rays: int | None = None) -> dict[str, list]:
    """从光线数据库（ZRD）读出光线到达指定物体的各段：段终点的位置、方向余弦、强度、波长序号、物体号和光线号。
    每段都要一次 pythonnet 调用（实测约 37 µs/段），强散射时一条光线有上千段，读之前用 ray_filter（如 "H9"）缩小范围。
    段的布局（2024 R1 实测）：(成功, 层级, 父段, 击中物体, 击中面, 所在物体, 状态, x, y, z, l, m, n, Ex/Ey/Ez 实虚部 6 个, 强度, 路程, …)。
    The segments where rays reach the given objects, read from a ZRD file: end point, direction cosines, intensity,
    wavelength index, object and ray number. Each segment is one pythonnet call (about 37 µs), so narrow it with ray_filter."""
    reader = system.Tools.OpenRayDatabaseReader()
    if reader is None:
        raise SaosError(ErrorCode.ZOS_ERROR, "cannot open the ray database reader (another tool is open)")
    wanted = set(objects)
    out: dict[str, list] = {k: [] for k in ("x", "y", "z", "l", "m", "n", "intensity", "wave", "object", "ray")}
    try:
        reader.ZRDFile = zrd
        reader.Filter = ray_filter or ""
        reader.RunAndWaitForCompletion()
        if not bool(reader.Succeeded):
            raise SaosError(ErrorCode.ZOS_ERROR, f"cannot read {zrd}: {reader.ErrorMessage}")
        res = reader.GetResults()
        status = zosapi.Tools.RayTrace.RayStatus(0)
        rays = 0
        while max_rays is None or rays < max_rays:
            r = res.ReadNextResult(0, 0, 0.0, 0)
            if not r[0]:
                break
            rays += 1
            for _ in range(int(r[4])):
                g = res.ReadNextSegmentFull(*([0] * 5), status, *([0.0] * 14), 0, 0, *([0.0] * 7))
                if int(g[3]) in wanted:
                    for k, v in zip(("x", "y", "z", "l", "m", "n"), g[7:13]):
                        out[k].append(float(v))
                    out["intensity"].append(float(g[19]))
                    out["wave"].append(int(r[2]))
                    out["object"].append(int(g[3]))
                    out["ray"].append(int(r[1]))
        out["rays_read"] = [rays]
        return out
    finally:
        reader.Close()


def _param(zosapi, obj, header: str) -> float:
    from saos.backend.zos import cell_value

    return cell_value(param_cell(zosapi, obj, f"object {obj.ObjectNumber}", header))


def _grid(arr) -> list[list[float]]:
    return [[float(arr[i, j]) for j in range(arr.GetLength(1))] for i in range(arr.GetLength(0))]


def detector(zosapi, system, number: int, grid: bool = False) -> dict[str, Any]:
    """探测器读数（上一次追迹留下的），按类型（2024 R1 实测）：
    - 矩形、曲面探测器：总功率、命中数、最大像素功率取 NSDD（像素 0、-3、-1）；grid 给逐像素功率（数据 0）和照度（数据 1）。
    - 极坐标探测器：NSDD 读出 0，总功率是 GetAllPolarDetectorDataSafe 功率格的和；grid 给功率和单位立体角功率（W/sr），
      行是方位角、列是径向（极角）像素。
    - 彩色探测器：NSDD 读出 0，总功率是 GetAllColorDetectorDataSafe（数据 1）的和；grid 给逐像素功率，按 # X Pixels 一行排开。
    - 体探测器：NSDD 像素 0 是各体素之和，光线穿过几层就记几次（1 W 光源穿过 2 层读出 2 W）；grid 给 [z][y][x] 体素。
    Readings left by the last trace, per detector type (measured in 2024 R1): NSDD for rectangle and surface detectors;
    polar and color detectors read 0 through NSDD, so their totals sum their own data; a volume detector's NSDD total
    counts a ray once per layer it crosses."""
    nce = system.NCE
    if not 1 <= number <= int(nce.NumberOfObjects):
        raise SaosError(ErrorCode.BAD_ARGS, f"object {number} out of range 1–{nce.NumberOfObjects}")
    o = nce.GetObjectAt(number)
    kind = str(o.Type)
    if kind_of(kind) != "detector":
        raise SaosError(ErrorCode.BAD_ARGS, f"object {number} is a {o.TypeName}, not a detector")
    nsdd = zosapi.Editors.MFE.MeritOperandType.NSDD
    value = lambda pix: float(system.MFE.GetOperandValue(nsdd, 1, number, pix, 0, 0, 0, 0, 0))  # noqa: E731
    ok, nx, ny = nce.GetDetectorDimensions(number, 0, 0)
    out: dict[str, Any] = {"object": number, "comment": str(o.Comment), "type": kind}
    if kind == "DetectorPolar":
        pt = zosapi.Editors.NCE.PolarDetectorDataType
        power = nce.GetAllPolarDetectorDataSafe(number, pt.Power)
        cells = _grid(power)
        out.update(power=sum(map(sum, cells)), hits=None, peak_pixel_power=max(map(max, cells)), pixels=[int(nx), int(ny)],
                   note="rows are azimuth pixels, columns radial (polar-angle) pixels")
        if grid:
            out["power_grid"] = cells
            out["intensity_grid"] = _grid(nce.GetAllPolarDetectorDataSafe(number, pt.PowerSolidAngle))
        return out
    if kind == "DetectorColor":
        flat = nce.GetAllColorDetectorDataSafe(number, False, 1)
        values = [float(flat[i, 0]) for i in range(flat.GetLength(0))]
        px, py = int(_param(zosapi, o, "# X Pixels")), int(_param(zosapi, o, "# Y Pixels"))
        out.update(power=sum(values), hits=None, peak_pixel_power=max(values) if values else 0.0, pixels=[px, py])
        if grid:
            out["power_grid"] = [values[r * px:(r + 1) * px] for r in range(py)]
        return out
    out.update(power=value(0), hits=int(value(-3)), peak_pixel_power=value(-1), pixels=[int(nx), int(ny)] if ok else None)
    if kind == "DetectorVolume":
        vx, vy, vz = (int(_param(zosapi, o, h)) for h in ("# X Pixels", "# Y Pixels", "# Z Pixels"))
        flat = nce.GetAllDetectorDataSafe(number, 0)
        values = [float(flat[i, 0]) for i in range(flat.GetLength(0))]
        # NSDD 对体探测器的命中数和最大像素读出 0（实测），最大体素取数据 / NSDD reads 0 hits and peak for a volume detector
        out.update(pixels=[vx, vy, vz], hits=None, peak_pixel_power=max(values) if values else 0.0,
                   note="the total counts a ray once per voxel layer it crosses")
        if grid:
            # 体素序号 = ix + nx·iy + nx·ny·iz（实测）/ voxel index = ix + nx·iy + nx·ny·iz (measured)
            out["power_grid"] = [[values[vx * vy * z + vx * y:vx * vy * z + vx * (y + 1)] for y in range(vy)] for z in range(vz)]
        return out
    if grid:
        out["power_grid"] = _grid(nce.GetAllDetectorDataSafe(number, 0))
        out["irradiance_grid"] = _grid(nce.GetAllDetectorDataSafe(number, 1))
    return out


# ---------- 三角网格（ExportCAD 导出 STL）/ meshes exported as STL ----------

# 2024 R1 实测：GetFacetedObjectData 对 Standard Lens 和 STL 物体都返回 None；ExportCAD 能把单个物体导出成 STL
# measured in 2024 R1: GetFacetedObjectData returns None even for STL objects; ExportCAD exports a single object to STL
_MESH_CACHE: dict[str, Any] = {}
MESH_CACHE_SIZE = 64
MAX_EDGES = 3000


def parse_stl(data: bytes) -> list[tuple[tuple[float, float, float], ...]]:
    """ASCII 或二进制 STL 的三角形。/ The triangles of an ASCII or binary STL file."""
    import struct

    if len(data) >= 84:
        count = struct.unpack_from("<I", data, 80)[0]
        if 84 + 50 * count == len(data):
            tris = []
            for k in range(count):
                v = struct.unpack_from("<12f", data, 84 + 50 * k)
                tris.append((tuple(v[3:6]), tuple(v[6:9]), tuple(v[9:12])))
            return tris
    pts = [tuple(float(x) for x in line.split()[1:4]) for line in data.decode("latin1").splitlines() if line.strip().startswith("vertex")]
    return [tuple(pts[i:i + 3]) for i in range(0, len(pts) - 2, 3)]


def export_mesh(system, number: int, folder) -> list | None:
    """把一个物体导出成 STL 并读出三角形（系统坐标）；导出失败时 None。/ Export one object to STL and read its triangles."""
    from pathlib import Path

    tool = system.Tools.OpenExportCAD()
    if tool is None:
        return None
    path = Path(folder) / f"object{number}.stl"
    try:
        tool.FileType = getattr(type(tool.FileType), "STL")
        tool.FirstObject, tool.LastObject = number, number
        tool.OutputFileName = str(path)
        tool.RunAndWaitForCompletion()
        ok = bool(tool.Succeeded)
    finally:
        tool.Close()
    if not ok or not path.is_file():
        return None
    try:
        return parse_stl(path.read_bytes())
    finally:
        path.unlink(missing_ok=True)


def mesh_edges(tris: list, angle_deg: float = 20.0) -> list[list[list[float]]]:
    """轮廓线：只在一个三角形上的边和两侧法线夹角大于 angle_deg 的边（镜片的边缘、柱面的棱）；最多 MAX_EDGES 条。
    Outline edges: boundary edges and edges whose two facets bend by more than angle_deg; at most MAX_EDGES."""
    def key(p):
        return tuple(round(c, 6) for c in p)

    def normal(t):
        a, b, c = t
        u = [b[i] - a[i] for i in range(3)]
        v = [c[i] - a[i] for i in range(3)]
        n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]]
        s = math.sqrt(sum(x * x for x in n)) or 1.0
        return [x / s for x in n]

    faces: dict[tuple, list] = {}
    for t in tris:
        n = normal(t)
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            k = tuple(sorted((key(a), key(b))))
            faces.setdefault(k, []).append(n)
    cos_limit = math.cos(math.radians(angle_deg))
    out = [[list(a), list(b)] for (a, b), ns in faces.items()
           if len(ns) == 1 or (len(ns) == 2 and sum(x * y for x, y in zip(*ns)) < cos_limit)]
    step = max(1, len(out) // MAX_EDGES)
    return out[::step]


def mesh_section(tris: list) -> list[list[list[float]]]:
    """网格与 x = 0 平面的截线（系统 YZ 面）。/ The mesh's section with the plane x = 0."""
    out = []
    for t in tris:
        pts = []
        for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])):
            if (a[0] > 0) != (b[0] > 0) and a[0] != b[0]:
                s = a[0] / (a[0] - b[0])
                pts.append([0.0, a[1] + s * (b[1] - a[1]), a[2] + s * (b[2] - a[2])])
        if len(pts) == 2:
            out.append(pts)
    step = max(1, len(out) // MAX_EDGES)
    return out[::step]


def attach_meshes(system, items: list[dict[str, Any]]) -> None:
    """给画不出解析轮廓的物体（Standard Lens、CAD、多边形物体等）挂上网格：mesh_edges、mesh_section（系统坐标）、
    mesh_box 和用于点在体内判断的 mesh_tris（只留在内存里，不写进结果）。按物体的类型、文件、参数和位置缓存。
    Attach meshes to objects without an analytic outline; cached by type, file, parameters and placement."""
    import json
    import tempfile

    todo = [it for it in items if outline(it) is None]
    if not todo:
        return
    with tempfile.TemporaryDirectory(prefix="saos-mesh-") as folder:
        for it in todo:
            key = json.dumps({k: it.get(k) for k in ("type", "file", "params", "position", "tilt", "ref", "inside_of", "global")},
                             sort_keys=True, default=str)
            if key not in _MESH_CACHE:
                tris = export_mesh(system, it["index"], folder)
                _MESH_CACHE[key] = None if not tris else {
                    "tris": tris, "edges": mesh_edges(tris), "section": mesh_section(tris),
                    "box": [[min(p[k] for t in tris for p in t) for k in range(3)], [max(p[k] for t in tris for p in t) for k in range(3)]]}
                while len(_MESH_CACHE) > MESH_CACHE_SIZE:
                    _MESH_CACHE.pop(next(iter(_MESH_CACHE)))
            mesh = _MESH_CACHE[key]
            if mesh is not None:
                it["mesh_tris"] = mesh["tris"]
                it["mesh_edges"], it["mesh_section"], it["mesh_box"] = mesh["edges"], mesh["section"], mesh["box"]
