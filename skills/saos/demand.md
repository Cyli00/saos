# Demand format (`sources/demand.yaml`)

The demand is the confirmed need of one job, condensed. It is what you reread when you resume work or lose track, so it holds only what the design must respect and reach. `saos check`, `saos round` and `saos opt local` read it on every call; `saos report` shows it to the user.

The user's words stay verbatim in `sources/interview.md`. The demand holds the current conclusions; rewrite it when they change, never append the conversation to it.

```yaml
schema: 2
constants:
  - id: wavelengths
    description: excitation / emission reference
    value: 488 / 532
    unit: nm
    source: user 2026-10-14
  - id: efl
    description: effective focal length kept from the sample
    check: {operand: {type: EFFL}}
    goal: {equals: 50.0, tol: 0.5}
    source: user
variables:
  - id: lens_index
    description: nd of every printed lens
    range: {max: 1.6}
    check: {script: checks/lens_index.py}
    scale: 0.05
    source: resin datasheet (sources/resin.pdf)
  - id: surfaces
    description: surface forms of lens 1 and lens 2
    options: [freeform, flat-based Fresnel]
objectives:
  - id: spot
    description: 532 nm RMS spot diameter (um), worst of the required fields
    check: {script: checks/spot.py}
    origin: user
    must: {max: 3.7}
    wish: {max: 1.5}
    weight: 2.0
    source: user 2026-10-14 (one camera pixel)
  - id: pass_rate
    description: worst-field share of rays reaching the sample (%)
    check: {script: checks/pass_rate.py}
    origin: agent
    must: {min: 90.0}
    scale: 10.0
    source: "agent assumption: below 90 % the fluorescence signal drops noticeably"
  - id: collection
    description: geometric collection fraction, centre field
    check: {script: checks/collect_center.py}
    origin: user
open_questions:
  - {question: "Which brain region? It sets the scattering length and the WD limit.", blocking: false}
summary: |
  Two to six sentences: why this design exists, what changed during the interview, what is agreed now, what is still open.
lang: zh
```

## What goes where (your judgement)

| Section | Put here | Not here |
|---|---|---|
| `constants` | Fixed facts and conditions the design must not change, and every requirement the user said cannot be relaxed (it must not be traded for other gains): wavelengths, given parts (fiber model and NA, prism, tube), the sample medium, the task scope. Each has a `source`: the user, a datasheet or paper, or `agent assumption: <why>`. | An unknown real-world fact (the actual tube glass, the brain region). Write it in `open_questions`; if you must use a provisional value, make it a constant whose source says `agent assumption`. |
| `variables` | Freedoms you may use and their limits: material candidates, surface families, a dimension's allowed range. A `range` needs a `check` that measures it. | A real-world unknown. The optimiser must never "choose" a fact (fiber type, tissue, vendor tube) to make the result look better. |
| `objectives` | Measured goals. `must` is the pass line and enters the shortfall G; `wish` is the value hoped for, reported only. An objective with neither is reported only. One quantity is one objective: write `must` and `wish` on the same entry, not two entries. `origin` says whose goal it is (see Weights). | A derived geometric condition that is really a limit on a freedom (put it in `variables`). |
| `open_questions` | What you still need from the user. `blocking: true` stops `round` and `opt local` until it is answered; use it when an answer could change a constant, a range or a pass line. | Research to-dos that do not change the demand. |
| `summary` | A short account of the conversation, 2–6 sentences. | Lists, numbers already in the sections, literature details. |

Do not restate the rules of `skills/saos/SKILL.md` (sourced materials, no model glass in a delivery, no discarding rays, model approval). They hold for every job. Do not copy literature details or process instructions (library work, file clean-up) into the demand; keep them in other files under `sources/` and cite the file in a `source`. Anything that affects design or acceptance must be in `constants`, `variables` or `objectives`, never only in the summary: only those sections are versioned and checked.

## Fields

