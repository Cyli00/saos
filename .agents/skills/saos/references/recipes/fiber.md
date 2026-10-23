# Recipe (draft): side-viewing imaging-fibre probes

Use this when the job designs a sub-millimetre side-viewing probe: imaging fibre face → lenses → 45° fold prism → glass tube wall → tissue, at 488 nm (excitation, focusing) and 532 nm (emission reference).

Not covered: the proximal side (beam splitter, relay, camera), volume scattering, tolerancing.

**Draft.** No fibre job has been delivered. The measurement library was lifted from six jobs that copied the same check helpers (`fiber-air`, `fiber-water`, `fiber-grin`, `fiber-grin-no1b`, `fiber-grin-edmund`, `fiber-printed-relay`). Their answers are not defaults for a new user.

## Backend status

`saos.recipes.fiber.side_view` reads the lens through the ZOS-API (`ctx.system`), so none of its checks run in the Optiland stage: they fail with `BAD_ARGS`. Until the library is ported, choose before confirming the demand:

- write Optiland checks for the items the exploration needs (spot, working distance, focus shift, pass rate through `ctx.spot`, `ctx.trace` and operands), and use this library only after `zos begin`; or
- tell the user the Optiland report cannot judge these items, and agree how acceptance will work.

Either way, the Huygens PSF items (`psf_*`, `ee80`) exist only in OpticStudio.

## Stage 1: questions for the demand

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
- `psf_z` is the slowest item: 31–61 Huygens PSFs per field at about 0.175 s each, 6.8–27 s per evaluation on the printed lenses. Keep it out of search objectives. In the daemon it spreads its five fields over the worker pool, starting it if needed and leaving 2 licences free; a detached scan holding the pool makes it run serially.
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

## Stage 3: optimisation

- A Nelder–Mead on the full shortfall (18 variables, one hour) stopped unconverged after 48 evaluations. Optimise on smooth proxies and keep the PSF items for rounds.

## Evidence

- 2026-10-17: the library reproduced the `fiber-printed-relay` helpers (version 6) on real OpticStudio, all 20 checks.
- 2026-10-19: after moving statistics to `saos.kit`, 16 real lenses × 20 checks matched the previous library: 261 of 320 bit-identical, the rest within 1.4e-13 relative, every `inf` unchanged.
- Not verified: the Optiland backend (the library cannot run there), any delivered design, the GRIN variant, non-sequential models.
