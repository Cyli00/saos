---
status: accepted; 第一批已落地，算法、接口与待对账项见 docs/plans/rtc-design-stage.md；第 4 条（独立 rtc 实例）由 ADR-0018 修订为一个后台进程持有两个后端；第 1 条的“交付前复测”由 ADR-0020 修订为 zemax 最后优化后记一轮、逐轮复测默认不做
---

# 设计轮次在 rtc 实例上计算，交付前在 OpticStudio 上复测

2026-10-29，用户决定第 3 阶段（设计轮次）不再依赖 Zemax 后端：变量、评价函数、局部优化、Hammer 与全局搜索、判定和轮次都在 rtc 实例（`saos daemon start --backend rtc`，ADR-0016 的同一种实例）上计算。CODE V 可借鉴之处一并采用。本决定修订 ADR-0013 的“从建模到交付统一经 ZOS-API standalone 计算”，把它在设计阶段的范围让给 rtc 实例，并把 ADR-0016 的实例从需求阶段扩展到设计阶段。

用户在同一次讨论中拍板了四件事：

1. **rtc 判定，交付前复测**：第 3 阶段的 `saos check` 与 `saos round` 在 rtc 上计算和判定，轮次记 `backend: rtc`。进入第 4 阶段前，用 `saos round --confirm` 在 OpticStudio 上复测要交付的那一轮；结论（达成、硬条件、可交付）与 rtc 不同时回到第 3 阶段。报告只用 OpticStudio 的数值。
2. **本次顺带补 FFT MTF**：rtc 给出 FFT MTF（`rtc.psf.mtf_fft`，`analysis("FftMtf")`），标为未对账。
3. **学 CODE V 的精确约束**：评价函数行保持 OpticStudio 的格式（类型、Int1、Int2、Data1–6、目标、权重，存进 .zmx 的 MFE 行），但边界类操作数（CTGT、MNCG、OPGT 等）不作罚项，而是阻尼最小二乘每一步里的不等式约束，用有效集与拉格朗日乘子精确满足。
4. **独立 rtc 实例**：模型确认后 `saos daemon stop`，再 `saos daemon start --backend rtc`；交付前停掉 rtc 实例，换回 OpticStudio 实例。

## 做法

- **镜头在两边之间只经 .zmx 交接**：rtc 实例读 OpticStudio 存的 .zmx，存盘时写回同样的行格式：评价函数行、变量标记（`CURV` 第 2 列、`VDSZ`、`VCON`、`VPAR`），以及 rtc 不建模的文件头行（偏振、膜层文件等，原样带过去）。rtc 没见过、也不建模的面块行（拾取等求解、膜层）一律拒绝，免得存盘的镜头与算的不同。换料变量的 .zmx 写法没有实测样本，只放在内存里。
- **结构确认照旧**：rtc 的 `describe_model(None)` 字段与 OpticStudio 相同，结构指纹按同一规则比较；rtc 读不出的偏振、切趾、膜层文件列在 `unread` 里，指纹不比较，复测时由 OpticStudio 核对。建模说明页面和用户确认仍在 OpticStudio 上做。
- **评价函数值按 OpticStudio 的定义**：MF² = ΣW(V−T)²/ΣW；边界类满足时取目标值。向导用高斯求积光瞳采样，环半径与 OpticStudio 存盘镜头里的向导行一致，行权重未对账。
- **复测记录不单独成轮**：`saos round --confirm` 追加一条 `tier: confirm` 的记录；读 ledger 时把它的数值换进被复测的那一轮（原 rtc 数值留在 `rtc` 字段），终止判断、流程图窗口和报告都按 OpticStudio 的数。`saos report` 拒绝没有复测的 rtc 轮次。
- **来源标注**：每轮记 `backend` 与 `provenance`（用到的未对账路径与操作数）；流程图窗口和报告给 rtc 轮次加标签。
- **算法是公开方法的净室实现**：`ref/mimic` 下对 Zemax 与 CODE V 的复现尝试没有可用的数值内核，只作功能清单参考；优化器按阻尼最小二乘、Marquardt 缩放、有效集 KKT 的教科书形式实现。

## 不变的部分

- 第 2 阶段（建模说明与确认）和第 4 阶段（公差、报告）在 OpticStudio 上做；`round --confirm`、`lens describe`、公差扫描在 rtc 实例上报 `BACKEND_UNSUPPORTED`。
- `ctx.fast()` 副本（ADR-0014）仍只用于探索；rtc 实例上不需要它。
- 判定脚本（`checks/`）里操作数与分析的写法不变；rtc 算不了的操作数和分析报 `BACKEND_UNSUPPORTED`，不近似。

## 后果

- 设计轮次不占 OpticStudio 许可，也不受 ZOS 工具独占（同时只能开一个优化工具）的限制。
- 新风险：rtc 与 OpticStudio 的评价函数值、优化路径不同，rtc 上达成的轮次在 OpticStudio 上可能不达成。复测是兜底：结论不同就回到设计，报告只用 OpticStudio 的数。复测发现不了 rtc 错过的更好解；这与 ADR-0016 的风险同类。
- 每准入一项（操作数、向导权重、MTF），都要在 rtc 的回归和 saos 的 `tests/test_zemax_rtc.py` 中留下对账用例。
