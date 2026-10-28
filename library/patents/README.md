# 专利镜头库

版本：2026-10-28。`seq/` 下是 2456 个 CODE V 序列文件（ORA 格式，`INI 'ORA'`、`LENS VERSION: 42`），每个是一份专利实施例。按标题分：日本专利 1999 个，美国 356 个，苏联 100 个，另有 1 个标题里没有国别。原文件不改动；`index.jsonl` 是由它们生成的检索索引。

这套整理文件的出处和再分发条款没有核实，专利数据本身是公开的。入库（git）前请先确认；目前两者都不在版本库里。专利可能仍然有效：用于商业产品时是否要做侵权排查，由用户决定，saos 只在出处里写明专利号。

## 文件里有什么，没有什么

文件里没有镜头类型标签，标题只写国别、专利号、日期，个别写了发明人。检索用的特征都是从处方算出来的（`saos.starts.seq.paraxial` / `elements`）：

- 用各介质的 nd 计算近轴量，长度单位与原文件相同。很多实施例按焦距 1 归一化，所以检索只用与尺度无关的量。
- 校验：最后一面带 `PIM` 的非无焦条目共 2322 个，比较算出的像距与文件里的像距，相对焦距的差中位数为 4.5e-5；1924 个（83%）在 1e-3 以内，2251 个（97%）在 1e-2 以内。差得大的主要是 40×～150× 的显微物镜：文件按参考波长求解，这里用 nd，高倍率会放大这个差。
- 2026-10-28 在 OpticStudio 2024 R1 里搭建了 9 个条目（单透镜、胶合、非球面、折反射、变焦两个位置、显微物镜、无焦），波长设为 d 线时 EFFL 与近轴值的相对差 ≤ 2e-6。其中 4 个与 OpticStudio 自带转换器的结果对照，EFFL 相同。

## 索引字段

每行一个条目（`schema: 1`）：

- `id`、`file`、`sha256`：原文件及其哈希。转换时哈希与索引不符就拒绝，要先重建索引。
- `title`，以及 `source` 中的 `country`、`number`、`rest`：从标题拆出来的国别、专利号和其余文字。日本专利号写法为 `年号_号码`，`rest` 里的日期未做解释。
- 系统数据：`wavelengths_nm`、`reference`、`aperture`、`field_type`（angle / object_height / paraxial_image_height）、`conjugate`（infinite / finite / afocal）。物距 ≥ 1e9 按无穷远处理。
- 结构：`surfaces`；`elements`（片数）；`groups`（空气隔开的组数）；`signs`（各片在空气中的光焦度符号，胶合组加括号，例如双高斯 `++-(-+)+`；有反射面时为 null）；`mirrors`；`aspheres`（非球面和二次曲面的个数）；`zoom`（变焦位置数）与 `zoom_ratio`。
- 材料：`materials.codes`（玻璃代码的个数）；`catalog` / `special` / `private` / `unknown` 列出非代码的材料名；`materials_to_map` 列出转换前必须由调用方指定的材料；`private_glasses` 是 PRV 私有玻璃的原始折射率表。
- `paraxial`：每个变焦位置一项。包括：
  - `efl`、`f_number`（=|EFL|/EPD）和 `half_field_deg`（角视场直接取值；像高视场在物在无穷远时用 atan(h/EFL) 换算）；
  - 物高、像高和有限共轭的 `magnification`；
  - 无穷远共轭时的 `ttl_over_efl` 与 `bfl_over_efl`；
  - `pim_residual`（上面的校验差）。
- `blockers`：不能转换的原因，`convertible` 为它是否为空。目前 7 条不能转换：3 条物面是曲面，2 条有含义不明的 `POS`，1 条全息面，1 条用像方 NA 作孔径。
- `tags`：由上述字段按固定规则导出，不是专利原文的分类：
  - `infinite`/`finite`/`afocal`、`zoom`、`aspheric`；
  - `catadioptric`（有反射面也有透镜）、`reflective`（只有反射面）；
  - `cemented`；`model_glass_only`（所有材料都是玻璃代码或反射面）；
  - `telephoto`（TTL/EFL < 1）、`retrofocus`（BFL/EFL > 1），这两项只针对无穷远共轭。

当前统计：可直接转换 2407 条；42 条要先指定材料；有近轴数据的 2406 条；无穷远共轭 1960、有限共轭 396、无焦 100；变焦 461；中位片数 7，最多 26。

## 用法

```python
from saos.starts import patents

found = patents.search(half_field_deg=23, f_number=2.0, wavelengths_um=[.486, .587, .656], elements=(None, 6), limit=10)
# 逐个看 found["candidates"] 的 signs、paraxial、tags；found["excluded"] 是各排除原因的条数
converted = patents.prescription("or01022")  # {"prescription": lens build 处方, "provenance": 出处}
```

