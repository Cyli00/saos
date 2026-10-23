---
name: saos
description: Run the saos optical design workflow, taking a user's optical need through an agreed demand, an approved model, Optiland design rounds and OpticStudio verification to a delivered lens with a measured HTML report. Use for designing, simulating or debugging an optical system in this repository. Not for changing saos itself (src/, tests/, CLI, docs); use normal repository work for that.
---

# saos optical design workflow

Deliver a lens the user can build and an HTML report whose numbers were measured by the saos backends, with every unmet goal stated with its gap. A conclusion that the need cannot be met, backed by evidence and the user's decision to stop, is also a valid outcome.

You are the designer: models, starting forms, variables, merit functions, algorithms and experiments are your judgement. The user owns four decisions: the need, the model, acceptance of the Optiland result and stopping. The tools record each as a gate with the user's own words; this skill gives no permission to infer or bypass one.

Run commands from the repository root as `uv run saos ...`. Every command prints one JSON object; `uv run saos <command> --help` documents arguments, results and error codes. `DESIGN.md` defines the terms.

## Rules in every stage

- **Measured evidence.** Every optical number you deliver or decide on comes from Optiland or OpticStudio through saos (operand, ray trace, analysis, check). Hand estimates guide reasoning and are labelled as estimates; never write your own ray tracer, paraxial or aberration calculator or surrogate (ADR-0012). Label each result with its backend; final delivery uses OpticStudio numbers.
- **One definition per number.** Optimisation targets, script objectives and first-order estimates use the demand checks' definition, or are compared with them on the same lens before you draw a conclusion.
- **No silent relaxing.** A goal that cannot be met is reported with its gap. Changing a constant, range, pass line or check condition goes back to the user for a new confirmation.
- **Sourced materials in delivery.** Model glasses are for exploration only.
- **Reports for the user are HTML.** Internal notes, the demand YAML and raw JSON keep their own formats.
- **Never resubmit running work.** `BUSY` or a client timeout means the daemon is still computing; follow it with `job show` or cancel it.

## Find where the job stands

For a new need, create the job (`uv run saos job create --name <name>`) and start at stage 1. Otherwise run `uv run saos job show <job>` and continue from `workflow.current`:

| `workflow.current` | Continue at |
|---|---|
| `interview`, `demand_confirm` | 1. Agree the need |
| `modeling`, `model_approve` | 2. Choose and approve the model |
| `design` | 3. Design in rounds |
| `deliver`, or `zos` whose step detail has `state: zos_accept` | 4. Get the Optiland result accepted |
| `zos` with `state: zos_pending`, `final_report` | 5. Finish in OpticStudio |

A step with status `blocked` has a blocking open question (`workflow.blocking`): get it answered before optimising or recording rounds. `progress` and `runs` show work still running; never start it again. Reread the demand with `uv run saos demand show --job J` before continuing.

Once per session, before other work, run `uv run saos daemon status` and ask in your first reply for anything missing: the daemon and which job it holds, another session using it, free OpticStudio licences, a visible window for extension mode. Do not work around a missing precondition silently.

## 1. Agree the need

Establish what the user needs, as conditions and measurable acceptance criteria with a source for every limit, and whether the structure can reach each `must`.

- Interview in prepared rounds, each question with your recommended answer, and keep the user's words verbatim in `sources/interview.md`.
- Before showing the demand, reason from physics whether the structure can reach every `must` (invariants, diffraction, apertures, degrees of freedom, how each part is made) and check the deciding number on a minimal backend model. Give each `must` a verdict: reachable, marginal or not reachable.
- Write `sources/demand.yaml` ([format](demand.example.yaml)) and its check scripts in `checks/`.

**Gate:** walk the user through the demand, its feasibility verdicts and every `weak_basis` limit from `demand show`, then record their words with `uv run saos demand confirm --job J --quote "..."`. Set up the workflow window for this kind of system (`ui open --tabs --metrics`).

Read [need.md](references/need.md) for the interview format, feasibility reasoning, demand rules and how to write checks.

## 2. Choose and approve the model

Establish a starting form chosen for reasons the user can follow, built in Optiland with sourced materials and described surface by surface.

- If a recipe matches the system, read it: [microscope objectives](references/recipes/microscope.md), [side-viewing fibre probes](references/recipes/fiber.md). Recipes are experience, not rules.
- For a free structure, generate starts from the library and from patents, predict them first-order and third-order, then screen every candidate under the same budget before you choose. For a prescribed structure, state the user's reason and your feasibility verdict.
- Record material sources in `sources/materials.yaml`, write model notes, and run `uv run saos lens describe --job J --notes <notes.json>`.

