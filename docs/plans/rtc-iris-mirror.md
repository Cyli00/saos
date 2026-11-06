# rtc 补齐孔径光阑与反射面：整体计划（2026-11-04）

目标：让 rtc 能追迹并对账两类系统：一是由光阑（iris）定孔径、带非圆面孔径或渐晕因子的系统，二是含反射镜的系统（先做同轴反射，再做折叠光路）。每一项都要与 OpticStudio 2024 R1 逐条光线对账，之后才从拒绝清单里移除。

[rtc-need-stage.md](rtc-need-stage.md) 当初把"倾斜偏心与反射镜"列为不涉及；本计划接着补这一块，不改动那份计划的范围。

**当前状态：候选代码已整合，已运行第 7.3 节的全部验证，结果和未解决项见第 9 节。** 2026-11-04 在第一版精简后的工作区补齐 D1、D2、D4、D5，加入 R1–R9 的实现或候选入口及回归用例。生产拒绝清单不因候选代码存在而解除，不能将本计划视为完成。

## 1. 现状（2026-11-04 读代码并按第一次录入的真值修正）

状态的含义：
- **保留**：代码留下，录入脚本和离线比对已写好，待用户重录后运行（第 6 节）。
- **待修**：代码留下，有已知缺陷或验收缺口，见第 3.2 节。
- **候选**：实现与验收入口已写，用户尚未运行；生产仍拒绝或保持原计算路径。
- **部分实现**：已有可调用路径，但仍缺持久化、算法或验收覆盖，见第 7 节。
- **未开始**：还没有实现。

| 能力 | 状态 | 现在的行为 | 位置 |
|---|---|---|---|
| Float By Stop Size，瞄准 off | 待对齐 | 当前仍是近轴传递系数；与UpdatePupil 的追迹标定分支映射未完成，见第 8 节 | `src/rtc/system.py` |
| Float By Stop Size，瞄准 real | 已放开 R1（2026-11-05 用户决定） | 真值光线对得上，EXPP、EXPD 差约 4e-6；生产可用，列在 `computed_by` | `src/rtc/trace.py`、`tests/rtc/test_case_truth.py` |
| 光阑半口径当设计变量 | 部分实现 R2 | rtc 的 vary/solve、优化器、AD 支持 FLOA 光阑；变量标志仅保存在内存；实际瞄准的半口径导数带隐函数项，已与差分核对 | `src/rtc/adtrace.py`、`src/saos/backend/rtc.py` |
| 圆孔、圆遮挡、矩形孔（含偏心） | 待修 | 内核裁剪；`.zmx` 读写 `CLAP`、`OBSC`、`SQAP`、`OBDC` | `src/rtc/zmx.py` |
| 浮动孔径（`FLAP`，类型码 10） | 保留 | 按面的半口径裁剪；只能从 `.zmx` 读入，处方写不了。真值在变体 `iris_floating`，待录入 | `src/rtc/kernel.py` |
| 蜘蛛、椭圆孔/遮挡、矩形遮挡 | 候选 R3 | 底层读写与裁剪已接通；蜘蛛改为整圈均布；后端仍拒绝 | `rtc_lens.from_prescription(experimental=True)` |
| 用户定义孔径（8/9） | 不在范围内 | 拒绝 | 同上 |
| 遮挡孔的画图与重叠检查 | 待验 R4 | 新增可选 obscuration 折线，窗口和报告叠画遮挡；实体检查不扣除遮挡区 | `src/saos/geometry.py`、`ui/static/app.js`、`report_sheet.py` |
| 自动半口径与 OpticStudio 对账 | 候选 R5 | 默认保留 16 条边缘光线；experimental=True 对旋转对称系统已对上真值（第 9 节），非旋转对称面型未对上 | `rtc_lens.auto_semi_diameters` |
| 视场渐晕因子：浮点追迹 | 已对账 R6 | 表外取曼哈顿距离最近的表内行，不插值（第 9 节）；模型描述保留真实因子 | `src/rtc/system.py` |
| 视场渐晕因子：AD 追迹 | 待验 R6 | 共用浮点追迹的光瞳变换，已移除渐晕拒绝，新增非主波长中心差分用例；表外视场的 AD 没有单独用例 | `src/rtc/adtrace.py` |
| 同轴反射：球面、圆锥面，瞄准 off | 待修 | 浮点、内核、AD 都有反射分支；真值在 `reflective` 及其变体 | `src/rtc/kernel.py` |
| 非球面/双锥/环面反射镜、反射后的近轴面 | 候选 R7 | 反射后近轴方向已修；底层真值用例覆盖这些路径，后端继续拒绝。Standard 反射镜配实际瞄准已放开（2026-11-05 用户决定），列在 `computed_by` | `src/rtc/trace.py`、`kernel.py`、`adtrace.py` |
| 反射系统的波前、PSF、OPD 操作数 | 部分实现 R8 | OPD/FFT/Strehl/RMS 底层实验入口；操作数和默认入口仍拒绝镜面，Huygens 未实现 | `src/rtc/psf.py` |
| 全反射 | 不改 | 记错误码 −面号，光线终止，和 OpticStudio 序列模式一致 | `kernel.py` |
| CODE V 优化基准 | 待验 R9 | pythonnet COM，限时 AUT 与 rtc AD-DLS；两终点由 OpticStudio 同设置测光斑，已运行，结果见第 9 节 | `tests/bench_codev.py` |
| 坐标断点、面倾斜/偏心 | 未开始（阶段 C） | 拒绝 | `rtc.py:38`、`:326` |

**历史：第一版舍去时删掉的东西**（本轮候选从定义和存盘样本重写，当前状态以上表为准）：
- `start_opl`（内核 `ostart`、`RayBlock.start_opl`）和主光线被挡时换参考光线的逻辑；
- `psf.py` 对反射系统的全部改动；
- `semi_diameter` AD 变量；
- 自动半口径的倍增取样、`SDMA` 余量和对应的真机用例；
- `geometry.py` 的 `obscure`/`hole_*`/蜘蛛判定，以及 `rim_hole`/`profile_hole` 视图字段和 `app.js` 的画法；
- `tests/rtc/test_material_codev.py`（拿 N-SF5 和 SF5 这两种不同玻璃相比，判不出什么）。

**这次一并收紧的行为**（这两条没有离线用例）：
- ZOS 只有 Float By Stop Size 才推迟设孔径类型，其余系统保持 HEAD 的顺序（`src/saos/backend/zos.py`），物方远心不再受影响；
- `_model_problems` 重新拒绝 OBNA 副本。

## 2. 对照资料与可复用代码

