---
status: accepted; ADR-0014 adds the rtc exploration copy; ADR-0016 lets need-stage feasibility use admitted rtc values; ADR-0017 moves design rounds to the rtc instance, re-measured on OpticStudio before delivery
---

# 计算统一在 ZOS standalone，光学数值只由计算后端算

用户于 2026-10-23 决定彻底放弃 Optiland 双后端（原 ADR-0012：探索在 Optiland，用户认可报告后才进 OpticStudio 复测），并授权在 `feat/zos-standalone` 实现。保留需求确认/计算版本拆分、起点库与等预算初筛、模型确认页、轮次与重测基准、封存和流程图窗口；从建模到交付统一经 ZOS-API standalone 计算，extension 只用于显性调试。这样不必维护两套镜头状态、数值约定和转换结论。

代价是探索也占用 OpticStudio 许可，冷启动成本通过常驻 daemon 和 worker 池摊薄。局部优化使用 Zemax DLS/OD，搜索使用 Hammer/Global；通用外层搜索、预算、取消、detach 保留。Optiland/Torch 专属代码和依赖退出运行包，实验可在 `archive/optiland-hybrid` 查阅。

## 光学数值由谁计算

agent 和任务脚本不自写追迹器、像差或近轴计算器、代理模型；光线追迹、材料色散和面型一律由 OpticStudio 计算。自己重新实现面型和介质的含义很容易出错且不易察觉：fiber-grin 任务里评价面的介质设错，三组焦距数据作废，而那还是在 OpticStudio 中算的；再加一层自写追迹，能出错的地方更多，交付数值也要多一道比对才能追溯。

saos 自己的代码只对后端给出的光线、光程和近轴量做后处理（Zernike 拟合、Seidel 到波像差的换算、RMS 的数值积分、闭式调焦、边缘厚度等）。起点预判复用 ZOS 操作数、批量追迹和 SeidelCoefficients 原生分析；三阶操作数以 waves 展示，不冒充未换算的 SI–SV。

例外有两处：

- 不依赖后端的近轴计算 `saos.starts.paraxial`（显微物镜配方的 `library check` 与 `adapt` 也用它），只用于入库前核对专利、教材提取的面数据，条目还要经后端复现校验才收录。按 25 个收录条目的 OpticStudio 复现记录核对：EFL 相对差 ≤ 5.4e-4，工作距离差 ≤ 0.0134 mm，比它自己的 1% 判据小一个数量级（`tests/recipes/microscope/test_paraxial.py`）。
- 脚本的 rtc 副本（ADR-0014），只用于探索，数值不作证据。

## 后果

- ZOS 保存的 `.zmx` 为权威文件。旧双后端任务和带 `.optiland.json` 快照的镜头不能续算或覆盖；新建任务按原始处方重建，或导入经 OpticStudio 另存的文件，重新确认与实测。旧账本、旧报告不改写，旧数值不作新证据。
- `saos zos begin` 和报告后重置轮次预算随双后端一起取消；需求与模型确认仍是优化关卡。
- 许可不足时报告失败，不切换计算后端。
