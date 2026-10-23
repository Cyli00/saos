# Design in rounds

The outcome of this stage is a ledger of rounds judged against the demand and a final round decision: `achieved`, `exhausted`, `limit` or `stopped`.

## Build a merit function that means the demand

- Optimise on smooth proxies: RMS spot or wavefront operands, Seidel terms, real-ray constraints. Judge with the demand checks. Step-like checks (ray counts, a field scanned in steps) give an optimiser almost no gradient.
- Weigh the merit function the way the demand weighs the objectives; the compromise should favour the user's objectives.
- An optimiser exploits whatever the target leaves open. Physically impossible states (intersecting parts, a virtual focus, rays missing the image, light leaving the part) must make the target invalid, not cost a linear penalty. Make sure the merit cannot be satisfied by a loophole such as discarding light.
- A merit value the backend cannot compute (for example 9e+09 when rays fail by total internal reflection) is a diagnosis signal: find the cause before going on.
- When a search must use the demand's own measurement (`ctx.measure(ids)`, a scan's `measure`, a `minimize` plan), give it only the items its variables act on and that take well under a second. Leave slow items (a Huygens PSF series takes seconds) to `saos check --only` and the round. A Nelder–Mead on the full shortfall of the fibre probe (18 variables, one-hour budget) stopped unconverged after 48 evaluations.

Tools: `opt vary --param radius|curvature|thickness|conic|par<N>|material [--min X --max X]`, `opt merit wizard|add|script|show`. `opt merit script loss.py` adds `def loss(ctx)` as one more merit row, a non-negative residual driven to 0, for example a demand check through `ctx.measure`.

## Optimisers in Optiland

`saos opt local` runs in process without a licence. Give it `--max-seconds`.

- `--algorithm auto` (default) prefers `torch-trf` on CPU float64 and falls back to `trf` only for an identified differentiability gap. Explicit choices never fall back. Script bugs, numerical failures, cancellation and timeout never trigger a retry; a fallback consumes the remaining budget.
- Torch algorithms (`torch-trf`, `torch-lbfgs`, `torch-adam`) use autograd when the merit holds only RSCE, RWCE, REAY, EFFL, TOTR, CTVA, ETVA and their boundary rows. A torch loss script returns a scalar Tensor connected to the variables through `ctx.backend.torch_operand(...)`. Real-ray autograd needs centred, unfolded systems with angle or object-height fields; other geometries report a capability gap.
- SciPy alternatives: `trf` (keeps `--min/--max` bounds), `lm`, `lbfgsb`, derivative-free `nelder-mead` and `powell`.
- Every trial passes a step gate: no part interference and at least 95 % valid rays in every field and wavelength. Unsafe steps are halved up to 12 times; only accepted points become the retained best. The torch path also adds continuous hinge losses for TIR, intersection domain and aperture violations; those guarded intersections are never measured results.
- `opt global --max-seconds S [--method de|basinhopping|shgo]` searches with SciPy (`de` and `shgo` need `--min/--max` on every variable) and keeps its ten best systems free of interference in `lenses/global/NNN/`. It refuses to run next to existing `GLOPT_*` lenses; `lens save --as` one elsewhere before searching again from it.
- `opt glass [--surfaces R] [--catalog C]` substitutes glasses from an in-production catalog (default `SAOS_CDGM_INPROD`) by a greedy neighbour search that re-optimises each candidate.
- `opt vary --param material --mode model` varies nd/Vd as a model glass; it must become a real glass before the OpticStudio stage.

Read the result, not just the merit:

- `requested_algorithm`, `algorithm` and `fallback` say what actually ran; `inner_loop` says whether it edited the live lens or rebuilt it each step (slower).
- `improved` and `converged` are separate; `succeeded` needs both, an admissible saved point and no interruption. `stopped_by` is null, timeout or cancel. An unfinished review returns `completed: false, valid: false`: finish the checks before recording a round.
- `review` holds the interference check after the run and, with `--measure <ids>|hard|all`, the demand checks before and after. Pass `--measure` with the fast checks the merit stands in for. A run that lowered the merit but made `worse` non-empty or `valid` false is no progress.
- If an optimiser fails mid-run, the lens is marked unsaved: `lens save --as` keeps what it reached, `lens load --discard` drops it.

When you optimise outside the lens's merit function (your own `exec` loop), record the value you optimised with `saos round --merit <value>`.

## Record rounds

Record a round when you have a design worth judging: after an optimisation run, a material substitution or a structural change, not after every tweak. Trials go through `saos check` (measures without recording; `--only <id>,...` measures just the affected items, never achieved) or `explore/`. After two optimisations or fifteen scripts without a round, `opt`, `exec` and `scan` results carry a `since_round` reminder; a long history with an empty ledger leaves the stop rule nothing to judge.

`uv run saos round --job J --tier <tier>[,<tier>...] --note "<what you did>"` names every tier the round used. Tiers, low to high: `local`, `variables`, `materials`, `global`, `structure`. The ladder records what was tried; it is not an order. When evidence says a tier cannot help, `uv run saos rule-out --job J --tier <tier> --reason "<evidence, e.g. scans/NNN>"` instead of spending a round on it; this holds until the next clear gain.

Read the decision (`achieved`, `exhausted`, `limit`, `continue` with `next_tier`) and compare rounds by shortfall, never by merit. A round whose parts cross carries `parts_clash` and is never achieved or deliverable. Each round exports `evaluations/NNN/viz.json` for the workflow window; a failed export is reported under `viz` and does not affect the round.

## Changing the checks

Changing `checks/` (or the recipe and kit code they import) is safe. The next round first re-measures the best round so far with the new checks (a `rebase` row) and compares from there. Equal numbers (a speed-up, a switch to a library) keep the comparison window; different numbers start a new one, and the old best lens stays deliverable under the new checks. An unintended difference is a bug in the new checks; an intended one is a demand question. A new window does not reset `max_rounds`, and after `limit` or `stopped` changed checks are refused like any other round.

## Gate expensive runs on the gap

Before a run you expect to take more than about ten minutes, or any `global` or `structure` attempt, tell the user for each failing `must` how far it is off, the mechanism you expect to close it, the variables acting on that mechanism and a rough expected gain. When an objective misses by a multiple of its pass line, or a diagnosis shows a cap in this structure (light clipped by an aperture the variables cannot widen, an aberration they cannot change), optimising is not the next step: diagnose ([diagnosis.md](diagnosis.md)) and take the result to the user.

Accuracy follows reachability. Convergence and sampling audits earn their time once the design is close to its pass lines; while it misses by a multiple, sample just finely enough that the verdict cannot flip.

If the structure must change beyond the approved description, describe the new model and get it approved before recording rounds on it.
