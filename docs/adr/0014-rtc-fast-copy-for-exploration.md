---
status: accepted; ADR-0016 lets need-stage feasibility use admitted rtc values; ADR-0017 runs design rounds on the rtc instance itself (no copy needed there)
---

# 脚本可用 rtc 副本做高频探索，判定与交付仍在 OpticStudio

用户于 2026-10-26 要求把 rtc（ray tracing core，`../rtc` 仓库：自研 float64 序列追迹内核，见其 ADR-0001）整合进 saos 并解决加速问题。本决定在 ADR-0013 上加一条例外：exec 脚本和 worker 可以用 `ctx.fast()` 取得当前镜头的 rtc 内存副本，在里面高频改参数、追迹、算点列；判定（`checks/`、`ctx.measure`）、轮次、扫描计划和报告不变，仍由 OpticStudio 计算。

## 做法

- **一份代码**：saos 以可编辑路径依赖（`[tool.uv.sources] rtc = { path = "../rtc", editable = true }`）引用 rtc，不复制源码。rtc 的 golden 回归（`tools/compare_golden.py`）验证的就是 saos 运行的那份代码。
- **镜头从 OpticStudio 来**：`ZosBackend.export_rtc` 用 `CopySystem` 另存副本（当前系统的文件名与保存状态不变），同时返回 `describe_model`。`saos.backend.rtc.RtcBackend` 读副本后逐项核对：半径、厚度、圆锥、非零 Par 列、各面各波长折射率（INDX，容差 1e-8）、视场与归一化、波长、孔径和瞄准方式。不一致报 `VERIFY_FAILED`。
- **复现不了就拒绝**：EPD 以外的系统孔径（物方 NA 的发射与瞄准没对上，见下）、反射镜、坐标断点和面倾斜偏心、未与 OpticStudio 对账的面型（Binary 2、Fresnel 等）、有限物距、近轴瞄准、非角度视场、视场渐晕因子、THIC 以外的多重结构操作数、圆孔和浮动孔径以外的面孔径，一律报 `BACKEND_UNSUPPORTED`，不近似。
- **材料按准入表放行**：rtc 材料层有 13 类色散、目录温压修正与 nd/Vd/dPgF 模型，但副本只使用与 OpticStudio 对过账的计算路径（`src/saos/backend/rtc_admitted.json`，键为「公式 × 环境模式」或「模型 × 环境模式 × 是否带 ΔPgF」）。不在表里的路径，只有建立副本时该材料在该波长的 INDX 与 rtc 差 ≤ 1e-8 才可用；换材料、改波长前逐波长检查，不通过报 `BACKEND_UNSUPPORTED` 且副本不变。2026-10-27 准入：空气、公式 1（间接证据）与公式 2 在「不修正，或参考温度且 1 atm」下、ΔPgF = 0 的模型玻璃。2026-10-28 用 `saos.rtc_golden` 扩充：公式 1、2、3、5 的温度与压力修正路径和公式 3、5 的不修正路径，以及模型玻璃的压力修正与带 ΔPgF 的路径。依据是 OpticStudio 24.1.0 的 1,001 个 LD 内 INDX 点，环境为 -40/20/25/60 °C × 0/0.5/1 atm（开修正）加 20 °C、1 atm（关修正），rtc 与之全部逐位相同。同日再扩充公式 6、7、8、9、11、12、13 的三种路径（公式 13 缺压力修正，样品参考温度 23 °C 不在采样温度里），样品增至 14 种目录玻璃，LD 内 1,677 点仍逐位相同；公式 4、10 在本机 Glasscat 里没有玻璃，未准入。修正关闭时 OpticStudio 不接受温度、压力的写入，采集须先开修正再写数值，处方也因此拒绝修正关闭时的非默认温度、压力。新路径由 `saos.rtc_golden` 采集 INDX 后用 `--admit` 写入。目录里没有的牌号、无效 nd/vd 是 `BAD_ARGS`（与 ZOS 后端一致）。
- **建立时追迹对照**：参数核对之后，每个视场、每个波长追主光线和 5×5 光瞳，两边位置差超过 1e-6 mm、方向余弦差超过 1e-7 或有光线只在一边可用，就报 `VERIFY_FAILED`；结果留在 `fast.agreement`。
- **求解**：拾取、边缘光线高度、F 数等求解在改参数后 OpticStudio 会重算，rtc 不会。镜头带这类求解时，副本默认拒绝改参数；`ctx.fast(freeze_solves=True)` 让它们保持存盘值，被锁住的格本身写入时报 `VALUE_NOT_APPLIED`。
- **同一定义**：副本继承 kit 的方法（`trace`、`spot`、`set`、`kept`、`configuration`、`cra` 等），点列等统计跑的是与 `ctx` 相同的 kit 代码，追迹结果按 OpticStudio 批量追迹的错误码、渐晕码约定交回。模型玻璃的 `material` 在两端都读作 `{nd, vd, dpgf}`。
- **回到 OpticStudio**：`fast.push(ctx)` 把改过的参数写回 OpticStudio，一面的 nd/vd/dpgf 合成整块 `material` 写回，避免 OpticStudio 用自己目录的值补齐另一项；`fast.compare(ctx, …)` 在两边参数相同时逐条比较同一批光线。判定脚本的上下文（`ctx.child`）拒绝 `fast()`。
- **编译内核**：rtc 用 numba 编译的逐光线内核，numpy 实现保留为参照（`RTC_KERNEL=numpy`）。首次编译约 6 s，之后的进程从缓存加载约 1 s。

