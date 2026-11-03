"""面型矢高 z(x, y) 与梯度 ∇z。

**注意**：追迹内核用的是 :mod:`rtc.geometry`（支持 Dual、带衍射相位与精确梯度），
这个模块只保留最初的标量实现供 `tests/test_surface.py` 的 SSAG 回归使用，
两者不共享代码。新增面型应当只改 `geometry.py`。

参数编号严格按 OpticStudio 手册的参数表（见 docs/plans/rtc/SURFACE-PARAMS.md 的逐项对照）。

每个面型函数返回 ``(z, dzdx, dzdy)``，输入已用 numpy 广播（float64）。
``VERIFIED`` 记录哪些面型已经有 OpticStudio 实测（SSAG）对照；不在集合里的只算
"按手册实现"，交付前必须补 oracle 回归。
"""

from __future__ import annotations

import numpy as np

from .zmx import Surface

#: 已经有 SSAG 实测对照的面型 / 已经过三件镜头人工核对的路径
VERIFIED: set[str] = {"STANDARD"}


def conic_sag(c: float, k: float, x, y):
    """旋转对称二次曲面：z = cρ²/(1+√(1−(1+k)c²ρ²))，返回 z 与 ∇z。

    手册式；ORACLE.md 记录的 SSAG 实测（R=50, h=1 与 h=6）在 1e-15 内一致。
    """
    r2 = x * x + y * y
    root = np.sqrt(np.maximum(1.0 - (1.0 + k) * c * c * r2, 0.0))
    den = 1.0 + root
    z = c * r2 / den
    g = np.where(root > 0.0, c / np.where(root > 0.0, root, 1.0), 0.0)
    return z, g * x, g * y


def _poly_sum(coefs: dict[int, float], x, y, even: bool = True):
    """Σ a_i ρ^(2i)（even）或 Σ a_i y^i（非 even 由调用方处理）。"""
    if not coefs:
        return 0.0, 0.0, 0.0
    r2 = x * x + y * y
    z = np.zeros_like(r2) if np.ndim(r2) else 0.0
    dzdr_over_r = np.zeros_like(z) if np.ndim(z) else 0.0
    for i, a in coefs.items():
        p = 2 * i
        z = z + a * r2**i
        dzdr_over_r = dzdr_over_r + p * a * r2 ** (i - 1)
    return z, dzdr_over_r * x, dzdr_over_r * y


def sag(surface: Surface, x, y):
    """按面型返回 (z, dzdx, dzdy)；未实现的面型直接报错，不返回近似值。"""
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    t = surface.type.upper()
    c = surface.curvature
    k = surface.conic

    if t in ("STANDARD", "STANDARDLENS"):
        return conic_sag(c, k, x, y)

    if t in ("EVENASPH", "EVENASPHRIC"):
        # 参数 1..8 = α2..α16，作用在 ρ²、ρ⁴ … ρ¹⁶ 上，无归一化半径（手册 + SSAG 差分实测）
        z0, dzdx, dzdy = conic_sag(c, k, x, y)
        coefs = {i: surface.params[i] for i in range(1, 9) if surface.params.get(i)}
        zp, dzpx, dzpy = _poly_sum(coefs, x, y)
        return z0 + zp, dzdx + dzpx, dzdy + dzpy

    if t in ("BICONICX", "BICONIC"):
        # LDE 半径/圆锥 = Y 方向；参数 1 = Rx，参数 2 = kx（手册 Biconic 参数表 + 任务脚本
        # set_biconic() 的用法：Radius=Ry, Conic=Ky, Par1=Rx, Par2=Kx）
        cx = 0.0 if surface.params.get(1, 0.0) == 0.0 else 1.0 / surface.params[1]
        kx = surface.params.get(2, 0.0)
        s2 = 1.0 - (1.0 + kx) * cx * cx * x * x - (1.0 + k) * c * c * y * y
        root = np.sqrt(np.maximum(s2, 0.0))
        z = (cx * x * x + c * y * y) / (1.0 + root)
        dzdx = np.where(root > 0.0, cx * x / np.where(root > 0.0, root, 1.0), 0.0)
        dzdy = np.where(root > 0.0, c * y / np.where(root > 0.0, root, 1.0), 0.0)
        return z, dzdx, dzdy

    if t == "BICONICZ":
        # 参数 0 = 外推标志，1 = Rx，2 = kx，3 = Zernike 项数，4 = 归一化半径，
        # 13..28 = x¹..x¹⁶ 的系数，29..44 = y¹..y¹⁶ 的系数，45.. = Zernike Standard
        z0, dzdx, dzdy = sag(Surface(index=surface.index, type="BICONICX", curvature=c, conic=k, params=surface.params), x, y)
        zx = zy = 0.0
        dx = dy = 0.0
        for i in range(1, 17):
            a = surface.params.get(12 + i, 0.0)
            if a:
                zx = zx + a * x**i
                dx = dx + i * a * x ** (i - 1)
            b = surface.params.get(28 + i, 0.0)
            if b:
                zy = zy + b * y**i
                dy = dy + i * b * y ** (i - 1)
        n_zern = int(surface.params.get(3, 0.0))
        if n_zern:
            raise NotImplementedError(
                "BICONICZ 的 Zernike 项尚未实现：三件最终镜头的参数 3 都是 0，"
                "需要 oracle 标定 Zernike Standard 项序后才实现"
            )
        return z0 + zx + zy, dzdx + dx, dzdy + dy

    if t == "TOROIDAL":
        # 参数 0 = 外推标志，1 = 旋转半径，2..8 = y²..y¹⁴ 的系数，
        # 13 = Zernike 项数，14 = 归一化半径，15.. = Zernike Standard
        if surface.params.get(1, 0.0) != 0.0:
            raise NotImplementedError(
                "TOROIDAL 的有限旋转半径路径未标定：三件镜头的参数 1 都是 0（圆柱面），"
                "有限半径需要 SSAG 实测确认符号与分支"
            )
        if int(surface.params.get(13, 0.0)):
            raise NotImplementedError("TOROIDAL 的 Zernike 项尚未实现（三件镜头均为 0 项）")
        yc = y
        r2 = yc * yc
        root = np.sqrt(np.maximum(1.0 - (1.0 + k) * c * c * r2, 0.0))
        z = c * r2 / (1.0 + root)
        g = np.where(root > 0.0, c / np.where(root > 0.0, root, 1.0), 0.0)
        dzdy = g * yc
        for i in range(1, 8):  # 参数 2..8 → y²,y⁴,…,y¹⁴
            a = surface.params.get(1 + i, 0.0)
            if a:
                z = z + a * yc ** (2 * i)
                dzdy = dzdy + 2 * i * a * yc ** (2 * i - 1)
        return z, np.zeros_like(z), dzdy

    if t == "PARAXIAL":
        # 面形是平面；光学作用见 refraction（u' = u − y/f），OPD 由参数 2 的 OPD Mode 决定
        zero = np.zeros_like(x + y)
        return zero, zero, zero

    raise NotImplementedError(f"面型 {surface.type} 不在复现范围内")
