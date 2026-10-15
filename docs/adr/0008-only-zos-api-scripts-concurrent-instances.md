---
status: accepted
---

# 光学计算只经 ZOS-API，并发靠多个 OpticStudio 实例

用户决定（2026-10-15）：agent 不自写光学算法，包括追迹器、像差或近轴计算、代理模型和 GPU 代码；只写调用 ZOS-API 的 Python 脚本，一阶量也由 OpticStudio 计算。原因是自己重新实现面型和介质的含义很容易出错，而且不易察觉：fiber-grin 任务里，评价面的介质设错，就让三组焦距数据作废，而那还是在 OpticStudio 中算的。如果再加一层自写追迹，能出错的地方更多，交付数值也要多一道比对才能追溯。平时用一个实例；需要大量相互独立的评估时，用 `ctx.parallel` 同时开几个 standalone 实例并发调用 ZOS-API（`saos.parallel`）。本条取代 ADR-0003（逐光线计算用 C#）。

## Considered Options

- 允许在模型确认后自写追迹，逐光线与 OpticStudio 比对（原规则）：光纤任务的 numpy 追迹与 OpticStudio 最大偏差 0.23 µm，融合 CUDA 核比 4 进程 CPU 快约 20 倍（27 个设计 × 833 条光线：0.74 ms 对 14.9 ms）。但每换一种面型或介质都要重新实现、重新比对。
- 逐光线计算用 C# 调 ZOS-API（ADR-0003）：批量追迹时比 Python 快，但脚本不再是 agent 容易读写的同一种语言，而且一直没有用上。

## Consequences

唯一的例外是显微物镜配方的近轴预筛（`saos.recipes.microscope.paraxial`，用于 `library check` 和 `adapt`）。它只在入库前核对专利表头，条目必须再通过 OpticStudio 的复现校验才算收录。2026-10-15 按 25 个收录条目的复现记录核对过：EFL 相对差 ≤ 5.4e-4，工作距离差 ≤ 0.0134 mm，比它自己的 1% 判据小一个数量级。这次对比由 `tests/recipes/microscope/test_paraxial.py` 固定下来。

吞吐受实例数和许可证限制。2026-10-15 实测：1、2、4 个实例分别约 89、161、304 次/秒，每个实例启动 5–6.5 s；许可证上限为同时 8 个实例，手动打开的窗口也计在内。需要比这更多评估的搜索，只能缩小搜索范围或延长运行时间。
