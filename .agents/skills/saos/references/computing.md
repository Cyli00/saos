# Running computations

How to run work that takes time, in parallel or through your own scripts, without losing it or blocking the user. Command details: `uv run saos exec --help`, `uv run saos scan --help`.

## Long runs

- **Detach anything that may outlast your command time limit.** `exec`, `opt local|hammer|global|glass`, `check`, `round` and `zos begin` take `--detach`: the command returns `runs/NNN-<op>.json` at once, and a detached process writes the full response there (`status` `running`, `done` or `failed`). `uv run saos job show J` lists recent runs next to the progress.
- **Scans detach differently.** `saos scan --detach` runs on the worker pool from a snapshot of the saved lens while the daemon keeps serving other commands. Follow it with `uv run saos scan --job J --show [NNN]`, which reads files and works while the daemon is busy. A second scan or `ctx.map` meanwhile gets `POOL_BUSY`; `daemon stop` waits for it (`BUSY`).
- **Never resend running work.** One daemon request runs at a time; any other request fails at once with `BUSY`, naming the running operation. A client timeout only stops waiting; the daemon keeps computing (`still_running`). Wait, read `job show`, or `saos job cancel J`. A 250.6 s computation once looked failed to a 240 s client and was nearly submitted again.
- **Keep runs visible and stoppable.** Long operations write the job's `progress.json` (a detached scan writes `scans/NNN/progress.json`), readable through `job show` while the daemon is busy. In a long script, call `ctx.progress(stage, **numbers)` at checkpoints; it returns True after `saos job cancel`, and the script should stop at a safe point and keep what it has (`ctx.cancel_requested()` checks without reporting). A failure to write `progress.json` is retried, then only logged.
- **Budget every optimiser.** Give `opt local` a `--max-seconds`; `opt hammer` and `opt global` require one and never end on their own. The budget includes initial and final measurements and review; native calls must return before it can stop.
- Tell the user what is running, where and how to follow it, and the numbers at each checkpoint. Stop a run when a checkpoint interval gains less than the demand's `rel`.
- Keep working while it computes: read results, prepare the next step, run cheap commands. Before a long piece of work, ask whether its result can still change what you do next; if a pending question could make it obsolete, wait for the answer.

## Use the machine fairly

Leave two logical cores to the user (the optimisers do by default) and start your own long processes at below-normal priority. Times and numbers in notes and messages come from tool output (results, logs, `job show`), never from memory; label an estimate as one.

## Parallel work

Many independent evaluations (a scan, multi-starts, a sensitivity table) run concurrently on the worker pool:

- `saos scan --plan` covers grids, point lists, `minimize`, `probes`, `candidates` and `tolerance` plans, measures every point with the demand checks, ranks by shortfall and keeps the best lenses in `lenses/scans/NNN/`. Prefer a plan to a script.
- In a script, put the per-point work in a function and run `ctx.map("fn", items, workers=N)` (`"explore/x.py:fn"` for a function in another job file). It runs as `fn(w, item)` in a worker that reloads a copy of the saved lens before every item. Results and the script copy go to `parallel/NNN/`.
- Optiland workers use no licences and default to the CPU count minus two. After `zos begin`, workers are standalone OpticStudio instances, default four, and count against the licence (see [opticstudio.md](opticstudio.md)). Workers stay warm between calls and close after 10 idle minutes; `ctx.pool_close()` frees them at once.

## Scripts

`uv run saos exec SCRIPT --job J` runs `def run(ctx)` inside the daemon and archives the copy and result in `scripts/`. Python may hold control logic (scans, multi-starts, sequencing, bookkeeping) as long as every optical evaluation goes through `ctx`. Reach for built-ins first; scripts you write cost more time than the runs they drive.

Change the lens only through `ctx` (`set`, `kept`, `load`, `save_as`), never through underscore members of `ctx.backend` or the optimiser internals: writing there skips the step gate, the review and the unsaved-lens bookkeeping. If a built-in optimiser is too slow, read its `phases` in `log.jsonl` and tell the user which phase dominates, since that is a saos issue worth fixing. Then search through public routes (`saos.search.minimize`, a `minimize` scan plan, `ctx.map`) rather than a hand-made finite-difference loop, which was rarely faster when tried.

What `ctx` (and a worker `w`) already provides:

- **Parameters:** `get`, `values`, `set` with keys `"<surface index or comment>.<radius|curvature|thickness|conic|semi_diameter|material|comment|par<N>>"`, `aperture`, `field.<n>.x|y`, `wave.<n>`, `config.<configuration>.<row>`. A value a solve overrides raises `VALUE_NOT_APPLIED`.
- **Undo and configurations:** `kept(keys)` and `restored()` undo trials; `with ctx.configuration(c): ...` measures one configuration and returns; `configurations()`.
- **Measuring:** `trace(rays, to)` (numpy arrays); `spot(fields, wave, grid, to, refocus)` (pass rate, chief, centroid, RMS and GEO radius per field, best-focus shift); `op(name, ...)`; `analysis(name, settings)`; `measure(ids)`, the demand's own checks on the current state without writing evidence, which is the right objective for any search; `cra`, `field_profile`, `wave_profile` for extremes over field or spectrum (`kit.profile` is the refinement behind them).
- **Non-sequential systems:** `nsc_trace(...)` (splitting, scattering and polarization on by default; fix `seed` in checks, and treat `dead_ray_errors` as a precondition), `detector(object, grid)`, `ray_hits(zrd, objects, ray_filter)` (a scattering medium writes about 130 kB per ray, so filter and sample), `objects()` and keys `object.<number or comment>.<param>`.
- **Bookkeeping:** `memo`, `lib`, `cache`, `load`, `save_as`, `mark_dirty`, `path`, `args`.

`analysis` returns `status` `ok`, `partial` or `empty` with `problems`, and when not `ok` the head of the text export: read them to tell a model without a result from an interface limit before you rerun anything.

`saos.search` has `grid`, `root` (scan then Brent, skipping invalid points) and `minimize` (Nelder–Mead or Powell with budget and cancel). Per-point work that searches, such as a best-focus solve, uses `search.root` rather than a coarse scan plus bisection; it took 58 % fewer evaluations for the same answer in the measured example.

A reduced model (paraxial surfaces, a coarse grid, one field) states which constraints it drops, and its key prediction is tested on the real lens before you act on it.

## Reading large files

Read a large file by section (find the headings, read the part you need), and read a helper library only when you are about to use or change it.
