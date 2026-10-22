# saos 命令行速览

详细参数、输入 JSON、返回字段、落盘文件和常见错误见 `uv run saos <命令> --help`；命令组再加子命令，结构库可到第三级。帮助用英文，供 agent 按需读取；本页面向人类读者。

每条操作向标准输出打印一个 JSON：成功为 `{"ok": true, "result": ...}`、退出码 0；失败为 `{"ok": false, "error": {"code", "message", "details"}}`、退出码 1；参数错误同格式、退出码 2。帮助本身输出文本。无穷值写成 `"inf"` / `"-inf"`，NaN 写成 null。

本地执行：后台控制、任务、需求确认、排除层级、停止、报告、扫描结果查询和配方的数据管理；镜头操作、优化、脚本、实测、轮次和扫描计算由持有 OpticStudio 连接的后台进程执行。完整进程模型和长任务规则见 `uv run saos --help`。

## 命令速查

| 分组 | 语法 | 作用 |
|---|---|---|
| 后台 | `saos daemon start [--mode standalone\|extension] [--start-timeout S]` | Optiland 在进程内启动、不占许可证；认可报告（`zos begin`）之前不启动 OpticStudio。`--mode extension` 决定认可后 OpticStudio 会话是否连接用户窗口。 |
| 后台 | `saos daemon status [--no-env]` / `saos daemon stop [--discard] [--force]` | 查看连接与许可证余量；正常或强制退出。 |
| 任务 | `saos job create --name N` / `saos job list` | 创建或列出任务。 |
| 任务 | `saos job show J [--log K]` / `saos job cancel J` / `saos job verify J` | 查看进度、请求取消或检查封存证据。 |
| 镜头 | `saos lens import --job J SRC [--as NAME] [--overwrite]` | 将外部镜头复制到任务。 |
| 镜头 | `saos lens build --job J JSON --as NAME [--overwrite] [--discard]` | 按序列（含混合模式）或纯非序列处方搭建并保存。 |
| 镜头 | `saos lens load --job J FILE [--discard]` / `saos lens unload --job J [--discard]` | 加载或卸载任务镜头。 |
| 镜头 | `saos lens save --job J [--as NAME] [--overwrite] [--no-verify]` | 保存原生快照（Optiland）或 .zmx（OpticStudio），默认重载核对；不自动导出 .zmx。 |
| 镜头 | `saos lens export --job J --as NAME [--overwrite]` | 把 Optiland 镜头显式导出为与快照成对的 .zmx；表达不了时明确报缺口。 |
| 镜头 | `saos lens info --job J` / `saos lens surfaces --job J` | 查看系统设置及面或非序列物体。 |
| 镜头 | `saos lens set --job J --surface N --param P --value V` | 修改一个面参数。 |
| 镜头 | `saos lens operand --job J TYPE [--int1 N] [--int2 N] [--d1 V … --d6 V]` | 计算优化操作数。 |
| 镜头 | `saos lens analysis --job J [TYPE] [--list] [--set K=V …] [--keep-open]` | 列出分析或导出数值与文本。 |
| 模型 | `saos lens describe --job J [--notes JSON] [--lang zh\|en]` | 生成选型依据、需求逐项、计算有效性、下一阶段预算四节及自动检查；notes.next_budget 格式为 {max_seconds, plan}，缺失警告、格式错误拒绝。需求状态区分满足、未满足、未知、无界限；没有材料记录时资料状态为未知。认可前只用 Optiland。 |
| 模型 | `saos lens approve --job J --model models/NNN-NAME --quote TEXT [--rebuild-reason TEXT]` | 核对 model.json 封存及 shown_sha 后记录原话与未知项；确认方向和取舍，不替系统担保计算有效性。普通调参、判定方法和比较记录更新不撤销模型确认；环境面声明属于结构指纹；结构指纹与上次确认不同须填写重建原因。 |
| 窗口 | `saos lens capture --job J --as NAME` / `saos lens screenshot --job J [--label L]` | 保留窗口镜头副本或截取 PNG。 |
| 窗口 | `saos lens view --job J [--viewer cross-section\|3d\|shaded] [--analysis TYPE] [--set K=V …]` | 在 extension 模式下打开和刷新布局、分析。 |
| 需求 | `saos demand show --job J` / `saos demand confirm --job J --quote TEXT` | 校验需求单或记录当前版本的用户确认。改描述、总结、待确认问题或语言不需重新确认；改数值、范围、判定、权重、单位、依据（source）需要重新确认，只改依据时已测数值的比较保留。 |
| 脚本 | `saos exec SCRIPT --job J [--arg K=V …] [--args-file JSON] [--name N] [--live] [--detach]` | 在后台执行 `run(ctx)` 并存档。 |
| 扫描 | `saos scan --job J --plan JSON [--workers N] [--detach]` / `saos scan --job J --show [NNN]` | 候选计划先零优化初筛，再给每个候选相同短预算；含优化阶段的处方候选必须提供 prepare 设置变量和评价函数，缺评价函数按配置错误返回 BAD_ARGS；hypothesis 可选，不设候选配额。结论为有效、预算内无效、未评估（材料、计算）；结构指纹去重。计划、候选和 prepare 副本封存，原脚本变化不影响副本；数值警告计数受脚本过滤器影响。缺少初筛只提示；深度优化要求需求和模型确认。 |
| 实测 | `saos check --job J [--only IDS] [--detach]` | 按需求单测量并留证据，不记录轮次。 |
| 轮次 | `saos round --job J [--tier T[,T…]] [--note TEXT] [--merit V] [--no-viz] [--detach]` | 归档镜头、实测并更新终止判断；进入 OpticStudio 后按本阶段预算与比较窗口判断。 |
| 停止 | `saos rule-out --job J --tier T [--tier T…] --reason TEXT` / `saos stop --job J --reason TEXT --quote TEXT` | 带证据排除手段或记录用户停止决定。 |
| 阶段 | `saos zos begin --job J --quote TEXT [--round N] [--max-rounds K] [--detach]` | 用户认可当前报告后，从所选可交付轮次的处方重建，封存认可原话并记录首轮复测与逐项对比；后续固定用 OpticStudio，预算从复测基线重新计数。中断后以原参数续跑；首轮基准与材料差别随账本保存，恢复复用原记录，入口和对比均封存后才开放后续轮次。 |
| 报告 | `saos report --job J [--lang zh\|en]` | 生成交付报告，区分可交付镜头与诊断结果；注明数值由哪个后端算出，Optiland 的数值标为尚未经 OpticStudio 验证；进入阶段后只选 OpticStudio 阶段轮次，并列出 Optiland 结论、逐项与基准差别、材料数据变化和未翻译设置。 |
| 优化 | `saos opt vary --job J --param P --surfaces RANGE [--min X] [--max X] [--mode model\|substitute] [--catalog C] [--off]` | 设置面变量和玻璃替换目录；Optiland 会话里 `--min/--max` 给变量上下界（存在伴随文件里），`--mode model` 用 Abbe 材料近似模型玻璃，`--mode substitute` 切到 OpticStudio。 |
| 优化 | `saos opt vary --job J --config-rows RANGE [--configs RANGE] [--off]` | 设置多重结构变量。 |
| 优化 | `saos opt vary --job J --objects RANGE --param P [--group S] [--off]` | 设置非序列物体的位置、倾斜或参数变量。 |
| 优化 | `saos opt merit --job J clear\|wizard\|add\|show [--file JSON]` | 编辑或查看评价函数（Zemax 操作数名；Optiland 未映射的操作数切到 OpticStudio）。 |
| 优化 | `saos opt merit --job J script PATH.py [--weight W]` | Optiland 会话：把脚本的 `loss(ctx)` 作为评价函数的一行。 |
| 优化 | `saos opt local --job J [--cycles N] [--algorithm auto\|trf\|lm\|lbfgsb\|nelder-mead\|powell\|torch-trf\|torch-lbfgs\|torch-adam\|dls\|od] [--device auto\|cpu\|cuda] [--max-seconds S] [--cores N] [--measure IDS] [--detach]` | 深度优化统一要求需求与当前模型确认；缺初筛仅提示。运行有预算的局部优化：Optiland 会话默认 auto，优先 torch-trf（CPU float64），明确的可微能力缺口才回退 trf；显式指定算法不回退，结果以 requested_algorithm、algorithm、fallback 记录选择和原因。候选短优化使用同一默认值，支持 algorithm/device；回退只用剩余预算；命令预算包含前后测量、复核及搜索归档。Python 光线循环有中断检查点，原生调用和原子文件写入须等待返回。到期恢复已验证快照；review.completed=false 时 valid=false，补做复核后再记录轮次。improved、converged 独立记录，Optiland succeeded 要求两者为真且未中断；stopped_by 统一为 null/timeout/cancel。torch-trf 使用 Torch 残差与自动微分雅可比、SciPy 信赖域选步，未采用差分雅可比。torch 算法对可微子集用自动微分，进程内、不占许可证；dls / od 在 OpticStudio。候选步遇到交叠或任一视场/波长有效率不足 95% 时拒绝；torch-trf 缩短信赖域，显式 Torch 下降法最多回溯 32 次，SciPy 路径最多减半 12 次；Torch 另加连续可微失效光线惩罚，返回回退次数与原因。 |
| 优化 | `saos opt hammer --job J --max-seconds S [--cores N] [--measure IDS] [--detach]` / `saos opt global --job J --max-seconds S [--method de\|basinhopping\|shgo] [--cores N] [--measure IDS] [--detach]` | 运行限时 Hammer（OpticStudio）或全局搜索；Optiland 会话里全局搜索用 scipy，de / shgo 要求每个变量都有上下界。 |
| 优化 | `saos opt glass --job J [--surfaces RANGE] [--catalog C] [--neighbours N] [--cycles N] [--algorithm A] [--max-seconds S] [--measure IDS] [--detach]` | Optiland 阶段的在产玻璃替换：贪心邻域搜索，每种候选都重新局部优化；默认优先 Torch，不支持时回退 trf，local_algorithms 记录选择和原因。 |
| 流程图 | `saos ui open --job J [--tabs LIST] [--metrics IDS] [--tab ID] [--reset] [--no-window]` / `saos ui notify --job J --message TEXT` | 打开工作流窗口（流程、布局、点列图、光线扇图、场曲/畸变、MTF、轮次、起点与初筛）或在询问用户前显示提示。`--tabs` 按系统类型选定并排序上方标签页，`--metrics` 指定“指标”页按轮显示的需求条目，两者按任务记住；Optiland 阶段每轮只导出选中的分析图。 |
| 配方 | `saos microscope adapt EXTRACT --out DIR [--embodiment ID] [--focal-length F] [--field-number N] [--tube-lens F] [--catalogs …] [--stop S]` | 将提取数据适配成处方和期望值。 |
| 配方 | `saos microscope repro --job J EXPECT` / `saos microscope accept --job J --spec FILE` | 做复现校验或规格验收。 |
| 配方 | `saos microscope perturb --job J --seed S --radius-rel A [--thickness-rel B]` | 对物镜施加可复现扰动。 |
| 结构库 | `saos microscope library check EXTRACT` / `saos microscope library add ID --extract FILE [--catalogs …] [--focal-length F] [--field-number N] [--stop S]` | 核对提取数据或适配入库。 |
| 结构库 | `saos microscope library verify ID --job J [--discard]` / `saos microscope library list` | 验证基准或列出条目。 |
| 结构库 | `saos microscope library query --na A --magnification M [--class C] [--field-number N] [--working-distance W] [--limit K]` | 按规格查询起始结构。 |
| 内部入口 | `saos daemon run [--mode standalone\|extension]` / `saos ui serve` | 前台运行后台进程或流程图服务，通常由启动命令调用。 |

