# saos 命令行

agent 只通过 `uv run saos ...` 操作 Zemax。每条命令向标准输出打印一个 JSON 对象：

- 成功：`{"ok": true, "result": ...}`，退出码 0
- 失败：`{"ok": false, "error": {"code", "message", "details"}}`，退出码 1；命令行参数错误时退出码为 2，格式相同

`daemon`、`job` 命令在命令行进程内执行；`lens`、`opt`、`exec` 命令转发给常驻后台进程，由它持有 OpticStudio 连接（ADR-0002）。`microscope` 下是显微物镜配方的命令，其中复现、验收、扰动通过 `exec` 在后台执行。

## 命令

| 命令 | 作用 |
|---|---|
| `saos daemon start [--mode standalone\|extension]` | 启动后台进程并连接 OpticStudio 2024 R1，就绪后返回。本机实测约 6 s。`extension` 自动准备已打开窗口的 Interactive Extension 并连接，见下文 |
| `saos daemon status [--no-env]` | 是否在运行、连接是否正常、当前加载的镜头；另附 `environment`：各实例后台进程的模式和正在跑的任务（只读状态文件，不连管道）、OpticStudio 进程数与许可余量（上限 8）、可见主窗口、extension 模式现在能否启动及会用哪个窗口。`--no-env` 只查本实例 |
| `saos daemon stop [--discard] [--force]` | 正常退出，等后台进程和它的 OpticStudio 都退出后才返回。有未保存修改时拒绝退出，除非加 `--discard`；有分离运行的扫描时返回 `BUSY`，先 `saos job cancel` 或等它结束；`--force` 在无法正常退出时强制结束 |
| `saos job create --name N` | 新建设计任务，编号为 `时间-名字`；建 `lenses/`、`sources/` 和 `job.json`，不建任何配方目录 |
| `saos job list` / `saos job show JOB [--log K]` | 列出任务；查看状态、镜头文件、最近 K 条执行记录、正在跑的计算的进度（`progress`）和最近 5 次 `--detach` 运行（`runs`：`running`、`done`、`failed`，子进程不在而结果没写出的为 `lost`，附结果文件路径）。只读文件，后台进程忙时也能用 |
| `saos lens import --job J SRC [--as NAME] [--overwrite]` | 把外部 .zmx 复制进任务的 `lenses/`，原文件不动 |
| `saos lens load --job J FILE [--discard]` | 加载任务中的镜头文件 |
| `saos lens unload --job J [--discard]` | 卸载当前镜头 |
| `saos lens info --job J` | 系统数据：孔径、视场、波长、玻璃目录、面数、光阑面 |
| `saos lens surfaces --job J` | 各面的类型、半径、厚度、材料、半口径、机械半口径（`mech_semi_diameter`）、二次曲面系数、求解类型 |
| `saos job verify <job>` | 检查工具写入并封存的证据文件（`seals.jsonl`）有没有被改动或删除；返回 `intact` 和 `problems` |
| `saos lens approve --job J --model models/NNN-<镜头> --quote "<用户原话>"` | 记录用户对这份建模说明的确认（`approval.json`，含结构指纹和当时仍在的自动检查结果）。之后 `saos round` 只接受结构与某份已确认说明一致的镜头 |
| `saos lens describe --job J [--notes N.json] [--lang zh\|en]` | 建模说明（任何模式下都能用）：写 `models/NNN-<镜头>/index.html`、`model.json` 和布局图（3D 三个视角，轴对称系统另有截面）。逐面列出面型、所有非零附加参数、材料来源与各波长折射率（INDX 实测）、孔径、求解和顶点全局位置，并给出系统孔径（无焦时连同无焦单位）、环境温度气压、切趾、偏振、膜层文件与逐面膜层、光线瞄准选项、多重结构（结构数、每行操作数和各结构的值，交叠检查逐个结构做、相交和穿插标出结构号）、物方远心、光线瞄准、视场、波长。N.json 写每个面代表的实物、假设和一阶分析：`{"surfaces": {"3": "…"}, "assumptions": ["…"], "first_order": ["…"]}`。返回值的 `warnings` 是自动检查的问题（模型玻璃、目录外材料、无色散介质、非平面的菲涅尔基底、物方 NA 未设远心、渐晕因子、缺少面说明或一阶分析、没有结构探索记录、交叠检查见下）。没有 `mode` 为 `candidates` 的扫描、`--notes` 里也没有 `exploration`（`scans/NNN`，或结构由用户指定的依据）时，警告先比较至少 3 个结构不同的候选。交叠检查按零件外缘取每个面：机械半口径（Mech Semi-Dia 列）与净半口径加崩边区中的较大者，一片镜片两面取同一个外径（取两面中较大的），胶合面取所在各片中最大的；净口径之外面形照常延伸，读不到矢高处接平台；矩形等显式孔径的面不延伸。结果另给相邻两个折射面之间的最小间隔 `clearances`（玻璃中是该片最小厚度，正透镜在外缘、负透镜在中心；空气中是最小空气间隔；负值即相交）和各片的净口径与外缘 `lenses`。没有一片镜片的外缘大于净口径时警告“只按净口径检查”（`edges_from_clear`）；镜头单位是毫米时，玻璃最薄处低于 `design-practice.md` 表 17-2 下限（外缘处按正透镜最小边厚、中心处按负透镜最小中心厚，外径 3 mm 以下不判）也给警告 |
| `saos lens set --job J --surface N --param P --value V` | 修改一个面参数：radius、thickness、conic、semi_diameter、mech_semi_diameter（零件外缘，交叠检查和边厚按它算；按 `design-practice.md` 表 17-1 在净口径上加装配余量）、material、comment，或面型参数列 `par<N>`（非球面系数、坐标断点的偏心与倾斜等，列号见建模说明页）。数值可写 `inf` |
| `saos lens save --job J [--as NAME] [--overwrite] [--no-verify]` | 保存；默认保存后从磁盘重新加载，与保存前逐面核对 |
| `saos lens operand --job J TYPE [--int1 --int2 --d1 … --d6]` | 计算一个优化操作数，例如 `EFFL --int2 2` |
| `saos lens analysis --job J TYPE [--set KEY=VALUE …] [--keep-open]` / `--list` | 运行一个 OpticStudio 分析（AnalysisIDM 名称，如 StandardSpot、FftMtf、RayFan、WavefrontMap、FieldCurvatureAndDistortion），导出数据序列、网格（超过 256×256 时降采样）、点列图 RMS/GEO 半径和表头，存为任务的 `analyses/NNN-TYPE.json`，并保存 OpticStudio 的文本全文 `.txt`。设置名取自分析的设置对象，Field、Wavelength、Surface 用编号或 `all`；枚举按成员名。返回值省略网格本体，只给统计量。各部分逐条提取，读不出的记在 `problems`（`part`、`kind`：`empty_series` 为 OpticStudio 给出了没有 X/Y 数据的序列，`extract_failed` 为读取出错；`message`），其余照常返回；`status` 为 `ok`、`partial`（读出一部分）或 `empty`（没有任何数值），不是 `ok` 时另附文本导出的开头 `text_head`，连同 `messages`、`settings`、`message` 用来分辨是模型给不出结果、接口限制还是提取缺陷。OpticStudio 完全不给结果对象时返回 `ZOS_ERROR`，`details.kind` 为 `no_results` 并附设置。`ctx.analysis` 同样返回这些字段 |
| `saos lens view --job J [--viewer cross-section\|3d\|shaded] [--analysis TYPE] [--set KEY=VALUE …]` | 显性调试：打开 Lens Data 和布局窗口（默认剖面图，非轴对称或非序列系统改用 3D Viewer），按需再开一个分析（默认不开，按系统最看重的像质选），按当前镜头刷新。选择会被记住；之后 load、set、build、opt local/hammer/global、exec、round 结束时自动刷新已打开的窗口，建模阶段还会重开被关掉的 Lens Data 和布局窗口。连接 extension 后自动按默认选择打开 |
| `saos lens screenshot --job J [--label L]` | 显性调试：把 OpticStudio 的可见窗口截成 PNG，存为任务的 `screens/NNN-L-K.png`（PrintWindow，被遮挡也能截；最小化的窗口跳过） |
| `saos demand show --job J` | 校验需求单 `sources/demand.yaml`，给出版本哈希、确认记录、待确认问题和访谈总结，不连接 OpticStudio |
| `saos demand confirm --job J --quote "<用户原话>"` | 记录用户对当前需求单版本的确认，写入 `confirmations.jsonl`；随后确保流程图小窗口开着（已开着就不再开第二个，结果在 `ui` 字段，`SAOS_UI=0` 时不开） |
| `saos ui notify --job J --message TEXT` | agent 停下来问用户之前调用：在流程图窗口顶部显示这句话（用户点“知道了”清除，服务重启后不保留），窗口按需要用户看的节点处理（关着就打开，开着就恢复并置顶，不夺焦点）。返回 `notice` 和 `window` |
| `saos ui open --job J [--no-window]` | 打开给用户看的流程图小窗口（Chrome 应用窗口，1080×720；没装 Chrome 时用系统默认浏览器，返回值 `window` 为 `chrome-app` 或 `browser`）：六个步骤的流程图，当前步骤带流光动画，等用户确认的停顿和 blocking 问题另有标识；布局页默认显示当前镜头（任务的 `view.json`，后台进程在 lens load/set/build、opt local/hammer/global、exec、round 之后重写；Cooke 样例约 0.7 s，14 面的光纤探头约 1.1 s），也可选某一轮；2D/3D 可旋转、平移、缩放；2D 画系统的对称平面（轴对称系统为全局 YZ 面，折转系统如光纤探头为 XZ 面，坐标轴按平面标注；服务端从 `view.json`/`viz.json` 的全局坐标推出，返回在 `section` 字段），各面不共一个对称平面时只有 3D；结构交叠（面在口径内相交、镜片互相穿插）在图上方列出并把相关的面描红；Lens Data 折叠在图下（URL 加 `&ld=1` 默认展开）。点列图、MTF 和差距曲线按轮次看。右侧显式调试时另有窗口控制页（切换布局窗口、打开分析窗口、只读查看孔径/视场/波长，经后台进程执行，后台进程忙时排队）。图放不下时右侧面板上下、左右滚动。lens、exec、opt、scan（不含 `--show`）、check、round 这些带任务的命令执行前，若这个任务的窗口 6 s 内没有轮询过，就重新打开（20 s 内不重复打开；最小化的窗口按 90 s 算），返回值带 `ui_opened`（自动重开不抢焦点：`focus_guard` 表示已起后台进程，等 Chrome 窗口抢到前台后把焦点还回去，最多等 15 s）；`SAOS_UI=0` 时不开。用户关掉的窗口（页面关闭时发 `/api/closed`，记在 `daemon/ui-closed.json`）只在需要用户看的命令前重开：需求单当前版本未确认时的 `demand show`、`lens describe`、`round`；`saos ui open` 总是打开；在这些节点上开着的窗口恢复并置顶，不夺键盘焦点（返回 `{"window": "raised"}`）。服务不在时命令先起服务、等约 4 s 让开着的窗口重连，再决定开不开新窗口。本机服务只读任务目录、只监听 127.0.0.1，15 分钟没有请求自行退出，重启时沿用上次的端口和令牌（`daemon/ui-endpoint.json`，端口被占才换），开着的窗口原地重连；状态在 `daemon/ui.json`，日志在 `daemon/ui.log` |
| `saos scan --job J --plan PLAN.json [--workers N] [--detach]` / `saos scan --job J --show [NNN]` | 并发扫描、多起点寻优或扰动诊断，不用写脚本：在后台进程常驻的 worker 池（见下文 `ctx.map`）里逐点写入参数（参数键见下文），可选在 `[lo, hi]` 内对一个变量求根让某个判定项或操作数等于目标值（先等分扫 `scan` 个点找变号，再 Brent 法），再补偿（`quick_focus` 或任务内脚本 `run(w)`），按需求单的判定实测（默认所有带界限的项）并附加操作数。计划五选一：`vars`（各轴取值的网格，轴可写列表或 `{"from","to","steps"}`）、`points`（逐点列表）、`candidates`（结构探索，见下）、`minimize`（每个起点一个 worker 跑 Nelder–Mead 或 Powell，目标是所测各项的目标差距，与需求单同一定义；`max_evals`、`max_seconds` 限定预算；和 `opt local` 一样要求需求已确认、没有 blocking 问题）、`probes`（扰动诊断，见下）。另有 `fixed`（每点先写入）、`operands`、`keep`（保留最好几个点的镜头，默认 3，probes 默认 0）。`probes` 计划带 `target`（需求单中差距最大的一项）、`mechanism`（它为什么差）和 1–6 个扰动，每个扰动写 `id`、`prediction`、`delta`、可选 `expect`（各项 up/down/none/valley/peak）和 `control`（阴性对照），改动方式是参数键 `key`（同下文，平面用 `curvature`）或任务内脚本 `run(w, delta)`；基准点加每个扰动的 ±delta 共 2N+1 个点同时跑，结果另给预测与实测的对照、按对目标归一化效应的排序 `ranking`（实测方向另有 `valley`/`peak`：±delta 两侧都比基准差或都好，基准在该变量的极值上，不算动目标的变量；一侧没有有效值时用另一侧对基准的差，`one_sided` 标出是哪一侧） 和审视提醒 `review`（没有阴性对照、对照动了目标、预测不符、没有变量动目标、`quick_focus` 从没动过）；基准点的目标没有有效值时不读扰动，提示先确认可行性。扰动诊断不需要需求单已确认（2026-10-17 由原 `saos probe` 并入，原来在主实例上串行，5 次共 1277 s）。结果按目标差距排序，返回前 10 行；全部行、计划存到 `scans/NNN/` 并封存，保留的镜头存为 `lenses/scans/NNN/pPPP.zmx`，可以直接 `lens load`。当前镜头不变，但要求已保存；扫描开始时把它复制为 `scans/NNN/base.zmx`，之后改动工作镜头不影响扫描。`--detach` 让命令立即返回、扫描在后台线程里跑，后台进程照常处理其他命令（ADR-0009），进度写在 `scans/NNN/progress.json`（流程图窗口也显示）；`saos scan --job J --show [NNN]` 读某次扫描（默认最近一次）的进度或结果，只读文件，后台进程忙时也能用。worker 池同一时间只给一个扫描或 `ctx.map` 用，忙时返回 `POOL_BUSY`。`saos job cancel` 在当前点完成后停止，分离运行的也一样。旧任务的 `probes/NNN/` 保留原样。`candidates` 计划（结构探索）：`{"candidates": [{"id", "lens": "<任务内相对路径的 .zmx>" 或 "prescription": "<任务内相对路径的处方 .json>", "hypothesis": "<它凭什么可能赢>"}, …], "prepare": "<可选，任务内脚本 run(w)>", "stages": [{"local": {"max_seconds": S, "cycles"?: "auto"}}, {"hammer": {"max_seconds": S}}, {"global": {"max_seconds": S}}], "measure", "keep", "workers"}`，2–12 个候选，每段必须给 `max_seconds`。和 `minimize` 一样要求需求已确认，不需要当前镜头；`fixed`、`solve`、`compensate` 不适用（各候选的面不同，在 `prepare` 里设）。每个候选给 `lens` 或 `prescription` 之一；处方在计划校验时就按「处方格式」检查，出错的候选在消息里点名。开始时把每个候选连同同名附属文件（处方只复制它自己）复制到 `scans/NNN/candidates/<id>/`；每个候选一个 worker，在临时目录里加载副本或按处方搭建并存成 `<id>.zmx`（Hammer、全局搜索写在镜头旁的文件互不干扰），执行 `prepare`，记下起点，逐段优化并在每段后按需求单实测（`measure` 总是加上全部硬条件），结束时做按零件外缘的交叠检查和玻璃状态检查，镜头存为 `lenses/scans/NNN/<id>.zmx`。每个 worker 给 OpticStudio 的核数为可用核数除以 worker 数。排序先看是否有效（零件不交叠、硬条件满足、判定没出错、目标差距有限），再看目标差距；最后一段目标差距仍比上一段下降超过需求单 `rel` 的标为 `still_improving`（预算受限），排名靠后也保留；默认保留前 3 个有效候选和所有预算受限的，其余镜头删除，全部行仍在 `result.json`。结果另有 `kept`、`budget_limited`、`best`（第一个有效候选）和对比页 `page`（`scans/NNN/index.html`：假设、结论与无效原因、起点到终点的目标差距和各段 merit、最薄玻璃与最小空气间隔、停产玻璃、变量数与评价函数行数及其来源 `prepare` 或 `candidate`、终点各项实测值）。`plan.json` 记下 `budget_s`（各段预算之和乘以候选数除以 worker 数向上取整，不含加载和测量），`--detach` 时返回值也带它 |
| `saos rule-out --job J --tier T [--tier T…] --reason TEXT` | 带证据排除帮不上的手段层级（`ruled_out.jsonl`），终止判断不再等它；只到下一次明显进步为止。必须先在当前需求版本下记录过轮次。不连接 OpticStudio |
| `saos stop --job J --reason TEXT --quote "<用户原话>"` | 记录用户的停止决定（`stops.jsonl`）：当前需求单版本下不再记录轮次，报告显示“按用户决定停止”、理由和原话。必须先在当前版本下记录过轮次。不连接 OpticStudio |
| `saos job cancel <job>` | 请求正在运行的计算在下一个安全点停止（写 `cancel.request`）：`opt local`、`opt hammer`、`opt global` 调用 Cancel，`scan`（含分离运行的）和 `ctx.map` 在各 worker 的当前点完成后停止，脚本通过 `ctx.progress()` 的返回值或 `ctx.cancel_requested()` 得知。不经过后台进程，正忙时也能用 |
| `saos check --job J [--only a,b] [--detach]` | 按需求单对当前镜头逐项实测和判定，不记录。需求单未确认时也能用，便于整理需求时先看数值。每项给出 `duration_s`。`--only` 只测列出的几项，结果标 `partial`、不会判为达成；记录轮次时总是全测。`--detach` 见下文「长任务」 |
| `saos round --job J [--tier T[,T…]] [--note TEXT] [--merit V] [--no-viz] [--detach]` | 记录一个调试轮次（T 为 local、variables、materials、global、structure，一轮用了几个就写几个）：存为 `lenses/rounds/rNNN.zmx`，逐项实测判定（全文存为 `evaluations/NNN/result.json`），写入 `ledger.jsonl`，返回终止判断（continue / achieved / exhausted / limit / stopped；有 minimize / maximize 目标时达标后仍是 continue，直到这些目标都到理想值或各层级不再明显进步）、既没试过也没排除的层级和需求提要 `brief`。第 0 轮不填 tier。当前需求单版本未确认（`DEMAND_NOT_CONFIRMED`）、有 blocking 待确认问题（`DEMAND_BLOCKED`）、已经终止、或镜头结构与已确认的建模说明不一致（`MODEL_NOT_APPROVED`）时拒绝。每轮记下所用材料；含模型玻璃或不满足常量、变量范围的轮次不能交付。每轮还按零件外缘做一次交叠检查：面在口径内相交或镜片互相穿插时记录带 `parts_clash`（涉及的面、深度），`achieved` 为假且不能交付，报告写明原因（物理上不可能的镜头不能靠判定数值过关）。`evaluations/NNN/result.json` 和返回值 `evaluation` 里的 `achieved` 只看判定，本轮是否达成以 `record.achieved` 为准；交叠检查本身出错时本轮不记录写入 ledger 后再导出 `evaluations/NNN/viz.json`（逐面全局几何、布局光线、点列图光线、Standard Spot 的 RMS/GEO、FFT MTF，OpticStudio 文本导出在 `viz/`）供 `saos ui` 显示；导出失败只记在 `viz.json` 和返回值 `viz` 中，不影响本轮。Cooke 三片式实测约 1 s；`--no-viz` 跳过。同一需求下判定版本（`checks/` 及其引用的 saos 配方包和 `saos.kit` 源码，副本随证据存在 `evaluations/NNN/support/_saos/`）变了时，先用新判定重测当前窗口目标差距最小的一轮的归档镜头，写成一条 tier 为 `rebase` 的记录（`rebase_of` 指向原轮次，`file` 仍是原归档镜头，证据的 `lens` 字段写明），本轮编号随之顺延；各项数值在相对 1e-9 内与原记录一致、没有判定出错时 `equivalent` 为真，比较窗口延续，否则从这条记录开始新窗口。返回值的 `rebase` 给出 `equivalent` 和各项差别 `diffs`。重测记录不计入轮次上限和“没有进步的轮数”，可以交付（ADR-0010）；新窗口不重置轮次上限，上限按需求单版本累计，本版本已是 limit 或 stopped 时判定改了也拒绝 |
| `saos report --job J [--lang zh\|en]` | 由需求单和 ledger 生成交付报告 `report/index.html`，不连接 OpticStudio；语言见「报告语言」。返回 `headline`（`deliver`、`best_so_far`、`best_blocked`、`no_valid`，与报告开头一致）和 `deliverable`；`deliver` 只放可以交付的镜头，没有有效设计或最好一轮不可交付（模型玻璃、硬条件不满足、测量出错、零件交叠）时为 `null`，那一轮放在 `diagnostic`（`file`、`round`、`hard_failing`、`measurement_errors`、`model_glass`）里供诊断；`achieved` 只在可交付且达成时为真；轮次已终止（exhausted、limit、stopped）时 `headline` 为 `deliver`、交付的是差距最小的可交付轮，此时 `achieved` 可以为假 |
| `saos exec SCRIPT --job J [--arg K=V …] [--args-file JSON] [--name N] [--live] [--detach]` | 在后台进程内执行脚本（ADR-0007），见下文「脚本执行」；`--detach` 见下文「长任务」 |
| `saos microscope adapt EXTRACT --focal-length F --field-number N [--tube-lens 200] [--catalogs …] [--stop rear_focal\|r<面号>] --out DIR` | 把子 agent 提取的面数据适配成标准设计布局的处方，输出 prescription.json、expect.json、adapt_report.json（不经后台进程） |
| `saos lens build --job J PRESCRIPTION --as NAME [--overwrite]` | 按处方（格式见下文「处方格式」）用 ZOS-API 从零搭建序列镜头，保存到 lenses/ 并加载。处方或某个面写错时返回 `BAD_ARGS`，消息里写明是哪个面、哪一项；面型名、多重结构操作数写错时 `details.choices` 列出 OpticStudio 的全部可选名，面型参数写错时 `details.parameters` 列出该面型的参数列 |
| `saos microscope repro --job J EXPECT` | 复现校验：焦距、物方 NA、顶点工作距离、f_TL/EFL 与期望值比较。每项允许的偏差取相对误差 1% 与 EXPECT 中 `tolerance_mm`（原文印刷精度的舍入包络半宽）的较大者。只临时调焦，结束后恢复 |
| `saos microscope perturb --job J --seed S --radius-rel A [--thickness-rel B]` | 受控扰动：物镜各面曲率、厚度按固定随机种子均匀改变 |
| `saos microscope accept --job J --spec FILE` | 按光学规格做验收评估（FILE 是任务内的规格文件，例如放进 `sources/` 的规格或 `library verify` 写的 `spec/…`；核心的 `job create` 不再复制规格），结果和验收模型存到任务的 eval/。结果的 `focus_trace` 列出调焦的每个试探位置（波长、厚度、轴上光线失败数、c4、拟合残差） |
| `saos opt vary --job J --param radius\|thickness\|conic\|par<N>\|material --surfaces 1-16 [--mode model\|substitute] [--catalog C] [--off]` / `saos opt vary --job J --config-rows 1,3 [--configs 2-3] [--off]` | 设变量或取消变量。`--config-rows` 改为把多重结构的格（变焦间隔、调焦量等）设为变量，`--configs` 不给时是该行的全部结构，返回 `config_solves`（`"<行>.<结构>": 求解类型`）；镜头至少要有 2 个结构，不能与 `--param`、`--surfaces` 同时给。物面只允许厚度（即无盖玻片时的工作距离间隔）。`par<N>` 为面型参数列；`material` 需要 `--mode`：`model` 把玻璃换成 nd、νd 可变的模型玻璃（起点取当前玻璃，只用于探索，不能交付），`substitute` 让 `opt hammer`、`opt global` 在目录内替换真实玻璃（ZOS-API 只能按目录限定，每个面一个目录。`--catalog` 不给时用在产目录 `SAOS_CDGM_INPROD`，成都光明 2026-09 目录里 Standard、Preferred 的 315 种，不在镜头的在用目录里就自动加入；`SAOS_NHG_INPROD` 为新华光 2026-08 目录的 260 种。两者由 `library/glasscat/inprod.py` 生成，没装时退回全部在用目录。返回值 `catalog` 写明实际用的目录；目录在某面通过校验、真正设上替换时才加入，校验失败（BAD_ARGS）的调用不改目录；执行中途出其他错误时，镜头保守地标为未保存。现在的目录玻璃不在所选目录里的面列在 `outside_catalog`（附 `note`；模型玻璃不算），替换会把它们换成该目录的玻璃，要保持厂家就按面给 `--catalog`。多个面中途有一面失败时，前面的面已经改了，镜头标为未保存）。`opt local` 不替换玻璃（实测玻璃不变）。`--off` 对模型玻璃保留当前 nd、νd 并停止变化。变量、边界条件和工艺约束见 `skills/saos/design-practice.md` |
| `saos opt merit --job J clear\|wizard\|add\|show [--file JSON]` | 评价函数：清空；按向导生成默认评价函数（type、data、reference、rings、arms、glass、air、start_at、assume_axial_symmetry 等；assume_axial_symmetry 默认 true，折转或非旋转对称的系统要设 false，否则只追半个光瞳）；追加操作数列表；查看 |
| `saos opt local --job J [--cycles auto\|1\|5\|10\|50] [--cores N] [--max-seconds S] [--measure IDS\|hard\|all] [--detach]` | 局部优化（阻尼最小二乘），默认用全部逻辑核减 2。异步运行，进度写进 `progress.json`；超过 `--max-seconds` 或收到 `job cancel` 时调用 OpticStudio 的 Cancel，保留已经得到的结果（返回 `stopped_by`）。进度写不进去（例如 `progress.json` 被流程图窗口读着，替换报 WinError 5，先重试约 1.4 s）不再中断优化，只记日志；循环里出其他错误时先 Cancel 并等工具停下再报错，镜头标为未保存（编辑器里是优化器走到的系统，`lens save --as` 留下，`lens load --discard` 丢弃）。运行中每 5 s 把当前镜头的几何、Lens Data 和交叠检查推给流程图窗口（优化器占着追迹工具，这时不带光线，结束后补上）。开始前做与 round 相同的需求单检查（已确认、没有 blocking 问题）。结束后返回 `review`：交叠检查（`crossings`、`overlaps`、最薄玻璃 `thinnest_glass`、最小空气间隔 `smallest_air_gap`、`edges_from_clear`），给了 `--measure` 时还有运行前后按需求单判定的对比 `measured`（各项值与差距、`better`、`worse`、运行后的 `hard_failing`、`invalid`、`errors`、两次的 `shortfall`）；零件相交、硬条件不满足、交叠检查或运行后测量没做完时 `valid` 为假，`warnings` 写明。`--measure` 取逗号分隔的判定 id、`hard`（常量和变量范围）或 `all`（所有进 G 的项），只放快的判定；它前后各测一次，耗时算在本命令里。设计阶段（需求已确认、有确认过的建模说明）距上次 `round` 累计 2 次以上优化或 15 个以上脚本时，返回值带 `since_round`（次数和提醒）；exec、scan 也带 |
| `saos opt hammer --job J --max-seconds S [--cores N] [--measure IDS\|hard\|all] [--detach]` | Hammer 优化：在局部极小之外继续找，配合 `opt vary --param material --mode substitute` 时在目录内替换真实玻璃。它不会自己结束（OpticStudio 的 TargetRunTimeM 也停不下它），所以必须给 `--max-seconds`，到时调用 Cancel，编辑器里留下找到的最好系统；`job cancel` 同样生效。Hammer 的自动模式会自己结束，但在 Cooke 样例上 2 s、704 个系统就停，玻璃一个也没换，与局部优化同值，所以不用。返回 `initial_merit`、`final_merit`、`systems`（试过的系统数）、`stopped_by`，以及 `materials_changed`（换了玻璃的面：原牌号、新牌号、目录状态）。玻璃替换不看目录状态，在厂家全目录里常换成停产牌号：Cooke 样例在 SCHOTT 里 3 次 Hammer、1 次全局搜索共 12 个替换，9 个是 Obsolete（SCHOTT 目录 364 种里 233 种是 Obsolete，2026-10-18 实测），所以默认用在产目录（见 `opt vary`；Cooke 三片 Hammer 10 s 换出的 3 种都在 `SAOS_CDGM_INPROD` 里，评价函数 0.01145 → 0.000574，115 万个系统，2026-10-19 实测）。Cooke 样例（曲率可变、三片都可替换，默认核数）两次各 20 s：评价函数 0.0114 → 0.00020 和 0.00024，试了 243 万和 256 万个系统，换出的玻璃两次不同。运行中 OpticStudio 在镜头旁写 `<镜头名>_NNNN_HAMMER.zmx`，结束时自己删掉；残留的会移到 `lenses/hammer/NNN/`。进度（评价函数、系统数）写进 `progress.json`，运行中每 5 s 推一次不带光线的视图。开始前做与 round 相同的需求单检查。客户端至少等 `--max-seconds` 加 120 s。`review`、`--measure`、出错时的处理同 `opt local` |
| `saos opt global --job J --max-seconds S [--cores N] [--measure IDS\|hard\|all] [--detach]` | 全局搜索：同样不会自己结束，必须给 `--max-seconds`，到时或 `job cancel` 时调用 Cancel；编辑器里留下最好的系统。返回值同 `opt hammer`，另有 `best_merits`（前 10 名的评价函数值，没找到更多不同系统时会重复）。OpticStudio 运行中把前 10 名不断重写为镜头旁的 `GLOPT_NNNN_00K.zmx`（编号 NNNN 不固定，不同运行会覆盖同名文件），结束后移到 `lenses/global/NNN/`，返回值 `files` 列出，可以直接 `lens load`。镜头旁已有 `GLOPT_*.zmx` 时（例如镜头就是从 `lenses/global/NNN/` 载入的）会被同名覆盖，因此拒绝运行（BAD_ARGS），先 `lens save --as` 存到别处。Cooke 样例用默认核数 9 s 约 103–107 万个系统（2 核 15 s 为 38–48 万）。其余同 `opt hammer` |
| `saos microscope library check EXTRACT` | 入库前用近轴计算核对提取结果：焦距和工作距离的印刷值落在印刷精度的舍入包络内，或与计算值相差不超过 1%，即为一致（不经后台进程） |
| `saos microscope library add ID --extract FILE [--catalogs …] [--focal-length F] [--field-number N] [--stop rear_focal\|printed\|r<面号>]` | 把提取结果适配成处方，写入 `library/microscope/entries/ID/`，状态为 extracted（不经后台进程） |
| `saos microscope library verify ID --job J [--discard]` | 搭建、复现校验、探测验收、推导基准规格并确认原结构 G = 0，结果写回条目。当前镜头有未保存修改时返回 UNSAVED_CHANGES；显式加 `--discard` 才会丢弃，包括其他任务的修改。复现不通过的条目状态为 rejected |
| `saos microscope library list` | 列出条目：状态、焦距、NA、片数、能否作基准及原因 |
| `saos microscope library query --na A --magnification M [--class achromat\|plan_achromat] [--field-number N] [--working-distance W] [--limit K]` | 按光学规格从已收录条目中选初始结构 |

