# 仓库导航
- 架构边界、用户决定、目录规范与核心术语：涉及工作流、命令行为、目录结构或概念命名时查阅 `DESIGN.md`
- 执行光学设计任务：按 `.agents/skills/saos/SKILL.md` 工作；修改 saos 本身（代码、测试、命令、文档）不走这份工作流
- 改动 agent 可见的接口或行为（CLI、ctx、starts、报告/流程判定）时，同步 `.agents/skills/saos/`
- 测试与验证由用户运行：pytest、基准脚本和任何启动 OpticStudio 的命令，只在用户本轮明确要求时执行；交付时把“验证结果”写成建议用户运行的命令清单

# 开发约定
- 写测试：断言真实行为，不写只为通过而存在的测试；光学案例只用 `tests/fixtures/cases/` 的序列、非序列、混合模式三例（用法见该目录 README），不取 `work/` 里的任务
- 用户要求提交时，若本次改了工作流的阶段或命令，先同步 `docs/workflow.drawio` 与流程图窗口（`src/saos/ui/stage.py`、`src/saos/ui/static/app.js`），再提交
- 有多个需要用户拍板的决定时，用 AskUserQuestion 逐题问，每题带推荐项