| Field | Required | Meaning |
|---|---|---|
| `schema` | yes | Always `2`. |
| `constants` | no | `id`, `description`, `source`, and either `value` (+ `unit`) or `check` + `goal`. |
| `variables` | no | `id`, `description`, optional `range` + `check`, optional `options`, `unit`, `source` (where a limit comes from, e.g. `李晓彤 表17-2`). |
| `objectives` | yes, non-empty | `id`, `description`, `check`, `origin`, optional `must`, `wish`, `weight`, `unit`, `source`. |
| `open_questions` | no | `{question, blocking}`. |
| `summary` | yes | See above. |
| `notes` | no | A list of short strings: context that changes neither the design nor the judgement (where source files are, decisions already taken). Not versioned; anything that affects the design belongs in the three sections. |
| `lang` | no | `zh` or `en`: the user's language; sets the report language. |
| `stop` | no | Stop rule. Defaults: `rel: 0.01`, `max_rounds: 30`, all five tiers. |

Other fields are refused, so that nothing that affects the design hides outside the versioned sections. `id`: letters, digits and `_`, starting with a letter, unique across all sections.

- `check`: exactly one of
  - `operand`: `{type, int1, int2, data}`, any Zemax optimisation operand on the current lens.
  - `analysis`: `{type, settings, path}`, runs an OpticStudio analysis (same export as `saos lens analysis`) and picks one number by `path`, e.g. `series[0].y.Tangential[20]`; keys with dots are written `['0.48']`.
  - `script`: a path under `checks/`. The script defines `run(ctx)` and returns one number; `ctx` is the same as for `saos exec`.
- Every check has preconditions under which its number means something: parts do not intersect, the image or focus is real, enough rays arrive, the quantity covers the whole beam and not only the rays that survive. Write them in the check (a docstring is enough) and return `inf` when one fails. A compensator inside a check (refocusing, a solve) that finds no sensible solution makes the value `inf` too; never return what it left behind.
- Bounds (`goal`, `range`, `must`, `wish`): `{max: v}`, `{min: v}`, both with `min <= max`, or `{equals: v, tol: t}` with `t > 0`; finite numbers.
- `scale`: normalises the gap of the bound. Default: `tol` for `equals`, otherwise `|bound|`. Required when the bound is 0.

## Weights

One structure often cannot meet every objective, so the shortfall must say which ones matter more. Every objective has an `origin`:

- `user`: the user stated it, or explicitly accepted your proposed value. Default weight 1.
- `agent`: you added it, or filled in a value the user never weighed in on. Default weight 0.3.

`weight` overrides the default, only on an objective with `must`. Give a user objective more than 1 when the user called it the main goal. An agent objective never weighs more than the lightest user objective; the file is refused otherwise. Weights are part of the demand version, so the user sees and confirms them. Constants and variable ranges have no weight: they are hard.

## Confirmation and versions

