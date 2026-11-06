"""命令帮助的公共契约与编排；处方和扫描格式分别放在对应实现模块。"""

from __future__ import annotations

import argparse


OVERVIEW = """saos: tools for agents to drive Zemax OpticStudio.

Output protocol (help itself is plain text):
  Success: {"ok": true, "result": ...}, exit 0.
  Failure: {"ok": false, "error": {"code": ..., "message": ..., "details": ...}}, exit 1.
  Invalid CLI arguments use the same error envelope, exit 2.
  Non-finite results: infinity -> "inf"/"-inf", NaN -> null; numpy arrays -> lists.

Process model:
  Local: daemon control, job, demand, rule-out, stop, report, scan --show.
    ui runs a local service.
  Daemon: lens, opt, exec, check, round and scan execution. It owns the
    OpticStudio connection.
"""

CONVENTIONS = """Long operations
  Use --detach for exec, check, round, opt local/hammer/global when computation
  may outlast your caller's time limit. A detached client writes the request to
  runs/NNN-<op>.request.json and the response to runs/NNN-<op>.json in the job.
  The immediate result has detached, run, launcher_pid and follow. The response
  file has status running/done/failed and result or error; job show also reports
  lost if the process disappeared without a final result. Follow with:
    uv run saos job show J
    uv run saos job cancel J
  scan --detach instead uses a background worker-pool thread with files under
  scans/NNN/; follow with uv run saos scan --job J --show.

Busy, cancellation and timeouts
  One daemon request runs at a time. BUSY: wait, inspect job show, or cancel the
  running job; do not resubmit. A detached scan leaves the daemon available but
  holds the worker pool (POOL_BUSY); daemon stop refuses until the scan ends.
  DAEMON_TIMEOUT stops waiting, not computing: inspect details.still_running,
  job progress and archived results before retrying. Cancellation is cooperative;
  completed work is retained. progress.json write failures are retried then
  logged without aborting computation. log.jsonl gains a row only on completion.
  Long computations leave the user cores free (default: logical cores minus 2,
  at least 1) and run below normal priority.

Environment
  SAOS_INSTANCE: work/<instance> (default main); isolates jobs and daemon state.
  SAOS_HOME: explicit runtime directory, overriding SAOS_INSTANCE.
  SAOS_LANG: zh/en for human outputs; command help/errors always English.
  SAOS_UI=0: disable automatic workflow windows.
  SAOS_LIVE_S: seconds the daemon spends measuring the live metrics after each
    lens change (default 3; 0 off).
  SAOS_POOL_IDLE_S: worker idle lifetime, default 600 seconds.
  SAOS_OPTICSTUDIO_DIR: OpticStudio installation; SAOS_BACKEND: auto (default), zos or rtc as
    daemon start --backend, fake for tool tests; SAOS_GLASSCAT: AGF folder for rtc,
    default Documents/Zemax/Glasscat.

Help
  uv run saos <command> --help
  uv run saos <command> <subcommand> --help
"""

GROUPS = {
    "metrics": "Query the backend-neutral metric catalog and store display-only selections for a job.",
    "daemon": 'Control the per-instance daemon holding rtc and a private standalone OpticStudio.\nJobs and daemon state use SAOS_INSTANCE (or SAOS_HOME).\n',
    "job": """Manage local job directories without contacting OpticStudio.
Jobs live in work/<instance>/jobs/<job>. show, cancel and verify read/write job
files directly and remain available while the daemon is busy.
""",
    "lens": """Inspect and edit lenses through the daemon.
--job must match the loaded lens for operations on the current system. Lens file
names are relative to this job's lenses/. A missing lens gives NO_LENS_LOADED;
a different job gives JOB_MISMATCH. Import copies files; build/load establish a
working lens.
Before load/unload/build replace the lens, the agent's unsaved edits require
lens save or --discard. Archived rounds cannot be overwritten. The workflow
window (saos ui) shows the current lens.
""",
    "opt": 'Configure and run optimization on the loaded job lens: OpticStudio, or on an\nrtc instance damped least squares with boundary rows as exact constraints and\nrtc Hammer/global search.\nlocal/hammer/global require the current demand to be confirmed and free of\nblocking questions. Model approval is required to record rounds. Geometry and\nmaterial changes require saving before worker scans; model glass is exploratory\nand cannot be delivered. Optimization keeps the best reached system in memory.\n',
    "demand": """Validate and confirm sources/demand.yaml locally, without OpticStudio.
Format: .agents/skills/saos/demand.example.yaml; shortfall and stop rules: saos round --help.
Each bound needs a source; confirmation records the user's actual approval and
content hash. Constants, variables, objectives (except their descriptions), structure
and stop define the version; rewording a description, the summary, questions or
language does not require reconfirmation. Changing only sources or the structure
reason needs confirming again while the measured numbers stay comparable.
""",
    "ui": """Show the user's workflow, current lens and archived round visualizations.
The local service listens only on 127.0.0.1 and reads job files. Optical controls
use the daemon; spot, ray fan, field curvature/distortion and MTF data come from
round exports, and the Starts tab reads explore/starts and scans. A user-closed window reopens
automatically only at unconfirmed demand show, lens describe or round; ui open
always opens it. Gate windows restore/raise without taking keyboard focus.
""",
}

