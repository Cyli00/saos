"""序列光线追迹：发射、求交、折射/衍射、渐晕、批量接口。

物理约定（与 OpticStudio 一致，已用 oracle 批量追迹对照）：
- 坐标：z 为光轴，光线沿 +z 前进；每面的矢高相对该面顶点。
- 输出 ``x, y, z`` 是该面局部坐标下的交点（z = 该点矢高），``l, m, n`` 是**该面折射后**的方向。
- 光程 OPL 从第 1 面开始累计（物在无穷远时第 1 面处 OPL = 0）。
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np

from . import geometry
from .system import OpticalSystem

# 错误码与渐晕码沿用 OpticStudio 批量追迹的约定：错误码为正 = 光线在该面丢失（面号），
# 为负 = 在该面全反射（−面号）；渐晕码 = 光线被面孔径挡住的第一个面号，0 为通过。


def aperture_clipped(item, x, y, *, aiming: bool = False) -> np.ndarray:
    """按面孔径判断哪些光线被挡（布尔数组，可广播）。

    孔径类型码见 :attr:`rtc.zmx.Surface.aperture_code`，编号与类型名的对应取自手册 §14.46。
    半口径槽位按类型复用：``ap_min`` = 最小半径/臂宽/x 半宽，``ap_max`` = 最大半径/臂数/y 半宽。
    用户定义孔径（8/9）需要外部孔径文件，未实现，直接报错而不是当作无孔径。
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    code = item.surface.aperture_code
    if code == 0:
        return np.zeros(np.broadcast(x, y).shape, bool)
    dx = x - item.surface.ap_dx
    dy = y - item.surface.ap_dy
    a = item.surface.ap_min
    b = item.surface.ap_max
    r2 = dx * dx + dy * dy
    if code in (1, 2, 10):
        # 金标准 is_ray_vignetted：容差乘在半径平方上，同时用于圆孔的内外边界。
        r2 *= 1.0 - (1e-7 if item.is_stop and aiming else 1e-8)
    if code == 1:                       # 圆孔
        return (r2 > b * b) | (r2 < a * a)
    if code == 2:                       # 圆遮挡：环带内被挡
        return (a * a <= r2) & (r2 < b * b)
    if code == 3:                       # 蜘蛛：ap_min = 臂宽（全宽），ap_max = 臂数
        n_arm = int(b)
        half = abs(a) * 0.5
        hit = np.zeros(r2.shape, bool)
        for k in range(n_arm):
            th = -2.0 * np.pi * k / n_arm
            ct, st = np.cos(th), np.sin(th)
            hit |= (ct * dx + st * dy >= 0.0) & (np.abs(-st * dx + ct * dy) <= half)
        return hit
    if code in (4, 5):                  # 矩形孔 / 矩形遮挡
        inside = (np.abs(dx) <= a) & (np.abs(dy) <= b)
        return ~inside if code == 4 else inside
    if code in (6, 7):                  # 椭圆孔 / 椭圆遮挡
        q = (dx * dx) / (a * a) + (dy * dy) / (b * b) if a and b else np.full(r2.shape, np.inf)
        return ~(q < 1.0) if code == 6 else q <= 1.0
    if code in (8, 9):
        raise NotImplementedError(
            "用户定义面孔径（类型 8/9）需要外部孔径文件，未实现（两者的码值语义相反）"
        )
    if code == 10:                      # 浮动孔径：半径 = 通光半口径
        r = item.semi or b
        return (0.0 < r * r) & (r * r < r2)
    raise NotImplementedError(f"孔径类型码 {code} 不在 1–10 范围内")


@dataclass
class RayBlock:
    """一个结构下的一批光线的追迹结果（每条光线一行）。"""

    x: np.ndarray      # (n_ray, n_surf)
    y: np.ndarray
    z: np.ndarray
    l: np.ndarray
    m: np.ndarray
    n: np.ndarray
    opl: np.ndarray    # 几何光程（与 OpticStudio 批量追迹的 OPL 列同口径，不含理想透镜相位）
    phase: np.ndarray  # 累计的理想透镜相位（mm）；波前/OPD 用 opl + phase
    error: np.ndarray  # int，0 = 正常
    vig: np.ndarray    # int，0 = 未渐晕·未遮挡
    surface_index: list[int]

    def at(self, surf: int) -> dict:
        k = self.surface_index.index(surf)
        return {
            "x": self.x[:, k],
            "y": self.y[:, k],
            "z": self.z[:, k],
            "l": self.l[:, k],
            "m": self.m[:, k],
            "n": self.n[:, k],
            "opl": self.opl[:, k],
            "phase": self.phase[:, k],
        }


