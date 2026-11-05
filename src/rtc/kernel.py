"""numba 编译的序列追迹内核：所有几何光线算法只有这一份单步。

一步做两件事（算法见 docs/plans/rtc-unify.md）：
1. 沿直线走到面上，解 F(t) = z(x0+t·l, y0+t·m) − (z0+t·n) = 0；
2. 切向动量守恒更新方向：n'·d' = n·d + (λ/2π)·∇φ_t + s·N，透射取负根，反射取正根。

面型只有一个公式，由参数行决定它是哪种面：
z = (cx·x²+cy·y²)/(1+S) + Σα_i·r^(2i) + Σa_i·x^i + Σb_i·y^i，S = √(1−(1+kx)cx²x²−(1+ky)cy²y²)。

每条光线可以带 P 个切向 T[j, :] = ∂(x, y, z, l, m, n, opl)/∂θ_j。种子是设计变量时得到优化雅可比，
种子是发射位置或斜率时得到瞄准 Newton 的 2×2 雅可比；P = 0 就是普通追迹。

光线丢失（错误码 > 0）或全反射（< 0）之后的各面输出为 NaN；瞄准迭代里全反射按掠射继续，由残差判断。
"""

from __future__ import annotations

import math
import os
import threading
from typing import NamedTuple

import numpy as np
from numba import config as _numba_config
from numba import njit, prange, set_num_threads

# 求交方式：平面（PARAXIAL、Fresnel）、闭式（STANDARD）、闭式初值加 Newton（EVENASPH）、括号保护 Newton（其余）
PLANE, CONIC, ASPHERE, BRACKET = range(4)
# 面参数行布局
CX, KX, CY, KY = 0, 1, 2, 3
EVEN, XP, YP = 4, 12, 28       # α1..α8、x^1..x^16、y^1..y^16
FOCAL, ORDER, RN, NTERM = 44, 45, 46, 47
PHASE = 48                     # 相位多项式 A_1(ρ²)..A_32
MAX_PHASE_TERMS = 32
HAS_EVEN, HAS_XY = 80, 81      # 是否有多项式，没有时跳过循环
ROW = 82
# 切向的 7 列
TANGENT = 7
# 少于这么多条光线时走单线程版本，省去线程池唤醒
PARALLEL_MIN_RAYS = 256

_LOCK = threading.Lock()  # numba 的 workqueue 线程层不允许多个 Python 线程同时进入并行内核
_threads = os.environ.get("RTC_THREADS")
if _threads:
    set_num_threads(max(1, min(int(_threads), _numba_config.NUMBA_NUM_THREADS)))
else:
    # 给用户留两个逻辑核（与 saos 计算约定一致）
    set_num_threads(max(1, _numba_config.NUMBA_NUM_THREADS - 2))

_JIT = dict(cache=True, error_model="numpy", nogil=True)


@njit(**_JIT)
def _max(a, b):
    # 与 np.maximum 一致：任一为 NaN 时结果为 NaN
    if a != a or b != b:
        return math.nan
    return a if a >= b else b


# ---------------------------------------------------------------- 矢高


@njit(**_JIT)
def _sag(row, x, y):
    """矢高和梯度。旋转对称时 (2c + z(1+k)c²/S)/(1+S) 化简后就是 c/S，这里保留各向异性的通式。"""
    cx, cy = row[CX], row[CY]
    a = (1.0 + row[KX]) * cx * cx
    b = (1.0 + row[KY]) * cy * cy
    x2, y2 = x * x, y * y
    S = math.sqrt(_max(1.0 - a * x2 - b * y2, 0.0))
    Q = 1.0 + S
    z = (cx * x2 + cy * y2) / Q
    if S > 0.0:
        zx = x * (2.0 * cx + z * a / S) / Q
        zy = y * (2.0 * cy + z * b / S) / Q
    else:
        zx = zy = 0.0
    if row[HAS_EVEN] != 0.0:
        r2 = x2 + y2
        power = 1.0
        for i in range(1, 9):
            alpha = row[EVEN + i - 1]
            if alpha != 0.0:
                # power = r2^(i−1)
                z += alpha * power * r2
                zx += 2.0 * i * alpha * power * x
                zy += 2.0 * i * alpha * power * y
            power *= r2
    if row[HAS_XY] != 0.0:
        px = py = 1.0
        for i in range(1, 17):
            ax, by = row[XP + i - 1], row[YP + i - 1]
            if ax != 0.0:
                z += ax * px * x
                zx += i * ax * px
            if by != 0.0:
                z += by * py * y
                zy += i * by * py
            px *= x
            py *= y
    return z, zx, zy


@njit(**_JIT)
def _hessian(row, x, y, z, zx, zy):
    """矢高的二阶导 (zxx, zxy, zyy)：对 z = U/(1+S) 的隐式求导，再加上多项式项。"""
    cx, cy = row[CX], row[CY]
    a = (1.0 + row[KX]) * cx * cx
    b = (1.0 + row[KY]) * cy * cy
    S = math.sqrt(_max(1.0 - a * x * x - b * y * y, 0.0))
    Q = 1.0 + S
    # 多项式的值和梯度要从 z、zx、zy 里扣掉，二次曲面部分的公式才成立
    pz, px1, py1, pxx, pxy, pyy = _polynomial(row, x, y)
    bz, bzx, bzy = z - pz, zx - px1, zy - py1
    if S > 0.0:
        Sx, Sy = -a * x / S, -b * y / S
        S3 = S * S * S
        Sxx = -a / S - a * a * x * x / S3
        Sxy = -a * b * x * y / S3
        Syy = -b / S - b * b * y * y / S3
    else:
        Sx = Sy = Sxx = Sxy = Syy = 0.0
    zxx = (2.0 * cx - 2.0 * bzx * Sx - bz * Sxx) / Q
    zxy = (-bzy * Sx - bz * Sxy - bzx * Sy) / Q
    zyy = (2.0 * cy - 2.0 * bzy * Sy - bz * Syy) / Q
    return zxx + pxx, zxy + pxy, zyy + pyy


