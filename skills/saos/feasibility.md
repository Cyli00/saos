# Feasibility by hand: bounds and formulas

Reference for `skills/saos/SKILL.md` §1 ("Question the structure before you write it down"). These are the first-order formulas of geometrical optics that decide most "can this structure reach the `must`?" questions in minutes. They are estimates: they decide the direction, and the deciding number is then checked in OpticStudio (see the end of this file). Sources:

- [LXT] 李晓彤《几何光学·像差·光学设计》第 3 版 (`ref/docs/`): eq. (1-5) p. 6 critical angle; p. 15 surface power; eq. (2-23) p. 18 Lagrange invariant; eq. (2-28) p. 19 transfer; eq. (7-11) p. 105 sine condition; eq. (8-20) p. 121 and §8.7 p. 137 Petzval; §8.3 p. 128 thin-lens colour; §10.3 p. 154 depth of focus; eq. (12-17) p. 195 resolution; eq. (12-31) pp. 213–214 equivalent air plate.
- Formulas without a tag are elementary geometry or standard results (sphere sag, solid angle, Fresnel reflectance at normal incidence).

## How to write a hand estimate

Write every estimate in one line the user can follow: **formula → inputs (value, unit, where it comes from) → result → what it decides**. For example: "single resin→air surface, collimated input, 4° from the critical angle: NA = n′ sin(I′ − I) with n = 1.55, I = 36.2°, so NA = 0.50; the image needs 0.5–0.67, so this structure is marginal at best."

- Use reduced distances d/n and the products n·u (paraxial) or n·sin U (real). They are what stays constant through flat interfaces and media, so they make the arithmetic short.
- Paraxial angles u underestimate real ones at large apertures (at NA 0.5, U = 30° while u = sin U would give 28.6°). Decide apertures and NAs with n·sin U, powers with the paraxial formulas, and keep a margin.
- A bound from these formulas is necessary, not sufficient: passing it means the structure may work, failing it means it cannot. When an estimate passes or fails by less than about 20 %, the verdict is marginal.
- Label every number as an estimate until OpticStudio has checked it.

## 1. Refraction, the critical angle and the most one surface can bend

- Snell: n sin I = n′ sin I′. Critical angle for n > n′: sin I_m = n′/n [LXT (1-5)].
- Deviation at one surface: δ = I′ − I. From the dense side it grows quickly near I_m; from the less dense side it is limited (at grazing incidence δ_max = 90° − arcsin(n/n′)).
- **Largest convergence from one surface, collimated input, dense → less dense**: the stigmatic surface is a hyperboloid with conic k = −(n/n′)². Its output NA approaches n′ cos I_m = √(n′² − n′⁴/n²) only as the incidence approaches I_m, where transmission falls. With a margin Δ below I_m: I = I_m − Δ, I′ = arcsin(n sin I / n′), NA = n′ sin(I′ − I).
  - Resin n = 1.55 into air: I_m = 40.2°, limit 0.76, with Δ = 4° about 0.50. Into water (1.335): I_m = 59.5°, limit 0.68, with Δ = 4° about 0.40. Into a medium of 1.46: I_m = 70.4°, limit 0.49, with Δ = 4° about 0.25.
  - Rays of an off-axis field arrive tilted, so their incidence is the surface slope plus the tilt: the field beam reaches I_m before the axial beam does.
- A beam that already converges into the surface needs only the remaining Δ(n sin U). Where the earlier part of the system can make the beam converge is limited by its apertures (§3).

## 2. Paraxial ray trace by hand

- Surface power φ = (n′ − n)/r [LXT p. 15]; mirror φ = −2n/r; thin lens in a medium n₀: φ = (n − n₀)(1/r₁ − 1/r₂).
- Refraction n′u′ = nu − yφ; transfer y₂ = y₁ + d·u′ = y₁ + (n′u′)·(d/n′) [LXT (2-28)].
- **Bending a surface must supply**: Δ(nu) = yφ. Trace the marginal ray from the object NA to the image NA (from §3); the change in n·u divided by the ray height at each surface gives the power that surface needs. Compare it with what the surface can be made to do (§1, §4).
- Image distance after the last surface ≈ y/(n′u′) in reduced units.

## 3. Lagrange invariant, NA, magnification and étendue

- Lagrange invariant J = n·u·y = n′·u′·y′ [LXT (2-23)]; real-ray form for a system satisfying the sine condition: n·y·sin U = n′·y′·sin U′ [LXT (7-11)]. With y the field height and n·sin U the NA, **NA′ = NA·|y/y′| = NA/|β|**: halving the magnification doubles the image NA.
- Étendue for a circular field of radius h and an NA: G ≈ π²·h²·NA². It is conserved; no lens can reduce it. Using only a fraction f of the field radius cuts it by f².
- **What two apertures can pass** (one meridian, exact in paraxial phase space): two clear semi-apertures a, separated by a distance L in a medium n, pass the rays with |y| ≤ a at both ends, a region of area 4·n·a²/L in (y, n·u). A beam of field half-height h and NA occupies 4·h·NA. So the pair can pass the beam only if **h·NA ≤ n·a²/L**, and only when the beam is shaped to that region (pupil midway, chief rays crossing at the middle). A beam that must also converge or carry spare margin needs more.
- Beam footprint at a plane: |y_marginal| + |y_chief| ≤ a, with a the clear semi-aperture minus the mechanical margin.

