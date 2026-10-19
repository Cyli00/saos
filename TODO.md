# TODO

- 光纤束侧视荧光镜头：saos 的第一个真实任务（DESIGN.md「近期工作」第 1 条，需要用户参与整理需求单，不能由 agent 单独完成）。不预设配方，用工具加脚本完成，记录遇到的工具缺口，做完后提炼成配方。已有材料在 zea 仓库的运行目录 `work/native-fiber/jobs/20261014-100420-fiber-hemiellipsoid/`：需求访谈原话在 `sources/interview.md`，初始框架探索记录在 `notes/native-framework.md`；框架搭建脚本在 `experiments/fiber/`。这些材料按旧流程写成，需求单要按新方式重新与用户确认。
- 显性调试断开、重连的工具回归测试：连接、窗口规则、刷新、截图、阻塞对话框检测已有真实回归（`tests/test_zemax_explicit.py`，连续 4 次通过，每次约 49 s）；用户在窗口中途关闭 OpticStudio、断开后重连还没有回归测试。
- 并发评估已有 `ctx.map` 和 `saos scan`（ADR-0008、ADR-0009，许可证上限 8 个实例）。暂缓：一个显性调试实例加多个隐式运行实例时的调度，以及与 CPU 线程数配套的每实例优化线程分配。等真实任务需要时再做。
- 入口技能 `skills/saos` 尚未链接到 `~/.agents/skills`（DESIGN.md「架构」要求）；saos 分支合并前不链接，避免其他会话加载未合并的技能。
- `saos ui` 点列图与 Standard Spot 的 GEO 对不上（2026-10-15，Cooke 40 degree field，31×31 方形光瞳网格，参考主波长主光线）：0° 视场画出的最远点 9.39 µm、GEO 9.34 µm；14° 视场主波长的最远点 40.78 µm 与 GEO 40.78 µm 相等，但全部波长的最远点是 47.80 µm；20° 视场全部波长 36.63 µm、主波长 22.53 µm，GEO 24.16 µm。三个视场 RMS 偏差在 8% 以内（5.16/15.00/11.79 对 4.99/16.18/12.07 µm）。GetGeoSpotSizeFor 对三个波长返回同一个值，尚不清楚它取的是哪些光线；界面标题用 OpticStudio 的值，点云来自批量光线追迹。查清前不要拿点云范围当 GEO。
- `saos ui` 显性调试的窗口控制只用假后端和接口测试验证过，还没有在 Interactive Extension 下点过（需要用户打开的 OpticStudio 窗口）。
- 交叠检查和布局按面孔径取零件边缘；没有显式孔径的非圆零件（例如只写了半口径的切边镜片）仍按圆处理，可能漏报或误报。柱面管壁按孔径画成一圈，看不出管子的长度。
- 用户恢复目录 `Documents/Zemax/Autosave/Recovered` 中的 `fiber-air.*`（2026-10-14 00:29）会让每个新启动的 OpticStudio 弹出自动保存恢复提示；saos 遇到时返回 UI_BLOCKED。是否清理由用户决定。
- 轮次可视化导出的 MTF 为空（2026-10-18，fiber-printed 任务第 0–2 轮，`evaluations/023–025/viz.json`）：`mtf.series` 为空、`errors` 也为空，设置 `MaximumFrequency 0.0`（自动）、`S_64x64`。系统为物方 NA 0.5、像方在水中、经 45° 折转与柱面管壁；失败没有报出来，用户在流程图窗口看到 MTF 导出失败。待查：自动最高频率在这种系统上取到 0，还是 FFT MTF 本身不返回数据；失败应写进 `errors`。
- 流程图窗口没有显示第 2 轮（2026-10-18，同一任务）：ledger 有第 0–2 轮，`evaluations/025/viz.json` 正常写出，用户反映第 2 轮结果没更新到界面、看不出第 1、2 轮的差别。界面服务 pid 103460 自 01:30 起运行。未查原因。
- `saos report` 只交付目标差距最小的一轮（本例 r001），其他轮次只在历史里列一行 G。用户要“交付这两轮的报告”时没有办法指定轮次或并排比较两轮的各项数值。