@njit(**_JIT)
def _polynomial(row, x, y):
    """多项式项的 (z, zx, zy, zxx, zxy, zyy)。"""
    z = zx = zy = zxx = zxy = zyy = 0.0
    if row[HAS_EVEN] != 0.0:
        r2 = x * x + y * y
        f = f1 = f2 = 0.0
        power = 1.0   # r2^(i−1)
        lower = 0.0   # r2^(i−2)
        for i in range(1, 9):
            alpha = row[EVEN + i - 1]
            if alpha != 0.0:
                f += alpha * power * r2
                f1 += i * alpha * power
                f2 += i * (i - 1) * alpha * lower
            lower = power
            power *= r2
        z += f
        zx += 2.0 * x * f1
        zy += 2.0 * y * f1
        zxx += 2.0 * f1 + 4.0 * x * x * f2
        zxy += 4.0 * x * y * f2
        zyy += 2.0 * f1 + 4.0 * y * y * f2
    if row[HAS_XY] != 0.0:
        px = py = 1.0     # x^(i−1)、y^(i−1)
        qx = qy = 0.0     # x^(i−2)、y^(i−2)
        for i in range(1, 17):
            ax, by = row[XP + i - 1], row[YP + i - 1]
            if ax != 0.0:
                z += ax * px * x
                zx += i * ax * px
                zxx += i * (i - 1) * ax * qx
            if by != 0.0:
                z += by * py * y
                zy += i * by * py
                zyy += i * (i - 1) * by * qy
            qx, qy = px, py
            px *= x
            py *= y
    return z, zx, zy, zxx, zxy, zyy


@njit(**_JIT)
def _sag_derivative(row, col, x, y, z, zx, zy):
    """矢高和梯度对参数行第 col 列的偏导 (∂z, ∂zx, ∂zy)。"""
    if col >= XP:
        i = col - XP + 1 if col < YP else col - YP + 1
        u = x if col < YP else y
        lower = 1.0
        for _ in range(i - 1):
            lower *= u
        return lower * u, i * lower if col < YP else 0.0, i * lower if col >= YP else 0.0
    if col >= EVEN:
        i = col - EVEN + 1
        r2 = x * x + y * y
        lower = 1.0
        for _ in range(i - 1):
            lower *= r2
        return lower * r2, 2.0 * i * lower * x, 2.0 * i * lower * y
    cx, cy = row[CX], row[CY]
    a = (1.0 + row[KX]) * cx * cx
    b = (1.0 + row[KY]) * cy * cy
    x2, y2 = x * x, y * y
    S = math.sqrt(_max(1.0 - a * x2 - b * y2, 0.0))
    Q = 1.0 + S
    pz, px1, py1, _, _, _ = _polynomial(row, x, y)
    bz, bzx, bzy = z - pz, zx - px1, zy - py1
    # 对该列求偏导时，U、a、b、2cx、2cy 各自的变化率
    dU = da = db = dcx = dcy = 0.0
    if col == CX:
        dU, da, dcx = x2, 2.0 * (1.0 + row[KX]) * cx, 1.0
    elif col == CY:
        dU, db, dcy = y2, 2.0 * (1.0 + row[KY]) * cy, 1.0
    elif col == KX:
        da = cx * cx
    else:
        db = cy * cy
    if not S > 0.0:
        return dU / Q, 0.0, 0.0
    dS = -(da * x2 + db * y2) / (2.0 * S)
    Sx, Sy = -a * x / S, -b * y / S
    dSx = -da * x / S + a * x * dS / (S * S)
    dSy = -db * y / S + b * y * dS / (S * S)
    dz = (dU - bz * dS) / Q
    return (dz, (2.0 * dcx * x - dz * Sx - bz * dSx - bzx * dS) / Q,
            (2.0 * dcy * y - dz * Sy - bz * dSy - bzy * dS) / Q)


@njit(**_JIT)
def _phase(row, x, y):
    """衍射相位 φ = M·ΣA_i·ρ^(2i)，ρ² = r²/r_n²。返回 (φ, g, g′)，∇φ = g·(x, y)，g′ = dg/dρ²。"""
    M, rn = row[ORDER], row[RN]
    rho2 = (x * x + y * y) / (rn * rn)
    phi = s = ds = 0.0
    power = 1.0   # ρ^(2(i−1))
    lower = 0.0   # ρ^(2(i−2))
    for i in range(1, int(row[NTERM]) + 1):
        A = row[PHASE + i - 1]
        if A != 0.0:
            phi += A * power * rho2
            s += i * A * power
            ds += i * (i - 1) * A * lower
        lower = power
        power *= rho2
    k = M * 2.0 / (rn * rn)
    return M * phi, k * s, k * ds


