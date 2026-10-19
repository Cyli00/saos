# ZOS-API 未用接口实测

目的：核对 ZOS-API 2024 R1 文档里 saos 尚未使用、但与现有需求相关的接口，在本机 OpticStudio 2024 R1.00 上实际行为如何，作为是否改造 `src/saos` 的依据。

状态：一次性实验，不属于 saos 包。结论写进本目录的 `findings.md`。

用法：

```
uv run python experiments/zosapi_probe/probe.py <输出目录> [探测名 ...]
```

只开一个 standalone 实例（占一个许可证）。样例镜头（Cooke 40 degree field、Double Gauss 28 degree field）先复制到输出目录再加载，全局优化写出的文件也落在那里。探测名见 `probe.py` 的 `ALL`。
