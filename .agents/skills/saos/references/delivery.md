# Report, tolerance and stopping

The outcome of this stage is a current report the user has accepted or rejected, or a recorded decision to stop.

## Re-measure on OpticStudio

Rounds recorded on rtc are re-measured before anything else in this stage: run `uv run saos round --job J --confirm --detach`, which computes on OpticStudio ([session.md](session.md#two-backends-in-one-daemon); no lens load needed; `--of N` picks a round other than the one the report would deliver). Its numbers replace that round's numbers in the ledger.

- `agrees: true`: continue with this stage on OpticStudio.
- `agrees: false`: OpticStudio reached another verdict (see `verdict` and `diffs`). Tell the user the differing items, return to stage 3, and record new rounds; the decision now uses OpticStudio's numbers, and `saos report` refuses until a new round is recorded, unless the user has stopped.

`saos report` refuses an rtc round that was not re-measured.

## Tolerance scan

When the demand or the user cares how the design survives manufacturing and assembly, run a tolerance scan on the lens of the round you will deliver, before the report: load that round's lens, then `saos scan --plan` (format: `uv run saos scan --help tolerance`).

- Every tolerance has a `source` (a maker's table, the user's words, a standard). Never invent tolerances; ask for them like any other bound.
- Sensitivity (each item alone at its limits) ranks what matters. Monte Carlo gives each check's distribution and pass rate, and the yield.
- Choose the sample count from the yield precision the user needs: the standard error of a yield p from N samples is about √(p(1−p)/N). Non-sequential checks trace per sample, so time one check run first.

Without such a need, skip it and offer it when you hand over the report.

## The report

`uv run saos report --job J` writes `report/index.html` in the user's language (which round it delivers: `uv run saos report --help`). Page 1 is a one-sheet summary, page 2 the full record; when pointing the user at a detail, say which page holds it.

- The JSON result names a lens in `deliver` only when it is deliverable (`deliverable`, `headline`). Otherwise `deliver` is null and the round shown for diagnosis is under `diagnostic`: never pass it on as the delivery.
- A later round, stop, rule-out or tolerance scan makes the report stale, and the workflow shows delivery as unfinished until you run it again. Regenerate an outdated report before asking for acceptance; the displayed report must match its sealed HTML and metadata.
- When the user wants changes, continue within the demand and stop rules, or return to demand or model confirmation when those change.

## Stopping

Stopping is a conclusion too, even before any round ([the reach gate](model.md#the-reach-gate)). When the evidence says the demand cannot be met with what is allowed, show the user the evidence and the options. When they decide to stop, record it with `uv run saos stop --job J --reason "<why, citing the evidence>" --quote "<their words, with the option text>"`. Only the user's decision stops the rounds; the report then shows the reason and their words. In an autonomous job you take the decision: `saos stop --job J --reason "..."` without `--quote`, and the report marks it as yours.
