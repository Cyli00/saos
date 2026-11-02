---
status: accepted; 修订 ADR-0017 第 4 条（独立 rtc 实例）；阶段分工由 ADR-0020 修订（建模与模型确认归 rtc，固定后端限于当时的阶段）
---

# 一个后台进程持有 rtc 与 OpticStudio，按流程阶段切换并逐值比对交接

2026-10-30，用户指出混合模式下 rtc 与 OpticStudio 之间的交接繁琐、检查不足：每个任务要手动切换实例 4–5 次（`lens save`、`daemon stop`、`daemon start [--backend rtc]`、`lens load`），而设计阶段经过的两条交接通道只比结构指纹，rtc 读入 OpticStudio 的文件后还用自己重算的求解厚度和半口径覆盖原值，差异要到交付前复测才以“结论不同”间接暴露。用户决定改为一个后台进程持有两个后端，并拍板两项细节（见下）。本决定修订 ADR-0017 第 4 条；ADR-0013、0014、0016、0017 关于“哪个阶段由谁计算、交付数值只来自 OpticStudio”的规则不变。

## 做法

- **两个后端，一个进程**：`saos daemon start`（默认 `--backend auto`）立即建好 rtc；第一次需要 OpticStudio 时才启动它，之后一直开着到后台进程退出（用户决定：切换最快，代价是设计阶段也占一个许可）。`--backend zos` 与显式调试模式在启动时就连上 OpticStudio；`--backend rtc` 仍是只有 rtc 的实例。
- **按流程阶段选后端**：每个带任务的操作按 `saos.ui.stage` 的当前阶段选——访谈、需求确认、设计用 rtc，建模、模型确认、交付用 OpticStudio；`OPTICSTUDIO_ONLY` 的操作和公差扫描总在 OpticStudio 上。`saos daemon use rtc|zos` 固定后端（需要 OpticStudio 的操作仍用它），`auto` 放开。只读状态的操作和 `lens save` 不切换。
- **交接走文件，再逐值比对**：切换时当前镜头不能有未保存修改（`UNSAVED_CHANGES`，同 `lens load`）；另一边从同一个 `.zmx` 加载，然后比较 OpticStudio 的 `describe_model(None)`（参数、各面各波长 INDX、环境、视场、波长）、半口径（物面除外：rtc 不重算物面半口径，OpticStudio 在物距无穷时给 inf，两边追迹都不用它；2026-10-30 真实 OpticStudio 上 0 对 inf 时 126 条光线位置差 ≤ 1.6e-14 mm），以及每个视场、每个波长的主光线和 5×5 光瞳光线，容差同 `ctx.fast()`（ADR-0014）。rtc 的求解厚度和自动半口径是重算后再比的，所以“重算值与 OpticStudio 原值”的差别在这里出现。比对在 `saos.handover`，与 `ctx.fast()` 共用光线比对代码。
- **不一致仍交给 rtc**（用户决定）：差异记在 rtc 镜头上，进入 `provenance` 的 `unverified`，随轮次记录，由交付前复测兜底；结果的 `backend_switch.handover` 列出每一项。rtc 复现不了的镜头（反射镜、坐标断点等）留在 OpticStudio 上算，`backend_switch.stayed` 写明原因，同一文件不重试。
- **worker 跟着走**：worker 池的后端随后台进程当前的后端切换；有分离扫描在跑时拒绝切换（`BUSY`）。

## 代价与实测

- 交接比对在 rtc 一侧约 1 ms（`tests/fixtures/cases/sequential.zmx`，5 面、3 波长、3 视场：加载含重算 2.4 ms、INDX 0.15 ms、9 个视场×波长各 14 条光线 0.82 ms，2026-10-30 本机）。OpticStudio 一侧没有单独实测；上限参考 ADR-0015：建副本、逐值比对加排名合计 1.39 s。一个任务交接约 3–4 次。
- 每个带任务的操作多一次阶段判断，新任务上约 1 ms（`stage.snapshot` 0.70 ms；`lens.operand` 自动路由 2.81 ms，固定后端 1.82 ms）；轮次多时的开销未测。
- OpticStudio 的冷启动（6–8 s）在第一次需要时发生，之后不再重复。

## 后果

- 技能文档里的实例切换表删去；agent 只在交接前保存镜头。
- 结果里的 `backend_switch` 说明这次操作前换了后端、交接比对是否一致；流程图窗口标出当前在哪个后端上算、OpticStudio 是否已启动。
- 只有 rtc 的实例（`--backend rtc`）保留，用于没有 OpticStudio 的机器。