**Gate:** show the description page (choice, requirements, validity, budget) and record the user's approval with `uv run saos lens approve --job J --model models/NNN-<lens> --quote "..."`. Rounds refuse a lens whose structure matches no approved description; a structural change later needs a new description and approval.

Read [model.md](references/model.md) for starts, screening, materials, notes and what counts as a structural change.

## 3. Design in rounds

Establish, round by round, how close the approved structure gets to the demand, until the round decision is `achieved`, `exhausted`, `limit` or the user stops.

- Optimise on smooth proxies and judge with the demand checks; `saos check --only` for trials, `saos round --tier ... --note ...` when a design is worth judging.
- Compare rounds by shortfall, never by merit value. Rule out tiers your evidence says cannot help (`saos rule-out`) instead of spending rounds on them.
- Before a run longer than about ten minutes, or any `global` or `structure` attempt, tell the user each failing `must`'s gap, the mechanism expected to close it and the expected gain. When a `must` misses by a multiple or a cap is already visible, diagnose instead of optimising.
- Run anything that may outlast your command time limit with `--detach`.

**Exit:** a final round decision. Leaving the approved structure sends you back to stage 2.

Read [rounds.md](references/rounds.md) for optimisers, merit construction and recording rounds, [diagnosis.md](references/diagnosis.md) when progress stalls or a round brief sets `review`, and [computing.md](references/computing.md) for long runs, workers and the script API.

## 4. Get the Optiland result accepted

Establish whether the user accepts the Optiland result as the design to verify in OpticStudio.

- Run a tolerance scan first when the demand or the user cares about manufacturing and assembly; otherwise offer one.
- `uv run saos report --job J` writes the report. Deliver only the lens named in `deliver`; a `diagnostic` round is not a delivery. Tell the user these are Optiland numbers, not yet verified in OpticStudio.

**Gate:** the user's explicit acceptance of this report, recorded by `uv run saos zos begin --job J --quote "..."`. Acceptance is never inferred from the demand or model approval. If they reject it, return to stage 3 within the current demand, or record their decision to stop.

Read [delivery.md](references/delivery.md) for the report, tolerance scans and stopping.

## 5. Finish in OpticStudio

Establish the final OpticStudio measurements of the accepted design, improved there if needed, and deliver them.

- `zos begin` rebuilds the accepted round, records a verify round and lists the settings it could not translate (`untranslated`); recreate those before optimising.
- Continue with OpticStudio optimisers and rounds; the round budget and comparison window restart at the verify baseline, and the design tiers and stop rules stay the same.
- Regenerate the report once the round decision is final.

Read [opticstudio.md](references/opticstudio.md) for the stage entry, OpticStudio optimisers, glass catalogs, licences and on-screen debugging.

## Stop and ask

Ask the user, with the evidence, its impact and your recommended options, as soon as one of these holds; you do not have to exhaust every tier first:

- the demand contradicts itself, or a constant, range or pass line rests on a weak basis;
- a measurement can be read in more than one way and the choice changes the result;
- feasibility or first-order analysis says the structure cannot reach the objectives, or a `must` misses by a multiple of its pass line;
- the results give no reason to keep the current strategy (the round `brief` sets `review` when rounds stall or before escalating to `global` or `structure`);
- the structure must change in a way the user has not seen.

Run `uv run saos ui notify --job J --message "..."` before you stop to ask. A question whose answer could change the demand goes into `open_questions` with `blocking: true`; rounds and optimisers refuse to run until it is answered, while diagnostic scripts still may. Otherwise keep working without asking.

## Finish

Before handing over, check that:

- the report is current and names a deliverable lens, or states that none exists and why;
- every `must` is met or reported with its gap, and every number names its backend;
- the delivered lens uses sourced materials and passes the interference check;
- a tolerance scan ran on the delivered lens, or you offered one;
- `uv run saos job verify <job>` reports the evidence intact;
- open questions and assumptions the user has not confirmed are listed.

Give the user the report path, the delivered lens file and what is still open.

## Read only the relevant guide

- Interview, feasibility, demand and checks: [need.md](references/need.md)
- Starting forms, screening, materials and model approval: [model.md](references/model.md)
- Optimisers, merit functions and rounds: [rounds.md](references/rounds.md)
- Stalled progress and probe scans: [diagnosis.md](references/diagnosis.md)
- Long runs, cancellation, workers and the script API: [computing.md](references/computing.md)
- Report, tolerance scans and stopping: [delivery.md](references/delivery.md)
- OpticStudio stage and on-screen debugging: [opticstudio.md](references/opticstudio.md)
- Job directory contents: [files.md](references/files.md)
- System families: [microscope objectives](references/recipes/microscope.md), [side-viewing fibre probes](references/recipes/fiber.md); writing a new recipe: [recipes/README.md](references/recipes/README.md)
