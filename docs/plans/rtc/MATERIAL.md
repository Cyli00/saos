# 材料层集成交接

状态：`unverified`。本轮按 saos 的 AGENTS.md 要求未运行测试、编译、golden 对照或 OpticStudio；
下面的原指令观测均为已有证据，不是本次集成的通过结果。

## 实现与边界

`rtc.material` 提供 Environment、Catalog/Model/None 材料描述、Medium 和 resolve。
OpticalSystem 为每面解析一次 Medium，通过 `index(波长, lens.environment)` 生成 n_after。
trace、adtrace、psf 的追迹实现未随材料层改动。

已迁入 13 类色散、空气绝对折射率、Schott TD、模型 nd/Vd/dPgF、AGF 字段和 ENVD。
FORMULAS 是算术分派表，不是应用级准入证明；材料数值仍只用于探索。

目录修正路径是：原始 λ 算 na 和 nr → λr=λ·na/nr → 目录相对 n(λr) → TD 绝对增量 →
`(n*nr+Δn)/na`。参考空气 nr 使用原始 λ，TD 使用 λr；不开 Adjust 时直接返回目录值。
模型玻璃不套用目录 TD：在单一环境中乘 `air(λ,T,1)/air(λ,T,P)`。
空气相对单一系统环境恒为 1。

AGF 保留 NM/GC/CD/ED/TD/LD/MD/OD/IT/BD 和目录 CC。CD 保留 10 槽；TD 缺省参考温度为
20 °C；IT/BD 按原生 float32 存储再提升为 float64，最多保留 100 项。读取不局限于 NM 后 7 行。
nd/Vd 在 LD 覆盖参考线时重算；原 NM 数值另存 catalog_nd/catalog_vd。
LD 按原生调用层 0.999 容差判断；超出时抛 MaterialUnsupported，不模拟错误后继续按空气追迹。

负折射率 ED 标志保留，但不接入追迹；ZTG/MIL、GRIN、双折射、NSC、局部逐面环境以及 TCE
几何缩放均未接入。TEMP/PRES 在 OpticalSystem 明确拒绝。

## 兼容与错误

- `rtc.materials.Catalog/Glass/catalog_dir` 保留兼容导入，算法没有第二份。
- `Glass.index(λ)` 返回参考环境的相对目录值，原文档所写“绝对折射率”有误。
- Surface 保留 glass/model_nd/model_vd 构造参数，material 作为统一读写视图；不是第二份可变状态。
  这避免打断现有调用方；新代码应读写 material。model_dpgf 是新增兼容字段。
- Lens.environment 对程序构造提供 20 °C、1 atm、关闭修正的参考状态；解析有 ENVD 的文件时以文件为准。
  saos 拒绝缺失 ENVD 的副本，不用这一缺省替代环境证据。
- model_glass_index 的原 3 参数调用仍可用，增加可选 dpgf；运行时不再查表。
- 材料不支持、数据范围和非有限计算统一抛 MaterialUnsupported(NotImplementedError)。

## 证据与待运行项

源目录：`E:/gitProjects/zemax-explore/zemax-material-reverse/2024-r1`。
目标 DLL SHA-256：`72ee0f121b1fb9bbfb6afe623f38d4e67e7819ad406cb381fbb47d9b5305671b`。
折射率核心 RVA 0x7b49d0，模型 0x7b2980，空气 0x7b5320。
材料运行时仅依赖已有 NumPy 和标准库，未引入仿真器、DLL、第三方光学实现或研究目录的运行时导入。
这不构成对恢复算法的开源授权判定，两个项目的对外许可证仍需项目方决定。

独立夹具包含 252 个模型、60 个空气、832 个合成目录温压、792 个真实目录系数和 108 个模型环境观测，
合计 2,044 个点。保存了来源文件哈希；预期值取 actual，不取候选公式的 expected。
原 21 组应用级模型玻璃 INDX 的 jobs/op.csv 均保留，并由新测试读取。
公式 4、10 尚无本机真实目录样品，已有的是原指令合成系数观测。

由用户安排运行：

```bash
cd /e/gitProjects/rtc
uv run pytest tests/test_material.py tests/test_zmx_materials.py -q
uv run pytest -q
uv run python tools/compare_golden.py
```

`tools/gen_golden.py` 同时修正了 INDX 参数：Harness3 的两个整数传面号、波长号，不能把面号
塞进第一个 double。历史 op.csv 未被覆盖；新采集结果应单独复核后替换。
saos 的应用级温压矩阵采集入口见其 `docs/plans/rtc-material-integration.md`。
