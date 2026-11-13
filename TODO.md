# TODO

- 并发评估已有 `ctx.map` 和 `saos scan`（许可证上限 8 个实例）。暂缓：与 CPU 线程数配套的每实例优化线程分配。等真实任务需要时再做。
- 没有显式孔径的非圆零件（例如只写了半口径的切边镜片）仍按圆处理，可能漏报或误报。柱面管壁按孔径画成一圈，看不出管子的长度。
- 用户恢复目录 `Documents/Zemax/Autosave/Recovered` 中的 `fiber-air.*`（2026-10-14 00:29）会让每个新启动的 OpticStudio 弹出自动保存恢复提示，standalone 实例会因此卡住。是否清理由用户决定。
- 流程图窗口没有显示第 2 轮（2026-10-18，同一任务）：ledger 有第 0–2 轮，`evaluations/025/viz.json` 正常写出，用户反映第 2 轮结果没更新到界面、看不出第 1、2 轮的差别。界面服务 pid 103460 自 01:30 起运行。未查原因。
- `saos report` 只交付目标差距最小的一轮（本例 r001），其他轮次只在历史里列一行 G。用户要“交付这两轮的报告”时没有办法指定轮次或并排比较两轮的各项数值。
- 非序列还没覆盖的（2026-10-20 按 ZOS-API 2024 R1 反射核对）：光源页（偏振、光源颜色/光谱文件、Sobol/随机采样、预传播、阵列）；Type 页（Rays Ignore This Object、Consider/Ignore Objects、Object Is A Detector、探测器记录光谱）；衍射页（光栅 DLL、按级次分裂）；折射率页（GRIN、双折射）；体物理的光致发光模型（PhotoluminescenceModel：吸收、发射、量子产率文件）；混合模式（序列系统里的 NonSequential 面）；源功率单位（Watts/Lumens/Joules）；极坐标、彩色、体探测器的读数，Path Analysis；NSC 下的优化（NSDD/NSTR 进评价函数）和公差没试过。Standard Lens、CAD、多边形等常用类型没有轮廓（画不出，交叠检查也不覆盖），`GetFacetedObjectData` 的三角网格可能是通用办法，没试。方形探测器超出所在体物体时会把从体侧面漏出的光记进去（杂散荧光任务实测多记 1.8 个百分点），建模说明还没有这条检查。
- GlassExpert 的成本、重量、透过率、热膨胀过滤只能由调用方的 `feasible(ctx, surface)` 提供：saos 没有材料的价格、密度、内透过率和 CTE 数据，达不到 CODE V 配好材料属性后的筛选能力。等真实任务给出这类限制时再决定数据来源。
- OpticStudio 上 `saos opt vary --param radius` 碰到被 CRVT 行盖住的格时不生效也不报错：`RadiusCell.MakeSolveVariable()` 之后存盘的 `CURV` 和 `CRVT` 仍是固定（2026-11-07 实测）。rtc 已改成设在单元格上，ZOS 后端没动。
- `rtc.zmx` 把厚度拾取的 `solve[4]` 当源面号，而热结构文件实测这一列是源行号（`solve[3]` 是源结构）。旧 pickup 的两份样本按源面号对得上，没有动，等有新样本再核。
- rtc 的 `TRAC` 与 OpticStudio 没对账：结构 1 的第 14 行差 3.7e-5 mm（sequential）、1.2e-3 mm（iris）。多结构优化的向导行用它，交付前的复测兜底。
- 2026-11-10 改的两条参考规则只按真机报告的 4 位差值离线核对过，逐行对账的真机用例 `tests/test_zemax_rtc.py::test_opticstudio_wizard_rows_have_the_same_values_on_rtc` 写好了还没跑：（1）主光线参考的行（TRAR/TRAX/TRAY、OPDC）和所有 OPD 行的参考球，在非主波长上取主波长的主光线；（2）向导的半光瞳质心组（TRAC/TRCX/TRCY、OPDX）在 Hx = 0 的视场按左右对称补全。质心行的质心是按本波长还是主波长取，4 位数字分不出来（`sequential` 上两种取法只差 1e-6 mm 量级），rtc 现在按本波长；Hx ≠ 0 的半光瞳组怎么取参考没测过，rtc 仍按组内的行求。`ctx.wavefront_rms` 和 FFT MTF 在非主波长上仍用本波长的参考球。
- `ZOS_ERROR` 这个错误码实际表示任一后端的操作失败；改名会破坏协议，暂时只在 `saos --help` 里说明。
- rtc 上 `lens save` 出现过一次 `VERIFY_FAILED`（`sph2.zmx differs after reloading from the state before saving`，2026-11-07 评测任务 evalafter），原因没查。
- `tests/test_zemax_cases.py` 录 `wavefront_rms` 时把归一化视场坐标当视场单位传给了 `ctx.wavefront_rms`，`truth.json` 里的值因此对应 0.6°、1.0° 这类视场而不是视场表的 3°、5°（2026-11-10 离线回放确认）。`tests/rtc/test_case_truth.py` 按录入时的调用回放，数值可用；下次重录时改成按视场单位传，并同步那条测试。
- 2026-11-10 真机验证：`ctx.seidel` 的逐面 W040/W131/W222/W220P/W311 与 OpticStudio 的 SPHA/COMA/ASTI/FCUR/DIST 操作数一致（1.3e-15 波），合计也与它的 Seidel 表 TOT 一致；但 Seidel 表的 W311 逐面值与 DIST 操作数本身差到 3e-3 波（W040 等四列只差 6e-7）。DIST(Int1=0) 改为像面实际畸变百分比（-0.020651% 对 0.153820 波），与 Seidel 表 TOT 不是同一量。有限物距的 OPD 在物距 100 mm 的 sequential 上与 OpticStudio OPDC 差 ≤ 2.6e-8 波。
- OPDX 还剩一处没查清：21 点整光瞳方格组（本来就左右对称，补全不改变它）上 rtc 与 OpticStudio 差到 6.97e-3 波，差几乎是一个线性函数（残差 1.8e-5 波），而向导的高斯求积组补全后离线复算与真机差值一致到 4 位。另外 `truth.json` 的 `opd` 行里 OPDX 一列与 OPDC 逐条相等：录入时用 `ctx.op` 单独求一行，质心参考要靠评价函数里的同组行才成立，这份真值验证不了 OPDX，重录时要把成组的 OPDX 行放进评价函数再读。
- rtc 向导与 OpticStudio 原生向导的行结构不同：`rings=3, arms=6` 时 rtc 每个视场/波长写 9 个光瞳点，OpticStudio 对离轴视场写 9 个、对轴上视场只写 3 个（同一 `Ring=2` 采样），所以 rtc 81 行对 OpticStudio 63 行；glass/air 边界行也一边 6 行、一边 12 行。2026-11-10 真机实测，向导建出的评价函数值因此差约一倍（Centroid：rtc 0.323395 对 OpticStudio 0.165489）。原生 `Ring` 与 saos `rings` 的换算、轴上视场的臂数和边界组数都还没对账。
- `saos backends` 的 `measurements` 没列 `ctx.spot`、`ctx.trace`、`ctx.quick_focus`：`RtcBackend` 算它们时不往 `_used` 记未对账标记，按 `provenance()` 的规则会被判成 `reconciled`，而点列 RMS/GEO 的取样与 OpticStudio 的 Standard Spot 不同、没有对账。先定点列要不要在 `RtcBackend` 里记标记，再把它们列进表。
- `BICONICZ` 在 `rtc.RECONCILED_TYPES` 里（`.zmx` 读得了），`rtc_lens.TYPES` 没有它（处方建不出），所以 `saos backends` 整表的 `surface_type` 不列它；`--lens` 的判定是对的。
- `capability.table()` 为取 `OPTICSTUDIO_ONLY`、`OPTICSTUDIO_SCANS`、`STAGE_BACKENDS` 三个常量导入了整个 `saos.daemon`。
- 两处未上真机验证：（1）后台进程 `lens build` 的预检改成在临时 `RtcBackend` 上完整建一遍后，带 Even Asphere 反射镜的处方应留在 OpticStudio（`backend_switch.stayed`）而不是在 rtc 上报 `BACKEND_UNSUPPORTED`，`tests/test_zemax_rtc.py` 还没有这条用例；（2）`rtc_lens.unread_settings` 不知道非均匀切趾在 `.zmx` 文件头里写成哪一行（仓库 836 份 `.zmx` 没有一份带切趾），现在只能报成 `header line KEY`，要一份 OpticStudio 存盘的高斯切趾样本来定名。
- 判定上收进度量目录（2026-11-13 统计 `work/` 里 16 个任务的 254 个脚本判定）：点列/PSF/包围能量 57 个（17 种 id），零件间隙与边厚 34 个，收集与 NA 31 个，畸变/视场 25 个，长度类 22 个，焦点/变焦/热 21 个，材料/质量 17 个，有效性 15 个，CRA/相对照度 7 个，MTF 6 个；95 个脚本没有 docstring。目录现有 11 项，`spot` 只算单波长、绕自身质心。最值得先收的是多色点列（参考点和波长权重要做成参数，各任务定义不一），其次是零件间隙/边厚和收集比例（`saos.micro` 已有原语）。按 `AGENTS.md`，上收前后同一批镜头的数值要逐项一致，需要把这些任务的镜头和脚本拿来对跑，还没做。