# ---------------------------------------------------------------- 求交


@njit(**_JIT)
def _bracket_newton(row, x0, y0, z0, l, m, n):
    """从顶点平面出发按几何增长找最近的符号变化建立括号，之后是括号保护的 Newton，最坏也按二分收敛。"""
    if n == 0.0:
        return math.nan
    t = -z0 / n
    ft = _sag(row, x0 + t * l, y0 + t * m)[0] - z0 - t * n
    if ft == 0.0:
        return t
    scale = _max(1.0, abs(t))
    step = 1e-3 * scale
    lo = hi = math.nan
    lo_pos = False
    bracketed = False
    pos = ft > 0
    for _ in range(80):
        a = t - step
        b = t + step
        fa = _sag(row, x0 + a * l, y0 + a * m)[0] - z0 - a * n
        fb = _sag(row, x0 + b * l, y0 + b * m)[0] - z0 - b * n
        if fa == 0.0:
            return a
        if (fa > 0) != pos:
            lo, hi, lo_pos, bracketed = a, t, not pos, True
            break
        if fb == 0.0:
            return b
        if (fb > 0) != pos:
            lo, hi, lo_pos, bracketed = t, b, pos, True
            break
        step *= 1.7
        if step > 1e6 * scale:
            break
    if not bracketed:
        return math.nan
    T = t
    zs, zx, zy = _sag(row, x0 + T * l, y0 + T * m)
    f = zs - (z0 + T * n)
    dF = zx * l + zy * m - n
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        tn = T - f / dF if dF != 0.0 else mid
        if not (lo < tn and tn < hi):
            tn = mid
        zs, zx, zy = _sag(row, x0 + tn * l, y0 + tn * m)
        fn = zs - (z0 + tn * n)
        if (fn > 0) == lo_pos:
            lo = tn
        else:
            hi = tn
        ta = T
        T = tn
        tol = 1e-14 * _max(1.0, abs(tn))
        if fn == 0.0 or abs(tn - ta) < tol or hi - lo < tol:
            break
        f = fn
        dF = zx * l + zy * m - n
    return T


@njit(**_JIT)
def _conic(c, k, x0, y0, z0, l, m, n):
    """旋转二次曲面的闭式求交（Spencer & Murty 1962），返回 (t, ok)：错过曲面时 t = NaN、ok 为真；
    小根落在矢高公式描述不到的另一分支时 ok 为假，由调用方回退到通用算法。"""
    t1 = -z0 / n
    x = x0 + t1 * l
    y = y0 + t1 * m
    A = c * (l * l + m * m + (1.0 + k) * n * n)
    B = c * (x * l + y * m) - n
    C = c * (x * x + y * y)
    disc = B * B - A * C
    den = B + math.copysign(math.sqrt(_max(disc, 0.0)), B)
    t2 = -C / den if den != 0.0 else math.nan
    t = t1 + t2 if disc >= 0.0 else math.nan
    branch = 1.0 - (1.0 + k) * c * (t2 * n) >= -1e-12
    return t, disc < 0.0 or (math.isfinite(t) and branch)


@njit(**_JIT)
def _intersect(mode, row, x0, y0, z0, l, m, n):
    if mode == PLANE:
        return -z0 / n if n != 0.0 else math.nan
    if mode == CONIC:
        t, ok = _conic(row[CY], row[KY], x0, y0, z0, l, m, n)
        return t if ok else _bracket_newton(row, x0, y0, z0, l, m, n)
    if mode == ASPHERE:
        t, ok = _conic(row[CY], row[KY], x0, y0, z0, l, m, n)
        if ok and math.isfinite(t):
            T = t
            for _ in range(30):
                zs, zx, zy = _sag(row, x0 + T * l, y0 + T * m)
                dt = -(zs - (z0 + T * n)) / (zx * l + zy * m - n)
                T = T + dt
                if math.isfinite(dt) and abs(dt) < 1e-14 * _max(1.0, abs(T)):
                    return T
    return _bracket_newton(row, x0, y0, z0, l, m, n)


# ---------------------------------------------------------------- 孔径


@njit(**_JIT)
def _clipped(ap, x, y):
    """孔径行：类型码、ap_min、ap_max、dx、dy、浮动孔径半径、平方半径容差。类型码见 rtc.zmx.Surface.aperture_code。"""
    code = int(ap[0])
    if code == 0:
        return False
    dx = x - ap[3]
    dy = y - ap[4]
    a = ap[1]
    b = ap[2]
    r2 = dx * dx + dy * dy
    if code == 1 or code == 2 or code == 10:
        r2 *= ap[6]
    if code == 1:
        return r2 > b * b or r2 < a * a
    if code == 2:
        return a * a <= r2 and r2 < b * b
    if code == 3:
        n_arm = int(b)
        half = abs(a) * 0.5
        for k in range(n_arm):
            th = -2.0 * math.pi * k / n_arm
            ct = math.cos(th)
            st = math.sin(th)
            if ct * dx + st * dy >= 0.0 and abs(-st * dx + ct * dy) <= half:
                return True
        return False
    if code == 4 or code == 5:
        inside = abs(dx) <= a and abs(dy) <= b
        return not inside if code == 4 else inside
    if code == 6 or code == 7:
        q = (dx * dx) / (a * a) + (dy * dy) / (b * b) if a != 0.0 and b != 0.0 else math.inf
        return not (q < 1.0) if code == 6 else q <= 1.0
    r = ap[5]  # 浮动孔径
    return 0.0 < r * r and r * r < r2