结构库的收录规则、提取格式和基准规格的推导见 [library.md](./recipes/microscope/library.md)。

`--job` 必须与当前加载的镜头属于同一任务；镜头文件名一律相对该任务的 `lenses/`。

## 需求单

`sources/demand.yaml` 的格式、目标差距的算法和终止规则以英文写在 [skills/saos/demand.md](../skills/saos/demand.md)，agent 直接读那一份；这里不再重复。

要点：只收 `constants`、`variables`、`objectives`、`open_questions`、`summary`、`notes`、`lang`、`stop`，其他字段一律拒绝；每个界限都要写 `source`，`demand show` 的 `weak_basis` 列出没有来源或只是 agent 假设的界限，`demand confirm` 拒绝没有来源的界限；判定方法三选一（operand、analysis、script）；界限写有限数值的 `max`、`min` 或 `equals` + `tol`；“越小/越大越好”写 `minimize: true` / `maximize: true`（maximize 要给 `ideal`，都要有尺度），按离理想值的距离计入目标差距、不影响是否达成，达标后继续改进到各层级不再明显进步才判达成，报告交付达成轮次中 G 最小的一轮；目标写 `must: {max: 0}` 一律拒绝（永远达不到）；版本只取三类条目和 `stop` 的哈希，改写总结、待确认问题或 `lang` 不需要重新确认；确认只能由 `saos demand confirm` 记录用户原话。旧格式 `sources/harness.yaml` 不会自动转换，只有它时命令返回 `FILE_NOT_FOUND` 并提示改写。