def _lens_phase_step(sqrt, x, y, tx, ty, xc, yc, tcx, tcy, m):
    """沿面从主光线 (xc, yc) 到本光线 (x, y) 积分 m·(L, M)：T = m·tan 是光线在折射率 m 里的方向（Tc 为主光线的）。

    m·(L, M) = T/√(1+|T|²/m²) 是 H(T) = m²(√(1+|T|²/m²) − 1) 对 T 的梯度。光束来自一个点（入射无像差）时，
    T 沿面是线性的 T = Tc + a·(r − rc)，积分恰为 (H(T) − H(Tc))/a；分子分母同乘消去 a，得到下面不含 a、a → 0 也稳定的式子。
    """
    num = (x - xc) * (tx + tcx) + (y - yc) * (ty + tcy)
    return num / (sqrt(1.0 + (tx * tx + ty * ty) / (m * m)) + sqrt(1.0 + (tcx * tcx + tcy * tcy) / (m * m)))


def _paraxial_lens_phase(sqrt, f, n1, n2, x, y, l, m, n, xc, yc, lc, mc, nc):
    """一个 PARAXIAL 面引入的相位（mm，相对同视场主光线）。(l, m, n) 是面后方向。

    OPD Mode 1 积分实际相位：相位梯度是 n'·(L', M') − n·(L, M)，出射规则 n'·tan u' = n·tan u − y/f。
    入射光束无像差时这是精确值；2026-10-28 与 OpticStudio OPDC 对照（面后空气 / N-BK7，边缘 32 / 185 waves）差 ≤ 1e-5 waves。
    入射光束有像差时 OpticStudio 的积分路径未公开，这里仍按上式，未与 OpticStudio 对照。
    """
    tx2, ty2 = n2 * l / n, n2 * m / n
    tcx2, tcy2 = n2 * lc / nc, n2 * mc / nc
    return (_lens_phase_step(sqrt, x, y, tx2, ty2, xc, yc, tcx2, tcy2, n2)
            - _lens_phase_step(sqrt, x, y, tx2 + x / f, ty2 + y / f, xc, yc, tcx2 + xc / f, tcy2 + yc / f, n1))


def _paraxial_phase(system: OpticalSystem, block: "RayBlock", hx, hy, aim) -> np.ndarray:
    """各面累计的理想透镜相位（mm），以同视场主光线为零。OPD Mode 1 的波前要加上它，批量追迹的 OPL 列不含它
    （golden 逐条差 ≤ 2e-12 mm）。此前用近轴近似 −r²/(2f)：f = 50、r = 4 时边缘差 0.46 waves。"""
    phase = np.zeros_like(block.x)
    if not any(it.type == "PARAXIAL" and it.focal for it in system.items):
        return phase
    fields, inv = np.unique(np.stack([hx, hy], axis=1), axis=0, return_inverse=True)
    inv = np.ravel(inv)
    zero = np.zeros(len(fields))
    chief = _trace_block(system, zero, zero, fields[:, 0], fields[:, 1], aim)
    acc = 0.0
    with np.errstate(divide="ignore", invalid="ignore"):
        for k, it in enumerate(system.items):
            if it.type == "PARAXIAL" and it.focal:
                acc = acc + _paraxial_lens_phase(
                    np.sqrt, it.focal, system.n_before(k), it.n_after,
                    block.x[:, k], block.y[:, k], block.l[:, k], block.m[:, k], block.n[:, k],
                    chief.x[inv, k], chief.y[inv, k], chief.l[inv, k], chief.m[inv, k], chief.n[inv, k])
            phase[:, k] = acc
    return phase


def _normalize(v):
    return v / np.sqrt(np.sum(v * v, axis=-1, keepdims=True))


