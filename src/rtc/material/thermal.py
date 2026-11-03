"""目录 TD 的绝对折射率温度增量；不包含几何热膨胀。"""

import numpy as np

from .types import MaterialUnsupported


def delta_absolute_index(n, wavelength_um, delta_t, coefficients):
    c = np.asarray(coefficients, dtype=np.float64)
    if c.shape != (6,) or not np.all(np.isfinite(c)) or not np.isfinite(delta_t):
        raise MaterialUnsupported("TD 须包含 6 个有限热系数和有限温差")
    d0, d1, d2, e0, e1, tk = c
    w = np.asarray(wavelength_um, dtype=np.float64)
    denominator = w * w - tk * tk if tk >= 0 else w * w + tk * tk
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        result = (n * n - 1.0) / (2.0 * n) * (
            ((d2 * delta_t + d1) * delta_t + d0) * delta_t
            + (e1 * delta_t + e0) * delta_t / denominator
        )
    if not np.all(np.isfinite(result)):
        raise MaterialUnsupported("TD 温度修正产生非有限折射率增量")
    return result