每次检查的快照和证据存在新的 `evaluations/NNN/`，返回的 `evidence_file` 指向完整结果。修改检查脚本会开始新的判定方法版本，旧轮次不再参与进展比较和交付选择；需求单或脚本改动后还没记录新轮次时，`saos report` 返回 `SPEC_CHANGED`。判定方法的版本跟踪 `checks/` 下的全部文件（脚本、辅助模块和数据），以及检查代码用到的 `saos.recipes` 配方包和 `saos.kit` 的源码；saos 的其他模块和第三方库不跟踪。

## 处方格式

`lens build` 的输入，也是结构探索候选的一种写法（`saos.prescription`）。处方只管怎么在 OpticStudio 里搭出镜头，不管怎么评价：评价指标在需求单的判定和配方里。saos 校验下面这层骨架并拒绝不成立的组合；开关项（`stop`、`telecentric_object`、`adjust_index`、`vary` 等）只收 JSON 的 true、false，枚举项只收所列的字符串；`title` 写进 OpticStudio 的标题；面型和面型参数不逐个写代码，按 OpticStudio 的名字原样写入，写错时 OpticStudio 搭建时拒绝，saos 把错误连同可选名报回。未知的键一律拒绝。

```json
{"title": "…", "units": "mm",
 "aperture": {"type": "entrance_pupil_diameter", "value": 10}, "telecentric_object": false,
 "fields": {"type": "angle", "y": [0, 10, 14], "x": [0, 0, 0], "weights": [1, 1, 1], "normalization": "radial"},
 "wavelengths": {"um": [0.486, 0.588, 0.656], "weights": [1, 1, 1], "primary": 2},
 "catalogs": ["CDGM"], "ray_aiming": "real",
 "environment": {"temperature": 20, "pressure": 1, "adjust_index": true},
 "polarization": {"unpolarized": true}, "coating_file": "COATING.DAT",
 "ray_aiming_options": {"robust": true},
 "object": {"thickness": "inf", "material": "", "comment": ""},
 "surfaces": [
   {"radius": 30, "thickness": 4, "material": "H-ZK9B", "mech_semi_diameter": 7, "coating": "AR"},
   {"type": "EvenAspheric", "radius": -80, "conic": -1.2, "thickness": 2,
    "params": {"par2": 1.2e-5, "6th Order Term": -3e-8}, "solves": {"radius": "variable", "par2": "variable"}},
   {"radius": "inf", "thickness": 5, "stop": true, "semi_diameter": 4},
   {"type": "CoordinateBreak", "params": {"par3": 12}},
   {"radius": "inf", "thickness": 0, "material": "MIRROR", "aperture": {"type": "rectangular", "x_half": 8, "y_half": 6}},
   {"type": "CoordinateBreak", "params": {"par3": 12}, "thickness": -20},
   {"radius": -60, "thickness": 18, "decenter_tilt": {"before": {"decenter_y": 0.5, "tilt_x": 1}, "after": "reverse"},
    "solves": {"thickness": {"type": "marginal_ray_height", "height": 0}}}],
 "image": {"radius": -120},
 "configurations": [{"operand": "THIC", "params": [3], "values": [5, 5.6]},
                    {"operand": "TEMP", "values": [20, -40]}]}
```

