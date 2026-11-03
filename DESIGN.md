# saos 设计与术语

saos（simulate any optical system）给 agent 提供以 **Zemax OpticStudio**（2024 R1 Premium + ZOS-API standalone）为交付数值权威后端的光学设计与仿真工具；需求阶段与设计轮次可在不占许可的 rtc 实例上计算，交付前由 OpticStudio 复测（ADR-0016、ADR-0017）。支持序列、非序列与多重结构系统，从用户需求出发完成建模、仿真、优化并交付有实测依据的报告。

相关文档按需查阅，不必在每次改代码前通读：
- 任务执行工作流：`.agents/skills/saos/SKILL.md`（入口，按阶段路由到 `references/`）
- 需求单模板与权重/无效值约定：`.agents/skills/saos/demand.example.yaml`
- 命令参数与输入输出格式：`uv run saos --help` 及各子命令 `--help`（英文 `--help` 是唯一详细说明，不另维护易漂移的中文命令速览）
- 历史决策与分阶段计划：`docs/adr/`、[docs/plans/](./docs/plans/README.md)

## 决策边界与底线

Agent 负责光学工程判断（初始结构选型、评价函数构造、变量放开、优化算法选择、扰动诊断与参数扫描）。系统只固定少数硬关卡与交付底线，在边界之内 Agent 应自主推进，无需每步请示。

### 两种流程：参与与自主

每个任务建立时选定一种流程（`saos job create --mode`，ADR-0021）：**参与模式 (interactive)** 用于从头设计，以下硬关卡由用户在工作流窗口里点确认（窗口用不了时才用 `--quote` 记用户原话），也可以在关卡处要求修改；**自主模式 (autonomous)** 用于做题和测试，agent 带理由自行通过关卡（`--reason`）、把阻塞问题按假设作答、按证据自行停止，关卡背后的内容检查照做，报告标明这些决定未经用户确认。自主任务只能由用户在窗口里改为参与，反过来不行。

### 何时必须停下等用户（硬关卡）

1. **确认或修改需求实质内容（`saos demand confirm`）**：首次确认需求单，或改动条目中描述以外的字段、结构要求或终止规则（清单以 `demand.example.yaml` 文件头为准，判定在 `demand.digest`；只改描述、总结、问题或语言无需重新确认，只改依据时已测数值仍可跨轮比较）。达不到目标时如实报告差距，绝不偷偷放宽需求。
2. **确认或变更模型结构（`saos lens approve`）**：首次确认建模说明，或改变**结构指纹**：本设计制造面的结构（面型、介质类别、孔径类型、光阑等）与系统设置、**环境面**（样品、浸没介质、外购件）的全部几何与材料，以及**判定条件**（多重结构骨架、温压、切趾、偏振等）。曲率半径、厚度、非球面系数、玻璃牌号等数值属于设计变量，调整不触发重新确认。指纹的完整定义以 `src/saos/model_gate.py` 模块说明为准，给 Agent 的清单在 `references/model.md`。
3. **按用户决定终止任务（`saos stop`）**：用户根据已有证据决定停止。
4. **存在未决阻塞问题或目标差出数量级**：需求单中有 `blocking: true` 的待确认问题，或结构上限导致核心达标线差好几倍时，带实测证据与备选方案回到用户，不靠长时间盲目优化碰运气。

### 交付底线

