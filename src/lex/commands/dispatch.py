from __future__ import annotations


def attach(subparsers, *, ctx) -> None:
    dispatch_parser = subparsers.add_parser("dispatch")
    dispatch_sub = dispatch_parser.add_subparsers(dest="dispatch_command", required=True)

    dispatch_create = dispatch_sub.add_parser("create")
    dispatch_create.add_argument("--task-id", type=int)
    dispatch_create.add_argument("--from", dest="from_agent", required=True)
    dispatch_create.add_argument("--to-worker", required=True)
    dispatch_create.add_argument("--summary", required=True)
    dispatch_create.add_argument("--body", required=True)
    dispatch_create.add_argument("--artifact", action="append")
    dispatch_create.add_argument("--metadata-json", default="{}")
    dispatch_create.add_argument("--sensitive-action")
    dispatch_create.add_argument("--require-approval", action="store_true")
    dispatch_create.add_argument("--approved-by")
    dispatch_create.set_defaults(func=ctx.cmd_dispatch_create)

    dispatch_list = dispatch_sub.add_parser("list")
    dispatch_list.add_argument("--json", action="store_true")
    dispatch_list.set_defaults(func=ctx.cmd_dispatch_list)

    dispatch_approve = dispatch_sub.add_parser("approve")
    dispatch_approve.add_argument("packet_id", type=int)
    dispatch_approve.add_argument("decision", choices=["approved", "rejected"])
    dispatch_approve.add_argument("--approved-by", required=True)
    dispatch_approve.set_defaults(func=ctx.cmd_dispatch_approve)

    dispatch_send = dispatch_sub.add_parser("send")
    dispatch_send.add_argument("packet_id", type=int)
    dispatch_send.add_argument("--runtime-id", type=int)
    dispatch_send.set_defaults(func=ctx.cmd_dispatch_send)

    dispatch_ack = dispatch_sub.add_parser("ack")
    dispatch_ack.add_argument("packet_id", type=int)
    dispatch_ack.add_argument("--runtime-id", type=int)
    dispatch_ack.add_argument("--note")
    dispatch_ack.set_defaults(func=ctx.cmd_dispatch_ack)

    dispatch_complete = dispatch_sub.add_parser("complete")
    dispatch_complete.add_argument("packet_id", type=int)
    dispatch_complete.add_argument("status", choices=["completed", "failed", "cancelled"])
    dispatch_complete.add_argument("--note")
    dispatch_complete.set_defaults(func=ctx.cmd_dispatch_complete)
