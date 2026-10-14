# Harness format (`sources/harness.yaml`)

The harness is the confirmed need of one job. `saos check` and `saos round` read it on every call; `saos report` renders it for the user. Field names are English; `request` and `confirmed.quote` keep the user's words in the user's language.

```yaml
schema: 1
request: "Keep the focal length at 50 mm (±1 %); make the on-axis spot as small as possible, no worse than 1.5x the original."
targets:
  - id: efl
    kind: constraint
    description: effective focal length
    check: {operand: {type: EFFL, int1: 0, int2: 0, data: []}}
    goal: {equals: 50.0, tol: 0.5}
  - id: spot_axis
    kind: target
    description: on-axis RMS spot radius (um), primary wavelength
    check: {analysis: {type: StandardSpot, settings: {}, path: "spot.rms_radius[0][1]"}}
    goal: {max: 7.48}
  - id: track
    kind: preference
    description: total track
    check: {operand: {type: TOTR}}
  - id: custom
    kind: target
    check: {script: checks/custom.py}
    goal: {min: 0.3}
    scale: 0.3
assumptions:
  - the sample's field and wavelengths are kept
recipes: []
lang: en
stop: {rel: 0.01, max_rounds: 30, tiers: [local, variables, materials, global, structure]}
confirmed: {quote: "Yes, go ahead."}
```

## Fields

| Field | Required | Meaning |
|---|---|---|
| `schema` | yes | Always `1`. |
| `request` | yes | The user's request, verbatim. Never paraphrase. |
| `targets` | yes | Non-empty list of goals, see below. |
| `assumptions` | no | Every condition you filled in that the user did not state. |
| `recipes` | no | Recipe names you plan to load (`skills/recipes/<name>`). |
| `lang` | no | `zh` or `en`: the language the user writes in. Sets the language of the delivery report. Omitted: `SAOS_LANG`, then the system UI language. Changing it needs no new confirmation. |
| `stop` | no | Stop rule parameters. Defaults: `rel: 0.01`, `max_rounds: 30`, all five tiers. Changing them changes the need. |
| `confirmed` | before round 0 | `{quote: <the user's explicit approval, verbatim>}`. Never write it yourself. |

## Targets

- `id`: letters, digits and `_`, starting with a letter, unique.
- `kind`:
  - `constraint`: must be met.
  - `target`: a performance goal with a pass line.
  - `preference`: no pass line; reported only, never decides whether the need is met. `goal` may be omitted.
- `description`: a short plain-language label shown in the report.
- `check`: exactly one of:
  - `operand`: `{type, int1, int2, data}`. Any Zemax optimisation operand, evaluated on the current lens.
  - `analysis`: `{type, settings, path}`. Runs an OpticStudio analysis (same export as `saos lens analysis`) and picks one number by `path`, for example `series[0].y.Tangential[20]` or `grids[0].stats.max`. Keys that contain dots are written as `['0.48']`. Run `saos lens analysis TYPE` first to see the structure.
  - `script`: a path inside the job. The script defines `run(ctx)` and returns one number; `ctx` is the same as for `saos exec`.
- `goal`: `{max: v}`, `{min: v}`, both, or `{equals: v, tol: t}` with `t > 0`.
- `scale`: normalises the gap of this goal. Default: `tol` for `equals`, otherwise `|bound|`. Required when the bound is 0.

## Shortfall

For each constraint and target, the gap is `max(0, excess over the pass line) / scale`; a check that cannot be evaluated counts as infinite. The shortfall G of a round is the sum of the gaps. The need is achieved when G = 0. G is the only number compared across rounds; merit function values are not comparable.

## Stop rule

After every `saos round` the ledger decides, in this order:

1. `achieved`: G = 0 in this round.
2. `limit`: the round number reached `max_rounds`.
3. `exhausted`: since the last round that cut the best G by at least `rel`, every tier listed in `stop.tiers` was tried at least once without such a gain.
4. `continue`: otherwise; `next_tier` is the lowest allowed tier not yet tried since the last gain.

Tiers, low to high: `local` (local optimisation on the current variables), `variables` (more variables or a different merit construction), `materials` (glass changes), `global` (global search, Hammer), `structure` (add, split or remove elements, aspheres). When the harness content changes (anything except `confirmed` and `lang`), a new confirmation is required and progress is measured again from that round.

## Workflow

1. `saos check --job J` evaluates every check on the loaded lens. Use it while framing; it needs no confirmation.
2. Show the user the harness in plain language and get an explicit approval; record it verbatim in `confirmed.quote`.
3. `saos round --job J [--tier T] --note "..."` records round 0 (no tier), then one round per adjustment. It refuses to record without confirmation, after a content change without a new quote, and after the job has stopped.
4. `saos report --job J [--lang zh|en]` writes `report/index.html`.
