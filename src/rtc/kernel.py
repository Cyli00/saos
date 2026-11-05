"""numba 编译的逐光线序列追迹内核。

与 ``rtc.trace`` 的 numpy 实现是同一套算法（求交、括号保护 Newton、Snell、孔径、EPD 实际瞄准），区别只在
组织方式：每条光线在一个编译循环里走完所有面，不再为每个面、每步运算各发一次 numpy 调用。numpy 实现保留为
参照（``RTC_KERNEL=numpy`` 强制使用），也负责内核不覆盖的情形（Fresnel、二元面、用户孔径、OBNA 瞄准等）。

与 numpy 实现的两处有意差别：
- EPD 瞄准逐条光线判收敛（|残差| < 1e-13 mm），差分步长按 ``max(1, EPD)`` 定，不随同批其他光线变化；
- 光线出错（丢失或全反射）之后的各面输出为 NaN（numpy 实现留下未定义的中间值）。
"""

from __future__ import annotations

import math
import os
import threading

import numpy as np
from numba import config as _numba_config
from numba import njit, prange, set_num_threads

STANDARD, EVENASPH, BICONIC, BICONICZ, TOROIDAL, PARAXIAL = range(6)
_KINDS = {"STANDARD": STANDARD, "EVENASPH": EVENASPH, "BICONICX": BICONIC, "BICONIC": BICONIC,
          "BICONICZ": BICONICZ, "TOROIDAL": TOROIDAL, "PARAXIAL": PARAXIAL}
# 面参数行布局：0 c、1 k、2 cx、3 kx、4–11 偶次项、12–27 x^1..x^16、28–43 y^1..y^16、44 近轴焦距
_NP = 45
_EVEN, _XP, _YP, _FOCAL = 4, 12, 28, 44
# 孔径行：类型码、ap_min、ap_max、dx、dy、浮动孔径半径
_APERTURES = (0, 1, 2, 3, 4, 5, 6, 7, 10)
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


@njit(**_JIT)
def _sag(kind, p, x, y):
    if kind == PARAXIAL:
        return 0.0, 0.0, 0.0
    if kind == STANDARD or kind == EVENASPH:
        c = p[0]
        k = p[1]
        r2 = x * x + y * y
        root = math.sqrt(_max(1.0 - (1.0 + k) * c * c * r2, 0.0))
        z = c * r2 / (1.0 + root)
        safe = _max(root, 1e-300)
        g = (2.0 * c + z * (1.0 + k) * c * c / safe) / (1.0 + root) if root > 0.0 else 0.0
        zx = g * x
        zy = g * y
        if kind == EVENASPH:
            for i in range(1, 9):
                a = p[_EVEN + i - 1]
                if a != 0.0:
                    z = z + a * math.pow(r2, i)
                    zx = zx + 2 * i * a * math.pow(r2, i - 1) * x
                    zy = zy + 2 * i * a * math.pow(r2, i - 1) * y
        return z, zx, zy
    if kind == BICONIC or kind == BICONICZ:
        c = p[0]
        k = p[1]
        cx = p[2]
        kx = p[3]
        s2 = 1.0 - (1.0 + kx) * cx * cx * x * x - (1.0 + k) * c * c * y * y
        root = math.sqrt(_max(s2, 0.0))
        z = (cx * x * x + c * y * y) / (1.0 + root)
        safe = _max(root, 1e-300)
        u = cx * x * x + c * y * y
        inv = 1.0 / ((1.0 + root) * (1.0 + root)) if root > 0.0 else 0.0
        gx = inv * (2.0 * cx * (1.0 + root) + u * (1.0 + kx) * cx * cx / safe)
        gy = inv * (2.0 * c * (1.0 + root) + u * (1.0 + k) * c * c / safe)
        zx = gx * x
        zy = gy * y
        if kind == BICONICZ:
            for i in range(1, 17):
                a = p[_XP + i - 1]
                if a != 0.0:
                    z = z + a * math.pow(x, i)
                    zx = zx + i * a * math.pow(x, i - 1)
                b = p[_YP + i - 1]
                if b != 0.0:
                    z = z + b * math.pow(y, i)
                    zy = zy + i * b * math.pow(y, i - 1)
        return z, zx, zy
    # TOROIDAL：旋转半径为 0 的圆柱面，Y 方向二次曲线 + y 的偶次项
    c = p[0]
    k = p[1]
    r2 = y * y
    root = math.sqrt(_max(1.0 - (1.0 + k) * c * c * r2, 0.0))
    z = c * r2 / (1.0 + root)
    safe = _max(root, 1e-300)
    g = (2.0 * c + z * (1.0 + k) * c * c / safe) / (1.0 + root) if root > 0.0 else 0.0
    zy = g * y
    for i in range(1, 8):
        a = p[_EVEN + i - 1]
        if a != 0.0:
            z = z + a * math.pow(y, 2 * i)
            zy = zy + 2 * i * a * math.pow(y, 2 * i - 1)
    return z, z * 0.0, zy


