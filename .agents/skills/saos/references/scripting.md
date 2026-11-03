# Scripts, loops and the rtc instance

How to run many evaluations and edit the lens from your own scripts. Command details: `uv run saos exec --help`, `uv run saos scan --help`; how to run them without losing work is in [session.md](session.md).

## Choose where a loop runs

A loop of many evaluations (a scan of a few variables, multi-starts, a sensitivity table, a refocus per point) runs on the backend computing now (`backend_switch` and `job show` name it):

- **On rtc** (every stage before delivery), run it on `ctx` itself ([The rtc instance](#the-rtc-instance)), or on the worker pool for many points at once; `ctx.fast()` is refused there.
- **On OpticStudio**, use the rtc copy (`ctx.fast()`, [rtc-copy.md](rtc-copy.md)) when the objective is a ray quantity (ray positions and angles, spot statistics, chief ray angle, pass rate) and the copy builds for this lens. It costs no licence and no worker start.
- **The worker pool** when the loop needs operands, analyses or the demand checks, or the copy refuses the lens. `saos scan --plan` covers grids, point lists, `minimize`, `probes`, `candidates` and `tolerance` plans, measures every point with the demand checks, ranks by shortfall and keeps the best lenses in `lenses/scans/NNN/`; prefer a plan to a script. In a script, `ctx.map("fn", items, workers=N)` runs `fn(w, item)` in a worker that reloads a copy of the saved lens before every item (`"explore/x.py:fn"` for a function in another job file); results and the script copy go to `parallel/NNN/`. Workers run the backend the daemon computes on, default four; OpticStudio workers are standalone instances that count against the licence (see [session.md](session.md#preconditions)). They close after 10 idle minutes, when the daemon switches backend, or at once with `ctx.pool_close()`.

## The rtc instance

While the daemon computes on rtc ([session.md](session.md#two-backends-in-one-daemon)), `ctx` itself is rtc and `ctx.fast()` is refused: use `ctx` directly. `lens build` takes the same prescriptions with what rtc can trace: standard, paraxial, even-asphere, biconic and toroidal surfaces, EPD or object-space NA, angle or object-height fields, infinite or finite object, circular apertures, variable flags and marginal-ray-height thickness solves. It lists every other item in one `BACKEND_UNSUPPORTED`. Automatic semi-diameters (except the object surface's, which keeps the file's value) and the solves are recomputed after every change, following OpticStudio's rules; `lens save` writes an OpticStudio `.zmx`.

- `ctx.op` evaluates the ray operands `REAX|Y|Z|R|A|B|C`, `RAGX|Y|Z|A|B|C`, `RANG`, `RAID` (Int1 = surface, Int2 = wavelength, data = Hx, Hy, Px, Py) and `EFFL`, `WFNO`, `ISFN`, `TOTR`, `TTHI`, `EPDI`, `ENPP`, `EXPP`, `EXPD`, `PETZ`, `INDX`, `SSAG`, `CONF`; any other operand is `BACKEND_UNSUPPORTED`, so compute that quantity from rays or leave it to OpticStudio. `ctx.trace`, `ctx.spot`, `ctx.cra`, `ctx.field_profile`, `ctx.set`/`get`/`kept` and `ctx.quick_focus()` (smallest RMS spot over every field and wavelength) work as on OpticStudio. `ctx.merit_add`, `ctx.change_surface_type` (Standard ↔ EvenAspheric) and `ctx.solve` with `fixed` or `variable` work; other solves are refused. The one analysis is `ctx.analysis("FftMtf", {...})`, shaped like OpticStudio's export and not yet reconciled with it. `ctx.huygens` and `ctx.wavefront_rms` are `BACKEND_UNSUPPORTED` until rtc's PSF is reconciled with OpticStudio, so a check using them is filed under `backend_unsupported`.
- Every `exec` result carries `computed_by`: the backend, the operands used that rtc has not yet reconciled with OpticStudio, and the unreconciled paths this lens takes (finite object, object-space NA, object-height field, a material outside the admission table). A number built on a listed item is an estimate: say so where you write it.

## Scripts

`uv run saos exec SCRIPT --job J` runs `def run(ctx)` inside the daemon and archives the copy and result in `scripts/`. Python may hold control logic (scans, multi-starts, sequencing, bookkeeping) as long as every optical evaluation goes through `ctx`. Reach for built-ins first; scripts you write cost more time than the runs they drive.

Change the lens only through `ctx` (`set`, `kept`, `load`, `save_as`), never through underscore members of `ctx.backend` or the optimiser internals: writing there skips the step gate, the review and the unsaved-lens bookkeeping. If a built-in optimiser is too slow, read its `phases` in `log.jsonl` and tell the user which phase dominates, since that is a saos issue worth fixing. Then search through public routes (`saos.search.minimize` on `ctx` or the rtc copy, a `minimize` scan plan, `ctx.map`) rather than a hand-made finite-difference loop, which was rarely faster when tried.

What `ctx` (and a worker `w`) already provides:

- **Parameters:** `get`, `values`, `set` with keys `"<surface index or comment>.<radius|curvature|thickness|conic|semi_diameter|material|comment|par<N>>"`, `aperture`, `field.<n>.x|y`, `wave.<n>`, `config.<configuration>.<row>`. A value a solve overrides raises `VALUE_NOT_APPLIED`. `ctx.surface_params(surface)` lists the surface type's parameter columns (`par`, header `name`, `value`, `read_only`); `ctx.info()` returns the system settings (on OpticStudio with the catalogs in use, `material_catalogs`).
- **Undo and configurations:** `kept(keys)` and `restored()` undo trials; `with ctx.configuration(c): ...` measures one configuration and returns; `configurations()`.
- **Measuring:** `trace(rays, to)` (numpy arrays); `spot(fields, wave, grid, to, refocus)` (pass rate, chief, centroid, RMS and GEO radius per field, best-focus shift); `op(name, ...)`; `analysis(name, settings)`; `measure(ids)`, the demand's own checks on the current state without writing evidence, which is the right objective for any search; `cra`, `field_profile`, `wave_profile` for extremes over field or spectrum (`kit.profile` is the refinement behind them).
- **Diffraction PSF and wavefront** (microscopes, focusing probes): inside `with ctx.huygens(wave, pupil=64, image=128) as h:`, `h.psf(field, delta_um)` returns a `kit.Psf` (unnormalised intensities, pixel pitch in µm); `field` is a field number or `(x, y)` in the lens's field units, added for the session and removed after. Do a focus scan or a field sweep inside one session, moving the plane with `ctx.set` under `ctx.kept`: each new session opens an analysis window on OpticStudio, and a few hundred crashed it. `kit.psf_widths(p, n_photon=1, ee=0.8)` gives the FWHM across the row and column through the peak and the encircled-energy diameter about the centroid; `kit.psf_axial(peak_at, step_um, reaches, n_photon=1)` gives the FWHM of the peak-versus-defocus curve and the offset of its peak, widening to the next reach when a half maximum falls outside; `scan="outward"` grows from focus only until both sides fall below half (about a quarter of the planes for a single peak) but cannot see a higher peak farther out, so compare it once with the default window scan on the lens before relying on it. `n_photon=2` is two-photon excitation (intensity squared). `ctx.wavefront_rms(field, wave)` is the ray-integrated RMS wavefront error to centroid, in waves (a list of fields returns a list from one OpticStudio analysis, so pass them together); `ctx.aperture(surface)` reads a surface aperture.
- **Non-sequential systems:** `nsc_trace(...)` (splitting, scattering and polarization on by default; fix `seed` in checks, and treat `dead_ray_errors` as a precondition), `detector(object, grid)`, `ray_hits(zrd, objects, ray_filter)` (a scattering medium writes about 130 kB per ray, so filter and sample), `objects()` and keys `object.<number or comment>.<param>`.
- **Bookkeeping:** `memo`, `lib`, `cache`, `load`, `save_as`, `mark_dirty`, `path`, `args`.

`analysis` returns `status` `ok`, `partial` or `empty` with `problems`, and when not `ok` the head of the text export: read them to tell a model without a result from an interface limit before you rerun anything.

## Search helpers

`saos.search` has `grid`, `root` (scan then Brent, skipping invalid points) and `minimize` (Nelder–Mead or Powell with budget and cancel). Per-point work that searches, such as a best-focus solve, uses `search.root` rather than a coarse scan plus bisection; it took 58 % fewer evaluations for the same answer in the measured example.

A reduced model (paraxial surfaces, a coarse grid, one field) states which constraints it drops, and its key prediction is tested on the real lens before you act on it.

## The metric catalog

`uv run saos metrics list --job J` lists the catalog on the current backend: parameters with their bounds, the scalars a check may pick, and units. `available` means the computation path exists, not that the lens is valid or the metric reconciled with OpticStudio. `saos metrics get <id> --job J --param KEY=JSON` and `ctx.metric(id, **params)` (workers too; `ctx.metrics()` returns the catalog) give scalars, curves or a table, never evidence.

A demand item checks a metric with `check: {metric: {id, params, pick}}`. `pick` defaults to `value` and takes only a scalar the catalog declares. An MTF check needs `params.frequency` (cycles/mm) and takes the minimum over the chosen fields' curves, the diffraction limit excluded. A check or round archives the full result, and the catalog code counts toward the check version, like `saos.kit`.

`saos metrics select --job J --file PATH` saves display-only extras for the window, e.g. `[{"key": "watch_mtf", "id": "mtf", "params": {"frequency": 50}}]`; `[]` clears them, and a key may not reuse a demand item id. Extras never judge. How the window computes them during optimisation: [session.md](session.md#live-catalog-metrics).

`field_focus` is the geometric RMS best-focus curve, not tangential or sagittal field curvature and not a diffraction depth of focus.
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

`ctx.AsphereExpert(surfaces, **options)` and `ctx.GlassExpert(surfaces, **options)` (`saos.ctx.experts`) work on the current backend and on worker contexts, not on rtc copies or in checks; non-sequential, mixed-mode and multi-configuration lenses are `BACKEND_UNSUPPORTED`. When to use them is in [rounds.md](rounds.md#optimisers-in-opticstudio); every option, the algorithms and the limits are in `docs/plans/optical-experts.md`.

- `ctx.AsphereExpert([1, 3, 5], terms=4, max_seconds=60).rank()` ranks the surfaces and restores the lens; `.run(nas=2)` converts and optimises the best ones, reranking after each. The residual comes from the merit rows (pass `residual(ctx)` when the positive-weight contributions do not add up to 100 %); `engine="rtc"` ranks on rtc ray errors instead. Other surface types need an `AsphereProfile`, finite slope limits a `constraints(ctx, surface, limit_waves)` provider.
- `ctx.GlassExpert([1, 3], catalogs=["SAOS_CDGM_INPROD"], neighbors=7, max_seconds=120).run()` searches glass by glass; `surfaces="model"` takes every model-glass surface, and `.candidates(surface)` lists candidates without changing the lens. Cost, transmission, weight and expansion filters exist only through your `feasible(ctx, surface)`.
- Neither saves the lens or records a round. Keep a result with `ctx.save_as(<lens name>)` or `saos lens save`, never `system.Save()`: their temporary snapshots move OpticStudio's SystemFile, and `opt hammer|global` refuse until the lens is saved.
