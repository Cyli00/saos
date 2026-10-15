# saos 命令行

agent 只通过 `uv run saos ...` 操作 Zemax。每条命令向标准输出打印一个 JSON 对象：

- 成功：`{"ok": true, "result": ...}`，退出码 0
- 失败：`{"ok": false, "error": {"code", "message", "details"}}`，退出码 1；命令行参数错误时退出码为 2，格式相同

`daemon`、`job` 命令在命令行进程内执行；`lens`、`opt`、`exec` 命令转发给常驻后台进程，由它持有 OpticStudio 连接（ADR-0002）。`microscope` 下是显微物镜配方的命令，其中复现、验收、扰动、轮次通过 `exec` 在后台执行。

## 命令

需求访谈和只读设计计划新增 `microscope interview check`、`microscope plan create/show/confirm/handoff`，均不连接 Zemax。访谈记录格式、参数及用户确认流程见 [interview-and-plan.md](./recipes/microscope/interview-and-plan.md)。

| 命令 | 作用 |
|---|---|
| `saos daemon start [--mode standalone\|extension]` | 启动后台进程并连接 OpticStudio 2024 R1，就绪后返回。本机实测约 6 s。`extension` 自动准备已打开窗口的 Interactive Extension 并连接，见下文 |
| `saos daemon status` | 是否在运行、连接是否正常、当前加载的镜头 |
| `saos daemon stop [--discard] [--force]` | 正常退出，等后台进程和它的 OpticStudio 都退出后才返回。有未保存修改时拒绝退出，除非加 `--discard`；`--force` 在无法正常退出时强制结束 |
| `saos job create --name N [--spec FILE]` | 新建设计任务，编号为 `时间-名字`，规格文件复制进任务目录 |
| `saos job list` / `saos job show JOB [--log K]` | 列出任务；查看状态、镜头文件和最近 K 条执行记录 |
| `saos lens import --job J SRC [--as NAME] [--overwrite]` | 把外部 .zmx 复制进任务的 `lenses/`，原文件不动 |
| `saos lens load --job J FILE [--discard]` | 加载任务中的镜头文件 |
| `saos lens unload --job J [--discard]` | 卸载当前镜头 |
| `saos lens info --job J` | 系统数据：孔径、视场、波长、玻璃目录、面数、光阑面 |
| `saos lens surfaces --job J` | 各面的类型、半径、厚度、材料、半口径、二次曲面系数、求解类型 |
| `saos job verify <job>` | 检查工具写入并封存的证据文件（`seals.jsonl`）有没有被改动或删除；返回 `intact` 和 `problems` |
| `saos lens approve --job J --model models/NNN-<镜头> --quote "<用户原话>"` | 记录用户对这份建模说明的确认（`approval.json`，含结构指纹和当时仍在的自动检查结果）。之后 `saos round` 只接受结构与某份已确认说明一致的镜头 |
| `saos lens describe --job J [--notes N.json] [--lang zh\|en]` | 建模说明：写 `models/NNN-<镜头>/index.html`、`model.json` 和布局图（3D 三个视角，轴对称系统另有截面）。逐面列出面型、所有非零附加参数、材料来源与各波长折射率（INDX 实测）、孔径、求解和顶点全局位置，并给出系统孔径、物方远心、光线瞄准、视场、波长。N.json 写每个面代表的实物、假设和一阶分析：`{"surfaces": {"3": "…"}, "assumptions": ["…"], "first_order": ["…"]}`。返回值的 `warnings` 是自动检查的问题（模型玻璃、目录外材料、无色散介质、非平面的菲涅尔基底、物方 NA 未设远心、渐晕因子、缺少面说明或一阶分析） |
| `saos lens set --job J --surface N --param P --value V` | 修改一个面参数：radius、thickness、conic、semi_diameter、material、comment。数值可写 `inf` |
| `saos lens save --job J [--as NAME] [--overwrite] [--no-verify]` | 保存；默认保存后从磁盘重新加载，与保存前逐面核对 |
| `saos lens operand --job J TYPE [--int1 --int2 --d1 … --d6]` | 计算一个优化操作数，例如 `EFFL --int2 2` |
| `saos lens analysis --job J TYPE [--set KEY=VALUE …] [--keep-open]` / `--list` | 运行一个 OpticStudio 分析（AnalysisIDM 名称，如 StandardSpot、FftMtf、RayFan、WavefrontMap、FieldCurvatureAndDistortion），导出数据序列、网格（超过 256×256 时降采样）、点列图 RMS/GEO 半径和表头，存为任务的 `analyses/NNN-TYPE.json`，并保存 OpticStudio 的文本全文 `.txt`。设置名取自分析的设置对象，Field、Wavelength、Surface 用编号或 `all`；枚举按成员名。返回值省略网格本体，只给统计量 |
| `saos lens view --job J [--viewer lde\|cross-section\|3d\|shaded] [--analysis TYPE] [--set KEY=VALUE …]` | 显性调试：打开 Lens Data 或一种 System Viewer，加一个像质分析（默认 StandardSpot，按系统最看重的像质选），按当前镜头刷新。选择会被记住；之后 load、set、build、opt local、exec、round 结束时自动刷新已打开的分析窗口。连接 extension 后自动按默认选择打开 |
| `saos lens screenshot --job J [--label L]` | 显性调试：把 OpticStudio 的可见窗口截成 PNG，存为任务的 `screens/NNN-L-K.png`（PrintWindow，被遮挡也能截；最小化的窗口跳过） |
| `saos demand show --job J` | 校验需求单 `sources/demand.yaml`，给出版本哈希、确认记录、待确认问题和访谈总结，不连接 OpticStudio |
| `saos demand confirm --job J --quote "<用户原话>"` | 记录用户对当前需求单版本的确认，写入 `confirmations.jsonl` |
| `saos probe --job J --plan PLAN.json` | 扰动诊断：按 agent 事先写下的预测，对 1–6 个变量（至少一个阴性对照）各取 ±delta，带补偿量（`quick_focus` 或任务内脚本）实测需求单各项，给出按目标影响排序、预测与实测不符的项和审视提醒。每次都从同一份基准副本重新加载，结束后恢复镜头；计划、基准副本和每次测量存到 `probes/NNN/` 并封存。不需要需求单已确认。格式见 `skills/saos/SKILL.md`「Diagnose before you escalate」 |
| `saos check --job J` | 按需求单对当前镜头逐项实测和判定，不记录。需求单未确认时也能用，便于整理需求时先看数值 |
| `saos round --job J [--tier local\|variables\|materials\|global\|structure] [--note TEXT]` | 记录一个调试轮次：存为 `lenses/rounds/rNNN.zmx`，逐项实测判定（全文存为 `evaluations/NNN/result.json`），写入 `ledger.jsonl`，返回终止判断（continue / achieved / exhausted / limit）、还没试过的层级和需求提要 `brief`。第 0 轮不填 tier。当前需求单版本未确认（`DEMAND_NOT_CONFIRMED`）、有 blocking 待确认问题（`DEMAND_BLOCKED`）、已经终止、或镜头结构与已确认的建模说明不一致（`MODEL_NOT_APPROVED`）时拒绝。每轮记下所用材料；含模型玻璃或不满足常量、变量范围的轮次不能交付 |
| `saos report --job J [--lang zh\|en]` | 由需求单和 ledger 生成交付报告 `report/index.html`，不连接 OpticStudio；语言见「报告语言」 |
| `saos exec SCRIPT --job J [--arg K=V …] [--args-file JSON] [--name N] [--quiet]` | 在后台进程内执行脚本（ADR-0007），见下文「脚本执行」 |
| `saos microscope adapt EXTRACT --focal-length F --field-number N [--tube-lens 200] [--catalogs …] [--stop rear_focal\|r<面号>] --out DIR` | 把子 agent 提取的面数据适配成标准设计布局的处方，输出 prescription.json、expect.json、adapt_report.json（不经后台进程） |
| `saos lens build --job J PRESCRIPTION --as NAME [--overwrite]` | 按处方用 ZOS-API 搭建镜头，保存到 lenses/ 并加载 |
| `saos microscope repro --job J EXPECT` | 复现校验：焦距、物方 NA、顶点工作距离、f_TL/EFL 与期望值比较。每项允许的偏差取相对误差 1% 与 EXPECT 中 `tolerance_mm`（原文印刷精度的舍入包络半宽）的较大者。只临时调焦，结束后恢复 |
| `saos microscope perturb --job J --seed S --radius-rel A [--thickness-rel B]` | 受控扰动：物镜各面曲率、厚度按固定随机种子均匀改变 |
| `saos microscope accept --job J [--spec FILE]` | 按光学规格做验收评估，结果和验收模型存到任务的 eval/。结果的 `focus_trace` 列出调焦的每个试探位置（波长、厚度、轴上光线失败数、c4、拟合残差） |
| `saos opt vary --job J --param radius\|thickness\|conic --surfaces 1-16 [--off]` | 设变量或取消变量。物面只允许厚度（即无盖玻片时的工作距离间隔） |
| `saos opt merit --job J clear\|wizard\|add\|show [--file JSON]` | 评价函数：清空；按向导生成默认评价函数（type、data、reference、rings、arms 等）；追加操作数列表；查看 |
| `saos opt local --job J [--cycles auto\|1\|5\|10\|50] [--cores N]` | 局部优化（阻尼最小二乘），默认用全部逻辑核减 2。开始前做与 round 相同的需求单检查（已确认、没有 blocking 问题） |
| `saos microscope round --job J [--spec FILE] [--note TEXT]` | 记录一个调整轮次：存为 lenses/rounds/rNNN.zmx，做验收评估，返回停机判断（pass / stagnated / limit / continue）。第 0 轮时冻结光学规格，之后不能更换。后台协议另有 `observe` 参数，供基准实验使用：照常冻结和核对，停机判断只记录不执行 |
| `saos microscope library check EXTRACT` | 入库前用近轴计算核对提取结果：焦距和工作距离的印刷值落在印刷精度的舍入包络内，或与计算值相差不超过 1%，即为一致（不经后台进程） |
| `saos microscope library add ID --extract FILE [--catalogs …] [--focal-length F] [--field-number N] [--stop rear_focal\|printed\|r<面号>]` | 把提取结果适配成处方，写入 `library/microscope/entries/ID/`，状态为 extracted（不经后台进程） |
| `saos microscope library verify ID --job J [--discard]` | 搭建、复现校验、探测验收、推导基准规格并确认原结构 G = 0，结果写回条目。当前镜头有未保存修改时返回 UNSAVED_CHANGES；显式加 `--discard` 才会丢弃，包括其他任务的修改。复现不通过的条目状态为 rejected |
| `saos microscope library list` | 列出条目：状态、焦距、NA、片数、能否作基准及原因 |
| `saos microscope library query --na A --magnification M [--class achromat\|plan_achromat] [--field-number N] [--working-distance W] [--limit K]` | 按光学规格从已收录条目中选初始结构 |
| `uv run python experiments/bench/run.py split` | 把可作基准的条目分为调参组和验证组，写入 `library/microscope/benchmarks.json` |
| `uv run python experiments/bench/run.py run ENTRY [--seed S] [--radius-rel A] [--thickness-rel B] [--no-amplify] [--stretch wd=F length=F] [--max-rounds N] [--cores K] [--job J] [--discard]` | 开发用基准任务：搭建条目、受控扰动（默认幅度按 ×1、×2、×4、×8 放大到 G0 ≥ 1）或拉伸规格，按参考策略逐轮优化，每轮用 observe 模式记录，不按停滞规则中途停机；轨迹写入任务的 `bench.jsonl`。`--job` 续跑；后台进程里有其他任务的未保存镜头时默认拒绝，`--discard` 才放弃 |
| `uv run python experiments/bench/run.py plan --batch B` | 生成正式批次的实验清单 `library/microscope/calibration/B.json`，记录任务、分组、策略版本和代码哈希；已存在时拒绝覆盖 |
| `uv run python experiments/bench/run.py run --batch B --task T [--cores K]` | 按清单运行正式任务；客户端或后台进程的代码版本与清单不同、或后台进程不是 standalone 时拒绝。本 SAOS_HOME 中已有该任务时续跑 |
| `uv run python experiments/bench/run.py calibrate --batch B --homes DIR… [--validate N REL CAP]` | 按清单准入正式轨迹并列出排除原因、重复起点，在调参组主实验上按冻结规则选停机参数；给出 `--validate` 时在验证组主实验和两组压力测试上检验固定参数 |