# ---------------------------------------------------------------- 单步


@njit(**_JIT)
def _walk(system, last, arrive, stop_on_tir, X, Y, Z, L, M, N, T, dvz, ptr, lvar, lcol, lmult, i, out, out_t, slot):
    """从第 0 面走到第 last 面。T 是 (P, 7) 的切向，原地更新。

    arrive 为真时在第 last 面只求交，不更新方向（瞄准用）。i ≥ 0 时把每一面的结果写进 out、out_t 的第 i 行。
    返回 (x, y, 错误码, 渐晕码)。
    """
    mode, rows, apertures, vz, nafter, mirror, sign, index, n0, lam = system
    P = T.shape[0]
    has_links = lvar.size > 0
    if has_links:
        ez, ex, ey, ef, ephi, es = np.zeros(P), np.zeros(P), np.zeros(P), np.zeros(P), np.zeros(P), np.zeros(P)
    else:
        ez = ex = ey = ef = ephi = es = T[:0, 0]
    c_phase = lam / (2.0 * math.pi)
    n_cur = n0
    opl = 0.0
    error = 0
    vig = 0
    for k in range(last + 1):
        row = rows[k]
        kind = mode[k]
        zl = Z - vz[k]
        t = _intersect(kind, row, X, Y, zl, L, M, N)
        if not math.isfinite(t):
            if i < 0:
                return math.nan, math.nan, index[k], vig
            return X, Y, index[k], vig
        if k > 0:
            opl += abs(n_cur) * t
        X += t * L
        Y += t * M
        zl += t * N
        Z = zl + vz[k]
        if vig == 0 and apertures[k, 0] != 0.0 and _clipped(apertures[k], X, Y):
            vig = index[k]
        focal = row[FOCAL]
        lens = kind == PLANE and focal != 0.0
        zs = zx = zy = 0.0
        if not lens:
            zs, zx, zy = _sag(row, X, Y)
        # 走到面上的切向。平面求交时 F 不含矢高；Fresnel 的斜率只用于折射
        ix, iy = (0.0, 0.0) if kind == PLANE else (zx, zy)
        if P > 0:
            if has_links:
                ez[:], ex[:], ey[:], ef[:], ephi[:], es[:] = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
                for link in range(ptr[k], ptr[k + 1]):
                    j, col, scale = lvar[link], lcol[link], lmult[link]
                    if col == FOCAL:
                        ef[j] += scale
                    elif col >= PHASE:
                        a = col - PHASE + 1
                        rho2 = (X * X + Y * Y) / (row[RN] * row[RN])
                        ephi[j] += scale * row[ORDER] * rho2 ** a
                        es[j] += scale * row[ORDER] * 2.0 / (row[RN] * row[RN]) * a * rho2 ** (a - 1)
                    else:
                        dz, dzx, dzy = _sag_derivative(row, col, X, Y, zs, zx, zy)
                        if kind != PLANE:
                            ez[j] += scale * dz
                        ex[j] += scale * dzx
                        ey[j] += scale * dzy
            Ft = ix * L + iy * M - N
            if abs(Ft) < 1e-14:
                Ft = 1e-14
            for j in range(P):
                local = T[j, 2] - dvz[k, j]
                ax = T[j, 0] + t * T[j, 3]
                ay = T[j, 1] + t * T[j, 4]
                az = local + t * T[j, 5]
                rhs = ix * ax + iy * ay - az
                if has_links:
                    rhs += ez[j]
                dt = -rhs / Ft
                if k > 0:
                    T[j, 6] += abs(n_cur) * dt
                T[j, 0] = ax + L * dt
                T[j, 1] = ay + M * dt
                T[j, 2] = az + N * dt + dvz[k, j]
        if arrive and k == last:
            return X, Y, error, vig
        n2 = nafter[k]
        if lens:
            # 近轴薄透镜（OpticStudio 24.1 实测，面后为 N-BK7、N-SF6 时差 ≤ 7e-18）：n'·tan u' = n·tan u − y/f
            # 光程只记几何路程（与批量追迹的 OPL 列一致）；理想透镜的相位见 trace._paraxial_phase
            ux = (n_cur * L / N - X / focal) / n2
            uy = (n_cur * M / N - Y / focal) / n2
            w = math.sqrt(ux * ux + uy * uy + 1.0)
            s = -1.0 if n2 < 0.0 else 1.0
            if P > 0:
                for j in range(P):
                    local = n_cur * (T[j, 3] * N - L * T[j, 5]) / (N * N) - T[j, 0] / focal
                    local2 = n_cur * (T[j, 4] * N - M * T[j, 5]) / (N * N) - T[j, 1] / focal
                    if has_links:
                        local += X * ef[j] / (focal * focal)
                        local2 += Y * ef[j] / (focal * focal)
                    dux, duy = local / n2, local2 / n2
                    dw = (ux * dux + uy * duy) / w
                    T[j, 3] = s * (dux / w - ux * dw / (w * w))
                    T[j, 4] = s * (duy / w - uy * dw / (w * w))
                    T[j, 5] = -s * dw / (w * w)
            L, M, N = s * ux / w, s * uy / w, s / w
        else:
            w = math.sqrt(zx * zx + zy * zy + 1.0)
            # 法线翻到入射侧
            f = -1.0 if -zx * L - zy * M + N > 0.0 else 1.0
            nx, ny, nz = -f * zx / w, -f * zy / w, f / w
            n1 = abs(n_cur)
            n_out = abs(n2)
            root = 1.0 if mirror[k] else -1.0
            Ax, Ay, Az = n1 * L, n1 * M, n1 * N
            diffractive = row[NTERM] > 0.0 and row[ORDER] != 0.0
            gx = gy = gn = g = dg = 0.0
            if diffractive:
                phi, g, dg = _phase(row, X, Y)
                # 相位加到光程上（手册 §2.3.1.2.10）
                opl += c_phase * phi
                # ∇φ_t 是 (φx, φy, 0) 在切平面的投影：它和切向量 (1, 0, zx)、(0, 1, zy) 的内积正好是 φx、φy
                gx, gy = g * X, g * Y
                gn = gx * nx + gy * ny
                Ax += c_phase * (gx - gn * nx)
                Ay += c_phase * (gy - gn * ny)
                Az -= c_phase * gn * nz
            an = Ax * nx + Ay * ny + Az * nz
            disc = an * an - (Ax * Ax + Ay * Ay + Az * Az) + n_out * n_out
            tir = disc < 0.0 and not mirror[k]
            R = math.sqrt(_max(disc, 0.0))
            sk = -an + root * R
            if P > 0:
                zxx, zxy, zyy = _hessian(row, X, Y, zs, zx, zy)
                for j in range(P):
                    dx, dy = T[j, 0], T[j, 1]
                    dzx = zxx * dx + zxy * dy
                    dzy = zxy * dx + zyy * dy
                    if has_links:
                        dzx += ex[j]
                        dzy += ey[j]
                    dw = (zx * dzx + zy * dzy) / w
                    dnx = f * (-dzx / w + zx * dw / (w * w))
                    dny = f * (-dzy / w + zy * dw / (w * w))
                    dnz = -f * dw / (w * w)
                    dAx, dAy, dAz = n1 * T[j, 3], n1 * T[j, 4], n1 * T[j, 5]
                    if diffractive:
                        dq = dg * 2.0 * (X * dx + Y * dy) / (row[RN] * row[RN])
                        dop = gx * dx + gy * dy
                        if has_links:
                            dq += es[j]
                            dop += ephi[j]
                        T[j, 6] += c_phase * dop
                        dgx = dq * X + g * dx
                        dgy = dq * Y + g * dy
                        dgn = dgx * nx + gx * dnx + dgy * ny + gy * dny
                        dAx += c_phase * (dgx - dgn * nx - gn * dnx)
                        dAy += c_phase * (dgy - dgn * ny - gn * dny)
                        dAz -= c_phase * (dgn * nz + gn * dnz)
                    dan = dAx * nx + Ax * dnx + dAy * ny + Ay * dny + dAz * nz + Az * dnz
                    dR = 0.0
                    if R > 0.0:
                        dR = (an * dan - Ax * dAx - Ay * dAy - Az * dAz) / R
                    ds = -dan + root * dR
                    T[j, 3] = (dAx + ds * nx + sk * dnx) / n_out
                    T[j, 4] = (dAy + ds * ny + sk * dny) / n_out
                    T[j, 5] = (dAz + ds * nz + sk * dnz) / n_out
            L = (Ax + sk * nx) / n_out
            M = (Ay + sk * ny) / n_out
            N = (Az + sk * nz) / n_out
            if tir and stop_on_tir:
                error = -index[k]
        n_cur = n2
        if i >= 0:
            # 对外报告的方向余弦带上 σ（反射一次翻一次），与 OpticStudio 批量追迹同口径；追迹内部仍用几何方向
            sg = sign[k]
            out[i, k, 0] = X
            out[i, k, 1] = Y
            out[i, k, 2] = zl
            out[i, k, 3] = L * sg
            out[i, k, 4] = M * sg
            out[i, k, 5] = N * sg
            out[i, k, 6] = opl
            place = slot[k]
            if place >= 0:
                for j in range(P):
                    out_t[i, place, j, 0] = T[j, 0]
                    out_t[i, place, j, 1] = T[j, 1]
                    out_t[i, place, j, 2] = T[j, 2] - dvz[k, j]
                    out_t[i, place, j, 3] = T[j, 3] * sg
                    out_t[i, place, j, 4] = T[j, 4] * sg
                    out_t[i, place, j, 5] = T[j, 5] * sg
                    out_t[i, place, j, 6] = T[j, 6]
        if error != 0:
            return X, Y, error, vig
    return X, Y, error, vig


