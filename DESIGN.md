# saos 设计与术语

saos（simulate any optical system）给 agent 提供以 **Optiland** 为默认计算后端、**Zemax OpticStudio**（2024 R1 Premium + ZOS-API）为最终验证与深化标准的光学设计与仿真工具。支持序列、非序列与多重结构系统，从用户需求出发完成建模、仿真、优化并交付有实测依据的报告。

相关文档按需查阅，不必在每次改代码前通读：
- 任务执行工作流：`skills/saos/SKILL.md`
- 需求单模板与权重/无效值约定：`skills/saos/demand.example.yaml`
- 命令参数与输入输出格式：`uv run saos --help` 及各子命令 `--help`（英文 `--help` 是唯一详细说明，不另维护易漂移的中文命令速览）
- 历史决策与分阶段计划：`docs/adr/`、[docs/plans/](./docs/plans/README.md)

## 决策边界与底线

Agent 负责光学工程判断（初始结构选型、评价函数构造、变量放开、优化算法选择、扰动诊断与参数扫描）。系统只固定少数硬关卡与交付底线，在边界之内 Agent 应自主推进，无需每步请示。

### 何时必须停下等用户（硬关卡）

1. **确认或修改需求实质内容（`saos demand confirm`）**：首次确认需求单，或改动数值、范围、判定、权重、单位、依据（纯文字、描述或总结润色无需重新确认；只改依据时已测数值仍可跨轮比较）。达不到目标时如实报告差距，绝不偷偷放宽需求。
2. **确认或变更模型结构（`saos lens approve`）**：首次确认建模说明，或改变**结构指纹**。结构指纹包含：
   - 本设计制造面的面型、介质类别（空气、玻璃、反射镜）、孔径类型、光阑位置、菲涅尔基底是否为平面、是否倾斜偏心，以及系统级视场/波长设置（允许在末尾追加采样点）；
   - **环境面**（样品、浸没介质、外购件）声明及其材料、膜层、半径、圆锥、孔径和倾斜偏心；
   - **判定条件**：多重结构的结构数与操作数、TEMP/PRES 值、环境温压、切趾、偏振、膜层文件和无焦单位。
   - 结构指纹之内的曲率半径、厚度、圆锥与非球面系数、倾斜角、孔径大小、玻璃牌号替换、设计面的膜层及多重结构间距值均属于设计变量，日常调参与更新确认页不触发重新确认。
3. **进入 OpticStudio 阶段（`saos zos begin`）或终止任务（`saos stop`）**：用户认可 Optiland 阶段的报告后，才启动 OpticStudio 重建复测并继续优化；或由用户根据证据决定停止。
4. **存在未决阻塞问题或目标差出数量级**：需求单中有 `blocking: true` 的待确认问题，或结构上限导致核心达标线差好几倍时，带实测证据与备选方案回到用户，不靠长时间盲目优化碰运气。

### 交付底线

- **实测依据**：所有评估与交付数值均来自计算后端（Optiland 或 OpticStudio）的实际追迹、操作数或分析结果，记录产生它的后端与命令/脚本。未经 OpticStudio 阶段复测的报告必须标明数值仅由 Optiland 算出；最终交付以 OpticStudio 实测为准。
- **可交付条件**：待制造零件不含模型玻璃（模型玻璃仅用于探索，进入 OpticStudio 前须换成真实牌号；环境面有数据来源即可）、满足全部常量与变量范围、判定未出错、零件无结构交叠（建模时即给定机械半口径，按零件外缘或显式孔径检查）。玻璃来源与状态（如 `Obsolete`）随轮次记录并写入报告。
- **判定只读且物理自洽**：判定脚本不得改动镜头（写回原值时后端 `1e-12` 末位舍入不算改动，详见 `saos round --help`）。物理上不成立的状态（零件相交、成虚像、光线丢失、只量幸存光而非整束光）必须判为无效（`inf`），不能当作可权衡的线性罚分。判定代码（`checks/` 及引用的配方库与 `saos.kit`）改动后，`saos round` 自动用新判定重测此前最佳轮次（`rebase`）再作比较（ADR-0010）。
- **报告与语言规范**：面向用户的报告一律生成 `.html`（中英双语，按 `saos report --help` 的 `--lang` 选择）；给 Agent 读的内容（技能、配方、需求单格式、CLI 报错与提示）用英文；仓库文档与代码注释用中文；访谈记录保留用户原话。

