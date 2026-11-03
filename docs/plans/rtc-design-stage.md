# 设计轮次脱离 OpticStudio：rtc 实例上的评价函数、优化与复测

依据 ADR-0017。目标：模型确认之后到交付前，变量、评价函数、局部优化、Hammer 与全局搜索、`saos check`、`saos round`、扫描都在 rtc 实例上计算；交付前用 `saos round --confirm` 在 OpticStudio 上复测要交付的那一轮。

## 落地情况（2026-10-29）

| 部分 | 代码 | 说明 |
|---|---|---|
| 评价函数 | `src/saos/backend/rtc_merit.py` | 行格式同 OpticStudio；.zmx 的 MFE 行读写；批量求值（同一结构、波长、视场的光线类行合并成一次追迹）；向导 |
| 优化 | `src/saos/backend/rtc_optim.py` | 带精确约束的阻尼最小二乘；Hammer 与全局搜索 |
| 后端接口 | `src/saos/backend/rtc.py` | `vary`、`merit_*`、`optimize`、`optimize_search`、`apply_values`、`describe_model(None)`、`analysis("FftMtf")`、`viz_data(full=True)`、`change_surface_type`、`set_solve`、玻璃目录 |
| .zmx 往返 | `src/saos/backend/rtc_lens.py` | 变量标记、评价函数行、公差行、多重结构行原样写回；文件头里 rtc 不建模的行原样带过去；不认识的面块行拒绝 |
| FFT MTF | `../rtc/src/rtc/psf.py`（`opd_rays`、`otf_fft`、`mtf_fft`） | 多色复 OTF 按权重平均后取模 |
| 复测 | `src/saos/workflow.py`（`confirm_round`）、`src/saos/ledger.py`（`effective`、`confirmed`、`deliver_round`） | `saos round --confirm [--of N]` |
| 门禁 | `src/saos/daemon.py` | `OPTICSTUDIO_ONLY` = `round.confirm`，与公差扫描一起总在 OpticStudio 上算；只有 rtc 的后台进程拒绝它们。`lens.describe` 在 rtc 上出模型页，剖面图由 `model_report.section_layout` 画（2026-11-03） |
| 专家类 | `src/saos/ctx/experts/` | rtc 实例上可用：`AsphereExpert(engine="rtc")` 直接用实例的镜头；`GlassExpert` 的牌号坐标全部在 rtc 材料层算 |
| 界面 | `src/saos/ui/stage.py`、`src/saos/ui/static/app.js` | 轮次标 rtc / 已复测 / 复测不符；交付节点提示待复测；点列、光线扇、MTF 的 rtc 脚注 |

## 评价函数

- **值**：MF² = ΣW·(V−T)²/ΣW，只计 W > 0 的行。边界类操作数满足时值取目标，越界时取实际值（同 OpticStudio，`saos.ctx.rows` 的实测说明）。有正权重的行算不出时为 inf。
- **求值复用**：一次优化只编译一次结构分段、光瞳点和质心分组；每次测量只重算数值。像差行与普通光线操作数共享批量追迹，波前从同一批结果取光程与主光线，不再另追。导入行中的非有限数值按行号、列号报错，省略列仍补零。材料缓存跨系统保留目录玻璃与空气；连续变化的模型玻璃每次即时计算，不进缓存。
- **rtc 能算的行**：BLNK、DMFS、CONF；边界类 CTGT/CTLT、CVGT/CVLT、COGT/COLT、PMGT/PMLT、OPGT/OPLT，MNCT/MXCT、MNCG/MXCG、MNCA/MXCA、MNET/MXET、MNEG/MXEG、MNEA/MXEA；镜头数据 CTVA、CVVA、COVA、PMVA；算术 OPVA、SUMM、DIFF、PROD、DIVI、ABSO、SQRT、CONS；像差 TRAR、TRAX、TRAY、TRAC、TRCX、TRCY、OPDC、OPDX、OPDM；以及 `RtcBackend.operand` 的全部操作数。其余（ETGT、STHI、STRH、EERA、RWCE 等）整批拒绝，评价函数不变。
- **像差行的约定**：Int1 = 0、Int2 = 波长（≥ 1），Data1–4 = Hx、Hy、Px、Py。相对质心的行（TRAC、TRCX、TRCY、OPDX、OPDM）按“连续出现、同一操作数、同一波长、同一视场”分组，组内按行权重求参考：质心，或 OPDX 的 piston 与倾斜、OPDM 的均值。OPD 以波长为单位，参考球心在主光线像点、半径为出瞳距离。
- **范围类边界**：Int1、Int2 为首末面（都为 0 时为全部面），面后是玻璃、空气或任意介质。边缘厚度取两面半口径的较大值处，只支持 zone 0。
- **向导**：只做 RMS；数据 Wavefront、Spot Radius、Spot X、Spot Y、Spot X+Y，参考 Centroid、Chief Ray（Wavefront 另有 Mean）。高斯求积环半径 √((1+x)/2)（x 为 Gauss–Legendre 节点），臂角从 90° − 180°/臂数开始等分。环半径和臂角与 OpticStudio 24.1 存盘镜头里 4 环 8 臂的向导行一致（rtc `fixtures/lenses/codex-r017.zmx`）。行权重 = 视场权重 × 波长权重 × 环权重 / 臂数，**未对账**。`start_at` 之前的行保留。

