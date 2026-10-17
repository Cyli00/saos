---
name: saos
description: Drive Ansys Zemax OpticStudio through the saos tools to take a user's optical design need to a measured, reported result. Use for any optical system design, simulation or explicit (on-screen) debugging task in the saos repository.
---

# saos — simulate any optical system

Run commands from the saos repository root with `uv run saos …`. Every command prints one JSON object (`{"ok": true, "result": …}` or `{"ok": false, "error": {code, message, details}}`). Command help: `uv run saos --help` and `uv run saos <command> --help`. Demand format: [demand.md](demand.md). Terms: `GLOSSARY.md` (English and Chinese names).

You are the designer. The workflow fixes only a few gates and invariants; how you model, which starting design you choose, which merit function, variables, optimiser and search strategy you use is your judgement. Think like an optical engineer: understand the physics of this particular system before you optimise it, and change your approach when the evidence says it is not working.

## Invariants (never break these)

1. **Record the need.** Keep the user's own words in `sources/interview.md` and the condensed, current need in `sources/demand.yaml`.
2. **Measured evidence.** Every number you deliver comes from an actual OpticStudio computation (operand, analysis export, script) archived in the job. Never report an estimate as a result.
3. **No silent relaxing.** If a goal cannot be met, say by how much. Changing a constant, a variable range or an objective requires going back to the user and a new confirmation.
4. **Real materials in what you deliver.** A delivered lens uses only materials with a source (catalog, library, datasheet, paper). A model glass (`nd`/`Vd` only) is fine for exploration; the tools mark such rounds as not deliverable.
5. **报告统一使用 HTML。** 所有面向用户的报告（模型审阅、设计分析、探索结果、验证结果、装配方案、最终交付），无论工具生成还是手写，都生成 `.html`，报告之间的链接也指向 HTML。技能文件、需求 YAML、访谈记录、内部工作笔记和原始测量 JSON 保持工作流规定的格式。

## Gates (where you always wait for the user)

1. **The need**: the user approves the demand; `saos demand confirm` records their words, bound to that version.
2. **The model**: the user approves the model description; `saos lens approve` records their words. `saos round` refuses a lens whose structure matches no approved description: the surface types, the class of medium (air, glass, mirror), aperture types, the stop, whether a Fresnel substrate is flat or a surface is tilted, and the system settings (fields and wavelengths may gain points at the end). Radii, thicknesses, conics, polynomial terms, tilt angles, aperture sizes and the choice of glass stay yours. Surfaces you declare as environment (sample, media, bought-in parts) are locked further: material, radius, conic, apertures and tilts.

## When to stop and ask

A single optimisation can run for hours, often unattended. Do not spend that time on a dead end. Stop and ask the user, with the evidence, its impact and your recommended options, as soon as one of these holds; you do not have to exhaust every tier first:

- the demand contradicts itself, or a constant, range or pass line rests on a weak basis;
- a measurement can be read in more than one way and the choice changes the result;
- the feasibility reasoning (step 1) or the first-order analysis says the structure cannot reach the objectives, or a `must` misses by a multiple of its pass line;
- the results so far give no reason to keep the current strategy (the round `brief` sets `review` when rounds stall or you are about to escalate to `global` or `structure`);
- the structure must change in a way the user has not seen.

Otherwise keep working without asking. Write a question whose answer could change the demand into `open_questions` with `blocking: true`; `round` and `opt local` refuse to run until it is answered. You may still run diagnostic scripts to gather evidence for the question.

## 1. Frame the need

Create the job: `uv run saos job create --name <name>`. Grill the user in rounds. Prepare a whole round before you ask anything: every question you can ask now, numbered, each with your recommended answer. Put the questions whose answer can change the structure or a pass line first and mark them, so the user can answer those even if they skip the rest. Explain optics terms in plain words. Look facts up yourself (files, `ref/docs`, the OpticStudio manual, the web) instead of asking. Then put the round to the user:

- If you have an ask-user tool (in Claude Code `AskUserQuestion`), ask through it. Your recommendation is the first option and says so; the other options are the realistic alternatives, and a value the user has to type goes into the tool's free-text answer. When the round exceeds one call's limits (Claude Code: 4 questions, 2–4 options each), ask in consecutive calls with the marked questions in the first, and do no other work between them.
- Otherwise write the round as a numbered list in which each item is a `Question:` line followed by an `Advice:` line (your recommended answer and its reason in one sentence), worded so "yes" accepts the advice.