- **实测依据**：所有交付数值来自 OpticStudio 的实际追迹、操作数或分析结果，记录后端版本与命令/脚本。交付之前的轮次在 rtc 上计算和判定（每轮记 `backend` 与未对账项）；zemax 做交付前的最后优化，在同一状态下记一轮，报告与公差只用这一轮的 OpticStudio 数值。逐轮交给 zemax 复测（`saos round --confirm`）在需求访谈时与用户确认，默认不做（ADR-0020）；假后端仅用于工具测试，不是光学实测。
- **可交付条件**：待制造零件不含模型玻璃（模型玻璃仅用于探索，交付前须换成真实牌号；环境面有数据来源即可）、满足全部常量与变量范围、判定未出错、零件无结构交叠（建模时即给定机械半口径，按零件外缘或显式孔径检查）。玻璃来源与状态（如 `Obsolete`）随轮次记录并写入报告。
- **判定只读且物理自洽**：判定脚本不得改动镜头（写回原值时后端 `1e-12` 末位舍入不算改动，详见 `saos round --help`）。物理上不成立的状态（零件相交、成虚像、光线丢失、只量幸存光而非整束光）必须判为无效（`inf`），不能当作可权衡的线性罚分。判定代码（`checks/` 及引用的配方库与 `saos.kit`）改动后，`saos round` 自动用新判定重测此前最佳轮次（`rebase`）再作比较（ADR-0010）。
- **报告与语言规范**：面向用户的报告一律生成 `.html`（中英双语，按 `saos report --help` 的 `--lang` 选择）；给 Agent 读的内容（技能、配方、需求单格式、CLI 报错与提示）用英文；仓库文档与代码注释用中文；访谈记录保留用户原话。

## 架构与关键设计取舍

以下设计决定反映了用户的明确取舍；2026-10-23 用户授权改回 ZOS standalone（ADR-0013）：

- **常驻后台进程**：后台进程在内存中持有当前镜头状态和两个后端（rtc 立即可用；OpticStudio standalone 会话在第一次需要时建立，之后一直开着，ADR-0018），命令行只转发请求（ADR-0002）。并发评估（`saos scan`、`ctx.map`）复用常驻 Worker 池（空闲 10 分钟关闭并释放许可）。支持 `--detach` 分离运行：扫描在后台线程用镜头快照和 Worker 池运行，主实例照常响应其他命令（ADR-0009）；`exec`/`opt`/`check`/`round` 的分离运行由独立子进程等待主实例完成并写入 `runs/`。
- **各阶段的计算后端**（同一后台进程按 `saos.ui.stage` 的阶段切换，ADR-0018）：
  - 需求访谈与确认、建模（起点生成、初筛）、模型确认、设计轮次用 rtc（ADR-0016：只有准入项可单独支撑可行性结论；ADR-0017：变量、评价函数、带精确约束的局部优化、Hammer 与全局搜索、判定和轮次；ADR-0020：建模与模型确认也归 rtc）。
  - 交付（zemax 最后优化、交付轮测量、公差、报告）用 ZOS，许可不足时报告失败，不改用 rtc；rtc 出模型页截面图之前，`lens describe` 也要 ZOS（ADR-0020）。许可计数见技能 `references/session.md`。
  - `saos daemon use rtc|zos` 固定后端只在当时的流程阶段内有效，阶段一变自动回到按阶段选择；rtc 上没有 `ctx.system`，检查与脚本经 `ctx` 读写，rtc 算不了的判定条目列入 `backend_unsupported`（ADR-0020）。
  - 切换时镜头经 `.zmx` 交接后逐值比对，不一致仍交给 rtc、差异记为未对账（2026-10-30 用户决定）。
  - 在 OpticStudio 上的脚本可用 rtc 内存副本 `ctx.fast()` 探索（ADR-0014），结果推回后先用 OpticStudio 局部优化按评价函数收尾，再判定和记录轮次（2026-10-28 用户决定）；专家类用 rtc 做非球面排名与玻璃坐标（ADR-0015）。
