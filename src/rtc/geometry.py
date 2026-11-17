r"""面型：把 .zmx 的面参数换成内核的参数行，矢高和求交都在 rtc.kernel 里只有一份。

参数编号按手册（对照表见下文）。面型类只回答内核不管的事：顶点曲率、
是否对过账、OPD 能不能算。``vertex_curvature`` 可以吃 Dual，光瞳对设计变量的导数要用。

面型参数对照表：手 parN ↔ OpticStudio 参数表 ↔ .zmx 字段
====================

规则：**参数语义只从 OpticStudio 手册的参数表和随文件保存的原始数据推断**。

手册出处为安装目录附带手册的纯文本提取（`manual.txt`，章节号即手册章节号）。

## 面型 → 参数

### Standard（§2.3.1.2，LDE 列）

| 量 | 来源 |
|---|---|
| 曲率 c | LDE "半径" 列，`.zmx` `CURV` |
| 圆锥系数 k | LDE "圆锥系数" 列，`.zmx` `CONI` |

`z = cρ²/(1+√(1−(1+k)c²ρ²))`。
**已实测**：`ORACLE.md` §2 的 SSAG 对照（R=50，h=1 与 h=6 一致到 1e-15）。

### Even Asphere（§2.3.1.2.24）

| 参数 | 含义 | `.zmx` |
|---|---|---|
| 1–8 | ρ²、ρ⁴ … ρ¹⁶ 的系数 α₂…α₁₆（**无归一化半径**） | `PARM 1..8` |

**已实测**（差分探测，`oracle/campaign_EvenAspheric.json`）：8 个参数逐个扰动，
幂次拟合得到 2.0000 / 4.0000 / … / 16.0000，缩放因子全部 = 1.0e-3（即扰动值本身），
证明无归一化。手册参数表同样只写 "1–8"。

### Biconic（`.zmx` 里写作 `BICONICX`，§2.3.1.2.7）

| 参数 | 含义 | `.zmx` |
|---|---|---|
| LDE 半径列 | **Y 方向**曲率半径 Ry | `CURV`（存曲率 1/R） |
| LDE 圆锥列 | **Y 方向**圆锥常数 ky | `CONI` |
| 1 | X 方向曲率半径 Rx（0 = 无穷） | `PARM 1` |
| 2 | X 方向圆锥常数 kx | `PARM 2` |

`z = (cx x² + cy y²)/(1 + √(1 − (1+kx)cx²x² − (1+ky)cy²y²))`，`cx=1/Rx`，`cy=1/Ry`。

证据：手册正文 "The radius in the x direction is set in the parameter 1 column" +
参数表 `1 → Rx, 2 → kx`；以及任务脚本自己的写法
（`fixtures/scripts/piA/compare_index_pairs.py` 的 `set_biconic`：`Radius=Ry, Conic=Ky,
Par1=Rx, Par2=Kx`）。数值旁证：pi-A r012 面 4 的 `CURV=-0.2`（Ry=−5）与 `PARM 1=-4.999459`
（Rx≈−5）、`CONI=-14.999979` 与 `PARM 2=-14.932061` 两两接近，符合"口径 0.115 mm 的近似对称透镜"。

### Biconic Zernike（§2.3.1.2.8）

| 参数 | 含义 | `.zmx` |
|---|---|---|
| 0 | 外推标志（0 = 归一化半径外忽略 Zernike 项） | `PARM 0` |
| 1 | X 方向曲率半径 Rx | `PARM 1` |
| 2 | X 方向圆锥常数 kx | `PARM 2` |
| 3 | Zernike 项数（最大 210） | `PARM 3` |
| 4 | Zernike 归一化半径 | `PARM 4` |
| 13–28 | x¹ … x¹⁶ 的系数 | `XDAT 1..16` |
| 29–44 | y¹ … y¹⁶ 的系数 | `XDAT 17..32` |
| 45–254 | Zernike Standard 项系数 | `XDAT 33..`（本项目未出现） |

Y 方向曲率/圆锥仍取 LDE 列（与 Biconic 相同）。

证据：手册参数表逐项；`XDAT_i = 参数(12+i)` 与三件镜头的取值一致
（pi-A 面 9 的 `XDAT 2 = -1.70402`、`XDAT 4 = -8.583354`、`XDAT 18 = -0.478973`、
`XDAT 20 = -13.747199`、`XDAT 22 = 277.948381`：只有偶次幂非零，符合对称面形；
若按"x⁰ 起"读法，`XDAT 1` 会是平移项、`XDAT 2` 是一次倾斜项，与轴上对称系统矛盾）。

### Toroidal（§2.3.1.2.71）

| 参数 | 含义 | `.zmx` |
|---|---|---|
| 0 | 外推标志 | `PARM 0` |
| 1 | **旋转半径** R（0 = 无穷 → 圆柱面） | `PARM 1` |
| 2–8 | Y–Z 截面曲线的偶次系数（y²、y⁴ … y¹⁴，16 阶项省略） | `PARM 2..8` |
| 13 | Zernike 项数 | `XDAT 1` |
| 14 | Zernike 归一化半径 | `XDAT 2` |
| 15–245 | Zernike Standard 项系数 | `XDAT 3..` |

LDE 半径列 = Y–Z 截面曲线的曲率半径，圆锥列 = 该曲线的圆锥常数。

三件任务的 30 个轮次里 `PARM 1` 恒为 0（`PARM 0..8` 全 0、`XDAT 2 = 100` 即归一化半径），
所以实际用到的只有"旋转半径无穷"的圆柱面分支。

### Paraxial（§2.3.1.2.58）

| 参数 | 含义 | `.zmx` |
|---|---|---|
| 1 | 焦距 f（空气中等效，0 = 无光焦度） | `PARM 1`（三件镜头都是 4.135） |
| 2 | OPD Mode（0/1/2/3，见下） | `PARM 2`（三件镜头都是 1） |

面形是平面；折射按 `n'·tan u' = n·tan u − y/f`。手册给出的四种 OPD 模式：

| Mode | 含义 |
|---|---|
| 0 | 按"实际 parabasal 光线"确定的共轭位置算 OPD（快，像差不大时准） |
| 1 | 对每条光线积分该面引入的实际相位（最慢、最准） |
| 2 | 假定无穷远共轭（入射光束不接近准直时结果是错的） |
| 3 | 用近轴光线而非实际光线（同 Mode 0 的前提） |

三件镜头都用 Mode 1 —— **这是必须按 Mode 1 实现的原因**，用 Mode 0 或 3 会得到不同的 OPD。

### Binary 2（§2.3.1.2.10，仅 pi-B 中间轮次）

| 参数 | 含义 |
|---|---|
| 0 | M，衍射级次 |
| 1–8 | 偶次非球面系数（与 Even Asphere 同） |
| 13 | 最大项数（0 = 无衍射效应，最大 240） |
| 14 | 归一化半径 |
| 15 | ρ̂² 上的相位系数（弧度） |
| 16 | ρ̂⁴ |
| 17–254 | ρ̂^(2(n−14)) |

相位 `φ = M·Σ Aᵢ ρ̂^(2i)`；相位的**斜率**才改变光线方向。追迹侧解
`n₂d′ = n₁d + (λ/2π)∇φ_t + s·N`（取 `d′·N > 0` 的根），光程加 `λφ/2π`（手册：
"The phase adds to the path length of the ray"）。正光焦度的二元面二次项为负，与手册符号约定一致。

**样本注意**：pi-B r003/r004 的四个 `BINARY_2` 面相位系数全为 0（只有项数与归一化半径），
所以只有"零级、无相位"路径有样本，相位项本身没有 oracle 对照。

### Fresnel（§2.3.1.2.32，仅 pi-B 一个面）

求交按平面（顶点平面），折射按 Even Asphere 的局部斜率处理；参数 1–8 与 Even Asphere 相同。
非平面基底的 Fresnel 不支持 OPD 类计算（手册明说），代码里 `Shape.opd_supported = False`，
`opd_map` 直报 `NotImplementedError`。

## `.zmx` 字段 ↔ 数据模型

| `.zmx` | 含义 | 备注 |
|---|---|---|
| `SURF n` + 缩进行 | 第 n 面 | 缩进行才是面属性，非缩进行从第一个 `SURF` 之后就是 MFE 操作数 |
| `TYPE` | 面型名 | `BICONICX` / `BICONICZ` / `TOROIDAL` / `PARAXIAL` / `EVENASPH` / `BINARY_2` / `FRESNELS` |
| `CURV` | 曲率 1/mm（LDE 半径的倒数） | 0 表示无穷 |
| `CONI` | 圆锥常数 | |
| `DISZ` | 厚度 | 多重结构用 `THIC` 覆盖 |
| `GLAS` | `名字 形式 0 nd Vd …` | `___BLANK` = 模型玻璃，nd/Vd 在第 4/5 个数上 |
| `DIAM` / `MEMA` | 通光半口径 / 机械半口径 | **不用于渐晕**：仅设半口径时 OpticStudio 不遮挡光线 |
| `PARM n v` / `VPAR n` | 参数 n 及其变量标记 | |
| `XDAT n v …` | 参数 (12+n) 的原始值 | 见上文各面型表 |
| `CLAP` / `FLAP` | 圆孔 / 浮动孔径：`CLAP 0 0.125 0`、`FLAP 0 0.115 0` | 前两个数当 (下限, 上限)；其后的偏移列在样本里恒为 0，未标定 |
| `STOP`（缩进） | 该面是光阑 | |
| `ENPD` / `OBNA` | 入瞳直径 / 物方 NA | |
| `FTYP` | 视场类型（0 = 角度） | 轴上单视场时不写 `XFLD/YFLD` |
| `RAIM 0 m …` | 光线瞄准：第 2 列 m = 0 关 / 1 近轴 / 2 实际 | 其余 8 列语义未确认 |
| `PUSH x y z cx cy …` | 瞳孔偏移与压缩（手册 §2.1.1.7 Pupil Shift 一节：3 个平移 + 2 个压缩） | 6 个数，第 6 个未确认 |
| `HYPR` | 逐文件不同（Codex=1，另两件=0），语义未确认 | |
| `MNUM n m` | 多重结构数 n / **文件当前结构 m** | 实测 piA m=1 / piB m=1 / codex m=5，与 golden `info.csv` 的 `current_config` 逐位一致；它不是行数 |
| `THIC 面 结构 值 …` | 结构覆盖的厚度 | 值之后是求解数据列；中间轮次出现过 pickup（scale −1 / offset 0.1） |
| `<操作数> 面 结构 值 码 参数2 参数3 源结构 源行 比例 偏移 0 "" 0` | 多重结构单元格的列序（2026-11-07 实测，`GLSS` 的值是带引号的牌号，比例、偏移写 `0 0.0`） | 码：0 固定、1 变量、2 结构拾取，**热跟随 = 结构数 + 2**（2、3、4、6、12 个结构为 4、5、6、8、14）；规则见 `src/rtc/thermal.py` |
| `TCED α 0 0` | 这一面的膨胀系数（1e-6/K） | |

## 已知未标定项（写进代码里会报错，不会静默近似）

- BICONICZ 的 Zernike 项（三件镜头项数恒为 0）
- TOROIDAL 的有限旋转半径分支（三件镜头半径恒为 0）
- 用户定义面孔径（类型 8/9，需要外部孔径文件）
- 以上全部需要 oracle（`SSAG` / `INDX` / `RAGA` 等）实测后才允许标为已验证

已经实现、但尚无 oracle 数值证据（可以算，不能当交付依据）：
Fresnel（Binary 2 的相位项已有小样本实测，生产仍拒绝）、模型玻璃（21 组表内）、MCE pickup、圆形以外的孔径与渐晕码、
Huygens PSF / STRH、DLS 的 AD 雅可比。

## 其它实测事实

从原 rtc 仓库的交接记录和差距分析搬来，文中的 `fixtures/` 已不在本仓库。

文件与结构：
- 保存时停在哪个结构，`LoadFile` 后 `MCE.CurrentConfiguration` 就是哪个：`MNUM` 第 2 列记的就是它（`codex-r017.zmx` 停在 5），拿结构 1 去比差 2e-2 mm。它与 MCE 行数不成比例（r012 是 1、有 6 行，r017 是 5、有 27 行），解析器按行形状识别 MCE 行。
- 厚度 pickup：`值 = scale·源厚度 + offset`，`scale`、`offset` 取 `solve[5]`、`solve[6]`，源面号取 `solve[4]`；`solve[3]` 不是源结构号（r003 随结构变、r004 恒为 1，两份文件实测都来自同结构）。
- AGF 有两种编码（`MISC2.AGF` 是 UTF-16LE，`SCHOTT.AGF` 是 ASCII），先看 BOM；按 UTF-16 解 ASCII 也会“成功”，只是解出乱码。

追迹：
- 只设通光半口径（`DIAM`）时 OpticStudio 不遮挡光线，渐晕不按 `DIAM` 判。
- 双锥面的梯度要用 `(cx,kx,U)`、`(cy,ky,U)` 分别算，`dz/dx = x/(1+S)·[2c + z·(1+k)c²/S]`，`U = cx·x² + cy·y²`。旋转对称圆锥面上这个式子与 `c·x/S` 相等（2026-11-05 核对）；原记录说后者只在 k=0 成立，不对。
- PARAXIAL 面：出射规则 `n'·tan u' = n·tan u − y/f`，之后换成 n'（2026-10-28，面后 N-BK7、N-SF6 差 ≤ 7e-18）；批量追迹的 OPL 列不含理想透镜相位，相位放 `RayBlock.phase`，波前用 `opl + phase`；相位是沿面积分 n'·(L', M') − n·(L, M)，以同视场主光线为零，入射光束有像差时 OpticStudio 的积分路径未公开，未对照。
- OBNA 实际瞄准的目标是 `(px·Rx, py·Ry)`，Rx、Ry 是射向近轴入瞳 x、y 边缘的两条实光线的光阑落点，带符号（piB：−0.03752733、−0.02444421，光线差 ≤ 8.9e-7 mm，2026-11-05）。piB 的光阑 y 高度在边缘光线处几乎到极值，那里 Newton 到不了，但近轴方向本身就是解。直接二维 Newton 会在圆柱面焦散处跳到反射分支，逐坐标二分会落到沿轴穿过光阑的伪根，所以 `trace` 沿光瞳坐标延拓，Newton 单步限幅 |Δu| ≤ 0.02，不收敛就减半步长而不换根。
- 模型玻璃的系数对 `(nd, 1/Vd)` 仿射回归残差 3.9e-2，不是简单线性关系（2026-10-26）。

ZOS-API：
- `GetOperandValue`：`SSAG`、`INDX` 固定评估第 1 个光学面，传面号无效；`CONF` 返回 0；`RAGA` 的 `field` 参数失效。
- 批量追迹 `ReadNextResult` 的签名是 `out int, out int, out int, 11×out double`：光线序号、错误码、渐晕码，`x,y,z,l,m,n`，法线 3 列，OPL，最后一列恒为 1。错误码只确认了 2 = 折射失败或全反射。
- `FRU__delta_init(): Attempt to start when running!` 是残留 `OpticStudio.exe` 进程的提示，不影响结果。

仍未定：`HYPR` 语义（`codex` = 1、另两件 = 0，文档未查到，可在同一镜头上 A/B 追迹判断）；`PUSH` 第 6 列、`RAIM` 其余 8 列；`9e9`、`1e10` 哨兵的含义。

评价函数规模：`codex-r017` 有 1440 个 `STHI`、1485 个 `OPGT`、287 个 `OPDX`，单次残差就是几千项。
"""

