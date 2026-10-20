# 镜坯与结构材料

版本：2026-10-20。用途：反射镜、折反射系统和无热化设计（温度多重结构）需要镜坯、镜筒和隔圈材料的线膨胀系数（CTE），OpticStudio 自带的玻璃目录里没有这些材料（ZERODUR、ULE、CLEARCERAM-Z、SiC、铍、金属、工程塑料都查不到，2026-10-20 用脚本核实）。

数据在 [materials.json](materials.json)：每种材料有 CTE（带适用温度范围）、密度、杨氏模量、泊松比（有就给），以及出处和页码。单位：CTE 为 10⁻⁶/K，密度 g/cm³，模量 GPa，温度 °C。

## 出处等级

| 等级 | 含义 | 用法 |
|---|---|---|
| `manufacturer` | 厂家数据表或技术页面（别处托管的副本标 copy） | 可以作为交付设计的依据 |
| `distributor` | 经销商或第三方页面，常缺参考温度等条件 | 可以用于探索；交付前换成厂家数据，或在报告里写明 |
| `secondary` | 只有摘要或宣传页 | 只作线索 |

经销商和二手数据按用户决定入库并标明等级（2026-10-20）。

## 在 OpticStudio 里怎么用

- 反射镜：面材料写 `MIRROR`，镜坯的热膨胀填在该面的 TCE 列（单位 10⁻⁶/K）。
- 隔圈和镜筒：空气间隔面的 TCE 列填所用结构材料的 CTE，温度变化时 OpticStudio 按它伸缩厚度。
- 选值时按设计的工作温度范围取对应区间的 CTE；ZERODUR、ULE、CLEARCERAM-Z 的数值是 0 附近的担保区间，用区间端点做最坏情况，不要只取 0。

## 已知的缺口和出入

- 没找到：反应烧结 SiC（只有一篇期刊摘要）、快速凝固铝 RSA-6061 和 RSA-905（厂家需注册）、AlSi40、碳纤维准各向同性层合板、PYREX 7740 的 dn/dT。
- ULE 用的是 2006 年数据表副本；2016 年官方 PDF 下载返回 403。
- ZERODUR：TIE-37（2006）只列 0/1/2 级，现行页面另有 0 SPECIAL（±0.010）和 0 EXTREME（±0.007）。
- CLEARCERAM-Z HS 的杨氏模量：2016 版 92 GPa，2022 版图表 90 GPa。
- 铍 I-70-H：数据表 290 GPa，Materion 2024 年文章 303 GPa；采用数据表。
- Invar 36、Super Invar、黄铜、钛合金的数值从网页提取，没有保存原件。

原件（PDF、HTML）在调研时下载到 `%TEMP%\saos-materials\substrates\`，不进仓库；每个来源的 URL 和访问日期在 `materials.json` 的 `sources` 里。
