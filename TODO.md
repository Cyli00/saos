# TODO

- 光纤束侧视荧光镜头：saos 的第一个真实任务（DESIGN.md「近期工作」第 1 条，需要用户参与整理需求单，不能由 agent 单独完成）。不预设配方，用工具加脚本完成，记录遇到的工具缺口，做完后提炼成配方。已有材料在 zea 仓库的运行目录 `work/native-fiber/jobs/20261014-100420-fiber-hemiellipsoid/`：需求访谈原话在 `sources/interview.md`，初始框架探索记录在 `notes/native-framework.md`；框架搭建脚本在 `experiments/fiber/`。这些材料按旧流程写成，需求单要按新方式重新与用户确认。
- 显性调试断开、重连的工具回归测试：连接、窗口规则、刷新、截图、阻塞对话框检测已有真实回归（`tests/test_zemax_explicit.py`，连续 4 次通过，每次约 49 s）；用户在窗口中途关闭 OpticStudio、断开后重连还没有回归测试。
- 并发评估已有 `ctx.parallel`（ADR-0008，许可证上限 8 个实例）。暂缓：一个显性调试实例加多个隐式运行实例时的调度，以及与 CPU 线程数配套的每实例优化线程分配。等真实任务需要时再做。
- 入口技能 `skills/saos` 尚未链接到 `~/.agents/skills`（DESIGN.md「架构」要求）；saos 分支合并前不链接，避免其他会话加载未合并的技能。
- 用户恢复目录 `Documents/Zemax/Autosave/Recovered` 中的 `fiber-air.*`（2026-10-14 00:29）会让每个新启动的 OpticStudio 弹出自动保存恢复提示；saos 遇到时返回 UI_BLOCKED。是否清理由用户决定。
