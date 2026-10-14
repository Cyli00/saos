# saos 命令行

agent 只通过 `uv run saos ...` 操作 Zemax。每条命令向标准输出打印一个 JSON 对象：

- 成功：`{"ok": true, "result": ...}`，退出码 0
- 失败：`{"ok": false, "error": {"code", "message", "details"}}`，退出码 1；命令行参数错误时退出码为 2，格式相同

`daemon`、`job` 命令在命令行进程内执行；`lens`、`opt`、`exec` 命令转发给常驻后台进程，由它持有 OpticStudio 连接（ADR-0002）。`microscope` 下是显微物镜配方的命令，其中复现、验收、扰动、轮次通过 `exec` 在后台执行。

## 命令

需求访谈和只读设计计划新增 `microscope interview check`、`microscope plan create/show/confirm/handoff`，均不连接 Zemax。访谈记录格式、参数及用户确认流程见 [interview-and-plan.md](./interview-and-plan.md)。

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
| `saos lens set --job J --surface N --param P --value V` | 修改一个面参数：radius、thickness、conic、semi_diameter、material、comment。数值可写 `inf` |
| `saos lens save --job J [--as NAME] [--overwrite] [--no-verify]` | 保存；默认保存后从磁盘重新加载，与保存前逐面核对 |
| `saos lens operand --job J TYPE [--int1 --int2 --d1 … --d6]` | 计算一个优化操作数，例如 `EFFL --int2 2` |
| `saos lens analysis --job J TYPE [--set KEY=VALUE …] [--keep-open]` / `--list` | 运行一个 OpticStudio 分析（AnalysisIDM 名称，如 StandardSpot、FftMtf、RayFan、WavefrontMap、FieldCurvatureAndDistortion），导出数据序列、网格（超过 256×256 时降采样）、点列图 RMS/GEO 半径和表头，存为任务的 `analyses/NNN-TYPE.json`，并保存 OpticStudio 的文本全文 `.txt`。设置名取自分析的设置对象，Field、Wavelength、Surface 用编号或 `all`；枚举按成员名。返回值省略网格本体，只给统计量 |
| `saos lens view --job J [--viewer lde\|cross-section\|3d\|shaded] [--analysis TYPE] [--set KEY=VALUE …]` | 显性调试：打开 Lens Data 或一种 System Viewer，加一个像质分析（默认 StandardSpot，按系统最看重的像质选），按当前镜头刷新。选择会被记住；之后 load、set、save、build、capture、opt local、exec 结束时自动刷新已打开的分析窗口。连接 extension 后自动按默认选择打开 |
| `saos lens screenshot --job J [--label L]` | 显性调试：把 OpticStudio 的可见窗口截成 PNG，存为任务的 `screens/NNN-L-K.png`（PrintWindow，被遮挡也能截；最小化的窗口跳过） |
| `saos check --job J` | 按任务的需求框定 `sources/harness.yaml` 对当前镜头逐项实测和判定，不记录。需求框定未确认时也能用，便于框定阶段先看数值 |
| `saos round --job J [--tier local\|variables\|materials\|global\|structure] [--note TEXT]` | 记录一个调试轮次：存为 `lenses/rounds/rNNN.zmx`，逐项实测判定（全文存为 `rounds/rNNN.json`），写入 `ledger.jsonl`，返回终止判断（continue / achieved / exhausted / limit）及还没试过的层级。第 0 轮不填 tier。需求框定未确认、改动后没有新的确认原话、或已经终止时拒绝 |
| `saos report --job J` | 由需求框定和 ledger 生成交付报告 `report/index.html`，不连接 OpticStudio |
| `saos exec SCRIPT --job J [--arg K=V …] [--args-file JSON] [--name N] [--quiet]` | 在后台进程内执行脚本（ADR-0007），见下文「脚本执行」 |
| `saos microscope adapt EXTRACT --focal-length F --field-number N [--tube-lens 200] [--catalogs …] [--stop rear_focal\|r<面号>] --out DIR` | 把子 agent 提取的面数据适配成标准设计布局的处方，输出 prescription.json、expect.json、adapt_report.json（不经后台进程） |
| `saos lens build --job J PRESCRIPTION --as NAME [--overwrite]` | 按处方用 ZOS-API 搭建镜头，保存到 lenses/ 并加载 |
| `saos microscope repro --job J EXPECT` | 复现校验：焦距、物方 NA、顶点工作距离、f_TL/EFL 与期望值比较。每项允许的偏差取相对误差 1% 与 EXPECT 中 `tolerance_mm`（原文印刷精度的舍入包络半宽）的较大者。只临时调焦，结束后恢复 |
| `saos microscope perturb --job J --seed S --radius-rel A [--thickness-rel B]` | 受控扰动：物镜各面曲率、厚度按固定随机种子均匀改变 |
| `saos microscope accept --job J [--spec FILE]` | 按光学规格做验收评估，结果和验收模型存到任务的 eval/。结果的 `focus_trace` 列出调焦的每个试探位置（波长、厚度、轴上光线失败数、c4、拟合残差） |
| `saos opt vary --job J --param radius\|thickness\|conic --surfaces 1-16 [--off]` | 设变量或取消变量。物面只允许厚度（即无盖玻片时的工作距离间隔） |
| `saos opt merit --job J clear\|wizard\|add\|show [--file JSON]` | 评价函数：清空；按向导生成默认评价函数（type、data、reference、rings、arms 等）；追加操作数列表；查看 |
| `saos opt local --job J [--cycles auto\|1\|5\|10\|50]` | 局部优化（阻尼最小二乘） |
| `saos microscope round --job J [--spec FILE] [--note TEXT]` | 记录一个调整轮次：存为 lenses/rounds/rNNN.zmx，做验收评估，返回停机判断（pass / stagnated / limit / continue）。第 0 轮时冻结光学规格，之后不能更换。后台协议另有 `observe` 参数，供基准实验使用：照常冻结和核对，停机判断只记录不执行 |
| `saos microscope library check EXTRACT` | 入库前用近轴计算核对提取结果：焦距和工作距离的印刷值落在印刷精度的舍入包络内，或与计算值相差不超过 1%，即为一致（不经后台进程） |
| `saos microscope library add ID --extract FILE [--catalogs …] [--focal-length F] [--field-number N] [--stop rear_focal\|printed\|r<面号>]` | 把提取结果适配成处方，写入 `library/entries/ID/`，状态为 extracted（不经后台进程） |
| `saos microscope library verify ID --job J [--discard]` | 搭建、复现校验、探测验收、推导基准规格并确认原结构 G = 0，结果写回条目。当前镜头有未保存修改时返回 UNSAVED_CHANGES；显式加 `--discard` 才会丢弃，包括其他任务的修改。复现不通过的条目状态为 rejected |
| `saos microscope library list` | 列出条目：状态、焦距、NA、片数、能否作基准及原因 |
| `saos microscope library query --na A --magnification M [--class achromat\|plan_achromat] [--field-number N] [--working-distance W] [--limit K]` | 按光学规格从已收录条目中选初始结构 |
| `uv run python experiments/bench/run.py split` | 把可作基准的条目分为调参组和验证组，写入 `library/benchmarks.json` |
| `uv run python experiments/bench/run.py run ENTRY [--seed S] [--radius-rel A] [--thickness-rel B] [--no-amplify] [--stretch wd=F length=F] [--max-rounds N] [--cores K] [--job J] [--discard]` | 开发用基准任务：搭建条目、受控扰动（默认幅度按 ×1、×2、×4、×8 放大到 G0 ≥ 1）或拉伸规格，按参考策略逐轮优化，每轮用 observe 模式记录，不按停滞规则中途停机；轨迹写入任务的 `bench.jsonl`。`--job` 续跑；后台进程里有其他任务的未保存镜头时默认拒绝，`--discard` 才放弃 |
| `uv run python experiments/bench/run.py plan --batch B` | 生成正式批次的实验清单 `library/calibration/B.json`，记录任务、分组、策略版本和代码哈希；已存在时拒绝覆盖 |
| `uv run python experiments/bench/run.py run --batch B --task T [--cores K]` | 按清单运行正式任务；客户端或后台进程的代码版本与清单不同、或后台进程不是 standalone 时拒绝。本 SAOS_HOME 中已有该任务时续跑 |
| `uv run python experiments/bench/run.py calibrate --batch B --homes DIR… [--validate N REL CAP]` | 按清单准入正式轨迹并列出排除原因、重复起点，在调参组主实验上按冻结规则选停机参数；给出 `--validate` 时在验证组主实验和两组压力测试上检验固定参数 |

