"""rtc 的操作数记录只算本次请求：前一个请求用过的未对账操作数不记进下一个结果。
The rtc instance counts the operands of the current request only."""

from __future__ import annotations

from saos import jobs, prescription
from saos.backend.rtc import RtcBackend
from saos.daemon import Daemon

# 模型玻璃单透镜，半口径全部固定、物在无穷远、不追光线瞄准：rtc 在它上面不经过任何未对账的路径
# a model-glass singlet with every semi-diameter fixed, the object at infinity and no ray aiming: rtc takes no unreconciled path
CLEAN = {"title": "clean singlet", "aperture": {"type": "entrance_pupil_diameter", "value": 10},
         "fields": {"type": "angle", "y": [0, 3]}, "wavelengths": {"um": [0.5876]}, "ray_aiming": "off",
         "object": {"thickness": "inf"},
         "surfaces": [{"radius": 50, "thickness": 5, "material": {"model": {"nd": 1.5168, "vd": 64.17}}, "stop": True, "semi_diameter": 6},
                      {"radius": -50, "thickness": 47, "semi_diameter": 6}],
         "image": {"semi_diameter": 5}}
# EFFL 没有对账（rtc.RECONCILED_OPERANDS）/ EFFL is not reconciled
EFFL = {"type": "EFFL", "int2": 1}


def call(daemon, op, **args):
    return daemon.handle({"op": op, "args": args})


def _rtc_job(raw=CLEAN):
    rtc = RtcBackend(None, standalone=True)
    daemon = Daemon(rtc, rtc.identity())
    job = jobs.create_job("trust")
    reply = call(daemon, "lens.build", job=job.id, prescription=prescription.normalize(raw), file="a.zmx")
    assert reply["ok"], reply
    return daemon, job


def test_an_operand_used_by_one_request_does_not_mark_the_next(saos_home):
    daemon, job = _rtc_job()
    assert call(daemon, "lens.operand", job=job.id, **EFFL)["ok"]
    source = "def run(ctx):\n    return ctx.op('REAY', 2, 1, 0, 1, 0, 1)\n"
    out = call(daemon, "script.exec", job=job.id, name="ray", source=source)["result"]
    assert out["computed_by"]["operands_unverified"] == []
