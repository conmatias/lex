from __future__ import annotations


def attach(subparsers, *, ctx) -> None:
    msg_parser = subparsers.add_parser("msg")
    msg_sub = msg_parser.add_subparsers(dest="msg_command", required=True)

    msg_send = msg_sub.add_parser("send")
    msg_send.add_argument("--task", dest="task_id", type=int)
    msg_send.add_argument("--from", dest="from_agent", required=True)
    msg_send.add_argument("--to", dest="to_agent")
    msg_send.add_argument("--type", required=True)
    msg_send.add_argument("--subject")
    msg_send.add_argument("--body", required=True)
    msg_send.set_defaults(func=ctx.cmd_msg_send)

    msg_inbox = msg_sub.add_parser("inbox")
    msg_inbox.add_argument("agent")
    msg_inbox.add_argument("--limit", type=int, default=20)
    msg_inbox.add_argument("--json", action="store_true")
    ctx.add_follow_arguments(msg_inbox)
    msg_inbox.set_defaults(func=ctx.cmd_msg_inbox)

    msg_task = msg_sub.add_parser("task")
    msg_task.add_argument("task_id", type=int)
    msg_task.add_argument("--limit", type=int, default=20)
    msg_task.add_argument("--json", action="store_true")
    ctx.add_follow_arguments(msg_task)
    msg_task.set_defaults(func=ctx.cmd_msg_task)
