"""基准实验自己的轮次规则：停机判断、验收配置冻结、observe 轮次（原显微物镜配方的 saos microscope round）。"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from rounds import check_frozen, decide, freeze, significant
from saos import jobs
from saos.backend.fake import FakeBackend, fake_surface
from saos.daemon import Daemon
from saos.protocol import ErrorCode, SaosError
from saos.recipes.microscope import ops
from saos.recipes.microscope.acceptance import acceptance_config, config_hash
from saos.recipes.microscope.spec import load_spec

INF = math.inf


def test_significant_rules():
    assert significant(INF, 3.0) is True
    assert significant(1.0, INF) is False
    assert significant(1.0, 0.99) is True
    assert significant(1.0, 0.995) is False


def test_pass_and_continue():
    assert decide([2.0])["status"] == "continue"
    assert decide([2.0, 1.0, 0.0])["status"] == "pass"
    assert decide([2.0, 1.0])["status"] == "continue"


def test_stagnation_after_three_flat_rounds():
    assert decide([1.0, 0.999, 0.998])["status"] == "continue"
    out = decide([1.0, 0.999, 0.998, 0.997])
    assert out["status"] == "stagnated" and out["rounds_without_progress"] == 3
    # 中间有一轮明显进步就重新计数
    assert decide([1.0, 0.999, 0.9, 0.899, 0.898])["status"] == "continue"


def test_infinite_rounds_count_as_no_progress():
    assert decide([INF, INF, INF, INF])["status"] == "stagnated"
    assert decide([INF, INF, 5.0])["rounds_without_progress"] == 0


def test_round_limit():
    history = [10.0 * 0.9**k for k in range(21)]
    assert decide(history)["status"] == "limit"


def test_explicit_parameters_do_not_touch_defaults():
    import rounds

    traj = [10.0, 5.0, 4.98, 4.97, 4.96]
    assert decide(traj)["status"] == "stagnated"
    assert decide(traj, n=4)["status"] == "continue"
    assert decide([3.0, 2.0, 1.0], cap=2)["status"] == "limit"
    assert significant(1.0, 0.995, rel=0.005) is True
    assert (rounds.STAGNATION_ROUNDS, rounds.STAGNATION_REL, rounds.MAX_ROUNDS) == (3, 0.01, 20)



SPEC_YAML = """spec_version: 1
correction_class: {cls}
magnification: 10
na: 0.25
limits:
  working_distance_min: {{value: 10.6, basis: "{basis}"}}
targets:
  field_rms_wfe: {{range: 0.6}}
"""

# ---- 2. 验收配置冻结 ----


def write_spec(job, text, name="spec.yaml"):
    job.spec_dir.mkdir(exist_ok=True)
    (job.spec_dir / name).write_text(text, encoding="utf-8")
    return f"spec/{name}"


def frozen_check(job, name):
    spec = load_spec(job.root / name)
    config = acceptance_config(spec)
    check_frozen(job, name, config, config_hash(config))


def test_changed_threshold_is_detected(saos_home):
    job = jobs.create_job("freeze")
    name = write_spec(job, SPEC_YAML.format(cls="achromat", basis="first"))
    config = acceptance_config(load_spec(job.root / name))
    freeze(job, name, config, config_hash(config))
    frozen_check(job, name)  # 未改动

    # 只改说明文字：补全后的验收配置不变，允许
    write_spec(job, SPEC_YAML.format(cls="achromat", basis="edited note"))
    frozen_check(job, name)

    # 原地改阈值：必须拒绝
    write_spec(job, SPEC_YAML.format(cls="achromat", basis="x").replace("10.6", "11.0"))
    with pytest.raises(SaosError) as exc:
        frozen_check(job, name)
    assert exc.value.code == ErrorCode.SPEC_CHANGED
    assert "items" in exc.value.details["changed_keys"]


def test_changed_spec_path_is_detected(saos_home):
    job = jobs.create_job("freeze")
    name = write_spec(job, SPEC_YAML.format(cls="achromat", basis="x"))
    other = write_spec(job, SPEC_YAML.format(cls="achromat", basis="x"), "other.yaml")
    config = acceptance_config(load_spec(job.root / name))
    freeze(job, name, config, config_hash(config))
    with pytest.raises(SaosError) as exc:
        frozen_check(job, other)
    assert exc.value.code == ErrorCode.SPEC_CHANGED


SPEC = """spec_version: 1
correction_class: achromat
magnification: 20
na: 0.4
limits:
  working_distance_min: {wd}
"""


def record(daemon, job, **kwargs):
    source = (Path(__file__).with_name("rounds.py")).read_text(encoding="utf-8")
    return daemon.handle({"op": "script.exec", "args": {"job": job.id, "name": "round", "source": source, "args": kwargs}})


def test_observe_rounds_keep_freeze_but_do_not_stop(saos_home, tmp_path, monkeypatch):
    fake_lens_file = tmp_path / "a.zmx"
    fake_lens_file.write_text(json.dumps({"title": "fake", "surfaces": [fake_surface(i, thickness=float("inf") if i == 0 else 1.0) for i in range(4)]}), encoding="utf-8")
    backend = FakeBackend()
    daemon = Daemon(backend, backend.start(), "standalone")
    job = jobs.create_job("observe")
    job.spec_dir.mkdir(exist_ok=True)
    (job.spec_dir / "b.yaml").write_text(SPEC.format(wd=10), encoding="utf-8")
    daemon.op_import({"job": job.id, "src": str(fake_lens_file), "name": "a.zmx"})
    daemon.op_load({"job": job.id, "file": "a.zmx"})
    gs = iter([5.0, 5.0, 5.0, 5.0, 5.0, 5.0])
    monkeypatch.setattr(backend, "merit_value", lambda: 0.0, raising=False)
    monkeypatch.setattr(ops, "_accept", lambda ctx, name, spec: {"verdict": "fail", "spec_shortfall": next(gs), "items": []})
    for _ in range(4):
        resp = record(daemon, job, spec="spec/b.yaml", observe=True)
        assert resp["ok"], resp
        out = resp["result"]["result"]
    assert out["decision"]["status"] == "stagnated" and out["record"]["round"] == 3
    out = record(daemon, job, spec="spec/b.yaml", observe=True)["result"]["result"]  # 停机后仍可观察
    assert out["record"]["round"] == 4 and out["record"]["observe"] is True
    # 记录轮次后当前镜头仍是工作文件，归档不会被之后的保存改写 / the working file stays current after a round
    assert daemon.loaded.path.name == "a.zmx" and not daemon.loaded.dirty and (job.lenses_dir / "rounds" / "r004.zmx").is_file()
    assert record(daemon, job)["error"]["code"] == ErrorCode.BAD_ARGS  # 不加 observe 时照常拒绝
    (job.spec_dir / "b.yaml").write_text(SPEC.format(wd=12), encoding="utf-8")
    assert record(daemon, job, spec="spec/b.yaml", observe=True)["error"]["code"] == ErrorCode.SPEC_CHANGED  # observe 也不能绕过冻结
