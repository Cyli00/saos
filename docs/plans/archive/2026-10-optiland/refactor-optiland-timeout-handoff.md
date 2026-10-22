> 历史归档（2026-10-22）：本文保留当时的方案、实施与测试记录，不再是执行入口。当前待办见[整合计划](../../integration-workflow.md)。正文中的“当前”“下一轮”、路径与执行指令均属于原记录；未完成事项的去向以新计划为准。

# 深度优化超时：历史验证与后续修复（当前实现待送测）

日期：2026-10-21（当晚）。仓库 HEAD `6d04ccc`，工作树只有文档改动。本轮范围是定位 E01 上 `--max-seconds` 不生效的位置、核对结果语义、给出可执行的验收用例；**没有保留任何源码或测试改动**。用户要求撤回非文档改动后，原型补丁与新增测试已移出仓库，备份在 `%TEMP%/saos-timeout-wip/`（临时目录，可能被清理，不代表已交付）。

后续状态说明：以上是验证报告形成时的状态。用户随后要求默认优先 Torch，当前工作树已修改算法选择，见第 8 节；本报告的原型数字仍不代表当前代码。2026-10-22 的修复实施状态见末节，运行验证尚未进行。

## 1. 定位：30 s 预算为什么跑到约 7 分钟

上一轮 E01（`work/verify/jobs/20261021-204540-r10-starts`，Cooke 三片、12 个变量、评价函数 EFFL 50 / TOTR 100）用默认 `trf`、`--max-seconds 30` 启动，约 7 分钟仍未返回；换 `lbfgsb` 10 s 才第一次按时返回。

2026-10-21 晚用该镜头副本（`lenses/scans/003/cooke.optiland.json` 复制到 `%TEMP%/probe-*`，未改动原任务）复现：

1. `trf` 的进度回调停在 13 次评价 / 11.1 s，之后 220 s 内没有任何进度，进程被外层 `timeout` 杀掉。
2. 第 14 次评价进入 `_update_apertures()` → `_trace_group()` → `generate_rays()`，卡在 Optiland 强健瞄准里（`robust.py` 的 `_solve_chief` / `_march_chief` → `iterative._solve_core`）。该视场（0, 0.7）整组光线先抛 “chief ray failed”，saos 的逐根回退再对每根光线重新瞄准；同一几何实测单根 9.6–10.8 s，而自动半口径一次要追 17 根 × 3 波长 × 3 视场。
3. 两次探针在同一路径上以 `Windows fatal exception: access violation` 结束（`faulthandler` 栈：`standard_surface.trace` → `real_rays.trace_on_surface` → `_record_real`）。这条路径不只是慢，还可能让进程直接崩溃。

结论：`src/saos/backend/optiland_opt.py` 只在 `residuals()` 入口和回退循环检查时间（`state["stopped_by"] = "time"`），单次评价内部的射线计算没有任何检查点。预算到期时若正停在一次评价里，就不会返回。`lbfgsb` 那次按时返回只是因为每次评价都短。

## 2. 原型验证（代码已撤回，不作为仓库状态）

为确认“到期必须返回”可以做到，我临时实现并在同一镜头副本上实测过一版看门狗（到期在计算线程里打断当前评价），随后按用户要求撤回。数字如下，**对应原型工作树，不是 HEAD**：

| 输入 | 结果 |
|---|---|
| trf `max_seconds=20` | 墙钟 20.59 s 返回；`elapsed_s 20.594`、`evaluations 15`、`interruptions 1`、`stopped_by timeout`、`improved true`；`final_merit 3.959776595`（起点 `3.959776658`）；镜头仍可读 |
| 同一输入（修复前代码） | 进度停在 11.1 s / 13 次评价，240 s 被外部 `timeout` 杀掉 |
| lbfgsb `max_seconds=10` | 墙钟 10.56 s，`stopped_by timeout`，`improved true`，`evaluations 11` |
| trf `cycles=1`、`max_seconds=30` | 11.69 s，`stopped_by null`、`interruptions 0`，`message` 为 “The maximum number of function evaluations is exceeded.” |
| powell / nelder-mead `cycles=1`、`max_seconds=30` | 30.59 s（powell 预算到期）、11.23 s（nelder-mead 迭代上限） |
| 全局搜索 `method=de`、`max_seconds=1`、评价人为放慢 0.1 s | 约 1 s 返回，`stopped_by timeout`，`systems > 0`，`GLOPT_0000` 写出 |
| torch-adam `max_seconds=5` | 5.06 s，`stopped_by timeout`、`interruptions 1`、`improved true` |

原型的做法（供下一轮实现参考，不是现有代码）：独立的预算对象（起点时钟、`remaining()`、取消优先于到期）；到期由看门狗线程在计算线程里抛一次异常，因为 Optiland 的循环是 Python 字节码、能被真正打断，停在 numpy 的 C 调用里时会等该调用返回；清理阶段（关关卡进程、写回最好点）不跑求解器，迟到的那一次异常接住后重做一次清理。原型同时新增了 `budget_s`、`improved`、`restored`、`interruptions` 字段，并把 `stopped_by` 统一成 `null/timeout/cancel`；这些都不在 HEAD。

