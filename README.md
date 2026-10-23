# saos

saos（simulate any optical system）让 AI agent 替你做光学设计。你用自己的话说清要什么（可以不懂光学），agent 通过访谈把它整理成需求单，选结构、建模，在 Optiland 上一轮轮优化；你认可结果后，再到 Zemax OpticStudio 里复测并继续优化，最后交给你一份镜头文件和一份 HTML 报告，报告里的每个数值都有实测依据，达不到的目标写明差多少。

目标范围是 OpticStudio 2024 R1 Premium 能建模的系统：序列、非序列和多重结构。Optiland 阶段表达不了的功能（例如部分非序列特性），agent 会直接告诉你这是能力缺口，不会悄悄换成别的办法。

## 安装

### 需要准备

| 项目 | 说明 |
|---|---|
| Windows | 目前只支持 Windows（OpticStudio 和 ZOS-API 只有 Windows 版） |
| [uv](https://docs.astral.sh/uv/) | 管理 Python 3.12 和依赖，所有命令都通过 `uv run` 执行 |
| 支持技能的编程 agent | Claude Code、Cursor、Codex 等；agent 负责全部设计工作 |
| Ansys Zemax OpticStudio 2024 R1.00 Premium | 只在最后的 OpticStudio 阶段需要；没有它也能走完 Optiland 阶段，但交付数值不会经过 OpticStudio 验证 |
| Chrome（可选） | 流程窗口优先用 Chrome 应用窗口打开，没有时用系统默认浏览器 |

### 步骤

1. 打开 Windows 的“开发人员模式”，然后带符号链接克隆仓库。技能放在 `.agents/skills/saos`，Claude Code 和 Cursor 通过符号链接 `.claude/skills/saos` 找到它；没有开启符号链接时，这个链接会变成一个普通文本文件，agent 就找不到技能。

   ```bash
   git clone -c core.symlinks=true https://github.com/Cyli00/saos.git
   cd saos
   ```

2. 安装依赖。第一次会下载 PyTorch 等较大的包，需要几分钟。Optiland 固定为 0.6.3，其他版本后台进程会拒绝启动。

   ```bash
   uv sync
   ```

3. OpticStudio 不在默认位置（`C:\Program Files\Ansys Zemax OpticStudio 2024 R1.00`）时，设置环境变量 `SAOS_OPTICSTUDIO_DIR` 指向安装目录。

4. 检查环境。结果会列出后台进程、正在运行的任务、OpticStudio 进程数与许可余量、可见的 OpticStudio 窗口。

   ```bash
   uv run saos daemon status
   ```

## 使用

### 开始一个设计

1. 启动后台进程。它在内存里持有当前镜头，所有计算命令都转发给它；认可前只用进程内的 Optiland，不会打开 OpticStudio。agent 发现它没启动时也会提醒你或自己启动。

   ```bash
   uv run saos daemon start
   ```

2. 在仓库根目录打开 agent，直接描述你的需求，例如“设计一个 20 倍、NA 0.4 的显微物镜”或“给成像光纤做一个侧视探头”。agent 会按 saos 技能的流程工作，包括新建任务目录。

### 你要做的四个决定

agent 自己决定结构、变量、优化算法和实验，只在下面四处停下来等你。你的原话会逐字记录在任务里，所以回答时把选择说清楚。

| 时机 | 你看到什么 | 你决定什么 |
|---|---|---|
| 确认需求 | 访谈中每个问题都附 agent 的推荐答案；访谈结束后给出需求单，以及每个硬性目标“能达到 / 勉强 / 达不到”的判断和依据薄弱的数值 | 需求单是否就是你要的 |
| 确认模型 | 一份建模说明页面：选了什么结构、为什么、每个面是什么实物、材料来源、一阶分析和优化预算 | 是否按这个结构开始优化 |
| 认可 Optiland 结果 | Optiland 阶段的报告，注明数值尚未经 OpticStudio 验证 | 认可后进入 OpticStudio 阶段；不认可就回去继续优化 |
| 停止 | 任何时候 | 根据已有证据结束任务，报告会写明原因和你的原话 |

除此之外，遇到需求自相矛盾、测量口径有歧义、结构明显达不到目标、优化不再有进展或需要改变你没见过的结构时，agent 也会带着证据和建议方案来问你。

改动需求（数值、范围、判定方法、权重）或改变已确认的结构，都要重新确认；agent 不会为了达标偷偷放宽需求。

### 看进度

- **流程窗口**：确认需求后自动打开的本机小窗口，显示流程走到哪一步、当前镜头的布局和 Lens Data、结构交叠检查，以及每轮的点列图、MTF 等。关掉后，到需要你确认的节点会重新打开；也可以手动打开：

  ```bash
  uv run saos ui open --job <任务>
  ```

- **命令行**：`uv run saos job list` 列出任务，`uv run saos job show <任务>` 查看所处步骤和正在运行的计算。

### 拿结果

每个任务的全部内容放在 `work/<实例>/jobs/<任务>/`（默认实例为 `main`），不进仓库：

| 位置 | 内容 |
|---|---|
| `report/index.html` | 最新的报告（中文或英文，按需求单的语言） |
| `lenses/rounds/` | 每一轮记录的镜头；报告写明交付的是哪一轮 |
| `sources/` | 你的原话、需求单、材料来源 |
| `models/` | 每次确认的建模说明 |

报告只交付满足全部可交付条件的镜头：材料有来源（不含模型玻璃）、满足所有已知常量与变量范围、零件没有交叠。没有这样的镜头时，报告会说明原因。

### 结束

```bash
uv run saos daemon stop          # 有未保存的修改时需要 --discard
```

## 注意事项

- **数值以 OpticStudio 为准**：Optiland 阶段的报告是给你判断要不要进 OpticStudio 的，最终交付以 OpticStudio 阶段的实测为准。两端数值不同时，报告列出差别。
- **OpticStudio 许可证**：同时最多 8 个 OpticStudio 实例，包括你自己打开的窗口。认可前不占许可证；OpticStudio 阶段的并发计算默认用 4 个。
- **OpticStudio 弹出对话框会卡住计算**：例如自动保存恢复提示，saos 遇到时返回 `UI_BLOCKED`，需要你手动关掉。`Documents/Zemax/Autosave/Recovered` 里的旧恢复文件会让每个新实例都弹提示。
- **长任务在后台跑**：命令超时不代表计算失败，不要让 agent 重新提交同一项计算。要中断用 `uv run saos job cancel <任务>`，已完成的部分会保留。
- **不要手改工具写入的文件**：任务目录里工具写入的文件记录了哈希，改动或删除后 `saos report` 会拒绝生成报告（`uv run saos job verify <任务>` 列出问题）。你可以自由看，也可以往 `sources/` 里放资料。
- **想在 OpticStudio 窗口里看**：用 `uv run saos daemon start --mode extension` 启动，OpticStudio 阶段会连接你已打开的 OpticStudio 窗口（saos 自动打开窗口里的 Interactive Extension；有多个窗口时用 `SAOS_UI_PID` 指定）。实时刷新会明显拖慢计算，平时看流程窗口就够了。
- **电脑资源**：长时间计算默认留出 2 个逻辑核并以低于正常的优先级运行；装了 CUDA 版 PyTorch 时，可设 `SAOS_DEVICE=cuda` 让自动微分优化用 GPU。
- **专用配方的局限**：显微物镜和光纤侧视探头有现成配方，但它们的完整验收目前只能在 OpticStudio 阶段运行。Optiland 阶段 agent 会告诉你报告评不了哪几项。

### 常用环境变量

| 变量 | 作用 |
|---|---|
| `SAOS_INSTANCE` | 运行目录 `work/<实例>/`，默认 `main`。同时跑几个互不干扰的设计时各用一个实例，每个实例有自己的后台进程 |
| `SAOS_LANG` | `zh` / `en`，面向人的输出（报告、流程窗口）的默认语言 |
| `SAOS_UI=0` | 不自动打开流程窗口 |
| `SAOS_OPTICSTUDIO_DIR` | OpticStudio 安装目录 |
| `SAOS_DEVICE` | `auto` / `cpu` / `cuda`，自动微分优化用的设备 |

完整列表见 `uv run saos --help`。

## 文档

| 文件 | 内容 |
|---|---|
| [.agents/skills/saos/SKILL.md](.agents/skills/saos/SKILL.md) | agent 执行设计任务的工作流、各阶段细节与配方（英文） |
| [docs/workflow.drawio](docs/workflow.drawio) | 工作流全貌图 |
| [DESIGN.md](DESIGN.md) | 架构原则、用户决定、目录规范与中英术语 |
| [docs/adr/](docs/adr/) | 关键决策记录 |
| [TODO.md](TODO.md) | 暂缓和待查的事项 |
| `library/<库>/README.md` | 跨任务共享的参考数据：材料、在产玻璃目录、热学与机械材料、膜层、初始结构、显微物镜结构库 |

## 参考资料

`ref/` 不纳入版本管理，是指向 zea 仓库中 `ref/` 的目录联接，没有它不影响使用。

| 路径 | 用途 |
|---|---|
| `ref/docs/光学设计及Zemax应用 (Pdg2Pic, 张欣婷)_OCR.pdf` 及配套 OCR JSON | Zemax 的具体操作 |
| `ref/docs/几何光学·像差·光学设计第3版-李晓彤_OCR.pdf` 及配套 OCR JSON | 几何光学、像差理论、初始结构求解 |
| `ref/repo/ai-zemax-optical-design` | 只借鉴经验，不复用代码（ADR-0001） |
| `ref/repo/patent-lens-to-excel` | 只借鉴做法（JSON 中间格式、按 nd/νd 匹配玻璃），不调用其代码（ADR-0005） |

两本教材出版较早，部分内容可能已经过时；拿不准时查最新的 OpticStudio 手册和几何光学资料。OCR 中的公式和表格质量不可靠，不能直接作为决策依据。光学胶与微透镜材料见 [library/materials/README.md](library/materials/README.md)，光纤探头的选材规则在 [光纤配方](.agents/skills/saos/references/recipes/fiber.md) 里。

## 参与开发

修改 saos 本身前先读 [AGENTS.md](AGENTS.md) 和 [DESIGN.md](DESIGN.md)。测试默认使用 `work/verify/`；测试夹具可以通过 `SAOS_HOME` 使用本轮独占的系统临时目录。`SAOS_HOME` 优先于 `SAOS_INSTANCE`，执行前确认实际目录没有指向用户实例。

```bash
SAOS_INSTANCE=verify uv run pytest                     # 不启动 Zemax 的单元测试和假后端的生命周期测试
SAOS_INSTANCE=verify SAOS_RUN_ZEMAX=1 uv run pytest -m zemax -s  # 在本机 OpticStudio 上跑工具和工作流回归
```

按改动范围和用户指定的验证模式选择检查；测试交给其他执行者时标明 `NOT RUN` 并交接。改动涉及工作流窗口或流程图时，另跑：

```bash
SAOS_INSTANCE=verify uv run saos ui open --job <任务> --no-window                    # 打开返回的 URL，检查中英文标签与返回边
SAOS_INSTANCE=verify SAOS_RENDER_DRAWIO=1 uv run pytest tests/test_docs_sync.py -s  # 渲染流程图 PNG 供查看，已含命令一致性检查
```