from __future__ import annotations

import numpy as np

from . import dual as D
from . import kernel
from .zmx import Surface


def _val(x):
    return x.v if isinstance(x, D.Dual) else x


def _radial(row, params) -> None:
    for i in range(1, 9):
        row[kernel.EVEN + i - 1] = params.get(i, 0.0)


class Shape:
    """一种面型。``sag`` 返回 (z, ∂z/∂x, ∂z/∂y)；x、y 可广播。"""

    kind = "?"
    verified = False
    #: 该面型是否给光线加相位（衍射面）。相位 φ 单位 rad，加进光程：OPL += λ/(2π)·φ（手册
    #: §2.3.1.2.10「The phase adds to the path length of the ray」）。
    diffractive = False
    #: OPD/波前是否能算。Fresnel 的求交按平面、折射按基底，手册明确说不支持 OPD。
    opd_supported = True
    #: 内核的求交方式
    mode = kernel.BRACKET

    def __init__(self, surface: Surface):
        self.surface = surface

    def row(self) -> np.ndarray:
        """内核的参数行。参数超出标定范围时报 NotImplementedError。"""
        row = np.zeros(kernel.ROW)
        self.fill(row, self.surface.params)
        row[kernel.HAS_EVEN] = float(np.any(row[kernel.EVEN:kernel.XP]))
        row[kernel.HAS_XY] = float(np.any(row[kernel.XP:kernel.FOCAL]))
        return row

    def fill(self, row, params) -> None:
        row[kernel.CX] = row[kernel.CY] = self.surface.curvature
        row[kernel.KX] = row[kernel.KY] = self.surface.conic

    def links(self, field: str, param: int = 0) -> list[tuple[int, float]]:
        """设计变量落在参数行的哪些列上：[(列, ∂列值/∂变量)]。没有作用的变量返回空表。"""
        if field == "curvature":
            return [(kernel.CX, 1.0), (kernel.CY, 1.0)]
        if field == "conic":
            return [(kernel.KX, 1.0), (kernel.KY, 1.0)]
        return []

    def sag(self, x, y):
        return kernel.sag(self.row(), x, y)

    def vertex_curvature(self, axis: str):
        """顶点处沿 x 或 y 的近轴曲率；float 和 Dual 都可以。"""
        return self.surface.curvature

    def petzval_curvature(self):
        """PETZ 用的曲率。近轴量计入的二次项，OpticStudio 的 PETZ 只有偶次非球面的 α₁ 计入（2026-11-05 实测）。"""
        return self.vertex_curvature("y")


