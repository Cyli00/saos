# 实测结论（2026-10-18）

环境：OpticStudio 2024 R1.00 Premium，一个 standalone 实例。样例：Cooke 40 degree field（下称 Cooke）、Double Gauss 28 degree field（下称 DG）。实测期间主工作区的 worker 池（4 个实例）正在跑扫描，计时有干扰；优化器只给 2 个核。原始结果在各次输出目录的 `results.json`。

## 结论改变了上一轮判断的

| 项 | 上一轮的判断 | 实测 |
|---|---|---|
| `New_Analysis` 先按默认设置算一遍 | 可能收益最大 | 确实先算（HuygensPsf、FftMtf 打开后就有结果，`SettingsFirst` 没有），但多出的只是默认采样那一遍：DG 上 0.01–0.14 s；HuygensPsf 256×256 一次 4.2–4.4 s，改用 `SettingsFirst` 只省约 0.01 s。两种做法结果逐位相同。值得改，但收益很小，除非某个分析的默认设置本身很重 |
| `glasscat` 解析 AGF 是重复造轮子 | 建议改用 `IMaterialsCatalog` | SCHOTT 364、OHARA 429、CDGM 331 种玻璃，AGF 解析与 `IMaterialsCatalog` 的 nd（差 ≤5e-5）、νd（差 ≤0.01）、状态全部一致；`IMaterialsCatalog` 逐个读要 14–27 s 一个目录，比解析文件慢得多。`app.GlassDir` 与 `DEFAULT_GLASSCAT_DIR` 在本机是同一目录（只差大小写）。保留 glasscat，只建议把目录改成取 `app.GlassDir` |
| `GetClosestGlass` / `DesignLockdown` 可以替代模型玻璃转换 | 推荐 | 不能直接用：6 个目标里 3 个选了 Obsolete 牌号（BAFN10、SF15、KZFSN2），而同 nd/νd 的 Preferred 牌号（N-BAF10、N-SF15、N-KZFS2）就在旁边；目标 (1.80, 25.0) 选了 SF6（νd 25.43）而不是更近的 N-SF6（25.36），度量不是单纯的 nd/νd 距离。DesignLockdown 换出的玻璃与 GetClosestGlass 相同；`UsePrecisionRounding`（3 位）之后经 API 读到的半径仍是 22.013592508…，`ConvertSDToMaxApertures` 之后半口径求解是 Fixed，与说明不符或含义不同，未查清 |

## 确认可用、值得接入的

- **Hammer 与玻璃替换**：Cooke 上 MaterialSubstitute 加 Hammer 15 s（`RunAndWaitWithTimeout(15)` 返回 TimedOut，`Cancel` 生效），评价函数 0.00979 → 0.00504，玻璃 SK16/F2/SK16 → SK51/F7/SK15。证实没有 Hammer 命令时 `--mode substitute` 不起作用。`TargetRunTimeM` 默认 30。
- **Global**：15 s 内 48 万个系统，`CurrentMeritFunction(1..10)` 可读，最好 0.00607；结束后编辑器里是最好的那个系统；在镜头文件所在目录写出 10 个 `GLOPT_0002_00N.zmx`，接入时必须让镜头先落在任务内的临时目录。
- **OrthogonalDescent**：可设，Cooke 上 0.51 s，0.00979 → 0.00909（DLS 同样用时到 0.00487），只是多一个选项。
- **SEQOptimizationWizard2**：可用。Spot + 高斯求积 3 环 6 臂与旧向导生成同样 69 行、同样评价值；矩形 8×8 加 DeleteVignetted 生成 201 行；Contrast 判据生成 MECA；MaxDistortionPct 加一行 DIMX；`IsHighManufacturingYieldAvailable` 为 true。
- **IComputeRMSSpotSize**：Cooke 三个视场 5.082/14.972/11.768 µm（高斯求积 6 环 12 臂），与 RSCE 操作数一致到 4e-13 mm，一次 0.013 s。StandardSpot 默认采样给 4.988/16.081/12.031 µm。
- **ILensCatalogs**：33 个厂商；THORLABS 3114 个镜头，EFL 45–55、EPD 10–30 检索到 161 个，0.02 s；`InsertLensSeq` 插入成功（8 面 → 11 面）。胶合透镜的 Shape 报 Unknown。
- **分析可以异步终止**：HuygensPsf 1024/512 采样，`IA_.Apply()` 0.007 s 返回、`IsRunning` 为 true，`Terminate()` 9 ms 内结束。可以用 Apply + 轮询 + Terminate 给分析加超时，替代现在无限等待的 `ApplyAndWaitForCompletion`。
- **LDE.GetIndex**：与逐个 INDX 结果完全相同，Cooke 8 面 × 3 波长 9 ms 对 25 ms。`GetGlass` 一次给出牌号、nd、νd。
- **实体模型三角网格**：Cooke 6176 个三角形，运行 0.043 s，`XCoords.Data` 一次读出 18528 个坐标 0.013 s，含面 1–7。几何是否反映机械边缘、非圆零件和管壁长度，没有核对。

