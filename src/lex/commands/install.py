from __future__ import annotations


def attach(subparsers, *, ctx) -> None:
    init_parser = subparsers.add_parser("init")
    init_parser.set_defaults(func=ctx.cmd_init)

    discovery_parser = subparsers.add_parser("discovery")
    discovery_sub = discovery_parser.add_subparsers(dest="discovery_command", required=True)

    discovery_list = discovery_sub.add_parser("list")
    discovery_list.add_argument("--timeout", type=float, default=2.0)
    discovery_list.set_defaults(func=ctx.cmd_discover)

    discovery_announce = discovery_sub.add_parser("announce")
    discovery_announce.add_argument("--session-id", type=int)
    discovery_announce.add_argument("--interval", type=int, default=5)
    discovery_announce.set_defaults(func=ctx.cmd_discovery_announce)

    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--agent-files", choices=["preserve", "merge", "assisted", "overwrite"], default="merge")
    install_parser.add_argument("--ignore-policy", choices=["none", "runtime", "all"], default="runtime")
    install_parser.add_argument("--ignore-target", choices=["gitignore", "local-exclude"], default="gitignore")
    install_parser.add_argument("--assisted-agent", choices=["codex", "claude", "gemini", "manual"], default="codex")
    install_parser.add_argument("--non-interactive", action="store_true")
    install_parser.set_defaults(func=ctx.cmd_install)

    merge_parser = subparsers.add_parser("merge")
    merge_sub = merge_parser.add_subparsers(dest="merge_command", required=True)

    merge_plan = merge_sub.add_parser("plan")
    merge_plan.add_argument("--agent", choices=["codex", "claude", "gemini", "manual"], default="codex")
    merge_plan.set_defaults(func=ctx.cmd_merge_plan)

    merge_diff = merge_sub.add_parser("diff")
    merge_diff.set_defaults(func=ctx.cmd_merge_diff)

    merge_apply = merge_sub.add_parser("apply")
    merge_apply.set_defaults(func=ctx.cmd_merge_apply)
