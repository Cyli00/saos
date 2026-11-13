"""脚本判定对用户可见：确认页给出每个脚本的说明，确认记下脚本哈希，之后改过的脚本由 demand show 列出。
Script checks made visible: the confirmation shows each script's docstring and records its hash; demand show lists edits since."""

from __future__ import annotations

import yaml

from saos import demand, jobs

DOCUMENTED = '"""轴上 RMS 点列半径，三个波长等权，参考点为多色质心。\n\n实现细节。"""\n\ndef run(ctx):\n    return 1.0\n'
BARE = "def run(ctx):\n    return 2.0\n"
DEMAND = {"schema": 2, "constants": [],
          "objectives": [{"id": "spot", "description": "spot", "check": {"script": "checks/spot.py"}, "origin": "user",
                          "must": {"max": 5.0}, "scale": 1.0, "source": "user"},
                         {"id": "bare", "description": "bare", "check": {"script": "checks/bare.py"}, "origin": "user",
                          "must": {"max": 5.0}, "scale": 1.0, "source": "user"}],
          "summary": "check scripts"}


def _job():
    job = jobs.create_job("checks")
    (job.root / "checks").mkdir(exist_ok=True)
    (job.root / "checks" / "spot.py").write_text(DOCUMENTED, encoding="utf-8")
    (job.root / "checks" / "bare.py").write_text(BARE, encoding="utf-8")
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(DEMAND, allow_unicode=True), encoding="utf-8")
    demand._PREPARED.clear()
    return job


def test_the_confirmation_shows_what_each_script_measures(saos_home):
    job = _job()
    gate = demand.confirm_gate(job.root)
    docs = {c["id"]: c["doc"] for c in gate["check_scripts"]}
    assert docs == {"spot": "轴上 RMS 点列半径，三个波长等权，参考点为多色质心。", "bare": None}
    assert demand.show(job.root)["undocumented_checks"] == ["bare"]


def test_a_script_edited_after_confirmation_is_listed_not_refused(saos_home):
    job = _job()
    record = demand.confirm(job.root, "确认")
    assert set(record["check_scripts"]) == {"spot", "bare"}
    assert "checks_changed_since_confirm" not in demand.show(job.root)
    (job.root / "checks" / "bare.py").write_text(BARE.replace("2.0", "3.0"), encoding="utf-8")
    demand._PREPARED.clear()
    shown = demand.show(job.root)
    # 需求单没变，确认仍然有效；改过的脚本只列出来 / the demand is unchanged and still confirmed; the edit is only listed
    assert shown["confirmed"] is not None and shown["checks_changed_since_confirm"] == ["bare"]
