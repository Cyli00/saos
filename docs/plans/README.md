# SAOS 计划

当前计划：[Optiland 起型与 OpticStudio 优化整合计划](./integration-workflow.md)。后续范围、执行顺序、验收缺口和状态只在这一份计划维护。

2026-10-22 整理：原目录的 9 份计划/交接及旧索引共 10 份文档已归档。归档表示退出当前排程，不表示全部完成。最新 Torch 复测结果及旧待办去向已汇入新计划；本次只整理文档，没有实施后端或工作流改动。

现行行为仍以代码、命令帮助和 `skills/saos/` 为准；设计决定见 [DESIGN.md](../../DESIGN.md)。新计划中的后端选择与提前交接是拟实现行为，不能按计划文字直接绕过现有关卡。

## 历史记录

| 文档 | 保留用途 |
| --- | --- |
| [旧索引与历史计时](./archive/2026-10-optiland/index-before-consolidation.md) | 原四阶段方案、历史环境与启动/评估计时；不是当前性能保证 |
| [Plan 1：Optiland 核心](./archive/2026-10-optiland/plan-1-optiland-core.md) | 默认后端、原语下沉与早期双端对齐设计 |
| [Plan 2：优化与扫描](./archive/2026-10-optiland/plan-2-opt-and-scan.md) | SciPy/Torch、玻璃替换、扫描及默认算法的演变 |
| [Plan 3：ZOS 阶段](./archive/2026-10-optiland/plan-3-zos-verifier-and-cleanup.md) | 报告认可后的重建、复测、恢复与预算设计 |
| [Plan 4：工作台](./archive/2026-10-optiland/plan-4-ui-workbench-and-sync.md) | 暂缓的诊断图表与工作台扩展 |
| [R0–R7 综合计划](./archive/2026-10-optiland/refactor-optiland-first-workflow.md) | 原可靠性审查、完成记录、测试编号与历史决定 |
| [A–D 批次交接](./archive/2026-10-optiland/refactor-optiland-handoff.md) | 原分批范围与 B0/B2 去留清单 |
| [选型证据交接](./archive/2026-10-optiland/refactor-optiland-selection-handoff.md) | 已被后续方向替代的选型约束及当轮证据 |
| [选型修复暂停交接](./archive/2026-10-optiland/refactor-optiland-selection-fixes-handoff.md) | 当轮暂停状态；不是现在的暂停指令 |
| [超时定位与修复](./archive/2026-10-optiland/refactor-optiland-timeout-handoff.md) | 已撤回原型、后续修复记录；最新复测结论见当前计划 |

## 维护约定

- 新进展更新当前计划的状态表，注明对应版本与证据，不另建并列的“总计划”。
- 旧记录保持历史含义；不把旧通过结果移植为新版本通过，也不把旧未测状态当成最新结论。
- 临时送测说明和 HTML 报告放系统临时目录。长期保留的结论、适用版本及未测边界写入当前计划；临时文件可能被清理。
- 与本次整合无关的既有事项仍见 [TODO.md](../../TODO.md)，不复制成第二份清单。
