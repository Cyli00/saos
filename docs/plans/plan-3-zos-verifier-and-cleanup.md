# Plan 3：用户认可后的 OpticStudio 验证与继续优化阶段

决定与理由见 [ADR-0012](../adr/0012-optiland-default-opticstudio-final-stage.md)（2026-10-20 用户决定）。本计划取代原来的“每轮 `round` 后台异步 ZOS 交叉验证 + Auto-Polish 定标 + `zos_alert` 告警 + 删除约 4,000 行 ZOS 代码”的方案：用户认为那样弄复杂了。

## 1. 为什么要做（Why）

1. **交付数值要有 OpticStudio 实测依据**：Plan 1、2 之后调试轮次默认在 Optiland 上算，报告里的数值只有 Optiland 依据。`DESIGN.md` 的底线要求最终交付以 OpticStudio 的实测为准。
2. **验证只对用户认可的设计做一次**：用户先看 Optiland 阶段的报告，认为结果没问题，才进入 OpticStudio。没被认可的设计不占许可证，也不需要每轮复算、自动补偿和偏差告警。
3. **在 OpticStudio 里还能继续往上推**：OpticStudio 阶段可以用 Zemax 的阻尼最小二乘、Hammer、全局搜索和 worker 池扫描与公差，轮次上限重新给，在 Optiland 阶段的结果上继续优化。

---

## 2. 怎么做（How）

### 2.1 阶段的入口：用户认可与记录
1. **新命令 `saos zos begin --job J --quote TEXT [--round N] [--max-rounds K]`**（名字落地时再定，`--help` 为准）：
   * 前提：当前需求与判定版本下有报告；所选轮次（默认是报告交付的那一轮）可交付，且不含 Optiland 近似的模型玻璃（Plan 2 §2.4.7）；需求单已确认、模型已确认；本机有可用的 OpticStudio 许可证。任一不满足就拒绝并说明。
   * 记录用户原话、所选轮次、新的轮次上限（不给时沿用需求单的 `max_rounds`），封存进任务目录（例如 `stages/zos.json`）。这是继需求单确认、模型确认之后的第三个必经停顿：没有用户原话不进入。
   * 用户不认可时不调用这个命令：agent 回到 Optiland 阶段继续设计，或由用户 `saos stop`。
2. **切到 OpticStudio**：按所选轮次归档镜头的伴随文件里的处方在 OpticStudio 中重建（与 `hybrid.switch_to_zos` 同一条路），之后整个任务都在 OpticStudio 上算，不再回到 Optiland。Optiland 写出的 `.zmx` 不用于重建，只供查看。
   * 进入 OpticStudio 阶段时，比较伴随文件保存的材料数据与重建所用的当前目录数据；有差别就写进报告，随本阶段入口命令一起实现。
3. **结构指纹跨后端一致**：模型确认按结构指纹判断。实施时先在 Cooke 样例、一个显微物镜条目和光纤探头上比较两端算出的指纹；有差别就改成从处方计算指纹，保证进入 OpticStudio 后原有的模型确认仍然有效。

### 2.2 OpticStudio 验证：第一轮复测
* 进入阶段后自动记录一轮，层级 `verify`：在 OpticStudio 上用同一套 `checks/` 复测所选轮次，写出普通的 `evaluations/NNN/result.json`（`environment.backend` 为 `zos`），ledger 里记 `verify_of`（原轮次号）。
* 同时写出逐项对比：每个需求单条目的 Optiland 值、OpticStudio 值和相对差别，另加双端基准对齐的基准量。不设通过线，不阻止继续：两端不同时以 OpticStudio 为准。
* 比较窗口从这一轮重新开始（换了后端，测量口径变了），轮次上限也从这一轮开始计。

