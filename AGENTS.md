# Working on the saos repository

`skills/saos/` is what the agent running a job reads; each command's `--help` defines its parameters, formats and results, while `docs/cli.md` is the Chinese command overview. `DESIGN.md` records only the design principles, the decisions the user made with their reasons, and the directory rules. The rules below are about changing this repository.

## The workflow lives in four places; change them together

A change to the workflow (a step, a gate, what the agent does before or after a step, the stop rule, a command or flag a step uses) is not done until each of these says the same thing:

1. `skills/saos/SKILL.md`, and `skills/saos/demand.md` when the demand format or its confirmation changes.
2. The user's workflow window: steps and their status in `src/saos/ui/stage.py` (`STEPS`, `snapshot`), node labels, sub-text and return edges in `src/saos/ui/static/app.js` (`NODES`, `TEXT` in both languages, `back(...)`). The window infers steps from job files only: a step that leaves no file behind belongs in the label or sub-text of the step it is part of, not in a new node. Return edges each need their own lane.
3. The workflow diagram `docs/workflow.drawio`.
4. The corresponding command's `--help` registered in `src/saos/cli.py` (recipe commands in their own CLI modules, detailed formats next to their implementation), and the command overview in `docs/cli.md`, for every command or flag that was added or changed.

`tests/test_docs_sync.py` checks that every `saos` command named in SKILL.md, demand.md, DESIGN.md, docs/cli.md, parser descriptions/epilogs and the diagram exists, and that the diagram names the command of every step; the wording itself is still yours to keep in step.

When one of them cannot follow yet, say which one and why in your report instead of leaving it silently behind.

Change `DESIGN.md` only when a principle, a user decision or a directory rule changes, and then in a sentence or two with the reason. Commands, flags, thresholds, measured timings and incident stories stay out of it: they live in the four places above and in the code, where they cannot drift from what runs.

## Checks that run the program

- The workflow window in a browser: `uv run saos ui open --job J --no-window` gives the URL. Check that the steps, labels and return edges match the change.
- The workflow diagram: `SAOS_RENDER_DRAWIO=1 uv run pytest tests/test_docs_sync.py -s` renders `docs/workflow.drawio` with the draw.io viewer and prints the PNG path (needs Chrome and the network); look at the PNG.

## Isolation during verification

- Run every `saos` command with `SAOS_INSTANCE=verify`, so jobs and daemon state go under `work/verify/`. Never read or write the jobs in `work/main/` or any other instance.
- Stop only the processes you started, by PID. Leave the user's daemon and OpticStudio windows running.