def _launch(system: OpticalSystem, px, py, hx=0.0, hy=0.0):
    """把归一化光瞳/视场坐标变成物理光线（起点 + 方向）。

    EPD / Float By Stop Size：在入瞳平面按半径 R·(px,py) 发射，方向取视场角。
    OBNA：物在无穷远时 Zemax 把光阑中心对齐到第 1 面顶点，方向由物方 NA 决定
    （oracle 实测：piB 在面 1 上所有光线的交点是 (0,0)，方向随 px、py 变化）。
    """
    z_ep, r = system.pupil_reference()
    px, py, hx, hy = np.broadcast_arrays(*(np.asarray(v, dtype=float) for v in (px, py, hx, hy)))
    pupil_aperture = system.lens.aperture_type in ("EPD", "FLOA")
    legacy_obna = (system.lens.aperture_type == "OBNA" and system.object_thickness == 0.0
                   and system.lens.field_type == "ANGLE" and not system.allow_unverified)
    if system.finite_object and not legacy_obna:
        # 手册 §2.1.1.7：瞄准关时从物面射向主波长轴上的近轴入瞳；实际瞄准从这里起步迭代
        system.require_verified("有限物距的发射")
        if not np.isfinite(z_ep):
            raise NotImplementedError("入瞳在无穷远（物方远心）的有限物距发射未实现")
        xo, yo = system.object_point(hx, hy)
        xo, yo = np.broadcast_arrays(xo, yo)
        z0 = np.full(xo.shape, -system.object_thickness)
        d = _normalize(np.stack([px * r - xo, py * r - yo, np.full(xo.shape, z_ep) - z0], axis=-1))
        return xo.astype(float).copy(), yo.astype(float).copy(), z0, d
    fx, fy = system.field_slopes(hx, hy)
    if pupil_aperture:
        if np.isfinite(system.object_thickness):
            raise NotImplementedError("EPD / Float By Stop Size 且物距有限的系统未实现（只按物在无穷远处理）")
        x0 = r * px
        y0 = r * py
        z0 = np.where(np.isfinite(z_ep), z_ep, 0.0) * np.ones_like(x0)
        d = _normalize(np.stack([fx, fy, np.ones_like(x0)], axis=-1))
        return x0, y0, z0, d
    # OBNA：方向锥，锚在第 1 面顶点
    na = system.lens.aperture_value
    qt = np.sqrt(np.asarray(px) ** 2 + np.asarray(py) ** 2)
    s = np.where(qt > 0, na * np.asarray(px) / np.where(qt == 0, 1.0, qt), 0.0)
    t = np.where(qt > 0, na * np.asarray(py) / np.where(qt == 0, 1.0, qt), 0.0)
    # 归一化到 sinθ = NA（该点方向）
    scale = np.where(qt > 0, 1.0 / np.sqrt(np.maximum(1e-300, 1.0 - na * na * np.minimum(qt, 1.0) ** 2)), 1.0)
    d = _normalize(np.stack([(s + fx) * scale, (t + fy) * scale, np.ones_like(s)], axis=-1))
    x0 = np.zeros_like(d[..., 0])
    y0 = np.zeros_like(x0)
    z0 = np.zeros_like(x0)
    return x0, y0, z0, d


def trace(system: OpticalSystem, px, py, hx=0.0, hy=0.0, aim: bool = None, start=None) -> RayBlock:
    """追迹一批光线（同一结构、同一波长）。

    ``aim`` 为真时做实际光线瞄准（对应 RAIM 0 2）：二维牛顿迭代调发射方向/位置，
    使光线打在光阑面的 (px·r, py·r) 处。``hx``、``hy`` 是归一化视场坐标，可以逐光线给数组。
    给了 ``start`` 时理想透镜相位仍以 (hx, hy) 视场的主光线为零。
    """
    block = _trace_block(system, px, py, hx, hy, aim, start)
    _, _, hx, hy = np.broadcast_arrays(*(np.atleast_1d(np.asarray(v, dtype=float)) for v in (px, py, hx, hy)))
    block.phase = _paraxial_phase(system, block, hx.ravel(), hy.ravel(), aim)
    return block


