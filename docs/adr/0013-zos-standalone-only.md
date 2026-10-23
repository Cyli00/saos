---
status: accepted
---

# 保留工作流，计算统一回到 ZOS standalone

用户于 2026-10-23 决定彻底放弃 Optiland，并授权在 `feat/zos-standalone` 实现。以当前分支为基础保留需求确认/计算版本拆分、起点库与等预算初筛、模型确认页、轮次与重测基准、封存和流程图窗口；从建模到交付统一经 ZOS-API standalone 计算，extension 只用于显性调试。此决定取代 ADR-0012 的双后端流程，避免维护两套镜头状态、数值约定和转换结论。

代价是探索也占用 OpticStudio 许可，冷启动成本通过常驻 daemon 和 worker 池摊薄。旧记录中的启动耗时和并发上限是历史测量，不作为此次迁移的性能保证。局部优化使用 Zemax DLS/OD，搜索使用 Hammer/Global；通用外层搜索、预算、取消、detach 保留。Optiland/Torch 专属代码和依赖退出运行包，实验可在 `archive/optiland-hybrid` 查阅。

ZOS 保存的 `.zmx` 为权威文件，不再生成 Optiland 快照。旧双后端任务和带快照的镜头不能直接续算或覆盖；用户需新建任务并按原始处方重建，或导入经 OpticStudio 另存的新文件，重新确认与实测。旧账本、旧报告与材料数据不改写，不把旧数值视为新后端证据。需求与模型确认仍是优化关卡，`zos begin` 取消，报告不再重置轮次预算。

起点预判复用 ZOS 操作数、批量追迹和 SeidelCoefficients 原生分析；三阶操作数以 waves 展示，不冒充未换算的 SI–SV。完整色差与三阶表保留原生文本。参考：[Ansys Seidel Coefficients](https://ansyshelp.ansys.com/public/Views/Secured/Zemax/v261/en/OpticStudio_User_Guide/OpticStudio_Help/topics/Seidel_Coefficients.html)。

实现阶段不运行测试、不启动 OpticStudio、不渲染界面；用户统一安排测试。旧 `saos` 分支的已测 ZOS 实现作为复用依据，不代表此次工作流接线已经通过验证。
