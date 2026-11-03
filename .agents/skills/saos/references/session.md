# Sessions, licences and long runs

The daemon holds two backends: rtc, ready at once, and an OpticStudio standalone session started the first time a command needs it and kept until the daemon stops (next section). This file covers what that session needs from the machine and how to run work that takes time without losing it or blocking the user.

## Preconditions

`uv run saos daemon status` shows whether a daemon runs and which job it holds, whether another session is using it, and how many OpticStudio licences are free. Ask the user for whatever is missing in your first reply.

The licence allows 8 OpticStudio instances at a time, the user's open windows and this daemon included; count them before choosing `workers`, and a refused worker is reported as unavailable. Workers default to four and a cold start costs about 6–8 s per instance (measured 2026-10-17), so start a pool only for work that outlasts the start-up or will be repeated. A daemon does not exit by itself when a session or its UI closes, and once its OpticStudio session has started it holds a licence: `uv run saos daemon list --all` shows every daemon on the machine; stop the ones of finished work with `saos daemon stop` in their instance. `saos daemon stop --all` also stops other sessions' work, so it needs the user's agreement.

## Two backends in one daemon

`uv run saos daemon start` runs each command on the backend its job's workflow stage uses (ADR-0020): rtc for every stage before delivery (interview, demand confirmation, modelling, model approval, design rounds); OpticStudio for delivery, and always for `lens describe`, `round --confirm` and tolerance scans. rtc needs no licence; its glass catalogs come from `SAOS_GLASSCAT`, default `Documents/Zemax/Glasscat`.

When a command moves to the other backend, the loaded lens is handed over through its file: save it first (`lens save`), or the command stops with `UNSAVED_CHANGES`. The result's `backend_switch` names the backend that took over. `handover.agrees: false` lists what rtc and OpticStudio read differently (`mismatches`, `rays`); these stay on the lens as unverified provenance until the re-measurement before delivery, so tell the user when one concerns a demand item. `backend_switch.stayed` means rtc cannot reproduce the lens and OpticStudio keeps computing it.

`saos daemon use zos|rtc` pins a backend for the current workflow stage, for instance for an OpticStudio analysis rtc lacks during the design rounds; the pin ends by itself when the stage changes, or at once with `saos daemon use auto`. While it holds, every result carries `backend_pin` with `stage_rule`, the backend the stage would use; the result where it ends carries `released`. Pin for a named step and say which item needs it, since every result you get under it costs a licence the stage rule would not. `daemon start --backend rtc` starts an rtc-only daemon for a machine without OpticStudio, which refuses OpticStudio's commands with `BACKEND_UNSUPPORTED`.

## Long runs

- **Follow detached work; never resend it** (SKILL.md, global rules). `uv run saos job show J` follows `--detach` runs of `exec`, `opt`, `check` and `round`; `uv run saos scan --job J --show [NNN]` follows a detached scan. Both read files and work while the daemon is busy. Result files, `BUSY`, `POOL_BUSY` and timeouts are in `uv run saos --help`. A 250.6 s computation once looked failed to a 240 s client and was nearly submitted again.
- **Keep runs visible and stoppable.** Long operations write the job's `progress.json` (a detached scan writes `scans/NNN/progress.json`), readable through `job show` while the daemon is busy. In a long script, call `ctx.progress(stage, **numbers)` at checkpoints; it also refreshes the window's current lens now and then, and returns True after `saos job cancel`, and the script should stop at a safe point and keep what it has (`ctx.cancel_requested()` checks without reporting). A failure to write `progress.json` is retried, then only logged.
- **Budget every optimiser.** Give `opt local` a `--max-seconds`; `opt hammer` and `opt global` require one and never end on their own. The budget includes initial and final measurements and review; native calls must return before it can stop.
- Tell the user what is running, where and how to follow it, and the numbers at each checkpoint. Stop a run when a checkpoint interval gains less than the demand's `rel`.
- Keep working while it computes: read results, prepare the next step, run cheap commands. Before a long piece of work, ask whether its result can still change what you do next; if a pending question could make it obsolete, wait for the answer.

## Use the machine fairly

Leave two logical cores to the user (the optimisers do by default) and start your own long processes at below-normal priority. Times and numbers in notes and messages come from tool output (results, logs, `job show`); label an estimate as one.

## Live catalog metrics

On rtc, the daemon hands a full `.zmx` snapshot of the lens to a dedicated rtc process at optimiser progress callbacks and at the idle refresh after a lens change; the loaded lens keeps its path and unsaved mark. The process uses no worker or licence, keeps only the newest pending snapshot and promises no update per step. It computes the demand's catalog items and the selected extras ([scripting.md](scripting.md#the-metric-catalog)); operand, analysis and script items keep their last formal measurement, and a formal measurement supersedes older live frames. Live numbers are previews, not evidence; each item's `view` names the lens state it belongs to.

`SAOS_LIVE_FRAME_S` (default 10 s) bounds a whole frame: on timeout the process is killed and the error published, and the next snapshot starts afresh; a frame is never retried. `SAOS_LIVE_S=0` or `SAOS_VIEW=0` turns the rtc submissions off. OpticStudio starts no live process and keeps its bounded measurement after each reply (`SAOS_LIVE_S`). Formats: `docs/adr/0022-metric-catalog-and-live-metrics.md`.
