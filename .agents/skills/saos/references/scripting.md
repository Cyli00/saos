# Scripts, loops and the rtc copy

How to run many evaluations and edit the lens from your own scripts. Command details: `uv run saos exec --help`, `uv run saos scan --help`; how to run them without losing work is in [session.md](session.md).

## Choose where a loop runs

A loop of many evaluations (a scan of a few variables, multi-starts, a sensitivity table, a refocus per point) runs on the backend computing now (`backend_switch` and `job show` name it):

- **On rtc** (every stage before delivery), run it on `ctx` itself ([The rtc instance](#the-rtc-instance)), or on the worker pool for many points at once; `ctx.fast()` is refused there.
- **On OpticStudio**, use the rtc copy (`ctx.fast()`, next section) when the objective is a ray quantity (ray positions and angles, spot statistics, chief ray angle, pass rate) and the copy builds for this lens. It costs no licence and no worker start.
- **The worker pool** when the loop needs operands, analyses or the demand checks, or the copy refuses the lens. `saos scan --plan` covers grids, point lists, `minimize`, `probes`, `candidates` and `tolerance` plans, measures every point with the demand checks, ranks by shortfall and keeps the best lenses in `lenses/scans/NNN/`; prefer a plan to a script. In a script, `ctx.map("fn", items, workers=N)` runs `fn(w, item)` in a worker that reloads a copy of the saved lens before every item (`"explore/x.py:fn"` for a function in another job file); results and the script copy go to `parallel/NNN/`. Workers run the backend the daemon computes on, default four; OpticStudio workers are standalone instances that count against the licence (see [session.md](session.md#preconditions)). They close after 10 idle minutes, when the daemon switches backend, or at once with `ctx.pool_close()`.

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

While the daemon computes on rtc ([session.md](session.md#two-backends-in-one-daemon)), `ctx` itself is rtc and `ctx.fast()` is refused: use `ctx` directly. `lens build` takes the same prescriptions with what rtc can trace: standard, paraxial, even-asphere, biconic and toroidal surfaces, EPD or object-space NA, angle or object-height fields, infinite or finite object, circular apertures, variable flags and marginal-ray-height thickness solves. It lists every other item in one `BACKEND_UNSUPPORTED`. Automatic semi-diameters (except the object surface's, which keeps the file's value) and the solves are recomputed after every change, following OpticStudio's rules; `lens save` writes an OpticStudio `.zmx`.

- `ctx.op` evaluates the ray operands `REAX|Y|Z|R|A|B|C`, `RAGX|Y|Z|A|B|C`, `RANG`, `RAID` (Int1 = surface, Int2 = wavelength, data = Hx, Hy, Px, Py) and `EFFL`, `WFNO`, `ISFN`, `TOTR`, `TTHI`, `EPDI`, `ENPP`, `EXPP`, `EXPD`, `PETZ`, `INDX`, `SSAG`, `CONF`; any other operand is `BACKEND_UNSUPPORTED`, so compute that quantity from rays or leave it to OpticStudio. `ctx.trace`, `ctx.spot`, `ctx.cra`, `ctx.field_profile`, `ctx.set`/`get`/`kept` and `ctx.quick_focus()` (smallest RMS spot over every field and wavelength) work as on OpticStudio. `ctx.merit_add`, `ctx.change_surface_type` (Standard ↔ EvenAspheric) and `ctx.solve` with `fixed` or `variable` work; other solves are refused. The one analysis is `ctx.analysis("FftMtf", {...})`, shaped like OpticStudio's export and not yet reconciled with it. `ctx.huygens` and `ctx.wavefront_rms` are `BACKEND_UNSUPPORTED` until rtc's PSF is reconciled with OpticStudio (ADR-0020), so a check using them is filed under `backend_unsupported`.
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

## Micro-optics measurements

`from saos import micro` holds the layout-free measurements of fibre probes, endoscopes, fibre-tip printed lenses and miniature objectives, lifted from the fibre recipe and three zoom-probe jobs. Build a job's checks from them instead of copying recipe internals: a zoom-probe job that imported a recipe's private helper broke when the recipe changed. Each function's docstring has its exact definition.

- **Validity:** a physically invalid state raises `micro.DesignFailure`; wrap a check in `micro.guarded(fn)` so it returns inf. `micro.once(ctx, key, compute)` is `ctx.memo` that also remembers a failure; put your own namespace first in `key`.
- **Rays and focus:** `micro.trace`, `field_hits(ctx, fields, wave, n)` and `axis_hits` (cached per evaluation), `field_hits_raw` (uncached, after moving the plane), `ray_arrays`, `best_focus_dz(hits)`, `rms_radius(hits, dz)`, `centroid(hits)`.
- **The medium before the image:** `micro.medium(ctx, surface)` returns a `Medium` whose `distance`, `place(ctx, d)` and `depth(ctx, dz)` count the travelled distance, also after a fold (`sign=-1`). `focus_depth(ctx, med, wave)` is the on-axis best-focus depth; positive means a real focus. `marginal_na(ctx, wave, to)` is the smallest n·sin u of a ring of on-axis marginal rays, one of several sample-side NA definitions the jobs used (marginal height over depth, half the angle between opposite rays). Settle which one the demand means before writing it as a check.
- **Parts:** `require_defined(ctx, rows)` (sag at each semi-diameter), `is_coordinate_break`, `sag`, `part_radius`, `transform`, `Axis` and `axis_through(ctx, a, b)` with `radial_extent(ctx, rows, axis)` for the clearance to a tube or housing, and `min_gap(ctx, front)` for the sampled gap between adjacent coaxial surfaces (per configuration inside `ctx.configuration(c)`).
- **Collection:** `collection_fraction(ctx, fields, wave, n)`, the geometric solid angle over 4π without Fresnel loss or absorption.
- **Diffraction:** `psf(hug, field, delta_um)`, `psf_pixel_um` (window about 4 RMS spot diameters), `lateral_widths(p, n_photon)`, `axial_fwhm_um(ctx, hug, med, focus, field, delta_um, step_um, reaches, scan)`, `worst_wavefront(ctx, fields, wave)`.
- **Zoom travel:** `focus_travel(ctx, apply, lo, hi, depth)` sweeps a motion q, with `apply(q)` changing the lens inside your `kit.kept`. It refines both ends and returns `near`, `far`, `span`, `monotonic` and `coverage_ok`. A `span` is only a check value when `coverage_ok` is true.
- **Spare licences:** `micro.pooled(ctx, "saos.<module>:<function>", items)` splits a slow per-field measurement over the worker pool through `ctx.spare_map` when licences are free. It leaves 2 for the user and copies the in-memory lens, so no save is needed. It returns only the finished items; compute the rest yourself. `ctx.spare_map` returns None when it cannot borrow the pool, unlike `ctx.map`, which archives under `parallel/` and needs a saved lens.

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

`ctx.AsphereExpert(surfaces, **options)` and `ctx.GlassExpert(surfaces, **options)` (`saos.ctx.experts`) work on the current backend and on worker contexts, not on rtc copies or in checks; non-sequential, mixed-mode and multi-configuration lenses are `BACKEND_UNSUPPORTED`. When to use them is in [rounds.md](rounds.md#optimisers-in-opticstudio); every option, the algorithms and the limits are in `docs/plans/optical-experts.md` (the rtc parts in ADR-0015).

- `ctx.AsphereExpert([1, 3, 5], terms=4, max_seconds=60).rank()` ranks the surfaces and restores the lens; `.run(nas=2)` converts and optimises the best ones, reranking after each. The residual comes from the merit rows (pass `residual(ctx)` when the positive-weight contributions do not add up to 100 %); `engine="rtc"` ranks on rtc ray errors instead. Other surface types need an `AsphereProfile`, finite slope limits a `constraints(ctx, surface, limit_waves)` provider.
- `ctx.GlassExpert([1, 3], catalogs=["SAOS_CDGM_INPROD"], neighbors=7, max_seconds=120).run()` searches glass by glass; `surfaces="model"` takes every model-glass surface, and `.candidates(surface)` lists candidates without changing the lens. Cost, transmission, weight and expansion filters exist only through your `feasible(ctx, surface)`.
- Neither saves the lens or records a round. Keep a result with `ctx.save_as(<lens name>)` or `saos lens save`, never `system.Save()`: their temporary snapshots move OpticStudio's SystemFile, and `opt hammer|global` refuse until the lens is saved.
