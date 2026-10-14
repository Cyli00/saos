"""需求访谈与设计计划命令；不连接 OpticStudio。"""

from pathlib import Path

from saos import interview, plan


def register(sub, parser_class) -> None:
    group = sub.add_parser("interview", help="检查对话整理出的用户需求与规格来源")
    commands = group.add_subparsers(dest="action", required=True, parser_class=parser_class)
    check = commands.add_parser("check", help="列出缺项与补全假设，不启动 Zemax")
    check.add_argument("file", help="访谈记录 YAML")
    check.set_defaults(func=lambda a: interview.check(interview.read_draft(Path(a.file))))

    group = sub.add_parser("plan", help="生成、审阅并记录设计计划确认")
    commands = group.add_subparsers(dest="action", required=True, parser_class=parser_class)
    create = commands.add_parser("create", help="根据访谈记录生成单文件 HTML 设计计划")
    create.add_argument("--job", required=True)
    create.add_argument("--interview", required=True)
    create.add_argument("--entry", help="已收录的初始结构；省略时生成等待结构的计划")
    create.add_argument("--rationale", default="", help="选择理由与仍需调整之处")
    create.set_defaults(func=lambda a: plan.create(a.job, Path(a.interview), a.entry, a.rationale))
    show = commands.add_parser("show", help="核对当前计划完整性并查看确认状态")
    show.add_argument("--job", required=True)
    show.set_defaults(func=lambda a: plan.inspect(a.job))
    confirm = commands.add_parser("confirm", help="用户在对话中明确确认后，记录该版本及确认原话")
    confirm.add_argument("--job", required=True)
    confirm.add_argument("--revision", required=True)
    confirm.add_argument("--evidence", required=True, help="用户确认当前版本的原话；不能由 agent 自行代答")
    confirm.set_defaults(func=lambda a: plan.confirm(a.job, a.revision, a.evidence))
    handoff = commands.add_parser("handoff", help="检查确认及规格一致性，交接给自动设计阶段")
    handoff.add_argument("--job", required=True)
    handoff.set_defaults(func=lambda a: plan.handoff(a.job))
