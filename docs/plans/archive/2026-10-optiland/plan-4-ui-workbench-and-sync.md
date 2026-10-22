> 历史归档（2026-10-22）：本文保留当时的方案、实施与测试记录，不再是执行入口。当前待办见[整合计划](../../integration-workflow.md)。正文中的“当前”“下一轮”、路径与执行指令均属于原记录；未完成事项的去向以新计划为准。

# Plan 4：Web 光学设计工作台（`saos ui` 6 大模块扩充）

## 1. 为什么要做（Why）

1. **Optiland 阶段没有 OpticStudio 窗口可看，诊断视图要进 `saos ui`**：
   * 原有的 `saos ui`（`src/saos/ui/`）右侧仅有 `Layout`、`Spot Diagram`、`MTF` 和一张只有总差距 `G` 的极简 `Rounds` 表。光学工程师最依赖的诊断工具——**光线扇图（Ray Fan / OPD Fan）、逐面赛德尔像差五项+色差柱状图（Surface Seidel）、场曲与畸变图、$n_d$-$V_d$ 玻璃分布图、需求单各项指标与目标的逐项差距看板**——此前都依赖在 Zemax 桌面窗口中手动点开查看。
   * Optiland 阶段默认不连 OpticStudio，这些分析视图要原生内建到 `saos ui` 中；Optiland 算全套诊断数据的耗时目标为 `15–30 ms`（待测）、零许可证消耗，**当前工作镜头（`live`）也可以实时展示全部像质与像差图**，不必再等记录 `round` 后才能看。
2. **直观展示两个阶段与新旧轮次变化**：
   * 用户需要一眼看清：当前在 Optiland 阶段还是 OpticStudio 阶段；进入 OpticStudio 阶段后，验证轮里每一项在 Optiland 和 OpticStudio 之间差多少；以及这一轮相比上一轮（或初始结构 `r000`）镜片弯曲和间隔改了哪里。
3. **本计划不改工作流步骤**：OpticStudio 阶段的新节点和四处同步由 Plan 3 完成；本计划只扩充界面里的视图，界面改动若涉及步骤、标签或返回边，仍按 `AGENTS.md` 的四处同步执行。

---

## 2. 怎么做（How）

### 2.1 保持两大架构原则不变，拆分前端 ES Module
1. **两条不变原则**：
   * **不引入 Node.js / Webpack / React 构建链**：继续使用随包分发的原生 HTML + CSS + Canvas 2D + ES Modules。
   * **UI 服务只读任务目录文件，不阻塞计算进程**：`src/saos/ui/server.py` 与 `stage.py` 只读取任务目录中的静态 JSON 文件（`view.json`、`evaluations/NNN/result.json`、`viz.json`、`scans/NNN/result.json`，以及 Plan 3 的阶段记录与验证轮对比）。显性调试保留（ADR-0011、Plan 3 §2.6），`server.py` 中转发 `lens.view` / `lens.info` 的接口不动。
2. **前端代码模块化拆分（`src/saos/ui/static/`）**：
   * 将原单文件 `app.js`（1,207 行）拆分为 4 个职责清晰的原生 ES Module：
     * `app.js`：状态轮询、顶部状态栏、左侧工作流 SVG 流程图、标签页路由；
     * `layout.js`：2D/3D 光路画布、新旧轮次虚实线叠影对比（Ghost Overlay）、焦点局部放大、测距尺、完整处方展开表；
     * `diagnostics.js`：点列图（含艾里斑）、MTF（含验证轮的 OpticStudio 叠线）、光线扇图/OPD 扇图、逐面赛德尔像差柱状图、场曲/畸变/CRA 曲线、$n_d$-$V_d$ 交互式玻璃图；
     * `boards.js`：需求单逐项指标达标看板、结构探索候选 PK 墙（`candidates`）、灵敏度探针表（`probes`）、标明计算后端的轮次表。

### 2.2 `saos ui` 6 大扩充模块详细设计

#### 模块 1：顶部状态栏扩充（后端与阶段芯片 + 多结构切换 + 对比选择器）
* **主引擎芯片**：显示 `Optiland · CPU FP64` 或 `Optiland · Torch Autograd`；当 `progress.json` 处于运行态时，动态显示当前迭代步数、用时与实时 Merit 下降。
* **阶段芯片**：`Optiland 阶段（未经 OpticStudio 验证）` 或 `OpticStudio 阶段（自第 N 轮验证起）`，读 Plan 3 的阶段记录；孪生实例在用时附注它占着 1 个许可证。
* **多重结构切换器（Config Switcher）**：当镜头含多个结构（变焦、多温度）时，顶部展示 `Config 1 | Config 2 | …` 切换按钮，联动切换光路、处方与像质图。
* **轮次对比选择器（Compare Selector）**：在轮次下拉框旁增加 `对比: [无 | r000 | 上一轮 | 验证前的 Optiland 轮]`，供光路图、像差图和指标表做 Diff 叠图。

#### 模块 2：新增 Tab `指标看板 (Checks)`
* 读取 `sources/demand.yaml`、当前轮次 `evaluations/NNN/result.json`（验证轮另读 Plan 3 的逐项对比），按 `constants`（硬条件）、`variables`（范围）、`objectives`（优化目标）分组展示：
  * 列结构：**`指标 ID 与物理说明` | `目标界限 (Must / Range)` | `实测值（注明后端）` | `验证轮：Optiland 值与相对差别 Δ%` | `占总差距 G 权重条`**。
  * 对未达标项用醒目颜色标出差距贡献比例（Pareto 拆解）。

