# TODO

- 并发评估已有 `ctx.map` 和 `saos scan`（许可证上限 8 个实例）。暂缓：与 CPU 线程数配套的每实例优化线程分配。等真实任务需要时再做。
- 没有显式孔径的非圆零件（例如只写了半口径的切边镜片）仍按圆处理，可能漏报或误报。柱面管壁按孔径画成一圈，看不出管子的长度。
- 用户恢复目录 `Documents/Zemax/Autosave/Recovered` 中的 `fiber-air.*`（2026-10-14 00:29）会让每个新启动的 OpticStudio 弹出自动保存恢复提示，standalone 实例会因此卡住。是否清理由用户决定。
- 流程图窗口没有显示第 2 轮（2026-10-18，同一任务）：ledger 有第 0–2 轮，`evaluations/025/viz.json` 正常写出，用户反映第 2 轮结果没更新到界面、看不出第 1、2 轮的差别。界面服务 pid 103460 自 01:30 起运行。未查原因。
- `saos report` 只交付目标差距最小的一轮（本例 r001），其他轮次只在历史里列一行 G。用户要“交付这两轮的报告”时没有办法指定轮次或并排比较两轮的各项数值。
- 非序列还没覆盖的（第 0 步定为不在范围内，等用户的 issue 再做；2026-10-20 按 ZOS-API 2024 R1 反射核对）：光源页（偏振、光源颜色/光谱文件、Sobol/随机采样、预传播、阵列）；Type 页（Rays Ignore This Object、Consider/Ignore Objects、Object Is A Detector、探测器记录光谱）；衍射页（光栅 DLL、按级次分裂）；折射率页（GRIN、双折射）；体物理的光致发光模型（PhotoluminescenceModel：吸收、发射、量子产率文件）；混合模式（序列系统里的 NonSequential 面）；源功率单位（Watts/Lumens/Joules）；极坐标、彩色、体探测器的读数，Path Analysis；NSC 下的优化（NSDD/NSTR 进评价函数）和公差没试过。Standard Lens、CAD、多边形等常用类型没有轮廓（画不出，交叠检查也不覆盖），`GetFacetedObjectData` 的三角网格可能是通用办法，没试。方形探测器超出所在体物体时会把从体侧面漏出的光记进去（杂散荧光任务实测多记 1.8 个百分点），建模说明还没有这条检查。
- GlassExpert 的成本、重量、透过率、热膨胀过滤只能由调用方的 `feasible(ctx, surface)` 提供：saos 没有材料的价格、密度、内透过率和 CTE 数据，达不到 CODE V 配好材料属性后的筛选能力。等真实任务给出这类限制时再决定数据来源。
- OpticStudio 上 `saos opt vary --param radius` 碰到被 CRVT 行盖住的格时不生效也不报错：`RadiusCell.MakeSolveVariable()` 之后存盘的 `CURV` 和 `CRVT` 仍是固定（2026-11-07 实测）。rtc 已改成设在单元格上，ZOS 后端没动。
- `rtc.zmx` 把厚度拾取的 `solve[4]` 当源面号，而热结构文件实测这一列是源行号（`solve[3]` 是源结构）。旧 pickup 的两份样本按源面号对得上，没有动，等有新样本再核。
- 被孔径挡住的光线在质心行里怎么算，rtc 与 OpticStudio 不一致（2026-11-14 真机，`thermal_variants` 的 `iris_6`，rtc 写的 Spot Radius / Centroid 向导）：54 行 TRAC 的光线被孔径挡住，rtc 拒绝给值，评价函数为 inf；OpticStudio 照样给值（评价函数 0.0919），其余 TRAC 行差到 0.067 mm，应是质心是否计入被挡光线的差别。`seq_3` 在复色质心修正后逐行一致（9e-15 mm）。
- OPDX（Wavefront / Centroid 向导）的参考没对上：2026-11-14 真机在 `sequential` 上逐行差到 0.0236 个波，`tests/test_zemax_rtc.py::test_opticstudio_wizard_rows_have_the_same_values_on_rtc[Wavefront-Centroid]` 标为 strict xfail。离线试过的参考取法（`sequential` 的向导 .zmx 与 OpticStudio 读数）：各波长各去 piston 与倾斜（现行）0.0236；各波长去 piston、几何倾斜各波长共用 2.4e-3；参考球心移到复色质心 0.0296；piston 共用的几种都在 0.17 以上。同一用例的另外 4 组（Wavefront 的 Chief Ray、Unreferenced，Spot Radius 的 Centroid、Chief Ray）已通过：非主波长上主光线参考与 OPD 参考球取主波长的主光线、Hx = 0 半光瞳组按对称补全，这两条规则成立；TRAC/TRCX/TRCY 的质心是同一视场各波长共用的复色质心（已改，`tests/test_rtc_backend.py` 有回归）。Hx ≠ 0 的半光瞳组怎么取参考没测过；`ctx.wavefront_rms` 和 FFT MTF 在非主波长上仍用本波长的参考球。
- `ZOS_ERROR` 这个错误码实际表示任一后端的操作失败；改名会破坏协议，暂时只在 `saos --help` 里说明。
- rtc 上 `lens save` 出现过一次 `VERIFY_FAILED`（`sph2.zmx differs after reloading from the state before saving`，2026-11-07 评测任务 evalafter），原因没查。
- `tests/test_zemax_cases.py` 录 `wavefront_rms` 时把归一化视场坐标当视场单位传给了 `ctx.wavefront_rms`，`truth.json` 里的值因此对应 0.6°、1.0° 这类视场而不是视场表的 3°、5°（2026-11-10 离线回放确认）。`tests/rtc/test_case_truth.py` 按录入时的调用回放，数值可用；下次重录时改成按视场单位传，并同步那条测试。
- 2026-11-10 真机验证：`ctx.seidel` 的逐面 W040/W131/W222/W220P/W311 与 OpticStudio 的 SPHA/COMA/ASTI/FCUR/DIST 操作数一致（1.3e-15 波），合计也与它的 Seidel 表 TOT 一致；但 Seidel 表的 W311 逐面值与 DIST 操作数本身差到 3e-3 波（W040 等四列只差 6e-7）。DIST(Int1=0) 改为像面实际畸变百分比（-0.020651% 对 0.153820 波），与 Seidel 表 TOT 不是同一量。有限物距的 OPD 在物距 100 mm 的 sequential 上与 OpticStudio OPDC 差 ≤ 2.6e-8 波。
- OPDX 还剩一处没查清：21 点整光瞳方格组（本来就左右对称，补全不改变它）上 rtc 与 OpticStudio 差到 6.97e-3 波，差几乎是一个线性函数（残差 1.8e-5 波），而向导的高斯求积组补全后离线复算与真机差值一致到 4 位。另外 `truth.json` 的 `opd` 行里 OPDX 一列与 OPDC 逐条相等：录入时用 `ctx.op` 单独求一行，质心参考要靠评价函数里的同组行才成立，这份真值验证不了 OPDX，重录时要把成组的 OPDX 行放进评价函数再读。
- rtc 向导与 OpticStudio 原生向导的行结构不同：`rings=3, arms=6` 时 rtc 每个视场/波长写 9 个光瞳点，OpticStudio 对离轴视场写 9 个、对轴上视场只写 3 个（同一 `Ring=2` 采样），所以 rtc 81 行对 OpticStudio 63 行；glass/air 边界行也一边 6 行、一边 12 行。2026-11-10 真机实测，向导建出的评价函数值因此差约一倍（Centroid：rtc 0.323395 对 OpticStudio 0.165489）。原生 `Ring` 与 saos `rings` 的换算、轴上视场的臂数和边界组数都还没对账。
- `BICONICZ` 在 `rtc.RECONCILED_TYPES` 里（`.zmx` 读得了），`rtc_lens.TYPES` 没有它（处方建不出），所以 `saos backends` 整表的 `surface_type` 不列它；`--lens` 的判定是对的。
- `capability.table()` 为取 `OPTICSTUDIO_ONLY`、`OPTICSTUDIO_SCANS`、`STAGE_BACKENDS` 三个常量导入了整个 `saos.daemon`。
- 两处未上真机验证：（1）后台进程 `lens build` 的预检改成在临时 `RtcBackend` 上完整建一遍后，带 Even Asphere 反射镜的处方应留在 OpticStudio（`backend_switch.stayed`）而不是在 rtc 上报 `BACKEND_UNSUPPORTED`，`tests/test_zemax_rtc.py` 还没有这条用例；（2）`rtc_lens.unread_settings` 不知道非均匀切趾在 `.zmx` 文件头里写成哪一行（仓库 836 份 `.zmx` 没有一份带切趾），现在只能报成 `header line KEY`，要一份 OpticStudio 存盘的高斯切趾样本来定名。
- 判定上收进度量目录（2026-11-13 统计 `work/` 里 18 份需求单的 254 个脚本判定；其中 `20261015-205649-fiber-grin-no1b` 在 `fiber`、`fiber-no1b` 两个工作区各有一份，按任务去重是 17 个任务、237 个）。2026-11-14 按判定 id 逐个重新归类（括号里是 id 种数）：点列/PSF/包围能量/波前 57（17），零件间隙与边厚 35（11），收集与 NA 31（7），畸变/视场 25（4），材料与透过率 24（14），长度类 23（11），焦点/变焦/热/F 数 22（14），有效性 15（6），外形尺寸与质量 7（4），CRA/相对照度 7（4），MTF 6（4），另有 2 个未归类（`monotonic`、`stat_error`），合计 254。原来的十类合计 235，间隙、长度、焦点、材料四类的边界没记下来，无法复核；95 个脚本没有 docstring。目录现有 11 项，`spot` 只算单波长、绕自身质心。最值得先收的是多色点列（参考点和波长权重要做成参数，各任务定义不一），其次是零件间隙/边厚和收集比例（`saos.micro` 已有原语）。按 `AGENTS.md`，上收前后同一批镜头的数值要逐项一致，需要把这些任务的镜头和脚本拿来对跑，还没做。
- 起型子 agent 实验（2026-11-13 暂缓，起型仍由主会话做）：当时的子 agent 草稿（只读检索、一阶/三阶估算、`opus` + `effort: high`）已按用户要求删除，重启时重写。评测方案已定：取 `work/` 里 ar-glasses-camera、lll-nightvision-obj、ar-glasses-telephoto、fiber-air 四份需求单复制成新任务，对照组主会话自己起型、实验组委托子 agent，之后同一优化预算、autonomous、只在 rtc 上跑，每格 3 次共 24 次；主量是最后一轮 `shortfall`（unreconciled 单列），另比 reach gate 是否通过、起型耗时与 token。重启前要先补工具隔离：frontmatter 的 `tools` 只能整件开关，按命令收窄要靠 frontmatter 的 `PreToolUse` hook；专利检索现在只能 `python -c` 调、hook 审查不了，需要一个只读的检索/缩放 CLI；草稿没给 Write 却要求写处方，`saos measure` 也不收处方。`effort` 会被环境变量 `CLAUDE_CODE_EFFORT_LEVEL` 覆盖。
- 通用接口计划（2026-11-13）原定验收里没达成、本轮不做的：
  - ctx 方法的 docstring 没有逐个写齐参数与单位、返回结构、后端差别、错误码四项（例：`ctx.op` 缺返回结构和单位）。
  - `saos backends` 的能力表没有拿 `tests/rtc/omnibus.py` 的全覆盖案例核对；预测为 `refused` 的操作数只测了 MTFT 一个。
  - 原要求技能里点名后端的行数明显下降，2026-11-13 计数反而由 57 增至 61。
  - 没有用 `non_sequential`、`mixed`、`reflective` 三个案例逐节走查阶段 1–2 的文字。
  - `saos measure` 只收操作数和度量目录项，追迹和点列没有无状态入口（脚本仍走 `exec`，分析走 `lens analysis`）。

