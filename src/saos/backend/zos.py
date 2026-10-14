"""通过 pythonnet 调用 OpticStudio ZOS-API 的后端。

显式加载 config.opticstudio_dir() 下的 DLL，避免本机多个 OpticStudio 版本时加载错版本。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from saos import config
from saos.backend.base import (
    NUMERIC_PARAMS,
    SURFACE_PARAMS,
    Backend,
    parse_numeric,
    same_number,
)
from saos.protocol import ErrorCode, SaosError

_ZOSAPI = None


def _load_zosapi(root: str):
    global _ZOSAPI
    if _ZOSAPI is not None:
        return _ZOSAPI
    for dll in ("ZOSAPI_NetHelper.dll", "ZOSAPI.dll", "ZOSAPI_Interfaces.dll"):
        if not os.path.isfile(os.path.join(root, dll)):
            raise SaosError(ErrorCode.ZOS_ERROR, f"在 {root} 中找不到 {dll}", {"opticstudio_dir": root})
    import clr  # pythonnet

    clr.AddReference(os.path.join(root, "ZOSAPI_NetHelper.dll"))
    import ZOSAPI_NetHelper

    if not ZOSAPI_NetHelper.ZOSAPI_Initializer.Initialize(root):
        raise SaosError(ErrorCode.ZOS_ERROR, f"ZOSAPI_Initializer 初始化失败：{root}")
    clr.AddReference(os.path.join(root, "ZOSAPI.dll"))
    clr.AddReference(os.path.join(root, "ZOSAPI_Interfaces.dll"))
    import ZOSAPI

    _ZOSAPI = ZOSAPI
    return ZOSAPI


class ZosBackend(Backend):
    def __init__(self, mode: str = "standalone"):
        if mode not in ("standalone", "extension"):
            raise SaosError(ErrorCode.BAD_ARGS, f"未知的连接方式：{mode}")
        self.mode = mode
        self.root = config.opticstudio_dir()
        self.app = None
        self.extension_ui: dict[str, Any] | None = None
        self._material_cache: dict[tuple[str, ...], set[str]] = {}

    # ---- 连接 ----

    def start(self) -> dict[str, Any]:
        zosapi = _load_zosapi(self.root)
        conn = zosapi.ZOSAPI_Connection()
        if self.mode == "standalone":
            app = conn.CreateNewApplication()
        else:
            from saos.native_ui import ensure_extension

            self.extension_ui = ensure_extension()
            app = conn.ConnectAsExtension(self.extension_ui["instance"])
        hint = "在 OpticStudio 中点 Programming → Interactive Extension，使它处于等待连接状态" if self.mode == "extension" else ""
        if app is None:
            raise SaosError(ErrorCode.ZOS_ERROR, f"无法连接 OpticStudio（{self.mode}）。{hint}")
        if self.mode == "extension" and not self._extension_ready(app):
            # 实测 2024 R1：上一个扩展客户端断开后，窗口退出扩展状态，ConnectAsExtension 仍返回对象，
            # 但没有 PrimarySystem，许可检查为 False，ProgramDir 抛 ArgumentNullException
            raise SaosError(ErrorCode.ZOS_ERROR, f"OpticStudio 窗口没有处于 Interactive Extension 状态。{hint}；每次后台进程退出后都要重新点一次")
        self.app = app
        version = (int(app.ZOSMajorVersion), int(app.ZOSMinorVersion), int(app.ZOSSPVersion))
        info = {
            "mode": self.mode,
            "version": ".".join(map(str, version)),
            "license": str(app.LicenseStatus),
            "program_dir": str(app.ProgramDir),
        }
        if self.mode == "extension":
            info.update(extension_ui=self.extension_ui, initial_file=str(app.PrimarySystem.SystemFile), initial_needs_save=bool(app.PrimarySystem.NeedsSave))
        if not app.IsValidLicenseForAPI:
            self.close()
            raise SaosError(ErrorCode.ZOS_LICENSE, "许可证不支持 ZOS-API", info)
        if version != config.EXPECTED_ZOS_VERSION:
            self.close()
            expected = ".".join(map(str, config.EXPECTED_ZOS_VERSION))
            raise SaosError(ErrorCode.ZOS_VERSION_MISMATCH, f"OpticStudio 版本为 {info['version']}，项目锁定 {expected}", info)
        return info

    @contextmanager
    def activity(self, label: str, quiet: bool):
        app = self.app if self.mode == "extension" else None
        if app is None:
            yield
            return
        try:
            app.ProgressMessage = label
            app.ShowChangesInUI = not quiet
        except Exception:
            pass
        try:
            yield
        finally:
            try:
                app.ShowChangesInUI = True
                app.ProgressMessage = ""
            except Exception:
                pass

    @staticmethod
    def _extension_ready(app) -> bool:
        try:
            return app.PrimarySystem is not None and bool(app.IsValidLicenseForAPI)
        except Exception:
            return False

    def close(self) -> None:
        app, self.app = self.app, None
        if app is not None and self.mode == "standalone":
            try:
                app.CloseApplication()
            except Exception:
                pass

    def healthy(self) -> bool:
        try:
            return self.app is not None and bool(self.app.IsValidLicenseForAPI) and self.app.PrimarySystem is not None
        except Exception:
            return False

    @property
    def system(self):
        if self.app is None:
            raise SaosError(ErrorCode.ZOS_CONNECTION_LOST, "未连接 OpticStudio")
        return self.app.PrimarySystem

    def needs_save(self) -> bool:
        return bool(self.system.NeedsSave)

    def has_unsaved_changes(self) -> bool:
        return self.mode == "extension" and self.needs_save()

    # ---- 分析导出 ----

    def analysis_types(self) -> list[str]:
        from saos.backend import analysis

        return analysis.analysis_names(_load_zosapi(self.root))

    def analysis(self, name: str, settings: dict[str, Any], text_path: Path, keep_open: bool = False) -> dict[str, Any]:
        from saos.backend import analysis

        zosapi = _load_zosapi(self.root)
        run = self.system.Analyses.New_Analysis(analysis.idm(zosapi, name))
        try:
            impl = analysis.settings_of(run)
            analysis.apply_settings(impl, settings)
            message = run.ApplyAndWaitForCompletion()
            results = run.GetResults()
            if results is None:
                raise SaosError(ErrorCode.ZOS_ERROR, f"{name} returned no results", {"message": str(message) if message else None})
            info = self.system_info()
            out = analysis.extract(results, len(info["fields"]["items"]), len(info["wavelengths"]))
            text_path.parent.mkdir(parents=True, exist_ok=True)
            out["text_file"] = bool(results.GetTextFile(str(text_path)))
            out["settings"] = analysis.describe_settings(impl)
            out["message"] = str(message) if message else None
            return out
        finally:
            # 显性调试时可以留下窗口给用户看；隐式运行一律关闭，避免分析对象越积越多
            if not (keep_open and self.mode == "extension"):
                run.Close()

    # 原生分析窗口；波前图和 PSF 只看轴上主波长，其余设为全部视场、全部波长
    _ANALYSES = ("Draw3D", "StandardSpot", "RayFan", "WavefrontMap", "HuygensPsf", "HuygensMtf", "RMSField", "GeometricEncircledEnergy")
    _AXIS_ONLY = ("WavefrontMap", "HuygensPsf")

    def show_analyses(self) -> dict[str, Any]:
        if self.mode != "extension":
            return super().show_analyses()
        zosapi = _load_zosapi(self.root)
        system = self.system
        self.app.ShowChangesInUI = True
        system.LDE.ShowLDE()
        system.MFE.ShowMFE()
        existing = {}
        for i in range(1, system.Analyses.NumberOfAnalyses + 1):
            analysis = system.Analyses.Get_AnalysisAtIndex(i)
            existing[str(analysis.AnalysisType)] = analysis
        opened = []
        for name in self._ANALYSES:
            self.app.ProgressMessage = f"saos：刷新 {name}"
            analysis = existing.get(name) or system.Analyses.New_Analysis_SettingsFirst(getattr(zosapi.Analysis.AnalysisIDM, name))
            settings = analysis.GetSettings()
            number = 1 if name in self._AXIS_ONLY else 0
            if hasattr(settings, "Field"):
                settings.Field.SetFieldNumber(number)
            if hasattr(settings, "Wavelength"):
                settings.Wavelength.SetWavelengthNumber(number)
            message = analysis.ApplyAndWaitForCompletion()
            opened.append({"analysis": name, "message": None if message is None else str(message), "title": str(analysis.Title)})
        system.UpdateMode = zosapi.LensUpdateMode.AllWindows
        status = str(system.UpdateStatus())
        self.app.ProgressMessage = "saos：分析窗口已刷新"
        return {"update_mode": str(system.UpdateMode), "system_status": status, "analyses": opened}

    # ---- 文件 ----

    def load(self, path: Path) -> None:
        path = Path(path).resolve()
        if not self.system.LoadFile(str(path), False):
            raise SaosError(ErrorCode.ZOS_LOAD_FAILED, f"OpticStudio 无法加载 {path}")
        self._material_cache.clear()

    def save_as(self, path: Path) -> None:
        path = Path(path).resolve()
        self.system.SaveAs(str(path))
        if not path.is_file():
            raise SaosError(ErrorCode.ZOS_SAVE_FAILED, f"保存后找不到文件 {path}")

    def new_system(self) -> None:
        self.system.New(False)
        self._material_cache.clear()

    # ---- 读取 ----

    def system_info(self) -> dict[str, Any]:
        system = self.system
        sd = system.SystemData
        fields = sd.Fields
        waves = sd.Wavelengths
        lde = system.LDE
        return {
            "file": str(system.SystemFile),
            "title": str(sd.TitleNotes.Title),
            "lens_units": str(sd.Units.LensUnits),
            "aperture": {
                "type": str(sd.Aperture.ApertureType),
                "value": float(sd.Aperture.ApertureValue),
                "telecentric_object": bool(sd.Aperture.TelecentricObjectSpace),
            },
            "fields": {
                "type": str(fields.GetFieldType()),
                "items": [
                    {"x": float(f.X), "y": float(f.Y), "weight": float(f.Weight)}
                    for f in (fields.GetField(i) for i in range(1, fields.NumberOfFields + 1))
                ],
            },
            "wavelengths": [
                {"um": float(w.Wavelength), "weight": float(w.Weight), "primary": bool(w.IsPrimary)}
                for w in (waves.GetWavelength(i) for i in range(1, waves.NumberOfWavelengths + 1))
            ],
            "material_catalogs": [c for c in self._catalogs_in_use()],
            "num_surfaces": int(lde.NumberOfSurfaces),
            "stop_surface": int(lde.StopSurface),
        }

    def surfaces(self) -> list[dict[str, Any]]:
        lde = self.system.LDE
        return [self._surface_dict(lde.GetSurfaceAt(i), i) for i in range(lde.NumberOfSurfaces)]

    @staticmethod
    def _surface_dict(surf, index: int) -> dict[str, Any]:
        return {
            "index": index,
            "type": str(surf.TypeName),
            "comment": str(surf.Comment),
            "radius": float(surf.Radius),
            "thickness": float(surf.Thickness),
            "material": str(surf.Material),
            "semi_diameter": float(surf.SemiDiameter),
            "conic": float(surf.Conic),
            "is_stop": bool(surf.IsStop),
            "solves": {
                "radius": str(surf.RadiusCell.GetSolveData().Type),
                "thickness": str(surf.ThicknessCell.GetSolveData().Type),
                "material": str(surf.MaterialCell.GetSolveData().Type),
                "semi_diameter": str(surf.SemiDiameterCell.GetSolveData().Type),
                "conic": str(surf.ConicCell.GetSolveData().Type),
            },
        }

    # ---- 修改 ----

    _ATTR = {
        "radius": "Radius",
        "thickness": "Thickness",
        "conic": "Conic",
        "semi_diameter": "SemiDiameter",
        "material": "Material",
        "comment": "Comment",
    }

    def set_surface(self, index: int, param: str, value: Any) -> dict[str, Any]:
        if param not in SURFACE_PARAMS:
            raise SaosError(ErrorCode.BAD_ARGS, f"不支持的面参数 {param!r}，可用：{', '.join(SURFACE_PARAMS)}")
        lde = self.system.LDE
        if not isinstance(index, int) or not 0 <= index < lde.NumberOfSurfaces:
            raise SaosError(ErrorCode.BAD_ARGS, f"面序号 {index!r} 超出范围 0–{lde.NumberOfSurfaces - 1}")
        surf = lde.GetSurfaceAt(index)
        attr = self._ATTR[param]

        if param in NUMERIC_PARAMS:
            target: Any = parse_numeric(param, value)
        else:
            target = "" if value is None else str(value)
            if param == "material":
                target = self._check_material(target)

        setattr(surf, attr, target)
        actual = getattr(surf, attr)
        applied = same_number(float(actual), target) if param in NUMERIC_PARAMS else str(actual) == target
        if not applied:
            cell = getattr(surf, f"{attr}Cell")
            raise SaosError(
                ErrorCode.VALUE_NOT_APPLIED,
                f"面 {index} 的 {param} 没有变成目标值，可能被求解或自动设置覆盖",
                {"requested": target, "actual": actual, "solve": str(cell.GetSolveData().Type)},
            )
        return self._surface_dict(surf, index)

    def _catalogs_in_use(self) -> list[str]:
        return [str(c) for c in self.system.SystemData.MaterialCatalogs.GetCatalogsInUse() if str(c)]

    def _check_material(self, name: str) -> str:
        # ZOS-API 对不存在的牌号不报错，只能自己按当前使用的玻璃目录核对
        if name == "" or name.upper() == "MIRROR":
            return name.upper()
        catalogs = tuple(self._catalogs_in_use())
        if catalogs not in self._material_cache:
            mc = self.system.SystemData.MaterialCatalogs
            names: set[str] = set()
            for cat in catalogs:
                names.update(str(m).upper() for m in mc.GetMaterialsInCatalog(cat))
            self._material_cache[catalogs] = names
        upper = name.upper()
        if upper not in self._material_cache[catalogs]:
            raise SaosError(
                ErrorCode.BAD_ARGS,
                f"当前使用的玻璃目录中没有 {name!r}",
                {"catalogs": list(catalogs)},
            )
        return upper

    # ---- 搭建与快照 ----

    def _set_material(self, row, material: Any, label: str) -> None:
        zosapi = _load_zosapi(self.root)
        if isinstance(material, dict):
            model = material["model"]
            solve = row.MaterialCell.CreateSolveType(zosapi.Editors.SolveType.MaterialModel)
            data = solve._S_MaterialModel
            data.IndexNd = model["nd"]
            data.AbbeVd = model["vd"]
            data.dPgF = model["dpgf"]
            row.MaterialCell.SetSolveData(solve)
            return
        try:
            row.Material = self._check_material(material)
        except SaosError as exc:
            raise SaosError(exc.code, f"{label}：{exc.message}", exc.details) from exc

    def build(self, prescription: dict[str, Any]) -> None:
        zosapi = _load_zosapi(self.root)
        p = prescription
        system = self.system
        system.New(False)
        self._material_cache.clear()
        sd = system.SystemData
        sd.Units.LensUnits = zosapi.SystemData.ZemaxSystemUnits.Millimeters

        mc = sd.MaterialCatalogs
        wanted = [c.upper() for c in p["catalogs"]]
        for cat in wanted:
            if not mc.IsCatalogInUse(cat):
                mc.AddCatalog(cat)
        for cat in [str(c) for c in mc.GetCatalogsInUse() if str(c)]:
            if cat.upper() not in wanted:
                mc.RemoveCatalog(cat)
        missing = [c for c in wanted if c not in [str(x).upper() for x in mc.GetCatalogsInUse()]]
        if missing:
            raise SaosError(ErrorCode.BAD_ARGS, f"无法加载玻璃目录 {missing}")

        sd.Aperture.ApertureType = zosapi.SystemData.ZemaxApertureType.ObjectSpaceNA
        sd.Aperture.ApertureValue = p["aperture"]["value"]
        # 光阑在物镜后焦面时入瞳在无穷远；用 OpticStudio 的物方远心设置表达，避免光线瞄准在退化情形下失败
        sd.Aperture.TelecentricObjectSpace = bool(p.get("telecentric_object", False))

        fields = sd.Fields
        fields.SetFieldType(zosapi.SystemData.FieldType.ObjectHeight)
        while fields.NumberOfFields > 1:
            fields.RemoveField(fields.NumberOfFields)
        first = fields.GetField(1)
        first.X, first.Y, first.Weight = 0.0, p["fields"]["y"][0], 1.0
        for y in p["fields"]["y"][1:]:
            fields.AddField(0.0, y, 1.0)
        fields.ClearVignetting()

        waves = sd.Wavelengths
        while waves.NumberOfWavelengths > 1:
            waves.RemoveWavelength(waves.NumberOfWavelengths)
        um, weights = p["wavelengths"]["um"], p["wavelengths"]["weights"]
        waves.GetWavelength(1).Wavelength = um[0]
        waves.GetWavelength(1).Weight = weights[0]
        for wl, wt in zip(um[1:], weights[1:]):
            waves.AddWavelength(wl, wt)
        waves.GetWavelength(p["wavelengths"]["primary"]).MakePrimary()

        aiming = zosapi.SystemData.RayAimingMethod
        sd.RayAiming.RayAiming = {"off": aiming.Off, "paraxial": aiming.Paraxial, "real": aiming.Real}[p["ray_aiming"]]

        lde = system.LDE
        surfaces = p["surfaces"]
        # 新系统有 物面、面 1、像面 三行
        for _ in range(len(surfaces) - 1):
            lde.InsertNewSurfaceAt(1)
        obj = lde.GetSurfaceAt(0)
        obj.Thickness = p["object"]["thickness"]
        obj.Comment = p["object"]["comment"]
        if p["object"]["material"]:
            self._set_material(obj, p["object"]["material"], "物面")
        columns = zosapi.Editors.LDE.SurfaceColumn
        for k, s in enumerate(surfaces, start=1):
            row = lde.GetSurfaceAt(k)
            if s["type"] == "paraxial":
                row.ChangeType(row.GetSurfaceTypeSettings(zosapi.Editors.LDE.SurfaceType.Paraxial))
                row.GetSurfaceCell(columns.Par1).DoubleValue = s["focal_length"]
                row.GetSurfaceCell(columns.Par2).IntegerValue = s["opd_mode"]
            else:
                row.Radius = s["radius"]
                row.Conic = s["conic"]
                if s["material"]:
                    self._set_material(row, s["material"], f"面 {k}")
            row.Thickness = s["thickness"]
            row.Comment = s["comment"]
        for k, s in enumerate(surfaces, start=1):
            if s["stop"]:
                lde.GetSurfaceAt(k).IsStop = True
        self.clear_apertures()

    def clear_apertures(self) -> list[dict[str, Any]]:
        """删除所有面孔径和遮拦、半口径改为自动；返回原来设置了孔径的面。"""
        zosapi = _load_zosapi(self.root)
        lde = self.system.LDE
        none_type = getattr(zosapi.Editors.LDE.SurfaceApertureTypes, "None")
        removed = []
        for i in range(lde.NumberOfSurfaces):
            row = lde.GetSurfaceAt(i)
            current = str(row.ApertureData.CurrentType)
            if current != "None":
                removed.append({"surface": i, "aperture": current})
                row.ApertureData.ChangeApertureTypeSettings(row.ApertureData.CreateApertureTypeSettings(none_type))
            if i > 0:
                cell = row.SemiDiameterCell
                cell.SetSolveData(cell.CreateSolveType(zosapi.Editors.SolveType.Automatic))
        self.system.SystemData.Fields.ClearVignetting()
        return removed

    def snapshot(self) -> list[dict[str, Any]]:
        zosapi = _load_zosapi(self.root)
        lde = self.system.LDE
        columns = zosapi.Editors.LDE.SurfaceColumn
        rows = []
        for i in range(lde.NumberOfSurfaces):
            row = lde.GetSurfaceAt(i)
            type_name = str(row.TypeName)
            item: dict[str, Any] = {
                "index": i,
                "type": type_name.lower() if type_name in ("Standard", "Paraxial") else type_name,
                "thickness": float(row.Thickness),
                "comment": str(row.Comment),
                "stop": bool(row.IsStop),
                "semi_diameter": float(row.SemiDiameter),
                "aperture": str(row.ApertureData.CurrentType),
            }
            if type_name == "Paraxial":
                item["focal_length"] = float(row.GetSurfaceCell(columns.Par1).DoubleValue)
                item["opd_mode"] = int(row.GetSurfaceCell(columns.Par2).IntegerValue)
            else:
                item["radius"] = float(row.Radius)
                item["conic"] = float(row.Conic)
            solve = row.MaterialCell.GetSolveData()
            if str(solve.Type) == "MaterialModel":
                m = solve._S_MaterialModel
                item["material"] = {"model": {"nd": float(m.IndexNd), "vd": float(m.AbbeVd), "dpgf": float(m.dPgF)}}
            else:
                item["material"] = str(row.Material).upper()
            rows.append(item)
        return rows

    def zernike(self, *, wave: int, field: int, samp: int, max_term: int, terms: tuple[int, ...]) -> dict[int, float]:
        # ZERN 的拟合项数取相邻 ZERN 行中最大的 Term（至少 11）。单独调用 GetOperandValue 只会拟合 11 项，
        # 所以在评价函数末尾临时加一组相邻的 ZERN 行，算完再删掉，不改动原有评价函数
        zosapi = _load_zosapi(self.root)
        mfe = self.system.MFE
        columns = zosapi.Editors.MFE.MeritColumn
        zern = zosapi.Editors.MFE.MeritOperandType.ZERN
        requested = [max_term, *terms]
        start = mfe.NumberOfOperands + 1
        try:
            for term in requested:
                row = mfe.AddOperand()
                row.ChangeType(zern)
                row.GetOperandCell(columns.Param1).IntegerValue = int(term)
                row.GetOperandCell(columns.Param2).IntegerValue = int(wave)
                row.GetOperandCell(columns.Param3).IntegerValue = int(samp)
                row.GetOperandCell(columns.Param4).IntegerValue = int(field)
                row.GetOperandCell(columns.Param5).IntegerValue = 1  # Standard
                row.GetOperandCell(columns.Param6).DoubleValue = 0.0
                row.GetOperandCell(columns.Param7).IntegerValue = 0
                row.Weight = 0.0
            mfe.CalculateMeritFunction()
            values = [float(mfe.GetOperandAt(start + k).Value) for k in range(len(requested))]
        finally:
            count = mfe.NumberOfOperands - start + 1
            if count > 0:
                mfe.RemoveOperandsAt(start, count)
        return dict(zip(terms, values[1:]))

    # ---- 优化 ----

    _CELLS = {"radius": "RadiusCell", "thickness": "ThicknessCell", "conic": "ConicCell"}

    def vary(self, surface: int, param: str, on: bool) -> str:
        if param not in self._CELLS:
            raise SaosError(ErrorCode.BAD_ARGS, f"只能把 {', '.join(self._CELLS)} 设为变量")
        lde = self.system.LDE
        # 物面只允许厚度（无盖玻片时即工作距离间隔）设为变量
        allowed = 0 < surface < lde.NumberOfSurfaces - 1 or (surface == 0 and param == "thickness")
        if not allowed:
            raise SaosError(ErrorCode.BAD_ARGS, f"面 {surface} 的 {param} 不能设变量")
        cell = getattr(lde.GetSurfaceAt(surface), self._CELLS[param])
        cell.MakeSolveVariable() if on else cell.MakeSolveFixed()
        return str(cell.GetSolveData().Type)

    def merit_wizard(self, settings: dict[str, Any]) -> int:
        wiz = self.system.MFE.SEQOptimizationWizard

        def pick(name: str, count_attr: str, getter: str, wanted: str) -> int:
            names = [str(getattr(wiz, getter)(i)) for i in range(getattr(wiz, count_attr))]
            for i, n in enumerate(names):
                if n.replace(" ", "").lower() == wanted.replace(" ", "").lower():
                    return i
            raise SaosError(ErrorCode.BAD_ARGS, f"优化向导的 {name} 只能是 {names}，收到 {wanted!r}")

        wiz.Type = pick("type", "NumberOfTypes", "GetTypeAt", settings.get("type", "RMS"))
        wiz.Data = pick("data", "NumberOfDataTypes", "GetDataTypeAt", settings.get("data", "Wavefront"))
        wiz.Reference = pick("reference", "NumberOfReferences", "GetReferenceAt", settings.get("reference", "Centroid"))
        wiz.Ring = int(settings.get("rings", 3)) - 1
        wiz.Arm = 0 if int(settings.get("arms", 6)) == 6 else (1 if int(settings.get("arms", 6)) == 8 else 2)
        glass = settings.get("glass")
        wiz.IsGlassUsed = glass is not None
        if glass:
            wiz.GlassMin, wiz.GlassMax, wiz.GlassEdge = float(glass[0]), float(glass[1]), float(glass[2])
        air = settings.get("air")
        wiz.IsAirUsed = air is not None
        if air:
            wiz.AirMin, wiz.AirMax, wiz.AirEdge = float(air[0]), float(air[1]), float(air[2])
        wiz.StartAt = int(settings.get("start_at", 1))
        wiz.IsAssumeAxialSymmetryUsed = True
        wiz.Apply()
        return int(self.system.MFE.NumberOfOperands)

    def merit_add(self, rows: list[dict[str, Any]]) -> int:
        zosapi = _load_zosapi(self.root)
        mfe = self.system.MFE
        columns = zosapi.Editors.MFE.MeritColumn
        params = [columns.Param1, columns.Param2, columns.Param3, columns.Param4, columns.Param5, columns.Param6, columns.Param7, columns.Param8]
        for spec in rows:
            op_type = getattr(zosapi.Editors.MFE.MeritOperandType, str(spec["type"]).upper(), None)
            if op_type is None:
                raise SaosError(ErrorCode.BAD_ARGS, f"未知的操作数 {spec['type']!r}")
            # 新行插在开头，避免被默认评价函数（从 DMFS 起）重建时覆盖
            row = mfe.InsertNewOperandAt(1)
            row.ChangeType(op_type)
            values = [spec.get("int1", 0), spec.get("int2", 0), *spec.get("data", [])]
            for col, value in zip(params, values):
                cell = row.GetOperandCell(col)
                if str(cell.DataType) == "Integer":
                    cell.IntegerValue = int(value)
                elif str(cell.DataType) == "Double":
                    cell.DoubleValue = float(value)
            row.Target = float(spec.get("target", 0.0))
            row.Weight = float(spec.get("weight", 1.0))
        return int(mfe.NumberOfOperands)

    def merit_clear(self) -> None:
        self.system.MFE.DeleteAllRows()

    def merit_value(self) -> float:
        return float(self.system.MFE.CalculateMeritFunction())

    def merit_rows(self) -> list[dict[str, Any]]:
        mfe = self.system.MFE
        mfe.CalculateMeritFunction()
        rows = []
        for i in range(1, mfe.NumberOfOperands + 1):
            row = mfe.GetOperandAt(i)
            kind = str(row.Type)
            if kind == "BLNK":
                continue
            rows.append({"row": i, "type": kind, "target": float(row.Target), "weight": float(row.Weight), "value": float(row.Value), "contribution": float(row.Contribution)})
        return rows

    def optimize(self, cycles: str, cores: int | None = None) -> dict[str, Any]:
        zosapi = _load_zosapi(self.root)
        names = {"auto": "Automatic", "1": "Fixed_1_Cycle", "5": "Fixed_5_Cycles", "10": "Fixed_10_Cycles", "50": "Fixed_50_Cycles"}
        if cycles not in names:
            raise SaosError(ErrorCode.BAD_ARGS, f"cycles 只能是 {list(names)}")
        tool = self.system.Tools.OpenLocalOptimization()
        try:
            tool.Algorithm = zosapi.Tools.Optimization.OptimizationAlgorithm.DampedLeastSquares
            tool.Cycles = getattr(zosapi.Tools.Optimization.OptimizationCycles, names[cycles])
            if cores:
                tool.NumberOfCores = int(cores)
            tool.RunAndWaitForCompletion()
            return {
                "initial_merit": float(tool.InitialMeritFunction),
                "final_merit": float(tool.CurrentMeritFunction),
                "variables": int(tool.Variables),
                "targets": int(tool.Targets),
                "succeeded": bool(tool.Succeeded),
            }
        finally:
            tool.Close()

    def trace_check(self, rays: list[tuple[int, float, float, float, float]]) -> list[dict[str, Any]]:
        zosapi = _load_zosapi(self.root)
        rt_ns = zosapi.Tools.RayTrace
        system = self.system
        image = system.LDE.NumberOfSurfaces - 1
        tool = system.Tools.OpenBatchRayTrace()
        try:
            batch = tool.CreateNormUnpol(len(rays), rt_ns.RaysType.Real, image)
            none_mode = getattr(rt_ns.OPDMode, "None")
            for wave, hx, hy, px, py in rays:
                batch.AddRay(int(wave), float(hx), float(hy), float(px), float(py), none_mode)
            tool.RunAndWaitForCompletion()
            batch.StartReadingResults()
            failures = []
            for ray in rays:
                result = batch.ReadNextResult(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
                error, vignette = int(result[2]), int(result[3])
                if error or vignette:
                    failures.append({"ray": list(ray), "error": error, "vignette": vignette})
            return failures
        finally:
            tool.Close()

    # ---- 操作数 ----

    def operand(self, name: str, int1: int, int2: int, data: list[float]) -> float:
        zosapi = _load_zosapi(self.root)
        op_type = getattr(zosapi.Editors.MFE.MeritOperandType, name.upper(), None)
        if op_type is None:
            raise SaosError(ErrorCode.BAD_ARGS, f"未知的操作数 {name!r}")
        if len(data) > 6:
            raise SaosError(ErrorCode.BAD_ARGS, "操作数最多 6 个浮点参数")
        padded = [float(x) for x in data] + [0.0] * (6 - len(data))
        return float(self.system.MFE.GetOperandValue(op_type, int(int1), int(int2), *padded))
