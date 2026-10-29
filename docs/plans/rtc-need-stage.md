# 需求环节脱离 OpticStudio：rtc 能力补齐计划

依据 ADR-0016。目标：需求访谈到 `demand confirm` 全程不启动 OpticStudio；可行性最小模型在 rtc 上建立、追迹、取数。建模起仍用 OpticStudio。

## 落地情况（2026-10-29）

- **已落地**：`saos daemon start --backend rtc`（或 `SAOS_BACKEND=rtc`）启动不连 OpticStudio 的实例；`lens build|load|save` 由处方或 `.zmx` 建 rtc 镜头，保存为 OpticStudio 24.1 行格式的 UTF-16 `.zmx`（`src/saos/backend/rtc_lens.py`）；每次改动后按 OpticStudio 规则重算边缘光线高度求解（MAZH）与自动半口径。
- **操作数**：光线类 REAX/Y/Z/R/A/B/C、RAGX/Y/Z/A/B/C、RANG、RAID；系统类 EFFL、WFNO、ISFN、TOTR、TTHI、EPDI、ENPP、EXPP、EXPD、PETZ、INDX、SSAG、CONF（`src/saos/backend/rtc.py`，一阶量在 `../rtc/src/rtc/firstorder.py`）；`quick_focus` 用闭式 RMS² 极小。其余报 `BACKEND_UNSUPPORTED`。
- **有限物距、物方 NA、物高视场**：rtc 加了 `allow_unverified` 开关，只在 rtc 实例上打开；这些路径的结果在 `computed_by` 里列为未对账。`ctx.fast()` 副本仍拒绝它们。
- **门禁**：daemon 的 `OPTICSTUDIO_ONLY` 拒绝建模说明与确认、分析、评价函数与优化、`check`、`scan`、`round`。
- **界面**：流程图窗口标出 rtc 实例，访谈节点显示可行性记录是否已写，布局脚注列出未对账项，走过需求确认仍在 rtc 实例时提示切换。
- **未做**：PARY、RAED、ETVA/ETGT/MNET、STRH 与第二批；有限物距瞄准的 Newton 精化（沿用 rtc 现有瞄准迭代）；对账与准入（下节），所以第一批里除 REAX/REAY/REAC/INDX/SSAG/EXPP/EXPD/CONF 外，都还只是按手册定义实现。

## 参照资料

| 资料 | 用法 |
|---|---|
| `E:\gitProjects\test-core`（`INDEX.md` 为能力索引，`NOTES.md` 为调用关系与数据偏移） | 算法与约定的线索 |
| OpticStudio 2024 R1 手册 | 操作数与分析的定义 |
| `../rtc/oracle/Harness3.cs`、`fixtures/golden/` | 真值采集与回归基线 |

阅读 test-core 时注意：

- `NOTES.md` 说明了偏移值是观察所得，字段含义是推断，其中一条读法（INDX 的符号翻转）已被实测否证。反编译器给出的字段名（如 `vertexZ`）不能当作语义。
- 函数体里同一个形参在每次使用时编号都不同（例如 `GetEfocaleng` 声明了 3 个参数，函数体里却出现到 `arg44`），所以参数之间的数据流读不出来。常数和控制流可以读。
- 每条线索都要经 OpticStudio 真值确认后才能写进实现；仓库里不复制函数正文。

## saos 侧集成

