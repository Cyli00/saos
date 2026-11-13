"""逐面三阶（Seidel）像差与初级色差。

定义按 Welford《Aberrations of Optical Systems》第 8 章，量纲是镜头单位（波像差的系数）：
近轴边缘光线 (h, u)、近轴主光线 (h̄, ū)，面前后折射率 n、n′，顶点曲率 c，

    A = n(hc + u)，Ā = n(h̄c + ū)，H = n(u·h̄ − ū·h)（拉氏不变量），Δ(x) = x′ − x
    S1 = −A²·h·Δ(u/n)                 球差
    S2 = −A·Ā·h·Δ(u/n)                彗差
    S3 = −Ā²·h·Δ(u/n)                 像散
    S4 = −H²·c·Δ(1/n)                 Petzval 场曲
    S5 = −Ā²·h̄·Δ(u/n) + Ā·H·Δ(ū/n)   畸变（(Ā/A)(S3 + S4) 的无奇点写法）
    CL = A·h·Δ(δn/n)，CT = Ā·h·Δ(δn/n) 轴向、垂轴色差，δn = n(短波) − n(长波)

非球面只多出四次项：矢高 z = (c/2)r² + q·r⁴ + …，与同顶点曲率球面的差 G = q − c³/8，
δS1 = 8G·(n′ − n)·h⁴，δS2、δS3、δS5 依次乘 h̄/h，δS4 = 0。

换成波数（OpticStudio 的 SPHA、COMA、ASTI、FCUR、逐面 DIST 以及 Seidel 表的 W020、W111）：
W040 = S1/8λ，W131 = S2/2λ，W222 = S3/2λ，W220P = S4/4λ，W311 = S5/2λ，W020 = CL/2λ，W111 = CT/λ。
DIST 的 Int1=0 不是 W311 之和，而是最大视场的畸变百分比（2026-11-10 与 OpticStudio 实测：-0.020651% 对 0.153820 波）。

只做同轴的 Standard 与 Even Asphere 折射面；反射面、理想透镜和非旋转对称面型拒绝，不给近似值。
2026-11-10 真机：逐面 W040…W311 与 OpticStudio 的 SPHA…DIST 操作数一致（1.3e-15 波）；它的 Seidel 表
W311 与 DIST 操作数本身差到 3e-3 波（W040 等四列只差 6e-7）。tests/rtc/test_seidel.py 用实光线的横向像差核对各项之和。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .system import OpticalSystem, paraxial_rays

COLUMNS = ("S1", "S2", "S3", "S4", "S5", "CL", "CT")
#: 波数列名与换算：W = S / (除数 · λ)
WAVES = {"W040": ("S1", 8.0), "W131": ("S2", 2.0), "W222": ("S3", 2.0), "W220P": ("S4", 4.0), "W311": ("S5", 2.0),
         "W020": ("CL", 2.0), "W111": ("CT", 1.0)}
SUPPORTED = ("STANDARD", "EVENASPH")


@dataclass
class Seidel:
    """一个波长下的逐面系数。rows[k] 对应 surfaces[k]（OpticStudio 的面号），列序同 COLUMNS，单位是镜头单位。"""

    surfaces: list[int]
    rows: np.ndarray
    wavelength: float      # µm
    invariant: float       # 拉氏不变量 H
    marginal: list[tuple]  # 每面 (h, u, u′)
    chief: list[tuple]     # 每面 (h̄, ū, ū′)

    def totals(self) -> np.ndarray:
        return self.rows.sum(axis=0)

    def waves(self) -> dict[str, np.ndarray]:
        lam = self.wavelength * 1e-3
        return {name: self.rows[:, COLUMNS.index(column)] / (divisor * lam) for name, (column, divisor) in WAVES.items()}


def _check(system: OpticalSystem) -> None:
    bad = [f"{it.index} ({'mirror' if it.reflect else it.shape.kind})" for it in system.items[:-1]
           if it.reflect or it.shape.kind not in SUPPORTED]
    if bad:
        raise NotImplementedError(f"第 {', '.join(bad)} 面：三阶像差只做同轴的 Standard 与 Even Asphere 折射面")


def max_field(system: OpticalSystem) -> float:
    """最大径向视场（视场单位）。"""
    return max((float(np.hypot(x, y)) for x, y, _ in system.lens.fields), default=0.0)


def launch(system: OpticalSystem, hy: float = 1.0) -> tuple[tuple[float, float], tuple[float, float]]:
    """第 1 面顶点处的近轴边缘光线与主光线 (高度, 斜率)：边缘光线从轴上物点到入瞳边缘，主光线从 ``hy`` 倍最大视场
    过入瞳中心。``hy`` 是归一化视场，角度视场按 ``tan(hy × 最大视场角)`` 取角，不能把最大视场的正切乘 ``hy``；
    两者在 0.6 × 5° 上差 8.6e-3 mm（2026-11-10 与 OpticStudio 的 PARY 实测）。
    入瞳的位置和半径按主波长定，所有波长共用（同 OpticalSystem.pupil_reference）。"""
    z_ep, radius = system.pupil_reference()
    if not np.isfinite(z_ep):
        raise NotImplementedError("入瞳在无穷远（物方远心）：近轴主光线没有按视场角的定义")
    field = max_field(system) * float(hy)
    if not system.finite_object:
        if system.lens.field_type != "ANGLE":
            raise NotImplementedError("无穷远物体的三阶像差只支持角度视场")
        slope = float(np.tan(np.radians(field)))
        return (float(radius), 0.0), (-slope * z_ep, slope)
    span = z_ep + system.object_thickness
    if abs(span) < 1e-12:
        raise NotImplementedError("物面与入瞳重合：近轴光线没有定义")
    u = float(radius) / span
    if system.lens.field_type == "OP1":
        height = field
    elif system.lens.field_type == "ANGLE":
        height = -float(np.tan(np.radians(field))) * span
    else:
        raise NotImplementedError(f"视场类型 {system.lens.field_type} 未实现（三阶像差只支持角度与物高视场）")
    ubar = -height / span
    return (u * system.object_thickness, u), (height + ubar * system.object_thickness, ubar)


def paraxial(system: OpticalSystem, hy: float = 1.0) -> tuple[list[tuple], list[tuple]]:
    """``hy`` 倍最大视场下近轴边缘光线与主光线的逐面 (高度, 面前斜率, 面后斜率)，含像面。"""
    (h, u), (hbar, ubar) = launch(system, hy)
    return paraxial_rays(system.items, h, u, system.n_before(0)), paraxial_rays(system.items, hbar, ubar, system.n_before(0))


def _quartic_departure(it) -> float:
    """矢高四次项与同顶点曲率球面的差 G。Even Asphere 的 α₁r² 已进顶点曲率，圆锥系数作用在基底曲率上。"""
    surface = it.surface
    c0 = float(surface.curvature)
    cv = float(it.shape.vertex_curvature("y"))
    alpha2 = float(surface.params.get(2, 0.0)) if it.shape.kind == "EVENASPH" else 0.0
    return (1.0 + float(surface.conic)) * c0 ** 3 / 8.0 + alpha2 - cv ** 3 / 8.0


def seidel(system: OpticalSystem, short: OpticalSystem | None = None, long: OpticalSystem | None = None) -> Seidel:
    """system 所在波长下的逐面 Seidel 系数。给出 short、long（同一镜头在最短、最长波长下的系统）时填 CL、CT，
    否则这两列为 0。"""
    _check(system)
    marginal, chief = paraxial(system)
    n0 = system.n_before(0)
    invariant = n0 * (marginal[0][1] * chief[0][0] - chief[0][1] * marginal[0][0])
    rows, surfaces = [], []
    for k, it in enumerate(system.items[:-1]):
        n1, n2 = system.n_before(k), it.n_after
        c = float(it.shape.vertex_curvature("y"))
        h, u, ua = marginal[k]
        hb, ub, uba = chief[k]
        a, ab = n1 * (h * c + u), n1 * (hb * c + ub)
        d_un, d_ubn, d_n = ua / n2 - u / n1, uba / n2 - ub / n1, 1.0 / n2 - 1.0 / n1
        s1 = -a * a * h * d_un
        s2 = -a * ab * h * d_un
        s3 = -ab * ab * h * d_un
        s4 = -invariant * invariant * c * d_n
        s5 = -ab * ab * hb * d_un + ab * invariant * d_ubn
        asphere = 8.0 * _quartic_departure(it) * (n2 - n1)
        s1 += asphere * h ** 4
        s2 += asphere * h ** 3 * hb
        s3 += asphere * h ** 2 * hb ** 2
        s5 += asphere * h * hb ** 3
        cl = ct = 0.0
        if short is not None and long is not None:
            d1 = short.n_before(k) - long.n_before(k)
            d2 = short.items[k].n_after - long.items[k].n_after
            d_dn = d2 / n2 - d1 / n1
            cl, ct = a * h * d_dn, ab * h * d_dn
        rows.append((s1, s2, s3, s4, s5, cl, ct))
        surfaces.append(it.index)
    return Seidel(surfaces, np.asarray(rows, dtype=float).reshape(len(rows), len(COLUMNS)), float(system.wavelength),
                  float(invariant), marginal, chief)
