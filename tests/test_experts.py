"""Expert 里不需要 OpticStudio 的代数：线性投影排名、斜率缩放与玻璃候选窗口。
镜头事务、ZOS 接口与专利模型玻璃的配合在 test_zemax_experts.py 里用 tests/fixtures/cases 实测。
"""
from __future__ import annotations

import math
import numpy as np
import pytest

from saos.ctx.experts import GlassCandidate
from saos.ctx.experts.glass import candidate_window, special_glasses
from saos.ctx.experts.ranking import SurfaceSensitivity, rank, usable_fraction


def test_projection_score_is_vector_norm_not_squared_merit_gain():
    m = SurfaceSensitivity(1, np.array([[1.], [0.]]))
    r = rank([3.,4.], [m])
    assert r["selected"][0]["unconstrained_factor"] == pytest.approx(3/5)
    assert r["selected"][0]["step"] == pytest.approx([-3.])
    assert r["method"] == "linear_projection"


@pytest.mark.parametrize("before,after,budget,expected", [
    (0.,100.,25.,.5), (4.,20.,8.,.5), (4.,1.,0.,1.),
    (4.,9.,1.,0.), (4.,4.,4.,0.), (1.,1.,2.,1.),
])
def test_slope_scaling_branches(before,after,budget,expected):
    assert usable_fraction(before,after,budget) == pytest.approx(expected)


def test_cumulative_projection_and_last_tie_wins():
    models = [SurfaceSensitivity(1,np.array([[1.],[0.]])),
              SurfaceSensitivity(2,np.array([[1.],[0.]])),
              SurfaceSensitivity(3,np.array([[0.],[1.]]))]
    result = rank([2.,1.], models)
    assert result["order"] == [2,3,1]
    assert result["selected"][-1]["improvement_factor"] == 0.
    assert models[0].current_slope is None


def test_slope_limited_step_scales_the_projection_direction():
    m = SurfaceSensitivity(1,np.diag([10.,1.]),np.eye(2),np.zeros(2),.5)
    r = rank([-10.,-1.], [m])["selected"][0]
    assert r["step"] == pytest.approx([.5/math.sqrt(2)]*2)
    assert r["usable_fraction"] == pytest.approx(.5/math.sqrt(2))


def test_invalid_sensitivity_does_not_silently_rank():
    with pytest.raises(ValueError):
        rank([1.],[SurfaceSensitivity(1,np.ones((1,1)),budget=1.)])
    with pytest.raises(ValueError):
        rank([float("nan")],[SurfaceSensitivity(1,np.ones((1,1)))])


def materials():
    return [GlassCandidate("T","A",1.5,50.,.50), GlassCandidate("T","B",1.6,50.,.50),
            GlassCandidate("T","C",1.7,50.,.50), GlassCandidate("T","D",1.8,50.,.50),
            GlassCandidate("T","E",1.9,50.,.90)]


def test_glass_distance_is_n_versus_v_over_100_and_special_comes_first():
    gs = materials()
    assert special_glasses(gs) == {"E"}
    rows = candidate_window(gs,1.5,50.,7,special={"E"})
    assert rows[0][1].name == "E" and rows[0][0] < 0
    rows = candidate_window(gs,1.5,50.,1,feasibility_phase=True)
    assert [g.name for _,g in rows] == ["A","B"]
    assert rows[1][0] == pytest.approx(.01)


def test_glass_expert_writes_progress_after_every_candidate(saos_home, tmp_path):
    import json
    from saos import jobs
    from saos.backend.fake import FakeBackend, fake_surface
    from saos.ctx.experts import GlassCandidate, GlassExpert
    from saos.daemon import Daemon
    from saos.scripting import ScriptContext

    rows = [fake_surface(0, thickness=float("inf")),
            fake_surface(1, radius=40.0, thickness=5.0, material="N-BK7", semi_diameter=8.0, is_stop=True),
            fake_surface(2, radius=-40.0, thickness=40.0, semi_diameter=8.0),
            fake_surface(3, semi_diameter=2.0)]
    src = tmp_path / "exp.zmx"
    src.write_text(json.dumps({"title": "exp", "surfaces": rows}), encoding="utf-8")
    backend = FakeBackend()
    backend.merit_value = lambda: 1.0 if backend.surfaces()[1]["material"] == "N-BK7" else 0.5
    daemon = Daemon(backend, backend.start())
    job = jobs.create_job("expert-progress")
    assert daemon.handle({"op": "lens.import", "args": {"job": job.id, "src": str(src), "name": "a.zmx"}})["ok"]
    assert daemon.handle({"op": "lens.load", "args": {"job": job.id, "file": "a.zmx"}})["ok"]
    glasses = [GlassCandidate("FAKE", "N-BK7", 1.5168, 64.17, 0.535), GlassCandidate("FAKE", "SF2", 1.6477, 33.85, 0.580)]
    GlassExpert(ScriptContext(daemon, job, {}), [1], materials=glasses, neighbors=2, max_passes=1,
                coordinates=lambda _ctx, _s: (1.5168, 64.17, 0.535), element_power=lambda _ctx, _s: 0.02,
                optimizer=lambda c: c.backend.merit_value()).run()
    progress = job.read_progress()
    # 最后写的是第二遍（也是最后一个）候选：SF2 把 merit 降到 0.5
    assert progress["stage"] == "glass_expert"
    assert progress["data"] == {"surface": 1, "glass": "SF2", "candidate": 2, "of": 2, "accepted": True, "merit": 0.5, "best_merit": 0.5}
