"""通过 pythonnet 调用 OpticStudio ZOS-API 的后端。

显式加载 config.opticstudio_dir() 下的 DLL，避免本机多个 OpticStudio 版本时加载错版本。
"""

from __future__ import annotations

import math
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from saos import config, geometry
from saos.backend.base import (
    MATERIAL_MODES,
    NUMERIC_PARAMS,
    PAR_RE,
    Backend,
    check_param,
    is_par,
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
            raise SaosError(ErrorCode.ZOS_ERROR, f"{dll} not found in {root}", {"opticstudio_dir": root})
    import clr  # pythonnet

    clr.AddReference(os.path.join(root, "ZOSAPI_NetHelper.dll"))
    import ZOSAPI_NetHelper

    if not ZOSAPI_NetHelper.ZOSAPI_Initializer.Initialize(root):
        raise SaosError(ErrorCode.ZOS_ERROR, f"ZOSAPI_Initializer failed: {root}")
    clr.AddReference(os.path.join(root, "ZOSAPI.dll"))
    clr.AddReference(os.path.join(root, "ZOSAPI_Interfaces.dll"))
    import ZOSAPI

    _ZOSAPI = ZOSAPI
    return ZOSAPI


def _to_local(tf, p):
    """全局点换到变换 tf（行优先旋转、平移）的局部坐标。/ A global point in the local frame of tf."""
    rot, off = tf
    d = [p[0] - off[0], p[1] - off[1], p[2] - off[2]]
    return [rot[0] * d[0] + rot[3] * d[1] + rot[6] * d[2], rot[1] * d[0] + rot[4] * d[1] + rot[7] * d[2],
            rot[2] * d[0] + rot[5] * d[1] + rot[8] * d[2]]


def _between(a, q, b) -> bool:
    """q 在 a→b 方向上的投影落在两端之间。/ q projects strictly between a and b along a→b."""
    d = [b[i] - a[i] for i in range(3)]
    n = sum(v * v for v in d)
    return n > 0 and 0 < sum((q[i] - a[i]) * d[i] for i in range(3)) / n < 1


# 梯度折射率面（Gradient 1…12、Gradium、Grid Gradient）和布局里 GRIN 介质内的分段数
GRIN_TYPE_RE = re.compile(r"gradi|grin", re.IGNORECASE)
GRIN_SLICES = 8


class ZosBackend(Backend):
    def __init__(self, mode: str = "standalone"):
        if mode not in ("standalone", "extension"):
            raise SaosError(ErrorCode.BAD_ARGS, f"unknown connection mode: {mode}")
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
        hint = "in OpticStudio click Programming → Interactive Extension so that it waits for a connection" if self.mode == "extension" else ""
        if app is None:
            raise SaosError(ErrorCode.ZOS_ERROR, f"cannot connect to OpticStudio ({self.mode}). {hint}")
        if self.mode == "extension" and not self._extension_ready(app):
            # 实测 2024 R1：上一个扩展客户端断开后，窗口退出扩展状态，ConnectAsExtension 仍返回对象，
            # 但没有 PrimarySystem，许可检查为 False，ProgramDir 抛 ArgumentNullException
            raise SaosError(ErrorCode.ZOS_ERROR, f"the OpticStudio window is not in Interactive Extension mode. {hint}; this is needed again after every daemon exit")
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
            raise SaosError(ErrorCode.ZOS_LICENSE, "the license does not allow ZOS-API", info)
        if version != config.EXPECTED_ZOS_VERSION:
            self.close()
            expected = ".".join(map(str, config.EXPECTED_ZOS_VERSION))
            raise SaosError(ErrorCode.ZOS_VERSION_MISMATCH, f"OpticStudio version is {info['version']}; the project is locked to {expected}", info)
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
            raise SaosError(ErrorCode.ZOS_CONNECTION_LOST, "not connected to OpticStudio")
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

    # ---- 显性调试的观察窗口 ----

    # 布局窗口（System Viewer）的几种：剖面图、3D 视图、实体模型；Lens Data 编辑器总是打开
    VIEWERS = {"cross-section": "Draw2D", "3d": "Draw3D", "shaded": "ShadedModel"}
    DEFAULT_WATCH = {"viewer": "cross-section", "analysis": None, "settings": {}}

    def _require_extension(self) -> None:
        if self.mode != "extension":
            raise SaosError(ErrorCode.BAD_ARGS, "explicit debugging windows need extension mode (saos daemon start --mode extension)")

    def _open_analyses(self) -> dict[str, Any]:
        system = self.system
        found = {}
        for i in range(1, system.Analyses.NumberOfAnalyses + 1):
            analysis = system.Analyses.Get_AnalysisAtIndex(i)
            found.setdefault(str(analysis.AnalysisType), analysis)
        return found

    def _cross_section_blocker(self) -> str | None:
        """剖面图只能画序列、轴对称的系统，不能画时返回原因。
        实测 2024 R1：折转的光纤模型导出剖面图报 "Cannot perform 2D Layout on non-axial system!"。"""
        system = self.system
        if str(system.Mode) != "Sequential":
            return "non-sequential system"
        if bool(system.IsNonAxial):
            return "non-axial system (tilts, decenters or non-rotational surfaces)"
        return None

    @staticmethod
    def _failure(message) -> str | None:
        """ApplyAndWaitForCompletion 返回的 IMessage：不是 Success 时返回其内容。"""
        if message is None:
            return None
        try:
            code = str(message.ErrorCode)
        except Exception:  # noqa: BLE001 - 读不到错误码时按成功处理，窗口内容仍可见
            return None
        return None if code == "Success" else f"{code}: {message.Text}"

    def _show_layout(self, wanted: str, opened: dict[str, Any], zosapi) -> dict[str, Any]:
        """打开布局窗口；要剖面图而系统画不了时，关掉剖面图窗口，改开 3D 视图。"""
        from saos.backend import analysis as an

        system = self.system
        reason = self._cross_section_blocker() if wanted == "cross-section" else None
        viewer = "3d" if reason else wanted
        new = []
        for _ in range(2):
            name = self.VIEWERS[viewer]
            run = opened.get(name)
            if run is None:
                run = system.Analyses.New_Analysis(an.idm(zosapi, name))
                opened[name] = run
                new.append(name)
            failure = self._failure(run.ApplyAndWaitForCompletion())
            if failure is None or viewer != "cross-section":
                break
            reason, viewer = failure, "3d"
        if viewer != "cross-section" and reason and "Draw2D" in opened:
            opened.pop("Draw2D").Close()
            new = [n for n in new if n != "Draw2D"]
        return {"viewer": viewer, "fallback_reason": reason, "new": new}

    def show_windows(self, viewer: str | None = None, analysis: str | None = None, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        """显性调试：总是打开 Lens Data 和一个布局窗口（默认剖面图，画不了时用 3D 视图），按需再开一个像质分析；
        已打开的复用，关掉的重新打开，并按当前镜头刷新。"""
        from saos.backend import analysis as an

        self._require_extension()
        zosapi = _load_zosapi(self.root)
        # 先在副本上改并校验，出错时保留上一次的选择 / change a copy and validate it, so an error keeps the previous choice
        watch = dict(getattr(self, "watch", self.DEFAULT_WATCH))
        if viewer is not None:
            if viewer not in self.VIEWERS:
                raise SaosError(ErrorCode.BAD_ARGS, f"viewer must be one of {list(self.VIEWERS)}")
            watch["viewer"] = viewer
        if analysis is not None:
            an.idm(zosapi, analysis)
            watch["analysis"], watch["settings"] = analysis, dict(settings or {})
        elif settings:
            watch["settings"] = dict(settings)
        self.watch = watch
        system = self.system
        self.app.ShowChangesInUI = True
        opened = self._open_analyses()
        if str(system.Mode) == "Sequential":
            system.LDE.ShowLDE()
        else:
            system.NCE.ShowNCE()
        layout = self._show_layout(watch["viewer"], opened, zosapi)
        message = None
        if watch["analysis"]:
            run = opened.get(watch["analysis"])
            if run is None:
                run = system.Analyses.New_Analysis(an.idm(zosapi, watch["analysis"]))
                layout["new"].append(watch["analysis"])
            an.apply_settings(an.settings_of(run), watch["settings"])
            message = run.ApplyAndWaitForCompletion()
        # 只让 OpticStudio 自动更新编辑器；分析窗口由 saos 在每次改动后显式刷新。
        # 实测 AllWindows 会在改动后异步重算分析窗口，与 API 读取竞争：读到 None，或与等待循环互相卡住
        system.UpdateMode = zosapi.LensUpdateMode.EditorsOnly
        self.app.ProgressMessage = f"saos: lens data + {layout['viewer']}" + (f" + {watch['analysis']}" if watch["analysis"] else "")
        return {"watch": watch, "layout": {k: layout[k] for k in ("viewer", "fallback_reason")}, "opened": layout["new"],
                "message": str(message) if message else None, "update_mode": str(system.UpdateMode), "windows": self.window_list()}

    def window_list(self) -> list[dict[str, str]]:
        return [{"type": str(a.AnalysisType), "title": str(a.Title)} for a in self._open_analyses().values()]

    def refresh_windows(self) -> dict[str, Any] | None:
        """改动镜头后刷新已打开的分析窗口；用户关掉的窗口不重开，下次 lens view 时再打开。"""
        if self.mode != "extension" or self.app is None:
            return None
        refreshed, failed = [], []
        for name, run in self._open_analyses().items():
            try:
                run.ApplyAndWaitForCompletion()
                refreshed.append(name)
            except Exception as exc:  # noqa: BLE001 - 窗口可能刚被用户关闭
                failed.append({"type": name, "error": str(exc)[:200]})
        return {"refreshed": refreshed, "failed": failed}

    def blocking_dialogs(self) -> list[str]:
        """显性调试：挡住 OpticStudio 的模态对话框。被挡住时分析会停在 Working 0%，请求会一直挂起。"""
        if self.mode != "extension" or not (self.extension_ui or {}).get("pid"):
            return []
        from saos import native_ui

        return native_ui.blocking_dialogs(int(self.extension_ui["pid"]))

    def screenshots(self, folder: Path, prefix: str) -> list[str]:
        from saos import native_ui

        self._require_extension()
        pid = (self.extension_ui or {}).get("pid")
        if not pid:
            raise SaosError(ErrorCode.ZOS_ERROR, "unknown OpticStudio window process")
        return native_ui.capture_windows(int(pid), folder, prefix)

    # ---- 文件 ----

    def load(self, path: Path) -> None:
        path = Path(path).resolve()
        if not self.system.LoadFile(str(path), False):
            raise SaosError(ErrorCode.ZOS_LOAD_FAILED, f"OpticStudio cannot load {path}")
        self._material_cache.clear()

    def save_as(self, path: Path) -> None:
        path = Path(path).resolve()
        self.system.SaveAs(str(path))
        if not path.is_file():
            raise SaosError(ErrorCode.ZOS_SAVE_FAILED, f"file not found after saving: {path}")

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

    def describe_model(self, folder: Path | None, prefix: str = "layout") -> dict[str, Any]:
        """建模说明：系统设置、逐面参数（含所有非空的 Par 列）、材料及各波长折射率（INDX 实测）、孔径、求解、顶点全局位置和布局图。"""
        zosapi = _load_zosapi(self.root)
        system = self.system
        sd = system.SystemData
        lde = system.LDE
        fields = sd.Fields
        waves = sd.Wavelengths
        n_waves = int(waves.NumberOfWavelengths)
        catalogs = self._catalogs_in_use()
        info = {
            "file": str(system.SystemFile),
            "title": str(sd.TitleNotes.Title),
            "lens_units": str(sd.Units.LensUnits),
            "aperture": {
                "type": str(sd.Aperture.ApertureType),
                "value": float(sd.Aperture.ApertureValue),
                "telecentric_object": bool(sd.Aperture.TelecentricObjectSpace),
                "afocal_image": bool(sd.Aperture.AFocalImageSpace),
            },
            "ray_aiming": str(sd.RayAiming.RayAiming),
            "fields": {
                "type": str(fields.GetFieldType()),
                "normalization": str(fields.Normalization),
                "items": [
                    {"x": float(f.X), "y": float(f.Y), "weight": float(f.Weight),
                     "vignetting": {k: float(getattr(f, k)) for k in ("VDX", "VDY", "VCX", "VCY", "VAN")}}
                    for f in (fields.GetField(i) for i in range(1, fields.NumberOfFields + 1))
                ],
            },
            "wavelengths": [
                {"um": float(w.Wavelength), "weight": float(w.Weight), "primary": bool(w.IsPrimary)}
                for w in (waves.GetWavelength(i) for i in range(1, n_waves + 1))
            ],
            "material_catalogs": catalogs,
            "stop_surface": int(lde.StopSurface),
        }
        mc = sd.MaterialCatalogs
        members = {c: {str(m).upper() for m in mc.GetMaterialsInCatalog(c)} for c in catalogs}
        columns = zosapi.Editors.LDE.SurfaceColumn
        surfaces = []
        for i in range(lde.NumberOfSurfaces):
            row = lde.GetSurfaceAt(i)
            item = self._surface_dict(row, i)
            item["material_source"] = self._material_source(row, members)
            # INDX 给出该面之后介质在各波长的折射率
            item["n"] = [self.operand("INDX", i, w, []) for w in range(1, n_waves + 1)]
            item["aperture"] = self._aperture_dict(row)
            item["solve_detail"] = {
                name: self._solve_dict(getattr(row, attr).GetSolveData())
                for name, attr in (("radius", "RadiusCell"), ("thickness", "ThicknessCell"), ("conic", "ConicCell"),
                                   ("semi_diameter", "SemiDiameterCell"), ("material", "MaterialCell"))
            }
            params = []
            for n in range(1, 255):
                col = getattr(columns, f"Par{n}", None)
                if col is None:
                    break
                try:
                    cell = row.GetSurfaceCell(col)
                    header = str(cell.Header)
                except Exception:  # noqa: BLE001 - 超出该面型的列
                    continue
                if not header or "unused" in header.lower():
                    continue
                kind = str(cell.DataType)
                value: Any = int(cell.IntegerValue) if kind == "Integer" else float(cell.DoubleValue) if kind == "Double" else str(cell.Value)
                if value not in (0, 0.0, ""):
                    params.append({"par": n, "name": header, "value": value, "solve": str(cell.GetSolveData().Type)})
            item["params"] = params
            item["tilt_decenter"] = self._tilt_decenter(row)
            m = lde.GetGlobalMatrix(i, *([0.0] * 12))
            item["global_vertex"] = [float(v) for v in m[10:13]] if m[0] else None
            surfaces.append(item)
        info["surfaces"] = surfaces
        info["layout"] = {} if folder is None else self._export_layouts(folder, prefix)
        return info

    def _material_source(self, row, members: dict[str, set[str]]) -> dict[str, Any]:
        solve = row.MaterialCell.GetSolveData()
        if str(solve.Type) == "MaterialModel":
            data = solve._S_MaterialModel
            return {"kind": "model", "nd": float(data.IndexNd), "vd": float(data.AbbeVd), "dpgf": float(data.dPgF)}
        name = str(row.Material).upper()
        if name == "-":  # 坐标断点不定义介质，沿用前一个面之后的介质
            return {"kind": "inherit"}
        if not name:
            return {"kind": "none"}
        if name == "MIRROR":
            return {"kind": "mirror"}
        return {"kind": "catalog", "name": name, "catalogs": [c for c, names in members.items() if name in names]}

    @staticmethod
    def _tilt_decenter(row) -> dict[str, Any]:
        """面属性里的倾斜与偏心（前、后）；全为零时返回空字典。
        Tilt and decenter in the surface properties (before and after); empty when all are zero."""
        out: dict[str, Any] = {}
        try:
            td = row.TiltDecenterData
        except Exception:  # noqa: BLE001 - 该面型没有这一项 / not available for this surface type
            return out
        for side in ("Before", "After"):
            for key in ("DecenterX", "DecenterY", "TiltX", "TiltY", "TiltZ"):
                try:
                    v = float(getattr(td, f"{side}Surface{key}"))
                except Exception:  # noqa: BLE001
                    continue
                if v:
                    out[f"{side.lower()}_{key}"] = v
        # 后置倾斜偏心的模式（显式/拾取/反向）和坐标返回同样决定布局 / the after-surface mode and coordinate return also fix the layout
        for key, attr, default in (("after_mode", "AfterSurfaceMode", "Explicit"), ("coordinate_return", "CoordinateReturn", "None")):
            try:
                value = str(getattr(td, attr))
            except Exception:  # noqa: BLE001
                continue
            if value not in ("", default) and (key != "after_mode" or out):
                out[key] = value
        return out

    @staticmethod
    def _aperture_dict(row) -> dict[str, Any]:
        data = row.ApertureData
        out: dict[str, Any] = {"type": str(data.CurrentType)}
        settings = data.CurrentTypeSettings
        for attr in ("_S_CircularAperture", "_S_CircularObscuration", "_S_RectangularAperture", "_S_RectangularObscuration",
                     "_S_EllipticalAperture", "_S_EllipticalObscuration"):
            try:
                s = getattr(settings, attr)
            except Exception:  # noqa: BLE001 - 不是这种孔径
                continue
            if s is None:
                continue
            for key in ("MinimumRadius", "MaximumRadius", "XHalfWidth", "YHalfWidth", "ApertureXDecenter", "ApertureYDecenter"):
                try:
                    out[key] = float(getattr(s, key))
                except Exception:  # noqa: BLE001 - 该孔径类型没有这个量
                    pass
            break
        if bool(data.IsPickedUp):
            out["picked_up_from"] = int(data.PickupFrom)
        return out

    @staticmethod
    def _solve_dict(solve) -> dict[str, Any]:
        out: dict[str, Any] = {"type": str(solve.Type)}
        if out["type"] == "SurfacePickup":
            p = solve._S_SurfacePickup
            out.update(surface=int(p.Surface), scale=float(p.ScaleFactor), offset=float(p.Offset))
        return out

    # 3D 布局的相机角度（度，绕 X、Y、Z）：默认视角看 YZ 平面；折转系统的光路常在 XZ 平面，另给俯视和斜视
    LAYOUT_VIEWS = {"3d-yz": (0.0, 0.0, 0.0), "3d-xz": (90.0, 0.0, 0.0), "3d-oblique": (30.0, 30.0, 0.0)}

    def _export_layouts(self, folder: Path, prefix: str) -> dict[str, Any]:
        """3D 布局（几个视角）和 YZ 截面导出为 PNG；某一种失败时记录原因，不影响其余内容。"""
        folder.mkdir(parents=True, exist_ok=True)
        lay = self.system.Tools.Layouts
        jobs_: list[tuple[str, Any, tuple[float, float, float] | None]] = [(k, lay.Open3DViewerExport, v) for k, v in self.LAYOUT_VIEWS.items()]
        jobs_.append(("cross-section", lay.OpenCrossSectionExport, None))
        out: dict[str, Any] = {}
        for key, opener, angles in jobs_:
            target = folder / f"{prefix}-{key}.png"
            tool = None
            try:
                tool = opener()
                tool.OutputFileName = str(target)
                tool.SaveImageAsFile = True
                tool.OutputPixelWidth, tool.OutputPixelHeight = 1600, 1000
                if angles is not None:
                    tool.CameraViewpointAngleX, tool.CameraViewpointAngleY, tool.CameraViewpointAngleZ = angles
                tool.RunAndWaitForCompletion()
                out[key] = target.name if target.is_file() else {"error": str(tool.ErrorMessage) or "no image written"}
            except Exception as exc:  # noqa: BLE001 - 布局图只是辅助，失败不影响其余内容
                out[key] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
            finally:
                if tool is not None:
                    tool.Close()
        return out

    # ---- 修改 ----

    _ATTR = {
        "radius": "Radius",
        "thickness": "Thickness",
        "conic": "Conic",
        "semi_diameter": "SemiDiameter",
        "material": "Material",
        "comment": "Comment",
    }

    def _row(self, index: Any):
        lde = self.system.LDE
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < lde.NumberOfSurfaces:
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {index!r} out of range 0–{lde.NumberOfSurfaces - 1}")
        return lde.GetSurfaceAt(index)

    def _par_cell(self, row, index: int, param: str):
        """面型参数列 par<N>；该面型没有这一列时报错，不写进无效单元格。"""
        n = int(PAR_RE.match(param).group(1))
        col = getattr(_load_zosapi(self.root).Editors.LDE.SurfaceColumn, f"Par{n}", None)
        cell = None
        if col is not None:
            try:
                cell = row.GetSurfaceCell(col)
            except Exception:  # noqa: BLE001 - 超出该面型的列 / beyond this surface type's columns
                cell = None
        header = str(cell.Header) if cell is not None else ""
        if cell is None or not header or "unused" in header.lower() or str(cell.DataType) != "Double":
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {index} ({row.TypeName}) has no numeric parameter {n}")
        return cell

    def surface_value(self, index: int, param: str) -> Any:
        if is_par(param):
            return float(self._par_cell(self._row(index), index, param).DoubleValue)
        return super().surface_value(index, param)

    def set_surface(self, index: int, param: str, value: Any) -> dict[str, Any]:
        check_param(param)
        surf = self._row(index)
        if is_par(param):
            cell = self._par_cell(surf, index, param)
            target = parse_numeric(param, value)
            cell.DoubleValue = target
            if not same_number(float(cell.DoubleValue), target):
                raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"surface {index} {param} did not take the requested value; a solve may override it",
                                {"requested": target, "actual": float(cell.DoubleValue), "solve": str(cell.GetSolveData().Type)})
            return {**self._surface_dict(surf, index), param: float(cell.DoubleValue), "header": str(cell.Header)}
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
                f"surface {index} {param} did not take the requested value; a solve or automatic setting may override it",
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
                f"{name!r} is not in the catalogs in use",
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
            raise SaosError(ErrorCode.BAD_ARGS, f"cannot load glass catalogs {missing}")

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
            self._set_material(obj, p["object"]["material"], "object surface")
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
                    self._set_material(row, s["material"], f"surface {k}")
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

    def _glass_constants(self, name: str) -> tuple[float, float]:
        """目录玻璃的 nd、νd，用作模型玻璃的起点。/ nd and Vd of a catalog glass, the start of a model glass."""
        mc = self.system.SystemData.MaterialCatalogs
        catalog = next((c for c in self._catalogs_in_use() if name.upper() in {str(m).upper() for m in mc.GetMaterialsInCatalog(c)}), None)
        if catalog is None:
            raise SaosError(ErrorCode.BAD_ARGS, f"{name} is not in the catalogs in use, so it has no nd / Vd to start a model glass from")
        tool = self.system.Tools.OpenMaterialsCatalog()
        try:
            tool.SelectedCatalog = catalog
            tool.SelectedMaterial = name
            return float(tool.Nd), float(tool.Vd)
        finally:
            tool.Close()

    def _vary_material(self, row, surface: int, on: bool, mode: str | None, catalog: str | None) -> str:
        zosapi = _load_zosapi(self.root)
        cell = row.MaterialCell
        solve = cell.GetSolveData()
        is_model = str(solve.Type) == "MaterialModel"
        name = str(row.Material).upper()
        if not on:
            if is_model:
                # 模型玻璃保留当前 nd、νd，只是不再变化 / a model glass keeps its nd and Vd, they just stop varying
                solve._S_MaterialModel.VaryIndex = False
                solve._S_MaterialModel.VaryAbbe = False
                cell.SetSolveData(solve)
            else:
                cell.MakeSolveFixed()
            return str(cell.GetSolveData().Type)
        if mode not in MATERIAL_MODES:
            raise SaosError(ErrorCode.BAD_ARGS, f"material needs --mode {' or '.join(MATERIAL_MODES)}")
        if not is_model and name in ("", "MIRROR", "-"):
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {surface} has no glass ({name or 'air'}); set a material before making it vary")
        if mode == "model":
            nd, vd = (float(solve._S_MaterialModel.IndexNd), float(solve._S_MaterialModel.AbbeVd)) if is_model else self._glass_constants(name)
            new = cell.CreateSolveType(zosapi.Editors.SolveType.MaterialModel)
            m = new._S_MaterialModel
            m.IndexNd, m.AbbeVd, m.VaryIndex, m.VaryAbbe = nd, vd, True, True
        else:
            new = cell.CreateSolveType(zosapi.Editors.SolveType.MaterialSubstitute)
            # 空目录名表示在当前使用的全部目录中替换 / an empty catalog substitutes within every catalog in use
            new._S_MaterialSubstitute.Catalog = str(catalog or "")
        cell.SetSolveData(new)
        return str(cell.GetSolveData().Type)

    def vary(self, surface: int, param: str, on: bool, mode: str | None = None, catalog: str | None = None) -> str:
        if param not in self._CELLS and param != "material" and not is_par(param):
            raise SaosError(ErrorCode.BAD_ARGS, f"only {', '.join(self._CELLS)}, par<N> and material can be variables")
        lde = self.system.LDE
        row = self._row(surface)
        # 物面只允许厚度（无盖玻片时即工作距离间隔）设为变量
        allowed = 0 < surface < lde.NumberOfSurfaces - 1 or (surface == 0 and param == "thickness")
        if not allowed:
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {surface} {param} cannot be a variable")
        if param == "material":
            return self._vary_material(row, surface, on, mode, catalog)
        cell = self._par_cell(row, surface, param) if is_par(param) else getattr(row, self._CELLS[param])
        cell.MakeSolveVariable() if on else cell.MakeSolveFixed()
        return str(cell.GetSolveData().Type)

    def merit_wizard(self, settings: dict[str, Any]) -> int:
        wiz = self.system.MFE.SEQOptimizationWizard

        def pick(name: str, count_attr: str, getter: str, wanted: str) -> int:
            names = [str(getattr(wiz, getter)(i)) for i in range(getattr(wiz, count_attr))]
            for i, n in enumerate(names):
                if n.replace(" ", "").lower() == wanted.replace(" ", "").lower():
                    return i
            raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard {name} must be one of {names}, got {wanted!r}")

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
                raise SaosError(ErrorCode.BAD_ARGS, f"unknown operand {spec['type']!r}")
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

    def optimize(self, cycles: str, cores: int | None = None, *, max_seconds: float | None = None,
                 on_progress=None, should_cancel=None) -> dict[str, Any]:
        """异步运行局部优化：边跑边报告进度，超时或收到取消请求时调用 Cancel，保留已经得到的结果。
        Run the local optimisation asynchronously: report progress while it runs, call Cancel on a timeout or a cancel
        request, and keep what it reached."""
        zosapi = _load_zosapi(self.root)
        names = {"auto": "Automatic", "1": "Fixed_1_Cycle", "5": "Fixed_5_Cycles", "10": "Fixed_10_Cycles", "50": "Fixed_50_Cycles"}
        if cycles not in names:
            raise SaosError(ErrorCode.BAD_ARGS, f"cycles must be one of {list(names)}")
        tool = self.system.Tools.OpenLocalOptimization()
        try:
            tool.Algorithm = zosapi.Tools.Optimization.OptimizationAlgorithm.DampedLeastSquares
            tool.Cycles = getattr(zosapi.Tools.Optimization.OptimizationCycles, names[cycles])
            if cores:
                tool.NumberOfCores = int(cores)
            started = time.monotonic()
            stopped_by = None
            tool.Run()
            while tool.IsRunning:
                time.sleep(0.5)
                elapsed = time.monotonic() - started
                if on_progress is not None:
                    on_progress({"progress": int(tool.Progress), "merit": float(tool.CurrentMeritFunction), "elapsed_s": round(elapsed, 1)})
                if stopped_by is None and should_cancel is not None and should_cancel():
                    stopped_by = "cancel"
                elif stopped_by is None and max_seconds is not None and elapsed > max_seconds:
                    stopped_by = "timeout"
                if stopped_by is not None and tool.CanCancel:
                    tool.Cancel()
            tool.WaitForCompletion()
            return {
                "initial_merit": float(tool.InitialMeritFunction),
                "final_merit": float(tool.CurrentMeritFunction),
                "variables": int(tool.Variables),
                "targets": int(tool.Targets),
                "succeeded": bool(tool.Succeeded),
                "stopped_by": stopped_by,
                "elapsed_s": round(time.monotonic() - started, 1),
            }
        finally:
            tool.Close()

    def quick_focus(self) -> dict[str, Any]:
        """OpticStudio 的 Quick Focus：按 RMS 光斑半径（质心参考）调整像面前一面的厚度。
        OpticStudio Quick Focus: adjust the thickness before the image surface for the smallest RMS spot radius (centroid)."""
        zosapi = _load_zosapi(self.root)
        surface = self.system.LDE.GetSurfaceAt(self.system.LDE.NumberOfSurfaces - 2)
        before = float(surface.Thickness)
        tool = self.system.Tools.OpenQuickFocus()
        try:
            tool.Criterion = zosapi.Tools.General.QuickFocusCriterion.SpotSizeRadial
            tool.UseCentroid = True
            tool.RunAndWaitForCompletion()
        finally:
            tool.Close()
        return {"surface": self.system.LDE.NumberOfSurfaces - 2, "thickness_before": before, "thickness_after": float(surface.Thickness)}

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

    # ---- 每轮的可视化数据（saos ui 只读这些导出，不自己算光学量）----

    def _batch(self, rays: list[tuple[int, float, float, float, float]], to_surface: int, system=None) -> list[tuple]:
        """归一化坐标的批量实际光线追迹，返回每条光线在 to_surface 局部坐标中的 (error, vignette, x, y, z, l, m, n)。
        system 省略时用主系统。"""
        rt = _load_zosapi(self.root).Tools.RayTrace
        tool = (system or self.system).Tools.OpenBatchRayTrace()
        try:
            batch = tool.CreateNormUnpol(len(rays), rt.RaysType.Real, to_surface)
            none_mode = getattr(rt.OPDMode, "None")
            for wave, hx, hy, px, py in rays:
                batch.AddRay(int(wave), float(hx), float(hy), float(px), float(py), none_mode)
            tool.RunAndWaitForCompletion()
            batch.StartReadingResults()
            out = []
            for _ in rays:
                r = batch.ReadNextResult(0, 0, 0, *([0.0] * 11))
                out.append((int(r[2]), int(r[3]), *(float(v) for v in r[4:10])))
            return out
        finally:
            tool.Close()

    def _global(self, index: int, system=None) -> tuple[list[float], list[float]] | None:
        """面 index 的局部→全局变换：3×3 旋转（行优先）和平移。"""
        m = (system or self.system).LDE.GetGlobalMatrix(index, *([0.0] * 12))
        if not m[0]:
            return None
        return [float(v) for v in m[1:10]], [float(v) for v in m[10:13]]

    def _sag(self, index: int, x: float, y: float) -> float | None:
        try:
            r = self.system.LDE.GetSag(index, x, y, 0.0, 0.0)
        except Exception:  # noqa: BLE001 - 读不到面形时退回平面画法
            return None
        return float(r[1]) if r[0] and math.isfinite(float(r[1])) else None

    def _grin_points(self, rays, image: int, apply, errors: dict[str, str]) -> dict[int, dict[int, list[list[float]]]]:
        """梯度折射率介质里光线是弯的，只连面交点会画成直线（2026-10-17 实测 1.13 mm 的 GRIN 棒内偏差达 0.42 mm）。
        在系统副本上把 GRIN 面的厚度依次缩短，追迹到下一面：介质定义不变，交点就是真实路径上的点。全局坐标参考面在 GRIN
        之后时，缩短厚度挪动的是前面的面，所以交点先换到副本里 GRIN 面的局部坐标，再用主系统里该面的变换换回全局。
        返回 {GRIN 面: {光线序号: [全局点, …]}}，主系统不动。
        Rays bend inside a gradient-index medium; joining surface intercepts draws them straight (a 1.13 mm GRIN rod was off
        by up to 0.42 mm, measured 2026-10-17). On a copy of the system the GRIN thickness is shortened step by step and the
        rays traced to the next surface: the medium is unchanged, so the intercepts lie on the real path. When the global
        reference surface lies after the GRIN, shortening moves the surfaces before it, so the intercepts go through the GRIN
        surface's local frame of the copy and back to global with that surface's transform in the main system."""
        lde = self.system.LDE
        grins = []
        for g in range(1, image):
            row = lde.GetSurfaceAt(g)
            if GRIN_TYPE_RE.search(str(row.TypeName)) and float(row.Thickness) != 0.0:
                grins.append(g)
        if not grins:
            return {}
        out: dict[int, dict[int, list[list[float]]]] = {}
        copy = self.system.CopySystem()
        try:
            for g in grins:
                row = copy.LDE.GetSurfaceAt(g)
                thickness = float(row.Thickness)
                try:
                    row.ThicknessCell.MakeSolveFixed()
                except Exception:  # noqa: BLE001 - 没有求解时无需处理 / nothing to fix
                    pass
                main = self._global(g)
                # 从 GRIN 前表面最凸处切起：更薄的切片落在光线入射点之前 / start beyond the front face's sag
                sd = float(lde.GetSurfaceAt(g).SemiDiameter)
                sags = [v for v in (self._sag(g, 0.0, y) for y in (-sd, -sd / 2, sd / 2, sd)) if v is not None]
                lo = (max([0.0, *sags]) if thickness > 0 else min([0.0, *sags])) if sags else 0.0
                if abs(lo) >= abs(thickness):
                    lo = 0.0
                points: dict[int, list[list[float]]] = {k: [] for k in range(len(rays))}
                for step in range(1, GRIN_SLICES):
                    row.Thickness = lo + (thickness - lo) * step / GRIN_SLICES
                    tf, frame = self._global(g + 1, copy), self._global(g, copy)
                    if tf is None or frame is None or main is None:
                        break
                    for k, (err, _vig, *xyz_lmn) in enumerate(self._batch([r[1:] for r in rays], g + 1, copy)):
                        if not err:
                            points[k].append(apply(main, _to_local(frame, apply(tf, xyz_lmn[:3]))))
                out[g] = points
        except Exception as exc:  # noqa: BLE001 - 退回直线画法 / fall back to straight segments
            errors["grin"] = f"GRIN ray paths unavailable ({type(exc).__name__}: {exc}); drawn straight"[:300]
        finally:
            copy.Close(False)
        return out

    def interference(self) -> dict[str, Any]:
        """结构交叠检查（saos.geometry）：面相交、镜片穿插。/ Interference check (saos.geometry): crossing surfaces, interpenetrating lenses."""
        lde = self.system.LDE
        return geometry.interference(self._geometry_items(range(1, lde.NumberOfSurfaces - 1), {}))

    def _geometry_items(self, indices, transforms: dict[int, Any]) -> list[dict[str, Any]]:
        lde = self.system.LDE
        items = []
        for i in indices:
            row = lde.GetSurfaceAt(i)
            tf = transforms[i] if i in transforms else self._global(i)
            item = {"index": i, "type": str(row.TypeName), "comment": str(row.Comment), "material": str(row.Material), "sd": float(row.SemiDiameter)}
            if tf is not None and item["sd"] > 0 and "Coordinate" not in item["type"]:
                ap = geometry.aperture(self._aperture_dict(row), item["sd"])
                grid = geometry.polar_points(geometry.extent(ap))
                sags = geometry.fill_outside(grid, [self._sag(i, x, y) for x, y in grid], ap)
                item.update(sd=geometry.extent(ap), ap=ap, rot=tf[0], off=tf[1], sags=sags)
            items.append(item)
        return items

    def viz_data(self, folder: Path | None, *, pupil_grid: int = 31, fan: int = 7, full: bool = True, rays: bool = True) -> dict[str, Any]:
        """saos ui 用的数据：逐面全局几何、布局光线、Lens Data、交叠检查；full 时另有点列图光线、点列图 RMS/GEO 和 FFT MTF
        （每轮的 viz.json）。不带 full 是改动镜头后给界面的当前视图。
        几何与光线坐标都来自 ZOS-API（GetGlobalMatrix、GetSag、批量光线追迹），这里只做坐标变换。
        Data for saos ui: global geometry, layout rays, Lens Data and the interference check; with full also spot rays,
        Standard Spot RMS/GEO and FFT MTF (a round's viz.json). Without full it is the live view after a lens change.
        rays=False 时不追迹布局光线：优化运行中工具被优化器占着，只能读几何（同一系统同时只能开一个工具）。
        rays=False skips the layout rays: while an optimisation runs its tool is open and only geometry can be read."""
        system = self.system
        lde = system.LDE
        info = self.system_info()
        image = lde.NumberOfSurfaces - 1
        waves = info["wavelengths"]
        primary = next((k for k, w in enumerate(waves, start=1) if w["primary"]), 1)
        fields = info["fields"]["items"]
        try:
            normalization = str(system.SystemData.Fields.Normalization)
        except Exception:  # noqa: BLE001
            normalization = "Radial"
        if normalization == "Rectangular":
            mx, my = max(abs(f["x"]) for f in fields), max(abs(f["y"]) for f in fields)
            norm = [(f["x"] / mx if mx else 0.0, f["y"] / my if my else 0.0) for f in fields]
        else:
            r = max(math.hypot(f["x"], f["y"]) for f in fields)
            norm = [(f["x"] / r if r else 0.0, f["y"] / r if r else 0.0) for f in fields]
        timings: dict[str, float] = {}
        errors: dict[str, str] = {}

        def apply(tf, p):
            rot, off = tf
            return [rot[0] * p[0] + rot[1] * p[1] + rot[2] * p[2] + off[0],
                    rot[3] * p[0] + rot[4] * p[1] + rot[5] * p[2] + off[1],
                    rot[6] * p[0] + rot[7] * p[1] + rot[8] * p[2] + off[2]]

        def rotate(tf, d):
            rot = tf[0]
            return [rot[0] * d[0] + rot[1] * d[1] + rot[2] * d[2], rot[3] * d[0] + rot[4] * d[1] + rot[5] * d[2],
                    rot[6] * d[0] + rot[7] * d[1] + rot[8] * d[2]]

        started = time.monotonic()
        transforms = {i: self._global(i) for i in range(1, image + 1)}
        surfaces = []
        for i in range(1, image + 1):
            row = lde.GetSurfaceAt(i)
            tf = transforms[i]
            item = {"index": i, "type": str(row.TypeName), "comment": str(row.Comment), "material": str(row.Material),
                    "semi_diameter": float(row.SemiDiameter), "is_stop": bool(row.IsStop)}
            sd = item["semi_diameter"]
            if tf is not None and sd > 0 and "Coordinate" not in item["type"]:
                item["vertex"] = tf[1]
                # 子午、弧矢两条截线和边缘一圈按面孔径取（矩形孔径的棱镜面画成方的），局部点 (x, y, sag) 变换到全局
                # sections and rim follow the surface aperture (a prism face with a rectangular aperture is drawn square)
                ap = geometry.aperture(self._aperture_dict(row), sd)
                if ap["explicit"]:
                    item["aperture"] = {k: ap[k] for k in ("shape", "hx", "hy", "dx", "dy")}
                y_lo, y_hi, x_at = geometry.chord(ap, "y")
                x_lo, x_hi, y_at = geometry.chord(ap, "x")
                line_y = [(x_at, y_lo + (y_hi - y_lo) * k / 20) for k in range(21)]
                line_x = [(x_lo + (x_hi - x_lo) * k / 20, y_at) for k in range(21)]
                rim = geometry.outline(ap, 32)
                sag_y = [self._sag(i, x, y) for x, y in line_y]
                sag_x = [self._sag(i, x, y) for x, y in line_x]
                sag_rim = [self._sag(i, x, y) for x, y in rim]
                if any(s is None for s in sag_y + sag_x + sag_rim):
                    errors.setdefault("sag", f"GetSag unavailable on surface {i}; drawn flat")
                item["profile_y"] = [apply(tf, (x, y, s or 0.0)) for (x, y), s in zip(line_y, sag_y)]
                item["profile_x"] = [apply(tf, (x, y, s or 0.0)) for (x, y), s in zip(line_x, sag_x)]
                item["rim"] = [apply(tf, (x, y, s or 0.0)) for (x, y), s in zip(rim, sag_rim)]
            surfaces.append(item)
        timings["geometry"] = round(time.monotonic() - started, 2)
        started = time.monotonic()
        clash = geometry.interference(self._geometry_items(range(1, image), transforms))
        timings["interference"] = round(time.monotonic() - started, 2)

        # 布局光线：每个视场一条子午扇和一条弧矢扇，主波长
        started = time.monotonic()
        steps = [2 * k / (fan - 1) - 1 for k in range(fan)]
        layout_rays = [(f, primary, hx, hy, 0.0, p) for f, (hx, hy) in enumerate(norm) for p in steps] + \
                      [(f, primary, hx, hy, p, 0.0) for f, (hx, hy) in enumerate(norm) for p in steps if p != 0.0]
        traced = {j: self._batch([r[1:] for r in layout_rays], j) for j in range(1, image + 1) if transforms[j] is not None} if rays else {}
        inner = self._grin_points(layout_rays, image, apply, errors) if rays else {}
        paths = []
        for k, (f, _, _, _, px, py) in enumerate(layout_rays):
            points, first_dir, pending = [], None, []
            for j in range(1, image + 1):
                if j not in traced:
                    continue
                err, vig, *xyz_lmn = traced[j][k]
                if err:
                    break
                here = apply(transforms[j], xyz_lmn[:3])
                if pending:
                    # 只留两端交点之间的点：弯的 GRIN 前表面上，厚度切得比矢高还薄时交点在入射点之前（虚延长）
                    # keep points between the two intercepts: a slice thinner than a curved front face's sag lands before the entry
                    points.extend(q for q in pending if _between(points[-1], q, here))
                points.append(here)
                if first_dir is None:
                    first_dir = rotate(transforms[j], xyz_lmn[3:])
                if vig and vig <= j:
                    break
                pending = inner.get(j, {}).get(k, [])
            if points:
                paths.append({"field": f, "px": px, "py": py, "points": points, "start_dir": first_dir})
        timings["layout_rays"] = round(time.monotonic() - started, 2)
        stop = next((s for s in surfaces if s["is_stop"]), None)
        base = {
            "system": {**{k: info[k] for k in ("title", "lens_units", "aperture", "fields", "wavelengths", "stop_surface")},
                       "primary_wave": primary, "normalization": normalization, "normalized_fields": norm,
                       "stop_semi_diameter": stop["semi_diameter"] if stop else None, "image_surface": image,
                       "non_axial": bool(system.IsNonAxial)},
            "surfaces": surfaces,
            "layout": {"wave": primary, "rays": paths, **({} if rays else {"rays_paused": True})},
            "lens_data": self.surfaces(),
            "interference": clash,
        }
        if not full:
            return {**base, "timings_s": timings, "errors": errors}

        # 点列图：方形网格取单位圆内的光瞳点，各视场各波长追到像面，相对主波长主光线
        started = time.monotonic()
        grid = [2 * k / (pupil_grid - 1) - 1 for k in range(pupil_grid)]
        pupil = [(px, py) for py in grid for px in grid if px * px + py * py <= 1.0]
        spot_rays, chief_rays = [], []
        for f, (hx, hy) in enumerate(norm):
            chief_rays.append((primary, hx, hy, 0.0, 0.0))
            spot_rays += [(w, hx, hy, px, py) for w in range(1, len(waves) + 1) for px, py in pupil]
        chief = self._batch(chief_rays, image)
        hits = self._batch(spot_rays, image)
        spots = []
        k = 0
        for f in range(len(norm)):
            cx, cy = chief[f][2], chief[f][3]
            per_wave = []
            for _ in waves:
                pts = []
                for _ in pupil:
                    err, vig, x, y = hits[k][:4]
                    k += 1
                    if not err and not vig:
                        pts.append([round(x - cx, 7), round(y - cy, 7)])
                per_wave.append(pts)
            spots.append({"field": f, "chief": [cx, cy] if not chief[f][0] else None, "points": per_wave})
        timings["spot_rays"] = round(time.monotonic() - started, 2)

        def run(name: str, wanted: dict[str, Any], keep: tuple[str, ...]) -> dict[str, Any] | None:
            started = time.monotonic()
            for settings in (wanted, {}):
                try:
                    out = self.analysis(name, settings, folder / f"{name}.txt")
                    timings[name] = round(time.monotonic() - started, 2)
                    return {"series": out["series"], "spot": out.get("spot"), "settings": {s: out["settings"].get(s) for s in keep}}
                except SaosError as exc:
                    if settings:
                        continue
                    errors[name] = exc.message
                except Exception as exc:  # noqa: BLE001 - 一个分析失败不影响其余数据
                    errors[name] = f"{type(exc).__name__}: {exc}"
                    break
            return None

        spot_analysis = run("StandardSpot", {"ReferTo": "ChiefRay"}, ("ReferTo", "Pattern", "RayDensity"))
        mtf = run("FftMtf", {"ShowDiffractionLimit": True}, ("MaximumFrequency", "SampleSize", "Type", "ShowDiffractionLimit"))
        spot_stats = (spot_analysis or {}).get("spot") or {}
        return {
            **base,
            "spot": {"reference": "chief ray, primary wavelength", "pupil_grid": pupil_grid, "fields": spots,
                     "rms_radius": spot_stats.get("rms_radius"), "geo_radius": spot_stats.get("geo_radius"),
                     "analysis_settings": (spot_analysis or {}).get("settings")},
            "mtf": mtf,
            "timings_s": timings,
            "errors": errors,
        }

    # ---- 操作数 ----

    def operand(self, name: str, int1: int, int2: int, data: list[float]) -> float:
        zosapi = _load_zosapi(self.root)
        op_type = getattr(zosapi.Editors.MFE.MeritOperandType, name.upper(), None)
        if op_type is None:
            raise SaosError(ErrorCode.BAD_ARGS, f"unknown operand {name!r}")
        if len(data) > 6:
            raise SaosError(ErrorCode.BAD_ARGS, "an operand takes at most 6 float parameters")
        padded = [float(x) for x in data] + [0.0] * (6 - len(data))
        return float(self.system.MFE.GetOperandValue(op_type, int(int1), int(int2), *padded))
