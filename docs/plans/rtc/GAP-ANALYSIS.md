# 差距分析：需求 ↔ 已有证据 ↔ 缺什么

标签约定（沿用 exe-reverse 的强度标签）：

- **observed**：有命令或数值输出为证（OpticStudio 实测、文件内容、手册原文）。
- **inferred**：由观察推出，推理链完整但未直接测。
- **unverified**：只在文档/日志里出现，未复现。
- **unknown**：查过但没查到；`unknown ≠ 不存在`，下文都写明查了什么。

台账基线：`fixtures/`（oracle 输出）+ `E:\gitProjects\saos`（三件任务的运行记录）+ 本机手册文本。

---

## 第一批：序列追迹内核

### 1. 面型矢高与法线

| 面型 | 现状 | 证据 | 缺什么 |
|---|---|---|---|
| Standard | **已实现，已实测** | SSAG：`ORACLE.md` §2（R=50，h=1 → 0.010001000200050014，h=6 → 0.36130541603657362，1e-15 内一致） | 无（法线由 ∇z 解析，见下） |
| Even Asphere | 已实现，参数语义**已实测** | `oracle/campaign_EvenAspheric.json`：参数 1–8 幂次 2,4,…,16，缩放 1.0e-3（=扰动值本身）→ 无归一化 | 矢高整体值未做 SSAG 对照（只对照了多项式项） |
| Biconic（`BICONICX`） | 已实现，**未实测** | 手册 §2.3.1.2.7 参数表 + 任务脚本 `set_biconic()` 的用法 + r012 面 4 的数值自洽（Rx≈Ry、kx≈ky） | SSAG 逐点对照；另外要测"LDE 半径到底作用于 X 还是 Y"（手册说参数 1 = Rx，但这一点值得用非对称 Rx/Ry 显式验证） |
| Biconic Zernike | 已实现基底 + x/y 多项式；**Zernike 项未实现** | 30 轮里参数 3（Zernike 项数）恒为 0 → 本阶段不需要；`XDAT_i = 参数(12+i)` 的映射见 SURFACE-PARAMS | Zernike Standard 项序（若回看其它镜头才需要）；x/y 幂次的起始下标（x¹ 还是 x⁰）需 SSAG 确认 |
| Toroidal | 已实现"旋转半径 = 无穷"的圆柱分支 | 30 轮里参数 1 恒为 0 | 有限旋转半径分支（符号/分支未标定，代码里直报 NotImplementedError） |
| Paraxial | 只实现"面形是平面" | 手册 §2.3.1.2.58 | 折射 `u′=u−y/f` 与 **OPD Mode 0–3**；三件镜头都是 Mode 1，必须按"逐光线积分相位"实现 |
| Binary 2 / Fresnel | **已实现，未对账** | 手册 §2.3.1.2.10 / §2.3.1.2.32；pi-B 中间轮次 4 个 `BINARY_2` 面（相位系数全为 0）、1 个 `FRESNELS` 面 | 相位项与光栅方程的符号需统一一次 oracle 对照；非平面 Fresnel 的 OPD 按手册不支持（`opd_map` 直报错） |

法线：现在由 `∇z` 解析给出（`rtc.geometry`，精确式）。**注意**：`ORACLE.md` §7.2 的折射实测用的是
**平面**（`SURF 1 STANDARD` + `CURV 0`），它验证了 Snell 式与折射率，但法线在那里是 (0,0,1)，
**没有直接验证曲面法线**；曲面法线是靠 golden 的 `Nx,Ny,Nz` 列间接对上的（见 HANDOFF §5）。

### 2. 光线与曲面求交

状态：**已实现并已逐面对账**（`geometry.Shape.intersect_value`：安全括号 + Newton/二分混合；
与 golden 逐面 `max|Δx| ≤ 3.4e-10 mm`）。

实现判据（都是自定的，不是复现对象的内部参数）：顶点平面上的初值→向两侧按几何增长找符号变化
建立括号→Newton 步落在括号内就接受、否则二分；收敛判据取相对 1e-14，无交点返回 `nan`。

