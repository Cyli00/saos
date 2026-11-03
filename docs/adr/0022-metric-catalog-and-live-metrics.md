# ADR-0022：指标目录与优化过程中的实时指标

状态：首批后端接口与工作流窗口均已实现（2026-11-03）；真实 rtc → OpticStudio 24.1 → 报告链路走通一次，Huygens 对账等仍待完成。

## 背景与用户决定

用户要求指标可按需求选择、优化过程中持续更新，并否定“script 判定另外附带曲线”的方向。此前已确认：

1. 中立目录 `saos.metrics` 与 `kit`、`micro` 同属 L2；设计阶段在 rtc 上计算，交付使用同一指标定义在 OpticStudio 上复测。
2. 需求绑定的指标自动成为展示数据，用户可另选只看不判定的指标，按任务保存。
3. 优化期间由另一个执行单元测量并提供真实数据。
4. rtc Huygens PSF、波前 RMS 先按 ADR-0020 对账，通过后轴向 PSF 才能进入目录。

讨论后收敛实现：不维护变量增量同步协议，不把任意判定脚本搬进后台调度，不复用通用 worker 池。用户随后授权先实现、不做任何测试和验证，再更新本文。

## 指标与正式判定

- `saos.metrics` 是现有测量函数的薄封装，只经上下文、`kit`、`micro` 与后端协议计算。`ctx.metric(id, **params)` 返回完整结果，`ctx.metrics()` 返回目录；脚本上下文、worker 上下文均通过 `saos.metrics.MetricMethods` 获得接口（不放在 `kit.KitMethods`，免得 `kit` 反向导入目录）；rtc 探索副本没有。
- CLI：`saos metrics list --job J`、`saos metrics get ID --job J --param KEY=JSON`、`saos metrics select --job J --file PATH`。
- 目录包含参数类型、缺省值、范围、指标定义、单位、形态、可判定标量和各标量单位。实际条目和参数以 `metrics list` 为准，不在技能里维护副本。
- 首批长度、曲率和 MTF 指标只支持镜头单位 `Millimeters`；其他单位显式报 `BACKEND_UNSUPPORTED`，不把英寸等数值标成 mm。
- 首批 20 项：一阶操作数 9 项、指定面区间厚度、光斑半径表、最佳焦点偏移、点列图、CRA、光线扇、几何焦点随视场曲线、边缘光线 NA、几何收集比例、最小间隙、FFT MTF。
- `availability` 为后端对计算路径的 `available / unsupported / unknown` 判断，不运行探测测量，不代表当前镜头有效或已与 OpticStudio 对账；具体模型和参数仍可触发 `BACKEND_UNSUPPORTED`。
- 需求的 `check` 在原有三类之外接受 `metric: {id, params?, pick?}`。`pick` 必须来自目录声明的标量；缺省为 `value`。仅显示的条目不能用于判定；MTF 判定必须显式给定 `frequency`，取该频率处全部非衍射极限曲线的最小值，不外推。
- 正式测量仍由 `demand.measure`、`check`、`round` 执行。完整结果保存在已有的逐项目录 `evaluations/NNN/<序号>-<需求ID>/<需求ID>.json`；证据中记录路径和指标定义。无效数值沿用既有设计失败语义，后端拒绝沿用 `backend_unsupported` 语义。
- 使用目录判定或在脚本里通过 `ctx.metric` / `saos.metrics` 调用时，把 `metrics.py`、`kit.py`、`micro.py` 的哈希及源码副本纳入判定方法与重测基准；旧的非目录判定接口不迁移。

## 展示选择

`metric_selection.json` 保存 JSON 数组，每项为 `{key, id, params?, pick?}`；`key` 是展示项 ID，必须唯一且不与任何已判定需求项冲突，最多追加 64 项。`metrics select` 整体替换，空数组清空。

此文件不参与需求判定、确认摘要或证据封存。需求内所有 `metric` 判定与追加选择共同组成实时请求；追加项标记 `display_only: true`。窗口判定卡的“＋”选择器经 `POST /api/metrics` 写入同一文件。

## 实时计算

- 仅 rtc 启用：每个 daemon 惰性建立一个专用 rtc 计算进程和一个监督线程；复用现有 `WorkerContext`，不占通用池，不启动 OpticStudio 或额外占用 ZOS 许可。
- 每次提交由主线程调用 rtc `snapshot_to` 导出完整 `.zmx`，保留主镜头的文件路径与未保存标记；另传当前多重结构号。快照含变量、玻璃及评价函数，子进程逐帧重新加载，不维护增量状态。
- 优化接受解的进度回调按现有 5 秒刷新间隔提交快照；长脚本沿用 `view_tick` 的节流；回复后的 rtc 空闲刷新也提交快照。短操作结束后的最终状态仍由 `idle` 提交。不承诺每步都有画面。
- Hammer/Global 的局部优化也提供接受解回调；外层报告最好评价函数值之前恢复对应的变量与玻璃，保证快照与报告值对应。
- 一个正在计算的帧加一个可替换待算帧；新快照替换待算快照并删除其临时目录，不排队积压。
- 只运行目录指标，不自动运行需求中的 operand、analysis 或任意 script 判定；这些条目保留最近一次正式判定的值与原视图版本。评价函数由副本计算，优化回调已有值时使用与快照同时取得的值。
- 整帧硬超时 `SAOS_LIVE_FRAME_S` 缺省 10 秒，包含进程启动、加载和全部测量。超时终止进程并发布错误帧；下一份快照按需重建，不重试同一帧，不实现逐项时间调度。正式 `check`、`round` 不受这个实时超时限制。
- `SAOS_LIVE_S=0` 或 `SAOS_VIEW=0` 关闭 rtc 实时提交；正的 `SAOS_LIVE_S` 数值不再限制 rtc 帧，OpticStudio 仍用 ADR-0019 的原有预算。
- 正式判定、镜头改动和后端切换使旧实时帧失效；正式结果写入与帧发布共用锁，避免迟到帧覆盖正式结果。关闭 daemon 时回收子进程、连接与临时快照目录。
- OpticStudio 保留原有回复后测量，优化期间不启动实时测量进程；长操作避免 API 竞争的规则继续适用于它。