@njit(**_JIT)
def _f(kind, p, x0, y0, z0, l, m, n, t):
    zs, _, _ = _sag(kind, p, x0 + t * l, y0 + t * m)
    return zs - (z0 + t * n)


@njit(**_JIT)
def _bracket_newton(kind, p, x0, y0, z0, l, m, n):
    """geometry.bracket_newton 的逐光线版本，判定顺序与常数相同。"""
    if n == 0.0:
        return math.nan
    t = -z0 / n
    ft = _f(kind, p, x0, y0, z0, l, m, n, t)
    if ft == 0.0:
        return t
    scale = _max(1.0, abs(t))
    step = 1e-3 * scale
    lo = math.nan
    hi = math.nan
    lo_pos = False
    bracketed = False
    pos = ft > 0
    for _ in range(80):
        a = t - step
        b = t + step
        fa = _f(kind, p, x0, y0, z0, l, m, n, a)
        fb = _f(kind, p, x0, y0, z0, l, m, n, b)
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
    zs, zx, zy = _sag(kind, p, x0 + T * l, y0 + T * m)
    f = zs - (z0 + T * n)
    dF = zx * l + zy * m - n
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        tn = T - f / dF if dF != 0.0 else mid
        if not (lo < tn and tn < hi):
            tn = mid
        zs, zx, zy = _sag(kind, p, x0 + tn * l, y0 + tn * m)
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
    """geometry.conic_intersect 的逐光线版本：返回 (t, ok)。"""
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
    ok = disc < 0.0 or (math.isfinite(t) and branch)
    return t, ok


@njit(**_JIT)
def _intersect(kind, p, x0, y0, z0, l, m, n):
    if kind == PARAXIAL:
        return -z0 / n if n != 0.0 else math.nan
    if kind == STANDARD:
        t, ok = _conic(p[0], p[1], x0, y0, z0, l, m, n)
        return t if ok else _bracket_newton(kind, p, x0, y0, z0, l, m, n)
    if kind == EVENASPH:
        t, ok = _conic(p[0], p[1], x0, y0, z0, l, m, n)
        if ok and math.isfinite(t):
            T = t
            for _ in range(30):
                zs, zx, zy = _sag(kind, p, x0 + T * l, y0 + T * m)
                f = zs - (z0 + T * n)
                dF = zx * l + zy * m - n
                dt = -f / dF
                T = T + dt
                if math.isfinite(dt) and abs(dt) < 1e-14 * _max(1.0, abs(T)):
                    return T
    return _bracket_newton(kind, p, x0, y0, z0, l, m, n)


@njit(**_JIT)
def _clipped(ap, x, y):
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


@njit(**_JIT)
def _refract(L, M, N, zx, zy, n1, n2):
    nx = -zx
    ny = -zy
    nz = 1.0
    norm = math.sqrt(nx * nx + ny * ny + nz * nz)
    nx, ny, nz = nx / norm, ny / norm, nz / norm
    dn = L * nx + M * ny + N * nz
    if dn > 0:
        nx, ny, nz = -nx, -ny, -nz
    cos1 = abs(dn)
    mu = n1 / n2
    sin2 = mu * mu * (1.0 - cos1 * cos1)
    cos2 = math.sqrt(_max(1.0 - sin2, 0.0))
    g = mu * cos1 - cos2
    lt = mu * L + g * nx
    mt = mu * M + g * ny
    nt = mu * N + g * nz
    s = math.sqrt(lt * lt + mt * mt + nt * nt)
    return lt / s, mt / s, nt / s, sin2 > 1.0


