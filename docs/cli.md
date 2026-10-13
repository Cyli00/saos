# zea 命令行

agent 只通过 `uv run zea ...` 操作 Zemax。每条命令向标准输出打印一个 JSON 对象：

- 成功：`{"ok": true, "result": ...}`，退出码 0
- 失败：`{"ok": false, "error": {"code", "message", "details"}}`，退出码 1；命令行参数错误时退出码为 2，格式相同

`daemon`、`job` 命令在命令行进程内执行；`lens` 命令转发给常驻后台进程，由它持有 OpticStudio 连接（ADR-0002）。

## 命令

| 命令 | 作用 |
|---|---|
| `zea daemon start [--mode standalone\|extension]` | 启动后台进程并连接 OpticStudio 2024 R1，就绪后返回。本机实测约 6 s。`extension` 连接手动打开、已点了 Interactive Extension 的窗口，见下文 |
| `zea daemon status` | 是否在运行、连接是否正常、当前加载的镜头 |
| `zea daemon stop [--discard] [--force]` | 正常退出，等后台进程和它的 OpticStudio 都退出后才返回。有未保存修改时拒绝退出，除非加 `--discard`；`--force` 在无法正常退出时强制结束 |
| `zea job create --name N [--spec FILE]` | 新建设计任务，编号为 `时间-名字`，规格文件复制进任务目录 |
| `zea job list` / `zea job show JOB [--log K]` | 列出任务；查看状态、镜头文件和最近 K 条执行记录 |
| `zea lens import --job J SRC [--as NAME] [--overwrite]` | 把外部 .zmx 复制进任务的 `lenses/`，原文件不动 |
| `zea lens load --job J FILE [--discard]` | 加载任务中的镜头文件 |
| `zea lens unload --job J [--discard]` | 卸载当前镜头 |
| `zea lens info --job J` | 系统数据：孔径、视场、波长、玻璃目录、面数、光阑面 |
| `zea lens surfaces --job J` | 各面的类型、半径、厚度、材料、半口径、二次曲面系数、求解类型 |
| `zea lens set --job J --surface N --param P --value V` | 修改一个面参数：radius、thickness、conic、semi_diameter、material、comment。数值可写 `inf` |
| `zea lens save --job J [--as NAME] [--overwrite] [--no-verify]` | 保存；默认保存后从磁盘重新加载，与保存前逐面核对 |
| `zea lens operand --job J TYPE [--int1 --int2 --d1 … --d6]` | 计算一个优化操作数，例如 `EFFL --int2 2` |
| `zea adapt patent EXTRACT --focal-length F --field-number N [--tube-lens 200] [--catalogs …] [--stop rear_focal\|r<面号>] --out DIR` | 把子 agent 提取的面数据适配成标准设计布局的处方，输出 prescription.json、expect.json、adapt_report.json（不经后台进程） |
| `zea lens build --job J PRESCRIPTION --as NAME [--overwrite]` | 按处方用 ZOS-API 搭建镜头，保存到 lenses/ 并加载 |
| `zea lens repro --job J EXPECT` | 复现校验：焦距、物方 NA、顶点工作距离、f_TL/EFL 与期望值比较。每项允许的偏差取相对误差 1% 与 EXPECT 中 `tolerance_mm`（原文印刷精度的舍入包络半宽）的较大者。只临时调焦，结束后恢复 |
| `zea lens perturb --job J --seed S --radius-rel A [--thickness-rel B]` | 受控扰动：物镜各面曲率、厚度按固定随机种子均匀改变 |
| `zea eval accept --job J [--spec FILE] [--no-precheck]` | 按光学规格做验收评估，结果和验收模型存到任务的 eval/。`--no-precheck` 仅供诊断：关闭调焦前的轴上光线预检，结果另存为 `.accept.noprecheck.json` |
| `zea eval precheck --job J [--spec FILE]` | 诊断：只搭验收模型并做调焦前的轴上光线预检，不调焦、不测量 |
| `zea opt vary --job J --param radius\|thickness\|conic --surfaces 1-16 [--off]` | 设变量或取消变量。物面只允许厚度（即无盖玻片时的工作距离间隔） |
| `zea opt merit --job J clear\|wizard\|add\|show [--file JSON]` | 评价函数：清空；按向导生成默认评价函数（type、data、reference、rings、arms 等）；追加操作数列表；查看 |
| `zea opt local --job J [--cycles auto\|1\|5\|10\|50]` | 局部优化（阻尼最小二乘） |
| `zea round record --job J [--spec FILE] [--note TEXT]` | 记录一个调整轮次：存为 lenses/rounds/rNNN.zmx，做验收评估，返回停机判断（pass / stagnated / limit / continue）。第 0 轮时冻结光学规格，之后不能更换。后台协议另有 `observe` 参数，供基准实验使用：照常冻结和核对，停机判断只记录不执行 |
| `zea library check EXTRACT` | 入库前用近轴计算核对提取结果：焦距和工作距离的印刷值落在印刷精度的舍入包络内，或与计算值相差不超过 1%，即为一致（不经后台进程） |
| `zea library add ID --extract FILE [--catalogs …] [--focal-length F] [--field-number N] [--stop rear_focal\|printed\|r<面号>]` | 把提取结果适配成处方，写入 `library/entries/ID/`，状态为 extracted（不经后台进程） |
| `zea library verify ID --job J [--discard]` | 搭建、复现校验、探测验收、推导基准规格并确认原结构 G = 0，结果写回条目。当前镜头有未保存修改时返回 UNSAVED_CHANGES；显式加 `--discard` 才会丢弃，包括其他任务的修改。复现不通过的条目状态为 rejected |
| `zea library list` | 列出条目：状态、焦距、NA、片数、能否作基准及原因 |
| `zea library query --na A --magnification M [--class achromat\|plan_achromat] [--field-number N] [--working-distance W] [--limit K]` | 按光学规格从已收录条目中选初始结构 |
| `zea bench split` | 把可作基准的条目分为调参组和验证组，写入 `library/benchmarks.json` |
| `zea bench run ENTRY [--seed S] [--radius-rel A] [--thickness-rel B] [--no-amplify] [--stretch wd=F length=F] [--max-rounds N] [--cores K] [--job J] [--discard]` | 开发用基准任务：搭建条目、受控扰动（默认幅度按 ×1、×2、×4、×8 放大到 G0 ≥ 1）或拉伸规格，按参考策略逐轮优化，每轮用 observe 模式记录，不按停滞规则中途停机；轨迹写入任务的 `bench.jsonl`。`--job` 续跑；后台进程里有其他任务的未保存镜头时默认拒绝，`--discard` 才放弃 |
| `zea bench plan --batch B` | 生成正式批次的实验清单 `library/calibration/B.json`，记录任务、分组、策略版本和代码哈希；已存在时拒绝覆盖 |
| `zea bench run --batch B --task T [--cores K]` | 按清单运行正式任务；客户端或后台进程的代码版本与清单不同、或后台进程不是 standalone 时拒绝。本 ZEA_HOME 中已有该任务时续跑 |
| `zea bench calibrate --batch B --homes DIR… [--validate N REL CAP]` | 按清单准入正式轨迹并列出排除原因、重复起点，在调参组主实验上按冻结规则选停机参数；给出 `--validate` 时在验证组主实验和两组压力测试上检验固定参数 |
| `zea bench replay [--homes DIR…] [--policy P]` | 开发用：在已有轨迹上离线比较停机参数，不做正式准入 |