COMMANDS = {
    "metrics list": ("List metrics, parameters, scalar selectors and active-backend availability.",
                     "Returns metrics and saved display-only selection. Availability does not imply reconciliation or model validity.", "NO_LENS_LOADED, JOB_MISMATCH"),
    "metrics get": ("Measure one catalog metric on the loaded backend.",
                    "Returns scalar, curves or table without creating evaluation evidence. Use demand metric checks for evidence.", "BAD_ARGS, BACKEND_UNSUPPORTED"),
    "metrics select": ("Replace the job's display-only metric selection.",
                       "Writes metric_selection.json. Keys must be unique and distinct from demand ids. Does not change judging.", "BAD_ARGS"),
    "daemon start": ('Start the daemon with rtc and OpticStudio (2024 R1 Premium) as its two backends.', 'Each command runs on the backend of its job\'s workflow stage:\n  rtc for every stage before delivery; OpticStudio for delivery, and always for\n  round --confirm and tolerance scans. OpticStudio starts\n  the first time a command needs it (cold start 6-8 s) and stays up until the\n  daemon stops. A switch hands the loaded lens over through its file: unsaved\n  changes are refused (UNSAVED_CHANGES), then both sides are compared value by\n  value; results carry backend_switch (handover.agrees, mismatches, rays, or\n  stayed when rtc cannot reproduce the lens). Differences stay on the lens as\n  unverified provenance. saos daemon use pins a backend.\n  --backend auto (default, or SAOS_BACKEND) as above; zos connects OpticStudio at\n  start; rtc runs rtc only, without OpticStudio or licence, and refuses its\n  commands with BACKEND_UNSUPPORTED. OpticStudio runs as a private standalone\n  instance. Startup errors include details.log_tail. Background logs: daemon/daemon.log;\n  startup creates no console window.', 'DAEMON_ALREADY_RUNNING, DAEMON_START_FAILED, DAEMON_TIMEOUT, ZOS_LICENSE, ZOS_VERSION_MISMATCH, ZOS_ERROR'),
    "daemon use": (
        "Pin the backend commands run on, or return to the workflow stage's choice.",
        "auto: by the job's workflow stage. rtc or zos: every command runs there, except\n"
        "  that commands needing OpticStudio still use it, until the workflow stage of\n"
        "  the job (--job, else the loaded lens's) changes; then auto returns by itself.\n"
        "  The result has pinned_stage. While pinned, results carry backend_pin (backend,\n"
        "  pinned_at, stage, stage_rule); the result where it ends has backend_pin.released.\n"
        "  With --job, the loaded lens is handed over now (unsaved changes refused) and\n"
        "  the result has backend_switch. Refused on an rtc-only daemon.", "BAD_ARGS, UNSAVED_CHANGES, BUSY, ZOS_LICENSE"),
    "daemon stop": (
        "Stop this daemon; wait for it and its private OpticStudio process to exit.",
        "Unsaved agent edits require --discard. A detached scan must finish or be\n"
        "  cancelled first. --force kills a process that cannot exit normally.\n"
        "  --all stops every daemon on the machine one by one under the same rules (one\n"
        "  that started OpticStudio holds a licence until it exits) and clears state files left by dead\n"
        "  ones; details list stopped, stale_removed and failed per instance.",
        "UNSAVED_CHANGES, BUSY, DAEMON_TIMEOUT"),
    "daemon list": (
        "List daemons from their state files, without contacting them.",
        "Each row: instance (None outside work/), home, pid, alive, started_at and\n"
        "  running jobs. Works while a daemon is busy. --all lists every daemon on the\n"
        "  machine: instances under work/ and ones registered elsewhere (test homes).",
        "BAD_ARGS"),
    "daemon status": (
        "Inspect this instance and, once per session, the machine's optical environment.",
        "Returns connection/current lens plus environment: all instance daemons and\n"
        "  running jobs and OpticStudio processes vs the 8-instance limit. --no-env\n"
        "  reports only this daemon.", "DAEMON_TIMEOUT"),
    "daemon run": (
        "Run the daemon in the foreground; internal entry point used by daemon start.",
        "Holds the connection until shutdown; logs under daemon/. Prefer daemon start\n"
        "  for normal use.", "ZOS_LICENSE, ZOS_VERSION_MISMATCH, ZOS_ERROR"),
    "job create": (
        "Create a job from --name; no daemon needed.",
        "Returns job, path, state. Creates a timestamp-name id, job.json, lenses/ and\n"
        "  sources/.\n"
        "  --mode interactive (default, designing from scratch): the user passes the gates\n"
        "  in the workflow window and may ask for changes there. --mode autonomous\n"
        "  (exercises, tests): the agent passes them with --reason and may stop without\n"
        "  --quote; reports mark these decisions. Only the user can later make an\n"
        "  autonomous job interactive (in the window); never the reverse.", "BAD_ARGS, JOB_EXISTS"),
    "job list": ("List this instance's jobs locally.", "Returns jobs with their recorded state; no optical files are changed.", "BAD_ARGS"),
    "job show": (
        "Read a job's state and running computation progress from local files.",
        "Returns job, path, state, workflow, lenses, log_tail (--log count), progress,\n"
        "  cancel_requested and the last 5 detached runs. Works while the daemon is busy.\n"
        "  workflow has current (interview, demand_confirm, modeling, model_approve,\n"
        "  design, deliver, zos or final_report), steps with status done/active/\n"
        "  waiting/blocked/pending and detail, blocking open questions and the round\n"
        "  decision; it is what the workflow window shows.\n"
        "  A quiet log is not proof of a stalled computation; use progress and runs.", "JOB_NOT_FOUND"),
    "job verify": (
        "Check hashes of sealed evidence files against seals.jsonl locally.",
        "Returns intact and problems for modified/missing files; inspect intact even\n"
        "  when the command succeeds. Does not repair or rewrite evidence.", "JOB_NOT_FOUND"),
    "job cancel": (
        "Request cooperative cancellation by writing the job's cancel.request.",
        "Works while the daemon is busy. OpticStudio optimizers receive Cancel;\n"
        "  scan/map workers finish their current points; scripts must check progress()\n"
        "  or cancel_requested(). Completed results remain archived.", "JOB_NOT_FOUND"),
    "lens import": (
        "Copy an external .zmx into the job's lenses/ without changing its source.",
        "SRC is an external path; --as chooses the destination name, default source\n"
        "  name. Existing files require --overwrite. Load the imported lens to use it.", "FILE_NOT_FOUND, FILE_EXISTS, PATH_OUTSIDE_JOB"),
    "lens load": (
        "Load FILE from this job's lenses/ into OpticStudio.",
        "Unsaved agent changes require saving or --discard. Updates current lens\n"
        "  state and view.json for the workflow window.", "FILE_NOT_FOUND, UNSAVED_CHANGES, ZOS_LOAD_FAILED, PATH_OUTSIDE_JOB"),
    "lens unload": (
        "Unload the current lens; --job must own it.",
        "Save unsaved agent edits or pass --discard.", "JOB_MISMATCH, UNSAVED_CHANGES, ZOS_ERROR"),
    "lens info": (
        "Read the loaded job lens's system settings.",
        "Returns aperture, fields, wavelengths, catalogs, surface count and stop;\n"
        "  non-sequential mode instead reports mode, wavelengths, NSC options and objects.", "NO_LENS_LOADED, JOB_MISMATCH"),
    "lens surfaces": (
        "Read the loaded job lens's Lens Data rows or non-sequential objects.",
        "Sequential rows include type, radius, thickness, material, clear/mechanical\n"
        "  semi-diameters, conic and solves. NSC returns objects with type, file,\n"
        "  placement, material, parameters, volume scattering, faces (face numbers and\n"
        "  names with is/coating/scatter), scatter_to, source and type_options, changed\n"
        "  diffraction/index_model, and per-wavelength index; it has no surface rows.", "NO_LENS_LOADED, JOB_MISMATCH"),
    "lens describe": (
        "Create the user's model description page for the loaded lens; on rtc its section is drawn from the viz data.",
        "", "NO_LENS_LOADED, JOB_MISMATCH, BAD_ARGS, ZOS_ERROR"),
    "lens approve": (
        "Record the user's approval after showing models/NNN-<lens>/index.html.",
        "The user normally approves with the workflow window's button (via: window);\n"
        "  this command is the fallback when the window cannot be used and is refused\n"
        "  while it is open. --model is the job-relative description folder; --quote is\n"
        "  one user message verbatim (via: cli); an autonomous job passes the gate with\n"
        "  --reason instead (via: auto), which is refused while shown.reach blocks: a must\n"
        "  without a class, a first_order must not met, or another must 5 times off or more\n"
        "  (a structure the demand prescribes is only recorded). The user may overrule it by\n"
        "  approving. Seals approval.json with fingerprint, outstanding warnings,\n"
        "  shown_sha (digest of the four sections the page showed) and the unknowns\n"
        "  still open then (unknowns_acknowledged). Descriptions made before these\n"
        "  fields existed lack the last two.\n"
        "  Rounds require a matching approved structure; structural changes need approval\n"
        "  again; --rebuild-reason (the window: notes.rebuild_reason) covers structure or\n"
        "  environment-declaration changes.\n"
        "  Appended field/wavelength samples are allowed, using the round gate comparison.\n"
        "  An intact earlier matching approval remains usable. Damaged history is reported\n"
        "  by path and skipped; modern missing seals cannot be treated as legacy.\n"
        "  Design variables may change. What the NSC fingerprint locks:\n"
        "  uv run saos lens build --help.", "FILE_NOT_FOUND, BAD_ARGS, MODEL_NOT_APPROVED, EVIDENCE_TAMPERED"),
    "lens set": (
        "Set one surface parameter on the loaded job lens.",
        "--value is numeric (including inf), material name or comment text; --param\n"
        "  selects radius, thickness, conic, semi_diameter, mech_semi_diameter, material,\n"
        "  comment or par<N>. Mechanical semi-diameter is the part edge for interference\n"
        "  and edge thickness checks. Marks the lens dirty and refreshes its view.", "BAD_ARGS, NO_LENS_LOADED, JOB_MISMATCH, VALUE_NOT_APPLIED"),
    "lens save": (
        "Save the loaded job lens and normally reload it to compare the saved system.",
        "Default destination is the current file; --as names another lenses/ file,\n"
        "  requiring --overwrite if it exists. --no-verify skips reload comparison.\n"
        "  Archived lenses/rounds/ files cannot be overwritten; use a working copy.", "FILE_EXISTS, PATH_OUTSIDE_JOB, ZOS_SAVE_FAILED, VERIFY_FAILED"),
    "lens operand": (
        "Evaluate an optimization operand on the loaded job lens.",
        "TYPE is the operand name; --int1/--int2 and --d1 through --d6 supply its\n"
        "  parameters. Example: uv run saos lens operand --job J EFFL --int2 2.\n"
        "  Returns the measured operand value; no evidence archive is written.", "BAD_ARGS, NO_LENS_LOADED, JOB_MISMATCH, ZOS_ERROR"),
    "lens analysis": (
        "Run an OpticStudio analysis and export numeric and text results.",
        "TYPE is an AnalysisIDM name; --list lists available types. --set KEY=VALUE\n"
        "  uses settings object names; Field/Wavelength/Surface accept numbers or all,\n"
        "  enums use member names.\n"
        "  Writes analyses/NNN-TYPE.json and .txt; grids larger than 256x256 are reduced.\n"
        "  Response omits grid bodies but includes statistics, status ok/partial/empty\n"
        "  and problems (part, kind empty_series/extract_failed, message). Partial/empty\n"
        "  also include text_head. Inspect messages/settings/message before retrying.\n"
        "  No result object: ZOS_ERROR with details.kind=no_results and settings.", "BAD_ARGS, NO_LENS_LOADED, JOB_MISMATCH, ZOS_ERROR"),
    "lens build": ("Build and save a sequential, mixed-mode or pure non-sequential lens from Prescription JSON.", "", "BAD_ARGS, FILE_EXISTS, UNSAVED_CHANGES, ZOS_ERROR"),
    "exec": ("Execute a Python script defining run(ctx) inside the daemon on this job.", "", "SCRIPT_ERROR, UNSAVED_CHANGES, JOB_MISMATCH, ZOS_CONNECTION_LOST"),
    "scan": (
        "Scan, compare structures, minimize, probe or tolerance with the daemon's worker pool.",
        "Use --plan to execute, or --show [NNN] to read progress/results locally.\n"
        "  Most modes need a saved current lens; candidates use their own files.\n"
        "  Candidates have optional hypothesis; stages: [] screens without optimisation.\n"
        "  Then run every candidate with the same short budget. Prescriptions with optimisation\n"
        "  require prepare to set variables and merit; script errors or empty merit are BAD_ARGS. Invalid means invalid within budget.", "BAD_ARGS, UNSAVED_CHANGES, POOL_BUSY"),

}