@njit(**_JIT)
def _reflect(L, M, N, zx, zy):
    """反射面：d′ = d − 2(d·N)N，法线取 ( −∂z/∂x, −∂z/∂y, 1 ) 并归一化。"""
    nx = -zx
    ny = -zy
    nz = 1.0
    norm = math.sqrt(nx * nx + ny * ny + nz * nz)
    nx, ny, nz = nx / norm, ny / norm, nz / norm
    dn = L * nx + M * ny + N * nz
    if dn > 0.0:
        nx, ny, nz = -nx, -ny, -nz
        dn = -dn
    g = -2.0 * dn
    lt = L + g * nx
    mt = M + g * ny
    nt = N + g * nz
    s = math.sqrt(lt * lt + mt * mt + nt * nt)
    return lt / s, mt / s, nt / s


@njit(**_JIT)
def _paraxial_lens(L, M, N, X, Y, f, n1, n2):
    # 近轴薄透镜（OpticStudio 24.1 实测，面后为 N-BK7、N-SF6 时差 ≤ 7e-18）：n'·tan u' = n·tan u − y/f
    ux = (n1 * L / N - X / f) / n2
    uy = (n1 * M / N - Y / f) / n2
    s = math.sqrt(ux * ux + uy * uy + 1.0)
    sign = -1.0 if n2 < 0.0 else 1.0
    return sign * ux / s, sign * uy / s, sign / s


@njit(**_JIT)
def _to_stop(kind, P, vz, nafter, ref, n0, stop, X, Y, Z, L, M, N):
    """瞄准用：追到光阑面，返回交点 (x, y)；不裁剪、不算光程（同 trace._trace_wo_aim）。"""
    n_cur = n0
    for k in range(stop + 1):
        t = _intersect(kind[k], P[k], X, Y, Z - vz[k], L, M, N)
        X = X + t * L
        Y = Y + t * M
        Z = Z + t * N
        if k == stop:
            break
        if kind[k] == PARAXIAL and P[k, _FOCAL] != 0.0:
            L, M, N = _paraxial_lens(L, M, N, X, Y, P[k, _FOCAL], n_cur, nafter[k])
        else:
            _, zx, zy = _sag(kind[k], P[k], X, Y)
            if ref[k]:
                L, M, N = _reflect(L, M, N, zx, zy)
            else:
                L, M, N, _ = _refract(L, M, N, zx, zy, n_cur, nafter[k])
        n_cur = nafter[k]
    return X, Y


@njit(**_JIT)
def _aim(kind, P, vz, nafter, ref, n0, stop, x0, y0, z0, L, M, N, tx, ty, h, cap):
    """EPD / Float By Stop Size 的实际瞄准：调发射位置，使光线落在光阑面 (tx, ty)；
    差分雅可比的 2×2 Newton，步长限幅。"""
    for _ in range(60):
        sx, sy = _to_stop(kind, P, vz, nafter, ref, n0, stop, x0, y0, z0, L, M, N)
        hx = sx - tx
        hy = sy - ty
        if not (math.isfinite(hx) and math.isfinite(hy)):
            break
        if _max(abs(hx), abs(hy)) < 1e-13:
            break
        ax, ay = _to_stop(kind, P, vz, nafter, ref, n0, stop, x0 + h, y0, z0, L, M, N)
        bx, by = _to_stop(kind, P, vz, nafter, ref, n0, stop, x0, y0 + h, z0, L, M, N)
        jxx = (ax - sx) / h
        jyx = (ay - sy) / h
        jxy = (bx - sx) / h
        jyy = (by - sy) / h
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


