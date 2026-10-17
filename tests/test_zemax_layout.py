"""流程图窗口布局数据在真实 OpticStudio 2024 R1 上的回归。默认跳过：SAOS_RUN_ZEMAX=1 uv run pytest tests/test_zemax_layout.py -s

GRIN 介质里的光线是弯的：布局光线在 GRIN 面和下一面之间带中间点，且不在两端点连线上；导出不改动主系统。
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import pytest

from saos import jobs
from saos.daemon import Daemon

pytestmark = [
    pytest.mark.zemax,
    pytest.mark.skipif(os.environ.get("SAOS_RUN_ZEMAX") != "1", reason="需要真实 OpticStudio，设置 SAOS_RUN_ZEMAX=1"),
]

SAMPLE = Path.home() / "Documents/Zemax/Samples/Sequential/Objectives/Cooke 40 degree field.zmx"


@pytest.fixture
def daemon(saos_home, monkeypatch):
    monkeypatch.setenv("SAOS_BACKEND", "zos")
    from saos.backend.zos import ZosBackend

    backend = ZosBackend("standalone")
    d = Daemon(backend, backend.start(), "standalone")
    yield d
    d.pool.close()
    backend.close()


def test_rays_bend_inside_a_grin_lens_in_the_live_view(daemon, monkeypatch):
    from saos.backend.zos import _load_zosapi

    job = jobs.create_job("grin")
    assert daemon.handle({"op": "lens.import", "args": {"job": job.id, "src": str(SAMPLE), "name": "cooke.zmx"}})["ok"]
    assert daemon.handle({"op": "lens.load", "args": {"job": job.id, "file": "cooke.zmx"}})["ok"]
    backend = daemon.backend
    # 第一片改成 Gradient 1（n = n0 + nr2·r²）；nr2 = -0.003 时口径边缘（r 9.5 mm）n 仍为 1.35，光线在 3.26 mm 内明显弯曲
    row = backend.system.LDE.GetSurfaceAt(1)
    row.ChangeType(row.GetSurfaceTypeSettings(_load_zosapi(backend.root).Editors.LDE.SurfaceType.Gradient1))
    assert "Gradient" in str(row.TypeName)
    thickness = float(row.Thickness)
    for param, value in (("par2", 1.62), ("par3", -0.003)):
        assert daemon.handle({"op": "lens.set", "args": {"job": job.id, "surface": 1, "param": param, "value": value}})["ok"]
    view = json.loads((job.root / jobs.VIEW_FILE).read_text(encoding="utf-8"))
    assert view["ok"] and not view.get("errors", {}).get("grin"), view.get("errors")
    assert float(backend.system.LDE.GetSurfaceAt(1).Thickness) == thickness  # 主系统不动 / the main system is untouched
    # 不分段再导出一次作对照：同一条光线多出来的点就是 GRIN 里的中间点 / a reference export without slices
    from saos.backend import zos

    monkeypatch.setattr(zos, "GRIN_SLICES", 1)
    plain = backend.viz_data(None, full=False)["layout"]["rays"]
    bent, checked = 0.0, 0
    for ray, ref in zip(view["layout"]["rays"], plain):
        if ray["px"] != 0 or abs(ray["py"]) != 1:
            continue
        pts, extra = ray["points"], len(ray["points"]) - len(ref["points"])
        assert extra >= 5, extra
        checked += 1
        # 物在无穷远：第一点是第 1 面的交点，接着是 GRIN 里的中间点，然后是第 2 面 / object at infinity: surface 1, GRIN points, surface 2
        a, inner, b = pts[0], pts[1:1 + extra], pts[1 + extra]
        assert a == ref["points"][0] and b == ref["points"][1]
        assert all(p[2] < q[2] for p, q in zip([a, *inner], [*inner, b]))  # 沿光轴依次排列 / in order along the axis
        for p in inner:
            t = (p[2] - a[2]) / (b[2] - a[2])
            bent = max(bent, math.hypot(p[0] - (a[0] + t * (b[0] - a[0])), p[1] - (a[1] + t * (b[1] - a[1]))))
    print(f"GRIN: {checked} rays checked, largest offset from the straight chord {bent:.4f} mm")
    assert checked >= 2 and bent > 0.01, (checked, bent)
