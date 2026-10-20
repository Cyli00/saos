# saos 命令行速览

详细参数、输入 JSON、返回字段、落盘文件和常见错误见 `uv run saos <命令> --help`；命令组再加子命令，结构库可到第三级。帮助用英文，供 agent 按需读取；本页面向人类读者。

每条操作向标准输出打印一个 JSON：成功为 `{"ok": true, "result": ...}`、退出码 0；失败为 `{"ok": false, "error": {"code", "message", "details"}}`、退出码 1；参数错误同格式、退出码 2。帮助本身输出文本。无穷值写成 `"inf"` / `"-inf"`，NaN 写成 null。

本地执行：后台控制、任务、需求确认、排除层级、停止、报告、扫描结果查询和配方的数据管理；镜头操作、优化、脚本、实测、轮次和扫描计算由持有 OpticStudio 连接的后台进程执行。完整进程模型和长任务规则见 `uv run saos --help`。

## 命令速查

| 分组 | 语法 | 作用 |
|---|---|---|
| 后台 | `saos daemon start [--mode standalone\|extension] [--start-timeout S]` | 启动独立实例或连接用户窗口。 |
| 后台 | `saos daemon status [--no-env]` / `saos daemon stop [--discard] [--force]` | 查看连接与许可证余量；正常或强制退出。 |
| 任务 | `saos job create --name N` / `saos job list` | 创建或列出任务。 |
| 任务 | `saos job show J [--log K]` / `saos job cancel J` / `saos job verify J` | 查看进度、请求取消或检查封存证据。 |
| 镜头 | `saos lens import --job J SRC [--as NAME] [--overwrite]` | 将外部镜头复制到任务。 |
| 镜头 | `saos lens build --job J JSON --as NAME [--overwrite] [--discard]` | 按序列（含混合模式）或纯非序列处方搭建并保存。 |
| 镜头 | `saos lens load --job J FILE [--discard]` / `saos lens unload --job J [--discard]` | 加载或卸载任务镜头。 |
| 镜头 | `saos lens save --job J [--as NAME] [--overwrite] [--no-verify]` | 保存，默认重载核对。 |
| 镜头 | `saos lens info --job J` / `saos lens surfaces --job J` | 查看系统设置及面或非序列物体。 |
| 镜头 | `saos lens set --job J --surface N --param P --value V` | 修改一个面参数。 |
| 镜头 | `saos lens operand --job J TYPE [--int1 N] [--int2 N] [--d1 V … --d6 V]` | 计算优化操作数。 |
| 镜头 | `saos lens analysis --job J [TYPE] [--list] [--set K=V …] [--keep-open]` | 列出分析或导出数值与文本。 |
| 模型 | `saos lens describe --job J [--notes JSON] [--lang zh\|en]` | 生成建模说明、布局及自动检查。 |
| 模型 | `saos lens approve --job J --model models/NNN-NAME --quote TEXT` | 记录用户对结构的确认。 |
| 窗口 | `saos lens capture --job J --as NAME` / `saos lens screenshot --job J [--label L]` | 保留窗口镜头副本或截取 PNG。 |
| 窗口 | `saos lens view --job J [--viewer cross-section\|3d\|shaded] [--analysis TYPE] [--set K=V …]` | 在 extension 模式下打开和刷新布局、分析。 |
| 需求 | `saos demand show --job J` / `saos demand confirm --job J --quote TEXT` | 校验需求单或记录当前版本的用户确认。 |
| 脚本 | `saos exec SCRIPT --job J [--arg K=V …] [--args-file JSON] [--name N] [--live] [--detach]` | 在后台执行 `run(ctx)` 并存档。 |
| 扫描 | `saos scan --job J --plan JSON [--workers N] [--detach]` / `saos scan --job J --show [NNN]` | 执行网格、点列、候选结构、多起点、扰动或公差计划；查询结果。 |
| 实测 | `saos check --job J [--only IDS] [--detach]` | 按需求单测量并留证据，不记录轮次。 |
| 轮次 | `saos round --job J [--tier T[,T…]] [--note TEXT] [--merit V] [--no-viz] [--detach]` | 归档镜头、实测并更新终止判断。 |
| 停止 | `saos rule-out --job J --tier T [--tier T…] --reason TEXT` / `saos stop --job J --reason TEXT --quote TEXT` | 带证据排除手段或记录用户停止决定。 |
| 报告 | `saos report --job J [--lang zh\|en]` | 生成交付报告，区分可交付镜头与诊断结果。 |
| 优化 | `saos opt vary --job J --param P --surfaces RANGE [--mode model\|substitute] [--catalog C] [--off]` | 设置面变量和玻璃替换目录。 |
| 优化 | `saos opt vary --job J --config-rows RANGE [--configs RANGE] [--off]` | 设置多重结构变量。 |
| 优化 | `saos opt vary --job J --objects RANGE --param P [--group S] [--off]` | 设置非序列物体的位置、倾斜或参数变量。 |
| 优化 | `saos opt merit --job J clear\|wizard\|add\|show [--file JSON]` | 编辑或查看评价函数。 |
| 优化 | `saos opt local --job J [--cycles N] [--algorithm dls\|od] [--max-seconds S] [--cores N] [--measure IDS] [--detach]` | 运行有预算的局部优化（阻尼最小二乘或正交下降）。 |
| 优化 | `saos opt hammer --job J --max-seconds S [--cores N] [--measure IDS] [--detach]` / `saos opt global --job J --max-seconds S [--cores N] [--measure IDS] [--detach]` | 运行限时 Hammer 或全局搜索。 |
| 流程图 | `saos ui open --job J [--no-window]` / `saos ui notify --job J --message TEXT` | 打开工作流窗口或在询问用户前显示提示。 |
| 配方 | `saos microscope adapt EXTRACT --out DIR [--embodiment ID] [--focal-length F] [--field-number N] [--tube-lens F] [--catalogs …] [--stop S]` | 将提取数据适配成处方和期望值。 |
| 配方 | `saos microscope repro --job J EXPECT` / `saos microscope accept --job J --spec FILE` | 做复现校验或规格验收。 |
| 配方 | `saos microscope perturb --job J --seed S --radius-rel A [--thickness-rel B]` | 对物镜施加可复现扰动。 |
| 结构库 | `saos microscope library check EXTRACT` / `saos microscope library add ID --extract FILE [--catalogs …] [--focal-length F] [--field-number N] [--stop S]` | 核对提取数据或适配入库。 |
| 结构库 | `saos microscope library verify ID --job J [--discard]` / `saos microscope library list` | 验证基准或列出条目。 |
| 结构库 | `saos microscope library query --na A --magnification M [--class C] [--field-number N] [--working-distance W] [--limit K]` | 按规格查询起始结构。 |
| 内部入口 | `saos daemon run [--mode standalone\|extension]` / `saos ui serve` | 前台运行后台进程或流程图服务，通常由启动命令调用。 |