## 新发现

- **StandardSpot 的 `GetRMSSpotSizeFor(f, w)`、`GetGeoSpotSizeFor(f, w)` 忽略波长参数**：w = 0..3 返回同一个值，值随分析设置里的 Wavelength 变化（全部波长 4.988 µm，只选 1/2/3 时 3.857/4.295/6.233 µm）。TODO 里 GEO 对不上的条目由此可以解释一部分：要按波长取值，必须每个波长单独跑一次分析。StandardSpot 不提供光线数据（RayData、ScatterPoints 都是 0 组），界面的点云不能从它取。
- **光线瞄准向导会卡死**：Cooke 上 `RunAndWaitForCompletion` 超过 10 分钟不返回；改用 `RunAndWaitWithTimeout(30)` 同样不返回。不运行就调 `GetRecommendedSettings` 报 "The wizard has not yet been run"。不要接入，除非查清原因。
- **74 / 156 种分析没有完整的设置接口**（`HasAnalysisSpecificSettings` 为 false），包括 RelativeIllumination、Transmission、ImageSimulation、Draw2D/Draw3D、CardinalPoints、FootprintSettings、VignettingDiagramSettings、UniversalPlot1D/2D、YNIContributions、SystemData、PrescriptionDataSettings。saos 的 `apply_settings` 改不了这些分析，只能走 `ModifySettings`。另有 14 种非序列分析在序列系统下 `New_Analysis_SettingsFirst` 返回 None。

## 第二轮：接入 `opt hammer` / `opt global` 时的实测

探测 `opt_lifecycle`、`hammer_auto`、`substitute_scope`（probe.py），以及经 saos CLI 的端到端运行（独立的 SAOS_HOME 和 standalone 实例，默认核数 10）。

- **都不会自己结束**：`IsFiniteDuration` 为 false；全局搜索设 `TargetRunTimeM = 0.2`（12 s）跑满 40 s 也不停；非自动的 Hammer 同样。所以 saos 把 `--max-seconds` 设为必填。
- **Hammer 的自动模式没用**：`AutomaticOptimization = True` 时 2 s、704 个系统就自己停，评价函数 0.004026，与局部优化相同；开着玻璃替换也一个都没换。`TargetRunTimeM` 设 1 或 30 结果一样。默认值是 false。
- **运行中写的文件**：全局搜索把前 10 名不断重写为镜头旁的 `GLOPT_NNNN_00K.zmx`，NNNN 在不同运行里是 0000、0001、0002，不固定，同名会覆盖，结束后文件留着。Hammer 写 `<镜头名>_0001_HAMMER.zmx`（状态栏显示 "Last Save: x Min"），正常结束或 Cancel 后 OpticStudio 自己删掉；自动模式不写。saos 结束后把新写或改写的这些文件移到 `lenses/global/NNN/`、`lenses/hammer/NNN/`。
- **玻璃替换的范围**：MaterialSubstitute 求解在局部优化里不起作用（玻璃不变），在 Hammer 和全局搜索里起作用。
- **替换不看目录状态**：Cooke 样例 3 次 Hammer、1 次全局搜索共 12 个替换，9 个是 Obsolete（SK51/F7/SK15，SSK2/KZFS1/BALF5，N-BALF5/KZFS6/N-BALF5，N-BASF2/BASF6/KZFSN5）；SCHOTT 目录 364 种里 233 种是 Obsolete，比例相近。`ISolveMaterialSubstitute` 只有 `Catalog` 一个设置，`ISDMaterialCatalogData` 也没有替换模板，ZOS-API 里找不到排除停产牌号的办法。saos 因此把目录状态（取自 AGF）写进材料来源、每轮记录和交付报告，建模说明对 Obsolete 给出提示，`opt hammer/global` 返回 `materials_changed`。
- **端到端数值**（Cooke，曲率可变、三片可替换，`--max-seconds 20`）：Hammer 两次分别 0.0114 → 0.000202（243 万个系统）和 → 0.000240（256 万个），换出的玻璃不同；随后全局搜索在第 8 s 被 `job cancel` 停下，用时 9.0 s，约 103–107 万个系统，`best_merits` 第一名等于编辑器里的值，也等于归档的第一个 GLOPT 文件 `lens load` 后的值；没找到更多不同系统时 `best_merits` 用起点值补齐。
- 运行结束时标准输出会出现一行 `*** FRU__delta_init(): Attempt to start when running!`，来自 OpticStudio，没有发现影响。

## 未测

ModifySettings 的实际用法、公差工具、多重结构、QuickAdjust；extension 模式（用户的 OpticStudio 窗口）下的 Hammer / 全局搜索。