## 局部优化（学 CODE V AUT）

- 目标行（非边界类、W > 0）组成残差 r = √W·(V−T)；边界类行组成不等式约束 g ≥ 0（范围类按面拆开）；模型玻璃的 nd ∈ [1.35, 2.2]、νd ∈ [15, 100] 也是约束。
- 每步求解 min ½‖J·d + r‖² + ½μ·dᵀD·d，满足 g_A + G_A·d = 0。D 为 JᵀJ 的对角（Marquardt 缩放）。有效集法：乘子为负的约束释放，线性化后被违背最多的约束加入，最多 3p+5 次增删。
- 接受准则：违背不增加且残差平方和下降，或者当前有违背时违背至少减小 0.1%。接受后 μ ÷ 3，否则 μ × 10，每次迭代最多加大 12 次。连续 2 次接受的相对下降都小于 1e-10，且解可行时，算作收敛。
- 雅可比用前向差分，追迹失败时改用后向差分。步长让矢高或折射率变化约 1e-7：曲率 2e-7/h²，EVENASPH 第 n 项 1e-7/h^(2n)，h 为半口径。
- 每次求值前检查预算与取消；异常、取消和超时都恢复最后接受点。
- `cycles`：auto → 至多 200 次迭代，1/5/10/50 → 同数迭代。`algorithm` 只有 dls。
- 实测（`tests/fixtures/cases/sequential.zmx`，三个曲率变量，向导 3 环 6 臂，玻璃 [1, 10, 1]、空气 [0.5, 50, 0.5]，94 行）：评价函数 61.76 → 0.631，15 次迭代、80 次求值，用时 0.52 s；初始违背 0.95，结束时 9.8e-10。

## Hammer 与全局搜索

- Hammer：在当前最好解附近扰动（σ = 0.5 × 典型幅度），有换料变量时以 0.5 的概率随机换一面玻璃，再局部优化 40 次迭代，更好就留下。
- 全局搜索：从起点大幅扰动（σ = 2），所有换料面同时随机换，局部优化，保留评价函数最小的 10 个。
- 两者都只比较满足全部约束的解，到 `max_seconds` 或取消为止；结束（含异常）时镜头是最好解，没有可行解时回到起点。
- 换料候选：所选目录（空为全部在用目录）里 nd、νd 离当前玻璃最近的 8 种（距离按 Δnd/0.03、Δνd/6 计），含当前玻璃。OpticStudio 是在整个目录里换。
- 找到的系统写在镜头文件旁（全局 `GLOPT_NNNN.zmx` 按评价函数从小到大，Hammer `<镜头>_0000_HAMMER.zmx`），由 daemon 归档到 `lenses/<kind>/NNN/`。全局搜索与 OpticStudio 相同；OpticStudio 的 Hammer 经 API 实测不写文件，结果只在内存里（2026-10-30）。
- 实测（同上镜头，两面换料）：Hammer 8 s 试了 26 个系统，0.6309 → 0.6177（换成 P-BK7、N-SF19）；全局搜索 8 s 试了 12 个系统，0.6177 → 0.5827。