@njit(**_JIT)
def _one(i, kind, P, AP, vz, nafter, ref, sign, index, n0, stop, x0, y0, z0, L0, M0, N0, aim, tx, ty, h, cap,
         ox, oy, oz, ol, om, on, oopl, err, vig):
    S = kind.size
    X, Y, Z, L, M, N = x0[i], y0[i], z0[i], L0[i], M0[i], N0[i]
    if aim:
        X, Y = _aim(kind, P, vz, nafter, ref, n0, stop, X, Y, Z, L, M, N, tx[i], ty[i], h, cap)
    n_cur = n0
    opl = 0.0
    e = 0
    v = 0
    done = S
    for k in range(S):
        t = _intersect(kind[k], P[k], X, Y, Z - vz[k], L, M, N)
        if not math.isfinite(t):
            e = index[k]
            done = k
            break
        if k > 0:
            opl = opl + abs(n_cur) * t
        X = X + t * L
        Y = Y + t * M
        Z = Z + t * N
        if v == 0 and AP[k, 0] != 0.0 and _clipped(AP[k], X, Y):
            v = index[k]
        n2 = nafter[k]
        if kind[k] == PARAXIAL and P[k, _FOCAL] != 0.0:
            # 光程只记几何路程（与批量追迹的 OPL 列一致）；理想透镜的相位见 trace._paraxial_phase
            L, M, N = _paraxial_lens(L, M, N, X, Y, P[k, _FOCAL], n_cur, n2)
            n_cur = n2
        else:
            _, zx, zy = _sag(kind[k], P[k], X, Y)
            if ref[k]:
                L, M, N = _reflect(L, M, N, zx, zy)
            else:
                L, M, N, tir = _refract(L, M, N, zx, zy, n_cur, n2)
                if tir:
                    e = -index[k]
            n_cur = n2
        ox[i, k] = X
        oy[i, k] = Y
        oz[i, k] = Z - vz[k]
        # 对外报告的方向余弦带上 σ（反射一次翻一次），与 OpticStudio 批量追迹同口径；追迹内部仍用几何方向
        ol[i, k] = L * sign[k]
        om[i, k] = M * sign[k]
        on[i, k] = N * sign[k]
        oopl[i, k] = opl
        if e != 0:
            done = k + 1
            break
    for k in range(done, S):
        ox[i, k] = math.nan
        oy[i, k] = math.nan
        oz[i, k] = math.nan
        ol[i, k] = math.nan
        om[i, k] = math.nan
        on[i, k] = math.nan
        oopl[i, k] = math.nan
    err[i] = e
    vig[i] = v


@njit(**_JIT)
def _trace_serial(kind, P, AP, vz, nafter, ref, sign, index, n0, stop, x0, y0, z0, L0, M0, N0, aim, tx, ty, h, cap,
                  ox, oy, oz, ol, om, on, oopl, err, vig):
    for i in range(x0.size):
        _one(i, kind, P, AP, vz, nafter, ref, sign, index, n0, stop, x0, y0, z0, L0, M0, N0, aim, tx, ty, h, cap,
             ox, oy, oz, ol, om, on, oopl, err, vig)


@njit(parallel=True, **_JIT)
def _trace_parallel(kind, P, AP, vz, nafter, ref, sign, index, n0, stop, x0, y0, z0, L0, M0, N0, aim, tx, ty, h, cap,
                    ox, oy, oz, ol, om, on, oopl, err, vig):
    for i in prange(x0.size):
        _one(i, kind, P, AP, vz, nafter, ref, sign, index, n0, stop, x0, y0, z0, L0, M0, N0, aim, tx, ty, h, cap,
             ox, oy, oz, ol, om, on, oopl, err, vig)


class Packed:
    """一个 OpticalSystem 的面数据，按内核布局排好。"""

    __slots__ = ("kind", "P", "AP", "vz", "nafter", "ref", "sign", "index", "n0", "stop")

    def __init__(self, kind, P, AP, vz, nafter, ref, sign, index, n0, stop):
        self.kind, self.P, self.AP, self.vz, self.nafter, self.ref, self.sign = kind, P, AP, vz, nafter, ref, sign
        self.index, self.n0, self.stop = index, n0, stop