镜头文件名相对任务的 `lenses/`，当前镜头操作的 `--job` 必须匹配。extension 替换未保存窗口内容前先另存 `lenses/captures/`；agent 自己的修改需保存或显式放弃。需求单格式见 [demand.md](../skills/saos/demand.md)，结构库格式见 [library.md](./recipes/microscope/library.md)。

封存证据由工具记录哈希；文件核验可发现后续改动或删除。存在被改动或缺失的封存文件时，报告生成拒绝继续，不能把损坏证据当作有效结果交付。

长任务的 `--detach` 结果在任务 `runs/NNN-<op>.json`；扫描例外，状态在 `scans/NNN/`。用 `saos job show J` 或 `saos scan --job J --show` 跟进；超时只停止等待，不代表计算结束，不要直接重复提交。

## 环境变量

| 变量 | 默认值与作用 |
|---|---|
| `SAOS_INSTANCE` / `SAOS_HOME` | 实例默认为 `main`，产物在 `work/<实例>/`；后者可指定运行目录并覆盖前者。 |
| `SAOS_OPTICSTUDIO_DIR` / `SAOS_BACKEND` / `SAOS_DEVICE` | 默认安装目录为 `C:\Program Files\Ansys Zemax OpticStudio 2024 R1.00`；后端只接受 `optiland`（默认，Optiland 0.6.3，float64）和工具测试用的 `fake`；`zos` 等值被拒绝，OpticStudio 只在 `zos begin` 认可后启动；`SAOS_DEVICE` 为 `cpu`（默认）或 `cuda`。 |
| `SAOS_LANG` | 报告语言依次取 `--lang`、需求单 `lang`、本变量、系统界面语言、英文；日志取本变量及系统语言。 |
| `SAOS_UI` / `SAOS_UI_LIVE` | 前者设 0 禁止自动打开流程图窗口；后者设 1 在长操作期间保持 extension 实时刷新。 |
| `SAOS_UI_PID` | 有多个 OpticStudio 窗口时选择要连接的进程。 |
| `SAOS_POOL_IDLE_S` / `SAOS_LIBRARY` | worker 空闲释放默认 600 秒；结构库默认 `<仓库>/library/microscope`。 |