- **ZOS 原生镜头权威保存**：`.zmx` 由 OpticStudio 保存，包含当前处方、求解与评价函数；rtc 实例按 OpticStudio 24.1 的行格式写回它建模的部分（处方、变量、评价函数行），其余行原样带过去，没见过的行拒绝加载；JSON 处方只是建模输入。带 `.optiland.json` 的旧文件不可加载或覆盖，旧双后端任务不追加新计算证据。迁移时新建任务，按原处方重建或导入另存的 ZOS 原生镜头，重新确认与实测；不继承旧指标。
  - **脚本编辑只经公开 ZOS-API**：`ctx.merit_add`、`ctx.solve`、`ctx.insert_surface`、`ctx.change_surface_type` 直接改当前镜头；评价函数行经 `.MF` 文件整批载入并另存回读核对（1501 行由逐行 9.8 s 降到约 65 ms，2026-10-27 实测）。不调用 OpticStudio 未公开的内部组件（如 `ZemaxEngine.dll`）：接口无文档、随版本变化，且绕开许可计数。
- **起点从已有设计生成，不设候选配额**：结构未由用户指定时，由 `saos.starts` 从起点库或专利库（`library/patents`，按规格检索）按需求缩放生成并给出一阶与三阶像差预判，以相同短期预算初筛；明显合适的形式可直接选用并在确认页写明理由，无需凑候选数量。
- **关卡只留在昂贵处，封存仅用于发现意外**：硬关卡仅设在深度优化前（需求与模型确认）；工具写入文件后记录哈希（`seals.jsonl`）以检测意外篡改或删除（见 `saos job verify --help`），缺失或过期的对比记录只作警告显示，不为防 Agent 规避流程叠加上锁机制。
- **目标方向与终止规则**：
  - 越小/越大越好的目标用 `minimize` / `maximize`，按离理想值的距离计入目标差距（Shortfall），达标后继续改进；不要把“越小越好”写成 `≤ 0` 这种永远达不到的达标线。
  - 局部优化只收敛到局部极小，每轮须声明手段层级（`local`、`variables`、`materials`、`global`、`structure`）。层级记录试过什么而非强行按序执行，诊断表明无效的层级可用 `saos rule-out` 带证据排除。终止状态分四种：达成（`achieved`）、达到轮次上限（`limit`）、无法再优化（`exhausted`，允许的手段均已尝试或排除仍无明显进步）、按用户决定停止（`stopped`）。
- **Zemax 优化器与玻璃在产目录**：
  - OpticStudio 上的局部优化使用 Zemax DLS 或正交下降；`auto` 在序列系统选 DLS，非序列系统选正交下降。Hammer 与 Global 使用原生工具；rtc 上是 rtc 自己的约束 DLS、Hammer 与全局搜索（ADR-0017）。外层 Brent、Nelder–Mead、Powell 搜索仍可调用 ZOS 测量。预算与取消共用 `saos.budget`，原生调用返回前不承诺硬实时中断。
  - 玻璃替换默认使用按厂家分开的在产目录 `SAOS_CDGM_INPROD`（另有 `SAOS_NHG_INPROD`，见 `library/glasscat/README.md`），防止全局搜索或 Hammer 换成停产牌号，并避免两家同名牌号参数不同带来的混淆。
  - `ctx.AsphereExpert` / `ctx.GlassExpert` 是当前 ZOS 或 rtc 实例镜头的非球面选面与材料搜索类（`saos.ctx.experts`）。只对纯序列、单结构镜头工作；分析恢复入口，接受态标未保存，保存仍经 ctx。项目物理过滤、Q 型列映射和持久斜率约束必须明确提供，不能用缺失数据猜测。接口、算法与待验收项见 `docs/plans/optical-experts.md`。
