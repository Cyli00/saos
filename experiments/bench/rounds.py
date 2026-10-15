"""优化基准自己的调整轮次：记录、验收配置冻结与停机判断（原显微物镜配方的 saos microscope round，比较规则见
docs/recipes/microscope/optical-spec-and-acceptance.md 4.2 节）。通用工作流的轮次见 saos round，这里只供基准实验使用。

每个基准任务一份 rounds.jsonl：第 0 轮是初始结构的验收评估，之后每轮一条。runner 把本文件作为 saos exec 脚本
送进后台进程执行（run(ctx) 调用 record_round），calibration 直接导入 decide / significant 离线回放。
"""

from __future__ import annotations

import json
import math
from typing import Any

from saos import jobs
from saos.protocol import ErrorCode, SaosError, jsonable

# 停机参数为初始值，未校准：第 5 步正式批次的调参组样本全部 1–2 轮通过，无法区分候选（docs/recipes/microscope/step5-calibration.md）
STAGNATION_ROUNDS = 3
STAGNATION_REL = 0.01
MAX_ROUNDS = 20


def significant(g_best: float, g_k: float, rel: float = STAGNATION_REL) -> bool:
    """g_k 相对此前最好值 g_best 是否明显缩小（至少 rel）。"""
    if math.isinf(g_k):
        return False
    if math.isinf(g_best):
        return True
    return (g_best - g_k) / g_best >= rel


def decide(history: list[float], n: int = STAGNATION_ROUNDS, rel: float = STAGNATION_REL, cap: int = MAX_ROUNDS) -> dict[str, Any]:
    """history 是第 0 轮起各轮的规格差距 G。返回当前状态：pass / stagnated / limit / continue。

    n、rel、cap 只在离线回放比较停机参数时改，实际任务用模块常量。"""
    if not history:
        raise ValueError("at least round 0 is needed")
    g_best = history[0]
    flat = 0
    for g in history[1:]:
        if g == 0:
            break
        flat = 0 if significant(g_best, g, rel) else flat + 1
        g_best = min(g_best, g)
    rounds = len(history) - 1
    current = history[-1]
    if current == 0:
        status = "pass"
    elif flat >= n:
        status = "stagnated"
    elif rounds >= cap:
        status = "limit"
    else:
        status = "continue"
    return {"status": status, "rounds": rounds, "g_best": min(history), "rounds_without_progress": flat}


def freeze(job, spec_name: str, config: dict[str, Any], digest: str) -> None:
    """第 0 轮：保存完整验收配置，并在 job.json 记录规格路径和配置哈希。"""
    path = job.spec_dir / "frozen_config.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    job.update_state(spec=spec_name, frozen={"spec": spec_name, "config": "spec/frozen_config.json", "sha256": digest})


def check_frozen(job, spec_name: str, config: dict[str, Any], digest: str) -> None:
    """之后各轮：规格路径和补全后的验收配置都必须与第 0 轮一致。"""
    frozen = job.read_state().get("frozen")
    if not frozen:
        raise SaosError(ErrorCode.SPEC_CHANGED, "the job has rounds but no frozen acceptance config; create a new job")
    if spec_name != frozen["spec"]:
        raise SaosError(ErrorCode.SPEC_CHANGED, f"the optical spec is frozen as {frozen['spec']} and cannot change to {spec_name}", {"frozen": frozen})
    if digest != frozen["sha256"]:
        old = json.loads((job.root / frozen["config"]).read_text(encoding="utf-8"))
        changed = sorted(k for k in set(old) | set(config) if old.get(k) != config.get(k))
        raise SaosError(
            ErrorCode.SPEC_CHANGED,
            "the completed acceptance config differs from the one frozen at round 0, so shortfalls are no longer comparable; create a new (or derived) job",
            {"changed_keys": changed, "frozen_sha256": frozen["sha256"], "current_sha256": digest},
        )


def load(path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def record_round(ctx, spec: str | None = None, note: str = "", observe: bool = False) -> dict[str, Any]:
    """记录一个调整轮次：把当前镜头存为 rounds/rNNN.zmx，做验收评估，给出是否停机。

    observe=True 用于校准停机规则：照常冻结并核对验收配置，但停机判断只记录、不执行，轨迹可以走到停机之后。"""
    from saos.recipes.microscope import acceptance, ops

    ops._lens(ctx)
    job = ctx.job
    log_path = job.root / "rounds.jsonl"
    history = load(log_path)
    frozen = job.read_state().get("frozen") if history else None
    spec_name = spec or (frozen or {}).get("spec")
    if not spec_name:
        raise SaosError(ErrorCode.BAD_ARGS, "round 0 needs a spec; later rounds reuse it")
    spec_obj = ops._job_spec(job, spec_name)
    config = acceptance.acceptance_config(spec_obj)
    digest = acceptance.config_hash(config)
    if history:
        # 每轮都按当前规格文件重新补全，与第 0 轮冻结的完整验收配置逐项比较
        check_frozen(job, spec_name, config, digest)
        status = decide([float(h["spec_shortfall"]) for h in history])["status"]
        if status != "continue" and not observe:
            raise SaosError(ErrorCode.BAD_ARGS, f"rounds already stopped ({status}); cannot record more", {"status": status})
    else:
        freeze(job, spec_name, config, digest)
    number = len(history)
    merit = ctx.backend.merit_value()
    # 归档目录只由工具写入；归档后回到工作文件，之后的 lens save 不会改写归档
    # only the tools write the archive; switch back to the working file so a later lens save cannot rewrite it
    working = ctx.lens
    rel = ctx.save_as(f"rounds/r{number:03d}.zmx", overwrite=True, archive=True)
    if working is not None and not job.is_archive_lens(working):
        ctx.save_as(job.relative_lens(working), overwrite=True)
    result = ops._accept(ctx, spec_name, spec_obj)
    record = {
        "round": number,
        "file": rel,
        "verdict": result["verdict"],
        "spec_shortfall": result["spec_shortfall"],
        "items": {i["id"]: i["shortfall"] for i in result["items"] if i["shortfall"] > 0},
        "merit": merit,
        "note": str(note),
        "observe": bool(observe),
        "config_sha256": digest,
        "time": jobs.now_iso(),
    }
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(jsonable(record), ensure_ascii=False) + "\n")
    decision = decide([float(h["spec_shortfall"]) for h in history] + [float(result["spec_shortfall"])])
    job.update_state(current_lens=rel, last_saved=rel, round=number, round_status=decision["status"])
    return {"record": record, "decision": decision, "evaluation": result}


def run(ctx):
    return record_round(ctx, **ctx.args)