结构库的收录规则、提取格式和基准规格的推导见 [library.md](./recipes/microscope/library.md)。

`--job` 必须与当前加载的镜头属于同一任务；镜头文件名一律相对该任务的 `lenses/`。

## 需求单

`sources/demand.yaml` 的格式、目标差距的算法和终止规则以英文写在 [skills/saos/demand.md](../skills/saos/demand.md)，agent 直接读那一份；这里不再重复。

要点：只收 `constants`、`variables`、`objectives`、`open_questions`、`summary`、`lang`、`stop`，其他字段一律拒绝；判定方法三选一（operand、analysis、script）；界限写有限数值的 `max`、`min` 或 `equals` + `tol`；访谈总结最后一句固定；版本只取三类条目和 `stop` 的哈希，改写总结、待确认问题或 `lang` 不需要重新确认；确认只能由 `saos demand confirm` 记录用户原话。旧格式 `sources/harness.yaml` 不会自动转换，只有它时命令返回 `FILE_NOT_FOUND` 并提示改写。

每次检查的快照和证据存在新的 `evaluations/NNN/`，返回的 `evidence_file` 指向完整结果。修改检查脚本会开始新的判定方法版本，旧轮次不再参与进展比较和交付选择；需求单或脚本改动后还没记录新轮次时，`saos report` 返回 `SPEC_CHANGED`。脚本版本只跟踪直接指定的源码，不跟踪它导入的模块。

