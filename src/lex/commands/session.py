from __future__ import annotations


def attach(subparsers, *, ctx) -> None:
    session_parser = subparsers.add_parser("session")
    session_sub = session_parser.add_subparsers(dest="session_command", required=True)

    session_start = session_sub.add_parser("start")
    session_start.add_argument("agent")
    session_start.add_argument("--label")
    session_start.add_argument("--cwd", default=".")
    session_start.add_argument("--capability", action="append")
    session_start.add_argument("--fingerprint")
    session_start.add_argument("--fingerprint-label")
    session_start.set_defaults(func=ctx.cmd_session_start)

    session_heartbeat = session_sub.add_parser("heartbeat")
    session_heartbeat.add_argument("session_id", type=int)
    session_heartbeat.set_defaults(func=ctx.cmd_session_heartbeat)

    session_end = session_sub.add_parser("end")
    session_end.add_argument("session_id", type=int)
    session_end.set_defaults(func=ctx.cmd_session_end)

    session_bootstrap_show = session_sub.add_parser("bootstrap-show")
    session_bootstrap_show.add_argument("session_id", type=int)
    session_bootstrap_show.add_argument("--json", action="store_true")
    session_bootstrap_show.set_defaults(func=ctx.cmd_session_bootstrap_show)

    session_bootstrap_ack = session_sub.add_parser("bootstrap-ack")
    session_bootstrap_ack.add_argument("session_id", type=int)
    session_bootstrap_ack.add_argument("--by", required=True)
    session_bootstrap_ack.set_defaults(func=ctx.cmd_session_bootstrap_ack)

    session_action = session_sub.add_parser("action")
    session_action.add_argument("session_id", type=int)
    session_action.add_argument("action_key")
    session_action.add_argument("--note")
    session_action.set_defaults(func=ctx.cmd_session_action_complete)

    session_list = session_sub.add_parser("list")
    session_list.add_argument("--active-only", action="store_true")
    session_list.add_argument("--json", action="store_true")
    session_list.set_defaults(func=ctx.cmd_session_list)