def _trace_block(system: OpticalSystem, px, py, hx=0.0, hy=0.0, aim: bool = None, start=None) -> RayBlock:
    """``trace`` 的追迹部分，``phase`` 留零。"""
    px, py, hx, hy = np.broadcast_arrays(*(np.atleast_1d(np.asarray(v, dtype=float)) for v in (px, py, hx, hy)))
    px, py, hx, hy = (np.ascontiguousarray(v.ravel()) for v in (px, py, hx, hy))
    # 视场渐晕因子只改光瞳坐标：先换成本视场的偏心/压缩/旋转，再发射与瞄准（手册 Vignetting Factors）
    px, py = (np.ascontiguousarray(v.ravel(), dtype=float) for v in system.vignetted_pupil(px, py, hx, hy))
    n_ray = px.size
    packed = system.packed() if start is None else None
    if packed is not None:
        return _trace_compiled(system, packed, px, py, hx, hy, aim)
    if start is not None:
        # 校验用：直接从给定的起点/方向追（跳过发射与瞄准），可把"瞄准"这一环单独隔离
        x0, y0, z0, d = start
        x0 = np.broadcast_to(np.asarray(x0, dtype=float), px.shape).copy()
        y0 = np.broadcast_to(np.asarray(y0, dtype=float), px.shape).copy()
        z0 = np.broadcast_to(np.asarray(z0, dtype=float), px.shape).copy()
        d = np.broadcast_to(np.asarray(d, dtype=float), px.shape + (3,)).copy()
    else:
        x0, y0, z0, d = _launch(system, px, py, hx, hy)
        do_aim = _aiming(system) if aim is None else aim
        if do_aim:
            x0, y0, z0, d = _aim(system, x0, y0, z0, d, px, py)

    n_items = len(system.items)
    out = {
        "x": np.zeros((n_ray, n_items)),
        "y": np.zeros((n_ray, n_items)),
        "z": np.zeros((n_ray, n_items)),
        "l": np.zeros((n_ray, n_items)),
        "m": np.zeros((n_ray, n_items)),
        "n": np.zeros((n_ray, n_items)),
        "opl": np.zeros((n_ray, n_items)),
    }
    error = np.zeros(n_ray, dtype=int)
    vig = np.zeros(n_ray, dtype=int)

    X, Y, Z = x0.copy(), y0.copy(), z0.copy()
    L, M, N = d[..., 0].copy(), d[..., 1].copy(), d[..., 2].copy()
    opl = np.zeros(n_ray)
    n_cur = np.full(n_ray, system.n_before(0))
    lam = system.wavelength * 1e-3  # µm → mm

    for k, it in enumerate(system.items):
        # 传递到该面（局部坐标：z 相对顶点）
        zloc = Z - it.vertex_z
        live = error == 0
        t = np.zeros(n_ray)
        if live.all():
            t = it.shape.intersect_batch(X, Y, zloc, L, M, N)
        elif live.any():
            t[live] = it.shape.intersect_batch(X[live], Y[live], zloc[live], L[live], M[live], N[live])
        good = np.isfinite(t) & live
        error = np.where(live & ~good, it.index, error)
        # OPL 以第 1 面为 0（物体在无穷远时 Zemax 也是这么报的；见 oracle 批量追迹的 opl 列）。
        # 反射之后 n_cur 为负，光程按 |n|·路程 累计。
        if k > 0:
            opl = opl + abs(n_cur) * np.where(good, t, 0.0)
        X = X + np.where(good, t, 0.0) * L
        Y = Y + np.where(good, t, 0.0) * M
        Z = Z + np.where(good, t, 0.0) * N
        zloc = Z - it.vertex_z

        zs, zx, zy = it.shape.sag(X, Y)
        zs = np.broadcast_to(np.asarray(zs, dtype=float), X.shape).copy()
        if it.shape.flat_intersection:
            # Fresnel：求交按平面，折射按偶次非球面基底（手册 §2.3.1.2.32）
            zx, zy = it.shape.refraction_gradient(X, Y)

        # 渐晕/遮挡：按面孔径（不是按 DIAM；仅设通光半口径时 Zemax 不遮挡光线）
        if it.surface.aperture_code:
            vig = np.where(good & (vig == 0) & aperture_clipped(it, X, Y, aiming=system.lens.ray_aiming > 0), it.index, vig)

        # 折射
        if it.type == "PARAXIAL" and it.focal:
            n2 = it.n_after
            # 理想透镜的相位不进 opl（批量追迹口径），由 _paraxial_phase 单独给出；
            # 波前少了它会多出 ~10 波的假离焦（piA 实测 8.75 → 0.12 waves RMS）。
            # 近轴薄透镜（OpticStudio 24.1 实测，面后为 N-BK7、N-SF6 时差 ≤ 7e-18）：n'·tan u' = n·tan u − y/f
            L, M, N = _normalize_uv((n_cur * L / N - X / it.focal) / n2, (n_cur * M / N - Y / it.focal) / n2)
            L, M, N = it.sign * L, it.sign * M, it.sign * N
            n_cur = np.full(n_ray, n2)
        else:
            n2 = it.n_after
            Nx, Ny, Nz = -np.asarray(zx, float), -np.asarray(zy, float), np.ones_like(X)
            norm = np.sqrt(Nx * Nx + Ny * Ny + Nz * Nz)
            Nx, Ny, Nz = Nx / norm, Ny / norm, Nz / norm
            phase = it.shape.phase_grad(X, Y) if it.shape.diffractive else None
            if it.reflect:
                L, M, N, err = _reflect(L, M, N, Nx, Ny, Nz)
            elif phase is None:
                L, M, N, err = _refract(L, M, N, Nx, Ny, Nz, n_cur, n2)
            else:
                phi, phx, phy = phase
                L, M, N, err = _grating(L, M, N, Nx, Ny, Nz, n_cur, n2, phx, phy, zx, zy, lam)
                # 相位加到光程上（手册 §2.3.1.2.10）；正光焦度二元面的二次项为负，与符号约定一致
                opl = opl + (lam / (2.0 * np.pi)) * (phi if phi is not None else 0.0)
            error = np.where(err & (error == 0), -it.index, error)
            n_cur = np.full(n_ray, n2)

        out["x"][:, k] = X
        out["y"][:, k] = Y
        out["z"][:, k] = zloc
        # 对外报告的方向余弦带上 σ（反射一次翻一次），与 OpticStudio 批量追迹同口径
        out["l"][:, k] = L * it.sign
        out["m"][:, k] = M * it.sign
        out["n"][:, k] = N * it.sign
        out["opl"][:, k] = opl

    return RayBlock(
        x=out["x"],
        y=out["y"],
        z=out["z"],
        l=out["l"],
        m=out["m"],
        n=out["n"],
        opl=out["opl"],
        phase=np.zeros_like(out["x"]),
        error=error,
        vig=vig,
        surface_index=[it.index for it in system.items],
    )


