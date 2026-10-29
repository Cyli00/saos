# Design in rounds

The outcome of this stage is a ledger of rounds judged against the demand and a final round decision: `achieved`, `exhausted`, `limit` or `stopped`. Leaving the approved structure sends you back to stage 2 ([the last section](#gate-expensive-runs-on-the-gap)).

## Build a merit function that means the demand

- Optimise on smooth proxies: RMS spot or wavefront operands, Seidel terms, real-ray constraints. Judge with the demand checks. Step-like checks (ray counts, a field scanned in steps) give an optimiser almost no gradient.
- Weigh the merit function the way the demand weighs the objectives; the compromise should favour the user's objectives.
- An optimiser exploits whatever the target leaves open. Physically impossible states (intersecting parts, a virtual focus, rays missing the image, light leaving the part) must make the target invalid, not cost a linear penalty. Make sure the merit cannot be satisfied by a loophole such as discarding light.
- A merit value the backend cannot compute (for example 9e+09 when rays fail by total internal reflection) is a diagnosis signal: find the cause before going on.
- When a search must use the demand's own measurement (`ctx.measure(ids)`, a scan's `measure`, a `minimize` plan), give it only the items its variables act on and that take well under a second. Leave slow items (a Huygens PSF series takes seconds) to `saos check --only` and the round. A Nelder–Mead on the full shortfall of the fibre probe (18 variables, one-hour budget) stopped unconverged after 48 evaluations.

Tools: `opt vary --param radius|curvature|thickness|conic|par<N>|material`, `opt merit wizard|add|show`. Bounds belong in Zemax boundary operands, such as CTGT/CTLT/ETGT; there are no per-variable --min/--max flags. From a script, add rows in one checked batch with `ctx.merit_add`, design-rule constraints from `saos.ctx.rows`, and solves with `ctx.solve` ([scripting.md](scripting.md#editing-the-lens-from-a-script)).

## Optimisers in OpticStudio

- `opt local --algorithm auto|dls|od --max-seconds S` uses Zemax local optimisation. Auto selects DLS for sequential systems and orthogonal descent for non-sequential systems.
- `opt hammer --max-seconds S` and `opt global --max-seconds S` run the native search tools until the budget, then cancel and leave the best system in the editor. Global search moves its ten best to `lenses/global/NNN/` (do not run next to existing `GLOPT_*` files); Hammer leftovers go to `lenses/hammer/NNN/`.
- Glass substitution (`opt vary --param material --mode substitute`) happens only in Hammer or global search, within one catalog per surface. Without `--catalog` it uses `SAOS_CDGM_INPROD` (CDGM Standard or Preferred glasses; `SAOS_NHG_INPROD` is the same for New Huaguang), and the result names the catalog. `outside_catalog` lists surfaces whose current glass is not in it; pass `--catalog` per surface where the maker must stay.
- Read `materials_changed`. A maker's full catalog often yields discontinued glasses (9 of 12 substitutions on the Cooke sample in SCHOTT were Obsolete); replace an `Obsolete` glass with one in production or ask the user before delivering it.
- `ctx.GlassExpert` in an `exec` script is the `materials` tier done glass by glass: in-production neighbours by working-band n/V, anomalous-dispersion glasses first, each trial re-optimised. `surfaces="model"` replaces every model glass, which a patent start always has (its glass codes), before delivery.
- `ctx.AsphereExpert(...).rank()` predicts which spherical surfaces gain most from an asphere and leaves the lens as it was. `.run(nas)` changes surface types, which changes the approved structure: show the ranking to the user, run, describe the model again and get `saos lens approve` before `saos round --tier structure`.
- Neither expert saves the lens or records a round; options are in [scripting.md](scripting.md#expert-search-classes).
- Your own `saos.search` loops on a ray-quantity objective run on the rtc copy ([scripting.md](scripting.md#choose-where-a-loop-runs)); a loop on OpticStudio measurements is slower and gets no gradient from the native solver. An rtc result is a starting point: push it and close with `opt local --detach` on the lens's merit function before `saos check` or `saos round` judges it. While that runs you may already show the pushed lens with `ui notify` and draft the user's report, then update both from the closing measurements.

Read the result, not just the merit:

- `algorithm` records the native local algorithm. `solver_succeeded`, `improved` and `stopped_by` have distinct meanings; `converged` is unknown when the native API does not establish it.
- Measurements and review share the command budget. Cancellation waits for native calls to return. An unfinished review is `completed: false, valid: false`; finish the checks before recording a round.
- `review` includes interference and, with `--measure <ids>|hard|all`, before/after demand checks. A lower merit with worse checks is not evidence of progress.
- A failed run leaves the lens marked unsaved: save under a new name to retain it, or load a saved lens with `--discard`.

When you optimise outside the lens's merit function (your own `exec` loop), record the value you optimised with `saos round --merit <value>`.

## Record rounds

Record a round when you have a design worth judging: after an optimisation run, a material substitution or a structural change, not after every tweak. Trials go through `saos check` (measures without recording; `--only <id>,...` measures just the affected items, never achieved) or `explore/`. After two optimisations or fifteen scripts without a round, `opt`, `exec` and `scan` results carry a `since_round` reminder; a long history with an empty ledger leaves the stop rule nothing to judge.

Your own loops (a multi-start, a batch least-squares script, a `ctx.map` search) count toward that reminder like `opt` runs. When it appears, either record the best lens so far or say in your next message why it is not yet worth judging. Leave the plots on: spot, fan, field and MTF plots exist only for recorded rounds, so `--no-viz` is for a failing or very slow export, and you say so in the note. On the AR-glasses job, half an hour of optimisation followed a baseline recorded with `--no-viz`, and the user saw no plot and no shortfall the whole time.

`uv run saos round --job J --tier <tier>[,<tier>...] --note "<what you did>"` names every tier the round used. Tiers, low to high: `local`, `variables`, `materials`, `global`, `structure`. The ladder records what was tried; it is not an order. When evidence says a tier cannot help, `uv run saos rule-out --job J --tier <tier> --reason "<evidence, e.g. scans/NNN>"` instead of spending a round on it; this holds until the next clear gain.

Read the decision (`achieved`, `exhausted`, `limit`, `continue` with `next_tier`) and compare rounds by shortfall, never by merit. A round whose parts cross carries `parts_clash` and is never achieved or deliverable. Each round exports `evaluations/NNN/viz.json` for the workflow window; a failed export is reported under `viz` and does not affect the round.

## Changing the checks

Changing `checks/` (or the recipe and kit code they import) is safe. The next round first re-measures the best round so far with the new checks (a `rebase` row) and compares from there. Equal numbers (a speed-up, a switch to a library) keep the comparison window; different numbers start a new one, and the old best lens stays deliverable under the new checks. An unintended difference is a bug in the new checks; an intended one is a demand question. A new window does not reset `max_rounds`, and after `limit` or `stopped` changed checks are refused like any other round.

## Gate expensive runs on the gap

Before a run you expect to take more than about ten minutes, or any `global` or `structure` attempt, tell the user for each failing `must` how far it is off, the mechanism you expect to close it, the variables acting on that mechanism and a rough expected gain. When an objective misses by a multiple of its pass line, or a diagnosis shows a cap in this structure (light clipped by an aperture the variables cannot widen, an aberration they cannot change), optimising is not the next step: diagnose ([diagnosis.md](diagnosis.md)) and take the result to the user.

Accuracy follows reachability. Convergence and sampling audits earn their time once the design is close to its pass lines; while it misses by a multiple, sample just finely enough that the verdict cannot flip.

If the structure must change beyond the approved description, describe the new model and get it approved before recording rounds on it.

Before you leave an approved structure, leave evidence of why in the ledger or `scans/`: its best round with the gaps, and a diagnosis or `rule-out` showing what caps them. A first-order or probe result is enough when it settles the question. The new candidates go through the screening in [model.md](model.md) with that best round as the reference. The user hears the gaps, the cap and what the new form gives up before it is built, even under an autonomy grant.
