# 热分析选材

版本：2026-10-20。用途：温度多重结构（处方的 `environment.adjust_index` 加 `TEMP` 行）要每种透射材料都有热光系数（dn/dT，AGF 的 TD 行）和线膨胀系数（AGF 的 ED 行 TCE），缺一项时 OpticStudio 按 0 算，温度变化看起来没有影响。镜坯和镜筒材料见 [../mechanics/](../mechanics/README.md)。

## 选材规则

1. **先查本机目录有没有热数据**：TD 行全为 0 就是没有 dn/dT，ED 行 TCE 为 0 就是没有膨胀系数。2026-10-20 扫描结果：CDGM、SCHOTT、OHARA、NHG 主力目录大多齐全；HOYA、HIKARI、Sumita 分别有 40%、54%、49% 的牌号缺 dn/dT；KOPPGLASS、VITAL、TOPAS 全缺 dn/dT；BIREFRINGENT 62 个里 52 个缺。
2. **ZEON 塑料用带年份的条目**：`330R`、`E48R`、`F52R`、`K26R_*` 这些基础条目没有 dn/dT（或只有 D0），同一目录里的 `ZEONEX_330R_2016`、`ZEONEX_E48R_2017`、`ZEONEX_K22R&K26R_2016` 等条目有完整 TD 行。这些条目没注明来源，2017 组有几个 λtk 为负，没有核实物理上是否合理；用之前在温度端点上比较 2016 和 2017 两组，差别大时在建模说明里写明。
3. **红外硫系玻璃只用现行牌号**：SCHOTT_IRG 的 IRG22、24、25、26、27 和 UMICORE 的 GASIR1、GASIR5 是两家现行全部牌号，热数据齐全并与厂家一致（IRG27 的 CTE 除外，见下表）。INFRARED 目录里的 IRG2、3、7、9、11、15、IRGN6 是 Schott 已废止的旧牌号，没有热数据，不要用；GASIR2、AMTIR3 已停产。
4. **熔石英**：本机 HERAEUS 目录的 HERASIL、HOMOSIL、HOQ、INFRASIL、SUPRASIL 条目没有 dn/dT；同一文件里带后缀的 9 个条目（如 `SUPRASIL_1_2A`）有 TD 行，但没注明来源，色散系数也与 Heraeus 2018 数据表不一致，不当作有出处的数据。CORNING 目录的 `C79-80` 色散与 Corning 2014 表逐位一致，但没有 dn/dT。需要熔石英热数据时，用 [dndt.json](dndt.json) 的厂家表：在任务里用 OpticStudio 拟合 TD 系数，或在多重结构里直接给各温度的折射率，并把做法写进建模说明。表本身不在这里换算（用户决定，2026-10-20）。
5. **厂家值和本机目录不一致时两个都记**，在建模说明里写用的是哪个、为什么。

## 厂家值与本机目录不一致的项

| 材料 | 厂家 CTE（10⁻⁶/K） | 本机目录 | 出处 |
|---|---|---|---|
| ZEONEX K22R | 58（−50 至 100 °C） | 62（`ZEONEX_K22R&K26R_*`） | Zeon 手册 p.4 |
| Makrolon（聚碳酸酯） | 65（23–55 °C） | 67（MISC `POLYCARB`） | Covestro 手册 p.24 |
| PLEXIGLAS 8N（PMMA） | 80（0–50 °C） | 0（MISC `PMMA`，缺） | Evonik TDS 2018 p.2 |
| IRG27 | 22.5（20–100 °C） | 21.0（SCHOTT_IRG） | Schott 2018 数据表 p.1 |

## 缺口（2026-10-20 未找到）

- 塑料 dn/dT 的厂家数值：ZEON 全系列、TOPAS 全系列、PMMA（NASA NTRS 20170006479 有 632.8 nm 的值，但页面 403 未核实）、聚碳酸酯（Covestro 只有曲线图）。
- ZEON 330R、E48R、F52R、K26R 的厂家 CTE 和吸水率；TOPAS 各牌号的厂家 CTE。
- 晶体 KBr、KCl、AlN、BeO、GeO₂、CdSe、PbF₂、SrTiO₃、ZBLAN 的可核实 dn/dT；VITAL VIG 系列（目录注明要向厂家索取）。
- LightPath BD6 本机没有条目，厂家数值只来自搜索摘要。

原件下载在 `%TEMP%\saos-materials\thermal\` 和 `%TEMP%\saos-materials\substrates\`，不进仓库；URL 和访问日期在 `dndt.json` 的 `sources` 里。
