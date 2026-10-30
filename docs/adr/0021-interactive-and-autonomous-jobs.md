# ADR-0021：任务分参与与自主两种流程

状态：已接受（2026-10-30）

## 背景

ADR-0020 把需求确认与模型确认改为工作流窗口按钮，窗口开着时拒绝命令行 `--quote`。这样一来，用户说“不用确认，直接做”时 agent 无法过关卡；而做题、测试这类运行本来就没有人守着窗口。此前技能里只有零散的“autonomy”说法，工具不认，报告也分不出哪些决定是用户做的。

## 决定

1. **建任务时选定，记入任务**：`saos job create --mode interactive|autonomous`，写进 `job.json`，缺省为 interactive，旧任务按 interactive。`job show` 与窗口显示模式。只有用户能在窗口里把自主任务改为参与（`/api/mode`）；任务不能中途改为自主，避免 agent 为了过关卡切换模式。
2. **自主模式的关卡**：agent 用 `demand confirm --reason`、`lens approve --reason` 自行通过，记录 `via: auto` 与理由；来源齐全、起型检索记录、重建原因等前提照样核对。
3. **自主模式下原本要问用户的地方**：阻塞问题由 agent 以 `agent assumption: <原因>` 作答；停止用 `saos stop --reason`，不带 `--quote`（`via: auto`）；放弃用户点名的方向目标等取舍由 agent 决定并写进 `sources/interview.md`。报告首页标明“自主运行”，“自主决定”一节列出需求与模型的理由、停止理由和确认时依据只是假设的界限。
4. **参与模式的调整**：关卡处除“确认”外还有“要求修改”按钮（`/api/revise`），用户原话写进 `revisions.jsonl` 并追加到 `sources/interview.md`，关卡保持等待；agent 从 `job show` 的 `workflow.revisions` 读到请求，出新版需求或建模说明后请求视为已处理。
5. 自主任务在关卡处不自动打开窗口，`saos ui open` 照样可以打开。

## 后果

- 做题与测试可以无人值守跑完，报告里每项未经用户确认的决定都可以追溯，便于事后核对。
- 参与模式下用户的修改意见不再只留在对话里，换 agent 或会话中断后仍可读到。
- 自主模式不改变交付底线：交付数值仍来自 OpticStudio，`must` 未达标仍要报告差距。
