"""全覆盖案例：把所有面型和孔径串进少数几个系统，用来锁定 rtc 重构前后的行为。

这些镜头只求光线走得通，不是好设计，也没有 OpticStudio 真值；是否正确由 tests/fixtures/cases/truth.json 回答。
一个系统只能有一种孔径类型、瞄准方式和光阑位置，所以按这些互斥设置分成多个模式。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rtc import zmx
from rtc.material.types import Mirror, Model
from rtc.system import OpticalSystem

LENSES = Path(__file__).parent / "fixtures" / "lenses"
CROWN = Model(1.5168, 64.17)
FLINT = Model(1.62, 36.4)
FIELDS = [(0.0, 0.0, 1.0), (0.0, 3.0, 1.0), (2.0, 2.0, 1.0)]
WAVES = [(0.4861, 1.0), (0.5876, 1.0), (0.6563, 1.0)]
# 光瞳点：中心、中带、近边缘，x、y 不对称，反转或交换坐标都会变
PUPIL = [(0.0, 0.0), (0.5, 0.0), (0.0, 0.5), (-0.35, 0.35), (0.95, 0.0), (-0.95, 0.0),
         (0.0, 0.95), (0.0, -0.95), (0.6, 0.7), (-0.7, -0.6), (0.1, 0.05), (0.04, -0.09)]


def _surface(index, kind="STANDARD", radius=0.0, thickness=0.0, material=None, conic=0.0, params=None,
             aperture=None, stop=False, semi=0.0) -> zmx.Surface:
    s = zmx.Surface(index=index, type=kind, curvature=1.0 / radius if radius else 0.0, conic=conic,
                    thickness=thickness, params=dict(params or {}), is_stop=stop, semi_diameter=semi)
    s.material = material
    if aperture:
        s.aperture_code, s.ap_min, s.ap_max = aperture[:3]
        s.ap_dx, s.ap_dy = aperture[3:] if len(aperture) > 3 else (0.0, 0.0)
    return s


def _lens(surfaces, *, aperture=("EPD", 10.0), aiming=0, fields=None, field_type="ANGLE", object_distance=float("inf")) -> zmx.Lens:
    fields = list(fields or FIELDS)
    lens = zmx.Lens(aperture_type=aperture[0], aperture_value=aperture[1], fields=fields, field_type=field_type,
                    num_fields=len(fields), num_waves=len(WAVES), wavelengths=list(WAVES), primary_wave=2, ray_aiming=aiming)
    lens.vignetting = [(0.0,) * 5 for _ in fields]
    lens.surfaces = [zmx.Surface(index=0, thickness=object_distance), *surfaces]
    return lens


def _refractive(stop_semi: float = 3.0) -> list[zmx.Surface]:
    """折射全面型：每个面带一种孔径。面 7 是光阑。"""
    return [
        _surface(1, radius=60.0, thickness=3.0, material=CROWN, conic=-0.5, aperture=(1, 0.0, 4.9)),
        _surface(2, "EVENASPH", radius=-200.0, thickness=2.0, params={1: 1e-4, 2: 2e-6}, aperture=(2, 0.0, 0.4)),
        _surface(3, "BICONICX", radius=80.0, thickness=2.0, material=FLINT, conic=0.1, params={1: 70.0, 2: 0.2},
                 aperture=(4, 4.2, 3.8, 0.2, -0.1)),
        _surface(4, "BICONICZ", radius=-150.0, thickness=2.0, conic=-0.2,
                 params={1: -140.0, 2: 0.1, 14: 2e-4, 15: 1e-6, 30: -1e-4, 32: 3e-7},
                 aperture=(5, 0.5, 0.4, 0.3, 0.0)),
        _surface(5, "TOROIDAL", radius=90.0, thickness=2.0, material=CROWN, conic=0.05, params={1: 0.0, 2: 1e-4, 3: 1e-6},
                 aperture=(6, 4.1, 3.6, 0.1, 0.0)),
        _surface(6, "BICONICX", radius=-300.0, thickness=1.5, params={1: 0.0, 2: 0.0}, aperture=(7, 0.5, 0.3, 0.0, 0.2)),
        _surface(7, thickness=3.0, stop=True, semi=stop_semi, aperture=(10, 0.0, stop_semi)),
        _surface(8, radius=-40.0, thickness=2.5, material=FLINT, aperture=(3, 0.15, 4.0, 0.1, 0.0)),
        _surface(9, radius=-25.0, thickness=4.0),
        _surface(10, "PARAXIAL", thickness=30.0, params={1: 80.0, 2: 1.0}),
        _surface(11),
    ]


def _reflective(stop: int = 1) -> list[zmx.Surface]:
    """三次反射（奇数次，方向符号翻转）后接玻璃和理想透镜，负厚度。"""
    surfaces = [
        _surface(1, radius=-400.0, thickness=-150.0, material=Mirror(), conic=-1.0, aperture=(2, 0.0, 2.0)),
        _surface(2, "EVENASPH", radius=-200.0, thickness=100.0, material=Mirror(), params={2: 1e-9}),
        _surface(3, "BICONICX", radius=-900.0, thickness=-60.0, material=Mirror(), params={1: -1000.0, 2: 0.0}),
        _surface(4, radius=50.0, thickness=-3.0, material=CROWN),
        _surface(5, radius=30.0, thickness=-20.0),
        _surface(6, "PARAXIAL", thickness=-30.0, params={1: 200.0, 2: 1.0}),
        _surface(7),
    ]
    surfaces[stop - 1].is_stop = True
    surfaces[stop - 1].semi_diameter = 8.0
    return surfaces


def _diffractive() -> list[zmx.Surface]:
    """二元面和 Fresnel 面，生产拒绝，只有 rtc 自己的路径。"""
    return [
        _surface(1, "BINARY_2", radius=80.0, thickness=3.0, material=CROWN, stop=True, semi=5.0,
                 params={0: 1.0, 1: 2e-5, 2: 1e-7, 13: 2.0, 14: 10.0, 15: -60.0, 16: 4.0}),
        _surface(2, "FRESNELS", radius=-120.0, thickness=4.0, params={1: 1e-5, 2: 1e-7}),
        _surface(3, radius=-80.0, thickness=40.0, material=None),
        _surface(4),
    ]


def _fixture(name: str) -> Callable[[], zmx.Lens]:
    return lambda: zmx.parse(LENSES / name)


@dataclass(frozen=True)
class Mode:
    name: str
    build: Callable[[], zmx.Lens]
    unverified: bool = False
    #: 这些面至少要首挡一条光线，否则案例没有判别力
    clips: tuple[int, ...] = ()


MODES = [
    Mode("refractive_epd", lambda: _lens(_refractive()), clips=(1, 2, 3, 4, 5, 6, 7, 8)),
    Mode("refractive_epd_real", lambda: _lens(_refractive(), aiming=2), clips=(1, 2, 3, 4, 5, 6, 7, 8)),
    Mode("refractive_float", lambda: _lens(_refractive(), aperture=("FLOA", 0.0)), clips=(2, 4, 6, 8)),
    Mode("refractive_float_real", lambda: _lens(_refractive(), aperture=("FLOA", 0.0), aiming=2), clips=(2, 4, 6, 8)),
    Mode("refractive_obna_finite", lambda: _lens(_refractive(), aperture=("OBNA", 0.025), aiming=2, object_distance=200.0,
                                                 fields=[(0.0, 0.0, 1.0), (0.0, 5.0, 1.0), (3.0, 3.0, 1.0)], field_type="OP1"),
         unverified=True, clips=(1, 2, 3, 4, 5, 8)),
    Mode("refractive_epd_finite_paraxial", lambda: _lens(_refractive(), aiming=1, object_distance=200.0,
                                                         fields=[(0.0, 0.0, 1.0), (0.0, 5.0, 1.0), (3.0, 3.0, 1.0)], field_type="OP1"),
         unverified=True, clips=(2, 3, 4, 5, 7, 8)),
    Mode("reflective_epd", lambda: _lens(_reflective(), aperture=("EPD", 30.0), fields=[(0.0, 0.0, 1.0), (0.0, 0.3, 1.0), (0.2, 0.2, 1.0)]),
         clips=(1,)),
    Mode("reflective_float_real", lambda: _lens(_reflective(stop=2), aperture=("FLOA", 0.0), aiming=2,
                                                fields=[(0.0, 0.0, 1.0), (0.0, 0.3, 1.0), (0.2, 0.2, 1.0)]), clips=(1,)),
    Mode("diffractive_epd", lambda: _lens(_diffractive(), fields=[(0.0, 0.0, 1.0), (0.0, 2.0, 1.0), (1.0, 1.0, 1.0)])),
    Mode("piA", _fixture("piA-r012.zmx")),
    Mode("piB", _fixture("piB-r007.zmx")),
    Mode("codex", _fixture("codex-r017.zmx")),
]
BY_NAME = {m.name: m for m in MODES}


def systems(mode: Mode) -> dict[int, OpticalSystem]:
    """主波长和第 1 波长各一个系统：非主波长要沿用主波长的入瞳。"""
    lens = mode.build()
    waves = lens.active_wavelengths
    primary = lens.primary_wave
    chosen = sorted({primary, 1})
    return {w: OpticalSystem(lens, wavelength_um=waves[w - 1][0], allow_unverified=mode.unverified) for w in chosen}


def field_points(lens: zmx.Lens, system: OpticalSystem) -> list[tuple[float, float]]:
    sx, sy = system._field_scale()
    return [(x / sx if sx else 0.0, y / sy if sy else 0.0) for x, y, _ in lens.fields]
