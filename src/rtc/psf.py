"""出瞳、OPD、PSF（FFT / Huygens）、Strehl。

OPD 的定义（与 OpticStudio 一致）：
1. 以主光线在像面的交点为球心 C；
2. 参考球半径 R = |出瞳处主光线点 − C|；
3. 每条光线追到像面后沿其出射方向反向延伸到参考球面；
   OPD = (光程 + n_image·s) − 主光线的同一量。

Huygens PSF：出瞳每个采样点向像面每个网格点直接叠加子波，
复杂度 O(N光瞳 × N像面)，按像面行分块以限制内存。
"""

from __future__ import annotations

import numpy as np

from .system import OpticalSystem
from .trace import trace


def exit_pupil(system: OpticalSystem) -> tuple[float, float]:
    """(相对像面的位置, 半口径)。位置为负表示在像面之前。"""
    z, mag = system.exit_pupil()
    if system.stop is None:
        return z, float("nan")
    return z, abs(mag) * system.items[system.stop].semi


def pupil_grid(n: int = 32, kind: str = "square"):
    """归一化光瞳采样：(px, py, mask)。"""
    if kind == "square":
        v = np.linspace(-1.0, 1.0, n)
        px, py = np.meshgrid(v, v)
        px, py = px.ravel(), py.ravel()
        return px, py, np.hypot(px, py) <= 1.0
    if kind == "rings":
        arms = 2 * n
        px, py = [0.0], [0.0]
        for r in np.linspace(1.0 / n, 1.0, n):
            th = np.linspace(0.0, 2.0 * np.pi, arms, endpoint=False)
            px.extend((r * np.cos(th)).tolist())
            py.extend((r * np.sin(th)).tolist())
        return np.array(px), np.array(py), np.ones(len(px), dtype=bool)
    raise ValueError(f"未知光瞳网格 {kind}")


def _pupil_frame(system: OpticalSystem, px, py, mask):
    """出瞳采样点的物理坐标（全局 z，单位 mm）与参考球半径。"""
    z_pos, r_pupil = exit_pupil(system)
    k_img = len(system.items) - 1
    chief = trace(system, [0.0], [0.0])
    cx, cy, cz = chief.x[0, k_img], chief.y[0, k_img], chief.z[0, k_img]
    z_img = system.image_z
    z_p = z_img - z_pos if np.isfinite(z_pos) else z_img
    P = np.stack([r_pupil * px, r_pupil * py, np.full_like(px, z_p)], axis=-1)
    C = np.array([cx, cy, cz + (z_img - cz)])
    return P[mask], C, float(abs(z_pos)) if np.isfinite(z_pos) else np.inf


def _check_opd(system: OpticalSystem) -> None:
    bad = [it.surface.index for it in system.items if not it.shape.opd_supported]
    if bad:
        raise NotImplementedError(
            f"第 {bad} 面是 Fresnel 面：手册明确说该面型不支持 OPD/波前，"
            "只能给几何追迹与横向结果"
        )


def opd_rays(system: OpticalSystem, px, py, hx: float = 0.0, hy: float = 0.0):
    """视场 (hx, hy) 下一批光线相对该视场主光线的 OPD（mm），参考球心在主光线与像面的交点。
    返回 (opd, ok)：ok 为假的光线（追迹出错、被渐晕或拦截）opd 为 nan。不去倾斜；主光线自身的 OPD 恒为 0。

    参考球半径取近轴出瞳到像面的轴向距离（与轴上视场时的 :func:`opd_map` 同一定义）；离轴视场时
    OpticStudio 以主光线在出瞳的点到像点的距离为半径，两者在离轴时不同，尚未与 OpticStudio 对账。
    """
    _check_opd(system)
    px = np.atleast_1d(np.asarray(px, float))
    py = np.atleast_1d(np.asarray(py, float))
    px, py = np.broadcast_arrays(px, py)
    block = trace(system, np.r_[0.0, px.ravel()], np.r_[0.0, py.ravel()], hx, hy)
    opd, ok = opd_from_trace(system, block, chief_index=0)
    return opd[1:], ok[1:]


