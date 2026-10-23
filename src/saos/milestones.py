"""从已有时间戳汇总需求确认到模型确认、首次报告的耗时。"""
from __future__ import annotations

import json
from datetime import datetime

from saos import demand, model_gate


def elapsed(job) -> dict:
    d = demand.load_job(job.root)
    path = job.root / "confirmations.jsonl"
    records = [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines() if s.strip()] if path.is_file() else []

    def timestamp(value):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (ValueError, AttributeError, TypeError, OverflowError, OSError):
            return None

    current = demand.compute_digest(d)
    confirmations = [timestamp(r.get("time")) for r in records
                     if demand.as_current(r.get("demand_sha"), d, "compute", job.root) == current
                     or demand.as_current(r.get("demand_sha"), d, "confirm", job.root) == demand.digest(d)]
    confirmations = [v for v in confirmations if v is not None]
    start = min(confirmations) if confirmations else None
    models = [timestamp(r.get("time")) for r in model_gate.approvals(job)]
    models = [v for v in models if v is not None and start is not None and v >= start]
    reports = []
    for path in (job.root / "report").glob("*-r*.json"):
        metadata = json.loads(path.read_text(encoding="utf-8"))
        if demand.as_current(metadata.get("demand_sha"), d, "compute", job.root) == current:
            value = timestamp(metadata.get("time"))
            if value is not None and start is not None and value >= start:
                reports.append(value)
    generated = min(reports) if reports else None
    return {"demand_confirmed_at_epoch_s": start,
            "to_first_model_approval_s": min(models) - start if models else None,
            "to_first_report_s": generated - start if generated is not None and start is not None else None,
            "scope": "当前计算需求版本的首个可追溯确认；未发生或缺时间戳为 null"}
