"""序列追迹的 Python 层：把归一化坐标变成发射光线、定瞄准目标、把内核结果排成 RayBlock。

单步物理在 rtc.kernel 里；这里只有 OpticStudio 的发射和瞄准约定。

物理约定（与 OpticStudio 一致，已用 oracle 批量追迹对照）：
- 坐标：z 为光轴，光线沿 +z 前进；每面的矢高相对该面顶点。
- 输出 ``x, y, z`` 是该面局部坐标下的交点（z = 该点矢高），``l, m, n`` 是**该面折射后**的方向。
- 光程 OPL 从第 1 面开始累计（物在无穷远时第 1 面处 OPL = 0）。
- 错误码与渐晕码沿用 OpticStudio 批量追迹的约定：错误码为正 = 光线在该面丢失（面号），为负 = 在该面全反射（−面号）；
  渐晕码 = 光线被面孔径挡住的第一个面号，0 为通过。
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass

import numpy as np

from . import dual as D
from . import geometry, kernel
from .system import OpticalSystem


def aperture_clipped(item, x, y, *, aiming: bool = False) -> np.ndarray:
    """按面孔径判断哪些光线被挡（布尔数组，可广播）。孔径类型码见 :attr:`rtc.zmx.Surface.aperture_code`。"""
    return kernel.clipped(geometry.aperture_row(item, aiming), x, y)


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


def _lens_phase_step(x, y, tx, ty, xc, yc, tcx, tcy, m):
    """沿面从主光线 (xc, yc) 到本光线 (x, y) 积分 m·(L, M)：T = m·tan 是光线在折射率 m 里的方向（Tc 为主光线的）。

    m·(L, M) = T/√(1+|T|²/m²) 是 H(T) = m²(√(1+|T|²/m²) − 1) 对 T 的梯度。光束来自一个点（入射无像差）时，
    T 沿面是线性的 T = Tc + a·(r − rc)，积分恰为 (H(T) − H(Tc))/a；分子分母同乘消去 a，得到下面不含 a、a → 0 也稳定的式子。
    """
    num = (x - xc) * (tx + tcx) + (y - yc) * (ty + tcy)
    return num / (D.sqrt(1.0 + (tx * tx + ty * ty) / (m * m)) + D.sqrt(1.0 + (tcx * tcx + tcy * tcy) / (m * m)))


def paraxial_lens_phase(f, n1, n2, x, y, l, m, n, xc, yc, lc, mc, nc):
    """一个 PARAXIAL 面引入的相位（mm，相对同视场主光线）。(l, m, n) 是面后方向；ndarray 和 Dual 都可以。

    OPD Mode 1 积分实际相位：相位梯度是 n'·(L', M') − n·(L, M)，出射规则 n'·tan u' = n·tan u − y/f。
    入射光束无像差时这是精确值；2026-10-28 与 OpticStudio OPDC 对照（面后空气 / N-BK7，边缘 32 / 185 waves）差 ≤ 1e-5 waves。
    入射光束有像差时 OpticStudio 的积分路径未公开，这里仍按上式；PLEN 对照残差 ≤ 1.3e-6 mm。
    """
    tx2, ty2 = n2 * l / n, n2 * m / n
    tcx2, tcy2 = n2 * lc / nc, n2 * mc / nc
    return (_lens_phase_step(x, y, tx2, ty2, xc, yc, tcx2, tcy2, n2)
            - _lens_phase_step(x, y, tx2 + x / f, ty2 + y / f, xc, yc, tcx2 + xc / f, tcy2 + yc / f, n1))


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
                acc = acc + paraxial_lens_phase(
                    it.focal, system.n_before(k), it.n_after,
                    block.x[:, k], block.y[:, k], block.l[:, k], block.m[:, k], block.n[:, k],
                    chief.x[inv, k], chief.y[inv, k], chief.l[inv, k], chief.m[inv, k], chief.n[inv, k])
            phase[:, k] = acc
    return phase


def _normalize(v):
    return v / np.sqrt(np.sum(v * v, axis=-1, keepdims=True))


def launch(system: OpticalSystem, px, py, hx=0.0, hy=0.0):
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


def aim_by_position(system: OpticalSystem) -> bool:
    """物在无穷远的 EPD / Float By Stop Size 调发射位置，其余（OBNA、有限物距）调发射斜率。依据 oracle 实测。"""
    return system.lens.aperture_type in ("EPD", "FLOA") and not system.finite_object


def aim(system: OpticalSystem, x0, y0, z0, d, px, py):
    """实际光线瞄准（RAIM = 2）：让光线打在光阑面的 ``aim_target`` 上，返回新的 (x0, y0, z0, d)。

    方向模式沿光瞳坐标延拓，不会跳到圆柱面的反射分支（piB 光阑落在面 9 的圆柱面上，多值根，见
    docs/plans/rtc/HANDOFF.md §6.5）。与 OpticStudio 一致，不收敛时退回近轴发射方向并给警告（手册 §2.1.1.7）。
    """
    packed = system.packed()
    tx, ty = system.aim_target(px, py)
    if aim_by_position(system):
        scale = system.lens.aperture_value or 2.0 * system.pupil_radius()
        x0, y0 = kernel.aim_positions(packed, x0, y0, z0, d, tx, ty, scale)
        return x0, y0, z0, d
    # 延拓起点：轴上物点用轴向光线（斜率 0），不能用近轴估计：估计一旦偏到另一支，延拓就跟踪错的分支
    # （piB 的圆柱面焦散就是这么出现多值根的）。离轴物点用射向近轴入瞳中心的主光线方向，否则第一步就要跨过整个视场角。
    z_ep, _ = system.entrance_pupil()
    start_x, start_y = np.zeros_like(x0), np.zeros_like(y0)
    if np.isfinite(z_ep):
        off_axis = (np.abs(x0) > 0) | (np.abs(y0) > 0)
        span = np.where(off_axis, z_ep - z0, 1.0)
        start_x, start_y = np.where(off_axis, -x0 / span, 0.0), np.where(off_axis, -y0 / span, 0.0)
    ux, uy, ok = kernel.aim_directions(packed, x0, y0, z0, start_x, start_y, tx, ty)
    failed = int(np.count_nonzero(~ok))
    if failed:
        warnings.warn(
            f"{failed}/{ok.size} 条光线的实际瞄准不收敛，已退回近轴发射方向；"
            f"光阑面 {system.items[system.stop].index} 可能落在圆柱/焦散上，或目标点超出可追迹区域",
            RuntimeWarning,
            stacklevel=2,
        )
    w = np.ones_like(ux)
    aimed = _normalize(np.stack([ux, uy, w], axis=-1))
    return x0, y0, z0, np.where(ok[..., None], aimed, d)


def stop_hits(system: OpticalSystem, x0, y0, z0, d):
    """给定起点和方向，追到光阑面的交点 (x, y)，不裁剪。"""
    return kernel.stop_hits(system.packed(), x0, y0, z0, d)


def trace(system: OpticalSystem, px, py, hx=0.0, hy=0.0, aim: bool = None, start=None) -> RayBlock:
    """追迹一批光线（同一结构、同一波长）。

    ``aim`` 为真时做实际光线瞄准（对应 RAIM 0 2）。``hx``、``hy`` 是归一化视场坐标，可以逐光线给数组。
    给了 ``start``（起点、方向）时直接从那里追，理想透镜相位仍以 (hx, hy) 视场的主光线为零。
    """
    block = _trace_block(system, px, py, hx, hy, aim, start)
    _, _, hx, hy = np.broadcast_arrays(*(np.atleast_1d(np.asarray(v, dtype=float)) for v in (px, py, hx, hy)))
    block.phase = _paraxial_phase(system, block, hx.ravel(), hy.ravel(), aim)
    return block


def aiming(system: OpticalSystem) -> bool:
    """文件里的瞄准方式是否要迭代：实际瞄准（RAIM 2）总要；近轴瞄准（RAIM 1）只在 allow_unverified 时按手册做
    （光阑半径由近轴边缘光线定，此后同样逐条瞄到光阑上），否则保持原来的不瞄准。"""
    mode = system.lens.ray_aiming
    return (mode == 2 or (mode == 1 and system.allow_unverified)) and system.stop is not None


def launched(system: OpticalSystem, px, py, hx, hy, do_aim):
    """瞄准后的发射光线；px、py 已按渐晕因子换算。"""
    x0, y0, z0, d = launch(system, px, py, hx, hy)
    if (aiming(system) if do_aim is None else do_aim and system.stop is not None):
        x0, y0, z0, d = aim(system, x0, y0, z0, d, px, py)
    return x0, y0, z0, d


def _trace_block(system: OpticalSystem, px, py, hx=0.0, hy=0.0, aim: bool = None, start=None) -> RayBlock:
    """``trace`` 的追迹部分，``phase`` 留零。"""
    px, py, hx, hy = np.broadcast_arrays(*(np.atleast_1d(np.asarray(v, dtype=float)) for v in (px, py, hx, hy)))
    px, py, hx, hy = (np.ascontiguousarray(v.ravel()) for v in (px, py, hx, hy))
    # 视场渐晕因子只改光瞳坐标：先换成本视场的偏心/压缩/旋转，再发射与瞄准（手册 Vignetting Factors）
    px, py = (np.ascontiguousarray(v.ravel(), dtype=float) for v in system.vignetted_pupil(px, py, hx, hy))
    if start is not None:
        x0, y0, z0, d = start
        x0, y0, z0 = (np.broadcast_to(np.asarray(v, dtype=float), px.shape) for v in (x0, y0, z0))
        d = np.broadcast_to(np.asarray(d, dtype=float), px.shape + (3,))
    else:
        x0, y0, z0, d = launched(system, px, py, hx, hy, aim)
    out, error, vig, _ = kernel.trace(system.packed(), x0, y0, z0, d)
    return RayBlock(x=out[..., 0], y=out[..., 1], z=out[..., 2], l=out[..., 3], m=out[..., 4], n=out[..., 5], opl=out[..., 6],
                    phase=np.zeros(out.shape[:2]), error=error, vig=vig, surface_index=[it.index for it in system.items])
