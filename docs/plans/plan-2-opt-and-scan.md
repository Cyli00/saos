# Plan 2：Optiland 优化器（SciPy + PyTorch Autograd）、在产玻璃替换与零许可证并发扫描

本计划只管 Optiland 阶段。OpticStudio 阶段（用户认可报告之后）照旧用 Zemax 的阻尼最小二乘、Hammer 和全局搜索，见 Plan 3；本计划不删除任何 OpticStudio 优化代码。

## 1. 为什么要做（Why）

1. **Zemax 优化器拖慢探索**：
   * Zemax 的阻尼最小二乘（DLS）用有限差分算雅可比矩阵，变量多（多片高次非球面 20–40 个变量）或带自定义判定时很慢；光纤探头任务里 18 个变量的无梯度搜索跑满 1 小时只完成 48 次评估。
   * `Hammer` 与 `Global Search` 不会自行停止，必须靠外部轮询中断；全目录替换玻璃时，Cooke 样例 12 次替换中有 9 次换成了停产玻璃（`Obsolete`）。
   * 目前 `opt` 命令在 Optiland 会话里会把整个会话切到 OpticStudio（`hybrid.py` 的 `ZOS_ONLY`），一用优化就失去零许可证和进程内计算的好处。
2. **ZOS 多实例进程池（`src/saos/parallel.py`）的并发上限**：
   * 现有的 `saos scan` 和 `ctx.map` 每个 worker 是一个 standalone OpticStudio，受本机最多 8 个许可证限制（含桌面窗口），每个 worker 启动 `5.8–7.9 s`，4 个 worker 吞吐约 `304 次/秒`。
   * Plan 1 之后 worker 已默认跑 Optiland、不占许可证；剩下的是让扫描的各种计划在 Optiland worker 上跑通并测出实际吞吐。
3. **可微优化的 `NaN` 梯度与高次非球面尺度问题**：
   * PyTorch 自动微分下，只要有一条边缘光线全反射（TIR）或错过表面，产生的 `NaN` 就会在反向传播时污染整个梯度。
   * 曲率半径（$R \sim 10^1\text{ mm}$）与高次非球面系数（$A_4 \sim 10^{-4}$ 至 $A_{16} \sim 10^{-18}$）相差近 20 个数量级，不缩放就调不动高次项。

---

## 2. 怎么做（How）

### 2.1 优化内循环不重建镜头
Plan 1 的 `OpticalState` 每改一次参数就从处方重建整个 Optic，再逐面追迹算自动半口径。重建会切断 PyTorch 的计算图，有限差分路径上每次评估也多出一次重建和若干次追迹，所以优化内循环另走一条路：

1. 内循环直接改活的 Optic 上的变量，不经处方。
2. 拾取（`pickup`）、F 数（`f_number`）、边缘光线高度（`marginal_ray_height`）三种求解改成就地更新，torch 路径上写成可微的。
3. 自动半口径只在线搜索接受一步或优化结束时重算（Zemax 也不用半口径裁光线，它只影响交叠检查）。
4. 优化结束时把变量写回处方，再完整同步一次（Plan 1 的 `_sync_optical_state`），之后才写 `view.json`、做交叠检查和 `review`。

### 2.2 两档执行路径

| 路径 | 算法 | 支持的目标 | 依据 |
|---|---|---|---|
| **scipy（有限差分）** | `trf`（默认，带边界信赖域反射最小二乘）、`lm`、`lbfgsb`、`nelder-mead`、`powell` | Plan 1 已映射的全部操作数，以及需求单判定（`ctx.measure`） | 不需要梯度；Plan 1 的操作数实现都转成 numpy 或 float，可以直接用 |
| **torch（自动微分）** | `torch-lbfgs`（`torch.optim.LBFGS` 自己接入，Optiland 0.6.3 的 torch 优化器只有 adam 和 sgd）、`torch-adam` | 可微子集：`RSCE`、`RWCE`、`REAY`、`EFFL`、`TOTR`、`CTVA`、`ETVA`，加上 `opt merit script` 的自定义损失 | 30 个变量时有限差分要 31 次追迹，自动微分 1 次前向 + 1 次反向；默认 CPU FP64，可选 `--device cuda` |

