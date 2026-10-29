# Scripts, loops and the rtc copy

How to run many evaluations and edit the lens from your own scripts. Command details: `uv run saos exec --help`, `uv run saos scan --help`; how to run them without losing work is in [session.md](session.md).

## Choose where a loop runs

A loop of many evaluations (a scan of a few variables, multi-starts, a sensitivity table, a refocus per point) runs in one of two places:

- **The rtc copy** (`ctx.fast()`, next section) when the objective is a ray quantity (ray positions and angles, spot statistics, chief ray angle, pass rate) and the copy builds for this lens. It costs no licence and no worker start.
- **The worker pool** when the loop needs operands, analyses or the demand checks, or the copy refuses the lens. `saos scan --plan` covers grids, point lists, `minimize`, `probes`, `candidates` and `tolerance` plans, measures every point with the demand checks, ranks by shortfall and keeps the best lenses in `lenses/scans/NNN/`; prefer a plan to a script. In a script, `ctx.map("fn", items, workers=N)` runs `fn(w, item)` in a worker that reloads a copy of the saved lens before every item (`"explore/x.py:fn"` for a function in another job file); results and the script copy go to `parallel/NNN/`. Workers are standalone OpticStudio instances, default four, and count against the licence (see [session.md](session.md#preconditions)); they close after 10 idle minutes, or at once with `ctx.pool_close()`.

## The rtc copy

`fast = ctx.fast()` (also `w.fast()` in a worker) is an in-memory copy of the current lens, unsaved changes included, traced by rtc in float64 (ADR-0014 holds the design and the measured agreement and speed). It has the kit methods `set`, `get`, `values`, `kept`, `trace`, `spot`, `cra`, `field_profile` and `configuration`, running the same kit code as `ctx`; operands, analyses, `measure` and checks are not there, and check scripts cannot call `ctx.fast()`. rtc traces with a compiled kernel: the first trace after rtc's code changes compiles it (about 6 s, once), later processes load it from cache (about 1 s on the first trace).

```python
from saos import search

def run(ctx):
    fast = ctx.fast(freeze_solves=True)
    steps = {"2.radius": 0.5, "3.radius": 1.0, "3.thickness": 0.2}

    def worst_rms(x):
        fast.set(x)
        rows = fast.spot([(0, 0), (0, 0.7), (0, 1)], wave=[1, 2, 3], grid=15, refocus=True)
        values = [r.get("rms_radius_best") for r in rows]
        return None if None in values else max(values)

    found = search.minimize(worst_rms, fast.values(steps), steps, max_seconds=60, should_stop=ctx.cancel_requested)
    fast.set(found["best"])
    fast.push(ctx)
    return {"built": fast.agreement, "rtc": found["best_f"], "opticstudio": ctx.measure(["spot"])}
```

- **It is checked against OpticStudio when it is built.** `ctx.fast()` first compares every parameter and the index of every medium at every wavelength, then traces the chief ray and a 5×5 pupil at every field and wavelength on both sides; a ray usable on one side only, or a difference beyond 1e-6 mm or 1e-7 in direction, raises `VERIFY_FAILED`. `fast.agreement` keeps that comparison. `fast.compare(ctx, fields, wave, grid)` repeats it for other rays while both sides hold the same parameters (after `push`).
- **What it cannot reproduce, it refuses.** Apertures other than EPD, mirrors, coordinate breaks and tilts/decenters, diffractive and Fresnel surfaces, a finite object distance, paraxial aiming, non-angle fields, field vignetting factors, live multi-configuration operands other than THIC, and surface apertures other than circular/floating raise `BACKEND_UNSUPPORTED`. Materials follow the admission table `src/saos/backend/rtc_admitted.json`: a dispersion formula and environment path listed there is usable everywhere, and any other material only at the wavelengths whose OpticStudio INDX matched when the copy was built. A material change or a new wavelength is checked at every active wavelength before it is applied; a refused change leaves the copy as it was. A glass name missing from the lens's catalogs and an invalid nd/vd are `BAD_ARGS`. On a refusal, move that step to the worker pool; never substitute an approximate number.
- **Solves stay at their saved values.** Pickups, marginal ray height and F-number solves are not recomputed. While the lens has one, changes are refused; `ctx.fast(freeze_solves=True)` keeps their saved values (fine for a focus solve when the objective refocuses, as above), `fast.held` lists them, and writing a held cell raises `VALUE_NOT_APPLIED`.
- **Results go back through `push`.** `fast.push(ctx)` writes every parameter changed through `fast.set` to the OpticStudio lens; OpticStudio then re-solves, and a closing `opt local` on the merit function comes before `saos check` or `saos round` judges it (SKILL.md, measured evidence); until an OpticStudio optimisation completes, their results carry a warning saying so. A number from the copy is labelled `rtc` wherever you write it and never stands in for a measurement.

Material keys are `material`, `nd`, `vd` and `dpgf`. Writing `nd`, `vd` or `dpgf` of a catalog glass turns it into a model glass (ΔPgF 0 for a catalog glass, as on OpticStudio); `with fast.kept(["1.material"]): ...` restores the catalog glass itself. A model glass reads back as `{"nd", "vd", "dpgf"}` on both `ctx` and the copy and can be written back as it is; `fast.push(ctx)` writes nd/vd/dpgf changes of a surface as that whole dictionary, so OpticStudio cannot fill in a value of its own.

## The rtc instance

On a daemon started with `--backend rtc` ([session.md](session.md#the-rtc-instance-stage-1)), `ctx` itself is rtc; `ctx.fast()` is not needed. `lens build` takes the same prescriptions with what rtc can trace: standard, paraxial, even-asphere, biconic and toroidal surfaces, EPD or object-space NA, angle or object-height fields, infinite or finite object, circular apertures, variable flags and marginal-ray-height thickness solves. It lists every other item in one `BACKEND_UNSUPPORTED`. Automatic semi-diameters and the solves are recomputed after every change, following OpticStudio's rules; `lens save` writes an OpticStudio `.zmx`.

- `ctx.op` evaluates the ray operands `REAX|Y|Z|R|A|B|C`, `RAGX|Y|Z|A|B|C`, `RANG`, `RAID` (Int1 = surface, Int2 = wavelength, data = Hx, Hy, Px, Py) and `EFFL`, `WFNO`, `ISFN`, `TOTR`, `TTHI`, `EPDI`, `ENPP`, `EXPP`, `EXPD`, `PETZ`, `INDX`, `SSAG`, `CONF`; any other operand is `BACKEND_UNSUPPORTED`, so compute that quantity from rays or leave it to OpticStudio. `ctx.trace`, `ctx.spot`, `ctx.cra`, `ctx.field_profile`, `ctx.set`/`get`/`kept` and `ctx.quick_focus()` (smallest RMS spot over every field and wavelength) work as on OpticStudio; analyses do not exist.
- Every `exec` result carries `computed_by`: the backend, the operands used that rtc has not yet reconciled with OpticStudio, and the unreconciled paths this lens takes (finite object, object-space NA, object-height field, a material outside the admission table). A number built on a listed item is an estimate: say so where you write it.

## Scripts

`uv run saos exec SCRIPT --job J` runs `def run(ctx)` inside the daemon and archives the copy and result in `scripts/`. Python may hold control logic (scans, multi-starts, sequencing, bookkeeping) as long as every optical evaluation goes through `ctx`. Reach for built-ins first; scripts you write cost more time than the runs they drive.

Change the lens only through `ctx` (`set`, `kept`, `load`, `save_as`), never through underscore members of `ctx.backend` or the optimiser internals: writing there skips the step gate, the review and the unsaved-lens bookkeeping. If a built-in optimiser is too slow, read its `phases` in `log.jsonl` and tell the user which phase dominates, since that is a saos issue worth fixing. Then search through public routes (`saos.search.minimize` on the rtc copy, a `minimize` scan plan, `ctx.map`) rather than a hand-made finite-difference loop, which was rarely faster when tried.

What `ctx` (and a worker `w`) already provides:

- **Parameters:** `get`, `values`, `set` with keys `"<surface index or comment>.<radius|curvature|thickness|conic|semi_diameter|material|comment|par<N>>"`, `aperture`, `field.<n>.x|y`, `wave.<n>`, `config.<configuration>.<row>`. A value a solve overrides raises `VALUE_NOT_APPLIED`.
- **Undo and configurations:** `kept(keys)` and `restored()` undo trials; `with ctx.configuration(c): ...` measures one configuration and returns; `configurations()`.
- **Measuring:** `trace(rays, to)` (numpy arrays); `spot(fields, wave, grid, to, refocus)` (pass rate, chief, centroid, RMS and GEO radius per field, best-focus shift); `op(name, ...)`; `analysis(name, settings)`; `measure(ids)`, the demand's own checks on the current state without writing evidence, which is the right objective for any search; `cra`, `field_profile`, `wave_profile` for extremes over field or spectrum (`kit.profile` is the refinement behind them).
- **Non-sequential systems:** `nsc_trace(...)` (splitting, scattering and polarization on by default; fix `seed` in checks, and treat `dead_ray_errors` as a precondition), `detector(object, grid)`, `ray_hits(zrd, objects, ray_filter)` (a scattering medium writes about 130 kB per ray, so filter and sample), `objects()` and keys `object.<number or comment>.<param>`.
- **Bookkeeping:** `memo`, `lib`, `cache`, `load`, `save_as`, `mark_dirty`, `path`, `args`.

`analysis` returns `status` `ok`, `partial` or `empty` with `problems`, and when not `ok` the head of the text export: read them to tell a model without a result from an interface limit before you rerun anything.

`saos.search` has `grid`, `root` (scan then Brent, skipping invalid points) and `minimize` (Nelder–Mead or Powell with budget and cancel). Per-point work that searches, such as a best-focus solve, uses `search.root` rather than a coarse scan plus bisection; it took 58 % fewer evaluations for the same answer in the measured example.

A reduced model (paraxial surfaces, a coarse grid, one field) states which constraints it drops, and its key prediction is tested on the real lens before you act on it.

## Editing the lens from a script

Merit rows, solves and new surfaces have `ctx` methods; reach for `ctx.system` only for what they do not cover.

- `ctx.merit_add(rows)` inserts rows in the given order before the first `DMFS` (at the end without one) and returns their row numbers. A row has `type`, `params` (or `int1`/`int2`/`data`), `target` and `weight`; `id` and `{"ref": id}` refer to an earlier row of the same batch, so references stay right wherever the batch lands. The batch loads as one merit file and is read back: 1501 rows take about 65 ms instead of 9.8 s row by row, and a value OpticStudio would silently change (a wavelength number beyond the system's, a fraction in an integer column) refuses the batch and leaves the merit function as it was. Rows after the insertion point move down.
- `saos.ctx.rows` builds common constraints: `thickness_bounds(s, minimum=, maximum=)` (CTGT/CTLT); `sag_gap(front, front + 1, [(x, y), ...], minimum=)`, the gap front thickness + back sag − front sag at each point (TTHI, SSAG, DIFF, SUMM, OPGT; it follows a varying thickness, holds only at the given points and only for adjacent coaxial surfaces); `na_proxy(surface, wave, index=, target=, minimum=)`, the RAGA/RAGB direction cosine of the axial marginal ray scaled by a fixed index, a proxy and not the NA check.
- Design-rule constraints (the set DeepLens regularises, written as native operands so DLS and Hammer keep them; every threshold is yours to source, none is a default): `clearances(first, last, glass_center=(min, max), glass_edge=, air_center=, air_edge=)` (MNCG/MXCG, MNEG/MXEG at the semi-diameter, MNCA/MXCA, MNEA/MXEA; each reads its target while met, so only a violation counts); `total_track(minimum=, maximum=)` (TOTR); `distortion(wave, [hy, ...], maximum=, reference_field=1)` (DISG in percent at each sampled field, both signs; field 1 must be on axis, otherwise pass the on-axis field number); `chief_ray_angle(image, wave, [hy, ...], maximum=)` (RAID of the chief ray at the image, degrees); `ray_bend([surfaces], wave, [(hx, hy, px, py), ...], maximum=)` (|RAID − RAED| per refracting surface, degrees). They constrain the sampled points only; the demand checks still judge.
- `ctx.solve("5.thickness", "marginal_ray_height", height=0, zone=0)` sets one cell's solve and returns it read back: `fixed`, `variable`, `pickup(surface, scale=1, offset=0)`, `marginal_ray_height` on thicknesses, `f_number(value)` on radii.
- `ctx.insert_surface(3, "EvenAspheric", radius=-40, thickness=2)` inserts before the image and sets the values as `ctx.set` does; `ctx.change_surface_type(3, "Standard")` changes a type (OpticStudio `SurfaceType` names, any case).

A new surface or type changes the structure, which the round gate compares with the approved model. Check scripts cannot call these methods.

```python
from saos.ctx import rows


def run(ctx):
    ctx.solve("4.thickness", "marginal_ray_height")
    added = ctx.merit_add([
        {"id": "efl", "type": "EFFL", "params": [0, 1], "target": 50, "weight": 1},
        {"type": "OPGT", "params": [{"ref": "efl"}], "target": 45, "weight": 10},
        *rows.thickness_bounds(1, minimum=3, maximum=7),
        *rows.sag_gap(1, 2, [(r, 0) for r in (0, 2, 4, 6)], minimum=0.5, weight=100),
    ])
    return {"rows": added}
```

`exec` adds `hints` to its result and archive when a script reads or writes a surface through `ctx.system.LDE.GetSurfaceAt(n)` that `ctx.get` / `ctx.set` already covers. The check reads only the top level of `run` with constant surface numbers; it never refuses a script.

## Expert search classes

`ctx.AsphereExpert(surfaces, **options)` and `ctx.GlassExpert(surfaces, **options)` construct classes from `saos.ctx.experts`, on the current backend. They also work on worker contexts, not on rtc copies or check contexts. Non-sequential, mixed-mode and multi-configuration systems are refused with `BACKEND_UNSUPPORTED`. When to use them in a design is in [rounds.md](rounds.md#optimisers-in-opticstudio).

- `ctx.AsphereExpert([1, 3, 5], terms=4, max_seconds=60).rank()` restores the lens after finite-difference ranking; `.run(nas=2)` converts and optimizes accepted surfaces, reranking after each one. The residual comes from the lens's merit rows (each row's contribution, so satisfied boundary operands such as OPGT count zero); when the positive-weight contributions do not add up to 100 %, pass `residual(ctx)`. `engine="rtc"` (ADR-0015) ranks in an rtc copy instead: the residual is the transverse ray errors of a `grid`×`grid` pupil (default 7) about the primary-wave chief ray, over every field and wavelength, with the last thickness as a focus compensator. It takes EvenAspheric `par2..par12` only, no `residual` or `slope_vector`, and refuses lenses the rtc copy refuses, as well as candidates before the stop under real ray aiming; `run()` still optimizes and accepts on the merit function in OpticStudio. Default type is `EvenAspheric` (r^4 onward); `AsphereProfile` makes other types and their parameter columns/steps explicit. Finite slope limits require a persistent `constraints(ctx, surface, limit_waves)` provider; a non-default profile also needs a calibrated `slope_vector`. The default slope metric measures departure from the current base conic, without refitting that conic.
- `ctx.GlassExpert([1, 3], catalogs=["SAOS_CDGM_INPROD"], neighbors=7, max_seconds=120).run()` runs the search [rounds.md](rounds.md#optimisers-in-opticstudio) describes, keeping the best state after each surface. `surfaces="model"` selects every model-glass surface, such as the glass codes of a patent start. Partial dispersion is working-band by default; `partial="gF"` uses the makers' P_g,F so anomalous glasses match catalog charts. `.candidates(surface)` restores the lens after reporting candidates; one expert measures a catalog once and reuses it. Glasses whose dispersion path is admitted for rtc get their coordinates from rtc's material layer without touching the lens; the rest are measured with INDX in OpticStudio; `coordinate_sources` reports the split. `feasible(ctx, surface)` supplies actual cost, transmission, weight, expansion and geometric restrictions; without it those filters do not exist. Default material substitution preserves geometry; `substitute` and `element_power` callbacks provide project-specific policies.

Full options and algorithm details are in `docs/plans/optical-experts.md`. The classes return JSON-compatible reports; keep a result with `ctx.save_as(<lens name>)` or `saos lens save`, not native `system.Save()`: temporary full-lens snapshots change ZOS's native SystemFile.
