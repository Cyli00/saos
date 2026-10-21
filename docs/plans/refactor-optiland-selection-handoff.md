# 选型证据与失效分层：本轮交接

日期：2026-10-21。对应综合计划 R3（G01–G06）的一部分和交接文档 B2，另含 B 批之外的需求哈希分层与建模确认页。完成状态仍以[综合计划](./refactor-optiland-first-workflow.md)第 13 节为准；本文记录本轮的改动、验证结论和下一轮做法。

> **2026-10-21 方向改定**：用户随后决定不再沿“加固选型证据”走下去，改为起点库、像差预判与诊断工具，关卡只留在昂贵处（综合计划第 7 节）。本文第 6 节的修复安排因此作废，改由[交接文档](./refactor-optiland-handoff.md)第 4 节 B0 的去留表处理；第 1–5 节作为本轮记录保留。

## 1. 起因

用户从使用者角度提出四项担忧：

1. 三个候选里可能只有一个是认真的，另外两个只为凑数；
2. 候选比较的排序可能受计算问题影响，而“结构不成立 / 参数不当 / 材料无法表达 / 计算出错”被混为一类“候选失败”；
3. 用户确认模型时看不到要求满足情况、证据、未知项和下一阶段预算，确认会替系统担保计算有效性；
4. 改了一处之后旧结论是否仍有效：换玻璃后旧比较仍算数，只改描述文字却要重走流程，中断恢复后旧证据可能支持新模型。

用户还说明后续会看三个结果指标：选型后因基础假设错误而重建的次数、获得可用初始模型的耗时、达到验收要求的总耗时。本轮没有实现这些指标的采集，见第 6 节。

## 2. 对象

- HEAD `b4c0851`，全部改动未提交。跟踪文件改动 29 个（+2315 / −342 行），新增 `tests/test_demand_layers.py`；`docs/plans/refactor-optiland-first-workflow.md` 与 `refactor-optiland-handoff.md` 本轮之前就未被跟踪。
- 后端：Optiland 0.6.3。本轮没有启动 OpticStudio。
- 实施分两步，每步两个子代理并行，文件范围互不重叠；验证交给 verifier 子代理。子代理的报告由主会话汇总写入本文，主会话没有逐行复核全部源码。

## 3. 本轮改动

### 3.1 候选记录、分类与去重（`candidates.py`、`scan.py`）

- 候选计划每项必填 `mechanism`、`risk`、`refuted_by: {check, expect}`，可选 `distinct_by: "material"`。`refuted_by.check` 只能是硬条件条目（`demand.HARD_KINDS`）或 `interference`，目标项被拒绝。
- `plan.json` 与候选副本在初筛开始前封存；写结果前核对，被改则 `EVIDENCE_TAMPERED`（`unchanged_since_start`）。
- 每个候选行有 `verdict`：
  - `material`：`MaterialError`，构建时或判定时都算；判定时靠 `demand.measure` 新增的 `error_kind` 识别。
  - `computation`：抛异常、判定出错、G 非有限、干涉检查报错。另外，无效候选出现数值 RuntimeWarning 时也归为此类。
  - `valid`：没有问题。
  - `structural`：候选无效，且失败原因正是它事先登记的推翻条件。
  - `parameters`：其余无效情况。
- 只有 `valid` 和 `structural` 计入；`material` 与 `computation` 在比较页单列为“未评估”。
- 候选构建后记录结构描述和 `model_gate.fingerprint`，写入 `candidates/<id>/structure.json`（as_built / end）。计数按结构指纹去重：指纹相同只算一个，例外是声明 `distinct_by: material` 且材料确实不同。
- worker 只对 RuntimeWarning 计数（`numeric_warnings: {count, first}`），其他警告交回原输出。

### 3.2 选型记录与优化关卡

- 新命令 `saos scan select --job J --scan scans/NNN --candidate ID --reason … [--premise material:<面号>]`，写入并封存 `scans/NNN/selection.json`。所选候选必须为 `valid`，比较必须合格。
- `workflow.require_optimise` 在结构为 free 时调用 `candidates.require_selected`，依次检查三项：有封存的选择记录；当前镜头与所选候选的结构差异为空（`model_gate.structure_changes`）；premises 中的材料未变。prescribed 结构的例外不变。
- `saos scan` 的 `--job` 改由 `cmd_scan` 检查，因此 usage 行不再显示它是必填项。

### 3.3 需求哈希分两层（`demand.py`、`ledger.py`、`workflow.py`、`report.py`、`stages.py`、`cli.py`、`ui/stage.py`）

