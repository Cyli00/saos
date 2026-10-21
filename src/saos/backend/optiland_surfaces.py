from __future__ import annotations

import math
import re

import numpy as np

from saos.backend.optiland_materials import unsupported
from saos.prescription import canonical_surface_params

TYPES = {"standard": "standard", "evenaspheric": "even_asphere", "oddaspheric": "odd_asphere",
         "paraxial": "paraxial", "coordinatebreak": "standard", "toroidal": "toroidal",
         "zernikestandardsag": "zernike", "zernikefringesag": "zernike", "chebyshevpolynomial": "chebyshev",
         "qtypeasphere": "forbes_qbfs", "qtypefreeform": "forbes_q2d", "diffractiongrating": "grating"}
ZEMAX_TYPES = {"standard": "STANDARD", "evenaspheric": "EVENASPH", "oddaspheric": "ODDASPHE", "paraxial": "PARAXIAL",
               "coordinatebreak": "COORDBRK", "toroidal": "TOROIDAL", "zernikestandardsag": "ZERNSAG",
               "zernikefringesag": "ZFRSAG", "chebyshevpolynomial": "CHEBY", "qtypeasphere": "QTYPE",
               "qtypefreeform": "QTYPE2", "diffractiongrating": "DGRATING"}
DISPLAY = {"standard": "Standard", "evenaspheric": "Even Asphere", "oddaspheric": "Odd Asphere", "coordinatebreak": "Coordinate Break",
           "paraxial": "Paraxial", "toroidal": "Toroidal", "zernikestandardsag": "Zernike Standard Sag",
           "zernikefringesag": "Zernike Fringe Sag", "chebyshevpolynomial": "Chebyshev Polynomial",
           "qtypeasphere": "Q-Type Asphere", "qtypefreeform": "Q-Type Freeform", "diffractiongrating": "Diffraction Grating"}


def kind(row):
    name = re.sub(r"[ _-]", "", row.get("type", "standard")).lower()
    return {"evenasphere": "evenaspheric", "oddasphere": "oddaspheric"}.get(name, name)


def parameters(row):
    params = canonical_surface_params(row.get("type", "standard"),
                                      {**(row.get("params") or {}), **{key: value for key, value in row.items() if re.fullmatch(r"par[1-9]\d*", key)}})
    if kind(row) in ("evenaspheric", "oddaspheric"):
        unknown = [key for key in params if not re.fullmatch(r"par[1-9]\d*", key)]
        if unknown:
            raise unsupported(f"未登记非球面表头 {unknown}；请使用已核实的 parN 列号，不能忽略这些系数")
    return params