def opd_from_trace(system: OpticalSystem, block, *, chief_index: int = 0):
    """从同一视场、包含主光线的追迹结果计算 OPD（mm），复用光程和像面交点。"""
    _check_opd(system)
    z_pos, _ = exit_pupil(system)
    if not np.isfinite(z_pos):
        raise ValueError("出瞳在无穷远：请用平面参考（reference='planar'）而不是球面参考")
    k_img = len(system.items) - 1
    if int(block.error[chief_index]) != 0 or int(block.vig[chief_index]) != 0:
        raise ValueError("主光线追不到像面或被拦截，没有参考球面")
    C = np.array([block.x[chief_index, k_img], block.y[chief_index, k_img], block.z[chief_index, k_img]])
    R = abs(z_pos)
    n_img = system.items[-1].n_after

    def path(b):
        opl = b.opl[:, k_img] + b.phase[:, k_img]  # 波前含理想透镜相位
        d = np.stack([b.l[:, k_img], b.m[:, k_img], b.n[:, k_img]], axis=-1)
        P = np.stack([b.x[:, k_img], b.y[:, k_img], b.z[:, k_img]], axis=-1)
        diff = P - C[None, :]
        bq = -(diff * d).sum(axis=1)
        cq = (diff * diff).sum(axis=1) - R * R
        # 反向延长到参考球面：s² − 2·bq·s + cq = 0 的负根（此前写成 −bq − √…，交点不在球面上）
        s = bq - np.sqrt(np.maximum(bq * bq - cq, 0.0))
        # 符号同 OpticStudio OPDC：光程比参考球面短为正（2026-10-28 对照，见 tests/test_paraxial_glass.py）；
        # Huygens 叠加里 ph = k·(phase0 − dist) 也要这个符号
        return -(opl + n_img * s)

    paths = path(block)
    opd = paths - paths[chief_index]
    ok = (block.error == 0) & (block.vig == 0) & np.isfinite(opd)
    return np.where(ok, opd, np.nan), ok


def opd_map(system: OpticalSystem, px=None, py=None, n: int = 32, kind: str = "square",
            wavefront: bool = True, remove_tilt: bool = True, hx: float = 0.0, hy: float = 0.0):
    """OPD 图。返回 (opd, mask, px, py)，单位：``wavefront`` 真时是 waves，否则 mm。
    ``hx``、``hy`` 是归一化视场；mask 不含追迹出错、被渐晕或拦截的光线（它们的 opd 置 0）。

    面型里任何一面报 ``opd_supported = False``（目前只有 Fresnel，手册明说"OPD 不支持"）
    就直报错误，而不是给一个看似正常的波前。
    """
    if px is None or py is None:
        px, py, mask = pupil_grid(n, kind)
    else:
        px = np.atleast_1d(np.asarray(px, float))
        py = np.atleast_1d(np.asarray(py, float))
        mask = np.hypot(px, py) <= 1.0
    opd, ok = opd_rays(system, px, py, hx, hy)
    mask = mask & ok
    opd = np.where(ok, opd, 0.0)
    # 去掉 piston；再按需去掉倾斜（倾斜只让像点平移，不改变 Strehl——Zemax 的 STRH 与倾斜无关）
    opd = opd - opd[np.argmin(np.hypot(px, py))]
    if remove_tilt and mask.sum() >= 3:
        A = np.stack([np.ones(mask.sum()), px[mask], py[mask]], axis=1)
        coef, *_ = np.linalg.lstsq(A, opd[mask], rcond=None)
        opd = opd - (coef[0] + coef[1] * px + coef[2] * py)
    if wavefront:
        opd = opd / (system.wavelength * 1e-3)
    return opd, mask, px, py


def psf_fft(system: OpticalSystem, n: int = 64, pad: int = 2, kind: str = "square"):
    """FFT PSF：光瞳函数 A·exp(i2πW) 补零后做 FFT，取模方并归一化。"""
    opd, mask, px, py = opd_map(system, n=n, kind=kind, wavefront=True)
    grid = np.zeros((n, n), dtype=complex)
    ix = np.clip(np.round((px + 1.0) / 2.0 * (n - 1)).astype(int), 0, n - 1)
    iy = np.clip(np.round((py + 1.0) / 2.0 * (n - 1)).astype(int), 0, n - 1)
    np.add.at(grid, (iy[mask], ix[mask]), np.exp(2j * np.pi * opd[mask]))
    m = n * pad
    psf = np.abs(np.fft.fftshift(np.fft.fft2(grid, s=(m, m)))) ** 2
    return psf / psf.max()


def _huygens_setup(system: OpticalSystem, n: int, kind: str):
    """出瞳采样：返回 (opd_mm, mask, P, C, R)。"""
    opd, mask, px, py = opd_map(system, n=n, kind=kind, wavefront=False)
    P, C, R = _pupil_frame(system, px, py, mask)
    return opd, mask, P, C, R


