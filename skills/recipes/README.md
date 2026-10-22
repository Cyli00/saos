# Recipe authoring standard

A recipe is an optional sub-skill for one family of optical systems. The `saos` entry skill loads it after the demand is confirmed. A recipe never overrides the invariants in `skills/saos/SKILL.md`.

## Where recipes come from

Write a recipe from design jobs run with saos; do not design recipes up front. When a job finishes, distil what worked into a recipe and cite the job.

Lift a **draft** recipe earlier when code repeats: as soon as a second job copies a helper (check library, model builder, scan driver) from an earlier one, move it into `src/saos/recipes/<system>/` and write a `SKILL.md` marked "Draft" that cites the jobs. Lifting must not change a number: run the old and the new code on the same lenses and record the comparison under `docs/recipes/<system>/` (example: `docs/recipes/fiber/equivalence-2026-10-17.md`). Recipe code that checks import counts towards the check version (`saos.demand.code_files`); after a change, the next `saos round` re-measures the best round with it, and the comparison window goes on only if the numbers are equal.

## Layout

- `skills/recipes/<system>/SKILL.md` — the recipe, in English, with front matter `name: saos-recipe-<system>` and a one-line `description` that says which systems it covers.
- `src/saos/recipes/<system>/` — optional Python helpers. Agents call them from `saos exec` scripts (`def run(ctx): …`); recipe code must not add daemon operations.
- Optional human notes in `docs/` (Chinese).

## Sections of SKILL.md

Scope and Verified are required. Write the others when the job taught you something for them, and leave out a section you have nothing verified for rather than filling it with guesses. Add a section the family needs that this list lacks.

1. **Scope** — which systems this covers and which it does not (conjugates, apertures, spectral range, size).
2. **Framing questions** — what to ask during the demand interview for this family, with recommended defaults and the source of each default.
3. **Checks** — the usual goals and how to measure them in OpticStudio: operand (type and parameters) or analysis (type, settings, `path` into the export), with units.
4. **Starting designs** — where to get one (library, patents, Zemax samples, first-order synthesis) and how to adapt it to the user's scale.
5. **Modelling notes** — layout conventions, solves, apertures, pitfalls seen in practice.
6. **Optimisation** — which variables and merit constructions worked, per strategy tier.
7. **Manufacturing and assembly** — limits this family usually needs (minimum thicknesses, diameter allowance, centring, test plates, mounting), with sources.
8. **Verified** — what has been measured on real OpticStudio (jobs, test files, numbers) and what is still unverified.