- **统一处方骨架与非序列支持**：序列与纯非序列系统（`"mode": "non_sequential"`）共用同一套处方入口、建模确认、结构指纹、判定和轮次；saos 只校验系统设置、通用面属性和多重结构骨架，具体面型和参数按 OpticStudio 原名透传。非序列能力随真实任务逐步扩展。
- **窗口、资源与未保存修改保护**：
  - 用户通过流程图窗口（`saos ui`）查看进度、当前镜头视图（`view.json`）与当前镜头指标（`metrics.json`）及每轮导出的点列图/MTF（`viz.json`）。后台进程在镜头改动后自动刷新；rtc 的目录指标另由专用进程读取完整镜头快照，在优化进度回调与空闲刷新时计算，只保留最新待算快照，正式判定仍走 check/round（ADR-0022）。追加展示选择存在 `metric_selection.json`，不参与判定；窗口尊重用户关闭选择，仅在需要查看的节点通知置顶且不抢键盘焦点；前端随包分发，不引入 Node 构建链。
  - 显性调试（Extension 模式连接桌面 OpticStudio 窗口）为可选（ADR-0011）。OpticStudio 长操作期间暂停实时测量，避免与原生工具竞争 API；rtc 的目录指标在独立副本上计算，不适用这一限制（ADR-0022）。替换窗口内容前一律先将窗口中的未保存状态存为副本（`lenses/captures/`），不替用户丢弃修改。
  - 计算后端固定版本（见 [README](./README.md)）并用双精度；长时间进程降低优先级并默认预留 2 个逻辑核，许可证并发预算计入用户已打开的窗口（见 `saos --help` 与 `saos opt local --help`）。
- **代码三层结构与上收原则**：分为固定 CLI 命令、库函数（`saos.kit`、`saos.search`、`saos.starts`、配方库）和任务内脚本（ADR-0007）。同一段代码在第二个任务中被复用时即上收为库函数或配方草稿，上收前后同一批镜头的计算数值必须逐项一致。测试只测工具，不测 Agent 的设计能力。
- **模块分层**（2026-11-02 用户决定，参照 ripgrep 各 crate 单向依赖）：L0 基础约定（`protocol`、参数键与追迹结果 `lensdata`、`geometry`、`prescription`、文件与目录、共享参考库的只读入口 `library`）→ L1 计算后端（`saos.backend`，OpticStudio 的参数读写在 `backend/zos_params.py`）→ L2 测量库（`saos.kit`、指标目录 `saos.metrics`、微光学测量 `saos.micro`、`saos.fast`、`saos.search`、专家类，以及只含测量函数的配方库如 `saos.recipes.fiber`）→ 任务、需求、轮次、扫描、worker 池、后台进程与窗口。配方只保留布局约定，可拆开的慢测量经 `ctx.spare_map` 借空闲许可证，不直接碰 worker 池。下层不导入上层，L0–L2 之间也不成环（只有完全落在同一包内的环除外，经过同包模块绕回的跨包环照样算），函数内的延迟导入和相对导入也算，由 `tests/test_layers.py` 检查；最上层相当于 ripgrep 的 core，是把流程粘起来的胶水，内部模块可以互相引用；测量库只经 `Backend` 协议调用后端，只有 OpticStudio 能做的功能由后端自己报错。

## 核心术语对照

统一使用下表中的中英文名称，避免括号内列出的含糊叫法：

