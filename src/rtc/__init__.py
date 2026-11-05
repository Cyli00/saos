"""rtc：浮点 64 位的序列光线追迹内核（复现 OpticStudio 复现范围内的功能）。

模块地图：
- :mod:`rtc.zmx` / :mod:`rtc.material`：输入与折射率（`rtc.materials` 保留兼容入口）
- :mod:`rtc.kernel`：numba 内核，矢高、求交、折射/反射/衍射、孔径、瞄准，带切向的单步只有这一份
- :mod:`rtc.geometry`：把面参数排成内核的参数行，近轴曲率
- :mod:`rtc.trace`：发射与瞄准约定、RayBlock、理想透镜相位
- :mod:`rtc.psf`：出瞳、OPD、FFT/Huygens PSF、Strehl/STRH
- :mod:`rtc.operands`：派生操作数
- :mod:`rtc.adtrace`：优化雅可比，给内核的单步播设计变量的切向种子
- :mod:`rtc.optimize`：最小二乘设计（残差和雅可比），带约束的生产优化器在 :mod:`saos.backend.rtc_optim`
- :mod:`rtc.cli`：``uv run python -m rtc.cli sag|trace|psf`` 命令行

实测事实与未定项见 ``docs/plans/rtc/SURFACE-PARAMS.md``。
"""

__version__ = "0.2.0"