## 4. What a surface you can make can do

- Sphere sag z(r) = R − √(R² − r²) ≈ r²/(2R). A sphere is never wider than R.
- **Power cap from height**: a lens that must cover a footprint of radius r (from §3, the beam at that surface) within a height h, with an edge thickness ≥ e, needs z(r) ≤ h − e, so R ≥ (r² + (h − e)²)/(2(h − e)) and **φ_max = Δn/R_min**. Example: r = 0.13 mm, h = 0.09 mm, e = 0: R ≥ 0.139 mm; resin (1.56) into air φ ≤ 4.0 mm⁻¹, into water φ ≤ 1.6 mm⁻¹.
- Slope: the surface normal tilts by arcsin(r/R) at the edge of a sphere. A process limit on the slope (printing, moulding, polishing) gives R ≥ r/sin α_max, and the slope plus the ray angle is the incidence of §1.
- Aspheres and freeforms concentrate curvature, but the height still limits the mean slope to about h/r, and the edge slope is limited by the process and by the critical angle. They change aberrations, not the first-order bending budget.
- The base of a lens has to cover the beam where the lens sits, not only the beam at its apex.

## 5. Plates, windows and tube walls

- A plane plate of thickness d moves the image along the axis by d(1 − 1/n); in reduced units it is an air plate of thickness d/n [LXT (12-31)]. In a converging beam it also adds spherical aberration and, between two wavelengths, axial colour.
- A cylindrical wall has power only across the cylinder; along its axis it is a plate. Each wall surface gives φ = (n′ − n)/R in that meridian, which shows as astigmatism (two line foci). Example, silica (1.46) tube of inner radius 0.3 mm, light going outwards: from air the inner wall gives about −1.5 mm⁻¹, from water about −0.4 mm⁻¹; the outer wall (radius 0.35 mm) into water gives about +0.36 mm⁻¹.

## 6. Field curvature: Petzval sum against depth of focus

- Petzval sum P = Σ φᵢ/(nᵢ nᵢ′) over all surfaces; the image surface with astigmatism corrected curves with radius ρ = −1/(n′P), so at image height y′ it departs from a plane by **Δz ≈ n′·P·y′²/2** [LXT (8-20), §8.7]. P depends only on the powers and indices: aspheres trade field curvature for astigmatism and cannot change P. Only a power of opposite sign (a negative lens, a concave mirror, a surface with the index step the other way) lowers it.
- Allowed defocus for a geometric spot diameter s in a medium n′ with image NA′: |Δz| ≤ s·n′/(2·NA′). Diffraction depth of focus about ±λ/(n′u′²) [LXT §10.3].
- The field over which the quality holds: y′ ≤ √(2·|Δz|_allowed/(n′·P)).

## 7. Diffraction and resolution

- Airy radius 0.61·λ/(n′ sin U′) [LXT (12-17)]. A required spot smaller than this cannot be reached by any design; a geometric spot much larger than this means aberrations dominate.

## 8. Axial colour

- Thin lens: Δf/f ≈ (n(λ₁) − n(λ₂))/(n − 1), about 1/ν across the band ν is defined for [LXT §8.3]. Several thin lenses: the axial colour of the system scales with Σ yᵢ²·φᵢ/νᵢ; lenses of one sign add up.
- Mirrors have none; plates in a converging beam add some (§5).

## 9. Collection and losses

- Solid-angle fraction of a cone of half-angle θ: (1 − cos θ)/2.
- Cutting the accepted NA from NA₁ to NA₂ keeps about (NA₂/NA₁)² of the light for a uniformly filled pupil (a Lambertian-like source).
- Fresnel reflectance at normal incidence ((n₁ − n₂)/(n₁ + n₂))²; it rises steeply within about 10° of the critical angle, so a surface worked near I_m loses light even before total reflection.

## 10. Count the freedoms

- List the independent first-order freedoms (each power, each spacing, the stop position) and the first-order conditions the structure must hold at once (magnification, image distance or working distance, pupil positions, the beam fitting each aperture, focus between two wavelengths). When the conditions use up the freedoms, the structure is pinned at first order and nothing is left for aberration correction; adding a surface of the right kind, not more aspheric terms, is the remedy.

## Checking the deciding number in OpticStudio

- Use a deterministic calculation on a minimal model: a single ray trace or operand, not a search. Useful operands: `RAID` (incidence angle), `RANG` (ray angle after a surface), `REAY` / `REAX` (ray heights), `SSAG` (sag), `ETVA` (edge thickness), `INDX` (index); in axial systems also `EFFL`, `PMAG` and the paraxial operands.
- Say which constraints the minimal model leaves out (apertures, edge thickness, other fields, lost rays), and whether leaving them out makes the check optimistic or pessimistic.
- Do not use an optimiser before the model is approved. It exploits every constraint a sketch leaves out (radii running to zero, surfaces crossing their base, rays thrown away), and when it finds nothing that proves little. When only a search could decide a verdict, call it marginal and settle it in the first-order analysis on the built model (§3 of SKILL.md).
- Before a check you expect to take more than a few minutes, tell the user how long and why.