- 系统：`units` 为 `mm`（默认）、`cm`、`in`、`m`。`aperture.type` 为 `object_na`、`entrance_pupil_diameter`、`image_f_number`（像方 F 数）、`working_f_number`（近轴工作 F 数）、`object_cone_angle`（物方半锥角，度）、`float_by_stop`（按光阑尺寸浮动，给了 `value` 就拒绝，光阑面必须给 `semi_diameter`）。`fields.type` 为 `object_height`、`angle`（低于 90°）、`paraxial_image_height`、`real_image_height`、`theodolite_angle`；`x`、`weights` 可省（默认 0 和 1），`normalization` 为 `radial`（默认）或 `rectangular`。`environment` 的默认值与 OpticStudio 新系统相同（20 °C、1 atm、不按环境修正折射率）。
- 拒绝的组合：物在无穷远（`object.thickness` 为 `"inf"`）时用 `object_na`、`object_cone_angle` 或 `object_height`；`telecentric_object` 配物在无穷远、角度视场或入瞳直径孔径；`float_by_stop` 的光阑面没有 `semi_diameter`；多重结构里有 `TEMP`、`PRES` 而 `environment.adjust_index` 不为 true。搭完回读孔径类型、视场类型和物方远心，OpticStudio 改掉了其中任何一项也返回 `BAD_ARGS`。
- 面：`type` 省略为 `standard`；`paraxial` 用 `focal_length` 和 `opd_mode`（默认 1）；其他面型写 ZOS-API `SurfaceType` 的名字（不分大小写，如 `EvenAspheric`、`OddAsphere`、`QTypeAsphere`、`Toroidal`、`Biconic`、`XYPolynomial`、`ZernikeStandardSag`、`Binary2`、`CoordinateBreak`、梯度折射率的 `Gradient1`…`Gradient12`、`Gradium`），只写给出的 `radius`、`conic`、`material`。通用项：`thickness`、`comment`、`stop`（至多一个）、`semi_diameter`（给出即固定，省略为自动）、`mech_semi_diameter`、`coating`（见下文膜层）。材料写法同 `lens set`：目录牌号、`MIRROR`、`{"model": {"nd", "vd", "dpgf"}}`。
- `params`：面型参数，键为 `par<N>`（Lens Data 的 Par 列号）或列标题（不分大小写，如 `"4th Order Term"`），值按单元格类型写入数字、整数或字符串，按给出的顺序写（有的面型先写项数，后面的列才出现），每项回读核对；列标为 unused 的不能写。
- `decenter_tilt`：面前的偏心倾斜 `before`（`decenter_x`、`decenter_y`、`tilt_x`、`tilt_y`、`tilt_z`，`order` 为 `decenter_tilt` 或 `tilt_decenter`），面后的 `after` 为 `"reverse"`（反向还原）或同样的显式值；坐标断点面没有这一项，用它的 `params`（par1–par6）。
- `aperture`：面孔径，每面一个。`circular`、`circular_obscuration`（`min`、`max`）；`rectangular`、`rectangular_obscuration`、`elliptical`、`elliptical_obscuration`（`x_half`、`y_half`）；`spider`（`arms`、`width`）；都可加 `x_decenter`、`y_decenter`。中心遮拦加支撑杆要用两个面。
- `solves`：`{"<列>": …}`，列为 `radius`、`thickness`、`conic`、`material` 或 `par<N>`。`"variable"`（材料变量仍用 `opt vary --mode`）；`{"type": "pickup", "surface": k, "scale": 1, "offset": 0}`（材料拾取没有 scale、offset）；`{"type": "marginal_ray_height", "height": 0, "zone": 0}`（只用于厚度）；`{"type": "f_number", "value": F}`（只用于半径）。求解在所有面搭完后才设，拾取可以读后面的面。
- `image`：像面（曲面像、倾斜像面），键同面，但没有 `thickness`、`material`、`stop`。
- `configurations`：多重结构。`TEMP`、`PRES` 行在结构 1 的值就是系统的温度、气压（OpticStudio 会用它覆盖 `environment`）：`environment` 没写这一项时取该行结构 1 的值，写了而不同就拒绝，每种至多一行；搭完回读温度和气压。每行一个操作数（ZOS-API `MultiConfigOperandType` 的名字，如 `THIC`、`GLSS`、`TEMP`、`PRES`、`PRAM`），`params` 是该操作数的整数参数（至多 3 个，如 `THIC` 的面号），`values` 每个结构一个值，结构数即其长度（至少 2，各行相同），`vary: true` 把这一行设为变量。搭完回到结构 1。
- `afocal`：无焦像空间（望远镜、扩束镜、目镜），`true` 或 `{"units": …}`，单位为 `microradians`、`milliradians`、`radians`、`arcseconds`、`arcminutes`、`degrees`，分析结果随之改用角度单位。不写为关。像在无穷远，所以拒绝像高视场（`paraxial_image_height`、`real_image_height`）和像方 F 数孔径（`image_f_number`、`working_f_number`）；搭完也回读这一项。
- `apodization`：光瞳切趾，`{"type": "uniform" | "gaussian" | "cosine_cubed", "factor": G}`，`factor` 只用于 `gaussian` 且必须给。
- `polarization`：只写给出的项，其余保持 OpticStudio 的默认：`unpolarized`、`jx`、`jy`、`x_phase`、`y_phase`（度）、`method`（`x_axis`、`y_axis`、`z_axis`）、`convert_thin_film_phase`。`unpolarized` 为 true 时不能再给琼斯矢量；给了琼斯矢量而没写 `unpolarized` 时按偏振光处理（`unpolarized: false`），因为 OpticStudio 新系统默认非偏振，琼斯矢量不会生效；`jx`、`jy` 不能同为 0。搭完逐项回读。
- 膜层：顶层 `coating_file` 选膜层文件（如 `COATING.DAT`，须是 OpticStudio 列出的文件），面上 `coating` 写文件里的膜层名（不分大小写，写错时 `details.choices` 列出可选名）。自定义膜层要先写进膜层文件，处方不定义膜系。多重结构里换膜层用 `COTN` 操作数。
- `ray_aiming_options`：光线瞄准的高级选项（超广角、鱼眼常要用），只写给出的项：`robust`、`enhanced`、`cache`、`auto_pupil_shift`、`scale_pupil_shift_by_field`（true 或 false），`pupil_shift`（x、y、z 三个数）、`pupil_compress`（x、y 两个数）。`ray_aiming` 为 `off` 时拒绝，`pupil_shift` 和 `auto_pupil_shift: true` 不能同时给；只给 `pupil_shift` 时自动关掉 `auto_pupil_shift`（OpticStudio 默认自动计算，会盖掉手动值）。搭完逐项回读。
- 光栅、全息、二元衍射面是普通面型（`DiffractionGrating`、`EllipticalGrating1`、`VariableLineSpaceGrating`、`ToroidalGrat`、`Hologram1`、`Binary2` 等），刻线密度、衍射级次和相位系数按它们的参数列写进 `params`，不用另写。
- 不在处方里：非序列和混合模式、物理光学传播、公差、膜系定义，仍用 `exec` 脚本。

