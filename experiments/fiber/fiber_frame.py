"""光纤侧视的初始序列框架（实验，第一版范围之外，见 TODO.md）；不调用显微物镜验收器。

不属于 saos 包，也不接入后台进程协议。自己连接一个 standalone OpticStudio，搭建并检查框架后保存：

    uv run python experiments/fiber/fiber_frame.py --medium air --out <路径>.zmx

需要在原生窗口中查看时，用 saos lens import 导入任务，再 lens load、lens view。
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from saos.backend.zos import ZosBackend, _load_zosapi
from saos.protocol import ErrorCode, SaosError, jsonable

ASSUMPTIONS = [
    "胶层厚 5 µm，n=1.49；组织 n=1.36，水 n=1.333；三者暂忽略色散和吸收。",
    "棱镜及管壁暂用 N-BK7；棱镜第三维暂取 400 µm。反射面暂用理想 MIRROR，不代表已选择镀膜或验证全反射。",
    "透镜为单片半椭球，平面贴棱镜、凸面朝组织；空气版 nd=1.6，水版 nd=1.7，Vd=50 为模型假设。",
    "透镜与棱镜先按理想光学接触，未建独立胶层。棱镜中心折转面暂位于管轴上；椭球高度决定顶点与管内壁间隙。",
    "以光纤端面为物面、组织平面为像面；输入视场覆盖直径 450 µm。300–400 µm 是目标样品 FOV，尚未通过倍率实现。",
    "仅建几何框架；未建逐纤芯、近端 sCMOS 中继、组织散射、制造装配公差和分辨率验收。",
]


def validate_medium(medium):
    if medium not in ("air", "water"):
        raise SaosError(ErrorCode.BAD_ARGS, "medium 只能为 air 或 water")


def hemiellipsoid(diameter_mm, height_mm):
    if not all(math.isfinite(v) and v > 0 for v in (diameter_mm, height_mm)):
        raise SaosError(ErrorCode.BAD_ARGS, "半椭球直径、高度必须为有限正数")
    if diameter_mm > 0.5 or height_mm >= 0.1:
        raise SaosError(ErrorCode.BAD_ARGS, "当前布局要求直径 ≤0.5 mm、高度 <0.1 mm")
    b, a = diameter_mm / 2, height_mm
    # 完整半椭球在赤道处回到贴合平面，R、k 和中心厚不能独立改变。
    return {
        "diameter_mm": diameter_mm,
        "height_mm": a,
        "vertex_radius_mm": b * b / a,
        "conic": b * b / (a * a) - 1,
    }


def build(backend, medium, diameter_mm=None, height_mm=0.09):
    validate_medium(medium)
    shape = hemiellipsoid(
        diameter_mm
        if diameter_mm is not None
        else (0.134 if medium == "air" else 0.106),
        height_mm,
    )
    z = _load_zosapi(backend.root)
    backend.new_system()
    sys = backend.system
    sd = sys.SystemData
    sd.Units.LensUnits = z.SystemData.ZemaxSystemUnits.Millimeters
    sd.MaterialCatalogs.AddCatalog("SCHOTT")
    sd.Aperture.ApertureType = z.SystemData.ZemaxApertureType.ObjectSpaceNA
    sd.Aperture.ApertureValue = 0.5
    sd.RayAiming.RayAiming = z.SystemData.RayAimingMethod.Paraxial
    sd.Fields.SetFieldType(z.SystemData.FieldType.ObjectHeight)
    for x, y in [
        (0, 0.1125),
        (0, 0.225),
        (0, -0.1125),
        (0, -0.225),
        (0.1125, 0),
        (0.225, 0),
        (-0.1125, 0),
        (-0.225, 0),
    ]:
        sd.Fields.AddField(x, y, 1.0)
    sd.Wavelengths.GetWavelength(1).Wavelength = 0.488
    sd.Wavelengths.AddWavelength(0.532, 1.0)
    sd.Wavelengths.GetWavelength(1).MakePrimary()

    def material(n, vd=1e8):
        return {"model": {"nd": n, "vd": vd, "dpgf": 0.0}}

    liquid = "" if medium == "air" else material(1.333)
    rows = [
        ("棱镜入射面", math.inf, 0.20, "N-BK7", False),
        ("棱镜反射位置", math.inf, 0.20, "N-BK7", False),
        ("贴合位置／零间距参考面", math.inf, 0.0, "N-BK7", False),
        (
            "单片透镜平面／贴棱镜",
            math.inf,
            height_mm,
            material(1.6 if medium == "air" else 1.7, 50),
            False,
        ),
        (
            "单片透镜半椭球凸面／朝组织",
            -shape["vertex_radius_mm"],
            0.1 - height_mm,
            liquid,
            False,
        ),
        ("玻璃管内壁", -0.30, 0.05, "N-BK7", True),
        ("玻璃管外壁", -0.35, 0.10, material(1.36), True),
    ]
    lde = sys.LDE
    for _ in range(len(rows) - 1):
        lde.InsertNewSurfaceAt(1)
    obj = lde.GetSurfaceAt(0)
    obj.Comment, obj.Thickness = "光纤端面／胶层", 0.005
    backend._set_material(obj, material(1.49), "胶层")
    for i, (name, r, t, m, cyl) in enumerate(rows, 1):
        s = lde.GetSurfaceAt(i)
        if cyl:
            s.ChangeType(s.GetSurfaceTypeSettings(z.Editors.LDE.SurfaceType.Biconic))
            s.GetSurfaceCell(z.Editors.LDE.SurfaceColumn.Par1).DoubleValue = 0.0
        s.Radius, s.Thickness, s.Comment = r, t, name
        if i == 5:
            s.Conic = shape["conic"]
        if m:
            backend._set_material(s, m, name)
        if i <= 3:
            a = s.ApertureData.CreateApertureTypeSettings(
                z.Editors.LDE.SurfaceApertureTypes.RectangularAperture
            )
            a._S_RectangularAperture.XHalfWidth = 0.20
            a._S_RectangularAperture.YHalfWidth = 0.20
        else:
            a = s.ApertureData.CreateApertureTypeSettings(
                z.Editors.LDE.SurfaceApertureTypes.CircularAperture
            )
            a._S_CircularAperture.MaximumRadius = (
                shape["diameter_mm"] / 2 if i in (4, 5) else 0.28
            )
        s.ApertureData.ChangeApertureTypeSettings(a)
        s.SemiDiameter = (
            0.20 if i <= 3 else shape["diameter_mm"] / 2 if i <= 5 else 0.28
        )
    lde.GetSurfaceAt(4).IsStop = True
    lde.GetSurfaceAt(8).Comment = "脑组织样品面／距管外壁100微米"
    lde.GetSurfaceAt(8).SemiDiameter = 0.20
    message = lde.RunTool_AddFoldMirror(2, z.Editors.LDE.TiltType.YTilt, 90.0)
    if message is not None:
        raise SaosError(ErrorCode.ZOS_ERROR, "添加折转面失败", {"message": str(message)})
    lde.GetSurfaceAt(2).Comment = "折转前坐标系"
    lde.GetSurfaceAt(3).Comment = "棱镜斜面／理想反射"
    lde.GetSurfaceAt(4).Comment = "折转后坐标系"
    a = lde.GetSurfaceAt(3).ApertureData.CreateApertureTypeSettings(
        z.Editors.LDE.SurfaceApertureTypes.RectangularAperture
    )
    a._S_RectangularAperture.XHalfWidth = 0.20 * math.sqrt(2)
    a._S_RectangularAperture.YHalfWidth = 0.20
    lde.GetSurfaceAt(3).ApertureData.ChangeApertureTypeSettings(a)
    return {
        "medium": medium,
        "assumptions": ASSUMPTIONS,
        "lens_shape": shape,
        "num_surfaces": lde.NumberOfSurfaces,
    }


def inspect_model(backend):
    sys = backend.system
    rows = backend.snapshot()
    positions = []
    for i, row in enumerate(rows):
        surf = sys.LDE.GetSurfaceAt(i)
        row["parameters"] = [str(surf.GetCellAt(j).Value) for j in range(12, 18)]
        aperture = surf.ApertureData.CurrentTypeSettings
        if row["aperture"] == "RectangularAperture":
            data = aperture._S_RectangularAperture
            row["aperture_dimensions"] = [
                float(data.XHalfWidth),
                float(data.YHalfWidth),
            ]
        elif row["aperture"] == "CircularAperture":
            data = aperture._S_CircularAperture
            row["aperture_dimensions"] = [
                float(data.MinimumRadius),
                float(data.MaximumRadius),
            ]
        matrix = sys.LDE.GetGlobalMatrix(i, *([0.0] * 12))
        if not matrix[0]:
            raise SaosError(ErrorCode.ZOS_ERROR, f"无法读取面 {i} 的全局坐标")
        positions.append([float(v) for v in matrix[-3:]])
    info = backend.system_info()
    info.pop("file", None)
    return {"system": info, "rows": rows, "positions_mm": positions}


def geometry_checks(snapshot):
    rows = snapshot["rows"]
    if (
        len(rows) != 11
        or rows[3]["material"] != "MIRROR"
        or rows[8]["type"] != "Biconic"
        or rows[9]["type"] != "Biconic"
    ):
        raise SaosError(ErrorCode.BAD_ARGS, "当前不是 11 面的光纤侧视框架")
    p = snapshot["positions_mm"]

    def vector(a, b):
        return [x - y for x, y in zip(p[a], p[b])]

    def norm(v):
        return math.sqrt(sum(x * x for x in v))

    incoming, outgoing = vector(1, 0), vector(10, 9)
    cosine = (
        sum(a * b for a, b in zip(incoming, outgoing)) / norm(incoming) / norm(outgoing)
    )
    angle = math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
    direction = [v / norm(incoming) for v in incoming]
    radii = []
    for index in (8, 9):
        v = vector(index, 3)
        along = sum(a * b for a, b in zip(v, direction))
        radii.append(math.sqrt(max(0.0, sum(a * a for a in v) - along * along)))
    wd = norm(vector(10, 9))
    values = {
        "fold_angle_deg": angle,
        "tube_inner_radius_mm": radii[0],
        "tube_outer_radius_mm": radii[1],
        "working_distance_mm": wd,
        "prism_before_mm": norm(vector(3, 1)),
        "prism_after_mm": norm(vector(5, 3)),
    }
    expected = {
        "fold_angle_deg": 90.0,
        "tube_inner_radius_mm": 0.30,
        "tube_outer_radius_mm": 0.35,
        "working_distance_mm": 0.10,
        "prism_before_mm": 0.20,
        "prism_after_mm": 0.20,
    }
    return {
        "values": values,
        "pass": all(
            math.isfinite(v) and math.isclose(v, expected[k], abs_tol=1e-9)
            for k, v in values.items()
        ),
    }


def focus_probe(backend):
    """从两面实际交点估算轴上近轴焦点；不代替全孔径像质验收。"""
    z = _load_zosapi(backend.root)
    rays = [(w, px, py) for w in (1, 2) for px, py in ((0.01, 0.0), (0.0, 0.01))]
    hits = {}
    for surface in (9, 10):
        tool = backend.system.Tools.OpenBatchRayTrace()
        try:
            batch = tool.CreateNormUnpol(
                len(rays), z.Tools.RayTrace.RaysType.Real, surface
            )
            for wave, px, py in rays:
                batch.AddRay(
                    wave, 0.0, 0.0, px, py, getattr(z.Tools.RayTrace.OPDMode, "None")
                )
            tool.RunAndWaitForCompletion()
            batch.StartReadingResults()
            hits[surface] = []
            for _ in rays:
                result = list(batch.ReadNextResult(0, 0, 0, *([0.0] * 11)))
                if (
                    not result[0]
                    or result[2]
                    or result[3]
                    or not all(math.isfinite(float(v)) for v in result[4:7])
                ):
                    raise SaosError(
                        ErrorCode.ZOS_ERROR,
                        "近轴焦点探针光线无效",
                        {"surface": surface, "result": result},
                    )
                hits[surface].append(result)
        finally:
            tool.Close()
    measured = []
    for j, (wave, px, py) in enumerate(rays):
        wall, image = hits[9][j], hits[10][j]
        axis = 4 if px else 5
        depth_wall = -float(wall[6])
        depth_image = abs(float(backend.system.LDE.GetSurfaceAt(9).Thickness))
        h_wall, h_image = float(wall[axis]), float(image[axis])
        slope = (h_image - h_wall) / (depth_image - depth_wall)
        focus = depth_wall - h_wall / slope if slope else None
        measured.append(
            {
                "wave": wave,
                "meridian": "X" if px else "Y",
                "focus_from_outer_wall_mm": focus,
                "target_offset_um": None
                if focus is None
                else (focus - depth_image) * 1000,
                "forward_converging": focus is not None
                and focus > 0
                and h_wall * slope < 0,
            }
        )
    return {
        "normalized_pupil_radius": 0.01,
        "measurements": measured,
        "all_forward_converging": all(r["forward_converging"] for r in measured),
        "scope": "轴上小孔径探针；不是全孔径焦斑或分辨率验收",
    }


def check(backend, path):
    """保存后重载一致、全局几何、30 条抽查光线和轴上小孔径会聚探针（原 lens fiber-check）。"""
    before = inspect_model(backend)
    backend.load(path)
    after = inspect_model(backend)
    if jsonable(before) != jsonable(after):
        raise SaosError(ErrorCode.VERIFY_FAILED, "重载后光纤框架发生变化")
    rays = [
        (w, x, y, px, py)
        for w in (1, 2)
        for x, y in [(0.0, 0.0), (0.0, 1.0), (1.0, 0.0)]
        for px, py in [(0.0, 0.0), (0.5, 0.0), (-0.5, 0.0), (0.0, 0.5), (0.0, -0.5)]
    ]
    try:
        focus = focus_probe(backend)
    except SaosError as exc:
        focus = {"all_forward_converging": False, "error": str(exc)}
    return {
        "reload_equal": True,
        "geometry": geometry_checks(after),
        "snapshot": after,
        "rays": len(rays),
        "failed_rays": backend.trace_check(rays),
        "focus_probe": focus,
        "optical_acceptance": "尚未执行",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="搭建并检查光纤侧视初始框架")
    parser.add_argument("--medium", choices=("air", "water"), default="air")
    parser.add_argument("--out", required=True, help="输出 .zmx 路径，不能已存在")
    args = parser.parse_args(argv)
    out = Path(args.out).resolve()
    if out.exists():
        parser.error(f"{out} 已存在")
    backend = ZosBackend("standalone")
    try:
        backend.start()
        result = build(backend, args.medium)
        out.parent.mkdir(parents=True, exist_ok=True)
        backend.save_as(out)
        result["check"] = check(backend, out)
    finally:
        backend.close()
    out.with_suffix(".check.json").write_text(json.dumps(jsonable(result), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(jsonable({k: v for k, v in result.items() if k != "check"} | {"geometry_pass": result["check"]["geometry"]["pass"]}), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
