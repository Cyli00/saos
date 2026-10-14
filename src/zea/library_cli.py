"""结构库的命令行编排：zea library …。

verify 通过后台进程完成搭建、复现校验和两次验收评估，结果写回结构库条目。
"""

from __future__ import annotations

import argparse
from typing import Any

import yaml

from zea import adapt, client, jobs, library
from zea.protocol import read_json as _read_json


def claimed_class_to_spec(claimed: str | None) -> str:
    text = (claimed or "").lower()
    return "plan_achromat" if any(k in text for k in ("plan", "flat")) else "achromat"


def cmd_add(args: argparse.Namespace) -> Any:
    extract = adapt.upgrade(_read_json(args.extract))
    out = adapt.from_extraction(
        extract, catalogs=args.catalogs, focal_length_mm=args.focal_length, field_number=args.field_number, stop=args.stop
    )
    report = out["report"]
    cover = report.get("cover_glass")
    entry = {
        "id": args.id,
        "status": "extracted",
        "source": extract.get("source"),
        "embodiment": extract.get("embodiment"),
        "metrics": {
            "focal_length_mm": report["focal_length_mm"],
            "na": out["expect"]["na"],
            "field_number": report["field_number"],
            "elements": report["elements"],
            "claimed_class": extract.get("claimed_class"),
            "cover_glass": None
            if not cover
            else {"thickness_mm": float(cover["thickness"]) * report["scale"], "n": float(cover["n"]), "nu": float(cover["nu"])},
            "catalogs": out["prescription"]["catalogs"],
            "glasses": [{k: g[k] for k in ("material", "catalog", "status", "how")} for g in report["glasses"]],
        },
        "assumptions": report["assumptions"],
        "doubts": extract.get("doubts", []),
    }
    library.write_file(args.id, "extract.json", extract)
    library.write_file(args.id, "prescription.json", out["prescription"])
    library.write_file(args.id, "expect.json", out["expect"])
    library.write_file(args.id, "adapt_report.json", report)
    library.save_entry(args.id, entry)
    return {"entry": args.id, "path": str(library.entry_dir(args.id)), "expect": out["expect"], "metrics": entry["metrics"]}


def check_extract(extract: dict[str, Any]) -> dict[str, Any]:
    """入库前核对提取结果：用独立近轴计算比较焦距和前焦距（无限远物镜的工作距离）与原文表头。"""
    from zea import paraxial

    ex = adapt.upgrade(extract)
    rows = adapt.oriented_rows(ex)
    lens = [paraxial.Surface(adapt._num(r["r"]), adapt._num(r.get("d") or 0.0), float(r.get("n", 1.0))) for r in rows]
    efl = paraxial.effective_focal_length(lens)
    ffd = paraxial.front_focal_distance(lens)
    header = ex["header"]
    cover = ex.get("cover_glass")
    if cover:
        scale = adapt.native_focal_length(ex) / float(header["f"]) if cover.get("thickness") is None else 1.0
        cover, _ = adapt.resolve_cover_glass(cover, scale=scale)
    envelope = adapt.rounding_envelope(rows, cover)
    checks = [{"quantity": "f", "printed": header.get("f"), "computed": efl, "envelope": envelope["efl"]}]
    if header.get("WD") is not None:
        # 原文工作距离从盖玻片上表面量起时：前焦距减去盖玻片的视厚度
        wd = ffd - (float(cover["thickness"]) / float(cover["n"]) if cover else 0.0)
        checks.append({"quantity": "WD", "printed": header["WD"], "computed": wd, "envelope": envelope["wd"]})
    for c in checks:
        printed = float(c["printed"]) if c["printed"] else None
        c["rel_error"] = abs(c["computed"] / printed - 1) if printed else None
        lo, hi = c["envelope"]
        # 印刷值落在舍入包络内，或与计算值相差不超过 1%，都算一致
        c["ok"] = printed is not None and (lo <= printed <= hi or c["rel_error"] <= 0.01)
    ok = all(c["ok"] for c in checks)
    return {
        "ok": ok,
        "checks": checks,
        "elements": sum(1 for r in rows if "n" in r),
        "stop_printed": any(r.get("stop") for r in rows),
        "doubts": len(ex.get("doubts") or []),
    }


def cmd_check(args: argparse.Namespace) -> Any:
    return check_extract(_read_json(args.extract))


def _accept(job: jobs.Job, entry_id: str, name: str, spec: dict[str, Any], timeout: float) -> dict[str, Any]:
    spec_rel = f"spec/{entry_id}-{name}.yaml"
    (job.root / spec_rel).write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return client.request("eval.accept", {"job": job.id, "spec": spec_rel}, timeout=timeout)


