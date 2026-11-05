"""OpticStudio ``.zmx`` 读取：只解析复现范围内用到的字段。

设计原则：
- 只读。绝不写回 ``.zmx``（权威文件由 OpticStudio 保存）。
- 未识别的关键字一律保留在 ``Surface.raw`` / ``Lens.raw`` 里，不静默丢弃，
  这样扫描脚本能如实报告"文件里存在但我没处理"的东西。
- XDAT 表按 ``XDAT_i == 参数(12+i)`` 处理（见 docs/plans/rtc/SURFACE-PARAMS.md），
  这是从三件镜头里 BICONICZ / TOROIDAL 的取值反推出来的，仍需 SSAG 实测确认。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path

from .textio import decode_text
from .material.types import Catalog, Environment, MaterialSpec, MaterialUnsupported, Mirror, Model


# ---------------------------------------------------------------- 解码


def decode_zmx_bytes(data: bytes) -> str:
    """ZOS 保存的 .zmx 是 UTF-16LE + BOM；兼容 UTF-8 与纯 ASCII 变体。"""
    return decode_text(data)


# ---------------------------------------------------------------- 数据模型


@dataclass
class Surface:
    index: int
    type: str = "STANDARD"
    curvature: float = 0.0          # 1/mm，等于 LDE "半径" 的倒数
    conic: float = 0.0              # LDE "圆锥系数" 列
    thickness: float = 0.0
    glass: str = ""                 # 目录玻璃名；模型玻璃为 ""（见 model_nd/model_vd）
    model_nd: float | None = None   # ___BLANK 时 nd
    model_vd: float | None = None   # ___BLANK 时 Vd
    comment: str = ""
    semi_diameter: float = 0.0       # DIAM（通光半口径）
    mech_semi_diameter: float = 0.0  # MEMA（机械半口径）
    #: DIAM / MEMA 第 2 列：1 为用户固定，0 为 OpticStudio 自动计算（存盘值是保存时算出的）
    semi_fixed: bool = False
    #: rtc 内存变量；原生文件只保存半口径数值，未猜写无存盘样本的变量求解。
    semi_variable: bool = False
    mech_fixed: bool = False
    is_stop: bool = False
    # ---- 面孔径（"Surface Properties > Aperture"）----
    #: 孔径类型码，按手册 §14.46 的表：
    #: 0 无 / 1 圆孔 / 2 圆遮挡 / 3 蜘蛛 / 4 矩形孔 / 5 矩形遮挡 /
    #: 6 椭圆孔 / 7 椭圆遮挡 / 8 用户孔 / 9 用户遮挡 / 10 浮动孔径。
    aperture_code: int = 0
    #: 第一个半口径槽位（按孔径类型复用）：
    #: 圆孔=最小半径、蜘蛛=臂宽、矩形/椭圆= x 半宽、浮动孔径= 半口径下限。
    ap_min: float = 0.0
    #: 第二个半口径槽位：圆孔=最大半径、蜘蛛=臂数、矩形/椭圆= y 半宽。
    ap_max: float = 0.0
    #: 孔径中心偏移。
    ap_dx: float = 0.0
    ap_dy: float = 0.0
    params: dict[int, float] = field(default_factory=dict)       # PARM n
    extra: dict[int, float] = field(default_factory=dict)        # XDAT n（= 参数 12+n）
    variable_params: set[int] = field(default_factory=set)       # VPAR
    #: ``CURV`` 行的第 2 列（半径列的求解类型）：0 固定 / 1 变量（30 个轮次里只有这两种）
    solve_radius: int = 0
    #: ``DISZ`` 行的第 2 列（厚度列的求解类型）：0 固定 / 1 变量。
    #: 30 个轮次里的 DISZ 全部只有一列，所以这条路径没有样本；按 LDE 的通用写法解析。
    solve_thickness: int = 0
    #: ``GLAS MIRROR``：反射面（材料列写 mirror）。
    mirror: bool = False
    raw: dict[str, str] = field(default_factory=dict)
    model_dpgf: float = 0.0

    @property
    def radius(self) -> float:
        return float("inf") if self.curvature == 0.0 else 1.0 / self.curvature

    @property
    def material(self) -> MaterialSpec:
        # 旧构造参数继续可用；统一视图不另存一份可失同步的材料状态。
        if self.mirror:
            return Mirror()
        if self.model_nd is not None:
            if self.model_vd is None:
                raise MaterialUnsupported(f"面 {self.index}: 模型玻璃缺少 Vd")
            return Model(self.model_nd, self.model_vd, self.model_dpgf)
        return Catalog(self.glass) if self.glass else None

    @material.setter
    def material(self, spec: MaterialSpec):
        if isinstance(spec, Mirror):
            self.glass, self.model_nd, self.model_vd, self.model_dpgf = "", None, None, 0.0
            self.mirror = True
        elif isinstance(spec, Model):
            self.glass, self.model_nd, self.model_vd, self.model_dpgf = "", spec.nd, spec.vd, spec.dpgf
            self.mirror = False
        elif isinstance(spec, Catalog) or spec is None:
            self.glass = spec.name if spec else ""
            self.model_nd, self.model_vd, self.model_dpgf = None, None, 0.0
            self.mirror = False
        else:
            raise MaterialUnsupported(f"面 {self.index}: 材料描述类型无效")


@dataclass
class MCEOperand:
    op: str
    surface: int
    config: int
    value: float
    #: 值单元格之后的求解数据列（原样保留，列语义只部分标定）。三件最终镜头的 THIC 都没有求解；
    #: 中间轮次出现过 pickup（scale −1 / offset 0.1），见 docs/plans/rtc/GAP-ANALYSIS.md「多重结构」。
    solve: list[float] = field(default_factory=list)
    raw: str = ""
    #: pickup 的 (scale, offset)：值 = scale·源厚度 + offset。
    #: 只有 scale/offset 偏离 (1, 0) 才算 pickup——这个判据来自实测的 30 个轮次
    #: （固定行的列是 ``1.0 0.0``，pickup 行是 ``-1.0 0.1``），比猜 solve 类型码更可靠。
    pickup: tuple[float, float] | None = None
    #: pickup 的源（面号, 结构号）。面号取 ``solve[4]``；**结构号一律同当前结构**：
    #: ``solve[3]`` 在 r003 随结构变、在 r004 恒为 1，而两份文件实测的取值关系都是
    #: "源自同结构"，所以那一列不参与计算（只在 ``solve`` 里留原样）。
    pickup_source: tuple[int, int] | None = None


@dataclass
class MFERow:
    op: str
    numbers: list[float] = field(default_factory=list)  # 操作数名之外的数值列
    raw: str = ""


@dataclass
class Lens:
    path: str = ""
    name: str = ""
    units: str = "MM"
    aperture_type: str = ""      # "EPD" | "OBNA" | "FLOA"（由光阑半口径定孔径，文件里只有一行 FLOA）
    aperture_value: float = 0.0
    fields: list[tuple[float, float, float]] = field(default_factory=list)  # (x, y, weight)，只含有效视场
    #: 各视场的渐晕因子 (VDX, VDY, VCX, VCY, VAN)，与 ``fields`` 一一对应；VDX… 是入瞳的偏心与压缩比，VAN 是切向角。
    vignetting: list[tuple[float, float, float, float, float]] = field(default_factory=list)
    field_type: str = "ANGLE"
    #: 视场归一化（``FTYP`` 第 5 列：0 径向 / 1 矩形；OpticStudio 24.1 存盘实测）。
    field_normalization: str = "radial"
    #: ``FTYP`` 第 3、4 列：有效视场数、有效波长数（WAVM 会多存默认槽位，不能按行数算）。
    num_fields: int = 0
    num_waves: int = 0
    wavelengths: list[tuple[float, float]] = field(default_factory=list)  # (um, weight)，含默认槽位
    primary_wave: int = 1
    ray_aiming: int = 0          # 0 关 / 1 近轴 / 2 实际（RAIM 第 2 列）
    pupil_shift: tuple[float, ...] = ()
    surfaces: list[Surface] = field(default_factory=list)
    num_configs: int = 1
    #: 保存文件时选中的结构（``MNUM`` 第 2 个数）。实测：piA 1 / piB 1 / codex 5，
    #: 与 golden ``info.csv`` 的 ``current_config`` 逐位一致（docs/plans/rtc/HANDOFF.md §6.4 说的就是这个）。
    current_config: int = 1
    mce: list[MCEOperand] = field(default_factory=list)
    mfe: list[MFERow] = field(default_factory=list)
    raw: dict[str, str] = field(default_factory=dict)
    environment: Environment = field(default_factory=Environment)

    @property
    def catalogs(self) -> list[str]:
        """``GCAT`` 行列出的玻璃目录，按 OpticStudio 查找牌号的顺序。"""
        return self.raw.get("GCAT", "").split()[1:]

    @property
    def active_wavelengths(self) -> list[tuple[float, float]]:
        """有效波长 (µm, 权重)：``FTYP`` 给出个数时只取前 N 个槽位。"""
        return self.wavelengths[: self.num_waves] if self.num_waves else self.wavelengths

    def surface(self, index: int) -> Surface:
        for s in self.surfaces:
            if s.index == index:
                return s
        raise KeyError(f"没有第 {index} 面")

    @property
    def stop_index(self) -> int | None:
        for s in self.surfaces:
            if s.is_stop:
                return s.index
        return None

    def thickness(self, index: int, config: int = 1, _seen=None) -> float:
        """多重结构只覆盖厚度（THIC），其余一律取 LDE。

        pickup（``scale/offset``）按 ``值 = scale·源厚度 + offset`` 求解。源面号取
        ``solve[4]``，源结构固定为当前结构（见 :attr:`MCEOperand.pickup_source`）。
        源可以再是 pickup，递归求解并检测环。
        """
        base = self.surface(index).thickness
        for row in self.mce:
            if row.op == "THIC" and row.surface == index and row.config == config:
                if row.pickup is None:
                    return row.value
                seen = set() if _seen is None else _seen
                key = (index, config)
                if key in seen:
                    raise ValueError(f"MCE pickup 成环：面 {index} 结构 {config}")
                seen.add(key)
                src_surf, src_cfg = row.pickup_source or (index, config)
                scale, offset = row.pickup
                if (src_surf, src_cfg) == key:
                    # 源自指：按 LDE 基准值求解（否则无解）
                    return scale * base + offset
                return scale * self.thickness(src_surf, src_cfg, seen) + offset
        return base


# ---------------------------------------------------------------- 解析

# LDE 之前出现的顶层关键字；第一个 SURF 之后出现的非缩进行一律视为 MFE 行。
_HEADER_KEYS = {
    "VERS", "MODE", "NAME", "AUTH", "IWDP", "PFIL", "LANG", "UNIT", "ENPD", "ENVD",
    "GFAC", "GCAT", "RAIM", "PUSH", "SDMA", "OMMA", "FTYP", "ROPD", "HYPR", "PICB",
    "XFLN", "YFLN", "FWGN", "VDXN", "VDYN", "VCXN", "VCYN", "VANN", "WAVM", "PWAV",
    "POLS", "GLRS", "GSTD", "NSCD", "COFN", "LUID", "OBNA", "XFLD", "YFLD", "MNUM",
    "MOFF", "MCFG", "TOL", "BLNK", "MKV", "PROT", "JUPN", "USTN", "MCON",
}

# 面孔径行 → 类型码（手册 §14.46 的编号），只收有 OpticStudio 24.1 存盘样本的类型（tests/fixtures/cases/truth.json）：
#   OBSC 0 8 0   圆遮挡       SQAP 3 4 0  矩形孔       SQOB 3 4 0  矩形遮挡
#   ELAP 3 4 0   椭圆孔       ELOB 3 4 0  椭圆遮挡     SPID 0.5 4 0  蜘蛛（臂宽、臂数）
#   OBDC dx dy 是孔径偏心行
# 读入不等于放行：saos 的准入检查按类型码拒绝未对账的类型。
APERTURE_LINES = {"OBSC": 2, "SPID": 3, "SQAP": 4, "SQOB": 5, "ELAP": 6, "ELOB": 7}
# 渐晕因子在文件头按视场逐列排开 / vignetting factors, one column per field
VIGNETTING_KEYS = ("VDXN", "VDYN", "VCXN", "VCYN", "VANN")


def _nums(parts: list[str]) -> list[float]:
    out: list[float] = []
    for tok in parts:
        try:
            out.append(float(tok))
        except ValueError:
            out.append(float("nan"))
    return out


def parse(path: str | Path) -> Lens:
    p = Path(path)
    return parse_text(decode_zmx_bytes(p.read_bytes()), path=str(p))


def parse_text(text: str, path: str = "") -> Lens:
    lens = Lens(path=path)

    cur: Surface | None = None
    in_mce = False
    seen_surface = False
    mce_row = re.compile(r"^[A-Z][A-Z0-9_]{3}\s+\d+\s+\d+")

    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        indented = raw_line[:1] in (" ", "\t")
        parts = raw_line.split()
        key = parts[0]

        # ---- 面块内部（缩进行）----
        if indented:
            if cur is None:
                continue
            if key == "TYPE":
                cur.type = parts[1]
            elif key == "CURV":
                cur.curvature = float(parts[1])
                if len(parts) > 2:
                    try:
                        cur.solve_radius = int(float(parts[2]))
                    except ValueError:
                        cur.solve_radius = 0
            elif key == "CONI":
                cur.conic = float(parts[1])
            elif key == "DISZ":
                cur.thickness = float(parts[1])
                if len(parts) > 2:
                    try:
                        cur.solve_thickness = int(float(parts[2]))
                    except ValueError:
                        cur.solve_thickness = 0
            elif key == "GLAS":
                if parts[1].upper() == "MIRROR":
                    cur.material = Mirror()
                elif parts[1].upper() == "___BLANK":
                    if len(parts) < 6:
                        raise MaterialUnsupported(f"面 {cur.index}: GLAS 模型缺少 nd/Vd")
                    cur.material = Model(float(parts[4]), float(parts[5]), float(parts[6]) if len(parts) > 6 else 0.0)
                else:
                    cur.material = Catalog(parts[1])
            elif key == "DIAM":
                cur.semi_diameter = float(parts[1])
                cur.semi_fixed = len(parts) > 2 and parts[2] == "1"
            elif key == "MEMA":
                cur.mech_semi_diameter = float(parts[1])
                cur.mech_fixed = len(parts) > 2 and parts[2] == "1"
            elif key == "COMM":
                cur.comment = raw_line.strip()[len("COMM"):].strip()
            elif key == "PARM":
                cur.params[int(parts[1])] = float(parts[2])
            elif key == "XDAT":
                # XDAT 是"参数 12 以后"的原样存储（见 docs/plans/rtc/SURFACE-PARAMS.md）：
                # 同时写进 params，面型代码只认 params，extra 留作原始记录。
                n = int(parts[1])
                v = float(parts[2])
                cur.extra[n] = v
                cur.params.setdefault(12 + n, v)
            elif key == "VPAR":
                cur.variable_params.add(int(parts[1]))
            elif key == "STOP":
                cur.is_stop = True
            elif key == "CLAP" or key == "FLAP":
                # 面孔径。实测格式（OpticStudio 24.1 存盘的例子文件，tests/rtc/fixtures 的样本同样如此）：
                #   CLAP 0 0.125 0   ← 圆孔：最小半径、最大半径、保留位
                #   FLAP 0 0.115 0   ← 浮动孔径：中间那个数与 DIAM 相同
                # 其后的数（偏移）在样本里恒为 0，孔径偏心由单独的 OBDC 行给出。
                nums = _nums(parts[1:])
                cur.aperture_code = 10 if key == "FLAP" else 1
                cur.ap_min = nums[0] if len(nums) > 0 else 0.0
                cur.ap_max = nums[1] if len(nums) > 1 else cur.semi_diameter
                cur.raw[key] = raw_line.strip()
            elif key in APERTURE_LINES:
                cur.aperture_code = APERTURE_LINES[key]
                nums = _nums(parts[1:])
                cur.ap_min = nums[0] if len(nums) > 0 else 0.0
                cur.ap_max = nums[1] if len(nums) > 1 else 0.0
                cur.raw[key] = raw_line.strip()
            elif key == "OBDC":
                nums = _nums(parts[1:])
                cur.ap_dx = nums[0] if len(nums) > 0 else 0.0
                cur.ap_dy = nums[1] if len(nums) > 1 else 0.0
                cur.raw[key] = raw_line.strip()
            else:
                cur.raw[key] = raw_line.strip()
            continue

        # ---- MCE 行 ----
        if in_mce and mce_row.match(raw_line):
            nums = _nums(parts[3:])
            solve = nums[1:]
            row = MCEOperand(
                op=key,
                surface=int(parts[1]),
                config=int(parts[2]),
                value=nums[0] if nums else float("nan"),
                solve=solve,
                raw=raw_line.strip(),
            )
            # pickup 判据：``solve[5]``/``solve[6]`` 是 (scale, offset)，固定行是 (1, 0)。
            if len(solve) >= 7:
                scale, offset = solve[5], solve[6]
                if (scale, offset) != (1.0, 0.0) and isfinite(scale) and isfinite(offset):
                    row.pickup = (scale, offset)
                    # 源面号取 solve[4]。**源结构号不取 solve[3]**：r003 的 solve[3] 随结构
                    # 变（1,2,3）、r004 恒为 1，但两边实测的取值关系都是"源自同结构"
                    # （codex r003/r004：−THIC(1, 同结构) + 0.1 逐位符），所以按同结构解。
                    src_surf = int(solve[4]) if len(solve) > 4 and isfinite(solve[4]) else row.surface
                    row.pickup_source = (src_surf, row.config)
            lens.mce.append(row)
            continue

        # ---- 面块开始 ----
        if key == "SURF":
            cur = Surface(index=int(parts[1]))
            lens.surfaces.append(cur)
            seen_surface = True
            in_mce = False
            continue

        # ---- 顶层关键字 ----
        if key == "NAME":
            lens.name = raw_line.strip()[len("NAME"):].strip()
        elif key == "UNIT":
            lens.units = parts[1]
        elif key == "ENPD":
            lens.aperture_type, lens.aperture_value = "EPD", float(parts[1])
        elif key == "FLOA":
            # Float By Stop Size：行内没有数值，孔径由光阑面的半口径定（手册 §2.1.1.1）
            lens.aperture_type, lens.aperture_value = "FLOA", 0.0
        elif key == "OBNA":
            lens.aperture_type, lens.aperture_value = "OBNA", float(parts[1])
        elif key == "FTYP":
            lens.field_type = "ANGLE" if parts[1] == "0" else f"OP{parts[1]}"
            nums = _nums(parts[1:])
            if len(nums) > 3:
                lens.num_fields, lens.num_waves = int(nums[2]), int(nums[3])
            if len(nums) > 4:
                lens.field_normalization = "rectangular" if int(nums[4]) == 1 else "radial"
            lens.raw[key] = raw_line.strip()
        elif key in ("XFLN", "YFLN", "FWGN"):
            lens.raw[key] = raw_line.strip()
        elif key == "WAVM":
            lens.wavelengths.append((float(parts[2]), float(parts[3])))
        elif key == "PWAV":
            lens.primary_wave = int(parts[1])
        elif key == "RAIM":
            lens.ray_aiming = int(parts[2])
        elif key == "PUSH":
            lens.pupil_shift = tuple(float(v) for v in parts[1:])
        elif key == "MNUM":
            # MNUM <结构数> <当前结构>：第二个数就是保存文件时选中的结构（实测 piA 1 / codex 5，
            # 与 golden info.csv 的 current_config 一致），不是行数。
            lens.num_configs = int(parts[1])
            lens.current_config = int(parts[2]) if len(parts) > 2 else 1
            in_mce = True
            continue
        elif key == "STOP":
            if cur is not None:
                cur.is_stop = True
        elif key == "BLNK" and seen_surface and not in_mce:
            # 面之后的 BLNK 是评价函数的空行或注释行：要占一个行号，OPGT、DIFF 等按行号引用
            # a BLNK after the surfaces is a merit row (blank or comment); it holds a row number that OPGT, DIFF, … refer to
            lens.mfe.append(MFERow(op=key, numbers=[], raw=raw_line.strip()))
        elif key in _HEADER_KEYS:
            lens.raw[key] = raw_line.strip()
        elif seen_surface:
            # 第一个 SURF 之后的非缩进行 = MFE 操作数
            lens.mfe.append(MFERow(op=key, numbers=_nums(parts[1:]), raw=raw_line.strip()))
        else:
            lens.raw[key] = raw_line.strip()

    _fields_from_raw(lens)
    _environment_from_raw(lens)
    return lens


def _environment_from_raw(lens: Lens) -> None:
    fields = lens.raw.get("ENVD", "").split()
    if not fields:
        return
    if len(fields) < 3:
        raise MaterialUnsupported("ENVD 至少须包含温度和压力")
    try:
        t, p = float(fields[1]), float(fields[2])
        if len(fields) > 3:
            adjust = int(fields[3]) != 0
        else:
            versions = lens.raw.get("VERS", "").split()
            if len(versions) < 2:
                raise MaterialUnsupported("缺少 ENVD 开关时须有 VERS，不能猜测修正状态")
            version = int(versions[1])
            adjust = version < 50915 or t != 20.0 or p != 1.0 or any(row.op in ("TEMP", "PRES") for row in lens.mce)
        lens.environment = Environment(t, p, adjust)
    except (ValueError, OverflowError) as exc:
        raise MaterialUnsupported("ENVD/VERS 数值格式无效") from exc


def _fields_from_raw(lens: Lens) -> None:
    """XFLN/YFLN/FWGN 三行按列组成视场表，只取 ``FTYP`` 声明的个数。"""
    xs = _nums(lens.raw.get("XFLN", "").split()[1:])
    ys = _nums(lens.raw.get("YFLN", "").split()[1:])
    ws = _nums(lens.raw.get("FWGN", "").split()[1:])
    count = lens.num_fields or max(len(xs), len(ys), 1)

    def pick(vals: list[float], i: int, default: float) -> float:
        return vals[i] if i < len(vals) else default

    lens.fields = [(pick(xs, i, 0.0), pick(ys, i, 0.0), pick(ws, i, 1.0)) for i in range(count)]
    columns = [_nums(lens.raw.get(key, "").split()[1:]) for key in VIGNETTING_KEYS]
    lens.vignetting = [tuple(pick(col, i, 0.0) for col in columns) for i in range(count)]
