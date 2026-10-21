---
status: accepted
---

# Optiland 做默认计算后端，OpticStudio 在用户认可结果后验证并继续优化

用户决定（2026-10-20）：日常建模、调试轮次和扫描默认在 Optiland 0.6.3 上算；OpticStudio 不再在每一轮里复算，而是放在调试轮次跑完、报告交给用户看过以后：用户认为结果没问题，就把镜头交给 OpticStudio 复测（OpticStudio 验证），并在 OpticStudio 里继续优化，轮次上限重新给。最终交付的镜头与指标以 OpticStudio 阶段测得的数值为准。原因：standalone 实例启动 5.8–7.9 s、许可证同时最多 8 个，Zemax 的优化器和逐项 ZERN 拟合拖慢探索；每轮都在后台复算、自动补偿再回传告警的方案（原 Plan 3）要多一条验证队列、一类告警和一份定标镜头，用户认为弄复杂了。用户先看 Optiland 的结果再决定要不要进 OpticStudio，验证只做一次，也只对用户认可的设计做。

> 2026-10-21 用户决定：认可前不再回退到 OpticStudio。会话切换、孪生代算和建模期双端基准对齐被取消，OpticStudio 只在 `zos begin` 认可后启动。见 [DESIGN.md](../../DESIGN.md) 与 [重构计划](../plans/refactor-optiland-first-workflow.md) §15。以下正文中相关段落仅作历史记录。

本条取代 ADR-0008。ADR-0008 的理由仍然成立：自己实现面型和介质的含义容易出错，而且不易察觉。所以边界是：

- 光线追迹、材料色散和面型由计算后端（Optiland 或 OpticStudio）计算；agent 和任务脚本仍不自写追迹器、像差或近轴计算器、代理模型和 GPU 代码。
- saos 自己的代码只对后端给出的光线、光程和近轴量做后处理（Zernike 拟合、Seidel 到波像差的换算、RMS 的数值积分、闭式调焦、边缘厚度等），每一项都要能在双端基准对齐或 OpticStudio 验证里与 OpticStudio 的结果比较。
- Optiland 表达不了的镜头整个会话交给 OpticStudio；Optiland 没有映射的单次计算由孪生实例代算，主会话留在 Optiland。

## Considered Options

- 维持 ADR-0008，一律经 ZOS-API：数值都来自 OpticStudio，但探索速度和并发受启动时间和许可证限制（ADR-0008 的 Consequences）。
- 每轮由后台单个 OpticStudio 实例异步复算、自动补偿离焦、偏差超限时在下一条命令里告警（原 Plan 3）：每一轮都有 OpticStudio 依据，但要维护验证队列、告警、定标镜头和报告等待，还会在用户没看过的设计上占用许可证。
- 只用 Optiland、不再验证：最简单，但交付数值没有 OpticStudio 依据，违反「实测依据」底线。

## Consequences

- Optiland 阶段的报告是给用户看的结果，数值注明由 Optiland 计算、尚未经 OpticStudio 验证；用户不进 OpticStudio 阶段时，报告保持这一说明。
- OpticStudio 阶段一律从伴随文件里的处方重建镜头，Optiland 写出的 `.zmx` 只供查看；两端数值不一致时以 OpticStudio 为准，差别写进报告。
- 双端基准对齐只在建模时做一次，确认两端追迹的是同一个系统；不一致时写进建模说明，交给用户在确认模型时判断，不阻止确认。
- 用到 `ctx.system`、`ctx.zosapi` 的脚本和判定只能在 OpticStudio 上运行（ADR-0007）。
- Optiland 的版本锁定在 0.6.3，升级要先在基准镜头上重做双端基准对齐。
- OpticStudio 阶段还要用 Zemax 的优化器（阻尼最小二乘、Hammer、全局搜索）和 worker 池，相应代码不随 Optiland 优化器的落地删除。
