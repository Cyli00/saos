# OpticStudio sessions and on-screen debugging

The daemon holds one ZOS standalone session from startup; modelling, screening, rounds and delivery all use it. Optimisers and glass substitution are in [rounds.md](rounds.md#optimisers-in-opticstudio).

## Licences and workers

The licence allows 8 OpticStudio instances at a time, the user's open windows and this daemon included. Count them with `uv run saos daemon status` before choosing `workers`; a refused worker is reported as unavailable. Workers default to four; a cold start costs about 6–8 s per instance (measured 2026-10-17), so start a pool only for work that outlasts the start-up or will be repeated. A daemon does not exit by itself when a session or its UI closes, and each one holds a licence: `uv run saos daemon list --all` shows every daemon on the machine; stop the ones of finished work with `saos daemon stop` in their instance, or all of them with `saos daemon stop --all` once the user agrees.

## On-screen debugging (extension mode)

The workflow window is how the user normally watches. When the user also wants the OpticStudio window, start with `uv run saos daemon start --mode extension` (set `SAOS_UI_PID` if several OpticStudio windows are open). Extension attaches at daemon startup. Standalone workers always remain independent of the user window.

- saos opens Lens Data and a layout window: the cross-section, or the 3D viewer when OpticStudio cannot draw one (`layout.fallback_reason` says why). `lens view --analysis TYPE` adds an analysis worth watching; `--viewer cross-section|3d|shaded` picks another layout. `lens screenshot` captures PNGs.
- On `UI_BLOCKED`, ask the user to close the named dialog.
- Before `lens load`, `unload` or `build` replace the window content, saos saves unsaved user content to `lenses/captures/NNN-unsaved.zmx` (returned as `captured`); tell the user where their work went. `ctx.load` in scripts does the same; call `ctx.capture_window()` first in a script that replaces the window content. Your own unsaved changes are refused until you `lens save` or pass `--discard`.
- Long operations pause window updates and refresh once at the end, because live updates once made an evaluation 4.5 times slower. `exec --live` or `SAOS_UI_LIVE=1` keeps them live when the user wants to watch.
- Open one analysis per measurement and recompute it: in extension mode every new analysis opens and closes a window, and about a hundred in a row crashed OpticStudio.
