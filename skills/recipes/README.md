# Recipe authoring standard

A recipe is an optional sub-skill for one family of optical systems. The `saos` entry skill loads it after the demand is confirmed. A recipe never overrides the invariants in `skills/saos/SKILL.md`.

## Where recipes come from

Write a recipe only from a design job that was actually completed with saos. Do not design recipes up front. When a job finishes, distil what worked into a recipe and cite the job.

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
7. **Manufacturing and assembly** — limits this family usually needs (minimum thicknesses, diameter allowance, centring, test plates, mounting), with sources; general values are in `skills/saos/design-practice.md`.
8. **Verified** — what has been measured on real OpticStudio (jobs, test files, numbers) and what is still unverified.
