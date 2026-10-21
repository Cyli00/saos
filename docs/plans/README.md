# saos 重构计划索引

## 当前综合计划

综合计划及完成状态的唯一入口：[saos 收敛与重构计划：先修契约，再完善设计流程](./refactor-optiland-first-workflow.md)。

2026-10-21 根据静态审查与主会话连续设计的要求重写。R0–R7 的实际实现和测试状态维护在综合计划第 13 节，不以已实现代替已验收。

下一轮执行参考：[重构剩余工作交接](./refactor-optiland-handoff.md)。依据最新交接报告，将剩余工作安排为 A 可靠性缺口、B 材料与选型、C 执行边界与宿主权限、D 数值性能及最终收尾；每批列明改什么、怎么改、查什么、测什么及完成条件。建议先执行 A。该文档补充综合计划，不另维护完成状态；本次文档更新没有执行测试。

选型证据与失效分层一轮（R3 部分、需求哈希分层、建模确认页）的改动与验证结论：[选型证据与失效分层：本轮交接](./refactor-optiland-selection-handoff.md)。同日用户改定 R3 方向为起点库、像差预判与诊断工具，关卡只留在昂贵处；该轮代码的去留和下一轮做法见交接文档第 4 节 B0、B2。

以下 Plan 1–4 保留为此前工作的历史背景。其中的孪生代算、自动 fallback、建模期双端对齐和强制双文件写出，以新综合计划的迁移方向为准；大范围工作台扩充后置。下文行数和性能数字是旧计划记录，不是当前仓库的重新统计或新验收结论。

本会话依据上轮缺陷清单进行的修复已按用户要求暂停，尚未完成最终回归：[选型证据修复：暂停交接](./refactor-optiland-selection-fixes-handoff.md)。文档同时指出这些关卡改动与最新 R3 方向的冲突，恢复前须先确定去留。

## 历史计划：默认后端改造与 Web 工作台

本目录记录将 `saos` 从“纯 ZOS-API 单一后端”演进为 **“Optiland 做建模、调试轮次与扫描的默认后端 + 用户认可结果后在 OpticStudio 中验证并继续优化 + 全功能 Web 光学设计工作台（`saos ui`）”** 的分阶段实施计划。决定与理由见 [ADR-0012](../adr/0012-optiland-default-opticstudio-final-stage.md)。

---

## 1. 为什么要做这次架构演进？

当前 `saos` 全库共 19,564 行代码，其中 `src/saos/backend/zos.py`（2,302 行）、`nsc.py`（1,190 行）、`parallel.py`（430 行）、`native_ui.py`（301 行）等超过 4,400 行代码耦合在 ZOS-API 上。实测暴露出以下效率瓶颈：

1. **进程启动与许可证上限限制并发吞吐**：单个 OpticStudio standalone 实例冷启动耗时 `5.8–7.9 s`；受本机许可证限制最多同时运行 8 个实例（含用户打开的桌面窗口），1、2、4 个实例的评估吞吐仅为 `88.7`、`161`、`304 次/秒`。
2. **Zemax 内置优化器在探索阶段又慢又难控制**：
   * `Hammer` 和 `Global Search` 不会自行停止（`TargetRunTimeM` 无效），必须靠后台线程每 `0.5 s` 轮询并调用 `Cancel()`；一旦异常未关掉工具就会锁死整个实例。
   * 玻璃替换在全目录中替换，Cooke 样例 12 次替换中有 9 次换出停产玻璃（`Obsolete`）。
   * 对需求单自定义判据的无梯度搜索极慢（光纤探头任务 18 变量 Nelder–Mead 跑满 1 小时仅完成 48 次评估）；显微物镜单次验收因反复往 MFE 插入/删除 66 项 `ZERN` 操作数耗时 `22 s`。
3. **显性调试（`extension` 模式）维护成本高且拖慢计算**：通过 Win32 `ctypes` + WPF `UIAutomation` 枚举窗口、检测模态弹窗、拦截未保存修改；打开实时刷新会让一次评估从 `22 s` 劣化到 `98 s`。

