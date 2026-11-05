"""前向自动微分的数：把内核的切向交给 Python 层组合（残差、理想透镜相位、光瞳的近轴量）。

约定：``Dual.v`` 是值（任意形状的 float64 数组），``Dual.d`` 是切向数组，在前面多一维参数轴 ``(P, ...)``。
P = 0 表示常数。只实现这几处用到的算子；遇到没实现的算子直接报错，不会悄悄丢导数。
"""

from __future__ import annotations

import numpy as np


class Dual:
    __slots__ = ("v", "d")
    #: 让 numpy 把 ``ndarray * Dual`` 交给 ``Dual.__rmul__``，而不是退化成 object 数组；
    #: 同时让未实现的 ufunc（如 ``np.exp(Dual)``）直接报错，不静默丢掉切向。
    __array_ufunc__ = None
    __array_priority__ = 1000.0

    def __init__(self, v, d=None):
        self.v = v
        self.d = np.zeros((0,) + np.shape(v)) if d is None else d

    @staticmethod
    def konst(v):
        return Dual(np.asarray(v, dtype=np.float64))

    @staticmethod
    def param(v, i, p):
        d = np.zeros((p,) + np.shape(v))
        d[i] = 1.0
        return Dual(np.asarray(v, dtype=np.float64), d)

    def __getitem__(self, i):
        """只支持一维值的下标（追迹结果按光线取）。"""
        if self.d.shape[0] == 0:
            return Dual(np.asarray(self.v)[i], self.d)
        return Dual(np.asarray(self.v)[i], self.d[(slice(None), i)])

    def __bool__(self):
        return bool(np.all(self.v))

    def __add__(self, o):
        av, ad, bv, bd = _pair(self, o)
        return Dual(av + bv, ad + bd)

    __radd__ = __add__

    def __sub__(self, o):
        av, ad, bv, bd = _pair(self, o)
        return Dual(av - bv, ad - bd)

    def __rsub__(self, o):
        av, ad, bv, bd = _pair(self, o)
        return Dual(bv - av, bd - ad)

    def __mul__(self, o):
        av, ad, bv, bd = _pair(self, o)
        return Dual(av * bv, ad * bv + av * bd)

    __rmul__ = __mul__

    def __truediv__(self, o):
        av, ad, bv, bd = _pair(self, o)
        v = av / bv
        return Dual(v, ad / bv - v * bd / bv)

    def __rtruediv__(self, o):
        return _as(o).__truediv__(self)

    def __neg__(self):
        return Dual(-self.v, -self.d)

    def __abs__(self):
        return Dual(np.abs(self.v), np.where(self.v >= 0.0, self.d, -self.d))

    def sqrt(self):
        v = np.sqrt(self.v)
        return Dual(v, self.d / (2.0 * v))


def _as(o) -> Dual:
    return o if isinstance(o, Dual) else Dual.konst(o)


def _lift(o: Dual, shape, p: int):
    """把值广播到 ``shape``、切向广播到 ``(p,) + shape``。

    切向的参数轴在最前，不能交给 numpy 的尾部对齐：标量参数（切向 ``(P,)``）
    与 n 条光线（值 ``(n,)``）相乘时，尾部对齐会把 P 当成光线轴，P == n 时还不报错。
    """
    v = o.v if np.shape(o.v) == shape else np.broadcast_to(o.v, shape)
    d = o.d
    if d.shape == (p,) + shape:
        return v, d
    if d.shape[0] == 0:
        return v, np.zeros((p,) + shape)
    pad = len(shape) - (d.ndim - 1)
    d = d.reshape((d.shape[0],) + (1,) * pad + d.shape[1:])
    return v, np.broadcast_to(d, (p,) + shape)


def _pair(a, b):
    """二元运算前对齐两个操作数：值广播到同一形状，P = 0（常数）的一侧补零切向。"""
    a, b = _as(a), _as(b)
    pa, pb = a.d.shape[0], b.d.shape[0]
    if pa and pb and pa != pb:
        raise ValueError(f"Dual 参数个数不一致：{pa} 与 {pb}")
    shape = np.broadcast_shapes(np.shape(a.v), np.shape(b.v))
    if a.d.shape == b.d.shape and np.shape(a.v) == np.shape(b.v) == shape:
        return a.v, a.d, b.v, b.d
    p = max(pa, pb)
    return (*_lift(a, shape, p), *_lift(b, shape, p))


def sqrt(o):
    return o.sqrt() if isinstance(o, Dual) else np.sqrt(o)