## 结构确认与 .zmx 往返

- rtc 的 `describe_model(None)` 与 OpticStudio 字段相同（面型名按 OpticStudio 的 TypeName：Standard、Even Asphere、Paraxial、Biconic、Toroidal；Biconic Zernike 未实测）。在 `tests/fixtures/rtc/doublet.*` 上，结构指纹除 `unread` 的三项外与 OpticStudio 描述完全一致。
- 存盘写回：变量标记（`CURV` 第 2 列、`VDSZ 0 0`、`VCON`、`VPAR n`）、评价函数行（`OP Int1 Int2 D1 D2 D3 D4 Target Weight D5 D6`，`BLNK` 占行号）、公差行与多重结构行原样写回；文件头里 rtc 不建模的行原样带过去；面块里的显示类行（HIDE、MIRR、SLAB、POPS、FIMP、SSID）原样写回。
- 换料变量（模型玻璃、替换）只在内存里：OpticStudio 存盘写法没有实测样本。重新加载存过的镜头后要重新 `opt vary --param material`。

## 复测（`saos round --confirm`）

1. 在 OpticStudio 实例上运行，不需要先加载镜头。换镜头前同 `lens load`：未保存修改报 `UNSAVED_CHANGES`（`--discard` 放弃）；结束（含失败）时从文件重载原来的镜头，后端文件路径不变。默认复测 `saos report` 会交付的那一轮（`ledger.deliver_round`），`--of N` 指定。
2. 加载那一轮的归档镜头，核对模型确认（OpticStudio 的完整指纹，含 rtc 读不出的三项），按当前判定测量，检查判定没有改动镜头，查零件交叠。
3. 追加 `tier: confirm` 的记录：`confirm_of`、`agrees`、`verdict`（两边的达成与可交付）、`diffs`（逐项 rtc 与 OpticStudio 值）。达成、`hard_failing` 或可交付性不同则 `agrees: false`。
4. `ledger.load` 默认返回 `effective` 之后的轮次：复测数值换进第 N 轮（`confirmed_by`、`measured_on: zos`，原值在 `rtc`）。终止判断、流程图窗口和报告都用它；`raw=True` 给编号新记录用。
5. `saos report` 拒绝没有复测的 rtc 轮次。最近一次复测不符、之后没有新设计轮次时，判定返回 `continue`（`confirm_failed`），报告也拒绝，回到 rtc 实例继续设计；用户已停止时停止优先，报告照常生成，数值是复测后的 OpticStudio 值。重复复测始终与原始 rtc 结果比较。

## 未对账（交付前复测兜底）

- 所有评价函数行的值、向导行权重、优化路径（与 OpticStudio DLS 的步长与阻尼不同，局部极小可能不同）。
- FFT MTF：各波长以自己的主光线为参考，波长之间的垂轴色差不进 MTF（OpticStudio 以主波长主光线为参考）；采样 16–256。
- 点列 RMS / GEO：31×31 方形网格取单位圆内的点，相对主波长主光线，按波长权重合算；与 Standard Spot 的采样不同。
- `rtc.psf.opd_map` 的掩码改为排除追迹失败和被拦的光线（此前只看光瞳圆），对有渐晕的系统结果会变。

## 待办

- 对账：向导行权重、TRAC/OPDX 组参考、MNEG 等边缘厚度、FFT MTF，在 `tests/test_zemax_rtc.py` 用 OpticStudio 真值逐项比对后写入准入。
- 操作数：ETGT/ETLT/ETVA、STHI、STRH 等常见评价函数行。
- 换料变量的 .zmx 写法（采一份 OpticStudio 存盘样本后实现）。
- 多线程：优化目前单线程，`cores` 不用。