COMMANDS.update({
    "opt vary": ('Make surface parameters or multi-configuration cells variable/fixed.', 'Use --param with --surfaces (1-16 or 1,3,5-7), --config-rows with\n  optional --configs (default all), or --param with --objects; these forms are\n  mutually exclusive. --objects varies non-sequential object position, tilt\n  or parameters (--param x/y/z/tilt_x/tilt_y/tilt_z/par<N>/column header) and\n  returns object_solves; in a mixed-mode lens --group names the NonSequential\n  surface holding them (optional when there is only one).\n  Configuration variables need at least 2 configurations and return\n  config_solves keyed by row.configuration. Only thickness may vary at object.\n  Material needs --mode: model varies nd/Vd (not deliverable); substitute lets\n  Hammer/global swap real glasses. --catalog selects one catalog per surface,\n  default installed SAOS_CDGM_INPROD, otherwise all catalogs in use;\n  SAOS_NHG_INPROD is another option. Catalog is added only after validation.\n  catalog reports the actual choice; outside_catalog warns about existing\n  glasses that substitution may replace. Use per-surface catalogs to preserve\n  manufacturers. Local optimization does not substitute glasses. --off fixes\n  variables (model glasses keep their current nd/Vd). Partial failures can leave\n  earlier surfaces modified and the lens dirty. Design constraints:\n  .agents/skills/saos/references/rounds.md.', 'BAD_ARGS, NO_LENS_LOADED, JOB_MISMATCH, ZOS_ERROR'),
    "opt merit": ("Clear, build, add to, script or inspect the loaded lens's merit function.", "", "BAD_ARGS, FILE_NOT_FOUND, NO_LENS_LOADED, JOB_MISMATCH, ZOS_ERROR"),
    "demand show": (
        "Validate sources/demand.yaml and show its current version and confirmation.",
        "Returns version/hash, confirmation, open questions, summary and weak_basis\n"
        "  for unsourced/assumed limits. Allowed top-level fields: constants, variables,\n"
        "  objectives, open_questions, summary, notes, lang, stop. Check method: operand,\n"
        "  analysis or script. Bounds use finite max/min or equals+tol; directional\n"
        "  goals use minimize/maximize with scale (maximize also ideal), not must.max=0.\n"
        "  Legacy harness.yaml is not converted. Format: .agents/skills/saos/demand.example.yaml.", "BAD_ARGS, FILE_NOT_FOUND, JOB_NOT_FOUND"),
    "demand confirm": (
        "Record the user's approval of the exact current demand version.",
        "The user normally confirms with the workflow window's button (via: window);\n"
        "  this command is the fallback when the window cannot be used and is refused\n"
        "  while it is open. Every limit requires source. --quote is one user message\n"
        "  verbatim; the record and the report mark it as not confirmed in the window.\n"
        "  In an autonomous job pass the gate with --reason instead (via: auto).\n"
        "  Appends confirmations.jsonl bound to the demand hash; opens/raises the\n"
        "  workflow window unless SAOS_UI=0 and reports its state under ui.", "BAD_ARGS, DEMAND_BLOCKED, FILE_NOT_FOUND, JOB_NOT_FOUND"),
    "check": (
        "Measure demand checks on the loaded job lens without recording a round.",
        "Confirmation is not required, so use this while framing feasibility.\n"
        "  --only lists comma-separated check ids; a partial result is never achieved.\n"
        "  Writes a snapshot and evidence to evaluations/NNN/; evidence_file points to\n"
        "  the complete result. Each item includes duration_s. A check changing the lens\n"
        "  cannot be accepted as read-only evidence. warnings flags rtc results pushed\n"
        "  with fast.push and not yet closed by an OpticStudio optimisation.",
        "BAD_ARGS, NO_LENS_LOADED, JOB_MISMATCH, FILE_NOT_FOUND"),
    "round": (
        "Record a measured round for a confirmed demand and approved lens structure.",
        "Requires no blocking questions and a nonterminal demand version. --tier is\n"
        "  comma-separated local,variables,materials,global,structure; omit for round 0.\n"
        "  --merit records a finite external objective instead of the lens merit.\n"
        "  record.backend names who computed the round. On an rtc instance rounds carry\n"
        "  backend rtc and provenance (paths and operands not reconciled with\n"
        "  OpticStudio). --confirm [--of N], on an OpticStudio instance with no lens\n"
        "  needed, re-measures rtc round N (default: the round saos report would\n"
        "  deliver) and appends a tier confirm record; reading the ledger puts its\n"
        "  numbers into round N (confirmed_by, rtc keeps the old ones). agrees is false\n"
        "  when achieved, hard_failing or deliverability differ: the decision returns\n"
        "  continue with confirm_failed and saos report refuses until a new round is\n"
        "  recorded or the user stops. As with lens load, unsaved changes are refused\n"
        "  (UNSAVED_CHANGES; --discard drops them); the lens loaded before is reloaded\n"
        "  from its file afterwards.\n"
        "  saos report refuses an rtc round that is not confirmed.\n"
        "  Saves lenses/rounds/rNNN.zmx only after checks prove the lens unchanged;\n"
        "  evaluations/NNN/result.json is authoritative and ledger.jsonl commits the\n"
        "  round. Returns record, evaluation, stop decision, untried tiers and brief;\n"
        "  warnings flags rtc results pushed with fast.push and not yet closed by an\n"
        "  OpticStudio optimisation (the round is still recorded).\n"
        "  record.achieved governs delivery: evaluation.achieved alone ignores model\n"
        "  glass, hard conditions and parts_clash. Clashing parts, or a surface whose sag\n"
        "  is undefined inside its part edge (parts_clash.undefined), cannot be delivered;\n"
        "  failed interference evaluation prevents recording. Checks must be read-only\n"
        "  (relative 1e-12/absolute 1e-15 roundoff tolerance). A duplicate lens/check/demand\n"
        "  version is refused; inspect the previous round after a client timeout.\n"
        "  Stop states: continue/achieved/exhausted/limit/stopped. Directional goals keep\n"
        "  improving after thresholds pass until ideal values or exhausted strategies.\n"
        "  Shortfall G sums max(0, excess over the bound)/scale per bounded item, plus for\n"
        "  directional goals max(0, distance to ideal)/scale, each times its weight; a check\n"
        "  with no valid value counts as infinite. Constants and variable ranges are hard:\n"
        "  a round breaking one is never delivered and lists it in hard_failing.\n"
        "  Decision order: achieved -> stopped -> limit (max_rounds, default 30) ->\n"
        "  invalid round (a check failed to run: continue) -> targets met with a model\n"
        "  glass (continue with materials) -> exhausted (every tier tried or ruled out\n"
        "  since the last gain of rel, default 0.01) -> continue with next_tier.\n"
        "  Changed checks (including helpers/data and the kit code they use) remeasure\n"
        "  the earlier best archived round first: rebase_of, rebase.equivalent and diffs\n"
        "  describe the comparison. Relative agreement within 1e-9 and no check errors\n"
        "  keeps the window; otherwise a new window starts. Rebases do not consume a\n"
        "  round or reset demand-version limits/stops. Support copies are sealed under\n"
        "  evaluations/NNN/support/_saos/.\n"
        "  After ledger append, exports viz.json and viz/ (geometry/layout/spot/ray\n"
        "  fan/field curvature/distortion/MTF; OpticStudio exports the selected\n"
        "  plots, a missing one shows as a gap) for the workflow window unless\n"
        "  --no-viz. Export errors under viz do not\n"
        "  invalidate the recorded round. MTF falls back to per-field calculation when\n"
        "  a chief ray fails; errors.FftMtf explains missing fields. StandardSpot traces\n"
        "  fewer rays for wavelength weights <1, so its GEO may understate point extent.",
        "DEMAND_NOT_CONFIRMED, DEMAND_BLOCKED, MODEL_NOT_APPROVED, LENS_MODIFIED_BY_CHECK, DUPLICATE_ROUND, SPEC_CHANGED, BACKEND_UNSUPPORTED, UNSAVED_CHANGES, BAD_ARGS"),
    "rule-out": (
        "Exclude strategy tiers using evidence for the current demand version locally.",
        "Requires an existing round in this demand version. --tier repeats or uses\n"
        "  commas; --reason must cite an existing file of the job (scans/NNN, scripts/NNN-...,\n"
        "  explore/...). materials also needs a materials round or a GlassExpert script, or a\n"
        "  reason starting 'not runnable:'. Appends ruled_out.jsonl.\n"
        "  Stop logic no longer waits for these tiers until the next clear gain.", "BAD_ARGS, FILE_NOT_FOUND, JOB_NOT_FOUND"),
    "stop": (
        "Record the decision to stop this demand version locally.",
        "Refers to the last round of the version. --reason cites evidence; --quote\n"
        "  is the user's words including the selected option. An autonomous job may\n"
        "  stop without --quote (via: auto, marked in the report). Appends stops.jsonl;\n"
        "  future rounds in this version are refused and reports show reason/quote.\n"
        "  With no round in the version (no structure passed the reach gate) the demand must\n"
        "  be confirmed and --reason must cite a file of the job; saos report then writes a\n"
        "  no-lens report with the screening, and a later round voids this stop.", "BAD_ARGS, FILE_NOT_FOUND, JOB_NOT_FOUND"),
    "report": ('Generate the delivery report from the demand and ledger without OpticStudio.', "Writes report/index.html and a sealed report/NNN-rMMM-<status>.html; after a stop\n  before any round, NNN-no-rounds-stopped.html with deliver: null. Language:\n  --lang > demand.lang > SAOS_LANG > system UI language > English. Returns\n  headline (deliver/best_so_far/best_blocked/no_valid), deliverable, deliver,\n  diagnostic and achieved. deliver is null for an undeliverable lens; diagnostic\n  identifies its file/round and hard_failing, measurement_errors, model_glass.\n  An achieved deliverable round with minimum shortfall is preferred; otherwise\n  the best deliverable round is used. Terminal exhausted/limit/stopped can\n  headline deliver with achieved:false. No valid lens means no delivery.\n  Changed demand/checks require a new measured round before reporting; sealed\n  evidence must be intact (uv run saos job verify J lists problems). An rtc\n  round must be confirmed (saos round --confirm); after a disagreeing\n  confirmation a new round is required unless the user stopped.\n  The tolerance section uses the latest finished, uncancelled tolerance scan\n  (yield, per-check distributions, top 10 sensitivities) and states, by lens\n  file content hash, whether it ran on the delivered round's lens; without one\n  it says none was run.", 'BAD_ARGS, SPEC_CHANGED, EVIDENCE_TAMPERED, FILE_NOT_FOUND, JOB_NOT_FOUND'),
    "ui open": (
        "Start the local workflow service if needed and open the user's job window.",
        "--tabs a,b,... picks and orders the analysis tabs (spot, fan, field, mtf,\n"
        "  detectors, metrics, rounds, starts; the layout always has its own card, so\n"
        "  layout is accepted and ignored); --metrics id,... picks and orders the\n"
        "  demand items of the Metrics tab and the check gauges (default every checked item; unknown ids are\n"
        "  refused when the demand exists). Both are stored as ui in the job's state.json\n"
        "  and apply to every later window; --reset forgets them. Tabs that do not apply\n"
        "  (detectors in a sequential system, starts before generation) are skipped;\n"
        "  rounds export every plot whatever the tabs. --tab ID opens on that tab.\n"
        "  --no-window starts only the service and returns its URL. Normally uses a\n"
        "  1080x720 Chrome app window, falling back to the default browser; window is\n"
        "  chrome-app/browser/raised. Six steps show gates/blockers; current geometry,\n"
        "  Lens Data and interference use view.json; the live merit and item values use\n"
        "  metrics.json, both rewritten by the daemon after every lens change (after its\n"
        "  reply; SAOS_LIVE_S bounds the measuring); archived spot/fan/field/MTF use\n"
        "  viz.json; Starts lists generated starts, predictions and screening.\n"
        "  URL &ld=1 expands Lens Data. 2D uses the system symmetry plane (section in\n"
        "  geometry); systems without a common plane offer 3D only.\n"
        "  Service state/log: daemon/ui.json, ui.log, endpoint in ui-endpoint.json.\n"
        "  Listens on loopback only; exits after 15 idle minutes and reuses endpoint\n"
        "  on restart if possible, allowing open pages to reconnect. User closure is\n"
        "  remembered in daemon/ui-closed.json; explicit open always reopens.", "JOB_NOT_FOUND, BAD_ARGS"),
    "ui notify": (
        "Before asking the user, show a message and open or raise their workflow window.",
        "--message is in the user's language. Returns notice and window; the user\n"
        "  can dismiss the notice and it does not survive service restart. The window\n"
        "  restores/raises without taking keyboard focus.", "JOB_NOT_FOUND, BAD_ARGS"),
    "ui serve": (
        "Run the loopback workflow service in the foreground; internal entry point.",
        "Normally started by ui open. Reads job files, serves the workflow and proxies\n"
        "  optical controls to the daemon; service state/log are under daemon/.", "BAD_ARGS"),
})

