# 显微物镜配方的数据

- `entries/<专利号>-e<n>/`：专利实施例的提取、处方、复现与准入记录，格式和准入流程见 [docs/recipes/microscope/library.md](../../docs/recipes/microscope/library.md)。
- `benchmarks.json`：优化基准案例与调参组 / 验证组划分。
- `calibration/`：原 zea 第 5 步停机参数校准的结果与证据，见 [docs/recipes/microscope/step5-calibration.md](../../docs/recipes/microscope/step5-calibration.md)。

代码入口是 `saos microscope library …`（`src/saos/recipes/microscope/library.py`），位置可用环境变量 `SAOS_LIBRARY` 改到别处。