原型新增的 8 个回归用例（预算对象、看门狗、单次慢评价、求解器内部不返回、正常结束不打断、全局搜索预算、参数校验）在原型树上通过；受影响套件 114 + 128 通过。撤回后这些数字不再适用，仓库测试没有变化。

## 3. HEAD 上仍需核对的结果语义（未改）

1. `stopped_by` 有三套词：Optiland 局部优化写 `"time"`（`optiland_opt.py`），非序列追迹写 `"max_seconds"`（`nsc.py`），OpticStudio 的 Hammer / 全局搜索写 `"timeout"`（`zos.py`）。同一份报告里三个词指同一件事。
2. `succeeded` 的含义不统一：scipy 路径是 `success and stopped_by is None and 有限最好点`；torch 路径还用消息文本判断（`"restored" not in message`、`message != "no admissible step after backtracking"`），消息文案一改就会影响结果字段。
3. 没有 `improved` 字段。`succeeded=false` 既可能是“到期但确实变好”，也可能是“收敛但没改善”，读结果的人无法直接区分。E01 上 lbfgsb 10 s 就是前者。
4. `saos opt local --max-seconds` 不校验正数：`0`、负数、`nan`、`inf`、`true` 都会被接受；Hammer / 全局搜索只检查 `<= 0`。
5. 预算只覆盖优化循环。`--measure` 的前后测量、`--review`（交叠检查 + 需求复核）、`loaded.describe()`、全局搜索的归档步骤都在预算之外；`measured` 的判定很慢时，整条命令会比 `--max-seconds` 长得多。

## 4. 三种状态分开记录

| 状态 | 含义 | 当前证据 |
|---|---|---|
| 流程通过 | 需求 → 起点生成与预判 → 初筛 → 模型确认 → 深度优化 → 报告认可 → OpticStudio 入口，各步都留下产物且能被下一道关卡接受 | 上一轮 E01（`20261021-204540-r10-starts`）走到 `round.record` 与 `report.write`，报告 `achieved`；对应旧工作树，本轮未重跑（NOT RUN）。起点本身已达标（TOTR 60.2 ≤ 200，shortfall 0），它只证明路径可走通 |
| 优化验收 | 到期能正常退出；充分预算下能把未达标的起点改到达标；保存后重新计算一致 | **NOT RUN**。本轮证明的是“到期不返回”的位置（第 1 节），不是验收通过。第 5 节给出可复现的用例与已测数值 |
| R8 实机兼容 | OpticStudio 2024 R1 的 Odd Asphere 实际列标题、写入、读取、快照重建 | **NOT RUN**。静态实现（未知表头拒绝、同值别名合并）在源码里，2024 R1 列标题仍未在隔离实例中读取 |

上一轮报告的 `achieved: true` 只属于“流程通过”，不能读成深度优化成功。

## 5. 深度优化验收用例（供验证模型执行）

用 verify 新建任务，不要复用 `work/main/`，也不要再用 E01 那个 `shortfall=0` 的起点。下面的输入已在本机后端上跑过（只调用现有 API，不依赖原型）：

- 镜头：E01 的 Cooke 三片 `lenses/scans/003/cooke.optiland.json`。
- 变量：6 个面的厚度（每个 `--min 1 --max 60`），半径固定。
- 评价函数：`EFFL → 50`（权重 1）+ `TOTR → 54`（权重 0.01）。
- 需求：总长上限 55 mm，标度 2 mm → 首轮 `shortfall = (60.2047 − 55) / 2 = 2.602`。

实测（HEAD 的 `optiland_opt`，未改代码；`stopped_by` 在 HEAD 上写 `"time"`）：

| 预算 | 墙钟 | 停止原因 | EFFL | TOTR | 评价次数 |
|---|---|---|---|---|---|
| 90 s | 88.9 s，自行收敛 | `stopped_by null` | 50.0000 | 54.0000 | 132 |
| 20 s | 21.1 s | 预算到期 | 50.0173 | 56.8219 | 30 |

即：20 s 时上限（55 mm）仍不满足，`shortfall 0.911`，但相对起点已改善；90 s 时 `shortfall` 归零。验收步骤：

1. 在工作流里用 `opt merit clear/add` 与 `opt vary` 复现上面的输入，确认首轮 `shortfall > 0`（约 2.602）。
2. 到期退出：`saos opt local --max-seconds 20`。记录墙钟、`stopped_by`、评价次数、`review.valid`、变好/变差的项。通过条件：墙钟 ≤ 25 s、`stopped_by` 为到期（HEAD 上是 `"time"`）、镜头可读、`review.valid` 为真、上限仍未满足。参考值见上表第二行。
3. 充分预算：同一镜头副本 `--max-seconds 120`。通过条件：上限满足（TOTR ≤ 55，参考 54.0000）、EFFL 相对 50 mm 在需求容差内、`review.valid` 为真。参考值见上表第一行。
4. 保存后重算一致：`lens save --as` → `lens load` → `check` / `round.record`，比较同一判定的数值；容差沿用需求里的 `rel`，没有写时先登记再测。
5. 慢评价与求解器内部长时间运行：本轮的原型用例已撤回，验证模型若需要这两项证据，要自己注入（放慢单次评价、让求解器长时间不返回），不能引用原型结果。