| 中文术语 | 英文 / 代码标识 | 含义与边界（*避免使用的叫法*） |
|---|---|---|
| **用户需求** | User Need | 用户用日常语言描述的使用目的和约束（*勿单称“需求”*） |
| **需求单** | Demand (`sources/demand.yaml`) | 经访谈确认的结构化文件，含已知常量、可调变量、目标、待确认问题和访谈总结（*勿称：光学规格、spec、harness*） |
| **已知常量** | Constant | 设计不能改变的外部条件，可带判定方法；不满足的轮次不可交付（*勿称：约束、固定参数*） |
| **可调变量** | Variable | 设计自由度及上下限范围；未知的客观实物事实属于待确认问题而非变量（*勿与 Zemax 优化变量混淆*） |
| **目标** | Objective | 需实测的性能量，含达标线（`must`）、优化方向（`minimize`/`maximize`）和期望值（`wish`）；用户目标默认权重 1，Agent 补充目标默认 0.3（*勿称：性能指标、约束*） |
| **待确认问题** | Open Question | 需用户回答的问题；标为 `blocking` 时阻止优化和记录轮次（*勿称：待办、pending inputs*） |
| **判定方法** | Check Method (`checks/`) | 计算带界限常量、变量和目标的方式；同次判定共享缓存且只读（*勿称：验收口径*） |
| **重测基准** | Rebase (`rebase`) | 判定代码改动后，用新判定重测此前最佳归档镜头的记录；数值一致则延续比较窗口，不一致则开启新窗口（*勿称：reverify、回归测试*） |
| **复测** | Confirmation (`confirm`) | 在 OpticStudio 上重测 rtc 记下的一轮（`saos round --confirm`，逐轮复测在需求访谈时约定，默认不做，ADR-0020）；不单独成轮，其数值替换那一轮的数值，结论与 rtc 不同则回到设计（*勿与重测基准混淆*） |
| **设计任务** | Design Job (`jobs/<job>/`) | 从一份用户需求到交付的一次完整设计及其文件目录（*勿称：项目、会话、run*） |
| **运行实例** | Instance (`work/<instance>/`) | 一个后台进程及其独占运行目录，不同实例可并行（*勿单称“实例”指代 OpticStudio 进程*） |
| **计算后端** | Backend (`zos`, `rtc`) | 后台进程里持有镜头并完成计算的一方：`zos` 是 OpticStudio 会话，`rtc` 做交付之前的全部计算，`zos` 做交付前的最后优化与交付轮测量（ADR-0020）；`fake` 仅供工具测试 |
| **OpticStudio 实例** | OpticStudio Instance | 占用一个 ZOS-API 许可的 OpticStudio 进程（桌面窗口、standalone 会话或 Worker）（*勿称：license 槽*） |
| **原生镜头** | Native Lens (`.zmx`) | OpticStudio 保存的完整镜头，作为加载、工作与轮次归档的权威文件 |
| **环境面** | Environment Surface | 声明为不由本设计制造的面（样品、介质、外购件），须有数据来源，使用模型介质不阻止交付（*勿称：背景介质*） |
| **模型玻璃** | Model Glass | 仅用 `nd`/`Vd`（及 `ΔPgF`）描述的探索用材料，由 OpticStudio 的模型玻璃求解表示，不可直接交付（*勿称：虚拟玻璃、理想材料*） |
| **建模说明** | Model Description | 优化前供用户核对的 HTML 页面：各面实物含义、材料来源、一阶分析、有效性检查与下一步预算（*勿称：模型报告*） |
| **模型确认** | Model Approval | 用户对建模说明的认可记录，绑定结构指纹（*勿称：模型审批*） |
| **当前镜头视图** | Live View (`view.json`) | 流程图窗口实时展示的镜头布局、Lens Data 与结构交叠检查（*勿称：预览*） |
| **当前镜头指标** | Live Metrics (`metrics.json`) | 带视图版本的评价函数值、需求项与追加展示指标；rtc 目录指标在完整快照副本上计算，其他判定保留正式测量值，OpticStudio 使用原有限时刷新；不是正式证据 |
| **结构交叠** | Interference | 零件在几何上相交或穿过（如负边厚、负空气间隔），按零件外缘或显式孔径计算（*勿称：干涉、碰撞*） |
| **调试轮次** | Round | 一次优化、换材或改结构后记录的检查点，含镜头快照、全项实测值与手段层级（*勿称：迭代、调整轮次*） |
| **目标差距** | Shortfall | 各带界限项超出界限的量与 `minimize`/`maximize` 距离归一化加权之和，用于跨轮比较（*勿与优化评价函数 merit 混淆*） |
| **手段层级** | Strategy Tier | 轮次所用的手段级别：`local`、`variables`、`materials`、`global`、`structure`（*勿称：优化阶段*） |
| **无法再优化** | Exhausted (`exhausted`) | 自上次明显进步以来，允许的各手段层级均已尝试或带证据排除仍无进展的终止状态（*勿称：停滞、卡住*） |
| **扫描 / 扰动诊断** | Scan / Probe (`saos scan`) | 在 Worker 池上并发评估候选结构（`candidates`）、参数网格、单变量扰动灵敏度（`probes`）或公差（`tolerance`）（*勿单称 map 或 sweep*） |
| **交付报告** | Delivery Report (`report/index.html`) | 汇总实测值、达标状态、材料与计算后端的 HTML 报告；只把满足全部可交付条件的轮次称为交付镜头，没有时写明原因并只作诊断展示差距最小的轮次（*勿称：验收报告*） |
| **诊断量** | Diagnostic | 随报告输出、帮助解释结果但不决定是否达标的光学量，如 Strehl 比（*勿称：附加指标*） |
| **隐式运行 / 显性调试** | Headless Run / Explicit Debugging | 默认无界面运行 vs 通过 Interactive Extension 连接用户桌面 OpticStudio 窗口调试（*勿称：后台模式 / 窗口模式*） |
| **配方** | Recipe (`.agents/skills/saos/references/recipes/<系统>.md`) | 针对一类光学系统、由入口技能在访谈确定系统类别后路由过去的参考文档与配套工具库，从真实任务中提炼（*勿称：模板、插件、子技能*） |

