"""FFT MTF 与逐条光线 OPD（rtc.psf.otf_fft、mtf_fft、opd_rays）的解析检查，镜头取 fixtures/golden/paraxial-glass。

无像差圆形光瞳的衍射极限 MTF 有闭式解 (2/π)(acos x − x√(1−x²))，x = ν/ν_c；与 OpticStudio FFT MTF 的对账尚未做。
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from rtc import zmx
from rtc.psf import mtf_fft, opd_rays, otf_fft
from rtc.system import OpticalSystem

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden" / "paraxial-glass"


@pytest.fixture(scope="module")
def system():
    data = json.loads((GOLDEN / "trace.json").read_text(encoding="utf-8"))
    return OpticalSystem(zmx.parse(GOLDEN / "lens.zmx"), wavelength_um=data["wavelength_um"])


def test_chief_ray_opd_is_zero(system):
    opd, ok = opd_rays(system, np.array([0.0, 0.5, 0.0]), np.array([0.0, 0.0, 0.7]))
    assert ok.all() and opd[0] == pytest.approx(0.0, abs=1e-12)


def test_diffraction_limited_mtf_follows_the_circular_pupil_formula(system):
    n = 128
    otf_y, otf_x, df = otf_fft(system, n=n, aberrated=False)
    cutoff = df * (n - 1)
    nu = df * np.arange(n)
    x = np.clip(nu / cutoff, 0.0, 1.0)
    exact = (2 / np.pi) * (np.arccos(x) - x * np.sqrt(1 - x * x))
    # 方格取样的圆边是台阶，误差随 1/n 收敛 / the sampled rim is a staircase, the error shrinks like 1/n
    assert np.abs(otf_y) == pytest.approx(exact, abs=0.02)
    assert np.abs(otf_x) == pytest.approx(exact, abs=0.02)


def test_polychromatic_mtf_starts_at_one_and_vanishes_beyond_every_cutoff(system):
    result = mtf_fft([(system, 1.0)], frequencies=None, n=64)
    assert result["tangential"][0] == pytest.approx(1.0, abs=1e-12)
    beyond = mtf_fft([(system, 1.0)], frequencies=[1.5 * result["cutoff"]], n=64)
    assert beyond["tangential"][0] == 0.0 and beyond["sagittal"][0] == 0.0
