"""需求访谈与设计计划命令；不连接 OpticStudio。"""

from pathlib import Path

from saos.recipes.microscope import interview, plan


def register(sub, parser_class) -> None:
    group = sub.add_parser("interview", help="check the structured interview record and its provenance")
    commands = group.add_subparsers(dest="action", required=True, parser_class=parser_class)
    check = commands.add_parser("check", help="list missing items and filled-in assumptions (no Zemax)")
    check.add_argument("file", help="interview record YAML")
    check.set_defaults(func=lambda a: interview.check(interview.read_draft(Path(a.file))))

    group = sub.add_parser("plan", help="create, review and confirm the HTML design plan")
    commands = group.add_subparsers(dest="action", required=True, parser_class=parser_class)
    create = commands.add_parser("create", help="render a single-file HTML design plan from the interview record")
    create.add_argument("--job", required=True)
    create.add_argument("--interview", required=True)
    create.add_argument("--entry", help="admitted starting design; omit to create a plan awaiting a structure")
    create.add_argument("--rationale", default="", help="why this structure and what still needs changing")
    create.set_defaults(func=lambda a: plan.create(a.job, Path(a.interview), a.entry, a.rationale))
    show = commands.add_parser("show", help="verify the current plan and show its confirmation state")
    show.add_argument("--job", required=True)
    show.set_defaults(func=lambda a: plan.inspect(a.job))
    confirm = commands.add_parser("confirm", help="record the version and the user's own words after explicit confirmation")
    confirm.add_argument("--job", required=True)
    confirm.add_argument("--revision", required=True)
    confirm.add_argument("--evidence", required=True, help="the user's own words confirming this version; never written by the agent")
    confirm.set_defaults(func=lambda a: plan.confirm(a.job, a.revision, a.evidence))
    handoff = commands.add_parser("handoff", help="check confirmation and spec consistency before the automatic phase")
    handoff.add_argument("--job", required=True)
    handoff.set_defaults(func=lambda a: plan.handoff(a.job))
