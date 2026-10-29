---
status: accepted; 第一批已落地（`saos daemon start --backend rtc`），清单与待对账项见 docs/plans/rtc-need-stage.md；ADR-0017 把同一种实例扩展到设计轮次
---

# 需求环节的可行性核算可用 rtc 已准入的数值

2026-10-29，用户决定推进需求环节，使其不再依赖 Zemax：需求环节可行性核算中，rtc 上**已对 OpticStudio 准入**的操作数与计算路径，其数值可以作为 `must` 判定（可达 / 边缘 / 不可达）的依据。本决定修订 ADR-0013 的"需求可行性使用 ZOS"，以及 ADR-0014 中"rtc 数值不作证据"一条在需求环节的适用范围；建模之后的规则不变。

## 做法

- **范围只限需求环节**：依据只用于 `explore/feasibility/README.md` 中的判定和估算。建模说明、`saos check`、轮次、重测基准、扫描计划和交付报告仍由 OpticStudio 计算。建模阶段第一次 `saos check` 的结果就是对可行性结论的复测；两者不一致时以 OpticStudio 为准，并回到用户重新说明判定。
- **只认准入项**：一个操作数或分析，在某类系统特征（孔径类型、物距有限或无限、瞄准方式、面型、材料路径）下与 OpticStudio 逐数对账通过后，才写入准入表。rtc 实例上，按手册定义实现但尚未对账的操作数与路径照常计算，结果的 `computed_by` 逐项列出；这类数值只作估算，要成为判定依据须先在 OpticStudio 上复核。rtc 没有实现的操作数、分析与处方项报 `BACKEND_UNSUPPORTED`，不近似，也不回退到其他定义。准入沿用 ADR-0014 材料准入的流程：先采集真值，再用 `--admit` 写入。
- **标注来源**：每个 rtc 数值都带 `computed_by`（`backend: rtc`、本次用到的未对账操作数、镜头走到的未对账路径）；可行性 README 里的数值同样标注，不能写成"OpticStudio 实测"。
- **没有 OpticStudio 也能运行**：需求环节的最小模型直接由处方在 rtc 中建立，不经过 `ZosBackend.export_rtc`。建立时不再逐项实时核对 OpticStudio，改为依赖离线准入表。
- **参照资料**：算法线索以 `E:\gitProjects\test-core` 为准，操作数定义以 OpticStudio 2024 R1 手册为准；实现写在 rtc 内，是否等价只由 OpticStudio 真值判定。

## 不变的部分

- 交付底线中"所有评估与交付数值来自 OpticStudio"一条仍然适用于建模之后的所有数值。
- `ctx.fast()` 副本（ADR-0014）、专家类（ADR-0015）的规则不变。
- 判定方法 `checks/` 中操作数与分析的写法不变（仍为 OpticStudio 名称）。

## 后果

- 需求访谈与可行性核算不再占用 OpticStudio 许可。
- 引入一种新风险：rtc 把可行方案误判为"不可达"，用户因此放弃方案，而建模阶段的复测发现不了，因为那个方案已不再建模。缓解办法：准入表按系统特征细分；`not reachable` 结论必须写出起决定作用的物理界限（不只是数值），以便用户能独立判断。
- 每多准入一项，都要在 rtc 的回归和 saos 的 `tests/test_zemax_rtc.py` 中留下对账用例。
