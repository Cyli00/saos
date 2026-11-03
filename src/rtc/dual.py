"""前向自动微分（向量化切向）：给 DLS 的雅可比用。

约定：``Dual.v`` 是值（任意形状的 float64 数组），``Dual.d`` 是同形状的切向数组，
额外在前面带一维参数轴 ``(P, ...)``。P = 0 表示没有切向（当作常数）。

只实现追迹与面形里实际用到的算子；遇到没实现的算子会直接报错而不是悄悄丢导数。
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
        if d is None:
            self.d = np.zeros((0,) + np.shape(v))
        else:
            self.d = d

    # ---- 构造 ----
    @staticmethod
    def konst(v):
        return Dual(np.asarray(v, dtype=np.float64))

    @staticmethod
    def param(v, i, p):
        d = np.zeros((p,) + np.shape(v))
        d[i] = 1.0
        return Dual(np.asarray(v, dtype=np.float64), d)

    # ---- 索引 ----
    def __getitem__(self, i):
        """只支持一维值的下标（追迹结果按光线取）。"""
        if self.d.shape[0] == 0:
            return Dual(np.asarray(self.v)[i], self.d)
        return Dual(np.asarray(self.v)[i], self.d[(slice(None), i)])

    def __bool__(self):
        return bool(np.all(self.v))

    # ---- 四则 ----
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

    # ---- 幂 ----
    def __pow__(self, n):
        if not isinstance(n, (int, np.integer)):
            raise NotImplementedError("Dual 只支持整数幂")
        v = self.v**n
        return Dual(v, n * self.v ** (n - 1) * self.d if n else np.zeros_like(self.d))

    # ---- 函数 ----
    def sqrt(self):
        v = np.sqrt(self.v)
        return Dual(v, self.d / (2.0 * v))

    def __abs__(self):
        mask = self.v >= 0.0
        return Dual(np.abs(self.v), np.where(mask, self.d, -self.d))

    # ---- 比较（只比数值部分；供 ``D.where(root > 0, ...)`` 这类分支持使用）----
    def __gt__(self, o):
        return self.v > _as(o).v

    def __lt__(self, o):
        return self.v < _as(o).v

    def __ge__(self, o):
        return self.v >= _as(o).v

    def __le__(self, o):
        return self.v <= _as(o).v

    def sin(self):
        return Dual(np.sin(self.v), np.cos(self.v) * self.d)

    def cos(self):
        return Dual(np.cos(self.v), -np.sin(self.v) * self.d)

    def atan2(self, x):
        av, ad, bv, bd = _pair(self, x)
        den = av * av + bv * bv
        return Dual(np.arctan2(av, bv), (bv * ad - av * bd) / den)


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


def _pair(a, b, extra=()):
    """二元运算前对齐两个操作数：值广播到同一形状（还要容下 ``extra``，如 where 的条件），
    P = 0（常数）的一侧补零切向。"""
    a, b = _as(a), _as(b)
    pa, pb = a.d.shape[0], b.d.shape[0]
    if pa and pb and pa != pb:
        raise ValueError(f"Dual 参数个数不一致：{pa} 与 {pb}")
    shape = np.broadcast_shapes(np.shape(a.v), np.shape(b.v), extra)
    if a.d.shape == b.d.shape and np.shape(a.v) == np.shape(b.v) == shape:
        return a.v, a.d, b.v, b.d
    p = max(pa, pb)
    return (*_lift(a, shape, p), *_lift(b, shape, p))


def sqrt(o):
    return o.sqrt() if isinstance(o, Dual) else np.sqrt(o)


def sin(o):
    return o.sin() if isinstance(o, Dual) else np.sin(o)


def cos(o):
    return o.cos() if isinstance(o, Dual) else np.cos(o)


def arctan2(y, x):
    return y.atan2(x) if isinstance(y, Dual) else np.arctan2(y, x)


def where(cond, a, b):
    if not isinstance(a, Dual) and not isinstance(b, Dual):
        return np.where(cond, a, b)
    cond = np.asarray(cond)
    av, ad, bv, bd = _pair(a, b, cond.shape)
    return Dual(np.where(cond, av, bv), np.where(cond, ad, bd))


def maximum(o, c):
    """与 max(x, 常数) 等价，常数处导数为 0（追迹里只用于非实解兜底）。"""
    if not isinstance(o, Dual):
        return np.maximum(o, c)
    o = _as(o)
    mask = o.v > c
    return Dual(np.maximum(o.v, c), np.where(mask, o.d, 0.0))
