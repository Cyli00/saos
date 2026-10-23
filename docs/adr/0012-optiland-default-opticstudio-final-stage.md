---
status: accepted
---

# Optiland 做默认计算后端，OpticStudio 在用户认可结果后验证并继续优化

用户决定（2026-10-20，2026-10-21 补充）：建模、调试轮次和扫描都在 Optiland 0.6.3 上算。轮次跑完、用户看过 Optiland 报告并认可后，才由 `saos zos begin` 启动 OpticStudio，按快照处方重建镜头、用同一套判定复测（OpticStudio 验证），并在 OpticStudio 里继续优化，轮次上限重新给。最终交付的镜头与指标以 OpticStudio 阶段测得的数值为准。

原因：standalone 实例启动 5.8–7.9 s、许可证同时最多 8 个，Zemax 的优化器和逐项 ZERN 拟合拖慢探索；用户先看 Optiland 的结果再决定要不要进 OpticStudio，验证只做一次，也只对用户认可的设计做。

## 光学数值由谁计算

agent 和任务脚本不自写追迹器、像差或近轴计算器、代理模型和 GPU 代码；光线追迹、材料色散和面型一律由计算后端（Optiland 或 OpticStudio）计算。原因是自己重新实现面型和介质的含义很容易出错，而且不易察觉：fiber-grin 任务里评价面的介质设错，就让三组焦距数据作废，而那还是在 OpticStudio 中算的；再加一层自写追迹，能出错的地方更多，交付数值也要多一道比对才能追溯。

saos 自己的代码只对后端给出的光线、光程和近轴量做后处理（Zernike 拟合、Seidel 到波像差的换算、RMS 的数值积分、闭式调焦、边缘厚度等）；这些结果在 OpticStudio 验证时与 OpticStudio 的数值比较。

唯一的例外是不依赖后端的近轴计算 `saos.starts.paraxial`（显微物镜配方的 `library check` 与 `adapt` 也用它）：只用于入库前核对专利、教材提取的面数据，条目还要经后端复现校验才收录。按 25 个收录条目的 OpticStudio 复现记录核对：EFL 相对差 ≤ 5.4e-4，工作距离差 ≤ 0.0134 mm，比它自己的 1% 判据小一个数量级（`tests/recipes/microscope/test_paraxial.py`）。

## 已取消的做法

2026-10-20 的初版还保留了认可前使用 OpticStudio 的几条路径：Optiland 表达不了的镜头整个会话交给 OpticStudio、单次计算由孪生实例代算、建模时做一次双端基准对齐。2026-10-21 用户决定全部取消：认可前 OpticStudio 不启动，Optiland 做不到的事直接报告为能力缺口；`SAOS_BACKEND` 也不能授权 OpticStudio。

## Considered Options

- 一律经 ZOS-API（原 ADR-0008，2026-10-15）：数值都来自 OpticStudio，但探索速度和并发受启动时间和许可证限制（1、2、4 个实例分别约 89、161、304 次评估/秒）。
- 每轮由后台单个 OpticStudio 实例异步复算、自动补偿离焦、偏差超限时告警：每一轮都有 OpticStudio 依据，但要维护验证队列、告警、定标镜头和报告等待，还会在用户没看过的设计上占用许可证。
- 只用 Optiland、不再验证：最简单，但交付数值没有 OpticStudio 依据，违反「实测依据」底线。

## Consequences

- Optiland 阶段的报告注明数值由 Optiland 计算、尚未经 OpticStudio 验证；用户不进 OpticStudio 阶段时保持这一说明。
- OpticStudio 阶段一律从原生快照的处方重建镜头，Optiland 写出的 `.zmx` 只供查看；两端数值不一致时以 OpticStudio 为准，差别写进报告。
- 用到 `ctx.system`、`ctx.zosapi` 的脚本和判定只能在 OpticStudio 阶段运行（ADR-0007）。目前两个配方的验收都属于这一类：光纤测量库直接读 ZOS-API，显微物镜验收用到 Optiland 没有的 `OSCD`、`DISG`、`MTFN`、`STRH`。
- Optiland 版本锁定在 0.6.3；升级要先在基准镜头上核对追迹结果。
- OpticStudio 阶段要用 Zemax 的优化器（阻尼最小二乘、Hammer、全局搜索）和 OpticStudio worker 池，相应代码不随 Optiland 优化器的落地删除。
- 后续整合方向见[当前计划](../plans/integration-workflow.md)，其中的拟实现行为不替代现行关卡。