缺：错误码语义（只确认 2 = 折射失败/TIR）；近退化几何（c→0、大 ρ、掠射）下根的选择规则；
掠射光（`|dir|` 接近垂直光轴）与大角度圆锥面的数值稳定性，只能以逐条光线比对为准。

### 3. 折射与衍射

| 项 | 状态 | 证据 |
|---|---|---|
| Snell 折射 | **已实测** | `ORACLE.md` §7.2：`RAGA = 0.22523220343266909`，与 `sin20°/n` 一致到 1.5e-16 |
| 全反射判据 | **已实测**（与 `n₁sinθ₁ = n₂sinθ₂` 无实解条件同构） | `ORACLE.md` §7.2 的折射对照；手册 §2.3.1.2 |
| Binary 2 光栅方程 | **已实现，未对账** | `trace._grating`：切向波矢式 `n₂d′ = n₁d + (λ/2π)∇φ_t + s·N` 解 `s`，取 `d′·N>0` 的根；光程加 `λφ/2π` |
| Paraxial 薄透镜 | **已实现** | `n′·tan u′ = n·tan u − y/f`（2026-10-28 前代码实际是 `tan u′ = tan u − y/f`）；理想透镜相位（Mode 1 沿面积分，以同视场主光线为零）在 `RayBlock.phase`，不进 opl；OPD 与 OPDC 对过（面后空气 / N-BK7，≤ 1e-5 waves） |
| Fresnel | **已实现，未对账** | 平面求交 + 偶次非球面基底的折射；`opd_map` 对该面型直报不支持 |

补：Snell 已被 RAGA 验证，实现按 `n₁/n₂` 的向量形式走。

### 4. 系统孔径、视场与瞄准

状态：**EPD 已对账（RAIM 0 的系统不需要瞄准）；OBNA + RAIM 2 已实现但未对账**。
证据比第一批里其它项都完整：

- 手册 §2.1.1.7 写清了算法：实光瞄准 = 反复追迹并调整物方坐标/方向余弦，直到光线穿过光阑
  面上的正确位置；光阑半径由主波长下从物中心出发的边缘光线追迹确定（近轴或实际光线）；
  实光失败时退回近轴并给警告。
- `.zmx` 数据齐全：`ENPD`/`OBNA`、光阑面索引（pi-A 面 2、pi-B 面 9、Codex 面 3）、
  `RAIM` 模式、`PUSH` 瞳孔偏移（pi-B 为 `1.7959434704269777`）。

缺：近轴 y-u 追迹求入瞳/出瞳与被追迹的归一化坐标 `(hx,hy,px,py)` → 物理光线；
二维 Newton 瞄准（迭代上限/收敛判据未公开，需自定并验证）。

风险：pi-B 用实光瞄准 + 物方 NA 0.12 且光阑在面 9；`PUSH` 非零说明初值需要辅助。
实光瞄准在宽角/强像差系统上可能不收敛，必须在 oracle 上逐步比对（批量追迹本身接受
`(hx,hy,px,py)`，所以瞄准结果可以逐条对比，这是最直接的验证方式）。

### 5. 材料折射率

| 项 | 状态 | 证据 |
|---|---|---|
| N-BK7 / BK7（AGF 公式 2，Sellmeier 1） | **已实测** | `ORACLE.md` §7.2：n(0.55) = 1.518522388；`rtc.materials` 复算 1.5185223876207… |
| WATER2（AGF 公式 1，Schott Laurent） | 已实现，**自洽验证** | 玻璃库文件在 `MISC2.AGF`（**UTF-16** 编码，容易被 ASCII 搜索漏掉）；用公式 1 回代 λ=0.5875618 得到 1.33304403，与目录 nd=1.333044 一致到 1e-8 |
| WATER2 在工作波长 | 手算对照，**待 INDX 实测** | 0.55 µm → 1.3343209，0.92 µm → 1.3263321 |
| 色散公式 1–13 | 已接入材料层，**本次集成未运行回归** | 复用指定构建原指令恢复结果；`fixtures/material/native-vectors.json` 保存独立观测 |
| 模型玻璃（nd/Vd/ΔPgF） | 已改为恢复公式，**本次集成未运行回归** | 原 21 组插值系数归档到 `fixtures/material/legacy_modelglass.py`，原始 INDX CSV 保留 |
| 单一系统温压修正 | 已接入 ENVD，**本次集成未运行回归** | 参考空气使用原始 λ；目录和模型玻璃采用各自的原生路径 |
| TEMP/PRES、TCE 几何缩放 | 未接入系统 | `OpticalSystem` 对 TEMP/PRES 明确拒绝；局部逆向证据不能代替完整热系统回归 |

