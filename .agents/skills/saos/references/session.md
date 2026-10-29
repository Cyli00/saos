# Sessions, licences and long runs

The daemon holds one ZOS standalone session from startup; modelling, screening, rounds and delivery all use it. This file covers what that session needs from the machine and how to run work that takes time without losing it or blocking the user.

## Preconditions

`uv run saos daemon status` shows whether a daemon runs and which job it holds, whether another session is using it, and how many OpticStudio licences are free. Extension mode also needs a visible OpticStudio window. Ask the user for whatever is missing in your first reply.

The licence allows 8 OpticStudio instances at a time, the user's open windows and this daemon included; count them before choosing `workers`, and a refused worker is reported as unavailable. Workers default to four and a cold start costs about 6–8 s per instance (measured 2026-10-17), so start a pool only for work that outlasts the start-up or will be repeated. A daemon does not exit by itself when a session or its UI closes, and each one holds a licence: `uv run saos daemon list --all` shows every daemon on the machine; stop the ones of finished work with `saos daemon stop` in their instance. `saos daemon stop --all` also stops other sessions' work, so it needs the user's agreement.

## Long runs

- **Detach anything that may outlast your command time limit.** `exec`, `opt local|hammer|global`, `check` and `round` take `--detach`: the command returns `runs/NNN-<op>.json` at once, and a detached process writes the full response there (`status` `running`, `done` or `failed`). `uv run saos job show J` lists recent runs next to the progress.
- **Scans detach differently.** `saos scan --detach` runs on the worker pool from a snapshot of the saved lens while the daemon keeps serving other commands. Follow it with `uv run saos scan --job J --show [NNN]`, which reads files and works while the daemon is busy. A second scan or `ctx.map` meanwhile gets `POOL_BUSY`; `daemon stop` waits for it (`BUSY`).
- **Never resend running work.** One daemon request runs at a time; any other request fails at once with `BUSY`, naming the running operation. A client timeout only stops waiting; the daemon keeps computing (`still_running`). Wait, read `job show`, or `saos job cancel J`. A 250.6 s computation once looked failed to a 240 s client and was nearly submitted again.
- **Keep runs visible and stoppable.** Long operations write the job's `progress.json` (a detached scan writes `scans/NNN/progress.json`), readable through `job show` while the daemon is busy. In a long script, call `ctx.progress(stage, **numbers)` at checkpoints; it returns True after `saos job cancel`, and the script should stop at a safe point and keep what it has (`ctx.cancel_requested()` checks without reporting). A failure to write `progress.json` is retried, then only logged.
- **Budget every optimiser.** Give `opt local` a `--max-seconds`; `opt hammer` and `opt global` require one and never end on their own. The budget includes initial and final measurements and review; native calls must return before it can stop.
- Tell the user what is running, where and how to follow it, and the numbers at each checkpoint. Stop a run when a checkpoint interval gains less than the demand's `rel`.
- Keep working while it computes: read results, prepare the next step, run cheap commands. Before a long piece of work, ask whether its result can still change what you do next; if a pending question could make it obsolete, wait for the answer.

## Use the machine fairly

Leave two logical cores to the user (the optimisers do by default) and start your own long processes at below-normal priority. Times and numbers in notes and messages come from tool output (results, logs, `job show`); label an estimate as one.

## On-screen debugging (extension mode)

The workflow window is how the user normally watches. When the user also wants the OpticStudio window, start with `uv run saos daemon start --mode extension` (set `SAOS_UI_PID` if several OpticStudio windows are open). Extension attaches at daemon startup. Standalone workers always remain independent of the user window.

- saos opens Lens Data and a layout window: the cross-section, or the 3D viewer when OpticStudio cannot draw one (`layout.fallback_reason` says why). `lens view --analysis TYPE` adds an analysis worth watching; `--viewer cross-section|3d|shaded` picks another layout. `lens screenshot` captures PNGs.
- On `UI_BLOCKED`, ask the user to close the named dialog.
- Before `lens load`, `unload` or `build` replace the window content, saos saves unsaved user content to `lenses/captures/NNN-unsaved.zmx` (returned as `captured`); tell the user where their work went. `ctx.load` in scripts does the same; call `ctx.capture_window()` first in a script that replaces the window content. Your own unsaved changes are refused until you `lens save` or pass `--discard`.
- Long operations pause window updates and refresh once at the end, because live updates once made an evaluation 4.5 times slower. `exec --live` or `SAOS_UI_LIVE=1` keeps them live when the user wants to watch.
- Open one analysis per measurement and recompute it: in extension mode every new analysis opens and closes a window, and about a hundred in a row crashed OpticStudio.