## 报告语言

`saos report` 的语言按以下顺序选择：`--lang` > 需求单的 `lang` > 环境变量 `SAOS_LANG` > 系统界面语言（Windows 取 `GetUserDefaultUILanguage`）> 英文。后台日志按 `SAOS_LANG` 和系统界面语言选择。给 agent 读的错误信息和命令帮助只有英文。

## 脚本执行

脚本定义 `run(ctx)`，返回可 JSON 化的结果。`ctx` 提供：

- `ctx.system`：OpticStudio 的 PrimarySystem；`ctx.zosapi`：ZOSAPI 命名空间；`ctx.backend`：saos 后端；
- `ctx.job`、`ctx.args`（`--arg` 和 `--args-file` 合并后的字典）、`ctx.lens`（当前加载的镜头路径，可能为 None）、`ctx.dirty`；
- `ctx.load(name)`、`ctx.save_as(name, overwrite=False)`：切换或保存镜头时必须用它们，后台进程才能跟踪当前文件；`ctx.load` 换掉窗口内容前会把未保存的窗口内容存成副本（`ctx.capture_window()` 也可单独调用，副本路径列在执行结果的 `captured`）；`ctx.mark_dirty()`；`ctx.path(rel)`：任务目录内的路径。

每次执行都把脚本副本和结果存为任务的 `scripts/NNN-<名>.py` 与 `.json`，交付的数值可以追溯到具体脚本。脚本抛出异常时返回 `SCRIPT_ERROR`（含 traceback），后台进程继续运行；连接已中断时按连接中断处理。执行结束后若 OpticStudio 的 NeedsSave 置位，当前镜头记为有未保存修改；只读的 Zernike 拟合也会置位，所以这一判断偏保守。`--quiet` 在 extension 模式下暂停窗口刷新，显微物镜的验收类命令默认带上。

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
| `SPEC_CHANGED` | 调整轮次中，规格路径或补全后的验收配置与第 0 轮冻结的不一致；需要新建任务 |
| `VALUE_NOT_APPLIED` | 修改没有生效（例如被求解覆盖），`details` 给出目标值、实际值和求解类型 |
| `LENS_MODIFIED_BY_CHECK` | 记录轮次时，判定脚本改动了镜头（取值前后的镜头内容不同）；本轮不记录。把判定改成只读后重试 |
| `DUPLICATE_ROUND` | 同一镜头在同一需求和判定版本下已经记过一轮（多为超时后的重试）；`details` 给出已有的轮次 |
| `EVIDENCE_TAMPERED` | 生成报告时发现封存的证据文件被改动或删除（`saos job verify` 可列出） |
| `DEMAND_NOT_CONFIRMED` | 记录轮次或运行优化时，当前需求单版本没有用户确认；`details.demand_sha` 给出版本。给用户看需求单，再 `demand confirm` |
| `DEMAND_BLOCKED` | 需求单有 blocking 待确认问题，`details.blocking_questions` 列出；先问用户，回答后改需求单 |
| `MODEL_NOT_APPROVED` | 记录轮次时，当前镜头的结构与任何已确认的建模说明都不一致（或还没有确认过）；`details.changed` 列出与最近一次确认相比变了什么。重新 `lens describe`、给用户看、`lens approve` |
| `NO_LENS_LOADED` / `JOB_MISMATCH` | 没有加载镜头；加载的是另一个任务的镜头 |
| `UNSAVED_CHANGES` | 当前镜头有未保存修改，不能加载其他镜头、卸载或退出 |
| `PATH_OUTSIDE_JOB` | 文件路径越出本任务的 `lenses/` |
| `JOB_NOT_FOUND` / `JOB_EXISTS` / `FILE_NOT_FOUND` / `FILE_EXISTS` | 任务或文件不存在、已存在 |
| `BAD_ARGS` / `UNKNOWN_OP` | 参数错误，包括不在当前玻璃目录中的材料名、未知的操作数 |