class Standard(Shape):
    kind = "STANDARD"
    verified = True
    mode = kernel.CONIC


class EvenAsphere(Shape):
    kind = "EVENASPH"
    verified = True
    mode = kernel.ASPHERE

    def fill(self, row, params) -> None:
        super().fill(row, params)
        _radial(row, params)

    def links(self, field, param=0):
        if field == "param" and 1 <= param <= 8:
            return [(kernel.EVEN + param - 1, 1.0)]
        return super().links(field, param)

    def vertex_curvature(self, axis: str):
        # 矢高里的 α₁r² 项改变顶点曲率：z ≈ (c/2 + α₁)r²
        return self.surface.curvature + 2.0 * self.surface.params.get(1, 0.0)


class Biconic(Shape):
    """LDE 半径/圆锥作用于 Y；参数 1 = Rx（0 表示 X 方向平直），参数 2 = kx。"""

    kind = "BICONICX"
    verified = True

    def fill(self, row, params) -> None:
        rx = params.get(1, 0.0)
        row[kernel.CX] = 0.0 if rx == 0.0 else 1.0 / rx
        row[kernel.KX] = params.get(2, 0.0)
        row[kernel.CY] = self.surface.curvature
        row[kernel.KY] = self.surface.conic

    def links(self, field, param=0):
        if field == "curvature":
            return [(kernel.CY, 1.0)]
        if field == "conic":
            return [(kernel.KY, 1.0)]
        if field == "param" and param == 1:
            rx = float(self.surface.params.get(1, 0.0))
            return [(kernel.CX, -1.0 / (rx * rx))] if rx else []
        if field == "param" and param == 2:
            return [(kernel.KX, 1.0)]
        return []

    def vertex_curvature(self, axis: str):
        if axis != "x":
            return self.surface.curvature
        rx = self.surface.params.get(1, 0.0)
        return 0.0 if _val(rx) == 0.0 else 1.0 / rx


