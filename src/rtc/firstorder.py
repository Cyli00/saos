"""一阶量与系统级操作数，按 OpticStudio 2024 R1 手册的定义实现。

全部 unverified：定义取自手册原文（各函数说明里注明章节），尚未与 OpticStudio 逐项对账。
调用方（saos 的 rtc 实例）把结果标为 rtc 计算、未准入。
"""

from __future__ import annotations

import numpy as np

from .system import OpticalSystem

# 近轴附近光线的起始高度相对入瞳直径的比例；太大时混入像差，太小时斜率的舍入误差变大
PARABASAL = 1e-4
# 手册 "Working F/#"：算不准时封顶 10,000
FNUM_CAP = 10000.0


def _image_index(system: OpticalSystem) -> float:
    """像面前那段介质的折射率（像方介质）。"""
    return system.n_before(len(system.items) - 1)


def entrance_pupil_diameter(system: OpticalSystem) -> float:
    """EPDI：近轴入瞳直径。"""
    return 2.0 * float(system.pupil_radius())


def entrance_pupil_position(system: OpticalSystem) -> float:
    """ENPP：近轴入瞳相对第 1 面的位置。"""
    return float(system.entrance_pupil()[0])


def effl(system: OpticalSystem) -> float:
    """EFFL。手册 §14.10：从无穷远追一条轴上的近轴附近光线（parabasal ray），用它在像方的边缘光线角求后主面，
    EFL = f′/n′（f′ 是像方焦距）。用实光线而不是 y-nu 近轴追迹，偶次非球面的 r² 项、双锥面的 y 截面都自动计入。"""
    from .trace import _trace_block

    epd = entrance_pupil_diameter(system)
    h = PARABASAL * (epd if np.isfinite(epd) and epd > 0 else 1.0)
    start = (np.array([0.0]), np.array([h]), np.array([0.0]), np.array([[0.0, 0.0, 1.0]]))
    block = _trace_block(system, [0.0], [0.0], start=start)
    if block.error[0] != 0:
        raise ValueError(f"近轴附近光线在第 {abs(int(block.error[0]))} 面丢失，EFL 无定义")
    # 像方方向取像面前最后一面折射之后（像面本身若是弯的或换了介质，不应改变像方光线）
    m, n = float(block.m[0, -2]), float(block.n[0, -2])
    if m == 0.0:
        return float("inf")
    # RayBlock 的方向余弦乘过反射符号 σ，坐标系因此是左手的：把 m 乘回 σ 才是沿传播方向的角度。
    # 单凹面镜 R = −800 时给 +400（= |R|/2），两次反射的系统不受影响。
    sigma = float(system.items[-2].sign)
    return -h * abs(n) * sigma / m / abs(_image_index(system))


def isfn(system: OpticalSystem) -> float:
    """ISFN。手册 §"Image space F/#"：无穷共轭的近轴 EFL 除以近轴入瞳直径。"""
    return effl(system) / entrance_pupil_diameter(system)


def wfno(system: OpticalSystem) -> float:
    """WFNO。手册 "Working F/#"：1/(2n·sinθ)，θ 是像方实际边缘光线角，n 是像方折射率；离轴或非轴对称时对轴向光线与
    上、下、左、右四条边缘光线之间的 NA² 取平均再换回 F/#，结果超过 10,000 时封顶。这里总取四条的平均（轴对称时四条相同）。
    手册还说边缘光线追不过时改用较小光瞳再外推，这里未实现，直接报错。"""
    from .trace import trace

    block = trace(system, [0.0, 0.0, 0.0, 1.0, -1.0], [0.0, 1.0, -1.0, 0.0, 0.0])
    if np.any(block.error != 0):
        raise ValueError("边缘光线追不到像面，WFNO 的缩小光瞳外推未实现")
    d = np.stack([block.l[:, -2], block.m[:, -2], block.n[:, -2]], axis=1)
    sin = np.linalg.norm(np.cross(d[1:], d[0]), axis=1)
    na2 = float(np.mean((abs(_image_index(system)) * sin) ** 2))
    if na2 == 0.0:
        return FNUM_CAP
    return min(1.0 / (2.0 * np.sqrt(na2)), FNUM_CAP)


def totr(system: OpticalSystem) -> float:
    """TOTR。手册 §"Total track"：从第 1 面到像面各面顶点的最大 z 减最小 z（忽略坐标旋转）。"""
    z = [it.vertex_z for it in system.items]
    return float(max(z) - min(z))


def tthi(lens, first: int, last: int, config: int = 1) -> float:
    """TTHI：第 first 到第 last 面的厚度之和（含两端）。"""
    lo, hi = sorted((int(first), int(last)))
    return float(sum(lens.thickness(i, config) for i in range(lo, hi + 1)))


def petzval_radius(system: OpticalSystem) -> float:
    """PETZ。手册：指定波长下的 Petzval 曲率半径，非近轴系统无效。按 Σφ/(n·n′) 求和（φ = c·(n′ − n)，理想透镜 φ = 1/f，
    曲率取 y 截面的近轴曲率），半径 = −1/(n_像·Σ)。

    n、n′ 用带符号的折射率：反射面 n′ = −n，于是 φ = −2nc、n·n′ = −n²，两面都进 Σ；像方折射率取绝对值，
    因为反射系统的符号只是记账（单凹面镜 R = −800 给 PETZ = +400，与 f = |R|/2 一致）。
    """
    total = 0.0
    for k, it in enumerate(system.items[:-1]):
        n1, n2 = system.n_before(k), it.n_after
        if it.type == "PARAXIAL" and it.focal:
            power = 1.0 / it.focal
        else:
            power = system.curvature_axis(it, "y") * (n2 - n1)
        total += power / (n1 * n2)
    if total == 0.0:
        return float("inf")
    return -1.0 / (abs(_image_index(system)) * total)
