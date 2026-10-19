"""由厂家玻璃目录生成只含在产牌号（Standard、Preferred）的目录，写进 OpticStudio 的 Glasscat 文件夹。

用法（仓库根目录）：uv run python library/glasscat/inprod.py [--glasscat DIR]
厂家文件按 manifest.json 的 file 名放在 Glasscat 文件夹里，sha256 不符时拒绝生成。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from saos import glasscat

ROOT = Path(__file__).resolve().parent


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--glasscat", type=Path, default=glasscat.DEFAULT_GLASSCAT_DIR)
    args = parser.parse_args()
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["catalogs"]:
        source = args.glasscat / entry["file"]
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            print(f"{source} 与 manifest.json 记录的 sha256 不符，未生成 {entry['catalog']}", file=sys.stderr)
            return 1
        note = f"{entry['catalog']}: Standard and Preferred glasses of {entry['file']} ({entry['edition']}), made by library/glasscat/inprod.py"
        text, counts = glasscat.in_production(glasscat._decode(raw), note)
        target = args.glasscat / f"{entry['catalog']}.AGF"
        # 与厂家文件一样用带 BOM 的 UTF-16 / UTF-16 with a BOM, like the makers' files
        target.write_bytes(text.encode("utf-16"))
        kept = sum(counts.get(s, 0) for s in glasscat.IN_PRODUCTION)
        print(f"{target.name}: {kept} / {sum(counts.values())} 种，各状态 {counts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