def cmd_verify(args: argparse.Namespace) -> Any:
    entry = library.load_entry(args.id)
    job = jobs.get_job(args.job)
    d = library.entry_dir(args.id)
    lens_name = f"{args.id}.zmx"
    t = args.timeout
    client.request(
        "lens.build",
        {"job": job.id, "prescription": library.read_json(d / "prescription.json"), "file": lens_name, "overwrite": True, "discard": args.discard},
        timeout=t,
    )
    repro = client.request("lens.repro", {"job": job.id, "expect": library.read_json(d / "expect.json")}, timeout=t)
    library.write_file(args.id, "repro.json", repro)
    entry["repro"] = {"pass": repro["pass"], "metrics": {m["metric"]: round(m["rel_error"], 6) for m in repro["metrics"]}}
    if not repro["pass"]:
        entry["status"] = "rejected"
        entry["reason"] = "复现校验未通过：" + ", ".join(f"{m['metric']} 相对误差 {m['rel_error']:.2%}" for m in repro["metrics"] if not m["pass"])
        library.save_entry(args.id, entry)
        return {"entry": args.id, "status": entry["status"], "reason": entry["reason"]}
    entry["status"] = "admitted"
    entry.pop("reason", None)
    entry["metrics"]["wd_vertex_mm"] = next((m["measured"] for m in repro["metrics"] if m["metric"] == "wd"), None)

    cls = claimed_class_to_spec(entry["metrics"].get("claimed_class"))
    probe = _accept(job, args.id, "probe", library.probe_spec(entry, cls), t)
    library.write_file(args.id, "probe_accept.json", probe)
    if probe["verdict"] == "invalid":
        entry["benchmark"] = {"eligible": False, "reason": f"格式默认规格下无法评估：{probe['invalid']}"}
        library.save_entry(args.id, entry)
        return {"entry": args.id, "status": entry["status"], "benchmark": entry["benchmark"]}
    values = {i["id"]: i["value"] for i in probe["items"]}
    entry["metrics"].update(
        wd_mech_mm=values["working_distance"], optical_length_mm=values["optical_length"], clear_diameter_mm=values["clear_diameter"]
    )
    entry["probe"] = {"class": cls, "items": {i["id"]: {"value": i["value"], "threshold": i["threshold"], "pass": i["pass"]} for i in probe["items"]}}

    derived = library.derive_benchmark_spec(entry, probe, cls)
    bench = {"eligible": derived["eligible"], "class": cls, "failed_fixed_items": derived["failed_fixed_items"], "calibration_evidence": derived["calibration_evidence"]}
    if derived["eligible"]:
        library.write_file(args.id, "benchmark.yaml", derived["spec"])
        check = _accept(job, args.id, "benchmark", derived["spec"], t)
        library.write_file(args.id, "bench_accept.json", check)
        bench["baseline_shortfall"] = check["spec_shortfall"]
        if check["verdict"] != "pass":
            bench["eligible"] = False
            bench["reason"] = f"基准规格下原结构未通过：G = {check['spec_shortfall']}"
    else:
        bench["reason"] = "有依据的判据未通过：" + ", ".join(derived["failed_fixed_items"])
    entry["benchmark"] = bench
    library.save_entry(args.id, entry)
    return {"entry": args.id, "status": entry["status"], "repro": entry["repro"], "benchmark": bench}


def cmd_list(args: argparse.Namespace) -> Any:
    rows = []
    for e in library.all_entries():
        m = e["metrics"]
        rows.append(
            {
                "id": e["id"],
                "status": e["status"],
                "f_mm": round(m["focal_length_mm"], 4),
                "na": m["na"],
                "elements": m["elements"],
                "claimed_class": m.get("claimed_class"),
                "benchmark_eligible": (e.get("benchmark") or {}).get("eligible"),
                "reason": e.get("reason") or (e.get("benchmark") or {}).get("reason"),
            }
        )
    return {"entries": rows}


def cmd_query(args: argparse.Namespace) -> Any:
    return {
        "matches": library.query(
            na=args.na,
            magnification=args.magnification,
            correction_class=args.correction_class,
            objective_field_number=args.field_number,
            working_distance_min=args.working_distance,
            limit=args.limit,
        )
    }


def register(sub, parser_class, default_timeout: float) -> None:
    lib = sub.add_parser("library", help="初始结构库")
    lsub = lib.add_subparsers(dest="action", required=True, parser_class=parser_class)
    p = lsub.add_parser("check", help="入库前用近轴计算核对提取结果的焦距和工作距离（印刷值落在舍入包络内或误差 1%% 以内为通过）")
    p.add_argument("extract")
    p.set_defaults(func=cmd_check)
    p = lsub.add_parser("add", help="把一份提取结果适配后加入结构库（状态 extracted）")
    p.add_argument("id", help="条目编号，如 US4521083-e1")
    p.add_argument("--extract", required=True)
    p.add_argument("--catalogs", nargs="+", default=["OHARA", "SCHOTT", "HOYA", "SUMITA", "NIKON-HIKARI", "CDGM"])
    p.add_argument("--focal-length", type=float)
    p.add_argument("--field-number", type=float)
    p.add_argument("--stop")
    p.set_defaults(func=cmd_add)
    p = lsub.add_parser("verify", help="复现校验并推导基准规格（需要后台进程）")
    p.add_argument("id")
    p.add_argument("--job", required=True, help="用来搭建和评估的设计任务")
    p.add_argument("--discard", action="store_true", help="放弃当前镜头未保存的修改（包括其他任务的镜头）")
    p.add_argument("--timeout", type=float, default=default_timeout)
    p.set_defaults(func=cmd_verify)
    lsub.add_parser("list", help="列出条目").set_defaults(func=cmd_list)
    p = lsub.add_parser("query", help="按光学规格选初始结构")
    p.add_argument("--na", type=float, required=True)
    p.add_argument("--magnification", type=float, required=True)
    p.add_argument("--class", dest="correction_class", default="plan_achromat", choices=("achromat", "plan_achromat"))
    p.add_argument("--field-number", type=float)
    p.add_argument("--working-distance", type=float)
    p.add_argument("--limit", type=int, default=5)
    p.set_defaults(func=cmd_query)
