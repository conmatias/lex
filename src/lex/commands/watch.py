from __future__ import annotations


def attach(subparsers, *, ctx) -> None:
    watch_parser = subparsers.add_parser("watch")
    watch_sub = watch_parser.add_subparsers(dest="watch_command", required=True)

    watch_add = watch_sub.add_parser("add")
    watch_add.add_argument("agent")
    watch_add.add_argument("task_id", type=int)
    watch_add.add_argument("--force-role-override", action="store_true")
    watch_add.set_defaults(func=ctx.cmd_watch_add)

    watch_list = watch_sub.add_parser("list")
    watch_list.add_argument("--agent")
    watch_list.add_argument("--json", action="store_true")
    watch_list.set_defaults(func=ctx.cmd_watch_list)

    watch_ack = watch_sub.add_parser("ack")
    watch_ack.add_argument("agent")
    watch_ack.add_argument("task_id", type=int)
    watch_ack.add_argument("--event-id", type=int)
    watch_ack.set_defaults(func=ctx.cmd_watch_ack)
