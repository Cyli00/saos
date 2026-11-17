"""saos backends 的离线回归：能力表说的要和 rtc 后端实际做的一致，镜头判定要和后台进程的路由一致。不开 OpticStudio。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from saos import capability, cli, jobs, prescription
from saos.backend import rtc, rtc_lens
from saos.backend.rtc import RtcBackend
from saos.daemon import Daemon
from saos.protocol import ErrorCode, SaosError

CASES = Path(__file__).parent / "fixtures" / "cases"
PROBED = ("aperture", "fields", "ray_aiming", "surface_type", "surface_aperture", "solve", "lens", "measurements", "analyses")


def _glasscat_ready() -> bool:
    from rtc.material import catalog_dir

    return any(p.stem.upper() == "SCHOTT" for p in catalog_dir().glob("*.[Aa][Gg][Ff]"))


needs_glass = pytest.mark.skipif(not _glasscat_ready(), reason="需要本机 Documents/Zemax/Glasscat/SCHOTT.AGF")


@pytest.fixture(scope="module")
def table():
    return capability.table()


def _probe(change) -> dict:
    raw = capability._probe()
    change(raw)
    return raw


def test_the_table_covers_every_choice_a_prescription_offers(table):
    listed = table["rtc"]
    assert set(listed["aperture"]) == set(prescription.APERTURES)
    assert set(listed["fields"]) == set(prescription.FIELDS)
    assert set(listed["ray_aiming"]) == set(prescription.RAY_AIMING)
    assert set(listed["surface_aperture"]) == set(prescription.SURFACE_APERTURES)
    assert set(listed["solve"]) == set(prescription.SOLVES)
    for group in PROBED:
        for name, entry in listed[group].items():
            assert entry["status"] in capability.STATUS, (group, name)
            # 没对上或拒绝的都要说原因，agent 才知道换什么 / anything short of reconciled says why
            assert (entry["status"] == "reconciled") == ("why" not in entry) or group in ("measurements", "analyses"), (group, name)


def test_the_table_says_what_the_backend_does(table):
    listed = table["rtc"]
    status = {group: {name: entry["status"] for name, entry in listed[group].items()} for group in PROBED}
    assert status["surface_type"]["standard"] == "reconciled"
    assert status["surface_type"]["coordinatebreak"] == status["surface_type"]["other"] == "refused"
    assert status["lens"]["decenter_tilt"] == "refused" and status["lens"]["mirror"] == "reconciled"
    assert status["lens"]["automatic_semi_diameter"] == "unreconciled"
    assert status["measurements"]["huygens"] == "refused"
    assert status["analyses"] == {"FftMtf": "unreconciled", "other": "refused"}
    # 表里拒绝的每一项，带它的镜头后台进程都不在 rtc 上建 / the daemon builds no lens on rtc that holds a refused item
    probes = capability._probes()
    for group, items in probes.items():
        for name, change in items.items():
            refused = False
            try:
                RtcBackend(None, standalone=True).build(prescription.normalize(_probe(change)))
            except SaosError as exc:
                assert exc.code == ErrorCode.BACKEND_UNSUPPORTED, (group, name, exc.message)
                refused = True
            assert refused == (status[group][name] == "refused"), (group, name)


def test_operand_statuses_follow_the_rule_computed_by_uses(table):
    operands = table["rtc"]["operands"]
    assert set(operands["reconciled"]) | set(operands["unreconciled"]) == rtc.RAY_OPERANDS | rtc.SYSTEM_OPERANDS
    backend = RtcBackend(None, standalone=True)
    backend.build(prescription.normalize(capability._probe()))
    backend.provenance()
    backend.operand("REAY", 2, 1, [0.0, 1.0, 0.0, 1.0])
    assert "REAY" in operands["reconciled"] and backend.provenance()["operands_unverified"] == []
    backend.operand("TOTR", 0, 0, [])
    assert "TOTR" in operands["unreconciled"] and backend.provenance()["operands_unverified"] == ["TOTR"]
    with pytest.raises(SaosError) as err:
        backend.operand("MTFT", 0, 1, [])
    assert err.value.code == ErrorCode.BACKEND_UNSUPPORTED and "MTFT" not in operands["reconciled"] + operands["unreconciled"]
    merit = table["rtc"]["merit_operands"]
    assert {"TRAC", "OPDX", "MNEG"} <= set(merit["unreconciled"]) and "BLNK" not in merit["unreconciled"]


def test_afocal_is_refused_not_dropped():
    # afocal: true 归一化成 {}；rtc 曾按真值判断，把它当没写，照非无焦系统去算 / afocal: true normalises to {} and used to slip through
    raw = _probe(lambda p: p.update(afocal=True))
    with pytest.raises(SaosError) as err:
        rtc_lens.from_prescription(prescription.normalize(raw))
    assert err.value.code == ErrorCode.BACKEND_UNSUPPORTED and "afocal" in err.value.message


def _built_on(raw: dict, name: str = "route") -> tuple[str, dict | None]:
    """auto 后台进程把这份处方建在哪个后端上，以及切换说明；OpticStudio 的位置放一个 rtc 替身，看的是路由。
    Where an auto daemon builds this prescription, and the switch note; an rtc stand-in takes OpticStudio's place."""
    backend = RtcBackend(None, standalone=True)
    daemon = Daemon(backend, backend.identity(), lambda: (RtcBackend(None, standalone=True), {"backend": "zos"}))
    job = jobs.create_job(name)
    reply = daemon.handle({"op": "lens.build", "args": {"job": job.id, "prescription": raw, "file": "a.zmx"}})
    return daemon.active, (reply.get("result") or reply["error"]["details"]).get("backend_switch")


