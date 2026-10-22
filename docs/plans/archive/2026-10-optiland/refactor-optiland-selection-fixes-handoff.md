> 历史归档（2026-10-22）：本文保留当时的方案、实施与测试记录，不再是执行入口。当前待办见[整合计划](../../integration-workflow.md)。正文中的“当前”“下一轮”、路径与执行指令均属于原记录；未完成事项的去向以新计划为准。

# 选型证据修复：暂停交接

日期：2026-10-21。用户要求先停止工作、记录交接；本文写入后不继续实现或验证。

上轮问题清单见[选型证据与失效分层交接](refactor-optiland-selection-handoff.md)。综合计划及验收状态仍以[综合计划第 13 节](refactor-optiland-first-workflow.md)为准。本轮尚未通过最终回归，不能标为完成。

**暂停核对时发现的新计划变化**：计划索引与综合计划第 7 节现在写明，R3 改为起点库、像差预判与诊断工具，关卡只留在昂贵处；第 13 节 R3 行将推翻条件、scan select/selection.json、require_selected 列为待回退。此方向文本不是本轮写入的，与本会话依据上轮交接实施的选型关卡修复存在冲突。本文保留实际工作记录，**不表示应继续推进这些关卡**；下一轮须先对照最新综合计划和剩余工作交接第 4 节 B0/B2，确定本轮哪些改动保留、哪些回退，再决定测试和收尾范围。不得将本文旧范围自动覆盖最新计划。

## 1. 已获授权的范围与决定

用户接受了主会话的五项建议，并授权实施：

1. 候选使用统一短优化预算；短优化失败只能证明预算内没有找到解。计为结构淘汰还需独立物理界限或其他独立证据。
2. `structure.reason` 移出计算层，保留在确认层。
3. 旧比较不豁免新增选型证据；旧需求记录的哈希兼容单独修复。
4. 保留“无界限”，与没有有效计算值的“未知”区分。
5. 不因整个确认页内容变化而阻断后续轮次；结构、材料前提、计算方法变化使对应证据失效，普通调参与数值变化不反复确认。

本轮承诺处理上轮交接第 1–9 项，以及相应文档和浏览器检查。第 10 项直接调用 `ctx.backend.optimize(...)` 的旁路仍留在 C 批。概念记录、完整两阶段比较及累计预算、三个效率指标、材料身份贯通，没有加入本轮范围。

## 2. 工作树与环境

- 进入本轮时已有大量未提交改动。本轮在原工作树上继续修改，没有提交、推送、重置或另建分支。`git diff` 包含上轮改动，不能直接当成本轮差异。
- 本轮新增 `tests/test_selection_evidence.py`；`tests/test_demand_layers.py` 上轮已未被跟踪，本轮继续追加测试。
- 仅使用 `C:\Git\bin\bash.exe` 执行终端命令；Python 经 `uv run`。
- 所有 `saos` 命令和 pytest 均设置 `SAOS_INSTANCE=verify`。pytest 自建任务使用系统临时目录中的 `SAOS_HOME`，未读写其他实例的任务目录。
- 没有启动 OpticStudio、daemon 或 UI 服务，没有关闭已有进程。测试中的 worker 由各 fixture 的 pool 清理。
- `SAOS_INSTANCE=verify uv run saos daemon status` 显示现有 verify daemon PID 41444，后端 Optiland 0.6.3，持有 `20261020-222113-plan2-verify-cooke`，镜头 dirty。本轮未修改或关闭它。
- 同一状态查询报告有 4 个 OpticStudio 进程、许可证空位 4；extension 窗口无法唯一确定。本轮没有进入这些窗口。
- Chrome 路径已确认存在；本轮检查时当前 uv 环境没有安装 Playwright。没有安装它，也没有执行浏览器操作测试。
- 没有使用子代理；先前交接中的 verifier 探针仅作阅读参考，没有原样重跑。

## 3. 已写入代码的改动

以下为实现状态，不表示全部验收通过。

### 3.1 旧需求记录兼容与理由分层

文件：`src/saos/demand.py`、`ledger.py`、`stages.py`、`ui/stage.py`。

