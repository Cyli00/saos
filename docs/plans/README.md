# SAOS 计划

当前实现依据为 [DESIGN.md](../../DESIGN.md) 与 [docs/adr/](../adr/)。进行中的计划：

- [rtc 材料集成与加速](rtc-material-integration.md)：准入表的扩充流程与运行命令（ADR-0014）。
- [需求环节脱离 OpticStudio](rtc-need-stage.md)：rtc 能力补齐、准入与 saos 集成的分批计划（ADR-0016）。
- [设计轮次脱离 OpticStudio](rtc-design-stage.md)：rtc 实例上的评价函数、约束优化、搜索与交付前复测（ADR-0017）。
- [用 ZEBASE 选起点](zebase-starts.md)：草案，待确认许可条款后实现。

已实现功能的接口与算法细节：

- [光学 Expert 类](optical-experts.md)：`ctx.AsphereExpert` / `ctx.GlassExpert` 的选项、算法与待验收项（ADR-0015）。

已取代的 Optiland 整合计划与实验在 `archive/optiland-hybrid` 分支查阅。其他待办见 [TODO.md](../../TODO.md)。