结构库的收录规则、提取格式和基准规格的推导见 [library.md](./library.md)。

`--job` 必须与当前加载的镜头属于同一任务；镜头文件名一律相对该任务的 `lenses/`。

## 需求框定

`sources/harness.yaml`，字段用英文：

```yaml
schema: 1
request: 用户原话
targets:
  - id: efl
    kind: constraint            # constraint | target | preference
    description: effective focal length
    check: {operand: {type: EFFL, int1: 0, int2: 0, data: []}}
    goal: {equals: 50.0, tol: 0.5}   # 或 {max: v}、{min: v}，可同时给 min 和 max
    scale: 0.5                  # 单项差距的尺度；默认取 tol 或界限的绝对值，界限为 0 时必须给出
  - id: spot_axis
    kind: target
    check: {analysis: {type: StandardSpot, settings: {}, path: "spot.rms_radius[0][1]"}}
    goal: {max: 10.0}
  - id: custom
    kind: preference            # 期望项只报告数值，可以没有 goal
    check: {script: checks/custom.py}   # 任务内的脚本，run(ctx) 返回一个数
assumptions: [agent 补全的条件]
recipes: []
stop: {rel: 0.01, max_rounds: 30, tiers: [local, variables, materials, global, structure]}
confirmed: {quote: 用户确认的原话}
```

