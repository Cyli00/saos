"""rtc 实例的评价函数：OpticStudio 的操作数行格式、.zmx 读写、批量求值、优化向导（ADR-0017）。
The rtc instance's merit function: OpticStudio's operand rows, .zmx reading and writing, batched evaluation and the wizard.

行与 OpticStudio 的评价函数编辑器同构：type、params（Int1、Int2、Data1…Data6）、target、weight。.zmx 里每行写成
``OP Int1 Int2 D1 D2 D3 D4 Target Weight D5 D6``（rtc 的 fixtures/lenses 与 fixtures/rounds 里 OpticStudio 24.1 存盘所见），
所以 rtc 存盘的镜头在第 4 阶段原样交给 OpticStudio。
Rows mirror OpticStudio's merit editor; in a .zmx a row is ``OP Int1 Int2 D1 D2 D3 D4 Target Weight D5 D6`` as OpticStudio
24.1 saves it, so a lens saved on rtc goes to OpticStudio unchanged in stage 4.

评价函数值按 OpticStudio 的定义：MF² = Σ W·(V−T)² / Σ W（W > 0 的行）。边界类操作数（*GT、*LT、MN**、MX**）满足时值等于目标、
不计入，越界时取实际值。这些定义来自 OpticStudio 手册与 saos.ctx.rows 的实测说明；rtc 求得的行值尚未与 OpticStudio 逐行对账，
所以用到的操作数都进 unverified。光线类行按 (结构, 波长, 视场) 合并成一次批量追迹。
The merit value follows OpticStudio: MF² = Σ W·(V−T)² / Σ W over rows with W > 0. Boundary operands take their target
when met and their actual value otherwise. Row values computed by rtc are not yet reconciled with OpticStudio row by row.
"""

from __future__ import annotations

import math
import operator
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from saos.protocol import ErrorCode, SaosError

# 不参与评价函数值的行 / rows that never enter the merit value
INERT = frozenset({"BLNK", "DMFS"})
# 边界类：(实际值的取法, 方向)；方向 ">" 为下限、"<" 为上限 / boundary operands: (how the actual value is read, direction)
BOUNDARY = {
    "CTGT": ("CTVA", ">"), "CTLT": ("CTVA", "<"),
    "CVGT": ("CVVA", ">"), "CVLT": ("CVVA", "<"),
    "COGT": ("COVA", ">"), "COLT": ("COVA", "<"),
    "PMGT": ("PMVA", ">"), "PMLT": ("PMVA", "<"),
    "OPGT": ("OPVA", ">"), "OPLT": ("OPVA", "<"),
    "MNCT": ("center:all", ">"), "MXCT": ("center:all", "<"),
    "MNCG": ("center:glass", ">"), "MXCG": ("center:glass", "<"),
    "MNCA": ("center:air", ">"), "MXCA": ("center:air", "<"),
    "MNET": ("edge:all", ">"), "MXET": ("edge:all", "<"),
    "MNEG": ("edge:glass", ">"), "MXEG": ("edge:glass", "<"),
    "MNEA": ("edge:air", ">"), "MXEA": ("edge:air", "<"),
}
LENS_DATA = frozenset({"CTVA", "CVVA", "COVA", "PMVA"})
ARITHMETIC = frozenset({"OPVA", "SUMM", "DIFF", "PROD", "DIVI", "ABSO", "SQRT", "CONS"})
BINARY = {"SUMM": operator.add, "DIFF": operator.sub, "PROD": operator.mul, "DIVI": operator.truediv}
# 相对主光线（逐条有定义）与相对质心（按同组行求质心）的像差行 / aberrations about the chief ray and about the group centroid
CHIEF_ABERRATIONS = frozenset({"TRAR", "TRAX", "TRAY", "OPDC"})
GROUP_ABERRATIONS = frozenset({"TRAC", "TRCX", "TRCY", "OPDX", "OPDM"})
ABERRATIONS = CHIEF_ABERRATIONS | GROUP_ABERRATIONS
WAVEFRONT = frozenset({"OPDC", "OPDX", "OPDM"})
# 向导：(数据类型, 参考) → 操作数 / the wizard's (data, reference) → operands
WIZARD_OPERANDS = {
    ("wavefront", "centroid"): ("OPDX",), ("wavefront", "chiefray"): ("OPDC",), ("wavefront", "mean"): ("OPDM",),
    ("spotradius", "centroid"): ("TRAC",), ("spotradius", "chiefray"): ("TRAR",),
    ("spotx", "centroid"): ("TRCX",), ("spotx", "chiefray"): ("TRAX",),
    ("spoty", "centroid"): ("TRCY",), ("spoty", "chiefray"): ("TRAY",),
    ("spotx+y", "centroid"): ("TRCX", "TRCY"), ("spotx+y", "chiefray"): ("TRAX", "TRAY"),
}


