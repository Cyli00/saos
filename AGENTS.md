# 仓库导航
- 架构边界、用户决定、目录规范与核心术语：涉及工作流、命令行为、目录结构或概念命名时查阅 `DESIGN.md`
- 执行光学设计任务：按 `.agents/skills/saos/SKILL.md` 工作，何时用 OpticStudio 由技能决定；修改 saos 本身（代码、测试、命令、文档）不走这份工作流

# 开发约定
- 测试与验证由用户运行：pytest、基准脚本和启动 OpticStudio 的命令，只在用户本轮明确要求时执行；交付时把“验证结果”写成建议用户运行的命令清单
- 写测试：断言真实行为，不写只为通过而存在的测试；光学案例只用 `tests/fixtures/cases/` 的序列、非序列、混合模式、`iris`（孔径光阑）、`reflective`（同轴反射）五例（用法见该目录 README），不取 `work/` 里的任务
- 用户要求提交时，若本次改了工作流的阶段或命令，先同步 `docs/workflow.drawio` 与流程图窗口（`src/saos/ui/stage.py`、`src/saos/ui/static/app.js`），再提交
- 有多个需要用户拍板的决定时，用 AskUserQuestion 逐题问，每题带推荐项
- 用户的新决定写进 `DESIGN.md` 对应段落，一句话带日期；`docs/adr/` 是冻结的历史记录，只在追溯某个决定的来由时读，技能、`DESIGN.md`、README 和命令帮助都指向 `DESIGN.md`

# 技能文档（`.agents/skills/saos/`）
- 改动 agent 可见的接口或行为（CLI、ctx、starts、search、报告/流程判定）时同步技能，写进内容所属的那一份：
  - 某阶段的做法 → 该阶段文件（`need.md`、`model.md`、`rounds.md`、`delivery.md`）；停滞与诊断 → `diagnosis.md`
  - `ctx` / `starts` / `search` 接口、rtc 副本、专家类 → `scripting.md`
  - daemon、许可证、长运行与 detach → `session.md`
  - 任务目录的文件及其类别 → `files.md`
  - 某一类系统的经验 → 对应配方
- `SKILL.md` 每次调用都整份加载，只放每个阶段都用得到的内容：会话开始、阶段路由表（`workflow.current` → 进入时先读的文件 → 完成判据）、全局规则、停下询问、收尾；阶段细节留在阶段文件里
- 新增参考文件时，在 `SKILL.md` 写明读取条件，agent 才会读到它
- 一条规则只写在一处，别处用链接指过去；案例佐证跟着规则走，同样只出现一次
- 环境里查得到的事实（命令 `--help`、`rtc_admitted.json` 这类数据表、`docs/plans/` 里的算法细节），技能里只写指向它的指针
- 移动或改名技能段落后，搜 `src/`、`docs/`、`DESIGN.md` 里指向它的文件名与"stage N"一类引用，一并更新
