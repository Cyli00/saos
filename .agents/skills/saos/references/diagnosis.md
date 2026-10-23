# Diagnose before you escalate

Use this when progress stalls, before an expensive escalation, or when a round `brief` sets `review`. Think from geometrical optics instead of trying everything; skip what the evidence already settles. The outcome is either a ranked set of variables worth optimising, or evidence that the structure cannot reach the target, to rule out tiers or take to the user.

## Is the target reachable at all?

Climb from the cheapest test: conserved quantities and invariants (étendue, the Lagrange invariant, the Petzval sum); then one point on axis under every real constraint (apertures, clearances, pass rate, TIR); then the full field. A failure low on this ladder is a structure question, and optimising higher up is wasted.

## Which variables move the target?

1. Name the objective with the largest weighted gap and the mechanism behind it: which aberration or limit (spherical, coma, astigmatism, field curvature, distortion, axial or lateral colour, vignetting, aperture, Lagrange invariant), seen in Seidel terms, ray fans, field curves or pass rates.
2. Reason which parts of the structure can change that mechanism and which cannot. Some quantities are invariant to whole classes of variables: the Petzval sum depends only on surface powers and indices, so aspheric and freeform terms only trade field curvature for astigmatism. Probe the few variables your reasoning ranks highest (one to six). Add a control that should not move the target whenever the ranking decides what you optimise; without it a small effect cannot be told from noise.
3. Write the predictions first (mechanism, direction, rough size), save the lens, and run the probes as a scan: `uv run saos scan --job J --plan plan.json` with a `probes` plan. The base and every probe at ±delta run at once on the worker pool, each from the saved lens, with a compensator so defocus does not hide the aberration: `"compensate": "quick_focus"`, your own script `run(w)`, or a `solve`. Quick Focus moves the thickness before the image surface and can fail silently on folded or partly failing systems; the result says when it never moved. Choose delta as a meaningful fraction of the variable's allowed range. The lens is left unchanged; results go to `scans/NNN/`.
4. Read `ranking` and `review`. A `valley` (or `peak`) means both sides moved the target the same way: the variable is already at its optimum, which says nothing about the structure. Optimise first on the variables that move the target. If a prediction failed or a control moved, your model of the system is wrong: fix that before optimising. If nothing moves the target and your first-order reasoning agrees, it is a structure question: rule out the tiers that cannot help (`saos rule-out`, citing `scans/NNN`) and go to `structure`, or ask the user.

## Probe plan format

```json
{"target": "spot", "mechanism": "Petzval field curvature: ~73 um sag at 150 um image height",
 "compensate": "quick_focus",
 "probes": [
   {"id": "l1_rear_r", "key": "3.radius", "delta": 0.02,
    "prediction": "less lens-1 power lowers the Petzval sum; spot down by a few um", "expect": {"spot": "down"}},
   {"id": "l2_a4", "script": "explore/l2_a4.py", "delta": 0.001, "control": true,
    "prediction": "trades astigmatism only, Petzval unchanged", "expect": {"spot": "none"}}]}
```

A built-in change is a parameter `key` as in scripts (`"3.radius"`, `"lens dome.conic"`, `"12.par1"`; probe a flat surface by its `curvature`). Anything else is a script in the job defining `run(w, delta)`. `measure` (optional) lists the demand items to measure; the default is every item with a bound. Other plan types: `uv run saos scan --help`.