class BiconicZernike(Biconic):
    """Biconic 基底 + x^1..x^16 / y^1..y^16 多项式（参数 13–44）+ Zernike（未实现）。"""

    kind = "BICONICZ"
    verified = True

    def fill(self, row, params) -> None:
        if int(params.get(3, 0.0)):
            raise NotImplementedError("BICONICZ 的 Zernike 项未实现（三件任务的 30 个轮次里项数恒为 0，没有标定数据）")
        super().fill(row, params)
        for i in range(1, 17):
            row[kernel.XP + i - 1] = params.get(12 + i, 0.0)
            row[kernel.YP + i - 1] = params.get(28 + i, 0.0)

    def links(self, field, param=0):
        if field == "param" and 13 <= param <= 28:
            return [(kernel.XP + param - 13, 1.0)]
        if field == "param" and 29 <= param <= 44:
            return [(kernel.YP + param - 29, 1.0)]
        return super().links(field, param)

    def vertex_curvature(self, axis: str):
        # x²、y² 项（参数 14、30）改变顶点曲率，OpticStudio 的近轴量同样计入（2026-11-05 实测 EFLX、EFLY、ENPP）
        return super().vertex_curvature(axis) + 2.0 * self.surface.params.get(14 if axis == "x" else 30, 0.0)

    def petzval_curvature(self):
        return self.surface.curvature


