# Build a merit function

The outcome is a merit function whose minimum is what the demand judges. You first write one in a `prepare` script for screening, so read this before that, and again whenever the checks and the merit disagree.

- Optimise on smooth proxies: RMS spot or wavefront operands, Seidel terms, real-ray constraints. Judge with the demand checks. Step-like checks (ray counts, a field scanned in steps) give an optimiser almost no gradient.
- Sample the pupil out to its rim (`r = 1`), especially when higher-order aspheric terms vary. Gaussian quadrature rings stop inside the rim, so an unguarded high-order asphere can lower the merit inside the outermost ring while throwing rim rays wild and collapsing full-aperture MTF or spot checks; add rim-ring ray operands (`TRAC`/`OPDC` or `REAX`/`REAY` at `px² + py² = 1`) whenever the wizard alone leaves the edge unsampled.
- Weigh the merit function the way the demand weighs the objectives; the compromise should favour the user's objectives.
- An optimiser exploits whatever the target leaves open. Physically impossible states (intersecting parts, a virtual focus, rays missing the image, light leaving the part) must make the target invalid, not cost a linear penalty. Make sure the merit cannot be satisfied by a loophole such as discarding light.
- A merit value the backend cannot compute (for example 9e+09 when rays fail by total internal reflection) is a diagnosis signal: find the cause before going on.
- When a search must use the demand's own measurement (`ctx.measure(ids)`, a scan's `measure`, a `minimize` plan), give it only the items its variables act on and that take well under a second. Leave slow items (a Huygens PSF series takes seconds) to `saos check --only` and the round. A Nelder–Mead on the full shortfall of the fibre probe (18 variables, one-hour budget) stopped unconverged after 48 evaluations.

Tools: `opt vary --param radius|curvature|thickness|conic|par<N>|material`, `opt merit wizard|add|show`. Bounds belong in boundary operands (CTGT/CTLT, MNEG/MNEA/MNET for edges); there are no per-variable --min/--max flags. From a script, add rows in one checked batch with `ctx.merit_add`, design-rule constraints from `saos.ctx.rows`, and solves with `ctx.solve` ([scripting.md](scripting.md#editing-the-lens-from-a-script)).

## On rtc

- **Merit rows.** Same row format and merit value as OpticStudio, saved in the `.zmx`. rtc computes the boundary, lens-data, arithmetic, aberration (TRAR/TRAX/TRAY/TRAC/TRCX/TRCY, OPDC/OPDX/OPDM with Int1 0 and one wavelength) and ray or system operands listed in `docs/plans/rtc-design-stage.md`; a batch with any other operand (ETGT, STHI, STRH, DISG, RAED, …) is refused whole and the merit function stays as it was. A merit function built on OpticStudio that uses refused operands is rebuilt with `opt merit wizard` and `ctx.merit_add`.
- **Wizard.** RMS only, Gaussian quadrature, `start_at` keeps the rows before it.
