from __future__ import annotations


def attach(subparsers, *, ctx) -> None:
    worker_parser = subparsers.add_parser("worker")
    worker_sub = worker_parser.add_subparsers(dest="worker_command", required=True)

    worker_register = worker_sub.add_parser("register")
    worker_register.add_argument("name")
    worker_register.add_argument("kind", choices=["codex", "claude", "cursor", "gemini", "ci", "automated"])
    worker_register.add_argument("--role")
    worker_register.add_argument("--specialty")
    worker_register.add_argument("--command-json", required=True)
    worker_register.add_argument("--env-json", default="{}")
    worker_register.add_argument("--cwd")
    worker_register.add_argument("--approval-policy", choices=list(ctx.VALID_WORKER_APPROVAL_POLICIES), default="always")
    worker_register.add_argument("--created-by")
    worker_register.set_defaults(func=ctx.cmd_worker_register)

    worker_list = worker_sub.add_parser("list")
    worker_list.add_argument("--json", action="store_true")
    worker_list.set_defaults(func=ctx.cmd_worker_list)

    worker_runtime_list = worker_sub.add_parser("runtime-list")
    worker_runtime_list.add_argument("--json", action="store_true")
    worker_runtime_list.set_defaults(func=ctx.cmd_worker_runtime_list)

    worker_cleanup = worker_sub.add_parser("cleanup")
    worker_cleanup.add_argument("--stale-minutes", type=int, default=ctx.WORKER_RUNTIME_STALE_MINUTES)
    worker_cleanup.add_argument("--json", action="store_true")
    worker_cleanup.set_defaults(func=ctx.cmd_worker_cleanup)

    worker_request = worker_sub.add_parser("request-start")
    worker_request.add_argument("worker")
    worker_request.add_argument("--requested-by", required=True)
    worker_request.add_argument("--task-id", type=int)
    worker_request.add_argument("--reason")
    worker_request.add_argument("--sensitive-action")
    worker_request.add_argument("--cwd")
    worker_request.add_argument("--approved-by")
    worker_request.set_defaults(func=ctx.cmd_worker_request_start)

    worker_approve = worker_sub.add_parser("approve")
    worker_approve.add_argument("runtime_id", type=int)
    worker_approve.add_argument("decision", choices=["approved", "rejected"])
    worker_approve.add_argument("--approved-by", required=True)
    worker_approve.set_defaults(func=ctx.cmd_worker_approve)

    worker_start = worker_sub.add_parser("start")
    worker_start.add_argument("runtime_id", type=int)
    worker_start.set_defaults(func=ctx.cmd_worker_start)

    worker_stop = worker_sub.add_parser("stop")
    worker_stop.add_argument("runtime_id", type=int)
    worker_stop.add_argument("--signal", choices=sorted(ctx.VALID_RUNTIME_STOP_SIGNALS), default="TERM")
    worker_stop.set_defaults(func=ctx.cmd_worker_stop)