OPTIMIZATION = """Results and files
  Defaults to logical cores minus 2. Writes progress.json (merit/system count)
  and pushes geometry without rays to the workflow window every 5 s; full view
  follows completion. --max-seconds/job cancel call Cancel and keep reached work;
  stopped_by explains termination. On loop errors, Cancel and wait before raising;
  the lens remains dirty: save it or reload with --discard.
  Returns review: interference crossings/overlaps, thinnest_glass,
  smallest_air_gap, edges_from_clear. --measure takes comma-separated check ids,
  hard or all; use fast checks (measured before AND after, included in runtime).
  measured reports values/gaps, better/worse, shortfall, hard_failing, invalid,
  errors. Unfinished checks, hard failures or interference make valid false;
  warnings explains. A since_round reminder follows accumulated optimization or
  scripts in the design stage; record rounds to measure and retain progress.

Common errors
  DEMAND_NOT_CONFIRMED, DEMAND_BLOCKED, NO_LENS_LOADED, JOB_MISMATCH, BAD_ARGS,
  BUSY, DAEMON_TIMEOUT (may still be running; inspect job show before retrying).
"""

for _name, _purpose, _details in (
    ("local", "Run local optimisation once the demand is confirmed (a model, once approved, must match).",
     "Algorithms: auto/dls/od. Auto selects DLS for sequential systems, OD for NSC. Cycles: auto/1/5/10/50."),
    ("hammer", "Run Hammer once the demand is confirmed (a model, once approved, must match).",
     "Requires --max-seconds. Material substitution uses opt vary --param material --mode substitute."),
    ("global", "Run global search once the demand is confirmed (a model, once approved, must match).",
     "Requires --max-seconds. The ten best systems are archived under lenses/global/NNN/."),
):
    if _name != "local":
        _details += (" Refused with BAD_ARGS while the lens in memory comes from a temporary copy (after ctx.restored() or an "
                     "expert run); lens save first.")
    COMMANDS[f"opt {_name}"] = (
        _purpose, _details + "\n"
        "The command budget includes initial measurements and final review. "
        "Native calls must return before cancellation can complete. An unfinished review is marked invalid. "
        "Use --detach for long work. Solver status, merit improvement and stop reason are recorded separately.\n"
        + OPTIMIZATION,
        "DEMAND_NOT_CONFIRMED, MODEL_NOT_APPROVED, DEMAND_BLOCKED, BAD_ARGS, ZOS_ERROR",
    )