| 来源 | 用途 | 注意 |
|---|---|---|
| OpticStudio 2024 R1 手册 | 系统孔径（Float By Stop Size）、面孔径类型（手册 §14.46，`rtc.zmx.Surface.aperture_code` 已引用）、渐晕因子公式、坐标断点与 Tilt/Decenter 顺序、反射镜符号约定 | 真值以实测为准，手册只当线索 |
| ZOS-API | `SystemData.Aperture.ApertureType = FloatByStopSize`；面孔径 `SurfaceApertureTypes.*`；`Field.VDX…VAN`；`SurfaceType.CoordinateBreak`；`TiltDecenterData`（Before/After、`ReverseThisSurface`）；`IBatchRayTrace` 逐条光线真值 | saos 的 ZOS 后端和 `prescription.py` 已能建这些结构（`prescription.py:47`、`:269`、`:1091`） |
| `E:\gitProjects\test-core`（`INDEX.md`、`NOTES.md`） | `geometry__UpdateAperture.c` 等文件里的常数和控制流 | 沿用 [rtc-need-stage.md](rtc-need-stage.md#参照资料) 的读法限制：每条线索都要实测确认，不复制函数正文 |
| `ref/repo/optiland`（MIT） | 反射：`rays/real_rays.py:215` `reflect`、`interactions/refractive_reflective_model.py`；坐标系：`coordinate_system.py`（`localize`/`globalize`）；孔径：`aperture/float_by_stop.py`、`physical_apertures/*`；`.zmx` 的 `COORDBRK`：`fileio/zemax/surfaces.py:321` | 只拿来对照算法和单测思路；optiland 不是 OpticStudio 的真值，不当对账依据 |
| CODE V 2026（`C:\CodeV`，COM 已注册 `CodeV.Application`） | 第二参考、优化基准、`.zmx` 关键字线索，见第 2.1 节 | 交付和准入的真值仍然只认 OpticStudio |
| 已有对账流水线 | `tests/test_zemax_rtc.py`：处方 → OpticStudio 与 `ctx.fast()` 逐条光线比对；设 `SAOS_RTC_RECORD=1` 时把真值录进 `tests/fixtures/rtc/`，离线回归由 `tests/test_rtc_backend.py` 跑。内核级 golden 在 `tests/rtc/fixtures/golden/` | 原 rtc 仓库的 `oracle/Harness3`、`tools/compare_golden.py` 没有并进来，仍在 `E:\gitProjects\rtc`。新对账一律走 saos 的流水线 |

### 2.1 CODE V 能提供什么（2026-11-04 查本机帮助与宏所得，CODE V 本身未运行）

资料位置：
- 帮助：`C:\CodeV\help\lens_system\*.html`、`C:\CodeV\help\api\*.html`
- PDF：`C:\CodeV\doc\LensSystemSetupRM.pdf`、`Optimization.pdf`、`APIReferenceGuide.pdf`
- 宏：`C:\CodeV\macro\zemaxos_to_cv.seq`（9349 行）
- 示例镜头：`C:\CodeV\lens\*.seq`

**一、概念对照（帮助原文）**

| 本计划的项 | CODE V | 出处 | 和 OpticStudio 的差别 |
|---|---|---|---|
| 系统孔径 | 只有 `EPD`、`FNO`、`NA`、`NAO` 四种 | `SystemData.02.05.html` | **没有 Float By Stop Size**。它的 `.zmx` 转换宏遇到 `FLOA` 时改用固定 EPD，并给出警告（宏第 730 行），所以 A1 只能和 OpticStudio 对账 |
| 光阑与默认孔径 | `STO Sk`。没有用户孔径的面，按参考光线交点给一个圆形"默认孔径"，而且**默认孔径会挡光**；`CA APE` 只用用户孔径，但光阑面的默认孔径仍然挡光 | `WorkingWithSurfaces.06.15.html`、`SystemData.02.53.html`、`HIDD_APERTURE.html` | OpticStudio 的自动半口径不挡光。拿 CODE V 对比逐条光线时，必须给每个面设足够大的用户孔径，或者用 `CA APE` 并核对光阑面 |
| 面孔径 | `CIR`、`ELX/ELY`、`REX/REY`、多边形；类型分 `CLR/EDG/HOL/OBS`；偏心 `ADX/ADY`，旋转 `ARO`；同一面多个孔径默认取交集，`ORA` 改为并集 | `HIDD_APERTURE.html` | CODE V 不支持蜘蛛孔径（宏第 2197 行直接丢弃） |
| 渐晕因子 | `VUX/VLX/VUY/VLY`：各方向半光瞳被裁掉的比例，只作用在 5 条参考光线上 | `SystemData.02.22.html`、`SystemData.02.24.html` | 转换宏按 `VUX = VCX − VDX`、`VLX = VCX + VDX` 换算（宏第 885–930 行）。但宏里 `ZVDY` 分支对 Y 用的是相反的符号，宏自身前后不一致，只能当线索 |
| 反射 | `RMD Sk REFL`（或 `GLA Sk REFL`）；`TIRO` 表示只在发生全反射时反射，否则算光线失败。反射面之后的介质自动取反射面之前的介质；奇数次反射后厚度为负，曲率符号约定不变 | `materials.11.29.html`、`lensdata.03.27.html` | 与阶段 B 采用的 OpticStudio 约定一致 |
| 偏心与倾斜 | `XDE/YDE/ZDE`、`ADE/BDE/CDE`。类型有基本型、`DAR`、`BEN`、`REV`、`RET`、`GLB`。运算顺序：平移 THI → 偏心 → 偏移点 → α → β → γ → 折射/反射 → `DAR` 时还原 / `BEN` 时再转一次 | `tilts.12.09.html`、`tilts.12.11.html` | 宏的换算（宏第 2256–2440、2890–2910 行）：<br>• OpticStudio 的 X、Y 倾斜对应 CODE V 的 `−ADE`、`−BDE`，Z 倾斜对应 `+CDE`；<br>• 面属性 After 的 mode 1 ≈ `BEN`，mode 2 ≈ `DAR`；<br>• order 0 = 先偏心后倾斜，order 1 = `REV` |
| 倾斜系统的一阶量 | 除 `RET` 面外，一阶计算忽略倾斜和偏心 | `tilts.12.19.html` | 这是 CODE V 的定义。阶段 C 第 5 项要实测的是 OpticStudio 怎么做，不能直接套用 |

**二、`.zmx` 关键字线索**

来源是 `zemaxos_to_cv.seq` 怎么读这些行；列号从关键字后的第 1 个数算起。

| 关键字 | 宏的读法 |
|---|---|
| `FLOA` | 系统孔径为 Float By Stop Size（行内无数值） |
| `CLAP a b` | 圆孔：a 为最小半径（非 0 时转成遮挡），b 为最大半径，和 `zmx.py:315` 一致 |
| `OBSC a b` | 圆遮挡：a 为最小半径（宏不使用），b 为最大半径 |
| `ELAP a b` / `ELOB a b` | 椭圆孔 / 椭圆遮挡，a、b 为 x、y 半轴 |
| `SQAP a b` / `SQOB a b` | 矩形孔 / 矩形遮挡，a、b 为 x、y 半宽 |
| `SPID` | 蜘蛛 |
| `USAP` / `USOB` / `UDAD` | 用户定义孔径 / 遮挡 |
| `OBDC dx dy` | 孔径偏心，作用于以上所有类型 |
| `SCBD k order mode dx dy tx ty tz` | 面属性 Tilt/Decenter：k = 1 为 Before、2 为 After；order 0/1；mode 只对 After 有意义。COORDBRK 面上的 `SCBD` 含义不同（宏第 2286 行） |
| `VDXN/VDYN/VCXN/VCYN/VANN` | 各视场的渐晕因子，按视场逐列排开 |

这些是 CODE V 的读法，不是 OpticStudio 的定义。照第 3 节第 1 步，每个关键字仍要用 OpticStudio 存盘确认。

**三、作为第二参考和优化基准怎么用**

- **自动化**：COM ProgID `CodeV.Application`（注册表 CurVer 为 `CodeV.Application12`），逐条光线用 `RAYRSI`（`help/api/6CVInterfaceFunctions.6.43.html`）。导入 `.zmx` 用宏 `in ZEMAXOS_TO_CV <文件>`，只转换活动视场。Python 端优先用已有依赖 pythonnet 做 COM 晚绑定；如果不够用，要新增 pywin32 或 comtypes，加之前先审查依赖。
- **用途 1，语义交叉核对**：案例先导入 CODE V，确认反射、遮挡、偏心的几何和 OpticStudio 一致（比如 EFL、像面位置）。CODE V 和 OpticStudio 结果不同时，以 OpticStudio 为准，并把差异记进本计划。
- **用途 2，优化基准**（用户 2026-11-04 指出：做优化参考，CODE V 比 optiland 强得多）：
  - 在 `iris`、`reflective` 两个案例上，各取一组变量和一个评价目标（比如 RMS 光斑），分别用 CODE V AUT 和 rtc 的 DLS（`src/rtc/optimize.py`、`src/saos/backend/rtc_optim.py`）从同一起点优化；
  - 用 OpticStudio 测两边的终点，比较达到的目标值和耗时；
  - 重点检查阶段 B 第 5 项：AD 追迹的反射分支有没有拖慢收敛；
  - 两边评价函数的定义不同，这一项只作参考，不进准入。
- **示例镜头**：`C:\CodeV\lens\cassrc.seq`（R-C，主镜是光阑，`CIR OBS` 遮挡，后接 SF11 校正片）、`maksutov.seq`（弯月校正片背面局部镀膜作次镜，带 `CIR OBS`/`CIR HOL`）可以给 `reflective` 案例的结构作参考。案例的数值由执行者自己设计，不照搬示例。

### 2.2 每一项对应 OpticStudio 和 CODE V 的什么功能（2026-11-04 查本机文档，均未运行）

出处：
- ZOS-API 名称已在 `C:\Program Files\Zemax OpticStudio\ZOSAPI_Interfaces.xml` 查到；
- `.zmx` 行来自仓库里的存盘文件，标"线索"的只来自第 2.1 节的 CODE V 转换宏；
- CODE V 的路径相对 `C:\CodeV\help\`。

此表的测量目标是 OpticStudio。CODE V 与 OpticStudio 的语义分别适用，定义不同的地方在“差别”里写明。

**阶段 A**

| 项 | OpticStudio：设置与 ZOS-API | OpticStudio：怎么取真值 | CODE V | 差别 |
|---|---|---|---|---|
| Float By Stop Size（R1、R2） | System Explorer → Aperture；`ZemaxApertureType.FloatByStopSize`；`.zmx` 行 `FLOA` | `EPDI` 操作数；`IBatchRayTrace` 在光阑面的交点 | 无对应类型，只有 `EPD/FNO/NA/NAO`（`lens_system/SystemData.02.05.html`）；瞄准见 `lens_system/SystemData.02.18.html` | 只能和 OpticStudio 对账 |
| 实际光线瞄准（R1、R7） | `ISDRayAimingData.RayAiming`；`.zmx` 行 `RAIM` | `IBatchRayTrace` 逐条比对 | 参考光线瞄准 `lens_system/HIDD_CHIEF_RAY_AIMING.html`、`introug/10TechTalk.11.14.html` | CODE V 瞄参考光线，定义不同 |
| 光阑半口径当变量（R2） | LDE 的 Semi-Diameter 列 `ILDERow.SemiDiameterCell` | 改值后重读 `EPDI`、`WFNO` | 无 Float By Stop Size，没有直接对应 | 只和 OpticStudio 比 |
| 面孔径裁剪（R3） | `ISurfaceApertureCircular`、`…Rectangular`、`…Elliptical`、`…Spider`；`.zmx` 行 `CLAP/OBSC/SQAP/OBDC` 有样本，`SQOB/ELAP/ELOB/SPID` 已有 truth.json 存盘样本 | `IRayTraceNormUnpolData.AddRay` 后由 `ReadNextResult` 读渐晕码 | `CIR`、`REX/REY`、`ELX/ELY`，类型 `CLR/OBS/HOL`，偏心 `ADX/ADY`（`lens_system/HIDD_APERTURE.html`、`HIDD_REVIEW_APERTURES.html`）；排除被挡光线见 `diagnostic/rim.07.13.html` | CODE V 无蜘蛛孔径；默认孔径会挡光（第 2.1 节） |
| 遮挡的画法（R4） | Cross-Section 分析；`ILDERow.SemiDiameter` 给外缘 | 目视对照 Cross-Section 的外缘 | `CIR OBS` 的 VIE 绘图 | 只是显示，不进逐条光线对账 |
| 自动半口径（R5） | Semi-Diameter 列 Automatic；`ISDApertureData.FastSemiDiameters`、`SemiDiameterMargin`、`SemiDiameterMarginPct`；`.zmx` 行 `DIAM`、`SDMA` | LDE 读 `ILDERow.SemiDiameter`，分别开关 `FastSemiDiameters` | 没有用户孔径时的"默认孔径"（`lens_system/WorkingWithSurfaces.06.15.html`） | CODE V 的默认孔径挡光，OpticStudio 的不挡；只能作参考 |
| 视场渐晕因子（R6） | `IField.VDX/VDY/VCX/VCY/VAN`；`IFields.SetVignetting`、`ClearVignetting`；`.zmx` 行 `VDXN…VANN` | 带因子的 `AddRay` 逐条比对；`WFNO` | `VUX/VLX/VUY/VLY`（`lens_system/SystemData.02.22.html`），自动生成见 `hlp/HIDC_CALC_VIGN_X.html`；AUT 选项 `hlp/HIDC_AUTPAN8_RAYGRID_VIGPUPIL.html` | CODE V 作用在参考光线上，OpticStudio 变换光瞳坐标，不能互换数值 |

**阶段 B**

| 项 | OpticStudio：设置与 ZOS-API | OpticStudio：怎么取真值 | CODE V | 差别 |
|---|---|---|---|---|
| 反射面（待修、R7） | LDE Material 列写 `MIRROR`；`.zmx` 行 `GLAS MIRROR` | `IBatchRayTrace`、`PLEN`、`EFFL`、`EXPP`、`EXPD` | `RMD Sk REFL`（`lens_system/materials.11.29.html`）；背景 `introug/6ReflSystems.07.1.html`；单条光线 `diagnostic/rsi.03.01.html`、API `RAYRSI`；一阶量 `diagnostic/HIDD_H_FirstOrderData.html`（FIR） | 符号约定一致（第 2.1 节），可用 FIR 和 RSI 交叉核对 |
| 反射面的折射率读数（D3） | `INDX` 操作数 | 在 `reflective` 的三个面上读一次 | 不适用 | 只和 OpticStudio 比 |
| 波前与 OPD（R8） | `OPDC`、`OPDX`、`OPDM`；`I_Analyses.New_WavefrontMap`、`IAS_WavefrontMap`；`New_ZernikeStandardCoefficients` | 带遮挡和不带遮挡各读一次 Wavefront Map 和 RMS | WAV（`diffraction/HIDD_WAVPAN1.html`），API `RMSWE`；参考球位置 `hlp/HIDC_RSLUserDefined.html`；光瞳图 `diagnostic/pma.09.27.html` 是带遮挡的 Cassegrain 例子 | 参考球和环形光瞳采样两边各有定义，以 OpticStudio 为准 |
| PSF 与 MTF（R8） | `I_Analyses.New_HuygensPsf`、`New_FftMtf` | Strehl、MTF 曲线 | PSF `diffraction/HIDD_H_PSF.html`；MTF `diffraction/mtf.2.02.html`，遮挡的影响见 `diffraction/mtf.2.20.html` | 可作趋势核对，不进准入 |
| 优化基准（R9） | DLS，`IOpticalSystemTools.OpenLocalOptimization`、`ILocalOptimization` | 用 OpticStudio 测两边终点 | AUT（`optimization/`，例子 `eml/ExampleModelLibrary_16.html` 起）；`AUT` 里 `Use Chromatic Pupil aiming`（`hlp/HIDC_AUTPAN8_DMD_0.html`） | 评价函数定义不同，只作参考 |

**阶段 C**

| 项 | OpticStudio：设置与 ZOS-API | OpticStudio：怎么取真值 | CODE V | 差别 |
|---|---|---|---|---|
| 坐标断点、面倾斜偏心 | `LDE.SurfaceType.CoordinateBreak`；`ILDETiltDecenterData`；`.zmx` 行 `COORDBRK`、`SCBD`（线索） | `IBatchRayTrace`、`EFFL`、`EPDI` | `XDE/YDE/ZDE`、`ADE/BDE/CDE`、`DAR/BEN/REV`（`lens_system/tilts.12.09.html`）；倾斜系统的一阶量 `diagnostic/fio.02.6.html`、视场 `diagnostic/fie.08.13.html` | 符号换算见第 2.1 节；CODE V 一阶计算忽略倾斜，OpticStudio 须实测 |

### 2.3 案例设计（2026-11-04 重做）

处方、变体和录入脚本都已落地：`tests/fixtures/cases/iris.json`、`reflective.json`，`tests/case_variants.py`，`tests/test_zemax_cases.py`。离线比对和覆盖自检在 `tests/rtc/test_case_truth.py`。

下面标“估算”的扫描用的是 rtc 自己的追迹（每视场 11285 条光线，主波长），只用来判断案例有没有判别力，不算对账；标“实测”的来自 2026-11-04 第一次录入。

**重做的原因：旧案例的硬伤**

| 案例 | 扫描结果 | 后果 |
|---|---|---|
| `iris` | 轴上 100% 通过；8° 只有 9.3% 通过；16° **0% 通过**，全部被面 1 挡住 | 三个视场有一个完全没有可用光线，一个几乎没有。这也是 Set Vignetting 压成一点的原因：不是工具的问题，是这条设计的问题 |
| `iris` | 矩形孔（面 6）紧贴光阑（厚度 0），到达它的光线 x ∈ [−2.09, 2.39]，矩形 x ∈ [−2.5, 3.5] | 全光瞳**没有一条**光线被矩形孔首挡，偏心完全没被验证。V1 原先"专门选光线"的说法行不通 |
| `iris` | 浮动孔径（面 5）也没有首挡光线 | 类型码 10 的裁剪没被验证 |
| `iris` | 面 1 的挡光和渐晕因子同时存在 | R5 分不清半口径受哪个影响 |
| `reflective` | 面 3 反射后介质还是 N-BK7，厚度 161 直到像面，**像面在玻璃里**（z = −149，像方 n = 1.5168） | 不是真正的曼金镜，光线从不折射出玻璃，"反射后再过前表面"没有覆盖 |
| `reflective` | 只有两次反射，像方 σ = +1 | 奇数次反射的 EFFL、PETZ、WFNO、EXPP 没有真值，只有单凹面镜的解析用例 |
| `reflective` | 主镜的孔没有光线穿回来（像面在主镜前方） | R4 的"遮挡区算不算实体"没有样本 |
| 两者 | 所有录入光线 `hx = 0`，视场全在 y 轴上 | x 视场、`VDX` 和 `VAN` 的方向没有判别力，表外视场也没样本 |
| 两者 | 没有 OPD、波前读数；`INDX` 也没逐面录 | R8、D3 无从验证 |
| `tests/rtc/test_paraxial_glass.py` | 第 1 面是平的近轴面 | 验证不了 R8 的第 1 个问题（弯曲第 1 面的 OPD），要在 `sequential` 上补 |
| 测试 | 当时默认只走内核，numpy 路径没和真值比 | 反射与裁剪分支没有对账；numpy 路径已于 2026-11-05 删除（`rtc-unify.md`） |

**新案例**

`iris`（基础版不带渐晕因子，不然挡光和因子分不开）：
- 视场 (0,0)、(0,5)、(0,10)、(6,8)°，F/d/C，Float By Stop Size，瞄准 off。
- 面：R40 t5 N-BK7（圆孔 6.0）/ R−40 t6 / R−30 t2 N-SF5 / R30 t4 N-BK7 / R−30 t3 / 光阑 inf t3（半口径 2.5，在空气里）/ R35 t4 N-BK7（矩形 2.6×2.2，x 偏心 0.5）/ R−80 t20.04。
- 实测：EPDI 6.76688，EFFL 32.42915，WFNO 4.78819，TOTR 47.04。主波长 11×11 网格各视场通过 96.3%、88.9%、69.0%、74.1%；10° 视场首挡面 1 的 14 条、面 7 的 12 条。
- 实测：光阑的半口径本身不挡光。(0.6, 0.8) 视场的 (Px, Py) = (1, 0) 在光阑面半径 2.50033（rtc 算的），越过 2.5，OpticStudio 仍判通过。此前“光阑是浮动孔径、要点名录入首挡光线”的设想是错的，点名光线已删；类型码 10 由变体 `iris_floating` 验证，这条光线和 (0, 1) 在该变体上预期被面 6 首挡，待录入确认。

`reflective`（真正的卡塞格林型，光线穿过主镜孔）：
- 视场 (0,0)、(0,0.2)、(0.1,0.1)°，EPD 80，瞄准 off。
- 面：抛物面 R−800 t−300 MIRROR（光阑，半口径 45，中心遮挡 15）/ R−3000 t−8 N-BK7 / R−380 t8 MIRROR / R−3000 t335.68 空气。
- 遮挡半径原为 16，正好压在 11×11 网格的 |P| = 0.4 上，逐条比对只比到边界语义（第 6 节），已改成 15（|P| = 0.375，离最近的网格圆 1 mm）。
- 实测（遮挡半径 16 时）：EFFL 1439.84949，WFNO 18.19644，TOTR 343.68，主波长 11×11 网格各视场通过 84.0%、85.2%、86.4%。
- 估算：像面 z = +35.7（在主镜之后），回程光束在主镜平面半径约 1.1 mm。

**变体**：由基础版改处方副本得到，定义在 `tests/case_variants.py`，录真值的脚本和离线测试共用。OpticStudio 存盘的 `.zmx` 文本（第 3.1 节第 1 条的存盘证据）和读数一起放在 `tests/fixtures/cases/truth.json`，一个变体一行。基础版的 `.zmx` 单独放在案例目录。所有变体都在 `tests/rtc/test_case_truth.py` 逐条比对，失败不跳过；比对通过不等于放行，准入只看后端的拒绝清单。

| 服务 | 变体 | 要录的真值 |
|---|---|---|
| 类型码 10 | `iris` 给光阑加浮动孔径（`iris_floating`，录入时在 OpticStudio 上设） | 光线、渐晕码 |
| R1 | `iris` 瞄准 real；光阑移到第一面前 | 光线、`EPDI`。估算判别力：10° 视场光阑面 y 差 0.09–0.12 mm，像面差 0.013–0.021 mm，远大于 1e-6 |
| R2 | `iris` 光阑半口径改 2.0 | `EPDI`、`WFNO`、光线 |
| R3 | `iris` 在空气间隔里加一个面：矩形遮挡、椭圆孔、椭圆遮挡、蜘蛛、带最小半径的圆孔，每种各有无偏心两版，共 10 个 | `.zmx` 列序；光线和渐晕码。蜘蛛额外录一圈方位角扫描，定臂的方向 |
| R5 | `iris` 去掉两个孔径；带渐晕因子；`FastSemiDiameters` 开/关；`SDMA` 百分比和毫米 | 逐面半口径（LDE） |
| R6 | `iris` 用 `SetVignetting` 生成的因子；手写因子（`VAN` ≠ 0，含 x 视场） | 光线、`WFNO`，再加表外视场 Hy = 0.7、Hx = 0.5 的光线 |
| R7 | `reflective` 主镜换偶次非球面；次镜换双锥、环面；反射后加近轴面；瞄准 real；光阑移到次镜（反射镜在光阑前） | 光线、一阶量 |
| R8 | `sequential` 不改镜头；`reflective` 去掉遮挡；`reflective` 基础版 | `OPDC`、`OPDX` 网格，Wavefront Map RMS，Strehl |
| 奇数次反射 | `reflective` 只留主镜（σ = −1，估算 EFFL 400） | 光线、`EFFL`、`WFNO`、`EXPP`、`EXPD`、`PETZ` |

**每个案例和变体都录的真值**（`truth.json` 里变体名对应的一行）：11×11 光瞳网格，基础版录所有波长，变体只录主波长；每条光线的错误码、渐晕码和像面的 x、y、z、l、m、n；每个视场五条光线的逐面数据；`PLEN`；`EPDI`、`EFFL`、`WFNO`、`ENPP`、`EXPP`、`EXPD`、`PETZ`、`TOTR`；逐面逐波长的 `INDX`；逐面半口径和类型（Automatic/Fixed）。R5 的变体只录半口径和设置，不录光线。R8 的 Strehl 没有录，实现 R8 时再定怎么取。

**覆盖自检**（`test_case_truth.py`）：离线测试断言真值本身有判别力，这样案例以后改坏了会直接报错：面 1 的圆孔和矩形孔都有首挡光线，越过光阑半口径的光线只在加了浮动孔径时被挡，每个视场至少有一半光线可用，主要变体相对基础版确有光线变化。

## 3. 执行流程

### 3.1 每一项的完成判据

一项能力同时满足以下五条才算完成，第 1 节的状态才能改成"通过"，才能从拒绝清单移除：

1. 写法已确认：`.zmx` 关键字和列序来自 OpticStudio 的存盘文件，文件在仓库里能找到。
2. 真值已录入：OpticStudio 读数进了 `truth.json`，`source` 写明测量方法，测量脚本进仓库，能重测。
3. 离线回归断言到位：逐条光线比对位置、方向、错误码和渐晕码，样本里确实有被这项能力影响的光线（去掉这项能力，结果要变，见第 2.3 节的覆盖自检）。
4. 用户运行通过：对账容差沿用副本建立时的标准（长度 1e-6 mm，方向余弦 1e-7）；内核级 golden 保持 1e-10 mm 量级。
5. 拒绝清单与文档已同步（第 4 节）。

按对应软件的算法语义实现；optiland 只作工程参考。验收用于发现转写、字段映射、接口或样本问题。用户要求先实现，最后统一测试。

### 3.2 下一轮的顺序

**本轮顺序：先整合代码，最后由用户统一测试。** 第一轮基线见第 6 节；本轮不运行任何 pytest、基准或 OpticStudio 命令。代码交付后先按第 3.5 节重录，再跑离线与真机对账；当前真值不能证明已改过的变体通过。

**第 1 步：确认案例（第 2.3 节）。** 处方、变体、录入脚本和离线比对已写好，这一步看重录结果：
- V3：第一次录入的通过率和一阶量已写进第 2.3 节，和估算吻合；重录后 `reflective` 的通过率要更新（遮挡半径变了）。
- V1：`test_case_truth.py` 的覆盖自检全部通过。
- V4：`reflective` 覆盖了"反射后过前表面、穿过主镜孔"；"反射后再打到前一片凹面"看录入后 R7 的变体是否够，不够再加。
- V5：确认"每个案例 1 秒内算完"。

**第 2 步：修留下来的缺陷。** 每修一项，先写能复现它的用例；修完在第 6 节写明用例名。
  - D1：反射面读出 `MIRROR`/`{"kind": "mirror"}`。
- D2：AD 在非主波长的 EPD 发射和浮点追迹不一致。
- D4：`primary` 不随 `invalidate` 更新。
- D5：改光阑半口径后 `FLAP` 写回旧值。

**第 3 步：按第 3.3 节逐项重修。** 建议顺序是 R5、R1、R6、R3、R4、R7、R8、R2。R5 排第一，是因为浮动孔径和 `iris` 的半口径都依赖它；R8 排在反射与孔径都稳定之后。

**第 4 步：R9 和收尾。** 同步第 4 节。

### 3.3 重修清单

每项的 OpticStudio 和 CODE V 对应功能见第 2.2 节。

**R1 Float By Stop Size + 实际瞄准。** 变体 `iris_real`、`iris_stop_front`。验收：`iris` 副本瞄准 real，逐条光线和 `EPDI`；光阑移到第一面前再测一次。完成后移除 `_file_problems` 的拒绝。

**R2 光阑半口径当设计变量（决定 2）。**
- 雅可比要真实：发射环节对半口径的导数要进 AD，`Design` 的写入要改 `Item.semi`；
- rtc 变量表能设它（本轮已接通，验收用例在 `tests/test_rtc_design.py`）；
- `src/saos/model_gate.py:197` 的指纹保留类型、排除数值；F 数约束放进评价函数。

验收：AD 雅可比和中心差分一致；优化后 `EPDI`、`WFNO` 跟 OpticStudio 重算一致。

**R3 其余面孔径。** 每类先存盘确认 `.zmx` 列序（`SQOB`、`ELAP`、`ELOB`、`SPID`；`SPID` 列序宏里也没给），再放行。蜘蛛臂的分布：第一次录入的方位角扫描（4 臂，宽 0.5，轴上，光瞳半径 0.7）里，面 7 挡光的角度集中在 0°、90°、180°、270° 附近，是四个方向均布；第一版内核 `π·k/n` 配半线判定的 4 条臂落在同一个半平面；本轮改为 `−2π·k/n`，仍待用下述真值验收。每类各加一个面，再各加一个带偏心的面，用全光瞳光线比对渐晕码（第一次录入里，有无偏心首挡面不同的光线：矩形遮挡 10/162，椭圆孔 14/162，椭圆遮挡 10/162，蜘蛛 53/234，带最小半径的圆孔 22/162，都有判别力）。此前渐晕码值没有对账，这次一并核对。

**R4 遮挡孔的画图与重叠检查。** 第一版把孔当成元件外缘（主镜半口径 45 mm 只画 ±8 mm），重叠检查跳过这一面。重做时外缘和孔分开：`hx/hy` 保持半口径，孔放单独字段。遮挡只挡光，零件不一定被挖空（曼金镜就不是），重叠检查要先定义遮挡区算不算实体。改动 `view.json` 字段要同步 `app.js`。

**R5 自动半口径。** 用 LDE 读数实测确定：
1. 被前面面孔径挡掉的光线算不算；
2. 是否计入渐晕因子；
3. 采样规则（关和开 `FastSemiDiameters` 各测一次）；
4. `SDMA` 余量的计算顺序。

第一版倍增到 512 条，每次 `_recompute` 的光线数最多是 HEAD 的 64 倍，`iris` 仍差 0.3–0.8%（第一版自述，未复核），还把真机容差放宽到 1%。重做要同时满足第 3.1 节容差和耗时要求，`_recompute` 在优化的每一步都会运行（`rtc.py:502`、`src/saos/backend/rtc_optim.py:85`）。样品是 `iris` 光阑前的圆孔。

**R6 渐晕因子进 AD。**
- `trace_ad` 发射前应用 `vignetted_pupil`，并移除它和非球面专家的拒绝。
- 实测表外视场（比如 Hy = 0.7）：OpticStudio 是插值还是不施加，现有实现只认视场表里的行。
- CODE V 的渐晕因子只作用于参考光线，这一项只和 OpticStudio 对账。

验收：`iris` 上 AD 值和浮点追迹相同，雅可比和中心差分一致。

**R7 其余反射路径。**
- 非球面、双锥、环面反射镜：`_bracket_newton`（`kernel.py:124`）和 `geometry.bracket_newton`（`src/rtc/geometry.py:117`）在 N < 0 时没有样本，要逐条比对；
- 反射后的近轴面：`_paraxial_lens` 在 n 为负时的符号；
- 反射镜配实际瞄准，尤其是光阑前有反射镜。

`_conic` 的分支判据（`kernel.py:197`）用的 `t2·n` 是交点的 z，读代码看与传播方向无关，球面和圆锥面没有这个风险。

**R8 反射系统的波前。** 三个问题都要对照 OpticStudio 实测，再决定实现：
1. HEAD 的 OPD 是否漏算发射面到第 1 面这一段。第一版声称漏了（第 1 面弯曲时差几百个波），它对折射系统也有影响，先在 `sequential` 上用 `OPDC`、`OPDX` 确认，HEAD 的 `tests/rtc/test_paraxial_glass.py` 是现有基线；
2. 带符号的方向和像方折射率怎么进参考球；
3. 中心遮挡时主光线被挡，参考点和环形光瞳采样怎么定。

通过前 `_check_opd` 继续拒绝。

**R9 CODE V 优化基准。** 按第 2.1 节用途 2，在 `reflective` 和 `iris` 上比较 CODE V AUT 和 rtc DLS 的终点与耗时，只作参考，不进准入。要先定 COM 依赖。

### 3.4 符号约定（同轴反射，已实现）

反射后厚度为负；反射面之后的介质就是反射面之前的介质，内部用 `n_after = −n_before` 记录（`src/rtc/system.py` 的 `sigma`）。光程按 `|n|·几何路程` 计算。追迹内部用几何方向，对外报告的方向余弦乘 σ，这是 `reference.json` 记录的 OpticStudio 批量追迹口径。

### 阶段 C：坐标断点与面倾斜/偏心（折叠光路，工作量大）

A、B 合入并对账通过后单独立项（第 5 节决定 3），本节只记范围。做完后，光纤侧视探头这类棱镜斜面加折叠的模型就能在 rtc 上建（原 fiber 配方已于 2026-11-06 删除）。

改动点：
1. **数据模型**：`Item` 从只有 `vertex_z` 改为带全局姿态，即 3×3 旋转加平移。每个面先把光线转到本地坐标，再求交和折射/反射，最后转回全局。参考 optiland 的 `coordinate_system.py`。
2. **`COORDBRK`**：参数 1–6 依次是 X 偏心、Y 偏心、X 倾斜、Y 倾斜、Z 倾斜、顺序标志。厚度沿新的局部 z 轴。optiland 的 `fileio/zemax/surfaces.py:321` 有解析可以对照。
3. **面的 Tilt/Decenter 属性**：Before/After，以及 After 的 `ReverseThisSurface`、拾取。`.zmx` 是 `SCBD` 行，列序和 mode 的线索见第 2.1 节，须存盘确认。处方侧已有 `decenter_tilt` 和 `order`（`prescription.py:47`）。
4. **内核布局**：`Packed` 增加每个面的旋转矩阵和平移，`_one`、`_to_stop` 都要走本地坐标。只有同轴面的系统应当走原来的路径，不能变慢；前后用 `tests/test_zemax_rtc.py` 里记录的速度对比。
5. **近轴与瞄准**：CODE V 的一阶计算忽略倾斜偏心（`RET` 面除外，见第 2.1 节）。OpticStudio 怎样做没有查到，须实测：先测 EFFL、EPDI，以及瞄准目标在倾斜光阑上的坐标，再决定实现。
6. **saos 侧**：
   - `RECONCILED_TYPES` 加入 `COORDBRK`，去掉 `rtc.py:326` 对 tilt/decenter 的拒绝；
   - 剖面图和干涉检查改用全局坐标；
   - 视图数据 `view.json` 的格式会变，要评估对流程图窗口的影响。
7. **对账样品**：立项时新增 `folded` 案例，至少包含一面 45° 折叠平面镜（坐标断点、MIRROR、坐标断点）和一个带偏心的折射面。

### 不在本计划内

- 序列模式下全反射继续沿反射方向追迹（OpticStudio 序列模式不这样做）；
- 用户定义孔径（8/9）；
- 膜层透过率和偏振；
- 鬼像；
- 非序列；
- OBNA 实际瞄准不收敛：原因是瞄准目标算错，2026-11-05 已修，规则见 [rtc/SURFACE-PARAMS.md](rtc/SURFACE-PARAMS.md)「其它实测事实」。

### 3.5 运行命令（由用户运行）

完整顺序与 CODE V 基准命令见第 7.3 节。

- 录入案例和真值（起 OpticStudio）：`SAOS_RUN_ZEMAX=1 SAOS_CASE_RECORD=1 uv run pytest tests/test_zemax_cases.py -s`；加 `-k <变体>` 只重录那几个，其余变体的行不动。
- 离线：`uv run pytest tests/rtc tests/test_rtc_backend.py tests/test_rtc_instance.py tests/test_rtc_material.py tests/test_rtc_design.py tests/test_geometry.py tests/test_prescription.py -q`
- 起 OpticStudio 对账：`SAOS_RUN_ZEMAX=1 uv run pytest tests/test_zemax_rtc.py -s`（录真值时加 `SAOS_RTC_RECORD=1`）。

## 4. 落地时同步（按 AGENTS.md）

每放行或收紧一项，同一次改动里改这些文档，写的是当时的实际行为：

- 技能 `references/rtc-copy.md` 的 "What it cannot reproduce, it refuses" 一条；`references/scripting.md` 的 "The rtc instance" 一段。
- `src/saos/prescription.py` 的处方帮助。
- `tests/fixtures/cases/README.md` 和 `truth.json` 的录入脚本：案例或测量方法变了就改，读数由用户重录。
- `DESIGN.md`：只写用户决定，执行进度留在本计划。
- `docs/plans/README.md`：本计划的状态。

本轮未改工作流阶段，`docs/workflow.drawio` 与 `src/saos/ui/stage.py` 不变；R4 已增加可选的 `obscuration` 视图字段，`app.js` 和报告同步读取，旧视图无此字段仍可显示。

## 5. 用户决定（2026-11-04）

1. **对账样品**：新增专用案例 `iris`、`reflective`，以后再加 `folded`，放在 `tests/fixtures/cases/`，配套写测试代码（见第 3 节"先做"）。
2. **Float By Stop Size 下的光阑半口径**：允许当设计变量，模型确认时列为结构项。
3. **阶段 C**：A、B 合入并对账通过后单独立项。
4. **OBNA 实际瞄准（原 A5）**：不放进本计划，留在 rtc-need-stage.md。
5. **第一版实现**：质量不满意的部分直接舍去，恢复拒绝并标记"重修"，从第 2.2 节的定义和实测重写，不在旧代码上补。
6. **案例重做**：`iris`、`reflective` 按第 2.3 节重设，旧真值作废。变体的 `.zmx` 和真值起初一个变体两个文件，共 66 个未跟踪文件；用户质疑后合并成 `tests/fixtures/cases/truth.json` 一个文件。

## 6. 待修与待确认项（2026-11-04）

第一版 review 的条目里，属于舍去部分的已转成第 3.3 节的 R 项。这里只留仍在代码里的问题，每条修完写明用例名。

**基线（用户 2026-11-04 运行，改动之前）**：录入通过；离线 257 项，249 通过，8 失败；真机 9 项，7 通过，1 失败，1 跳过。逐项诊断（比对真值所得）：

| 失败 | 原因 | 判定 | 处理 |
|---|---|---|---|
| `test_rays_at_the_image…` 的 `reflective`、`reflective_stop_on_secondary`、`reflective_one_mirror`（内核、numpy 共 6 项） | 渐晕码不同的光线共 18 条，在主镜上半径全部是 16.0 到 16.00000001，正好压在遮挡边界上：OpticStudio 判挡，rtc 判通过 | 案例设计 | 遮挡半径改 15，边界语义见下面的待实测项 |
| `test_first_order…[reflective_stop_on_secondary]` | EXPD。瞄准 off 时 OpticStudio 用近轴边缘光线高度（9.46349，算得 18.94419），rtc 一律用实际高度（9.48013，算得 18.97750，瞄准 real 的变体实测正是这个值） | 实现 | `stop_pupil_radius` 按瞄准分开；瞄准 off 改后相对差 4e-8，瞄准 real 8e-8。近轴瞄准和有限物距没有样本，保持原逻辑 |
| `test_iris_rays_exercise_every_aperture` | 断言“光阑有首挡光线”。`iris` 的光阑没有孔径类型，半口径本身不挡光 | 用例 | 改成 `test_stop_semi_diameter_clips_only_with_a_floating_aperture`，加变体 `iris_floating` |
| `test_case_copy…[reflective]` | D3，见下 | 实现 | 已修 |

以上修改只用临时脚本比对了 `truth.json`（EXPD、INDX、未对账路径），pytest 待用户重录后运行。

**已修**

- **D3 反射面的材料准入。** 实测 `INDX`：反射面报入射侧介质（`reflective` 面 1 为 1.0，玻璃内的面 3 为 1.52238，与面 2 相同），所有面符号为正。`_confirm_indices`、`medium_indices` 对反射面改读入射侧那一面，`INDX` 操作数取绝对值，`_admit` 不再把反射面记成未对账路径。用例 `test_indx_of_every_surface_matches_opticstudio`、`test_medium_indices_of_a_mirror_read_the_incident_side`，以及 `test_case_copy_matches_opticstudio_ray_by_ray[reflective]`。

**本轮已修代码，尚未运行回归**

- **D1 镜面读取。** 材料名与来源分别返回 `MIRROR`、`{"kind": "mirror"}`；用例 `test_mirror_material_reads_as_mirror_and_has_mirror_source`，同时覆盖视图外缘与遮挡分离。
- **D2 非主波长 AD。** 发射改用主波长系统及其参数导数；求交值先应用当前 theta，避免面形仍停在初值。用例 `test_ad_values_and_derivatives_follow_changed_float_system`：4 个案例/变体 × 3 个波长，比较浮点值和中心差分。
- **D4 主波长缓存。** `invalidate()` 清理 primary，新系统从当前 items 及当前配置厚度构造；用例 `test_invalidating_nonprimary_system_rebuilds_primary_from_current_items`。
- **D5 FLAP 存盘。** 最大半径取当前半口径，与 DIAM 一致；用例 `test_floating_aperture_dump_tracks_the_changed_semi_diameter`。

**待实测**

- **孔径边界实现已修正，待运行回归。** 圆孔、圆遮挡及普通浮动孔径均对半径平方乘 `1−1e−8`；光阑面且瞄准开启时乘 `1−1e−7`。内外边界都应用该因子。椭圆遮挡含边界，依据 `decomp/is_ray_vignetted.c` case 7（资料摘要的 `<1` 与源码不一致，按源码 `≤1`）。

**验收缺口（第 3.2 节第 1 步，案例本身的问题见第 2.3 节）**

- **V1 孔径裁剪的真值。** 已改为全网格比对渐晕码，并加了覆盖自检，待重录后运行。圆遮挡、矩形孔和浮动孔径留在放行清单，要靠这一项通过。
- **V2 真值可重测。** `tests/test_zemax_cases.py` 第一次录入通过；改动后待重录。
- **V3 案例设计。** 已按第 2.3 节重做：光阑在空气里，基础版不带渐晕因子（因子在变体里），实际瞄准也在变体里（`iris_real`），`reflective` 的光线折射出玻璃并穿过主镜孔。一阶量和通过率已由第一次录入确认（第 2.3 节）。

## 7. 本轮实现边界与统一验证（2026-11-04）

### 7.1 实现取舍

- R2：`ctx.solve("6.semi_diameter", "variable")` / rtc `opt vary` 只接受 FLOA 的正光阑半口径；普通设置也禁止零或负值。AD 传播近轴入瞳位置和半口径导数，优化副本同时更新镜头与 items。结构指纹保留系统孔径类型和光阑身份，排除孔径数值，兼容旧 FLOA 指纹。未拿到原生半口径变量求解的存盘样本，变量标志仅存内存，保存数值后重载须重新 vary；实际瞄准的半口径 AD 明确拒绝。
- R3：格式来自已录入的原生 `SPID/SQOB/ELAP/ELOB`；低层 `from_prescription(experimental=True)` 可建候选，正常 backend 的拒绝列表不变。
- R4：`obscuration` 的 `rim/profile_x/profile_y` 是多段三维折线；原有外缘字段不改。遮挡不证明零件挖空，结构检查保留材料；已有玻璃内反射面无显式外缘时的排除规则不变。浏览器和报告的显示尚未人工检查。
- R5：自定义候选只复用既有追迹，因无已证实的兼容算法才补嵌套采样；每视场每波长最多 256×20 条光线。默认重算仍取 16 个方向，实验入口不进入每一步优化。已排除当前面及此前被挡的光线，并在失败或挡光时缩瞳；采样终止和开关映射仍须继续对齐，容差仍为 1e-6 mm；没有耗时结果。
- R6：表外规则按GetTargetPupil 实现，用已录的 off_table 检查实现一致性。AD 与浮点共用光瞳变换。已有实际瞄准、OBNA 发射导数近似不在此次补齐范围，不能据此宣称全路径雅可比正确。
- R7：未修改求交器根选择；新增奇数次反射后近轴面的真值变体，以此检查传播方向修正。其余反向非球面求交通过逐面光线用例验收。
- R8：实验路径从第 1 面交点补入射平面波相位，将输出方向还原为几何方向，按出瞳所在侧选参考球交点；按OO_OPDC 在主光线被挡时明确报错，不换参考光线，也不改 PLEN/RayBlock。仅支持无穷远物体、角度视场。OPDC 已有候选测试；OPDX、Wavefront Map RMS、Strehl 的一致性及 Huygens 反射实现仍未完成，默认镜面拒绝保持。
- R9：不加 COM 依赖，复用 pythonnet；每个命令设超时，AUT 可取消并关闭 COM。FLOA 先冻结为起点 EPD，只比较首面曲率与最后空气间隔两个变量；rtc 用固定采样相对主光线横向残差，CODE V 用默认 ERR CDV，权重不同。导入先核对各面曲率、厚度和 EFL，再优化；终点统一由 OpticStudio 测量。CODE V 终点文件由这两个读回变量重建，不是原生 CODE V 导出。尚未验证 COM 调用和商业软件运行结果。rtc 预算在每次残差计算前检查，单次 AD 求导可越过预算；超时保留已经求得的最低残差点。

### 7.2 文档、参考与代码的差异

以下按当前代码或已录真值处理，并已同步可操作的文档：

1. FLOA：UpdatePupil 使用追迹标定；当前代码仍是近轴传递系数，属于尚未对齐的实现。原“依据旧真值保留近轴定义”的结论撤回；须补齐孔径枚举与 SetUpRay 模式映射后对齐，不能把资料中实光路追迹直接等同于自行求解边缘光线。
2. INDX：金标准色散入口包含负折射率路径；当前记录的公开 INDX 读数为正，现代码在内部带符号、公开接口取正值。内部玻璃记录标志、GimmeGrinIndex 与公开操作数的映射仍需补齐，不能据当前读数否定内部算法。
3. 旧实现/用例将 FLOA 光阑半口径当作裁剪；现在只有显式面孔径（例如 FLAP）挡光，不自动创建 FLAP。
4. 旧技能称镜面材料读回为空；D1 已修，现读回 MIRROR。
5. DESIGN 仍指向已撤掉的 variants/、truth/ 两目录；现以 truth.json 为准。
6. 旧技能称自动半口径遵循 OpticStudio 规则；默认算法尚未对账，现明确标为估算，R5 只走实验入口。
7. 代码注释保留过旧 iris 的 EPDI 常量；已改为指向真值文件，避免把旧案例数值当当前事实。

### 7.3 建议用户按顺序运行

没有本轮运行结果。以下命令在 Git Bash 仓库根目录运行：

```bash
SAOS_RUN_ZEMAX=1 SAOS_CASE_RECORD=1 uv run pytest tests/test_zemax_cases.py -s
uv run pytest tests/rtc tests/test_rtc_backend.py tests/test_rtc_instance.py tests/test_rtc_material.py tests/test_rtc_design.py tests/test_geometry.py tests/test_prescription.py -q
SAOS_RUN_ZEMAX=1 uv run pytest tests/test_zemax_rtc.py -s
uv run python tests/bench_codev.py --case iris --output "$TEMP/saos-codev-iris-$(date +%Y%m%d-%H%M%S)"
uv run python tests/bench_codev.py --case reflective --output "$TEMP/saos-codev-reflective-$(date +%Y%m%d-%H%M%S)"
```

必须先重录：`iris_stop_front` 已改为 real 瞄准，新增 `reflective_one_mirror_paraxial`；此前调整的基础反射案例和 `iris_floating` 同样需重录。缺真值直接失败，不用 skip/xfail 掩盖。候选失败先分析算法和样本，不放宽容差或解除生产拒绝。R9 输出目录必须不存在，结果 JSON 包含命令、耗时、两终点测量或错误；两类评价函数不同，不把快慢或终点差异归因于单一求导方法。

## 8. 边界行为与错误处理修正（2026-11-05）

已改代码（尚未运行）：

- `trace.aperture_clipped` 与 `kernel._clipped` 同步平方半径容差；内核 AP 从 6 列增至 7 列携带容差，pack 的唯一生产写入点已同步。
- 椭圆遮挡按源码 case 7 包含边界，修正摘要写作严格小于造成的偏差。
- R5 候选只累计到当前面未失败、未被遮挡的交点；挡光也触发有限次数缩瞳重试。生产默认 16 点算法尚未切换，不能称已对齐目标算法。
- 实验 OPD 在主光线失败或被挡时抛错；带中心遮挡的候选用例检查失败语义，不再强求数值比较。无挡光主光线的用例仍逐点比较 OPDC。
- R6 的浮点与 AD 追迹共用渐晕变换和表外插值。

尚未对齐的具体项：FLOA 的 UpdatePupil/SetUpRay 分支与 AD；实际瞄准的步长、阻尼和迭代上限；R5 的环数终止、视场方位旋转及 FastSemiDiameters 内部开关映射；INDX 内部路径与公开接口映射；资料明确未给出的 TraceOPD 内部参考球算法。缺失信息不以新猜测补全，旧近似也不标为已对齐。

本轮仍按用户约定不运行 pytest、基准或 OpticStudio。统一验证命令见第 7.3 节；新增边界回归同时覆盖 kernel/numpy、瞄准开关、圆孔/圆遮挡/普通浮动孔径及椭圆遮挡边界。

## 9. 统一验证结果（2026-11-05）

用户要求运行第 7.3 节并修复。OpticStudio 重录全部 36 个变体（基线 34 个，新增 `iris_floating`、`reflective_one_mirror_paraxial`），之后又重录两个渐晕变体以加入表外测量点。

| 步骤 | 修复前 | 修复后 |
|---|---|---|
| 重录真值 `tests/test_zemax_cases.py` | 36 通过 | 渐晕变体重录 3 通过 |
| 离线回归（第 7.3 节第 2 条命令） | 387 项，349 通过，38 失败 | 388 项，387 通过，1 失败 |
| 真机 `tests/test_zemax_rtc.py` | 未运行 | 8 通过，1 跳过（跳过的是 `SAOS_RTC_RECORD=1` 才运行的录入用例） |
| CODE V 基准 | 两例都因脚本误判退出 | 两例 `status=measured` |

**测量得到的规则（推翻了之前的线索）**

- **R3 解析。** `zmx.py` 不读 `SPID/SQOB/ELAP/ELOB`，而 `SURFACE_MODELLED` 已把它们算作已建模：这类文件既不进未知行拒绝，也不裁剪光线。改为读入类型码，由 `_file_problems` 按类型码拒绝，回归在 `test_settings_rtc_cannot_reproduce_are_refused`。
- **R5 面孔径不影响自动半口径。** `iris` 面 1 带 6 mm 圆孔仍报 7.221506，与 `iris_no_apertures` 逐面相同。第 8 节“只累计未被遮挡的交点、挡光也触发缩瞳”与实测不符，已改。
- **R5 取样方向。** 旋转对称系统只沿 ±视场方向取边缘光线（轴上视场 ±y），33 个无余量变体中 31 个逐面偏差不超过 3.4e-10 mm，带渐晕因子时同样成立。第 8 节列为未对齐的“环数终止、视场方位旋转”在旋转对称系统上不需要。
- **R6 表外渐晕。** 不插值，取曼哈顿距离最近的表内行。两个变体各 4 个表外视场共 8 次反解全部符合；(0.26, 0.72) 和 (1, 0) 上欧氏距离指向另一行，已排除。距离相等时的取舍没有样本。

**未解决**

- **`iris_real` 的 EXPP、EXPD。** 相对差 −4.0e-6、−3.9e-6，超过 1e-6 容差，用例标严格 `xfail`，未放宽。2026-11-05 查明不是 FLOA 特有，也不是瞄准缓存或数值噪声：OpticStudio 的出瞳用一条小视场实主光线，瞄准开时读数还随最大视场变，这一部分规则未定，见 `rtc-unify.md`“查明的 OpticStudio 规则”。
- **R5 非旋转对称面型。** `reflective_biconic`、`reflective_toroidal` 的面 5 既不符合 ±视场方向（偏小），也不符合整圈取样（偏大 0.037、0.70 mm），暂用嵌套采样，没有用例覆盖。
- **R5 生产默认未切换。** 默认 16 点算法在 33 个变体中有 5 个偏差超过 1e-6，最大 0.70 mm（`reflective_toroidal`），旋转对称里最大 0.19 mm（`iris_set_vignetting`）。决定不整体切换，见 `DESIGN.md`；旋转对称的规则已对账，单独启用是下一步。
- **R9 基准只作参考。** `iris` 起点 RMS 均值 82.9 µm，rtc 47.7 µm（通过率 64–70%），CODE V 27.9 µm（42–48%）；`reflective` 起点 71.2 µm，rtc 28.6 µm，CODE V 38.0 µm（均 86%）。两边评价函数不同，也都没有焦距和通过率约束，不能据此判断优劣。rtc 两例都跑满 60 s 预算（`iris` 每次 AD 雅可比约 1.6 s，162 条光线），CODE V 0.25 s、0.09 s 正常结束；这不能归因于反射分支，`iris` 没有反射面反而更慢。
- 其余第 7 节列出的项（半口径变量存盘、Huygens 反射、OPDX/RMS/Strehl 一致性、遮挡显示的人工检查、孔径边界容差）本轮未处理。