- `compute_digest` 从 structure 中排除 reason；确认层仍包含 reason，旧公式 `legacy_digest` 保留。
- `as_current` 增加可选 root。从 `confirmations.jsonl` 的 content 和 `evaluations/*/result.json` 的 demand 恢复历史需求全文；先核对存储哈希确实属于该全文，再比较目标层，不能把其他数值需求直接认作当前需求。
- 兼容旧公式，以及本次移除 reason 之前的计算层公式。不改写历史记录。
- 确认、账本轮次、stop、rule-out、候选比较、OpticStudio 阶段入口比较均已传入任务 root。
- UI `rounds_of` 读取当前需求后在内存里转换旧轮次版本。
- 缺少存档全文时，仍只能识别与当前需求内容完全一致的旧公式值。

新增 3 个需求测试：存档确认在改描述后保留确认/轮次/stop/rule-out；仅有 evaluation 全文时恢复旧轮次；reason 改写只影响确认层并兼容过渡哈希。

### 3.2 不把短优化失败当成结构证明

文件：`src/saos/candidates.py`、`scan.py`。

- `refuted_by` 允许可选 bound，格式为有限的 min 和/或 max，加非空 source、argument；单位沿用登记硬条件。界限必须与该硬条件的 goal 矛盾。
- 只有候选完成共享计划的全部短优化阶段、未记录取消、登记硬条件失败且有 bound 时，才产生可计入的 structural。
- 没有独立界限时，初筛失败或短优化失败均为 parameters。单凭当前几何交叠也只算 parameters。
- 行记录加入 screening（initial/optimised）、structural_evidence。
- `_accounted` 不再只看 structural 字样，还要求 structural_evidence。
- 新比较在 plan 写 `selection_schema: 2`；旧格式比较不会因只有历史哈希兼容而自动取得资格。
- 比较页和确认页显示独立界限与初筛/优化后失败的区别，并说明系统只核对界限与需求矛盾，不自动证明推导正确。

目前只实现独立物理界限这一类证据，没有通用的“其他独立证据”执行协议。bound 来源与推导由设计者登记，系统未独立求解物理证明。完整两阶段初筛和跨重试累计预算仍未实现。

### 3.3 封存窗口、prepare 与材料核对

文件：`src/saos/candidates.py`、`scan.py`、`jobs.py`。

- 每个候选完成 structure.json 后立即通过现有 `job.seal` 封存。
- 扫描结束时核对结构文件；最终只新增封存 result.json 与 index.html，避免把已有候选文件重新封存而掩盖修改。
- plan 保存 input_files 清单，因此输入被删除也可发现，而不只检查仍存在的文件。
- prepare 复制进本次扫描目录，初筛前封存；worker 执行副本。plan 记录原路径及 prepare_sha，选型时原文件被改或删除均失效。
- 选择候选时核对 end 面材料与 material_source.name，并核对结构及材料摘要与结果行一致。
- 实测发现多个 worker 同时向 seals.jsonl 追加时，Windows 会丢一条记录；已为 `Job.seal` 加进程间文件锁。Windows 用 msvcrt，其他平台分支用 fcntl；其他平台未实跑。

### 3.4 并发选择

文件：`src/saos/protocol.py`、`candidates.py`。

- `write_json` 使用唯一临时文件名，完成后清理。
- 增加 exclusive 选项，以同目录临时文件的硬链接原子创建目标；目标已存在返回 FILE_EXISTS。
- `scan select` 使用 exclusive 写 selection.json；没有改成经 daemon 串行执行。
- 两个线程并发选择的回归已加入；跨 CLI 进程的原探针尚未重新执行。硬链接在本机临时 NTFS 目录的测试通过，其他文件系统未验证。

### 3.5 模型确认与“未知”

文件：`src/saos/model_gate.py`、`model_report.py`。

- approve 和读取 approvals 均核对 model.json 封存；读取 approvals 时还核对 approval.json 封存。
- 带 shown 或新 approval_schema 的描述必须有匹配的 shown_sha；删除新字段不能绕过封存检查冒充旧格式。
- 旧格式描述仍可确认，但也必须保留有效封存。相应旧格式测试 fixture 先构造旧文件再封存，没有保留“篡改后照样确认”的旧断言。
- 新描述记录 approval_schema 和 approval_context（method_sha、所选 scan）。`require_approved` 核对方法版本及选择前提，不按新测量结果比较整个 shown_sha。
- 普通半径变化的测试保留确认；前提材料改变、checks 文件变化则拒绝。
- 无 materials.yaml 时，“资料找到”改为 unknown，而非 no；自动未知项记录也包含 found。
- 保留“无界限”状态。

### 3.6 数值警告边界