MERIT_HELP_EPILOG = 'Input: wizard --file JSON object (all fields optional)\n  {\n    "type": "RMS", "data": "Wavefront", "reference": "Centroid",\n    "rings": 3, "arms": 6, "glass": [1, 10, 0.5], "air": [0.1, 20, 0.1],\n    "start_at": 1, "assume_axial_symmetry": true\n  }\n  type/data/reference match OpticStudio wizard choices, ignoring case/spaces;\n  invalid names report available choices. Defaults shown above, except glass\n  and air are disabled if omitted. Each is [minimum, maximum, edge] thickness\n  in lens units. arms is 6, 8 or 10; rings and start_at are integers from 1;\n  other keys are rejected. Folded, cylindrical and other non-rotationally\n  symmetric systems must use assume_axial_symmetry:false or the wizard samples\n  only half the pupil. Omitting --file uses defaults.\n\nInput: wizard --file for a non-sequential system (OpticStudio\'s NSC wizard)\n  {\n    "criterion": "Total Flux", "detector": 3, "source": "All",\n    "ray_sampling": "Low (1X)", "boundary": "Greater Than", "target": 1, "weight": 1\n  }\n  Keys: criterion (one of the wizard\'s 13, e.g. Total Flux, Spatial Uniformity,\n  RMS Spot Radius, X Centroid), detector (object number), source (All or object\n  number), ray_sampling (Low (1X) to 1024X), edge_sampling, boundary (Equal To,\n  Greater Than, Less Than), target, weight, configuration, start_at, overwrite,\n  minimum_flux, split, scatter, polarization, ignore_errors. Wrong names list\n  the choices. For add in NSC use NSTR to trace and NSDD to read detectors,\n  filling int1, int2 and data with their parameter columns.\n\nInput: add --file JSON operand list (--file required)\n  [\n    {"type": "EFFL", "int1": 0, "int2": 2, "data": [], "target": 50, "weight": 1},\n    {"type": "TOTR", "int1": 0, "int2": 0, "target": 100, "weight": 0.1}\n  ]\n  type is a MeritOperandType name (case-insensitive). int1/int2 default to 0;\n  data supplies remaining operand parameters (up to 8 columns total including\n  int1/int2). target defaults to 0, weight to 1. Rows insert in list order before the first\n  DMFS (at the end without one), so rebuilding the default function from DMFS\n  keeps them; later rows move down. The batch is read back and refused whole\n  when OpticStudio would change a value (e.g. a wavelength number out of range).\n  clear removes all rows; show reads rows and calculates the current merit.\n\nResults and files\n  Changes the in-memory merit function; use lens save to persist it. Returns\n  loaded lens state, merit and rows; no separate archive is written.\n\nCommon errors\n  BAD_ARGS (unknown operand/wizard choice), FILE_NOT_FOUND, NO_LENS_LOADED,\n  JOB_MISMATCH, ZOS_ERROR.\n'

