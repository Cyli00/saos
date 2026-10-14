"""读取 Zemax .AGF 玻璃目录，并按 nd/νd 查找最接近的牌号。

NM 行格式（OpticStudio 手册 PDF p1295 附近）：NM <名称> <色散公式> <MIL> <nd> <vd> <排除> <状态> <熔炼频率>
状态码：0 Standard、1 Preferred、2 Obsolete、3 Special、4 Melt。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

STATUS_NAMES = {0: "Standard", 1: "Preferred", 2: "Obsolete", 3: "Special", 4: "Melt"}
DEFAULT_GLASSCAT_DIR = Path.home() / "Documents" / "Zemax" / "Glasscat"


@dataclass(frozen=True)
class Glass:
    catalog: str
    name: str
    nd: float
    vd: float
    status: str


def _decode(raw: bytes) -> str:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    return raw.decode("latin-1")


def load_catalog(path: Path) -> list[Glass]:
    catalog = path.stem.upper()
    glasses = []
    for line in _decode(path.read_bytes()).splitlines():
        if not line.startswith("NM "):
            continue
        parts = line.split()
        try:
            status = int(float(parts[7])) if len(parts) > 7 else 0
            glasses.append(Glass(catalog, parts[1], float(parts[4]), float(parts[5]), STATUS_NAMES.get(status, str(status))))
        except (IndexError, ValueError):
            continue
    return glasses


def load_catalogs(names: list[str], directory: Path = DEFAULT_GLASSCAT_DIR) -> list[Glass]:
    found = {p.stem.upper(): p for p in directory.iterdir() if p.suffix.lower() == ".agf"}
    glasses = []
    for name in names:
        if name.upper() not in found:
            raise FileNotFoundError(f"glass catalog {name} not found in {directory}")
        glasses.extend(load_catalog(found[name.upper()]))
    return glasses


def nearest(nd: float, vd: float, glasses: list[Glass], *, nd_tol: float = 0.0005, vd_tol: float = 0.05, k: int = 3) -> list[tuple[float, Glass]]:
    """按 ((Δnd/nd_tol)² + (Δνd/vd_tol)²)^0.5 排序，返回前 k 个（距离, 玻璃）。距离 ≤ 1 视为在容差内。"""
    scored = [(((g.nd - nd) / nd_tol) ** 2 + ((g.vd - vd) / vd_tol) ** 2) ** 0.5 for g in glasses]
    order = sorted(range(len(glasses)), key=scored.__getitem__)
    return [(scored[i], glasses[i]) for i in order[:k]]
