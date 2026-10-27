# 用 ZEBASE 选起点：设计草案

状态：草案（2026-10-27），待用户确认第 5 节的条款问题后再实现。目标是补上建模、起型阶段起点来源太少的短板：
通用起点库 `library/starts` 只有 6 条，显微物镜库 25 条。

## 1. 已确认的事实（本机只读查看，2026-10-27）

- OpticStudio 2024 R1 自带 `Documents\Zemax\Design Templates\ZebaseDb.db`（SQLite，28.9 MB，文件日期 2025-05-01），收录 642 个设计。
- 表 `DesignParametersModel` 是明文规格，每个设计一行，经 `OpticalDesignItemModelId` 与 `OpticalDesigns.Name` 对应。字段包括：
  - 波长：`MinWavelength`、`MaxWavelength`、`PrimaryWavelength`，单位 µm；
  - 视场：`FieldType`（取值 0/1/2 的设计分别有 537/88/17 个，含义未核实）、`MaxField`、`NumberOfFields`；
  - 一阶量：`EffectiveFocalLength`、`WorkingFNumber`、`EntrancePupilDiameter`、`TotalTrackLength`、`ObjectDistance`。±1e10 表示无穷远或无焦，536 个设计的物在无穷远；
  - 结构：`NumberOfElements`（中位数 4，最多 25）、`Reflective`（含反射面的 143 个）；
  - 像质：各项 Seidel 像差（W040…W311）、轴上/中间/全视场的光斑与波前、MTF@30/50；
  - 出处：`Notes` 的首行写着 `Source: …`，按文字粗分：专利 223、书籍 186、Zemax 自己的设计 138、期刊等 45、未写 50。
- 表 `OpticalDesignFileDataModel` 里的镜头文件（`ZmxFileData`）是加密的，并带访问控制字段（`UserZmxAccess`）。**本方案不读取、不解密这张表。**
- 公开 ZOS-API（2024 R1）里没有打开 Design Templates 或 ZEBASE 的接口（反射检查过 ZOSAPI 全部公开类型）；`ILensCatalogs` 是供应商的现货镜头目录，与此无关。打开设计只能在 OpticStudio 界面里做。
- 已有的功能足以接住后续步骤：
  - `saos scan` 的 candidates 计划直接接收任务里的 `.zmx`（`lens`），可以和起点库候选按同样预算初筛；
  - 公开 ZOS-API 有缩放工具 `Tools.OpenScale`（`IScale.ScaleFactor`），可按需求焦距缩放原生镜头。

## 2. 流程

1. **检索（新代码，只读明文规格表）**：`saos.starts.zebase.query(bindings, …)` 沿用 `starts.generate` 的需求绑定（焦距、半视场、f/#、波长），并增加片数上限、是否允许反射面、共轭这几个筛选条件。返回每个候选的名称、出处、规格，以及按 `starts.query` 同样的"相对示例范围的压力"排出的次序和排除理由。结果写入 `explore/starts/zebase-NNN/query.json`，只含名称与规格，不含镜头数据。
2. **取镜头（人工，界面操作）**：agent 用 `ui notify` 列出要的设计名，由用户在 OpticStudio 的 Design Templates 中打开，另存到任务的 `lenses/zebase/<名称>.zmx`。agent 不自动操作这个界面。
3. **入库到任务**：`lens import` 后读一阶量，与检索时的规格核对（焦距、f/#、视场、波长）；不一致说明另存的不是同一个设计，或者字段含义理解错了，报错，不继续。
4. **缩放与准备**：用 `Tools.OpenScale` 按需求焦距缩放；视场、波长和 f/# 按需求设置，评价函数和变量由 `prepare` 脚本统一设定，与 `starts.generate` 的约定相同。
5. **初筛**：与起点库、自写处方的候选放进同一个 candidates 计划，零优化一遍，再用同样的短预算跑一遍。
6. **出处记录**：模型说明与 `sources/materials.yaml` 写明"ZEBASE <名称>（数据库文件哈希、日期），原始出处 <Notes 的 Source 行>"。原始出处是专利时，报告里注明交付镜头的起点来自该专利。

## 3. 不做的事

- 不读取、不解密、不批量导出 `OpticalDesignFileDataModel`。
- 数据库、检索结果之外的任何内容都不进仓库。`query.json` 只存在于 `work/` 下的任务目录（不入库）。
- 不把 ZEBASE 规格表里的像质数字（光斑、MTF）当作证据：那是 Zemax 在原始条件下算出的，缩放并改条件后要在本任务里重新实测。

## 4. 实现要点与测试

- 数据库路径默认为 `%USERPROFILE%\Documents\Zemax\Design Templates\ZebaseDb.db`，可用 `SAOS_ZEBASE_DB` 覆盖。以 `mode=ro` 打开；只查询 `OpticalDesigns` 和 `DesignParametersModel` 两张表；文件缺失或表结构不符时返回 `FILE_NOT_FOUND` / `BACKEND_UNSUPPORTED`，并写明原因。
- `FieldType` 的含义（猜测是角度、物高、像高）要先找 2–3 个设计在界面里打开对照，确认后才能用于筛选；确认之前按"视场类型未知"排除，不猜。
- 单元测试用合成的小数据库（结构相同、内容是虚构的），不用真实数据；另有一个需要本机数据库的真实测试，默认跳过。
- 技能文档（`references/model.md`）在"Choose a starting form"一节加一段：先查起点库，再查 ZEBASE；ZEBASE 的设计要用户在界面里打开另存。

## 5. 需要用户确认的条款问题（实现前）

我无法确认 Ansys 的许可条款，以下几点需要你查看许可协议，或向 Ansys 支持确认：

1. 用程序只读访问 `ZebaseDb.db` 的明文规格表（不碰加密的镜头数据），用于在本机检索设计，是否允许？还是 Design Templates 只能经界面使用？
2. 在界面里打开设计、另存为 `.zmx` 放进任务目录，用于内部的设计工作，是否允许？
3. 以 ZEBASE 设计为起点做出的镜头，交付给第三方时有没有使用限制？
4. 不属于许可协议、但需要你决定的一点：原始出处是专利的设计，专利可能仍然有效。用于商业产品时是否需要做侵权排查，这不是 saos 能判断的；saos 只负责在报告里写清楚出处。
