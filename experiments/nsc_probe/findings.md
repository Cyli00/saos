# 非序列（NSC）覆盖与实测（2026-10-20，OpticStudio 2024 R1.00）

`probe.py` 按处方搭最小系统（`saos.prescription.normalize` → `ZosBackend.build`，与 `lens build` 同一条路），读回后追迹看探测器功率。端到端（`lens build`、`lens surfaces`、`exec` 脚本键、`lens describe` 建模说明、`model_gate` 指纹、流程图窗口物体表）另在 `SAOS_INSTANCE=verify` 的任务 `20261020-144536-nsc-faces` 上走过一遍。

## 覆盖表

“实测”指在 OpticStudio 里搭过、读回过并追迹核对过；“能写未测”指处方或脚本键能写但没有专门核对；“写不了”指 saos 没有接口，只能自己写 ZOS-API 脚本。

| 场景 | 状态 | 依据 |
|---|---|---|
| 体物体（Cylinder、Annular、Rectangular Volume）、Standard Lens | 实测 | 杂散荧光任务；本探测 |
| 其他物体类型（按 ZOS-API 名，121 种） | 能写未测 | 参数按列标题或 par<N> 写，写错列出可选项 |
| 目录玻璃、模型玻璃、ABSORB、MIRROR | 实测（MIRROR 未测） | 模型玻璃用 GetIndex 回读 |
| 体散射：H-G、Mie DLL，角度散射 | 实测（角度散射未测） | `work/brainstray` 任务 explore/scatter-models |
| 体散射：光致发光模型 | 写不了 | 接口在 `VolumePhysicsModelType.PhotoluminescenceModel` |
| 面：反射、吸收、膜层 | 实测 | 见下 |
| 面散射：Lambertian | 实测 | 见下 |
| 面散射：Gaussian、ABg、BSDF、User DLL、profile | 读回实测，物理未核对 | 见下 |
| 面散射：ABg 文件 | 能写未测 | 本机 ABg 数据文件为空 |
| Scatter To 列表、重要性采样 | 实测 | 见下 |
| 要文件的类型：STL、POB、光线文件光源 | 实测 | 见下 |
| STEP/IGES/SAT、Source DLL、User Defined Object、Grid Sag | 能写未测 | 同一条路（ObjectTypeSettings.FileName1） |
| 非序列选项（段数、交点、强度阈值、简单光线分裂、嵌套数、胶合距离） | 实测（后两项未测） | 交点数上限 4000 |
| 追迹：分裂、散射、偏振、种子、核数、取消、存 ZRD | 实测 | |
| 探测器读数：矩形探测器总功率、命中数、逐像素功率与照度 | 实测 | |
| ZRD 读段（位置、方向、强度） | 实测 | |
| 光源页（偏振、光源颜色/光谱、采样方法、预传播、阵列） | 写不了 | `SourcesData` |
| Type 页（Rays Ignore、Consider/Ignore Objects、Object Is A Detector、记录光谱） | 写不了 | `TypeData` |
| 衍射页、折射率页（GRIN、双折射） | 写不了 | `DiffractionData`、`IndexData` |
| 混合模式（序列系统里的 NonSequential 面） | 写不了 | 处方只能纯序列或纯非序列 |
| 源功率单位（W、lm、J） | 写不了 | `SystemData.Units.SourceUnits` |
| 极坐标、彩色、体探测器读数；Path Analysis | 未测 | NCE 有 GetAllPolarDetectorDataSafe、GetAllColorDetectorDataSafe |
| 矩形探测器角空间数据（DetectorViewer） | 未测 | |
| NSC 优化（NSDD/NSTR 评价函数）、公差 | 未测 | |
| 轮廓与交叠检查 | 只有约 15 种类型 | Standard Lens、CAD、多边形没有轮廓 |

## 本次实测

平板：Standard Lens，两面平，厚 2 mm，N-BK7；光源为锥半角 1° 的点光源，10 万条光线，550 nm，追迹不分裂。

- **反射、吸收**：不加设置时后方大探测器 0.9167 W（两面菲涅耳透过 0.957²）。后表面设吸收、前表面设反射、全部面吸收，三种都是 0 W，读回的 `is` 与写入一致。
- **膜层**：两面 `I.50` 与两面 `I.99999999` 的功率比 0.2500000（理想膜 I.x 透过率 x）。面设为反射或吸收后膜层读回 `None`（OpticStudio 清掉），所以处方拒绝两者同写。
- **Lambertian**：后表面散射比例 0.3，远处 ±5 mm 探测器（只收未散射光）功率为不散射时的 0.6997。
- **Scatter To 列表**：后表面 100% Lambertian，列表只有一个偏轴 ±1 mm 探测器时，近处 ±100 mm 探测器从 0.917 W 降到 0（方向不朝列表物体的散射光被丢弃）。逗号分隔的 `"2, 3"` 只留下 `3`，saos 改用空格分隔并回读。
- **重要性采样**：后表面 100% Lambertian，偏轴（x = 20 mm、z = 50 mm）±2 mm 探测器。不用时 24 个种子平均 0.002011 ± 0.000018 W，每种子约 450 次命中；`size` 3 和 5 为 0.001979 ± 0.000001、0.001969 ± 0.000004（4 个种子），命中约 10 万和 3.6 万次；`size` 2 为 0.001676，偏低 17%。`size` 是以目标原点为心的球半径，要包住整个目标（±2 mm 方形半对角线 2.83 mm）。
- **其余散射模型**：Gaussian（fraction 0.2、sigma 0.05）、ABg（`ABG-EXAMPLE`）、BSDF（`FractalBlack_VIS.bsdf`）、User（`Lambertian.DLL`，fraction 0.4）、profile `LAMBERTIAN50` 写入后逐项读回一致，追迹无出错光线；物理结果没有核对。IOSS_* 接口各模型字段不同（Lambertian 只有 ScatterFraction，ABg 没有 ScatterFraction），处方按模型收字段。
- **要文件的类型**：类型设置的 `FileName1` 写入后 ChangeType；`GetFileNames1` 列出 OpticStudio 数据目录里的可选文件。这类物体的注释栏就是文件名：写注释 `my label` 后读回 `my label.SDF`、`FileName1` 为空，光线文件光源追迹 0 W 且不报错。处方因此拒绝给它们写注释。`sample.SDF` 只有 11 条光线，`# Analysis Rays` 写 20000 被改回 11；用 10 万条的 `RAYFILE_LB_T67C_100K_190608_ZEMAX.DAT` 追 2 万条，±500 mm 探测器收到 0.994 W。STL `block.stl`、POB `cube.pob` 搭建后文件名读回一致。
- **脚本键**（verify 任务 `scripts/001`）：`object.plate.face2.fraction` 改 0.5 后远处探测器 0.2407 W（预期 0.5 × 0.957 × 0.5 = 0.239）；`kept` 退出后膜层和比例恢复；给 STL 物体改注释、Lambertian 面读 sigma、面号越界、`is` 写错都报错。
- **指纹**：要设计的物体改膜层、散射比例、重要性采样不算结构变化；改面的反射/吸收或散射模型、环境物体换文件或改面、探测器加膜层算结构变化。
