"""模型玻璃 n(λ; nd, Vd, dPgF)：由 ZOS-API 实测 INDX 结合公开的 Laurent 型色散式反推；Vd <= 0 时为常量 nd。"""

import numpy as np

from .formulas import wavelengths
from .types import MaterialUnsupported, Model


def model_index(wavelength_um, nd, vd, dpgf=0.0):
    Model(nd, vd, dpgf)
    w = wavelengths(wavelength_um)
    if vd <= 0:
        return np.full_like(w, nd)
    p = (0.6438 - 0.001682 * vd) + dpgf
    q = (nd - 1.0) / vd
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        result = (((8.84931 * p - 7.12849) * q + nd)
                  + (4.95284 - 6.91677 * p) * q / w
                  + (0.454415 * p - 0.202244) * q / (np.sqrt(w) * w * w * w))
    if not np.all(np.isfinite(result)):
        raise MaterialUnsupported("模型玻璃产生非有限折射率")
    return result
