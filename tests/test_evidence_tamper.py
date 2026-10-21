"""A2：封存证据被删除或改动后，报告与 ZOS 认可入口明确拒绝；旧绑定双文件（schema 2）迁移不改原件。
A2: deleted or changed sealed evidence is refused by the report and the ZOS acceptance; legacy bound pairs (schema 2)
migrate without touching their originals. Real Optiland throughout."""

from __future__ import annotations

import hashlib
import json

import pytest
import yaml

from saos import demand, jobs, lens_files, prescription, report, stages
from saos.backend.optiland import OptilandBackend
from saos.daemon import Daemon
from saos.protocol import ErrorCode, SaosError
from test_optiland_flow import DEMAND, NOTES, call
from test_lens_snapshot_files import PRESCRIPTION


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    """一个已记录一轮 Optiland 结果、模型已认可、报告已生成的任务。 / A job with one recorded Optiland round, an approved model and a report."""
    monkeypatch.setenv("SAOS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAOS_BACKEND", "optiland")
    monkeypatch.setenv("SAOS_UI", "0")
    backend = OptilandBackend()
    daemon = Daemon(backend, backend.start(), "standalone")
    job = jobs.create_job("tamper")
    assert call(daemon, "lens.build", job=job.id, prescription=PRESCRIPTION, file="single.zmx")["ok"]
    (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(DEMAND, allow_unicode=True), encoding="utf-8")
    demand.confirm(job.root, "确认，开始吧")
    folder = call(daemon, "lens.describe", job=job.id, notes=NOTES)["result"]["folder"]
    assert call(daemon, "lens.approve", job=job.id, model=folder, quote="模型可以")["ok"]
    assert call(daemon, "round.record", job=job.id)["ok"]
    report.write(job)
    yield daemon, job
    daemon.optiland.close()


def _round_snapshot(job):
    return job.lenses_dir / "rounds" / "r000.optiland.json"


def test_a_deleted_round_snapshot_is_refused_by_report_and_acceptance(recorded):
    daemon, job = recorded
    snapshot = _round_snapshot(job)
    assert snapshot.is_file() and job.verify_seals() == []
    backup = snapshot.read_bytes()
    snapshot.unlink()
    assert [p["problem"] for p in job.verify_seals()] == ["missing"]
    with pytest.raises(SaosError) as refused:
        report.write(job)
    assert refused.value.code == ErrorCode.EVIDENCE_TAMPERED
    reply = call(daemon, "zos.begin", job=job.id, quote="可以，接受", round=None)
    assert reply["error"]["code"] == ErrorCode.EVIDENCE_TAMPERED
    assert daemon.zos is None and stages.read(job.root) is None  # 没有连接 OpticStudio，也没有写入口 / no OpticStudio, no entry
    # 恢复原件后，检查通过 / restoring the original clears the problem
    snapshot.write_bytes(backup)
    assert job.verify_seals() == []


def test_a_changed_round_snapshot_is_refused_and_no_round_is_added(recorded):
    daemon, job = recorded
    snapshot = _round_snapshot(job)
    rounds_before = len(open(job.root / "ledger.jsonl", encoding="utf-8").read().splitlines())
    snapshot.write_bytes(snapshot.read_bytes().replace(b"\"prescription\"", b"\"prescription \"", 1))
    assert any(p["problem"] == "changed" for p in job.verify_seals())
    with pytest.raises(SaosError) as refused:
        report.write(job)
    assert refused.value.code == ErrorCode.EVIDENCE_TAMPERED
    assert call(daemon, "zos.begin", job=job.id, quote="可以，接受", round=None)["error"]["code"] == ErrorCode.EVIDENCE_TAMPERED
    assert len(open(job.root / "ledger.jsonl", encoding="utf-8").read().splitlines()) == rounds_before


def _legacy_pair(path, payload, zmx_bytes):
    """旧 schema 2 双文件：.zmx 与快照都写出，快照记录 .zmx 哈希。 / A schema-2 pair: .zmx and snapshot, the snapshot records the .zmx hash."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zmx_bytes)
    body = {"schema": lens_files.LEGACY_PAIR_SCHEMA, "zmx_sha256": hashlib.sha256(zmx_bytes).hexdigest(),
            "payload_sha256": hashlib.sha256(lens_files.payload_bytes(payload)).hexdigest(), "payload": payload}
    lens_files.companion(path).write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")


def test_a_legacy_pair_loads_and_migrates_without_touching_the_original(tmp_path, monkeypatch):
    monkeypatch.setenv("SAOS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SAOS_BACKEND", "optiland")
    monkeypatch.setenv("SAOS_UI", "0")
    backend = OptilandBackend()
    backend.start()
    try:
        backend.build(prescription.normalize(PRESCRIPTION))
        payload = backend.companion_payload()
        source_dir = tmp_path / "legacy"
        source = source_dir / "old.zmx"
        _legacy_pair(source, payload, b"ZEMAX legacy primary file\n")
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, lens_files.companion(source))}

        # 只读兼容：读得出 payload，配对检查通过 / readable, and the pair checks out
        assert lens_files.read_companion(source) == payload
        # 单独改动旧 .zmx：拒绝 / a lone edit of the old .zmx is refused
        source.write_bytes(b"edited by hand\n")
        with pytest.raises(SaosError) as refused:
            lens_files.read_companion(source)
        assert refused.value.code == ErrorCode.LENS_COMPANION_MISMATCH
        source.write_bytes(b"ZEMAX legacy primary file\n")

        # 迁移：复制到任务中成为新副本，原件字节不变，副本可读可加载 / migrate: a copy in the job, originals untouched
        job = jobs.create_job("migrate")
        target = job.resolve_lens("migrated.zmx", must_exist=False)
        lens_files.copy_lens(source, target)
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, lens_files.companion(source))}
        assert after == before
        assert lens_files.read_companion(target) == payload
        backend.load(target)
        assert len(backend.surfaces()) == 4
    finally:
        backend.close()