## 架构与关键设计取舍

以下设计决定反映了用户的明确取舍，修改相关行为前须先与用户确认：

- **常驻后台进程与进程内 Optiland**：后台进程在内存中持有当前镜头状态（进程内 Optiland 实例，以及 `saos zos begin` 后的 OpticStudio 会话），命令行只转发请求（ADR-0002）。并发评估（`saos scan`、`ctx.map`）复用常驻 Worker 池（空闲 10 分钟关闭并释放许可）。支持 `--detach` 分离运行：扫描在后台线程用镜头快照和 Worker 池运行，主实例照常响应其他命令（ADR-0009）；`exec`/`opt`/`check`/`round` 的分离运行由独立子进程等待主实例完成并写入 `runs/`。
- **认可前不启动 OpticStudio**：Optiland 阶段不做建模期双端基准对齐，也不在后台启动 OpticStudio 回退代算；Optiland 不支持的功能直接报告为能力缺口（ADR-0012）。这避免了前期占用许可证（本机上限 8 个并发实例）及在优化前维护两套结论。
- **原生快照权威保存，`.zmx` 为导出件**：保存镜头时总是写出原生快照 `.optiland.json`（含完整处方、材料、光线瞄准、优化设置及哈希）；仅当写出器能准确表达时才另写同名 `.zmx` 并绑定哈希。OpticStudio 接手时从快照处方重建，`.zmx` 仅供查看；旧版 `.zmx` + `.optiland.json` 成对文件保持只读兼容。
- **起点从已有设计生成，不设候选配额**：结构未由用户指定时，由 `saos.starts` 从起点库按需求缩放生成并给出一阶与三阶像差预判，以相同短期预算初筛；明显合适的形式可直接选用并在确认页写明理由，无需凑候选数量。
- **关卡只留在昂贵处，封存仅用于发现意外**：硬关卡仅设在深度优化前（需求与模型确认）和进入 OpticStudio 前（报告认可）；工具写入文件后记录哈希（`seals.jsonl`）以检测意外篡改或删除（见 `saos job verify --help`），缺失或过期的对比记录只作警告显示，不为防 Agent 规避流程叠加上锁机制。
- **目标方向与终止规则**：
  - 越小/越大越好的目标用 `minimize` / `maximize`，按离理想值的距离计入目标差距（Shortfall），达标后继续改进；不要把“越小越好”写成 `≤ 0` 这种永远达不到的达标线。
  - 局部优化只收敛到局部极小，每轮须声明手段层级（`local`、`variables`、`materials`、`global`、`structure`）。层级记录试过什么而非强行按序执行，诊断表明无效的层级可用 `saos rule-out` 带证据排除。终止状态分四种：达成（`achieved`）、达到轮次上限（`limit`）、无法再优化（`exhausted`，允许的手段均已尝试或排除仍无明显进步）、按用户决定停止（`stopped`）。
- **默认自动微分与玻璃在产目录**：
  - Optiland 局部优化默认优先使用 Torch 自动微分（`torch-trf`，float64），遇到明确不可微缺口时回退数值算法并记录原因；显式指定的算法不回退。
  - 玻璃替换默认使用按厂家分开的在产目录 `SAOS_CDGM_INPROD`（另有 `SAOS_NHG_INPROD`，见 `library/glasscat/README.md`），防止全局搜索或 Hammer 换成停产牌号，并避免两家同名牌号参数不同带来的混淆。