## 实测依据

样品是 `tests/test_zemax_rtc.py` 的 4 面双胶合（N-BK7/N-SF2，第 3 面偶次非球面，视场 (0,0)/(0,3.5°)/(2°,5°)，F/d/C 三波长）。2026-10-26，OpticStudio 24.1：

- 光阑在第 1 面、不瞄准：600 条光线到像面的位置差 ≤ 1.4e-14 mm，方向余弦差 ≤ 1e-15。49 点曲率扫描（每点 4 视场 × 3 波长、21×21 光瞳的点列）OpticStudio 5.5–5.7 s。
- 光阑在镜后、实际瞄准：像面位置差 ≤ 1.2e-9 mm，第 2 面 ≤ 5.7e-8 mm；差别来自 OpticStudio 自己的瞄准收敛（它让 px = 1 的光线落在光阑半径以内 2.8e-9 处，rtc 落在半径上）。改参数并写回后仍在 1e-6 mm 容差内一致。
- 真实路径：`saos exec` 脚本里的 `ctx.fast()` 与 `ctx.map` worker 进程里的 `w.fast()`，建立时 126 条光线位置差 ≤ 1.5e-9 mm，9 个视场×波长的点列 RMS 相对差 ≤ 3e-10；worker 里改曲率并写回后同样一致。
- 物方 NA：piB（OBNA 0.12 + 实际瞄准）走 rtc 自己的瞄准时 10 条光线 9 条不收敛，位置差 0.096 mm，所以拒绝。
- 玻璃库：`SAOS_CDGM_INPROD` 有 11 种、`SAOS_NHG_INPROD` 有 10 种 Herzberger 公式玻璃，当日未准入；SCHOTT、CDGM、NHG 的参考温度均为 20 °C，OHARA 有 427 种、HOYA 有 148 种为 25 °C。
- rtc 的 PARAXIAL 光程与 golden 差 2.96e-2 mm（piA、codex），是 rtc 已知问题，副本因此不提供 OPD/波前。

2026-10-27，本机离线、同一扫描、三次取最好：编译内核加 saos 侧介质缓存与向量化点列后，不瞄准 0.122 s（此前 numpy 版 0.362 s），光阑在第 3 面实际瞄准 0.210 s（强制 numpy 1.33 s）。内核与 numpy 参照逐条比较：不瞄准完全相同，实际瞄准位置差 ≤ 7.1e-15 mm。与上面 OpticStudio 的 5.5–5.7 s 不是同一次运行，只作量级参考。

## 后果

- 探索脚本的内层循环（扫描、多起点、`saos.search.minimize` 的目标函数）可以不占 OpticStudio 许可、不排队；结果在 OpticStudio 上实测后才能进入轮次或决定。
- rtc 有了第二个用户，改动它的追迹约定要同时跑 rtc 的 golden 回归和 saos 的 `tests/test_zemax_rtc.py`。
- 光学数值的「后端」多了 `rtc`；任何写进笔记、消息的 rtc 数值都要标明，不能当作 OpticStudio 实测。
- 不在本决定内：操作数、分析、OPD/PSF/STRH、多重结构单元格的写入、DLS。需要时按 rtc 的回归准入逐项接入。
