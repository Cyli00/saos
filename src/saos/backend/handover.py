"""一个后台进程里 rtc 与 OpticStudio 交接同一份镜头时的逐值比对（ADR-0018）。
Value-by-value comparison when rtc and OpticStudio hand the same lens over inside one daemon (ADR-0018).

两边各自加载同一个 .zmx 之后比较：OpticStudio 的 describe_model(None)（参数、各面各波长 INDX、环境、视场、波长）、
半口径（两边都会自动重算），以及每个视场、每个波长的主光线和 5×5 光瞳光线，容差同 ctx.fast()（ADR-0014）。
不一致不拒绝交接：差异记在 rtc 镜头上，进入 provenance 的 unverified，由交付前的复测兜底（用户 2026-10-30 决定）。
Both sides load the same .zmx, then compare OpticStudio's describe_model(None), the semi-diameters (both recompute
automatic ones) and the chief ray plus a 5×5 pupil of every field and wavelength, with ctx.fast()'s tolerances.
Differences do not stop the handover: they are kept on the rtc lens and listed as unverified in its provenance.
"""

from __future__ import annotations

from typing import Any

from saos import fast
from saos.kit import pupil


def compare(rtc, zos) -> dict[str, Any]:
    """rtc 与 OpticStudio 当前持有同一份镜头时比较两边，结果同时记在 rtc.handover 上。
    Compare both sides while they hold the same lens; the result is also kept on rtc.handover."""
    mismatches = rtc.reconcile(zos.describe_model(None))
    # 物面半口径不比：rtc 不重算它（保留文件里的值，建镜头时为 0），OpticStudio 在物距无穷时给 inf；两边追迹都不用它，
    # 是否影响光线由下面的光线比对判断（2026-10-30 真实 OpticStudio：0 对 inf，126 条光线位置差 ≤ 1.6e-14 mm）
    # the object surface's semi-diameter is skipped: rtc keeps the file's value (0 when built) while OpticStudio gives
    # inf at an infinite object distance; neither traces with it, and the ray comparison below judges any effect
    for mine, ref in list(zip(rtc.surfaces(), zos.surfaces()))[1:]:
        a, b = float(mine["semi_diameter"]), float(ref["semi_diameter"])
        if abs(a - b) > fast.POSITION_TOL:
            mismatches.append(f"surface {mine['index']} semi_diameter: OpticStudio {b}, rtc {a}")
    waves = range(1, rtc.wavelength_count() + 1)
    rows = [(w, hx, hy, px, py) for hx, hy in fast.field_points(rtc.lens) for w in waves for px, py in [(0.0, 0.0), *pupil(5)]]
    image = len(rtc.surfaces()) - 1
    rays = fast.agreement(rtc.trace_batch(rows, image), zos.trace_batch(rows, image))
    rays_agree = fast.within_tolerance(rays)
    rtc.handover = {"agrees": not mismatches and rays_agree, "mismatches": mismatches, "rays_agree": rays_agree, "rays": rays}
    return rtc.handover