@dataclass
class Row:
    type: str
    # Int1、Int2、Data1…Data6 / Int1, Int2, Data1–Data6
    params: list[float] = field(default_factory=lambda: [0.0] * 8)
    target: float = 0.0
    weight: float = 0.0
    comment: str = ""

    @property
    def int1(self) -> int:
        return int(self.params[0])

    @property
    def int2(self) -> int:
        return int(self.params[1])

    @property
    def data(self) -> list[float]:
        return list(self.params[2:8])


def _padded(values: list[float]) -> list[float]:
    return [float(v) for v in values] + [0.0] * (8 - len(values))


def from_specs(specs: list[dict[str, Any]]) -> list[Row]:
    """saos.ctx.rows.compile_rows 的结果 → 行。/ compile_rows output → rows."""
    return [Row(s["type"], _padded(s["params"]), float(s["target"]), float(s["weight"])) for s in specs]


def from_lens(lens) -> list[Row]:
    """rtc.zmx.Lens 的 MFE 行（数值列 Int1 Int2 D1–D4 Target Weight D5 D6）→ 行。"""
    out = []
    for k, raw in enumerate(lens.mfe, 1):
        op = raw.op.upper()
        if op == "BLNK":
            out.append(Row("BLNK", comment=raw.raw[4:].strip()))
            continue
        n = (list(raw.numbers) + [0.0] * 10)[:10]
        for column, value in enumerate(n, 1):
            if not math.isfinite(value):
                raise SaosError(ErrorCode.BAD_ARGS, f"merit row {k} {op}, column {column}: expected a finite number, got {value}")
        out.append(Row(op, [n[0], n[1], n[2], n[3], n[4], n[5], n[8], n[9]], n[6], n[7]))
    return out


def _num(v: float) -> str:
    v = float(v)
    return str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)


def to_lines(rows: list[Row]) -> list[str]:
    """行 → .zmx 的 MFE 行（OpticStudio 24.1 存盘的列序）。"""
    out = []
    for r in rows:
        if r.type == "BLNK":
            out.append(f"BLNK {r.comment}".rstrip() + " ")
            continue
        p = r.params
        values = [p[2], p[3], p[4], p[5], r.target, r.weight, p[6], p[7]]
        out.append(f"{r.type} {int(p[0])} {int(p[1])} " + " ".join(_num(v) for v in values))
    return out


def unsupported(message: str, rows: list[int] | None = None) -> SaosError:
    return SaosError(ErrorCode.BACKEND_UNSUPPORTED, message, {"rows": rows} if rows else None)


# ---- 求值 / evaluation ----

@dataclass
class Result:
    """一次求值：每行的值（边界行满足时为目标）、边界行的实际值（范围类按面列出）、算不出的行。
    One evaluation: every row's value (a met boundary row reads its target), boundary rows' actual values (per surface
    for the range kind), and the rows that could not be computed."""
    values: list[float | None]
    actual: dict[int, list[tuple[str, float]]]
    failed: dict[int, str]
    merit: float
    contributions: list[float]


@dataclass
class Segment:
    lo: int
    hi: int
    groups: dict[int, list[int]]
    pupils: dict[tuple, tuple[np.ndarray, np.ndarray, dict]]
    wavefront: set[tuple]