def _trace_compiled(system: OpticalSystem, packed, px, py, hx, hy, aim) -> RayBlock:
    """编译内核路径：发射在这里算，EPD 瞄准与逐面追迹在内核里逐条完成；OBNA 瞄准仍走 numpy 延拓。"""
    from . import kernel

    x0, y0, z0, d = _launch(system, px, py, hx, hy)
    do_aim = (_aiming(system) if aim is None else aim) and system.stop is not None
    # 内核只做物在无穷远的 EPD / Float By Stop Size 瞄准（调发射位置）；有限物距调发射方向，走 numpy 延拓
    epd_aim = do_aim and system.lens.aperture_type in ("EPD", "FLOA") and not system.finite_object
    if do_aim and not epd_aim:
        x0, y0, z0, d = _aim(system, x0, y0, z0, d, px, py)
    tx, ty = system.aim_target(px, py) if epd_aim else (None, None)
    scale = system.lens.aperture_value or 2.0 * system.pupil_radius()
    x, y, z, l, m, n, opl, error, vig = kernel.trace(packed, x0, y0, z0, d, epd_aim, tx, ty, aim_scale=scale)
    return RayBlock(x=x, y=y, z=z, l=l, m=m, n=n, opl=opl, phase=np.zeros_like(x), error=error, vig=vig,
                    surface_index=[it.index for it in system.items])


def _normalize_uv(ux, uy):
    v = _normalize(np.stack([ux, uy, np.ones_like(ux)], axis=-1))
    return v[..., 0], v[..., 1], v[..., 2]


def _refract(L, M, N, Nx, Ny, Nz, n1, n2):
    """向量形式 Snell；全反射时返回错误标记。"""
    n1 = np.asarray(n1, dtype=float)
    n1 = np.broadcast_to(n1, L.shape)
    dn = L * Nx + M * Ny + N * Nz
    flip = dn > 0
    # 法线翻到入射侧（使 d·N < 0），入射角余弦取正
    Nx = np.where(flip, -Nx, Nx)
    Ny = np.where(flip, -Ny, Ny)
    Nz = np.where(flip, -Nz, Nz)
    cos1 = np.abs(dn)
    mu = n1 / n2
    sin2_2 = mu * mu * (1.0 - cos1 * cos1)
    tir = sin2_2 > 1.0
    cos2 = np.sqrt(np.maximum(1.0 - sin2_2, 0.0))
    Lt = mu * L + (mu * cos1 - cos2) * Nx
    Mt = mu * M + (mu * cos1 - cos2) * Ny
    Nt = mu * N + (mu * cos1 - cos2) * Nz
    v = np.stack([Lt, Mt, Nt], axis=-1)
    v = _normalize(v)
    return v[..., 0], v[..., 1], v[..., 2], tir


