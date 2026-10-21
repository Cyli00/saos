from __future__ import annotations

import copy
import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path

import numpy as np

from saos import config, glasscat
from saos.protocol import ErrorCode, SaosError


class MaterialError(SaosError):
    """材料数据无法使用：牌号不在目录、缺色散或热数据、超出适用范围、目录读不出。候选比较按这个类型把失败归为“材料”，不靠消息文字。
    A material whose data cannot be used: not in a catalog, no dispersion or thermal data, outside its range, a catalog that cannot be
    read. Candidate comparison files a failure under material by this type, never by its text."""


def unsupported(message):
    return MaterialError(ErrorCode.BACKEND_UNSUPPORTED, message, {"backend": "optiland"})


@lru_cache(maxsize=32)
def _read(path: str, modified: int):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def library(name: str, filename: str):
    path = config.REPO_ROOT / "library" / name / filename
    return _read(str(path), path.stat().st_mtime_ns) if path.exists() else {}


def _thermal_record(name: str):
    for record in library("thermal", "dndt.json").get("dndt_tables", []):
        if name.casefold() in [alias.casefold() for alias in record.get("materials", [])]:
            if "unconfirmed" in record.get("note", "").lower():
                raise unsupported(f"{name}: the source does not confirm the dn/dT units")
            return record
    return None


def raw_thermal_delta(record, wavelength, start, end):
    if start == end:
        return np.zeros_like(wavelength, dtype=float)
    lo, hi = sorted((start, end))
    sign = 1 if end >= start else -1
    formula = record.get("formula")
    if formula:
        valid = formula["t_range"]
        if not valid[0] <= lo <= hi <= valid[1]:
            raise unsupported(f"thermal table {record['id']} does not cover {lo}–{hi} C")
        w = np.asarray(wavelength, dtype=float)
        if "C0" in formula:
            rate = sum(formula[f"C{i}"] * w ** (-2 * i) for i in range(4))
        else:
            rate = sum(formula[f"D{i}"] * w ** power for i, power in enumerate((0, 2, -2, -4, -6)))
        return (end - start) * rate * 1e-6
    rows = record.get("rows", [])
    cuts = sorted({lo, hi, *(v for row in rows for v in row.get("t_range", []) if lo < v < hi)})
    delta = np.zeros_like(wavelength, dtype=float)
    for a, b in zip(cuts, cuts[1:]):
        eligible = [row for row in rows if row.get("t_range", [math.inf, -math.inf])[0] <= a
                    and b <= row.get("t_range", [math.inf, -math.inf])[1]]
        if len(eligible) != 1:
            raise unsupported(f"thermal table {record['id']} does not uniquely cover {a}–{b} C")
        row = eligible[0]
        scale = 1.0 if row.get("wavelength_unit", "nm") == "um" else 0.001
        samples = sorted((float(w) * scale, value) for w, value in row["values"].items())
        x, y = np.asarray(samples).T
        if np.any(wavelength < x[0]) or np.any(wavelength > x[-1]):
            raise unsupported(f"thermal table {record['id']} does not cover the requested wavelength")
        delta += (b - a) * np.interp(wavelength, x, y) * 1e-6
    return sign * delta


def expansion_factor(material, start, end, *, mechanical=None, override=None):
    if start == end:
        return 1.0
    metadata = getattr(material, "metadata", {})
    if override is not None:
        alpha = override["cte"] * 1e-6
    elif mechanical:
        entries = library("mechanics", "materials.json").get("materials", [])
        entry = next((m for m in entries if m["id"] == mechanical), None)
        if entry is None:
            raise unsupported(f"mechanical material {mechanical!r} is absent from library/mechanics")
        eligible = [r for r in entry.get("cte", []) if r.get("range") is not None
                    and r["range"][0] <= min(start, end) and max(start, end) <= r["range"][1]]
        if not eligible:
            raise unsupported(f"{mechanical}: no CTE covers {start}–{end} C")
        row = min(eligible, key=lambda r: r["range"][1] - r["range"][0])
        alpha = row["value"] * 1e-6
    elif metadata.get("expansion"):
        alpha = metadata["expansion"][0] * 1e-6
    else:
        name = getattr(material, "name", "")
        candidates = [r for r in library("thermal", "dndt.json").get("cte_manufacturer", [])
                      if r["material"].casefold() == name.casefold() and r["range"][0] <= min(start, end)
                      and max(start, end) <= r["range"][1]]
        if len(candidates) != 1:
            raise unsupported(f"{name or material.display_name}: missing thermal expansion coefficient")
        alpha = candidates[0]["value"] * 1e-6
    factor = 1 + alpha * (end - start)
    if not math.isfinite(factor) or factor <= 0:
        raise SaosError(ErrorCode.BAD_ARGS, "thermal expansion produces a non-positive scale")
    return factor