- `demand.digest`：确认层，包含三类条目的全部字段，不含 `description`；另含 schema、stop 和声明时的 structure。
- `demand.compute_digest`：计算层，即确认层去掉 `source`。轮次版本、G 可比性、结构比较、stop/rule-out、报告版本对照、OpticStudio 阶段入口都用这一层。
- 兼容：`demand.legacy_digest` 原样保留旧公式；`demand.as_current` 在当前需求按旧公式算出的值与记录一致时，把旧记录认作当前版本。历史文件不改写。
- 效果：只改 `description` 两层都不变；只改 `source` 要重新确认，但数值证据沿用；改数值、范围、判定、权重、单位或 stop，两层都变。

### 3.4 建模确认页四节（`model_report.py`、`model_gate.py`、`daemon.py`）

- **选型依据**：所选候选的机制、风险、推翻条件、理由和 premises；同一比较里其他候选的结论；比较页链接。结构为 prescribed 时显示用户给的理由。
- **需求逐项**：每个条目的当前值、界限、状态和来源，并注明值来自哪个后端。状态有四种：满足、未满足、未知（判定出错或没有有效值）、无界限（没有判定方法的条目）。来源为 agent 假设的会标出来。
- **计算有效性**：注明由系统核对，用户确认不担保这一节。内容包括：所选候选的数值警告；判定出错或没有有效值的条目；材料四级（资料找到、数据完整、后端支持、实际加载），确定不了的级别写“未知”。
- **下一阶段预算**：读取 `notes.next_budget: {max_seconds, plan}`。格式错误时拒绝生成页面；缺失时给自动警告。
- `model.json` 记录 `shown`、`shown_sha`（语言无关）和 `unknowns`；`lens approve` 写入 `shown_sha` 与 `unknowns_acknowledged`。旧文件没有这些字段时仍然有效。
- `lens describe` 每次都会跑一遍需求单的全部判定（`demand.quick`），耗时未测。

### 3.5 工作流四处同步

- SKILL.md、demand.md、`cli_help.py`、`docs/cli.md`（`scan`、`scan select`、`demand`）已更新。
- UI 的结构比较节点显示“已比较 n 个 · 已选择 id”，没有新增节点。
- drawio 的结构比较菱形改为五行文字并放大，渲染后已查看。
- DESIGN.md 补记三条用户决定：淘汰必须对应事先登记的推翻条件；深度优化的模型必须是比较中选出的结构；需求失效分两层。
- 未完成：drawio 与 `docs/cli.md` 中 `lens describe` / `lens approve` 的部分，还没写确认页四节和 `next_budget`。

## 4. 测试与验证

| 项 | 命令 / 方式 | 结果 |
|---|---|---|
| 非 ZOS 全量 | `SAOS_INSTANCE=verify uv run pytest -q` | 441 通过、11 跳过（主会话本次复跑，62.7 s；跳过的为 `SAOS_RUN_ZEMAX` 测试） |
| 新增/修改测试 | `test_candidates.py`、`test_demand_layers.py`（22 例）、`test_model_report.py`（新增 8 例）、`test_ui.py`、`test_workflow.py` | 均包含在上面的全量中 |
| 修改过的旧断言 | 见各子代理说明，要点：候选数据补新必填字段；推翻条件由目标项 `length` 改为硬条件 `max_length`；“只有目标未达标”由 `structural` 改为 `valid` | 依据规则变化，不是放宽 |
| drawio 渲染 | `SAOS_RENDER_DRAWIO=1 … tests/test_docs_sync.py -s` | 3 通过；结构比较区域人工查看无重叠，返回边未逐条放大检查 |
| UI | 子代理用 HTTP `/api/state` 和 node 执行 `app.js` 的文案函数核对 | 没有浏览器视觉检查 |
| 对抗性验证 | verifier，进程内 Daemon + 真实 Optiland | **FAIL**，见第 5 节 |
| OpticStudio 阶段入口版本比较 | — | NOT RUN（需要 OpticStudio） |

