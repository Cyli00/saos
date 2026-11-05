"""把 ``.zmx`` 变成可追迹的系统：材料、厚度、光阑、孔径、近轴量。"""

from __future__ import annotations

import copy
from dataclasses import dataclass

import numpy as np

from . import geometry, materials, zmx
from .material import MaterialUnsupported, Medium, resolve
from .material.model import model_index

_CATALOG: materials.Catalog | None = None


def catalog() -> materials.Catalog:
    global _CATALOG
    if _CATALOG is None:
        _CATALOG = materials.Catalog()
    return _CATALOG


def model_glass_index(nd: float, vd: float, wavelength_um: float, dpgf: float = 0.0) -> float:
    """兼容旧入口；模型玻璃不再依赖有限查表。"""
    return float(model_index(wavelength_um, nd, vd, dpgf))


@dataclass
class Item:
    """一个面 + 它后面那段介质的追迹信息。"""

    index: int
    surface: zmx.Surface
    shape: geometry.Shape
    thickness: float          # 到下一面的轴向距离
    n_after: float            # 面后介质在working wavelength 下的折射率（反射之后带负号，见 OpticalSystem）
    semi: float
    is_stop: bool
    is_image: bool
    vertex_z: float
    focal: float | None = None  # PARAXIAL 面的焦距
    medium: Medium | None = None
    #: 反射面：方向按 d − 2(d·N)N 更新，面后介质的折射率带负号（OpticStudio 序列模式约定）
    reflect: bool = False
    #: 这一面之后的方向余弦符号 σ（反射一次翻一次）。追迹内部用几何方向，对外报告按 σ 乘回去，
    #: 与 OpticStudio 批量追迹一致：光线穿过奇数个反射面之后，它报的方向余弦仍是 σ·d。
    sign: float = 1.0

    @property
    def type(self) -> str:
        return self.surface.type.upper()


def paraxial_rays(items, y, u, n: float = 1.0, axis: str = "y"):
    """逐面近轴追迹，返回 [(y_at, u_before, u_after)]，y_at 是该面顶点处的光线高度。float 和 Dual 都可以。

    在光轴上它就是实光线的切向（iris、reflective、sequential 实测差 ≤ 4.4e-16）；单独保留是因为光瞳对设计变量的导数要二阶。
    """
    rows = []
    for k, it in enumerate(items):
        if k > 0:
            y = y + u * items[k - 1].thickness
        if it.type == "PARAXIAL" and it.focal:
            # 与实际追迹同一规则 n'u' = n·u − y/f，之后换成面后折射率（面后是玻璃时出瞳才对）
            ua = (n * u - y / it.focal) / it.n_after
        else:
            ua = (n * u - y * it.shape.vertex_curvature(axis) * (it.n_after - n)) / it.n_after
        n = it.n_after
        rows.append((y, u, ua))
        u = ua
    return rows


def stop_transfer(items, stop: int, axis: str = "y"):
    """第 1 面到光阑面的近轴传递系数 (A, B)：y_stop = A·y1 + B·u1。入瞳、Float By Stop Size、OBNA 都由它们定义。"""
    return paraxial_rays(items, 1.0, 0.0, axis=axis)[stop][0], paraxial_rays(items, 0.0, 1.0, axis=axis)[stop][0]


def na_pupil_radius(z_ep, object_distance: float, na: float):
    """OBNA 的入瞳半径：物点到入瞳的距离 × tanθ，物方 NA = sinθ（手册 §14.32，物方 n = 1）。"""
    return abs(z_ep + object_distance) * (na / np.sqrt(max(1e-300, 1.0 - na * na)))


