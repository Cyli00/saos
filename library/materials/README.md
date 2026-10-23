# 光学胶与微透镜材料库

版本：2026-10-14。用途：为光纤侧视微光学设计提供有来源的材料参数，优先覆盖数百微米尺度的打印透镜。收录 22 种材料：12 种光学胶、8 种两光子聚合（2PP）打印候选树脂、2 种压印/模塑树脂。这是首批选材库，不是完整市场清单；已查到公开产品或材料资料，不代表已确认库存、报价或本地采购渠道。

**今后本项目调试以这里的材料 ID、数据版本和色散模型为基准。** 光纤任务的透镜按打印树脂选材，不再把任意 `nd=1.60, Vd=50` 当成已选定的实物材料。制造方的同工艺、同批次实测数据可替代当前基准，但必须新建有来源的记录，保留旧版本。

## 文件及使用入口

| 文件 | 内容 |
|---|---|
| [catalog.json](catalog.json) | 主数据：材料、固化状态、来源、折射率、公式、单位、波段和缺项 |
| [summary.csv](summary.csv) | 可用表格软件打开的汇总；488/532 nm 列明确为模型计算值 |
| [catalog.py](catalog.py) | 标准库实现的查询与导出工具，不需要连接 OpticStudio |
| [SAOS_MICRO_20261014.AGF](SAOS_MICRO_20261014.AGF) | 9 条具有色散模型的自定义 Zemax 材料；仅用于折射率与色散计算 |
| [sources/darus-6471/manifest.json](sources/darus-6471/manifest.json) | 研究数据原文件的下载地址、版本、许可及 SHA-256 |
| `catalog.json` 中的 `sources` | 厂家 TDS/PDS 的来源链接及下载时 SHA-256；PDF 已删除，不在项目内保存 |
| [光纤配方](../../.agents/skills/saos/references/recipes/fiber.md) | 光纤侧视探头的选材规则（Stage 2：modelling conventions） |

在仓库根目录用 Git Bash 运行：

```bash
uv run --no-sync python library/materials/catalog.py
uv run --no-sync python library/materials/catalog.py ipn162-darus2026 --wavelength 488 532
uv run --no-sync python library/materials/catalog.py noa-61 --wavelength 488 532
uv run --no-sync python library/materials/catalog.py --export
```

脚本也可以直接调用：

```python
from library.materials.catalog import load, refractive_index

materials = {m["id"]: m for m in load()["materials"]}
n532 = refractive_index(materials["ipn162-darus2026"], 532)
```

调用约定：波长输入为 **nm**，输出为无量纲折射率；缺少色散模型、非有限波长或越界查询会明确失败。单点材料不自动补齐色散，不提供默认 `Vd=50` 或“无色散”回退。

## 打印透镜：8 种可计算色散的候选

下表 488/532 nm 均由作者发布的 Sellmeier 系数计算，显示 6 位小数便于复核，**不表示 10⁻⁶ 的测量精度**。阿贝数为来源目录元数据，表内只显示两位小数。

| 材料 ID | 材料 | n(488 nm) | n(532 nm) | Vd |
|---|---|---:|---:|---:|
| ipxs-darus2026 | IPX-S | 1.518246 | 1.514475 | 48.58 |
| ips-darus2026 | IP-S | 1.518135 | 1.514484 | 50.34 |
| ipdip2-darus2026 | IP-Dip2 | 1.565524 | 1.559672 | 35.36 |
| ipvisio-darus2026 | IP-Visio | 1.518025 | 1.514516 | 51.95 |
| ipxclear-darus2026 | IPX-Clear | 1.552769 | 1.547841 | 39.97 |
| ipxq-darus2026 | IPX-Q | 1.517706 | 1.514184 | 51.16 |
| ipn162-darus2026 | IP-n162 | 1.639738 | 1.630483 | 25.05 |
| ormocomp-darus2026 | OrmoComp | 1.528854 | 1.525225 | 50.80 |