@njit(**_JIT)
def _no_links(system, P):
    """没有设计变量、不记录输出时 _walk 的只读参数；一批光线共用一份。"""
    S = system[0].size
    return (np.zeros((S, P)), np.zeros(S + 1, np.int64), np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros(0),
            np.zeros((0, 0, TANGENT)), np.zeros((0, 0, 0, TANGENT)), np.zeros(0, np.int64))


# ---------------------------------------------------------------- 瞄准：发射位置（物在无穷远的 EPD、Float By Stop Size）


@njit(**_JIT)
def _aim_position(system, stop, x0, y0, z0, l, m, n, tx, ty, cap, links):
    """调发射位置，使光线落在光阑面 (tx, ty)：2×2 Newton，雅可比来自切向，步长限幅。"""
    dvz, ptr, lvar, lcol, lmult, hole2, hole3, slot = links
    T = np.zeros((2, TANGENT))
    for _ in range(60):
        T[:] = 0.0
        T[0, 0] = 1.0
        T[1, 1] = 1.0
        sx, sy, _, _ = _walk(system, stop, True, False, x0, y0, z0, l, m, n, T, dvz, ptr, lvar, lcol, lmult, -1, hole2, hole3, slot)
        hx = sx - tx
        hy = sy - ty
        if not (math.isfinite(hx) and math.isfinite(hy)):
            break
        if _max(abs(hx), abs(hy)) < 1e-13:
            break
        jxx, jyx, jxy, jyy = T[0, 0], T[0, 1], T[1, 0], T[1, 1]
        det = jxx * jyy - jxy * jyx
        if abs(det) < 1e-14:
            det = 1e-14
        dx = (-hx * jyy + hy * jxy) / det
        dy = (-hy * jxx + hx * jyx) / det
        step = _max(abs(dx), abs(dy))
        s = cap / step if step > cap else 1.0
        x0 = x0 + s * dx
        y0 = y0 + s * dy
    return x0, y0