* 可微子集里的每个操作数各写一份 torch 实现，这是本计划工作量最大的一项；每一份都要在同一镜头上与 numpy 版和 OpticStudio 的值比较。
* 评价函数里有可微子集之外的操作数时，torch 算法直接报错并列出这些操作数，不静默退回有限差分。

### 2.3 防 `NaN` 与变量缩放
1. **防 TIR / 脱靶 `NaN`**：
   * 开方项（斯涅尔折射 $\sqrt{1 - \sin^2\theta}$、二次曲面求交判别式 $\sqrt{\Delta}$）施加 `torch.clamp(val, min=1e-12)`。
   * 全反射或超出孔径的光线用 `torch.where(valid_mask, ray_loss, soft_penalty)` 换成关于 $\sin\theta - 1$ 或脱靶距离连续可导的惩罚项。
   * 线搜索一步后有效光线比例低于阈值或零件交叠时回退步长。
2. **变量缩放**：Optiland 0.6.3 已有 `optimization/scaling/`，半径变量默认线性缩放。在现有缩放器上改：
   * 半径以归一化边缘曲率 $\tilde{c} = c \cdot R_{\text{norm}}$ 为变量（$R_{\text{norm}}$ 为该面净半口径），平面对应 $\tilde{c} = 0$；
   * 非球面系数以边缘矢高贡献 $\tilde{A}_{2k} = A_{2k} \cdot R_{\text{norm}}^{2k}$ 为变量；
   * 厚度与偏心按典型特征长度缩放。

### 2.4 `saos opt` 命令组（`src/saos/cli.py` + `optiland.py`）
1. **`saos opt vary`**：
   * 保持 `--surfaces`、`--param radius|curvature|thickness|conic|par<N>|material` 语法。
   * 新增 `--min <float>` / `--max <float>`：变量的物理上下界，由优化器在边界内约束。
   * 多重结构变量（`--config-rows`）、非序列物体变量（`--objects`）本计划不迁入 Optiland，用到时会话照旧切到 OpticStudio，结果里说明。
2. **`saos opt merit`**：
   * `wizard`：生成多视场多波长 RMS 光斑 / 波前加中心与边缘厚度边界。
   * `add`：接受 Zemax 操作数名（`EFFL`、`TOTR`、`RSCE`、`RAID` 等），不另造一套语义名，免得同一个量有两个名字。
   * 新增 `script <path.py>`：任务脚本定义 `def loss(ctx) -> float | Tensor`；返回 Tensor 的才能用 torch 算法。
3. **变量、边界与评价函数的保存**：三者存进伴随文件，随镜头复制、归档和加载；它们不是结构，不进结构指纹。会话切到 OpticStudio 时，变量照译为 OpticStudio 变量，Zemax 名字的操作数照译为评价函数行，边界和脚本损失译不过去的在结果里逐项列出，由 agent 在 OpticStudio 里重设。
4. **`saos opt local`**：
   * `--algorithm`：见 §2.2；`--device`：`auto`（默认，CPU FP64）、`cpu`、`cuda`。
   * 保留 `--max-seconds`、`--cycles`、`--measure`、`--detach`；结束后照旧返回 `review`（交叠检查与 `--measure` 的前后对比）。
5. **`saos opt global`**：新增 `--method de|basinhopping|shgo`（默认 `de` 差分进化），在 `--max-seconds` 预算或收敛判据达成时停止，前 10 个无交叠的结果存入 `lenses/global/NNN/`（与现在的输出位置一致）。
6. **新增 `saos opt glass`（Optiland 阶段的在产玻璃替换）**：
   * 候选牌号和 nd、Vd 取自在产 AGF（默认 `SAOS_CDGM_INPROD`，`--catalog` 可指定 `SAOS_NHG_INPROD`），材料对象从 Plan 1 的 `MaterialStore` 取，与建模用的折射率是同一份数据；只借用 Optiland `GlassExpert` 的贪心邻域算法，不用它按名字查 refractiveindex 数据库的部分。
   * AGF 色散公式 1、2、3 以外的牌号 Plan 1 不支持，不作为候选，结果里列出被跳过的牌号。
   * 每换一次玻璃配合连续变量局部重优化。
   * `saos opt hammer` 不删除，供 OpticStudio 阶段使用；在 Optiland 会话里调用它照旧切到 OpticStudio。