def test_the_daemon_keeps_off_rtc_what_only_the_built_system_refuses(saos_home):
    aspheric_mirror = _probe(capability._mirror("EvenAspheric"))
    # 处方转换本身接受它，拒绝出在建系统时 / the conversion accepts it; the refusal comes when the system is built
    rtc_lens.from_prescription(prescription.normalize(aspheric_mirror))
    active, switch = _built_on(aspheric_mirror)
    assert active == "zos" and "mirror" in switch["reason"]
    assert _built_on(capability._probe(), "plain") == ("rtc", None)


@needs_glass
@pytest.mark.parametrize("case,backend", [("sequential", "rtc"), ("iris", "rtc"), ("reflective", "rtc"),
                                          ("non_sequential", "zos"), ("mixed", "zos")])
def test_the_test_cases_land_where_the_daemon_puts_them(case, backend, saos_home):
    for verdict in (capability.lens(CASES / f"{case}.zmx"), capability.from_prescription(CASES / f"{case}.json")):
        assert verdict["backend"] == backend, verdict
        assert ("refused" in verdict and bool(verdict["refused"])) == (backend == "zos")
        assert len(verdict.get("refused", [])) <= capability.REFUSED_SHOWN + 1
    raw = json.loads((CASES / f"{case}.json").read_text(encoding="utf-8"))
    assert _built_on(raw)[0] == backend
    if backend == "rtc":
        loaded = RtcBackend(None, standalone=True)
        loaded.load(CASES / f"{case}.zmx")
        assert capability.lens(CASES / f"{case}.zmx")["unreconciled"] == loaded.provenance()["unverified"]


