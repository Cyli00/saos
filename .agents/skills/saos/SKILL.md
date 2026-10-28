---
name: saos
description: Run the saos optical design workflow, taking a user's optical need through an agreed demand, an approved model, OpticStudio design rounds to a delivered lens with a measured HTML report. Use for designing, simulating or debugging an optical system in this repository. Not for changing saos itself (src/, tests/, CLI, docs); use normal repository work for that.
---

# saos optical design workflow

Deliver a lens the user can build and an HTML report whose numbers were measured by the OpticStudio backend, with every unmet goal stated with its gap. A conclusion that the need cannot be met, backed by evidence and the user's decision to stop, is also a valid outcome.

You are the designer: models, starting forms, variables, merit functions, algorithms and experiments are your judgement. The user owns three decisions: the need, the model and stopping. The tools record each as a gate with the user's own words; this skill gives no permission to infer or bypass one.

Run commands from the repository root as `uv run saos ...`. Every command prints one JSON object; `uv run saos <command> --help` documents arguments, results and error codes. `DESIGN.md` defines the terms.

## Rules in every stage

- **Measured evidence.** Every optical number you deliver or decide on comes from OpticStudio through saos (operand, ray trace, analysis, check). Hand estimates guide reasoning and are labelled as estimates; never write your own ray tracer, paraxial or aberration calculator or surrogate (ADR-0013). The rtc copy (`ctx.fast()`) is the one other tracer: its numbers steer loops, never decisions. Label each result with its backend; final delivery uses OpticStudio numbers.
- **One definition per number.** Optimisation targets, script objectives and first-order estimates use the demand checks' definition, or are compared with them on the same lens before you draw a conclusion.
- **No silent relaxing.** A goal that cannot be met is reported with its gap. Changing what the demand judges (a constant, range, pass line, check; the full list is in the [format](demand.example.yaml) header) goes back to the user for a new confirmation.
- **Sourced materials in delivery.** Model glasses are for exploration only.
- **Autonomy is not a waiver.** When the user tells you to proceed without confirming, their words may stand as the quote at the gates they cover (never beyond the scope they named), but the work each gate shows still gets done, written down and announced with `ui notify`: feasibility verdicts, weak-basis limits, the model page, and any trade of an objective they named. Read their requirements as written; with nobody checking your interpretations, an added restriction or a quietly surrendered objective goes unnoticed until the end.
- **Reports for the user are HTML.** Internal notes, the demand YAML and raw JSON keep their own formats.
- **Detach long work and never resubmit it.** Run anything that may outlast your command time limit with `--detach`. `BUSY` or a client timeout means the daemon is still computing; follow it with `job show` or cancel it.
- **Nothing lands in the repository.** Commands run from the repository root, so never redirect output or write files with relative paths there. Input files you pass to commands (merit lists, operand lists, settings JSON) go in the job's `notes/`; command output you keep for later parsing goes to the system temporary directory (`$TEMP`). [files.md](references/files.md) lists what belongs where in a job.

## Find where the job stands

For a new need, create the job (`uv run saos job create --name <name>`) and start at stage 1. Otherwise run `uv run saos job show <job>` and continue from `workflow.current`:

| `workflow.current` | Continue at |
|---|---|
| `interview`, `demand_confirm` | 1. Agree the need |
| `modeling`, `model_approve` | 2. Choose and approve the model |
| `design` | 3. Design in rounds |
| `deliver` | 4. Deliver the measured design |

A step with status `blocked` has a blocking open question (`workflow.blocking`): get it answered before optimising or recording rounds. `progress` and `runs` show work still running; never start it again. Reread the demand with `uv run saos demand show --job J` before continuing.

Once per session, before other work, run `uv run saos daemon status` and ask in your first reply for anything missing: the daemon and which job it holds, another session using it, free OpticStudio licences, a visible window for extension mode. Do not work around a missing precondition silently. Each daemon holds a licence and none exits by itself: `uv run saos daemon list --all` shows them all, and `daemon stop --all` stops other sessions' work, so it needs the user's agreement. [opticstudio.md](references/opticstudio.md) covers sessions, licences and on-screen debugging.

## 1. Agree the need

Establish what the user needs, as conditions and measurable acceptance criteria with a source for every limit, and whether the structure can reach each `must`.

- Interview in prepared rounds, each question with your recommended answer, and keep the user's words verbatim in `sources/interview.md`.
- Before showing the demand, reason from physics whether the structure can reach every `must` (invariants, diffraction, apertures, degrees of freedom, how each part is made) and check the deciding number on a minimal backend model. Give each `must` a verdict: reachable, marginal or not reachable. For each `minimize` / `maximize` objective, estimate how far the `must`s let it go. Write both to `explore/feasibility/README.md`; later stages compare against them.
- Write `sources/demand.yaml` ([format](demand.example.yaml)) and its check scripts in `checks/`.