## 报告语言

`saos report` 的语言按以下顺序选择：`--lang` > 需求单的 `lang` > 环境变量 `SAOS_LANG` > 系统界面语言（Windows 取 `GetUserDefaultUILanguage`）> 英文。后台日志按 `SAOS_LANG` 和系统界面语言选择。给 agent 读的错误信息和命令帮助只有英文。

## 脚本执行

脚本定义 `run(ctx)`，返回可 JSON 化的结果（numpy 数值和数组会自动转换）。`ctx` 提供：

- `ctx.system`：OpticStudio 的 PrimarySystem；`ctx.zosapi`：ZOSAPI 命名空间；`ctx.backend`：saos 后端；
- `ctx.job`、`ctx.args`（`--arg` 和 `--args-file` 合并后的字典）、`ctx.lens`（当前加载的镜头路径，可能为 None）、`ctx.dirty`；
- `ctx.load(name)`、`ctx.save_as(name, overwrite=False)`：切换或保存镜头时必须用它们，后台进程才能跟踪当前文件；`ctx.load` 换掉窗口内容前会把未保存的窗口内容存成副本（`ctx.capture_window()` 也可单独调用，副本路径列在执行结果的 `captured`）；`ctx.mark_dirty()`；`ctx.path(rel)`：任务目录内的路径。
- 参数读写（`saos.kit`，`ctx` 和 worker 的 `w` 上都有同名方法）：`ctx.get(key)`、`ctx.values(keys)`、`ctx.set({key: value, …})`。参数键写成 `<面>.<参数>`：面是序号（负数从像面倒数，`-1` 为像面）或 Lens Data 的注释；参数为 `radius`、`curvature`（1/R，0 为平面）、`thickness`、`conic`、`semi_diameter`、`mech_semi_diameter`、`material`、`comment`、`par<N>`；系统量为 `aperture`、`field.<n>.x|y`、`wave.<n>`、`config.<结构>.<行>`（多重结构第几行在第几个结构的值，扫描和扰动诊断也能用；GLSS 行的牌号按在用目录核对；改的是当前结构时立即套用到镜头）。值写不进去（被求解或 pickup 占着）时报 `VALUE_NOT_APPLIED`。`with ctx.kept(keys):` 在块结束时恢复这些参数，`with ctx.restored():` 恢复整个镜头（存临时副本再加载，约 0.1 s，用于增删面的试探）。`with ctx.configuration(c):` 在块内切到第 c 个结构、结束时（出错也）回到原来的结构，`ctx.configurations()` 是结构数；多重结构的判定按结构分别测量时用它，判定结束时镜头不变，否则判定会因改动了镜头被拒绝记录（`ctx.memo` 的键要带上结构号）。`ctx.surface(ref)` 返回面序号
- 光线与操作数：`ctx.trace(rays, to=None)` 批量实际光线追迹，`rays` 是 `(wave, hx, hy, px, py)` 的序列，返回各量的 numpy 数组（`x, y, z, l, m, n, error, vignette, intensity`，`ok` 为有效光线；全局坐标用 `saos.kit.global_matrix(ctx, 面)` 给出的旋转与平移换算）；本机实测约 20 µs/条，主要是 pythonnet 的逐条调用。`ctx.spot(fields, wave=None, grid=21, to=None, refocus=False)` 一次追迹给出各视场（和波长）的通过率、主光线落点、质心、RMS 与几何半径（相对质心），`refocus=True` 另给沿目标面局部 z 的最佳焦移和该处 RMS（直线外推，前提是目标面之后为均匀介质）；无焦系统（像在无穷远）不给 `to` 时拒绝，因为像面上的落点不反映像质，角度弥散从 `ctx.trace` 的方向余弦或无焦单位的操作数、分析取；2026-10-17 在 Cooke 样例上与 RSCE（高斯求积）相差 0.005%–0.23%（81×81 网格）。`ctx.op(name, int1, int2, *data)` 取任意优化操作数；`ctx.analysis(name, settings)` 运行分析并返回导出数据，不留文件；`ctx.quick_focus()`；`kit.global_matrix`、`kit.sag`、`kit.pupil`、`kit.best_focus`、`kit.material_nd`（面材料的 nd，模型玻璃取求解值）用 `from saos import kit` 调用
- 加密采样与极值（同一格式：量的定义 `quantity`/`definition` 和单位 `unit`、采样范围 `range` 与步长 `step`、极值 `value` 及位置 `at`、全部局部极值 `local`、读不出的点 `missing`、加密使极值移动的量 `refine_shift`、极值是否在区间端点 `at_edge`、全部样点 `samples`、覆盖是否够 `coverage`（`ok` 和 `notes`：有读不出的点、加密后极值变化超过相对 1e-3、有效样点少于 3 个时为假））：`ctx.cra(direction=(0, 1), wave=None, n=33, refine=2, to=None)` 主光线角（实际主光线与目标面局部 z 轴的夹角，取各波长中最大，度；`direction` 是归一化视场里的终点，矩形归一化的对角是 `(1, 1)`；任一波长主光线到不了的视场点记为读不出；另给极值处各波长的值 `per_wave_at_extreme`）；`ctx.field_profile(fn, direction, n=17, find="max", refine=1, unit=, quantity=)` 对任意逐视场量调 `fn(ctx, hx, hy)`（相对照度、畸变等，`fn` 不能改镜头）；`ctx.wave_profile(fn, lo, hi, n=33, find="min", refine=2, wave=None)` 把第 `wave` 个波长（默认主波长）依次设为 `[lo, hi]` µm 内各点调 `fn(ctx, wave)`，结束后恢复，用来找离散波长之间的低谷；`kit.profile(evaluate, lo, hi, n, find=, refine=)` 是它们共用的一维加密取极值，`evaluate` 一次收到一批 x。2026-10-19 nightvision 任务里，只看视场边缘漏掉了中间视场 19.88° 的主光线角，五个波长的透过率漏掉了加密后出现的低谷，所以判定脚本里的极值量用这些函数，并把 `coverage` 不足当作测量没完成
- `ctx.measure(ids=None)`：按需求单的判定测当前镜头（与 `check`、`round` 同一套定义），不存证据、不进 `evaluations/`，返回 `values`、`shortfall`（只含所测各项）、`gaps`、`hard_failing`、`invalid`、`errors`。改镜头—测量的循环用它，不必在脚本里重写判定
- `ctx.show()`：把当前镜头（布局、Lens Data、交叠检查）推给流程图窗口，长脚本在用户该看的状态调用；每条命令结束时本来就推一次
- `ctx.cache`、`ctx.memo(key, compute)`、`ctx.lib(name)`：一次判定（check、round、measure、扫描的一个点）内的各判定脚本共享同一个 `cache`，镜头在判定期间不变，所以几项判定共用的追迹用 `ctx.memo` 只算一次；`ctx.lib("fiberlib")` 加载 `checks/fiberlib.py`，同一 cache 内只加载一次。exec 脚本自己改了镜头之后要 `ctx.cache.clear()`
- `ctx.map(fn, items, workers=None, reset=True, args=None)`：并发评估。`fn` 是本脚本里定义的函数名、任务内文件 `"explore/x.py:fn"` 或 `"saos.scan:point"`，在 worker 里以 `fn(w, item)` 调用，返回值要能 JSON 化。worker 是后台进程常驻的池：第一次调用时并发启动（本机每个实例 5.8–7.9 s），之后的调用直接复用，空闲 `SAOS_POOL_IDLE_S` 秒（默认 600）后关闭并释放许可证，`ctx.pool_close()` 立即关闭；worker 进程以低于正常的优先级运行。每次调用把已保存的当前镜头复制给每个 worker（有未保存修改时返回 `UNSAVED_CHANGES`），`reset` 时每个条目前重新加载（约 0.08 s）。`w` 有上面全部参数、光线和操作数方法，以及 `measure`、`args`（调用时的 `args`）、`path(rel)`、`job`、`worker`、`reload()`、`cache`（每个条目一份）。`workers` 默认 4，上限为可用核数；许可证上限 8 个实例（含用户打开的窗口和后台进程自己），拒绝过的规模在池关闭前不再尝试。返回按条目顺序的 `results`、出错条目的 `errors`（含异常信息）、各 worker 的条目数与忙碌时间、`startup`（新启动的 worker 和不可用原因）；全部不可用时报 `ZOS_ERROR`。计划、脚本副本和结果存到 `parallel/NNN/` 并封存，`saos job cancel` 会在当前条目完成后停止。2026-10-17 实测（Cooke 样例，36 点，每点求根加三视场点斑）：池已启动时 1、2、4、6 个 worker 分别 6.76、3.83、2.21、1.63 s
- 搜索的控制逻辑（`from saos import search`，评估仍由脚本经 ZOS-API 完成）：`search.grid(axes)` 网格；`search.root(f, lo, hi, target=0, scan=9, xtol=None)` 先扫后 Brent 求根，跳过返回 None/inf 的无效点（同一例中比“粗扫 17 点+二分 14 次”少 58% 评估）；`search.minimize(f, start, step, bounds=None, method="nelder-mead"|"powell", max_evals, max_seconds)` 无导数寻优，变量按 `step` 归一化，无效点当作很差的点，预算用完时保留已找到的最好点；`search.ranked(rows)` 按 `shortfall` 排序