def _huygens_intensity(phase0, P, C, R, xs, ys, lam, reference: str = "planar", chunk: int = 16):
    """Huygens 叠加：出瞳每个采样点向像面每个点叠加子波。phase0 单位 mm，未归一化强度。"""
    k = 2.0 * np.pi / lam
    if reference == "spherical" and np.isfinite(R):
        # 球面参考：出瞳平面上的点到参考球面还有 r²/(2R) 的二次附加光程
        r2 = (P[:, 0] - C[0]) ** 2 + (P[:, 1] - C[1]) ** 2
        phase0 = phase0 - 0.5 * r2 / R
    z_img = C[2]
    field = np.zeros((len(ys), len(xs)), dtype=complex)
    for j0 in range(0, len(ys), chunk):
        j1 = min(j0 + chunk, len(ys))
        gx, gy = np.meshgrid(xs, ys[j0:j1])
        Q = np.stack([gx, gy, np.full(gx.shape, z_img)], axis=-1)
        diff = Q[None, :, :, :] - P[:, None, None, :]
        dist = np.sqrt((diff * diff).sum(axis=-1))
        dist = np.maximum(dist, 1e-9)
        ph = k * (phase0[:, None, None] - (dist - np.abs(C[2] - P[:, 2])[:, None, None]))
        field[j0:j1] = (np.exp(1j * ph) / dist).sum(axis=0)
    return np.abs(field) ** 2


def psf_huygens(system: OpticalSystem, n: int = 24, image_pixels: int = 48,
                half_width: float | None = None, reference: str = "planar",
                kind: str = "square", chunk: int = 16):
    """Huygens PSF：出瞳每个采样点向像面每个点叠加子波。

    ``reference``：
    - ``planar``：相位只含 OPD（出瞳横跨像面时更稳，Codex 的实测结论）；
    - ``spherical``：再减去相对于参考球面的二次相位差 r²/(2R)。

    返回 ``(psf, xs, ys)``，xs/ys 是像面网格坐标（mm），psf 已归一化到峰值 1。
    """
    opd, mask, P, C, R = _huygens_setup(system, n, kind)
    xs, ys = _image_grid(system, C, half_width, image_pixels)
    psf = _huygens_intensity(opd[mask], P, C, R, xs, ys, system.wavelength * 1e-3,
                             reference=reference, chunk=chunk)
    return psf / psf.max(), xs, ys


def _image_grid(system: OpticalSystem, C, half_width, image_pixels: int):
    if half_width is None:
        # 以像方数值孔径定标：艾里半径 ≈ 0.61λ/NA，取 6 倍
        m = system.marginal_image_ray()
        na = system.items[-1].n_after * float(np.hypot(m[0], m[1]))
        half_width = 6.0 * 0.61 * (system.wavelength * 1e-3) / max(na, 1e-6)
    xs = C[0] + np.linspace(-half_width, half_width, image_pixels)
    ys = C[1] + np.linspace(-half_width, half_width, image_pixels)
    return xs, ys


def strehl_huygens(system: OpticalSystem, n: int = 24, image_pixels: int = 48,
                   half_width: float | None = None, reference: str = "planar",
                   kind: str = "square", chunk: int = 16) -> float:
    """STRH：Huygens PSF 峰值 / 同一采样下无像差 PSF 峰值。

    分母用**同一组出瞳采样点、同一个像面网格、同一个参考相位设置**，只是把相位全置 0
    （即手册里"STRH 的结果受 Huygens 参考相位设置影响"的那一层要两边一致，否则比值无意义）。
    手册 §2.1.1.6.4 把 STRH 归为依赖 Huygens 积分的操作数。
    """
    opd, mask, P, C, R = _huygens_setup(system, n, kind)
    xs, ys = _image_grid(system, C, half_width, image_pixels)
    lam = system.wavelength * 1e-3
    peak = _huygens_intensity(opd[mask], P, C, R, xs, ys, lam, reference=reference,
                              chunk=chunk).max()
    ideal = _huygens_intensity(np.zeros(int(mask.sum())), P, C, R, xs, ys, lam,
                               reference=reference, chunk=chunk).max()
    return float(peak / ideal)


def strehl(system: OpticalSystem, n: int = 32, kind: str = "square") -> float:
    """Strehl = |Σ A·exp(i2πW)|² / (Σ A)²（= OPD 全零时的峰值归一化）。"""
    opd, mask, _, _ = opd_map(system, n=n, kind=kind, wavefront=True)
    amp = np.exp(2j * np.pi * opd[mask])
    return float(np.abs(amp.sum()) ** 2 / amp.size**2)


