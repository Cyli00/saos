"""面型几何：矢高、梯度、光线求交。

所有 ``sag`` 都能吃 float 或 ``rtc.dual.Dual``（DLS 的雅可比要用），
运算符只用到 + − × ÷ ** sqrt where，避免破坏切向传播。

参数编号按手册（见 docs/plans/rtc/SURFACE-PARAMS.md）。
"""

from __future__ import annotations

import numpy as np

from . import dual as D
from .zmx import Surface


def _where_strict(cond, a, b):
    return D.where(cond, a, b)


class Shape:
    """一种面型的矢高/梯度。子类只需实现 ``sag``。"""

    kind = "?"
    verified = False
    #: 该面型是否给光线加相位（衍射面）。相位 φ 单位 rad，加进光程：OPL += λ/(2π)·φ（手册
    #: §2.3.1.2.10「The phase adds to the path length of the ray」）。
    diffractive = False
    #: OPD/波前是否能算。Fresnel 的求交按平面、折射按基底，手册明确说不支持 OPD。
    opd_supported = True
    #: 求交是否按顶点平面（只 Fresnel 为真；折射时改用 ``refraction_gradient``）。
    flat_intersection = False

    def __init__(self, surface: Surface):
        self.surface = surface

    def sag(self, x, y):
        raise NotImplementedError

    def refraction_gradient(self, x, y):
        """折射用的法线斜率 (dz/dx, dz/dy)。默认与矢高梯度相同；Fresnel 覆盖它。"""
        _, zx, zy = self.sag(x, y)
        return zx, zy

    def phase(self, x, y):
        """衍射相位 φ(x, y)，单位 rad；非衍射面返回 None。"""
        return None

    def phase_grad(self, x, y):
        """返回 (φ, ∂φ/∂x, ∂φ/∂y)，φ 单位 rad，梯度单位 rad/mm；非衍射面返回 None。"""
        return None

    # ---- 求交 ----
    def intersect(self, x0, y0, z0, d, t_hint=None):
        """返回沿 d 到该面的参数 t（使 z = sag(x0+t l, y0+t m)）。

        牛顿法，起点取顶点平面；收敛判据取相对 1e-13（比模型本身的精度要求严一档），
        迭代上限 60 轮只作发散保护。
        """
        l, m, n = d
        if n == 0.0:
            return float("nan")
        z, _, _ = self.sag(x0, y0)  # 只用值部分
        t = (0.0 - z0) / n if self.surface.curvature == 0.0 else (0.0 - z0) / n
        t = _val(t)
        for _ in range(60):
            xs = x0 + t * l
            ys = y0 + t * m
            zs, zx, zy = self.sag(xs, ys)
            F = _val(zs) - (z0 + t * n)
            dF = _val(zx) * l + _val(zy) * m - n
            if dF == 0.0:
                break
            dt = -F / dF
            t = t + dt
            if abs(dt) < 1e-13 * max(1.0, abs(t)):
                break
        return t

    def intersect_value(self, x0, y0, z0, l, m, n):
        """单条光线的求交参数 t（纯 float），丢失时为 ``nan``；与 :meth:`intersect_batch` 同一算法。

        旧的标量实现（括号 + Newton/二分）在同号时不收缩括号，随机光线里约 12% 跑满 200 轮仍未收敛
        （矢高方程残差到 4e-7 mm），已由向量化的带括号保护 Newton 取代。
        """
        return float(self.intersect_batch([x0], [y0], [z0], [l], [m], [n])[0])

    def intersect_batch(self, x0, y0, z0, l, m, n):
        """一批光线的求交参数 t（一维数组），丢失的光线为 ``nan``。

        通用面型用 :func:`bracket_newton`（与 :meth:`intersect_value` 同一算法的向量化版本）；
        有闭式解的面型在子类里覆盖。
        """
        return bracket_newton(self.sag, x0, y0, z0, l, m, n)


def _val(x):
    return x.v if isinstance(x, D.Dual) else x


def _live(a) -> bool:
    """系数是否参与计算。跳过零系数只为省计算；Dual 系数即使值为 0 也要算，否则导数被丢掉。"""
    return isinstance(a, D.Dual) or a != 0


