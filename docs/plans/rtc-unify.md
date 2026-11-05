# rtc 核心算法归一（2026-11-05）

目标：所有几何光线算法都由一个带切向的单步派生，只保留一份实现，优化雅可比提速，并保持与 OpticStudio 真值的对账结果不变。用户决定（2026-11-05）：分步全做；`BINARY_2`、`FRESNELS` 迁入内核并修复，不删除；先提交基线再重构。

## 1. 基础算法

一步只做两件事：沿直线走到面上（解 `F(t)=0`），再按切向动量守恒更新方向：

`n'·d' = n·d + (λ/2π)·∇φ_t + s·N`，法线翻到入射侧，`s = −A·N ∓ √((A·N)² − |A|² + n'²)`，`A = n·d + (λ/2π)·∇φ_t`。

透射取负根，反射取正根且 `n' = |n|`；φ≠0 就是衍射。PARAXIAL 是唯一例外：OpticStudio 实测 `n'·tan u' = n·tan u − y/f`，走 tan 空间，保留为特例。

面型只有一个公式，参数表决定它是哪种面：

`z = (cx·x² + cy·y²)/(1+S) + Σα_i·r^(2i) + Σa_i·x^i + Σb_i·y^i`，`S = √(1 − (1+kx)cx²x² − (1+ky)cy²y²)`。

| 面型 | 参数取值 | 求交 |
|---|---|---|
| STANDARD | cx=cy、kx=ky | 闭式 |
| EVENASPH | 同上 + α | 闭式初值 + Newton |
| BICONIC、BICONICX | cx≠cy | 括号保护 Newton |
| BICONICZ | 同上 + a、b | 括号保护 Newton |
| TOROIDAL（旋转半径 0） | cx=0，y 偶次项放进 b | 括号保护 Newton |
| BINARY_2 | 同 EVENASPH，另加相位多项式 | 括号保护 Newton |
| FRESNELS | 折射按 EVENASPH 的斜率 | 平面 |
| PARAXIAL | 平面，tan 空间折射 | 平面 |

## 2. 由切向种子派生的算法

| 切向种子 | 得到什么 |
|---|---|
| 无 | 普通追迹 |
| 设计变量 | 优化雅可比 |
| 发射位置或发射斜率 | 瞄准 Newton 的 2×2 雅可比，以及实际瞄准导数的隐函数项 |

实际瞄准的导数按隐函数定理：`da/dθ = −J⁻¹·(∂stop/∂θ − ∂target/∂θ)`，切向一次走完，事后线性组合。

近轴量（焦距、入瞳、出瞳）在光轴上等于实光线的切向（`iris`、`reflective`、`sequential` 实测差 ≤ 4.4e-16），但光瞳对设计变量的导数要二阶，所以仍保留一份 y-nu 追迹，由浮点和 AD 共用，不用嵌套切向。

## 3. 留在约定层的内容

这些是 OpticStudio 的标定事实，物理推不出来，集中放在 `trace.py` 的发射与瞄准目标、`system.py` 的光瞳定义里：Float By Stop Size 的标定、OBNA、R5 自动半口径、R6 表外渐晕、孔径平方半径容差、`iris_real` 未解决的残差。

## 4. 验证

每一步的门禁：

- `tests/rtc/test_case_truth.py`：OpticStudio 真值回放，容差不放宽。`iris_real` 的 EXPP、EXPD 是已知失败。
- `tests/rtc/test_omnibus_lock.py`：全覆盖案例（`tests/rtc/omnibus.py`）的快照，所有面型、孔径、瞄准模式都在内。快照只证明结果没变；雅可比用中心差分独立校验。
- 全仓离线 `uv run pytest tests -q`。
- 浮点追迹不能变慢。

重构前已修：`trace._grating` 取错根（`BINARY_2` 光线往回走）；`curvature_axis` 在 `BICONICX` Rx=0 的 y 向返回 0、在 `BICONIC` 的 x 向取错曲率；PETZ 错把理想透镜计入（实测不计）；AD 漏掉多重结构 pickup 厚度的导数；`Design` 逐条调用 AD 且每个试探步都算雅可比。

## 5. 进度

见文末“结果”一节；未写结果的步骤尚未完成。