def geometry_kwargs(row):
    k, p = kind(row), parameters(row)
    if k not in TYPES:
        raise unsupported(f"surface type {row.get('type')} is not supported by Optiland 0.6.3")
    kwargs = {"radius": row.get("radius", math.inf) or math.inf, "conic": row.get("conic", 0.0)}
    if k == "coordinatebreak":
        return "standard", {"radius": math.inf}
    if k == "paraxial":
        return "paraxial", {"f": row.get("focal_length", p.get("par1")), "radius": math.inf}
    if k in ("evenaspheric", "oddaspheric"):
        last = max((int(key[3:]) for key in p if re.fullmatch(r"par\d+", key)), default=0)
        kwargs["coefficients"] = [p.get(f"par{n}", 0.0) for n in range(1, last + 1)]
    elif k == "toroidal":
        if any(p.get(f"par{n}", 0) for n in range(13, 246)):
            raise unsupported("toroidal surfaces with additional Zernike terms require ZOS")
        kwargs = {"radius_y": kwargs["radius"], "radius_x": p.get("par1", math.inf) or math.inf,
                  "conic": kwargs["conic"], "toroidal_coeffs_poly_y": [p.get(f"par{n}", 0.0) for n in range(2, 9)]}
    elif k.startswith("zernike"):
        count = int(p.get("par13", max((int(key[3:]) - 14 for key in p if re.fullmatch(r"par\d+", key)), default=0)))
        norm = p.get("par14", 1.0)
        if count < 0 or norm <= 0:
            raise unsupported("Zernike term count and normalization radius must be non-negative and positive")
        kwargs.update(norm_radius=norm, coefficients=[p.get(f"par{15 + n}", 0.0) for n in range(count)],
                      zernike_type="noll" if k == "zernikestandardsag" else "fringe")
    elif k == "chebyshevpolynomial":
        nx, ny = int(p.get("par1", 0)), int(p.get("par2", 0))
        matrix = np.zeros((nx + 1, ny + 1))
        for key, value in p.items():
            match = re.fullmatch(r"[cC]\(?([0-9]+)[,_]([0-9]+)\)?", key)
            if match:
                i, j = map(int, match.groups())
                if i > nx or j > ny:
                    raise unsupported(f"Chebyshev coefficient {key} exceeds the declared polynomial order")
                matrix[i, j] = value
            elif re.fullmatch(r"par\d+", key) and int(key[3:]) >= 13 and value:
                raise unsupported("use explicit C(i,j) headers for Chebyshev coefficients; numeric column order requires ZOS")
        kwargs.update(coefficients=matrix.tolist(), norm_x=p.get("par3", 1.0), norm_y=p.get("par4", 1.0))
    elif k == "qtypeasphere":
        if p.get("par13", 0) != 0:
            raise unsupported("Optiland 0.6.3 provides Forbes Qbfs; Zemax Qcon requires ZOS")
        count = int(p.get("par14", 0))
        kwargs.update(norm_radius=p.get("par15", 1.0), radial_terms={n: p.get(f"par{16 + n}", 0.0) for n in range(count)})
    elif k == "qtypefreeform":
        terms = {}
        for key, value in p.items():
            match = re.fullmatch(r"([ab])\(([0-9]+),([0-9]+)\)", key.lower())
            if match:
                terms[(match[1], int(match[2]), int(match[3]))] = value
            elif re.fullmatch(r"par\d+", key) and int(key[3:]) >= 13 and value:
                raise unsupported("use explicit a(m,n)/b(m,n) headers for Forbes freeform coefficients")
        if p.get("par1", 0) or p.get("par2", 0):
            raise unsupported("decentred Forbes polynomial departures require ZOS")
        kwargs.update(norm_radius=p.get("par4", 1.0), freeform_coeffs=terms)
    elif k == "diffractiongrating":
        frequency = p.get("par1", 0.0)
        kwargs.update(grating_period=math.inf if frequency == 0 else 1.0 / frequency,
                      grating_order=int(p.get("par2", 0)), groove_orientation_angle=0.0)
    return TYPES[k], kwargs


def composite_zernike(surface, row):
    from optiland.geometries import BaseGeometry, EvenAsphere
    from optiland.geometries.newton_raphson import NewtonRaphsonGeometry

    p = parameters(row)
    base = EvenAsphere(surface.geometry.cs, row.get("radius", math.inf) or math.inf, row.get("conic", 0.0),
                      coefficients=[p.get(f"par{n}", 0.0) for n in range(1, 9)])
    departure = surface.geometry
    departure.radius = math.inf
    departure.k = 0.0

    class SaosCompositeSag(NewtonRaphsonGeometry):
        def __init__(self, cs, base_geometry, departure_geometry, dx, dy, extrapolate):
            super().__init__(cs)
            self.base, self.departure = base_geometry, departure_geometry
            self.dx, self.dy, self.extrapolate = dx, dy, extrapolate

        @property
        def radius(self):
            return self.base.radius

        @property
        def k(self):
            return self.base.k

        def set_radius(self, value):
            self.base.set_radius(value)

        def _mask(self, x, y):
            return (x - self.dx)**2 + (y - self.dy)**2 <= self.departure.norm_radius**2

        def sag(self, x=0, y=0):
            import optiland.backend as be

            value = self.departure.sag(x - self.dx, y - self.dy)
            return self.base.sag(x, y) + (value if self.extrapolate else be.where(self._mask(x, y), value, 0.0))

        def _surface_normal(self, x, y):
            from types import SimpleNamespace

            import optiland.backend as be

            nx, ny, nz = self.base.surface_normal(SimpleNamespace(x=x, y=y))
            ax, ay, az = self.departure.surface_normal(SimpleNamespace(x=x - self.dx, y=y - self.dy))
            gx, gy = -ax / az, -ay / az
            if not self.extrapolate:
                gx, gy = be.where(self._mask(x, y), gx, 0), be.where(self._mask(x, y), gy, 0)
            gx, gy = gx - nx / nz, gy - ny / nz
            length = be.sqrt(1 + gx * gx + gy * gy)
            return gx / length, gy / length, -be.ones_like(gx) / length

        def scale(self, value):
            self.base.scale(value)
            self.departure.scale(value)
            self.dx *= value
            self.dy *= value

        def flip(self):
            self.base.flip()
            self.departure.flip()

        def to_dict(self):
            return {**super().to_dict(), "base": self.base.to_dict(), "departure": self.departure.to_dict(),
                    "dx": self.dx, "dy": self.dy, "extrapolate": self.extrapolate}

        @classmethod
        def from_dict(cls, data):
            from optiland.coordinate_system import CoordinateSystem

            return cls(CoordinateSystem.from_dict(data["cs"]), BaseGeometry.from_dict(data["base"]),
                       BaseGeometry.from_dict(data["departure"]), data["dx"], data["dy"], data["extrapolate"])

    surface.geometry = SaosCompositeSag(surface.geometry.cs, base, departure, p.get("par9", 0), p.get("par10", 0), bool(p.get("par0", 0)))