class Evaluator:
    """在 RtcBackend 上逐行求值；CONF 行切换后面各行的结构，结束时回到原结构。
    Evaluates rows on an RtcBackend; a CONF row switches the configuration of the rows after it."""

    def __init__(self, backend, rows: list[Row]):
        self.check(rows)
        self.backend = backend
        self.rows = rows
        self.segments = []
        lo = 0
        for k in range(len(rows) + 1):
            if k == len(rows) or rows[k].type == "CONF":
                self.segments.append(self._compile(rows, lo, k))
                lo = k + 1

    def _key(self, row):
        return (row.int2 or self.backend.lens.primary_wave, float(row.params[2]), float(row.params[3]))

    def _compile(self, rows, lo, hi):
        from saos.backend.rtc import RAY_OPERANDS

        wanted, wavefront = {}, set()
        for row in rows[lo:hi]:
            if row.type in ABERRATIONS or row.type in RAY_OPERANDS:
                key = self._key(row)
                points = wanted.setdefault(key, {(0.0, 0.0): 0})
                point = (float(row.params[4]), float(row.params[5]))
                if point not in points:
                    points[point] = len(points)
                if row.type in WAVEFRONT:
                    wavefront.add(key)
        pupils = {key: (np.array([p[0] for p in points]), np.array([p[1] for p in points]), points)
                  for key, points in wanted.items()}
        return Segment(lo, hi, _groups(rows, lo, hi), pupils, wavefront)

    # 每次求值前检查一遍能不能算，免得优化跑到一半才发现 / checked once before evaluating, not mid-optimisation
    @staticmethod
    def check(rows: list[Row]) -> None:
        from saos.backend.rtc import RAY_OPERANDS, SYSTEM_OPERANDS

        known = INERT | {"CONF"} | set(BOUNDARY) | LENS_DATA | ARITHMETIC | ABERRATIONS | RAY_OPERANDS | SYSTEM_OPERANDS | {"TTHI", "TOTR"}
        bad = [k for k, r in enumerate(rows, 1) if r.type not in known]
        if bad:
            names = sorted({rows[k - 1].type for k in bad})
            raise unsupported(f"rtc does not compute merit operands {names} (rows {bad[:20]}); it has {sorted(known - INERT)}. "
                              "Replace those rows, or run this merit function on an OpticStudio instance", bad)
        for k, r in enumerate(rows, 1):
            if r.type in ABERRATIONS and (r.int1 != 0 or r.int2 < 1):
                raise unsupported(f"row {k} {r.type}: rtc computes these rows with Int1 = 0 and one wavelength (Int2 ≥ 1); rebuild the "
                                  "rows with saos opt merit wizard on the rtc instance", [k])
            if r.type.startswith(("MNE", "MXE")) and r.data[0] != 0.0:
                raise unsupported(f"row {k} {r.type}: rtc computes edge thickness at the semi-diameter only (zone 0)", [k])
            if r.type in BOUNDARY and BOUNDARY[r.type][0] == "OPVA" or r.type in ("OPVA", "ABSO", "SQRT"):
                refs = [r.int1]
            elif r.type in ("SUMM", "DIFF", "PROD", "DIVI"):
                refs = [r.int1, r.int2]
            else:
                refs = []
            if any(not 1 <= ref < k for ref in refs):
                raise SaosError(ErrorCode.BAD_ARGS, f"row {k} {r.type} refers to rows {refs}; it may only refer to rows before it")

    def evaluate(self) -> Result:
        rows = self.rows
        backend = self.backend
        start = backend.config
        values: list[float | None] = [None] * len(rows)
        actual: dict[int, list[tuple[str, float]]] = {}
        failed: dict[int, str] = {}
        try:
            for segment in self.segments:
                self._segment(rows, segment, values, actual, failed)
                if segment.hi < len(rows):
                    values[segment.hi] = 0.0
                    backend.select_configuration(rows[segment.hi].int1)
        finally:
            backend.select_configuration(start)
        merit, contributions = merit_value(rows, values)
        return Result(values, actual, failed, merit, contributions)

    def _segment(self, rows, segment, values, actual, failed) -> None:
        """一个结构内的一段行：先把光线类行按 (波长, 视场) 合并追迹，再逐行求值。"""
        from saos.backend.rtc import RAY_OPERANDS

        traces = self._traces(segment)
        for k in range(segment.lo, segment.hi):
            r = rows[k]
            try:
                if r.type in INERT:
                    values[k] = 0.0
                elif r.type in ABERRATIONS:
                    values[k] = self._aberration(rows, k, traces, segment.groups)
                elif r.type in RAY_OPERANDS:
                    entry = traces[self._key(r)]
                    ray = entry["index"][(float(r.params[4]), float(r.params[5]))]
                    values[k] = self.backend._ray_value(r.type, self.backend._index(r.int1), entry["system"], entry["block"], ray,
                                                        tuple(r.params[2:6]))
                elif r.type in BOUNDARY:
                    how, direction = BOUNDARY[r.type]
                    found = self._actual(how, r, values)
                    actual[k] = found
                    met = all((v >= r.target) if direction == ">" else (v <= r.target) for _, v in found)
                    worst = (min if direction == ">" else max)(v for _, v in found) if found else r.target
                    values[k] = r.target if met else worst
                elif r.type in LENS_DATA:
                    values[k] = self._lens_data(r.type, r)
                elif r.type in ARITHMETIC:
                    values[k] = self._arithmetic(r, values)
                else:
                    values[k] = float(self.backend.operand(r.type, r.int1, r.int2, r.data[:4]))
            except SaosError as exc:
                if exc.code == ErrorCode.BACKEND_UNSUPPORTED:
                    raise
                failed[k] = exc.message
            except (ValueError, ZeroDivisionError, FloatingPointError) as exc:
                failed[k] = str(exc)
            if values[k] is not None and not math.isfinite(values[k]):
                failed[k] = f"{r.type} is not finite"
                values[k] = None

    # ---- 光线 / rays ----

    def _traces(self, segment) -> dict[tuple, dict[str, Any]]:
        """(波长, Hx, Hy) → 这一视场所需全部光瞳点的追迹结果（像面坐标、OPD）。"""
        from rtc.psf import opd_from_trace
        from rtc.trace import trace

        out = {}
        backend = self.backend
        waves = len(backend.lens.active_wavelengths)
        for key, (px, py, points) in segment.pupils.items():
            wave, hx, hy = key
            if not 1 <= wave <= waves:
                raise SaosError(ErrorCode.BAD_ARGS, f"merit rows use wavelength {wave}; the lens has 1–{waves}")
            system = backend._system(wave)
            try:
                block = trace(system, px, py, hx, hy)
            except NotImplementedError as exc:
                raise unsupported(f"rtc: {exc}") from exc
            image = len(system.items) - 1
            ok = (block.error == 0) & (block.vig == 0)
            entry = {"index": points, "block": block, "system": system, "x": block.x[:, image], "y": block.y[:, image], "ok": ok,
                     "chief_ok": bool(ok[0])}
            if key in segment.wavefront:
                try:
                    opd, good = opd_from_trace(system, block, chief_index=0)
                except NotImplementedError as exc:
                    raise unsupported(f"rtc: {exc}") from exc
                except ValueError as exc:
                    opd, good = np.full(len(px), np.nan), np.zeros(len(px), dtype=bool)
                    entry["opd_error"] = str(exc)
                entry["opd"] = opd / (system.wavelength * 1e-3)
                entry["opd_ok"] = good
            out[key] = entry
        return out

    def _aberration(self, rows, k, traces, groups) -> float:
        r = rows[k]
        key = self._key(r)
        entry = traces[key]
        i = entry["index"][(float(r.data[2]), float(r.data[3]))]
        if r.type in WAVEFRONT:
            if not entry["opd_ok"][i]:
                raise ValueError(entry.get("opd_error") or f"ray (Hx {key[1]}, Hy {key[2]}, Px {r.data[2]}, Py {r.data[3]}) does not reach the image")
        elif not entry["ok"][i] or not entry["chief_ok"]:
            raise ValueError(f"ray (Hx {key[1]}, Hy {key[2]}, Px {r.data[2]}, Py {r.data[3]}) or its chief ray does not reach the image")
        if r.type == "OPDC":
            return float(entry["opd"][i])
        if r.type in CHIEF_ABERRATIONS:
            dx, dy = entry["x"][i] - entry["x"][0], entry["y"][i] - entry["y"][0]
            return float({"TRAR": math.hypot(dx, dy), "TRAX": dx, "TRAY": dy}[r.type])
        group = groups[k]
        ref = _group_reference(rows, group, entry, r.type)
        if r.type in ("TRAC", "TRCX", "TRCY"):
            dx, dy = entry["x"][i] - ref[0], entry["y"][i] - ref[1]
            return float({"TRAC": math.hypot(dx, dy), "TRCX": dx, "TRCY": dy}[r.type])
        # OPDX：去掉组内加权的 piston 与倾斜；OPDM：只去均值 / OPDX removes weighted piston and tilt; OPDM the mean only
        a, bx, by = ref
        return float(entry["opd"][i] - (a + bx * r.data[2] + by * r.data[3]))

    # ---- 镜头数据 / lens data ----

    def _lens_data(self, kind: str, r: Row) -> float:
        backend = self.backend
        surface = r.int1
        if kind == "CTVA":
            return float(backend.get_param(f"{surface}.thickness"))
        if kind == "CVVA":
            return float(backend.get_param(f"{surface}.curvature"))
        if kind == "COVA":
            return float(backend.get_param(f"{surface}.conic"))
        return float(backend.get_param(f"{surface}.par{r.int2}"))

    def _actual(self, how: str, r: Row, values) -> list[tuple[str, float]]:
        if how in LENS_DATA:
            return [(f"surface {r.int1}", self._lens_data(how, r))]
        if how == "OPVA":
            v = values[r.int1 - 1]
            if v is None:
                raise ValueError(f"row {r.int1} has no value")
            return [(f"row {r.int1}", float(v))]
        part, which = how.split(":")
        return [(f"surface {i}", v) for i, v in self._thicknesses(part, which, r.int1, r.int2)]

    def _thicknesses(self, part: str, which: str, first: int, last: int) -> list[tuple[int, float]]:
        """first..last（含两端；都为 0 时为全部面）中，面后是玻璃、空气或任意介质的中心或边缘厚度。边缘取两面半口径的较大值处，
        沿 +y 方向（旋转对称面上与方向无关）。
        Centre or edge thickness after each surface first..last (both 0: every surface) whose following medium is glass,
        air or anything; edges at the larger semi-diameter of the two surfaces, along +y."""
        backend = self.backend
        lens = backend.lens
        image = len(lens.surfaces) - 1
        lo, hi = (1, image - 1) if first == 0 and last == 0 else (max(1, first), min(image - 1, last or first))
        out = []
        for i in range(lo, hi + 1):
            s = lens.surface(i)
            glass = s.material is not None
            if (which == "glass" and not glass) or (which == "air" and glass):
                continue
            t = float(lens.thickness(i, backend.config))
            if part == "center":
                out.append((i, t))
                continue
            h = max(s.semi_diameter, lens.surface(i + 1).semi_diameter)
            front, back = backend.sag(i, 0.0, h), backend.sag(i + 1, 0.0, h)
            if front is None or back is None:
                raise ValueError(f"no sag at height {h} on surface {i} or {i + 1}")
            out.append((i, t + back - front))
        return out

    @staticmethod
    def _arithmetic(r: Row, values) -> float:
        if r.type == "CONS":
            return r.target

        def v(n: int) -> float:
            value = values[n - 1]
            if value is None:
                raise ValueError(f"row {n} has no value")
            return float(value)

        if r.type == "OPVA":
            return v(r.int1)
        if r.type == "ABSO":
            return abs(v(r.int1))
        if r.type == "SQRT":
            return math.sqrt(v(r.int1))
        a, b = v(r.int1), v(r.int2)
        return BINARY[r.type](a, b)