class Toroidal(Shape):
    """旋转半径 = 0（无穷）时是圆柱面：Y 方向二次曲线 + 参数 2..8 的 y²..y¹⁴ 项；有限半径未标定。"""

    kind = "TOROIDAL"
    verified = True

    def fill(self, row, params) -> None:
        if params.get(1, 0.0) != 0.0:
            raise NotImplementedError("TOROIDAL 有限旋转半径未标定（三件镜头全为 0）")
        if int(params.get(13, 0.0)):
            raise NotImplementedError("TOROIDAL 的 Zernike 项未实现")
        row[kernel.CY] = self.surface.curvature
        row[kernel.KY] = self.surface.conic
        for i in range(1, 8):
            row[kernel.YP + 2 * i - 1] = params.get(1 + i, 0.0)

    def links(self, field, param=0):
        if field == "curvature":
            return [(kernel.CY, 1.0)]
        if field == "conic":
            return [(kernel.KY, 1.0)]
        if field == "param" and 2 <= param <= 8:
            return [(kernel.YP + 2 * (param - 1) - 1, 1.0)]
        return []

    def vertex_curvature(self, axis: str):
        # y² 项（参数 2）改变顶点曲率，OpticStudio 的近轴量同样计入（2026-11-05 实测 EFLY、ENPP）
        return 0.0 if axis == "x" else self.surface.curvature + 2.0 * self.surface.params.get(2, 0.0)

    def petzval_curvature(self):
        return self.surface.curvature