def aperture(spec, semi=None):
    from optiland.physical_apertures import (
        DifferenceAperture,
        EllipticalAperture,
        OffsetRadialAperture,
        RadialAperture,
        RectangularAperture,
    )

    if not spec:
        # 瞄准到光阑边缘的边缘光线落点有 1e-12 量级的舍入，按严格不等式会被误裁；留相对 1e-9 的余量
        return RadialAperture(semi * (1 + 1e-9)) if semi is not None else None
    k, dx, dy = spec["type"], spec.get("x_decenter", 0.0), spec.get("y_decenter", 0.0)
    if k.startswith("circular"):
        shape = OffsetRadialAperture(spec["max"], spec.get("min", 0.0), dx, dy)
    elif k.startswith("rectangular"):
        shape = RectangularAperture(dx - spec["x_half"], dx + spec["x_half"], dy - spec["y_half"], dy + spec["y_half"])
    elif k.startswith("elliptical"):
        shape = EllipticalAperture(spec["x_half"], spec["y_half"], dx, dy)
    else:
        raise unsupported(f"physical aperture {k} requires ZOS")
    return DifferenceAperture(RadialAperture(math.inf), shape) if k.endswith("obscuration") else shape


def aperture_description(spec):
    if not spec:
        return {"type": "None"}
    from saos.prescription import SURFACE_APERTURES

    return {"type": SURFACE_APERTURES[spec["type"]], "ApertureXDecenter": spec.get("x_decenter", 0),
            "ApertureYDecenter": spec.get("y_decenter", 0), "MinimumRadius": spec.get("min", 0),
            "MaximumRadius": spec.get("max"), "XHalfWidth": spec.get("x_half"), "YHalfWidth": spec.get("y_half")}


def transform(reference, values):
    from optiland.coordinate_system import CoordinateSystem

    values = values or {}
    rotate = lambda ref: CoordinateSystem(rx=math.radians(values.get("tilt_x", 0)), ry=math.radians(values.get("tilt_y", 0)),
                                          rz=math.radians(values.get("tilt_z", 0)), reference_cs=ref)
    shift = lambda ref: CoordinateSystem(x=values.get("decenter_x", 0), y=values.get("decenter_y", 0), reference_cs=ref)
    return shift(rotate(reference)) if values.get("order") == "tilt_decenter" else rotate(shift(reference))


