from __future__ import annotations

import json

import pytest

from saos import jobs
from saos.protocol import ErrorCode, SaosError


def test_create_job_layout(saos_home):
    job = jobs.create_job("plan10x")

    assert job.root.parent == saos_home / "jobs"
    assert job.id.endswith("-plan10x")
    assert job.lenses_dir.is_dir() and (job.root / "sources").is_dir()
    assert not (job.root / "spec").exists()
    state = job.read_state()
    assert state["status"] == "created" and "spec" not in state
    first_log = json.loads(job.log_file.read_text(encoding="utf-8").splitlines()[0])
    assert first_log["op"] == "job.create"


@pytest.mark.parametrize("name", ["", "-x", "has space", "a/b", "x" * 49])
def test_create_job_rejects_bad_names(saos_home, name):
    with pytest.raises(SaosError) as exc:
        jobs.create_job(name)
    assert exc.value.code == ErrorCode.BAD_ARGS


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


def _cli(capsys, *argv):
    from saos import cli

    code = cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_the_user_hands_an_autonomous_job_back_in_their_own_words(saos_home, capsys):
    job = jobs.create_job("exercise", "autonomous")
    code, out = _cli(capsys, "job", "make-interactive", job.id)
    assert code == 1 and out["error"]["code"] == ErrorCode.BAD_ARGS and "user's own words" in out["error"]["message"]
    assert jobs.get_job(job.id).mode == "autonomous"

    code, out = _cli(capsys, "job", "make-interactive", job.id, "--quote", "后面的关卡我自己来过")
    assert code == 0 and out["result"] == {"mode": "interactive", "changed": True}
    state = jobs.get_job(job.id).read_state()
    assert state["mode"] == "interactive" and state["mode_changed_via"] == "cli"
    interview = (job.root / "sources" / "interview.md").read_text(encoding="utf-8")
    assert interview.count("后面的关卡我自己来过") == 1 and "在对话里" in interview
    log = [json.loads(line) for line in job.log_file.read_text(encoding="utf-8").splitlines()]
    assert log[-1]["args"] == {"mode": "interactive", "via": "cli"}

    # 再调一次：不变，不重复写 / again: unchanged, nothing written twice
    code, out = _cli(capsys, "job", "make-interactive", job.id, "--quote", "后面的关卡我自己来过", "--asked")
    assert code == 0 and out["result"] == {"mode": "interactive", "changed": False}
    assert (job.root / "sources" / "interview.md").read_text(encoding="utf-8") == interview
    assert jobs.get_job(job.id).read_state()["mode_changed_via"] == "cli"


def test_the_ask_channel_is_recorded_and_analysis_jobs_have_no_gates_to_hand_back(saos_home, capsys):
    job = jobs.create_job("exercise", "autonomous")
    code, out = _cli(capsys, "job", "make-interactive", job.id, "--quote", "问：要自己过关吗？选项：是/否。选：是", "--asked")
    assert code == 0 and jobs.get_job(job.id).read_state()["mode_changed_via"] == "ask"
    analysis = jobs.create_job("question", "analyse")
    code, out = _cli(capsys, "job", "make-interactive", analysis.id, "--quote", "我自己来")
    assert code == 1 and out["error"]["code"] == ErrorCode.BAD_ARGS
    assert jobs.get_job(analysis.id).mode == "analyse"
    # 窗口按钮不带原话也行，渠道记为 window / the window's button needs no quote and is recorded as window
    other = jobs.create_job("drill", "autonomous")
    assert jobs.make_interactive(other)["changed"] is True
    assert jobs.get_job(other.id).read_state()["mode_changed_via"] == "window"
    assert "窗口按钮" in (other.root / "sources" / "interview.md").read_text(encoding="utf-8")