验收评估无法完成时（检查光线失败或被截断、调焦时轴上光线失败且回退后仍无有效位置、调焦不收敛、Zernike 拟合残差超限），命令本身成功返回，结果中 `verdict` 为 `invalid`、`spec_shortfall` 为 `inf`，`invalid` 字段说明原因。

`DAEMON_TIMEOUT` 只表示客户端没有及时收到响应，后台可能仍在计算，或随后保存完整结果。先检查对应任务的 `eval/<镜头名>.accept.json`，核对输入、规格及运行版本后再决定是否重试；不要直接将超时当作 `invalid` 或自动终止实例。`focus_trace` 随完整结果保存，不保证进程被强制结束前的每一步都已落盘。第 5 步曾在客户端 120 s 超时后取得后台耗时 173.85 s 的完整结果。

## 目录

运行产物全部在仓库下的 `work/`（已加入 .gitignore）。一个后台进程对应一个实例目录 `work/<实例名>/`，默认实例是 `main`；并行批次、专项探索各用独立实例，用 `SAOS_INSTANCE=<实例名>` 选择，不要在仓库根目录另建 `work-*`。`SAOS_HOME` 仍可指向任意路径，优先级最高，主要供测试隔离。

用户需求和访谈记录属于具体设计任务，保存在该任务的 `sources/` 下，不放进仓库的 `docs/`。`docs/` 只放跨任务的方法、规则和命令说明。