镜头文件名相对任务的 `lenses/`，当前镜头操作的 `--job` 必须匹配。extension 替换未保存窗口内容前先另存 `lenses/captures/`；agent 自己的修改需保存或显式放弃。需求单格式见 [demand.md](../skills/saos/demand.md)，结构库格式见 [library.md](./recipes/microscope/library.md)。

长任务的 `--detach` 结果在任务 `runs/NNN-<op>.json`；扫描例外，状态在 `scans/NNN/`。用 `saos job show J` 或 `saos scan --job J --show` 跟进；超时只停止等待，不代表计算结束，不要直接重复提交。

## 环境变量

| 变量 | 默认值与作用 |
|---|---|
| `SAOS_INSTANCE` / `SAOS_HOME` | 实例默认为 `main`，产物在 `work/<实例>/`；后者可指定运行目录并覆盖前者。 |
| `SAOS_OPTICSTUDIO_DIR` / `SAOS_BACKEND` | 默认安装目录为 `C:\Program Files\Ansys Zemax OpticStudio 2024 R1.00`；后端默认 `zos`，工具测试可用 `fake`。 |
| `SAOS_LANG` | 报告语言依次取 `--lang`、需求单 `lang`、本变量、系统界面语言、英文；日志取本变量及系统语言。 |
| `SAOS_UI` / `SAOS_UI_LIVE` | 前者设 0 禁止自动打开流程图窗口；后者设 1 在长操作期间保持 extension 实时刷新。 |
| `SAOS_UI_PID` | 有多个 OpticStudio 窗口时选择要连接的进程。 |
| `SAOS_POOL_IDLE_S` / `SAOS_LIBRARY` | worker 空闲释放默认 600 秒；结构库默认 `<仓库>/library/microscope`。 |

## 错误码

| 错误码 | 处理 |
|---|---|
| `BAD_ARGS` / `FILE_NOT_FOUND` / `FILE_EXISTS` / `PATH_OUTSIDE_JOB` / `JOB_NOT_FOUND` / `JOB_EXISTS` | 核对当前命令帮助、输入文件、目标路径和实例。 |
| `DAEMON_NOT_RUNNING` / `DAEMON_ALREADY_RUNNING` / `DAEMON_START_FAILED` | 核对后台状态；启动错误附 `details.log_tail`。 |
| `DAEMON_TIMEOUT` / `BUSY` / `POOL_BUSY` | 先查进度和结果；等待或请求取消，避免重复计算。 |
| `ZOS_LICENSE` / `ZOS_VERSION_MISMATCH` / `ZOS_CONNECTION_LOST` | 检查许可或版本；连接丢失后后台退出，需要重启，未保存修改会丢失。 |
| `ZOS_ERROR` / `ZOS_LOAD_FAILED` / `ZOS_SAVE_FAILED` / `VERIFY_FAILED` / `UI_BLOCKED` | 查看细节；核对文件、回读差异或关闭被列出的模态对话框。 |
| `NO_LENS_LOADED` / `JOB_MISMATCH` / `UNSAVED_CHANGES` / `VALUE_NOT_APPLIED` | 加载正确任务镜头、保存修改，或检查覆盖参数的求解。 |
| `DEMAND_NOT_CONFIRMED` / `DEMAND_BLOCKED` / `MODEL_NOT_APPROVED` | 处理待确认问题，并取得对应版本或结构的用户确认。 |
| `SCRIPT_ERROR` / `LENS_MODIFIED_BY_CHECK` / `DUPLICATE_ROUND` | 查看 traceback、保持判定只读，或读取已记录的重复轮次。 |
| `SPEC_CHANGED` / `EVIDENCE_TAMPERED` / `CANCELLED` | 重新实测变更后的判定、检查封存文件，或读取取消前已完成的结果。 |
| `PROTOCOL_ERROR` / `UNKNOWN_OP` / `INTERNAL_ERROR` | 查看错误细节和后台日志，核对客户端与后台版本。 |