Keep every round in `sources/interview.md`: the questions, the options or advice you showed, and the user's answers verbatim (the full text of the chosen option and anything they typed).

At the start of every session, before any other work, check the external preconditions once and ask for what is missing in your first reply: the daemon of this instance (mode, which job it holds, whether another session uses it), whether the mode you will need can start (extension mode needs a visible OpticStudio window), how many OpticStudio instances are already open against the licence, and the tools you plan to use. `uv run saos daemon status` reports the machine side in one call (`environment`: the daemon of every instance with its mode and running jobs, OpticStudio processes against the licence, visible windows, and whether extension mode can start and with which window). Do not work around a missing one silently.

**Question the structure before you write it down.** A structure the user prescribes, or asks you to change (add, remove or swap an element), is a hypothesis, not a fact: the user may not be a specialist in this part of optics, and an unreachable structure costs hours of optimisation later. Before you show the demand, reason from physics whether it can reach every `must`, and say what you find even when nobody asked:

- what each element is there to do, and whether the rest can still do it after the requested change;
- the bounds no optimiser can move: conserved or invariant quantities (étendue and the Lagrange invariant, the Petzval sum), the diffraction limit, one-way conditions such as critical angles, the space and apertures available, and how many independent degrees of freedom the structure has against how many objectives it must hold at once;
- how each part is made and what that process cannot do (coatings, surface forms, sizes, materials); find out before you make the part's shape a variable.

Work these bounds by hand with the formulas in [feasibility.md](feasibility.md), and write each estimate out so the user can follow it: the formula, its inputs with their source, the result, and what it decides. Before the user confirms, check the one or two deciding numbers in OpticStudio with a deterministic calculation on a minimal model (a ray trace or an operand such as `RAID`, `RANG`, `REAY`, `SSAG`, `ETVA`), and say which constraints the minimal model leaves out. Do not search with an optimiser at this stage: it exploits every constraint a sketch leaves out (edge thickness, apertures, lost rays), and when it finds nothing that proves little. A verdict that only a search could decide is marginal; settle it in the first-order analysis (step 3). Before a check you expect to take more than a few minutes, tell the user how long and why. For each `must` give a verdict, reachable, marginal or not reachable, with the argument that decides it, and show it with the demand. When a verdict is marginal or not reachable, offer alternatives that change the deciding bound, not a list of variables to try.