def _groups(rows, lo, hi) -> dict[int, list[int]]:
    """相对质心的行按连续出现、同一操作数、同一波长、同一视场分组。/ Centroid rows grouped by run, operand, wavelength, field."""
    out: dict[int, list[int]] = {}
    current: list[int] = []
    for k in range(lo, hi):
        r = rows[k]
        if r.type not in GROUP_ABERRATIONS:
            current = []
            continue
        if current:
            p = rows[current[0]]
            if p.type != r.type or p.int2 != r.int2 or (p.data[0], p.data[1]) != (r.data[0], r.data[1]):
                current = []
        current.append(k)
        out[k] = current
    return out


def _group_reference(rows, group: list[int], entry, kind: str):
    """组内按行权重求的参考：TRAC/TRCX/TRCY 为质心 (x, y)；OPDX 为 piston 与倾斜 (a, bx, by)，OPDM 为均值 (a, 0, 0)。
    权重全为 0 时等权。/ The weighted group reference: the centroid for spots, piston and tilt for OPDX, the mean for OPDM."""
    cache = entry.setdefault("refs", {})
    key = (kind, group[0])
    if key in cache:
        return cache[key]
    idx = [entry["index"][(float(rows[k].data[2]), float(rows[k].data[3]))] for k in group]
    w = np.array([max(rows[k].weight, 0.0) for k in group], dtype=float)
    if not w.any():
        w = np.ones(len(group))
    px = np.array([rows[k].data[2] for k in group])
    py = np.array([rows[k].data[3] for k in group])
    if kind in ("TRAC", "TRCX", "TRCY"):
        ok = entry["ok"][idx]
        if not ok.any():
            raise ValueError("no ray of this group reaches the image")
        ref = (float(np.average(entry["x"][idx][ok], weights=w[ok])), float(np.average(entry["y"][idx][ok], weights=w[ok])))
    else:
        ok = entry["opd_ok"][idx]
        if not ok.any():
            raise ValueError(entry.get("opd_error") or "no ray of this group reaches the image")
        opd = entry["opd"][idx][ok]
        if kind == "OPDM" or ok.sum() < 3:
            ref = (float(np.average(opd, weights=w[ok])), 0.0, 0.0)
        else:
            A = np.stack([np.ones(int(ok.sum())), px[ok], py[ok]], axis=1) * np.sqrt(w[ok])[:, None]
            coef, *_ = np.linalg.lstsq(A, opd * np.sqrt(w[ok]), rcond=None)
            ref = (float(coef[0]), float(coef[1]), float(coef[2]))
    cache[key] = ref
    return ref


