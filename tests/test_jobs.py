from __future__ import annotations

import json

import pytest

from saos import jobs
from saos.protocol import ErrorCode, SaosError


def test_create_job_layout(saos_home, tmp_path):
    spec = tmp_path / "spec.yaml"
    spec.write_text("spec_version: 1\n", encoding="utf-8")
    job = jobs.create_job("plan10x", str(spec))

    assert job.root.parent == saos_home / "jobs"
    assert job.id.endswith("-plan10x")
    assert (job.spec_dir / "spec.yaml").read_text(encoding="utf-8") == "spec_version: 1\n"
    assert job.lenses_dir.is_dir()
    state = job.read_state()
    assert state["status"] == "created"
    assert state["spec"] == "spec/spec.yaml"
    first_log = json.loads(job.log_file.read_text(encoding="utf-8").splitlines()[0])
    assert first_log["op"] == "job.create"


@pytest.mark.parametrize("name", ["", "-x", "has space", "a/b", "x" * 49])
def test_create_job_rejects_bad_names(saos_home, name):
    with pytest.raises(SaosError) as exc:
        jobs.create_job(name)
    assert exc.value.code == ErrorCode.BAD_ARGS


def test_create_job_missing_spec(saos_home):
    with pytest.raises(SaosError) as exc:
        jobs.create_job("a", "no-such-spec.yaml")
    assert exc.value.code == ErrorCode.FILE_NOT_FOUND


def test_get_job_unknown(saos_home):
    with pytest.raises(SaosError) as exc:
        jobs.get_job("20260101-000000-missing")
    assert exc.value.code == ErrorCode.JOB_NOT_FOUND
    with pytest.raises(SaosError) as exc:
        jobs.get_job("../escape")
    assert exc.value.code == ErrorCode.BAD_ARGS


@pytest.mark.parametrize(
    "name, code",
    [
        ("../other.zmx", ErrorCode.PATH_OUTSIDE_JOB),
        ("../../x/lenses/a.zmx", ErrorCode.PATH_OUTSIDE_JOB),
        ("C:/temp/a.zmx", ErrorCode.PATH_OUTSIDE_JOB),
        ("/temp/a.zmx", ErrorCode.PATH_OUTSIDE_JOB),
        ("", ErrorCode.PATH_OUTSIDE_JOB),
        ("a.txt", ErrorCode.BAD_ARGS),
    ],
)
def test_resolve_lens_rejects(saos_home, name, code):
    job = jobs.create_job("a")
    with pytest.raises(SaosError) as exc:
        job.resolve_lens(name, must_exist=False)
    assert exc.value.code == code


def test_resolve_lens_inside_job(saos_home):
    job = jobs.create_job("a")
    path = job.resolve_lens("round/r001.zmx", must_exist=False)
    assert path == (job.lenses_dir / "round" / "r001.zmx").resolve()
    assert job.relative_lens(path) == "round/r001.zmx"
    with pytest.raises(SaosError) as exc:
        job.resolve_lens("missing.zmx", must_exist=True)
    assert exc.value.code == ErrorCode.FILE_NOT_FOUND


def test_list_jobs(saos_home):
    assert jobs.list_jobs() == []
    a = jobs.create_job("a")
    ids = [j["id"] for j in jobs.list_jobs()]
    assert ids == [a.id]
