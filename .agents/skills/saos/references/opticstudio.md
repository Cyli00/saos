# OpticStudio stage and on-screen debugging

The outcome of this stage is the accepted design measured, and if needed improved, in OpticStudio, with a final report on those numbers.

## Entering the stage

`uv run saos zos begin --job J --quote "..." [--round N] [--max-rounds K]` runs only after the user accepted the current Optiland report ([delivery.md](delivery.md)). It:

- rebuilds the selected round from its archived prescription, never from an Optiland-written `.zmx`;
- records a `verify` round with the same checks, and writes item, baseline and material comparisons to `stages/zos-result.json`. Differences have no pass threshold; use the OpticStudio numbers;
- lists in `untranslated` the optimisation settings it could not carry over. Recreate each one: variable bounds and merit scripts do not silently become Zemax constraints.

An unavailable licence leaves the entry unrecorded: explain the failure and retry when one is available. Resume an interrupted entry with the same arguments; a committed verify round is not duplicated, and its archived evidence is reused rather than recomputed. Both `stages/zos.json` and `stages/zos-result.json` must be sealed before further rounds.

## Working in OpticStudio

After entry the job stays in OpticStudio across lens loads, builds and daemon restarts. The round budget and comparison window restart at the verify baseline (`--max-rounds` or the demand limit); tiers and stop rules are the same as in [rounds.md](rounds.md). Reports select only this stage's rounds and keep the accepted Optiland conclusion and comparisons.

- `opt local` runs damped least squares (`dls`) or orthogonal descent (`od`, default for non-sequential systems); the Optiland algorithm names are refused here, and `dls`/`od` are refused before the stage.
- `opt hammer --max-seconds S` and `opt global --max-seconds S` never end on their own; they cancel at the budget and leave the best system in the editor. Global search moves its ten best to `lenses/global/NNN/`; Hammer leftovers go to `lenses/hammer/NNN/`.
- Glass substitution (`opt vary --param material --mode substitute`) happens only in Hammer or global search, within one catalog per surface. Without `--catalog` it uses `SAOS_CDGM_INPROD` (CDGM Standard or Preferred glasses; `SAOS_NHG_INPROD` is the same for New Huaguang), and the result names the catalog. `outside_catalog` lists surfaces whose current glass is not in it; pass `--catalog` per surface where the maker must stay.
- Read `materials_changed`. A maker's full catalog often yields discontinued glasses (9 of 12 substitutions on the Cooke sample in SCHOTT were Obsolete); replace an `Obsolete` glass with one in production or ask the user before delivering it.

Regenerate the report once the round decision is final.

## Licences and workers

The licence allows 8 OpticStudio instances at a time, the user's open windows and this daemon included. Count them with `uv run saos daemon status` before choosing `workers`; a refused worker is reported as unavailable. Workers default to four in this stage; a cold start costs about 6–8 s per instance (measured 2026-10-17), so start a pool only for work that outlasts the start-up or will be repeated.

## On-screen debugging (extension mode)

The workflow window is how the user normally watches. When the user also wants the OpticStudio window, start with `uv run saos daemon start --mode extension` (set `SAOS_UI_PID` if several OpticStudio windows are open). The mode only matters after `zos begin`: the Optiland stage never opens OpticStudio, and the OpticStudio stage then attaches to the user's window.

- saos opens Lens Data and a layout window: the cross-section, or the 3D viewer when OpticStudio cannot draw one (`layout.fallback_reason` says why). `lens view --analysis TYPE` adds an analysis worth watching; `--viewer cross-section|3d|shaded` picks another layout. `lens screenshot` captures PNGs.
- On `UI_BLOCKED`, ask the user to close the named dialog.
- Before `lens load`, `unload` or `build` replace the window content, saos saves unsaved user content to `lenses/captures/NNN-unsaved.zmx` (returned as `captured`); tell the user where their work went. `ctx.load` in scripts does the same; call `ctx.capture_window()` first in a script that replaces the window content. Your own unsaved changes are refused until you `lens save` or pass `--discard`.
- Long operations pause window updates and refresh once at the end, because live updates once made an evaluation 4.5 times slower. `exec --live` or `SAOS_UI_LIVE=1` keeps them live when the user wants to watch.
- Open one analysis per measurement and recompute it: in extension mode every new analysis opens and closes a window, and about a hundred in a row crashed OpticStudio.
