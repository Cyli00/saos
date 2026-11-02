# library

跨任务共享的参考数据。每个库一个子目录，各有 README.md 写明来源、版本和用法；具体任务的数据不放这里。

脚本里统一经 `saos.library` 读（只读，不缓存，权威数据仍是各库文件）：`library.catalog()` 列出各库的条目数、字段和按规格检索的函数；`library.find("mechanics", "zerodur", category="mirror_substrate")` 按文本（条目任一处，不分大小写）和字段取条目，每条带上它引用的来源；`library.index("noa-61", 587.56)` 按材料库的色散模型算折射率，超出有效范围拒绝。新增库时在 `src/saos/library.py` 的 `LIBRARIES` 登记读法，`tests/test_library.py` 会检查仓库里每个库都读得出来。

| 库 | 内容 | 使用者 |
|---|---|---|
| [materials/](materials/README.md) | 光学胶、打印透镜树脂等材料数据：来源原件、色散模型、自定义 AGF | 所有任务（建模时选材） |
| [glasscat/](glasscat/README.md) | 在产玻璃目录（去掉停产牌号）及其生成脚本 | 玻璃替换 |
| [thermal/](thermal/README.md) | 热分析选材规则、厂家 dn/dT 原表、厂家与本机目录不一致的膨胀系数 | 无热化（温度多重结构） |
| [mechanics/](mechanics/README.md) | 镜坯与镜筒、隔圈材料的膨胀系数、密度、模量，带出处等级 | 反射、折反射、无热化 |
| [coatings/](coatings/README.md) | 红外、紫外和金属膜层材料的 n、k 表及生成 SAOS_COATING.DAT 的脚本 | 膜层与偏振透过率 |
| [patents/](patents/README.md) | 2456 个专利实施例（CODE V .seq 原文件）及按规格检索的索引 | 建模起型（`saos.starts.patents`） |
| [microscope/](microscope/README.md) | 显微物镜的初始结构库（专利实施例）、优化基准案例、停机参数校准证据 | 显微物镜配方 |