EXEC_HELP_EPILOG = """Input: a Python script defining run(ctx)
  def run(ctx):
      before = ctx.get("3.radius")
      ctx.set({"3.radius": ctx.args.get("radius", before)})
      ctx.progress("measuring", radius=ctx.get("3.radius"))
      return ctx.measure()

  Return JSON-compatible data; numpy scalars/arrays convert automatically.
  --args-file is a JSON object. Repeated --arg KEY=VALUE overrides its keys;
  VALUE is parsed as JSON or retained as text. --name selects the archive label.
  Requires the job's lens when the script uses optical operations. Always use
  ctx.load/save_as rather than raw ZOS-API file operations to track lens state.

Context and file operations
  ctx.system: PrimarySystem; ctx.zosapi: ZOSAPI namespace; ctx.backend: backend.
  Editing: ctx.merit_add(rows) (ordered, before DMFS, id/ref rows; saos.ctx.rows
    builds thickness, sag-gap and NA-proxy rows), ctx.solve(key, kind, ...),
    ctx.insert_surface(i, type, **values), ctx.change_surface_type(ref, type).
  Experts (sequential, one configuration): ctx.GlassExpert(surfaces | "model",
    catalogs=..., partial="working"|"gF").run() / .candidates(s);
    ctx.AsphereExpert(surfaces, terms=4, engine="opticstudio"|"rtc").rank() /
    .run(nas); engine="rtc" ranks by ray errors in an rtc copy, acceptance stays
    on the MFE. They keep the lens in memory only: save with ctx.save_as(name);
    asphere run changes structure.
  ctx.job, ctx.args, ctx.lens (Path or None), ctx.dirty.
  ctx.path(rel): path contained in the job; ctx.mark_dirty(): record edits.
  ctx.load(name), ctx.save_as(name, overwrite=False): names relative to lenses/.
  ctx.show(): push geometry/Lens Data/interference to the workflow window now;
    ctx.progress and the end of every command push it on their own.
  ctx.progress(stage, **numbers): report progress and refresh the window's current
    lens now and then; True means cancellation requested.
  ctx.cancel_requested(): check cancellation without reporting. Long scripts must
    stop at safe points and retain completed work after uv run saos job cancel J.

Parameters (also available on worker w)
  ctx.get(key), ctx.values(keys), ctx.set({key: value}), ctx.surface(index_or_comment).
  Surface keys: "3.radius", "lens dome.conic", "12.par1"; surface index or unique
    comment; negative indices count from image (-1 is image). Parameters: radius,
    curvature (1/R; 0 is flat), thickness, conic, semi_diameter,
    mech_semi_diameter (part edge), material, comment, par<N>, nd, vd.
  nd/vd read the material's nd and Vd (model glass from its solve, catalog glass
    from the catalogs in use). Writing either replaces the material by a model
    glass keeping the other value and dPgF, so a catalog glass's partial
    dispersion becomes approximate; AIR, MIRROR and ABSORB raise. ctx.kept
    restores only the numbers (the material stays a model glass); use
    ctx.restored() to return to the catalog glass. Scan workers reload per point.
  System keys: aperture, field.<n>.x/y, wave.<n>, config.<configuration>.<row>.
    GLSS values are checked against active catalogs; changing the current config
    applies immediately. Solves/pickups preventing a value raise VALUE_NOT_APPLIED.
  NSC keys: object.<number or comment>.<parameter>; x/y/z, tilt_x/tilt_y/tilt_z,
    material, comment, par<N> or column header, mean_path, vp.<DLL parameter>
    (e.g. object.Cortex.vp.g), nd, vd, face<N>.is|coating|fraction|sigma|rays
    (face N, e.g. object.plate.face2.fraction; fraction, sigma and rays change
    the face's current scatter model and raise when it has no such field).
    Mixed mode: object.<object>@<NonSequential surface>.<parameter>; @ may be
    omitted with a single group. File-based objects (CAD, polygon, ray file)
    are referenced by number or file name (e.g. block.STL); their comment is
    the file name and cannot be set. ctx.object_index(ref) resolves a reference.
  with ctx.kept(keys): restores those values, including on error.
  with ctx.restored(): restores the whole lens using a temporary saved copy
    (about 0.1 s); useful for trials that add/remove surfaces. Afterwards opt
    hammer/global wait for a lens save.
  ctx.configurations(): configuration count; with ctx.configuration(n): temporarily
    switches to configuration n and restores the original even on error. Include
    n in memo keys for per-configuration checks. Checks must leave the lens unchanged.

Sequential rays, analyses and focus
  ctx.trace(rays, to=None): rays are (wave,hx,hy,px,py) tuples. Returns numpy arrays
    x,y,z,l,m,n,error,vignette,intensity and mask ok. Coordinates are target-local;
    kit.global_matrix(ctx, surface) supplies rotation/translation to global.
    Measured cost on this machine: about 20 microseconds/ray (pythonnet calls).
  ctx.spot(fields=((0,0),), wave=None, grid=21, to=None, refocus=False, min_rays=3):
    per-field/per-wave pass rate, chief intercept, centroid, RMS and geometric
    radius about centroid. refocus adds best shift along local target z and RMS,
    extrapolating straight rays (requires uniform medium beyond target).
    Afocal systems reject an implicit image-plane target: use direction cosines,
    afocal operands/analyses or an explicit finite target instead.
  ctx.op(name, int1=0, int2=0, *data): any optimization operand.
  ctx.analysis(name, settings=None): exported numeric data without files; status
    ok/partial/empty, problems and text_head when incomplete. Inspect these before
    rerunning. ctx.quick_focus(): OpticStudio quick focus.
  with ctx.huygens(wave=None, pupil=64, image=128) as h: h.psf(field, delta_um)
    returns kit.Psf (values[y][x] unnormalised, dx/dy in um) or None; field is a
    field number or (x, y) in field units, added for the session only. Keep focus
    scans and field sweeps in one session (each session opens an analysis window).
    kit.psf_widths(p, n_photon=1, ee=0.8): fwhm_x/fwhm_y through the peak and
    ee_diameter about the centroid. kit.psf_axial(peak_at, step_um, reaches=(15, 30),
    n_photon=1, scan="window"): peak_at(k) gives the peak k steps out of focus (move
    and restore the plane yourself); returns fwhm_um, peak_offset_um, reach, planes,
    curve or None. scan="outward" stops once both sides fall below half: far fewer
    planes, but a higher peak farther out is missed (a different definition).
    ctx.wavefront_rms(field, wave=None): ray-integrated RMS to centroid, waves; a
    list of fields gives a list from one analysis.
    ctx.aperture(surface): the surface aperture. rtc: huygens and wavefront_rms
    are BACKEND_UNSUPPORTED until reconciled with OpticStudio.
  from saos import kit: global_matrix, sag, pupil, best_focus, material_nd
    (model glass uses its solve's nd), profile, fwhm. Optical evaluations use ZOS-API.
  from saos import micro: micro-optics measurements (focus depth in a medium,
    marginal NA, part gaps and housing clearance, collection, PSF widths, zoom
    travel); DesignFailure means inf through micro.guarded. Definitions are in
    each function's docstring. ctx.spare_map(fn, items, keep_free=2) runs fn
    ("saos.<module>:<function>") on the pool only when licences are free, else None.
  from saos import library: shared reference data. library.catalog() lists the
    libraries; library.find(name, text, **fields) returns entries with their
    sources; library.index(material_id, wavelength_nm) never extrapolates.

Extrema and sampling coverage
  ctx.cra(direction=(0,1), wave=None, n=33, refine=2, to=None): actual chief-ray
    angle to local target z, degrees; maximum over wavelengths, with
    per_wave_at_extreme. Missing chief rays count as missing samples.
  ctx.field_profile(fn, direction=(0,1), n=17, find="max", refine=1,
                    unit="", quantity=""): fn(ctx,hx,hy) must not change the lens.
  ctx.wave_profile(fn, lo, hi, n=33, find="min", refine=2, wave=None): temporarily
    sweeps a wavelength (default primary) in [lo,hi] micrometres; calls fn(ctx,wave),
    then restores it. Finds troughs between discrete design wavelengths.
  direction is the normalized-field endpoint; rectangular field diagonal is (1,1).
  kit.profile(evaluate,lo,hi,n=33,find="max",refine=2): evaluate gets a batch of x.
  Shared result: quantity/definition/unit, range/step, value/at, local extrema,
    missing, refine_shift, at_edge, samples, coverage {ok,notes}. Coverage fails
    for missing values, refinement shift > relative 1e-3 or fewer than 3 valid
    points. In checks, insufficient coverage means an incomplete measurement;
    do not assume a field edge or the design wavelengths contain the extreme.

Demand checks and shared cache
  ctx.measure(ids=None): same demand checks as check/round, without evidence files;
    returns values, shortfall (measured items only), gaps, hard_failing, invalid,
    errors. Use it as the objective rather than reimplementing the checks.
  ctx.cache is shared across checks within one check/round/measure/scan point.
  ctx.memo(key, compute) calculates once per cache; ctx.lib("fiberlib") loads
    checks/fiberlib.py once per cache. After changing the lens in exec, call
    ctx.cache.clear(). Cache keys must distinguish configurations and settings.

Non-sequential trace and detectors
  ctx.nsc_trace(split=True, scatter=True, polarization=True, ignore_errors=True,
    seed=None, cores=None, clear=True, save_rays=None,
    zrd_format="CompressedFullData", ray_filter="", max_seconds=None): ray count
    is each source's # Analysis Rays; default cores leave 2 logical cores free.
    Fix seed for reproducible checks. Splitting/polarization include Fresnel
    reflection and total internal reflection. Reports progress; Cancel follows
    job cancel or max_seconds. Returns seconds,succeeded,stopped_by,error,
    dead_ray_errors,dead_ray_threshold,total_energy,cores. Treat truncated/dead-ray
    energy as a measurement precondition. save_rays names a ZRD beside the lens;
    strong scattering can make it large: use ray_filter to retain needed rays.
  ctx.detector(ref, grid=False): last-trace power (W; NSDD pixel 0), hits (NSDD
    pixel -3), peak_pixel_power,pixels; grid adds per-pixel power and irradiance
    (W/cm^2). Rectangular and curved detectors as above. Polar and color
    detectors read 0 through NSDD: power is the sum of their own data, hits is
    null; polar grid is azimuth x radial power and power per solid angle (W/sr),
    color grid is power laid out by # X Pixels. Volume detectors sum voxels and
    count a ray once per layer crossed (1 W through 2 layers reads 2 W); grid is
    [z][y][x]. Angular distributions use ctx.analysis("DetectorViewer",
    {"Detector": n, "DataType": "AngleSpace"}).
  ctx.ray_hits(zrd, objects, ray_filter="", max_rays=None): read saved segments
    reaching object numbers/comments; arrays x,y,z,l,m,n,intensity,wave,object,ray
    and rays_read. Filter (e.g. H9 for hits at object 9) before reading a huge file.
  ctx.objects(): type, file, placement, material, parameters, volume scattering,
    faces, scatter_to, per-wavelength indices and global transform per object.

Parallel work
  ctx.map(fn, items, workers=None, reset=True, args=None): fn is a function name
    in this script, "explore/x.py:fn" in the job, or "saos.scan:point". Workers call
    fn(w,item) and must return JSON-compatible data. A saved current lens is copied
    per call; reset reloads it before every item (about 0.08 s). Current lens stays
    unchanged. w exposes the kit methods, measure,args,path,job,worker,reload,cache;
    its cache is fresh per item.
  Persistent pool on the backend the daemon computes on (closed when it switches):
    OpticStudio workers start in about 6-8 s/instance, reused until idle for
    SAOS_POOL_IDLE_S (default 600). ctx.pool_close() releases licences immediately.
    Workers run below normal priority. workers defaults to 4, capped by available
    cores. Licence limit is 8 total including user windows and daemon; inspect
    uv run saos daemon status first. Refused sizes are not retried until pool closes.
  Returns ordered results, per-item errors, worker counts/busy times and startup
    (new workers/unavailability). All unavailable -> ZOS_ERROR. Plan, script copy
    and results are sealed in parallel/NNN/. job cancel waits for current items.
    The pool serves one scan or map at a time (POOL_BUSY).

Search control (from saos import search; evaluations still use ZOS-API)
  search.grid(axes): Cartesian parameter grid.
  search.root(f,lo,hi,target=0,scan=9,xtol=None): scan then Brent; skip None/inf.
  search.minimize(f,start,step,bounds=None,method="nelder-mead",max_evals=200,
    max_seconds=None): Nelder-Mead or Powell, normalize by step, penalize invalid
    points, retain the best at budget/cancellation.
  search.ranked(rows): rank by shortfall.

Results and files
  Seals scripts/NNN-<name>.py and .json for every execution. The response contains
  the script result/archive references.
  NeedsSave after execution conservatively marks the lens dirty (even some
  read-only analyses set it).

Common errors
  SCRIPT_ERROR includes traceback and archive references; daemon stays alive.
  ZOS_CONNECTION_LOST instead ends the daemon; restart after inspecting evidence.
  VALUE_NOT_APPLIED, PATH_OUTSIDE_JOB, UNSAVED_CHANGES, JOB_MISMATCH, POOL_BUSY,
  BUSY, DAEMON_TIMEOUT (client wait ended; computation may continue).
"""