`analysis` 的 `path` 指向 `saos lens analysis` 导出结构中的一个数，例如 `series[0].y.Tangential[20]`、`grids[0].stats.max`；键里有点号时写成 `['0.48']`。终止规则见 DESIGN.md「终止规则」。

## 脚本执行

脚本定义 `run(ctx)`，返回可 JSON 化的结果。`ctx` 提供：

- `ctx.system`：OpticStudio 的 PrimarySystem；`ctx.zosapi`：ZOSAPI 命名空间；`ctx.backend`：saos 后端；
- `ctx.job`、`ctx.args`（`--arg` 和 `--args-file` 合并后的字典）、`ctx.lens`（当前加载的镜头路径，可能为 None）、`ctx.dirty`；
- `ctx.load(name)`、`ctx.save_as(name, overwrite=False)`：切换或保存镜头时必须用它们，后台进程才能跟踪当前文件；`ctx.mark_dirty()`；`ctx.path(rel)`：任务目录内的路径。

每次执行都把脚本副本和结果存为任务的 `scripts/NNN-<名>.py` 与 `.json`，交付的数值可以追溯到具体脚本。脚本抛出异常时返回 `SCRIPT_ERROR`（含 traceback），后台进程继续运行；连接已中断时按连接中断处理。执行结束后若 OpticStudio 的 NeedsSave 置位，当前镜头记为有未保存修改；只读的 Zernike 拟合也会置位，所以这一判断偏保守。`--quiet` 在 extension 模式下暂停窗口刷新，显微物镜的验收类命令默认带上。

## 后台窗口与执行记录

`daemon start` 使用 Windows 无窗口方式启动后台 Python，标准输出和错误输出写入 `<SAOS_HOME>/daemon/daemon.log`。旧启动方式使用 `DETACHED_PROCESS`，虚拟环境启动器的解释器子进程仍可能弹出空白终端；改为 `CREATE_NO_WINDOW` 后，新启动的后台进程不再创建该控制台窗口。已运行的进程需等任务结束后正常停止，再启动才会采用新方式。

在 Git Bash 中，用 `SAOS_INSTANCE=verify uv run saos daemon status` 查询指定实例的连接状态和已加载任务。执行记录位于 `<SAOS_HOME>/jobs/<任务编号>/log.jsonl`，包含操作名、成功与否及耗时；可用 `tail -f` 跟随查看。每条记录在操作完成后写入，因此长时间计算中日志没有新增记录不代表进程已卡死。后台串行处理请求，忙碌时 `daemon status` 也可能等待并超时；目前没有独立的实时进度接口。

## 连接手动打开的窗口（extension 模式）

调试时可以让后台进程连接一个手动打开的 OpticStudio 窗口，在窗口里看到镜头的变化：打开 OpticStudio 后执行 `saos daemon start --mode extension`。启动流程通过 Windows UI Automation 选择 Programming、打开 Interactive Extension，将 Auto Close on Disconnect 设为 Off，核对等待连接状态并读取实际实例编号。已有等待对话框会直接复用；窗口已连接到其他扩展时拒绝接管。存在多个窗口时用 `SAOS_UI_PID` 指定进程编号，否则拒绝猜选。控件不可用或窗口被模态对话框阻塞时返回明确错误。

