# 仓库导航
- 现状以代码、测试和命令帮助为准：命令行为查 `uv run saos <命令> --help`，Python 接口查 `uv run saos api`，目录规定在 `tests/test_repo_layout.py`，模块分层在 `tests/test_layers.py`，术语在 `.agents/skills/saos/references/terms.md`
- 一个决定为什么这样定：查提交历史（`git log -S<关键词>`）；2026-11-10 之前的设计文档、决策记录和计划也在历史里（`git show 3c488f0:DESIGN.md`、`docs/adr/`、`docs/plans/`）
- 执行光学设计任务：按 `.agents/skills/saos/SKILL.md` 工作，何时用 OpticStudio 由技能决定；修改 saos 本身（代码、测试、命令、文档）不走这份工作流

# 开发约定
- 测试与验证：pytest、基准脚本，你可以执行；启动 OpticStudio 的命令，只在用户本轮明确要求时执行
- 写测试：断言真实行为，不写只为通过而存在的测试；光学案例只用 `tests/fixtures/cases/` 的序列、非序列、混合模式、`iris`（孔径光阑）、`reflective`（同轴反射）五例（用法见该目录 README），不取 `work/` 里的任务；rtc 内核的面型与模式覆盖另用 `tests/rtc/omnibus.py` 的全覆盖案例，它没有 OpticStudio 真值
- 用户要求提交时，若本次改了工作流的阶段或命令，先同步 `docs/workflow.drawio` 与流程图窗口（`src/saos/ui/stage.py`、`src/saos/ui/static/app.js`），再提交
- 有多个需要用户拍板的决定时，用 AskUserQuestion 逐题问，每题带推荐项
- 用户的新决定落到它生效的地方：代码与测试、命令帮助、技能，或下面的“代码表达不了的约束”；理由写进提交信息。还没做的事记进 `TODO.md`，做完就删掉那一条。仓库里只留描述现状和待办的文档，不写设计文档、决策记录或计划文档
- 改了行为，就在同一次改动里改掉描述它的每一处（帮助、docstring、技能、README、`TODO.md`）；发现文档与代码不符时以代码为准，并当场改文档

# 代码表达不了的约束
- 方向：rtc 要能独立承担全部计算，让 saos 不装 OpticStudio 也能用（包括 macOS）。rtc 的行为以真机 OpticStudio 的实测为准，包括它与手册或界面标签不符的地方；对账真值只认 OpticStudio，CODE V 只作交叉核对和优化基准
- 现阶段交付数值仍只来自 OpticStudio：rtc 算不了或没对上的，报 `BACKEND_UNSUPPORTED` 或标进 `computed_by`，不给近似值
- 只经公开的 ZOS-API 用 OpticStudio，不调用它未公开的内部组件
- saos 是通用框架：代码、命令、技能都不按系统类别做配方，某类系统的经验提炼成与类别无关的规则
- 关卡只设在需求确认、模型确认和交付这几处昂贵的地方；不为防 agent 绕过流程加锁，封存哈希只用来发现意外改动
- 同一段任务脚本在第二个任务里被复用时，上收成与系统类别无关的库函数，上收前后同一批镜头的数值逐项一致；测试只测工具，不测 agent 的设计能力
- 加速先测基线，做成通用的算法改进；rtc 内核的回退单项不超过 5%
- 语言：给用户的报告是中英双语的 HTML；给 agent 读的内容（技能、需求单格式、命令的帮助和报错）用英文；仓库文档和代码注释用中文，docstring 随代码注释（`saos api` 读出的也一样，中文或中英并列）；访谈记录保留用户原话
- 流程图窗口只做深色主题，前端随包分发，不引入 Node 构建链

# 技能文档（`.agents/skills/saos/`）
- 技能只写判断：什么时候用、怎么选、出了什么结果该怎么办。接口写在环境里，改接口时改环境，技能不重复：
  - 命令的参数、格式、结果字段、错误码 → `src/saos/cli_help.py`（大的帮助页按 `TOPICS` 分主题）
  - 脚本 import 的 Python 接口 → docstring，由 `saos api` 读出；ctx 方法的说明只写在 docstring（英文部分），`exec --help` 各主题里用一行 `<CTX 名字 …>` 从它生成，主题里只写跨方法的说明。ctx 上转调 kit 函数的方法沿用函数的 docstring（`kit.documented_like`）
  - rtc 能算什么这类清单从代码常量生成（`cli_help._operands`），不手写；镜头能带什么、各项是否对过账由 `saos backends` 现场用探针镜头问 rtc 后端（`src/saos/capability.py`），新增一类处方选项时在它的探针表里加一项
- 改动 agent 的判断或流程时，写进内容所属的那一份：
  - 某阶段的做法 → 该阶段文件（`need.md`、`model.md`、`rounds.md`、`delivery.md`）；停滞与诊断 → `diagnosis.md`；评价函数（初筛和轮次共用）→ `merit.md`
  - 脚本和循环怎么选、rtc 数字怎么对待、rtc 副本 → `scripting.md`；微光学测量 → `micro.md`
  - 后端切换、许可证、长运行 → `session.md`
  - 任务目录里你写的文件和什么指挥你 → `files.md`
- 帮助也按“都有后端 rtc 和 OpticStudio”来写：只对一个后端成立的说法要点名后端
  - 某一类系统的经验：提炼成与系统类别无关的规则，写进它所属的阶段文件，不按系统类别另开文档
- `SKILL.md` 每次调用都整份加载，只放每个阶段都用得到的内容：会话开始、阶段路由表（`workflow.current` → 进入时先读的文件 → 完成判据）、全局规则、停下询问、收尾；阶段细节留在阶段文件里
- 新增参考文件时，在 `SKILL.md` 写明读取条件，agent 才会读到它
- 新能力写进决定用它的那个阶段文件，写明什么情况下用；实现进度、候选入口和实验开关留在 `TODO.md`，技能只写 agent 现在能调用的
- 阶段文件先写该阶段默认后端的做法，另一个后端只写差别
- 一条规则只写在一处，别处用链接指过去；案例佐证跟着规则走，同样只出现一次
- 环境里查得到的事实（命令 `--help`、`saos api`、`rtc_admitted.json` 这类数据表、模块 docstring 里的算法细节），技能里只写指向它的指针，指针写在做决定的那一步，写明具体命令和主题
- 移动或改名技能段落后，搜 `src/`、`tests/`、`README.md` 里指向它的文件名与"stage N"一类引用，一并更新