def environment_material(base, temperature, pressure, adjust):
    from optiland.materials.base import BaseMaterial
    from optiland.materials.material_file import MaterialFile

    if not adjust:
        return base

    class SaosEnvironmentMaterial(BaseMaterial):
        def __init__(self, material, t, p):
            super().__init__()
            self.base, self.temperature, self.pressure = material, t, p
            self.name = getattr(material, "name", material.display_name)
            self.metadata = copy.deepcopy(getattr(material, "metadata", {}))

        @property
        def display_name(self):
            return self.base.display_name

        def _cache_state(self):
            return self._state_key((self.base.to_dict(), self.temperature, self.pressure))

        def _calculate_n(self, wavelength, **kwargs):
            import optiland.backend as be

            td = self.metadata.get("thermal") or list(getattr(self.base, "thermdispcoef", []))
            reference = float(td[6]) if len(td) >= 7 else float(getattr(self.base, "_t0", None) or 20.0)
            t, p = self.temperature, self.pressure
            if self.name.lower() in ("air", "1.0"):
                return be.ones_like(wavelength)
            air = lambda w, temp, pres: MaterialFile._nair(self, w, temp, pres)
            relative_wave = wavelength * air(wavelength, t, p) / air(wavelength, reference, 1.0)
            absolute = self.base.n(relative_wave) * air(wavelength, reference, 1.0)
            dt = t - reference
            if dt:
                if len(td) >= 6:
                    d0, d1, d2, e0, e1, ltk = td[:6]
                    delta = (absolute * absolute - 1) / (2 * absolute) * (
                        d0 * dt + d1 * dt**2 + d2 * dt**3 + (e0 * dt + e1 * dt**2) / (wavelength**2 - ltk**2))
                else:
                    record = _thermal_record(self.name)
                    if record is None:
                        raise unsupported(f"{self.name}: missing dn/dT at {t} C")
                    delta = be.array(raw_thermal_delta(record, np.asarray(be.to_numpy(wavelength)), reference, t))
                absolute = absolute + delta
            return absolute / air(wavelength, t, p)

        def _calculate_k(self, wavelength, **kwargs):
            return self.base.k(wavelength)

        def to_dict(self):
            return {**super().to_dict(), "base": self.base.to_dict(), "temperature": self.temperature, "pressure": self.pressure}

        @classmethod
        def from_dict(cls, data):
            return cls(BaseMaterial.from_dict(data["base"]), data["temperature"], data["pressure"])

    return SaosEnvironmentMaterial(base, temperature, pressure)


def model_glass(value):
    """Zemax 模型玻璃在 Optiland 阶段的近似：Optiland 的 Abbe 材料（Buchdahl 三项模型，由 nd、Vd 定色散）。
    OpticStudio 的模型玻璃色散公式没有公开，两者的折射率不相等，只用于探索；交付前要换成真实牌号（Plan 2，用户决定）。
    ΔPgF 非零的模型玻璃 Abbe 材料表达不了，留给 OpticStudio。"""
    from optiland.materials import AbbeMaterial

    model = value.get("model") or {}
    if model.get("dpgf", 0.0):
        raise unsupported("a model glass with a nonzero ΔPgF has no Optiland approximation")
    result = AbbeMaterial(float(model["nd"]), float(model["vd"]), model="buchdahl")
    result.metadata = {"nd": float(model["nd"]), "vd": float(model["vd"]), "dpgf": 0.0, "approximation": "optiland-abbe-buchdahl"}
    return result