没有重写 Python 警告拦截器。采用上轮交接允许的说明方案：结果行写 numeric_warning_scope=python_filters；比较页与确认页均说明检查脚本能通过过滤器屏蔽 RuntimeWarning，零条警告不证明计算有效。原 p31 屏蔽探针尚未重跑，也未新增针对说明文案的独立断言。

## 4. 测试实际执行到哪里

| 顺序 | 命令 | 实际结果 | 随后处理 |
|---|---|---|---|
| 1 | `SAOS_INSTANCE=verify uv run pytest -q tests/test_candidates.py tests/test_demand_layers.py tests/test_model_report.py tests/test_workflow.py --tb=short` | 132 通过、11 失败，16.25 秒 | 多数失败为规则改变后的旧断言；另定位到并发封存丢记录，已修改代码和 fixture |
| 2 | `SAOS_INSTANCE=verify uv run pytest -q tests/test_selection_evidence.py tests/test_candidates.py tests/test_demand_layers.py tests/test_model_report.py --tb=short` | 81 通过、2 失败，29.05 秒 | 两例对 FakeBackend 不存在的 optimize 做 monkeypatch，已加 raising=False |
| 3 | `SAOS_INSTANCE=verify uv run pytest -q --tb=short` | **457 通过、2 失败、11 跳过**，90.89 秒，126 条弃用警告 | 两个失败均为模型页文案还断言“结构不成立”/structure not supported；已改成“结构淘汰”/structure ruled out，**未复跑** |
| 4 | `SAOS_INSTANCE=verify SAOS_RENDER_DRAWIO=1 uv run pytest tests/test_docs_sync.py -q -s` | **2 通过、1 失败**，3.49 秒；PNG 已生成 | 文档脚本把 cli_help.py 普通字符串中的换行写坏，导致 SyntaxError；已改回转义换行，**未复跑** |

两个全量失败的测试名：

- `tests/test_model_report.py::test_four_sections_appear_in_both_languages`
- `tests/test_model_report.py::test_selection_warnings_and_the_other_candidates`

文档检查失败的测试名：`tests/test_docs_sync.py::test_every_saos_command_in_the_docs_exists`。

全量结束后继续改了文档、帮助和前端文案，所以 457 通过不能视为当前最终工作树的结果。暂停时没有待轮询的 pytest 会话；上述命令均已返回结束结果。

`tests/test_selection_evidence.py` 有 15 个参数展开后的案例，覆盖凑数、短优化不足以证明结构不可达、独立界限、封存前篡改/删除、prepare 改动/删除、并发选型、旧比较不豁免、材料身份不一致、模型删字段冒充旧格式、普通变量与计算前提变化。`tests/test_demand_layers.py` 本轮新增 3 例。

没有执行真实 OpticStudio 测试。上轮标注 NOT RUN 的 OpticStudio 阶段入口版本比较仍未补测。也没有重跑原 verifier 的整批探针。

## 5. 文档与 UI：有改动，尚未同步完成

已修改 `skills/saos/SKILL.md`、`skills/saos/demand.md`、`docs/cli.md`、`src/saos/cli_help.py`、`docs/workflow.drawio`、`src/saos/ui/static/app.js`。DESIGN.md 更新了结构淘汰原则，并记录本轮接受的决定。

- drawio 的 lens describe、lens approve、notes 节点补入四节、next_budget、shown_sha 和未知项；describe 节点高度从 80 改为 110。
- UI 的建模副文字改为“候选比较 · 四节确认页”；模型确认默认副文字改为“方向 · 取舍 · 未知项”；设计副文字缩短，避免“选定”被截掉；节点增加 SVG title 保留完整副文字。
- UI 中文返回边文字已改为“结构或计算前提改变”；英文仍为 revise model，**尚未同步**。
- drawio 已渲染到 `C:\Users\leahd\AppData\Local\Temp\saos-workflow.png` 并用图片工具查看。建模确认区域可见；没有逐条放大检查返回边，也没有检查全部底部区域。临时 PNG 可能被清理。
- **没有打开流程窗口做浏览器视觉检查**；没有检查窄屏、键盘、语言切换和加载失败状态。
- 前端技能的静态审计命令没有执行；已读取 frontend-design、frontend-design-premium、canonical-ui-resolution 和 verification-checklist。

已发现的具体同步缺口，不要只看文末新增说明便标完成：

