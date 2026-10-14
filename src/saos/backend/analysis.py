"""ZOS-API 分析的设置与结果导出（只供 ZosBackend 使用）。

结果分四类，导出为普通 JSON：数据序列（MTF、光线扇图等曲线）、数据网格（波前图、PSF 等二维数据，
超过 MAX_GRID 时按步长降采样）、点列图的 RMS/GEO 半径，以及分析窗口的表头文字。
任何分析都另存一份 OpticStudio 自己的文本导出（GetTextFile），作为完整数据的依据。
"""

from __future__ import annotations

import math
from typing import Any

from saos.protocol import ErrorCode, SaosError

MAX_GRID = 256
# 这三类设置是对象而不是数值：用编号选择，"all" 表示全部（Surface 的 "image" 表示像面）
SELECTORS = {
    "Field": ("SetFieldNumber", "UseAllFields"),
    "Wavelength": ("SetWavelengthNumber", "UseAllWavelengths"),
    "Surface": ("SetSurfaceNumber", "UseImageSurface"),
}


def idm(zosapi, name: str):
    value = getattr(zosapi.Analysis.AnalysisIDM, name, None)
    if value is None:
        raise SaosError(ErrorCode.BAD_ARGS, f"unknown analysis type {name!r}; run 'saos lens analysis --list'")
    return value


def analysis_names(zosapi) -> list[str]:
    enum = zosapi.Analysis.AnalysisIDM
    return sorted(str(n) for n in type(enum.StandardSpot).GetNames(type(enum.StandardSpot)))


def settings_of(analysis):
    """GetSettings 返回基接口 IAS_；__implementation__ 才有 Field、SampleSize 等具体属性。"""
    settings = analysis.GetSettings()
    return getattr(settings, "__implementation__", settings)


def describe_settings(impl) -> dict[str, str]:
    out = {}
    for name in dir(impl):
        if name.startswith("_") or name[0].islower() or name.startswith(("get_", "set_")):
            continue
        try:
            value = getattr(impl, name)
        except Exception:  # noqa: BLE001 - 部分属性在当前系统下不可读
            continue
        if callable(value) and name not in SELECTORS:
            continue
        out[name] = str(value)
    return out


def _convert(current: Any, value: Any) -> Any:
    kind = type(current)
    if isinstance(current, bool):
        return value if isinstance(value, bool) else str(value).lower() in ("1", "true", "yes", "on")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(value)
    if isinstance(current, float):
        return float(value)
    if isinstance(current, str):
        return str(value)
    # .NET 枚举：按成员名取值
    if hasattr(kind, "GetNames") or hasattr(kind, "__members__") or str(kind).startswith("<enum"):
        member = getattr(kind, str(value), None)
        if member is None:
            names = [n for n in dir(kind) if not n.startswith("_")]
            raise SaosError(ErrorCode.BAD_ARGS, f"{value!r} is not a member of {kind.__name__}", {"members": names[:60]})
        return member
    return value


def apply_settings(impl, values: dict[str, Any]) -> None:
    for key, value in values.items():
        if not hasattr(impl, key):
            raise SaosError(ErrorCode.BAD_ARGS, f"analysis has no setting {key!r}", {"settings": describe_settings(impl)})
        if key in SELECTORS:
            setter, use_all = SELECTORS[key]
            target = getattr(impl, key)
            if str(value).lower() in ("all", "image"):
                getattr(target, use_all)()
            else:
                getattr(target, setter)(int(value))
            continue
        current = getattr(impl, key)
        try:
            setattr(impl, key, _convert(current, value))
        except SaosError:
            raise
        except Exception as exc:  # noqa: BLE001 - 设置值被 OpticStudio 拒绝
            raise SaosError(ErrorCode.BAD_ARGS, f"cannot set {key}={value!r}: {exc}") from exc


def _finite(x: float) -> float | None:
    return x if math.isfinite(x) else None


def _series(r) -> list[dict[str, Any]]:
    out = []
    for i in range(int(r.NumberOfDataSeries)):
        s = r.GetDataSeries(i)
        x = [float(v) for v in s.XData.Data]
        data = s.YData.Data
        cols = int(s.YData.Cols)
        labels = [str(label) for label in s.SeriesLabels]
        y = {labels[c] if c < len(labels) else str(c): [_finite(float(data[k, c])) for k in range(len(x))] for c in range(cols)}
        out.append({"description": str(s.Description), "x_label": str(s.XLabel), "x": x, "y": y})
    return out


def _grids(r) -> list[dict[str, Any]]:
    out = []
    for i in range(int(r.NumberOfDataGrids)):
        g = r.GetDataGrid(i)
        nx, ny = int(g.Nx), int(g.Ny)
        values = g.Values
        step = max(1, math.ceil(max(nx, ny) / MAX_GRID))
        rows = [[_finite(float(values[iy, ix])) for ix in range(0, nx, step)] for iy in range(0, ny, step)]
        flat = [v for row in rows for v in row if v is not None]
        out.append(
            {
                "description": str(g.Description),
                "nx": nx,
                "ny": ny,
                "dx": float(g.Dx),
                "dy": float(g.Dy),
                "min_x": float(g.MinX),
                "min_y": float(g.MinY),
                "value_label": str(g.ValueLabel),
                "step": step,
                "values": rows,
                "stats": {"min": min(flat), "max": max(flat), "mean": sum(flat) / len(flat)} if flat else None,
            }
        )
    return out


def _spot(r, fields: int, waves: int) -> dict[str, Any] | None:
    data = getattr(r, "SpotData", None)
    if data is None:
        return None
    try:
        rms = [[float(data.GetRMSSpotSizeFor(f, w)) for w in range(1, waves + 1)] for f in range(1, fields + 1)]
        geo = [[float(data.GetGeoSpotSizeFor(f, w)) for w in range(1, waves + 1)] for f in range(1, fields + 1)]
    except Exception:  # noqa: BLE001 - 不是点列图时 SpotData 不可用
        return None
    if not any(v for row in rms for v in row):
        return None
    return {"rms_radius": rms, "geo_radius": geo, "index": "[field-1][wave-1]"}


def extract(results, fields: int, waves: int) -> dict[str, Any]:
    header = [str(line) for line in results.HeaderData.Lines] if results.HeaderData is not None else []
    messages = [str(results.GetMessageAt(i)) for i in range(int(results.NumberOfMessages))]
    out: dict[str, Any] = {"header": header, "messages": messages, "series": _series(results), "grids": _grids(results)}
    spot = _spot(results, fields, waves)
    if spot:
        out["spot"] = spot
    return out
