# 通用起点库

版本：2026-10-21。6 个条目均为 `sourced_unverified`：已登记来源，核验层级见下文，尚未完成整库数值验收。默认筛选范围保留来源示例条件，不代表整个形式的物理能力边界；材料缺失和计算失败必须保留为“未评估”。

| ID | 形式 | 片数 | 半视场上限 | f/# 下限 | 来源 |
|---|---|---:|---:|---:|---|
| cooke | Cooke 三片 | 3 | 20° | 5 | [EP1425624B1](https://patents.google.com/patent/EP1425624B1/en)，Figure 9 附近的无相位编码处方 |
| petzval | Petzval | 4 | 7° | 1.4 | Laikin，*Lens Design*，第 4 版，p.75；[Optiland v0.6.3 转录](https://github.com/optiland/optiland/blob/v0.6.3/optiland/samples/objectives.py) |
| double_gauss | Double-Gauss 缩小物镜 | 6 | 17° | 5.6 | [US4863251A](https://patents.google.com/patent/US4863251A/en)，LENS SURFACE DATA TABLE；有限共轭，总长 550 mm |
| telephoto | 远摄 | 5 | 10° | 5.6 | Laikin，第 4 版，p.91；[Optiland v0.6.3 转录](https://github.com/optiland/optiland/blob/v0.6.3/optiland/samples/objectives.py) |
| reverse_telephoto | 反远摄 | 5 | 14.25°（推算） | 3.5 | [US3799655A](https://patents.google.com/patent/US3799655)，PDF 第 4 页、说明书栏 3 数值表 |
| cassegrain | 双反射加校正镜组 | 2 镜＋4 片 | 0.1951° | 10 | Laikin，第 4 版，p.198；[Optiland 转录](https://optiland.readthedocs.io/en/stable/gallery/reflective/Cassegrain.html) |

反远摄半视场由原文的 0.6 in 全对角像高和 30 mm 焦距估算，采用 atan(7.62/30) 后向下取到 14.25°；不是追迹结果。R4 是入瞳位置，不额外加入折射面；R8 是光阑，位于 R7 后 0.154 in。原文使用 e 线材料数据，未把 Ne/Ve 写成 nd/Vd。

Petzval、远摄的原始英寸尺寸按 25.4 转成 mm。远摄网页画廊与 v0.6.3 的 `Telephoto` 不是同一个处方，本库固定后者。Cassegrain 按示例数值作为 mm 起点，保留中心遮挡；缩放时由实际 EFL 定标。Cooke、Double-Gauss、反远摄的标称焦距分别为 50、50、30 mm；另外 3 条没有把未实测的焦距填成数值。

原玻璃牌号按原来源保留；没有安装相应目录时，程序记录材料未评估，不自动换成相近玻璃。Cooke 与 Double-Gauss 的 F/d/C 采样是明确登记的初筛假设，原专利未列波长采样；其有限共轭条件单独筛选。Cassegrain 示例只有 0.55 µm，不能凭单色示例认定宽波段性能。所有起点的机械余量都需按任务补充。

## 来源核验层级

- Cooke、Double-Gauss：用户本轮复核报告确认与专利原表逐面一致；保留为外部复核证据，不冒充本轮重新测量。
- Petzval、远摄、Cassegrain：仅核对了 Optiland 的转录及其书目页码，尚未核验 Laikin 原书页。条目的 `source.review.original_pages_checked` 为 false。
- 反远摄：本轮重新查看原 PDF 第 4 页、说明书栏 3，核对 R1/R2/R3/R5/R6/R7/R10/R11 共 8 个有限半径及 R8/R9 两个平面。乘 25.4 后与处方的最大十进制表示差为 4e−15 mm；逐项值与 PDF 摘要记录在 `entry.json` 的 `source.review`。这里只证明半径转录，不证明材料或像质。

## 格式与用法

沿用显微物镜库的 `entries/<id>/entry.json` 元数据与 `prescription.json` 处方布局。`metrics` 保存来源给出的标称量，`applicability` 保存选库范围，`source` 保存书目页码或专利号、URL 和转录说明，`notes` 保留假设。生成时先保留原处方几何并设置需求的波长、视场和 f/#，通过 Optiland 测参考 EFL，再缩放至需求焦距；生成记录保存实测参考焦距、比例及条目/处方摘要。

从脚本调用库函数，不增加独立 CLI：

```python
from saos import starts

def run(ctx):
    generated = starts.generate(
        ctx.job,
        {"efl_mm": "focal", "half_field_deg": "half_field",
         "f_number": "fno", "wavelengths_um": "waves"},
        short_seconds=30,
        prepare="explore/prepare.py",
    )
    return starts.screen(ctx, generated, workers=2)
```

映射的值是需求条目的 ID。焦距支持 mm/cm/m/in，半视场支持 deg/degree/°，波长支持 um/µm/nm，f/# 不带单位或用 `1`、`f/#`。条目必须给出具体 `value` 或 `equals`；只有范围时，映射可写 `{"id": "focal", "value": 50}`，程序检查该值是否在需求范围内。程序不从描述文字猜单位、全视场/半视场或焦距。目标波长排序去重，主波长取中间项（偶数时取较短的中间项）；这是生成起点的明确约定，模型确认前应核对。

`query` 按视场、f/#、波段及共轭筛选，并按相对示例范围的压力列最吃紧指标；这是可解释的选库启发式，不是像差优化结论。默认无限远共轭；`conjugate="finite"` 保持有限共轭条目的缩放后物距，使用前必须核对这是否符合任务的物距。不能匹配的需求保留排除理由。可显式设置 `allow_extrapolation=True` 做外推初筛：保留原范围，在候选与确认页列出每一项超范围条件，不宣称已适用，也不绕过材料适用范围检查；不同共轭仍排除。2026-10-21 在 verify 实例用可见光 F/d/C、f/4、20° 半视场实测：严格模式 0 个候选，6 个来源均带排除理由；外推模式 4 个候选（cooke、petzval、telephoto、cassegrain），reverse_telephoto 因 LF4 材料缺失未评估，double_gauss 因共轭、视场和 f 数排除。候选仍需同预算初筛，不承诺达标。新增处方仍按真实任务需求入库。

`generate` 必须提供存在的 `prepare` 脚本；起点处方没有评价函数，脚本须统一设置变量与评价函数。prepare 是必填关键字参数；省略参数由 Python 调用绑定报 TypeError，传空值在建立目录和启动后端前报 BAD_ARGS；脚本路径不存在报 FILE_NOT_FOUND；脚本未设置评价函数时，在候选测量前报配置错误，不把它当作计算异常。

`generate` 写入任务的 `explore/starts/NNN/`：处方、预判、`generated.json`、零优化 `screen.json` 和同预算 `short.json`。无候选时只写原因，不生成空扫描计划。`screen` 先完成所有候选的零优化扫描，再以相同预算扫描全部候选；取消后不启动第二段。单独调用两个计划也可，不作强制关卡。

`scale_prescription` 同时供自写处方使用：长度乘 K，角度、f/#、圆锥系数保持不变，N 阶非球面项乘 K^(1−N)，长度拾取偏移和光线高度求解同步缩放。当前支持单配置序列处方；未知参数量纲、混合或非序列物体、多配置显式拒绝，避免静默漏缩放。缩放数值中的字符串、布尔值、NaN 与非法容器返回 BAD_ARGS；平面半径或无穷远物距可用 inf。非球面表头名和 parN 共用阶数映射，例如偶次面的 par3 与 6th Order Term 都指 A6；同时提供两种写法会拒绝重复定义。

## 预判约定

`starts.predict` 调用 Optiland 的 `SeidelAberrations`、`ThirdOrderAberrations`、`ChromaticAberrations`，没有另写像差计算。表格保存一阶量、各面及合计 SI–SV、横向/纵向三阶像差及一阶色差、Petzval 半径/EFL、各面采样最大入射角。

- 近轴/三阶量沿用处方长度单位，SI–SV 不转换成 waves。三阶量采用主波长、边缘近轴光线与最大视场主光线。
- 原生关系为 SC=−TSC/u_image、AC=−TAC/u_image、PC=−TPC/u_image、TCC=3CC。Petzval 半径按 R=h_image²/(2ΣPC) 的纵向移焦符号换算；轴上场或零曲率不报告有限半径。
- 色差采用配置中的最短减最长波长；单波长时后端退回 F/C 参考，页面不将它描述为实际工作带宽。
- 入瞳位置相对首面，出瞳相对像面；光阑位置使用展开光轴坐标。总光路长取物理面到像面的间隔绝对值之和。
- 默认入射角采样为当前配置的每个视场、每个波长，3 环×8 方位加主光线，即每场每波长 25 条。失效数量单列；最大值只覆盖这些采样，不宣称是连续孔径最大值。
- 三阶表当前限同轴标准球面/圆锥面。附加非球面项、偏心等明确写出缺口，仍可保留其他部分。预判不阻断初筛或模型确认。
- OpticStudio SEID 符号与归一化实测对照：NOT RUN。不得据当前代码宣称两端一致。

上收前后对照与预先登记的数值容差记录见 Git 历史；当前版本的验收缺口见[整合计划](../../docs/plans/integration-workflow.md)。

## 静态审查修复后的输入与历史兼容

生成记录的未评估类别区分 library（读取、规范化、缩放库处方失败）、configuration（建模参数或能力配置）、material（材料数据）、computation（实际计算）；同时记录 phase、code 和原异常。库元数据本身无法查询时在生成前返回 BAD_ARGS，details.kind=library。prepare 编译或执行错误为配置错误，scan 返回 BAD_ARGS 与候选、脚本、原始错误；材料错误仍归 material，连接中断和 UI 阻塞继续上抛。

[Ansys Odd Asphere 帮助](https://ansyshelp.ansys.com/public/Views/Secured/Zemax/v26103/en/OpticStudio_User_Guide/OpticStudio_Help/topics/Odd_Asphere.html)列出参数 1–8 对应幂次 1–8；它不等于 2024 R1 界面列标题已核验。规范化保留未知表头供 ZOS 查列；Optiland 对未登记表头明确报能力缺口，缩放仍拒绝未知量纲。新输入重复别名报错；哈希校验后的旧快照只在内存副本合并同值别名，冲突值报 BAD_ARGS 并指出面号，需另建无歧义的处方副本，不改写存档。

无候选的生成仍保存并封存 generated.json，保留排除/未评估记录并递增编号，不改变该归档规则；零候选不启动后端。