引入纯 Python、支持 PyTorch 自动微分的 **Optiland 0.6.3** 作为建模、调试轮次、扫描、结构探索与优化的默认后端，探索阶段不占许可证。OpticStudio 不在每一轮里复算：调试轮次跑完、报告交给用户看过、用户认为结果没问题之后，镜头才交给 OpticStudio 复测（OpticStudio 验证），并在 OpticStudio 里用 Zemax 的手段继续优化、轮次上限重新给，最终交付以这一阶段的数值为准。这样既保住“交付数值有 OpticStudio 实测依据”的底线，又只为用户认可的设计占用许可证。提速的幅度是目标，基线见各计划的“待测”项。

---

## 2. 核心架构原则

1. **单一模块、单一技能，按计算特征选执行方式**：
   * 光学追迹要求双精度（`float64`）。本机 GTX 1060 6GB 的 FP64 算力仅为 FP32 的 1/32（约 `120 GFLOPS`），常规序列追迹单次仅数千条光线、按面串行，在消费级显卡上 CUDA 受制于 Kernel 启动开销，默认 CPU。
   * 多变量局部优化的加速来自 PyTorch `Autograd`：CPU FP64 上 1 次前向 + 1 次反向拿到可微目标对全部变量的梯度；只有可微的目标才能走这条路（见 Plan 2）。
   * 全库只维护一个 `src/saos/backend/optiland.py` 和唯一入口技能 `skills/saos/SKILL.md`。
2. **两个阶段，OpticStudio 放在用户认可之后**：
   * **Optiland 阶段**：需求单、建模与模型确认、调试轮次、扫描都在 Optiland 上做；建模说明时做一次双端基准对齐，不一致写进建模说明，由用户在确认模型时判断。报告注明数值由 Optiland 计算、尚未经 OpticStudio 验证。
   * **OpticStudio 阶段**：用户认可报告后（原话记录），镜头按伴随文件里的处方在 OpticStudio 中重建，用同一套判定复测，与 Optiland 逐项比较；之后在 OpticStudio 里继续优化（阻尼最小二乘、Hammer、全局搜索、worker 池扫描与公差），轮次上限重新给。两端不一致时以 OpticStudio 为准，差别写进报告。
3. **不另加快照机制**：任务里已有每轮归档（`lenses/rounds/`）、窗口副本（`lenses/captures/`）、`kit.restored` 和封存（`seals.jsonl`）四种快照，新的优化器沿用它们，agent 在大改前用 `lens save --as` 留副本。
4. **仓库级 Git 快照**：大改前封存 `archive/zos-only` 分支与 `v0.1.0-zos-only` 标签；每完成一个 Plan 打一个阶段标签（`v0.2.0-plan1` 至 `v0.2.0-plan4`）。

---

## 3. 四步走分阶段计划目录

| 阶段 | 文档 | 核心目标 | 关键产出 |
|---|---|---|---|
| **Plan 1** | [plan-1-optiland-core.md](./plan-1-optiland-core.md) | Optiland 核心后端、`kit.py` 原语下沉与通用物理补丁 | `checks/` 脚本在双后端不改代码通跑；建模说明的双端基准对齐跑通；`daemon start` 与显微物镜单次评估的耗时目标见计划 |
| **Plan 2** | [plan-2-opt-and-scan.md](./plan-2-opt-and-scan.md) | Optiland 优化器（SciPy/Torch）、在产玻璃替换与零许可证并发扫描 | 局部/全局优化与 `opt glass` 在产玻璃替换落地；`scan` 6 种模式在 Optiland worker 上运行；变量与评价函数随镜头保存 |
| **Plan 3** | [plan-3-zos-verifier-and-cleanup.md](./plan-3-zos-verifier-and-cleanup.md) | 用户认可后的 OpticStudio 验证与继续优化阶段，清理探索阶段不再需要的 ZOS 代码 | 进入阶段的命令与确认记录、复测与逐项对比、OpticStudio 阶段的轮次与上限、以 OpticStudio 数值交付的报告；四处工作流同步 |
| **Plan 4** | [plan-4-ui-workbench-and-sync.md](./plan-4-ui-workbench-and-sync.md) | Web 光学设计工作台（`saos ui` 6 大模块扩充） | 指标看板、赛德尔逐面像差/光线扇图/场曲畸变/玻璃图、双轮次叠影对比、探索面板、OpticStudio 验证对比 |
