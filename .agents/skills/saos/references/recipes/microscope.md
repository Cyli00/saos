# Recipe: dry infinity-corrected microscope objectives

Use this when the user needs a dry, infinity-corrected, all-spherical objective with NA ≤ 0.65, achromat or plan achromat, roughly 4x–40x, evaluated with an ideal reference tube lens (default f = 200 mm).

Not covered: immersion objectives, apochromats, finite conjugates (160 mm tube length), aspheres, a real tube lens, parfocal mechanics. For those, use the general workflow without this recipe.

Code: `saos.recipes.microscope`; commands: `uv run saos microscope --help`. The full specification and acceptance definitions, with sources, are in `docs/recipes/microscope/optical-spec-and-acceptance.md` (Chinese); read the section you need, not the whole file.

## Backend status

The acceptance evaluation uses the operands `OSCD`, `DISG`, `MTFN` and `STRH`, which the Optiland backend does not provide. Until that changes, the full acceptance runs only in OpticStudio. In the Optiland stage, write demand checks from supported operands and analyses for the items you need there (focal length, NA, working distance, RMS wavefront, axial and lateral colour), and keep the full acceptance for the OpticStudio stage. Tell the user which items the Optiland report cannot judge.

## Stage 1: questions for the demand

| Question | Default when the user has none | Source |
|---|---|---|
| Nominal magnification | none: always ask | — |
| NA | 4x / 10x / 20x / 40x → 0.10 / 0.25 / 0.40 / 0.65; otherwise ask | spec doc §2.2 |
| Correction class | plan achromat when the user mentions photography, measurement or "the whole field sharp"; else achromat | spec doc §2.2 |
| Visual or camera | visual; for a camera ask the pixel size p, which enables an MTF check at 500/p lp/mm on the intermediate image | spec doc §2.3 |
| Cover glass | biological use: 0.17 mm, n_e 1.5255, ν_e 56; reflected light: none | spec doc §2.2 |
| Minimum working distance | 10x ≥ 10.6, 20x ≥ 1.2, 40x ≥ 0.6 mm; 4x has no verified default: ask | spec doc §2.2 |
| Distortion limit | only when used for measurement; offer 1 % as an assumption to confirm | spec doc §2.3 |
| Glass catalogue | CDGM, status Standard or Preferred | spec doc §2.2 |

Wavelengths are F′ 479.99, e 546.07 (primary) and C′ 643.85 nm, as ISO 19012-2 defines achromatism on them. Several pass lines in the spec are marked "to be calibrated" (optical length, clear diameter, minimum thicknesses, field wavefront, lateral colour, MTF); treat them as `agent assumption` sources and put them through the `weak_basis` walk.

## Stage 1: checks

`saos microscope accept --job J --spec <job-relative spec.yaml>` builds an acceptance model (objective, cover glass and reference tube lens, apertures and vignetting cleared), focuses by working distance only (on-axis e-line c4 = 0) and judges magnification, working distance, optical length, clear diameter, glass status, surface types, centre and edge thickness, air gaps, axial RMS wavefront ≤ λ/14, axial colour ≤ 2·δob, |OSC| ≤ 0.0025, plan field ratio ≥ 0.85 (plan achromat), field RMS wavefront, lateral colour, MTF and distortion. It returns `spec_shortfall` and writes `eval/<lens>.accept.json`. The spec format and an example are in §2.6 of the spec doc.

It evaluates the saved lens file (unsaved changes are refused), rebuilds and reloads the lens around the evaluation, and the focus search once took 174 s on a slow input; give it long timeouts or `--detach`. Using `ops.accept` inside a demand check script has not been run in the current workflow; confirm it leaves the lens unchanged before relying on it in rounds.

## Stage 2: starting forms

`uv run saos microscope library query --na A --magnification M` ranks the admitted patent embodiments in `library/microscope/entries/`. Entries are at the patent's own scale:

1. Scale radii and thicknesses to the target focal length f_obj = f_TL / M.
2. Set the cover glass from the demand.
3. Swap glasses into the allowed catalogue before round 0.

Adding a patent: `docs/recipes/microscope/library.md` (extract, `library check`, `library add`, `library verify`). The `library check` paraxial pre-screen screens transcriptions only; it is never a design result.

## Stage 2: modelling conventions

- Surface comments `saos:object`, `saos:cover_top`, `saos:stop`, `saos:tube` mark the standard layout (`saos.recipes.microscope.layout`); the acceptance relies on them.
- A stop at the rear focal plane makes the objective object-space telecentric.

## Stage 3: optimisation that worked before

In OpticStudio, this policy recovered all 65 perturbed benchmark tasks, most in one or two rounds:

- Variables: radii and the working distance; then all objective thicknesses.
- Merit: RMS wavefront to centroid (6 rings × 6 arms), EFL, and structural limits `CTGT`, `MNCG`, `MNEG`, `MNCA`, `MNEA`, `OPLT`, `MXSD`.
- When items fail, add `AXCL`, `LACL`, `OSCD`, `FCGS`/`FCGT` and raise the weight of the failing ones ×3 per round, up to ×3⁵.

These are OpticStudio operands; in Optiland, build the equivalent merit from supported operands and a `loss` script.

## Evidence

- Verified in OpticStudio under the former zea workflow: reproduction of all 25 library entries (paraxial pre-screen agreed with them within 5.4e-4 in EFL and 0.0134 mm in working distance), a minimal design loop, and the 65 benchmark tasks. Records are in Git history; the benchmark tool was removed on 2026-10-17.
- Not verified: any run on the Optiland backend; any job of the current saos workflow; acceptance used as a demand check.
