# Answer a question about existing lenses

The outcome of an analysis job is the user's question answered: each number with its definition, the lens it was measured on and the backend that measured it. There is no demand, gate, round or report; `workflow.current` stays `analyse`.

## Set up

Create the job with `--mode analyse`, copy each lens in with `lens import` and load one at a time. Before the first number, run `uv run saos backends --lens <file> --job J` on each lens: `backend` says where it computes, `unreconciled` lists the paths `computed_by` will name, and `carried_unread` lists settings in the file that rtc saves back but never applies. A question that depends on one of those settings (transmission through coatings, a polarised source) is answered on OpticStudio.

## Say what the number is before computing it

Turn the user's words into one definition: the operand, analysis or metric, the fields and wavelengths, the focus (as the file has it, or refocused), the sampling and the reference (chief ray or centroid). When the words allow more than one reading and the choice changes the number, ask before computing ([Stop and ask](../SKILL.md#stop-and-ask)), or give each reading's number with its definition beside it.

Prefer one operand, one analysis or a catalog metric (`uv run saos metrics list --job J`, `ctx.metric`) over a script; write a script ([scripting.md](scripting.md)) only for what they do not cover, reading the lens through `ctx`.

To compare lenses, measure each with the same definition: the same field points in field units (normalised coordinates differ when the field tables do), the same wavelengths and the same focus treatment. When their own settings differ (aperture, field type, object distance), say so next to the numbers: the comparison is then between different conditions.

## Which backend gives the number

The job computes on rtc. An answer needs `trust` `reconciled` or `measured`; anything lower is measured on OpticStudio as [SKILL.md](../SKILL.md#what-a-number-can-decide) describes. An analysis job never changes stage, so the pin holds until `uv run saos daemon use auto`: release it as soon as the OpticStudio numbers are in.

## When the question is a design

Changing parameters to answer what the user asked (a what-if, a sensitivity) belongs here: do it with `ctx.kept`, or save the variant under a new name, and leave the imported lens as it was. Optimising toward a target the user has not agreed as a demand does not: when the question turns out to be "make it meet X", say so, create an interactive job, import the same lens and start at stage 1 ([need.md](need.md)). The analysis job stays as the record of what was measured.

## Answer

Give each number with its unit, definition, lens file and backend, in your reply unless the user asked for a report. Scripts are archived in the job by `exec`; name the archive of any number the user may want to check.
