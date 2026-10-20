# 非序列处方新增项实测

目的：核对 saos 非序列处方的面属性、Scatter To 和要文件的物体类型在 OpticStudio 2024 R1.00 上是否按写的生效，并记录非序列功能的覆盖范围。

状态：一次性实验，不属于 saos 包。结论和覆盖表在本目录的 `findings.md`。

用法：

```
uv run python experiments/nsc_probe/probe.py <输出 JSON> [探测名 ...]
```

只开一个 standalone 实例（占一个许可证）。探测名见 `probe.py` 的 `main`。