def merit_value(rows: list[Row], values: list[float | None]) -> tuple[float, list[float]]:
    """OpticStudio 的评价函数值与各行贡献（%）；有正权重的行算不出时为 inf。
    OpticStudio's merit value and each row's contribution (%); inf when a row with positive weight has no value."""
    total_w, total = 0.0, 0.0
    parts = [0.0] * len(rows)
    for k, r in enumerate(rows):
        if r.type in INERT or r.type == "CONF" or r.weight <= 0:
            continue
        v = values[k]
        if v is None:
            return math.inf, [0.0] * len(rows)
        parts[k] = r.weight * (v - r.target) ** 2
        total += parts[k]
        total_w += r.weight
    if total_w <= 0:
        return 0.0, parts
    contributions = [100.0 * p / total if total > 0 else 0.0 for p in parts]
    return math.sqrt(total / total_w), contributions


# ---- 优化向导 / the optimisation wizard ----

def wizard_rows(lens, settings: dict[str, Any]) -> list[Row]:
    """rtc 的序列优化向导：高斯求积光瞳采样（rings 个环，环半径 √((1+x)/2)、x 为 Gauss–Legendre 节点；每环 arms 条臂，
    假设轴对称时只取 px ≥ 0 的半个光瞳），每个视场、每个波长一组行，行权重 = 视场权重 × 波长权重 × 环权重 / 臂数；
    glass、air 给出 [最小, 最大, 边缘] 时加 MNCG/MXCG/MNEG、MNCA/MXCA/MNEA（面 1 到像面前一面）。
    环半径与 OpticStudio 24.1 存盘镜头里 4 环 8 臂向导行的光瞳坐标一致（rtc fixtures/lenses/codex-r017.zmx）；行权重未对账。
    The rtc sequential wizard: Gaussian-quadrature pupil sampling, one group per field and wavelength, optional glass and
    air boundaries. Ring radii match OpticStudio's 4-ring 8-arm wizard rows in a saved lens; the row weights are not reconciled."""
    known = {"type", "data", "reference", "rings", "arms", "glass", "air", "start_at", "assume_axial_symmetry"}
    extra = sorted(set(settings) - known)
    if extra:
        raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard settings {extra} unknown; allowed {sorted(known)}")
    kind = str(settings.get("type", "RMS")).replace(" ", "").lower()
    if kind != "rms":
        raise unsupported(f"the rtc wizard builds RMS merit functions only, got type {settings.get('type')!r}")
    data = str(settings.get("data", "Wavefront")).replace(" ", "").lower()
    reference = str(settings.get("reference", "Centroid")).replace(" ", "").lower()
    operands = WIZARD_OPERANDS.get((data, reference))
    if operands is None:
        raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard data/reference {settings.get('data')!r}/{settings.get('reference')!r} unknown; "
                        f"rtc has data Wavefront, Spot Radius, Spot X, Spot Y, Spot X+Y with reference Centroid or Chief Ray (Mean for Wavefront)")

    def integer(key: str, default: int) -> int:
        v = settings.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard {key} must be an integer from 1, got {v!r}")
        return v

    rings, arms = integer("rings", 3), integer("arms", 6)
    if arms not in (6, 8, 10):
        raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard arms must be 6, 8 or 10, got {arms!r}")
    if rings > 20:
        raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard rings must be 1–20, got {rings}")
    symmetric = settings.get("assume_axial_symmetry", True)
    if not isinstance(symmetric, bool):
        raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard assume_axial_symmetry must be true or false, got {symmetric!r}")
    bounds = {}
    for key in ("glass", "air"):
        v = settings.get(key)
        if v is not None and not (isinstance(v, list) and len(v) == 3
                                  and all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) for x in v)):
            raise SaosError(ErrorCode.BAD_ARGS, f"optimization wizard {key} must be [minimum, maximum, edge] numbers, got {v!r}")
        bounds[key] = v
    nodes, weights = np.polynomial.legendre.leggauss(rings)
    radii = np.sqrt((1.0 + nodes) / 2.0)
    ring_w = weights / 2.0
    count = arms // 2 if symmetric else arms
    angles = [math.radians(90.0 - (j + 0.5) * 360.0 / arms) for j in range(count)]
    sx = max((abs(x) for x, _, _ in lens.fields), default=0.0)
    sy = max((abs(y) for _, y, _ in lens.fields), default=0.0)
    if lens.field_normalization != "rectangular":
        sx = sy = max((math.hypot(x, y) for x, y, _ in lens.fields), default=0.0)
    rows = [Row("DMFS"), Row("BLNK", comment=f"Sequential merit function (rtc): RMS {settings.get('data', 'Wavefront')} "
                                              f"{settings.get('reference', 'Centroid')} GQ {rings} rings {arms} arms")]
    image = len(lens.surfaces) - 1
    for key, (glass_ops, air_ops) in (("glass", (("MNCG", "MXCG", "MNEG"), None)), ("air", (None, ("MNCA", "MXCA", "MNEA")))):
        v = bounds[key]
        ops = glass_ops or air_ops
        if v is None:
            continue
        rows.append(Row("BLNK", comment=f"Default {key} boundaries"))
        for op, target in zip(ops, v):
            rows.append(Row(op, _padded([1, image - 1]), float(target), 1.0))
    for f, (x, y, fw) in enumerate(lens.fields, 1):
        hx, hy = (x / sx if sx else 0.0), (y / sy if sy else 0.0)
        rows.append(Row("BLNK", comment=f"Operands for field {f}."))
        for w, (_, ww) in enumerate(lens.active_wavelengths, 1):
            for op in operands:
                # 先臂后环，同 OpticStudio 存盘的行序 / arm by arm, ring by ring within, as OpticStudio saves them
                for th in angles:
                    for rho, a in zip(radii, ring_w):
                        weight = float(fw) * float(ww) * float(a) / count
                        rows.append(Row(op, _padded([0, w, hx, hy, rho * math.cos(th), rho * math.sin(th)]), 0.0, weight))
    return rows
