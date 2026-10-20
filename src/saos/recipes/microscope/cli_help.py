"""显微物镜配方的命令帮助，与配方命令一起维护。"""

GROUPS = {
    "microscope": """Microscope objective recipe: adaptation, reproduction, acceptance and library.
adapt and library check/add/list/query are local; repro/accept/perturb/library
verify run through scripts in the daemon. Recipe specifications and extraction
schemas: skills/recipes/microscope/ and docs/recipes/microscope/library.md.
The core demand/model approval gates and rounds still govern delivery.
""",
    "microscope library": """Manage the microscope starting-design library.
Entries live under SAOS_LIBRARY (default library/microscope). check/add/list/query
need no daemon; verify uses the named job and an OpticStudio connection. An
extracted entry is not a verified benchmark. Source/extraction/benchmark rules:
docs/recipes/microscope/library.md.
""",
}

COMMANDS = {
    "microscope repro": (
        "Compare the loaded objective's focal length, NA, working distance and magnification with EXPECT.",
        "Requires the job's loaded lens. EXPECT is a local JSON object, for example\n"
        '  {"efl": 10, "na": 0.25, "wd": 5, "magnification": 20, "rel_tol": 0.01};\n'
        "  omitted quantities are not compared. adapt writes expect.json. rel_tol\n"
        "  defaults to 0.01; optional tolerance_mm gives source-rounding half-widths.\n"
        "  The larger\n"
        "  relative/absolute allowance applies. Focus is temporary and restored.\n"
        "  Returns measurements and per-quantity agreement; execution is archived\n"
        "  through exec in scripts/.", "FILE_NOT_FOUND, UNSAVED_CHANGES, JOB_MISMATCH, SCRIPT_ERROR"),
    "microscope accept": (
        "Evaluate the saved objective against a recipe optical specification.",
        "--spec is a job-relative YAML spec such as sources/spec.yaml or\n"
        "  spec/benchmark.yaml. Format: skills/recipes/microscope/. Creates eval/\n"
        "  results and acceptance models; focus_trace records wavelength, thickness,\n"
        "  failed on-axis rays, c4 and fitting residual at each trial. An incomplete\n"
        "  evaluation returns verdict invalid, spec_shortfall inf and invalid reasons\n"
        "  even when the command succeeds. After a timeout, inspect job progress and\n"
        "  eval/<lens>.accept.json and confirm inputs/spec/version before retrying.",
        "FILE_NOT_FOUND, UNSAVED_CHANGES, JOB_MISMATCH, SCRIPT_ERROR, DAEMON_TIMEOUT"),
    "microscope perturb": (
        "Apply reproducible uniform curvature/thickness perturbations to a saved objective.",
        "--seed fixes the random sequence; --radius-rel is the relative curvature\n"
        "  limit; --thickness-rel defaults to 0. Runs through exec; script/result\n"
        "  copies go to scripts/. Inspect returned lens/state before measuring.",
        "BAD_ARGS, UNSAVED_CHANGES, JOB_MISMATCH, SCRIPT_ERROR"),
    "microscope adapt": (
        "Adapt patent/textbook extraction JSON into a standard objective prescription locally.",
        "EXTRACT schema: docs/recipes/microscope/library.md; --embodiment chooses\n"
        "  a schema-1 embodiment. --focal-length defaults to the printed value for mm\n"
        "  data or 200/abs(beta) for normalized data; --tube-lens defaults to 200 mm.\n"
        "  Field number defaults to the extraction. Stop defaults to printed when\n"
        "  present, else rear_focal; explicit rear_focal/printed/r<surface> is allowed.\n"
        "  --out receives prescription.json, expect.json, adapt_report.json; returns\n"
        "  written paths, expect and report. No daemon needed.", "BAD_ARGS, FILE_NOT_FOUND"),
    "microscope library check": (
        "Check extracted focal length and working distance before library ingestion locally.",
        "Reads extraction JSON, computes an independent paraxial consistency check\n"
        "  and returns checks with printed/computed/envelope/rel_error/ok. Printed\n"
        "  values must lie inside their rounding envelope or within 1% relative error.\n"
        "  This checks the transcription; optical delivery still uses OpticStudio.", "BAD_ARGS, FILE_NOT_FOUND"),
    "microscope library add": (
        "Adapt --extract and store a new library entry with status extracted locally.",
        "--catalogs, --focal-length, --field-number and --stop override adaptation.\n"
        "  Writes entries/ID/extract.json, prescription.json, expect.json,\n"
        "  adapt_report.json and entry metadata with sources/assumptions/doubts.\n"
        "  Returns entry, path, expect and metrics; verification is a separate step.", "BAD_ARGS, FILE_NOT_FOUND"),
    "microscope library verify": (
        "Build a library entry, check reproduction and derive its benchmark using the daemon.",
        "--job supplies the scratch design job. Unsaved changes (even another job's)\n"
        "  are refused unless --discard explicitly allows losing them. Runs reproduction\n"
        "  and two acceptance evaluations to derive/confirm a zero-shortfall baseline;\n"
        "  writes results and benchmark status back to the library entry. Failed\n"
        "  reproduction marks it rejected. Do not treat extracted as verified.",
        "FILE_NOT_FOUND, UNSAVED_CHANGES, SCRIPT_ERROR, DAEMON_TIMEOUT"),
    "microscope library list": (
        "List library entries and benchmark readiness locally.",
        "Returns entries with status, focal length, NA, element count and benchmark\n"
        "  eligibility/reasons. Does not start OpticStudio or alter entries.", "BAD_ARGS"),
    "microscope library query": (
        "Rank stored starting designs for the requested optical specification locally.",
        "--na and --magnification are required; --class defaults to plan_achromat.\n"
        "  Optional --field-number/--working-distance constrain candidates; --limit\n"
        "  defaults to 5. Returns matches for choosing a starting structure, not an\n"
        "  achieved design or delivery verdict.", "BAD_ARGS"),
}
