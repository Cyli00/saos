# Recipe (draft): side-viewing imaging-fibre probes

Use this when the job designs a sub-millimetre side-viewing probe: imaging fibre face → lenses → 45° fold prism → glass tube wall → tissue, at 488 nm (excitation, focusing) and 532 nm (emission reference). For a fibre-delivered excitation probe (a fibre point source focused through lenses and a tube wall into tissue, such as a two-photon zoom probe), use the backend status, the diffraction bounds and the starting forms below, and build the checks from `saos.micro` ([micro.md](../micro.md)): focus depth in the medium, sample-side NA, zoom travel, gaps and housing clearance, PSF widths. The side-view library below assumes the fold and the tube comments and does not fit it.

Not covered: the proximal side (beam splitter, relay, camera), volume scattering, tolerancing.

**Draft.** No fibre job has been delivered. The side-view library (`saos.recipes.fiber.side_view`, on top of `saos.micro`) was lifted from six jobs that copied the same check helpers (`fiber-air`, `fiber-water`, `fiber-grin`, `fiber-grin-no1b`, `fiber-grin-edmund`, `fiber-printed-relay`). Their answers are not defaults for a new user.

## Backend status

The measurement library (`saos.recipes.fiber.side_view`) and the diffraction kit work through `ctx` only. rtc refuses the side-view model itself (fold mirror, coordinate breaks), so the daemon builds it on OpticStudio and says so in `backend_switch`. On a probe rtc can build, the geometry and ray items run on rtc. The Huygens PSF and wavefront items (`psf_x`, `psf_y`, `ee80`, `psf_z`, `wfe_rms`), and the edge-thickness precondition (ETVA), come back under `backend_unsupported` until rtc has them: measure those with OpticStudio pinned for that step. Write a job's own checks through `ctx` as well ([need.md](../need.md#write-checks-that-measure-the-real-quantity)).

## Stage 1: diffraction bounds for two-photon PSF goals

Before a PSF width becomes a `must`, write its diffraction floor at the NA the structure can reach. For a uniformly filled pupil, two-photon excitation (intensity squared), paraxial estimate:

