# TODO

- 光纤束侧视荧光镜头：saos 的第一个真实任务（DESIGN.md「近期工作」第 7 条）。不预设配方，用工具加脚本完成，记录遇到的工具缺口，做完后提炼成配方。已有材料在 zea 仓库的运行目录 `work/native-fiber/jobs/20261014-100420-fiber-hemiellipsoid/`：需求访谈原话在 `sources/interview.md`，初始框架探索记录在 `notes/native-framework.md`；框架搭建脚本在 `experiments/fiber/`。这些材料按旧流程写成，需求框定要按新方式重新与用户确认。
- 显性调试断开、重连的工具回归测试：自动准备 Interactive Extension（选择 Programming、打开对话框、关闭 Auto Close、读取实例编号）已实现并在本机连接成功，一次约 10 s；异常中断和重连还没有回归测试。
- 实测许可证的并行实例上限（含手动打开的窗口），以及本机可稳定运行的并行数量。
- 暂缓：一个显性调试实例加多个隐式运行实例的并行调度，以及与 CPU 线程数配套的每实例优化线程分配。等真实任务需要并行时再做。
