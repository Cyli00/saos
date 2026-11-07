# 光学 Expert 类

## 接口与运行范围

`ctx.AsphereExpert(...)` 和 `ctx.GlassExpert(...)` 在当前计算后端的镜头上（OpticStudio 或 rtc 实例）执行非球面选面与材料搜索；类从 `saos.ctx.experts` 导入（`saos.ctx` 是 L0，只导出 `rows`）。脚本与 worker 上下文经 `saos.ctx.experts.ExpertMethods` 获得这两个入口，rtc 探索副本没有；不另占 OpticStudio 许可。OpticStudio 上 `engine="rtc"` 的排名和玻璃建库在内存里用 rtc（ADR-0015），数值只用于排序和建库，接受仍看 OpticStudio；rtc 实例上候选库与当前材料的坐标都在 rtc 材料层计算（`RtcBackend.medium_indices`，与 INDX 同一路径），不改波长表。

当前只支持纯序列镜头。多重结构只在 rtc 实例上支持（2026-11-07）：要从被跟随的结构开始，评价函数要覆盖每个结构；非球面转换时补 PRAM 行让系数跟随温度，`engine="rtc"` 把各结构的残差叠在一起，最后厚度跟随同一结构时只算一个对焦列。OpticStudio 上的多重结构、非序列、混合模式、rtc 探索副本及只读判定上下文不支持。工作流里的用法（手段层级、结构确认）见技能 `references/rounds.md`。

## 非球面排名

实现位于 `src/saos/ctx/experts/ranking.py`。设当前带权残差为 e，第 k 面系数雅可比为 Jk：

1. 用 SVD 求系数步长 `dq = -pinv(Jk) @ e`。
2. 投影为 `p = -Jk @ dq`，无约束分数为 `norm(p) / norm(e_initial)`。
3. 根据斜率预算缩放 dq，最终分数为无约束分数乘可用比例。
4. 选分数最大的面，扣除它的缩放投影，再给剩余面重排。分母始终使用初始残差范数，精确并列时选择后遇到的候选。

令 C、P 分别为当前和预测斜率向量的平方和，B 为预算平方，可用比例为：

- `C < B` 或 `P < C` 时：若 `P > C`，取 `sqrt(min(1, (B-C)/(P-C)))`，否则为 1；
- 其他情况为 0。

这是投影方向的比例缩放，不重新求解受约束最小二乘方向；分数也不是预测的 merit 平方下降量。已超限但预测斜率下降时可以保留完整步长，最终设计仍须通过实际斜率检查。

每面只分解一次雅可比，复用于全部排名轮次。秩阈值由 `rcond` 控制，零残差给出零分。

## AsphereExpert

```python
def run(ctx):
    expert = ctx.AsphereExpert([1, 3, 5], terms=4, max_seconds=60)
    return expert.rank()
    # 或 expert.run(nas=2)：逐面转换与优化，每接受一个面后重新排名
```

### 配置

- 默认面型 `EvenAspheric`，系数从 `par2`（r^4）起；不释放 r^2 或 conic 来重复曲率自由度。
- `terms` 为 1–14，实际可用阶数还受所选面型限制。
- `AsphereProfile(surface_type=..., coefficients=(...), settings={...}, steps=(...))` 配置其他面型。QTypeAsphere 必须显式给出该版本的参数列、归一化设置和差分步长，不猜列号或类型选择器。
- 默认从当前 MFE 行构造带权残差：`e_i = sign(value-target)·sqrt(contribution_i/100)·merit`。边界操作数（OPGT、OPLT、MNCT 等，`saos.ctx.rows` 生成的约束行都是）满足时贡献为 0，value-target 却不为 0，所以不按权重重算。正权重行贡献合计不为 100% 时要求提供 `residual(ctx)`；单个 scalar merit 不足以还原残差雅可比。
- 有限差分每面需要 2×系数数量次残差计算，以及临时快照操作。默认步长按口径与阶数缩放，控制边缘 sag 扰动。
- `engine="rtc"`（ADR-0015）：候选面在 OpticStudio 上转成零系数偶次非球面后交给 rtc 副本（`freeze_solves=True`，建立时与 OpticStudio 对照追迹）。残差是各视场、各有效波长 `grid`×`grid`（默认 7）光瞳内光线相对主波长主光线的像面横向偏差，按 √(视场权重×波长权重) 加权；雅可比由一次前向 AD 追迹给出，像面前一面厚度作对焦补偿从残差和各列投影掉。丢失或瞄准不收敛的光线不进残差，结果的 `dropped_rows`、`dropped`（按波长、视场）列出丢了多少；丢得多时样本已经变了，排名只作参考。只接 EvenAspheric `par2…par12`，不接 `residual`、`slope_vector`；ADR-0014 拒绝的镜头在建副本时报 `BACKEND_UNSUPPORTED`；实际瞄准时雅可比带瞄准解的隐函数项，光阑前的候选面不再拒绝（2026-11-05，已与中心差分核对，未与 OpticStudio 的排名对比）。`run()` 的优化与接受仍按 MFE 在 OpticStudio 上做，两种 engine 的残差定义不同，排名可能不同。结果带 `engine`、`residual_rows`（参与排名的残差行数）、`rtc` 与 `agreement`。
- `candidate_filter(ctx, surface)` 可加入口径、厚度、曲率等限制；`feasible(ctx, surface)` 执行实际有效性判定，必须返回 bool。
- 单面偏好、空气邻接与胶合面排除根据当前介质拓扑决定，不按面号奇偶猜元件。
- `optimizer(ctx)` 可替换默认局部优化。默认复用 backend.optimize 的预算和取消处理。

