"""镜头产物（原生快照、.zmx 导出）的离线与真实 Optiland 测试。快照与导出件的光学结果用真实 Optiland 计算。
Lens products (native snapshot, .zmx export): offline file tests plus real Optiland checks."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from saos import jobs, lens_files
from saos.backend.optiland import OptilandBackend
from saos.daemon import Daemon
from saos.protocol import ErrorCode

PRESCRIPTION = {
    "title": "snapshot singlet",
    "aperture": {"type": "entrance_pupil_diameter", "value": 10.0},
    "fields": {"type": "angle", "y": [0.0, 5.0]},
    "wavelengths": {"um": [0.5876], "weights": [1.0], "primary": 1},
    "ray_aiming": "real",
    "object": {"thickness": "inf", "comment": "saos:object"},
    "surfaces": [{"radius": 50.0, "thickness": 5.0, "material": "N-BK7", "stop": True},
                 {"radius": -50.0, "thickness": 95.0, "material": "", "stop": False}],
}


def _payload(tag: str) -> dict:
    """离线测试用的快照内容：只要求是可哈希的字典，不需要光学意义。 / Offline payload: any hashable dict will do."""
    return {"optiland_version": "0.6.3", "prescription": {"tag": tag}, "materials": {}}


def _snapshot_only(path: Path, tag: str) -> None:
    lens_files.write_snapshot(path, lens_files.companion_bytes(None, _payload(tag)))


def test_exists_files_and_listing_count_a_snapshot_on_its_own(tmp_path):
    folder = tmp_path / "lenses"
    folder.mkdir()
    a = folder / "GLOPT_0000.zmx"
    assert not lens_files.exists(a) and lens_files.files(a) == []
    _snapshot_only(a, "a")
    assert lens_files.exists(a) and lens_files.files(a) == [lens_files.companion(a)]
    assert lens_files.listing(folder, "GLOPT_*.zmx") == [a]
    assert lens_files.all_lenses(folder) == [a]


def test_write_snapshot_removes_the_old_primary_and_keeps_the_pair_valid(tmp_path):
    path = tmp_path / "a.zmx"
    path.write_bytes(b"old zemax file")
    lens_files.write_pair(path, b"old zemax file", lens_files.companion_bytes(b"old zemax file", _payload("old")))
    _snapshot_only(path, "new")
    assert not path.exists() and lens_files.read_companion(path) == _payload("new")


def test_a_failed_snapshot_write_restores_the_old_primary(tmp_path, monkeypatch):
    """替换快照失败时，已删除的旧 .zmx 必须写回：旧镜头保持成对。 / If replacing the snapshot fails, the deleted old .zmx comes back."""
    path = tmp_path / "a.zmx"
    lens_files.write_pair(path, b"old zemax file", lens_files.companion_bytes(b"old zemax file", _payload("old")))
    real_replace = os.replace

    def failing(src, dst):
        if str(dst).endswith(".optiland.json"):
            raise OSError("injected failure while replacing the snapshot")
        return real_replace(src, dst)

    monkeypatch.setattr(lens_files.os, "replace", failing)
    with pytest.raises(OSError, match="injected"):
        _snapshot_only(path, "new")
    assert path.read_bytes() == b"old zemax file"
    assert lens_files.read_companion(path) == _payload("old")
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_a_failed_removal_of_the_old_primary_changes_nothing(tmp_path, monkeypatch):
    path = tmp_path / "a.zmx"
    lens_files.write_pair(path, b"old zemax file", lens_files.companion_bytes(b"old zemax file", _payload("old")))
    before = lens_files.companion(path).read_bytes()
    real_unlink = Path.unlink

    def refusing(self, missing_ok=False):
        if self == path:
            raise PermissionError("injected: the .zmx is locked")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refusing)
    with pytest.raises(PermissionError):
        _snapshot_only(path, "new")
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert path.read_bytes() == b"old zemax file" and lens_files.companion(path).read_bytes() == before
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


@pytest.fixture
def optiland_daemon(saos_home):
    backend = OptilandBackend()
    daemon = Daemon(backend, backend.start(), "extension")
    yield daemon, jobs.create_job("snap")
    daemon.optiland.close()


def call(daemon, op, **args):
    return daemon.handle({"op": op, "args": args})


def test_optiland_saves_a_snapshot_only_and_reloads_it(optiland_daemon):
    daemon, job = optiland_daemon
    built = call(daemon, "lens.build", job=job.id, prescription=PRESCRIPTION, file="single.zmx")
    assert built["ok"], built
    lens = job.resolve_lens("single.zmx", must_exist=True)
    assert not lens.exists() and lens_files.companion(lens).is_file()
    assert lens_files.read_companion(lens)["prescription"]["surfaces"]
    saved = call(daemon, "lens.save", job=job.id, file="copy.zmx")
    assert saved["ok"] and "zmx_exported" not in saved["result"]
    assert not job.resolve_lens("copy.zmx", must_exist=False).exists()
    assert lens_files.companion(job.resolve_lens("copy.zmx", must_exist=False)).is_file()
    loaded = call(daemon, "lens.load", job=job.id, file="copy.zmx")
    assert loaded["ok"], loaded
    assert len(daemon.backend.surfaces()) == 4


def test_saving_over_a_snapshot_only_lens_needs_overwrite(optiland_daemon):
    daemon, job = optiland_daemon
    assert call(daemon, "lens.build", job=job.id, prescription=PRESCRIPTION, file="one.zmx")["ok"]
    assert call(daemon, "lens.save", job=job.id, file="two.zmx")["ok"]
    refused = call(daemon, "lens.build", job=job.id, prescription=PRESCRIPTION, file="two.zmx")
    assert refused["error"]["code"] == ErrorCode.FILE_EXISTS
    # 当前镜头是 two.zmx，另存为只有快照的 one.zmx 时同样需要 overwrite / saving as a snapshot-only name needs overwrite
    assert call(daemon, "lens.save", job=job.id, file="one.zmx")["error"]["code"] == ErrorCode.FILE_EXISTS


def test_export_writes_a_pair_or_names_the_gap_and_leaves_the_snapshot_alone(optiland_daemon):
    daemon, job = optiland_daemon
    assert call(daemon, "lens.build", job=job.id, prescription=PRESCRIPTION, file="exp.zmx")["ok"]
    snapshot = job.resolve_lens("exp.zmx", must_exist=True)
    before = lens_files.companion(snapshot).read_bytes()
    out = call(daemon, "lens.export", job=job.id, file="exp-out.zmx")
    target = job.resolve_lens("exp-out.zmx", must_exist=False)
    if "error" in out:
        assert out["error"]["code"] == ErrorCode.BACKEND_UNSUPPORTED and out["error"]["message"]
        assert not lens_files.exists(target)
    else:
        assert target.is_file() and lens_files.read_companion(target) is not None
    assert lens_files.companion(snapshot).read_bytes() == before
    assert call(daemon, "lens.export", job=job.id, file="exp-out.zmx")["error"]["code"] == ErrorCode.FILE_EXISTS
