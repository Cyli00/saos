"""通过 pythonnet 调用 OpticStudio ZOS-API 的后端。

显式加载 config.opticstudio_dir() 下的 DLL，避免本机多个 OpticStudio 版本时加载错版本。
"""

from __future__ import annotations

import logging
import math
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from saos import config, geometry, glasscat
from saos.backend import nsc
from saos.backend.base import (
    MATERIAL_MODES,
    NUMERIC_PARAMS,
    PAR_RE,
    Backend,
    check_param,
    check_size,
    is_par,
    parse_numeric,
    same_number,
)
from saos.prescription import (
    AFOCAL_UNITS,
    APERTURES,
    APODIZATIONS,
    FIELDS,
    NORMALIZATIONS,
    ORDERS,
    POLARIZATION_METHODS,
    RAY_AIMING_FLAGS,
    SURFACE_APERTURES,
    UNITS,
)
from saos.protocol import ErrorCode, SaosError

log = logging.getLogger("saos.backend.zos")
_ZOSAPI = None
# 分析没有完整读出时，结果里附上文本导出的开头这么多字符，给诊断用 / text export head attached when an analysis was not fully read
TEXT_HEAD_CHARS = 3000


def _mech_semi_diameter(row) -> float | None:
    """Lens Data 的 Mech Semi-Dia；读不到时为 None，不让一列拖垮整张面表。/ Mech Semi-Dia; None when unreadable."""
    try:
        return float(row.MechanicalSemiDiameter)
    except Exception:  # noqa: BLE001
        return None


def _cell_solve(row, cell: str) -> str | None:
    try:
        return str(getattr(row, cell).GetSolveData().Type)
    except Exception:  # noqa: BLE001
        return None


def _read_text_export(path: Path) -> str:
    """OpticStudio 的文本导出通常是带 BOM 的 UTF-16。/ OpticStudio's text exports are usually UTF-16 with a BOM."""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace")
    return raw.decode("utf-8", errors="replace")


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


def par_cell(zosapi, row, index: int, param: str):
    """面型参数列 par<N>；该面型没有这一列、列标为 unused 或不是数值时报错，不读写无效单元格。后端和 saos.kit 共用。
    The par<N> cell of a row; refuses a column this surface type lacks, marks unused, or that holds no number. Shared with saos.kit."""
    n = int(PAR_RE.match(param).group(1))
    col = getattr(zosapi.Editors.LDE.SurfaceColumn, f"Par{n}", None)
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


def enum_member(enum, sample: str, wanted: str, label: str):
    """ZOS-API 枚举成员，按名字不分大小写匹配；没有时列出全部可选名。sample 是该枚举里确定存在的一个名字。
    A ZOS-API enum member by case-insensitive name; the error lists every name. sample is a name the enum surely has."""
    kind = type(getattr(enum, sample))
    names = [str(n) for n in kind.GetNames(kind)]
    for n in names:
        if n.lower() == wanted.lower():
            return getattr(enum, n)
    raise SaosError(ErrorCode.BAD_ARGS, f"{label}: OpticStudio has no {wanted!r}", {"choices": names})


def _usable_cell(zosapi, row, n: int):
    try:
        cell = row.GetSurfaceCell(getattr(zosapi.Editors.LDE.SurfaceColumn, f"Par{n}"))
    except Exception:  # noqa: BLE001 - 超出该面型的列 / beyond this surface type's columns
        return None
    header = str(cell.Header) if cell is not None else ""
    return cell if header and "unused" not in header.lower() else None


def param_cells(zosapi, row) -> dict[int, Any]:
    """该面型可用的参数列 {列号: 单元格}。扫一遍 254 列约 0.2 s（2026-10-19 验证实测），同一个面按标题查几项时复用。
    The usable parameter columns of the row's type; one scan of 254 columns takes about 0.2 s, so reuse it on one surface."""
    return {n: c for n in range(1, 255) if (c := _usable_cell(zosapi, row, n)) is not None}


def type_param_cell(zosapi, row, label: str, key: str, cells: dict[int, Any] | None = None):
    """面型参数单元格：par<N> 按列号，其余按 Lens Data 的列标题（不分大小写）；没有这一列或列标为 unused 时报错并列出该面型的参数。
    cells 是 param_cells 的结果，按标题查不到时重扫一次（写了项数之后会多出列）。
    A surface-type parameter cell by par<N> or by its Lens Data column header; the error lists the type's parameters.
    cells is a param_cells result, rescanned once on a miss (writing a term count adds columns)."""
    m = PAR_RE.match(key)
    if m:
        cell = _usable_cell(zosapi, row, int(m.group(1)))
        if cell is not None:
            return cell
    for attempt in (cells, None):
        if attempt is None:
            cells = param_cells(zosapi, row)
        if not m:
            for c in cells.values():
                if str(c.Header).strip().lower() == key.strip().lower():
                    return c
        if attempt is None:
            break
    raise SaosError(ErrorCode.BAD_ARGS, f"{label} ({row.TypeName}) has no parameter {key!r}",
                    {"parameters": {f"par{n}": str(c.Header) for n, c in cells.items()}})


def cell_value(cell) -> Any:
    """按数据类型读一个编辑器单元格。/ Read an editor cell by its data type."""
    kind = str(cell.DataType)
    return float(cell.DoubleValue) if kind == "Double" else int(cell.IntegerValue) if kind == "Integer" else str(cell.Value)


def set_cell(cell, value: Any, label: str) -> None:
    """按单元格的数据类型（Double、Integer、String）写值并回读核对。/ Write a value by the cell's data type and read it back."""
    if cell.IsReadOnly:
        raise SaosError(ErrorCode.BAD_ARGS, f"{label} is read-only")
    kind = str(cell.DataType)
    if kind in ("Double", "Integer") and (isinstance(value, bool) or not isinstance(value, (int, float))):
        raise SaosError(ErrorCode.BAD_ARGS, f"{label} needs a number, got {value!r}")
    # NaN 写进去会被 OpticStudio 存成 inf（2026-10-20 验证实测），所以写之前拒绝 / OpticStudio stores NaN as inf, so refuse before writing
    if kind in ("Double", "Integer") and (math.isnan(value) or (kind == "Integer" and not math.isfinite(value))):
        raise SaosError(ErrorCode.BAD_ARGS, f"{label} needs a {'finite integer' if kind == 'Integer' else 'number, not NaN'}, got {value!r}")
    before = cell_value(cell)
    if kind == "Double":
        cell.DoubleValue = float(value)
        actual: Any = float(cell.DoubleValue)
        applied = same_number(actual, float(value))
    elif kind == "Integer":
        if value != int(value):
            raise SaosError(ErrorCode.BAD_ARGS, f"{label} needs an integer, got {value!r}")
        cell.IntegerValue = int(value)
        actual = int(cell.IntegerValue)
        applied = actual == int(value)
    else:
        cell.Value = str(value)
        actual = str(cell.Value)
        applied = actual == str(value)
    if not applied:
        try:
            # 没写进去的值不留在镜头里 / a value that did not take is not left in the lens
            set_value = {"Double": "DoubleValue", "Integer": "IntegerValue"}.get(kind, "Value")
            setattr(cell, set_value, before)
        except Exception:  # noqa: BLE001 - 写回失败时仍报原错误 / the original error is reported either way
            pass
        raise SaosError(ErrorCode.BAD_ARGS, f"{label} did not take {value!r} (now {actual!r}; the previous value {before!r} was written back)")


def pick_name(names: list[str], wanted: str, label: str) -> str:
    """在 OpticStudio 给出的名字里按不分大小写取原写法；没有时列出全部。/ The listed name matching case-insensitively."""
    for n in names:
        if n.lower() == wanted.lower():
            return n
    raise SaosError(ErrorCode.BAD_ARGS, f"{label}: OpticStudio has no {wanted!r}", {"choices": names})


def _plain(value: Any) -> Any:
    return value if isinstance(value, bool) else float(value)


def _same(a: Any, b: Any) -> bool:
    """回读核对：数值按 same_number，列表逐项。/ Read-back comparison: numbers by same_number, lists item by item."""
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    if isinstance(a, float) and isinstance(b, (int, float)) and not isinstance(b, bool):
        return same_number(a, float(b))
    return a == b


@contextmanager
def _named(label: str):
    """ZOS-API 的异常带上是哪一项。/ Name the item in a ZOS-API error."""
    try:
        yield
    except SaosError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SaosError(ErrorCode.BAD_ARGS, f"{label}: {type(exc).__name__}: {exc}"[:500]) from exc


def catalog_constants(system, name: str) -> tuple[float, float] | None:
    """启用目录里牌号 name 的 nd、νd（材料目录工具，按目录里的原写法选牌号）；不在任何启用目录里时为 None。后端和 saos.kit 共用。
    nd and Vd of glass name from the catalogs in use (materials catalog tool, selected by the catalog's own spelling);
    None when no catalog in use has it. Shared with saos.kit."""
    catalogs = system.SystemData.MaterialCatalogs
    for catalog in catalogs.GetCatalogsInUse():
        # 在用目录列表里可能有空名，它的牌号表是 None（实测 ["SCHOTT", ""]）/ the list may hold an empty name whose material list is None
        if not str(catalog):
            continue
        names = {str(m).upper(): str(m) for m in catalogs.GetMaterialsInCatalog(catalog) or []}
        if name.upper() in names:
            tool = system.Tools.OpenMaterialsCatalog()
            try:
                tool.SelectedCatalog = str(catalog)
                tool.SelectedMaterial = names[name.upper()]
                return float(tool.Nd), float(tool.Vd)
            finally:
                tool.Close()
    return None


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


