"""面型：把 .zmx 的面参数换成内核的参数行，矢高和求交都在 rtc.kernel 里只有一份。

参数编号按手册（见 docs/plans/rtc/SURFACE-PARAMS.md）。面型类只回答内核不管的事：顶点曲率、
是否对过账、OPD 能不能算。``vertex_curvature`` 可以吃 Dual，光瞳对设计变量的导数要用。
"""

from __future__ import annotations

import numpy as np

from . import dual as D
from . import kernel
from .zmx import Surface


def _val(x):
    return x.v if isinstance(x, D.Dual) else x


def _radial(row, params) -> None:
    for i in range(1, 9):
        row[kernel.EVEN + i - 1] = params.get(i, 0.0)


class Shape:
    """一种面型。``sag`` 返回 (z, ∂z/∂x, ∂z/∂y)；x、y 可广播。"""

    kind = "?"
    verified = False
    #: 该面型是否给光线加相位（衍射面）。相位 φ 单位 rad，加进光程：OPL += λ/(2π)·φ（手册
    #: §2.3.1.2.10「The phase adds to the path length of the ray」）。
    diffractive = False
    #: OPD/波前是否能算。Fresnel 的求交按平面、折射按基底，手册明确说不支持 OPD。
    opd_supported = True
    #: 内核的求交方式
    mode = kernel.BRACKET

    def __init__(self, surface: Surface):
        self.surface = surface

    def row(self) -> np.ndarray:
        """内核的参数行。参数超出标定范围时报 NotImplementedError。"""
        row = np.zeros(kernel.ROW)
        self.fill(row, self.surface.params)
        row[kernel.HAS_EVEN] = float(np.any(row[kernel.EVEN:kernel.XP]))
        row[kernel.HAS_XY] = float(np.any(row[kernel.XP:kernel.FOCAL]))
        return row

    def fill(self, row, params) -> None:
        row[kernel.CX] = row[kernel.CY] = self.surface.curvature
        row[kernel.KX] = row[kernel.KY] = self.surface.conic

    def links(self, field: str, param: int = 0) -> list[tuple[int, float]]:
        """设计变量落在参数行的哪些列上：[(列, ∂列值/∂变量)]。没有作用的变量返回空表。"""
        if field == "curvature":
            return [(kernel.CX, 1.0), (kernel.CY, 1.0)]
        if field == "conic":
            return [(kernel.KX, 1.0), (kernel.KY, 1.0)]
        return []

    def sag(self, x, y):
        return kernel.sag(self.row(), x, y)

    def vertex_curvature(self, axis: str):
        """顶点处沿 x 或 y 的近轴曲率；float 和 Dual 都可以。"""
        return self.surface.curvature

    def petzval_curvature(self):
        """PETZ 用的曲率。近轴量计入的二次项，OpticStudio 的 PETZ 只有偶次非球面的 α₁ 计入（2026-11-05 实测）。"""
        return self.vertex_curvature("y")


class Standard(Shape):
    kind = "STANDARD"
    verified = True
    mode = kernel.CONIC


class EvenAsphere(Shape):
    kind = "EVENASPH"
    verified = True
    mode = kernel.ASPHERE

    def fill(self, row, params) -> None:
        super().fill(row, params)
        _radial(row, params)

    def links(self, field, param=0):
        if field == "param" and 1 <= param <= 8:
            return [(kernel.EVEN + param - 1, 1.0)]
        return super().links(field, param)

    def vertex_curvature(self, axis: str):
        # 矢高里的 α₁r² 项改变顶点曲率：z ≈ (c/2 + α₁)r²
        return self.surface.curvature + 2.0 * self.surface.params.get(1, 0.0)


class Biconic(Shape):
    """LDE 半径/圆锥作用于 Y；参数 1 = Rx（0 表示 X 方向平直），参数 2 = kx。"""

    kind = "BICONICX"
    verified = True

    def fill(self, row, params) -> None:
        rx = params.get(1, 0.0)
        row[kernel.CX] = 0.0 if rx == 0.0 else 1.0 / rx
        row[kernel.KX] = params.get(2, 0.0)
        row[kernel.CY] = self.surface.curvature
        row[kernel.KY] = self.surface.conic

    def links(self, field, param=0):
        if field == "curvature":
            return [(kernel.CY, 1.0)]
        if field == "conic":
            return [(kernel.KY, 1.0)]
        if field == "param" and param == 1:
            rx = float(self.surface.params.get(1, 0.0))
            return [(kernel.CX, -1.0 / (rx * rx))] if rx else []
        if field == "param" and param == 2:
            return [(kernel.KX, 1.0)]
        return []

    def vertex_curvature(self, axis: str):
        if axis != "x":
            return self.surface.curvature
        rx = self.surface.params.get(1, 0.0)
        return 0.0 if _val(rx) == 0.0 else 1.0 / rx