### 2.3 OpticStudio 阶段的继续优化
* agent 照常用 `opt local`（DLS）、`opt hammer`、`opt global`、`saos scan`（worker 池为 standalone OpticStudio）推进，`saos round` 记录，五个手段层级都可以用；终止规则与 Optiland 阶段相同，按本阶段的轮次和上限判断。
* Plan 2 的变量和评价函数照 Plan 2 §2.4.3 翻译到 OpticStudio，译不过去的由 agent 重设。
* 只用 ZOS-API 的判定和脚本在这一阶段可以直接运行（ADR-0007）。

### 2.4 报告
* 进入 OpticStudio 阶段后，`saos report` 只从 OpticStudio 阶段的轮次里选交付镜头，数值全部来自 OpticStudio；另列 Optiland 阶段的结论和 §2.2 的逐项对比。
* 没进入 OpticStudio 阶段时，报告照现在的样子写，并注明“数值由 Optiland 计算，尚未经 OpticStudio 验证”（这一条已在 Plan 3 之前实现）。

### 2.5 工作流四处同步（AGENTS.md）
1. `skills/saos/SKILL.md`：在报告之后加一步“用户认可 → `zos begin` → 验证轮 → 继续优化 → 最终报告”，写明用户不认可时怎么办、没有许可证时怎么办。
2. `src/saos/ui/stage.py`（`STEPS`、`snapshot`）与 `app.js`（`NODES`、两种语言的 `TEXT`、`back(...)`）：新增“OpticStudio 验证与继续优化”节点，由 `stages/zos.json` 和验证轮推断；返回边从它回到“自主设计”（本阶段的轮次）。
3. `docs/workflow.drawio`：加同一节点和返回边，写上 `saos zos begin`。
4. `src/saos/cli.py` 注册新命令的 `--help`，`docs/cli.md` 加一行速览；`report --help` 写明两阶段下怎样选交付镜头。

### 2.6 清理：只删探索阶段确实不再需要的代码
OpticStudio 阶段要用 Zemax 的优化器、评价函数、worker 池和分析导出，原计划要删的大部分代码都留下：

| 保留 | 原因 |
|---|---|
| `zos.py` 的 `optimize`、`optimize_search`（Hammer / Global 与 `GLOPT_*.zmx` 处理）、`merit_*` | OpticStudio 阶段的优化手段 |
| `src/saos/parallel.py` 与 `daemon.py` 的 `self.pool` | Optiland worker 和 OpticStudio 阶段的 worker 都用它的生命周期 |
| `src/saos/native_ui.py` 与 `--mode extension` | ADR-0011：等流程图窗口在一个真实任务里证明够用再删；OpticStudio 阶段用户也可能想看原生窗口 |
| `analysis.py`、`nsc.py`、`zos.analysis` | 孪生代算、非序列和 OpticStudio 阶段都要 |

| 删除或改写 | 替代 |
|---|---|
| `zos.py` 中往 MFE 临时插删 `ZERN` 行算 Zernike 的做法 | 读 `ZernikeStandardCoefficients` 分析导出（孪生实例和 OpticStudio 阶段都更快） |
| `hybrid.py` 中 Optiland 会话因 `opt` 命令切到 OpticStudio 的路径 | Plan 2 的 Optiland 优化器；多重结构变量、物体变量和窗口操作仍会切换 |

---

## 3. 做完预期（Expected Outcome）

1. 用户认可后，一条命令进入 OpticStudio 阶段：记下原话，按处方重建，自动记录验证轮并给出逐项对比；没有原话、没有许可证或所选轮次不可交付时拒绝并说明原因。
2. 进入阶段后原有的模型确认仍然有效；轮次上限按用户给的值重新计。
3. 最终报告的交付镜头和数值全部来自 OpticStudio 阶段，并列出 Optiland 与 OpticStudio 的逐项差别。
4. 探索阶段零许可证；整个任务最多在 OpticStudio 阶段占 1 个主实例加 worker 池的许可证。
5. 四处工作流同步，`tests/test_docs_sync.py` 通过；提交并打标签 `v0.2.0-plan3`。