### 斜率与持久约束

默认不设斜率上限。配置 `slope_limit_waves` 时，必须同时提供 `constraints(ctx, surface, limit_waves)`，向当前 MFE 加入后续优化仍能使用的约束；单纯事后剔除不能替代持久约束。

默认 EvenAspheric 度量为相对于**当前基底 conic**的径向 departure，在圆口径上计算面积 RMS，以测量波长表示为 waves/radius。Gauss-Legendre 样本用于径向多项式积分，不重新拟合最佳 conic。

非默认面型设置有限上限时必须提供校准的 `slope_vector(ctx, surface)`，使排名与优化后的检查采用同一度量。`wavelength_um` 默认系统主波长，长度单位取当前系统。约束回调负责匹配这个度量、权重及归一化范围，不自动猜测约束操作数。

`rank()` 分析后恢复完整镜头、求解、MFE 和原 dirty 标志。`run(nas=0)` 等同分析；`run(nas>0)` 每接受一个面后重新计算灵敏度。真实 merit 没改善时恢复上个接受态。新增约束会改变 MFE 归一化，前后比较必须使用相同的新 MFE。

## GlassExpert

```python
def run(ctx):
    expert = ctx.GlassExpert(
        [1, 3, 5], catalogs=["SAOS_CDGM_INPROD"],
        neighbors=7, single_optimization_pass=False,
        max_passes=5, max_seconds=120,
    )
    return expert.run()
```

`candidates(surface)` 只列候选，结束时恢复镜头。

### 搜索策略

1. 在 V / partial-dispersion 平面拟合正常色散直线，按垂直距离标出最远约 20% 的异常色散候选。部分色散默认用工作波段的 (n_ref-n_long)/(n_short-n_long)；`partial="gF"` 改用厂家的 P_g,F = (n_g-n_F)/(n_F-n_C)，与目录图表一致。
2. 候选距离为 `(abs(n)-abs(n0))² + (abs(V/100)-abs(V0/100))²`。
3. 先寻找满足材料可行性条件的集合，此阶段可接受较差 merit、跳过局部优化；全部可行后进入性能搜索。
4. 每轮按元件光焦度排序。优先尝试异常色散材料，再试普通邻居。窗口含端点，通常为当前玻璃加指定数量的邻居。
5. Single Pass 时优化一次；否则最多四次，第 2/3 次根据相对最佳 merit 的进展阈值 0.333/0.5 提前停止。
6. 改善后立即保存整个设计为新 best，后续候选从新 best 开始。没有玻璃变化时结束；达到轮次或时长上限单独报告，不作为收敛。

### 材料与项目回调

- 默认沿用 `substitute_catalog(None)`：有 `SAOS_CDGM_INPROD` 时优先它，否则使用当前目录。自动目录仅纳入 Standard/Preferred。
- AGF 用于材料身份和状态。工作波段 n/V/partial 不以目录标准 nd/Vd 冒充：色散路径在 rtc 准入表（`rtc_admitted.json`）内的玻璃由 rtc 材料层直接算（与 INDX 差 ≤ 1e-8 的路径才放行，ADR-0015），其余在 ZOS 上测 INDX；结果的 `coordinate_sources` 记各自片数。
- 默认坐标波长为最长/主/最短工作波长；单波长采用标准 C/d/F。可用 `coordinate_wavelengths_um=(long, reference, short)` 指定。
- 需要 INDX 的玻璃建库时波长只切换一次，逐片换料后直接测，最后恢复一次镜头；同一个实例的 `candidates()` 与 `run()` 复用已测的库（按目录、坐标波长和部分色散定义缓存）。
- 测量时临时修改前几个波长（gF 时共 6 个），不足时追加，finally 恢复数值并删除临时项；不改变原权重、温度或压力。探测外侧还有完整镜头快照。
- ZOS 按材料名及启用目录的优先级解析材料。重复名只测一次；候选的 `catalog` 是声明的数据来源，不保证强制选择另一个同名定义。需要明确身份时整理当前目录，或提供可回读的 `substitute(ctx, front, glass)`。
- 默认使用共轴厚透镜的一阶光焦度排序；复杂系统通过 `element_power(ctx, front)` 提供排序值。默认换料保持表面几何不变；项目需要曲率重整时通过 substitute 回调实现。
- `feasible(ctx, surface)` 承担实际成本、总重量、透过率、热膨胀及几何有效性判定。不配置就没有这些过滤，不从 nd/Vd 猜密度、成本或 CTE。
- `candidate_filter(ctx, surface, GlassCandidate)` 在窗口截断前剪枝。
- 可传 `materials=[GlassCandidate(...), ...]` 与 `coordinates(ctx, surface)->(n,V,partial)`，用于私有材料或工具测试；调用方负责坐标波长一致性。
- 没有找到完整可行材料集合时恢复入口，不留下部分不可交付集合。

