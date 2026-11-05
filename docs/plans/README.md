# SAOS 计划

当前实现依据为 [DESIGN.md](../../DESIGN.md) 与 [docs/adr/](../adr/)。进行中的计划：

- [rtc 材料集成与加速](rtc-material-integration.md)：准入表的扩充流程与运行命令（ADR-0014）。
- [需求环节脱离 OpticStudio](rtc-need-stage.md)：rtc 能力补齐、准入与 saos 集成的分批计划（ADR-0016）。
- [设计轮次脱离 OpticStudio](rtc-design-stage.md)：rtc 实例上的评价函数、约束优化、搜索与交付前复测（ADR-0017）。
- [rtc 补齐孔径光阑与反射面](rtc-iris-mirror.md)：Float By Stop Size、面孔径、渐晕因子、同轴反射镜的分阶段计划，坐标断点另行立项。D1/D2/D4/D5 修复、R1–R9 实现或候选入口已整合，第 7.3 节验证已运行，结果与未解决项见计划第 9 节。
- [rtc 核心算法归一](rtc-unify.md)：一个带切向的单步派生追迹、雅可比、瞄准，只留一份实现。
- [用 ZEBASE 选起点](zebase-starts.md)：草案，待确认许可条款后实现。

已实现功能的接口与算法细节：

- [光学 Expert 类](optical-experts.md)：`ctx.AsphereExpert` / `ctx.GlassExpert` 的选项、算法与待验收项（ADR-0015）。

rtc 追迹内核的依据（文中的 `fixtures/`、`tools/`、`oracle/` 是原 rtc 仓库的路径）：

- [rtc/SURFACE-PARAMS.md](rtc/SURFACE-PARAMS.md)：面型参数编号对照。
- [rtc/MATERIAL.md](rtc/MATERIAL.md)：材料与色散公式。
- [rtc/GAP-ANALYSIS.md](rtc/GAP-ANALYSIS.md)：逐项差距。
- [rtc/HANDOFF.md](rtc/HANDOFF.md)：追迹细节的排查记录。

已取代的 Optiland 整合计划与实验留在提交历史里（`git log --oneline --grep optiland`）。其他待办见 [TODO.md](../../TODO.md)。