# 不指定目录时玻璃替换用的目录：成都光明的在产牌号（2026-10-19 用户决定）/ the default catalog for glass substitution
DEFAULT_SUBSTITUTE_CATALOG = "SAOS_CDGM_INPROD"


class ZosBackend(Backend):
    def __init__(self, mode: str = "standalone"):
        if mode not in ("standalone", "extension"):
            raise SaosError(ErrorCode.BAD_ARGS, f"unknown connection mode: {mode}")
        self.mode = mode
        self.root = config.opticstudio_dir()
        self.app = None
        self.extension_ui: dict[str, Any] | None = None
        self._material_cache: dict[tuple[str, ...], set[str]] = {}
        # 目录 → {牌号: 状态}，取自 AGF 文件 / catalog → {name: status}, read from the AGF files
        self._glass_status: dict[str, dict[str, str]] = {}

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
                raise SaosError(ErrorCode.ZOS_ERROR, f"{name} returned no results",
                                {"kind": "no_results", "message": str(message) if message else None,
                                 "settings": analysis.describe_settings(impl)})
            # 文本导出先写：数值提取出问题时它仍是 OpticStudio 自己给出的完整依据
            # the text export comes first: when extracting numbers fails it is still OpticStudio's own full record
            text_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                text_file = bool(results.GetTextFile(str(text_path)))
            except Exception:  # noqa: BLE001
                text_file = False
            info = self.system_info()
            out = analysis.extract(results, len(info["fields"]["items"]), len(info["wavelengths"]))
            out["text_file"] = text_file
            out["settings"] = analysis.describe_settings(impl)
            out["message"] = str(message) if message else None
            out["status"] = analysis.outcome(out)
            if out["status"] != "ok" and text_file:
                out["text_head"] = _read_text_export(text_path)[:TEXT_HEAD_CHARS]
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
        if nsc.is_nsc(system):
            # 非序列系统没有孔径、视场和面；物体按序号列出 / no aperture, fields or surfaces; objects listed instead
            return {"mode": "non_sequential", "file": str(system.SystemFile), "title": str(sd.TitleNotes.Title),
                    "lens_units": str(sd.Units.LensUnits),
                    "wavelengths": [{"um": float(w.Wavelength), "weight": float(w.Weight), "primary": bool(w.IsPrimary)}
                                    for w in (waves.GetWavelength(i) for i in range(1, waves.NumberOfWavelengths + 1))],
                    "material_catalogs": self._catalogs_in_use(), "nsc": nsc.settings(system),
                    "objects": [{"index": i, "type": str(o.Type), "comment": str(o.Comment)}
                                for i, o in ((i, system.NCE.GetObjectAt(i)) for i in range(1, int(system.NCE.NumberOfObjects) + 1))]}
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
            **({"nsc_groups": nsc.groups(system), "nsc": nsc.settings(system)} if nsc.groups(system) else {}),
        }

    def objects(self) -> list[dict[str, Any]]:
        """非序列物体表；序列系统列出各 NonSequential 面的物体（带 group = 面号），没有时为空。
        The non-sequential objects; for a sequential lens those of every NonSequential surface (with group = surface)."""
        zosapi = _load_zosapi(self.root)
        if nsc.is_nsc(self.system):
            return nsc.objects(zosapi, self.system)
        return [{**o, "group": g} for g in nsc.groups(self.system) for o in nsc.objects(zosapi, self.system, g)]

    def surfaces(self) -> list[dict[str, Any]]:
        if nsc.is_nsc(self.system):
            return []
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
            "mech_semi_diameter": _mech_semi_diameter(surf),
            "conic": float(surf.Conic),
            "is_stop": bool(surf.IsStop),
            "solves": {
                "radius": str(surf.RadiusCell.GetSolveData().Type),
                "thickness": str(surf.ThicknessCell.GetSolveData().Type),
                "material": str(surf.MaterialCell.GetSolveData().Type),
                "semi_diameter": str(surf.SemiDiameterCell.GetSolveData().Type),
                "mech_semi_diameter": _cell_solve(surf, "MechanicalSemiDiameterCell"),
                "conic": str(surf.ConicCell.GetSolveData().Type),
            },
        }

    def describe_model(self, folder: Path | None, prefix: str = "layout") -> dict[str, Any]:
        """建模说明：系统设置、逐面参数（含所有非空的 Par 列）、材料及各波长折射率（INDX 实测）、孔径、求解、顶点全局位置和布局图。"""
        zosapi = _load_zosapi(self.root)
        system = self.system
        if nsc.is_nsc(system):
            return self._describe_nsc(zosapi, folder, prefix)
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
            **self._system_extras(),
        }
        if info["aperture"]["afocal_image"]:
            info["aperture"]["afocal_units"] = str(sd.Units.AfocalModeUnits)
        mc = sd.MaterialCatalogs
        members = {c: {str(m).upper() for m in mc.GetMaterialsInCatalog(c)} for c in catalogs}
        for op in info["configurations"]["operands"]:
            # 多重结构换进的玻璃同样要有来源、查停产状态 / glasses swapped in by GLSS need a source and a status check too
            if op["type"] == "GLSS":
                op["sources"] = [self._name_source(str(v).upper(), members) for v in op["values"]]
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
            item["coating"] = str(row.Coating)
            m = lde.GetGlobalMatrix(i, *([0.0] * 12))
            item["global_vertex"] = [float(v) for v in m[10:13]] if m[0] else None
            if nsc.is_group(row):
                item["objects"] = self._described_objects(zosapi, members, group=i)
            surfaces.append(item)
        info["surfaces"] = surfaces
        if any("objects" in s for s in surfaces):
            info["nsc"] = nsc.settings(system)
        info["layout"] = {} if folder is None else self._export_layouts(folder, prefix)
        return info

    def _describe_nsc(self, zosapi, folder: Path | None, prefix: str) -> dict[str, Any]:
        """非序列系统的建模说明：系统项、非序列选项和逐个物体（类型、参考与嵌套、位置、材料来源和各波长折射率、参数、体散射）。
        The model description of a non-sequential system: system items, NSC options and every object."""
        system = self.system
        sd = system.SystemData
        catalogs = self._catalogs_in_use()
        members = {c: {str(m).upper() for m in sd.MaterialCatalogs.GetMaterialsInCatalog(c)} for c in catalogs}
        # 交叠和探测器检查由 interference()（后台进程随后调用）给出 / overlaps and detector checks come from interference()
        items = self._described_objects(zosapi, members)
        return {
            "mode": "non_sequential", "file": str(system.SystemFile), "title": str(sd.TitleNotes.Title),
            "lens_units": str(sd.Units.LensUnits),
            "wavelengths": [{"um": float(w.Wavelength), "weight": float(w.Weight), "primary": bool(w.IsPrimary)}
                            for w in (sd.Wavelengths.GetWavelength(i) for i in range(1, sd.Wavelengths.NumberOfWavelengths + 1))],
            "material_catalogs": catalogs, "coating_file": str(sd.Files.CoatingFile), "nsc": nsc.settings(system),
            "objects": items, "surfaces": [],
            "layout": {} if folder is None else self._export_layouts(folder, prefix),
        }

    def _described_objects(self, zosapi, members: dict[str, set[str]], group: int | None = None) -> list[dict[str, Any]]:
        """一组物体的读数加材料来源；group 是混合模式里 NonSequential 面的面号。/ One group's objects with material sources."""
        system = self.system
        items = nsc.objects(zosapi, system, group)
        for item in items:
            if item["kind"] == "object":
                row = system.NCE.GetObjectAt(item["index"])
                model = nsc.model_material(item["material"])
                if model is not None:
                    item["material_source"] = {"kind": "model", **model}
                elif item["material"].upper() == "ABSORB":
                    item["material_source"] = {"kind": "absorb"}
                else:
                    item["material_source"] = self._material_source(row, members)
        return items

    def _system_extras(self) -> dict[str, Any]:
        """处方第二期的系统设置和多重结构，供建模说明、结构指纹和交付报告用。
        The system settings beyond the basics and the multi-configuration data, for the model page, fingerprint and report."""
        sd = self.system.SystemData
        pol, aim = sd.Polarization, sd.RayAiming
        return {
            "environment": {"temperature": float(sd.Environment.Temperature), "pressure": float(sd.Environment.Pressure),
                            "adjust_index": bool(sd.Environment.AdjustIndexToEnvironment)},
            "apodization": {"type": str(sd.Aperture.ApodizationType),
                            **({"factor": float(sd.Aperture.ApodizationFactor)} if sd.Aperture.ApodizationFactorIsUsed else {})},
            "polarization": {"unpolarized": bool(pol.Unpolarized), "jx": float(pol.Jx), "jy": float(pol.Jy), "x_phase": float(pol.XPhase),
                             "y_phase": float(pol.YPhase), "method": str(pol.Method),
                             "convert_thin_film_phase": bool(pol.ConvertThinFilmPhaseToRayEquivalent)},
            "ray_aiming_options": {**{k: bool(getattr(aim, a)) for k, a in RAY_AIMING_FLAGS.items()},
                                   "pupil_shift": [float(aim.PupilShiftX), float(aim.PupilShiftY), float(aim.PupilShiftZ)],
                                   "pupil_compress": [float(aim.PupilCompressX), float(aim.PupilCompressY)]},
            "coating_file": str(sd.Files.CoatingFile),
            "configurations": self._configurations(),
        }

    def _configurations(self) -> dict[str, Any]:
        """多重结构：结构数、当前结构和每行操作数（类型、整数参数、各结构的值与求解）。单结构时 operands 为空。
        Multi-configuration: count, current configuration and each operand row (type, integer parameters, values and solves)."""
        mce = self.system.MCE
        count = int(mce.NumberOfConfigurations)
        operands = []
        for j in range(1, int(mce.NumberOfOperands) + 1):
            row = mce.GetOperandAt(j)
            kind = str(row.TypeName)
            if kind == "MOFF":
                continue
            cells = [row.GetOperandCell(c) for c in range(1, count + 1)]
            operands.append({
                "row": j, "type": kind,
                "params": [int(getattr(row, f"Param{n}")) for n in (1, 2, 3) if getattr(row, f"Param{n}Enabled")],
                "values": [cell_value(c) for c in cells],
                "solves": [str(c.GetSolveData().Type) for c in cells],
            })
        return {"count": count, "current": int(mce.CurrentConfiguration), "operands": operands}

    def vary_config(self, rows: list[int], configs: list[int] | None, on: bool) -> dict[str, str]:
        """把多重结构的格设为变量或固定；configs 为空时是该行的全部结构。返回 {"<行>.<结构>": 求解类型}。
        Make multi-configuration cells variable or fixed; configs empty means every configuration of the row."""
        mce = self.system.MCE
        count, n_rows = int(mce.NumberOfConfigurations), int(mce.NumberOfOperands)
        configs = configs or list(range(1, count + 1))
        bad = [r for r in rows if not 1 <= r <= n_rows] + [c for c in configs if not 1 <= c <= count]
        if count < 2 or bad:
            raise SaosError(ErrorCode.BAD_ARGS, f"multi-configuration rows 1–{n_rows} and configurations 1–{count} (at least 2 configurations); got {bad}")
        out = {}
        for r in rows:
            for c in configs:
                cell = mce.GetOperandAt(r).GetOperandCell(c)
                cell.MakeSolveVariable() if on else cell.MakeSolveFixed()
                out[f"{r}.{c}"] = str(cell.GetSolveData().Type)
        return out

    def _material_source(self, row, members: dict[str, set[str]]) -> dict[str, Any]:
        solve = row.MaterialCell.GetSolveData()
        if str(solve.Type) == "MaterialModel":
            data = solve._S_MaterialModel
            return {"kind": "model", "nd": float(data.IndexNd), "vd": float(data.AbbeVd), "dpgf": float(data.dPgF)}
        return self._name_source(str(row.Material).upper(), members)

    def _name_source(self, name: str, members: dict[str, set[str]]) -> dict[str, Any]:
        """按名字判断介质来源：空气、反射镜、坐标断点沿用、目录玻璃（含所在目录和停产状态）。
        The source of a medium by name: air, mirror, coordinate-break inherit, catalog glass with catalogs and status."""
        if name == "-":  # 坐标断点不定义介质，沿用前一个面之后的介质
            return {"kind": "inherit"}
        if not name:
            return {"kind": "none"}
        if name == "MIRROR":
            return {"kind": "mirror"}
        catalogs = [c for c, names in members.items() if name in names]
        return {"kind": "catalog", "name": name, "catalogs": catalogs, "status": self.glass_status(name, catalogs)}

    def glass_status(self, name: str, catalogs: list[str]) -> str | None:
        """牌号在目录里的状态（Standard、Preferred、Obsolete、Special、Melt），取自 OpticStudio 玻璃目录文件夹里的 AGF；
        与 IMaterialsCatalog 逐一核对过 SCHOTT、OHARA、CDGM 全部牌号，一致，而 IMaterialsCatalog 读一个目录要 14–27 s
        （2026-10-18 实测）。找不到时为 None。
        Catalog status of a glass, from the AGF files in OpticStudio's glass folder; it matched IMaterialsCatalog for every
        SCHOTT, OHARA and CDGM glass, which takes 14-27 s per catalog (measured 2026-10-18). None when not found."""
        for catalog in catalogs:
            key = catalog.upper()
            if key not in self._glass_status:
                folder = self._glass_dir()
                try:
                    self._glass_status[key] = {g.name.upper(): g.status for g in glasscat.load_catalogs([catalog], folder)}
                except (OSError, ValueError):  # describe 每次都走到这里，坏文件不能让它失败 / describe always passes here
                    self._glass_status[key] = {}
            if name.upper() in self._glass_status[key]:
                return self._glass_status[key][name.upper()]
        return None

    def _glass_dir(self) -> Path:
        glass_dir = str(getattr(self.app, "GlassDir", "") or "") if self.app is not None else ""
        return Path(glass_dir) if glass_dir else glasscat.DEFAULT_GLASSCAT_DIR

    def substitute_catalog(self, catalog: str | None) -> str:
        """玻璃替换用的目录名（不改镜头）：指定了就用指定的，否则用成都光明的在产目录（Standard、Preferred，
        library/glasscat 生成；Glasscat 里没有它时为空串，即全部在用目录）。
        The catalog name for glass substitution, without touching the lens: the one given, else CDGM's in-production
        catalog (made by library/glasscat); empty, meaning every catalog in use, when that is not installed."""
        name = (catalog or "").upper()
        if not name and (self._glass_dir() / f"{DEFAULT_SUBSTITUTE_CATALOG}.AGF").is_file():
            name = DEFAULT_SUBSTITUTE_CATALOG
        return name

    def catalog_has(self, name: str, catalog: str) -> bool | None:
        """牌号是否在这个目录的 AGF 里；目录为空、找不到它的 AGF，或这不是在用目录里的牌号（模型玻璃）时为 None。
        Whether a glass is in this catalog's AGF; None for an empty catalog, one whose AGF is not found, or a name that
        is no glass of the catalogs in use (a model glass)."""
        if not catalog or not name or name.upper() not in self._names_in_use():
            return None
        self.glass_status(name, [catalog])  # 读入并缓存该目录 / loads and caches the catalog
        table = self._glass_status.get(catalog.upper())
        return None if not table else name.upper() in table

    def _use_catalog(self, name: str) -> None:
        """替换求解只认在用的目录：不在就加进去（这是对镜头的改动）/ the solve only takes catalogs in use: add it (a lens change)"""
        if not name:
            return
        mc = self.system.SystemData.MaterialCatalogs
        if not mc.IsCatalogInUse(name):
            mc.AddCatalog(name)
            self._material_cache.clear()
            if not mc.IsCatalogInUse(name):
                raise SaosError(ErrorCode.BAD_ARGS, f"cannot load glass catalog {name} from {self._glass_dir()}")

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
        "mech_semi_diameter": "MechanicalSemiDiameter",
        "material": "Material",
        "comment": "Comment",
    }

    def _row(self, index: Any):
        lde = self.system.LDE
        if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < lde.NumberOfSurfaces:
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {index!r} out of range 0–{lde.NumberOfSurfaces - 1}")
        return lde.GetSurfaceAt(index)

    def _par_cell(self, row, index: int, param: str):
        return par_cell(_load_zosapi(self.root), row, index, param)

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
            check_size(param, target)
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
        upper = name.upper()
        if upper not in self._names_in_use():
            raise SaosError(
                ErrorCode.BAD_ARGS,
                f"{name!r} is not in the catalogs in use",
                {"catalogs": self._catalogs_in_use()},
            )
        return upper

    def _names_in_use(self) -> set[str]:
        catalogs = tuple(self._catalogs_in_use())
        if catalogs not in self._material_cache:
            mc = self.system.SystemData.MaterialCatalogs
            names: set[str] = set()
            for cat in catalogs:
                names.update(str(m).upper() for m in mc.GetMaterialsInCatalog(cat))
            self._material_cache[catalogs] = names
        return self._material_cache[catalogs]

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
        if p.get("mode") == "non_sequential":
            self._build_nsc(zosapi, p)
            return
        # 2024 R1 实测：非序列系统上 New(False) 之后仍是非序列模式；MakeSequential 又会把原系统转成带一个 NonSequential 面的
        # 序列镜头，所以切回序列后再新建一次，得到干净的序列系统
        # measured in 2024 R1: New(False) on a non-sequential system stays non-sequential, and MakeSequential turns it into a
        # sequential lens holding a NonSequential surface; so switch back and start a new system once more
        if nsc.is_nsc(system):
            system.MakeSequential()
            system.New(False)
            if nsc.is_nsc(system) or nsc.groups(system):
                raise SaosError(ErrorCode.ZOS_ERROR, "OpticStudio did not give a clean sequential system after a non-sequential one")
        sd = system.SystemData
        self._build_common(zosapi, p)

        env = p["environment"]
        sd.Environment.AdjustIndexToEnvironment = env["adjust_index"]
        sd.Environment.Temperature = env["temperature"]
        sd.Environment.Pressure = env["pressure"]

        aperture_type = APERTURES[p["aperture"]["type"]]
        sd.Aperture.ApertureType = getattr(zosapi.SystemData.ZemaxApertureType, aperture_type)
        if p["aperture"]["value"] is not None:
            sd.Aperture.ApertureValue = p["aperture"]["value"]
        # 光阑在物镜后焦面时入瞳在无穷远；用 OpticStudio 的物方远心设置表达，避免光线瞄准在退化情形下失败
        sd.Aperture.TelecentricObjectSpace = bool(p.get("telecentric_object", False))
        sd.Aperture.AFocalImageSpace = p["afocal"] is not None
        if p["afocal"] and "units" in p["afocal"]:
            sd.Units.AfocalModeUnits = getattr(zosapi.SystemData.ZemaxAfocalModeUnits, AFOCAL_UNITS[p["afocal"]["units"]])
        if p["apodization"]:
            apod = p["apodization"]
            sd.Aperture.ApodizationType = getattr(zosapi.SystemData.ZemaxApodizationType, APODIZATIONS[apod["type"]])
            if "factor" in apod:
                if not sd.Aperture.ApodizationFactorIsUsed:
                    raise SaosError(ErrorCode.BAD_ARGS, f"OpticStudio uses no apodization factor with {apod['type']} apodization")
                sd.Aperture.ApodizationFactor = apod["factor"]
        if p["polarization"]:
            with _named("polarization"):
                self._set_polarization(zosapi, sd.Polarization, p["polarization"])

        f = p["fields"]
        field_type = FIELDS[f["type"]]
        fields = sd.Fields
        fields.SetFieldType(getattr(zosapi.SystemData.FieldType, field_type))
        fields.Normalization = getattr(zosapi.SystemData.FieldNormalizationType, NORMALIZATIONS[f["normalization"]])
        while fields.NumberOfFields > 1:
            fields.RemoveField(fields.NumberOfFields)
        first = fields.GetField(1)
        first.X, first.Y, first.Weight = f["x"][0], f["y"][0], f["weights"][0]
        for x, y, w in zip(f["x"][1:], f["y"][1:], f["weights"][1:]):
            fields.AddField(x, y, w)

        aiming = zosapi.SystemData.RayAimingMethod
        sd.RayAiming.RayAiming = {"off": aiming.Off, "paraxial": aiming.Paraxial, "real": aiming.Real}[p["ray_aiming"]]
        options = p["ray_aiming_options"] or {}
        for key, attr in RAY_AIMING_FLAGS.items():
            if key in options:
                setattr(sd.RayAiming, attr, options[key])
        if "pupil_shift" in options:
            sd.RayAiming.PupilShiftX, sd.RayAiming.PupilShiftY, sd.RayAiming.PupilShiftZ = options["pupil_shift"]
        if "pupil_compress" in options:
            sd.RayAiming.PupilCompressX, sd.RayAiming.PupilCompressY = options["pupil_compress"]

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
        rows = [(k, s, f"surface {k}") for k, s in enumerate(surfaces, start=1)]
        if p.get("image"):
            rows.append((len(surfaces) + 1, p["image"], "image surface"))
        for k, s, label in rows:
            self._build_surface(zosapi, lde.GetSurfaceAt(k), s, label)
        for k, s in enumerate(surfaces, start=1):
            if s["stop"]:
                lde.GetSurfaceAt(k).IsStop = True
        # 混合模式：每个 NonSequential 面一组物体，NCE.SetActiveSurface 选组（2024 R1 实测：组内物体号从 1 起，保存后保留）
        # mixed mode: one object group per NonSequential surface, selected with NCE.SetActiveSurface (numbered from 1 within the group)
        for k, s in enumerate(surfaces, start=1):
            if s.get("objects"):
                self._build_nsc_group(zosapi, k, s["objects"], f"surface {k}")
        # 求解最后设：拾取可能读后面的面
        for k, s, label in rows:
            for column, solve in (s.get("solves") or {}).items():
                with _named(f"{label} {column}"):
                    self._set_solve(zosapi, lde.GetSurfaceAt(k), solve, column, f"{label} {column}")
        if p["configurations"]:
            with _named("configurations"):
                self._build_configurations(zosapi, p["configurations"])
        fields.ClearVignetting()

        # OpticStudio 遇到不成立的组合会悄悄改设置，搭完回读一次
        got = {"aperture.type": (str(sd.Aperture.ApertureType), aperture_type), "fields.type": (str(fields.GetFieldType()), field_type),
               "telecentric_object": (bool(sd.Aperture.TelecentricObjectSpace), bool(p.get("telecentric_object", False))),
               "afocal": (bool(sd.Aperture.AFocalImageSpace), p["afocal"] is not None),
               "environment.temperature": (float(sd.Environment.Temperature), env["temperature"]),
               "environment.pressure": (float(sd.Environment.Pressure), env["pressure"])}
        # 偏振和光线瞄准选项彼此有默认联动（非偏振盖掉琼斯矢量、自动光瞳偏移盖掉手动值），逐项回读
        # polarization and ray-aiming options override each other by default, so each given item is read back
        pol = sd.Polarization
        for key, attr in (("unpolarized", "Unpolarized"), ("jx", "Jx"), ("jy", "Jy"), ("x_phase", "XPhase"), ("y_phase", "YPhase"),
                          ("convert_thin_film_phase", "ConvertThinFilmPhaseToRayEquivalent")):
            if key in (p["polarization"] or {}):
                got[f"polarization.{key}"] = (_plain(getattr(pol, attr)), p["polarization"][key])
        if "method" in (p["polarization"] or {}):
            got["polarization.method"] = (str(pol.Method), POLARIZATION_METHODS[p["polarization"]["method"]])
        for key, attr in RAY_AIMING_FLAGS.items():
            if key in options:
                got[f"ray_aiming_options.{key}"] = (bool(getattr(sd.RayAiming, attr)), options[key])
        for key, attrs in (("pupil_shift", ("PupilShiftX", "PupilShiftY", "PupilShiftZ")), ("pupil_compress", ("PupilCompressX", "PupilCompressY"))):
            if key in options:
                got[f"ray_aiming_options.{key}"] = ([float(getattr(sd.RayAiming, a)) for a in attrs], options[key])
        changed = {k: {"requested": want, "actual": have} for k, (have, want) in got.items() if not _same(have, want)}
        if changed:
            raise SaosError(ErrorCode.BAD_ARGS, f"OpticStudio did not keep {', '.join(changed)}; the combination of settings is not valid", changed)

    def _build_common(self, zosapi, p: dict[str, Any]) -> None:
        """序列和非序列共用：标题、单位、玻璃目录、膜层文件、波长。/ Shared by both modes: title, units, catalogs, coating file, wavelengths."""
        sd = self.system.SystemData
        sd.TitleNotes.Title = p["title"]
        sd.Units.LensUnits = getattr(zosapi.SystemData.ZemaxSystemUnits, UNITS[p["units"]])
        mc = sd.MaterialCatalogs
        wanted = [c.upper() for c in p["catalogs"]]
        for cat in wanted:
            if not mc.IsCatalogInUse(cat):
                mc.AddCatalog(cat)
        for cat in [str(c) for c in mc.GetCatalogsInUse() if str(c)]:
            if cat.upper() not in wanted:
                mc.RemoveCatalog(cat)
        self._material_cache.clear()
        missing = [c for c in wanted if c not in [str(x).upper() for x in mc.GetCatalogsInUse()]]
        if missing:
            raise SaosError(ErrorCode.BAD_ARGS, f"cannot load glass catalogs {missing}")
        if p["coating_file"]:
            # 面的膜层名在这个文件里查，所以先于各面设定 / surface coatings are looked up in this file, so it goes first
            sd.Files.CoatingFile = pick_name([str(c) for c in sd.Files.GetCoatingFiles()], p["coating_file"], "coating_file")
        waves = sd.Wavelengths
        while waves.NumberOfWavelengths > 1:
            waves.RemoveWavelength(waves.NumberOfWavelengths)
        um, weights = p["wavelengths"]["um"], p["wavelengths"]["weights"]
        waves.GetWavelength(1).Wavelength = um[0]
        waves.GetWavelength(1).Weight = weights[0]
        for wl, wt in zip(um[1:], weights[1:]):
            waves.AddWavelength(wl, wt)
        waves.GetWavelength(p["wavelengths"]["primary"]).MakePrimary()
        with _named("nsc"):
            nsc.apply_settings(self.system, p.get("nsc") or {})
            nsc.apply_units(zosapi, self.system, p)

    def _build_nsc(self, zosapi, p: dict[str, Any]) -> None:
        """非序列处方：切到非序列模式，设系统项和非序列选项，逐个写物体（类型、位置、材料、参数、体散射），模型玻璃回读核对。
        A non-sequential prescription: switch modes, set the system and NSC options, write each object, check model glasses."""
        system = self.system
        if not system.MakeNonSequential() or not nsc.is_nsc(system):
            raise SaosError(ErrorCode.ZOS_ERROR, "OpticStudio did not switch to non-sequential mode")
        self._build_common(zosapi, p)
        self._build_objects(zosapi, p["objects"], "")

    def _build_objects(self, zosapi, objects: list[dict[str, Any]], prefix: str) -> None:
        """当前非序列组（纯非序列系统或 NCE.SetActiveSurface 选中的组）的物体。/ The objects of the current NSC group."""
        system = self.system
        nce = system.NCE
        while int(nce.NumberOfObjects) < len(objects):
            nce.AddObject()
        primary = system.SystemData.Wavelengths.GetWavelength(1).Wavelength
        for i in range(1, int(system.SystemData.Wavelengths.NumberOfWavelengths) + 1):
            if system.SystemData.Wavelengths.GetWavelength(i).IsPrimary:
                primary = system.SystemData.Wavelengths.GetWavelength(i).Wavelength
        for k, spec in enumerate(objects, start=1):
            with _named(f"{prefix}objects[{k}]"):
                nsc.build_object(self, zosapi, system, k, spec, prefix)
        for k, spec in enumerate(objects, start=1):
            nsc.check_model_index(system, k, spec, float(primary), prefix)

    def _build_nsc_group(self, zosapi, surface: int, objects: list[dict[str, Any]], label: str) -> None:
        nce = self.system.NCE
        nce.SetActiveSurface(surface)
        if int(nce.ActiveSurface) != surface:
            raise SaosError(ErrorCode.BAD_ARGS, f"{label}: OpticStudio did not select its non-sequential group (surface type must be NonSequential)")
        self._build_objects(zosapi, objects, f"{label} ")

    def _build_surface(self, zosapi, row, s: dict[str, Any], label: str) -> None:
        """处方里的一个面（或像面）：面型、通用项、面型参数、倾斜偏心、半口径、孔径。求解另设。"""
        with _named(label):
            kind = s["type"]
            if kind != "standard":
                surface_type = enum_member(zosapi.Editors.LDE.SurfaceType, "Standard", "Paraxial" if kind == "paraxial" else kind, f"{label} type")
                if not row.ChangeType(row.GetSurfaceTypeSettings(surface_type)):
                    raise SaosError(ErrorCode.BAD_ARGS, f"{label}: OpticStudio refused surface type {kind}")
            if kind == "paraxial":
                columns = zosapi.Editors.LDE.SurfaceColumn
                row.GetSurfaceCell(columns.Par1).DoubleValue = s["focal_length"]
                row.GetSurfaceCell(columns.Par2).IntegerValue = s["opd_mode"]
            for key, attr in (("radius", "Radius"), ("conic", "Conic")):
                if key in s:
                    setattr(row, attr, s[key])
            if s.get("material"):
                self._set_material(row, s["material"], label)
            if "thickness" in s:
                row.Thickness = s["thickness"]
            row.Comment = s["comment"]
            cells = None
            for key, value in (s.get("params") or {}).items():
                if cells is None and not is_par(key):
                    cells = param_cells(zosapi, row)
                cell = type_param_cell(zosapi, row, label, key, cells)
                set_cell(cell, value, f"{label} {key}")
                if str(cell.DataType) == "Integer":
                    cells = None  # 项数一类的整数列会增减后面的列 / a term count adds or removes later columns
            if s.get("decenter_tilt"):
                self._set_decenter_tilt(zosapi, row, s["decenter_tilt"], label)
            if "semi_diameter" in s:
                row.SemiDiameterCell.MakeSolveFixed()
                row.SemiDiameter = s["semi_diameter"]
                if not same_number(float(row.SemiDiameter), s["semi_diameter"]):
                    raise SaosError(ErrorCode.BAD_ARGS, f"{label}: semi_diameter did not take {s['semi_diameter']} (now {float(row.SemiDiameter)})")
            if "mech_semi_diameter" in s:
                row.MechanicalSemiDiameter = s["mech_semi_diameter"]
            if s.get("aperture"):
                self._set_surface_aperture(zosapi, row, s["aperture"], label)
            if s.get("coating"):
                name = pick_name([str(c) for c in row.CoatingData.GetAvailableCoatings()], s["coating"], f"{label} coating")
                row.Coating = name
                if str(row.Coating) != name:
                    raise SaosError(ErrorCode.BAD_ARGS, f"{label}: coating did not take {name!r} (now {str(row.Coating)!r})")

    def _set_polarization(self, zosapi, pol, d: dict[str, Any]) -> None:
        if "unpolarized" in d:
            pol.Unpolarized = d["unpolarized"]
        for key, attr in (("jx", "Jx"), ("jy", "Jy"), ("x_phase", "XPhase"), ("y_phase", "YPhase")):
            if key in d:
                setattr(pol, attr, d[key])
        if "method" in d:
            pol.Method = getattr(zosapi.SystemData.PolarizationMethod, POLARIZATION_METHODS[d["method"]])
        if "convert_thin_film_phase" in d:
            pol.ConvertThinFilmPhaseToRayEquivalent = d["convert_thin_film_phase"]

    def _set_decenter_tilt(self, zosapi, row, dt: dict[str, Any], label: str) -> None:
        td = row.TiltDecenterData
        if not td.BeforeAfterSurfaceEnabled:
            raise SaosError(ErrorCode.BAD_ARGS, f"{label} ({row.TypeName}) takes no tilt/decenter; use a CoordinateBreak surface")
        orders = zosapi.Editors.LDE.TiltDecenterOrderType
        if dt["before"]:
            b = dt["before"]
            td.BeforeSurfaceDecenterX, td.BeforeSurfaceDecenterY = b["decenter_x"], b["decenter_y"]
            td.BeforeSurfaceTiltX, td.BeforeSurfaceTiltY, td.BeforeSurfaceTiltZ = b["tilt_x"], b["tilt_y"], b["tilt_z"]
            td.BeforeSurfaceOrder = getattr(orders, ORDERS[b["order"]])
        after = dt["after"]
        if after == "reverse":
            if not td.SetAfterSurfaceModeReverseThis():
                raise SaosError(ErrorCode.BAD_ARGS, f"{label}: OpticStudio refused to reverse the tilt/decenter after the surface")
        elif after:
            td.SetAfterSurfaceModeExplicit()
            td.AfterSurfaceDecenterX, td.AfterSurfaceDecenterY = after["decenter_x"], after["decenter_y"]
            td.AfterSurfaceTiltX, td.AfterSurfaceTiltY, td.AfterSurfaceTiltZ = after["tilt_x"], after["tilt_y"], after["tilt_z"]
            td.AfterSurfaceOrder = getattr(orders, ORDERS[after["order"]])

    def _set_surface_aperture(self, zosapi, row, a: dict[str, Any], label: str) -> None:
        name = SURFACE_APERTURES[a["type"]]
        settings = row.ApertureData.CreateApertureTypeSettings(getattr(zosapi.Editors.LDE.SurfaceApertureTypes, name))
        data = getattr(settings, f"_S_{name}")
        if a["type"].startswith("circular"):
            data.MinimumRadius, data.MaximumRadius = a["min"], a["max"]
        elif a["type"] == "spider":
            data.NumberOfArms, data.WidthOfArms = a["arms"], a["width"]
        else:
            data.XHalfWidth, data.YHalfWidth = a["x_half"], a["y_half"]
        data.ApertureXDecenter, data.ApertureYDecenter = a["x_decenter"], a["y_decenter"]
        if not row.ApertureData.ChangeApertureTypeSettings(settings):
            raise SaosError(ErrorCode.BAD_ARGS, f"{label}: OpticStudio refused the {a['type']} aperture")

    def _set_solve(self, zosapi, row, solve: dict[str, Any], column: str, label: str) -> None:
        cell = type_param_cell(zosapi, row, label, column) if is_par(column) else getattr(row, f"{column.capitalize()}Cell")
        kind = solve["type"]
        if kind == "variable":
            cell.MakeSolveVariable()
            expected = "Variable"
        else:
            expected = {"pickup": "SurfacePickup", "marginal_ray_height": "MarginalRayHeight", "f_number": "FNumber"}[kind]
            solve_type = getattr(zosapi.Editors.SolveType, expected)
            if not cell.IsSolveTypeSupported(solve_type):
                raise SaosError(ErrorCode.BAD_ARGS, f"{label}: OpticStudio does not support a {kind} solve here")
            data = cell.CreateSolveType(solve_type)
            d = getattr(data, f"_S_{expected}")
            if kind == "pickup":
                d.Surface = solve["surface"]
                if "scale" in solve:
                    if d.SupportsScale:
                        d.ScaleFactor = solve["scale"]
                    if d.SupportsOffset:
                        d.Offset = solve["offset"]
            elif kind == "marginal_ray_height":
                d.Height, d.PupilZone = solve["height"], solve["zone"]
            else:
                d.FNumber = solve["value"]
            cell.SetSolveData(data)
        actual = str(cell.GetSolveData().Type)
        if actual != expected:
            raise SaosError(ErrorCode.BAD_ARGS, f"{label}: the {kind} solve did not take (the cell holds {actual})")

    def _build_configurations(self, zosapi, rows: list[dict[str, Any]]) -> None:
        """多重结构：每行一个操作数，按结构写值；搭完回到结构 1。"""
        mce = self.system.MCE
        for _ in range(len(rows[0]["values"]) - 1):
            mce.AddConfiguration(False)
        for j, op in enumerate(rows, start=1):
            label = f"configurations[{j}] {op['operand']}"
            row = mce.GetOperandAt(j) if j <= mce.NumberOfOperands else mce.AddOperand()
            if not row.ChangeType(enum_member(zosapi.Editors.MCE.MultiConfigOperandType, "THIC", op["operand"], label)):
                raise SaosError(ErrorCode.BAD_ARGS, f"{label}: OpticStudio refused the operand")
            for n, value in enumerate(op["params"], start=1):
                if not getattr(row, f"Param{n}Enabled"):
                    raise SaosError(ErrorCode.BAD_ARGS, f"{label} has no parameter {n}")
                setattr(row, f"Param{n}", value)
            for c, value in enumerate(op["values"], start=1):
                if op["operand"] == "GLSS" and isinstance(value, str):
                    value = self._check_material(value)
                cell = row.GetOperandCell(c)
                set_cell(cell, value, f"{label} configuration {c}")
                if op["vary"]:
                    cell.MakeSolveVariable()
        mce.SetCurrentConfiguration(1)

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
                # 其他面型用 ZOS-API 的枚举名，处方按它重建 / other types by their ZOS-API enum name, which a prescription rebuilds
                "type": type_name.lower() if type_name in ("Standard", "Paraxial") else str(row.Type),
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
                if type_name != "Standard":
                    # 按列序给出全部可写的参数列（项数一类的整数列在系数之前）/ every writable parameter column in column order
                    item["params"] = {f"par{n}": cell_value(c) for n, c in param_cells(zosapi, row).items() if not c.IsReadOnly}
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
        constants = catalog_constants(self.system, name)
        if constants is None:
            raise SaosError(ErrorCode.BAD_ARGS, f"{name} is not in the catalogs in use, so it has no nd / Vd to start a model glass from")
        return constants

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
            # 面已经过校验才加目录，校验失败不会留下目录改动；之后的异常由后台进程把镜头标为未保存
            # the catalog is added only after the surface passed its checks; the daemon marks the lens unsaved on a later error
            # catalog 已由 substitute_catalog 解析（空串为全部在用目录），这里不再重选默认目录 / already resolved; no second pick of the default
            name = (catalog or "").upper()
            self._use_catalog(name)
            new = cell.CreateSolveType(zosapi.Editors.SolveType.MaterialSubstitute)
            # 空目录名表示在当前使用的全部目录中替换 / an empty catalog substitutes within every catalog in use
            new._S_MaterialSubstitute.Catalog = name
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
        if nsc.is_nsc(self.system):
            return self.merit_wizard_nsc(settings)
        known = {"type", "data", "reference", "rings", "arms", "glass", "air", "start_at", "assume_axial_symmetry"}
        extra = sorted(set(settings) - known)
        if extra:
            raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard settings {extra} unknown; allowed {sorted(known)}")
        for key in ("type", "data", "reference"):
            if key in settings and not isinstance(settings[key], str):
                raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard {key} must be a name, got {settings[key]!r}")

        def integer(key: str, default: int) -> int:
            v = settings.get(key, default)
            if isinstance(v, bool) or not isinstance(v, int) or v < 1:
                raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard {key} must be an integer from 1, got {v!r}")
            return v

        rings, arms, start_at = integer("rings", 3), integer("arms", 6), integer("start_at", 1)
        symmetric = settings.get("assume_axial_symmetry", True)
        if not isinstance(symmetric, bool):
            raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard assume_axial_symmetry must be true or false, got {symmetric!r}")
        if arms not in (6, 8, 10):
            raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard arms must be 6, 8 or 10, got {arms!r}")
        for key in ("glass", "air"):
            v = settings.get(key)
            if v is not None and not (isinstance(v, list) and len(v) == 3
                                      and all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in v)):
                raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard {key} must be [minimum, maximum, edge] numbers, got {v!r}")
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
        wiz.Ring = rings - 1
        wiz.Arm = {6: 0, 8: 1, 10: 2}[arms]
        glass = settings.get("glass")
        wiz.IsGlassUsed = glass is not None
        if glass:
            wiz.GlassMin, wiz.GlassMax, wiz.GlassEdge = float(glass[0]), float(glass[1]), float(glass[2])
        air = settings.get("air")
        wiz.IsAirUsed = air is not None
        if air:
            wiz.AirMin, wiz.AirMax, wiz.AirEdge = float(air[0]), float(air[1]), float(air[2])
        wiz.StartAt = start_at
        # 折转、双圆锥、柱面等非旋转对称系统必须关掉，否则向导只追半个光瞳 / must be off for systems that are not rotationally symmetric
        wiz.IsAssumeAxialSymmetryUsed = symmetric
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
                 on_progress=None, should_cancel=None, algorithm: str | None = None) -> dict[str, Any]:
        """异步运行局部优化：边跑边报告进度，超时或收到取消请求时调用 Cancel，保留已经得到的结果。
        Run the local optimisation asynchronously: report progress while it runs, call Cancel on a timeout or a cancel
        request, and keep what it reached."""
        zosapi = _load_zosapi(self.root)
        names = {"auto": "Automatic", "1": "Fixed_1_Cycle", "5": "Fixed_5_Cycles", "10": "Fixed_10_Cycles", "50": "Fixed_50_Cycles"}
        if cycles not in names:
            raise SaosError(ErrorCode.BAD_ARGS, f"cycles must be one of {list(names)}")
        # 非序列系统的探测器评价函数有统计噪声，阻尼最小二乘的差分导数不可靠，默认用正交下降（OpticStudio 对非序列的建议）
        # a non-sequential detector merit is noisy and spoils DLS's finite differences: default to orthogonal descent there
        algorithm = algorithm or ("od" if nsc.is_nsc(self.system) else "dls")
        if algorithm not in ("dls", "od"):
            raise SaosError(ErrorCode.BAD_ARGS, f"algorithm must be dls or od, got {algorithm!r}")
        tool = self.system.Tools.OpenLocalOptimization()
        try:
            tool.Algorithm = getattr(zosapi.Tools.Optimization.OptimizationAlgorithm,
                                     "DampedLeastSquares" if algorithm == "dls" else "OrthogonalDescent")
            tool.Cycles = getattr(zosapi.Tools.Optimization.OptimizationCycles, names[cycles])
            if cores:
                tool.NumberOfCores = int(cores)
            stopped_by, elapsed = self._run_optimizer(tool, lambda: {"merit": float(tool.CurrentMeritFunction)},
                                                      max_seconds, on_progress, should_cancel)
            return {
                "initial_merit": float(tool.InitialMeritFunction),
                "final_merit": float(tool.CurrentMeritFunction),
                "variables": int(tool.Variables),
                "targets": int(tool.Targets),
                "succeeded": bool(tool.Succeeded),
                "stopped_by": stopped_by,
                "elapsed_s": elapsed,
                "algorithm": algorithm,
            }
        finally:
            tool.Close()

    def vary_object(self, group: int | None, number: int, param: str, on: bool) -> str:
        """非序列物体的一个参数设为变量或固定：x、y、z、tilt_x、tilt_y、tilt_z、par<N> 或列标题。"""
        system = self.system
        if not nsc.is_nsc(system):
            found = nsc.groups(system)
            if group is None and len(found) == 1:
                group = found[0]
            if group is None:
                raise SaosError(ErrorCode.BAD_ARGS, f"the lens has NonSequential surfaces {found}; give --group", {"groups": found})
        nsc.select_group(system, group)
        nce = system.NCE
        if not 1 <= number <= int(nce.NumberOfObjects):
            raise SaosError(ErrorCode.BAD_ARGS, f"object {number} out of range 1-{nce.NumberOfObjects}")
        o = nce.GetObjectAt(number)
        cells = {"x": "XPositionCell", "y": "YPositionCell", "z": "ZPositionCell", "tilt_x": "TiltAboutXCell", "tilt_y": "TiltAboutYCell",
                 "tilt_z": "TiltAboutZCell"}
        cell = getattr(o, cells[param]) if param in cells else nsc.param_cell(_load_zosapi(self.root), o, f"object {number}", param)
        cell.MakeSolveVariable() if on else cell.MakeSolveFixed()
        return str(cell.GetSolveData().Type)

    def merit_wizard_nsc(self, settings: dict[str, Any]) -> int:
        """非序列评价函数向导：按探测器上的判据（总通量、均匀度、质心、RMS 光斑或角度等）生成操作数。名字取向导自己列的可选项。
        The non-sequential merit wizard: operands for a criterion on a detector; names come from the wizard's own lists."""
        w = self.system.MFE.NSCOptimizationWizard
        w.Initialize()

        def pick(name: str, count_attr: str, getter: str, wanted: Any) -> int:
            names = [str(getattr(w, getter)(i)) for i in range(int(getattr(w, count_attr)))]
            text = str(wanted).replace(" ", "").lower()
            for i, n in enumerate(names):
                # 探测器、光源列成 "3: Rectangle"，可以只写编号 / detectors and sources read "3: Rectangle"; the number alone is enough
                if n.replace(" ", "").lower() == text or n.split(":")[0].strip() == str(wanted).strip():
                    return i
            raise SaosError(ErrorCode.BAD_ARGS, f"NSC optimization wizard {name} must be one of {names}, got {wanted!r}")

        known = {"criterion", "detector", "source", "ray_sampling", "edge_sampling", "boundary", "target", "weight", "configuration",
                 "start_at", "overwrite", "minimum_flux", "split", "scatter", "polarization", "ignore_errors"}
        extra = sorted(set(settings) - known)
        if extra:
            raise SaosError(ErrorCode.BAD_ARGS, f"NSC optimization wizard settings {extra} unknown; allowed {sorted(known)}")
        w.Criterion = pick("criterion", "NumberOfCriterion", "GetCriterionAt", settings.get("criterion", "Total Flux"))
        if "detector" in settings:
            w.UseDetector = pick("detector", "NumberOfDetectors", "GetDetectorAt", settings["detector"])
        w.UseSource = pick("source", "NumberOfSources", "GetSourceAt", settings.get("source", "All"))
        w.RaySampling = pick("ray_sampling", "NumberOfRaySamplings", "GetRaySamplingAt", settings.get("ray_sampling", "Low (1X)"))
        if "edge_sampling" in settings:
            w.EdgeSampling = pick("edge_sampling", "NumberOfEdgeSamplings", "GetEdgeSamplingAt", settings["edge_sampling"])
        w.Boundary = pick("boundary", "NumberOfBoundaries", "GetBoundaryAt", settings.get("boundary", "Equal To"))
        if "configuration" in settings:
            w.Configuration = pick("configuration", "NumberOfConfigurations", "GetConfigurationAt", settings["configuration"])
        w.Target = float(settings.get("target", 0.0))
        w.OverallWeight = float(settings.get("weight", 1.0))
        w.StartAt = int(settings.get("start_at", 1))
        w.IsOverwriteUsed = bool(settings.get("overwrite", True))
        if "minimum_flux" in settings:
            w.IsMinimumFluxUsed, w.MinimumFlux = True, float(settings["minimum_flux"])
        for key, attr in (("split", "IsSplitRaysUsed"), ("scatter", "IsScatterRaysUsed"), ("polarization", "IsUsePolarizationUsed"),
                          ("ignore_errors", "IsIgnoreErrorsUsed")):
            if key in settings:
                setattr(w, attr, bool(settings[key]))
        w.Apply()
        return int(self.system.MFE.NumberOfOperands)

    def optimize_search(self, kind: str, cores: int | None = None, *, max_seconds: float,
                        on_progress=None, should_cancel=None) -> dict[str, Any]:
        """Hammer（kind="hammer"）或全局搜索（kind="global"），按 max_seconds 调用 Cancel；结束后编辑器里是找到的最好系统。
        两者都不会自己结束，TargetRunTimeM 也不能让它们停下。Hammer 的自动模式会自己结束，但在 Cooke 上 2 s、704 个
        系统就停，玻璃替换一个也没换，与局部优化同值，所以不用（2026-10-18 实测）。
        Hammer (kind="hammer") or global search (kind="global"), cancelled after max_seconds; afterwards the editor holds
        the best system found. Neither ends on its own and TargetRunTimeM does not stop them. Hammer's automatic mode does
        end, but on the Cooke triplet it stopped after 2 s and 704 systems, substituted no glass and matched the local
        optimum, so it is not used (measured 2026-10-18)."""
        zosapi = _load_zosapi(self.root)
        opt = zosapi.Tools.Optimization
        tools = self.system.Tools
        if kind == "hammer":
            tool = tools.OpenHammerOptimization()
        elif kind == "global":
            tool = tools.OpenGlobalOptimization()
        else:
            raise SaosError(ErrorCode.BAD_ARGS, "kind must be hammer or global")
        if tool is None:
            raise SaosError(ErrorCode.ZOS_ERROR, f"OpticStudio cannot open the {kind} optimiser (another tool is open, or the licence lacks it)")
        lde = self.system.LDE
        materials = lambda: [str(lde.GetSurfaceAt(i).Material) for i in range(lde.NumberOfSurfaces)]  # noqa: E731
        before = materials()
        try:
            tool.Algorithm = opt.OptimizationAlgorithm.DampedLeastSquares
            if cores:
                tool.NumberOfCores = int(cores)
            if kind == "hammer":
                tool.AutomaticOptimization = False
                best = lambda: float(tool.CurrentMeritFunction)  # noqa: E731
            else:
                tool.NumberToSave = opt.OptimizationSaveCount.Save_10
                best = lambda: float(tool.CurrentMeritFunction(1))  # noqa: E731
            stopped_by, elapsed = self._run_optimizer(tool, lambda: {"merit": best(), "systems": int(tool.Systems)},
                                                      max_seconds, on_progress, should_cancel)
            out = {
                "initial_merit": float(tool.InitialMeritFunction),
                "final_merit": best(),
                "systems": int(tool.Systems),
                "variables": int(tool.Variables),
                "targets": int(tool.Targets),
                "succeeded": bool(tool.Succeeded),
                "stopped_by": stopped_by,
                "elapsed_s": elapsed,
            }
            if kind == "global":
                out["best_merits"] = [float(tool.CurrentMeritFunction(k)) for k in range(1, 11)]
        finally:
            tool.Close()
        # 玻璃替换常换成停产牌号（Cooke 上 3 次 Hammer、1 次全局搜索共 12 个替换，9 个是 Obsolete），把状态一并报出 / substitution often picks obsolete glasses
        catalogs = self._catalogs_in_use()
        out["materials_changed"] = [{"surface": i, "before": a, "after": b, "status": self.glass_status(b, catalogs)}
                                    for i, (a, b) in enumerate(zip(before, materials())) if a.upper() != b.upper()]
        return out

    @staticmethod
    def _run_optimizer(tool, reading, max_seconds, on_progress, should_cancel) -> tuple[str | None, float]:
        """异步运行一个优化工具直到结束；超时或收到取消请求时调用 Cancel。返回 (stopped_by, 用时 s)。
        进度回调出错（例如写 progress.json 被拒）只记日志，优化照常跑完；循环里其他异常先取消并等工具真正停下再抛出，
        否则 Close 关不掉运行中的工具，它会一直占着系统，之后的追迹和优化都打不开工具（2026-10-19 nightvision 任务 021–024）。
        Run an optimisation tool to its end; Cancel on a timeout or a cancel request. Returns (stopped_by, seconds).
        A failing progress callback (progress.json refused, …) is only logged and the run goes on; any other error in the
        loop first cancels the tool and waits until it has stopped, because Close does not close a running tool, which then
        holds the system and blocks every later trace and optimisation (nightvision job, scripts 021–024, 2026-10-19)."""
        started = time.monotonic()
        stopped_by = None
        progress_error = None
        tool.Run()
        try:
            while tool.IsRunning:
                time.sleep(0.5)
                elapsed = time.monotonic() - started
                if on_progress is not None:
                    try:
                        on_progress({"progress": int(tool.Progress), **reading(), "elapsed_s": round(elapsed, 1)})
                    except Exception as exc:  # noqa: BLE001 - 进度只是显示 / progress is display only
                        if progress_error is None:
                            progress_error = f"{type(exc).__name__}: {exc}"
                            log.warning("optimiser progress callback failed, the run goes on: %s", progress_error)
                if stopped_by is None and should_cancel is not None and should_cancel():
                    stopped_by = "cancel"
                elif stopped_by is None and max_seconds is not None and elapsed > max_seconds:
                    stopped_by = "timeout"
                if stopped_by is not None and tool.CanCancel:
                    tool.Cancel()
        except BaseException:
            ZosBackend._stop_tool(tool)
            raise
        tool.WaitForCompletion()
        return stopped_by, round(time.monotonic() - started, 1)

    @staticmethod
    def _stop_tool(tool, wait_s: float = 30.0) -> None:
        """出错后尽力让运行中的工具停下：Cancel，再等它结束（最多 wait_s 秒）。/ Stop a running tool after an error, best effort."""
        try:
            if tool.IsRunning and tool.CanCancel:
                tool.Cancel()
            deadline = time.monotonic() + wait_s
            while tool.IsRunning and time.monotonic() < deadline:
                time.sleep(0.2)
            if not tool.IsRunning:
                tool.WaitForCompletion()
        except Exception:  # noqa: BLE001 - 已经在处理另一个异常 / already handling another error
            log.exception("could not stop the optimisation tool")

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
        image = self.system.LDE.NumberOfSurfaces - 1
        return [{"ray": list(ray), "error": error, "vignette": vignette}
                for ray, (error, vignette, *_) in zip(rays, self._batch(rays, image)) if error or vignette]

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
        """结构交叠检查（saos.geometry）：面相交、镜片穿插。多重结构时逐个结构检查（变焦、调焦的镜组在别的结构里可能相撞），
        结果按结构合并，结束时回到原来的结构。
        Interference check (saos.geometry): crossing surfaces, interpenetrating lenses. With several configurations each is
        checked (zoom or focus groups may collide in another one), the results are merged and the current configuration restored."""
        if nsc.is_nsc(self.system):
            items = nsc.objects(_load_zosapi(self.root), self.system)
            nsc.attach_meshes(self.system, items)
            return geometry.nsc_overlaps(items)
        lde, mce = self.system.LDE, self.system.MCE
        count = int(mce.NumberOfConfigurations)
        if count < 2:
            return geometry.interference(self._geometry_items(range(1, lde.NumberOfSurfaces - 1), {}))
        current = int(mce.CurrentConfiguration)
        results = []
        try:
            for c in range(1, count + 1):
                mce.SetCurrentConfiguration(c)
                results.append(geometry.interference(self._geometry_items(range(1, lde.NumberOfSurfaces - 1), {})))
        finally:
            mce.SetCurrentConfiguration(current)
        return geometry.merge_configurations(results)

    def _geometry_items(self, indices, transforms: dict[int, Any]) -> list[dict[str, Any]]:
        """交叠检查的面：网格取零件外缘（geometry.part_radii：机械半口径、崩边区，一片镜片一个外径），不只是净口径；
        净口径外面形照常延伸，读不到时接平台。
        Surfaces for the interference check, sampled out to the part's outer edge (geometry.part_radii: mechanical
        semi-diameter, chip zone, one outer diameter per lens), not just the clear aperture; beyond the clear aperture the
        surface shape continues, or a flat annulus where it has no sag."""
        lde = self.system.LDE
        items = []
        for i in indices:
            row = lde.GetSurfaceAt(i)
            tf = transforms[i] if i in transforms else self._global(i)
            item = {"index": i, "type": str(row.TypeName), "comment": str(row.Comment), "material": str(row.Material), "sd": float(row.SemiDiameter)}
            if tf is not None and item["sd"] > 0 and "Coordinate" not in item["type"]:
                ap = geometry.aperture(self._aperture_dict(row), item["sd"])
                item.update(clear=geometry.extent(ap), edge=self._part_edge(row), ap=ap, rot=tf[0], off=tf[1])
            items.append(item)
        radii = geometry.part_radii(items)
        for item in items:
            if "clear" not in item:
                continue
            i, ap, clear = item["index"], item["ap"], item["clear"]
            outer = radii.get(i, clear)
            if not ap["explicit"] and outer > clear:
                ap = {**ap, "hx": outer, "hy": outer}
            grid = geometry.polar_points(geometry.extent(ap))
            sags = geometry.fill_beyond(grid, [self._sag(i, x, y) for x, y in grid], clear)
            item.update(sd=geometry.extent(ap), ap=ap, sags=geometry.fill_outside(grid, sags, ap))
        return items

    @staticmethod
    def _part_edge(row) -> float:
        """零件外缘半径：机械半口径与净半口径加崩边区中的较大者；读不到的列跳过。
        The part's outer edge radius: the larger of the mechanical semi-diameter and the clear one plus the chip zone."""
        clear = float(row.SemiDiameter)
        edge = clear
        for read in (lambda: float(row.MechanicalSemiDiameter), lambda: clear + float(row.ChipZone)):
            try:
                value = read()
            except Exception:  # noqa: BLE001 - 某些面型没有这些列 / some surface types lack these columns
                continue
            if math.isfinite(value):
                edge = max(edge, value)
        return edge

    def viz_data(self, folder: Path | None, *, pupil_grid: int = 31, fan: int = 7, full: bool = True, rays: bool = True) -> dict[str, Any]:
        """saos ui 用的数据：逐面全局几何、布局光线、Lens Data、交叠检查；full 时另有点列图光线、点列图 RMS/GEO 和 FFT MTF
        （每轮的 viz.json）。不带 full 是改动镜头后给界面的当前视图。
        几何与光线坐标都来自 ZOS-API（GetGlobalMatrix、GetSag、批量光线追迹），这里只做坐标变换。
        Data for saos ui: global geometry, layout rays, Lens Data and the interference check; with full also spot rays,
        Standard Spot RMS/GEO and FFT MTF (a round's viz.json). Without full it is the live view after a lens change.
        rays=False 时不追迹布局光线：优化运行中工具被优化器占着，只能读几何（同一系统同时只能开一个工具）。
        rays=False skips the layout rays: while an optimisation runs its tool is open and only geometry can be read."""
        system = self.system
        if nsc.is_nsc(system):
            return self._viz_nsc(full)
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

        def why_empty(out: dict[str, Any]) -> str:
            # OpticStudio 把原因写在文本导出里（例如 "Cannot compute when chief ray cannot be traced"）
            head = (out.get("text_head") or "").strip().splitlines()
            return (head[0] if head else "") or "; ".join(p["message"] for p in out.get("problems") or []) or "no data"

        def run(name: str, wanted: dict[str, Any], keep: tuple[str, ...], file: str | None = None) -> dict[str, Any] | None:
            started = time.monotonic()
            for settings in (wanted, {}):
                try:
                    out = self.analysis(name, settings, folder / f"{file or name}.txt")
                    timings[file or name] = round(time.monotonic() - started, 2)
                    if out["status"] == "empty":
                        errors[file or name] = why_empty(out)
                    return {"series": out["series"], "spot": out.get("spot"), "settings": {s: out["settings"].get(s) for s in keep}}
                except SaosError as exc:
                    if settings:
                        continue
                    errors[file or name] = exc.message
                except Exception as exc:  # noqa: BLE001 - 一个分析失败不影响其余数据
                    errors[file or name] = f"{type(exc).__name__}: {exc}"
                    break
            return None

        spot_analysis = run("StandardSpot", {"ReferTo": "ChiefRay"}, ("ReferTo", "Pattern", "RayDensity"))
        mtf_keep = ("MaximumFrequency", "SampleSize", "Type", "ShowDiffractionLimit")
        mtf = run("FftMtf", {"ShowDiffractionLimit": True}, mtf_keep)
        if mtf is not None and not mtf["series"] and len(fields) > 1:
            # 全部视场一起算时，只要一个视场的主光线追不出，FFT MTF 整个不给数据（2026-10-20 在 fiber-printed 第 0 轮复现：
            # 第 3、5 视场失败，其余单独算都正常）；逐个视场算，失败的视场写进 errors
            # with all fields at once, one field whose chief ray fails empties the whole FFT MTF; compute field by field
            whole = errors.pop("FftMtf", "no data")
            series, missing = [], []
            for f in range(1, len(fields) + 1):
                one = run("FftMtf", {"ShowDiffractionLimit": True, "Field": f}, mtf_keep, file=f"FftMtf-field{f}")
                reason = errors.pop(f"FftMtf-field{f}", None)
                if one is None or not one["series"]:
                    missing.append(f"field {f}: {reason or 'no data'}")
                    continue
                # 衍射极限每次都附带一条，只留第一条 / each run carries the diffraction limit; keep the first
                series += [s for s in one["series"] if not (series and "diff" in s["description"].lower())]
                mtf["settings"] = one["settings"]
            mtf["series"] = series
            mtf["per_field"] = True
            errors["FftMtf"] = f"all fields at once: {whole}" + (f"; computed field by field, missing {'; '.join(missing)}" if missing else "; computed field by field")
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

    def _viz_nsc(self, full: bool) -> dict[str, Any]:
        """界面用的非序列数据：物体轮廓（全局坐标）和物体表；full（每轮的 viz.json）时另给各探测器在内存里的读数
        （上一次追迹留下的，判定若追迹过就是判定最后一次追迹的结果）。没有布局光线。
        Non-sequential data for the window: object outlines in global coordinates and the object table; with full also
        each detector's readings currently in memory (left by the last trace). No layout rays."""
        zosapi = _load_zosapi(self.root)
        system = self.system
        started = time.monotonic()
        items = nsc.objects(zosapi, system)
        nsc.attach_meshes(system, items)
        info = self.system_info()
        primary = next((k for k, w in enumerate(info["wavelengths"], start=1) if w["primary"]), 1)
        out: dict[str, Any] = {
            "system": {"mode": "non_sequential", "title": info["title"], "lens_units": info["lens_units"], "wavelengths": info["wavelengths"],
                       "primary_wave": primary, "nsc": info["nsc"], "non_axial": True},
            "objects": nsc.drawing(items),
            "object_data": [{k: v for k, v in item.items() if k != "global" and not k.startswith("mesh_")} for item in items],
            "surfaces": [], "layout": {"rays": [], "rays_paused": False}, "lens_data": [],
            "interference": geometry.nsc_overlaps(items),
        }
        errors: dict[str, str] = {}
        if full:
            detectors = []
            for item in items:
                if item["kind"] != "detector":
                    continue
                try:
                    detectors.append(nsc.detector(zosapi, system, item["index"], grid=True))
                except Exception as exc:  # noqa: BLE001 - 一个探测器读不出不影响其余
                    errors[f"detector {item['index']}"] = f"{type(exc).__name__}: {exc}"[:300]
            out["detectors"] = detectors
        out["timings_s"] = {"nsc": round(time.monotonic() - started, 2)}
        out["errors"] = errors
        return out

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