@njit(parallel=True, **_JIT)
def _aim_positions_parallel(system, stop, x0, y0, z0, l, m, n, tx, ty, cap, ox, oy):
    links = _no_links(system, 2)
    for i in prange(x0.size):
        ox[i], oy[i] = _aim_position(system, stop, x0[i], y0[i], z0[i], l[i], m[i], n[i], tx[i], ty[i], cap, links)


@njit(**_JIT)
def _aim_positions_serial(system, stop, x0, y0, z0, l, m, n, tx, ty, cap, ox, oy):
    links = _no_links(system, 2)
    for i in range(x0.size):
        ox[i], oy[i] = _aim_position(system, stop, x0[i], y0[i], z0[i], l[i], m[i], n[i], tx[i], ty[i], cap, links)


# ---------------------------------------------------------------- 瞄准：发射方向（OBNA、有限物距）


@njit(**_JIT)
def _direction_hit(system, stop, x0, y0, z0, ux, uy, T, dvz, ptr, lvar, lcol, lmult, hole2, hole3, slot):
    """斜率 (ux, uy) 发射，返回光阑面交点；T[j, :2] 是交点对 ux、uy 的导数。"""
    w = math.sqrt(ux * ux + uy * uy + 1.0)
    w3 = w * w * w
    T[:] = 0.0
    T[0, 3], T[0, 4], T[0, 5] = 1.0 / w - ux * ux / w3, -ux * uy / w3, -ux / w3
    T[1, 3], T[1, 4], T[1, 5] = -ux * uy / w3, 1.0 / w - uy * uy / w3, -uy / w3
    sx, sy, _, _ = _walk(system, stop, True, False, x0, y0, z0, ux / w, uy / w, 1.0 / w, T, dvz, ptr, lvar, lcol, lmult, -1, hole2, hole3, slot)
    return sx, sy


@njit(**_JIT)
def _aim_newton(system, stop, x0, y0, z0, ux, uy, tx, ty, trust, T, dvz, ptr, lvar, lcol, lmult, hole2, hole3, slot):
    """单条光线的 2×2 阻尼 Newton，返回 (ux, uy, 是否收敛)。步长限幅是防止跳到反射或伪根的关键。"""
    sx, sy = _direction_hit(system, stop, x0, y0, z0, ux, uy, T, dvz, ptr, lvar, lcol, lmult, hole2, hole3, slot)
    fx, fy = sx - tx, sy - ty
    best = _max(abs(fx), abs(fy))
    bx, by = ux, uy
    for _ in range(6):
        norm = _max(abs(fx), abs(fy))
        if norm < 1e-13:
            return ux, uy, True
        if norm < best:
            best, bx, by = norm, ux, uy
        jxx, jyx, jxy, jyy = T[0, 0], T[0, 1], T[1, 0], T[1, 1]
        det = jxx * jyy - jxy * jyx
        if abs(det) < 1e-18:
            return bx, by, False
        px = -(jyy * fx - jxy * fy) / det
        py = -(jxx * fy - jyx * fx) / det
        lim = min(1.0, trust / _max(_max(abs(px), abs(py)), 1e-18))
        accepted = False
        for _ in range(8):
            cx, cy = ux + lim * px, uy + lim * py
            sx, sy = _direction_hit(system, stop, x0, y0, z0, cx, cy, T, dvz, ptr, lvar, lcol, lmult, hole2, hole3, slot)
            gx, gy = sx - tx, sy - ty
            if _max(abs(gx), abs(gy)) < norm:
                accepted = True
                break
            lim *= 0.5
        if not accepted:
            return bx, by, False
        ux, uy, fx, fy = cx, cy, gx, gy
    if _max(abs(fx), abs(fy)) < 1e-10:
        return ux, uy, True
    return bx, by, False


