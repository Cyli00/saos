"""镜头数据的基础约定：参数键语法、面引用的解析、批量追迹结果与光瞳采样。后端和测量库都用它，它只依赖 protocol。
Lens-data conventions shared by the backends and the measurement library: the parameter-key grammar, resolving surface
references, batch trace results and pupil sampling. It depends on protocol only.

参数键 / parameter keys:
    "<面>.<参数>"   面是序号（"3"、"-1" 为像面）或 Lens Data 的注释（"lens1 back"）；
                    参数：radius、curvature（1/R，0 为平面）、thickness、conic、semi_diameter、mech_semi_diameter（零件外缘）、
                    material、comment、nd、vd、dpgf、par<N>
    "aperture"      系统孔径值 / the system aperture value
    "field.<n>.x"、"field.<n>.y"、"wave.<n>"   第 n 个视场坐标、第 n 个波长（µm）/ field n coordinates, wavelength n (µm)
    "config.<c>.<r>" 多重结构第 r 行在结构 c 的值（变焦间隔、温度等）/ multi-configuration row r in configuration c
    "object.<物体>.<参数>" 非序列物体：物体是序号或注释；参数为 x、y、z、tilt_x、tilt_y、tilt_z、material、comment、
                    par<N> 或参数列标题（"Z Length"）、mean_path（体散射平均自由程）、vp.<DLL 参数名>（"vp.g"）
                    / a non-sequential object by number or comment: position, tilt, material, comment, a parameter by
                    par<N> or column header, mean_path, or vp.<DLL parameter name>
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import numpy as np

from saos.protocol import ErrorCode, SaosError

PARAMS = ("radius", "curvature", "thickness", "conic", "semi_diameter", "mech_semi_diameter", "material", "comment", "nd", "vd", "dpgf")
# 面型参数列（非球面系数、坐标断点的偏心和倾斜、菲涅尔基底等），按 Lens Data 的 Par 列号
# surface-type parameter columns (aspheric terms, coordinate-break decenters and tilts, …) by their Lens Data Par number
PAR_RE = re.compile(r"^par([1-9]\d{0,2})$")
INT_RE = re.compile(r"^-?\d+$")
_SYSTEM_RE = re.compile(r"^(aperture|wave\.(\d+)|field\.(\d+)\.([xy])|config\.(\d+)\.(\d+))$")


def parse_key(key: str) -> tuple[str, Any, str]:
    """("system", 名, 序号)、("surface", 面, 参数) 或 ("object", 物体, 参数)。
    ("system", name, n), ("surface", ref, param) or ("object", ref, param)."""
    key = str(key).strip()
    if key.startswith("object."):
        ref, sep, param = key[len("object."):].partition(".")
        if not sep or not ref or not param:
            raise SaosError(ErrorCode.BAD_ARGS, f"parameter key {key!r}: use object.<number or comment>.<param>")
        return "object", ref, param
    m = _SYSTEM_RE.match(key)
    if m:
        if m.group(1) == "aperture":
            return "system", "aperture", ""
        if m.group(2):
            return "system", "wave", m.group(2)
        if m.group(5):
            return "system", "config", f"{m.group(5)}.{m.group(6)}"
        return "system", f"field_{m.group(4)}", m.group(3)
    ref, sep, param = key.rpartition(".")
    if not sep or not ref or not (param in PARAMS or PAR_RE.match(param)):
        raise SaosError(ErrorCode.BAD_ARGS, f"parameter key {key!r}: use <surface>.<param> with param in {PARAMS} or par<N>, "
                                            "or aperture, field.<n>.x|y, wave.<n>, config.<c>.<row>")
    return "surface", ref, param


def surface(backend, ref: int | str, names: dict[str, int] | None = None) -> int:
    """面序号：整数原样（负数从像面倒数，-1 为像面），字符串按注释查找（names 是已读好的注释表，省一次读取）。
    Surface index: an int as is (negative counts back from the image, -1 is the image), a string by its comment
    (names is a comment table already read, saving one read)."""
    count = backend.surface_count()
    if isinstance(ref, str) and INT_RE.match(ref.strip()):
        ref = int(ref)
    if isinstance(ref, int) and not isinstance(ref, bool):
        index = ref + count if ref < 0 else ref
        if not 0 <= index < count:
            raise SaosError(ErrorCode.BAD_ARGS, f"surface {ref} out of range 0–{count - 1}")
        return index
    names = backend.surface_comments() if names is None else names
    if ref not in names:
        raise SaosError(ErrorCode.BAD_ARGS, f"no surface has the comment {ref!r}", {"comments": sorted(names)})
    return names[ref]


@dataclass
class Rays:
    """批量追迹的结果，每个量是一维数组；坐标在目标面的局部坐标系、镜头单位。ok = 没有错误、没有渐晕、数值有限。
    Batch trace results, one array per quantity; coordinates in the target surface's local frame, lens units.
    ok = no error, not vignetted, finite."""

    wave: np.ndarray
    hx: np.ndarray
    hy: np.ndarray
    px: np.ndarray
    py: np.ndarray
    error: np.ndarray
    vignette: np.ndarray
    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    l: np.ndarray  # noqa: E741 - 方向余弦沿用 ZOS-API 的名字 / direction cosines keep the ZOS-API names
    m: np.ndarray
    n: np.ndarray
    intensity: np.ndarray
    surface: int

    @classmethod
    def from_rows(cls, rays: np.ndarray, rows: np.ndarray, surface: int) -> Rays:
        return cls(wave=rays[:, 0].astype(int), hx=rays[:, 1], hy=rays[:, 2], px=rays[:, 3], py=rays[:, 4],
                   error=rows[:, 0].astype(int), vignette=rows[:, 1].astype(int), x=rows[:, 2], y=rows[:, 3], z=rows[:, 4],
                   l=rows[:, 5], m=rows[:, 6], n=rows[:, 7], intensity=rows[:, 8], surface=surface)

    @property
    def ok(self) -> np.ndarray:
        finite = np.isfinite(self.x) & np.isfinite(self.y) & np.isfinite(self.z) & np.isfinite(self.l) & np.isfinite(self.m) & np.isfinite(self.n)
        return (self.error == 0) & (self.vignette == 0) & finite

    def select(self, mask: np.ndarray) -> Rays:
        return Rays(**{k: (getattr(self, k)[mask] if k != "surface" else self.surface) for k in self.__dataclass_fields__})


def pupil(n: int = 21) -> list[tuple[float, float]]:
    """单位圆内的 n×n 方格光瞳采样点（归一化坐标）。/ An n×n square grid of normalised pupil points inside the unit circle."""
    if n < 2:
        return [(0.0, 0.0)]
    # 与各任务原有的写法逐位一致：-1 + 2i/(n-1)，外层 px / bit-identical to the jobs' own grids: -1 + 2i/(n-1), px outer
    grid = [-1 + 2 * i / (n - 1) for i in range(n)]
    return [(px, py) for px in grid for py in grid if px * px + py * py <= 1 + 1e-12]