1. 重构基线、真值测试精简、缺陷修复、全覆盖快照。
2. 内核归一：一个面型公式、一个单步，`BINARY_2`、`FRESNELS`、方向瞄准进内核，删除 numpy 追迹和 `surface.py`。
3. AD 改走内核切向，实际瞄准和有限物距的发射导数接上。
4. 近轴追迹和光瞳定义归一。
5. 生产优化器 `rtc_optim` 评估切向雅可比，修掉求值路径的热点。切向暂不接入，理由见结果。

## 结果

代码量（`src/rtc`，行）：`surface` 132 → 0，`kernel` 532 → 900，`geometry` 523 → 295，`trace` 609 → 244，`adtrace` 394 → 370，`dual` 187 → 116，`optimize` 152 → 87，`system` 494 → 482，合计 3023 → 2494。`kernel` 变长是因为 `BINARY_2`、`FRESNELS`、方向瞄准和切向都搬了进来。

门禁（2026-11-05，离线）：`uv run pytest tests -q` 除 `iris_real` 一阶量外 915 passed、70 skipped。`iris_real` 的 EXPP 与重构前逐位相同（−26.04007 对 OpticStudio −26.04018，相对差 4e-6）。去掉瞄准隐函数项后 12 个雅可比用例失败，说明差分校验有判别力。

浮点与雅可比（新 / 533cc98，同机临时测量）：

| 项目 | 新 | 旧 |
|---|---:|---:|
| iris 浮点 1253 条 | 0.38 ms | 0.44 ms |
| reflective 浮点 1253 条 | 0.39 ms | 0.31 ms |
| piA 浮点 1253 条 | 1.86 ms | 1.78 ms |
| piB OBNA 方向瞄准 5 条 | 1.66 ms | 3680.88 ms |
| iris FLOA 实际瞄准 1253 条 | 1.17 ms | 1.10 ms |
| iris `Design.jacobian` 196 条、P = 2 | 5.17 ms | 32.93 ms |

reflective 浮点这一行变慢 0.08 ms；同一脚本前一次测得 0.32 ms，单次波动与实际回退尚未区分。

生产优化器（`sequential`，向导行，端到端 3 次取最快）：

| 向导 | P | 新 | 旧 |
|---|---:|---:|---:|
| OPDX 3×6 | 3 | 76 次求值、228.6 ms | 80 次、394.6 ms |
| OPDX 3×6 带边界 | 5 | 177 次、570.7 ms | 133 次、663.0 ms |
| TRAC 6×8 带边界 | 5 | 507 次、1513.6 ms | 494 次、2365.5 ms |

每次求值约快 1.6 倍，来自三处：`RtcBackend.surface_count` 不再拼全部面的描述（原来占 `apply` 的 2/3）、自动半口径和 `Evaluator` 都按波长合批。终点不同是差分噪声：同一点残差差 ≤ 2.3e-11，起点扰动 1e-13 时 OPDX 3×6 的终值在 1.4e-5 到 5.6e-5 之间、带边界的在 0.22558 到 0.22579 之间，新旧终点都落在这个范围内或更优。

切向未接入生产，依据：

- 现在差分每列约 `apply` 0.9 ms + `evaluate`。切向追迹本身：P = 19、10 条光线 4.9 ms（差分雅可比 36.3 ms）；P = 5、297 条 7.8 ms（差分 19.3 ms）。这些数不含 OPD 链式求导和行组合。
- 边界行（`MNEG`、`MNEA` 等）依赖自动半口径，每列仍要 `apply`。P = 19 带边界时按上述数字估计只从 47.6 ms 降到约 33 ms。
- 要补的导数：OPD 的参考球（出瞳、主光线）、OPDX 的 piston/倾斜拟合、质心组、`TRAR`、边缘光线高度求解、模型玻璃 nd/vd（内核没有折射率切向）、多重结构。这是第二套推导，和这次归一的目标相反。

切向的另一个好处是没有差分噪声：OPDX 带边界案例里新旧雅可比的个别列相对差到 2.35e-2，优化器终值离散 9e-4。如果以后要追求收敛精度，优先从这里做。

两份 DLS：`rtc.optimize.lm` 只有 `tests/bench_codev.py` 在用，已搬进该基准，生产只有 `rtc_optim.local` 一份。

未验证：OpticStudio、CODE V 真机未重跑；切向在 `AsphereExpert` 上光阑前候选面的排名未与 OpticStudio 对比；`BINARY_2`、`FRESNELS` 没有真值。