class MaterialStore:
    def __init__(self, records=None):
        self.records = copy.deepcopy(records or {})
        self.objects = {}
        self.catalogs = {}
        self.paths = None

    def _paths(self):
        if self.paths is None:
            self.paths = {}
            for directory in (glasscat.DEFAULT_GLASSCAT_DIR, config.REPO_ROOT / "library" / "materials"):
                if directory.exists():
                    for path in directory.glob("*"):
                        if path.is_file() and path.suffix.lower() == ".agf":
                            self.paths.setdefault(path.stem.upper(), []).append(path)
        return self.paths

    def catalog(self, name):
        key = name.upper()
        if key not in self.catalogs:
            paths = self._paths().get(key, [])
            if len(paths) > 1 and len({hashlib.sha256(p.read_bytes()).digest() for p in paths}) > 1:
                raise unsupported(f"catalog {key} has conflicting local AGF files")
            try:
                self.catalogs[key] = glasscat.load_optical_catalog(paths[0]) if paths else {}
            except (OSError, ValueError) as exc:
                raise MaterialError(ErrorCode.BACKEND_UNSUPPORTED, f"catalog {key} cannot be read: {exc}", {"backend": "optiland"}) from exc
        return self.catalogs[key]

    def get(self, value, catalogs):
        from optiland.materials import BaseMaterial, IdealMaterial
        from optiland.materials.data import DataMaterial

        if not value or (isinstance(value, str) and value.upper() == "AIR"):
            return IdealMaterial(1.0)
        key = json.dumps([catalogs, value], sort_keys=True)
        if key in self.objects:
            return self.objects[key]
        if key in self.records:
            result = BaseMaterial.from_dict(copy.deepcopy(self.records[key]))
        elif isinstance(value, dict):
            return model_glass(value)
        else:
            result = None
            for catalog in catalogs:
                record = self.catalog(catalog).get(str(value).upper())
                if record:
                    c, number = record["coefficients"], record["formula"]
                    if number == 2 and len(c) >= 6:
                        formula, coefficients = "formula 2", [0.0, *c[:6]]
                    elif number == 1 and len(c) >= 6:
                        formula, coefficients = "formula 3", [c[0], c[1], 2, c[2], -2, c[3], -4, c[4], -6, c[5], -8]
                    elif number == 3 and len(c) >= 6:
                        formula, coefficients = "formula 7", c[:6]
                    else:
                        raise unsupported(f"{catalog}/{value}: unsupported AGF formula {number}")
                    glass = record["glass"]
                    extinction = None
                    samples = {}
                    for row in record["transmission"]:
                        if len(row) < 3 or row[0] <= 0 or row[2] <= 0 or not 0 <= row[1] <= 1:
                            raise unsupported(f"{value}: invalid internal transmission record")
                        if row[1] == 0:
                            continue
                        k = -math.log(row[1]) * row[0] / (4 * math.pi * row[2] * 1000)
                        if row[0] in samples and not math.isclose(samples[row[0]], k, rel_tol=1e-9, abs_tol=1e-15):
                            raise unsupported(f"{value}: conflicting internal transmission records")
                        samples[row[0]] = k
                    if samples:
                        wavelengths = sorted(samples)
                        extinction = {"kind": "tabulated_k", "wavelengths_um": wavelengths, "values": [samples[w] for w in wavelengths]}
                    result = DataMaterial.from_coefficients(formula, coefficients, name=glass.name, extinction=extinction,
                        wavelength_range=record["range"], metadata={"catalog": glass.catalog, "nd": glass.nd, "vd": glass.vd,
                        "status": glass.status, "thermal": record["thermal"], "expansion": record["expansion"]})
                    break
            if result is None:
                entry = next((m for m in library("materials", "catalog.json").get("materials", [])
                              if str(value).casefold() in [str(m.get(k, "")).casefold() for k in ("id", "name", "agf_name")]), None)
                if entry is None and catalogs and all(self.catalog(c) for c in catalogs):
                    # 所用目录本机都有 AGF 而牌号不在其中：是处方写错了，不必再交给 OpticStudio 查
                    # every catalog in use has a local AGF and none lists the name: a prescription error, not a missing capability
                    raise MaterialError(ErrorCode.BAD_ARGS, f"material {value!r} is not in the catalogs {list(catalogs)} or library/materials")
                if entry is None or entry.get("dispersion") is None:
                    raise unsupported(f"material {value!r} has no exact sourced dispersion definition")
                model = entry["dispersion"]
                if model["formula"] == "sellmeier1_um":
                    formula, coefficients = "formula 2", [0.0, *model["coefficients"]]
                elif model["formula"] == "cauchy_nm":
                    a, b, c = model["coefficients"]
                    formula, coefficients = "formula 5", [a, b / 1e6, -2, c / 1e12, -4]
                else:
                    raise unsupported(f"{value}: unsupported library dispersion {model['formula']}")
                result = DataMaterial.from_coefficients(formula, coefficients, name=entry["agf_name"],
                    wavelength_range=[w / 1000 for w in model["valid_range_nm"]],
                    metadata={"catalog": "SAOS_MICRO_20261014", "source_ids": entry["source_ids"]})
        self.records[key] = result.to_dict()
        self.objects[key] = result
        return result