def _reflect(L, M, N, Nx, Ny, Nz):
    """反射面：``d′ = d − 2(d·N)N``（法线先翻到入射侧，使 d·N < 0）。返回 (l, m, n, 永假)。"""
    dn = L * Nx + M * Ny + N * Nz
    flip = dn > 0
    Nx = np.where(flip, -Nx, Nx)
    Ny = np.where(flip, -Ny, Ny)
    Nz = np.where(flip, -Nz, Nz)
    g = 2.0 * np.abs(dn)
    v = _normalize(np.stack([L + g * Nx, M + g * Ny, N + g * Nz], axis=-1))
    return v[..., 0], v[..., 1], v[..., 2], np.zeros(L.shape, bool)


def _grating(L, M, N, Nx, Ny, Nz, n1, n2, phx, phy, zx, zy, lam):
    """衍射面的方向更新：折射与光栅合并成同一条切向波矢方程。

    ``n2·d′ = n1·d + (λ/2π)·∇φ_t + s·N``，其中 ∇φ_t 是相位梯度在**切平面**上的投影，
    s 由 |d′| = 1 解出（取 d′·N > 0 的那个根）。相位梯度给的是 x/y 方向的偏导，
    它在面上的三维梯度是 (φx, φy, φx·zx + φy·zy)，再去掉法向分量。
    """
    n1 = np.broadcast_to(np.asarray(n1, dtype=float), L.shape)
    dn = L * Nx + M * Ny + N * Nz
    flip = dn > 0
    Nx = np.where(flip, -Nx, Nx)
    Ny = np.where(flip, -Ny, Ny)
    Nz = np.where(flip, -Nz, Nz)
    gx = np.asarray(phx, dtype=float)
    gy = np.asarray(phy, dtype=float)
    gz = gx * zx + gy * zy
    gdot = gx * Nx + gy * Ny + gz * Nz
    gx, gy, gz = gx - gdot * Nx, gy - gdot * Ny, gz - gdot * Nz
    c = lam / (2.0 * np.pi)
    Ax = n1 * L + c * gx
    Ay = n1 * M + c * gy
    Az = n1 * N + c * gz
    an = Ax * Nx + Ay * Ny + Az * Nz
    disc = an * an - (Ax * Ax + Ay * Ay + Az * Az) + n2 * n2
    tir = disc < 0.0
    s = -an + np.sqrt(np.maximum(disc, 0.0))
    v = np.stack([Ax + s * Nx, Ay + s * Ny, Az + s * Nz], axis=-1) / n2
    v = _normalize(v)
    return v[..., 0], v[..., 1], v[..., 2], tir


