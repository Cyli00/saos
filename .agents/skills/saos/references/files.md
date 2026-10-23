# Job directory

Everything about a job lives in `work/<instance>/jobs/<job>/`; the instance directory holds only `daemon/` and `jobs/`. Nothing about a specific job goes into the repository, and temporary files go to the system temporary directory. NNN is a three-digit sequence number.

## What you write

| Location | Content | Changes |
|---|---|---|
| `sources/interview.md` | each interview round: questions, recommendations, the user's words | append only |
| `sources/demand.yaml` | the demand ([format](../demand.example.yaml)) | values, ranges, checks, weights, units, origins, sources or stop rule need a new confirmation |
| `sources/materials.yaml` | the source of every material (format in `saos.materials_record`) | append or correct sources |
| `sources/` (other) | datasheets, papers, downloads | free |
| `checks/` | check scripts and every helper or data file they use; checks depend on nothing else | any change starts a new comparison window |
| `notes/model-NNN.json` | model notes for `lens describe` | sealed once used |
| `notes/*.html` | reports for the user (design, exploration, verification) | free |
| `notes/*.md` | internal working notes, never delivered | free |
| `explore/<topic>/` with `README.md` | code, intermediate lenses and logs run outside `saos exec`; the README states purpose, outcome and whether it is superseded | free |
| working lenses | through `lens build`, `lens load`, `lens save`, `lens export` | free |

## What the tools write

Use commands to create or update these; find paths in command results and `job show`.

| Location | Content |
|---|---|
| `job.json`, `log.jsonl`, `progress.json`, `view.json` | job state, every command and result, running progress, the current lens for the workflow window |
| `confirmations.jsonl`, `models/NNN-<lens>/` (with `approval.json`), `stages/zos.json` | the user's approvals with their words: demand, model, Optiland report acceptance |
| `evaluations/NNN/` | every check or round: snapshot, `support/` copy of `checks/`, exports, authoritative `result.json`, `viz.json` |
| `ledger.jsonl`, `lenses/rounds/rNNN.*` | per round: shortfall, tiers, materials, lens hash, approved model, evaluation; the archived round lens (`lens save` refuses it) |
| `ruled_out.jsonl`, `stops.jsonl` | ruled-out tiers with evidence; the user's decisions to stop |
| `scripts/`, `analyses/`, `parallel/NNN/`, `scans/NNN/`, `runs/` | `exec` copies and results, analysis exports, `ctx.map` and scan plans and results, detached run requests and responses |
| `lenses/scans/NNN/`, `lenses/global/NNN/`, `lenses/hammer/NNN/`, `lenses/captures/`, `lenses/rounds/orphaned/` | lenses ready for `lens load`; window content saved before replacement; archives of failed attempts |
| `stages/zos-result.json` | verify round, item/baseline/material differences, untranslated settings |
| `report/` | every generated report and its metadata; `index.html` and `latest.json` are the latest |
| `screens/` | screenshots |
| `seals.jsonl` | hashes of sealed files |

Sealed files carry a recorded hash. `uv run saos job verify <job>` reports any later change or deletion, and `saos report` refuses to run while one exists. Recipe commands may add their own directories (the microscope recipe writes `spec/` and `eval/`).