## rtc 待办（2026-11-10 从已删除的计划文档并入；当时的细节用 `git show 3c488f0:docs/plans/<文件>` 查）

按对通用接口的阻塞程度排（2026-11-13，计划第 11 步）：前两项决定序列系统（第 0 步定的 A2）能不能整段留在 rtc 上，其后是会把整份镜头或整类判定挡在 rtc 外面的，再往后是数值差异和性能。非序列不在范围内。

- **坐标断点与面倾斜/偏心（折叠光路）**没开始，rtc 现在拒绝。要做的：`Item` 从只有 `vertex_z` 改成带全局姿态（旋转加平移），每面先转到本地坐标再求交；`COORDBRK` 的参数 1–6 是 X/Y 偏心、X/Y/Z 倾斜、顺序标志，厚度沿新的局部 z；面自身的 Tilt/Decenter 在 `.zmx` 里是 `SCBD` 行，列序要存盘确认；内核 `Packed` 加每面的旋转和平移，只有同轴面的系统走原路径、不能变慢；OpticStudio 对倾斜系统的近轴量和瞄准目标怎么算要先实测（EFFL、EPDI、倾斜光阑上的瞄准坐标）；saos 侧剖面图、交叠检查和 `view.json` 改用全局坐标；新增一个 `folded` 案例（45° 折叠平面镜加一个偏心折射面）。
- **Huygens PSF**：`rtc.psf.psf_huygens` 已有，但出瞳取法与 OpticStudio 没对上账（变形面不一致），所以 `ctx.huygens` 在 rtc 上报 `BACKEND_UNSUPPORTED`，吃它结果的 `kit.psf_widths`、`kit.psf_axial` 也用不了，所以衍射类判定（PSF 宽度、Strehl、包围能量）在设计轮次里只能占许可证上 OpticStudio。2026-11-13 统计的 254 个脚本判定里，点列/PSF/包围能量类有 57 个，其中 psf_x/psf_y/psf_z、strehl_ratio、ee80 属于这一类。要按 OpticStudio Huygens 的出瞳与取样约定逐项对账后再放开。
- **后端仍拒绝、底层已有候选实现的**：蜘蛛、椭圆孔与遮挡、矩形遮挡（`rtc_lens.from_prescription(experimental=True)`）；非球面、双锥、环面反射镜和反射后的近轴面；反射系统的波前、三阶像差、Huygens PSF；`BINARY_2`、`FRESNELS`（没有 OpticStudio 真值）。
- **rtc 还没有的评价函数行与操作数**：ETGT/ETLT/ETVA、STHI、STRH、RAED、RSCE、RELI（相对照度是三条光线加渐晕判定，不是 cos⁴）、DISG。
- **还没对账的**：向导行的权重、MNEG 等边缘厚度、FFT MTF（rtc 各波长以自己的主光线为参考，OpticStudio 以主波长主光线为参考）、点列 RMS/GEO（rtc 是 31×31 方格，与 Standard Spot 的取样不同）、优化路径（步长和阻尼与 OpticStudio 的 DLS 不同，局部极小可能不同）。
- **自动半口径**：与 OpticStudio 对上的旋转对称取样规则（只沿 ±视场方向取边缘光线）还没成为生产默认；默认的 16 点算法在 33 个变体里有 5 个偏差超过 1e-6 mm，旋转对称最大 0.19 mm、环面 0.70 mm。双锥面、环面的规则没对上。
- **出瞳 EXPP、EXPD**：rtc 用近轴定义，OpticStudio 用小视场实光线。瞄准开时它的读数随最大视场变，规则没定出来（`iris_real` 的用例标 `xfail`）；面很强时它用的视场角会从 0.01° 降级，降级规则也没定。已查明的部分写在 `rtc.system.OpticalSystem.exit_pupil` 的说明里。
- **视场渐晕因子**：表外视场取曼哈顿距离最近的表内行，距离相等时怎么取没有样本；表外视场的 AD 追迹没有用例。Float By Stop Size 配瞄准 off 仍用近轴传递系数，没和 OpticStudio 的标定方式对齐。
- **遮挡**：窗口和报告叠画了遮挡折线，没做人工核对；实体交叠检查不扣除遮挡区。
- **光阑半口径变量**只在内存里，存盘只留数值。**换料变量**同样只在内存里：缺一份 OpticStudio 存盘样本，重新加载后要重新 `opt vary --param material`。
- **局部优化加速**（基线用 `tests/bench_rtc_optim.py` 测，2026-11-06：38 个变量、192 个残差，一次求值 4.8 ms，其中内核追迹只占 0.57 ms；雅可比 190 ms，占一次迭代的 87%）：切向导数（`rtc.adtrace`）接进 `rtc_optim`，先接 TRAC/TRAR/OPDX 残差行，要补 OPD 参考球、OPDX 的 piston 与倾斜、质心组、求解和模型玻璃的导数；差分时冻结自动半口径，只在评价函数不依赖半口径、也没有浮动孔径时做。切向没有差分噪声（带边界的 OPDX 案例里差分雅可比个别列相对差到 2.35e-2）。
- **`BINARY_2` 的手写文件**：文件写 N=0 却带 A1 时，OpticStudio 的实光线不用 A1、近轴量却用，rtc 的 EFFL、EXPP、EXPD 与它不一致（一个样例 EFFL 差 3.6%）。OpticStudio 另存后会清掉未启用的系数，重载后两边一致。
- **材料**：色散公式 4、10 没有真实目录样品，只有合成系数的观测；ZTG/MIL、GRIN、双折射、逐面局部环境、TCE 几何缩放没接入。
- **Expert 类**：与 CODE V 的速度对比没跑完（`tests/bench_experts.py`；CODE V 的 Glass Expert 双胶合 210.6 s、6 片专利镜头 1020.5 s）；CODE V 的 Asphere Expert 在 0 个非球面时的误差函数与 Glass Expert 的初值不一致，原因没查；切向排名在光阑前候选面上没和 OpticStudio 比过。
- **用 ZEBASE 选起点**：只有草案，没实现。动手前要用户确认许可条款：能否用程序只读访问 `ZebaseDb.db` 的明文规格表；能否把设计另存为 `.zmx` 放进任务目录；以它为起点的镜头交付第三方有没有限制；出处是专利的设计是否要做侵权排查。