@njit(**_JIT)
def _aim_direction(system, stop, x0, y0, z0, ux, uy, tx, ty, links):
    """光瞳坐标从 0 按 s∈(0,1] 逐段增大，目标取 s·(tx, ty)，每段用上一段的解作初值。
    这样不会像直接 Newton 那样跳到圆柱面的反射分支；失败时步长减半，而不是换一个根。"""
    dvz, ptr, lvar, lcol, lmult, hole2, hole3, slot = links
    T = np.zeros((2, TANGENT))
    s = 0.0
    ds = 0.125
    while s < 1.0 - 1e-12:
        step = min(ds, 1.0 - s)
        nx, ny, ok = _aim_newton(system, stop, x0, y0, z0, ux, uy, (s + step) * tx, (s + step) * ty, 0.02,
                                 T, dvz, ptr, lvar, lcol, lmult, hole2, hole3, slot)
        if ok:
            ux, uy, s = nx, ny, s + step
            ds = min(ds * 1.6, 0.25)
            continue
        ds *= 0.5
        if ds < 1e-3:
            return ux, uy, False
    return ux, uy, True


@njit(parallel=True, **_JIT)
def _aim_directions_parallel(system, stop, x0, y0, z0, ux, uy, tx, ty, ox, oy, ok):
    links = _no_links(system, 2)
    for i in prange(x0.size):
        ox[i], oy[i], ok[i] = _aim_direction(system, stop, x0[i], y0[i], z0[i], ux[i], uy[i], tx[i], ty[i], links)


@njit(**_JIT)
def _aim_directions_serial(system, stop, x0, y0, z0, ux, uy, tx, ty, ox, oy, ok):
    links = _no_links(system, 2)
    for i in range(x0.size):
        ox[i], oy[i], ok[i] = _aim_direction(system, stop, x0[i], y0[i], z0[i], ux[i], uy[i], tx[i], ty[i], links)


# ---------------------------------------------------------------- 批量追迹


@njit(**_JIT)
def _one(i, system, last, x0, y0, z0, d, seed, dvz, ptr, lvar, lcol, lmult, out, out_t, slot, error, vig):
    # P = 0 时是空数组，不写；普通追迹每条光线省一次分配
    T = seed[i]
    if seed.shape[1] > 0:
        T = T.copy()
    _, _, e, v = _walk(system, last, False, True, x0[i], y0[i], z0[i], d[i, 0], d[i, 1], d[i, 2], T, dvz, ptr, lvar, lcol, lmult, i, out, out_t, slot)
    error[i] = e
    vig[i] = v


@njit(parallel=True, **_JIT)
def _trace_parallel(system, last, x0, y0, z0, d, seed, dvz, ptr, lvar, lcol, lmult, out, out_t, slot, error, vig):
    for i in prange(x0.size):
        _one(i, system, last, x0, y0, z0, d, seed, dvz, ptr, lvar, lcol, lmult, out, out_t, slot, error, vig)


@njit(**_JIT)
def _trace_serial(system, last, x0, y0, z0, d, seed, dvz, ptr, lvar, lcol, lmult, out, out_t, slot, error, vig):
    for i in range(x0.size):
        _one(i, system, last, x0, y0, z0, d, seed, dvz, ptr, lvar, lcol, lmult, out, out_t, slot, error, vig)


@njit(**_JIT)
def _hits(system, stop, x0, y0, z0, d, ox, oy):
    dvz, ptr, lvar, lcol, lmult, hole2, hole3, slot = _no_links(system, 0)
    T = np.zeros((0, TANGENT))
    for i in range(x0.size):
        ox[i], oy[i], _, _ = _walk(system, stop, True, False, x0[i], y0[i], z0[i], d[i, 0], d[i, 1], d[i, 2], T, dvz, ptr, lvar, lcol, lmult, -1, hole2, hole3, slot)


@njit(**_JIT)
def _sag_rows(row, x, y, z, zx, zy):
    for i in range(x.size):
        z[i], zx[i], zy[i] = _sag(row, x[i], y[i])


@njit(**_JIT)
def _intersect_rows(mode, row, x0, y0, z0, l, m, n, out):
    for i in range(x0.size):
        out[i] = _intersect(mode, row, x0[i], y0[i], z0[i], l[i], m[i], n[i])


@njit(**_JIT)
def _clipped_rows(ap, x, y, out):
    for i in range(x.size):
        out[i] = _clipped(ap, x[i], y[i])


# ---------------------------------------------------------------- Python 接口


class Packed(NamedTuple):
    """一个 OpticalSystem 的面数据，按内核布局排好。"""

    mode: np.ndarray
    P: np.ndarray
    AP: np.ndarray
    vz: np.ndarray
    nafter: np.ndarray
    mirror: np.ndarray
    sign: np.ndarray
    index: np.ndarray
    n0: float
    wavelength_mm: float
    stop: int

    @property
    def system(self):
        return (self.mode, self.P, self.AP, self.vz, self.nafter, self.mirror, self.sign, self.index, self.n0, self.wavelength_mm)


class Tangents(NamedTuple):
    """切向的种子和设计变量如何作用到面上。"""

    seed: np.ndarray    # (光线, P, 7)，发射时的切向
    dvz: np.ndarray     # (面, P)，面顶点 z 对变量的导数
    ptr: np.ndarray     # (面 + 1)，第 k 面的连接是 links[ptr[k]:ptr[k+1]]
    lvar: np.ndarray    # 连接：变量下标
    lcol: np.ndarray    # 连接：参数行的列
    lmult: np.ndarray   # 连接：∂列值/∂变量
    slot: np.ndarray    # (面)，这一面的切向写到输出的第几格，−1 为不记录


def _rays(*arrays):
    return [np.ascontiguousarray(np.asarray(a, dtype=np.float64)).ravel() for a in arrays]


