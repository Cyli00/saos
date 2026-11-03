"""rtc：浮点 64 位的序列光线追迹内核（复现 OpticStudio 复现范围内的功能）。

模块地图：
- :mod:`rtc.zmx` / :mod:`rtc.material`：输入与折射率（`rtc.materials` 保留兼容入口）
- :mod:`rtc.geometry`：面型矢高、梯度、相位
- :mod:`rtc.trace`：发射、瞄准、求交、折射/衍射、渐晕、光程
- :mod:`rtc.psf`：出瞳、OPD、FFT/Huygens PSF、Strehl/STRH
- :mod:`rtc.operands`：派生操作数
- :mod:`rtc.adtrace` / :mod:`rtc.optimize`：前向 AD 与 DLS
- :mod:`rtc.cli`：``uv run python -m rtc.cli sag|trace|psf`` 命令行

逐项差距见 ``docs/plans/rtc/GAP-ANALYSIS.md``。
"""

__version__ = "0.2.0"
