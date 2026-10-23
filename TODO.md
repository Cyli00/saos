# TODO

- 显性调试断开、重连的工具回归测试：连接、窗口规则、刷新、截图、阻塞对话框检测已有真实回归（`tests/test_zemax_explicit.py`，连续 4 次通过，每次约 49 s）；用户在窗口中途关闭 OpticStudio、断开后重连还没有回归测试。
- 两个配方的验收都只能在 OpticStudio 阶段运行：光纤测量库 `saos.recipes.fiber.side_view` 经 `ctx.system` 直接读 ZOS-API，在 Optiland 阶段返回 BAD_ARGS；显微物镜验收用到 Optiland 不支持的 `OSCD`、`DISG`、`MTFN`、`STRH`。用这两个配方的任务在认可前只能自写 Optiland 判定，或与用户约定 Optiland 报告不评哪几项（见两份配方的 Backend status）。待把可移植的项目改到 `ctx` 通用接口上。
- 并发评估已有 `ctx.map` 和 `saos scan`（ADR-0009，许可证上限 8 个实例）。暂缓：一个显性调试实例加多个隐式运行实例时的调度，以及与 CPU 线程数配套的每实例优化线程分配。等真实任务需要时再做。
- 没有显式孔径的非圆零件（例如只写了半口径的切边镜片）仍按圆处理，可能漏报或误报。柱面管壁按孔径画成一圈，看不出管子的长度。
- 用户恢复目录 `Documents/Zemax/Autosave/Recovered` 中的 `fiber-air.*`（2026-10-14 00:29）会让每个新启动的 OpticStudio 弹出自动保存恢复提示；saos 遇到时返回 UI_BLOCKED。是否清理由用户决定。
- 流程图窗口没有显示第 2 轮（2026-10-18，同一任务）：ledger 有第 0–2 轮，`evaluations/025/viz.json` 正常写出，用户反映第 2 轮结果没更新到界面、看不出第 1、2 轮的差别。界面服务 pid 103460 自 01:30 起运行。未查原因。
- `saos report` 只交付目标差距最小的一轮（本例 r001），其他轮次只在历史里列一行 G。用户要“交付这两轮的报告”时没有办法指定轮次或并排比较两轮的各项数值。
- 非序列还没覆盖的（2026-10-20 按 ZOS-API 2024 R1 反射核对）：光源页（偏振、光源颜色/光谱文件、Sobol/随机采样、预传播、阵列）；Type 页（Rays Ignore This Object、Consider/Ignore Objects、Object Is A Detector、探测器记录光谱）；衍射页（光栅 DLL、按级次分裂）；折射率页（GRIN、双折射）；体物理的光致发光模型（PhotoluminescenceModel：吸收、发射、量子产率文件）；混合模式（序列系统里的 NonSequential 面）；源功率单位（Watts/Lumens/Joules）；极坐标、彩色、体探测器的读数，Path Analysis；NSC 下的优化（NSDD/NSTR 进评价函数）和公差没试过。Standard Lens、CAD、多边形等常用类型没有轮廓（画不出，交叠检查也不覆盖），`GetFacetedObjectData` 的三角网格可能是通用办法，没试。方形探测器超出所在体物体时会把从体侧面漏出的光记进去（杂散荧光任务实测多记 1.8 个百分点），建模说明还没有这条检查。