def _rays(*arrays):
    return [np.ascontiguousarray(np.asarray(a, dtype=np.float64).ravel()) for a in arrays]


def _sag_value(sag, xs, ys):
    zs, zx, zy = sag(xs, ys)
    shape = np.shape(xs)
    return (np.broadcast_to(np.asarray(zs, float), shape), np.broadcast_to(np.asarray(zx, float), shape),
            np.broadcast_to(np.asarray(zy, float), shape))


def bracket_newton(sag, x0, y0, z0, l, m, n):
    """通用面型的向量化求交：括号搜索与 :meth:`Shape.intersect_value` 相同，迭代改为带括号保护的 Newton。

    从顶点平面出发，按几何增长找最近的符号变化建立括号；之后每步都按函数值符号收缩括号
    （Newton 步落在括号外就二分），所以最坏也按二分收敛。标量版在同号时不收缩括号，个别光线
    会跑满 200 轮，结果相同但慢两个数量级。已收敛或已放弃的光线从工作集里移除。
    """
    x0, y0, z0, l, m, n = _rays(x0, y0, z0, l, m, n)
    out = np.full(x0.shape, np.nan)
    idx = np.flatnonzero(n != 0.0)
    if idx.size == 0:
        return out
    X0, Y0, Z0, L, M, N = x0[idx], y0[idx], z0[idx], l[idx], m[idx], n[idx]

    def F(t, sel):
        return _sag_value(sag, X0[sel] + t * L[sel], Y0[sel] + t * M[sel])[0] - (Z0[sel] + t * N[sel])

    K = idx.size
    t = -Z0 / N
    ft = F(t, np.arange(K))
    res = np.full(K, np.nan)
    hit = ft == 0.0
    res[hit] = t[hit]
    lo = np.full(K, np.nan)
    hi = np.full(K, np.nan)
    lo_pos = np.zeros(K, bool)      # 括号左端的函数值是否为正
    bracketed = np.zeros(K, bool)
    scale = np.maximum(1.0, np.abs(t))
    step = 1e-3 * scale
    searching = ~hit
    for _ in range(80):
        s = np.flatnonzero(searching)
        if s.size == 0:
            break
        a, b = t[s] - step[s], t[s] + step[s]
        fa, fb = F(a, s), F(b, s)
        pos = ft[s] > 0
        # 与标量版的判定顺序一致：先看 t−step，再看 t+step
        za = fa == 0.0
        sa = ~za & ((fa > 0) != pos)
        zb = ~za & ~sa & (fb == 0.0)
        sb = ~za & ~sa & ~zb & ((fb > 0) != pos)
        res[s[za]] = a[za]
        res[s[zb]] = b[zb]
        lo[s[sa]], hi[s[sa]], lo_pos[s[sa]] = a[sa], t[s[sa]], ~pos[sa]
        lo[s[sb]], hi[s[sb]], lo_pos[s[sb]] = t[s[sb]], b[sb], pos[sb]
        bracketed[s[sa | sb]] = True
        found = za | sa | zb | sb
        searching[s[found]] = False
        rest = s[~found]
        step[rest] *= 1.7
        searching[rest[step[rest] > 1e6 * scale[rest]]] = False

    h = np.flatnonzero(bracketed)
    T, LO, HI, LP = t[h].copy(), lo[h], hi[h], lo_pos[h]
    zs, zx, zy = _sag_value(sag, X0[h] + T * L[h], Y0[h] + T * M[h])
    f = zs - (Z0[h] + T * N[h])
    dF = zx * L[h] + zy * M[h] - N[h]
    active = np.arange(h.size)
    for _ in range(200):
        if active.size == 0:
            break
        sel = h[active]
        Ta = T[active]
        mid = 0.5 * (LO[active] + HI[active])
        with np.errstate(divide="ignore", invalid="ignore"):
            tn = np.where(dF != 0.0, Ta - f / dF, mid)
        tn = np.where((LO[active] < tn) & (tn < HI[active]), tn, mid)
        zs, zx, zy = _sag_value(sag, X0[sel] + tn * L[sel], Y0[sel] + tn * M[sel])
        fn = zs - (Z0[sel] + tn * N[sel])
        right = (fn > 0) == LP[active]
        LO[active] = np.where(right, tn, LO[active])
        HI[active] = np.where(right, HI[active], tn)
        T[active] = tn
        tol = 1e-14 * np.maximum(1.0, np.abs(tn))
        conv = (fn == 0.0) | (np.abs(tn - Ta) < tol) | (HI[active] - LO[active] < tol)
        keep = ~conv
        active = active[keep]
        f = fn[keep]
        dF = (zx * L[sel] + zy * M[sel] - N[sel])[keep]
    res[h] = T
    out[idx] = res
    return out


