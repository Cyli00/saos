"""不启动 Zemax 的假后端，只供单元测试使用。

假镜头文件虽然以 .zmx 结尾，内容是 JSON：{"title": str, "surfaces": [{...}, ...]}。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from saos import geometry
from saos.backend.base import MATERIAL_MODES, NUMERIC_PARAMS, Backend, check_param, check_size, is_par, parse_numeric
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
        "mech_semi_diameter": 0.0,
        "conic": 0.0,
        "is_stop": False,
        "solves": {k: "Fixed" for k in ("radius", "thickness", "material", "semi_diameter", "mech_semi_diameter", "conic")},
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

    def identity(self) -> dict[str, Any]:
        return {"backend": "fake", "version": "fake", "precision": None}

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
        self.lens = {"title": data.get("title", ""), "surfaces": surfaces, **({"non_axial": True} if data.get("non_axial") else {})}
        self.file = path

    def save_as(self, path: Path) -> None:
        if self.lens is None:
            raise SaosError(ErrorCode.NO_LENS_LOADED, "no lens loaded")
        # 用 repr 精度写出，模拟 .zmx 的高精度保存
        path.write_text(json.dumps(self.lens, allow_nan=True), encoding="utf-8")
        self.file = path

    def native_file(self) -> Path | None:
        return self.file

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

    def describe_model(self, folder: Path | None, prefix: str = "layout") -> dict[str, Any]:
        info = self.system_info()
        info["aperture"].update(telecentric_object=False, afocal_image=False)
        info["fields"]["normalization"] = "Radial"
        info["ray_aiming"] = "Off"
        surfaces = []
        for s in self.surfaces():
            material = s.get("material", "")
            source = {"kind": "none"} if not material else {"kind": "mirror"} if material == "MIRROR" else {"kind": "catalog", "name": material, "catalogs": ["FAKE"]}
            surfaces.append({**s, "material_source": s.get("material_source", source), "n": s.get("n", [1.5 if material else 1.0]),
                             "aperture": s.get("aperture", {"type": "None"}), "solve_detail": {}, "params": s.get("params", []), "global_vertex": None})
        return {**info, "surfaces": surfaces, "layout": {} if folder is None else {"3d": {"error": "fake backend draws no layout"}}}

    def set_surface(self, index: int, param: str, value: Any) -> dict[str, Any]:
        lens = self._require()
        check_param(param)
        if not isinstance(index, int) or not 0 <= index < len(lens["surfaces"]):
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {index!r} out of range")
        surface = lens["surfaces"][index]
        if is_par(param):
            # 假镜头的面型参数直接存在面数据里，键名就是 par<N> / fake surface-type parameters live in the surface under par<N>
            if param not in surface:
                raise SaosError(ErrorCode.BAD_ARGS, f"surface {index} has no numeric parameter {param[3:]}")
            surface[param] = parse_numeric(param, value)
        elif param in NUMERIC_PARAMS:
            number = parse_numeric(param, value)
            check_size(param, number)
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

    def merit_append(self, rows: list[dict[str, Any]]) -> list[int]:
        from saos.ctx.rows import compile_rows

        merit = self._require().setdefault("merit", [])
        position = next((i for i, r in enumerate(merit) if r["type"] == "DMFS"), len(merit))
        try:
            specs = compile_rows(rows, position + 1)
        except ValueError as exc:
            raise SaosError(ErrorCode.BAD_ARGS, f"merit rows: {exc}") from exc
        merit[position:position] = specs
        return list(range(position + 1, position + 1 + len(specs)))

    def insert_surface(self, index: int, surface_type: str = "Standard") -> str:
        surfaces = self._require()["surfaces"]
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index < len(surfaces):
            raise SaosError(ErrorCode.BAD_ARGS, f"a new surface goes at 1–{len(surfaces) - 1}, got {index!r}")
        surfaces.insert(index, fake_surface(index))
        for i, s in enumerate(surfaces):
            s["index"] = i
        return self.change_surface_type(index, surface_type)

    def change_surface_type(self, index: int, surface_type: str) -> str:
        surfaces = self._require()["surfaces"]
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index < len(surfaces) - 1:
            raise SaosError(ErrorCode.BAD_ARGS, f"only surfaces 1–{len(surfaces) - 2} can change type, got {index!r}")
        surfaces[index]["type"] = str(surface_type)
        return surfaces[index]["type"]

    def set_solve(self, index: int, param: str, solve: dict[str, Any]) -> dict[str, Any]:
        surfaces = self._require()["surfaces"]
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(surfaces):
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {index!r} out of range")
        name = {"fixed": "Fixed", "variable": "Variable", "pickup": "SurfacePickup", "marginal_ray_height": "MarginalRayHeight",
                "f_number": "FNumber"}[solve["type"]]
        surfaces[index]["solves"][param] = name
        return {"type": name}

    def vary(self, surface: int, param: str, on: bool, mode: str | None = None, catalog: str | None = None) -> str:
        lens = self._require()
        if param not in ("radius", "thickness", "conic", "material") and not is_par(param):
            raise SaosError(ErrorCode.BAD_ARGS, "only radius, thickness, conic, par<N> and material can be variables")
        if not 0 <= surface < len(lens["surfaces"]):
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {surface!r} out of range")
        row = lens["surfaces"][surface]
        if is_par(param) and param not in row:
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {surface} has no numeric parameter {param[3:]}")
        if param == "material" and on:
            if mode not in MATERIAL_MODES:
                raise SaosError(ErrorCode.BAD_ARGS, f"material needs --mode {' or '.join(MATERIAL_MODES)}")
            if row.get("material", "") in ("", "MIRROR"):
                raise SaosError(ErrorCode.BAD_ARGS, f"surface {surface} has no glass; set a material before making it vary")
        solve = ("MaterialModel" if mode == "model" else "MaterialSubstitute") if param == "material" and on else "Variable" if on else "Fixed"
        row.setdefault("solves", {})[param] = solve
        return solve

    def quick_focus(self) -> dict[str, Any]:
        """假后端不对焦，只报告像面前一面的厚度。/ the fake backend does not refocus; it reports the thickness before the image."""
        lens = self._require()
        index = len(lens["surfaces"]) - 2
        t = lens["surfaces"][index]["thickness"]
        return {"surface": index, "thickness_before": t, "thickness_after": t}

    def analysis_types(self) -> list[str]:
        return ["FakeCurve"]

    def analysis(self, name: str, settings: dict[str, Any], text_path: Path) -> dict[str, Any]:
        """假分析：一条以 TOTR 为斜率的直线，用来测试导出和存档流程。"""
        if name != "FakeCurve":
            raise SaosError(ErrorCode.BAD_ARGS, f"unknown analysis type {name!r}")
        slope = self.operand("TOTR", 0, 0, [])
        x = [0.0, 0.5, 1.0]
        text_path.parent.mkdir(parents=True, exist_ok=True)
        text_path.write_text("fake analysis" + chr(10), encoding="utf-8")
        return {"header": ["fake"], "messages": [], "series": [{"description": "line", "x_label": "x", "x": x, "y": {"v": [slope * v for v in x]}}],
                "grids": [], "problems": [], "status": "ok", "text_file": True, "settings": dict(settings), "message": None}

    def _geometry_items(self) -> list[dict[str, Any]]:
        """假几何：平面沿 z 轴排开，交叠检查照常算。/ Flat surfaces along z; the interference check runs as usual."""
        lens = self._require()
        items, z = [], 0.0
        for s in lens["surfaces"][1:-1]:
            sd = float(s.get("semi_diameter") or 0.0)
            items.append({"index": s["index"], "type": s["type"], "comment": s["comment"], "material": s["material"], "sd": sd,
                          "rot": [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0], "off": [0.0, 0.0, z],
                          "sags": [0.0] * len(geometry.polar_points(sd))})
            if s["thickness"] != float("inf"):
                z += s["thickness"]
        return items

    def interference(self) -> dict[str, Any]:
        return geometry.interference(self._geometry_items())

    def viz_data(self, folder: Path | None, *, full: bool = True, rays: bool = True) -> dict[str, Any]:
        """假数据：面沿 z 轴排开，光线平行于轴到像面，只用来测试导出、存档和界面读取。"""
        lens = self._require()
        surfaces, z = [], 0.0
        for s in lens["surfaces"][1:]:
            sd = float(s.get("semi_diameter") or 0.0)
            item = {"index": s["index"], "type": s["type"], "comment": s["comment"], "material": s["material"], "semi_diameter": sd,
                    "is_stop": s["is_stop"], "vertex": [0.0, 0.0, z]}
            if sd > 0:
                item["profile_y"] = [[0.0, y, z] for y in (-sd, 0.0, sd)]
                item["profile_x"] = [[x, 0.0, z] for x in (-sd, 0.0, sd)]
                item["rim"] = [[sd, 0.0, z], [0.0, sd, z], [-sd, 0.0, z], [0.0, -sd, z], [sd, 0.0, z]]
            surfaces.append(item)
            if s["thickness"] != float("inf"):
                z += s["thickness"]
        paths = [{"field": 0, "px": 0.0, "py": py, "points": [[0.0, py, v["vertex"][2]] for v in surfaces], "start_dir": [0.0, 0.0, 1.0]}
                 for py in (-1.0, 0.0, 1.0)] if rays else []
        base = {"system": {**self.system_info(), "primary_wave": 1, "normalization": "Radial", "normalized_fields": [[0.0, 0.0]],
                           "stop_semi_diameter": 10.0, "image_surface": len(lens["surfaces"]) - 1, "non_axial": bool(lens.get("non_axial"))},
                "surfaces": surfaces, "layout": {"wave": 1, "rays": paths, **({} if rays else {"rays_paused": True})}, "lens_data": self.surfaces(), "interference": self.interference()}
        if not full:
            return {**base, "timings_s": {}, "errors": {}}
        return {**base,
                "spot": {"reference": "chief ray, primary wavelength", "pupil_grid": 3, "fields": [{"field": 0, "chief": [0.0, 0.0], "points": [[[0.0, 0.0], [0.001, 0.0]]]}],
                         "rms_radius": [[1.0]], "geo_radius": [[2.0]], "analysis_settings": None},
                "mtf": {"series": [{"description": "Field: 0.00 (deg)", "x_label": "Spatial Frequency", "x": [0.0, 50.0], "y": {"Tangential": [1.0, 0.5], "Sagittal": [1.0, 0.4]}}],
                        "spot": None, "settings": {}},
                "timings_s": {}, "errors": {}}

    def operand(self, name: str, int1: int, int2: int, data: list[float]) -> float:
        lens = self._require()
        name = name.upper()
        if name == "TOTR":
            thicknesses = [s["thickness"] for s in lens["surfaces"][1:-1]]
            return float(sum(t for t in thicknesses if t != float("inf")))
        if name == "CRASH":
            raise RuntimeError("unexpected exception simulated by the fake backend")
        raise SaosError(ErrorCode.BAD_ARGS, f"the fake backend does not support operand {name!r}")