每次执行都把脚本副本和结果存为任务的 `scripts/NNN-<名>.py` 与 `.json`，交付的数值可以追溯到具体脚本。脚本抛出异常时返回 `SCRIPT_ERROR`（含 traceback），后台进程继续运行；连接已中断时按连接中断处理。执行结束后若 OpticStudio 的 NeedsSave 置位，当前镜头记为有未保存修改；只读的 Zernike 拟合也会置位，所以这一判断偏保守。extension 模式下，exec、check、round、scan、opt 这些长操作默认暂停窗口刷新，结束后刷新一次（实测实时刷新让一次验收评估从 22 s 变成 98 s）；`exec --live`（请求里的 `live`）或环境变量 `SAOS_UI_LIVE=1` 全程刷新。

## 长任务

后台进程一次只处理一个请求。它正在跑长操作（`opt local/hammer/global`、`exec`、`check`、`round`、`scan`，以任务 `progress.json` 的 `status: running` 和 `daemon_pid` 为准）时，其他请求直接返回 `BUSY`，不再在管道里排到客户端超时；`ping`（`daemon status`）和 `shutdown` 照旧，流程图窗口的控制页照旧排队。

`exec`、`opt local`、`opt hammer`、`opt global`、`check`、`round` 加 `--detach` 后命令立即返回 `{"detached": true, "run": "runs/NNN-<op>.json", "launcher_pid", "follow"}`（`launcher_pid` 是 venv 启动器的进程号，真正发请求的解释器进程号写在结果文件的 `pid` 里）：一个脱离的子进程替你发送请求、不设客户端时限地等到结束，把响应写进任务的 `runs/NNN-<op>.json`（`status` 为 `running`、`done` 或 `failed`，`done` 时 `result` 与不加 `--detach` 的返回值相同，`failed` 时 `error`），请求本身在旁边的 `runs/NNN-<op>.request.json`。进度照常在 `progress.json`，`saos job show` 同时列出最近的运行，`saos job cancel` 照常生效。代理自己的命令时限（常见 2–4 分钟）短于计算时，用它；2026-10-19 nightvision 任务里一次 250.6 s 的计算在客户端 240 s 时被当成失败，后台其实完成了。

