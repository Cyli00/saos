# bench：显微物镜优化基准与停机参数校准

原 zea 第 5 步的实验工具，已移出 saos 包，**不再推进**。规则和结论见 [docs/recipes/microscope/step5-calibration.md](../../docs/recipes/microscope/step5-calibration.md)，结果在 `library/microscope/calibration/`。

```bash
uv run python experiments/bench/run.py <split|plan|calibrate|run> …
uv run pytest experiments/bench
```