补模型玻璃：已按标定流程完成（`fixtures/golden/modelglass/`：`MaterialModel` 求解设 (nd, Vd) →
`INDX` 在 6 个波长上取 n → 解 `n² = A0 + A1λ² + A2λ⁻² + A3λ⁻⁴ + A4λ⁻⁶ + A5λ⁻⁸`，21 组）。

上述 6 点精确插值只证明插值残差，不能证明真实模型色散就是 Laurent 形式。材料集成范围、
兼容接口及待运行命令见 [MATERIAL.md](MATERIAL.md)。

### 6. 多重结构（MCE）

已实现：解析 + 结构作为批维度（`Lens.thickness(i, cfg)`）。
`observed`：30 个轮次的 MCE 操作数只有 `THIC`（pi-B 只有一行 `MOFF`，即没有实际覆盖）。

缺：**pickup 求解（已实现，列语义部分未标定）**。`Lens.thickness` 按
`值 = scale·源厚度 + offset` 递归求解并检测成环；`scale/offset` 从 `solve[5]/solve[6]` 读
（固定行 `1.0 0.0`、pickup 行 `-1.0 0.1`），源面号取 `solve[4]`；**源结构号不取 `solve[3]`**
（r003 随结构变、r004 恒为 1，但两份文件实测都是"源自同结构"），一律按同结构解。
最终 r017 已无 pickup。

`MNUM` 第二个数 = **保存文件时选中的结构**（实测 piA 1 / piB 1 / codex 5，与 golden `info.csv`
的 `current_config` 一致），已解析为 `Lens.current_config`。

### 7. 孔径与渐晕

状态：**已实现，未对账**。类型码用手册 §14.46 的表（None 0 / 圆孔 1 / 圆遮扢 2 /
蜘蛛 3 / 矩形 4、5 / 椭圆 6、7 / 用户 8、9 / 浮动 10），半口径与中心偏移的槽位按类型复用：

| 量 | 偏移 | 说明 |
|---|---|---|
| 孔径类型码 | `surface+0x9b0` | 1…10 十个分支，default 走到用户孔径文件（类型 8/9 语义相反） |
| 半口径 X / Y | `+0x9b8` / `+0x9c0` | 圆形/椭圆/矩形/蜘蛛都用它 |
| 中心偏移 X / Y | `+0x9c8` / `+0x9d0` | 偏心孔径 |

渐晕码：`0x131`（前一面被遮挡的特殊情形）、`0x132`（浮动孔径）、`0x133`（圆孔超出）、
`0x134`（内圈/未达最小口径）、`0x135`（蜘蛛线）、`0x136`/`0x137`（矩形 x/y 越界）、`0xf0`（用户孔径）。
三个任务的光线都是圆形半口径（`DIAM`），只需要 1 号分支，但仍需把码值对齐到名字。

缺：码值 ↔ 手册孔径类型名的对齐靠手册 §14.46 那张表；仍需 oracle 实测（设不同类型的孔径 +
故意越界光线，读批量追迹的 vig 列）。
三件任务只用到圆孔/浮动孔径（`CLAP`/`FLAP`），其余分支没有样本。
另外：**渐晕不再按 `DIAM` 判**——只设通光半口径时 OpticStudio 不遮挡光线。
改动只影响 `vig` 列，不动光线几何。

### 8. 批量追迹接口（IBatchRayTrace / NormUnpol）

