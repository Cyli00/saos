"""ZOS-API 分析的设置与结果导出（只供 ZosBackend 使用）。

结果分四类，导出为普通 JSON：数据序列（MTF、光线扇图等曲线）、数据网格（波前图、PSF 等二维数据，
超过 MAX_GRID 时按步长降采样）、点列图的 RMS/GEO 半径，以及分析窗口的表头文字。
任何分析都另存一份 OpticStudio 自己的文本导出（GetTextFile），作为完整数据的依据。
"""

from __future__ import annotations

import math
import re
from typing import Any

from saos.protocol import ErrorCode, SaosError

MAX_GRID = 256


def zernike_values(text: str, terms: tuple[int, ...]) -> dict[int, float]:
    """读取分析导出的系数与光线积分统计；不以系数统计冒充光线统计。"""
    number = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[EeDd][-+]?\d+)?"
    values = {}
    rays = False
    labels = {"rms (to centroid)": -4, "rms (to chief)": -5, "rms (to chief ray)": -5,
              "rms (to zero)": -6, "p-v (to chief)": -7, "p-v (to chief ray)": -7,
              "p-v (to centroid)": -8, "variance": -3, "strehl ratio": -2}
    for line in text.splitlines():
        coefficient = re.match(rf"\s*Z\s*(\d+)\s*(?:[:=]\s*)?({number})(?:\s|$)", line, re.IGNORECASE)
        if coefficient:
            values[int(coefficient[1])] = float(coefficient[2].replace("D", "E").replace("d", "e"))
            continue
        lower = line.strip().lower()
        if "from integration of the rays" in lower:
            rays = True
            continue
        if lower.startswith("from "):
            rays = False
        found = re.search(rf"[:=]\s*({number})", line)
        if not found:
            continue
        value = float(found[1].replace("D", "E").replace("d", "e"))
        label = re.split(r"[:=]", lower, maxsplit=1)[0].strip()
        if "fit" in label and "rms" in label and ("error" in label or "residual" in label):
            values[-1] = value
        elif "fit" in label and ("maximum" in label or "max" in label or "peak" in label):
            values[0] = value
        elif rays:
            for key, term in labels.items():
                if label == key or label.startswith(key + " "):
                    values[term] = value
                    break
    missing = [term for term in terms if term not in values or not math.isfinite(values[term])]
    if missing:
        raise SaosError(ErrorCode.ZOS_ERROR, f"Zernike Standard text export lacks finite terms {missing}; no MFE fallback was applied")
    return {term: values[term] for term in terms}
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


def _series(r, problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i in range(int(r.NumberOfDataSeries)):
        try:
            s = r.GetDataSeries(i)
            # 实测相对照度在模型给不出结果时 XData 为 None（2026-10-19 nightvision）；记下而不是在 .Data 上抛 AttributeError
            # relative illumination had XData None on a model without a result (nightvision, 2026-10-19); note it instead of failing on .Data
            if s is None or s.XData is None or s.YData is None:
                problems.append({"part": f"series[{i}]", "kind": "empty_series",
                                 "message": "OpticStudio returned this data series without X or Y data",
                                 "description": str(s.Description) if s is not None else None})
                continue
            x = [float(v) for v in s.XData.Data]
            data = s.YData.Data
            cols = int(s.YData.Cols)
            labels = [str(label) for label in s.SeriesLabels]
            y = {labels[c] if c < len(labels) else str(c): [_finite(float(data[k, c])) for k in range(len(x))] for c in range(cols)}
            out.append({"description": str(s.Description), "x_label": str(s.XLabel), "x": x, "y": y})
        except Exception as exc:  # noqa: BLE001 - 一条序列读不出不连累其他结果 / one unreadable series spares the rest
            problems.append({"part": f"series[{i}]", "kind": "extract_failed", "message": f"{type(exc).__name__}: {exc}"[:300]})
    return out


def _grids(r, problems: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for i in range(int(r.NumberOfDataGrids)):
        try:
            out.append(_grid(r.GetDataGrid(i)))
        except Exception as exc:  # noqa: BLE001 - 同上 / as above
            problems.append({"part": f"grids[{i}]", "kind": "extract_failed", "message": f"{type(exc).__name__}: {exc}"[:300]})
    return out


def _grid(g) -> dict[str, Any]:
    if g is None or g.Values is None:
        raise SaosError(ErrorCode.ZOS_ERROR, "OpticStudio returned a data grid without values")
    nx, ny = int(g.Nx), int(g.Ny)
    values = g.Values
    step = max(1, math.ceil(max(nx, ny) / MAX_GRID))
    rows = [[_finite(float(values[iy, ix])) for ix in range(0, nx, step)] for iy in range(0, ny, step)]
    flat = [v for row in rows for v in row if v is not None]
    return {
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
    # OpticStudio 忽略 GetRMSSpotSizeFor/GetGeoSpotSizeFor 的波长参数，各列都是分析设置里所选波长的值；选全部波长时，
    # 每个波长追的光线数与其权重成正比，权重小于 1 的波长常漏掉边缘光线，GEO 偏小（2026-10-20 Cooke 样例实测）
    # the wave argument is ignored; with all wavelengths, rays per wavelength scale with its weight, so GEO can miss rim rays
    return {"rms_radius": rms, "geo_radius": geo, "index": "[field-1][wave-1]",
            "note": "every wave column holds the value for the analysis' Wavelength setting; with all wavelengths OpticStudio traces "
                    "rays per wavelength in proportion to its weight, so GEO can miss the rim rays of wavelengths weighted below 1"}


def _guarded(problems: list[dict[str, Any]], part: str, read, default):
    try:
        return read()
    except Exception as exc:  # noqa: BLE001
        problems.append({"part": part, "kind": "extract_failed", "message": f"{type(exc).__name__}: {exc}"[:300]})
        return default


def extract(results, fields: int, waves: int) -> dict[str, Any]:
    """导出结果的各部分，读不出的部分记在 problems 里（part、kind、message），其余照常返回。
    Export every part of the results; parts that cannot be read go to problems (part, kind, message), the rest is returned."""
    problems: list[dict[str, Any]] = []
    header = _guarded(problems, "header",
                      lambda: [str(line) for line in results.HeaderData.Lines] if results.HeaderData is not None else [], [])
    messages = _guarded(problems, "messages", lambda: [str(results.GetMessageAt(i)) for i in range(int(results.NumberOfMessages))], [])
    out: dict[str, Any] = {"header": header, "messages": messages,
                           "series": _guarded(problems, "series", lambda: _series(results, problems), []),
                           "grids": _guarded(problems, "grids", lambda: _grids(results, problems), [])}
    spot = _spot(results, fields, waves)
    if spot:
        out["spot"] = spot
    out["problems"] = problems
    return out


def outcome(out: dict[str, Any]) -> str:
    """ok：全部读出；partial：读出了一部分；empty：没有任何数值（模型给不出结果、接口限制或提取缺陷，看 problems、messages 和文本导出）。
    ok: everything was read; partial: some of it; empty: no numbers at all (no result for this model, an interface
    limit or an extractor defect; see problems, messages and the text export)."""
    has_data = bool(out.get("series") or out.get("grids") or out.get("spot"))
    if not out.get("problems"):
        return "ok" if has_data else "empty"
    return "partial" if has_data else "empty"