def trace(packed: Packed, x0, y0, z0, d, tangents: Tangents | None = None):
    """追迹一批光线。返回 (out, error, vig, out_t)：out 形状 (光线, 面, 7)，列为 x, y, z, l, m, n, opl；
    给了 tangents 时 out_t 形状 (光线, 记录的面, P, 7)，否则为 None。"""
    x0, y0, z0 = _rays(x0, y0, z0)
    d = np.ascontiguousarray(np.asarray(d, dtype=np.float64).reshape(-1, 3))
    n_ray, S = x0.size, packed.mode.size
    out = np.full((n_ray, S, TANGENT), np.nan)
    error = np.zeros(n_ray, np.int64)
    vig = np.zeros(n_ray, np.int64)
    if tangents is None:
        seed = np.zeros((n_ray, 0, TANGENT))
        dvz, ptr, lvar, lcol, lmult = np.zeros((S, 0)), np.zeros(S + 1, np.int64), np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros(0)
        slot = np.full(S, -1, np.int64)
        out_t = np.zeros((n_ray, 0, 0, TANGENT))
    else:
        seed, dvz, ptr, lvar, lcol, lmult, slot = tangents
        seed = np.ascontiguousarray(np.asarray(seed, dtype=np.float64))
        out_t = np.full((n_ray, int(slot.max(initial=-1)) + 1, seed.shape[1], TANGENT), np.nan)
    args = (packed.system, S - 1, x0, y0, z0, d, seed, dvz, ptr, lvar, lcol, lmult, out, out_t, slot, error, vig)
    if n_ray >= PARALLEL_MIN_RAYS:
        with _LOCK:
            _trace_parallel(*args)
    else:
        _trace_serial(*args)
    return out, error, vig, (None if tangents is None else out_t)


def aim_positions(packed: Packed, x0, y0, z0, d, tx, ty, scale: float):
    """物在无穷远：调发射位置瞄准光阑面 (tx, ty)。``scale`` 定信赖域（入瞳直径）。"""
    x0, y0, z0, tx, ty = _rays(x0, y0, z0, tx, ty)
    d = np.ascontiguousarray(np.asarray(d, dtype=np.float64).reshape(-1, 3))
    ox, oy = np.empty(x0.size), np.empty(x0.size)
    args = (packed.system, packed.stop, x0, y0, z0, np.ascontiguousarray(d[:, 0]), np.ascontiguousarray(d[:, 1]),
            np.ascontiguousarray(d[:, 2]), tx, ty, 0.25 * max(float(scale), 1.0), ox, oy)
    if x0.size >= PARALLEL_MIN_RAYS:
        with _LOCK:
            _aim_positions_parallel(*args)
    else:
        _aim_positions_serial(*args)
    return ox, oy


def aim_directions(packed: Packed, x0, y0, z0, ux, uy, tx, ty):
    """有限物距或 OBNA：沿光瞳坐标延拓，调发射斜率。返回 (ux, uy, 是否收敛)，不收敛的光线停在最后一段的解上。"""
    x0, y0, z0, ux, uy, tx, ty = _rays(x0, y0, z0, ux, uy, tx, ty)
    ox, oy, ok = np.empty(x0.size), np.empty(x0.size), np.zeros(x0.size, np.bool_)
    args = (packed.system, packed.stop, x0, y0, z0, ux, uy, tx, ty, ox, oy, ok)
    if x0.size >= PARALLEL_MIN_RAYS:
        with _LOCK:
            _aim_directions_parallel(*args)
    else:
        _aim_directions_serial(*args)
    return ox, oy, ok


def stop_hits(packed: Packed, x0, y0, z0, d):
    """给定起点和方向，追到光阑面的交点 (x, y)。不裁剪、不算光程。"""
    x0, y0, z0 = _rays(x0, y0, z0)
    d = np.ascontiguousarray(np.asarray(d, dtype=np.float64).reshape(-1, 3))
    ox, oy = np.empty(x0.size), np.empty(x0.size)
    _hits(packed.system, packed.stop, x0, y0, z0, d, ox, oy)
    return ox, oy


def sag(row, x, y):
    """矢高和梯度，x、y 可广播；形状与广播结果相同。"""
    x, y = np.broadcast_arrays(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64))
    shape = x.shape
    fx, fy = _rays(x, y)
    z, zx, zy = np.empty(fx.size), np.empty(fx.size), np.empty(fx.size)
    _sag_rows(row, fx, fy, z, zx, zy)
    return z.reshape(shape), zx.reshape(shape), zy.reshape(shape)


def intersect(mode: int, row, x0, y0, z0, l, m, n):
    """一批光线到一个面的 t（局部坐标），错过的光线为 NaN。"""
    x0, y0, z0, l, m, n = _rays(x0, y0, z0, l, m, n)
    out = np.empty(x0.size)
    _intersect_rows(int(mode), row, x0, y0, z0, l, m, n, out)
    return out


def clipped(ap, x, y):
    """一批点是否被这个孔径挡住，x、y 可广播。"""
    x, y = np.broadcast_arrays(np.asarray(x, dtype=np.float64), np.asarray(y, dtype=np.float64))
    shape = x.shape
    fx, fy = _rays(x, y)
    out = np.zeros(fx.size, np.bool_)
    _clipped_rows(ap, fx, fy, out)
    return out.reshape(shape)