## 与专利库配合

专利起点（`saos.starts.patents`）的玻璃代码转成模型玻璃（`MaterialModel` 求解），不可交付。`ctx.GlassExpert("model")` 选中全部模型玻璃面：可行性阶段按工作波段 n/V 距离给每面换上最近的在产牌号，再按性能逐片搜索。专利条目自带的非球面不是标准面，AsphereExpert 把它们算作已占用，不会在同一元件上再加。

## 状态与工作流

- 当前后端原位工作，不启动额外许可实例，不自动保存用户镜头或记录调试轮次。
- 未预期异常恢复入口；取消/超时只保留已接受的完整设计。提交标 dirty，缓存随修改/恢复失效。
- 快照使用临时 `.zmx` 的 SaveAs/Load，会改变 ZOS SystemFile。保存必须走 `ctx.save_as(<镜头名>)` 或 `saos lens save`，不能随后直接调用 `ctx.system.Save()`；保存前 `opt hammer|global` 拒绝运行，免得结果写到临时目录。
- 回调不得嵌套同一 ctx 的 Expert、切换镜头/后端、自行保存用户镜头或持有跨快照的 .NET 编辑器行对象。
- max_seconds 是安全检查点和优化器预算，不承诺抢占任意用户回调或原生调用。
- 这些类不确认需求/模型、不将 MFE merit 当目标差距、不绕过结构审批。改变面型仍触发正常模型结构指纹变化。

结果中的 `method` 分别为 `linear_projection` 与 `sequential_glass_search`，并记录实际计算后端身份。

## 真机测试

`tests/test_zemax_experts.py`（`SAOS_RUN_ZEMAX=1`）用 `tests/fixtures/cases/` 的三个案例：序列案例上验证 AsphereExpert 的排名与恢复（评价函数含边界操作数）、GlassExpert 的候选与 gF 部分色散、`surfaces="model"` 把模型玻璃换成在产牌号；非序列与混合模式案例验证两类 Expert 都拒绝。rtc 相关：准入玻璃的 rtc 坐标与 INDX 实测一致、`engine="rtc"` 的排名在序列案例上跑通并恢复镜头。`tests/test_experts.py` 只留不需要 OpticStudio 的排名与候选窗口代数。

## 与 CODE V 2026 的速度对比（2026-11-06，用户要求后停止，未跑完）

脚本：`tests/bench_experts.py`。镜头是序列案例的双胶合和两份专利（`or00178` 6 片、`or00156` 10 片，都是全球面、模型玻璃）。两边变量都是全部半径和空气间隔，都保持 EFL；Glass Expert 用 SCHOTT，邻居数 7。

| 镜头 | AUT | Asphere 排名（3 次） | 放 2 个非球面 | Glass Expert | Glass Expert 前后的误差函数 |
|---|---|---|---|---|---|
| 双胶合 | 0.03 s | 0.14–0.17 s | 0.33 s | 210.6 s，88 次试探，接受 12 次 | 148.02 → 146.67 |
| `or00178` | 0.61 s | 0.19–0.20 s | 0.56 s | 1020.5 s，469 次试探，接受 32 次 | 42.68 → 9.65 |
| `or00156` | 0.84 s | 0.30–0.48 s | 0.97 s | 约 12 分钟时手动停止，无统计 | — |

rtc 只测了双胶合，且和 CODE V 的 6 片同时运行，用时不准：`GlassExpert` 58.4 s，125 次试探，接受 16 次，评价函数 0.333 → 0.183；`AsphereExpert` 排名 0.11–0.14 s（`engine="rtc"`），放置 0.375 s，结果是 `no_actual_gain`，而 CODE V 同一镜头放 2 个面后从 110.04 降到 105.22，原因没查。两边排名都是 [3, 1]。

注意事项：

- 两边评价函数不同，降幅不能直接比。rtc 侧双胶合的像距由求解锁定，没有作为变量；CODE V 侧是变量。
- Asphere Expert 表中 0 个非球面时的评价函数（双胶合 110.04，`or00178` 480.21）和 Glass Expert 的初始值不一致，原因没查。
- `or00178` 的 77% 降幅里包括模型玻璃换成真实玻璃的影响，不全是换玻璃的额外收益。
- Glass Expert 每次试探约 2.2–2.4 s，远多于一次 AUT；多出来的时间可能在宏和绘图上，没有验证。