#### 模块 3：增强 Tab `光路与处方 (Layout)`
1. **双轮次光路叠影（Ghost Overlay）**：开启对比模式时，底层用半透明虚线绘制对比轮次（如 `r000`）的镜片截面，上层用实线绘制当前镜头，直观呈现各镜片弯曲度、厚度与空气间隔的演变。
2. **焦点局部放大窗（Focus Inset）**：在 2D 视图右上角可选开启像面焦点微米级放大框，直接观察边缘光线与主光线的交汇球差与离焦量。
3. **完整处方表（Full Prescription Inspector）**：
   * 在原有 `R, T, Material, SD, Conic` 基础上，增加显示：**机械半口径 (`Mech SD`)、边缘厚度 (`Edge T`)、偶次/奇次非球面各阶系数 ($A_4 \sim A_{16}$ 及其对边缘矢高的贡献 $\mu\text{m}$)、坐标断点偏心与倾斜参数、变量的上下界 (`[min, max]`)**。
   * 提供 `.zmx` 文件路径与下载入口（Optiland 写出的 `.zmx` 注明只供查看，OpticStudio 从处方重建）。

#### 模块 4：新增 Tab `像差与材料 (Aberrations & Glass)`
由 `OptilandBackend.viz_data()` 在生成 `view.json`（当前镜头）和 `viz.json`（每轮归档）时一并导出以下 4 组数据（总耗时增加 `< 20 ms`），前端 Canvas 渲染：
1. **逐面赛德尔像差柱状图（Surface Seidel Bar Chart）**：逐面加上总和（Sum）展示球差 $S_I$、彗差 $S_{II}$、像散 $S_{III}$、佩兹瓦尔场曲 $S_{IV}$、畸变 $S_V$、轴向色差 $C_L$、垂轴色差 $C_T$，悬停显示具体数值，快速定位像差源头面。
2. **光线像差扇图与光程差图（Ray Fan & OPD Fan）**：各视场、各波长的子午（$\Delta y$ vs $p_y$）与弧矢（$\Delta x$ vs $p_x$）横向像差曲线及波前 OPD 曲线。
3. **场曲、畸变与 CRA 曲线**：子午/弧矢场曲（$T/S$）、畸变（%）随视场变化曲线，以及全视场主光线角（CRA）分布。
4. **交互式玻璃图（$n_d$-$V_d$ Glass Map）**：以当前在产目录（`SAOS_CDGM_INPROD`）为背景灰点，高亮标出各透镜 `L1..Ln` 当前玻璃牌号；当发生材料替换时，用带向箭头画出 `旧牌号 → 新牌号` 的迁移路径。

#### 模块 5：增强 Tab `点列图与 MTF (Spot & MTF)`
1. **`Current (live)` 实时化**：`view.json` 直接包含 Optiland 算出的点列图与 MTF，改动镜头后无需记 `round` 即可实时查看。
2. **艾里斑圆（Airy Disk）**：在每个视场的点列图中心叠加绘制衍射极限艾里斑圆（半径 $1.22 \lambda F/\#$）。
3. **验证轮叠线**：OpticStudio 阶段的验证轮在 MTF 图上同时绘制被验证的 Optiland 轮（实线）与 OpticStudio 复测（虚线带圆点）。

#### 模块 6：增强 Tab `探索与轮次 (Exploration & Rounds)`
1. **候选结构 PK 墙（`candidates`）**：读取 `scans/NNN/result.json`，并排渲染各候选结构的 2D 微缩光路图、片数、总长、各阶段 `G` 下降曲线与胜出状态。
2. **灵敏度探针核对图（`probes`）**：并排展示 Agent 事先写下的物理预测方向（`expect`）与实测归一化效应柱状图。
3. **轮次账本表增强**：每行展示 `rNNN | 层级 | 后端 | G | 说明`，验证轮标出它验证的是哪一轮，支持点击任意两行开启对比。

### 2.3 文档同步
* 本计划新增的视图若改变流程图节点的标签、副标题或返回边，按 `AGENTS.md` 同步 SKILL.md、`stage.py` 与 `app.js`、`docs/workflow.drawio`、命令 `--help` 与 `docs/cli.md`；否则只改界面代码。
* ADR-0012 已于 2026-10-20 写好，本计划不再新增 ADR。

---

## 3. 做完预期（Expected Outcome）

1. **用户体验与可视化指标**：
   * 打开 `saos ui` 单个浏览器窗口，即可查看 **光路叠影、完整处方（含非球面高次项与外径）、点列图（含艾里斑）、MTF（含验证轮叠线）、光线扇图、逐面赛德尔柱状图、场曲畸变、$n_d$-$V_d$ 玻璃图、需求逐项达标看板、结构候选 PK 与各轮的计算后端**。
   * 改动镜头后，`live` 视图的上述全部图表在 **`< 50 ms`** 内完成计算并在界面刷新。
2. **文档与自动化测试同步指标**：
   * `uv run pytest` 全量离线测试（含 `tests/test_repo_layout.py` 与 `tests/test_docs_sync.py`）100% 通过。
   * `SAOS_RENDER_DRAWIO=1 uv run pytest tests/test_docs_sync.py -s` 渲染出的 `workflow.png` 清晰无重叠，命令与代码完全一致。
3. **最终快照**：
   * 全部检查通过后，提交并打标签 `v0.2.0-plan4`（完成本次架构升级）。
