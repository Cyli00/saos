# TODO

- 并发评估已有 `ctx.map` 和 `saos scan`（ADR-0009，许可证上限 8 个实例）。暂缓：与 CPU 线程数配套的每实例优化线程分配。等真实任务需要时再做。
- 没有显式孔径的非圆零件（例如只写了半口径的切边镜片）仍按圆处理，可能漏报或误报。柱面管壁按孔径画成一圈，看不出管子的长度。
- 用户恢复目录 `Documents/Zemax/Autosave/Recovered` 中的 `fiber-air.*`（2026-10-14 00:29）会让每个新启动的 OpticStudio 弹出自动保存恢复提示，standalone 实例会因此卡住。是否清理由用户决定。
- 流程图窗口没有显示第 2 轮（2026-10-18，同一任务）：ledger 有第 0–2 轮，`evaluations/025/viz.json` 正常写出，用户反映第 2 轮结果没更新到界面、看不出第 1、2 轮的差别。界面服务 pid 103460 自 01:30 起运行。未查原因。
- `saos report` 只交付目标差距最小的一轮（本例 r001），其他轮次只在历史里列一行 G。用户要“交付这两轮的报告”时没有办法指定轮次或并排比较两轮的各项数值。
- 非序列还没覆盖的（2026-10-20 按 ZOS-API 2024 R1 反射核对）：光源页（偏振、光源颜色/光谱文件、Sobol/随机采样、预传播、阵列）；Type 页（Rays Ignore This Object、Consider/Ignore Objects、Object Is A Detector、探测器记录光谱）；衍射页（光栅 DLL、按级次分裂）；折射率页（GRIN、双折射）；体物理的光致发光模型（PhotoluminescenceModel：吸收、发射、量子产率文件）；混合模式（序列系统里的 NonSequential 面）；源功率单位（Watts/Lumens/Joules）；极坐标、彩色、体探测器的读数，Path Analysis；NSC 下的优化（NSDD/NSTR 进评价函数）和公差没试过。Standard Lens、CAD、多边形等常用类型没有轮廓（画不出，交叠检查也不覆盖），`GetFacetedObjectData` 的三角网格可能是通用办法，没试。方形探测器超出所在体物体时会把从体侧面漏出的光记进去（杂散荧光任务实测多记 1.8 个百分点），建模说明还没有这条检查。
- GlassExpert 的成本、重量、透过率、热膨胀过滤只能由调用方的 `feasible(ctx, surface)` 提供：saos 没有材料的价格、密度、内透过率和 CTE 数据，达不到 CODE V 配好材料属性后的筛选能力。等真实任务给出这类限制时再决定数据来源。
- OpticStudio 上 `saos opt vary --param radius` 碰到被 CRVT 行盖住的格时不生效也不报错：`RadiusCell.MakeSolveVariable()` 之后存盘的 `CURV` 和 `CRVT` 仍是固定（2026-11-07 实测）。rtc 已改成设在单元格上，ZOS 后端没动。
- `rtc.zmx` 把厚度拾取的 `solve[4]` 当源面号，而热结构文件实测这一列是源行号（`solve[3]` 是源结构）。旧 pickup 的两份样本按源面号对得上，没有动，等有新样本再核。
- rtc 的 `TRAC` 与 OpticStudio 没对账：结构 1 的第 14 行差 3.7e-5 mm（sequential）、1.2e-3 mm（iris）。多结构优化的向导行用它，交付前的复测兜底。
- rtc 的 `TRAR/TRAC/OPDC/OPDX` 按每个波长自己的主光线或质心参考（`rtc_merit`），所以横向色差不进向导行。OpticStudio 的参考是不是也按波长还没实测：要对账时在真机上比一个有明显横向色差的镜头。
- `ZOS_ERROR` 这个错误码实际表示任一后端的操作失败；改名会破坏协议，暂时只在 `saos --help` 里说明。
- rtc 上 `lens save` 出现过一次 `VERIFY_FAILED`（`sph2.zmx differs after reloading from the state before saving`，2026-11-07 评测任务 evalafter），原因没查。