结构库的收录规则、提取格式和基准规格的推导见 [library.md](./library.md)。

`--job` 必须与当前加载的镜头属于同一任务；镜头文件名一律相对该任务的 `lenses/`。

## 后台窗口与执行记录

`daemon start` 使用 Windows 无窗口方式启动后台 Python，标准输出和错误输出写入 `<ZEA_HOME>/daemon/daemon.log`。旧启动方式使用 `DETACHED_PROCESS`，虚拟环境启动器的解释器子进程仍可能弹出空白终端；改为 `CREATE_NO_WINDOW` 后，新启动的后台进程不再创建该控制台窗口。已运行的进程需等任务结束后正常停止，再启动才会采用新方式。

在 Git Bash 中，用 `ZEA_HOME=work-verify uv run zea daemon status` 查询指定实例的连接状态和已加载任务。执行记录位于 `<ZEA_HOME>/jobs/<任务编号>/log.jsonl`，包含操作名、成功与否及耗时；可用 `tail -f` 跟随查看。每条记录在操作完成后写入，因此长时间计算中日志没有新增记录不代表进程已卡死。后台串行处理请求，忙碌时 `daemon status` 也可能等待并超时；目前没有独立的实时进度接口。

## 连接手动打开的窗口（extension 模式）

调试时可以让后台进程连接一个手动打开的 OpticStudio 窗口，在窗口里看到镜头的变化：先在 OpticStudio 中点 Programming → Interactive Extension，检查并取消勾选对话框中的 “Auto close on disconnect”，保持对话框打开，再 `zea daemon start --mode extension`。这是开启可见调试前的检查项；目前需要在界面确认，命令尚未自动检查该选项。

- 之前实测后台进程退出后扩展对话框关闭，需要重新开启；用户随后在本机确认，取消勾选 “Auto close on disconnect” 即可避免该问题。重连前检查对话框仍处于等待连接状态，若已关闭则重新开启。没有开启交互扩展时启动返回 `ZOS_ERROR`。该设置不代替 OpticStudio 进程意外退出后的连接恢复处理。
- 窗口实时刷新会拖慢运算：实测一次验收评估 98 s，standalone 下为 13 s。所以验收评估、轮次记录、复现校验期间暂停刷新，结束后窗口显示最终状态（暂停后验收评估为 22 s）。局部优化不暂停，实测 15.8 s，standalone 下为 13 s。设环境变量 `ZEA_UI_LIVE=1` 后全程刷新。
- 后台进程退出时不关闭窗口。