def rms_wavefront(system: OpticalSystem, n: int = 32, kind: str = "square") -> float:
    """波前 RMS（waves，已去 piston 与倾斜）。"""
    opd, mask, _, _ = opd_map(system, n=n, kind=kind, wavefront=True)
    w = opd[mask]
    return float(np.sqrt(np.mean((w - w.mean()) ** 2)))


def otf_fft(system: OpticalSystem, hx: float = 0.0, hy: float = 0.0, n: int = 64, aberrated: bool = True):
    """单个视场、单个波长的 FFT OTF：光瞳函数 A·exp(i2πW) 在 n×n 方格上取样，自相关由补零 FFT 求得。
    返回 (otf_y, otf_x, df)：沿光瞳 y、x 方向错开 s 个格点（s = 0…n−1）的复 OTF（已按零频归一化），
    以及每错开一格对应的像方空间频率 df（cycles/mm）。``aberrated`` 为假时相位全置零（同一光瞳的衍射极限）。

    频率定标：错开 s 格 = s·(2/(n−1))·r_xp 的出瞳位移，ν = 位移·n′/(λ·R)，r_xp 为近轴出瞳半径、R 为出瞳到像面距离、
    n′ 为像方折射率；截止频率 2·r_xp·n′/(λ·R) 即近轴 2NA′/λ。与 OpticStudio FFT MTF 的取样与插值方式不同，尚未对账。
    """
    z_pos, r_xp = exit_pupil(system)
    if not (np.isfinite(z_pos) and np.isfinite(r_xp)) or r_xp <= 0.0:
        raise ValueError("出瞳在无穷远或没有光阑，FFT MTF 的频率没有定义")
    opd, mask, px, py = opd_map(system, n=n, kind="square", wavefront=True, remove_tilt=True, hx=hx, hy=hy)
    if mask.sum() < 4:
        raise ValueError(f"视场 ({hx}, {hy}) 能通过的光线不足 4 条，算不出 MTF")
    phase = opd if aberrated else np.zeros_like(opd)
    pupil = np.zeros(n * n, dtype=complex)
    pupil[mask] = np.exp(2j * np.pi * phase[mask])
    # pupil_grid 的 meshgrid(v, v)：行是 py，列是 px
    pupil = pupil.reshape(n, n)
    spectrum = np.fft.fft2(pupil, s=(2 * n, 2 * n))
    auto = np.fft.ifft2(np.abs(spectrum) ** 2)
    auto = auto / auto[0, 0]
    lam_mm = system.wavelength * 1e-3
    n_img = system.items[-1].n_after
    df = (2.0 / (n - 1)) * r_xp * n_img / (lam_mm * abs(z_pos))
    return auto[:n, 0], auto[0, :n], df


def mtf_fft(systems, hx: float = 0.0, hy: float = 0.0, frequencies=None, n: int = 64, aberrated: bool = True) -> dict:
    """多色 FFT MTF：``systems`` 为 [(OpticalSystem, 权重)]，同一视场 (hx, hy)。各波长的复 OTF 线性插值到同一组频率后按权重平均，
    再取模。子午取光瞳 y 方向、弧矢取 x 方向（y 轴上的视场与 OpticStudio 的约定一致）。各波长以自己的主光线为参考，
    所以波长之间的垂轴色差不进 MTF；OpticStudio 以主波长主光线为参考，两者不同，尚未对账。
    ``frequencies`` 省略时取 0 到各波长截止频率最大值之间 41 点。返回 {frequency, tangential, sagittal, cutoff}。
    """
    parts = [(otf_fft(s, hx, hy, n, aberrated), float(w)) for s, w in systems if float(w) > 0.0]
    if not parts:
        raise ValueError("没有权重为正的波长")
    cutoff = max(df * (n - 1) for (_, _, df), _ in parts)
    freq = np.linspace(0.0, cutoff, 41) if frequencies is None else np.asarray(frequencies, float)
    total = sum(w for _, w in parts)
    out = {}
    for name, axis in (("tangential", 0), ("sagittal", 1)):
        acc = np.zeros(len(freq), dtype=complex)
        for (oy, ox, df), w in parts:
            otf = oy if axis == 0 else ox
            grid = df * np.arange(len(otf))
            # 超出这一波长截止频率的部分为 0 / beyond this wavelength's cutoff the OTF is zero
            re = np.interp(freq, grid, otf.real, right=0.0)
            im = np.interp(freq, grid, otf.imag, right=0.0)
            acc += w * (re + 1j * im)
        out[name] = np.abs(acc / total)
    return {"frequency": freq, "tangential": out["tangential"], "sagittal": out["sagittal"], "cutoff": float(cutoff)}