- 之前实测后台进程退出后扩展对话框关闭，需要重新开启；用户随后在本机确认，取消勾选 “Auto close on disconnect” 即可避免该问题。重连前检查对话框仍处于等待连接状态，若已关闭则重新开启。没有开启交互扩展时启动返回 `ZOS_ERROR`。该设置不代替 OpticStudio 进程意外退出后的连接恢复处理。
- 窗口实例与后台实例的验收结果在数值上相差约 10⁻⁵（相对），拟合残差贴近界限时结论可能翻转（docs/step5-calibration.md 3.2 节）。正式实验只用 standalone 实例。
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
  jobs/<job_id>/
    job.json         任务状态：status、current_lens、last_saved 等
    spec/            光学规格文件
    sources/         输入材料：用户需求访谈（interview.md、interview.yaml）、提取结果、处方、复现期望值、评价函数设置等
    notes/           agent 对该任务的设计记录（框架搭建、检查结论等）
    lenses/          镜头文件副本，所有读写都限制在这里；lenses/rounds/ 存各调整轮次
    eval/            验收评估结果（<镜头名>.accept.json）和验收模型（.accept.zmx）
    rounds.jsonl     调整轮次记录：轮次、规格差距、各项单项差距、评价函数值、说明
    bench.jsonl      优化基准任务的完整轨迹：每轮的策略、规格差距、单项差距、耗时
    log.jsonl        执行记录：每条命令的参数、结果、错误码、耗时
```

## 环境变量

| 变量 | 默认值 | 用途 |
|---|---|---|
| `SAOS_INSTANCE` | `main` | 实例名，运行目录为 `<仓库>/work/<实例名>`；只允许字母、数字、`-`、`_` |
| `SAOS_HOME` | 未设置 | 直接指定运行目录，优先于 `SAOS_INSTANCE`；任务目录、后台进程状态和命名管道名都由最终目录推出 |
| `SAOS_OPTICSTUDIO_DIR` | `C:\Program Files\Ansys Zemax OpticStudio 2024 R1.00` | 加载 ZOS-API DLL 的目录 |
| `SAOS_BACKEND` | `zos` | 测试时设为 `fake`，不启动 Zemax |
| `SAOS_LIBRARY` | `<仓库>/library` | 初始结构库的位置 |
| `SAOS_UI_LIVE` | 未设置 | extension 模式下设为 1 时，验收评估等操作期间也实时刷新窗口 |

## 测试

- `uv run pytest`：不启动 Zemax 的单元测试，以及用假后端启动真实后台子进程的生命周期测试。
- `SAOS_RUN_ZEMAX=1 uv run pytest -m zemax -s`：在本机 OpticStudio 上跑第 2 步的命令流程（`test_zemax_e2e.py`）和第 3 步的最小闭环（`test_zemax_loop.py`），共约 80 s。

## 显性调试命令

- `saos lens capture --job J --as backup.zmx`：将窗口当前内容另存到任务，不覆盖源文件或已有副本；可保留连接前尚未保存的修改。
- `saos lens view --job J [--viewer …] [--analysis TYPE]`：extension 模式下打开 Lens Data 或一种 System Viewer，加一个像质分析，并刷新；UpdateMode 设为 EditorsOnly，分析窗口由 saos 在改动后刷新。standalone 模式下返回 BAD_ARGS。
- `saos lens screenshot --job J [--label L]`：截取 OpticStudio 窗口 PNG。

extension 模式下，窗口有 saos 之外的未保存修改（OpticStudio 的 NeedsSave）时禁止换镜头。实测（2024 R1）临时增删评价函数行的 Zernike 拟合、把面参数写回相同的值都会让 NeedsSave 置位，所以 saos 自己的修改仍以后台进程记录的 dirty 为准；复现校验结束时对未修改的镜头直接重新加载文件，不留下 NeedsSave。分析窗口刷新是否会置位尚未实测；遇到误报时，确认要保存当前任务内容后用 `lens save --no-verify` 保存再切换。

光纤侧视框架的搭建脚本已移出命令行，见 `experiments/fiber/` 和 TODO。