状态：**已实现并已对账**（`trace.trace` 返回逐面 `x,y,z,l,m,n,opl,error,vig`）。
oracle 侧的同构实现：
`E:\gitProjects\saos\src\saos\backend\zos.py:330-355` 用 `OpenBatchRayTrace().CreateNormUnpol(...)`
+ `AddRay(wave, hx, hy, px, py, OPDMode.None)` + `ReadNextResult(...)` 读出 9 列，
其中第 2 列是错误码、第 3 列是渐晕、4–9 列是 x,y,z,l,m,n。这一层直接复用，不重写。

实已做：`tools/compare_golden.py` 把 rtc 的列直接对齐到 golden 的 9 列（`CreateNormUnpol`
+ `ReadNextResult`，误差码位置、`x,y,z` 为局部坐标、`l,m,n` 为折射后方向），
逐面最大偏差见 HANDOFF §5。

### 9. 派生操作数

| 操作数 | 状态 |
|---|---|
| SSAG | 参数布局**已实测**：`SSAG(Hx, Hy, ?, mode, ?, ?)`，固定评估第 1 个光学面，wave/field 必须 1 起始，mode 0 = 总矢高、1 = 仅多项式偏离量（`ORACLE.md` §2） |
| RAGA / RAIN | **已实测**（`ORACLE.md` §7.2、§7.4） |
| REAX / REAY / REAC / RAID / TRAC | 未标定参数布局 |
| CONF / INDX / EXPP / EXPD | **已实现**（STRH 见第二批，已实现未对账） |
| CTGT / ETGT / MNET / MNCT / MNCA（`rtc.operands`） | **已实现，未对账**（按手册定义；ETGT 取两面半口径较小者的圆周逐点求值） |
| 质心参考的 spot RMS + 闭式最佳焦面；纵向像差；NA = n·sinθ | 未实现（saos 的 `ctx.spot(refocus=True)` 用的是 OpticStudio 自己的重聚焦，不是闭式解） |

最大的坑：`GetOperandValue` 的 6 个 double 参数里，**光线选择（哪条光线 / 哪个光瞳点）的
布局尚未标定**。`ORACLE.md` §7.4 明确记录了 `RAGA` 上 `field` 参数失效的现象。这一项不解决，
光靠手册无法复现 REA*/RAID/TRAC 的取值。

### 10. DLS 优化

状态：**已实现，未对账**。Zemax 的 DLS 内部细节未公开（ADR 已声明"算法同类，不保证逐步相同"）。

实现：`adtrace.py` 前向向量切向 AD（求交用隐函数定理 `dt/dθ = −F_θ/F_t`）+ `optimize.Design`
（残差/雅可比）+ LM。`optimize.lm` 传普通残差函数时仍回退中心差分，旧接口不变。

未验证：雅可比与中心差分的逐列比对；`BINARY_2` 相位与 `RAIM=2` 的瞄准迭代**不进**雅可比
（模块 docstring 已写明）。

规模提示（`observed`）：Codex r017 的评价函数有 1440 个 `STHI` + 1485 个 `OPGT` + 287 个 `OPDX`，
单次残差计算就是几千项，雅可比列数可能上百——这正是 GPU 有用的地方。

---

## 第二批：波前、PSF、Strehl

| 项 | 状态 | 证据 | 缺什么 |
|---|---|---|---|
| 出瞳 EXPP/EXPD | **已实现** | 近轴追迹；双锥面 X 用参数 1 的半径，圆柱面 X 无光焦度 | piB 的 EXPP 与 golden 一致；piA、codex 的 EXPP 与三件的 EXPD 仍不符，见 HANDOFF 末尾 |
| OPD 与波前 | **已实现** | 参考球面 + 反向延伸到球面 + 去 piston/tilt；Paraxial 面按 OPD Mode 1 | 2026-10-28 与 OPDC 对过 PARAXIAL 面后空气 / N-BK7 两例，≤ 1e-5 waves（`tests/test_paraxial_glass.py`）；符号同 OPDC；入射有像差时的 Mode 1 积分未对照 |
| FFT PSF | **已实现** | 光瞳函数 + 补零 FFT | 与 Zemax 的 FFT PSF 未逐点对比（三件任务没用） |
| Huygens PSF | **已实现，未对账** | 出瞳每点到像面每点直接叠加，按像面行分块；Planar/Spherical 两种参考 | 与 `GPUZemaxCore` 的批量积分结果未对比 |
| STRH | **已实现，未对账** | `psf.strehl_huygens`：峰值 / 同采样无像差峰值，分母用同一组采样点 + 同一参考相位 | golden 的 `STRH`：piA 0.9194 / piB 0.1875 / codex 0.8789 |

