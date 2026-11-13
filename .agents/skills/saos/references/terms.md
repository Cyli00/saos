# Terms

The Chinese and English names saos uses, with what each covers. Use these names in what you write for the user and into the job; the last column also lists the names to avoid.

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
| **复测** | Confirmation (`confirm`) | 在 OpticStudio 上重测 rtc 记下的一轮（`saos round --confirm`，逐轮复测在需求访谈时约定，默认不做）；不单独成轮，其数值替换那一轮的数值，结论与 rtc 不同则回到设计（*勿与重测基准混淆*） |
| **设计任务** | Design Job (`jobs/<job>/`) | 从一份用户需求到交付的一次完整设计及其文件目录（*勿称：项目、会话、run*） |
| **分析任务** | Analysis Job (`job create --mode analyse`) | 回答关于现成镜头的问题（某个量、某条曲线、两个镜头对比）的任务，没有需求单、关卡、轮次和报告；要按目标改镜头时另建设计任务（*勿称：仿真任务*） |
| **运行实例** | Instance (`work/<instance>/`) | 一个后台进程及其独占运行目录，不同实例可并行（*勿单称“实例”指代 OpticStudio 进程*） |
| **计算后端** | Backend (`zos`, `rtc`) | 后台进程里持有镜头并完成计算的一方：`zos` 是 OpticStudio 会话，`rtc` 做交付之前的全部计算，`zos` 做交付前的最后优化与交付轮测量；`fake` 仅供工具测试 |
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
