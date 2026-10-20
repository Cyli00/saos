"""把 manifest.json 列出的膜层材料 n、k 表追加到 OpticStudio 随附的 COATING.DAT 之后，写成 Coatings 文件夹里的 SAOS_COATING.DAT。

用法（仓库根目录）：uv run python library/coatings/build.py [--coatings DIR]
处方用 "coating_file": "SAOS_COATING.DAT" 选它；原有的膜层和材料照常可用。取 window 内的表点，
虚部按 COATING.DAT 的约定写成 -k（本机文件里吸收材料的虚部为负）。来源文件 sha256 不符时拒绝生成。
OpticStudio 2024 R1 每个 MATE 段只用前 119 点和最后一点，其余不报错地丢掉，在两点间按波长线性插值（2026-10-20 用合成材料实测）。
所以点数超过 MAX_POINTS 时从原表里挑点：不改动任何点的值，只决定留哪些，留下的点线性插值后与被丢点的偏差写在段首注释里。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import yaml

from saos import glasscat

ROOT = Path(__file__).resolve().parent
MAX_POINTS = 120


def table(path: Path) -> list[tuple[float, float, float]]:
    """refractiveindex.info 文件里的 tabulated nk 表：(波长 µm, n, k)。只认这一种数据。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = data.get("DATA") or []
    if len(entries) != 1 or entries[0].get("type") != "tabulated nk":
        raise SystemExit(f"{path}: only a single 'tabulated nk' table is supported")
    rows = []
    for line in str(entries[0]["data"]).splitlines():
        parts = line.split()
        if parts:
            wl, n, k = (float(x) for x in parts[:3])
            rows.append((wl, n, k))
    return rows


def _errors(rows, keep: list[int]) -> tuple[list[float], list[float]]:
    """按 keep 里的点线性插值后，每个原表点的 |Δn|、|Δk|。"""
    dn, dk = [0.0] * len(rows), [0.0] * len(rows)
    for a, b in zip(keep, keep[1:]):
        (w0, n0, k0), (w1, n1, k1) = rows[a], rows[b]
        for i in range(a + 1, b):
            t = (rows[i][0] - w0) / (w1 - w0)
            dn[i] = abs(n0 + t * (n1 - n0) - rows[i][1])
            dk[i] = abs(k0 + t * (k1 - k0) - rows[i][2])
    return dn, dk


def thin(rows, limit: int = MAX_POINTS) -> tuple[list[tuple[float, float, float]], float, float]:
    """从原表挑至多 limit 个点：先留两端，每次补上线性插值偏差最大的那个点。返回挑出的点和最大 |Δn|、|Δk|。
    偏差按 max(|Δn|, |Δk|)/λ 排：膜层相位差和吸收都随 Δ·d/λ 变，短波同样的 Δ 影响更大。"""
    keep = [0, len(rows) - 1] if len(rows) > limit else list(range(len(rows)))
    while True:
        dn, dk = _errors(rows, keep)
        worst = max(range(len(rows)), key=lambda i: max(dn[i], dk[i]) / rows[i][0])
        if len(keep) >= limit or max(dn[worst], dk[worst]) == 0.0:
            return [rows[i] for i in keep], max(dn), max(dk)
        keep = sorted([*keep, worst])


def block(entry: dict, rows: list[tuple[float, float, float]]) -> tuple[str, str]:
    """MATE 段文本和一行摘要。"""
    lo, hi = entry["window"]
    # 先按写出的精度取整，挑点和偏差都对 OpticStudio 实际读到的数
    inside = [tuple(float(f"{v:.6g}") for v in r) for r in rows if lo <= r[0] <= hi]
    if len(inside) < 2:
        raise SystemExit(f"{entry['name']}: fewer than 2 points in {lo}-{hi} um")
    kept, dn, dk = thin(inside)
    head = [f"! {entry['name']}: {entry['process']}; {entry['reference']}",
            f"! source {entry['file']} (refractiveindex.info, CC0), {len(inside)} of {len(rows)} points in {lo}-{hi} um"
            + (f"; {entry['note']}" if entry.get("note") else "")]
    if len(kept) < len(inside):
        head.append(f"! {len(kept)} of those {len(inside)} points kept (OpticStudio reads at most {MAX_POINTS}); "
                    f"linear interpolation misses the dropped points by at most |dn| {dn:.2g}, |dk| {dk:.2g}")
    text = "\r\n".join([*head, f"MATE {entry['name']}", *(f"{wl:.6g} {n:.6g} {-k if k else 0.0:.6g}" for wl, n, k in kept)]) + "\r\n"
    return text, f"{entry['name']}: {len(kept)}/{len(inside)} points, max |dn| {dn:.2g}, |dk| {dk:.2g}"


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--coatings", type=Path, default=glasscat.DEFAULT_GLASSCAT_DIR.parent / "Coatings")
    args = parser.parse_args()
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    base_raw = (args.coatings / manifest["base"]["file"]).read_bytes()
    if hashlib.sha256(base_raw).hexdigest() != manifest["base"]["sha256"]:
        print(f"注意：{manifest['base']['file']} 与 manifest.json 记录的版本不同，仍以它为底", file=sys.stderr)
    blocks = []
    for entry in manifest["materials"]:
        source = ROOT / entry["file"]
        if hashlib.sha256(source.read_bytes()).hexdigest() != entry["sha256"]:
            print(f"{source} 与 manifest.json 记录的 sha256 不符，未生成", file=sys.stderr)
            return 1
        text, summary = block(entry, table(source))
        blocks.append(text)
        print(summary)
    base = glasscat._decode(base_raw).replace("\r\n", "\n").replace("\n", "\r\n").rstrip("\r\n")
    text = base + "\r\n\r\n! ---- saos materials (library/coatings/build.py) ----\r\n" + "\r\n".join(blocks)
    target = args.coatings / manifest["target"]
    # 与 COATING.DAT 一样用带 BOM 的 UTF-16、CRLF / UTF-16 with a BOM and CRLF, like COATING.DAT
    target.write_bytes(text.encode("utf-16"))
    print(f"{target}: {len(blocks)} materials appended to {manifest['base']['file']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
