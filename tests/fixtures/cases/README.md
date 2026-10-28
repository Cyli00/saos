# 测试用光学案例

saos 自身测试里的光学场景只用这三个案例：都能在 1 秒内算完，覆盖序列、非序列和混合模式。新测试复用它们，不另造镜头，也不取 `work/` 里的任务。

| 案例 | 结构 | 典型用途 |
|---|---|---|
| `sequential` | N-BK7/N-SF5 胶合双片，EPD 10 mm，视场 0/3/5°，F/d/C 三波长，最后厚度用边缘光线高度求解对焦 | 序列追迹、评价函数、优化、换料与非球面类工具 |
| `non_sequential` | 点光源（锥角 5°，20000 条 Sobol 光线）、N-BK7 双凸透镜、矩形探测器 | 非序列追迹、探测器读数、非序列处方 |
| `mixed` | N-BK7 单透镜后接非序列组（组内 5 mm N-BK7 平板），出口设在焦点，像面半口径 6 mm 作出口端口 | 混合模式处方、组内物体、出口端口 |

每个案例有两份文件：`<案例>.json` 是 `saos lens build` 的处方，`<案例>.zmx` 是 saos 用它在 OpticStudio 24.1.0 里建出并保存的镜头。`reference.json` 记录在这些镜头上实测的参考值和测量方法；改处方后要重新实测并更新它。

用法：

- 不需要 OpticStudio 的测试读处方：`json.loads((Path(__file__).parent / "fixtures" / "cases" / "mixed.json").read_text(encoding="utf-8"))`，例如 `tests/test_prescription.py`。
- 真机测试（`SAOS_RUN_ZEMAX=1`）经 daemon 的 `lens.build` 重建，再取 `ScriptContext`，例如 `tests/test_zemax_experts.py` 的 `build()`；`test_cases_rebuild_to_their_reference_values` 核对重建结果与 `reference.json` 一致。
- 需要变体时在测试里改处方副本（例如把玻璃换成模型玻璃来模拟专利起点），不改这里的文件。