### 显微物镜配方专用术语

以下术语仅在显微物镜配方（`saos.recipes.microscope`，详见 [光学规格格式与验收口径](./docs/recipes/microscope/optical-spec-and-acceptance.md)）中使用，与通用需求单概念区分：

- **光学规格 (Optical Spec)**：该配方验收评估（`saos microscope accept`）的输入文件，含**工作条件 (Operating Condition)**（NA、倍率、波长、盖玻片，只核对设置）、**结构界限 (Structural Limit)**（工作距离、通光直径、最小厚度等上下限）和性能指标（含待实测校准的**待标定阈值 Uncalibrated Threshold**）。
- **光学参量**：**物镜视场数 (OFN)**（一次像面视场直径，mm）、**物方景深 ($\delta_{ob} = n\lambda / (2\text{NA}^2)$)**、**校正等级 (Correction Class)**（消色差 / 平场消色差 / 平场复消色差）、**参考筒镜 (Reference Tube Lens)**（评估无限远物镜用的理想透镜）、**平场比 (PFR)**（按 ISO 19012-1 目视焦深判据计算的清晰直径与 OFN 之比，平场要求 $\ge 0.85$）。
- **验收评估与差距**：在加入盖玻片与参考筒镜、清除渐晕与遮拦的**验收模型 (Acceptance Model)** 上，移动物距对焦至**验收焦面 (Acceptance Focus)** 后进行评估，输出**验收报告 (Acceptance Report)**（`eval/`）。其**单项差距 (Item Shortfall)** 与不加权的**规格差距 (Spec Shortfall, `spec_shortfall`)** 仅用于配方内同结构状态比较（如**受控扰动 Controlled Perturbation** `saos microscope perturb` 前后），通用轮次比较仍用需求单的目标差距。
- **初始结构库 (Starting Design Library)**：收录通过**复现校验 (Reproduction Check)**（偏差在印刷四舍五入的**舍入包络 Rounding Envelope** 内）的专利实施例；由 `saos microscope library verify` 导出使原结构规格差距为 0 的**基准规格 (Benchmark Spec, `benchmark.yaml`)** 用于回归测试。

## 仓库目录

文件放置位置由 `tests/test_repo_layout.py` 自动检查：