The demand version is a hash of `constants`, `variables`, `objectives` and `stop`. `summary`, `open_questions` and `lang` are not part of it, so rewording them needs no new confirmation and keeps rounds comparable. Every limit (a constant's value or goal, a variable `range`, a `must`) needs a `source`; `demand confirm` refuses one without. `demand show` lists under `weak_basis` the limits without an outside basis (`agent assumption: …`): go through each with the user before they confirm; the list is kept with the confirmation. Show the user the demand in plain language, with your feasibility verdict for each `must` (SKILL.md, "Question the structure before you write it down"), then record their own words with `uv run saos demand confirm --job J --quote "<their words>"`; the confirmation is bound to the version. Never record an instruction to write the demand as an approval of its content. `uv run saos demand show --job J` validates the file and prints its version, confirmation and open questions.

## Shortfall and delivery

For every item with a bound (`goal`, `range`, `must`), the gap is `max(0, excess over the bound) / scale`; a check that cannot be evaluated counts as infinite. The shortfall G of a round is the sum of the gaps, each multiplied by its weight (1 for constants and variables); the demand is achieved when every bounded item passes and the lens uses no model glass. Constants and variable ranges are hard: a round that breaks one is never delivered, whatever G says, and the round lists them in `hard_failing`. G adds gaps linearly, so a cheap item can be traded for an expensive one; anything that must not be traded belongs in the hard sections. Keep two cases apart: a physically impossible state (intersecting parts, a virtual focus, rays missing the image) is invalid and its check returns `inf`; a realisable state that breaks a hard limit (a clearance of 5 µm where 10 µm is required) keeps a finite gap and is simply not deliverable. Rounds with G = ∞ are fine: G judges, the optimiser works on smooth proxies. G is the only number compared across rounds; merit function values are not comparable.

## Stop rule

After every `saos round` the ledger decides, in this order:

A round whose checks failed to run (exception, unreadable file) is invalid: it counts as no progress, no tier tried, and is never delivered. A check that runs but finds no valid value for this design should return `inf`; that is a design result and counts as unmet. `max_rounds` counts the rounds of the current demand version.

1. `achieved`: every bounded item passes in this round and the lens uses no model glass (with a model glass: `continue` with `next_tier: materials`).
2. `limit`: the round number reached `max_rounds`.
3. `stopped`: the user decided to stop under this demand version (`saos stop --reason --quote`, after the last round); stopping is a conclusion, recorded with the evidence and the user's words.
4. `exhausted`: since the last round that cut the best G by at least `rel`, every tier in `stop.tiers` was tried at least once, or ruled out with evidence (`saos rule-out --tier T --reason "…"`), without such a gain. This says the strategies stopped working, not that the goal is physically out of reach.
5. `continue`: otherwise; `next_tier` is the lowest allowed tier neither tried nor ruled out since the last gain.

A round may name several tiers (`--tier variables,materials`). A rule-out holds until the next clear gain; after a gain, judge again.

Tiers, low to high: `local`, `variables`, `materials` (substitute sourced real materials), `global` (global search or Hammer, or a multi-start scan in a ZOS-API script), `structure` (add, split or remove elements, aspheres).

Every round also returns a `brief`: the objectives (with origin and weight) against `must` / `wish`, `hard_failing`, the open questions, `doubts` raised by evidence (an item without a valid value for two rounds, a hard limit never met in this demand version, a failing limit that rests on an agent assumption; the reminder to question the demand comes with them, not every round), and `review` when two rounds passed without a clear gain, the next tier is `global` or `structure`, or the rounds stopped. When `review` is set, reread the summary and your first-order analysis before spending more compute, and ask the user if the demand, the structure or a measurement looks wrong.

## Workflow

Check scripts live under `checks/` with every helper module and data file they use. Every check archives the demand, a copy of `checks/` (`support/`), analysis exports and `result.json` (with the saos code hash and the OpticStudio version) in a new `evaluations/NNN/`; `evidence_file` points to the result. Editing any file under `checks/` starts a new comparison window without a new confirmation; changing what an item means changes the demand and needs one.

1. `saos check --job J` evaluates every check on the loaded lens; it needs no confirmation.
2. `saos demand confirm --job J --quote "..."` after the user approves the demand.
3. `saos lens describe` and `saos lens approve --model models/NNN-<lens> --quote "..."` for the model.
4. `saos round --job J [--tier T] --note "..."` records round 0 (no tier), then one round per adjustment. It refuses without a confirmation of the current version (`DEMAND_NOT_CONFIRMED`), with a blocking question (`DEMAND_BLOCKED`), after the job stopped, when the lens matches no approved model (`MODEL_NOT_APPROVED`), when a check changed the lens (`LENS_MODIFIED_BY_CHECK`), or for the same lens under the same versions (`DUPLICATE_ROUND`). `saos opt local` applies the same demand checks before it starts.
5. `saos report --job J [--lang zh|en]` writes `report/index.html`.
