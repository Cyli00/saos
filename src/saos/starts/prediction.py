"""汇总 Optiland 自带近轴、三阶和色差结果；不实现另一套像差计算。"""
from __future__ import annotations

import math
from importlib.metadata import version

import numpy as np

from saos import jobs, model_gate
from saos.protocol import jsonable

NAMES = ("TSC", "SC", "CC", "TCC", "TAC", "AC", "TPC", "PC", "DC")
CONVENTIONS = {
    "length": "处方长度单位；库生成处方为 mm",
    "seidel": "Optiland 原生 SI–SV；边缘近轴光线与最大视场主光线，主波长；未换算为 waves",
    "third_order": "逐面长度量；SC=-TSC/u_image，AC=-TAC/u_image，PC=-TPC/u_image，TCC=3*CC",
    "chromatic": "最短减最长波长；单波长沿用后端 F/C 定义，不代表实测带宽色差",
    "petzval": "按原生纵向 PC 的符号，R=h_image^2/(2*sum(PC))；轴上场不能据此求半径",
    "pupils": "入瞳相对首面，出瞳相对像面；光阑位置使用展开光轴坐标",
    "zos_seid": "NOT RUN：未完成 OpticStudio SEID 的符号与归一化实测对照",
}


def predict(backend, *, rings: int = 3, arms: int = 8) -> dict:
    if rings < 1 or arms < 4:
        raise ValueError("入射角采样至少需要 1 环、4 个方位")
    backend._require()
    optic = backend.optic
    desc = backend.describe_model(None)
    out = {"backend": "optiland", "version": version("optiland"), "time": jobs.now_iso(),
           "fingerprint": model_gate.digest(model_gate.fingerprint(desc)),
           "model_sha": model_gate.digest({k: v for k, v in desc.items() if k not in ("file", "layout")}),
           "conventions": CONVENTIONS, "configuration": getattr(backend, "_configuration", 1), "errors": {}}
    from optiland.aberrations import SeidelAberrations, ThirdOrderAberrations, ChromaticAberrations
    from saos.backend.optiland_operands import trace_record

    def scalar(v):
        return float(np.asarray(v).reshape(-1)[0])

    f = None
    try:
        p = optic.paraxial
        positions = np.asarray(optic.surfaces.positions).reshape(-1)
        f = scalar(p.f2())
        out["first_order"] = {"efl": f, "bfl": scalar(p.F2()) + float(backend._rows[-2]["thickness"]),
                              "total_track": float(sum(abs(r.get("thickness", 0.0)) for r in backend._rows[1:-1])),
                              "stop_surface": int(optic.surfaces.stop_index),
                              "stop_z": float(positions[int(optic.surfaces.stop_index)]),
                              "entrance_pupil": scalar(p.EPL()), "exit_pupil": scalar(p.XPL()),
                              "entrance_pupil_diameter": scalar(p.EPD()), "f_number": scalar(p.FNO())}
    except Exception as exc:
        out["errors"]["first_order"] = f"{type(exc).__name__}: {exc}"
    try:
        if any(r.get("decenter_tilt") or r.get("params") or str(r.get("type", "standard")).lower() != "standard" for r in backend._rows[1:-1]):
            raise ValueError("三阶表当前仅用于同轴标准球面/圆锥面；附加面型项和偏心系统需单独核对")
        third = ThirdOrderAberrations(optic)
        values = third.third_order()
        terms = {name: np.asarray(value).reshape(-1) for name, value in zip(NAMES, values)}
        chromatic = ChromaticAberrations(optic)
        terms.update({name: np.asarray(getattr(chromatic, name)()).reshape(-1) for name in ("TAchC", "LchC", "TchC")})
        out["third_order"] = {"surfaces": [{"surface": i + 1, **{k: float(v[i]) for k, v in terms.items()}}
                                            for i in range(len(terms["TSC"]))],
                              "totals": {k: float(v.sum()) for k, v in terms.items()}}
        per_surface = [np.asarray(third._sum_seidels(*(terms[k][i:i+1] for k in ("TSC", "CC", "TAC", "TPC", "DC")))).reshape(-1).tolist()
                       for i in range(len(terms["TSC"]))]
        out["seidel"] = {"columns": ["SI", "SII", "SIII", "SIV", "SV"], "surfaces": per_surface,
                         "totals": np.asarray(SeidelAberrations(optic).seidels()).reshape(-1).tolist()}
        pc, height = float(terms["PC"].sum()), scalar(third._hp)
        radius = height * height / (2 * pc) if pc != 0 and height != 0 else None
        out["petzval"] = {"radius": radius, "radius_over_efl": radius / f if radius is not None and f else None,
                          "reason": None if radius is not None else "轴上场或零曲率；不报告有限半径"}
    except Exception as exc:
        out["errors"]["aberrations"] = f"{type(exc).__name__}: {exc}"
    angles = [[] for _ in backend._rows]
    failures = attempted = 0
    pupil = [(0.0, 0.0)] + [(r / rings * math.cos(a * 2 * math.pi / arms), r / rings * math.sin(a * 2 * math.pi / arms))
                           for r in range(1, rings + 1) for a in range(arms)]
    for wave in range(1, len(backend._effective["wavelengths"]["um"]) + 1):
        for hx, hy in backend._normalized_fields():
            for px, py in pupil:
                attempted += 1
                try:
                    records = trace_record(backend, wave, hx, hy, px, py, len(angles) - 1)
                    valid = len(records) == len(angles) and records[-1]["intensity"] > 0
                    failures += not valid
                    for i, record in enumerate(records[1:], 1):
                        if record["intensity"] > 0 and math.isfinite(record["aoi"]):
                            angles[i].append(record["aoi"])
                except Exception as exc:
                    failures += 1
                    out["errors"].setdefault("incidence", f"{type(exc).__name__}: {exc}")
    out["incidence"] = {"sampling": {"rings": rings, "arms": arms, "fields": "all configured", "waves": "all configured"},
                        "attempted": attempted, "invalid": failures, "continuous_maximum_verified": False,
                        "surfaces": [{"surface": i, "max_deg": max(a) if a else None, "valid_samples": len(a)}
                                     for i, a in enumerate(angles) if i > 0]}
    return jsonable(out)
