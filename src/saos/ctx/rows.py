"""纯数据的评价函数构造；行号由追加位置解析，避免复制脚本后引用错行。"""

from __future__ import annotations

import math


def number(value, name, *, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if nonnegative and value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


def integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def compile_rows(rows, start):
    if not isinstance(rows, list) or len(rows) > 10000:
        raise ValueError("merit rows must be a list of at most 10000 rows")
    integer(start, "start row", 1)
    result, names = [], {}
    for offset, source in enumerate(rows):
        if not isinstance(source, dict) or set(source) - {"id", "type", "params", "int1", "int2", "data", "target", "weight"}:
            raise ValueError("a merit row may only have id, type, params, int1, int2, data, target and weight")
        row = start + offset
        kind = source.get("type")
        if not isinstance(kind, str) or len(kind) != 4 or not kind.isascii() or not kind.isalnum():
            raise ValueError("an operand type is 4 letters or digits")
        if "params" in source and any(k in source for k in ("int1", "int2", "data")):
            raise ValueError("give either params or int1/int2/data, not both")
        data = source.get("data", [])
        if not isinstance(data, list) or len(data) > 6:
            raise ValueError("data is a list of at most 6 values")
        params = source.get("params", [source.get("int1", 0), source.get("int2", 0), *data])
        if not isinstance(params, list) or len(params) > 8:
            raise ValueError("params is a list of at most 8 values")
        values = []
        for value in params:
            if isinstance(value, dict):
                if set(value) != {"ref"} or not isinstance(value["ref"], str) or value["ref"] not in names:
                    raise ValueError("{\"ref\": id} must name an earlier row of the same batch")
                value = names[value["ref"]]
            values.append(number(value, "an operand parameter"))
        identity = source.get("id")
        if identity is not None:
            if not isinstance(identity, str) or not identity or identity in names:
                raise ValueError("a row id must be a non-empty string used once")
            names[identity] = row
        result.append({"type": kind.upper(), "params": values,
                       "target": number(source.get("target", 0), "target"),
                       "weight": number(source.get("weight", 1), "weight", nonnegative=True)})
    return result


def thickness_bounds(surface, *, minimum=None, maximum=None, weight=1):
    """CTGT/CTLT rows bounding the centre thickness of a surface. / 中心厚度上下限（CTGT/CTLT）。"""
    integer(surface, "surface")
    number(weight, "weight", nonnegative=True)
    if minimum is None and maximum is None:
        raise ValueError("give minimum, maximum or both")
    for value in (minimum, maximum):
        if value is not None:
            number(value, "a thickness bound")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("minimum must not exceed maximum")
    return [{"type": kind, "params": [surface], "target": value, "weight": weight}
            for kind, value in (("CTGT", minimum), ("CTLT", maximum)) if value is not None]


def sag_gap(front, back, samples, *, minimum=0, weight=1):
    """相邻、共轴、同局部坐标系两面在采样点处的间隙：front 的厚度（TTHI，随优化变化）+ 后面矢高 − 前面矢高 ≥ minimum。
    只约束给定采样点，不是全口径无交叠证明。"""
    integer(front, "front surface")
    if back != front + 1 or isinstance(back, bool):
        raise ValueError("sag_gap needs adjacent surfaces (back = front + 1)")
    number(minimum, "minimum gap")
    number(weight, "weight", nonnegative=True)
    if not isinstance(samples, list) or not 1 <= len(samples) <= 2500:
        raise ValueError("samples is a list of 1-2500 [x, y] points")
    thickness = f"gap_{front}_{back}_t"
    rows = [{"id": thickness, "type": "TTHI", "params": [front, front], "weight": 0}]
    for i, point in enumerate(samples):
        if not isinstance(point, (tuple, list)) or len(point) != 2:
            raise ValueError("a sample point is [x, y]")
        x, y = [number(v, "a sample coordinate") for v in point]
        a, b, diff, gap = (f"gap_{front}_{back}_{i}_{k}" for k in ("a", "b", "diff", "gap"))
        rows.extend([
            {"id": a, "type": "SSAG", "params": [front, 0, x, y], "weight": 0},
            {"id": b, "type": "SSAG", "params": [back, 0, x, y], "weight": 0},
            {"id": diff, "type": "DIFF", "params": [{"ref": b}, {"ref": a}], "weight": 0},
            {"id": gap, "type": "SUMM", "params": [{"ref": thickness}, {"ref": diff}], "weight": 0},
            {"type": "OPGT", "params": [{"ref": gap}], "target": minimum, "weight": weight},
        ])
    return rows


def na_proxy(surface, wave, *, index, target, minimum=None, axis="y", weight=1, bound_weight=100):
    """轴上正方向边缘光线的方向余弦代理；折射率必须固定，不替代真实 NA 判定。"""
    integer(surface, "surface")
    integer(wave, "wavelength", 1)
    number(index, "index")
    number(target, "target NA", nonnegative=True)
    if index <= 0 or target > index or axis not in ("x", "y"):
        raise ValueError("needs index > 0, target NA <= index and axis x or y")
    number(weight, "weight", nonnegative=True)
    number(bound_weight, "bound_weight", nonnegative=True)
    identity = f"na_{surface}_{wave}_{axis}"
    rows = [{"id": identity, "type": "RAGA" if axis == "x" else "RAGB",
             "params": [surface, wave, 0, 0, int(axis == "x"), int(axis == "y")],
             "target": target / index, "weight": weight}]
    if minimum is not None:
        number(minimum, "minimum NA", nonnegative=True)
        if minimum > target:
            raise ValueError("minimum NA must not exceed the target NA")
        rows.append({"type": "OPGT", "params": [{"ref": identity}], "target": minimum / index, "weight": bound_weight})
    return rows


# 以下约束的种类与 DeepLens（vccimaging/DeepLens，Apache-2.0）geolens_pkg/optim.py 的正则项对应，只借鉴约束集合，不含其代码；
# 阈值一律由调用方给出（DeepLens 的手机/相机默认值只是参考，不是本任务的依据）。操作数语义在 2024 R1 上实测（2026-10-27）。

def _bound_rows(kinds, bounds, params, weight):
    rows = []
    for (kind_min, kind_max), pair in zip(kinds, bounds):
        if pair is None:
            continue
        if not isinstance(pair, (tuple, list)) or len(pair) != 2:
            raise ValueError("a bound is (minimum, maximum); use None for an open side")
        low, high = pair
        for value in (low, high):
            if value is not None:
                number(value, "a bound", nonnegative=True)
        if low is not None and high is not None and low > high:
            raise ValueError("minimum must not exceed maximum")
        rows += [{"type": kind, "params": list(params), "target": value, "weight": weight}
                 for kind, value in ((kind_min, low), (kind_max, high)) if value is not None]
    return rows


def clearances(first, last, *, glass_center=None, glass_edge=None, air_center=None, air_edge=None, weight=1):
    """first..last 各面玻璃与空气的中心、边缘厚度上下限，各项为 (min, max)，None 表示不限。用 MNCG/MXCG、MNEG/MXEG、
    MNCA/MXCA、MNEA/MXEA：满足时取值等于目标，越界时取实际值，所以只在越界时计入评价函数；边缘在各面半口径处（Zone 0）。"""
    integer(first, "first surface", 1)
    integer(last, "last surface", 1)
    number(weight, "weight", nonnegative=True)
    if last < first:
        raise ValueError("last surface must not precede first")
    rows = _bound_rows([("MNCG", "MXCG"), ("MNEG", "MXEG"), ("MNCA", "MXCA"), ("MNEA", "MXEA")],
                       [glass_center, glass_edge, air_center, air_edge], (first, last), weight)
    if not rows:
        raise ValueError("give at least one of glass_center, glass_edge, air_center, air_edge")
    return rows


def total_track(*, minimum=None, maximum=None, weight=1):
    """总长（TOTR：1 号面到像面的厚度之和）的上下限。"""
    number(weight, "weight", nonnegative=True)
    rows = _bound_rows([("OPGT", "OPLT")], [(minimum, maximum)], (), weight)
    if not rows:
        raise ValueError("give minimum, maximum or both")
    for row in rows:
        row["params"] = [{"ref": "total_track"}]
    return [{"id": "total_track", "type": "TOTR", "weight": 0}, *rows]


def _fields(fields):
    if not isinstance(fields, list) or not 1 <= len(fields) <= 100:
        raise ValueError("fields is a list of 1-100 normalised field heights Hy")
    return [number(h, "a field height") for h in fields]


def distortion(wave, fields, *, maximum, reference_field=1, weight=1):
    """各归一化视场 Hy 处的畸变（DISG，百分比，带符号）限制在 ±maximum 内。DIST 只给最大视场一点，峰值可能在中间视场，
    所以逐点采样。DISG 的第一个参数是参考视场号（从 1 起），畸变在参考视场处为零；参考视场在轴上时，DISG 等于
    (实际 − 近轴)/近轴 主光线高度，Hy=1 处也等于 DIST（2024 R1 实测）。默认 1 假定 1 号视场在轴上，否则须给出轴上视场号。"""
    integer(wave, "wavelength", 1)
    integer(reference_field, "reference field", 1)
    number(maximum, "maximum distortion (%)", nonnegative=True)
    number(weight, "weight", nonnegative=True)
    rows = []
    for i, hy in enumerate(_fields(fields)):
        name = f"distortion_{wave}_{i}"
        rows += [{"id": name, "type": "DISG", "params": [reference_field, wave, 0, hy, 0, 0], "weight": 0},
                 {"type": "OPLT", "params": [{"ref": name}], "target": maximum, "weight": weight},
                 {"type": "OPGT", "params": [{"ref": name}], "target": -maximum, "weight": weight}]
    return rows


def chief_ray_angle(image, wave, fields, *, maximum, weight=1):
    """各归一化视场 Hy 的主光线在像面上的入射角（RAID，度，不带符号）不超过 maximum。image 是像面序号。"""
    integer(image, "image surface", 1)
    integer(wave, "wavelength", 1)
    number(maximum, "maximum angle (deg)", nonnegative=True)
    number(weight, "weight", nonnegative=True)
    rows = []
    for i, hy in enumerate(_fields(fields)):
        name = f"cra_{wave}_{i}"
        rows += [{"id": name, "type": "RAID", "params": [image, wave, 0, hy, 0, 0], "weight": 0},
                 {"type": "OPLT", "params": [{"ref": name}], "target": maximum, "weight": weight}]
    return rows


def ray_bend(surfaces, wave, rays, *, maximum, weight=1):
    """给定光线在每个折射面上的偏折角 |RAID − RAED|（度）不超过 maximum；rays 为 (Hx, Hy, Px, Py)。入射角与出射角都相对
    面法线、在同一入射面内，所以二者之差就是偏折角（2024 R1 上与追迹方向夹角一致到 1e-6）。不适用于反射面。"""
    if not isinstance(surfaces, list) or not surfaces:
        raise ValueError("surfaces is a non-empty list of surface numbers")
    for s in surfaces:
        integer(s, "surface", 1)
    integer(wave, "wavelength", 1)
    number(maximum, "maximum bend (deg)", nonnegative=True)
    number(weight, "weight", nonnegative=True)
    if not isinstance(rays, list) or not 1 <= len(rays) <= 100:
        raise ValueError("rays is a list of 1-100 (Hx, Hy, Px, Py)")
    rows = []
    for j, ray in enumerate(rays):
        if not isinstance(ray, (tuple, list)) or len(ray) != 4:
            raise ValueError("a ray is (Hx, Hy, Px, Py)")
        hx, hy, px, py = [number(v, "a ray coordinate") for v in ray]
        for s in surfaces:
            base = f"bend_{s}_{j}"
            rows += [{"id": base + "_in", "type": "RAID", "params": [s, wave, hx, hy, px, py], "weight": 0},
                     {"id": base + "_out", "type": "RAED", "params": [s, wave, hx, hy, px, py], "weight": 0},
                     {"id": base + "_diff", "type": "DIFF", "params": [{"ref": base + "_in"}, {"ref": base + "_out"}], "weight": 0},
                     {"id": base, "type": "ABSO", "params": [{"ref": base + "_diff"}], "weight": 0},
                     {"type": "OPLT", "params": [{"ref": base}], "target": maximum, "weight": weight}]
    return rows
