"""AGF 公式编号 1–13 的色散式（手册 §8.1.2.6 的写法），统一使用 float64。

极点与零值的回退行为由 ZOS-API 实测结果反推，见 docs/plans/rtc/MATERIAL.md。
"""

from functools import partial
from numbers import Real

import numpy as np

from .types import MaterialInvalid, MaterialUnsupported


def wavelengths(value):
    try:
        value = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise MaterialInvalid("波长须为数值或数值数组") from exc
    if not np.all(np.isfinite(value) & (value > 0)):
        raise MaterialInvalid("波长须为有限正数，单位 µm")
    return value


def _power(value, exponent):
    result = np.ones_like(value)
    for _ in range(exponent):
        result = result * value
    return result


def _schott(w, c, nd, kind):
    s = w * w
    if kind == 13:
        value = c[0] + c[1] * s + c[2] * s * s
        for i in range(3, 9):
            value = value + c[i] / _power(s, i - 2)
    else:
        value = c[0] + c[1] * s
        for i in range(2, 8 if kind == 10 else 6):
            value = value + c[i] / _power(s, i - 1)
        if kind == 12:
            value = value + c[6] * s * s + c[7] * s * s * s
    # n² 为零或 NaN 时回退到 nd²（ZOS-API 实测）
    value = np.where((value == 0) | np.isnan(value), nd * nd if nd != 0 else 1.0, value)
    return np.sqrt(np.abs(value))


def _sellmeier(w, c, nd, count, offset):
    s = w * w
    value = np.zeros_like(w) if offset == 0 else c[0] + np.zeros_like(w)
    start = 0 if offset == 0 else 1
    for i in range(start, start + count * 2, 2):
        value = value + np.where(s == c[i + 1], 1000.0, c[i] * s / (s - c[i + 1]))
    return np.sqrt(np.abs(value + (1.0 if offset == 0 else 0.0)))


def _herzberger(w, c, nd):
    s = w * w
    term = np.where(s == 0.028, 100.0, 1.0 / (s - 0.028))
    return c[0] + (c[1] + c[2] * term) * term + c[3] * s + c[4] * s * s + c[5] * s * s * s


def _sellmeier2(w, c, nd):
    s = w * w
    return np.sqrt(np.abs(1.0 + c[0] + c[1] / (1.0 - c[2] * c[2] / s) + c[3] / (s - c[4] * c[4])))


def _conrady(w, c, nd):
    return c[0] + c[1] / w + c[2] / (np.sqrt(w) * w * w * w)


def _handbook(w, c, nd, kind):
    s = w * w
    numerator = c[1] if kind == 7 else c[1] * s
    return np.sqrt(np.abs(c[0] + numerator / (s - c[2]) - c[3] * s))


FORMULAS = {
    1: (6, partial(_schott, kind=1)),
    2: (6, partial(_sellmeier, count=3, offset=0)),
    3: (6, _herzberger),
    4: (5, _sellmeier2),
    5: (3, _conrady),
    6: (8, partial(_sellmeier, count=4, offset=0)),
    7: (4, partial(_handbook, kind=7)),
    8: (4, partial(_handbook, kind=8)),
    9: (5, partial(_sellmeier, count=2, offset=1)),
    10: (8, partial(_schott, kind=10)),
    11: (10, partial(_sellmeier, count=5, offset=0)),
    12: (8, partial(_schott, kind=12)),
    13: (9, partial(_schott, kind=13)),
}


def dispersion(wavelength_um, coefficients, formula, nd=0.0):
    w = wavelengths(wavelength_um)
    if isinstance(formula, bool) or not isinstance(formula, (int, np.integer)) or formula not in FORMULAS:
        raise MaterialUnsupported(f"色散公式 {formula} 未实现")
    count, evaluate = FORMULAS[formula]
    try:
        c = np.asarray(coefficients, dtype=np.float64)
    except (ValueError, TypeError) as exc:
        raise MaterialUnsupported("色散系数须为一维数值序列") from exc
    if c.ndim != 1 or len(c) < count or not np.all(np.isfinite(c)) or not isinstance(nd, Real) or not np.isfinite(nd):
        raise MaterialUnsupported(f"公式 {formula} 须有至少 {count} 个有限系数和有限 nd")
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        result = evaluate(w, c, nd)
    if not np.all(np.isfinite(result)):
        raise MaterialUnsupported(f"公式 {formula} 在所给波长产生非有限折射率")
    return result