class Paraxial(Shape):
    """面形是平面；折射按 n'·tan u' = n·tan u − y/f。"""

    kind = "PARAXIAL"
    verified = True
    mode = kernel.PLANE

    def fill(self, row, params) -> None:
        row[kernel.FOCAL] = params.get(1, 0.0)

    def links(self, field, param=0):
        return [(kernel.FOCAL, 1.0)] if field == "param" and param == 1 else []


class Binary2(EvenAsphere):
    """偶次非球面基底 + 相位 φ = M·Σ_{i=1..N} A_i ρ^(2i)（手册 §2.3.1.2.10）。

    参数表：0 = 衍射级次 M，1–8 = 基底的非球面项，13 = 最大项数 N，14 = 归一化半径 r_n，
    15 起 = 系数 A_1(ρ²)、A_2(ρ⁴)…（单位 rad，手册原文 "Coefficient in radians on ρ^2"…）。ρ² = (x² + y²)/r_n²。
    r_n 缺省或为 0 时取 100（2026-11-05 OpticStudio 实测，读数与显式写 100 相同）。

    符号约定（手册 BINARY 1 的 "Coefficients Sign Conventions"）：正光焦度的二元面
    二次相位系数为负，且它给边缘光线加的是**负**光程——与 OPL += λ/(2π)·φ 一致。
    """

    kind = "BINARY_2"
    verified = False
    diffractive = True
    mode = kernel.BRACKET

    def _norm_radius(self) -> float:
        return self.surface.params.get(14, 0.0) or 100.0

    def fill(self, row, params) -> None:
        super().fill(row, params)
        terms = int(params.get(13, 0.0))
        if terms > kernel.MAX_PHASE_TERMS:
            raise NotImplementedError(f"BINARY_2 相位项数 {terms} 超过内核上限 {kernel.MAX_PHASE_TERMS}")
        row[kernel.NTERM] = max(terms, 0)
        row[kernel.ORDER] = params.get(0, 1.0)
        row[kernel.RN] = self._norm_radius()
        for i in range(1, terms + 1):
            row[kernel.PHASE + i - 1] = params.get(14 + i, 0.0)

    def links(self, field, param=0):
        if field == "param" and param in (0, 13, 14):
            raise NotImplementedError(f"BINARY_2 的参数 {param}（衍射级次、项数、归一化半径）没有实现导数，不能作优化变量")
        if field == "param" and param >= 15:
            # 项数之外的系数不进实光线的相位；A_1 仍进近轴量，由 phase_power 单独传递
            return [(kernel.PHASE + param - 15, 1.0)] if param < 15 + int(self.surface.params.get(13, 0.0)) else []
        return super().links(field, param)

    def phase_power(self, wavelength_um: float):
        """相位的二次项给的近轴光焦度：φ ≈ M·A_1·r²/r_n²，n'u' = n·u + (λ/2π)·∂φ/∂y = n·u − y·光焦度。OpticStudio 的近轴量同样计入（2026-11-05 实测 ENPP）。

        不看项数：原始文件写 N=0 且带 A_1 时，OpticStudio 的实光线不用 A_1，近轴量却用（2026-11-05 实测，另存后 A_1 被删，两者才一致）。
        """
        params = self.surface.params
        rn = self._norm_radius()
        return -wavelength_um * 1e-3 / (2.0 * np.pi) * 2.0 * params.get(0, 1.0) * params.get(15, 0.0) / (rn * rn)