- axial FWHM ≈ 1.276 · n·λ / NA²; lateral FWHM ≈ 0.369 · λ / NA (λ in the medium's units of length, n the sample index).
- Example, 920 nm in water (n 1.326): NA 0.19 gives 43 µm axial and 1.8 µm lateral; 20 µm axial needs NA ≈ 0.28, 1 µm lateral NA ≈ 0.34. On the 2026-10-30 zoom job, `psf_z < 20 µm` became a `must` at NA 0.19 with no such bound, and a third lens was tried before anyone saw that the floor was twice the pass line.
- These are estimates: they assume no aberration and a filled pupil; an underfilled Gaussian launch behaves like a Gaussian beam instead. Confirm the deciding number with a Huygens PSF of an ideal (paraxial) lens at that NA before the verdict (Zipfel et al., Nat. Biotechnol. 21, 1369, 2003, give the Gaussian-fit form of the same bounds).

## Stage 2: starting forms

**Untried method** (user decision 2026-10-30; no job has run it yet, so record what it gave in Evidence). The patent and starts libraries hold camera and objective forms at infinite conjugate; on 2026-10-30 a search for finite conjugate, 920 nm, at most five elements matched one of 2456 patent examples. Search them anyway and record the query and hit count in the model notes' `exploration`; then synthesise the start paraxially:

1. Replace each lens to design with a Paraxial surface (focal length a variable) at a trial position, keeping the fixed parts (fibre launch, probe lens, tube wall, sample) real.
2. On rtc, scan focal lengths and spacings (a `saos scan` grid or `minimize` plan) against the first-order goals: focus range over the zoom travel, sample NA, clearances. Keep every solution that meets them with margin, not only the best.
3. Thicken each kept solution into real surfaces of the allowed types and materials with the same powers, check that first-order values held, and screen these candidates on equal terms ([model.md](../model.md#screen-candidates-on-equal-terms)).

## Stage 1: questions for the demand

- For a forward-viewing or zoom probe, how the sample-side NA is defined. Three zoom jobs used three definitions: the marginal height over the focus depth, n·sin u of the marginal ring (`micro.marginal_na`), and half the angle between opposite marginal rays. Over which part of the travel it must hold matters too, at the far end or everywhere.
- Which part of the fibre face is imaged (the jobs used the centre 400 µm circle), and how the image size on the sample is measured (four-direction minimum diameter of edge image centroids, or both spans).
- The working distance window outside the tube and whether the focus must be real in tissue. The jobs first used (0, 30] µm, then the user set 0 ≤ WD ≤ 0.5 ℓs with ℓs = 1/µs ≈ 47 µm, giving 0–23.5 µm. That ℓs is the Yona et al. (2016) estimate for mouse cortex slices at 473 nm: not a general brain-tissue value and not measured at 488/532 nm. Ask which tissue the probe is for.
- Allowed focus shift between excitation and emission (jobs: ≤ 20 µm).
- Which fields judge image quality (jobs: on axis and ±0.7 in X and Y; the edge only for position).
- GRIN rods allowed, or printed lenses only; the printable index ceiling (jobs: nd ≤ 1.6 for the air version, nd ≤ 1.7 for the water version); resins and glues with sourced 488/532 nm dispersion from `library/materials/`.
- Whether the fibre face, lens 1 and the prism entrance are glued. An air-gap version is a control and its performance never stands in for the glued one. Choose the glue and the resin together.
- The medium between the prism exit and the tube wall (air, water, index-matching fluid).
- How focus is set in assembly (air gap or glue layer between fibre and prism, locked after focusing).
- The fibre's glass, dispersion and real launch angle distribution if known; do not infer the core material from NA alone.

Example fibre: Asahi MBI-450S (PMMA cores, NA 0.5, 450 ± 25 µm). Example tube: fused silica, ID 0.6 / OD 0.7 mm. Tissue is modelled as WATER.

## Stage 1: checks (OpticStudio)

A check script is one line; the checks of one evaluation share a cache, so geometry, traces and PSFs run once:

```python
from saos.recipes.fiber import side_view as sv

def run(ctx):
    return sv.check(ctx, "spot")
```

| Name | Quantity (µm unless stated) |
|---|---|
| `fov` | four-direction minimum diameter of the edge image centroids around the on-axis centroid |
| `working_distance`, `focus_shift` | 532 nm real best focus beyond the outer wall; 488–532 nm axial separation |
| `spot`, `spot_axis`, `spot_mid`, `spot_edge` | worst 532 nm RMS spot diameter at the 532 nm on-axis best focus |
| `pass_rate` | worst share (%) of the 21×21 pupil grid reaching the sample, axis and ±0.7 fields |
| `fov_resolved` | field diameter with RMS diameter ≤ 3.7 µm (report only) |
| `psf_x`, `psf_y`, `ee80`, `psf_z` | Huygens PSF FWHM in X and Y, 80 % encircled-energy diameter, axial FWHM |
| `wfe_rms` | worst RMS wavefront error to centroid (waves) |
| `wall_clearance` | minimum gap of every part to the tube inner wall, from sampled real sags |
| `lens_index` (nd), `lens_base` | highest printed-material nd; base diameter of the prism-exit lens |
| `collect_center`, `collect_mid`, `collect_edge` | geometric collection fraction of 4π |

- Every measurement first checks part geometry (thickness along propagation, surfaces defined at their semi-diameters, edge thickness) and returns `inf` when it fails. A parallel beam, or fewer than 3 usable rays, has no focus and also returns `inf`.
- `psf_z` is the slowest item: 31–61 Huygens PSFs per field at about 0.175 s each, 6.8–27 s per evaluation on the printed lenses. Keep it out of search objectives. `layout=Layout(psf_z_scan="outward")` computes only the planes around the focus (offline, 7–9 instead of 31 for a single peak), a different definition: run both on the job's lens once and keep outward only when they agree. In the daemon it spreads its five fields over the worker pool, starting it if needed and leaving 2 licences free; a detached scan holding the pool makes it run serially.
- None of these include absorption or fluorescence (the material AGFs carry refractive index only); say so in the report.

## Stage 2: modelling conventions

- Object = fibre face, object-space telecentric, object height = imaged circle radius. One ideal fold (prism hypotenuse as MIRROR), negative thicknesses after it. The image surface is the sample; the thickness after the outer-wall surface sets the sample plane.
- Surface comments the library looks for: `prism exit`, `tube inner wall`, `tube outer wall`, optional `lens plane` / `lens dome` on the prism exit. Printed materials start with `SAOS_`. For other comments or another fibre, pass `layout=Layout(...)`.
- Printed lenses use resin IDs from the materials library with version, AGF name and process assumptions recorded; `nd 1.60 / Vd 50` model glasses appear only in exploration labelled as such.
- Under a glue layer, a surface's power depends on the real index difference at both wavelengths; check its size and sign. "Index-matched" is not a goal for every interface. NOA 61 has a full dispersion formula and serves as a reference, not as the default glue; low-index glues with single-point data are enough to screen but not to claim two-colour correction.
- The fibre's glass is unknown: never use the glue index as the core index. After changing the medium at the fibre face, recheck the object NA and the ray launch model.
- The prism is catalogue N-BK7 and its coated hypotenuse is an ideal mirror. Record whether there is a glue layer and whether glue touches the reflecting face.
- A flat substrate under a Generalized Fresnel surface must not cross its mounting plane; fix the substrate before optimising.
- The GRIN jobs used a different convention (`[role]` comment tags, direct launch at the air NA, a segmented rod boundary audit, a reversed system for collection, glue-layer focus compensation). That code is not lifted; lift it, with its own `Layout`, when a GRIN job uses it again.

## Evidence

- 2026-10-17: the library reproduced the `fiber-printed-relay` helpers (version 6) on real OpticStudio, all 20 checks.
- 2026-10-19: after moving statistics to `saos.kit`, 16 real lenses × 20 checks matched the previous library: 261 of 320 bit-identical, the rest within 1.4e-13 relative, every `inf` unchanged.
- 2026-10-30 (`fiber-var-zoom`, OpticStudio): round 22 at sample NA ≈ 0.19 measured two-photon axial FWHM 65–72 µm and lateral 2.2–2.5 µm, above the paraxial floors of 43 µm and 1.8 µm above, as an aberrated lens should be. The floors themselves are not yet checked against a Huygens PSF of an ideal lens.
- Not verified: any delivered design, the GRIN variant, non-sequential models, the paraxial synthesis of starting forms, and the 2026-10-30 rewrite of the library through `ctx` (no OpticStudio comparison with the previous library yet).