DESCRIBE_HELP_EPILOG = """Input: --notes JSON object (optional)
  {
    "surfaces": {"1": "Front face of the first designed element",
                 "3": "Sample boundary"},
    "environment": [3],
    "assumptions": ["The sample medium is fixed by the user's experiment"],
    "first_order": ["Measured EFFL and working distance, with evidence paths"],
    "exploration": {"searches": [{"library": "patents", "query": {"half_field_deg": 20, "f_number": 4},
                                  "hits": 3, "decision": "or01022 screened in scans/001; the others exceed the length"}],
                    "fixed_by_user": {"parts": "element count and the fold prism", "quote": "the user's words"},
                    "notes": "optional text"},
    "next_budget": {"max_seconds": 900, "plan": "Hammer on the chosen structure, then a 3-point scan of the spacing"},
    "rebuild_reason": "only when the structure differs from the last approval"
  }
  next_budget is the computation the next stage may spend, with the optimisations and
  scans it covers. The page's budget section shows it; a warning asks for it when it is
  missing. Both keys are required: max_seconds a positive number, plan non-empty text.
  surfaces maps string surface indices to the physical part each face represents.
  Non-sequential notes use objects instead: {"objects": {"1": "Sample volume",
    "2": "Source", "3": "Detector"}, "environment": [1], ...}. In a mixed-mode
    lens, objects keys are "<surface>:<object>" and each NonSequential group
    gets its own section; the group follows its surface in the fingerprint.
  environment lists environment/bought-in part indices (sequential: 0 through
  image; NSC: object numbers starting at 1). These parts still need material
  sources but are exempt from the manufactured-part model-glass restriction;
  their declared settings are locked into the approval fingerprint.
  assumptions/first_order are lists of explanatory text. exploration records the
  starting-form search: each search with its library, query, hits (0 when nothing
  matched) and decision, and/or fixed_by_user with the parts the user fixed and
  their words. A description without either (old free text included) shows a
  warning and lens approve refuses it; a malformed exploration is BAD_ARGS. Without
  a candidates scan the page separately warns that screening evidence is absent.
  rebuild_reason is shown on the page; the window's approval of a structure that
  differs from the last approval takes it from here.

Results and files
  Seals models/NNN-<lens>/index.html, model.json and layout PNGs (3D views and,
  when available, a symmetry-plane section); with --notes, also archives
  notes/model-NNN.json.
  Returns page, lang, warnings, layout, shown_sha, unknowns, next. Show the page to the user before
  uv run saos lens approve --job J --model models/NNN-<lens> --quote "their words".
  Four sections follow the warnings. Structure choice: saved predictions, screening results, sources and the matching candidate,
  the other candidates of its comparison with their
  verdicts, and a link to the comparison page (a prescribed structure shows the user's reason).
  Requirements: each demand item with its current value (measured with the demand's own checks,
  no evidence kept), bound, status met / not met / unknown / no bound, and source; agent assumptions are
  marked. Computation validity, checked by the system: the chosen candidate's numeric warnings,
  items with no valid value or a failed check, and each glass at four levels (source found, data
  complete, backend supports, actually loaded); a level the code cannot establish reads unknown.
  Budget: notes.next_budget; malformed budgets are refused, missing budgets warn.
  model.json and approval.json are checked against their seals; shown_sha records what
  the user saw. Normal variable/value changes do not require fresh model approval.
  Changed checks and stale comparisons are shown as notes only. A missing materials record means unknown.
  Scripts can suppress numeric warnings through Python filters; zero proves no validity.
  The validity section says that approval does not vouch for it.
  Language: --lang > demand.lang > SAOS_LANG > system UI language > English.
  Lists all surfaces/nonzero parameters, material sources/per-wave indices,
  aperture/afocal units, environment, apodization, polarization, coating settings,
  ray aiming, configurations and global vertices. Interference is checked in
  every configuration; crossings/overlaps name the configuration.
  warnings includes model glass, missing sources/dispersion, nonflat Fresnel
  substrates, object-NA without telecentricity, vignetting, missing notes/first
  order, missing structure exploration and mechanical clearance concerns.
  Part edges use max(mechanical semi-diameter, clear radius + chip zone), shared
  across both faces of an element and the largest adjacent radius at cemented
  faces; explicit noncircular apertures are not extended. clearances gives the
  minimum glass/air spacing (negative means crossing); lenses lists clear and
  outer radii. edges_from_clear warns if no element has a mechanical margin;
  in mm, thin glass warns against table 17-2 of 李晓彤 (diameter >=3 mm).
  NSC outlines: cylinders, rectangular volumes, sources and rectangular
  detectors from parameters; other types (Standard Lens, CAD, polygon objects)
  from a per-object ExportCAD STL mesh, cached by type, file, parameters and
  placement. NSC overlap uses outline bounding boxes; unrenderable objects are
  unchecked. A detector with outline points outside the volume containing its
  centre (and not inside an ABSORB object) is warned about: light leaking from
  elsewhere in the volume would count as arriving. Cylinder, rectangular and
  annular volumes are tested by parameters, others by mesh ray casting.

Common errors
  BAD_ARGS (--notes must be an object, environment indices must be in range),
  NO_LENS_LOADED, JOB_MISMATCH, ZOS_ERROR.
"""

