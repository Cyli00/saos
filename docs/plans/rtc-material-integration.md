# rtc 材料集成与加速：交接

rtc（ray tracing core，`src/rtc/`）提供材料层与编译追迹内核；本文只记 saos 侧行为、准入流程与运行命令。
决定与实测数字见 ADR-0014，用法见技能 `references/scripting.md`。

## saos 侧行为

- **准入表** `src/saos/backend/rtc_admitted.json`：键是 rtc 的 `Medium.path(环境)`，如 `catalog:2:fixed`、`catalog:13:thermal`、
  `model:pressure:dpgf`。`fixed` = Adjust Index 关闭，或打开但系统温度等于玻璃参考温度且 1 atm（模型玻璃：1 atm）。
  不在表里的路径只在建立副本时 INDX 与 rtc 差 ≤ 1e-8 的 (材料, 波长) 上可用（`RtcBackend.confirmed`）。
- **建立顺序**：文件与描述的适用范围 → 逐项核对（含 ENVD 与 INDX，一致的记入 confirmed）→ 逐波长建系统（准入在这里生效）。
- **换材料、改波长**：逐个有效波长先查准入、再算折射率，全部通过才写入；失败时材料、波长与系统缓存都不变。
- **错误码**：牌号不在在用目录、nd ≤ 0 或非有限、材料字典键不对 → `BAD_ARGS`；越过 LD、公式未实现、未准入、MIRROR/ZTG → `BACKEND_UNSUPPORTED`。
- **材料值**：`get("N.material")` 在 ZOS 与 rtc 上对模型玻璃都返回 `{nd, vd, dpgf}`；rtc 的 `surfaces()` 写成文字 `MODEL nd vd dpgf`。
  `fast.push` 把 nd/vd/dpgf 的改动合成整块 `N.material` 写回。LD 不覆盖 F–C 线的目录玻璃（红外材料）没有可转模型的 nd/Vd，改 nd/vd 时拒绝。
- **加速**：rtc 内核由 numba 编译（`RTC_THREADS` 设线程数，缺省为逻辑核数减 2；numpy 参照实现已于 2026-11-05 删除，见 `rtc-unify.md`）；
  RtcBackend 跨改参数复用介质与折射率缓存，非主波长系统借主波长系统取光阑半径；`kit.spot` 用数组组织光线（ZOS 后端同样受益）。

## 扩大准入

```bash
uv run python -m saos.rtc_golden --catalogs SCHOTT OHARA HOYA CDGM NHG MISC INFRARED BIREFRINGENT LZOS HIKARI MICRO_RESIST_TECHNOLOGY DOWSIL_MS --output E:/gitProjects/saos/work/<新目录>
uv run python -m saos.rtc_golden --admit E:/gitProjects/saos/work/<新目录>/summary.json
```

采集只开新的 standalone 会话，不连接用户桌面镜头；输出目录须不存在。每个 (公式, 参考温度 20/25 °C) 取一种目录玻璃（20、25 °C 都没有的公式取其它参考温度；名字含非 ASCII 字符、LD 不含主波长 0.55 µm 的不取），加 4 组模型玻璃，
开修正时 4 个温度 × 3 个压力、关修正时只取 20 °C、1 atm（关修正时 OpticStudio 不接受温度、压力的写入，所以先开关、后数值，并读回核对），× 8 个波长（目录玻璃只取不短于其 LD 下限的波长：有一个更短时 OpticStudio 不采用该材料，INDX 全是 1，采集把这种环境记为失败），默认上限 20,000 点。summary 按路径汇总，只统计 LD 内的点，路径上每点 ≤ 1e-8 才算通过；
LD 外的点只记录 OpticStudio 的值。某个环境失败记入 `failed_environments` 并继续。`--admit` 只接受 status 为 recorded 的 summary，
把通过且尚未收录的路径连同证据写入准入表。

## 已运行的检查（2026-10-27，本机离线，只用简单系统）

- `uv run pytest tests/test_rtc_material.py tests/test_rtc_backend.py -q`：32 passed。
- `uv run pytest tests/test_kit_parallel.py tests/test_model_report.py tests/test_workflow.py -q`：129 passed。
- rtc 仓库 `uv run pytest -q`：48 passed。
- 编译内核与 numpy 参照在 4 面双胶合上逐条比较（每波长 3,000 条随机光线）：不瞄准完全一致，光阑在第 3 面实际瞄准时位置差 ≤ 7.1e-15 mm。

## 未运行 / 待办

- 2026-10-28 已运行：`saos.rtc_golden`（SCHOTT OHARA HOYA CDGM NHG MISC，10 个样品、1,040 点，LD 内 1,001 点与 INDX 全部逐位相同）并 `--admit` 新增 13 条路径；`SAOS_RUN_ZEMAX=1 uv run pytest tests/test_zemax_rtc.py -q` 6 passed、1 skipped；rtc 的 `tools/compare_golden.py` 位置差 ≤ 1.1e-11 mm，OPL 差 2.96e-2 mm 为已知的 PARAXIAL 问题，`--aim` 下 piB 不收敛同 ADR-0014。
- 证据已归档到 rtc 的 `fixtures/golden/material-indx`（summary.json、indx.jsonl 与 README），`tests/test_material_golden.py` 逐点回归；以后扩大准入照此归档，summary 现在会记下 `catalogs` 与每个样品的 `catalog`。
- 2026-10-28 第二次采集（SCHOTT OHARA HOYA CDGM NHG MISC INFRARED BIREFRINGENT LZOS HIKARI MICRO_RESIST_TECHNOLOGY DOWSIL_MS；14 种目录玻璃 + 4 组模型、1,807 点，LD 内 1,677 点逐位相同）新准入公式 6、7、8、9、11、12、13 的 20 条路径，golden 已替换为这次的数据，准入表里 33 条证据改指它。仍未覆盖：公式 4、10（本机 Glasscat 没有这两种公式的玻璃）、公式 3 参考温度 25 °C 的玻璃、`catalog:13:pressure`。
- 编译内核对 BICONIC/TOROIDAL/PARAXIAL 当时只与 numpy 实现同构；numpy 实现删除后，这些面型由 `tests/rtc/test_omnibus_lock.py` 的快照和差分校验覆盖，`reflective_biconic`、`reflective_toroidal`、`reflective_paraxial_after` 等变体与 OpticStudio 逐条比较（`tests/rtc/test_case_truth.py`）。
- 已修（2026-10-28）：PARAXIAL 面的出射规则改为 OpticStudio 实测的 `n'·tan u' = n·tan u − y/f` 并更新当前折射率（面后为玻璃时
  此前像面差 2.6 mm）；光程差 2.96e-2 mm 是理想透镜相位混进了 opl，与折射率无关，现在 opl 与批量追迹同口径（≤ 2.1e-12 mm），
  相位在 `RayBlock.phase`。saos 建副本时不再拿 PARAXIAL 面的 conic 核对（ZOS-API 读作 inf、文件为 0），此前带 PARAXIAL 面的镜头都建不了副本。
  见 [rtc/SURFACE-PARAMS.md](rtc/SURFACE-PARAMS.md)「其它实测事实」。