def _aim(system: OpticalSystem, x0, y0, z0, d, px, py, iters: int = 60):
    """实际光线瞄准（RAIM = 2）。

    两种模式（由孔径类型决定，依据 oracle 实测）：
    - EPD：锚在入瞳平面，调发射**位置**，让光线打在光阑的 (px,py) 处；
    - OBNA（物在无穷远）：锚在第 1 面顶点，调发射**方向**（Zemax 实测交点为 (0,0)）。

    光阑面上的瞄准目标由 `system.aim_target()` 给出（近轴传递 × 孔径），
    各方向分别计算——这类系统（双锥面 + 圆柱面）的近轴 Pupil 是各向异性的。

    方向模式用**延拓**求解：光瞳坐标从 0 按 s∈(0,1] 逐段增大，目标点取 s·(tx,ty)，
    每段用上一段的解作初值、步长受限的阻尼 Newton（|Δu| ≤ 0.02）。这样不会像直接 Newton
    那样跳到圆柱面的反射分支（piB 光阑落在面 9 的圆柱面上，多值根，见 docs/plans/rtc/HANDOFF.md §6.5）；
    失败时把步长减半重试，而不是换一个根。
    """
    k_stop = system.stop
    if k_stop is None:
        return x0, y0, z0, d
    tx, ty = system.aim_target(px, py)

    if system.lens.aperture_type == "OBNA" or system.finite_object:
        ux = np.array(d[..., 0] / d[..., 2], dtype=float)
        uy = np.array(d[..., 1] / d[..., 2], dtype=float)
        # 延拓起点：轴上物点用轴向光线（斜率 0），不能用近轴估计：估计一旦偏到另一支，延拓就跟踪错的分支
        # （piB 的圆柱面焦散就是这么出现多值根的）。离轴物点用射向近轴入瞳中心的主光线方向，否则第一步就要跨过整个视场角。
        z_ep, _ = system.entrance_pupil()
        off_axis = (np.abs(x0) > 0) | (np.abs(y0) > 0)
        failed = 0
        for i in range(ux.size):
            start = np.zeros(2)
            if off_axis.flat[i] and np.isfinite(z_ep):
                span = z_ep - float(z0.flat[i])
                start = np.array([-float(x0.flat[i]) / span, -float(y0.flat[i]) / span])
            u, ok = _aim_one_direction(
                system, float(x0[i]), float(y0[i]), float(z0[i]),
                start, np.array([tx[i], ty[i]]),
            )
            if ok:
                ux[i], uy[i] = u
            else:
                failed += 1  # 保留近轴发射方向
        if failed:
            # 与 OpticStudio 一致：实光瞄准失败时退回近轴并给警告（手册 §2.1.1.7）
            warnings.warn(
                f"{failed}/{ux.size} 条光线的实际瞄准不收敛，已退回近轴发射方向；"
                f"光阑面 {system.items[system.stop].index} 可能落在圆柱/焦散上，或目标点超出可追迹区域",
                RuntimeWarning,
                stacklevel=2,
            )
        d = _normalize(np.stack([ux, uy, np.ones_like(ux)], axis=-1))
        return x0, y0, z0, d

    # EPD：调发射位置。保留原来的向量化 Newton，但加步长限幅（避免走出入射光瞳面附近的收敛域）。
    x0 = x0.copy()
    y0 = y0.copy()
    scale = max(1.0, float(np.max(np.abs(x0)) + np.max(np.abs(y0))))
    for _ in range(iters):
        bx = _trace_wo_aim(system, x0, y0, z0, d)
        hx = bx["x"] - tx
        hy = bx["y"] - ty
        if max(np.max(np.abs(hx)), np.max(np.abs(hy))) < 1e-13:
            break
        h = 1e-7 * scale
        cols = []
        for axis in range(2):
            xx, yy = x0.copy(), y0.copy()
            if axis == 0:
                xx = xx + h
            else:
                yy = yy + h
            b2 = _trace_wo_aim(system, xx, yy, z0, d)
            cols.append(((b2["x"] - bx["x"]) / h, (b2["y"] - bx["y"]) / h))
        (jxx, jyx), (jxy, jyy) = cols
        det = jxx * jyy - jxy * jyx
        det = np.where(np.abs(det) < 1e-14, 1e-14, det)
        dx = (-hx * jyy + hy * jxy) / det
        dy = (-hy * jxx + hx * jyx) / det
        # 信赖域：一步不超过 1/4 光瞳半径
        step = np.maximum(np.abs(dx), np.abs(dy))
        cap = 0.25 * max(scale, 1e-9)
        s = np.where(step > cap, cap / np.where(step == 0, 1.0, step), 1.0)
        x0 = x0 + s * dx
        y0 = y0 + s * dy
    return x0, y0, z0, d


def _aiming(system: OpticalSystem) -> bool:
    """文件里的瞄准方式是否要迭代：实际瞄准（RAIM 2）总要；近轴瞄准（RAIM 1）只在 allow_unverified 时按手册做
    （光阑半径由近轴边缘光线定，此后同样逐条瞄到光阑上），否则保持原来的不瞄准。"""
    mode = system.lens.ray_aiming
    return mode == 2 or (mode == 1 and system.allow_unverified)


def _stop_intercept(system: OpticalSystem, x0: float, y0: float, z0: float,
                    u: np.ndarray) -> np.ndarray:
    """单条光线：给定横向斜率 u=(ux,uy)，返回光阑面的交点 (x,y)。"""
    d = _normalize(np.array([u[0], u[1], 1.0]))
    r = _trace_wo_aim(system, np.array([x0]), np.array([y0]), np.array([z0]), d[None, :])
    return np.array([float(r["x"][0]), float(r["y"][0])])


