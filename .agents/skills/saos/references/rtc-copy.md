# The rtc copy

How to steer loops with `ctx.fast()` while the daemon computes on OpticStudio (delivery, or after `saos daemon use zos`). Where loops run otherwise: [scripting.md](scripting.md#choose-where-a-loop-runs).

`fast = ctx.fast()` (also `w.fast()` in a worker) is an in-memory copy of the current lens, unsaved changes included, traced by rtc in float64. It has the kit methods `set`, `get`, `values`, `kept`, `trace`, `spot`, `cra`, `field_profile` and `configuration`, running the same kit code as `ctx`; operands, analyses, `measure` and checks are not there, and check scripts cannot call `ctx.fast()`. rtc traces with a compiled kernel: the first trace after rtc's code changes compiles it (about 6 s, once), later processes load it from cache (about 1 s on the first trace).

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
- **Results go back through `push`.** `fast.push(ctx)` writes every parameter changed through `fast.set` to the OpticStudio lens; OpticStudio then re-solves, and since the copy kept solves frozen and never saw the merit function, a closing `opt local` on the merit function comes before `saos check` or `saos round` judges it; until an OpticStudio optimisation completes, their results carry a warning saying so. A number from the copy is labelled `rtc` wherever you write it and never stands in for a measurement.

Material keys are `material`, `nd`, `vd` and `dpgf`. Writing `nd`, `vd` or `dpgf` of a catalog glass turns it into a model glass (ΔPgF 0 for a catalog glass, as on OpticStudio); `with fast.kept(["1.material"]): ...` restores the catalog glass itself. A model glass reads back as `{"nd", "vd", "dpgf"}` on both `ctx` and the copy and can be written back as it is; `fast.push(ctx)` writes nd/vd/dpgf changes of a surface as that whole dictionary, so OpticStudio cannot fill in a value of its own.