## rtc 待办（2026-11-10 从已删除的计划文档并入；当时的细节用 `git show 3c488f0:docs/plans/<文件>` 查）

- **坐标断点与面倾斜/偏心（折叠光路）**没开始，rtc 现在拒绝。要做的：`Item` 从只有 `vertex_z` 改成带全局姿态（旋转加平移），每面先转到本地坐标再求交；`COORDBRK` 的参数 1–6 是 X/Y 偏心、X/Y/Z 倾斜、顺序标志，厚度沿新的局部 z；面自身的 Tilt/Decenter 在 `.zmx` 里是 `SCBD` 行，列序要存盘确认；内核 `Packed` 加每面的旋转和平移，只有同轴面的系统走原路径、不能变慢；OpticStudio 对倾斜系统的近轴量和瞄准目标怎么算要先实测（EFFL、EPDI、倾斜光阑上的瞄准坐标）；saos 侧剖面图、交叠检查和 `view.json` 改用全局坐标；新增一个 `folded` 案例（45° 折叠平面镜加一个偏心折射面）。
- **出瞳 EXPP、EXPD**：rtc 用近轴定义，OpticStudio 用小视场实光线。瞄准开时它的读数随最大视场变，规则没定出来（`iris_real` 的用例标 `xfail`）；面很强时它用的视场角会从 0.01° 降级，降级规则也没定。已查明的部分写在 `rtc.system.OpticalSystem.exit_pupil` 的说明里。
- **自动半口径**：与 OpticStudio 对上的旋转对称取样规则（只沿 ±视场方向取边缘光线）还没成为生产默认；默认的 16 点算法在 33 个变体里有 5 个偏差超过 1e-6 mm，旋转对称最大 0.19 mm、环面 0.70 mm。双锥面、环面的规则没对上。
- **后端仍拒绝、底层已有候选实现的**：蜘蛛、椭圆孔与遮挡、矩形遮挡（`rtc_lens.from_prescription(experimental=True)`）；非球面、双锥、环面反射镜和反射后的近轴面；反射系统的波前、三阶像差、Huygens PSF；`BINARY_2`、`FRESNELS`（没有 OpticStudio 真值）。
- **光阑半口径变量**只在内存里，存盘只留数值。**换料变量**同样只在内存里：缺一份 OpticStudio 存盘样本，重新加载后要重新 `opt vary --param material`。
- **视场渐晕因子**：表外视场取曼哈顿距离最近的表内行，距离相等时怎么取没有样本；表外视场的 AD 追迹没有用例。Float By Stop Size 配瞄准 off 仍用近轴传递系数，没和 OpticStudio 的标定方式对齐。
- **遮挡**：窗口和报告叠画了遮挡折线，没做人工核对；实体交叠检查不扣除遮挡区。
- **rtc 还没有的评价函数行与操作数**：ETGT/ETLT/ETVA、STHI、STRH、RAED、RSCE、RELI（相对照度是三条光线加渐晕判定，不是 cos⁴）、DISG。
- **还没对账的**：向导行的权重、MNEG 等边缘厚度、FFT MTF（rtc 各波长以自己的主光线为参考，OpticStudio 以主波长主光线为参考）、点列 RMS/GEO（rtc 是 31×31 方格，与 Standard Spot 的取样不同）、优化路径（步长和阻尼与 OpticStudio 的 DLS 不同，局部极小可能不同）。
- **局部优化加速**（基线用 `tests/bench_rtc_optim.py` 测，2026-11-06：38 个变量、192 个残差，一次求值 4.8 ms，其中内核追迹只占 0.57 ms；雅可比 190 ms，占一次迭代的 87%）：切向导数（`rtc.adtrace`）接进 `rtc_optim`，先接 TRAC/TRAR/OPDX 残差行，要补 OPD 参考球、OPDX 的 piston 与倾斜、质心组、求解和模型玻璃的导数；差分时冻结自动半口径，只在评价函数不依赖半口径、也没有浮动孔径时做。切向没有差分噪声（带边界的 OPDX 案例里差分雅可比个别列相对差到 2.35e-2）。
- **`BINARY_2` 的手写文件**：文件写 N=0 却带 A1 时，OpticStudio 的实光线不用 A1、近轴量却用，rtc 的 EFFL、EXPP、EXPD 与它不一致（一个样例 EFFL 差 3.6%）。OpticStudio 另存后会清掉未启用的系数，重载后两边一致。
- **材料**：色散公式 4、10 没有真实目录样品，只有合成系数的观测；ZTG/MIL、GRIN、双折射、逐面局部环境、TCE 几何缩放没接入。
- **Expert 类**：与 CODE V 的速度对比没跑完（`tests/bench_experts.py`；CODE V 的 Glass Expert 双胶合 210.6 s、6 片专利镜头 1020.5 s）；CODE V 的 Asphere Expert 在 0 个非球面时的误差函数与 Glass Expert 的初值不一致，原因没查；切向排名在光阑前候选面上没和 OpticStudio 比过。
- **用 ZEBASE 选起点**：只有草案，没实现。动手前要用户确认许可条款：能否用程序只读访问 `ZebaseDb.db` 的明文规格表；能否把设计另存为 `.zmx` 放进任务目录；以它为起点的镜头交付第三方有没有限制；出处是专利的设计是否要做侵权排查。