ARGUMENT_HELP = {
    "job": "job id in this SAOS_INSTANCE; must own the current lens for optical operations",
    "timeout": "seconds to wait for a response (default: %(default)s); timeout does not stop computation",
    "mode": "connection mode (default: %(default)s)",
    "start_timeout": "seconds to wait for daemon readiness (default: %(default)s)",
    "cycles": "optimization cycle budget: auto lets the optimiser stop on convergence; otherwise 1, 5, 10 or 50 iterations (default: %(default)s)",
    "name": "human-readable job name",
    "as_name": "destination file name relative to the job's lenses/",
    "overwrite": "allow replacing an existing destination (archived rounds remain protected)",
    "discard": "discard the agent's unsaved lens edits",
    "src": "external source .zmx path",
    "prescription": "Prescription JSON file; sequential (including mixed mode) and pure non-sequential formats below",
    "surface": "Lens Data surface index",
    "int1": "first integer operand parameter (default: %(default)s)",
    "int2": "second integer operand parameter (default: %(default)s)",
    "merit_action": "clear rows, build with wizard settings, add operands, or show rows",

}
ARGUMENT_HELP.update({f"d{i}": f"operand data parameter {i} (default: %(default)s)" for i in range(1, 7)})


def configure(parser, path: str = "", *, commands=None, groups=None) -> None:
    if commands is None:
        commands = COMMANDS
    if groups is None:
        groups = GROUPS
    from saos import prescription, scan

    formats = {"lens build": prescription.HELP_EPILOG, "scan": scan.HELP_EPILOG,
               "exec": EXEC_HELP_EPILOG, "lens describe": DESCRIBE_HELP_EPILOG,
               "opt merit": MERIT_HELP_EPILOG}

    def visit(current, key):
        current.formatter_class = argparse.RawDescriptionHelpFormatter
        if not key:
            current.description, current.epilog = OVERVIEW, CONVENTIONS
        elif key in groups:
            current.description = groups[key]
            current.epilog = "Run uv run saos " + key + " <subcommand> --help for parameters, formats and results."
        elif key in commands:
            purpose, details, errors = commands[key]
            current.description = "Purpose and prerequisites\n  " + purpose
            if key == "scan":
                current.description += "\n  " + details
            if key in formats:
                current.epilog = formats[key]
            elif key in ("opt local", "opt hammer", "opt global"):
                current.epilog = "Prerequisites and operation\n  " + details
            else:
                current.epilog = "Results and files\n  " + details + "\n\nCommon errors\n  " + errors + "."
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                # argparse 只给带 help 参数的子命令列说明行；命令行定义不写 help，这里按定义顺序补上
                # argparse lists only subcommands added with help; the parser definitions leave it out, so add the rows here in order
                listed = {choice.dest: choice for choice in action._choices_actions}
                action._choices_actions = [listed.get(name) or action._ChoicesPseudoAction(name, (), None) for name in action.choices]
                for name, child in action.choices.items():
                    child_key = f"{key} {name}".strip()
                    visit(child, child_key)
                    brief = (groups.get(child_key) or (commands.get(child_key) or ("",))[0]).split("\n", 1)[0]
                    if brief:
                        for choice in action._choices_actions:
                            if choice.dest == name:
                                choice.help = brief.replace("%", "%%")
            elif action.help is None and action.dest in ARGUMENT_HELP:
                action.help = ARGUMENT_HELP[action.dest]

    visit(parser, path)
