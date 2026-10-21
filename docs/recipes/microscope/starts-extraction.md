# 起点库通用能力上收记录

2026-10-21。对应 Optiland 交接 B2。本轮用户指定“只实施与静态检查，测试交给其他模型”；本轮修复版没有新增运行通过记录；下述 NOT RUN 均指当前修复版。

## 上收边界

| 原实现 | 核心实现 | 保留方式 |
|---|---|---|
| `recipes/microscope/paraxial.py` | `starts/paraxial.py` | 原函数原样移入，旧路径仅转出同名函数；仍只用于专利提取预筛，设计结论通过后端计算 |
| `recipes/microscope/library.py` 的条目路径、读取及枚举 | `starts/library.py` | 复用 `entries/<id>/entry.json`；显微库路径、旧条目格式、查询和接纳规则不变 |
| `recipes/microscope/adapt.py` 的半径、厚度乘尺度 | `starts/scaling.py` | 适配器调用 `scale_surface`，原焦距、工作距离、盖玻片和筒镜处理不变 |

新增 `starts.generate` 以需求 ID 映射读取焦距、半视场、f/#、波长，记录来源及选库压力；`starts.screen` 先零优化全体初筛，再同预算短优化。新增预判使用 Optiland 自带计算，不把显微预筛矩阵作为新设计结果。

静态源代码对照：上收后的 `starts/paraxial.py` 整个模块 AST 与 HEAD 原模块一致，包含 `Surface`、`_power`、`system_matrix`、`effective_focal_length`、`front_focal_distance`、`back_focal_distance` 共 6 个定义。此检查没有运行数值计算。

## 已收到的旧版外部复核

用户核对旧清单的 63 个 SHA-256 后报告：25 个显微条目上收前后数值完全一致；缩放规律误差小于 1e−15；轴上像差预判与真实光线偏差 2.3e−8 mm，容差 1.7e−6 mm；Petzval 与独立手算一致。非球面缩放阶数的先前指控已由用户撤回，两个示例系数代表不同阶次。

旧版 E01 在建模前加规范化垫片后走完，确认耗时 382 秒；不加垫片生成 0 个候选。本轮把规范化放回 generate 的实际入口，并修复 verify 读取、prepare 配置错误、缩放坏输入和非球面表头映射。所有修复版测试保持 NOT RUN，需去掉垫片后重测，数值容差不变。

## 对照输入与预登记容差

| 场景 | 输入 | 预期与容差 | 本轮状态 |
|---|---|---|---|
| 上收数值一致 | `library/microscope/entries/` 中原有 25 条目的 `extract.json`，使用相同 catalog、focal_length_mm、stop 参数 | 上收前后适配输出的半径、厚度、EFL、FFD、BFD 应逐值一致；未登记放宽容差 | NOT RUN |
| 原有物镜预筛 | 原有 `test_paraxial.py` 的厚双凸、平凸及 25 条复现存档 | 继续使用原测试的解析容差与归档阈值；存档数据通过不能替代本轮重新计算 | NOT RUN |
| G01 | 测试单片，EPD=2 mm；尺度 0.5、2、10 | 后端 EFL 按比例，rtol=1e−9、atol=1e−9 mm；同计算内核、相同波长下的尺度一致性 | NOT RUN |
| G01 非球面 | A4=0.001，K=2；带偏心、倾角、实体口径及物高 | 缩放前后归一化 sag 相同，rtol=1e−12；长度乘 K，倾角/锥面/f/# 不变 | NOT RUN |
| G02 | N-BK7 双凸，R=±50 mm、厚 5 mm、EPD=1 mm、0/0.1°、0.55 µm；像面移至近轴焦面 | 原生 TSC 与真实边缘光线像高，rtol=2%、atol=1e−7 mm；小孔径下保留少量高阶差异。逐面 SI–SV 求和与后端总和 rtol/atol=1e−12 | NOT RUN |
| G02 SEID 对照 | 同一处方与一致的孔径、视场、波长、像面 | 先登记 ZOS 系数含义与转换，才能定数值容差；本轮禁止启动 OpticStudio | NOT RUN |
| G03 | 两候选，零优化计划及统一 12 s 局部预算 | 第一阶段优化调用为 0；第二阶段全部候选使用同一阶段列表 | NOT RUN |
| G04–G06 | 旧 stop 改描述重新确认、模型文件改动、自由结构无比较、指定结构变化 | 原停止决定继续阻止记轮次；封存损坏拒绝；缺比较只提示；指定结构差异显示 | NOT RUN |

上收前数值对照的基线应从 HEAD `b4c085156dc97b95f6b6c87a3387d2ca8cd257f8` 的这 3 个模块提取到系统临时目录，在相同环境、相同输入运行两套入口并保存 JSON 对照；不得覆盖现有未提交文件。用户已对旧 63 文件版本完成该对照并报告 25 条逐值一致；当前修复版改动了缩放输入检查，尚未复测，不能把旧结果当作本版运行结论。

## 供测试执行者使用的命令

先检查 `SAOS_HOME` 没有把任务导向其他实例；用新建 verify 任务，禁止读写 `work/main/`。纯文件测试使用 pytest 临时目录。禁止关闭已有 daemon、不得使用历史 PID。

```bash
SAOS_INSTANCE=verify uv run pytest tests/test_candidates.py tests/test_selection_evidence.py tests/test_demand_layers.py tests/test_model_report.py tests/test_workflow.py tests/test_ui.py tests/test_docs_sync.py
SAOS_INSTANCE=verify uv run pytest tests/test_starts.py tests/test_milestones.py tests/recipes/microscope/test_paraxial.py tests/recipes/microscope/test_adapt.py tests/recipes/microscope/test_library.py
SAOS_INSTANCE=verify uv run pytest -q
```

G02 的真实 OpticStudio 对照需另行授权。E01 先以新 verify 任务完成需求、起点生成、两段初筛、模型确认、深度优化、报告；报告认可与进入 ZOS 的后半段另行授权。自动化确认语句标明“自动化测试夹具”。

浏览器与 draw.io 渲染同样没有在本轮执行；执行者按 AGENTS.md 命令检查状态、链接、节点文字和返回通道，并实际查看渲染 PNG。XML 能解析不等于展示通过。