def position_surfaces(optic, rows):
    """按处方逐面放置坐标系，返回每个面厚度方向的全局单位向量（下标同面号，物面为 None）：
    第 i 面厚度变化 Δ 时，其后所有面整体平移 Δ·axes[i]，优化内循环据此就地移动镜面而不重建。"""
    from optiland.coordinate_system import CoordinateSystem

    frame = CoordinateSystem()
    spacers = [None]
    for i, row in enumerate(rows):
        if i == 0:
            continue
        dt = row.get("decenter_tilt") or {}
        if kind(row) == "coordinatebreak":
            p = parameters(row)
            own = transform(frame, {"decenter_x": p.get("par1", 0), "decenter_y": p.get("par2", 0),
                "tilt_x": p.get("par3", 0), "tilt_y": p.get("par4", 0), "tilt_z": p.get("par5", 0),
                "order": "tilt_decenter" if p.get("par6", 0) else "decenter_tilt"})
            after = own
        else:
            own = transform(frame, dt.get("before")) if dt.get("before") else frame
            after = frame if dt.get("after") == "reverse" else transform(own, dt["after"]) if dt.get("after") else own
        optic.surfaces[i].geometry.cs = own
        if kind(row).startswith("zernike"):
            composite_zernike(optic.surfaces[i], row)
        frame = CoordinateSystem(z=row.get("thickness", 0.0), reference_cs=after)
        spacers.append(frame)
    flatten_frames([surface.geometry.cs for surface in optic.surfaces] + spacers[1:])
    from scipy.spatial.transform import Rotation

    return [None] + [Rotation.from_euler("xyz", [float(np.asarray(v).ravel()[0]) for v in (cs.rx, cs.ry, cs.rz)]).as_matrix()[:, 2]
                     for cs in spacers[1:]]


def _rotate(r: np.ndarray, v: np.ndarray) -> np.ndarray:
    """r @ v，但跳过 r 中的零元素。无穷远物体的 z 平移是 ±∞：矩阵乘法里 0·∞ 得 NaN，会把横向坐标也污染；
    这里只让非零的转换项参与，∞ 沿旋转后的方向传递，结果与有限值的情形一致。
    r @ v that skips the zero entries of r. An object at infinity has an infinite z translation: in a matrix product
    0·∞ is NaN and would contaminate the transverse coordinates; here only non-zero terms take part, so the infinity
    follows the rotated direction exactly as finite values do."""
    out = np.zeros(3)
    for i in range(3):
        for j in range(3):
            if r[i, j] != 0.0:
                out[i] += r[i, j] * v[j]
    return out


def flatten_frames(frames) -> None:
    """把逐面相连的参考坐标系改成各自的绝对坐标系。Optiland 每次变换都沿参考链递归，链长随面号增长，
    追迹因此按面数平方变慢（300 面 build 10.7 s）。先按缓存一遍算出每个坐标系的有效变换，再原地改写，
    有效变换不变；复合面型等仍引用同一个对象的地方随之更新。"""
    from scipy.spatial.transform import Rotation

    cache: dict[int, tuple[np.ndarray, np.ndarray]] = {}

    def local(cs):
        rx, ry, rz = (float(np.asarray(v).ravel()[0]) for v in (cs.rx, cs.ry, cs.rz))
        t = np.array([float(np.asarray(v).ravel()[0]) for v in (cs.x, cs.y, cs.z)])
        return t, Rotation.from_euler("xyz", [rx, ry, rz]).as_matrix()

    def effective(cs):
        chain = []
        node = cs
        while node is not None and id(node) not in cache:
            chain.append(node)
            node = node.reference_cs
        t, r = cache[id(node)] if node is not None else (np.zeros(3), np.eye(3))
        for item in reversed(chain):
            lt, lr = local(item)
            t, r = t + _rotate(r, lt), r @ lr
            cache[id(item)] = (t, r)
        return cache[id(cs)]

    resolved = [(cs, effective(cs)) for cs in frames]
    for cs, (t, r) in resolved:
        if cs.reference_cs is None:
            continue
        rx, ry, rz = Rotation.from_matrix(r).as_euler("xyz")
        cs.reference_cs = None
        cs.x, cs.y, cs.z = float(t[0]), float(t[1]), float(t[2])
        cs.rx, cs.ry, cs.rz = float(rx), float(ry), float(rz)
