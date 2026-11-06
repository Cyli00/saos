# Design in rounds

The outcome of this stage is a ledger of rounds judged against the demand and a final round decision: `achieved`, `exhausted`, `limit` or `stopped`. Leaving the approved structure sends you back to stage 2 ([the last section](#gate-expensive-runs-on-the-gap)).

This stage computes on rtc ([session.md](session.md#two-backends-in-one-daemon)). A check result's `backend_unsupported` names the items rtc cannot compute: rewrite them through `ctx` ([need.md](need.md#write-checks-that-measure-the-real-quantity)), or, for an analysis rtc lacks, pin OpticStudio for that step as [session.md](session.md#two-backends-in-one-daemon) describes.

## Merit function

Build and change it by [merit.md](merit.md).

## Optimisers

`opt local --max-seconds S` is local optimisation. `opt hammer --max-seconds S` and `opt global --max-seconds S` run until the budget, then cancel and leave the best system in the editor; global search moves its ten best to `lenses/global/NNN/`. Where your own `saos.search` loops run: [scripting.md](scripting.md#choose-where-a-loop-runs).

**On rtc**, the default here:

- **`opt local`.** Damped least squares where boundary rows are exact constraints, not penalties (after CODE V): read `constraints`, `violation.after` and `violated_after`; constraints already violated at the start take damped, line-searched restoration steps into the feasible region rather than a forced full jump. A start where a weighted row cannot be computed (a ray misses) is refused with the failing rows; diagnose it first. `--algorithm dls` only; `cycles` count iterations, `auto` stops at convergence or 200. Cancellation and time limits are checked before each evaluation; cancellation or an exception restores the last accepted point.
- **`opt hammer|global`.** Hammer archives the systems it finds to `lenses/hammer/NNN/`. Only solutions meeting every constraint compete. On rtc, the candidates after the first local run are tried in `--cores` worker processes. The result lists `checkpoints` (every tenth of the budget, 1–60 s) and `best_at_s`, when the best system was found; `--rel R` stops at a checkpoint whose best merit fell by less than `R` relative to the previous one (`stopped_by: stalled`). Substitution picks among the 8 glasses of the catalog nearest in nd/νd to the current one, not the whole catalog. Material variables and a stop's variable semi-diameter live in memory only: after `lens load`, set them again.
- **Plots and rounds.** Round plots are rtc's spot, ray fan and FFT MTF. Rounds carry `backend: rtc` and `provenance` (paths and operands not reconciled with OpticStudio). Judge and stop on them as usual; delivery re-measures the delivered round ([delivery.md](delivery.md#re-measure-on-opticstudio)).

**On OpticStudio** (a pin, or a lens rtc cannot trace): `opt local --algorithm auto|dls|od` selects DLS for sequential systems and orthogonal descent for non-sequential ones. Do not run global search next to existing `GLOPT_*` files. Hammer writes no files: keep its result with `lens save`.

**Glass substitution** (`opt vary --param material --mode substitute`) happens only in Hammer or global search, within one catalog per surface. Use it to escape a local minimum, since it only tries the 8 nearest glasses; to choose materials, use `ctx.GlassExpert`. Without `--catalog` it uses `SAOS_CDGM_INPROD` (CDGM Standard or Preferred glasses; `SAOS_NHG_INPROD` is the same for New Huaguang), and the result names the catalog. `outside_catalog` lists surfaces whose current glass is not in it; pass `--catalog` per surface where the maker must stay. Read `materials_changed`: a maker's full catalog often yields discontinued glasses (9 of 12 substitutions on the Cooke sample in SCHOTT were Obsolete); replace an `Obsolete` glass with one in production or ask the user before delivering it. Material and asphere decisions beyond these: [Choose an expert](#choose-an-expert).

Read the result, not just the merit:

- `algorithm` records the native local algorithm. `solver_succeeded`, `improved` and `stopped_by` have distinct meanings; `converged` is unknown when the native API does not establish it.
- Measurements and review share the command budget. Cancellation waits for native calls to return. An unfinished review is `completed: false, valid: false`; finish the checks before recording a round.
- Pass `--measure <ids>|hard|all` on every optimisation call so `review` reports before/after demand checks alongside interference. When the merit falls while the checks get worse, inspect and fix the merit function first (missing rim rays, missing fields, unweighted constraints) before spending another optimisation run. `opt`, `scan` and `check` surface any `parts_clash` at the top level of their result and list `"parts_clash"` in `hard_failing`.
- A failed run leaves the lens marked unsaved: save under a new name to retain it, or load a saved lens with `--discard`.

When you optimise outside the lens's merit function (your own `exec` loop), record the value you optimised with `saos round --merit <value>`.

## Choose an expert

Two expert classes answer questions the optimisers never ask: which materials, and which surfaces earn an asphere. Run them in an `exec` script; options and how to keep the result are in [scripting.md](scripting.md#expert-search-classes). A stall alone is no reason to call one: name the mechanism behind the largest gap first ([diagnosis.md](diagnosis.md#which-variables-move-the-target)).

`ctx.GlassExpert(surfaces)` is the `materials` tier done glass by glass: in-production neighbours by working-band n/V, anomalous-dispersion glasses first, each trial re-optimised. It is slow ([model.md](model.md#spherical-version-first-an-aspheric-version-to-compare)), so it is not a routine step. Its place is the last round before delivery, with `max_seconds` 120–180 on the surfaces that matter, and only when:

- a round still has model glass (a patent start always does, from its glass codes): pass the surfaces its `model_glass` lists;
- axial or lateral colour or the secondary spectrum is the largest weighted gap and the demand lets the glasses change. The merit function must weigh that colour over several wavelengths, or the trials optimise something else;
- a glass is discontinued, or misses a cost, transmission, weight or expansion limit the user gave: pass that limit as `feasible(ctx, surface)`, since saos holds no such data.

Otherwise leave the glass alone, or try Hammer with substitution when you want a cheap look. Resins: [model.md](model.md#choose-resins).

`.candidates(surface)` lists what it would try and leaves the lens unchanged; show the list to the user when the material choice is theirs. Record the result as a `materials` round.

`ctx.AsphereExpert` follows the rule in [model.md](model.md#spherical-version-first-an-aspheric-version-to-compare), in the rounds as in choosing the structure. `.run(nas)` changes the approved structure: show the ranking to the user, run, describe the model again and get `saos lens approve` before `saos round --tier structure`. A round brief with `reach_blocking` means the lens is still too far off for more of the same: read [the reach gate](model.md#the-reach-gate) before spending more compute.

## Record rounds

Record a round when you have a design worth judging: after an optimisation run, a material substitution or a structural change, not after every tweak. Trials go through `saos check` (measures without recording; `--only <id>,...` measures just the affected items, never achieved) or `explore/`. After two optimisations or fifteen scripts without a round, `opt`, `exec` and `scan` results carry a `since_round` reminder; a long history with an empty ledger leaves the stop rule nothing to judge.

Your own loops (a multi-start, a batch least-squares script, a `ctx.map` search) count toward that reminder like `opt` runs. When it appears, either record the best lens so far or say in your next message why it is not yet worth judging. Leave the plots on: spot, fan, field and MTF plots exist only for recorded rounds, so `--no-viz` is for a failing or very slow export, and you say so in the note. On the AR-glasses job, half an hour of optimisation followed a baseline recorded with `--no-viz`, and the user saw no plot the whole time.

`uv run saos round --job J --tier <tier>[,<tier>...] --note "<what you did>"` names every tier the round used. Tiers, low to high: `local`, `variables`, `materials`, `global`, `structure`. The ladder records what was tried; it is not an order. `next_tier` only names the lowest tier neither tried nor ruled out, so choose by the mechanism. When evidence says a tier cannot help, `uv run saos rule-out --job J --tier <tier> --reason "<evidence, e.g. scans/NNN>"` instead of spending a round on it; this holds until the next clear gain. The reason must cite an existing file of the job. `materials` also needs a real attempt, or a reason starting `not runnable:` that says why the search cannot run here (a multi-configuration lens, glasses fixed by the demand).

Read the decision (`achieved`, `exhausted`, `limit`, `continue` with `next_tier`) and compare rounds by shortfall, never by merit. A round whose parts cross, or with a surface whose sag has no real value inside its part edge (`parts_clash.undefined`, checked from radius and conic because OpticStudio and rtc both return the boundary value silently there), carries `parts_clash` and is never achieved or deliverable. Each round exports `evaluations/NNN/viz.json` for the workflow window; a failed export is reported under `viz` and does not affect the round.

## Changing the checks

Changing `checks/` (or the kit code they import) is safe. The next round first re-measures the best round so far with the new checks (a `rebase` row) and compares from there. Equal numbers (a speed-up, a switch to a library) keep the comparison window; different numbers start a new one, and the old best lens stays deliverable under the new checks. An unintended difference is a bug in the new checks; an intended one is a demand question. A new window does not reset `max_rounds`, and after `limit` or `stopped` changed checks are refused like any other round.

## Gate expensive runs on the gap

Before a run you expect to take more than about ten minutes, or any `global` or `structure` attempt, tell the user for each failing `must` how far it is off, the mechanism you expect to close it, the variables acting on that mechanism and a rough expected gain. When an objective misses by a multiple of its pass line, or a diagnosis shows a cap in this structure (light clipped by an aperture the variables cannot widen, an aberration they cannot change), optimising is not the next step: diagnose ([diagnosis.md](diagnosis.md)) and take the result to the user.

Accuracy follows reachability. Convergence and sampling audits earn their time once the design is close to its pass lines; while it misses by a multiple, sample just finely enough that the verdict cannot flip.

The same goes for manufacturability. Once the design is close to its pass lines and the tolerances have sources, run a sensitivity-only scan (`"samples": 0`, format in [delivery.md](delivery.md#tolerance-scan)) before you polish further. A few items that break the design are a structure question, far cheaper to learn now than at delivery.

If the structure must change beyond the approved description, describe the new model and get it approved before recording rounds on it.

Before you leave an approved structure, leave evidence of why in the ledger or `scans/`: its best round with the gaps, and a diagnosis or `rule-out` showing what caps them. A first-order or probe result is enough when it settles the question. The new candidates go through the screening in [model.md](model.md) with that best round as the reference. The user hears the gaps, the cap and what the new form gives up before it is built; in an autonomous job they go into `sources/interview.md` and the new description's `--reason`.