开发验证的隔离规则见 [AGENTS.md](../AGENTS.md)：命令和测试均设置 `SAOS_INSTANCE=verify`；允许 `SAOS_HOME` 指向本轮独占的系统临时目录。后者会覆盖实例设置，因此必须确认实际路径，不能仅凭 verify 环境变量认定已隔离，也不能指向用户实例。

资源与窗口策略：默认计算核数为本机逻辑核数减 2、至少 1 核，自建的长时间进程使用低于正常的优先级。extension 默认采用 `UpdateMode=EditorsOnly`，长操作期间暂停实时刷新、结束后刷新一次；`SAOS_UI_LIVE=1` 可开启运行中刷新。窗口操作与通知命令见本页命令表。

判定脚本必须只读镜头；写回原值造成的后端末位舍入按相对 `1e-12`、绝对 `1e-15` 容差比较，超出则拒绝记录。需求权重、停止阈值和轮次默认值统一见 [需求格式](../skills/saos/demand.md)。

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

需求哈希分层中，structure.reason 仅属确认层；仅改理由需重新确认需求，但不重算数值。旧记录有存档需求全文时按对应层恢复，旧停止决定不会因改描述而解除；缺全文时只能识别内容未变的旧需求。

通用起点库通过 `saos.starts.generate` 和 `saos.starts.screen` 库函数生成与分两段初筛，见 [library/starts/README.md](../library/starts/README.md)。`lens describe` 只读取已有预判/初筛记录来显示选型依据；当前需求值仍按原检查入口测量。报告的流程耗时由已存时间戳计算，未认可报告时该项显示未知。

