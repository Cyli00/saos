# Scripts and loops

Interfaces live where you can look them up: `uv run saos exec --help` lists the `ctx` topics (`params`, `measure`, `edit`, `rtc`, `fast`, `parallel`, `experts`, ...), and `uv run saos api <module>` gives the Python helpers (`saos.ctx.rows`, `saos.kit`, `saos.search`). Before your first script on rtc, read `uv run saos exec --help rtc`: it lists what rtc computes. This file holds the choices those pages cannot make for you.

## Choose where a loop runs

A loop of many evaluations (a scan of a few variables, multi-starts, a sensitivity table, a refocus per point) runs on the backend computing now (`backend_switch` and `job show` name it):

- **On rtc** (every stage before delivery), run it on `ctx` itself, or on the worker pool for many points at once.
- **On OpticStudio**, use [the rtc copy](#the-rtc-copy) when the objective is a ray quantity (ray positions and angles, spot statistics, chief ray angle, pass rate) and the copy builds for this lens. It costs no licence and no worker start.
- **The worker pool** when the loop needs operands, analyses or the demand checks, or the copy refuses the lens. Prefer a `saos scan --plan` (grids, point lists, `minimize`, `probes`, `candidates`, `tolerance`) to a script: it measures every point with the demand checks, ranks by shortfall and keeps the best lenses. In a script, use `ctx.map` (`uv run saos exec --help parallel`). OpticStudio workers count against the licence ([session.md](session.md#preconditions)).

## Write scripts

- Python may hold control logic (scans, multi-starts, sequencing, bookkeeping) as long as every optical evaluation goes through `ctx`. Reach for built-ins first; scripts you write cost more time than the runs they drive.
- Change the lens only through `ctx`. If a built-in optimiser is too slow, read its `phases` in `log.jsonl` and tell the user which phase dominates, since that is a saos issue worth fixing. Then search through public routes (`saos.search.minimize` on `ctx` or the rtc copy, a `minimize` scan plan, `ctx.map`) rather than a hand-made finite-difference loop, which was rarely faster when tried.
- `ctx.measure(ids)` is the right objective for any search: the demand's own checks, without writing evidence.
- Per-point work that searches, such as a best-focus solve, uses `search.root` rather than a coarse scan plus bisection; it took 58 % fewer evaluations for the same answer in the measured example.
- Build part-edge and gap checks on `ctx.clearances`, `ctx.part_radii` and `ctx.parts_clash`, the edges of the interference check that blocks rounds and delivery, rather than your own definition.
- A reduced model (paraxial surfaces, a coarse grid, one field) states which constraints it drops, and its key prediction is tested on the real lens before you act on it.

## Numbers from rtc

Every `exec` result carries `computed_by`: a number built on an item it lists is an estimate, so say so where you write it. Automatic semi-diameters are one such item: measured against OpticStudio they differ by up to 0.19 mm on rotationally symmetric lenses and 0.70 mm on a toroidal one, so a part-edge limit that close is settled on OpticStudio.

## The rtc copy

While the daemon computes on OpticStudio (delivery, or after `saos daemon use zos`), `ctx.fast()` steers loops on an rtc copy of the lens, checked against OpticStudio when it is built. Interface and example: `uv run saos exec --help fast`.

- What it cannot reproduce, it refuses (`BACKEND_UNSUPPORTED`). Move that step to the worker pool; never substitute an approximate number.
- Solves stay at their saved values. `freeze_solves=True` is fine for a focus solve when the objective refocuses.
- `fast.push(ctx)` writes the result back; OpticStudio re-solves, and the copy never saw the merit function. So a closing `opt local` on the merit function comes before `saos check` or `saos round` judges it.
- A number from the copy is labelled `rtc` wherever you write it and never stands in for a measurement.

## Metrics

`uv run saos metrics list --job J` is the catalog on the current backend, and `uv run saos metrics --help` covers checks, extras and live values. `available` means the computation path exists, not that the lens is valid or the metric reconciled with OpticStudio. `saos metrics get` and `ctx.metric` give numbers, never evidence; `saos metrics select` adds display-only extras that never judge.

## Experts

When to use `ctx.AsphereExpert` and `ctx.GlassExpert` is in [rounds.md](rounds.md#choose-an-expert); options and how to keep the result: `uv run saos exec --help experts`.
