"""从命令行一侧调用 ops 中的函数：组装一段调用脚本，交给后台进程的 script.exec 执行。"""

from __future__ import annotations

from typing import Any

from saos import client

SOURCE = """from saos.recipes.microscope import ops


def run(ctx):
    return ops.{fn}(ctx, **ctx.args)
"""
OPS = ("repro", "accept", "perturb")


def call(fn: str, job: str, timeout: float = client.DEFAULT_REQUEST_TIMEOUT_S, **kwargs: Any) -> Any:
    """返回 ops.<fn> 的结果；脚本副本和结果照常存进任务的 scripts/。验收类操作期间暂停窗口刷新。"""
    if fn not in OPS:
        raise ValueError(f"unknown microscope op {fn!r}")
    payload = {"job": job, "name": f"microscope-{fn.replace('_', '-')}", "source": SOURCE.format(fn=fn), "args": kwargs, "quiet": fn != "perturb"}
    return client.request("script.exec", payload, timeout=timeout)["result"]
