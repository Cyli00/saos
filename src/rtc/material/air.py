"""空气绝对折射率（手册 §2.1.1.4.2 的空气公式，参考 15 °C、1 atm）。"""

import numpy as np

from .formulas import wavelengths
from .types import Environment, MaterialUnsupported


def n_air(wavelength_um, temperature=20.0, pressure=1.0):
    Environment(temperature, pressure, True)
    w = wavelengths(wavelength_um)
    s = w * w
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        n0 = 0.0294981 * s / (146.0 * s - 1.0) + 1.000064328 + 0.0002554 * s / (41.0 * s - 1.0)
        result = 1.0 + (n0 - 1.0) / (1.0 + 0.0034785 * (temperature - 15.0)) * pressure
    if not np.all(np.isfinite(result) & (result > 0)):
        raise MaterialUnsupported("空气公式在所给波长及温压下无有限正结果")
    return result