- **统一处方骨架与非序列支持**：序列与纯非序列系统（`"mode": "non_sequential"`）共用同一套处方入口、建模确认、结构指纹、判定和轮次；saos 只校验系统设置、通用面属性和多重结构骨架，具体面型和参数按 OpticStudio 原名透传。非序列能力随真实任务逐步扩展。
- **窗口、资源与未保存修改保护**：
  - 用户通过流程图窗口（`saos ui`）查看进度、当前镜头视图（`view.json`）及每轮导出的点列图/MTF（`viz.json`），避免重复计算占许可；窗口尊重用户关闭选择，仅在需要查看的节点通知置顶且不抢键盘焦点；前端随包分发，不引入 Node 构建链。
  - 显性调试（Extension 模式连接桌面 OpticStudio 窗口）为可选（ADR-0011）。长操作期间暂停实时刷新以防异步重算与 API 竞争（见 `saos lens view --help`、`saos lens analysis --help`）。替换窗口内容前一律先将窗口中的未保存状态存为副本（`lenses/captures/`），不替用户丢弃修改。
  - 计算后端固定版本与双精度（见 [README](./README.md)）；长时间进程降低优先级并默认预留 2 个逻辑核，许可证并发预算计入用户已打开的窗口（见 `saos --help` 与 `saos opt local --help`）。
