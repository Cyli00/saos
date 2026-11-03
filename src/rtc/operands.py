"""操作数（都是追迹结果的后处理）。

已验证：REAX / REAY / REAC / INDX / SSAG / EXPP / EXPD（对 oracle 批量追迹与操作数实测）。
未验证：CTGT/CTLT/ETGT/MNET/MNCT/MNCA（定义按手册写，缺 oracle 对照，见 GAP-ANALYSIS）。
"""

from __future__ import annotations

import numpy as np

from .system import OpticalSystem


class Operands:
    """绑定一个系统 + 一条主光线（chief ray）结果的操作数求值器。

    ``px, py`` 是考察的光瞳坐标；``ray_aiming`` 等系统级设置来自 ``.zmx``。
    """

    def __init__(self, system: OpticalSystem):
        self.system = system
        self._blocks = {}

    def block(self, px, py):
        from .trace import trace

        key = (tuple(np.atleast_1d(px).ravel()), tuple(np.atleast_1d(py).ravel()))
        if key not in self._blocks:
            self._blocks[key] = trace(self.system, px, py)
        return self._blocks[key]

    # ---- 光线类 ----
    def reax(self, surf: int, px=0.0, py=0.0):
        b = self.block(px, py)
        return b.at(surf)["x"]

    def reay(self, surf: int, px=0.0, py=0.0):
        b = self.block(px, py)
        return b.at(surf)["y"]

    def reac(self, surf: int, px=0.0, py=0.0):
        """y 方向余弦（折射后）。"""
        b = self.block(px, py)
        return b.at(surf)["m"]

    def raid(self, surf: int, px=0.0, py=0.0):
        """入射角（度）：光线与该面法线的夹角。"""
        b = self.block(px, py)
        k = b.surface_index.index(surf)
        it = self.system.items[k]
        zb = b.z[:, k]
        # 入射方向 = 上一面折射后的方向
        if k == 0:
            raise ValueError("第 1 面没有入射光线方向")
        l, m, n = b.l[:, k - 1], b.m[:, k - 1], b.n[:, k - 1]
        _, zx, zy = it.shape.sag(b.x[:, k], b.y[:, k])
        Nx, Ny, Nz = -np.asarray(zx, float), -np.asarray(zy, float), np.ones_like(zb)
        nn = np.sqrt(Nx * Nx + Ny * Ny + Nz * Nz)
        cos = np.abs((l * Nx + m * Ny + n * Nz) / nn)
        return np.degrees(np.arccos(np.clip(cos, -1.0, 1.0)))

    def trac(self, surf: int, px=0.0, py=0.0):
        """横向像差：该光线的 y 交点减去主光线的 y 交点。"""
        b = self.block(px, py)
        c = self.block(0.0, 0.0)
        return b.at(surf)["y"] - c.at(surf)["y"][0]

    # ---- 材料 ----
    def indx(self, surf: int):
        """第 ``surf`` 面之前那段介质的折射率（与 oracle 实测一致：INDX 取面间介质、面号从 1 起）。"""
        k = self.system.items.index(next(it for it in self.system.items if it.index == surf))
        return self.system.n_before(k)

    # ---- 面形 ----
    def ssag(self, x, y, mode: int = 0):
        """第 1 个光学面的矢高。mode 0 = 含圆锥基底，1 = 只有多项式偏离量。"""
        it = self.system.items[0]
        z, _, _ = it.shape.sag(x, y)
        if mode == 1:
            from .geometry import Paraxial, Standard

            base = it.surface.curvature
            z0, _, _ = Standard(it.surface).sag(x, y)
            z = np.asarray(z) - np.asarray(z0)
        return z

    # ---- 光瞳 ----
    def expp(self) -> float:
        """出瞳位置（相对像面，向前为正）。"""
        return self.system.exit_pupil()[0]

    def expd(self) -> float:
        """出瞳直径。"""
        z, mag = self.system.exit_pupil()
        if self.system.stop is None:
            return float("nan")
        r_stop = self.system.items[self.system.stop].semi
        return 2.0 * abs(mag) * r_stop

    # ---- 像质 ----
    def strh(self, n: int = 24, image_pixels: int = 48, reference: str = "planar",
             kind: str = "square") -> float:
        """STRH：Huygens PSF 峰值比（依赖 Huygens 参考相位设置，见 psf.strehl_huygens）。

        注意采样敏感性：Codex 的单次记录是 32×32 与 128×128 最多差 0.0017（unverified，
        需要重测）。比对时必须两边同采样。
        """
        from .psf import strehl_huygens

        return strehl_huygens(self.system, n=n, image_pixels=image_pixels,
                              reference=reference, kind=kind)

    def rms_wavefront(self, n: int = 32, kind: str = "square") -> float:
        """波前 RMS（waves，已去 piston 与倾斜）。"""
        from .psf import rms_wavefront

        return rms_wavefront(self.system, n=n, kind=kind)

    # ---- 厚度 ----
    def ctgt(self, s1: int, s2: int) -> float:
        """Surf1 到 Surf2 的中心厚度之和（含 Surf1、不含 Surf2 之后的厚度）。"""
        total = 0.0
        for it in self.system.items:
            if s1 <= it.index < s2:
                total += it.thickness
        return total

    def etgt(self, s1: int, s2: int) -> float:
        """边缘厚度 = 中心厚度 + sag2(r) − sag1(r)，r 取两面通光半口径的较小者。"""
        r = min(
            next(it for it in self.system.items if it.index == s1).semi,
            next(it for it in self.system.items if it.index == s2).semi,
        )
        return self.ctgt(s1, s2) + float(self.ssag_at(s2, r)) - float(self.ssag_at(s1, r))

    def ssag_at(self, surf: int, r):
        it = next(it for it in self.system.items if it.index == surf)
        z, _, _ = it.shape.sag(np.asarray(r, float), np.zeros_like(np.asarray(r, float)))
        return z

    def _ring(self, surf: int, n: int = 64, frac: float = 1.0):
        """口径内一圈采样点（相对半口径的比例 frac）。"""
        it = next(it for it in self.system.items if it.index == surf)
        th = np.linspace(0.0, 2 * np.pi, n, endpoint=False)
        r = frac * it.semi
        return r * np.cos(th), r * np.sin(th)

    def _thickness_map(self, s1: int, s2: int, n: int = 64):
        """t + sag2 − sag1，在两面口径较小者的圆周上逐点求值。"""
        r = min(
            next(it for it in self.system.items if it.index == s1).semi,
            next(it for it in self.system.items if it.index == s2).semi,
        )
        th = np.linspace(0.0, 2 * np.pi, n, endpoint=False)
        x, y = r * np.cos(th), r * np.sin(th)
        a = next(it for it in self.system.items if it.index == s1).shape.sag(x, y)[0]
        b = next(it for it in self.system.items if it.index == s2).shape.sag(x, y)[0]
        return self.ctgt(s1, s2) + np.asarray(b, float) - np.asarray(a, float)

    def mnet(self, s1: int, s2: int, samples: int = 64) -> float:
        """口径内最小边缘厚度（逐点求 t + sag2 − sag1 后取极小）。"""
        return float(self._thickness_map(s1, s2, samples).min())

    def mnct(self, s1: int, s2: int) -> float:
        """最小中心厚度（中心处就是 CTGT）。"""
        return self.ctgt(s1, s2)

    def mnca(self, s1: int, s2: int, samples: int = 64) -> float:
        """口径内最小厚度/间隙（与 MNET 同式，用于空气间隔）。"""
        return float(self._thickness_map(s1, s2, samples).min())
