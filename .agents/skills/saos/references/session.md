# Sessions, licences and long runs

The daemon holds two backends: rtc, ready at once, and an OpticStudio standalone session started the first time a command needs it and kept until the daemon stops (next section). This file covers what that session needs from the machine and how to run work that takes time without losing it or blocking the user.

## Preconditions

`uv run saos daemon status` shows whether a daemon runs and which job it holds, whether another session is using it, and how many OpticStudio licences are free. Ask the user for whatever is missing in your first reply.

The licence allows 8 OpticStudio instances at a time, the user's open windows and this daemon included; count them before choosing `workers`, and a refused worker is reported as unavailable. Workers default to four and a cold start costs about 6–8 s per instance (measured 2026-10-17), so start a pool only for work that outlasts the start-up or will be repeated. A daemon does not exit by itself when a session or its UI closes, and once its OpticStudio session has started it holds a licence: `uv run saos daemon list --all` shows every daemon on the machine; stop the ones of finished work with `saos daemon stop` in their instance. `saos daemon stop --all` also stops other sessions' work, so it needs the user's agreement.

## Two backends in one daemon

`uv run saos daemon start` runs each command on the backend its job's workflow stage uses: rtc for every stage before delivery, OpticStudio for delivery and always for `round --confirm` and tolerance scans. Details and `daemon start --backend rtc` for a machine without OpticStudio: `uv run saos daemon start --help`.

When a command moves to the other backend, the loaded lens is handed over through its file, so save it first. `handover.agrees: false` lists what rtc and OpticStudio read differently; these stay on the lens as unverified provenance until the re-measurement before delivery, so tell the user when one concerns a demand item. `backend_switch.stayed` means rtc cannot reproduce the lens and OpticStudio keeps computing it.

`saos daemon use zos|rtc` pins a backend for the current workflow stage, for instance for an OpticStudio analysis rtc lacks during the design rounds (`uv run saos daemon use --help`). Pin for a named step and say which item needs it, since every result you get under it costs a licence the stage rule would not.

## Long runs

- **Follow detached work; never resend it** (SKILL.md, global rules). `uv run saos job show J` follows `--detach` runs of `exec`, `opt`, `check` and `round`; `uv run saos scan --job J --show [NNN]` follows a detached scan. Both read files and work while the daemon is busy. Result files, `BUSY`, `POOL_BUSY` and timeouts are in `uv run saos --help`. A 250.6 s computation once looked failed to a 240 s client and was nearly submitted again.
- **Keep runs visible and stoppable.** In a long script, call `ctx.progress(stage, **numbers)` at checkpoints; it returns True after `saos job cancel`, and the script should stop at a safe point and keep what it has.
- **Budget every optimiser.** Give `opt local` a `--max-seconds`; `opt hammer` and `opt global` require one and never end on their own.
- Tell the user what is running, where and how to follow it, and the numbers at each checkpoint. Stop a run when a checkpoint interval gains less than the demand's `rel`.
- Keep working while it computes: read results, prepare the next step, run cheap commands. Before a long piece of work, ask whether its result can still change what you do next; if a pending question could make it obsolete, wait for the answer.

## Use the machine fairly

Leave two logical cores to the user (the optimisers do by default) and start your own long processes at below-normal priority. Times and numbers in notes and messages come from tool output (results, logs, `job show`); label an estimate as one.