来源：[Siegle、Guimarães、Giessen，DaRUS-6471，版本 1.0](https://doi.org/10.18419/DARUS-6471)，2026-09-28 发布，CC BY 4.0。本库保留原始数据，并提取作者 AGF 系数；未自行重新拟合。数据集简介列出 7 种材料，实际下载的折射率表和 AGF 均包含 IP-n162，共 8 种。

这些数据来自 **UV 聚合块体试样**，不是当前打印机生产的镜片。库内没有足够信息确定本批试样的完整固化剂量、后烘条件、折射率测量温度及逐点不确定度，相应字段留空。2PP 曝光、后固化、显影、热处理和收缩对最终材料的影响，必须在实际工艺确定后核对。不能将 IP-Dip 与 IP-Dip2、IP-S 与 IPX-S 互换。

厂商资料作为产品和工艺依据：[Nanoscribe 打印树脂目录](https://www.nanoscribe.com/en/products/high-resolution-3d-2pp-printing-materials/)、[IP-n162 产品页](https://www.nanoscribe.com/en/products/2pp-printing-materials/photoresin-ip-n162/)。后者给出直径 400 µm 的打印菲涅尔透镜示例，支持该工艺与当前尺寸范围的相关性；不代表本项目面型已达到制造要求。

### 488/532 nm 的透射率不能忽略

下面是数据文件 `TransmissionData.txt` 中 **1 mm 试样**的值，按原文件定义记录，四舍五入到 0.1%。本库尚未确认其反射扣除及完整实验处理方法，不能直接作为任意厚度的内部透射率。

| 材料 | 488 nm / 1 mm | 532 nm / 1 mm |
|---|---:|---:|
| IP-S | 27.7% | 62.0% |
| IP-Dip2 | 60.8% | 77.8% |
| IP-n162 | 40.5% | 60.4% |
| IP-Visio | 93.6% | 95.9% |
| IPX-Clear | 98.2% | 98.6% |
| IPX-Q | 97.8% | 98.9% |
| OrmoComp | 99.6% | 99.6% |

因此，初筛应同时比较光焦度、色散、透射和荧光。IP-n162 的折射率较高，但不能只根据这一项选材。也不能把上述试样的透过率直接当成数百微米打印件的损耗，更不能据此断言某材料不适用。

原始数据同时含 `Luminescence532nmData.txt`、`LuminescenceUVData.txt`、消光系数和透射谱。荧光强度是原实验的 counts/s/mW，不是量子产率；未收集到当前 488 nm 激发条件下的同工艺定量荧光背景。

## 光学胶：固化值与液体值分开

下表采用原厂 TDS/PDS。除另有说明，这些单点固化折射率**没有明确波长**，不能自动当成 589 nm 或 nd，更不能用于精确计算 n(488)、n(532)。固化波段也不等于工作透射波段。黏度列为液体、25°C；Dymax 行未录入黏度。

| 材料 ID | 固化折射率 | 液体折射率 | 黏度 cP | 色散数据 | 备注 |
|---|---:|---:|---:|---|---|
| noa-61 | 1.56 | 1.52 | 300 | 厂商公式，25°C | 可做双色仿真基准 |
| noa-63 | 1.56 | 1.52 | 2500 | 未取得 | 厂商称低荧光；没有定量背景谱 |
| noa-65 | 1.52 | 1.49 | 900–1000 | 未取得 | 柔性、低应力 |
| noa-68 | 1.54 | 1.51 | 5000 | 未取得 | 塑料/玻璃粘合候选 |
| noa-81 | 1.56 | 1.52 | 300 | 未取得 | 快速 UV 固化 |
| noa-84 | 1.476 | 1.436 | 40 | 未取得 | 塑料/玻璃粘合，UV/可见光固化 |
| noa-1375 | 1.43 | 1.38 | 45–60 | 未取得 | 按当前 TDS；型号不等于折射率 |
| noa-147 | 1.474 | 1.436 | 200 | 未取得 | 低折射率候选 |
| noa-148 | 1.474 | 1.441 | 300 | 未取得 | 当前 TDS 并非 1.48；采用时核对批次 |
| noa-160 | 1.60 | 1.58 | 2000 | 未取得 | 高折射率胶 |
| noa-170 | 1.705 | 1.68 | 4800 | 未取得 | 刚硬且脆的高折射率胶 |
| dymax-op32 | 1.51，20°C | 未记录 | — | 未取得 | 低应力柔性胶，线收缩 2.3% |

Norland 原始链接逐项保存在 `catalog.json`；统一入口为 [Norland TDS 目录](https://norlandproducts.com/resources/)。Dymax 来源为 [厂家 OP-32 PDS](https://dymax.com/content/download/4396/file_archived/OP-32%20PDS.pdf)，下载使用文件中标明厂商的经销商镜像，地址保存在主数据。12 份 PDF 在提取数据后已按用户要求删除；`download_sha256` 仅记录当时下载版本的校验值。

NOA 61 的公式为 `n = 1.5375 + 8290.45/λ² − 2.11046×10⁸/λ⁴`，λ 单位 nm，温度 25°C。库允许的计算范围为 435.8–1500 nm，位于厂商色散图展示范围内，属于本库的使用限制，不是材料性能保证。计算得到 n(488)=**1.568591**、n(532)=**1.564158**；不要把单点典型值 1.56 当作两个波长共同的折射率。

胶层最终仍需核对固化后的色散、光谱、收缩、热稳定性及与具体打印树脂的粘接/溶胀相容性。某胶对玻璃粘接良好，不能推出它对所有光学树脂同样适用。资料未披露的字段保持空值。

### 本批没有作为已知固化值收录的胶

EPO-TEK 301、301-2、353ND 的一些资料混用液态、固态或未注明状态的折射率。[厂家 Tech Tip 18](https://www.epotek.com/docs/en/Related/Tech%20Tip%2018%20Understanding%20Optical%20Properties%20of%20Epoxy%20Applications.pdf) 明确介绍液体折射率测量及固化后的变化。本库没有把其通用变化量当作每个牌号的实测修正，也没有把 301-2 常见的 1.5318 直接录成固化折射率。获得明确固化条件的数据后再补录。

## 另存的微透镜复制材料

| 材料 | 固化 n(589 nm) | 厂商阿贝数 | 工艺 |
|---|---:|---:|---|
| OrmoClear | 1.555 | 34 | UV 压印/模塑 |
| OrmoClear30 | 1.561 | 34 | UV 压印/模塑 |

来源：[micro resist technology](https://microresist.de/en/produkt/ormoclear-series/)。这两条只有单点和阿贝数，未作为当前双色仿真的精确色散材料导出；也没有将它们默认归入已验证的 2PP 打印树脂。

常规玻璃另用 OpticStudio 的 SCHOTT、OHARA 等厂家目录，并记录所用目录版本；N-BK7 可继续作为棱镜候选。**常规玻璃目录不替代打印树脂目录。** 本次没有复制整套已有玻璃目录，避免产生另一份过期材料表。

## Zemax 导入与数据边界

将项目生成的 `SAOS_MICRO_20261014.AGF` 复制到 OpticStudio 配置的材料目录，再在系统材料目录中添加 `SAOS_MICRO_20261014`，按 `agf_name` 选材。具体位置取决于本机 OpticStudio 设置，操作参照 [Ansys 自定义材料目录说明](https://optics.ansys.com/hc/en-us/articles/42661706968211-How-to-add-new-materials-and-glasses-in-OpticStudio)。本次只在项目中生成文件，未修改本机安装目录或当前镜头。

导出规则：

1. 8 种树脂采用来源 AGF 的 Sellmeier 1 系数，波长单位 µm，按 `B1,C1,B2,C2,B3,C3` 排列。实际测量表为 405–1940 nm，因此项目 LD 收窄至该区间。
2. NOA 61 的 Cauchy 式经过单位换算和平方，精确转成 Schott 形式；未重新拟合。导出 Nd、Vd 由同一公式计算，只是 AGF 元数据。
3. 原始研究 AGF 保存在 sources 中，仅用于溯源。其 IT 数据出现约 0–100 的数值，原始透射表明确为百分比；内部透射的定义及换算尚待核实。**不要把原始 AGF 直接作为生产目录使用。** 项目导出文件不含 IT、热参数或机械参数，不能用于宣称吸收损耗、热漂移或系统绝对收光效率。
4. 只有单点数据的 13 条材料不导出，不把未知色散设成零。目录材料全部排除自动替换；后续选材须显式指定。
5. 当前只完成离线数值和格式检查；尚未在本机 OpticStudio 中实际导入验收。

格式依据：[Ansys AGF 格式](https://optics.ansys.com/hc/en-us/articles/46891219767827-Zemax-AGF-material-catalog-file-format)。

## 校核结果与已知差异

8 种树脂各有 24 个原始测量波长，共 192 个点。NOA 61 TDS 有 5 个折射率表值。将库模型与这些值比较：IP-Dip2 最大绝对残差约 **0.001131**，其余 8 个模型均小于 **0.0005**。这些是拟合残差，不能代替测量不确定度。

作者另提供逐 nm 的 Sellmeier 表。IPX-Clear 的 AGF 系数与该表在 405–1940 nm 内最大差约 **3.321×10⁻⁵**，其余 7 种树脂不超过 **1.1×10⁻⁸**。本库明确选择 AGF 系数为计算基准，保留原表，不对两者求平均。

```bash
uv run --no-sync pytest tests/test_material_catalog.py -q
```

检查覆盖保留的研究数据文件 SHA-256、公开测量点、作者独立色散表、AGF 公式与单位转换，以及缺数据/越界查询。已删除的 PDF 不参与本地文件校验。后续修改 `catalog.json` 后应重新导出 CSV/AGF 并运行此检查。更新系数时新建版本化材料名，避免旧镜头重新打开后静默采用不同材料。

2026-10-14 本次运行结果：**37 passed**。此结果只覆盖上述离线检查，不包含 OpticStudio 导入、真实打印件测量或胶合装配验证。

## 后续补充数据

- 实际打印设备、树脂牌号、曝光/显影/后固化条件，以及同工艺测试件的 n(488)、n(532)。
- 低折射率胶 NOA 84、147、148 等的固化后色散曲线、488/532 nm 透射及荧光背景。
- 实际胶层厚度分布、收缩与打印树脂相容性。厚胶层应作为独立介质建模。
- 对研究数据的内部透射定义和原始 AGF 的 IT 单位取得进一步依据后，再建立损耗模型。