## 后台窗口与执行记录

`daemon start` 使用 Windows 无窗口方式启动后台 Python，标准输出和错误输出写入 `<SAOS_HOME>/daemon/daemon.log`。旧启动方式使用 `DETACHED_PROCESS`，虚拟环境启动器的解释器子进程仍可能弹出空白终端；改为 `CREATE_NO_WINDOW` 后，新启动的后台进程不再创建该控制台窗口。已运行的进程需等任务结束后正常停止，再启动才会采用新方式。

在 Git Bash 中，用 `SAOS_INSTANCE=verify uv run saos daemon status` 查询指定实例的连接状态和已加载任务。执行记录位于 `<SAOS_HOME>/jobs/<任务编号>/log.jsonl`，包含操作名、成功与否及耗时；可用 `tail -f` 跟随查看。每条记录在操作完成后写入，因此长时间计算中日志没有新增记录不代表进程已卡死。后台串行处理请求，忙碌时 `daemon status` 也可能等待并超时；目前没有独立的实时进度接口。

## 连接手动打开的窗口（extension 模式）

调试时可以让后台进程连接一个手动打开的 OpticStudio 窗口，在窗口里看到镜头的变化：打开 OpticStudio 后执行 `saos daemon start --mode extension`。启动流程通过 Windows UI Automation 选择 Programming、打开 Interactive Extension，将 Auto Close on Disconnect 设为 Off，核对等待连接状态并读取实际实例编号。已有等待对话框会直接复用；窗口已连接到其他扩展时拒绝接管。存在多个窗口时用 `SAOS_UI_PID` 指定进程编号，否则拒绝猜选。控件不可用或窗口被模态对话框阻塞时返回明确错误。

- 之前实测后台进程退出后扩展对话框关闭，需要重新开启；用户随后在本机确认，取消勾选 “Auto close on disconnect” 即可避免该问题。重连前检查对话框仍处于等待连接状态，若已关闭则重新开启。没有开启交互扩展时启动返回 `ZOS_ERROR`。该设置不代替 OpticStudio 进程意外退出后的连接恢复处理。
- 窗口实例与后台实例的验收结果在数值上相差约 10⁻⁵（相对），拟合残差贴近界限时结论可能翻转（docs/recipes/microscope/step5-calibration.md 3.2 节）。正式实验只用 standalone 实例。
- 窗口实时刷新会拖慢运算：实测一次验收评估 98 s，standalone 下为 13 s。所以验收评估、轮次记录、复现校验期间暂停刷新，结束后窗口显示最终状态（暂停后验收评估为 22 s）。局部优化不暂停，实测 15.8 s，standalone 下为 13 s。设环境变量 `SAOS_UI_LIVE=1` 后全程刷新。
- 后台进程退出时不关闭窗口。

## 错误码

| 错误码 | 含义 |
|---|---|
| `DAEMON_NOT_RUNNING` | 后台进程没有运行，先 `daemon start` |
| `DAEMON_ALREADY_RUNNING` | 已有后台进程（同一 `SAOS_HOME` 只允许一个） |
| `DAEMON_START_FAILED` / `DAEMON_TIMEOUT` | 启动失败或超时，`details.log_tail` 附后台日志 |
| `ZOS_LICENSE` / `ZOS_VERSION_MISMATCH` | 许可证不支持 ZOS-API；OpticStudio 不是 24.1.0 |
| `ZOS_CONNECTION_LOST` | OpticStudio 进程已退出。后台进程随即退出，未保存的修改丢失，需要重新 `daemon start` |
| `ZOS_ERROR` | OpticStudio 操作抛出异常，连接仍正常，后台进程继续运行 |
| `ZOS_LOAD_FAILED` / `ZOS_SAVE_FAILED` | OpticStudio 无法加载或保存文件 |
| `VERIFY_FAILED` | 保存后重新加载与保存前不一致，`details.diffs` 列出差异 |
| `UI_BLOCKED` | extension 模式下 OpticStudio 窗口被模态对话框挡住（如自动保存恢复提示、错误消息框），`details.dialogs` 列出标题；在窗口中关闭后重试 |
| `SCRIPT_ERROR` | `exec` 的脚本抛出异常，`details` 给出脚本存档路径和 traceback；后台进程继续运行 |
| `SPEC_CHANGED` | 需求单或检查脚本改动后还没有新轮次时生成报告 |
| `VALUE_NOT_APPLIED` | 修改没有生效（例如被求解覆盖），`details` 给出目标值、实际值和求解类型 |
| `LENS_MODIFIED_BY_CHECK` | 记录轮次时，判定脚本改动了镜头（取值前后的镜头数据相差超过相对 1e-12、绝对 1e-15；写回原值时 OpticStudio 舍到 15 位有效数字的末位差不算）；本轮不记录。把判定改成只读后重试 |
| `DUPLICATE_ROUND` | 同一镜头在同一需求和判定版本下已经记过一轮（多为超时后的重试）；`details` 给出已有的轮次 |
| `EVIDENCE_TAMPERED` | 生成报告时发现封存的证据文件被改动或删除（`saos job verify` 可列出） |
| `DEMAND_NOT_CONFIRMED` | 记录轮次或运行优化时，当前需求单版本没有用户确认；`details.demand_sha` 给出版本。给用户看需求单，再 `demand confirm` |
| `CANCELLED` | 计算按 `saos job cancel` 的请求停止；已完成的部分照常存档 |
| `DEMAND_BLOCKED` | 需求单有 blocking 待确认问题，`details.blocking_questions` 列出；先问用户，回答后改需求单 |
| `MODEL_NOT_APPROVED` | 记录轮次时，当前镜头的结构与任何已确认的建模说明都不一致（或还没有确认过）；`details.changed` 列出与最近一次确认相比变了什么。重新 `lens describe`、给用户看、`lens approve` |
| `NO_LENS_LOADED` / `JOB_MISMATCH` | 没有加载镜头；加载的是另一个任务的镜头 |
| `UNSAVED_CHANGES` | 当前镜头有未保存修改，不能加载其他镜头、卸载或退出 |
| `PATH_OUTSIDE_JOB` | 文件路径越出本任务的 `lenses/` |
| `JOB_NOT_FOUND` / `JOB_EXISTS` / `FILE_NOT_FOUND` / `FILE_EXISTS` | 任务或文件不存在、已存在 |
| `BAD_ARGS` / `UNKNOWN_OP` | 参数错误，包括不在当前玻璃目录中的材料名、未知的操作数 |
| `BUSY` | 后台进程正在跑某个任务的长操作（`details.running` 列出操作、任务和开始时间），这个请求不排队、直接拒绝，免得排到客户端超时后又被重复提交。等它结束（`saos job show`）或取消（`saos job cancel`）后再发；`daemon stop` 在有分离运行的扫描时也返回它 |

