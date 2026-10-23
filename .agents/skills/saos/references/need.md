# Agree the need

The outcome of this stage is a confirmed `sources/demand.yaml`: conditions and acceptance criteria the user approved, each limit with a source, each `must` with a feasibility verdict, and checks that measure the real quantities.

## Interview in rounds

Prepare a whole round before you ask anything: every question you can ask now, numbered, each with your recommended answer. Put first, and mark, the questions whose answer can change the structure or a pass line, so the user can answer those even if they skip the rest. Explain optics terms in plain words. Look facts up yourself (repository files, `ref/docs`, the OpticStudio manual, the web) instead of asking.

- With an ask-user tool, ask through it. Your recommendation is the first option and says so; the other options are the realistic alternatives; a value the user must type goes into the free-text answer. When a round exceeds one call's limits, ask in consecutive calls with the marked questions first and do no other work between them.
- Without one, write a numbered list in which each item is a `Question:` line followed by an `Advice:` line (your recommended answer and its reason in one sentence), worded so "yes" accepts the advice.

Append every round to `sources/interview.md`: the questions, the options or advice shown, and the user's answers verbatim (the full text of the chosen option and anything typed). Whenever a quote records a choice (`demand confirm`, `lens approve`, `zos begin`, `stop`), it contains the full text of the options shown and the one chosen, not just "c".

Material and bought-in part searches that should end in one sourced table go to the `optic-material-explorer` agent when it is available: give it the hard limits, the columns you need and a download folder under `sources/`.

## Question the structure before you write it down

A structure the user prescribes, or asks you to change, is a hypothesis: the user may not be a specialist in this part of optics, and an unreachable structure costs hours of optimisation later. Before you show the demand, reason whether it can reach every `must`, and say what you find even when nobody asked:

- what each element is there to do, and whether the rest can still do it after a requested change;
- the bounds no optimiser can move: conserved or invariant quantities (étendue, the Lagrange invariant, the Petzval sum), the diffraction limit, one-way conditions such as critical angles, the space and apertures available, and the independent degrees of freedom against the objectives they must hold at once;
- how each part is made and what that process cannot do (coatings, surface forms, sizes, materials); find out before you make a part's shape a variable.

Write each estimate out so the user can follow it: the formula, its inputs with their source, the result and what it decides. Then check the one or two deciding numbers on the backend with a deterministic calculation on a minimal model (a ray trace, or an operand such as `RAID`, `RANG`, `REAY`, `SSAG`, `ETVA`), and say which constraints the minimal model leaves out. Do not search with an optimiser here: it exploits every constraint a sketch leaves out (edge thickness, apertures, lost rays), and finding nothing proves little. Before a check you expect to take more than a few minutes, tell the user how long and why.

Give each `must` a verdict, reachable, marginal or not reachable, with the deciding argument. For marginal or not reachable, offer alternatives that change the deciding bound, not a list of variables to try.

## Write the demand

Copy [demand.example.yaml](../demand.example.yaml) to `sources/demand.yaml`. Its header states the accepted fields, bounds, weights and what needs reconfirmation.

- `constants` are fixed conditions, `variables` the freedoms with their ranges, `objectives` the measured goals with pass lines (`must`), `minimize` / `maximize` for "as small / large as possible", and hoped-for values (`wish`).
- Every limit names its `source`: the user, a datasheet, a paper, a textbook table, or `agent assumption: <why>`.
- An unknown real-world fact is an open question, never a variable. A question whose answer could change the demand gets `blocking: true`.
- Keep it short: no verbatim conversation, no literature details, no restated workflow rules. `summary` says in a few sentences why the design exists and what is agreed and open.
- The demand carries the hard limits; surface settings in the lens do not, because only the demand is judged.

## Write checks that measure the real quantity

Checks live in `checks/` with every helper module or data file they use; recipe libraries and `saos.kit` they import count too. Any change to them starts a new comparison window (see [rounds.md](rounds.md)).

- Measure the real quantity. Never special-case a surface type or setting to skip a measurement.
- Measure an extreme over the field or spectrum (chief ray angle, relative illumination, transmission) with `ctx.cra`, `ctx.field_profile` or `ctx.wave_profile`. They sample densely, refine around the extreme and report its location and coverage. When `coverage.ok` is false, sample finer or return `inf`, never the coarse value: an edge-only CRA missed 19.88° in mid field, and five wavelengths missed a transmission dip, on the nightvision job.
- State the preconditions (parts do not intersect, the image is real, enough rays arrive, the quantity covers the whole beam and not only the surviving rays) and return `inf` when one fails; a number from an invalid state is worse than no number. Return `inf` when the design gives no valid value; raise only when the check itself cannot run.
- Keep checks read-only: work on a copy, or restore the lens (`ctx.kept`, `ctx.restored`, `ctx.configuration`). A round refuses a check that changed the lens (`LENS_MODIFIED_BY_CHECK`); last-digit rounding by OpticStudio (relative 1e-12) does not count.
- Keep checks fast where you can. Every item reports `duration_s` in `saos check`; slow items cost every evaluation of a search that uses them.

## Confirm and set up the window

Show the demand in plain language with the feasibility verdicts. Go through every limit that `uv run saos demand show --job J` lists under `weak_basis` one by one and get explicit approval of its content. Record the user's words with `uv run saos demand confirm --job J --quote "..."`; the tools keep the full demand text the user saw.

`demand confirm` opens the workflow window (result field `ui`). Then choose its tabs and plotted metrics for this kind of system with `uv run saos ui open --job J --tabs ... --metrics ...`, remembered for the job:

- infinite-conjugate imaging lens: `spot,fan,field,mtf` with focal length, f-number or distortion items;
- microscope objective: spot, MTF and field curvature with magnification, NA, working distance and parfocal length items;
- illumination or non-sequential system: `detectors` with power or uniformity items.

Rounds export only the chosen spot, fan, field and MTF plots, so leave one out only when the user does not need it. `--reset` restores the default tabs; `ui open --job J` reopens a closed window.

Changing a value, range, check, weight, unit, origin, source or stop rule later needs a new confirmation; changing a description, the summary or the questions does not.
