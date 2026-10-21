"""真实 OpticStudio 测试的共用夹具：Optiland 搭建 → 建模说明 → 用户认可 → 基线轮次 → 报告 → zos begin。
之后 OpticStudio 会话由测试自己使用，结束时只关闭本测试启动的会话。

Shared fixture for the OpticStudio tests: Optiland build → model notes → user approval → baseline round → report →
zos begin. The live OpticStudio session is then used by the test and closed by it; nothing else is touched.
The legacy tests started the daemon on SAOS_BACKEND=zos and imported a sample .zmx; that entry is refused by design
(OpticStudio only starts through zos begin), so each legacy test now starts from this accepted lens.
"""

from __future__ import annotations

import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import yaml

from saos import demand, jobs, report, stages
from saos.backend.optiland import OptilandBackend
from saos.daemon import Daemon

from test_optiland_flow import LINE, call

# 两片玻璃的样品：第 1 面 N-BK7 为目录玻璃，第 3 面 N-SF2 用于替换与模型玻璃测试
# Two-glass sample: surface 1 is a catalog glass (N-BK7), surface 3 another one (N-SF2) for the substitution tests
TWO_GLASS = {
    "title": "two-glass sample (test fixture)",
    "aperture": {"type": "entrance_pupil_diameter", "value": 10.0},
    "fields": {"type": "angle", "y": [0.0, 5.0]},
    "wavelengths": {"um": [0.5876], "weights": [1.0], "primary": 1},
    "ray_aiming": "real",
    "object": {"thickness": "inf", "comment": "saos:object"},
    "surfaces": [
        {"radius": 50.0, "thickness": 5.0, "material": "N-BK7", "stop": True},
        {"radius": -50.0, "thickness": 2.0, "material": "", "stop": False},
        {"radius": -40.0, "thickness": 2.0, "material": "N-SF2", "stop": False},
        {"radius": -200.0, "thickness": 95.0, "material": "", "stop": False},
    ],
}
NOTES = {"surfaces": {"0": "object", "1": "lens front", "2": "lens back", "3": "second lens front", "4": "second lens back",
                      "5": "image"}, "assumptions": ["test sample"], "first_order": ["focal length as built"]}
DEMAND = {"schema": 2,
          "constants": [{"id": "lens", "description": "the two-glass sample", "value": "two_glass", "source": "user"}],
          "variables": [{"id": "radii", "description": "radii and thicknesses are free"}],
          "objectives": [{"id": "length", "description": "total track", "check": {"operand": {"type": "TOTR"}}, "origin": "user",
                          "must": {"max": 100.0}, "scale": 5.0, "source": "user"}],
          "structure": {"mode": "prescribed", "reason": "测试样品的两片结构由用户指定"},
          "summary": f"用户要总长不超过 100 mm。{LINE}"}


def opticstudio_pids() -> set[int]:
    """本机 OpticStudio 进程号。用于确认测试结束后没有残留。 / PIDs of OpticStudio processes, to confirm nothing is left over."""
    proc = subprocess.run(["tasklist", "/FI", "IMAGENAME eq OpticStudio.exe", "/FO", "CSV", "/NH"], capture_output=True, text=True)
    pids = set()
    for line in proc.stdout.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


@contextmanager
def accepted_session(home: Path, monkeypatch, name: str = "accepted"):
    """产出 (daemon, job, entry)。daemon.backend 已是 OpticStudio 会话；退出时关闭它和 Optiland。
    Yields (daemon, job, entry); daemon.backend is the OpticStudio session. On exit it closes that session and Optiland."""
    monkeypatch.setenv("SAOS_HOME", str(home))
    monkeypatch.setenv("SAOS_BACKEND", "optiland")
    monkeypatch.setenv("SAOS_UI", "0")
    backend = OptilandBackend()
    daemon = Daemon(backend, backend.start(), "standalone")
    before = opticstudio_pids()
    try:
        job = jobs.create_job(name)
        assert call(daemon, "lens.build", job=job.id, prescription=TWO_GLASS, file="two_glass.zmx")["ok"]
        (job.root / "sources" / "demand.yaml").write_text(yaml.safe_dump(DEMAND, allow_unicode=True), encoding="utf-8")
        demand.confirm(job.root, "确认，开始吧")
        folder = call(daemon, "lens.describe", job=job.id, notes=NOTES)["result"]["folder"]
        assert call(daemon, "lens.approve", job=job.id, model=folder, quote="模型可以")["ok"]
        assert call(daemon, "round.record", job=job.id)["ok"]
        report.write(job)
        reply = call(daemon, "zos.begin", job=job.id, quote="可以，接受这份报告，进入 OpticStudio", round=None)
        assert reply["ok"], reply
        assert daemon.backend.system_info()["backend"] == "zos"
        yield daemon, job, stages.read(job.root)
    finally:
        daemon.pool.close()
        if daemon.zos is not None:
            daemon.zos.close()
        daemon.optiland.close()
        # 关闭后进程退出有延迟：等待最多 30 s，仍在的才算残留 / exit is asynchronous: wait up to 30 s before calling it a leftover
        deadline = time.monotonic() + 30
        leftover = opticstudio_pids() - before
        while leftover and time.monotonic() < deadline:
            time.sleep(1)
            leftover = opticstudio_pids() - before
        assert not leftover, f"OpticStudio processes left behind by this test: {sorted(leftover)}"
