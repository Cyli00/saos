---
name: saos-recipe-fiber
description: Draft recipe for side-viewing imaging-fibre probes (fibre face → lenses → 45° fold prism → glass tube wall → tissue), 488/532 nm, sub-millimetre. Load after the saos demand is confirmed when the job designs such a probe; its measurement library replaces hand-written check helpers.
---

# Recipe (draft): side-viewing fibre probe

**Draft.** No fibre job has been delivered yet. This recipe was lifted early (2026-10-17) because five jobs copied the same check helpers: `fiber-air`, `fiber-water`, `fiber-grin`, `fiber-grin-no1b`, `fiber-grin-edmund` (`checks/grinlib.py`), and `fiber-printed-relay` (`checks/fiberlib.py`, check version 6). Everything below comes from those jobs; treat it as experience, not as rules, and correct it when a job teaches otherwise.

## 1. Scope

A coherent imaging fibre (example: Asahi MBI-450S, PMMA cores, NA 0.5, 450 ± 25 µm, centre 400 µm circle used) imaged sideways through a 45° prism and a cylindrical glass tube (example: fused silica, ID 0.6 / OD 0.7 mm) into tissue modelled as WATER, at 488 nm (excitation, focusing) and 532 nm (emission reference). Lenses are UV-printed resin/glue parts, GRIN rods, or both, on the fibre face and the prism faces. Not covered: the proximal side (beam splitter, relay, camera), volume scattering, tolerancing.

## 2. Framing questions

From the jobs' interviews; the user's answers there are not defaults for a new user.

- Which part of the fibre face is imaged (the jobs used the centre 400 µm circle) and how the image size on the sample is measured (four-direction minimum diameter of edge image centroids vs. both spans).
- Working distance window outside the tube (jobs: (0, 30] µm) and whether the focus must be real in tissue.
- Allowed focus shift between excitation and emission (jobs: ≤ 20 µm).
- Which fields judge image quality (jobs: on axis and ±0.7 in X and Y; the edge only for position).
- GRIN rods allowed, or printed lenses only; the printable index ceiling (jobs: nd ≤ 1.6) and the resin/glue library with sourced 488/532 nm dispersion (`library/materials/`).
- Medium between the prism exit and the tube wall (air, water, index-matching fluid).
- How focus is set in assembly (air gap or glue layer between fibre and prism, locked after focusing).

## 3. Checks

`saos.recipes.fiber.side_view` measures every quantity the printed-relay demand used. A check script is one line, and the 20 checks of one evaluation share one cache, so the geometry test, the on-axis trace, the field traces and the Huygens PSF run once instead of once per check:

```python
from saos.recipes.fiber import side_view as sv

def run(ctx):
    return sv.check(ctx, "spot")
```

| name | quantity (µm unless stated) |
|---|---|
| `fov` | four-direction minimum diameter of the edge image centroids around the on-axis centroid |
| `working_distance`, `focus_shift` | 532 nm real best focus beyond the outer wall; 488–532 nm axial separation |
| `spot`, `spot_axis`, `spot_mid`, `spot_edge` | worst 532 nm RMS spot diameter at the 532 nm on-axis best focus |
| `pass_rate` | worst share (%) of the 21×21 pupil grid reaching the sample, axis and ±0.7 fields |
| `fov_resolved` | field diameter with RMS diameter ≤ 3.7 µm (report only) |
| `psf_x`, `psf_y`, `ee80`, `psf_z` | Huygens PSF FWHM in X, Y, 80% encircled-energy diameter, axial FWHM of the peak. `psf_z` is the slowest item: 31–61 Huygens PSFs per field at about 0.175 s each (6.8–27 s on the fiber-printed lenses, 2026-10-19); keep it out of search objectives. When the daemon's worker pool is already up (2+ workers) and idle, `check` and `round` share its five fields out to the workers; it never starts the pool itself, so run it after a `scan` or `ctx.map` has the pool up if you want the speed-up |
| `wfe_rms` | worst RMS wavefront error to centroid (waves), Zernike standard text export |
| `wall_clearance` | minimum gap of every part to the tube inner wall, from sampled real sags |
| `lens_index` (nd), `lens_base` | highest printed-material nd; base diameter of the prism-exit lens |
| `collect_center`, `collect_mid`, `collect_edge` | geometric collection fraction of 4π, forward solid-angle method |

Every measurement first checks the part geometry (thicknesses along the propagation direction, surfaces defined at their semi-diameters, edge thicknesses) and returns `inf` through `DesignFailure` when it fails. The best focus is `kit.focus_shift` (closed form for straight rays after the sample plane): a parallel beam, or fewer than 3 usable rays, has no focus and is a `DesignFailure` too; rays with |n| ≤ 1e-12 are left out of extrapolation along z, as in `kit.spot`. Surface names and dimensions come from `side_view.Layout`; pass `layout=Layout(...)` for a model with other comments or another fibre.

After switching an existing job's checks to this library, the next `saos round` re-measures the best round with it first; equal numbers keep the comparison window open.

## 5. Modelling notes

- Object = fibre face, object-space telecentric, object height = imaged circle radius; one ideal fold (prism hypotenuse as MIRROR), thicknesses negative after it; the image surface is the sample, a thickness of the outer-wall surface sets the sample plane.
- Surface comments the library looks for: `prism exit`, `tube inner wall`, `tube outer wall`, optional `lens plane` / `lens dome` on the prism exit; printed materials start with `SAOS_`.
- Open one Huygens PSF analysis per measurement and recompute it: in extension mode every new analysis opens and closes a window, and about a hundred in a row crashed OpticStudio (2026-10-16).
- The GRIN jobs used a different convention (`[role]` comment tags, direct launch at the air NA, a segmented boundary audit of the rod, a rebuilt reversed system for collection, glue-layer focus compensation). That code (`checks/grinlib.py`, `grin_boundary.py` in the fiber-grin jobs) is not lifted yet; lift it when a GRIN job uses it again, and give it its own `Layout`.

## 8. Verified

- 2026-10-17: the library reproduced `fiberlib.py` v6 on real OpticStudio, all 20 checks, on lenses of fiber-printed-relay; results and timings in `docs/recipes/fiber/equivalence-2026-10-17.md`.
- 2026-10-19: statistics moved to `saos.kit` (`focus_shift`, `spread`) and collection traces only in-circle pupil points; 16 real lenses × 20 checks against the previous library: 261/320 bit-identical, the rest within 1.4e-13 relative, every inf unchanged; details in the same equivalence document.
- Not verified: any delivered design; the GRIN variant; behaviour on non-sequential models.