## 实时文件与 HTTP 数据契约

`metrics.json` 保留旧版的 `items` 映射和每项 `value / view / source / t / gap / error / duration_s`。实时帧增加 `schema: 2`，使用以下字段：

| 字段 | 含义 |
|---|---|
| `view` | 快照对应的视图版本，与 `view.json.view` 比较判断是否过时 |
| `backend` | 实时帧为 `rtc` |
| `iteration` | 回调给出的局部迭代号或外层搜索系统数；空闲刷新时为 null，不保证跨局部搜索单调递增 |
| `merit`、`merit_error` | 同快照的评价函数值及其错误；不可用为 null |
| `provenance` | rtc 对这一帧使用路径的来源与未对账项记录；失败帧可能为 null |
| `t`、`updated` | 快照提交的 Unix 秒时间与帧发布的 ISO 时间 |
| `live` | `{status: ready|error|recorded, view, iteration?, error?, timeout_s?, preview?}`；实时帧 `preview: true`，不作为正式证据 |
| `items.<展示ID>` | 需求或追加选择的结果，逐项保留版本，允许与旧正式判定结果共存 |

目录项额外字段：`id`（需求/展示 ID）、`metric_id`（目录 ID）、`name`、`description`、`unit`、`shape`、`params`、`scalars`、`scalar_units`、`display_only`；计算失败时不保证有所有结果字段，必须先处理 `error`。

- `scalar`：`value`；`scalars` 提供可由 `pick` 选择的命名数值。目录条目的 `value` 已按需求/选择的 `pick` 提取，条目同时写出 `pick` 与 `unit`（所选标量的单位，由 `metrics.picked` 统一给出），消费方直接用 `unit`，不再自行查 `scalar_units`；完整 `scalars` 仍保留。
- `curve`：`series: [{name? / description?, x: [...], y: {曲线名: [...]}, render?}]`；`x_axis / y_axis` 给出名称与单位。空值表示该样点无结果；点列图以 `render: scatter` 表示散点。原生分析的系列保留原 `x_label`。
- `table`：`table.rows` 保存逐视场/波长的数据。
- 协议预留 `grid` 形态；原生分析可在 `grids` 保留网格，首批未注册独立二维图指标。
- 已绑定需求项有 `goal`；有达标界限时给出 `passed` 与 `gap`。仅显示项不计算达标结论。无效值、失败或超时时不能当作有效达标结果。

正式 `check`、`round` 的目录结果也写入实时文件，`source` 标明来源，并将 `live.status` 设为 `recorded`。完整正式证据路径保持原约定。

现有 `/api/live` 原先只返回 `view.json` 的视图数据；本次在保留原响应字段的基础上增加 `metrics`，读取 `metrics.json`。视图与指标异步到达，消费方必须按每项 `view` 判断，不能假定 HTTP 返回的两部分是同一帧。原有流程状态响应里的 `metrics` 读取方式保留；工作流窗口的判定圆环、需求卡和指标选择器读取这些条目。

沿用窗口服务的 Host 与 token 校验，新增 `GET /api/metrics?job=J` 查询目录，`POST /api/metrics` 传 `{job, items}` 保存选择，`POST /api/metric` 传 `{job, id, params?}` 单次测量。响应沿用 `{ok, result}` 或 `{ok, error}`。它们复用 daemon 接口，不绕过其加载任务与后端约束；优化占用 daemon 时会报 `BUSY`，消费方应缓存目录并保留用户尚未保存的选择，待空闲再保存。

## 取舍、未实现项与未验证项

完整快照减少同步规则，代价是每帧保存/加载开销和一份 rtc 常驻副本。仅测目录指标避免执行任意脚本，但旧判定不会自动获得优化中实时刷新；可逐项改成等价目录判定，修改后仍须按现行规则重新确认需求。

本次不实现 BFL 专门条目、子午/弧矢场曲、畸变、独立二维图或布局配方的 FOV/工作距离上收。`field_focus` 只是几何 RMS 最佳焦点曲线，不能冒充子午/弧矢场曲或衍射焦深。目录后续按已有可复用测量逐项扩展。

Huygens PSF、波前 RMS 的 rtc 后端拒绝保持不变，未新增对账通过的声明，未把轴向 PSF 放进目录。

2026-11-03 的独立验证在真实 rtc → OpticStudio 24.1.0 → 报告链路上通过：rtc 与 OpticStudio 的 EFL 相差约 5e-9 mm，复测 `agrees: true`；两个后端的正式测量、实时需求项、追加项与错误条目都写出所选标量的单位；参数边界（null、重复 key、非法 pick）返回 `BAD_ARGS`；超出 float 范围的整数参数随后改为在入口拒绝，尚待复验。仍未覆盖：Extension 窗口、非序列与混合模式案例、超时与并发压力、刷新成本。光学数值的对账范围仍按 ADR-0020 执行。
