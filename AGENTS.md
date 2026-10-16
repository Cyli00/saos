# Working on the saos repository

`DESIGN.md` is the design; `skills/saos/` is what the agent running a job reads. The rules below are about changing this repository.

## The workflow lives in five places; change them together

A change to the workflow (a step, a gate, what the agent does before or after a step, the stop rule, a command or flag a step uses) is not done until each of these says the same thing:

1. `skills/saos/SKILL.md`, and `skills/saos/demand.md` when the demand format or its confirmation changes.
2. `DESIGN.md`, section 工作方式 (and 终止规则 for the stop rule).
3. The user's workflow window: steps and their status in `src/saos/ui/stage.py` (`STEPS`, `snapshot`), node labels, sub-text and return edges in `src/saos/ui/static/app.js` (`NODES`, `TEXT` in both languages, `back(...)`), and `tests/test_ui.py`. The window infers steps from job files only: a step that leaves no file behind belongs in the label or sub-text of the step it is part of, not in a new node. Check the result in a browser (`uv run saos ui open --job J --no-window` gives the URL); return edges each need their own lane.
4. The workflow diagram `docs/workflow.drawio`.
5. `docs/cli.md` for every command or flag that was added or changed.

When one of them cannot follow yet, say which one and why in your report instead of leaving it silently behind.