Write `sources/demand.yaml` ([demand.md](demand.md)): the fixed `constants`, the `variables` you may adjust and their ranges, the `objectives` with their pass lines (`must`) and hoped-for values (`wish`), `open_questions`, and a short `summary` of the interview. Every limit (a constant's value or goal, a variable range, a pass line) names its `source`: the user, a datasheet, a paper, a textbook table, or `agent assumption: <why>`. Classifying each condition is your judgement; an unknown real-world fact is an open question, never a variable. Keep it short: no verbatim conversation, no literature details, no restated global rules. Every check must measure the real quantity; never special-case a surface type or setting to skip a measurement. Every check states its preconditions (parts do not intersect, the image is real, enough rays arrive, the quantity covers the whole beam and not only the surviving rays) and returns `inf` when one fails: a number from an invalid state is worse than no number. Checks are read-only: work on a copy or restore the lens, otherwise the round is refused (`LENS_MODIFIED_BY_CHECK`). When the design gives no valid value, return `inf`; raise only when the check itself cannot run. Show the demand in plain language with the feasibility verdicts, go through every limit that `uv run saos demand show --job J` lists under `weak_basis` (no outside basis) one by one, get explicit approval of its content, and record their words with `uv run saos demand confirm --job J --quote "..."`; the tools keep the full demand text the user saw. `demand confirm` also opens a small workflow window for the user (result field `ui`); reopen it with `uv run saos ui open --job J` if the user closed it or asks to see progress. It reads the job folder only and never needs your attention. When the user answers by choosing an option, the quote must contain the full text of the options shown and the one chosen, not just "c".

## 2. Recipes

If `skills/recipes/<system>/SKILL.md` matches, read it. Recipes are experience, not rules.

## 3. Model, understand, describe

- The user watches modelling and material choice. From the confirmed demand until a model description is approved, run the daemon in extension mode (`uv run saos daemon start --mode extension`); a standalone daemon refuses every operation on the job with `EXTENSION_REQUIRED`, and `lens describe` / `lens approve` need extension mode at any stage. In this phase saos keeps Lens Data and a layout window open after every lens-changing command and reopens them if they were closed (see "Explicit debugging"). Once a model is approved you may switch to standalone for long optimisation.
- Before you build, confirm what the session check (step 1) could not see yet: the material catalogs the lens needs are installed (`lens describe` warns about materials outside the catalogs) and OpticStudio started with the expected licence (`daemon start` reports it).
- Build the lens however suits the system: `lens import`, `lens build`, or `exec` a ZOS-API script that calls `ctx.save_as(...)`.
- Materials: look in OpticStudio catalogs and `library/` first; if neither has what the design needs, search the web (WebSearch/WebFetch) for datasheets or papers. For bought-in parts, filter by the hard limits first (size, wavelength range, mounting) and go straight to the vendor's datasheet, catalog export or ZMX/CAD download; when a vendor site blocks the fetch, try one other regional copy of the page before changing the kind of source. Record every material's source in `sources/materials.yaml` (format in `src/saos/materials_record.py`; a library entry or an OpticStudio vendor catalog counts as a source). Never invent dispersion or let one index stand for several wavelengths. Only the parts this design makes must be real catalog or library materials. The sample, immersion media and bought-in parts are environment: declare their surfaces in the notes (`"environment": [indices]`) so a sourced model medium there does not block delivery, and still record their data source and dispersion. Never change the physical setting (for example tissue to water) just to pass a gate.
- Know what OpticStudio does with every surface type and system setting you rely on; when unsure, read the manual or probe it (`lens operand`, ray traces) before trusting a result.
- Do a first-order analysis before optimising: whatever this system needs to show that the structure can reach the objectives (for example magnification, stop and pupils, beam sizes at the apertures, the bending each surface must do, what must be given up). This repeats the feasibility reasoning of step 1 on the built model with OpticStudio numbers; the `first_order` notes hold that argument. If it cannot, go back to the user now.
- Write notes (`{"surfaces": {"<index>": "what it physically is"}, "environment": [<indices>], "assumptions": [...], "first_order": [...]}`) and run `uv run saos lens describe --job J --notes notes.json`; the tools keep them as `notes/model-NNN.json`, and approval needs them. The page (`models/NNN-<lens>/index.html`) lists every surface, material source and index per wavelength, apertures, fields, wavelengths, ray aiming, your notes, layout pictures and automatic warnings. Resolve the warnings or explain them, give the user the page, and after their approval run `uv run saos lens approve --job J --model models/NNN-<lens> --quote "<their words>"`.

## 4. Design in rounds

- When you resume a job, or are unsure what you are doing, reread `sources/demand.yaml` first (`uv run saos demand show --job J`).
- Variables, boundary conditions and manufacturing limits (minimum thicknesses, diameter allowance, centring, test plates), with textbook values and the OpticStudio operands: [design-practice.md](design-practice.md).
- Tools: `lens surfaces/set/save/operand`, `opt vary --param radius|thickness|conic|par<N>|material`, `opt merit/local`, `lens analysis TYPE --set KEY=VALUE`, `scan --plan` and `exec SCRIPT` for anything ZOS-API can do (`ctx.system`, `ctx.zosapi`, `ctx.args`, `ctx.load`, `ctx.save_as`, `ctx.mark_dirty`, `ctx.path`).
- **Reach for the built-ins before you write code**; agent-written scripts cost more time than the runs they drive. In scripts, `ctx` (and the worker `w` of `ctx.map`) already has: `get`/`values`/`set` with keys `"<surface index or comment>.<radius|curvature|thickness|conic|semi_diameter|material|comment|par<N>>"`, `aperture`, `field.<n>.x|y`, `wave.<n>` (a value a solve overrides raises `VALUE_NOT_APPLIED`); `kept(keys)` and `restored()` to undo trials; `trace(rays, to)` (numpy arrays), `spot(fields, wave, grid, to, refocus)` (pass rate, chief, centroid, RMS and GEO radius per field, best focus shift), `op(name, …)`, `analysis(name, settings)`; `measure(ids)` (the demand's own checks on the current state, no evidence written: use it as the objective of any search so it means what the demand means); `memo`/`lib`/`cache`. `saos.search` has `grid`, `root` (scan then Brent, skips invalid points) and `minimize` (Nelder–Mead or Powell with budget and cancel). Details and measured costs: `docs/cli.md`, section 脚本执行.
- A scan, a sweep with a per-point solve, a first-order map or a multi-start search needs no script: `uv run saos scan --job J --plan plan.json` runs it on the worker pool, measures every point with the demand's checks, ranks by shortfall and keeps the best lenses under `lenses/scans/NNN/` (plan format in `docs/cli.md`). Write a script only for what a plan cannot say, and then put the per-point work in a function and run it with `ctx.map("fn", items)`.
- **Keep working while it computes.** A scan you expect to run more than a few minutes goes with `--detach`: the command returns at once, the scan runs on the worker pool from a snapshot of the saved lens, and the daemon keeps serving your other commands (`lens info`, `check`, reading results, preparing the next step). Follow it with `uv run saos scan --job J --show [NNN]` (reads the files, works while the daemon is busy); `saos job cancel` stops it. A second scan or `ctx.map` while one runs gets `POOL_BUSY`; `daemon stop` waits for you to cancel or finish it (`BUSY`).
- **Changing the checks is safe.** Check code includes `checks/` and the saos recipe libraries and `saos.kit` it uses. When it changes, the next `saos round` first re-measures the best round so far with the new checks (a ledger row with tier `rebase` pointing at the old archived lens) and compares from there. If the numbers are the same (a speed-up, a switch to kit or a recipe), the comparison window goes on; if they differ, a new window starts at that row, and the old best lens stays deliverable under the new checks. The round result shows it under `rebase` (`equivalent`, `diffs`); an unintended difference is a bug in the new checks, an intended one is a demand question.
- Prefer OpticStudio's own optimisers (local DLS, Hammer, global search) and merit operands; make sure the merit function measures what the demand means and cannot be satisfied by a loophole (for example by discarding light or leaving the physical part).
- Optimise on smooth proxies (RMS spot or wavefront operands, Seidel terms, real-ray constraints); use the demand checks to judge. Step-like checks (ray counts, a field scanned in steps) give an optimiser almost no gradient.
- An optimiser exploits whatever the target leaves open. Physically impossible states (intersecting parts, a virtual focus, rays missing the image, light leaving the part) must make the target invalid, not cost a linear penalty. A merit value OpticStudio cannot compute (for example 9e+09 when rays fail by total internal reflection) is a diagnosis signal: find the cause before going on, never skip it as noise.
- When you optimise outside the OpticStudio merit function, record the value you actually optimised with `saos round --merit <value>`; otherwise the ledger shows whatever merit function the lens file holds.
- Weigh the merit function the way the demand weighs the objectives: one structure rarely meets everything, and the compromise should favour the user's objectives.
- Record a round when you have a design worth judging against the demand: after an optimisation run, a material substitution or a structural change, not after every tweak. Small trial changes go through `saos check` (measures without recording) or `explore/`; `saos check --only <id>,<id>` measures just the items your change can affect (the result is `partial` and never achieved; every round measures all items). Each measured item reports `duration_s`, so you can see which checks are slow. `uv run saos round --job J --tier <tier>[,<tier>…] --note "<what you did>"`; name every tier the round used. Tiers, low to high: `local`, `variables`, `materials` (substitute sourced materials), `global`, `structure`. The ladder is a record of what was tried, not an order to follow: when your evidence says a tier cannot help, rule it out with `uv run saos rule-out --job J --tier <tier> --reason "<evidence, e.g. probes/NNN>"` instead of spending a round on it (it holds until the next clear gain). Read the decision (`achieved`, `exhausted`, `limit`, `continue` with `next_tier`); compare rounds by the shortfall, never by the merit value. Each round also exports `evaluations/NNN/viz.json` (layout, spot and MTF data) for the user's window; a failed export is reported under `viz` and does not affect the round.
- If the structure must change in a way the approved description does not cover, describe the new model and get it approved before recording rounds on it.
- **Gate expensive runs on the gap.** Before a run you expect to take more than about ten minutes, or any `global` or `structure` attempt, tell the user for each failing `must` how far it is off, the mechanism you expect to close it, the variables that act on that mechanism and a rough expected gain. When an objective misses by a multiple of its pass line rather than a fraction, or a diagnosis already shows a cap in this structure (light clipped by an aperture the variables cannot widen, an aberration the variables cannot change), optimising is not the next step: diagnose (below) and take the result to the user.
- **Accuracy follows reachability.** Convergence and sampling audits (finer ray grids, more segments, more directions) earn their time once the design is close to its pass lines. While it misses by a multiple, sample just finely enough that the verdict cannot flip.

### Diagnose before you escalate

When progress stalls, before an expensive escalation, or when a round `brief` sets `review`, think from geometrical optics instead of trying everything. How much of the following you need depends on the system and on what you already know; skip what the evidence already settles.

Is the target reachable at all? A useful ladder, from the cheapest test: conserved quantities and invariants (étendue, the Lagrange invariant, the Petzval sum); one point on axis under every real constraint (apertures, clearances, pass rate, TIR); the full field. A failure low on the ladder is a structure question, and optimising higher up is wasted.

Which variables move the target?

1. Name the objective with the largest weighted gap and the mechanism behind it: which aberration or limit (spherical, coma, astigmatism, field curvature, distortion, axial or lateral colour, vignetting, aperture, Lagrange invariant), seen in Seidel terms, ray fans, field curves or pass rates.
2. Reason which parts of the structure can change that mechanism and which cannot. Some quantities are invariant to whole classes of variables: the Petzval sum depends only on surface powers and indices, so aspheric and freeform terms only trade field curvature for astigmatism. Probe the few variables your reasoning ranks highest (a plan takes 1–6). A control that should not move the target is worth adding whenever the ranking decides what you optimise: without it a small effect cannot be told from noise.
3. Write the predictions first (mechanism, direction, rough size), then run `uv run saos probe --job J --plan plan.json`. Each probe is measured at ±delta from the same base copy, with a compensator (`"compensate": "quick_focus"` or your own script) so that defocus does not hide the aberration (Quick Focus moves the thickness before the image surface and can fail silently on folded or partly failing systems; the result says so when it never moved); choose delta as a meaningful fraction of the variable's allowed range. The lens is left unchanged; everything goes to `probes/NNN/`.
4. Read `ranking` and `review`. Optimise first on the variables that move the target. If a prediction failed or a control moved, your model of the system is wrong: fix that before optimising. If nothing moves the target and your first-order reasoning agrees, it is a structure question: rule out the tiers that cannot help (`saos rule-out`, citing `probes/NNN`) and go to `structure`, or ask the user.

Plan format:

```json
{"target": "spot", "mechanism": "Petzval field curvature: ~73 um sag at 150 um image height",
 "compensate": "quick_focus",
 "probes": [
   {"id": "l1_rear_r", "surface": 3, "param": "radius", "delta": 0.02,
    "prediction": "less lens-1 power lowers the Petzval sum; spot down by a few um", "expect": {"spot": "down"}},
   {"id": "l2_a4", "script": "probes/l2_a4.py", "delta": 0.001, "control": true,
    "prediction": "trades astigmatism only, Petzval unchanged", "expect": {"spot": "none"}}]}
```

A built-in change is `surface` + `param` (`radius`, `thickness`, `conic`, `par<N>`); anything else is a script under the job defining `run(ctx, delta)`. `measure` (optional) lists the demand items to measure; the default is every item with a bound.

## 5. Deliver

`uv run saos report --job J` writes `report/index.html` in the user's language: status, the delivered lens (the first achieved round, else the smallest shortfall among deliverable rounds), its materials, and for each unmet goal how far it is off. With no valid round it says so instead of naming a "best" lens. Give the user the path and say that a tolerance analysis can follow.

Stopping is a conclusion too. When the evidence says the demand cannot be met with what is allowed, show the user the evidence and the options; when they decide to stop, record it with `uv run saos stop --job J --reason "<why, citing the evidence>" --quote "<their words, with the option text>"`. Only the user's decision stops the rounds; the report then shows the reason and their words.

## Compute

- Every optical number is computed by OpticStudio through ZOS-API, first-order numbers included (paraxial operands such as `EFFL`, `PMAG`, `PARY`, `PARA`, Seidel terms, ray traces, analyses). Do not write your own ray tracer, aberration or paraxial calculator, surrogate model or GPU code (ADR-0008). Python scripts may hold the control logic, such as scans, multi-starts, sequencing and bookkeeping, as long as every evaluation is a ZOS-API call. This rule is about code and about the numbers you decide on: reasoning with hand estimates to question a structure or choose a direction is expected (step 1, formulas in [feasibility.md](feasibility.md)), and the estimate that decides is then checked in OpticStudio.
- Normally one OpticStudio instance does the work. When many independent evaluations are needed (a scan, multi-starts, a sensitivity table), run them concurrently: `saos scan`, or `ctx.map("<function of this script>", items, workers=N)` in a `saos exec` script (`"explore/x.py:fn"` for a function in another job file). The function runs as `fn(w, item)` in a worker, its own standalone instance, which reloads a copy of the saved lens before every item. The workers stay up in the daemon between calls and close after 10 idle minutes (`ctx.pool_close()` frees the licences at once), so only the first call pays the start-up of 5.8–7.9 s per instance (measured 2026-10-17). Measured on this machine: with the pool up, 36 points of a solve-and-measure scan took 6.76, 3.83, 2.21 and 1.63 s on 1, 2, 4 and 6 workers; started cold, the same 4-worker scan took 10.2 s against 5.25 s serial in the daemon, so start a pool only for work that outlasts the start-up or will be repeated. The licence allows 8 OpticStudio instances at a time, the user's open windows and this daemon included; count them (`daemon status`) before choosing `workers`, and expect a refused worker to be reported as unavailable. Results and the script copy go to `parallel/NNN/` (`scans/NNN/` for `saos scan`).
- Per-point work that searches (find the curvature that puts the focus at the working distance, a best-focus solve) uses `search.root` rather than a coarse scan plus bisection: in the measured example it took 58% fewer evaluations for the same answer.
- Every number you base a decision on uses one definition: optimisation targets, your scripts' evaluations, first-order estimates and the merit value in the ledger use the same sampling, constraints and definition as the demand checks, or have been compared with the checks on the same lens before you draw a conclusion. A reduced or simplified OpticStudio model (paraxial surfaces, a coarse ray grid, one field) states which constraints it drops, and its key prediction is tested on the real lens before you act on it.
- Times and numbers in notes and messages come from tool output (results, logs, `saos job show`), never from memory or estimate; label an estimate as one.
- Leave 2 logical cores to the user and use the rest (`saos opt local` does this by default); start your own long-running processes at below-normal priority so the user's work stays responsive.
- Long runs are visible and stoppable. `opt local`, `round`, `check`, `scan` and `exec` write the job's `progress.json` (a detached scan writes `scans/NNN/progress.json`); `uv run saos job show <job>` reads it while the daemon is busy. Give `opt local` a `--max-seconds` budget. In a long script call `ctx.progress(stage, **numbers)` at checkpoints; it returns True after `uv run saos job cancel <job>`, and the script then stops at a safe point and keeps what it has (`ctx.cancel_requested()` checks without reporting). A client timeout only stops waiting; the daemon keeps computing until it finishes or is cancelled. Tell the user what is running, where and how to follow it, and the numbers at each checkpoint; stop a run when a checkpoint interval gains less than the demand's `rel` instead of holding the instance.

## Context economy

Read a large file by section (find the headings, read the part you need), and read a helper library only when you are about to use or change it. Material and bought-in part searches that should end in one table (part numbers, sizes, indices, dispersion data) go to the `optic-material-explorer` agent when it is available (`~/.claude/agents/`): give it the hard limits, the columns you need and a download folder under the job's `sources/`; it returns a sourced candidate table, an exclusion table and the files it saved. Without it, search yourself as the Materials bullet in step 3 says. Before a long piece of work, ask whether its result can still change what you do next; if a pending question could make it obsolete, wait for the answer.

## Explicit debugging (on-screen)

When the user wants to watch, start with `uv run saos daemon start --mode extension` (set `SAOS_UI_PID` if several OpticStudio windows are open). saos opens Lens Data and a layout window: the cross-section, or the 3D viewer when OpticStudio cannot draw a cross-section (non-axial or non-sequential systems; the result's `layout.fallback_reason` says why). Add an analysis worth watching with `lens view --analysis TYPE`; `--viewer cross-section|3d|shaded` picks another layout. Windows refresh after every lens-changing command. `lens screenshot` captures PNGs. On `UI_BLOCKED`, ask the user to close the named dialog. Before `lens load`, `unload` or `build` replace the window content, saos saves unsaved window content to `lenses/captures/NNN-unsaved.zmx` (returned as `captured`); tell the user where their work went. `ctx.load` in scripts does the same; call `ctx.capture_window()` first in a script that replaces the window content. Your own unsaved changes are refused until you `lens save` or pass `--discard`, which drops only your changes. `exec --quiet` pauses window updates during long scripts.

## Where files go

Instance directory `work/<instance>/` holds only `daemon/` and `jobs/`. Files of a job `work/<instance>/jobs/<job>/`, step by step (NNN is a three-digit sequence number):

| Step | File | Written by | Purpose | Changes |
|---|---|---|---|---|
| create | `job.json` | tools | job state, current lens | tools update it |
| any command | `log.jsonl` | tools | every command, result, error code, time | append only |
| frame the need | `sources/interview.md` | you | each grilling round: questions, recommendations, the user's words | append only |
| | `sources/demand.yaml` | you | constants, variables, objectives with checks, open questions, interview summary, stop rule | changes to constants / variables / objectives / stop need a new confirmation |
| | `confirmations.jsonl` | tools | the user's approval quotes, each bound to a demand version (`saos demand confirm`) | append only |
| | `checks/` | you | check scripts and every helper module or data file they use; checks may depend on nothing else | any change starts a new comparison window |
| | `evaluations/NNN/` | tools | snapshot, `support/` (copy of `checks/`), analysis exports and `result.json` (with code and OpticStudio versions) of every check or round; `result.json` is the authoritative measurement | sealed |
| model | `lenses/<name>.zmx` | you | working lenses | free |
| | `sources/materials.yaml` | you | source of every material (format in `saos.materials_record`, fields as in `library/materials`) | append or correct sources |
| | `scripts/NNN-<name>.py`, `.json` | tools | copies and results of `saos exec` | sealed |
| | `analyses/NNN-<type>.json`, `.txt` | tools | numeric exports of `lens analysis` | sealed |
| | `notes/model-NNN.json` | you, saved by the tools | what each surface is, assumptions, first-order analysis (input of `lens describe --notes`) | sealed |
| | `models/NNN-<lens>/` | tools | model description: `index.html`, `model.json` (with the structural fingerprint), `layout-*.png` | sealed |
| model approval | `models/NNN-<lens>/approval.json` | tools | the user's words, fingerprint, warnings still shown | sealed |
| rounds | `lenses/rounds/rNNN.zmx` | tools | archived lens, written after the checks proved they did not change it; the working file stays the current lens | sealed; `lens save` refuses it |
| | `lenses/rounds/orphaned/` | tools | archives left by a failed attempt, moved aside instead of overwritten | kept |
| | `ruled_out.jsonl` | tools | tiers you ruled out with the evidence (`saos rule-out`) | append only |
| | `stops.jsonl` | tools | the user's decisions to stop, with reason and quote (`saos stop`) | append only |
| | `ledger.jsonl` | tools | per round: shortfall, tier, materials, lens hash, approved model, pointer to `evaluations/NNN/result.json`; appending it completes the round | append only |
| explicit debugging | `screens/NNN-<label>-*.png`, `lenses/captures/NNN-unsaved.zmx` | tools | screenshots; window content saved before it is replaced | sealed |
| concurrent evaluation | `parallel/NNN/` | tools | `ctx.map`: plan with the items, the work script copy, every result | sealed |
| | `scans/NNN/` | tools | `saos scan` (probes included): plan with your predictions, lens snapshot `base.zmx`, every point's measurements ranked; `progress.json` when detached | sealed |
| | `lenses/scans/NNN/pPPP.zmx` | tools | lenses of the best scan points, ready for `lens load` | free |
| exploration | `explore/<topic>/` with `README.md` | you | code, intermediate lenses, logs run outside `saos exec`; README states purpose, outcome, superseded or not | free |
| delivery | `report/NNN-rMMM-<status>.html` | tools | every generated report | sealed |
| | `report/index.html` | tools | the latest report | regenerated |
| any time | `notes/*.html` | you | 面向用户的设计、探索、验证等报告 | free |
| | `notes/*.md` | you | 内部工作笔记，不作为报告交付 | free |
| any time | `seals.jsonl` | tools | hashes of sealed files | append only |

Sealed means the tools recorded the file's hash; `uv run saos job verify <job>` and `tests/test_repo_layout.py` report any later change or deletion, and `saos report` refuses to run while one exists. Nothing about a specific job goes into the repository (see DESIGN.md "仓库目录"). Temporary files go to the system temp directory.
