# SAOS 计划

当前实现依据为 [ADR-0013](../adr/0013-zos-standalone-only.md) 和 [DESIGN.md](../../DESIGN.md)：保留当前工作流，正式计算统一使用 ZOS standalone。

[原整合计划](integration-workflow.md) 已停止执行，保留历史取舍与测量记录。Optiland/Torch 实验在 `archive/optiland-hybrid` 查阅；旧测量不代表此次迁移通过测试。其他待办见 [TODO.md](../../TODO.md)。

测试由用户统一安排。本次实现未运行自动化测试、ZOS 实测或浏览器/流程图渲染。交接重点是 daemon 连接与重启、原生镜头保存恢复、起点预判与等预算初筛、取消与许可释放、轮次重测和报告，以及流程图中英节点与标签页。临时测试报告放系统临时目录。