1. **处方直接建 rtc 镜头**：`saos lens new` 的处方 JSON → rtc 系统，复用 `prescription.py` 的校验。rtc 无法复现的处方项报 `BACKEND_UNSUPPORTED`，范围与 ADR-0014 的拒绝清单一致，随准入逐步放开。
2. **无 ZOS 的实例**：新增 `SAOS_BACKEND=rtc`，`make_backend` 返回一个实现 `Backend` 子集的 rtc 后端，未实现的方法一律报 `BACKEND_UNSUPPORTED`。实际做法：rtc 实例与之后的 OpticStudio 实例共用同一 `SAOS_INSTANCE`，需求确认后 `daemon stop` 再 `daemon start`。
3. **操作数分派**：`operand(name, int1, int2, data)` 调用 rtc；准入表尚未细分到操作数，来源改由 `exec` 结果的 `computed_by` 标注（用到的未对账操作数与路径）。
4. **门禁**：rtc 实例上的 `saos check`、`round`、`opt`、`scan`、`report` 一律拒绝（`BACKEND_UNSUPPORTED`），证据记录只来自 ZOS。实现前先核对这些命令目前对 `backend` 身份的检查。
5. **玻璃目录**：实际做法是读 `SAOS_GLASSCAT`，未设时用 rtc 的默认 `Documents/Zemax/Glasscat`（与 OpticStudio 同一份 AGF）。

## 第一批能力（需求环节最常用）

排序依据：历史可行性核算（fiber 三例、AR 眼镜）实际用到的量。"未收录"指该函数不在 test-core 中。

| 能力 | test-core 文件 | 线索（test-core 读出的，或手册原文） | 对账要点 |
|---|---|---|---|
| EFFL | `firstorder__GetEfocaleng.c`、`operands__OO_EFFL.c` | 手册 §14.10：从无穷远追迹一条近轴附近的轴上光线，用它在像方的边缘光线角求 EFL；像方 EFL = EFL × n′。test-core：光线初始高度由孔径类型决定，然后每次乘 0.1 重新追迹，直到出射方向分量的绝对值 ≤ 0.2，最多 10 次；EFL = −(高度 × 方向余弦项) / 方向分量；像面记录上一个数值为负时翻转符号；最后除以像方折射率的绝对值（`NOTES.md` 中 `0x690` 为折射率表基址）。按面记录上的一对数值选 x 向或 y 向光线，含义未解 | 必须包含 fiber 三例中的 BICONICX/TOROIDAL 面；x、y 两个方向都要对账 |
| WFNO | 无独立文件 | 手册"Working F/#"：1/(2n·sinθ)，θ 为像方实际边缘光线角；离轴或非轴对称时，对轴向光线和上下左右 4 条边缘光线的 NA² 取平均；忽略面孔径但计入渐晕因子；边缘光线追不过时改用较小光瞳再外推；上限 10,000 | 包含有限物距和物方 NA 两种系统 |
| EXPP、EXPD、ISFN、TOTR、TTHI、EPDI、PARY、PETZ | `firstorder__GetExitPupilDiameter.c`、`operands__OO_TTHI.c`；其余无独立文件 | TTHI 只是厚度求和。PETZ 按手册是指定波长下的 Petzval 曲率半径，非近轴系统无效。偶次非球面的 r² 项改变近轴曲率：由矢高式得 c_eff = c + 2·α₁ | 逐项对账 |
| 有限物距 + EPD；物方 NA + 实际瞄准 | `geometry__getRayAimingStepFinite.c`、`geometry__getRayAimingStepInfinite.c`、`geometry__ImproveAimAccuracyFinite.c`、`geometry__UpdateAperture.c`、`trace__SetUpRay.c` | 每一步为 2×2 有限差分 Newton：坐标绝对值 ≤ 1e-3 时差分步长取 1e-6，否则取坐标的 1e-6 倍；行列式绝对值 < 1e-8 时报错，不做更新。有限物距在逐步迭代之后还有一轮精化 | 以 piB r007（OBNA 0.12 + RAIM 2）为基准；ADR-0014 记录 rtc 现在 10 条光线中有 9 条不收敛 |
| 单光线 RAID、RANG、RAGX、RAED | `operands__OO_RANG.c`、`operands__OO_RAED.c`、`operands__OO_RAGX.c` | 都是先 SetUpRay 再追迹，按操作数编号取不同分量；追迹失败时区分全反射和其他失败，给出不同错误码。手册：RANG 是光线折射或反射后与面法线的夹角 | 操作数值和全反射时的错误语义都要对账 |
| QuickFocus | `analysis__QuickFocus.c`、`analysis__QuickFocus2.c`；`GetRms` 与 `focus` 未收录 | 从像面前一面开始向前查找，跳过带标志的面，调整找到的那个面上的一个长度字段，结束时写回每个结构的记录。某个系统标志为 0 时，先用光瞳 0.5 处的光线与光轴交点给出初值。之后最多迭代 10 次（调用方可另给次数）：步长逐次加倍直到 RMS 不再下降，再用三点抛物线求顶点。步长 ≤ 1e-6 时停止；第 3 次之后新 RMS > 0.9999 × 上次时也停止。判据（径向 / x / y / 波前）由 QuickFocus2 的参数选择 | 被调整的是哪个量、RMS 怎样采样，都要靠对账确定；先对齐点列径向 RMS |
| ETVA、ETGT、MNET（SSAG 已验证） | `operands__OO_ETVA.c`、`operands__OO_MNET.c`、`operands__OO_SSAG.c` | `NOTES.md`：SSAG、ETVA 的几何计算全部在函数内完成，不调用外部函数 | 边缘取哪个半口径要对账；显式孔径与浮动孔径两种都要测 |
| STRH | `operands__OO_STRH.c`、`psf__get_psf_data.c`；`get_strh` 未收录 | 采样参数被限制在 1–10。rtc 已有 `strh` 与 `opd_map` | 先厘清 PARAXIAL 面的 OPD 是否已修复：ADR-0014 记录与 golden 相差 2.96e-2 mm，rtc 的 GAP-ANALYSIS 第 3 节则称已对上（≤ 1e-5 waves） |

