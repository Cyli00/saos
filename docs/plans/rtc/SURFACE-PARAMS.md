# 面型参数对照表：手 parN ↔ OpticStudio 参数表 ↔ .zmx 字段

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

面形是平面；折射按 `u′ = u − y/f`。手册给出的四种 OPD 模式：

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

## 已知未标定项（写进代码里会报错，不会静默近似）

- BICONICZ 的 Zernike 项（三件镜头项数恒为 0）
- TOROIDAL 的有限旋转半径分支（三件镜头半径恒为 0）
- 用户定义面孔径（类型 8/9，需要外部孔径文件）
- 以上全部需要 oracle（`SSAG` / `INDX` / `RAGA` 等）实测后才允许标为已验证

已经实现、但尚无 oracle 数值证据（可以算，不能当交付依据）：
Binary 2 的相位项、Fresnel、模型玻璃（21 组表内）、MCE pickup、圆形以外的孔径与渐晕码、
Huygens PSF / STRH、DLS 的 AD 雅可比。