verifier 的探针脚本和输出在 `C:\Users\leahd\AppData\Local\Temp\saos-verify\probes\`（`p*.py` / `p*_out.txt`）。这是临时目录，可能被清理；下一轮应把有价值的探针改写成仓库内的回归测试。第 5 节的缺陷来自 verifier 的报告，主会话没有逐一独立复现。

## 5. 验证发现的问题

按严重性排序。“位置”取自 verifier 报告，行号对应当时的工作树。

| # | 严重性 | 问题 | 复现要点 | 位置 |
|---|---|---|---|---|
| 1 | 高 | 升级前写下的确认、轮次、stop 和已封存比较，只改一次描述就全部失效；重新确认后可以越过用户的停止决定继续记轮次 | p18_legacy.py、p18b_legacy.py、p29_legacy_cmp.py | `demand.py` `as_current`（约 159–168）只认“内容与旧公式完全相同”；`ledger.stop_for` |
| 2 | 高 | 凑数仍可行：1 个有效候选，加 2 个登记了起点必然失败的硬条件（总长约 206 / 255 mm，对 150 mm 上限）的候选，即可满足“3 个计入 / 1 个有效”，选型和结构关卡都通过 | p3_strawman.py、p4_gates.py | `candidates.validate`（约 166–169）只检查推翻条件的类型 |
| 3 | 高 | 封存窗口：运行中途改已完成候选的 `structure.json`（材料 N-BK7→F2），会在结束时一并被封存，之后 premise 检查和结构关卡都会认它 | p10_window.py W1 | `scan.py` 约 935 的 `job.seal(folder)` 在结束时才执行；`structure.json` 在候选完成时写入（`candidates.py` 约 455），不在 `unchanged_since_start` 的检查范围内；end 材料与 `material_source.name` 没有交叉核对 |
| 4 | 中 | 两个 `saos scan select` 同时执行，其中一个报 INTERNAL_ERROR（`selection.json.tmp` 重命名失败），数据没有损坏 | p28_race_setup.py | `cli.py` 约 257 在 CLI 进程内直接调用，绕开了 daemon 的串行处理；`protocol.write_json` 的临时文件名固定 |
| 5 | 中 | 已封存的 `model.json` 被改（删 shown/shown_sha、清空 unknowns）后仍可确认，`require_approved` 也接受 | p20_approval.py F | `model_gate.approve`（约 318–345）不核对封存，也不要求有 `shown_sha` |
| 6 | 中 | 比较完成后修改 prepare 脚本，选型仍被接受 | p10_window.py W3 | prepare 不在 `unchanged_since_start` 的检查范围内，也不在 `demand.method_digest` 的范围内（约 301） |
| 7 | 低–中 | 检查脚本可以自行屏蔽 RuntimeWarning，数值警告计为 0 | p31_suppress.py | `_Numeric` 依赖 `simplefilter("always")`，脚本可以覆盖它（`candidates.py` 约 478–482） |
| 8 | 低 | drawio 与 `docs/cli.md` 中 `lens describe` / `lens approve` 的部分，缺确认页四节和 `next_budget` | 逐句核对 | `docs/workflow.drawio`、`docs/cli.md` |
| 9 | 低 | 没有 `sources/materials.yaml` 时，“资料找到”写“否”，应为“未知” | p20_approval.py E4 | `model_report.py` `_level_found` |
| 10 | 已知 | 脚本里直接调用 `ctx.backend.optimize(...)` 不经过任何关卡 | p14_gates_all.py G5 | `scripting`，属于交接文档 C1 |

verifier 判为 PASS 的项（摘要）：

- **篡改**：选择记录、封存后的结构文件、result.json、plan.json 被改都会被拒绝；选择非 valid 的候选、空理由、非法 premise 都被拒绝。
- **深度优化入口**：opt local/hammer/global/glass 和 scan minimize 在结构不一致时都被拒绝。
- **凑数**：只差半径、只改标题、材料名只差大小写的候选都会被去重。
- **分类**：九类场景都分对了。
- **哈希分层**：description、source、unit、summary 四种改动的失效行为符合设计；`as_current` 不能用来让别的需求的旧哈希冒充当前版本。
- **确认页**：`shown_sha` 与语言无关。
- **文档**：SKILL.md、demand.md、`cli_help.py` 与代码一致；按 SKILL.md 写的候选计划能通过校验。

## 6. 下一轮做法（已作废，见文首说明）

### 6.1 需要用户先决定

1. **第 2 项凑数（建议方案）**：被淘汰的候选必须先和其他候选用同样的短优化预算跑过，优化后它登记的那条硬条件仍然失败，才记为 `structural`；只经过零优化初筛就失败的，一律记为 `parameters`，不计入。代价是每次比较多花一段短优化的时间，具体多少要实测。另一个方案是要求至少 2 个有效候选，约束很紧时容易卡死。
2. **`structure.reason`**：现在同时进入两层，只改写理由文字也会让比较和轮次数值失效。建议移出计算层，只留在确认层。
3. **旧比较**：升级前的候选比较没有推翻条件和四类结论，新规则下全部不计入，进行中的旧任务要重跑一次比较。建议不豁免。
4. **需求逐项的“无界限”**：保留为第四种状态，还是并入“未知”。
5. **`shown_sha`**：确认后页面内容再变，是否要挡住后续轮次。目前只做记录。

### 6.2 修复（第 1–9 项）

| # | 改法 | 回归 |
|---|---|---|
| 1 | 旧记录用它写下时存档的需求全文重算新哈希：确认记录有 `content`，轮次和比较能从证据中取到需求时也这样做。取不到全文的旧记录保持现状，并写明限制 | p18/p18b/p29 改写为测试：只改描述后，确认、轮次、stop、比较都仍有效；有 stop 时不能记新轮次 |
| 2 | 按 6.1 第 1 项的决定实现：`structural` 必须有优化阶段的证据；比较页和确认页标出每个淘汰是“优化后仍失败”还是“仅初筛” | p3/p4 改写为测试：凑数组合不再合格；正常淘汰仍然合格 |
| 3 | 每个候选完成时立即封存它的 `structure.json`（沿用现有 `job.seal`），或在写结果前把它纳入核对；选择时核对 end 材料与 `material_source` 一致 | p10 W1：运行中篡改被拒绝 |
| 4 | `scan select` 经 daemon 执行；或者 `write_json` 改用唯一的临时文件名，并在目标已存在时明确报 FILE_EXISTS | p28：并发时一个成功，另一个得到明确的错误码，不出现 INTERNAL_ERROR |
| 5 | `approve` 先核对 `model.json` 的封存；新格式描述缺 `shown_sha` 时拒绝。旧格式（从来没有 shown）仍然接受 | p20 F：篡改后拒绝确认；旧格式仍然通过 |
| 6 | prepare 脚本的内容摘要写入 plan，并纳入初筛前封存；选择时核对 | p10 W3：改 prepare 后选择被拒绝 |
| 7 | 计数不再依赖调用方的过滤器：在判定执行处临时恢复 always 过滤器并在结束后还原；或者在比较页说明脚本可以屏蔽警告。两种做法都要写清能力边界 | p31：脚本屏蔽后仍能计数，或页面上有说明 |
| 8 | 补 drawio 中 lens describe/approve 节点的文字和 `docs/cli.md` 对应行；渲染后查看 PNG | `test_docs_sync` 加人工查看渲染图 |
| 9 | “资料找到”在没有 `materials.yaml` 时改为“未知” | 补单元测试 |

第 10 项留给交接文档 C1，这一轮不动。

修完后让 verifier 用同一批探针回归，并补测 G04（只改注释或普通变量不应失效）、G05（逐个入口循环调用时的累计预算）、G06（指定结构越界）。

### 6.3 之后的工作（不在下一轮）

- **概念记录**（`sources/concept.json`：约束、自由度、未知项、候选机制、下一步最小计算）没有实现，现在只有候选级的 mechanism/risk/refuted_by。
- **两阶段比较**：先统一初筛，再按预先写好的规则给入围候选分配短优化预算，累计预算不能因重试或重启而重置。尚未实现；6.2 第 2 项会先用到其中“短优化”这一段。
- **用户的三个结果指标**：从需求确认到首次模型确认的耗时、到报告认可的耗时，可以从现有时间戳算出。“因基础假设错误而重建的次数”需要在模型重新确认时留一个原因字段，目前没有。现有真实任务只有一个，不足以判断返修是否减少。
- **材料身份贯通**（交接文档 B1、M01–M04）未做；`MaterialError` 目前只覆盖 Optiland 路径，ZOS 路径的材料错误仍归为计算异常。
- **其他小问题**：
  - 未在 OpticStudio 下验证：`stages.begin` 的版本比较。
  - 未测：`lens describe` 每次都跑全部判定的耗时。
  - UI：节点副文字截到 29 个字符，设计节点的“并选定”会被截掉。
  - UI：`rounds_of` 显示的 demand_sha 仍是存储值。
  - UI：没有在浏览器里做视觉检查。

## 7. 副作用与遗留

- 本轮所有命令都在 `SAOS_INSTANCE=verify` 下运行，未设置 `SAOS_HOME`，没有读写 `work/main/`。
- 保留的 verify 任务：`work/verify/jobs/20261021-171100-ui-compare-check`，是同步代理做 UI 检查时建的。
- 进程：同步代理启动的 UI 服务（PID 166888）已结束；verifier 没有启动守护进程，它看到的 PID 41444 不是本轮启动的，没有动；verifier 建的 44 个临时任务已删除。
- 没有提交、推送或修改全局配置。