1. `skills/saos/SKILL.md` 主体第 3 节旧长段仍写 structural 只要推翻条件失败即可，还写零优化初筛可直接排除结构；与文末新增说明、当前实现矛盾。应重写原段，避免靠追加条款覆盖旧规则。
2. `candidates.py` 的若干旧 docstring、测试注释仍描述“登记的硬条件失败就算结构不成立”；要更新。部分旧测试名仍声称“升级前比较仍合格”，实际 fixture 已补 selection_schema=2；需区分历史需求哈希兼容与旧比较证据不豁免。
3. drawio 结构比较菱形 a8 仍只写“≥3 候选比较、选定一个”，未写计入规则和独立界限；尚未完成四处措辞一致。
4. 流程图仍只有原来的返回边。是否需要新增 design → modeling 的计算前提失效边尚未核对；如新增，每条边必须单独走 lane。`app.js` 动态 viewBox 宽度为 330，不要只看 index.html 的初始宽度 300。
5. UI exploration 仍主要显示计划候选数，未明确区分“已跑过多少候选”和“多少实质结构计入资格”；需要评估是否会误导。
6. DESIGN.md 新增决定目前追加在末尾；应检查放置位置与原“用户决定与取舍”的组织是否一致。不要把命令参数、阈值或本轮测量数据放入 DESIGN.md。

## 6. 下一轮从这里继续

1. 先看 `git status` 和本文，不覆盖上轮已有改动。HEAD 为 `b4c0851`；暂停前跟踪文件整体 diff 为 31 个文件、+2554/−367 行，未跟踪文件另计；这些数字包含上轮，不是本轮净改动量。
2. 先跑修正后的失败测试和 docs_sync，确认最后的文案断言、cli_help 字符串确已修好；暂停后没有新的通过证据。
3. 复核本轮实现，尤其是：
   - structure bound 的 BAD_ARGS 校验是否覆盖错误类型；`refuted_by.check` 如果不是字符串，带 bound 时可能在字典查找处变成 INTERNAL_ERROR，尚未验证。
   - 最后一段优化内部取消与 row.cancelled 的一致性；当前只在段开始处设置 cancelled，不能未经检查就宣称所有取消路径都不会计 structural。
   - 非 selected 状态生成的旧确认页后来再选择候选时，approval_context.scan 为空；之后 round 的材料前提检查是否完整，尚未专项测试。
   - `Job.seal` 的多进程锁和 protocol 的 exclusive 硬链接写入需要跨进程回归；目前新增并发选择测试使用线程，真实多 worker 扫描已进入全量测试。
   - 原 prepare 被改会使选型失效，已完成比较的报告如何提示失效尚未专项检查。
   - 旧需求兼容逐条扫描存档全文的开销没有测量；不要先做无依据的缓存优化。
4. 把第 5 节的文档矛盾消除，并补“旧 stop 后只改描述、重新确认，仍不能记新轮次”的实际入口回归；目前新增需求测试只核对读到的 stop 和版本，未走到 round.record 的拒绝路径。
5. 补模型批准后篡改再记轮次、p31 警告屏蔽说明的回归；逐项对照上轮探针，而非仅复跑现有单元测试。
6. 做真实浏览器检查和 drawio 渲染检查。需要自建 UI 服务时只停止自己启动的 PID，不复用旧 daemon 去改变它当前 dirty 镜头。
7. OpticStudio 入口版本比较优先先加测试替身覆盖，再决定实机测量。若实跑，使用自建 standalone 会话，只关闭自建实例；本轮没有为此增加或修改测试。
8. 针对性检查通过后再跑非 ZOS 全量，记录最终结果、未覆盖项和副作用。G04 注释/普通变量、G05 累计预算、G06 prescribed 越界仍不能声明全部通过；预算与指定范围的实现缺口要留在综合计划中。

## 7. 临时文件与暂停状态

- 本轮文档批量更新脚本：`C:\Users\leahd\AppData\Local\Temp\saos_selection_docs.py`。第一次 heredoc 因命令截断未闭合，实际只产生不完整脚本；检查尾部后补齐并执行过。**不要直接重跑**：它包含追加文档的操作，不保证幂等。
- 上轮探针仍在 `C:\Users\leahd\AppData\Local\Temp\saos-verify\probes\`，本轮只阅读部分脚本；仓库新回归不依赖该目录。
- pytest 临时任务在系统临时目录，本轮没有新增常驻 verify 工作任务，也没有新服务需要交接关闭。
- 用户要求暂停后，只进行了交接所需的只读状态核对与文档写入，没有继续修代码或跑测试。
- **未提交、未推送；当前代码与文档仍需复查和回归，不可标为已验收。**
