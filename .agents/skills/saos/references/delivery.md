# Report, tolerance and stopping

The outcome of this stage is a current report the user has accepted or rejected, or a recorded decision to stop.

## Re-measure on OpticStudio

Rounds recorded on the rtc instance are re-measured before anything else in this stage. On an OpticStudio instance ([session.md](session.md#the-rtc-instance-stages-1-and-3)), run `uv run saos round --job J --confirm --detach` (no lens load needed; `--of N` picks a round other than the one the report would deliver). Its numbers replace that round's numbers in the ledger.

- `agrees: true`: continue with this stage on OpticStudio.
- `agrees: false`: OpticStudio reached another verdict (see `verdict` and `diffs`). Tell the user the differing items, return to stage 3 on the rtc instance, and record new rounds; the decision now uses OpticStudio's numbers.

`saos report` refuses an rtc round that was not re-measured.

## Tolerance scan

When the demand or the user cares how the design survives manufacturing and assembly, run a tolerance scan on the lens of the round you will deliver, before the report. Load that round's lens, then run `uv run saos scan --job J --plan plan.json` with:

```json
{"tolerance": {"items": [{"key": "3.radius", "min": -0.02, "max": 0.02, "source": "maker table, sources/tolerances.pdf"}],
               "samples": 200, "seed": 1, "sensitivity": true},
 "compensate": "quick_focus"}
```

- Each item names a numeric parameter as in scripts: thicknesses, radii or curvatures, `<surface>.nd` / `.vd` for index and Abbe errors, `object.<n>.x|tilt_x|...` for non-sequential parts, coordinate-break `par<N>` for element tilts and decenters. Optional `dist` and `relative`.
- Every tolerance has a `source` (a maker's table, the user's words, a standard). Never invent tolerances; ask for them like any other bound.
- Sensitivity (each item alone at its limits) ranks what matters. Monte Carlo gives each check's distribution and pass rate, and the yield (all hard bounds met, all measured goals met).
- Choose the sample count from the yield precision the user needs: the standard error of a yield p from N samples is about √(p(1−p)/N). Non-sequential checks trace per sample, so time one check run first.

Without such a need, skip it and offer it when you hand over the report.

## The report

`uv run saos report --job J` writes `report/index.html` in the user's language: status, the delivered lens, its materials, and for each unmet goal how far it is off. The delivered lens is the achieved round with the smallest shortfall, else the smallest shortfall among deliverable rounds. With no valid round it says so instead of naming a best lens. The report includes the latest tolerance scan and says whether it ran on the delivered lens.

- The JSON result names a lens in `deliver` only when it is deliverable (`deliverable`, `headline`). Otherwise `deliver` is null and the round shown for diagnosis is under `diagnostic`: never pass it on as the delivery.
- A later round, stop, rule-out or tolerance scan makes the report stale, and the workflow shows delivery as unfinished until you run it again. Regenerate an outdated report before asking for acceptance; the displayed report must match its sealed HTML and metadata.
- When the user wants changes, continue within the demand and stop rules, or return to demand or model confirmation when those change.

## Stopping

Stopping is a conclusion too. When the evidence says the demand cannot be met with what is allowed, show the user the evidence and the options. When they decide to stop, record it with `uv run saos stop --job J --reason "<why, citing the evidence>" --quote "<their words, with the option text>"`. Only the user's decision stops the rounds; the report then shows the reason and their words.