**关于 STRH 的确认**：需求里"STRH 基于 Huygens PSF，受采样设置影响"这一点，
手册 §2.1.1.6.4 明确列出了 STRH 属于依赖 Huygens 积分的操作数，并列出了三种参考相位设置；
所以 Codex 日志里"球面参考在出瞳横跨像面时算出虚假低谷、改成 Planar"的结论
从 `unverified` 升级为 `inferred`（手册支持 + 一次经验记录），仍需在 oracle 上重测确认。

采样敏感性（32×32 与 128×128 最多差 0.0017）是 Codex 的单次记录，`unverified`，要重测。

---

## 范围之外（明确不做）

非序列追迹、偏振/膜层、公差、MTF、Hammer/Global、温压修正、GRIN。
三件任务都没有用到，且 GRIN 的 12 种模型是独立子系统，代价高。

---

## 未定项清单（写代码时按"未知"处理，不要填默认值）

| # | 项 | 现状 | 补法 |
|---|---|---|---|
| 1 | `HYPR` 语义（Codex r017 = 1，另两件 = 0） | 官方文档与社区都没查到定义（搜索过，见 EVIDENCE） | 在同一镜头上 A/B 追迹，看 `IBatchRayTrace` 结果是否有差异 |
| 2 | `PUSH` 第 6 列、`RAIM` 其余 8 列 | 只有部分列的含义有手册对应 | 变异测试（改一列看行为）。`MNUM` 第 2 列已解：= 文件当前结构 |
| 3 | 射线错误码全表 | 只确认 2 = 折射失败/TIR | 构造各类失败几何 + `RSCH` 读码（`ORACLE.md` §5 已列出这条路线） |
| 4 | 面型枚举码 ↔ 面型名 | 只影响“读 oracle 导出的枚举值”，不影响追迹 | **不必要**——按手册的面型名实现即可 |
| 5 | 9e9 / 1e10 哨兵含义 | 出现 41/20 次 | 构造无交点几何，看哪些操作数返回哨兵 |
| 6 | 模型玻璃公式 | 已接入原指令恢复的 nd/Vd/dPgF 公式；21 组旧表归档为 oracle | 用户安排执行新材料回归和应用级 INDX；不再限制在旧表的组合 |
| 7 | 掠射分支（`ABS(dir) < 0.3`）的具体处理 | 只看到判据 | 掠射算例逐条比对 |

---

## 建议顺序（按依赖与覆盖的耗时）

1. **面型 SSAG 回归**（6 个面型 × 网格）—— 决定后面所有数值是否可信。
2. 求交 + 折射 + 批量追迹，与 `IBatchRayTrace` 逐条比对（3 件镜头 × 30 个结构）。
3. 几何/光线类操作数（CTGT/CTLT/ETGT/MNET/MNCT/MNCA/REAX/REAY/RAID/TRAC/CONF/INDX/SSAG）。
4. DLS（LM + 自动微分 + 隐函数定理）。
5. 出瞳 → OPD（含 Paraxial Mode 1）→ Huygens PSF → STRH。
6. 回看中间轮次才需要的：Binary 2、Fresnel、模型玻璃、pickup、有限旋转半径、Zernike 项。

### 本轮进展（代码已完成，未跑验证）

1–2：已完成并已对账（HANDOFF §5）。
3：已实现；前 7 个操作数已对账，厚度类未对账。
4：已实现（`adtrace` + `optimize.Design`），雅可比未与差分对比。
5：已实现（Huygens/STRH），未与 golden `STRH` 对账。
6：Binary 2、Fresnel、模型玻璃、pickup 已实现（均未对账）；有限旋转半径与 Zernike 仍直报
`NotImplementedError`（三件任务的 30 个轮次里这两条路径没有样本，见 HANDOFF §6.3）。