def test_a_merit_row_rtc_lacks_is_named(tmp_path):
    backend = RtcBackend(None, standalone=True)
    backend.build(prescription.normalize(capability._probe()))
    backend.merit_add([{"type": "EFFL", "int1": 0, "int2": 1, "target": 50, "weight": 1}])
    path = tmp_path / "probe.zmx"
    backend.save_as(path)
    # 把存盘的评价函数行换成 rtc 不算的操作数，相当于一份在 OpticStudio 里写了 MTFT 的镜头
    # swap the saved merit row for an operand rtc lacks, as in a lens whose merit function was written in OpticStudio
    text = path.read_bytes().decode("utf-16")
    assert text.count("EFFL") == 1
    path.write_bytes(text.replace("EFFL", "MTFT").encode("utf-16"))
    verdict = capability.lens(path)
    assert verdict["backend"] == "rtc" and verdict["merit_refused"] == ["MTFT"]


def test_the_command_answers_without_a_daemon(capsys, tmp_path):
    assert cli.main(["backends"]) == 0
    result = json.loads(capsys.readouterr().out)["result"]
    assert result["rtc"]["surface_type"]["standard"] == {"status": "reconciled"} and result["always_zos"]["scan_plans"] == ["tolerance"]
    path = tmp_path / "p.json"
    path.write_text(json.dumps(_probe(capability._surface_type("CoordinateBreak"))), encoding="utf-8")
    assert cli.main(["backends", "--prescription", str(path)]) == 0
    verdict = json.loads(capsys.readouterr().out)["result"]
    assert verdict["backend"] == "zos" and "CoordinateBreak" in verdict["refused"][0]
    assert cli.main(["backends", "--lens", str(tmp_path / "missing.zmx")]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == ErrorCode.FILE_NOT_FOUND
    path.write_text("{}", encoding="utf-8")
    assert cli.main(["backends", "--prescription", str(path)]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == ErrorCode.BAD_ARGS


@needs_glass
def test_settings_carried_unread_are_named_only_when_the_lens_changes_them(tmp_path):
    case = CASES / "sequential.zmx"
    assert capability.lens(case)["carried_unread"] == []
    text = case.read_bytes().decode("utf-16")
    assert "POLS 1 0 1 0 0 1 0" in text and text.count("COATING.DAT") == 2
    # 改成偏振入射、换一个膜层文件、加一行读取器不认识的文件头：三项 rtc 都原样带过去不应用。APDX 代表一项还没有存盘样本
    # 定出行名的设置（切趾）/ polarised input, another coating file and a header line the reader does not know: rtc carries
    # all three unread. APDX stands in for a setting whose line no OpticStudio save in the repo shows yet (apodization).
    changed = (text.replace("POLS 1 0 1 0 0 1 0", "POLS 0 1 0 0 0 1 0").replace("COATING.DAT", "MYCOATS.DAT")
               .replace("PWAV 2", "PWAV 2\r\nAPDX 1 1", 1))
    path = tmp_path / "changed.zmx"
    path.write_bytes(changed.encode("utf-16"))
    verdict = capability.lens(path)
    assert verdict["backend"] == "rtc"
    assert verdict["carried_unread"] == ["coating_file", "header line APDX", "polarization"]


SEIDEL = ["ASTI", "COMA", "DIST", "FCUR", "SPHA"]


@pytest.mark.parametrize("path, refused", [(Path(__file__).parent / "rtc" / "fixtures" / "lenses" / f"{name}.zmx", SEIDEL)
                                           for name in ("piA-r012", "piB-r007", "codex-r017")] + [(CASES / "sequential.zmx", [])])
def test_a_lens_verdict_names_the_operands_rtc_refuses_on_it(path, refused):
    # 能力表按探针镜头判；双锥面、环面、理想透镜上 Seidel 各项会被拒，--lens 要当场说出来，measure 实际也拒
    # the table judges on probe lenses; on biconic, toroidal or paraxial surfaces the Seidel terms are refused, and
    # --lens says so, as measure finds
    from saos import measure

    verdict = capability.lens(path)
    assert verdict["backend"] == "rtc" and verdict["operands_refused"] == refused
    for op in SEIDEL:
        item = measure.run(path, [{"operand": {"type": op}}])["items"][0]
        assert (item["trust"] == "refused") == (op in refused), op