## 6. 残余风险与未测项

1. **瞄准路径可能崩溃**：定位过程中同一路径两次以 access violation 结束。预算修复能保证“慢”有界，但不能把崩溃变成异常；实机验收若再遇到，先留 `faulthandler` 栈和触发几何再决定规避方式。
2. **`scan` 的 `stopped` 是另一套词**：`src/saos/search.py` 用 `converged/max_seconds/max_evals/cancelled` 写进扫描行，和优化器的 `stopped_by` 不是同一字段；阅读扫描结果时不要混用。旧任务里已有这些值，改名要迁移。
3. **协作取消仍是逐评价的**：`saos job cancel` 在两次评价之间生效；取消请求落在一次很长的评价里时，要等这次评价结束或预算到期。
4. **NOT RUN**：非 ZOS 全量、无垫片 E01 完整路径、浏览器窗口检查、drawio 渲染、OpticStudio 实机、R8 列标题。

## 7. 副作用

- 全部命令在 `SAOS_INSTANCE=verify` 下运行；探针只读写 `%TEMP%/probe-*`，没有修改 `work/verify/jobs/20261021-204540-r10-starts` 或 `work/main/`。
- 没有启动或关闭 OpticStudio 会话，没有动现有 verify 守护进程，没有提交或推送。
- 本轮仓库只改了文档；原型补丁与新增测试在 `%TEMP%/saos-timeout-wip/`，未进入仓库。

## 8. 后续实施：默认优先 Torch（仅实施与静态检查）

- 省略算法或指定 auto：Optiland 局部优化优先 torch-lbfgs、CPU float64。已知不可微能力缺口才回退 trf，返回 requested_algorithm、algorithm、fallback；显式指定算法不回退。
- 候选短优化转交 algorithm/device，生成计划显式记录 auto，扫描阶段保留选择与回退原因。玻璃替换的局部重优化使用相同策略，以 local_algorithms 记录选择。OpticStudio 的 auto 仍选择原有 DLS/OD。
- 自定义损失返回标量 float 或与变量无关的 Tensor 时可回退；脚本错误、非有限数值、取消和超时不触发重试。运行中已接受优化步后再出现能力变化，按脚本错误处理。
- 回退前恢复原处方、清除 Torch 状态，并从剩余预算运行；选择阶段已耗尽预算时返回带停止原因的错误，不再启动 trf。Optiland 局部优化入口新增有限正数预算校验；这不代表其他入口与整条命令预算问题已修复。
- **长评价超时、access violation、统一结果语义、整条命令预算、N02 梯度与性能、R8 实机仍未验收。没有恢复看门狗原型，没有运行 saos 或测试。**
- 第 5 节参考数字来自旧默认 trf。复现旧对照必须显式指定 `--algorithm trf`；另外从同一起点副本运行 auto，并记录实际算法，不能把旧数字用作 Torch 结果。默认算法改变后，旧测试中依赖隐式 SciPy 的用例应显式指定算法，默认选择用例应检查 Torch 与已说明的回退。


## 2026-10-22 复测后修复（实现待送测）

上一轮 14 项复测确认：真实光线梯度偏差、默认 L-BFGS 零步、非有限脚本错误被吞和成功状态误报。本轮修改代码，未运行测试：

- auto 改为 torch-trf：Torch CPU float64 残差/雅可比，SciPy 信赖域选步；明确的可微能力缺口才回退差分 trf。显式 torch-lbfgs/torch-adam 保留，零步不计接受。
- 中心、未折叠系统的实际瞄准使用隐式导数，保留原生前向解，补上参考光阑半径随变量变化的导数；偏心、折叠与其他视场定义目前报 aiming_geometry 能力缺口。需要 T01 证明精度，静态定位不等于数值验收。
- 非有限脚本值和脚本异常按 SCRIPT_ERROR 传播；初始化在 Torch 状态下执行，不再要求脚本先接受 float。improved/converged/停止原因分开，Optiland succeeded 不再依赖英文消息。
- Optiland Python 评价/瞄准循环按预算中断，退出恢复已验证的镜头快照，不重新追迹最好点。全局搜索在预算内边验证边保存；前后测量、复核和归档计入命令用时。未完成复核标 completed=false、valid=false，不可据此宣称通过。
- 中断检查依赖返回 Python 的检查点；原生 C/CUDA 调用与文件系统操作不保证硬实时。瞄准内部关闭不必要的逐面记录，但 access violation 是否消失仍需复现验证。
- 送测先跑梯度、非有限值、Cooke 与单镜片回归、超时/取消及恢复，再跑回退链与回归，最后补实际不少于 20 变量的同质量性能比较。R8、CUDA、OpticStudio 和浏览器实机仍单列待测。

这段是当前实施状态。前文 2026-10-21 的原型计时和“未实施”描述是历史记录，不能作为本轮通过证据。
