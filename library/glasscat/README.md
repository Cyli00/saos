# 在产玻璃目录

由厂家最新的 Zemax 玻璃目录生成只含在产牌号（Standard、Preferred）的目录，供 `saos opt vary --param material --mode substitute` 使用（不给 `--catalog` 时默认 `SAOS_CDGM_INPROD`）。Hammer 和全局搜索的玻璃替换不看目录状态，在 SCHOTT 全目录里 12 次替换有 9 次换成停产牌号（2026-10-18 实测），ZOS-API 又只能按目录限定替换范围，所以从源头把停产牌号去掉。

| 目录 | 来源 | 牌号 |
|---|---|---|
| `SAOS_CDGM_INPROD` | 成都光明 `CDGM_202609.AGF`（CC Updated Sep. 2026） | 315 / 352：Preferred 170、Standard 145，去掉 Obsolete 37 |
| `SAOS_NHG_INPROD` | 湖北新华光 `NHG_202608.AGF`（CC 20260827） | 260 / 263，去掉 Obsolete 3 |

两家有 88 个同名牌号，其中 8 个光学参数不同（H-ZLAF71 的 nd：CDGM 1.850、NHG 1.901），所以按厂家分开，不合并。替换求解每个面只能指定一个目录，不同的面可以用不同厂家。

厂家文件不进仓库，装在 OpticStudio 的 Glasscat 文件夹（`Documents\Zemax\Glasscat`）里，下载地址、版本和 SHA-256 记在 [manifest.json](manifest.json)。换新版时：把新文件装进 Glasscat，更新 manifest 里的 `file`、`edition`、`sha256`，再运行

```bash
uv run python library/glasscat/inprod.py
```

已知的缺口（2026-10-19）：

- 新华光 2026-08 目录比 2024-08 版少了 347 个牌号（S- 系列、L- 低熔点模压系列、含铅的 F/ZF/QF、IRG 红外），是删除而不是标为停产；其中 L- 系列官网另有低熔点玻璃资料，是否仍在产没有核实。
- 湖北戈碧迦（OpticStudio 自带的 GBJ 目录，2022-08，111 种，全部标为 Standard）没有找到官网或公开的 Zemax 数据，看不出哪些在产，未收入。
- 其他国产厂家只查到名单，没有可核实的牌号目录。
