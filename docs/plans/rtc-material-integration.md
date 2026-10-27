# rtc 材料集成与加速：交接

rtc（ray tracing core，并列仓库 `../rtc`）提供材料层与编译追迹内核；本文只记 saos 侧行为、准入流程与运行命令。
决定与实测数字见 ADR-0014，用法见技能 `references/computing.md`。

## saos 侧行为

- **准入表** `src/saos/backend/rtc_admitted.json`：键是 rtc 的 `Medium.path(环境)`，如 `catalog:2:fixed`、`catalog:13:thermal`、
  `model:pressure:dpgf`。`fixed` = Adjust Index 关闭，或打开但系统温度等于玻璃参考温度且 1 atm（模型玻璃：1 atm）。
  不在表里的路径只在建立副本时 INDX 与 rtc 差 ≤ 1e-8 的 (材料, 波长) 上可用（`RtcBackend.confirmed`）。
- **建立顺序**：文件与描述的适用范围 → 逐项核对（含 ENVD 与 INDX，一致的记入 confirmed）→ 逐波长建系统（准入在这里生效）。
- **换材料、改波长**：逐个有效波长先查准入、再算折射率，全部通过才写入；失败时材料、波长与系统缓存都不变。
- **错误码**：牌号不在在用目录、nd ≤ 0 或非有限、材料字典键不对 → `BAD_ARGS`；越过 LD、公式未实现、未准入、MIRROR/ZTG → `BACKEND_UNSUPPORTED`。
- **材料值**：`get("N.material")` 在 ZOS 与 rtc 上对模型玻璃都返回 `{nd, vd, dpgf}`；rtc 的 `surfaces()` 写成文字 `MODEL nd vd dpgf`。
  `fast.push` 把 nd/vd/dpgf 的改动合成整块 `N.material` 写回。LD 不覆盖 F–C 线的目录玻璃（红外材料）没有可转模型的 nd/Vd，改 nd/vd 时拒绝。
- **加速**：rtc 内核由 numba 编译（`RTC_KERNEL=numpy` 改走 numpy 参照实现，`RTC_THREADS` 设线程数，缺省为逻辑核数减 2）；
  RtcBackend 跨改参数复用介质与折射率缓存，非主波长系统借主波长系统取光阑半径；`kit.spot` 用数组组织光线（ZOS 后端同样受益）。

## 扩大准入

```bash
uv run python -m saos.rtc_golden --catalogs SCHOTT OHARA HOYA CDGM NHG MISC --output E:/gitProjects/saos/work/rtc-material-golden
uv run python -m saos.rtc_golden --admit E:/gitProjects/saos/work/rtc-material-golden/summary.json
```

采集只开新的 standalone 会话，不连接用户桌面镜头；输出目录须不存在。每个 (公式, 参考温度) 取一种目录玻璃，加 4 组模型玻璃，
4 个温度 × 3 个压力 × 2 个开关 × 8 个波长，默认上限 20,000 点。summary 按路径汇总，只统计 LD 内的点，路径上每点 ≤ 1e-8 才算通过；
LD 外的点只记录 OpticStudio 的值。某个环境失败记入 `failed_environments` 并继续。`--admit` 只接受 status 为 recorded 的 summary，
把通过且尚未收录的路径连同证据写入准入表。

## 已运行的检查（2026-10-27，本机离线，只用简单系统）

- `uv run pytest tests/test_rtc_material.py tests/test_rtc_backend.py -q`：32 passed。
- `uv run pytest tests/test_kit_parallel.py tests/test_model_report.py tests/test_workflow.py -q`：129 passed。
- rtc 仓库 `uv run pytest -q`：48 passed。
- 编译内核与 numpy 参照在 4 面双胶合上逐条比较（每波长 3,000 条随机光线）：不瞄准完全一致，光阑在第 3 面实际瞄准时位置差 ≤ 7.1e-15 mm。

## 未运行 / 待办

- `SAOS_RUN_ZEMAX=1 uv run pytest tests/test_zemax_rtc.py -q`（需要 OpticStudio）未运行；`saos.rtc_golden` 未运行，准入表只有已有证据的 4 条路径。
- rtc 的 golden 回归（`tools/compare_golden.py`）未运行；编译内核对 BICONIC/TOROIDAL/PARAXIAL 只与 numpy 实现同构，未在这些面型上逐条比较。
- rtc 的 numpy 追迹在 PARAXIAL 面之后不更新当前折射率（`trace.py` 中 `n_cur = ... if not np.isscalar(n2) else n_cur`），内核按原样保留；
  面后不是空气时是否正确未核对，可能与已知的 PARAXIAL 光程差有关。
- 仓库目录已改名 `rt-calc` → `rtc`（2026-10-27）；别处引用旧路径的脚本或会话需要改成 `../rtc`。
