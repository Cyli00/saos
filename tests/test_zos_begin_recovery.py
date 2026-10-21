"""Z03：zos begin 在各个写入点中断后重试，只补缺项；成功入口恰有 1 条对应 verify；损坏的证据不被重新封存掩盖。

OpticStudio 会话由测试替身 FakeZos 代替（它只读取封存快照里的处方），以便在不占用 OpticStudio 的情况下注入中断。
状态写入的故障注入按 A3 的要求使用测试替身；真实 OpticStudio 的入口由 test_zemax_acceptance.py 覆盖。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
import yaml

from saos import demand, jobs, ledger, lens_files, report, stages, workflow
from saos.backend.fake import FakeBackend, fake_surface
from saos.backend.optiland import OptilandBackend
from saos.daemon import Daemon
from saos.protocol import ErrorCode, SaosError
from test_optiland_flow import call
from zemax_support import NOTES, TWO_GLASS

QUOTE = "可以，接受这份报告，进入 OpticStudio"
# 总长上限放宽到 110 mm，使未优化的基线轮次就合资格；这里测的是入口的写入顺序，不是光学结果
# The total-track limit is loosened to 110 mm so the unoptimised baseline round is eligible; this file tests write order, not optics
DEMAND = {"schema": 2,
          "constants": [{"id": "lens", "description": "the two-glass sample", "value": "two_glass", "source": "user"}],
          "variables": [{"id": "radii", "description": "radii and thicknesses are free"}],
          "objectives": [{"id": "length", "description": "total track", "check": {"operand": {"type": "TOTR"}}, "origin": "user",
                          "must": {"max": 110.0}, "scale": 5.0, "source": "user"}],
          "structure": {"mode": "prescribed", "reason": "测试样品的两片结构由用户指定"},
          "summary": "用户要总长不超过 110 mm。"}
INJECTED = "injected: the write failed before completing"


class FakeZos(FakeBackend):
    """OpticStudio 会话的测试替身：读取快照里的处方并转成面表。 / Test double for the OpticStudio session: reads the prescription in a snapshot."""

    def identity(self) -> dict:
        # 报告只接受 zos 环境的阶段记录，替身按 OpticStudio 会话报告自己 / the report accepts only zos environments, so the double identifies as OpticStudio
        return {"backend": "zos", "version": "fake", "precision": None}

    def load(self, path: Path) -> None:
        payload = lens_files.read_companion(path)
        if payload is None:
            raise SaosError(ErrorCode.ZOS_LOAD_FAILED, f"no snapshot for {path}")
        presc = payload["prescription"]
        rows = [fake_surface(0, radius=float("inf"), thickness=float("inf"))]
        for i, s in enumerate(presc["surfaces"], start=1):
            rows.append(fake_surface(i, radius=s["radius"], thickness=s["thickness"], material=s["material"], is_stop=bool(s.get("stop"))))
        rows.append(fake_surface(len(rows), radius=float("inf"), thickness=0.0))
        self.lens = {"title": presc.get("title", ""), "surfaces": rows}
        self.file = path


def fail_once(monkeypatch, owner, name: str, matches, *, after: bool = False) -> None:
    """第一次满足 matches(args) 的调用失败。after=True 时真实动作先完成，再报错（模拟写完后、返回前中断）。
    The first call that matches fails. With after=True the real action completes first, then the call fails."""
    real = getattr(owner, name)
    state = {"left": True}

    def wrapper(*args, **kwargs):
        if state["left"] and matches(args):
            state["left"] = False
            if after:
                real(*args, **kwargs)
            raise SaosError(ErrorCode.ZOS_SAVE_FAILED, INJECTED)
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, wrapper)


def named(*names):
    return lambda args: any(Path(a).name in names for a in args if isinstance(a, (str, Path)))


@pytest.fixture
def ready(tmp_path, monkeypatch):
    """经用户认可、报告已生成、等待 zos begin 的任务；OpticStudio 用 FakeZos 代替。 / A job ready for zos begin, with FakeZos as OpticStudio."""
    monkeypatch.setenv("SAOS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAOS_BACKEND", "optiland")
    monkeypatch.setenv("SAOS_UI", "0")
    backend = OptilandBackend()
    daemon = Daemon(backend, backend.start(), "standalone")
    window = FakeZos()
    window.start()
    daemon.zos = window
    job = jobs.create_job("zos-begin")
    assert call(daemon, "lens.build", job=job.id, prescription=TWO_GLASS, file="two_glass.zmx")["ok"]
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(DEMAND, allow_unicode=True), encoding="utf-8")
    demand.confirm(job.root, "确认，开始吧")
    folder = call(daemon, "lens.describe", job=job.id, notes=NOTES)["result"]["folder"]
    assert call(daemon, "lens.approve", job=job.id, model=folder, quote="模型可以")["ok"]
    # 测试替身报告与认可时相同的模型描述，使模型门槛通过；被测的是写入顺序，不是模型比较
    # The double reports the same model description as the approval, so the model gate passes; the test covers write order
    approved = json.loads((job.root / folder / "model.json").read_text(encoding="utf-8"))["model"]
    daemon.zos.describe_model = lambda folder=None, prefix="layout": copy.deepcopy(approved)
    assert call(daemon, "round.record", job=job.id)["ok"]
    report.write(job)
    yield daemon, job
    daemon.pool.close()
    daemon.optiland.close()


def verify_rows(job, source_round: int) -> list[dict]:
    history = ledger.load(job.root / ledger.LEDGER_FILE)
    return [r for r in history if r.get("stage") == "zos" and r.get("verify_of") == source_round]


def begin(daemon, job):
    return call(daemon, "zos.begin", job=job.id, quote=QUOTE, round=None)


@pytest.fixture
def injection(monkeypatch, ready):
    """注入点表：名称 → 安装故障的函数。每一项都是入口写入流程里的一个动作。 / Injection points: name → installer; each is one write step."""
    daemon, job = ready
    return {
        "connect": lambda: fail_once(monkeypatch, daemon, "_zos_session", lambda args: True),
        "load": lambda: fail_once(monkeypatch, daemon.zos, "load", lambda args: True),
        "entry-write": lambda: fail_once(monkeypatch, stages, "write_json", named("zos.json")),
        "entry-seal": lambda: fail_once(monkeypatch, jobs.Job, "seal", named("zos.json")),
        "working-save": lambda: fail_once(monkeypatch, daemon.zos, "save_as", named("zos-entry.zmx")),
        "verify-append": lambda: fail_once(monkeypatch, workflow, "record_round", lambda args: True),
        "verify-append-after": lambda: fail_once(monkeypatch, workflow, "record_round", lambda args: True, after=True),
        "result-write": lambda: fail_once(monkeypatch, stages, "write_json", named("zos-result.json")),
        "result-seal": lambda: fail_once(monkeypatch, jobs.Job, "seal", named("zos-result.json")),
    }


@pytest.mark.parametrize("point", ["connect", "load", "entry-write", "entry-seal", "working-save",
                                   "verify-append", "verify-append-after", "result-write", "result-seal"])
def test_interrupted_entry_resumes_without_a_second_verify(ready, injection, point):
    daemon, job = ready
    root = job.root
    injection[point]()

    first = begin(daemon, job)
    assert not first["ok"] and first["error"]["code"] == ErrorCode.ZOS_SAVE_FAILED, first
    entry_after_failure = stages.read(root)

    again = begin(daemon, job)
    assert again["ok"], again
    source = stages.read(root)["source_round"]
    rows = verify_rows(job, source)
    assert len(rows) == 1, [r["round"] for r in rows]  # 同一成功入口恰有 1 条对应 verify / exactly one verify for the entry
    assert stages.sealed(root, stages.ENTRY) and stages.sealed(root, stages.RESULT)
    assert job.verify_seals() == []
    if entry_after_failure:  # 入口一经写入就不再重建 / once written, the entry is not rebuilt
        assert stages.read(root)["time"] == entry_after_failure["time"]
    assert (root / "lenses" / "zos-entry.zmx").is_file()


def test_a_sealed_entry_changed_after_success_is_refused_and_not_resealed(ready):
    daemon, job = ready
    assert begin(daemon, job)["ok"]
    source = stages.read(job.root)["source_round"]
    rows_before = len(verify_rows(job, source))
    entry = job.root / stages.ENTRY
    entry.write_bytes(entry.read_bytes().replace(b'"quote"', b'"quotx"', 1))
    assert any(p["problem"] == "changed" for p in job.verify_seals())
    assert begin(daemon, job)["error"]["code"] == ErrorCode.EVIDENCE_TAMPERED
    assert len(verify_rows(job, source)) == rows_before
    assert any(p["problem"] == "changed" for p in job.verify_seals())  # 没有被重新封存 / not resealed


def test_a_changed_comparison_is_refused_and_no_second_verify_is_added(ready):
    daemon, job = ready
    assert begin(daemon, job)["ok"]
    source = stages.read(job.root)["source_round"]
    result = job.root / stages.RESULT
    result.write_bytes(result.read_bytes() + b" ")
    assert any(p["problem"] == "changed" for p in job.verify_seals())
    assert begin(daemon, job)["error"]["code"] == ErrorCode.EVIDENCE_TAMPERED
    assert len(verify_rows(job, source)) == 1


def test_r2_corrupt_earliest_approval_blocks_report_and_zos_begin(ready):
    """最早确认损坏但最新确认完整时，round 可继续；报告与 ZOS 入口的全任务封存检查仍拒绝并指出坏路径。"""
    daemon, job = ready
    second = call(daemon, "lens.describe", job=job.id, notes=NOTES)["result"]["folder"]
    assert call(daemon, "lens.approve", job=job.id, model=second, quote="再次确认模型")["ok"]
    first = sorted((job.root / "models").glob("*/approval.json"))[0]
    first.write_text("{not json", encoding="utf-8")
    with pytest.raises(SaosError) as refused:
        report.write(job)
    assert refused.value.code == ErrorCode.EVIDENCE_TAMPERED
    assert any(p["path"] == first.relative_to(job.root).as_posix() for p in refused.value.details["problems"])
    reply = begin(daemon, job)
    assert reply["error"]["code"] == ErrorCode.EVIDENCE_TAMPERED
    assert stages.read(job.root) is None


def test_r8_zos_begin_reports_snapshot_alias_conflict(ready):
    """存档处方同时写 parN 和同阶表头且值不同：zos begin 在加载快照时返回 BAD_ARGS，并指出面与阶段。"""
    daemon, job = ready
    rows = ledger.load(job.root / ledger.LEDGER_FILE)
    source = job.resolve_lens(rows[-1]["file"], must_exist=True)
    side = lens_files.companion(source)
    data = json.loads(side.read_text(encoding="utf-8"))
    payload = data["payload"]
    surface = payload["prescription"]["surfaces"][0]
    surface["type"] = "EvenAspheric"
    surface["params"] = {"par2": 1e-5, "4th Order Term": 2e-5}
    primary = source.read_bytes() if source.is_file() else None
    side_bytes = lens_files.companion_bytes(primary, payload)
    if primary is None:
        lens_files.write_snapshot(source, side_bytes)
    else:
        lens_files.write_pair(source, primary, side_bytes)
    job.seal(source, lens_files.companion(source))

    reply = begin(daemon, job)
    assert reply["error"]["code"] == ErrorCode.BAD_ARGS
    assert reply["error"]["details"] == {"surface": "surfaces[0]", "phase": "snapshot_parameters"}
    assert "重复指定" in reply["error"]["message"]
    assert stages.read(job.root) is None
