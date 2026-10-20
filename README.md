# saos

saos（simulate any optical system）是一套让 agent 驱动 Zemax OpticStudio 做光学设计的工具。用户用自己的话说清要什么（可以不懂光学），agent 拷问需求、建模、仿真、优化，最后交付每个数值都有 OpticStudio 实测依据的 HTML 报告。

OpticStudio 2024 R1 Premium 加 ZOS-API 能建模的系统都在范围内：序列、非序列和多重结构。

## 怎么工作

- **agent 读技能**：执行设计任务的 agent 读 [skills/saos/SKILL.md](skills/saos/SKILL.md)（入口技能）和按需加载的配方 `skills/recipes/<系统>/`。Claude Code 通过 `.claude/skills/saos` 自动发现入口技能；Codex 等平台从 `~/.agents/skills` 链接。
- **命令行**：agent 只通过 `uv run saos …` 操作，每条命令打印一个 JSON 对象。参数、输入格式和返回值见 `uv run saos <命令> --help`，中文速览见 [docs/cli.md](docs/cli.md)。
- **常驻后台进程**：持有 OpticStudio 连接，命令行把请求转发给它；需要并发时另带一个 standalone 实例组成的 worker 池。
- **两个必经停顿**：用户确认需求单（`saos demand confirm`）、确认建模说明（`saos lens approve`），其余由 agent 自己决定。
- **流程图窗口**：需求确认后打开的本机小窗口（`saos ui`），用户在里面看流程走到哪一步、当前镜头的布局和每轮的点列图、MTF。
- **任务目录**：每个设计任务的需求原话、镜头、脚本、证据和报告都放在 `work/<实例>/jobs/<任务>/`，不进仓库。

工作流的全貌见 [docs/workflow.drawio](docs/workflow.drawio)。

## 环境

- Windows，Ansys Zemax OpticStudio 2024 R1.00 Premium（ZOS-API）。默认从 `C:\Program Files\Ansys Zemax OpticStudio 2024 R1.00` 加载 DLL，其他位置用 `SAOS_OPTICSTUDIO_DIR` 指定。
- Python 3.12，一律通过 [uv](https://docs.astral.sh/uv/) 运行。
- 流程图窗口优先用 Chrome 应用窗口，没装时用系统默认浏览器。

## 开始

```bash
uv sync
uv run saos daemon status        # 各实例后台进程、许可余量、可见窗口
uv run saos daemon start         # standalone；--mode extension 连接已打开的 OpticStudio 窗口
uv run saos job create --name <名字>
```

之后交给加载了 saos 技能的 agent。同时跑几个互不干扰的工作时，用 `SAOS_INSTANCE=<实例名>` 分开运行目录。

## 文档

| 文件 | 内容 |
|---|---|
| [skills/saos/SKILL.md](skills/saos/SKILL.md)、[demand.md](skills/saos/demand.md) | 执行任务的 agent 的工作方式、需求单格式、终止规则（英文） |
| [docs/cli.md](docs/cli.md) | 中文命令速览、输出协议、环境变量和错误码；详细格式见各命令的 `--help` |
| [DESIGN.md](DESIGN.md) | 设计原则、用户做过的决定及理由、环境事实、目录规定 |
| [GLOSSARY.md](GLOSSARY.md) | 术语（中英并列）和不要用的叫法 |
| [docs/adr/](docs/adr/) | 关键决策记录 |
| [TODO.md](TODO.md) | 暂缓和待查的事项 |
| [AGENTS.md](AGENTS.md) | 修改本仓库时给 agent 的规则 |
| `library/<库>/README.md` | 跨任务共享的参考数据：材料、在产玻璃目录、显微物镜结构库 |

## 参考资料

`ref/` 不纳入版本管理，是指向 zea 仓库中 `ref/` 的目录联接。

| 路径 | 用途 |
|---|---|
| `ref/docs/光学设计及Zemax应用 (Pdg2Pic, 张欣婷)_OCR.pdf` 及配套 OCR JSON | Zemax 的具体操作 |
| `ref/docs/几何光学·像差·光学设计第3版-李晓彤_OCR.pdf` 及配套 OCR JSON | 几何光学、像差理论、初始结构求解 |
| `ref/repo/ai-zemax-optical-design` | 只借鉴经验，不复用代码（ADR-0001） |
| `ref/repo/patent-lens-to-excel` | 只借鉴做法（JSON 中间格式、按 nd/νd 匹配玻璃），不调用其代码（ADR-0005） |

两本教材出版较早，部分内容可能已经过时；拿不准时查最新的 OpticStudio 手册和几何光学资料。OCR 中的公式和表格质量不可靠，不能直接作为决策依据。光学胶与微透镜材料见 [library/materials/README.md](library/materials/README.md)，光纤项目的材料选择按 [材料建模基准](docs/recipes/fiber/FIBER_BASELINE.md)。

## 测试

```bash
uv run pytest                                # 不启动 Zemax 的单元测试和假后端的生命周期测试
SAOS_RUN_ZEMAX=1 uv run pytest -m zemax -s   # 在本机 OpticStudio 上跑工具和工作流回归
```
