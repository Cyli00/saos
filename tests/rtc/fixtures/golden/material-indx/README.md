# 材料层 INDX 对照（2026-10-28）

saos 的 `saos.rtc_golden` 采集，OpticStudio 24.1.0 standalone：

```bash
uv run python -m saos.rtc_golden --catalogs SCHOTT OHARA HOYA CDGM NHG MISC INFRARED BIREFRINGENT LZOS HIKARI MICRO_RESIST_TECHNOLOGY DOWSIL_MS --output <新目录>
```

- `indx.jsonl`：每行一个 (样品, 环境, 波长)，`indx` 是 OpticStudio 的 INDX，`rtc` 是当时 rtc 材料层的值；目录玻璃记有 `catalog`，回归按它解析（CALCITE 等同名牌号在多个目录里）。
- `summary.json`：按材料计算路径（`Medium.path`）汇总；saos 的 `src/saos/backend/rtc_admitted.json` 据此准入了 33 条路径。
- 环境：开修正时 -40/20/25/60 °C × 0/0.5/1 atm，关修正时只有 20 °C、1 atm（关修正时 OpticStudio 不接受温度、压力的写入）。
  每个环境都读回核对过，13 个环境全部与设定一致。
- 波长 0.36、0.4358343、0.4861327、0.55（主波长）、0.5875618、0.6562725、1.0、2.5 µm。目录玻璃只取不短于其 LD 下限的波长：
  有一个波长短于下限时，OpticStudio 不采用该材料，各波长 INDX 都是 1（ORMOCLAD、ZNO、LZ_LK1、J-BAF3 实测如此）；长于上限不受影响。
  LD 外的 130 个点只记 OpticStudio 的值。
- 样品玻璃（每个公式 × 20/25 °C 参考温度一种；20、25 °C 都没有的公式取其它参考温度）：

  | 样品 | 目录 | 公式 | 参考温度 |
  |---|---|---|---|
  | B270 | SCHOTT | 1 | 20 °C |
  | APL1 | OHARA | 1 | 25 °C |
  | F2 | SCHOTT | 2 | 20 °C |
  | BAF2 | INFRARED | 2 | 25 °C |
  | IRG102 | NHG | 3 | 20 °C |
  | BASF5 | MISC | 5 | 20 °C |
  | ORMOCLAD | MICRO_RESIST_TECHNOLOGY | 5 | 25 °C |
  | CALCITE | INFRARED | 6 | 20 °C |
  | BBO | BIREFRINGENT | 7 | 20 °C |
  | ZNO | BIREFRINGENT | 8 | 20 °C |
  | ALN | INFRARED | 9 | 20 °C |
  | QUARTZ | BIREFRINGENT | 11 | 20 °C |
  | LZ_LK1 | LZOS | 12 | 20 °C |
  | J-BAF3 | HIKARI | 13 | 23 °C |

  另有 4 组模型玻璃 (nd, Vd, dPgF)：(1.3, 40, 0)、(1.637, 57.3, −0.02)、(1.8, 20, 0.03)、(1.5168, 0, 0)。
- 没有覆盖：公式 4、10（本机 Glasscat 里没有这两种公式的玻璃）；公式 3 没有 25 °C 参考的玻璃；公式 13 的压力修正路径
  （样品参考温度 23 °C，采样温度里没有 23 °C）。
- 结果：LD 内 1,677 点，rtc 与 INDX 全部逐位相同。`tests/test_material_golden.py` 用它做回归（容差取准入标准 1e-8）。