class Fresnel(EvenAsphere):
    """平面求交 + 偶次非球面基底的折射（手册 §2.3.1.2.32）。

    手册原文："The surface intercept is determined by computing the intersection of the
    incoming ray with a plane. Once the plane intercept points are found, the surface is
    then treated as generally curved for the purposes of refraction into the next medium."
    另外手册明说 Fresnel 不支持 OPD，所以 ``opd_supported = False``。
    """

    kind = "FRESNELS"
    verified = False
    opd_supported = False
    mode = kernel.PLANE

    def sag(self, x, y):
        zero = np.zeros(np.broadcast(np.asarray(x), np.asarray(y)).shape)
        return zero, zero, zero


_SHAPES = {
    "STANDARD": Standard,
    "EVENASPH": EvenAsphere,
    "BICONICX": Biconic,
    "BICONIC": Biconic,
    "BICONICZ": BiconicZernike,
    "TOROIDAL": Toroidal,
    "PARAXIAL": Paraxial,
    "BINARY_2": Binary2,
    "FRESNELS": Fresnel,
    "FRESNEL": Fresnel,
}


def make(surface: Surface) -> Shape:
    cls = _SHAPES.get(surface.type.upper())
    if cls is None:
        raise NotImplementedError(f"面型 {surface.type} 不在复现范围内")
    return cls(surface)


# 内核实现的面孔径；用户定义孔径（8/9）需要外部孔径文件，未实现，报错而不是当作无孔径
_APERTURES = (0, 1, 2, 3, 4, 5, 6, 7, 10)


def aperture_row(item, aiming: bool) -> np.ndarray:
    """内核的孔径行。金标准 is_ray_vignetted：容差乘在半径平方上，光阑面在瞄准时放宽一个量级。"""
    s = item.surface
    code = s.aperture_code
    if code in (8, 9):
        raise NotImplementedError("用户定义面孔径（类型 8/9）需要外部孔径文件，未实现（两者的码值语义相反）")
    if code not in _APERTURES:
        raise NotImplementedError(f"孔径类型码 {code} 不在 1–10 范围内")
    epsilon = 1.0 - (1e-7 if item.is_stop and aiming else 1e-8)
    return np.array([code, s.ap_min, s.ap_max, s.ap_dx, s.ap_dy, (item.semi or s.ap_max) if code == 10 else 0.0, epsilon], dtype=float)


def pack(system) -> kernel.Packed:
    """把系统排成内核数据。"""
    items = system.items
    aiming = system.lens.ray_aiming > 0
    return kernel.Packed(
        mode=np.array([it.shape.mode for it in items], dtype=np.int64),
        P=np.array([it.shape.row() for it in items], dtype=np.float64),
        AP=np.array([aperture_row(it, aiming) for it in items], dtype=np.float64),
        vz=np.array([it.vertex_z for it in items], dtype=np.float64),
        nafter=np.array([it.n_after for it in items], dtype=np.float64),
        mirror=np.array([bool(it.reflect) for it in items], dtype=np.bool_),
        sign=np.array([float(it.sign) for it in items], dtype=np.float64),
        index=np.array([it.index for it in items], dtype=np.int64),
        n0=float(system.n_before(0)),
        wavelength_mm=float(system.wavelength) * 1e-3,
        stop=-1 if system.stop is None else int(system.stop),
    )
