from __future__ import annotations


def attach(subparsers, *, ctx) -> None:
    agent_parser = subparsers.add_parser("agent")
    agent_sub = agent_parser.add_subparsers(dest="agent_command", required=True)

    agent_identify = agent_sub.add_parser("identify")
    agent_identify.add_argument("kind", choices=["codex", "claude", "cursor", "gemini", "ci", "automated"])
    agent_identify.add_argument("--name")
    agent_identify.add_argument("--role")
    agent_identify.add_argument("--specialty")
    agent_identify.add_argument("--json", action="store_true")
    agent_identify.set_defaults(func=ctx.cmd_agent_identify)

    agent_register = agent_sub.add_parser("register")
    agent_register.add_argument("name")
    agent_register.add_argument("kind", choices=["codex", "claude", "cursor", "gemini", "ci", "automated"])
    agent_register.add_argument("--role")
    agent_register.add_argument("--specialty")
    agent_register.set_defaults(func=ctx.cmd_agent_register)

    agent_role = agent_sub.add_parser("role")
    agent_role.add_argument("agent")
    agent_role.add_argument("role")
    agent_role.add_argument("--specialty")
    agent_role.set_defaults(func=ctx.cmd_agent_update_role)

    agent_list = agent_sub.add_parser("list")
    agent_list.add_argument("--json", action="store_true")
    agent_list.set_defaults(func=ctx.cmd_agent_list)

    agent_preflight = agent_sub.add_parser("preflight")
    agent_preflight.add_argument("--json", action="store_true")
    agent_preflight.set_defaults(func=ctx.cmd_agent_preflight)

    agent_retire = agent_sub.add_parser("retire")
    agent_retire.add_argument("agent")
    agent_retire.add_argument("--by", help="agent performing the retirement (for audit)")
    agent_retire.add_argument("--force", action="store_true", help="retire even if agent owns active tasks")
    agent_retire.set_defaults(func=ctx.cmd_agent_retire)

    specialty_parser = subparsers.add_parser("specialty")
    specialty_sub = specialty_parser.add_subparsers(dest="specialty_command", required=True)

    specialty_add = specialty_sub.add_parser("add")
    specialty_add.add_argument("name")
    specialty_add.set_defaults(func=ctx.cmd_specialty_add)

    specialty_list = specialty_sub.add_parser("list")
    specialty_list.add_argument("--json", action="store_true")
    specialty_list.set_defaults(func=ctx.cmd_specialty_list)
