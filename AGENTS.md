# SAOS Repository Harness

## 1. Architecture & Source of Truth

| Concern | Authoritative location | Contract |
| --- | --- | --- |
| Runtime agent instructions | `skills/saos/` | Defines the workflow read by the agent executing a job. |
| CLI specifications | Each command's `--help` | Defines parameters, formats, and results. Register help in `src/saos/cli.py`, or the recipe's CLI module; keep detailed formats next to their implementation. |
| Chinese command overview | `docs/cli.md` | Summarizes the CLI in Chinese; must agree with command help. |
| Workflow state | Job files, interpreted by `src/saos/ui/stage.py` | Derive steps and status from job files only. |
| Workflow presentation | `src/saos/ui/static/app.js` and `docs/workflow.drawio` | Present the same workflow as the runtime instructions and CLI. |
| Design records | `DESIGN.md` | Contains only design principles, user decisions with rationale, and directory rules. |

**DESIGN.md update contract**:

- Trigger: a design principle, user decision, or directory rule changes.
- Required update: 1–2 sentences stating the change and its reason.
- Excluded content: commands, flags, thresholds, measured timings, and incident stories. Keep operational details in code and the relevant workflow targets below.

## 2. Four-Way Workflow Sync

**Trigger**: any change to a workflow step, gate, pre/post-step agent action, stop rule, or step command/flag.

**Required action**: review all four targets. Update each affected target so behavior and wording remain consistent. A target that already matches requires no edit.

| Target | Review and update scope |
| --- | --- |
| Agent skill docs | `skills/saos/SKILL.md`; also `skills/saos/demand.md` when demand format or confirmation logic changes. |
| Workflow UI | `src/saos/ui/stage.py`: `STEPS`, `snapshot`; `src/saos/ui/static/app.js`: `NODES`, `TEXT` in both languages, `back(...)`. |
| Workflow diagram | `docs/workflow.drawio`. |
| CLI help and overview | The corresponding command's help and `docs/cli.md` for every added or modified command/flag. |

**UI invariants**:

- A step that leaves no job file belongs in the label or sub-text of the step containing it; do not create a separate node.
- Each return edge has its own lane.

**Completion condition**: all four targets agree. If a target cannot be brought into sync, identify the target and reason in the final delivery report. Reporting an outstanding mismatch does not make the workflow change complete.

## 3. Verification & Visual Checks

**Execution scope**: run checks relevant to the changed targets, subject to the user's requested verification scope. If execution is delegated or unavailable, report the checks as `NOT RUN` and provide handoff instructions.

| Trigger | Command | Required inspection |
| --- | --- | --- |
| Workflow command references change | `SAOS_INSTANCE=verify uv run pytest tests/test_docs_sync.py` | Check command consistency results. |
| Workflow diagram changes | `SAOS_INSTANCE=verify SAOS_RENDER_DRAWIO=1 uv run pytest tests/test_docs_sync.py -s` | Inspect the generated PNG for labels, layout, and return edges. Requires Chrome and network. This includes the command consistency test; do not run it separately without a reason. |
| Workflow UI changes | `SAOS_INSTANCE=verify uv run saos ui open --job <JOB_ID> --no-window` | Open the returned URL in a browser. Inspect affected states, steps, labels in both languages, and return-edge lanes. |

**Automated coverage**: `tests/test_docs_sync.py` checks that each `saos` command named in `SKILL.md`, `demand.md`, `DESIGN.md`, `docs/cli.md`, parser descriptions/epilogs, and the diagram exists, and that the diagram names the command of every step. It does not establish semantic or visual consistency; review those separately.

## 4. Runtime & Process Isolation

| Boundary | Constraint |
| --- | --- |
| Instance selection | Set `SAOS_INSTANCE=verify` for every `saos` command and verification invocation. The default verification directory is `work/verify/`; `SAOS_HOME` overrides it. Confirm the resolved directory before accessing job or daemon state. |
| Temporary test homes | Tests may set `SAOS_HOME` to a dedicated directory created for the current task under the system temporary directory, including directories provided by test fixtures. |
| Data access | Access job and daemon state only in `work/verify/` or the dedicated temporary test home. Never point `SAOS_HOME` at `work/main/` or another user instance, and do not read or write their state. |
| Process ownership | Stop only processes started for the current task, targeting them by PID. Leave the user's daemon and OpticStudio windows running. |
