# Report, tolerance and stopping

The outcome of this stage is a current report the user has accepted or rejected, or a recorded decision to stop.

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
- When `backend` is `optiland` (`opticstudio_verified` false), the page says the numbers are Optiland's and not yet verified in OpticStudio; tell the user so.
- Regenerate an outdated report before asking for acceptance. The displayed report must match its sealed HTML and metadata.

## Acceptance into OpticStudio

Verifying and optimising further in OpticStudio comes only after the user accepts this report. Record their words with `uv run saos zos begin --job J --quote "<their words>" [--round N] [--max-rounds K]`. The selected round must be deliverable under the current demand and checks and contain no model glasses. If the user rejects the report, continue designing in Optiland within the current demand and stop rules, or record their decision to stop; do not enter OpticStudio. What happens next: [opticstudio.md](opticstudio.md).

## Stopping

Stopping is a conclusion too. When the evidence says the demand cannot be met with what is allowed, show the user the evidence and the options. When they decide to stop, record it with `uv run saos stop --job J --reason "<why, citing the evidence>" --quote "<their words, with the option text>"`. Only the user's decision stops the rounds; the report then shows the reason and their words.
