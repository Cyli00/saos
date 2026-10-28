---
status: accepted
---

# 专家类可在 rtc 里排名非球面、算玻璃坐标，接受仍看 OpticStudio

2026-10-28，用户提出把专家类（`ctx.GlassExpert`、`ctx.AsphereExpert`）的光学追迹放进 rtc，并定下四点：

- 目标：用光线残差排名，再由 ZOS 复核；
- 分工：算法留在 saos，rtc 只补通用能力；
- 文档：另写本 ADR；
- 范围：先做 GlassExpert 建玻璃库和 AsphereExpert 排名。

本决定扩展 ADR-0014 的探索例外，不改变它的任何条款。

## 做法

- **非球面排名**：`ctx.AsphereExpert(..., engine="rtc", grid=7)`。流程：
  1. 候选面先在 OpticStudio 上转成零系数偶次非球面（光学上不变），再整份交给 `ctx.fast(freeze_solves=True)` 等价的副本。副本建立时照 ADR-0014 与 OpticStudio 做追迹对照。
  2. 残差：每个视场、每个有效波长，取 `grid×grid` 方格中落在单位圆内的光瞳光线，算它们在像面相对主波长主光线的横向偏差 x、y，按 √(视场权重 × 波长权重) 加权。主光线取主波长，所以残差里包含横向色差。
  3. 雅可比：一次 `rtc.adtrace.trace_ad` 前向 AD 追迹得到，变量是候选面的 `par2…` 系数加上像面前一面的厚度。厚度这一列作对焦补偿，从残差和其余各列里投影掉。
  4. 排名用的线性投影（`ranking.py`）与 `engine="opticstudio"` 相同，也同样支持斜率预算。
  - 只接 EvenAspheric 的 `par2…par12`，不接 `residual`、`slope_vector` 回调：rtc 排名用自己的残差定义，不去模仿 MFE。
  - 实际瞄准（RAIM 2）时 `trace_ad` 把发射当常量，光阑前的面改变瞄准却不进导数，所以这时光阑前有候选面就报 `BACKEND_UNSUPPORTED`。
- **接受仍在 OpticStudio**：`run()` 每选一个面，照旧在 OpticStudio 上把系数设为变量，按镜头 MFE 局部优化，再按 MFE 判定是否接受。rtc 只决定先试哪个面，排名数值不进轮次，不作证据。两种 engine 的残差定义不同，排名可能不同。
- **玻璃坐标**：`GlassExpert` 建库时，色散路径在 `rtc_admitted.json` 准入表内的目录玻璃，由 `saos.backend.rtc.catalog_indices` 在 rtc 材料层直接算坐标波长上的折射率，不换料、不追迹。其余玻璃（未准入公式、环境修正不在准入范围等）照旧在 OpticStudio 上逐片换料、测 INDX。实际各用了多少片记在结果的 `coordinate_sources`。
- **rtc 侧的通用修补**（保持算法在 saos）：`RtcBackend.optical_system(wave)` 把内核系统交给探索代码。AD 路径此前没有端到端用过，这次修了 5 处：
  - `Dual` 二元运算与 `where` 对齐值形状与参数轴：常数（P = 0）一侧补零切向，标量参数与光线数组相乘时不再按尾部对齐错位；
  - 折射后方向余弦的解包；
  - 隐函数求导里的 F 改用面顶点局部坐标；
  - `Design.system_with` 改厚度后重算顶点位置；
  - 面形 sag 跳过零系数时保留 `Dual` 系数，因为排名正是在零系数处求导。

## 实测依据

2026-10-28，本机离线（未启动 OpticStudio），rtc 的 `tests/fixtures/rtc/doublet.zmx`（第 3 面偶次非球面，EPD，不瞄准）：

- AD 与中心差分（差分用 `Design.system_with` 加普通 `trace`）在 0° 与满视场、13 条光瞳光线（5×5 方格落在单位圆内的点）上比较：

  | 变量 | AD 与差分最大差 | 导数最大值 |
  |---|---|---|
  | `par2` | 2.3e-6 | 3.7e4 |
  | `par3` | 5.4e-4 | 1.5e6 |
  | 曲率 | 1.0e-8 | 3.2e2 |
  | 厚度 | 9.6e-12 | 1.5e-1 |

  相对差都 ≤ 4e-10。AD 的值与普通追迹逐位相同；批量追迹与逐条追迹的切向逐位相同。
- 排名核心（3 波长 × 3 视场 × 29 条光线，522 行，4 个系数 + 对焦列）一次 0.17 s，含读镜头。投影后残差与对焦列的内积 4.6e-16。
- `catalog_indices`：SCHOTT 的 N-BK7、N-SF5、F2、N-LAK22 在 20 °C、1 atm、按环境修正时走准入路径，N-BK7 的 nd 算得 1.5168000。25 °C 时路径为 `catalog:2:thermal`，未准入，退回 OpticStudio。

2026-10-28 真机实测（OpticStudio 24.1.0，`tests/fixtures/cases/sequential`，各 3 次）：

- `SAOS_RUN_ZEMAX=1 uv run pytest tests/test_zemax_rtc.py tests/test_zemax_experts.py -q`：14 passed、1 skipped，含 rtc 玻璃坐标与 INDX 逐片对照（≤ 1e-8）、`engine="rtc"` 排名后镜头完整恢复。
- AsphereExpert（第 1、3 面，2 项系数）：`engine="opticstudio"` 3.42–3.43 s，`engine="rtc"` 首次 2.40 s（含 numba 编译与建副本）、之后 1.39 s，约快 2.5 倍。
  两者排名不同：OpticStudio 为 [1, 3]，rtc 为 [3, 1]。rtc 的残差是光线横向像差，不是镜头 MFE，只作排名参考，接受与否仍看 OpticStudio 上的实测。
- GlassExpert 建库（SCHOTT 在产 128 种，候选 11 个）：rtc 坐标 0.24 s，全部换料测 INDX 2.64–3.30 s，约快 11 倍；两种来源给出同样多的候选。

## 后果

- 非球面排名在 rtc 里只需一次 AD 追迹，OpticStudio 上的有限差分对每面每个系数要改参数、重算 MFE 2 次。小镜头上实测快约 2.5 倍、建库快约 11 倍（见上）；更大镜头的倍数未测。
- rtc 的 `adtrace`、`dual`、`geometry` 有了第二个用户，改动它们要同时跑 rtc 的 golden 回归、`tests/test_adtrace.py`（piA 上 AD 对中心差分、`Dual` 广播恒等式）与 saos 的 `tests/test_zemax_experts.py`。
- 带坐标断点、反射镜、NA 孔径等 ADR-0014 拒绝的镜头，`engine="rtc"` 在建副本时报 `BACKEND_UNSUPPORTED`，此时改用默认的 `engine="opticstudio"`。
- 不在本决定内：GlassExpert 的性能扫描、AsphereExpert 的局部优化、rtc 内的 DLS 优化。
