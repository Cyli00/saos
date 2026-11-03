"""材料层与 OpticStudio INDX 的回归：fixtures/golden/material-indx（2026-10-28 实测，说明见该目录 README）。
每个 LD 内的点按当时的计算路径重算，差不超过准入标准 1e-8；路径名也要与当时一致（saos 的准入表按路径放行）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtc.material import Catalog, Environment, GlassCatalog, Model, resolve

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "golden" / "material-indx"
CATALOGS = ["SCHOTT", "OHARA", "HOYA", "CDGM", "NHG", "MISC", "INFRARED", "BIREFRINGENT", "LZOS", "HIKARI",
            "MICRO_RESIST_TECHNOLOGY", "DOWSIL_MS"]


def test_material_layer_reproduces_recorded_opticstudio_indx():
    rows = [json.loads(line) for line in (GOLDEN / "indx.jsonl").read_text(encoding="utf-8").splitlines() if line]
    rows = [r for r in rows if not r.get("out_of_range")]
    assert len(rows) == 1677
    directory = GlassCatalog()
    media = {}
    worst = 0.0
    for row in rows:
        key = (row.get("catalog"), row.get("glass")) if "glass" in row else tuple(row["model"])
        if key not in media:
            spec = Catalog(row["glass"]) if "glass" in row else Model(*row["model"])
            # 同名牌号（如 CALCITE）在多个目录里：按采集时样品所在的目录解析
            media[key] = resolve(spec, directory, [row["catalog"]] if row.get("catalog") else CATALOGS)
        e = row["environment"]
        env = Environment(e["temperature"], e["pressure"], e["adjust_index"])
        assert media[key].path(env) == row["path"], row
        got = float(media[key].index(row["wavelength"], env))
        assert got == pytest.approx(row["indx"], rel=0, abs=1e-8), row
        worst = max(worst, abs(got - row["indx"]))
    print(f"\n{len(rows)} points, worst |rtc - INDX| = {worst:.1e}")