def pack(system) -> Packed | None:
    """把系统排成内核数据；内核不覆盖的面型、孔径或参数时返回 None（由 numpy 实现处理或报错）。"""
    if os.environ.get("RTC_KERNEL", "").lower() == "numpy":
        return None
    items = system.items
    S = len(items)
    kind = np.empty(S, np.int64)
    P = np.zeros((S, _NP))
    AP = np.zeros((S, 7))
    for k, it in enumerate(items):
        s = it.surface
        code = _KINDS.get(it.type)
        if code is None:
            return None
        params = s.params
        P[k, 0] = s.curvature
        P[k, 1] = s.conic
        if code == EVENASPH:
            for i in range(1, 9):
                P[k, _EVEN + i - 1] = params.get(i, 0.0)
        elif code in (BICONIC, BICONICZ):
            rx = params.get(1, 0.0)
            P[k, 2] = 0.0 if rx == 0.0 else 1.0 / rx
            P[k, 3] = params.get(2, 0.0)
            if code == BICONICZ:
                if int(params.get(3, 0.0)):
                    return None
                for i in range(1, 17):
                    P[k, _XP + i - 1] = params.get(12 + i, 0.0)
                    P[k, _YP + i - 1] = params.get(28 + i, 0.0)
        elif code == TOROIDAL:
            if params.get(1, 0.0) != 0.0 or int(params.get(13, 0.0)):
                return None
            for i in range(1, 8):
                P[k, _EVEN + i - 1] = params.get(1 + i, 0.0)
        elif code == PARAXIAL:
            P[k, _FOCAL] = it.focal or 0.0
        ap = s.aperture_code
        if ap not in _APERTURES:
            return None
        epsilon = 1.0 - (1e-7 if it.is_stop and system.lens.ray_aiming > 0 else 1e-8)
        AP[k] = (ap, s.ap_min, s.ap_max, s.ap_dx, s.ap_dy, (it.semi or s.ap_max) if ap == 10 else 0.0, epsilon)
        kind[k] = code
    vz = np.array([it.vertex_z for it in items], dtype=np.float64)
    nafter = np.array([it.n_after for it in items], dtype=np.float64)
    ref = np.array([bool(it.reflect) for it in items], dtype=np.bool_)
    sign = np.array([float(it.sign) for it in items], dtype=np.float64)
    index = np.array([it.index for it in items], dtype=np.int64)
    stop = -1 if system.stop is None else int(system.stop)
    return Packed(kind, P, AP, vz, nafter, ref, sign, index, float(system.n_before(0)), stop)


def trace(packed: Packed, x0, y0, z0, d, aim: bool = False, tx=None, ty=None, aim_scale: float = 1.0):
    """追迹一批光线；返回 (x, y, z, l, m, n, opl, error, vig)，前 7 项形状 (光线数, 面数)。

    ``aim`` 为真时做 EPD 实际瞄准，目标点 (tx, ty) 在光阑面；``aim_scale`` 定差分步长与信赖域。
    """
    x0, y0, z0 = (np.ascontiguousarray(v, dtype=np.float64).ravel() for v in (x0, y0, z0))
    d = np.asarray(d, dtype=np.float64).reshape(-1, 3)
    L0, M0, N0 = (np.ascontiguousarray(d[:, j]) for j in range(3))
    n_ray, S = x0.size, packed.kind.size
    aim = bool(aim) and packed.stop >= 0
    if aim:
        tx = np.ascontiguousarray(np.broadcast_to(np.asarray(tx, dtype=np.float64), x0.shape))
        ty = np.ascontiguousarray(np.broadcast_to(np.asarray(ty, dtype=np.float64), x0.shape))
    else:
        tx = ty = np.zeros(n_ray)
    scale = max(1.0, float(aim_scale))
    h, cap = 1e-7 * scale, 0.25 * max(scale, 1e-9)
    out = [np.empty((n_ray, S)) for _ in range(7)]
    err = np.zeros(n_ray, np.int64)
    vig = np.zeros(n_ray, np.int64)
    args = (packed.kind, packed.P, packed.AP, packed.vz, packed.nafter, packed.ref, packed.sign, packed.index, packed.n0,
            max(packed.stop, 0), x0, y0, z0, L0, M0, N0, aim, tx, ty, h, cap, *out, err, vig)
    if n_ray >= PARALLEL_MIN_RAYS:
        with _LOCK:
            _trace_parallel(*args)
    else:
        _trace_serial(*args)
    return (*out, err, vig)
