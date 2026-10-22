"""真实 Optiland 的任务流程（不用假后端，不启动 OpticStudio）：搭建 → 建模说明 → 用户认可 → 需求确认 → 记轮次 → 局部优化。
The task flow on real Optiland (no fake backend, no OpticStudio): build → model notes → approval → demand confirmation →
round → local optimisation."""

from __future__ import annotations

import json

import pytest
import yaml

from saos import demand, jobs
from saos.backend.optiland import OptilandBackend
from saos.daemon import Daemon
from saos.protocol import ErrorCode
from test_lens_snapshot_files import PRESCRIPTION

LINE = demand.QUESTION_LINE["zh"]
NOTES = {"surfaces": {"0": "object", "1": "lens front", "2": "lens back", "3": "image"}, "assumptions": ["test"],
         "first_order": ["f = 49.2 mm"]}
DEMAND = {"schema": 2, "constants": [{"id": "lens", "description": "the sample singlet", "value": "single.zmx", "source": "user"}],
          "variables": [{"id": "radii", "description": "both radii are free"}],
          "objectives": [{"id": "length", "description": "total track", "check": {"operand": {"type": "TOTR"}}, "origin": "user",
                          "must": {"max": 95.0}, "scale": 5.0, "source": "user"}],
          "structure": {"mode": "prescribed", "reason": "测试用的单片结构由用户指定"},
          "summary": f"用户要总长不超过 95 mm。{LINE}"}


def call(daemon, op, **args):
    return daemon.handle({"op": op, "args": args})


@pytest.fixture(scope="module")
def flow(tmp_path_factory):
    import os

    home = tmp_path_factory.mktemp("optiland-flow")
    previous = {k: os.environ.get(k) for k in ("SAOS_HOME", "SAOS_BACKEND", "SAOS_UI")}
    os.environ.update(SAOS_HOME=str(home), SAOS_BACKEND="optiland", SAOS_UI="0")
    try:
        backend = OptilandBackend()
        daemon = Daemon(backend, backend.start(), "standalone")
        job = jobs.create_job("flow")
        yield daemon, job
        daemon.optiland.close()
    finally:
        for k, v in previous.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_optiland_task_runs_from_build_to_a_recorded_round(flow):
    daemon, job = flow
    built = call(daemon, "lens.build", job=job.id, prescription=PRESCRIPTION, file="single.zmx")
    assert built["ok"], built
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(DEMAND, allow_unicode=True), encoding="utf-8")
    refused = call(daemon, "round.record", job=job.id)
    assert refused["error"]["code"] == ErrorCode.DEMAND_NOT_CONFIRMED
    demand.confirm(job.root, "确认，开始吧")
    refused = call(daemon, "round.record", job=job.id)
    assert refused["error"]["code"] == ErrorCode.MODEL_NOT_APPROVED
    folder = call(daemon, "lens.describe", job=job.id, notes=NOTES)["result"]["folder"]
    assert call(daemon, "lens.approve", job=job.id, model=folder, quote="模型可以")["ok"]
    recorded = call(daemon, "round.record", job=job.id)
    assert recorded["ok"], recorded
    record = recorded["result"]["record"]
    assert record["file"] == "rounds/r000.zmx" and record["stage"] == "exploration"
    assert (job.lenses_dir / "rounds" / "r000.optiland.json").is_file()
    evaluation = json.loads((job.root / record["evaluation"]).read_text(encoding="utf-8"))
    assert evaluation["environment"]["backend"] == "optiland" and evaluation["environment"]["precision"] == "float64"
    length = next(item for item in evaluation["items"] if item["id"] == "length")
    assert length["value"] == pytest.approx(100.0, abs=1e-9) and length["passed"] is False
    assert recorded["result"]["decision"]["status"] == "continue"
    first_shortfall = recorded["result"]["record"]["shortfall"]
    assert first_shortfall == pytest.approx(5.0 / 5.0)  # 超出 5 mm，标度 5 mm / 5 mm over the limit, scale 5 mm

    # 局部优化：只有一个变量（第 2 面厚度，60–95 mm），用波前合成的评价函数，记一轮 local
    # local optimisation: one variable (surface 2 thickness, 60–95 mm) and the wizard merit function, then one local round
    assert call(daemon, "opt.merit", job=job.id, action="wizard", settings={})["ok"]
    assert call(daemon, "opt.vary", job=job.id, surfaces=[2], param="thickness", min=60.0, max=95.0)["ok"]
    optimised = call(daemon, "opt.local", job=job.id, max_seconds=20)
    assert optimised["ok"], optimised
    # 同一起点的数值差分可下降约 58%；保守要求至少下降 50%，排除舍入误差冒充改善。
    assert optimised["result"]["final_merit"] < 0.5 * optimised["result"]["initial_merit"], optimised
    second = call(daemon, "round.record", job=job.id, tier="local")
    assert second["ok"], second
    assert second["result"]["record"]["shortfall"] < first_shortfall
