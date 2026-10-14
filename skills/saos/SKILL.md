---
name: saos
description: Drive Ansys Zemax OpticStudio through the saos tools to take a user's optical design need to a measured, reported result. Use for any optical system design, simulation or explicit (on-screen) debugging task in the saos repository.
---

# saos — simulate any optical system

Run commands from the saos repository root with `uv run saos …`. Every command prints one JSON object (`{"ok": true, "result": …}` or `{"ok": false, "error": {code, message, details}}`). Full command reference: `docs/cli.md`. Terms: `GLOSSARY.md`. Design intent: `DESIGN.md`.

## Three invariants (never break these)

1. **Record the need.** Keep the user's own words and every assumption you fill in, in the job's `sources/`.
2. **Measured evidence.** Every number you deliver comes from an actual OpticStudio computation (operand, analysis export, script) that is archived in the job. Never report an estimate as a result.
3. **No silent relaxing.** If a goal cannot be met, say by how much. Changing the harness requires going back to the user and a new confirmation quote.

Everything else — modelling, starting design, merit functions, variables, strategy — is your call.

## 1. Frame the need (the only mandatory stop)

Create the job first: `uv run saos job create --name <name>`. Then grill the user in rounds: ask every question you can ask now, number them, give your recommended answer for each, and word them so "yes" accepts your recommendation. Explain optics terms in plain words for non-experts. Look up facts yourself (files, references in `ref/docs`, OpticStudio manual) instead of asking.

Write `sources/harness.yaml` (format in `docs/cli.md`, section "需求框定"):

- `request`: the user's words verbatim.
- `targets`: each with `kind` (constraint = must meet; target = has a pass line; preference = no pass line, reported only), a `check` (one Zemax operand, one analysis value via `path`, or a job script returning a number), and a `goal` (`max`, `min`, or `equals` + `tol`) with a `scale` when the bound is 0.
- `assumptions`: everything you filled in.
- `stop`: keep the defaults (rel 0.01, max_rounds 30, all tiers) unless the user wants otherwise; it is part of the need.
- `recipes`: the recipes you plan to load.

Use `uv run saos check --job J` on a loaded lens to sanity-check that every check evaluates. Show the user the harness in plain language and ask for explicit approval. Only an explicit approval counts; record it verbatim as `confirmed.quote`. After that, do not stop to ask again unless the harness turns out to be unreachable or must change.

## 2. Load recipes

If a recipe in `skills/recipes/<system>/SKILL.md` matches the system, load it now. Recipes are optional shortcuts, not rules. If none matches, work with the general tools.

## 3. Design in rounds

- Get a lens into the job: `lens import` (a .zmx), `lens build` (a prescription JSON), or `exec` a script that builds it with ZOS-API and calls `ctx.save_as(...)`.
- General tools: `lens surfaces/set/save/operand`, `opt vary/merit/local`, `lens analysis TYPE --set KEY=VALUE` (numeric export of any OpticStudio analysis), and `exec SCRIPT` for anything else ZOS-API can do (`ctx.system`, `ctx.zosapi`, `ctx.args`, `ctx.load`, `ctx.save_as`, `ctx.mark_dirty`, `ctx.path`). Scripts and results are archived under `scripts/`.
- After each adjustment record a round: `uv run saos round --job J --tier <tier> --note "<what you did>"` (round 0 has no tier). Tiers, low to high: `local` (local optimisation on current variables), `variables` (more variables or a different merit construction/weights), `materials` (glass changes), `global` (global search / Hammer), `structure` (add, split or remove elements, aspheres).
- Read the decision: `achieved` → deliver; `exhausted` → every allowed tier was tried since the last ≥ rel gain, deliver the gap report; `limit` → deliver; `continue` → keep going and escalate to `next_tier` once your current tier stops gaining. Do not judge progress by the merit function; only the shortfall in the ledger is comparable across rounds.
- Local optimisation only finds a local minimum; free variables in batches (curvatures, then spacings, then materials) and keep thickness and edge limits as boundary conditions.

## 4. Deliver

Run `uv run saos report --job J` and give the user the `report/index.html` path, the status, the delivered lens, and for each unmet goal how far it is off. Mention that a tolerance analysis can follow.

## Explicit debugging (on-screen)

When the user wants to watch, start with `uv run saos daemon start --mode extension` (set `SAOS_UI_PID` if several OpticStudio windows are open). saos opens Lens Data plus one image-quality analysis; pick the analysis that matters most for this system with `lens view --analysis TYPE` (for example FftMtf, StandardSpot, WavefrontMap) and optionally `--viewer cross-section|3d|shaded`. Windows refresh after every lens-changing command. Use `lens screenshot` to capture PNGs. If a command returns `UI_BLOCKED`, ask the user to close the named dialog. If the window holds unsaved user work, `lens capture --as NAME` saves a copy before you replace it.

## Job directory

`work/<instance>/jobs/<job>/`: `sources/` (harness, user's words), `lenses/` (lens files; `rounds/rNNN.zmx`), plus tool-written `scripts/`, `analyses/`, `screens/`, `rounds/`, `ledger.jsonl`, `report/`. Put your own notes in `notes/`. Never write requirement documents into the repository's `docs/`.