**Gate:** walk the user through the demand, its feasibility verdicts and every `weak_basis` limit from `demand show`, then record their words with `uv run saos demand confirm --job J --quote "..."`. Set up the workflow window for this kind of system (`ui open --tabs --metrics`).

Read [need.md](references/need.md) for the interview format, feasibility reasoning, demand rules and how to write checks.

## 2. Choose and approve the model

Establish a starting form chosen for reasons the user can follow, built in OpticStudio with sourced materials and described surface by surface.

- If a recipe matches the system, read it: [microscope objectives](references/recipes/microscope.md), [side-viewing fibre probes](references/recipes/fiber.md). Recipes are experience, not rules; [recipes/README.md](references/recipes/README.md) says how to write one.
- For a free structure, generate starts from the library and from patents, predict them first-order and third-order, then screen every candidate under the same budget before you choose. Search the 2456 patent examples by field, f-number, band, element count and power signs (`saos.starts.patents.search`) rather than recalling forms from memory; they enter the same screening through `starts.generate(..., patents=[...])`. For a prescribed structure, state the user's reason and your feasibility verdict.
- Record material sources in `sources/materials.yaml`, write model notes, and run `uv run saos lens describe --job J --notes <notes.json>`.

**Gate:** show the description page (choice, requirements, validity, budget) and record the user's approval with `uv run saos lens approve --job J --model models/NNN-<lens> --quote "..."`. Rounds refuse a lens whose structure matches no approved description; a structural change later needs a new description and approval.

Read [model.md](references/model.md) for starts, screening, materials, notes and what counts as a structural change.

## 3. Design in rounds

Establish, round by round, how close the approved structure gets to the demand, until the round decision is `achieved`, `exhausted`, `limit` or the user stops.

- Run your own loops on ray quantities (scans of a few variables, multi-starts, sensitivities, refocusing) on the rtc copy when it builds, and push the result to OpticStudio before you judge it; loops that need operands, analyses or checks use the worker pool ([computing.md](references/computing.md#choose-where-a-loop-runs)).
- Edit the lens from scripts through `ctx`: `ctx.set`, `ctx.merit_add` (one checked batch, with design-rule constraints from `saos.ctx.rows`), `ctx.solve`, `ctx.insert_surface`. Write ZOS-API calls only for what these lack, and act on the `hints` that `exec` returns ([computing.md](references/computing.md#editing-the-lens-from-a-script)).
- Optimise on smooth proxies and judge with the demand checks; `saos check --only` for trials, `saos round --tier ... --note ...` when a design is worth judging. Your own script loops are optimisation runs too, and rounds keep their plots: the ledger and its plots are all the user sees of this stage.
- Compare rounds by shortfall, never by merit value. Rule out tiers your evidence says cannot help (`saos rule-out`) instead of spending rounds on them.
- Before a run longer than about ten minutes, or any `global` or `structure` attempt, tell the user the gaps and what you expect to close them ([rounds.md](references/rounds.md#gate-expensive-runs-on-the-gap)). When a `must` misses by a multiple or a cap is already visible, diagnose instead of optimising.

**Exit:** a final round decision. Leaving the approved structure sends you back to stage 2.

Read [rounds.md](references/rounds.md) for optimisers, glass substitution, merit construction and recording rounds, [diagnosis.md](references/diagnosis.md) when progress stalls or a round brief sets `review`, and [computing.md](references/computing.md) for long runs, the rtc copy, workers and the script API.

## 4. Deliver the measured design

- Run `uv run saos report --job J` for the current OpticStudio rounds. Deliver only the lens named in `deliver`; a `diagnostic` round is not a delivery. A later round, stop, rule-out or tolerance scan makes the report stale, and the workflow shows delivery as unfinished until you run it again.
- State every unmet goal with its measured gap, material limitations and tolerance status.
- If the user wants changes, continue within the demand and stop rules, or return to demand/model confirmation when those change. Only the user decides to stop.

Read [delivery.md](references/delivery.md) for tolerance scans, the report and stopping.

## Stop and ask

Ask the user, with the evidence, its impact and your recommended options, as soon as one of these holds; you do not have to exhaust every tier first:

- the demand contradicts itself, or a constant, range or pass line rests on a weak basis;
- a measurement can be read in more than one way and the choice changes the result;
- feasibility or first-order analysis says the structure cannot reach the objectives, or a `must` misses by a multiple of its pass line;
- the results give no reason to keep the current strategy (the round `brief` sets `review` when rounds stall or before escalating to `global` or `structure`);
- the structure must change in a way the user has not seen.

A candidate or structure that meets the `must`s by giving up a clear share of a directional objective the user named, compared with the feasibility estimate or the approved model, is a trade for the user to see: notify them with the numbers before building on it, and ask unless they granted autonomy over that choice.

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