7. **模型玻璃**：Optiland 阶段允许用 Optiland 的 Abbe 材料近似 Zemax 的模型玻璃，探索更快（用户标准：交付前有 OpticStudio 阶段复测，模型玻璃本来也不可交付）。这类轮次在 ledger 和报告里标明“模型玻璃为 Optiland 近似”；进入 OpticStudio 阶段前必须换成真实牌号。落地时同步改 GLOSSARY 的「模型玻璃」词条和 `can_handle`。

### 2.5 `saos scan` 与 `candidates.py` 在 Optiland worker 上运行
* `vars`（网格）、`points`（点列）、`probes`（扰动诊断）、`minimize`（多起点寻优）、`candidates`（候选结构同等预算比较）、`tolerance`（灵敏度与蒙特卡洛公差）全部在 Optiland worker 上跑通。
* worker 池沿用 `parallel.py` 的常驻、预热和空闲关闭的生命周期，不为每次扫描新开进程：Windows 只能用 spawn，每个新进程都要重新导入 optiland 和 torch（主进程里导入约 5 s，2026-10-20 实测）。
* 先测 Optiland 自带的 `batched_evaluator.py` 向量化追迹能替代多少多进程，再定 worker 数；默认上限 `max(1, os.cpu_count() - 2)`（本机 10 个）。
* `scans/NNN/plan.json`、`result.json`、`index.html` 与 `lenses/scans/NNN/pPPP.zmx` 的封存输出格式不变。

### 2.6 去掉 Optiland 会话里 `opt` 命令的会话切换要同步的地方
* `hybrid.py` 的 `ZOS_ONLY` 只留多重结构变量、物体变量和窗口操作。
* `round --tier materials` 的含义：Optiland 阶段指 `opt glass`，OpticStudio 阶段指 Hammer 或全局搜索的玻璃替换。
* `skills/saos/SKILL.md` 的优化一节（手段层级、`opt glass`、`opt hammer` 只在 OpticStudio 阶段）、`job cancel` 与 `--detach` 的安全中断点、`--measure` 的前后对比。
* AGENTS.md 要求的四处：SKILL.md、`ui/stage.py` 与 `app.js`、`docs/workflow.drawio`、各命令 `--help` 与 `docs/cli.md`。

---

## 3. 做完预期（Expected Outcome）

1. **速度：目标值，基线待测**。下面的数都没有测过，是目标而不是验收线；实施第一步先测基线，测法写清镜头、变量数和光线数：
   * `saos opt local`：Cooke 三片镜（样例文件）6 个半径变量、每视场 15×15 光线网格、3 视场 3 波长，目标单次收敛 `0.2–2.0 s`；对照同一镜头在 OpticStudio DLS 上的耗时。
   * torch 路径：`library/microscope/` 中一个 20 个以上变量的条目，单步梯度耗时对比有限差分，目标快 `10 倍` 以上；全反射光线出现时不产生 `NaN`。
   * `saos scan`：现有基线是 36 点求解加测量，ZOS 4 worker 冷启动 `10.2 s`、热池 `2.21 s`；目标热池 `< 0.3 s`，占用 0 个许可证。
   * 3 个候选结构同预算探索（`scan --plan candidates`）：目标 `15–45 s`，基线取一个已完成任务的候选计划在 ZOS 上的耗时。
2. **玻璃替换**：`saos opt glass` 换出的牌号全部属于指定的在产目录（在产目录只收 Standard 和 Preferred，所以不会换出 `Obsolete`）。
3. **变量与评价函数**：保存、重载、复制镜头后 `opt vary` 与 `opt merit` 的设置还在；会话切到 OpticStudio 时译不过去的项逐项列出。
4. **阶段快照**：全部完成后提交并打标签 `v0.2.0-plan2`。