```
work/<实例名>/
  daemon/            state.json、authkey、daemon.log（后台进程运行时存在）
  jobs/<job_id>/     每一步产生哪些文件、谁写、能否改动：见 DESIGN.md「任务目录」
    job.json         任务状态
    log.jsonl        执行记录：每条命令的参数、结果、错误码、耗时
    seals.jsonl      封存文件的哈希（saos job verify 据此检查）
    sources/         demand.yaml（需求单）、interview.md（拷问过程）、materials.yaml（材料来源）
    checks/          需求单引用的判定脚本
    lenses/          镜头文件；rounds/rNNN.zmx 与 captures/NNN-unsaved.zmx 是工具写入的归档，lens save 不能写入
    notes/           model-NNN.json（lens describe 的说明输入，工具保存）与 agent 的说明文字
    models/          建模说明 NNN-<镜头>/：index.html、model.json、layout-*.png、approval.json（用户确认）
    scripts/         saos exec 的脚本副本和结果
    analyses/        lens analysis 的数值导出
    screens/         显性调试截图
    evaluations/     每次 check / round 的需求快照、support/（checks/ 副本）、分析导出、result.json（测量的权威记录）
    ledger.jsonl     调试轮次台账（指向 evaluations/NNN/result.json）；旧任务另有 rounds/rNNN.json
    report/          NNN-rMMM-<状态>.html（每次生成的封存副本）与 index.html（最新）
    explore/<主题>/  不经过 saos exec 的探索代码、中间镜头和日志，附 README.md
    spec/ eval/ plan/ rounds.jsonl bench.jsonl   显微物镜配方专用，只在用到时创建
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
| `SAOS_UI_LIVE` | 未设置 | extension 模式下设为 1 时，验收评估等操作期间也实时刷新窗口 |

## 测试

- `uv run pytest`：不启动 Zemax 的单元测试，以及用假后端启动真实后台子进程的生命周期测试。
- `SAOS_RUN_ZEMAX=1 uv run pytest -m zemax -s`：在本机 OpticStudio 上跑第 2 步的命令流程（`test_zemax_e2e.py`）和第 3 步的最小闭环（`test_zemax_loop.py`），共约 80 s。

## 显性调试命令

- `saos lens capture --job J --as backup.zmx`：将窗口当前内容另存到任务，不覆盖源文件或已有副本；可保留连接前尚未保存的修改。
- `saos lens view --job J [--viewer …] [--analysis TYPE]`：extension 模式下打开 Lens Data 或一种 System Viewer，加一个像质分析，并刷新；UpdateMode 设为 EditorsOnly，分析窗口由 saos 在改动后刷新。standalone 模式下返回 BAD_ARGS。
- `saos lens screenshot --job J [--label L]`：截取 OpticStudio 窗口 PNG。

extension 模式下，`lens load`、`lens unload`、`lens build` 换掉窗口内容前，若 OpticStudio 的 NeedsSave 置位（分析窗口的刷新也会置位，无法与用户修改区分），先把窗口内容存成 `lenses/captures/NNN-unsaved.zmx`，返回值 `captured` 给出路径，然后继续。脚本中的 `ctx.load` 和显微物镜配方的 accept、repro 同样先存副本，返回值带 `captured`。agent 自己未保存的修改（后台 dirty）仍返回 UNSAVED_CHANGES，先 `lens save` 或加 `--discard`；`--discard` 只放弃 agent 自己的修改，窗口内容照样先存副本。

光纤侧视框架的搭建脚本已移出命令行，见 `experiments/fiber/` 和 TODO。
