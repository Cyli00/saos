# Choose and approve the model

The outcome of this stage is an approved model description: a structure chosen for stated reasons, built with sourced materials, whose fingerprint the round gate will compare against.

## Build the model

Models are built, saved, described and approved on rtc, like every stage before delivery; the model page then shows the meridional section drawn from the viz data, the same one the workflow window shows. A surface type or setting rtc cannot trace comes back as `backend_switch.stayed`: the lens then computes on OpticStudio, and you tell the user which item caused it ([session.md](session.md#two-backends-in-one-daemon)).

- Build or import through `lens build`, `lens load` and `lens save`. Commands use native `.zmx` files (`lens build --as start.zmx`) in OpticStudio 24.1 line format, whichever backend saves them.
- Keep sourced materials and mechanical semi-diameters (`lens set --param mech_semi_diameter`) in the model; the interference check uses the parts' outer edges.
- Give surfaces what the model needs with `lens set`; hard limits belong in the demand.

## Set the aperture and mirrors as the real system does

These are part of the approved structure, so settle them with the form. Formats: `uv run saos lens build --help`.

- **Variable focal length, fixed F-number:** use `image_f_number` (`EPD = EFL / F#` on an infinite-conjugate lens), so the entrance pupil scales with the focal length as radii and thicknesses vary during optimisation; do not emulate this with `float_by_stop` and a stop semi-diameter variable.
- **A physical iris or mount sets the beam:** use `float_by_stop`, where the stop's semi-diameter defines the system aperture. Give the stop an explicit circular aperture if it must also clip rays. Its semi-diameter may be a variable (`opt vary --param semi_diameter`); put the F-number requirement in the merit function, or the optimiser buys image quality by closing the iris.
- **A coaxial reflective or catadioptric form:** Standard mirrors, negative thicknesses after a reflection, and decentred circular obscurations or rectangular apertures for holes and supports.
- **A beam that clips off axis by design:** `fields.vignetting` describes it at launch.

All of these compute on rtc, ray aiming off or real; real aiming with a stop-defined aperture or a mirror is listed in `computed_by`. A mirror on another surface type, other aperture shapes, tilts and folds keep the lens on OpticStudio for stages 1 to 3 (`backend_switch.stayed`), which holds a licence throughout: choose them when the optics needs them and tell the user why. rtc's wavefront operands refuse mirrors, and the wizard defaults to wavefront: for a reflective form on rtc, set its `data` to `SpotRadius`.

## Choose a starting form

First list what the user fixed (element count, which parts move, positions, surface types, materials) and what they left free; "redesign L1 and L2" fixes the count and leaves the forms and positions free. For whatever is free, generate starts from sourced library designs and patent examples scaled to the demand, searching the patents by field, f-number, band, element count and power signs rather than recalling forms from memory, or synthesise them paraxially. Use the most demanding field, f-number and spectral requirements to choose forms, and inspect first-order and third-order predictions before investing in optimisation. There is no candidate quota; a clearly suitable form may be used directly, with the reason in the model notes. Only a structure fixed in every part skips the search: show the user's reason and your feasibility verdict.

Record each search in the model notes' `exploration.searches`, a null result included (library, query, hits, why you took or left them), and the parts the user fixed in `exploration.fixed_by_user` with their words; `lens approve` refuses a description that has neither (format: `uv run saos lens describe --help`). On the 2026-10-30 fibre zoom job, "redesign L1 and L2" was read as a prescribed structure, the notes said "no structure search", and the start came from slow optimisation instead.

In a job script:

- `saos.starts.generate(ctx.job, bindings, short_seconds=..., prepare=...)` generates library starts. `bindings` map `efl_mm`, `half_field_deg`, `f_number` and `wavelengths_um` to demand item ids. Read `library/starts/README.md` for sources, units and limits; the source examples do not establish a form's physical limits.
- If strict selection finds nothing, `allow_extrapolation=True` permits exploratory starts outside the example field, f-number or band. Every exceeded condition is kept in the plan and model page; it never changes the demand, source limits or material range checks. Different conjugates stay excluded.
- `prepare` is required: generated prescriptions have no merit function, so the prepare script sets variables and merit. An empty value is `BAD_ARGS`; a script that fails or leaves the merit empty is a configuration error before screening.
- `saos.starts.screen(ctx, generated, workers=...)` runs all candidates without optimisation before the shared short stage. Generated files live in `explore/starts/NNN/`.
- `saos.starts.patents.search(half_field_deg=..., f_number=..., wavelengths_um=..., elements=(lo, hi), ...)` searches 2456 patent examples (`library/patents/README.md` lists filters, tags and conversion rules). Pass the chosen ids to `generate(..., patents=[...])`; zoom lenses are taken one position at a time (`or01401-p2`), and `_SPECIAL` or private glasses need an explicit `materials` mapping, never a substitute. `patents.prescription(id)` gives the prescription alone. Glass codes (`nnnnnn.vvvvvv`) become model glasses; `ctx.GlassExpert` replaces them with in-production glasses before delivery ([rounds.md](rounds.md#choose-an-expert)).
- `saos.starts.scale_prescription(data, K)` scales your own prescription.

Scale every start to the demand and the feasibility estimate, not to wherever its source happened to sit. A start scaled to a smaller target because it is easier there changes a directional objective, which is the user's trade (SKILL.md, stop and ask).

## Screen candidates on equal terms

Screen every candidate without optimisation first (`stages: []` in a `candidates` plan of `saos scan`), then run every candidate with the same short stage budget, checks, sampling and prepare script.

The unoptimised pass only shows whether a start traces and fits together; choosing on it is not screening. On the AR-glasses job, a form picked because it alone traced cleanly absorbed half an hour of optimisation before being dropped. Compare candidates after the short stage by shortfall, and list each one's directional objectives next to the feasibility estimate.

- The verdict per candidate is valid, invalid within budget, or not evaluated because of a material or computation failure; material failures stay distinct from connection and UI failures, which propagate.
- Equal fingerprints count once. Keep budget-limited candidates and record `RuntimeWarning` counts; zero warnings does not establish validity, because check scripts can suppress them.
- Plans, input copies, the prepare copy and structure descriptions are sealed. The model page shows prediction and screening records matched to the current model by fingerprint; missing, stale or unmatched comparisons are warnings, not gates.

## Place aspheres before approval

Which surfaces are aspheric is part of the structure, so decide it here rather than in the rounds, where a conversion means a new description and approval. When the demand allows aspheres and a screened candidate misses on a monochromatic residual (spherical, coma, astigmatism), or its element count, length or weight is tight, run `ctx.AsphereExpert(surfaces).rank()` on it after its short stage: the ranking reads the merit rows and means little on an unoptimised start. Give it only the surfaces that can be made aspheric, with their slope limits. Convert the best with `.run(nas)`, screen that version against the spherical one on the same budget, and describe the one you choose. Options: [scripting.md](scripting.md#expert-search-classes).

## Record material sources

Shared reference data (materials, coatings, thermal dn/dT, mechanics, glass catalogs, patents, starts, microscope objectives) is read through `saos.library`. `library.catalog()` lists each library with its entry count, fields and specification search. `library.find("materials", "NOA", category="adhesive")` returns the entries containing the text with equal fields, each with the sources it cites (`total` plus the first `limit`). `library.index(id, wavelength_nm)` gives a materials-library index and refuses wavelengths outside the model's range; `refractive_index(record, wavelength_nm)` does the same for a record in hand. Read the library's README before using its numbers: it states versions, source grades and limits.

Every material needs a source in `sources/materials.yaml` (format in `saos.materials_record`, fields as in `library/materials`). The model page distinguishes source found, data complete, backend supported and actually loaded; without the record, "source found" is unknown. A model glass (`nd`/`Vd` only) is fine for exploration; rounds and reports mark it as not deliverable.

## Describe the model

Write `notes/model-NNN.json` (format: `uv run saos lens describe --help`): what each surface is, the environment you declare (sample, media, bought-in parts), assumptions, first-order reasoning, why this form, and `next_budget` (`max_seconds` and `plan`). Run `uv run saos lens describe --job J --notes <file>` and show the user the four sections of the page: choice, requirements, validity and budget. Before asking them to review, run `uv run saos ui notify --job J --message "..."`.

## Approval and what counts as a structural change

In an interactive job the user approves with the button in the workflow window ([need.md](need.md#confirm-and-set-up-the-window) has the gate rule and the `lens approve --quote` fallback); the window opens the page and records the approval with the fingerprint, `shown_sha`, unknowns and warnings shown. In an autonomous job, `uv run saos lens approve --job J --model models/NNN-<lens> --reason "<the search, the screening, the first-order numbers>"` records the same with your reason. Approval does not certify unknown computations. Rounds refuse a lens whose structure matches no approved description, so a structural change later needs a new description and approval.

The approved structure is what `saos round` compares against:

- **Structure (needs a new approval):** surface types, the class of medium (air, glass, mirror), aperture types, the stop, a flat Fresnel substrate or a tilted surface, and the system settings (system aperture type, its value except under float by stop size, where the stop's semi-diameter is yours to change, telecentricity, ray aiming; fields and wavelengths may gain points at the end). In a non-sequential system: each object's type, reference object, Inside Of and medium class, and sources and detectors in full except their ray counts. Check conditions are part of the model too: the number of configurations, each multi-configuration row's operand and parameters, TEMP and PRES, environment temperature and pressure, apodization, polarization, the coating file and afocal units.
- **Yours to change:** radii, thicknesses, conics, polynomial terms, tilt angles, aperture sizes, other multi-configuration values (zoom spacings and the like), glass choice and the coatings of surfaces this design makes. Glasses and coatings are recorded with every round.
- **Environment surfaces** are locked further: material, coating, radius, conic, apertures and tilts; environment objects also their parameters, position and volume physics.

Approving a structure that differs under the round comparison (beyond appended field or wavelength samples), or a changed environment declaration, needs a rebuild reason: write it in the notes' `rebuild_reason`, which the page shows and the window records (the button stays disabled without it); the fallback takes `--rebuild-reason`. An intact earlier approval of the current structure remains usable even if a later description is unapproved. Damaged approval records are skipped and their paths shown; pre-seal records stay readable as `legacy_unsealed` and must never be given manufactured seals.
