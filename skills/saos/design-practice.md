# Design practice: variables, boundary conditions, manufacturability

Reference for `skills/saos/SKILL.md` §4. It is textbook experience, not a rule: use what fits the system, and when a value enters the demand, cite it as its `source`. Sources:

- [LXT] 李晓彤《几何光学·像差·光学设计》第 3 版, ch. 17 (book pp. 311–321): §17.1, §17.3.5 variable choice, §17.4 boundary conditions, §17.5 manufacturing and assembly.
- [ZXT] 张欣婷《光学设计及Zemax应用》, design examples (e.g. 设计实例四: radii first, then air gaps, then glass thicknesses) and ch. 8 (aspheres).

Operand names below were checked against the ZOS-API `MeritOperandType` of OpticStudio 2024 R1.

## Variables

The structural parameters are the radii (or aspheric terms), the thicknesses and air gaps, and the refractive indices [LXT §17.1]. In saos:

| What | Command | Notes |
|---|---|---|
| radius, thickness, conic | `saos opt vary --param radius\|thickness\|conic --surfaces …` | the object surface allows thickness only |
| surface-type parameters | `saos opt vary --param par<N>`; `saos lens set --param par<N>`; scan plans and probes take the key `"<surface>.par<N>"` | the Lens Data Par column: aspheric terms, coordinate-break decenters and tilts, Fresnel or freeform terms. `lens describe` lists each surface's parameters with their column numbers |
| glass as a model | `saos opt vary --param material --mode model` | nd and Vd vary, starting from the current glass. Exploration only: the round is not deliverable until real glasses replace it |
| glass substitution | `saos opt vary --param material --mode substitute [--catalog C]` | for Hammer or global search; OpticStudio swaps real glasses of the catalogs in use |

- Do not free everything at once. Damped least squares cannot separate strongly correlated variables, so choose the variables that act on the current gap [LXT §17.3.5]. A common order is radii, then air gaps, then glass thicknesses [ZXT 设计实例四]; aspheric terms from the lowest order up, each one earning its place. Aspheric coefficients correlate with one another, which also makes them hard to tolerance [ZXT §8.2.3].
- A model glass should stay where real glasses exist: bound nd within 1.48–1.85 and keep Vd consistent with nd, so that a catalog glass can replace it later [LXT §17.4]. Use `RGLA` (distance to real glasses), or `MNIN` / `MXIN` and `MNAB` / `MXAB`.

## Boundary conditions

There are two kinds [LXT §17.4].

**On the variables themselves.**

- Radii: usually no limit is needed.
- Lens centre thickness: a strict lower limit, roughly d ≥ 0.1 D (D = lens diameter); an upper limit against over-thick lenses if needed.
- Air gaps: a lower limit only.
- Model-glass indices: see above.

Operands: `MNCG` / `MXCG` / `MNEG` / `MXEG` (glass centre / edge), `MNCA` / `MXCA` / `MNEA` / `MXEA` (air centre / edge), `MNCT` / `MXCT` / `MNET` / `MXET` (any), `CTGT` / `CTLT` / `ETGT` / `ETLT` (one surface).

**On functions of the variables.** Positive-lens edge thickness, focal length or magnification, back focal distance, total length, even cost.

- Quantities that must be held exactly (focal length, magnification) stay in the merit function all the time.
- One-sided limits enter as a penalty that is zero while the limit holds.

Operands: `EFFL`, `PMAG`, `TOTR`, `TTHI` (back focal distance = the last gap, or a paraxial solve), `OPLT` / `OPGT`, `MXSD` / `MNSD` (semi-diameter), `GCOS` (glass cost).

In saos the product's hard limits belong in the demand (`constants`, or a `range` on a `variable`, with a check), because only the demand is judged. The merit function carries smooth one-sided versions of the same limits.

## Manufacturing and assembly [LXT §17.5]

**Diameter allowance.** The outer diameter is the clear aperture D plus a mounting allowance. Leave it during correction, not afterwards: added later, it can leave a positive lens without edge thickness or turn a surface past a hemisphere.

| Clear aperture (mm) | rolled edge | retaining ring | Clear aperture (mm) | rolled edge | retaining ring |
|---|---|---|---|---|---|
| ≤ 6 | D + 0.6 | — | > 30–50 | D + 2.0 | D + 2.5 |
| > 6–10 | D + 0.8 | D + 1.0 | > 50–80 | D + 2.5 | D + 3.0 |
| > 10–18 | D + 1.0 | D + 1.5 | > 80–120 | — | D + 3.5 |
| > 18–30 | D + 1.5 | D + 2.0 | > 120 | — | D + 4.5 |

(表 17-1)

**Minimum thickness.** Measure the edge thickness on the outer diameter, i.e. after the allowance; `MNEG` on the surface's semi-diameter does this when the semi-diameter includes the allowance.

| Lens diameter (mm) | min edge, positive lens (mm) | min centre, negative lens (mm) |
|---|---|---|
| 3–6 | 0.4 | 0.6 |
| > 6–10 | 0.6 | 0.8 |
| > 10–18 | 0.8–1.2 | 1.0–1.5 |
| > 18–30 | 1.2–1.8 | 1.5–2.2 |
| > 30–50 | 1.8–2.4 | 2.2–3.5 |
| > 50–80 | 2.4–3.0 | 3.5–5.0 |
| > 80–120 | 3.0–4.0 | 5.0–8.0 |
| > 120–150 | 4.0–6.0 | 8.0–12.0 |

(表 17-2)

**Mechanical centring.** Mass production centres a lens between two coaxial chucks. That needs |D1/r1 ± D2/r2| ≥ 0.4, where D1 and D2 are the diameters and r1 and r2 the radii of the two surfaces; "+" for biconvex or biconcave lenses, "−" for menisci (式 17-16). Check it for any meniscus whose two radii are close.

**Test plates.** Once the design is corrected, nudge the radii onto the maker's test-plate list (testplate fitting), keeping the image quality. Order:

- radii shared by several surfaces (cemented surfaces) first;
- sensitive surfaces before insensitive ones;
- radii in sparse parts of the list before dense ones;
- large diameters before small.

Small, steep, very sensitive surfaces may stay off the list. A plate much smaller than the part is not usable.

**Other points.**

- Use few glass types.
- Prefer common, inexpensive glasses that work and stay stable; take care with high-index glasses of poor workability or chemical stability.
- Check the spectral transmission against the band.
- Biconvex or biconcave lenses with nearly equal radii are easy to mount reversed: make the radii equal or mark the lens.
- Injection-moulded plastic parts need even, not too large thickness, because of shrinkage.

In a demand these are usually `variables` ranges or `constants` with `source: 李晓彤 表17-2` (or the maker's data when the user has it). Ask the user for the mounting method and the maker when the answer changes a limit.
