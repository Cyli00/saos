"""不启动 Zemax 的假后端，只供单元测试使用。

假镜头文件虽然以 .zmx 结尾，内容是 JSON：{"title": str, "surfaces": [{...}, ...]}。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from saos.backend.base import NUMERIC_PARAMS, SURFACE_PARAMS, Backend, parse_numeric
from saos.protocol import ErrorCode, SaosError

FAKE_MATERIALS = {"", "N-BK7", "SF2", "H-K9L", "H-ZF2", "MIRROR"}


def fake_surface(index: int, **overrides: Any) -> dict[str, Any]:
    surface = {
        "index": index,
        "type": "Standard",
        "comment": "",
        "radius": float("inf"),
        "thickness": 0.0,
        "material": "",
        "semi_diameter": 0.0,
        "conic": 0.0,
        "is_stop": False,
        "solves": {k: "Fixed" for k in ("radius", "thickness", "material", "semi_diameter", "conic")},
    }
    surface.update(overrides)
    return surface


class FakeBackend(Backend):
    def __init__(self) -> None:
        self.started = False
        self.lens: dict[str, Any] | None = None
        self.file: Path | None = None

    def start(self) -> dict[str, Any]:
        self.started = True
        return {"mode": "fake", "version": "fake", "license": "fake", "program_dir": ""}

    def close(self) -> None:
        self.started = False

    def healthy(self) -> bool:
        return self.started

    def load(self, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            surfaces = data["surfaces"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise SaosError(ErrorCode.ZOS_LOAD_FAILED, f"cannot load {path}: {exc}") from exc
        self.lens = {"title": data.get("title", ""), "surfaces": surfaces}
        self.file = path

    def save_as(self, path: Path) -> None:
        if self.lens is None:
            raise SaosError(ErrorCode.NO_LENS_LOADED, "no lens loaded")
        # 用 repr 精度写出，模拟 .zmx 的高精度保存
        path.write_text(json.dumps(self.lens, allow_nan=True), encoding="utf-8")
        self.file = path

    def new_system(self) -> None:
        self.lens = None
        self.file = None

    def _require(self) -> dict[str, Any]:
        if self.lens is None:
            raise SaosError(ErrorCode.NO_LENS_LOADED, "no lens loaded")
        return self.lens

    def system_info(self) -> dict[str, Any]:
        lens = self._require()
        stop = next((s["index"] for s in lens["surfaces"] if s.get("is_stop")), 0)
        return {
            "file": str(self.file),
            "title": lens["title"],
            "lens_units": "Millimeters",
            "aperture": {"type": "EntrancePupilDiameter", "value": 10.0},
            "fields": {"type": "Angle", "items": [{"x": 0.0, "y": 0.0, "weight": 1.0}]},
            "wavelengths": [{"um": 0.5876, "weight": 1.0, "primary": True}],
            "material_catalogs": ["FAKE"],
            "num_surfaces": len(lens["surfaces"]),
            "stop_surface": stop,
        }

    def surfaces(self) -> list[dict[str, Any]]:
        return copy.deepcopy(self._require()["surfaces"])

    def set_surface(self, index: int, param: str, value: Any) -> dict[str, Any]:
        lens = self._require()
        if param not in SURFACE_PARAMS:
            raise SaosError(ErrorCode.BAD_ARGS, f"unsupported surface parameter {param!r}")
        if not isinstance(index, int) or not 0 <= index < len(lens["surfaces"]):
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {index!r} out of range")
        surface = lens["surfaces"][index]
        if param in NUMERIC_PARAMS:
            number = parse_numeric(param, value)
            # 模拟求解覆盖：非 Fixed/Variable 的单元格写不进去
            if surface["solves"].get(param) not in ("Fixed", "Variable"):
                raise SaosError(ErrorCode.VALUE_NOT_APPLIED, f"surface {index} {param} is overridden by a solve")
            surface[param] = number
        elif param == "material":
            name = "" if value is None else str(value).upper()
            if name not in FAKE_MATERIALS:
                raise SaosError(ErrorCode.BAD_ARGS, f"{value!r} is not in the catalogs in use")
            surface[param] = name
        else:
            surface[param] = "" if value is None else str(value)
        return copy.deepcopy(surface)

    def analysis_types(self) -> list[str]:
        return ["FakeCurve"]

    def analysis(self, name: str, settings: dict[str, Any], text_path: Path, keep_open: bool = False) -> dict[str, Any]:
        """假分析：一条以 TOTR 为斜率的直线，用来测试导出和存档流程。"""
        if name != "FakeCurve":
            raise SaosError(ErrorCode.BAD_ARGS, f"unknown analysis type {name!r}")
        slope = self.operand("TOTR", 0, 0, [])
        x = [0.0, 0.5, 1.0]
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text("fake analysis" + chr(10), encoding="utf-8")
        return {"header": ["fake"], "messages": [], "series": [{"description": "line", "x_label": "x", "x": x, "y": {"v": [slope * v for v in x]}}],
                "grids": [], "text_file": True, "settings": dict(settings), "message": None}

    def operand(self, name: str, int1: int, int2: int, data: list[float]) -> float:
        lens = self._require()
        name = name.upper()
        if name == "TOTR":
            thicknesses = [s["thickness"] for s in lens["surfaces"][1:-1]]
            return float(sum(t for t in thicknesses if t != float("inf")))
        if name == "CRASH":
            raise RuntimeError("unexpected exception simulated by the fake backend")
        raise SaosError(ErrorCode.BAD_ARGS, f"the fake backend does not support operand {name!r}")