class OpticalSystem:
    """一个结构、一个波长下的可追迹系统。

    ``media``：调用方持有的介质缓存，同一镜头环境与玻璃库下跨多次重建复用，
    改参数后重建系统时不必重算折射率。``primary``：同一结构主波长下的系统，非主波长系统借它取光阑半径。
    """

    def __init__(self, lens: zmx.Lens, wavelength_um: float | None = None, config: int | None = None,
                 aim_scale: tuple[float, float] | None = None, glasses: materials.Catalog | None = None,
                 media: dict | None = None, primary: "OpticalSystem | None" = None, allow_unverified: bool = False):
        self.lens = lens
        # 只按手册定义实现、尚未与 OpticStudio 逐条对账的路径（有限物距发射、物高视场、按实光线定的 OBNA 光阑半径）
        # 默认拒绝；调用方显式打开时照算，结果须标 unverified
        self.allow_unverified = bool(allow_unverified)
        if any(row.op in ("TEMP", "PRES") for row in lens.mce):
            raise MaterialUnsupported("多重结构 TEMP/PRES 的逐面环境尚未接入系统")
        # 调用方可以给自己的玻璃库（例如 OpticStudio 实际使用的 Glasscat 目录）；默认用进程内共享的那份
        self.glasses = glasses if glasses is not None else catalog()
        # 默认用文件保存时选中的结构（MNUM 第 2 个数）。codex-r017 保存时停在第 5 个结构，
        # 拿结构 1 去比会差 2e-2 mm（docs/plans/rtc/HANDOFF.md §6.4）。
        self.config = lens.current_config if config is None else config
        self.aim_scale = aim_scale
        self.wavelength = wavelength_um if wavelength_um is not None else lens.wavelengths[lens.primary_wave - 1][0]
        self.primary = primary
        self._media = media if media is not None else {}
        self.items: list[Item] = []
        self.object_thickness = lens.surface(0).thickness
        z = 0.0
        image_index = max(s.index for s in lens.surfaces)
        # 带符号的折射率：每过一次反射面符号翻一次（OpticStudio 的序列模式约定，反射面之后介质不变）。
        # 这样同一套 Snell 与近轴公式在 n' = −n 时正好给出反射，光程按 |n|·路程累计。
        sigma, material_before = 1.0, 1.0
        for s in sorted(lens.surfaces, key=lambda s: s.index):
            if s.index == 0:
                continue
            medium, n_material = self._medium(s.material)
            reflect = bool(getattr(medium, "reflect", False))
            if reflect:
                sigma = -sigma
                n_after = sigma * material_before
            else:
                n_after = sigma * n_material
                material_before = n_material
            focal = s.params.get(1) if s.type.upper() == "PARAXIAL" else None
            item = Item(
                index=s.index,
                surface=s,
                shape=geometry.make(s),
                thickness=lens.thickness(s.index, self.config),
                n_after=n_after,
                semi=s.semi_diameter,
                is_stop=s.is_stop,
                is_image=s.index == image_index,
                vertex_z=z,
                focal=focal,
                medium=medium,
                reflect=reflect,
                sign=sigma,
            )
            self.items.append(item)
            if np.isfinite(item.thickness):
                z += item.thickness
        self.image_z = z
        self.stop = next((i for i, it in enumerate(self.items) if it.is_stop), None)

    # ---- 材料 ----
    def _medium(self, spec) -> tuple[Medium, float]:
        """面后介质与它在本波长下的折射率；同一 (材料, 波长) 只算一次。"""
        from .material import Model

        env = self.lens.environment
        key = (spec, tuple(self.lens.catalogs), env)
        # 模型玻璃的连续参数每步都变，跨系统缓存只保留目录材料。
        cacheable = not isinstance(spec, Model)
        entry = self._media.get(key) if cacheable else None
        if entry is None:
            entry = (resolve(spec, self.glasses, self.lens.catalogs), {})
            if cacheable:
                self._media[key] = entry
        medium, values = entry
        n = values.get(self.wavelength)
        if n is None:
            n = values[self.wavelength] = float(medium.index(self.wavelength, env))
        return medium, n

    def _index_of(self, s: zmx.Surface) -> float:
        return self._medium(s.material)[1]

    def packed(self):
        """编译内核的面数据，首次调用时排好。"""
        if "_packed" not in self.__dict__:
            self._packed = geometry.pack(self)
        return self._packed

    def invalidate(self) -> None:
        """改过 items 之后调用：丢掉由面数据导出的缓存（内核数据、入瞳、光阑半径、Float By Stop Size 的入瞳半径）。"""
        for name in ("_packed", "_entrance_pupil", "_stop_radius", "_float_pupil_radius"):
            self.__dict__.pop(name, None)
        self.primary = None

    def primary_system(self) -> "OpticalSystem":
        """当前面数据在主波长下的系统；直接改 items 的设计副本也必须同步。"""
        wavelength = self.lens.active_wavelengths[self.lens.primary_wave - 1][0]
        if wavelength == self.wavelength:
            return self
        if self.primary is None:
            lens = copy.deepcopy(self.lens)
            for it in self.items:
                surface = copy.deepcopy(it.surface)
                surface.thickness, surface.semi_diameter = it.thickness, it.semi
                lens.surfaces[lens.surfaces.index(lens.surface(it.index))] = surface
            for row in lens.mce:
                if row.op == "THIC" and row.config == self.config:
                    row.value = lens.surface(row.surface).thickness
            self.primary = OpticalSystem(lens, wavelength_um=wavelength, config=self.config,
                                         aim_scale=self.aim_scale, glasses=self.glasses,
                                         media=self._media, allow_unverified=self.allow_unverified)
        return self.primary

    def n_before(self, k: int) -> float:
        return 1.0 if k == 0 else self.items[k - 1].n_after

    # ---- 视场 ----
    @property
    def finite_object(self) -> bool:
        return bool(np.isfinite(self.object_thickness))

    def require_verified(self, what: str) -> None:
        """只按手册定义实现的路径：allow_unverified 为假时报 NotImplementedError，不给近似值。"""
        if not self.allow_unverified:
            raise NotImplementedError(f"{what} 只按 OpticStudio 手册定义实现，尚未与 OpticStudio 对账；"
                                      "需要时以 allow_unverified=True 构造系统，结果须标 unverified")

    def _field_scale(self) -> tuple[float, float]:
        fx = [f[0] for f in self.lens.fields] or [0.0]
        fy = [f[1] for f in self.lens.fields] or [0.0]
        if self.lens.field_normalization == "rectangular":
            return max(abs(v) for v in fx), max(abs(v) for v in fy)
        r = max(float(np.hypot(a, b)) for a, b in zip(fx, fy))
        return r, r

    def object_point(self, hx, hy):
        """有限物距：归一化视场 → 物面上的点 (x, y)；物面在第 1 面顶点之前 object_thickness 处。

        物高视场（FTYP 1）按 Hx、Hy 的归一化直接缩放。角度视场按手册"瞄准关时光线射向近轴入瞳"：
        主光线从物点射向入瞳中心、与 z 轴夹角为视场角，所以物点 = −tanθ·(z_ep + 物距)。两者都未与 OpticStudio 对账。
        """
        hx = np.asarray(hx, dtype=float)
        hy = np.asarray(hy, dtype=float)
        sx, sy = self._field_scale()
        if self.lens.field_type == "OP1":
            return hx * sx, hy * sy
        if self.lens.field_type != "ANGLE":
            raise NotImplementedError(f"视场类型 {self.lens.field_type} 未实现（有限物距只支持角度与物高视场）")
        z_ep, _ = self.entrance_pupil()
        if not np.isfinite(z_ep):
            raise NotImplementedError("入瞳在无穷远（物方远心）时角度视场没有物点定义")
        span = z_ep + self.object_thickness
        return -np.tan(np.radians(hx * sx)) * span, -np.tan(np.radians(hy * sy)) * span

    def field_slopes(self, hx, hy):
        """归一化视场 (hx, hy) → 物方主方向的斜率 (tan θx, tan θy)，可广播。

        只支持角度视场（``FTYP`` 第 1 列 = 0）。径向归一化按最大视场半径缩放两个分量，
        矩形归一化按 x、y 各自的最大值缩放（与 OpticStudio 的 Hx/Hy 定义一致）。
        """
        if self.lens.field_type != "ANGLE":
            raise NotImplementedError(f"视场类型 {self.lens.field_type} 未实现（只支持角度视场）")
        sx, sy = self._field_scale()
        return np.tan(np.radians(np.asarray(hx, dtype=float) * sx)), np.tan(np.radians(np.asarray(hy, dtype=float) * sy))

    # ---- 近轴 ----
    def _paraxial(self, y: float, u: float, k0: int = 0, n: float | None = None, axis: str = "y"):
        """逐面近轴追迹，见 :func:`paraxial_rays`。"""
        return paraxial_rays(self.items[k0:], y, u, self.n_before(k0) if n is None else n, axis)

    def aim_target(self, px, py):
        """光阑面上的瞄准目标点（毫米）。

        EPD：目标是 (px,py)×光阑半径。光阑半径取主波长的实际边缘光线（轴上、平行入射、高度 EPD/2）在光阑面的
        高度，所有波长共用（手册：光阑半径由主波长边缘光线追迹确定）。不读 LDE 的半口径：改过参数它就过时。
        2026-10-26 在光阑位于镜后的 4 面双胶合上与 OpticStudio 24.1 对照：它瞄准的光阑交点比这个半径短 2.8e-9（相对），
        是它自己的瞄准收敛误差；近轴高度 |A|·EPD/2 则差 1e-3 量级，不是它的定义。
        OBNA：目标由近轴传递 × 物方 NA 给出，各方向分别算；``aim_scale`` 可用实测值覆盖
        （Zemax 的 OBNA 光阑半径定义没有公开到可以纯推导的程度，见 GAP-ANALYSIS）。
        """
        px = np.asarray(px, dtype=float)
        py = np.asarray(py, dtype=float)
        if self.aim_scale is not None:
            rx, ry = self.aim_scale
            return px * rx, py * ry
        if self.stop is None:
            return px, py
        if self.lens.aperture_type == "FLOA":
            # Float By Stop Size：瞄准目标就是光阑的半口径本身，不经近轴换算（手册 §2.1.1.1）
            semi = self.stop_semi_diameter()
            return px * semi, py * semi
        if self.lens.aperture_type == "EPD" or self.allow_unverified:
            semi = self.stop_radius()
            return px * semi, py * semi
        na = self.lens.aperture_value
        bx = self.pupil_transfer("x")
        by = self.pupil_transfer("y")
        return -px * bx * na, -py * by * na

    def stop_radius(self) -> float:
        """EPD 系统的光阑半径：主波长的轴上实际边缘光线在光阑面的高度（见 :meth:`aim_target`），算一次后缓存。"""
        if getattr(self, "_stop_radius", None) is None:
            from .trace import stop_hits

            primary = self.lens.active_wavelengths[self.lens.primary_wave - 1][0]
            if primary != self.wavelength:
                ref = self.primary_system()
                self._stop_radius = ref.stop_radius()
                return self._stop_radius
            if self.finite_object or self.lens.ray_aiming == 1:
                self.require_verified("有限物距或近轴瞄准的光阑半径")
                self._stop_radius = self._marginal_stop_height()
                return self._stop_radius
            ref = self
            h = self.lens.aperture_value / 2.0
            _, y = stop_hits(ref, np.array([0.0]), np.array([h]), np.array([0.0]), np.array([[0.0, 0.0, 1.0]]))
            self._stop_radius = abs(float(y[0]))
        return self._stop_radius

    def _marginal_stop_height(self) -> float:
        """手册 §2.1.1.7：光阑半径由主波长下从物面中心出发的边缘光线追到光阑面确定；近轴瞄准用近轴光线，实际瞄准用实光线。
        边缘光线射向近轴入瞳边缘（y 方向）。"""
        from .trace import stop_hits

        z_ep, _ = self.entrance_pupil()
        r = self.pupil_radius()
        if not self.finite_object:
            y0, z0, d = r, 0.0, np.array([0.0, 0.0, 1.0])
            u = 0.0
        else:
            if not np.isfinite(z_ep):
                raise NotImplementedError("入瞳在无穷远（物方远心）时没有边缘光线定义")
            z0, y0 = -self.object_thickness, 0.0
            d = np.array([0.0, r, z_ep - z0])
            d = d / np.linalg.norm(d)
            u = r / (z_ep - z0)
        if self.lens.ray_aiming == 1:
            y1 = (y0 + u * self.object_thickness) if self.finite_object else y0
            return abs(float(self._paraxial(y1, u)[self.stop][0]))
        _, y = stop_hits(self, np.array([0.0]), np.array([y0]), np.array([z0]), d[None, :])
        value = abs(float(y[0]))
        if not np.isfinite(value):
            raise NotImplementedError("边缘光线追不到光阑面（手册：此时 OpticStudio 改用近轴光线定光阑半径，未实现）")
        return value

    def pupil_transfer(self, axis: str = "y") -> float:
        """从第 1 面到光阑面的近轴传递系数 B：y_stop = B·u0（锚在第 1 面轴上）。"""
        if self.stop is None:
            return 0.0
        return stop_transfer(self.items, self.stop, axis)[1]

    def entrance_pupil(self) -> tuple[float, float]:
        """(入瞳相对第 1 面的位置 z_ep, 光阑的顶点 z)。z_ep=inf 表示入瞳在无穷远。"""
        if "_entrance_pupil" not in self.__dict__:
            self._entrance_pupil = self._find_entrance_pupil()
        return self._entrance_pupil

    def _find_entrance_pupil(self) -> tuple[float, float]:
        if self.stop is None:
            return 0.0, self.items[0].vertex_z
        A, B = stop_transfer(self.items, self.stop)
        if abs(A) < 1e-12:
            return float("inf"), self.items[self.stop].vertex_z
        return B / A, self.items[self.stop].vertex_z

    def exit_pupil(self) -> tuple[float, float]:
        """出瞳：光阑被其后各面成像。返回 (相对像面的位置, 相对光阑半口径的放大率)。"""
        if self.stop is None:
            return 0.0, 1.0
        r1 = self._paraxial(1.0, 0.0, k0=self.stop)
        r2 = self._paraxial(0.0, 1.0, k0=self.stop)
        z_end = self.items[-1].vertex_z
        # 最后一面之后还有一段到像面（像面就是最后一项），用像面处的光线
        yA, uA = r1[-1][0], r1[-1][2]
        yB, uB = r2[-1][0], r2[-1][2]
        # 出瞳在过光阑中心的光线（B）与光轴的交点；放大率取光阑边缘出发的光线（A）在那里的高度。
        # 此前取 A、B 两条线的交点，与 OpticStudio 不符（piB 的 EXPP：旧 +0.2665，新 −0.226188 与 golden 一致）
        if abs(uB) < 1e-300:
            return float("-inf"), float("inf")
        z = -yB / uB                  # 相对像面的偏移（负为在像面之前）
        return z, abs(yA + uA * z)

    # ---- 视场渐晕因子 ----
    def vignetting_factors(self, hx, hy) -> np.ndarray:
        """视场渐晕：取曼哈顿距离最近的表内视场那一行，不插值。

        由 iris_vignetting、iris_set_vignetting 的 4 个表外视场反解所得；(0.26, 0.72)、(1, 0) 排除了欧氏距离。
        距离相等时取哪一行没有样本，这里取表中靠前的一行。浮点与 AD 共用此映射。
        """
        hx, hy = np.broadcast_arrays(np.atleast_1d(np.asarray(hx, float)),
                                     np.atleast_1d(np.asarray(hy, float)))
        if not np.all(np.isfinite(hx)) or not np.all(np.isfinite(hy)):
            raise ValueError("视场坐标必须有限")
        rows = self.lens.vignetting or []
        out = np.zeros(hx.shape + (5,))
        if not rows or not np.any(rows):
            return out
        sx, sy = self._field_scale()
        fields = np.asarray([(x / sx if sx else 0.0, y / sy if sy else 0.0)
                             for x, y, _ in self.lens.fields], dtype=float)
        factors = np.zeros((len(fields), 5))
        factors[:min(len(rows), len(fields))] = rows[:len(fields)]
        points = np.column_stack([hx.ravel(), hy.ravel()])
        nearest = np.abs(points[:, None, :] - fields).sum(axis=2).argmin(axis=1)
        return factors[nearest].reshape(hx.shape + (5,))

    def vignetted_pupil(self, px, py, hx, hy):
        """按渐晕因子把归一化光瞳坐标变成实际入瞳上的点（手册「Vignetting Factors」）：

        先 ``Px' = VDX + Px·(1−VCX)``、``Py' = VDY + Py·(1−VCY)``（偏心 + 压缩），再绕光轴转 ``VAN``（度）。
        只影响发射位置，不改变内核追迹。
        """
        px = np.asarray(px, dtype=float)
        py = np.asarray(py, dtype=float)
        f = self.vignetting_factors(hx, hy)
        vdx, vdy, vcx, vcy, van = (f[..., k] for k in range(5))
        if not np.any(vdx) and not np.any(vdy) and not np.any(vcx) and not np.any(vcy) and not np.any(van):
            return px, py
        qx = vdx + px * (1.0 - vcx)
        qy = vdy + py * (1.0 - vcy)
        th = np.radians(van)
        c, s = np.cos(th), np.sin(th)
        return qx * c - qy * s, qx * s + qy * c

    # ---- 孔径 ----
    def stop_semi_diameter(self) -> float:
        """光阑面的半口径（Float By Stop Size 用它定义孔径；文件里 DIAM 与浮动孔径同值）。"""
        if self.stop is None:
            raise NotImplementedError("系统里没有光阑面，Float By Stop Size 没有定义")
        it = self.items[self.stop]
        semi = float(it.semi or it.surface.ap_max)
        if not (np.isfinite(semi) and semi > 0):
            raise NotImplementedError(f"光阑面 {it.index} 的半口径为 {semi}，不能用于 Float By Stop Size")
        return semi

    def float_stop_pupil_radius(self) -> float:
        """Float By Stop Size 的入瞳半径 = 光阑半口径 / |A|，A 是第 1 面到光阑面的近轴横向传递系数。

        这是当前近轴实现；尚未对齐 UpdatePupil 追迹标定分支。
        旧案例相符不足以覆盖分支映射，缺口见 iris/mirror 计划第 8 节。
        """
        if getattr(self, "_float_pupil_radius", None) is None:
            if self.stop is None:
                raise NotImplementedError("系统里没有光阑面，Float By Stop Size 没有定义")
            transfer = stop_transfer(self.items, self.stop)[0]
            if not (np.isfinite(float(transfer)) and float(transfer) != 0.0):
                raise NotImplementedError("第 1 面到光阑面的近轴传递系数为 0，Float By Stop Size 的入瞳半径无定义")
            self._float_pupil_radius = self.stop_semi_diameter() / abs(float(transfer))
        return self._float_pupil_radius

    def stop_pupil_radius(self) -> float:
        """光阑面处的边缘光线高度：无穷远物体且瞄准 off 时用近轴高度，其余用实际高度；FLOA 用指定半口径。

        EXPD 用它：reflective 案例的光阑半口径是 45 mm 而边缘光线只到 40 mm，OpticStudio 报的 EXPD 51.65 = 2×0.6456×40。
        """
        if self.lens.aperture_type == "FLOA":
            return self.stop_semi_diameter()
        # 光阑在反射镜之后时两种高度才分得开（reflective_stop_on_secondary）：瞄准 off 的 EXPD 18.94419 是近轴高度
        # 9.46349 算出的，瞄准 real 的 18.97750 是实际高度 9.48013 算出的
        if self.lens.ray_aiming == 0 and not self.finite_object:
            return abs(self._paraxial(self.pupil_radius(), 0.0)[self.stop][0])
        return self.stop_radius()

    def pupil_radius(self) -> float:
        if self.lens.aperture_type == "EPD":
            return self.lens.aperture_value / 2.0
        if self.lens.aperture_type == "FLOA":
            return self.float_stop_pupil_radius()
        z_ep, _ = self.entrance_pupil()
        if not np.isfinite(z_ep):
            return float(self.lens.aperture_value)
        return na_pupil_radius(z_ep, self.object_thickness if self.finite_object else 0.0, self.lens.aperture_value)

    def pupil_reference(self) -> tuple[float, float]:
        """发射用的入瞳 (位置 z_ep, 半径 R)，按**主波长**定，所有波长共用。

        2026-11-04 与 OpticStudio 24.1 实测（iris 案例）：同一光瞳坐标在 F 线与 C 线上打在面 1 的交点逐位相同，
        说明它把入瞳位置和半径都冻在主波长上；rtc 早先按各波长自己的色散算 z_ep，两条谱线在面 1 差 6.9e-3 mm。
        """
        primary = self.lens.active_wavelengths[self.lens.primary_wave - 1][0]
        if abs(primary - self.wavelength) <= 1e-15:
            return self.entrance_pupil()[0], self.pupil_radius()
        ref = self.primary_system()
        return ref.entrance_pupil()[0], ref.pupil_radius()

    def marginal_image_ray(self):
        """像方边缘光线的方向余弦（用于定标 PSF 网格与 NA）。"""
        from .trace import trace

        b = trace(self, [1.0], [0.0])
        k = len(self.items) - 1
        return (float(b.l[0, k]), float(b.m[0, k]), float(b.n[0, k]))