## 错误码

| 错误码 | 含义 |
|---|---|
| `DAEMON_NOT_RUNNING` | 后台进程没有运行，先 `daemon start` |
| `DAEMON_ALREADY_RUNNING` | 已有后台进程（同一 `ZEA_HOME` 只允许一个） |
| `DAEMON_START_FAILED` / `DAEMON_TIMEOUT` | 启动失败或超时，`details.log_tail` 附后台日志 |
| `ZOS_LICENSE` / `ZOS_VERSION_MISMATCH` | 许可证不支持 ZOS-API；OpticStudio 不是 24.1.0 |
| `ZOS_CONNECTION_LOST` | OpticStudio 进程已退出。后台进程随即退出，未保存的修改丢失，需要重新 `daemon start` |
| `ZOS_ERROR` | OpticStudio 操作抛出异常，连接仍正常，后台进程继续运行 |
| `ZOS_LOAD_FAILED` / `ZOS_SAVE_FAILED` | OpticStudio 无法加载或保存文件 |
| `VERIFY_FAILED` | 保存后重新加载与保存前不一致，`details.diffs` 列出差异 |
| `SPEC_CHANGED` | 调整轮次中，规格路径或补全后的验收配置与第 0 轮冻结的不一致；需要新建任务 |
| `VALUE_NOT_APPLIED` | 修改没有生效（例如被求解覆盖），`details` 给出目标值、实际值和求解类型 |
| `NO_LENS_LOADED` / `JOB_MISMATCH` | 没有加载镜头；加载的是另一个任务的镜头 |
| `UNSAVED_CHANGES` | 当前镜头有未保存修改，不能加载其他镜头、卸载或退出 |
| `PATH_OUTSIDE_JOB` | 文件路径越出本任务的 `lenses/` |
| `JOB_NOT_FOUND` / `JOB_EXISTS` / `FILE_NOT_FOUND` / `FILE_EXISTS` | 任务或文件不存在、已存在 |
| `BAD_ARGS` / `UNKNOWN_OP` | 参数错误，包括不在当前玻璃目录中的材料名、未知的操作数 |

验收评估无法完成时（检查光线失败或被截断、调焦不收敛、Zernike 拟合残差超限），命令本身成功返回，结果中 `verdict` 为 `invalid`、`spec_shortfall` 为 `inf`，`invalid` 字段说明原因。

## 目录

默认在仓库下的 `work/`（已加入 .gitignore），可用环境变量 `ZEA_HOME` 改到别处。

```
work/
  daemon/            state.json、authkey、daemon.log（后台进程运行时存在）
  jobs/<job_id>/
    job.json         任务状态：status、current_lens、last_saved 等
    spec/            光学规格文件
    sources/         提取结果、处方、复现期望值、评价函数设置等输入材料
    lenses/          镜头文件副本，所有读写都限制在这里；lenses/rounds/ 存各调整轮次
    eval/            验收评估结果（<镜头名>.accept.json）和验收模型（.accept.zmx）
    rounds.jsonl     调整轮次记录：轮次、规格差距、各项单项差距、评价函数值、说明
    bench.jsonl      优化基准任务的完整轨迹：每轮的策略、规格差距、单项差距、耗时
    log.jsonl        执行记录：每条命令的参数、结果、错误码、耗时
```

## 环境变量

| 变量 | 默认值 | 用途 |
|---|---|---|
| `ZEA_HOME` | `<仓库>/work` | 任务目录和后台进程状态的位置；命名管道名也由它推出 |
| `ZEA_OPTICSTUDIO_DIR` | `C:\Program Files\Ansys Zemax OpticStudio 2024 R1.00` | 加载 ZOS-API DLL 的目录 |
| `ZEA_BACKEND` | `zos` | 测试时设为 `fake`，不启动 Zemax |
| `ZEA_LIBRARY` | `<仓库>/library` | 初始结构库的位置 |
| `ZEA_UI_LIVE` | 未设置 | extension 模式下设为 1 时，验收评估等操作期间也实时刷新窗口 |

## 测试

- `uv run pytest`：不启动 Zemax 的单元测试，以及用假后端启动真实后台子进程的生命周期测试。
- `ZEA_RUN_ZEMAX=1 uv run pytest -m zemax -s`：在本机 OpticStudio 上跑第 2 步的命令流程（`test_zemax_e2e.py`）和第 3 步的最小闭环（`test_zemax_loop.py`），共约 80 s。
