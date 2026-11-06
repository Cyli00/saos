"""AGF 全字段读取与按 GCAT 顺序查找；数据只读，保留原目录字段。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..textio import decode_text
from .formulas import FORMULAS, dispersion
from .types import MaterialUnsupported


def catalog_dir() -> Path:
    override = os.environ.get("RTC_GLASSCAT")
    return Path(override) if override else Path(os.environ.get("USERPROFILE", Path.home())) / "Documents" / "Zemax" / "Glasscat"


@dataclass
class Glass:
    name: str
    formula: int
    nd: float
    vd: float
    coef: list[float] = field(default_factory=list)
    catalog: str = ""
    tref: float | None = 20.0
    thermal: tuple[float, ...] = (0.0,) * 6
    wavelength_range: tuple[float, float] = (0.0, 0.0)
    mil: float = 0.0
    exclude: int = 0
    status: int = 0
    melt_frequency: int = 0
    comment: str = ""
    ed: tuple[float, ...] = (0.0,) * 4
    ignore_expansion: bool = False
    negative_index: bool = False
    od: tuple[float, ...] = (-1.0,) * 6
    md: tuple[float | None, ...] = (None,) * 5
    it: list[tuple[float, ...]] = field(default_factory=list)
    bd: list[tuple[float, ...]] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)
    catalog_nd: float | None = None
    catalog_vd: float | None = None
    reference_error: str | None = None
    data_error: str | None = None

    @property
    def dpgf(self) -> float:
        return self.ed[3]

    @property
    def tce(self) -> float:
        return self.ed[0]

    def index(self, wavelength_um):
        """兼容旧 API：返回参考环境的相对目录值；系统温压由 CatalogGlass.index 处理。"""
        result = dispersion(wavelength_um, self.coef, self.formula, self.nd)
        return -result if self.negative_index else result


@dataclass
class CatalogDocument:
    comment: str = ""
    glasses: dict[str, Glass] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _float32_row(values):
    with np.errstate(over="ignore", invalid="ignore"):
        result = np.asarray(values, dtype=np.float32).astype(np.float64)
    if not np.all(np.isfinite(result)):
        raise MaterialUnsupported("AGF IT/BD 数据超出 float32 存储范围")
    return tuple(float(v) for v in result)


def _finish(glass):
    glass.it = sorted((row for row in glass.it if row[0] > 0), key=lambda row: row[0])
    glass.bd = [row for row in glass.bd if row[0] > 0]
    if len(glass.it) <= 1:
        values = glass.it[0][1:] if glass.it else (1.0, 1.0)
        glass.it = [_float32_row((w, *values)) for w in glass.wavelength_range if w > 0]
    lo, hi = glass.wavelength_range
    if lo > 0.4861327 or hi < 0.6562725:
        glass.nd, glass.vd = 1.0, 0.0
    elif glass.formula in FORMULAS:
        try:
            nd, nf, nc = glass.index([0.5875618, 0.4861327, 0.6562725])
        except MaterialUnsupported as exc:
            glass.reference_error = str(exc)
            return
        glass.nd = float(nd)
        glass.vd = float((nd - 1.0) / (nf - nc)) if nf != nc else 0.0


def _optional_number(token, missing):
    """OD/MD 这类不参与折射率的字段：占位符（?、_、-）与等级区间（1-2）按缺失处理。"""
    try:
        value = float(token)
    except ValueError:
        return missing
    return value if np.isfinite(value) else missing


def _comment(line):
    # 按文本模式读入的一行计：最多 140 字符，末位是换行或被终止符覆盖
    return line[3:].replace("\r\n", "\n")[:140][:-1].rstrip("\r\n")


def parse_agf(text: str, *, catalog: str = "") -> CatalogDocument:
    """解析整份目录。参与折射率计算的行（NM/CD/TD/LD/ED）格式无效时只作废该牌号（data_error），
    不连累同目录的其他牌号；成本、耐候、透过率等行里的占位符按缺失处理。"""
    document = CatalogDocument()
    current = None
    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        tokens = line.split()
        if not tokens:
            continue
        key = tokens[0].upper()
        if key == "CC":
            document.comment = _comment(line)
            continue
        if key == "NM":
            current = None
            if len(tokens) < 6:
                document.problems.append(f"{catalog}:{line_number}: NM 至少需要名称、公式、MIL、nd、Vd")
                continue
            try:
                current = Glass(tokens[1].upper()[:32], int(float(tokens[2])), float(tokens[4]), float(tokens[5]), catalog=catalog,
                                # 有的厂家（APEL）整行都写成 0.00000000E+00 / some makers write every field in scientific notation
                                mil=_optional_number(tokens[3], 0.0), exclude=int(float(tokens[6])) if len(tokens) > 6 else 0,
                                status=int(float(tokens[7])) if len(tokens) > 7 else 0,
                                melt_frequency=int(float(tokens[8])) if len(tokens) > 8 else 0)
            except ValueError:
                document.problems.append(f"{catalog}:{line_number}: NM 数值格式无效")
                continue
            current.catalog_nd, current.catalog_vd = current.nd, current.vd
            if len(document.glasses) >= 10000:
                raise MaterialUnsupported(f"{catalog}: 目录超过 10000 条记录")
            document.glasses.setdefault(current.name, current)
        if current is None:
            continue
        current.raw.append(line.rstrip("\r\n"))

        def numbers(count):
            result = [float(tokens[i]) if i < len(tokens) else 0.0 for i in range(1, count + 1)]
            if not all(np.isfinite(v) for v in result):
                raise ValueError("非有限 AGF 数值")
            return result

        if key == "GC":
            current.comment = _comment(line)
        elif key == "OD":
            current.od = tuple(_optional_number(v, -1.0) for v in (tokens[1:] + ["0"] * 6)[:6])
        elif key == "MD":
            values = list(current.md)
            for i, token in enumerate(tokens[1:6]):
                values[i] = _optional_number(token, None)
            current.md = tuple(values)
        elif key in ("IT", "BD"):
            # 透过率与双折射数据不参与序列追迹；含占位符的行丢弃，不作废牌号
            rows = current.it if key == "IT" else current.bd
            try:
                row = _float32_row(numbers(3 if key == "IT" else 4))
            except (ValueError, OverflowError, MaterialUnsupported):
                continue
            if len(rows) == 100:
                rows[-1] = row
            else:
                rows.append(row)
        elif key in ("CD", "TD", "LD", "ED"):
            try:
                if key == "CD":
                    current.coef = numbers(10)
                elif key == "TD":
                    current.thermal = tuple(numbers(6))
                    current.tref = float(tokens[7]) if len(tokens) > 7 else 20.0
                    if not np.isfinite(current.tref):
                        raise ValueError("非有限参考温度")
                elif key == "LD":
                    current.wavelength_range = tuple(numbers(2))
                else:
                    current.ed = tuple(numbers(4))
                    current.ignore_expansion = bool(int(float(tokens[5]))) if len(tokens) > 5 else False
                    current.negative_index = bool(int(float(tokens[6]))) if len(tokens) > 6 else False
                    current.od = (-1.0,) * 6
            except (ValueError, OverflowError):
                current.data_error = current.data_error or f"{catalog}:{line_number}: {key} 数值格式无效"
    for glass in document.glasses.values():
        if glass.data_error is None:
            _finish(glass)
    return document


class GlassCatalog:
    """每个 AGF 只解析一次；同名牌号按镜头 GCAT 顺序查找。"""

    def __init__(self, folder: Path | None = None):
        self.folder = Path(folder) if folder else catalog_dir()
        self._files = None
        self._parsed = {}

    def _index(self):
        if self._files is None:
            paths = sorted(self.folder.glob("*.[Aa][Gg][Ff]")) if self.folder.is_dir() else []
            self._files = {path.stem.upper(): path for path in paths}
        return self._files

    def _glasses(self, stem):
        if stem not in self._parsed:
            path = self._index().get(stem)
            self._parsed[stem] = parse_agf(decode_text(path.read_bytes()), catalog=stem).glasses if path else {}
        return self._parsed[stem]

    def get(self, name, catalogs=None):
        name = name.strip().upper()
        for stem in ([c.upper() for c in catalogs] if catalogs else self._index()):
            glass = self._glasses(stem).get(name)
            if glass is not None:
                return glass
        raise KeyError(f"玻璃库里找不到 {name!r}（{catalogs or self.folder}）")

    def glasses(self, catalog):
        return tuple(self._glasses(catalog.upper()).values())
