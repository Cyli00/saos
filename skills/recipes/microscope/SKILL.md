---
name: saos-recipe-microscope
description: Recipe for dry, infinity-corrected microscope objectives (NA ≤ 0.65, achromat or plan achromat, about 4x–40x) evaluated with an ideal reference tube lens. Load after the saos harness is confirmed when the user needs such an objective.
---

# Recipe: microscope objective

Distilled from the original zea workflow (steps 1–6, see `docs/recipes/microscope/step3-minimal-loop.md` … `docs/recipes/microscope/step6-interview-and-plan.md`). Commands live under `uv run saos microscope …`; helpers under `saos.recipes.microscope`.

## 1. Scope

Dry objectives, infinity corrected, all-spherical, NA ≤ 0.65, achromat or plan achromat, evaluated with an ideal paraxial reference tube lens (default f = 200 mm). Not covered: immersion, apochromats, finite conjugate (160 mm tube length), aspheres, the real tube lens, parfocal mechanics.

## 2. Framing questions

- Nominal magnification — must come from the user. NA has agreed defaults only for 4x/10x/20x/40x (0.10/0.25/0.40/0.65); otherwise ask.
- Visual or camera; a camera needs the pixel size (enables an MTF check at 500/pixel lp/mm on the intermediate image).
- Cover glass (thickness, nd, νd) or none.
- Minimum working distance; 4x has no verified default — ask.
- "The whole field should be sharp" means plan achromat and a field range; measurement use needs a distortion limit.
- Glass catalogs (default CDGM, status Standard/Preferred).

`saos microscope interview check FILE` lists missing items; `saos microscope plan create/confirm/handoff` renders and binds an HTML review page (Chinese and English versions; show the user the one in their language) if the user wants one.

## 3. Checks

The recipe's own acceptance (`ops.accept`, via `saos microscope accept`) builds an acceptance model (objective + cover glass + reference tube lens, apertures and vignetting cleared), focuses by working distance only (on-axis e-line c4 = 0) and judges: magnification, working distance, optical length, clear diameter, glass status, surface types, centre/edge thickness, air gaps, axial RMS WFE ≤ λ/14, axial colour ≤ 2·δob, OSC ≤ 0.0025, plan field ratio ≥ 0.85 (plan achromat), field RMS WFE, lateral colour, MTF, distortion. Details and sources: `docs/recipes/microscope/optical-spec-and-acceptance.md`. To use it inside a saos harness, add a `script` check whose `run(ctx)` calls `ops.accept(ctx, spec=...)` and returns `spec_shortfall`.

## 4. Starting designs

`saos microscope library query --na A --magnification M` ranks 25 admitted patent embodiments (`library/microscope/entries/`). The library prescription is at the patent's own scale: scale radii and thicknesses to the target focal length, set the cover glass and swap glasses into the allowed catalogs before round 0. New patents: `docs/recipes/microscope/library.md` (extract → `library check` → `library add` → `library verify`).

## 5. Modelling notes

Standard layout surface comments `saos:object`, `saos:cover_top`, `saos:stop`, `saos:tube` (see `saos.recipes.microscope.layout`). Rear-focal stop = object-space telecentric. Focusing uses the paraxial restart and secant back-off of `acceptance.focus_on_axis`; a slow input once took 174 s, so give long timeouts.

## 6. Optimisation

Reference policy that recovered all 65 perturbed benchmark tasks (most in 1–2 rounds): local — radii + working distance with an RMS wavefront (centroid, 6 rings × 6 arms) merit plus EFL and structural limits (`CTGT`, `MNCG`, `MNEG`, `MNCA`, `MNEA`, `OPLT`, `MXSD`); variables — free all objective thicknesses; then weight the failing items ×3 per round (up to ×3⁵) with `AXCL`, `LACL`, `OSCD`, `FCGS/FCGT`. Code: `experiments/bench/runner.py`.

## 7. Verified

Reproduction of 25 library entries; real-OpticStudio minimal loop (`tests/test_zemax_loop.py`); 65 benchmark tasks (`docs/recipes/microscope/step5-calibration.md`). The stop parameters of the old round rule (3 rounds, 1 %, 20) were never calibrated.