- `search` 把需求与每个原例比较：原例的半视场不小于需求、f/# 不大于需求、波段覆盖需求，才算覆盖需求。覆盖的条目按"最吃紧的一项最接近需求"排序，再按片数少的优先。可以加的筛选条件：`conjugate`、`elements=(下限, 上限)`、`zoom`、`aspheric`、`reflective`、`ttl_over_efl_max`、`bfl_over_efl_min`、`signs`（正则，例如 `^\+-\+$` 为三片式）、`country`。`allow_extrapolation=True` 时，未覆盖需求的条目也列出来，排在后面，超出的项写在 `extrapolation` 里。
- 进入同预算初筛：`starts.generate(ctx.job, bindings, short_seconds=…, prepare=…, patents=["or01022", {"id": "or00123", "materials": {"CAFL_SPECIAL": "CAF2"}}])`。处理方式与通用起点库相同：设需求的波长、视场和 f/#，测参考焦距，缩放，预判，生成扫描计划。以下条目会进排除表，并写明原因：物高视场、无焦、没有近轴数据的条目，以及没有指定位置的变焦条目。

## 转换规则（`saos.starts.seq.to_prescription`）

只转换认识的命令；不认识的命令列在 `unrecognized` 里，并拒绝转换（`allow_unrecognized=True` 时跳过这些命令）。材料不会被替换：

| 原文件 | 处方 |
|---|---|
| 玻璃代码 `nnnnnn.vvvvvv` | 模型玻璃 nd = 1.nnnnnn，vd = vv.vvvv（与 OpticStudio 自带转换器相同） |
| `REFL` | `MIRROR`，反射后的厚度符号照抄（两边约定相同） |
| `NAME_SCHOTT` / `_HOYA` / `_OHARA` | 该厂目录的牌号 `NAME`；本机目录里没有时，按材料未评估处理 |
| `NAME_SPECIAL`、引号里的私有玻璃名 | 必须在 `materials={原名: saos 材料}` 里给出，否则拒绝 |
| `STO`、`PIM` | 光阑；近轴像距求解（像面近轴边缘光线高度为 0）。无焦条目不加求解 |
| `ASP` + `K`、`A`…`G` | EvenAspheric，conic 取 K，par2…par8 依次取 A…G（r⁴…r¹⁶）；H、J 不为 0 的拒绝转换 |
| `CON` + `K` | 二次曲面 |
| `SI` 的厚度 | 在像面前加一个虚设面表示离焦（与自带转换器相同） |
| `AFI f` | 无焦像空间。CODE V 用焦距 f 的理想透镜成像；这里没有加理想透镜 |
| `ZOO` | 只取一个位置（`or01401-p2`，或用 `position=`），得到该位置的定焦镜头。取用的是 THI（S0 为物距，S(n+1) 为离焦）、CUY、EPD/FNO/NAO 和视场 |

没有带过去的内容记在 `not_converted` 里：

- 渐晕系数：CODE V 与 OpticStudio 的定义不同，没有换算；
- 优化控制 `CCY`/`THC`/`KC`；
- `CIR`；
- `ZOO PIM`/`THC`/`CCY`/`RSL`；
- 非球面行里的 `IC Yes; CUF 0.0`：103 个非球面都写这一组值，含义没有核实，所以只认这一组。

整套变焦（多重结构）不在这里转换。需要时由 agent 按 `index.jsonl` 和原文件自行搭建。

## OpticStudio 自带的 CODE V 转换器

自带转换器在 `Documents\Zemax\ZOS-API\Extensions\CODE V Converter.exe`，可以用命令行 `-convert <文件或目录>` 调用。它能转 ZOO 多重结构，但 2026-10-28 实测发现两个问题，所以不作为默认路径：

- 找不到的材料（例如 `CAFL_SPECIAL`）会被换成 n = 1.5 的模型玻璃，只在 HTML 报告里写一句；
- PRV 私有玻璃会写成 `Documents\Zemax\Glasscat\<名字>.ZTG`。这是全局目录，不同专利里同名的 `K8` 折射率并不相同，后转换的会覆盖先转换的。

如果确实要用它转换整套变焦，要做到三点：

1. 把原文件复制到临时目录再转换，输出写在原文件旁边；
2. 转换前确认文件里没有 PRV，转换后检查报告里的 "could not be found"、"Not Supported / Unrecognized" 和 "Failed Commands"；
3. 用 `paraxial` 的各位置焦距核对结果。

## 重建索引

```bash
uv run python -c "from saos.starts import patents; print(patents.build_index())"
```

约 2.3 s。增删或修改 `seq/` 下的文件后要重建；转换时会核对哈希。