## 第二批

- 点列与 RSCE（`analysis__MakeSpot.c`；参考点、光斑类型、是否含 Airy 圈是数据块状态，不是公式分支）
- 带像差的 FFT MTF（`analysis__MakeMtf.c` 调用 `mtf__gimme_pupil_opd.c` 取瞳面 OPD，再逐频点调用 `mtf__get_one_mtf.c`）
- RELI 与相对照度（`analysis__GetRelativeIllumination.c`：三条光线 + 渐晕判定，并调用 `analysis__GetRayTransmission.c`，不是 cos⁴）
- Seidel 系数（`seidel__MakeSei.c`）

本计划不涉及：膜层与偏振透过率、Huygens PSF、非序列、GRIN、倾斜偏心与反射镜、多重结构操作数（test-core 未收录 `OO_CONF`）。

## 对账与准入

- **真值**：rtc 的 golden 镜头 + `tests/fixtures/cases/sequential.zmx`，再为有限物距、物方 NA、双锥面这几类特征补最小系统。真值采集需要 OpticStudio，属于一次性工作。
- **准入键**：操作数 × 系统特征（孔径类型、物距有限 / 无限、瞄准方式、出现的面型），与材料准入表并列存放于 `rtc_admitted.json`。
- **容差（建议值，待用户确认）**：长度 1e-6 mm、方向余弦 1e-7（同 ADR-0014 的建立对照）；一阶量相对误差 1e-9；STRH 绝对误差 1e-4；QuickFocus 焦面位置 1e-5 mm。
- 每准入一项，在 `tests/test_zemax_rtc.py` 中加入对账用例；离线回归只比对已存的真值。

## 落地时同步

- `need.md`：在 "check the one or two deciding numbers on the backend" 一段写明需求环节可使用 rtc 已准入的量，以及标注要求。
- `session.md`：rtc 实例的启动方式，说明它不占用许可。
- `scripting.md`：rtc 后端上的 `ctx.op` 与 `BACKEND_UNSUPPORTED`。
- `DESIGN.md`："从建模到交付使用同一后端"一条补充需求环节的例外。
- 若需求环节的命令有变化，同步更新 `docs/workflow.drawio` 和流程图窗口。
