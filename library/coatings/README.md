# 膜层材料

版本：2026-10-20。用途：处方可以给面指定膜层（`coating_file` 加面上的 `coating`），算透过率和偏振时 OpticStudio 用膜层文件里各层材料的 n、k。随附的 `COATING.DAT` 只覆盖可见和近红外：中长波红外只有 ZnS（到 2.0 µm）和含钍的 ThF4，紫外的 MgF2、Al2O3 从 250 nm 起，保护银用的 Al2O3 只到 0.9 µm，增强铝用的 TiO2 只到 1.0 µm（2026-10-20 核实）。

## 怎么用

```bash
uv run python library/coatings/build.py
```

在 OpticStudio 的 Coatings 文件夹里生成 `SAOS_COATING.DAT`：随附 `COATING.DAT` 的全部内容在前，后面追加下表的材料（名字以 `SAOS_` 开头）。处方写 `"coating_file": "SAOS_COATING.DAT"`，原有膜层名照常可用。膜系（`COAT` 段）要用这些材料时，写在任务自己的膜层文件或说明里；核对过的公开层结构见 [designs.json](designs.json)。

脚本取每种材料 `window` 内的表点，虚部写成 −k（`COATING.DAT` 里吸收材料的虚部为负）。来源文件的 sha256 不符时拒绝生成。随附文件版本不同时只提示。

**每个材料最多 120 点。** 2026-10-20 在 OpticStudio 2024 R1 上用合成材料实测：一个 `MATE` 段只用前 119 点和最后一点，中间多出的点不报错地丢掉，两点之间按波长线性插值；写 120 点时全部读入，121 点时第 120 点被丢。所以原表超过 120 点时，脚本从原表里挑 120 点，点的值不改：先留两端，每次补上线性插值偏差 max(|Δn|, |Δk|)/λ 最大的点。14 种材料都超过 120 点，被丢点上的最大 |Δn|、|Δk| 写在各段段首注释里，运行时也会打印。同日复核：在 0.3 µm 单层加不透明背层的膜系上，对每个原表点比较 OpticStudio 算出的反射率和用原表值算的反射率，HfO2 电子束膜最大差 3.4e-3，ZnS 7.9e-4，其余不超过 3.7e-4。OpticStudio 在写出的 120 点之间的插值与线性插值一致到 1e-13。

## 材料

| 名字 | 材料与工艺 | 波段 µm | 来源 |
|---|---|---|---|
| `SAOS_ZNSE_BULK` | ZnSe 块体 | 0.5–14 | Querry 1987 |
| `SAOS_ZNS_BULK` | ZnS 块体 | 0.4–14 | Querry 1987 |
| `SAOS_BAF2_BULK` | BaF2 块体 | 0.22–14 | Querry 1987 |
| `SAOS_AL2O3_SPUT_IR` | Al2O3 反应溅射膜 | 1.5–14.3 | Kischkat 2012 |
| `SAOS_SIO2_SPUT_IR` | SiO2 反应溅射膜 | 1.5–14.3 | Kischkat 2012 |
| `SAOS_HFO2_SPUT_IR` | HfO2 磁控溅射膜 | 0.385–14 | Bright 2012 |
| `SAOS_TAC` | 非氢化四面体非晶碳 ta-C | 0.19–10.1 | Larruquert 2013 |
| `SAOS_MGF2_EB_UV` | MgF2 电子束蒸发膜 | 0.19–12 | Franta 2017 |
| `SAOS_AL2O3_EB_UV` | Al2O3 电子束蒸发膜 | 0.19–12 | Franta 2015 |
| `SAOS_HFO2_EB_UV` | HfO2 电子束蒸发膜 | 0.19–12 | Franta 2015 |
| `SAOS_SIO2_IAD_UV` | SiO2 离子辅助蒸发膜 | 0.25–1.25 | Gao 2013 |
| `SAOS_AG_RAKIC` | 银 | 0.248–12.4 | Rakić 1998 |
| `SAOS_AU_OLMON` | 金（蒸发膜） | 0.3–14 | Olmon 2012 |
| `SAOS_AL_RAKIC` | 铝 | 0.19–14 | Rakić 1998 |

每条的工艺、DOI 和注意事项在 [manifest.json](manifest.json)；原文件在 `sources/rii/`，来自 refractiveindex.info 数据库（CC0），2026-10-20 下载。

注意：折射率随沉积工艺变化，块体数据（Querry）不等于镀出的膜；选材时按工艺对得上的来源，对不上就在建模说明里写明。

## 没收进来的

- **Ge 薄膜**（Amotchkina 2020）：k 从 2.600 µm 到 15.000 µm 的 621 个点全等于 0.00498258306，而论文声明的表征范围只到 12 µm；作者的数据说明没写这段怎么来的，原文全文没取到（2026-10-20 核对）。这段 k 不能当测量值用，所以不收；n 也随之不收。
- **YbF3、LaF3 薄膜**：n 只有公式（要自己算表，属于光学计算），k 只在 9–14 µm 有表。
- **未找到**：氢化 DLC、YF3 和 BaF2 薄膜，以及紫外 LaF3、AlF3 的 n、k。
- **膜系**：对照原文核过的公开层结构在 [designs.json](designs.json)：US4057316 的 Ge、Si 基底红外增透（例 A、C、D），US5963365 的 193 nm 三层增透和两层对照例（光学厚度按专利给的 n 换算成物理厚度），METU 硕士论文（Üçer 2009）的 Ge 基底 8–12 µm 七层 ZnSe/PbTe 增透。每组都缺至少一种工艺对得上的层材料（Ge 薄膜、红外 Si、193 nm 的 LaF3、PbTe），所以没写成 `COAT` 段，缺什么记在各条的 `missing`。伊斯法罕理工大学的 ZnS 基底 8–12 µm 增透只核到摘要，Gainutdinov 2009 只有摘要，单列在 `abstract_only`。厂家的保护银、增强铝、保护金层结构没有公开。
