"""显微物镜配方的后台操作：复现校验、验收评估、受控扰动。

原来是后台进程的 lens.repro、eval.accept、lens.perturb，逻辑不变，改为在脚本执行中调用：

    from saos.recipes.microscope import ops

    def run(ctx):
        return ops.accept(ctx, spec="spec/benchmark.yaml")
"""

from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Any

from saos import jobs
from saos.protocol import ErrorCode, SaosError, jsonable, write_json
from saos.recipes.microscope import acceptance, layout
from saos.recipes.microscope.spec import Spec, load_spec


def _lens(ctx) -> Path:
    if ctx.lens is None:
        raise SaosError(ErrorCode.NO_LENS_LOADED, f"job {ctx.job.id} has no lens loaded; run lens load first")
    return ctx.lens


def _job_spec(job: jobs.Job, spec_name: str) -> Spec:
    spec_path = (job.root / spec_name).resolve()
    if job.root.resolve() not in spec_path.parents or not spec_path.is_file():
        raise SaosError(ErrorCode.PATH_OUTSIDE_JOB, f"spec file must be inside the job directory: {spec_name}")
    return load_spec(spec_path)


def _saved_lens_in_window(ctx, lens: Path) -> dict[str, Any]:
    """验收类操作会在窗口里重建或重新加载镜头：窗口有未保存内容时先存副本，再从文件加载，保证测的是已保存的镜头。"""
    captured = ctx.capture_window()
    if captured:
        ctx.backend.load(lens)
        return {"captured": captured}
    return {}


def repro(ctx, expect: dict[str, Any]) -> dict[str, Any]:
    """复现校验：焦距、数值孔径、工作距离、放大率与期望值比较。"""
    lens = _lens(ctx)
    backend = ctx.backend
    captured = {} if ctx.dirty else _saved_lens_in_window(ctx, lens)
    design = backend.snapshot()
    gap = layout.gap_surface(design)
    original = design[gap]["thickness"]
    primary = next(w["um"] for w in backend.system_info()["wavelengths"] if w["primary"])
    try:
        result = acceptance.reproduction_check(backend, design, expect, primary)
    except acceptance.InvalidEvaluation as exc:
        raise SaosError(ErrorCode.ZOS_ERROR, f"reproduction check failed: {exc.reason}", exc.details) from exc
    finally:
        # 复现校验只临时调焦，结束后恢复。未修改的镜头直接重新加载文件：
        # 实测调焦用的 ZERN 和写回原厚度都会让 OpticStudio 的 NeedsSave 置位，extension 模式下会误报未保存修改
        if ctx.dirty:
            backend.set_surface(gap, "thickness", original)
        else:
            backend.load(lens)
    return {**ctx.describe(), **result, **captured}


def accept(ctx, spec: str | None = None) -> dict[str, Any]:
    """按光学规格对当前已保存的镜头做验收评估。"""
    _lens(ctx)
    if ctx.dirty:
        raise SaosError(ErrorCode.UNSAVED_CHANGES, "acceptance evaluates the saved lens file; save first", ctx.describe())
    spec_name = spec or ctx.job.read_state().get("spec")
    if not spec_name:
        raise SaosError(ErrorCode.BAD_ARGS, f"job {ctx.job.id} has no optical spec; pass spec or set it at job create")
    return _accept(ctx, spec_name, _job_spec(ctx.job, spec_name))


def _accept(ctx, spec_name: str, spec: Spec) -> dict[str, Any]:
    backend, job, lens = ctx.backend, ctx.job, _lens(ctx)
    captured = _saved_lens_in_window(ctx, lens)
    design = backend.snapshot()
    info = backend.system_info()
    evaluator = acceptance.Evaluator(backend, spec, info["material_catalogs"], telecentric_object=info["aperture"].get("telecentric_object", False))
    try:
        result = evaluator.run(design)
    finally:
        eval_dir = job.root / "eval"
        eval_dir.mkdir(exist_ok=True)
        model_path: Path | None = eval_dir / f"{lens.stem}.accept.zmx"
        try:
            backend.save_as(model_path)
        except Exception:  # noqa: BLE001 - 搭建失败时可能没有可保存的模型
            model_path = None
        backend.load(lens)
    rel = job.relative_lens(lens)
    result = {"job": job.id, "file": rel, "spec": spec_name, **result, **captured}
    if model_path is not None:
        result["acceptance_model"] = model_path.relative_to(job.root).as_posix()
    write_json(job.root / "eval" / f"{lens.stem}.accept.json", result)
    job.update_state(last_evaluation={"file": rel, "verdict": result["verdict"], "spec_shortfall": jsonable(result["spec_shortfall"])})
    return result


def perturb(ctx, seed: int, radius_rel: float, thickness_rel: float = 0.0) -> dict[str, Any]:
    """受控扰动（固定随机种子）：物镜各面曲率按相对量、各面厚度按相对量均匀随机改变。"""
    _lens(ctx)
    backend = ctx.backend
    rows = backend.snapshot()
    first, last = layout.objective_block(rows)
    rng = random.Random(int(seed))
    changes = []
    for r in rows[first : last + 1]:
        if r["type"] != "standard" or r["comment"] == layout.TAG_STOP:
            continue
        i = r["index"]
        if not math.isinf(r["radius"]):
            new_r = 1.0 / ((1.0 / r["radius"]) * (1.0 + rng.uniform(-radius_rel, radius_rel)))
            backend.set_surface(i, "radius", new_r)
            changes.append({"surface": i, "param": "radius", "before": r["radius"], "after": new_r})
        if i < last and thickness_rel > 0:
            new_t = r["thickness"] * (1.0 + rng.uniform(-thickness_rel, thickness_rel))
            backend.set_surface(i, "thickness", new_t)
            changes.append({"surface": i, "param": "thickness", "before": r["thickness"], "after": new_t})
    ctx.mark_dirty()
    return {**ctx.describe(), "seed": seed, "radius_rel": radius_rel, "thickness_rel": thickness_rel, "changes": changes}
