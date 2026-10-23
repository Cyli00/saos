# Choose and approve the model

The outcome of this stage is an approved model description: a structure chosen for stated reasons, built in Optiland with sourced materials, whose fingerprint the round gate will compare against.

## Build in Optiland

Before the user accepts the Optiland report, all modelling and design happens in Optiland. Optics Optiland cannot represent are explicit capability gaps; report them, and do not start OpticStudio to fill them.

- Build or import through `lens build`, `lens load` and `lens save`. Commands take a logical `.zmx` name (`lens build --as start.zmx`); the backend stores `start.optiland.json` as the authoritative snapshot, and `lens export` writes a `.zmx` only on request.
- Keep sourced materials and mechanical semi-diameters (`lens set --param mech_semi_diameter`) in the model; the interference check uses the parts' outer edges.
- Give surfaces what the model needs with `lens set`; hard limits belong in the demand.

## Choose a starting form

If a recipe matches the system (see the list in the skill entry), read it first.

For a free structure, generate starts from sourced library designs scaled to the demand. Use the most demanding field, f-number and spectral requirements to choose forms, and inspect first-order and third-order predictions before investing in optimisation. There is no candidate quota; a clearly suitable form may be used directly, with the reason in the model notes. For a prescribed structure, show the user's reason and your feasibility verdict.

In a job script:

- `saos.starts.generate(ctx.job, bindings, short_seconds=..., prepare=...)` generates library starts. `bindings` map `efl_mm`, `half_field_deg`, `f_number` and `wavelengths_um` to demand item ids. Read `library/starts/README.md` for sources, units and limits; the source examples do not establish a form's physical limits.
- If strict selection finds nothing, `allow_extrapolation=True` permits exploratory starts outside the example field, f-number or band. Every exceeded condition is kept in the plan and model page; it never changes the demand, source limits or material range checks. Different conjugates stay excluded.
- `prepare` is required: generated prescriptions have no merit function, so the prepare script sets variables and merit. An empty value is `BAD_ARGS`; a script that fails or leaves the merit empty is a configuration error before screening.
- `saos.starts.screen(ctx, generated, workers=...)` runs all candidates without optimisation before the shared short stage. Generated files live in `explore/starts/NNN/`.
- `saos.starts.scale_prescription(data, K)` scales your own prescription.

Scale every start to the demand and the feasibility estimate, not to wherever its source happened to sit. A start scaled to a smaller target because it is easier there changes a directional objective, which is the user's trade (see the skill entry).

## Screen candidates on equal terms

Screen every candidate without optimisation first (`stages: []` in a `candidates` plan of `saos scan`), then run every candidate with the same short stage budget, checks, sampling and prepare script.

The unoptimised pass only shows whether a start traces and fits together; choosing on it is not screening. On the AR-glasses job, a form picked because it alone traced cleanly absorbed half an hour of optimisation before being dropped. Compare candidates after the short stage by shortfall, and list each one's directional objectives next to the feasibility estimate.

- The verdict per candidate is valid, invalid within budget, or not evaluated because of a material or computation failure; material failures stay distinct from connection and UI failures, which propagate.
- Equal fingerprints count once. Keep budget-limited candidates and record `RuntimeWarning` counts; zero warnings does not establish validity, because check scripts can suppress them.
- Plans, input copies, the prepare copy and structure descriptions are sealed. The model page shows prediction and screening records matched to the current model by fingerprint; missing, stale or unmatched comparisons are warnings, not gates.

## Record material sources

Every material needs a source in `sources/materials.yaml` (format in `saos.materials_record`, fields as in `library/materials`). The model page distinguishes source found, data complete, backend supported and actually loaded; without the record, "source found" is unknown. A model glass (`nd`/`Vd` only) is fine for exploration; rounds and reports mark it as not deliverable.

## Describe the model

Write `notes/model-NNN.json` (format: `uv run saos lens describe --help`): what each surface is, the environment you declare (sample, media, bought-in parts), assumptions, first-order reasoning, why this form, and `next_budget` (`max_seconds` and `plan`). Run `uv run saos lens describe --job J --notes <file>` and show the user the four sections of the page: choice, requirements, validity and budget. Before asking them to review, run `uv run saos ui notify --job J --message "..."`.

## Approval and what counts as a structural change

`uv run saos lens approve --job J --model models/NNN-<lens> --quote "..."` records the user's words with the fingerprint, `shown_sha`, unknowns and warnings shown. Approval does not certify unknown computations.

The approved structure is what `saos round` compares against:

- **Structure (needs a new approval):** surface types, the class of medium (air, glass, mirror), aperture types, the stop, a flat Fresnel substrate or a tilted surface, and the system settings (fields and wavelengths may gain points at the end). Check conditions are part of the model too: the number of configurations, each multi-configuration row's operand and parameters, TEMP and PRES, environment temperature and pressure, apodization, polarization, the coating file and afocal units.
- **Yours to change:** radii, thicknesses, conics, polynomial terms, tilt angles, aperture sizes, other multi-configuration values (zoom spacings and the like), glass choice and the coatings of surfaces this design makes. Glasses and coatings are recorded with every round.
- **Environment surfaces** are locked further: material, coating, radius, conic, apertures and tilts.

Approving a structure that differs under the round comparison (beyond appended field or wavelength samples), or a changed environment declaration, needs `--rebuild-reason`. An intact earlier approval of the current structure remains usable even if a later description is unapproved. Damaged approval records are skipped and their paths shown; pre-seal records stay readable as `legacy_unsealed` and must never be given manufactured seals.