class BiconicZernike(Biconic):
    """Biconic 基底 + x^1..x^16 / y^1..y^16 多项式（参数 13–44）+ Zernike（未实现）。"""

    kind = "BICONICZ"
    verified = True

    def fill(self, row, params) -> None:
        if int(params.get(3, 0.0)):
            raise NotImplementedError("BICONICZ 的 Zernike 项未实现（三件任务的 30 个轮次里项数恒为 0，没有标定数据）")
        super().fill(row, params)
        for i in range(1, 17):
            row[kernel.XP + i - 1] = params.get(12 + i, 0.0)
            row[kernel.YP + i - 1] = params.get(28 + i, 0.0)

    def links(self, field, param=0):
        if field == "param" and 13 <= param <= 28:
            return [(kernel.XP + param - 13, 1.0)]
        if field == "param" and 29 <= param <= 44:
            return [(kernel.YP + param - 29, 1.0)]
        return super().links(field, param)

    def vertex_curvature(self, axis: str):
        # x²、y² 项（参数 14、30）改变顶点曲率，OpticStudio 的近轴量同样计入（2026-11-05 实测 EFLX、EFLY、ENPP）
        return super().vertex_curvature(axis) + 2.0 * self.surface.params.get(14 if axis == "x" else 30, 0.0)

    def petzval_curvature(self):
        return self.surface.curvature


class Toroidal(Shape):
    """旋转半径 = 0（无穷）时是圆柱面：Y 方向二次曲线 + 参数 2..8 的 y²..y¹⁴ 项；有限半径未标定。"""

    kind = "TOROIDAL"
    verified = True

    def fill(self, row, params) -> None:
        if params.get(1, 0.0) != 0.0:
            raise NotImplementedError("TOROIDAL 有限旋转半径未标定（三件镜头全为 0）")
        if int(params.get(13, 0.0)):
            raise NotImplementedError("TOROIDAL 的 Zernike 项未实现")
        row[kernel.CY] = self.surface.curvature
        row[kernel.KY] = self.surface.conic
        for i in range(1, 8):
            row[kernel.YP + 2 * i - 1] = params.get(1 + i, 0.0)

    def links(self, field, param=0):
        if field == "curvature":
            return [(kernel.CY, 1.0)]
        if field == "conic":
            return [(kernel.KY, 1.0)]
        if field == "param" and 2 <= param <= 8:
            return [(kernel.YP + 2 * (param - 1) - 1, 1.0)]
        return []

    def vertex_curvature(self, axis: str):
        # y² 项（参数 2）改变顶点曲率，OpticStudio 的近轴量同样计入（2026-11-05 实测 EFLY、ENPP）
        return 0.0 if axis == "x" else self.surface.curvature + 2.0 * self.surface.params.get(2, 0.0)

    def petzval_curvature(self):
        return self.surface.curvature


class Paraxial(Shape):
    """面形是平面；折射按 n'·tan u' = n·tan u − y/f。"""

    kind = "PARAXIAL"
    verified = True
    mode = kernel.PLANE

    def fill(self, row, params) -> None:
        row[kernel.FOCAL] = params.get(1, 0.0)

    def links(self, field, param=0):
        return [(kernel.FOCAL, 1.0)] if field == "param" and param == 1 else []


class Binary2(EvenAsphere):
    """偶次非球面基底 + 相位 φ = M·Σ_{i=1..N} A_i ρ^(2i)（手册 §2.3.1.2.10）。

    参数表：0 = 衍射级次 M，1–8 = 基底的非球面项，13 = 最大项数 N，14 = 归一化半径 r_n，
    15 起 = 系数 A_1(ρ²)、A_2(ρ⁴)…（单位 rad，手册原文 "Coefficient in radians on ρ^2"…）。ρ² = (x² + y²)/r_n²。
    r_n 缺省或为 0 时取 100（2026-11-05 OpticStudio 实测，读数与显式写 100 相同）。

    符号约定（手册 BINARY 1 的 "Coefficients Sign Conventions"）：正光焦度的二元面
    二次相位系数为负，且它给边缘光线加的是**负**光程——与 OPL += λ/(2π)·φ 一致。
    """

    kind = "BINARY_2"
    verified = False
    diffractive = True
    mode = kernel.BRACKET

    def _norm_radius(self) -> float:
        return self.surface.params.get(14, 0.0) or 100.0

    def fill(self, row, params) -> None:
        super().fill(row, params)
        terms = int(params.get(13, 0.0))
        if terms > kernel.MAX_PHASE_TERMS:
            raise NotImplementedError(f"BINARY_2 相位项数 {terms} 超过内核上限 {kernel.MAX_PHASE_TERMS}")
        row[kernel.NTERM] = max(terms, 0)
        row[kernel.ORDER] = params.get(0, 1.0)
        row[kernel.RN] = self._norm_radius()
        for i in range(1, terms + 1):
            row[kernel.PHASE + i - 1] = params.get(14 + i, 0.0)

    def links(self, field, param=0):
        if field == "param" and param in (0, 13, 14):
            raise NotImplementedError(f"BINARY_2 的参数 {param}（衍射级次、项数、归一化半径）没有实现导数，不能作优化变量")
        if field == "param" and param >= 15:
            # 项数之外的系数不进实光线的相位；A_1 仍进近轴量，由 phase_power 单独传递
            return [(kernel.PHASE + param - 15, 1.0)] if param < 15 + int(self.surface.params.get(13, 0.0)) else []
        return super().links(field, param)

    def phase_power(self, wavelength_um: float):
        """相位的二次项给的近轴光焦度：φ ≈ M·A_1·r²/r_n²，n'u' = n·u + (λ/2π)·∂φ/∂y = n·u − y·光焦度。OpticStudio 的近轴量同样计入（2026-11-05 实测 ENPP）。

        不看项数：原始文件写 N=0 且带 A_1 时，OpticStudio 的实光线不用 A_1，近轴量却用（2026-11-05 实测，另存后 A_1 被删，两者才一致）。
        """
        params = self.surface.params
        rn = self._norm_radius()
        return -wavelength_um * 1e-3 / (2.0 * np.pi) * 2.0 * params.get(0, 1.0) * params.get(15, 0.0) / (rn * rn)


