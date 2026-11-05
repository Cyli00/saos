# 测试用光学案例

saos 自身测试里的光学场景只用这五个案例，覆盖序列、非序列、混合模式、孔径光阑和同轴反射。前三个都能在 1 秒内算完；`iris`、`reflective` 的耗时还没测。新测试复用它们，不另造镜头，也不取 `work/` 里的任务。

| 案例 | 结构 | 典型用途 |
|---|---|---|
| `sequential` | N-BK7/N-SF5 胶合双片，EPD 10 mm，视场 0/3/5°，F/d/C 三波长，最后厚度用边缘光线高度求解对焦 | 序列追迹、评价函数、优化、换料与非球面类工具 |
| `non_sequential` | 点光源（锥角 5°，20000 条 Sobol 光线）、N-BK7 双凸透镜、矩形探测器 | 非序列追迹、探测器读数、非序列处方 |
| `mixed` | N-BK7 单透镜后接非序列组（组内 5 mm N-BK7 平板），出口设在焦点，像面半口径 6 mm 作出口端口 | 混合模式处方、组内物体、出口端口 |
| `iris` | 三组四片（N-BK7 单片、N-SF5/N-BK7 胶合、N-BK7 单片），Float By Stop Size（面 6 是空气里的专用光阑，半口径 2.5 mm），视场 (0,0)、(0,5)、(0,10)、(6,8)°，面 1 圆孔 6 mm，面 7 矩形孔 2.6×2.2 mm（x 偏心 0.5），光线瞄准 off | Float By Stop Size、面孔径、渐晕码 |
| `reflective` | 抛物面主镜（R −800，EPD 80，光阑，中心遮挡半径 15 mm），后接 N-BK7 曼金镜（前表面折射、后表面在玻璃里反射、再从前表面折射出去），光线穿过主镜的孔到主镜之后的像面，视场 (0,0)、(0,0.2)、(0.1,0.1)°，光线瞄准 off | 反射面、负厚度、符号约定、中心遮挡、反射系统的一阶量与光程 |

每个案例有两份文件：`<案例>.json` 是 `saos lens build` 的处方，`<案例>.zmx` 是 saos 用它在 OpticStudio 24.1.0 里建出并保存的镜头。前三个案例的实测参考值在 `reference.json`。

`iris`、`reflective` 有一批变体，定义在 `tests/case_variants.py`，服务 [rtc-iris-mirror.md](../../../docs/plans/rtc-iris-mirror.md) 第 2.3 节列的各项验证。变体的 `.zmx` 文本和所有读数在 `truth.json`（一个变体一行），由 `tests/test_zemax_cases.py` 录入（`SAOS_RUN_ZEMAX=1 SAOS_CASE_RECORD=1`）。改了处方或变体就要重录。

用法：

- 不需要 OpticStudio 的测试读处方：`json.loads((Path(__file__).parent / "fixtures" / "cases" / "mixed.json").read_text(encoding="utf-8"))`，例如 `tests/test_prescription.py`；`iris`、`reflective` 及其变体的离线比对在 `tests/rtc/test_case_truth.py`。
- 真机测试（`SAOS_RUN_ZEMAX=1`）经 daemon 的 `lens.build` 重建，再取 `ScriptContext`，例如 `tests/test_zemax_experts.py` 的 `build()`；`test_cases_rebuild_to_their_reference_values` 核对前三个案例的重建结果与 `reference.json` 一致，`iris`、`reflective` 由 `tests/test_zemax_rtc.py` 的 `test_case_copy_matches_opticstudio_ray_by_ray` 与 OpticStudio 对照。
- 需要变体时在测试里改处方副本（例如把玻璃换成模型玻璃来模拟专利起点），不改这里的文件。

候选实现的验收在 `tests/rtc/test_iris_mirror_candidates.py`：R1/R3/R7 逐光线与逐面比对、R3 存盘往返、R5 自动半口径、R6 表外渐晕、R8 实验 OPDC。失败不代表生产已经放行；候选表由 `case_variants.py` 的 serves 字段选取。

实测用于检查实现一致性；主光线被挡的 OPD 检查报错语义，不将接口返回的占位数当作有效波前。孔径边界另有双路径回归，覆盖平方半径容差和椭圆遮挡含边界。

真值已于 2026-11-05 重录，结果见计划第 9 节，完整运行顺序见第 7.3 节；CODE V 基准 `tests/bench_codev.py` 只用这两个案例的副本，并由 OpticStudio 同设置复测终点，不产生准入真值。