- **代码三层结构与上收原则**：分为固定 CLI 命令、库函数（`saos.kit`、`saos.search`、`saos.starts`、配方库）和任务内脚本（ADR-0007）。同一段代码在第二个任务中被复用时即上收为库函数或配方草稿，上收前后同一批镜头的计算数值必须逐项一致。测试只测工具，不测 Agent 的设计能力。

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
| **设计任务** | Design Job (`jobs/<job>/`) | 从一份用户需求到交付的一次完整设计及其文件目录（*勿称：项目、会话、run*） |
| **运行实例** | Instance (`work/<instance>/`) | 一个后台进程及其独占运行目录，不同实例可并行（*勿单称“实例”指代 OpticStudio 进程*） |
| **计算后端** | Backend (`optiland` \| `zos`) | 持有镜头并完成计算的后端：默认进程内 Optiland，认可报告后切换至 OpticStudio（*勿称：引擎、求解器*） |
| **OpticStudio 实例** | OpticStudio Instance | 占用一个 ZOS-API 许可的 OpticStudio 进程（桌面窗口、standalone 会话或 Worker）（*勿称：license 槽*） |
| **原生快照** | Native Snapshot (`.optiland.json`) | Optiland 保存的权威镜头处方与状态文件，记录内容哈希；OpticStudio 阶段由此重建（*勿称：sidecar、元数据文件*） |
| **环境面** | Environment Surface | 声明为不由本设计制造的面（样品、介质、外购件），须有数据来源，使用模型介质不阻止交付（*勿称：背景介质*） |
| **模型玻璃** | Model Glass | 仅用 `nd`/`Vd`（及 `ΔPgF`）描述的探索用材料，Optiland 阶段用 Abbe 材料近似，不可直接交付（*勿称：虚拟玻璃、理想材料*） |
| **建模说明** | Model Description | 优化前供用户核对的 HTML 页面：各面实物含义、材料来源、一阶分析、有效性检查与下一步预算（*勿称：模型报告*） |
| **模型确认** | Model Approval | 用户对建模说明的认可记录，绑定结构指纹（*勿称：模型审批*） |
| **当前镜头视图** | Live View (`view.json`) | 流程图窗口实时展示的镜头布局、Lens Data 与结构交叠检查（*勿称：预览*） |
| **结构交叠** | Interference | 零件在几何上相交或穿过（如负边厚、负空气间隔），按零件外缘或显式孔径计算（*勿称：干涉、碰撞*） |
| **调试轮次** | Round | 一次优化、换材或改结构后记录的检查点，含镜头快照、全项实测值与手段层级（*勿称：迭代、调整轮次*） |
| **目标差距** | Shortfall | 各带界限项超出界限的量与 `minimize`/`maximize` 距离归一化加权之和，用于跨轮比较（*勿与优化评价函数 merit 混淆*） |
| **手段层级** | Strategy Tier | 轮次所用的手段级别：`local`、`variables`、`materials`、`global`、`structure`（*勿称：优化阶段*） |
| **无法再优化** | Exhausted (`exhausted`) | 自上次明显进步以来，允许的各手段层级均已尝试或带证据排除仍无进展的终止状态（*勿称：停滞、卡住*） |
| **扫描 / 扰动诊断** | Scan / Probe (`saos scan`) | 在 Worker 池上并发评估候选结构（`candidates`）、参数网格、单变量扰动灵敏度（`probes`）或公差（`tolerance`）（*勿单称 map 或 sweep*） |
| **交付报告** | Delivery Report (`report/index.html`) | 汇总实测值、达标状态、材料与计算后端的 HTML 报告，只交付满足全部可交付条件的轮次（*勿称：验收报告*） |
| **OpticStudio 验证** | OpticStudio Verification | 用户认可 Optiland 报告后，在 OpticStudio 中按快照处方重建、用同一判定复测比对并继续优化的阶段（*勿称：双端校验、终验*） |
| **诊断量** | Diagnostic | 随报告输出、帮助解释结果但不决定是否达标的光学量，如 Strehl 比（*勿称：附加指标*） |
| **隐式运行 / 显性调试** | Headless Run / Explicit Debugging | 默认无界面运行 vs 通过 Interactive Extension 连接用户桌面 OpticStudio 窗口调试（*勿称：后台模式 / 窗口模式*） |
| **配方** | Recipe (`skills/recipes/<系统>/`) | 针对特定光学系统按需加载的子技能与配套工具库，从真实任务中提炼（*勿称：模板、插件*） |

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
| 根目录 | 只有 `README.md`、`DESIGN.md`、`TODO.md`、`AGENTS.md`、`pyproject.toml`、`uv.lock`、`.gitignore`，以及 `.claude/skills/saos`（指向 `skills/saos` 的链接）；`ref`（联接到 zea 参考资料）和 `work/` 不入库 |
| `src/saos/` | 核心工具代码。不依赖任何配方，仅 `cli.py` 注册配方命令 |
| `src/saos/recipes/<配方>/` | 配方配套代码 |
| `skills/saos/`、`skills/recipes/<配方>/` | 入口技能与按《配方编写规范》（`skills/recipes/README.md`）编写的配方子技能 |
| `docs/` | 跨任务文档：`workflow.drawio`（工作流图）、`adr/NNNN-*.md`、`plans/*.md` |
| `docs/recipes/<配方>/` | 配方的文档与历史对照记录 |
| `library/<库>/` | 跨任务共享参考数据（均含 `README.md` 说明来源与用法）：`materials/`、`glasscat/`、`thermal/`、`mechanics/`、`coatings/`、`starts/`、`microscope/` |
| `tests/`、`tests/recipes/<配方>/` | 核心工具测试与配方测试 |
| `experiments/<名称>/` | 不属于 saos 包的独立实验代码（均含 `README.md`） |

镜头文件（`.zmx`、`.optiland.json`、`.ZDA`）只出现在 `library/`、测试夹具或 `work/` 中；日志和临时文件不入库（临时文件放系统临时目录）。具体设计任务的一切产物均放进该任务目录，不进仓库。

## 任务目录

运行产物放在 `work/<实例>/`（不入版本控制），下设 `daemon/` 和 `jobs/`。任务目录 `work/<实例>/jobs/<任务>/` 中，Agent 自写内容只放在 `sources/`、`checks/`、`notes/`、`explore/<主题>/`（须含 `README.md`）和工作镜头中，其余文件由工具写入并记录封存哈希（`seals.jsonl`）；各步骤文件清单见 `skills/saos/SKILL.md` 的 "Where files go"。