def _aim_one_direction(system: OpticalSystem, x0: float, y0: float, z0: float,
                       u_start: np.ndarray, target: np.ndarray,
                       trust: float = 0.02, ds0: float = 0.125):
    """单条光线沿光瞳坐标做延续，返回 ``(u, 是否收敛)``。

    ``target`` 是光瞳坐标 = 1 时的光阑交点；内部按 s∈(0,1] 逐步逼近 s·target。
    不收敛时返回最后一步的解并让调用方决定（OpticStudio 的语义是退回近轴 + 警告）。
    """
    u = np.array(u_start, dtype=float)
    s = 0.0
    ds = ds0
    while s < 1.0 - 1e-12:
        step = min(ds, 1.0 - s)
        u_new, ok = _aim_newton(system, x0, y0, z0, u, (s + step) * target, trust=trust)
        if ok:
            u, s = u_new, s + step
            ds = min(ds * 1.6, 0.25)
            continue
        ds *= 0.5
        if ds < 1e-3:
            return u, False
    return u, True


def _aim_newton(system: OpticalSystem, x0: float, y0: float, z0: float,
                u: np.ndarray, target: np.ndarray, trust: float = 0.02,
                iters: int = 6, h: float = 1e-6):
    """单条光线的 2×2 阻尼 Newton；返回 (u, 是否收敛)。"""
    f = _stop_intercept(system, x0, y0, z0, u) - target
    best = (np.max(np.abs(f)), np.array(u, dtype=float))
    for _ in range(iters):
        norm = float(np.max(np.abs(f)))
        if norm < 1e-13:
            return u, True
        if norm < best[0]:
            best = (norm, np.array(u, dtype=float))
        J = np.empty((2, 2))
        for j in range(2):
            du = np.zeros(2)
            du[j] = h
            J[:, j] = (_stop_intercept(system, x0, y0, z0, u + du)
                       - _stop_intercept(system, x0, y0, z0, u - du)) / (2.0 * h)
        det = J[0, 0] * J[1, 1] - J[0, 1] * J[1, 0]
        if abs(det) < 1e-18:
            return best[1], False
        step = -np.linalg.solve(J, f)
        # 步长限幅：这一步是防止跳到反射/伪根的关锰
        lim = min(1.0, trust / max(float(np.max(np.abs(step))), 1e-18))
        for _ in range(8):
            u_try = u + lim * step
            f_try = _stop_intercept(system, x0, y0, z0, u_try) - target
            if np.max(np.abs(f_try)) < norm:
                break
            lim *= 0.5
        else:
            return best[1], False
        u, f = u_try, f_try
    return (u, True) if float(np.max(np.abs(f))) < 1e-10 else (best[1], False)


def _trace_wo_aim(system: OpticalSystem, x0, y0, z0, d):
    """瞄准迭代内部用：从给定起点直接追到光阑面（不裁剪、不算 OPL）。"""
    X, Y, Z = x0.copy(), y0.copy(), z0.copy()
    L, M, N = d[..., 0].copy(), d[..., 1].copy(), d[..., 2].copy()
    n_cur = np.full(X.shape, system.n_before(0))
    for k, it in enumerate(system.items):
        zloc = Z - it.vertex_z
        t = it.shape.intersect_batch(X, Y, zloc, L, M, N).reshape(X.shape)
        X = X + t * L
        Y = Y + t * M
        Z = Z + t * N
        if it.type == "PARAXIAL" and it.focal:
            L, M, N = _normalize_uv((n_cur * L / N - X / it.focal) / it.n_after, (n_cur * M / N - Y / it.focal) / it.n_after)
            L, M, N = it.sign * L, it.sign * M, it.sign * N
            n_cur = np.full(X.shape, it.n_after)
        else:
            zs, zx, zy = it.shape.sag(X, Y)
            if it.shape.flat_intersection:
                zx, zy = it.shape.refraction_gradient(X, Y)
            Nx, Ny, Nz = -np.asarray(zx, float), -np.asarray(zy, float), np.ones_like(X)
            nn = np.sqrt(Nx * Nx + Ny * Ny + Nz * Nz)
            if it.reflect:
                L, M, N, _ = _reflect(L, M, N, Nx / nn, Ny / nn, Nz / nn)
            else:
                L, M, N, _ = _refract(L, M, N, Nx / nn, Ny / nn, Nz / nn, n_cur, it.n_after)
            n_cur = np.full(X.shape, it.n_after)
        if k == system.stop:
            break
    return {"x": X, "y": Y}