起点生成的 `prepare` 是必填关键字参数（省略时 Python 报 TypeError）；传空值在生成前报 BAD_ARGS，路径不存在报 FILE_NOT_FOUND；脚本执行后评价函数为空时，在初筛测量前拒绝。严格查询无结果时，可显式传 `allow_extrapolation=True` 保留超出来源示例范围的起点，超出项随候选显示；不放宽需求、共轭和材料有效范围。`lens build --as` 使用逻辑 `.zmx` 名，Optiland 实际保存 `.optiland.json`。偶次非球面的 `par2` 为四阶项，`par3` 与 `6th Order Term` 均为六阶项，同义键重复指定时报错。

模型确认与轮次使用同一结构比较规则，允许在末尾追加视场/波长采样点；环境面声明变化仍需 `--rebuild-reason`。当前结构匹配较早的完整确认时可继续；损坏历史确认按路径提示，不阻断其他匹配确认。没有 schema/shown 且两文件从未登记封存的旧确认标记 legacy_unsealed，现代记录缺封存不按旧格式放行。确认时间统一由 jobs.now_iso 生成，确认使用微秒精度，同时间以 approval_sequence 排序；无效旧时间保持未知。生成阶段区分库数据、配置、材料和计算错误，prepare 编译/执行失败返回带候选与脚本原因的 BAD_ARGS。