验收评估无法完成时（检查光线失败或被截断、调焦时轴上光线失败且回退后仍无有效位置、调焦不收敛、Zernike 拟合残差超限），命令本身成功返回，结果中 `verdict` 为 `invalid`、`spec_shortfall` 为 `inf`，`invalid` 字段说明原因。

`DAEMON_TIMEOUT` 只表示客户端没有及时收到响应，后台可能仍在计算，或随后保存完整结果；错误信息和 `details`（`still_running`、`running`）都写明这一点。不要重新提交同一个计算：用 `saos job show` 跟进度，下次改用 `--detach`。先检查对应任务的 `eval/<镜头名>.accept.json`，核对输入、规格及运行版本后再决定是否重试；不要直接将超时当作 `invalid` 或自动终止实例。`focus_trace` 随完整结果保存，不保证进程被强制结束前的每一步都已落盘。第 5 步曾在客户端 120 s 超时后取得后台耗时 173.85 s 的完整结果。

## 目录

运行产物全部在仓库下的 `work/`（已加入 .gitignore）。一个后台进程对应一个实例目录 `work/<实例名>/`，默认实例是 `main`；并行批次、专项探索各用独立实例，用 `SAOS_INSTANCE=<实例名>` 选择，不要在仓库根目录另建 `work-*`。`SAOS_HOME` 仍可指向任意路径，优先级最高，主要供测试隔离。

用户需求和访谈记录属于具体设计任务，保存在该任务的 `sources/` 下，不放进仓库的 `docs/`。`docs/` 只放跨任务的方法、规则和命令说明。

```
work/<实例名>/
  daemon/            state.json、authkey、daemon.log（后台进程运行时存在）；ui.json、ui.log、ui-chrome/（saos ui 的服务状态、日志和 Chrome 配置目录）
  jobs/<job_id>/     每一步产生哪些文件、谁写、能否改动：见 skills/saos/SKILL.md「Where files go」
    job.json         任务状态
    log.jsonl        执行记录：每条命令的参数、结果、错误码、耗时
    seals.jsonl      封存文件的哈希（saos job verify 据此检查）
    sources/         demand.yaml（需求单）、interview.md（拷问过程）、materials.yaml（材料来源）
    checks/          需求单引用的判定脚本
    lenses/          镜头文件；rounds/rNNN.zmx 与 captures/NNN-unsaved.zmx 是工具写入的归档，lens save 不能写入；scans/NNN/、global/NNN/（opt global 的前 10 名）、hammer/NNN/（Hammer 残留的中间文件）由工具写入
    notes/           model-NNN.json（lens describe 的说明输入，工具保存）与 agent 的说明文字
    models/          建模说明 NNN-<镜头>/：index.html、model.json、layout-*.png、approval.json（用户确认）
    scripts/         saos exec 的脚本副本和结果
    analyses/        lens analysis 的数值导出
    screens/         显性调试截图
    evaluations/     每次 check / round 的需求快照、support/（checks/ 副本）、分析导出、result.json（测量的权威记录）；round 另有 viz.json 与 viz/（saos ui 的数据）
    ledger.jsonl     调试轮次台账（指向 evaluations/NNN/result.json）；旧任务另有 rounds/rNNN.json
    report/          NNN-rMMM-<状态>.html（每次生成的封存副本）与 index.html（最新）
    runs/            --detach 运行的请求 NNN-<op>.request.json 与响应 NNN-<op>.json
    explore/<主题>/  不经过 saos exec 的探索代码、中间镜头和日志，附 README.md
    spec/ eval/      显微物镜配方专用（规格与验收评估），只在用到时创建
```

## 环境变量

| 变量 | 默认值 | 用途 |
|---|---|---|
| `SAOS_INSTANCE` | `main` | 实例名，运行目录为 `<仓库>/work/<实例名>`；只允许字母、数字、`-`、`_` |
| `SAOS_HOME` | 未设置 | 直接指定运行目录，优先于 `SAOS_INSTANCE`；任务目录、后台进程状态和命名管道名都由最终目录推出 |
| `SAOS_OPTICSTUDIO_DIR` | `C:\Program Files\Ansys Zemax OpticStudio 2024 R1.00` | 加载 ZOS-API DLL 的目录 |
| `SAOS_BACKEND` | `zos` | 测试时设为 `fake`，不启动 Zemax |
| `SAOS_LIBRARY` | `<仓库>/library/microscope` | 显微物镜配方的初始结构库位置 |
| `SAOS_LANG` | 未设置 | 给人看的输出（交付报告、后台日志）的语言，`zh` 或 `en`；未设置时取系统界面语言 |
| `SAOS_UI` | 未设置 | 设为 0 时 `demand confirm` 不打开流程图窗口（测试、无界面环境） |
| `SAOS_UI_LIVE` | 未设置 | extension 模式下设为 1 时，验收评估等操作期间也实时刷新窗口 |

## 测试

- `uv run pytest`：不启动 Zemax 的单元测试，以及用假后端启动真实后台子进程的生命周期测试。
- `SAOS_RUN_ZEMAX=1 uv run pytest -m zemax -s`：在本机 OpticStudio 上跑第 2 步的命令流程（`test_zemax_e2e.py`）和第 3 步的最小闭环（`test_zemax_loop.py`），共约 80 s。

## 显性调试命令

- `saos lens capture --job J --as backup.zmx`：将窗口当前内容另存到任务，不覆盖源文件或已有副本；可保留连接前尚未保存的修改。
- `saos lens view --job J [--viewer cross-section|3d|shaded] [--analysis TYPE]`：extension 模式下打开 Lens Data 和布局窗口（默认剖面图，画不了时改用 3D Viewer，`layout.fallback_reason` 给出原因），按需再开一个分析，并刷新；关掉的窗口重新打开；UpdateMode 设为 EditorsOnly，分析窗口由 saos 在改动后刷新。standalone 模式下返回 BAD_ARGS。
- extension 模式是可选的：用户看建模过程靠流程图窗口的当前镜头视图，任何阶段都不要求 extension（ADR-0011）。连接 extension 时，建模阶段每条改动镜头的命令之后 saos 保证 Lens Data 和布局窗口开着。
- `saos lens screenshot --job J [--label L]`：截取 OpticStudio 窗口 PNG。

extension 模式下，`lens load`、`lens unload`、`lens build` 换掉窗口内容前，若 OpticStudio 的 NeedsSave 置位（分析窗口的刷新也会置位，无法与用户修改区分），先把窗口内容存成 `lenses/captures/NNN-unsaved.zmx`，返回值 `captured` 给出路径，然后继续。脚本中的 `ctx.load` 和显微物镜配方的 accept、repro 同样先存副本，返回值带 `captured`。agent 自己未保存的修改（后台 dirty）仍返回 UNSAVED_CHANGES，先 `lens save` 或加 `--discard`；`--discard` 只放弃 agent 自己的修改，窗口内容照样先存副本。

光纤侧视框架的搭建脚本已移出命令行，见 `experiments/fiber/` 和 TODO。