class Fresnel(EvenAsphere):
    """平面求交 + 偶次非球面基底的折射（手册 §2.3.1.2.32）。

    手册原文："The surface intercept is determined by computing the intersection of the
    incoming ray with a plane. Once the plane intercept points are found, the surface is
    then treated as generally curved for the purposes of refraction into the next medium."
    另外手册明说 Fresnel 不支持 OPD，所以 ``opd_supported = False``。
    """

    kind = "FRESNELS"
    verified = False
    opd_supported = False
    mode = kernel.PLANE

    def sag(self, x, y):
        zero = np.zeros(np.broadcast(np.asarray(x), np.asarray(y)).shape)
        return zero, zero, zero


_SHAPES = {
    "STANDARD": Standard,
    "EVENASPH": EvenAsphere,
    "BICONICX": Biconic,
    "BICONIC": Biconic,
    "BICONICZ": BiconicZernike,
    "TOROIDAL": Toroidal,
    "PARAXIAL": Paraxial,
    "BINARY_2": Binary2,
    "FRESNELS": Fresnel,
    "FRESNEL": Fresnel,
}


def make(surface: Surface) -> Shape:
    cls = _SHAPES.get(surface.type.upper())
    if cls is None:
        raise NotImplementedError(f"面型 {surface.type} 不在复现范围内")
    return cls(surface)


# 内核实现的面孔径；用户定义孔径（8/9）需要外部孔径文件，未实现，报错而不是当作无孔径
_APERTURES = (0, 1, 2, 3, 4, 5, 6, 7, 10)


def aperture_row(item, aiming: bool) -> np.ndarray:
    """内核的孔径行。金标准 is_ray_vignetted：容差乘在半径平方上，光阑面在瞄准时放宽一个量级。"""
    s = item.surface
    code = s.aperture_code
    if code in (8, 9):
        raise NotImplementedError("用户定义面孔径（类型 8/9）需要外部孔径文件，未实现（两者的码值语义相反）")
    if code not in _APERTURES:
        raise NotImplementedError(f"孔径类型码 {code} 不在 1–10 范围内")
    epsilon = 1.0 - (1e-7 if item.is_stop and aiming else 1e-8)
    return np.array([code, s.ap_min, s.ap_max, s.ap_dx, s.ap_dy, (item.semi or s.ap_max) if code == 10 else 0.0, epsilon], dtype=float)


def pack(system) -> kernel.Packed:
    """把系统排成内核数据。"""
    items = system.items
    aiming = system.lens.ray_aiming > 0
    return kernel.Packed(
        mode=np.array([it.shape.mode for it in items], dtype=np.int64),
        P=np.array([it.shape.row() for it in items], dtype=np.float64),
        AP=np.array([aperture_row(it, aiming) for it in items], dtype=np.float64),
        vz=np.array([it.vertex_z for it in items], dtype=np.float64),
        nafter=np.array([it.n_after for it in items], dtype=np.float64),
        mirror=np.array([bool(it.reflect) for it in items], dtype=np.bool_),
        sign=np.array([float(it.sign) for it in items], dtype=np.float64),
        index=np.array([it.index for it in items], dtype=np.int64),
        n0=float(system.n_before(0)),
        wavelength_mm=float(system.wavelength) * 1e-3,
        stop=-1 if system.stop is None else int(system.stop),
    )
