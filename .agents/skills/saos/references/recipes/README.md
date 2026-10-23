# Writing a recipe

A recipe is a reference for one family of optical systems. The skill entry routes to it in stage 2, and its sections feed the stages of the workflow. A recipe adds experience; it never changes the gates or rules of the workflow.

## Where recipes come from

Write a recipe from design jobs run with saos, not up front. When a job finishes, distil what worked and cite the job.

Lift a **draft** earlier when code repeats: as soon as a second job copies a helper (check library, model builder, scan driver), move it into `src/saos/recipes/<system>/` and write the recipe marked "Draft", citing the jobs. Lifting must not change a number: run the old and new code on the same lenses and record the comparison under `docs/recipes/<system>/`. Recipe code imported by checks counts towards the check version, so the next round re-measures the best round with it.

## Layout

- `.agents/skills/saos/references/recipes/<system>.md`: the recipe, in English. Add it to the recipe list in the skill entry.
- `src/saos/recipes/<system>/`: optional Python helpers, called from `saos exec` scripts and checks; recipe code adds no daemon operations. Tests go to `tests/recipes/<system>/`.
- `docs/recipes/<system>/`: optional longer notes for people (Chinese), such as specifications with sources and lifting comparisons.

## Sections

Write a section only when a job taught you something verified for it.

1. **Scope** (required): which systems it covers and which not (conjugates, apertures, spectral range, size).
2. **Backend status** (required): which checks and helpers run in Optiland, which only in OpticStudio, and what to do in the Optiland stage meanwhile.
3. **Stage 1: questions and checks**: what to ask in the interview with recommended defaults and the source of each; the usual goals and how to measure them (operand or analysis and its settings, units, cost).
4. **Stage 2: starting forms and modelling**: where to get a start and how to adapt it to the user's scale; layout conventions, solves, apertures, pitfalls.
5. **Stage 3: optimisation**: variables and merit constructions that worked, with the backend they ran on.
6. **Manufacturing and assembly**: limits this family usually needs, with sources.
7. **Evidence** (required): what was measured, on which backend, in which jobs, and what is still unverified.

Keep the history of how the recipe came about out of it: one line naming the source jobs is enough, and Git keeps the rest.