| 位置 | 放什么 |
|---|---|
| 根目录 | 只有 `README.md`、`DESIGN.md`、`TODO.md`、`AGENTS.md`、`pyproject.toml`、`uv.lock`、`.gitignore`，以及 `.agents/`（技能源文件）和 `.claude/skills/saos`（指向 `.agents/skills/saos` 的链接）；`ref`（联接到 zea 参考资料）和 `work/` 不入库 |
| `src/saos/` | 核心工具代码。不依赖任何配方，仅 `cli.py` 注册配方命令 |
| `src/saos/recipes/<配方>/` | 配方配套代码 |
| `.agents/skills/saos/` | 光学设计工作流技能：`SKILL.md` 入口（定位阶段、各阶段关卡与出口）、`references/` 按阶段的细节与按时机读取的通用参考（`session.md` 会话与长运行、`scripting.md` 脚本与 rtc 实例、`rtc-copy.md` OpticStudio 上的 rtc 副本、`micro.md` 微光学测量、`files.md` 任务目录）、`references/recipes/<配方>.md` 按《配方编写规范》（`references/recipes/README.md`）编写的配方 |
| `docs/` | 跨任务文档：`workflow.drawio`（工作流图）、`adr/NNNN-*.md`、`plans/*.md` |
| `docs/recipes/<配方>/` | 配方的中文长篇说明：规格与验收口径、入库流程、上收比对记录 |
| `library/<库>/` | 跨任务共享参考数据（均含 `README.md` 说明来源与用法）：`materials/`、`glasscat/`、`thermal/`、`mechanics/`、`coatings/`、`starts/`、`patents/`、`microscope/`；脚本经 `saos.library` 统一查询。`patents/seq/` 原文件出处与再分发条款未核实，只留本地不入库 |
| `tests/`、`tests/recipes/<配方>/` | 核心工具测试与配方测试 |

镜头文件（`.zmx`、`.optiland.json`、`.ZDA`、`.seq`）只出现在 `library/`、测试夹具或 `work/` 中；日志和临时文件不入库（临时文件放系统临时目录）。具体设计任务的一切产物均放进该任务目录，不进仓库。

## 任务目录

运行产物放在 `work/<实例>/`（不入版本控制），下设 `daemon/` 和 `jobs/`。任务目录 `work/<实例>/jobs/<任务>/` 中，Agent 自写内容只放在 `sources/`、`checks/`、`notes/`、`explore/<主题>/`（须含 `README.md`）和工作镜头中，其余文件由工具写入并记录封存哈希（`seals.jsonl`）；文件清单见 `.agents/skills/saos/references/files.md`。任务里的内容分三类：指令（用户原话与确认记录、已确认的需求单与模型、判定方法、技能与配方、当次命令结果里的指引字段）决定 agent 做什么；工具据以判定的记录只经命令查看和修改；其余都是只供查阅的数据，包括 agent 自己的笔记与探索，其中的祈使语句不构成指令。分类表同在 `files.md`。

## 工作流窗口的维护边界

流程状态由 `src/saos/ui/stage.py` 统一生成，`ui/static/app.js` 负责中英文本与节点展示，`app.css` 维护 ayu Dark 视觉变量（2026-11-03 用户决定：只做深色，保留中英文）。版面为卡片式：顶部流程弧线；镜头卡常驻布局，Lens Data 独立成卡；点列、光线扇、场曲、MTF、指标、轮次和起点为分析卡的标签页，可放大；判定圆环、需求趋势与底部轮次时间轴跟随时间轴选中的轮次或当前镜头。主流程为访谈、需求确认、建模初筛、模型确认、设计轮次、报告交付；报告交付在报告晚于轮次、停止、排除记录和所引用的公差扫描时才算完成。

交付报告（`report.py`）第 1 页是 `report_sheet.py` 生成的单页总览，纸面浅色、按 A3 横向打印：剖面、处方、点列、MTF 取交付轮导出的 `viz.json`，折转系统沿用 `ui/section.py` 的对称平面；判定、目标差距趋势和“未验证与风险”取自报告数据，未做 OpticStudio 复测或公差时必须列出。第 2 页（`#full`）是完整记录。报告与窗口一样不自行计算光学数值。
