from __future__ import annotations


def attach(subparsers, *, ctx) -> None:
    event_parser = subparsers.add_parser("event")
    event_sub = event_parser.add_subparsers(dest="event_command", required=True)

    event_list = event_sub.add_parser("list")
    event_list.add_argument("--task", dest="task_id", type=int)
    event_list.add_argument("--agent")
    event_list.add_argument("--limit", type=int, default=20)
    event_list.add_argument("--json", action="store_true")
    ctx.add_follow_arguments(event_list)
    event_list.set_defaults(func=ctx.cmd_event_list)