def conic_intersect(c, k, x0, y0, z0, l, m, n):
    """旋转二次曲面的闭式求交（Spencer & Murty 1962 的写法）。

    先把光线移到顶点切平面，再解 ``c(x²+y²+(1+k)z²) − 2z = 0``，取数值稳定的小根。
    返回 ``(t, ok)``：判别式为负（光线错过曲面）时 t 为 ``nan``、ok 为真；小根落在矢高公式
    描述不到的另一分支（``1 − (1+k)c·z < 0``）或方程退化时 ok 为假，由调用方回退到通用算法。
    """
    x0, y0, z0, l, m, n = _rays(x0, y0, z0, l, m, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        t1 = -z0 / n
        x = x0 + t1 * l
        y = y0 + t1 * m
        A = c * (l * l + m * m + (1.0 + k) * n * n)
        B = c * (x * l + y * m) - n
        C = c * (x * x + y * y)
        disc = B * B - A * C
        den = B + np.copysign(np.sqrt(np.maximum(disc, 0.0)), B)
        t2 = np.where(den != 0.0, -C / den, np.nan)
        t = np.where(disc >= 0.0, t1 + t2, np.nan)
        branch = 1.0 - (1.0 + k) * c * (t2 * n) >= -1e-12
    miss = disc < 0.0
    ok = miss | (np.isfinite(t) & branch)
    return t, ok


# ---------------------------------------------------------------- 具体面型


class Standard(Shape):
    kind = "STANDARD"
    verified = True

    def sag(self, x, y):
        c = self.surface.curvature
        k = self.surface.conic
        r2 = x * x + y * y
        root = D.sqrt(D.maximum(1.0 - (1.0 + k) * c * c * r2, 0.0))
        z = c * r2 / (1.0 + root)
        # 精确梯度：dz/dx = x/(1+S) · [2c + z(1+k)c²/S]
        # （k≠0 时不能用近似的 c·x/S，两者差 1e-4 量级，直接决定法线与折射角）
        safe = D.maximum(root, 1e-300)
        g = D.where(root > 0.0, (2.0 * c + z * (1.0 + k) * c * c / safe) / (1.0 + root), 0.0)
        return z, g * x, g * y

    def intersect_batch(self, x0, y0, z0, l, m, n):
        x0, y0, z0, l, m, n = _rays(x0, y0, z0, l, m, n)
        t, ok = conic_intersect(_val(self.surface.curvature), _val(self.surface.conic), x0, y0, z0, l, m, n)
        if not ok.all():
            bad = ~ok
            t[bad] = bracket_newton(self.sag, x0[bad], y0[bad], z0[bad], l[bad], m[bad], n[bad])
        return t


class EvenAsphere(Shape):
    kind = "EVENASPH"
    verified = True

    def sag(self, x, y):
        z, zx, zy = Standard(self.surface).sag(x, y)
        r2 = x * x + y * y
        for i in range(1, 9):
            a = self.surface.params.get(i, 0.0)
            if _live(a):
                z = z + a * r2**i
                zx = zx + 2 * i * a * r2 ** (i - 1) * x
                zy = zy + 2 * i * a * r2 ** (i - 1) * y
        return z, zx, zy

    def intersect_batch(self, x0, y0, z0, l, m, n):
        """以基底二次曲面的闭式交点为初值做 Newton；不收敛或基底失败的光线回退到通用算法。"""
        x0, y0, z0, l, m, n = _rays(x0, y0, z0, l, m, n)
        t, ok = conic_intersect(_val(self.surface.curvature), _val(self.surface.conic), x0, y0, z0, l, m, n)
        done = np.zeros(t.shape, bool)
        sel = np.flatnonzero(ok & np.isfinite(t))
        T = t[sel]
        for _ in range(30):
            if sel.size == 0:
                break
            zs, zx, zy = _sag_value(self.sag, x0[sel] + T * l[sel], y0[sel] + T * m[sel])
            f = zs - (z0[sel] + T * n[sel])
            dF = zx * l[sel] + zy * m[sel] - n[sel]
            with np.errstate(divide="ignore", invalid="ignore"):
                dt = -f / dF
            T = T + dt
            conv = np.isfinite(dt) & (np.abs(dt) < 1e-14 * np.maximum(1.0, np.abs(T)))
            t[sel] = T
            done[sel[conv]] = True
            sel, T = sel[~conv], T[~conv]
        rest = ~done
        if rest.any():
            t[rest] = bracket_newton(self.sag, x0[rest], y0[rest], z0[rest], l[rest], m[rest], n[rest])
        return t


class Biconic(Shape):
    """LDE 半径/圆锥作用于 Y；参数 1 = Rx，参数 2 = kx。"""

    kind = "BICONICX"
    verified = True

    def sag(self, x, y):
        c = self.surface.curvature
        k = self.surface.conic
        rx = self.surface.params.get(1, 0.0)
        cx = 0.0 if rx == 0.0 else 1.0 / rx
        kx = self.surface.params.get(2, 0.0)
        s2 = 1.0 - (1.0 + kx) * cx * cx * x * x - (1.0 + k) * c * c * y * y
        root = D.sqrt(D.maximum(s2, 0.0))
        z = (cx * x * x + c * y * y) / (1.0 + root)
        safe = D.maximum(root, 1e-300)
        U = cx * x * x + c * y * y
        inv = D.where(root > 0.0, 1.0 / ((1.0 + root) * (1.0 + root)), 0.0)
        gx = inv * (2.0 * cx * (1.0 + root) + U * (1.0 + kx) * cx * cx / safe)
        gy = inv * (2.0 * c * (1.0 + root) + U * (1.0 + k) * c * c / safe)
        return z, gx * x, gy * y


class BiconicZernike(Shape):
    """Biconic 基底 + x^1..x^16 / y^1..y^16 多项式（参数 13–44）+ Zernike（未实现）。"""

    kind = "BICONICZ"
    verified = True

    def sag(self, x, y):
        z, zx, zy = Biconic(self.surface).sag(x, y)
        p = self.surface.params
        for i in range(1, 17):
            a = p.get(12 + i, 0.0)
            if _live(a):
                z = z + a * x**i
                zx = zx + i * a * x ** (i - 1)
            b = p.get(28 + i, 0.0)
            if _live(b):
                z = z + b * y**i
                zy = zy + i * b * y ** (i - 1)
        if int(_val(p.get(3, 0.0))):
            raise NotImplementedError(
                "BICONICZ 的 Zernike 项未实现（三件任务的 30 个轮次里项数恒为 0，没有标定数据）"
            )
        return z, zx, zy


class Toroidal(Shape):
    """旋转半径 = 0（无穷）时是圆柱面；有限半径未标定。"""

    kind = "TOROIDAL"
    verified = True

    def sag(self, x, y):
        p = self.surface.params
        if _val(p.get(1, 0.0)) != 0.0:
            raise NotImplementedError("TOROIDAL 有限旋转半径未标定（三件镜头全为 0）")
        if int(_val(p.get(13, 0.0))):
            raise NotImplementedError("TOROIDAL 的 Zernike 项未实现")
        c = self.surface.curvature
        k = self.surface.conic
        r2 = y * y
        root = D.sqrt(D.maximum(1.0 - (1.0 + k) * c * c * r2, 0.0))
        z = c * r2 / (1.0 + root)
        safe = D.maximum(root, 1e-300)
        g = D.where(root > 0.0, (2.0 * c + z * (1.0 + k) * c * c / safe) / (1.0 + root), 0.0)
        zy = g * y
        for i in range(1, 8):
            a = p.get(1 + i, 0.0)
            if _live(a):
                z = z + a * y ** (2 * i)
                zy = zy + 2 * i * a * y ** (2 * i - 1)
        return z, z * 0.0, zy


class Paraxial(Shape):
    """面形是平面；折折射在 trace 里按 u' = (n u − y/f)/n' 处理。"""

    kind = "PARAXIAL"
    verified = True

    def sag(self, x, y):
        zero = x * 0.0
        return zero, zero, zero

    def intersect_batch(self, x0, y0, z0, l, m, n):
        return _plane_t(z0, n)


class Binary2(Shape):
    """偶次非球面基底 + 相位 φ = M·Σ_{i=1..N} A_i ρ^(2i)（手册 §2.3.1.2.10）。

    参数表：0 = 衍射级次 M，1–8 = 基底的非球面项，13 = 最大项数 N，14 = 归一化半径 r_n，
    15 起 = 系数 A_1(ρ²)、A_2(ρ⁴)…（单位 rad，手册原文 "Coefficient in radians on ρ^2"…）。
    ρ² = (x² + y²)/r_n²。

    符号约定（手册 BINARY 1 的 "Coefficients Sign Conventions"）：正光焦度的二元面
    二次相位系数为负，且它给边缘光线加的是**负**光程——与 OPL += λ/(2π)·φ 一致。
    """

    kind = "BINARY_2"
    verified = False
    diffractive = True

    def substrate(self):
        return EvenAsphere(self.surface)

    def sag(self, x, y):
        return self.substrate().sag(x, y)

    def phase(self, x, y):
        p = self.surface.params
        nterm = int(_val(p.get(13, 0.0)))
        if nterm <= 0:
            return None
        order = p.get(0, 1.0)
        if _val(order) == 0.0:
            return None  # M = 0：零级衍射，不加相位
        rn = _val(p.get(14, 0.0)) or 1.0
        rho2 = (x * x + y * y) / (rn * rn)
        phi = x * 0.0
        for i in range(1, nterm + 1):
            a = p.get(14 + i, 0.0)
            if _live(a):
                phi = phi + a * rho2**i
        return order * phi

    def phase_grad(self, x, y):
        """解析梯度：∂φ/∂x = M·(2x/r_n²)·Σ i·A_i·ρ^(2i−2)。

        旧实现里有一行 ``gy = gx * y / x`` 的死代码（x=0 时除零），已删除；
        径向多项式的 x/y 梯度只是同一标量乘以 x/r_n² 或 y/r_n²。
        """
        p = self.surface.params
        nterm = int(_val(p.get(13, 0.0)))
        if nterm <= 0:
            return None
        order = p.get(0, 1.0)
        if _val(order) == 0.0:
            return None
        rn = _val(p.get(14, 0.0)) or 1.0
        rho2 = (x * x + y * y) / (rn * rn)
        s = x * 0.0
        for i in range(1, nterm + 1):
            a = p.get(14 + i, 0.0)
            if _live(a):
                s = s + i * a * rho2 ** (i - 1)
        scale = order * 2.0 * s / (rn * rn)
        phi = self.phase(x, y)
        return phi, scale * x, scale * y


class Fresnel(Shape):
    """平面求交 + 偶次非球面基底的折射（手册 §2.3.1.2.32）。

    手册原文："The surface intercept is determined by computing the intersection of the
    incoming ray with a plane. Once the plane intercept points are found, the surface is
    then treated as generally curved for the purposes of refraction into the next medium."
    另外手册明说 Fresnel 不支持 OPD，所以 ``opd_supported = False``。
    """

    kind = "FRESNELS"
    verified = False
    opd_supported = False
    flat_intersection = True

    def sag(self, x, y):
        zero = x * 0.0
        return zero, zero, zero

    def intersect_batch(self, x0, y0, z0, l, m, n):
        return _plane_t(z0, n)

    def refraction_gradient(self, x, y):
        _, zx, zy = EvenAsphere(self.surface).sag(x, y)
        return zx, zy


def _plane_t(z0, n):
    z0, n = _rays(z0, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(n != 0.0, -z0 / n, np.nan)


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
