"""优化基准与停机参数校准（原 zea 第 5 步的实验工具，已移出 saos 包，不再推进）。

    uv run python experiments/bench/run.py <split|plan|calibrate|run> …
    uv run pytest experiments/bench
"""

from __future__ import annotations

import sys

import cli as bench_cli

from saos import client
from saos.cli import _emit, _Parser
from saos.protocol import SaosError, fail, ok


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(prog="bench")
    sub = parser.add_subparsers(dest="group", required=True, parser_class=_Parser)
    bench_cli.register(sub, _Parser, client.DEFAULT_REQUEST_TIMEOUT_S)
    args = parser.parse_args(["bench", *(sys.argv[1:] if argv is None else argv)])
    try:
        _emit(ok(args.func(args)))
    except SaosError as exc:
        _emit(fail(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
