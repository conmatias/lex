from __future__ import annotations


def attach(subparsers, *, ctx) -> None:
    prompt_parser = subparsers.add_parser("prompt")
    prompt_sub = prompt_parser.add_subparsers(dest="prompt_command", required=True)

    prompt_create = prompt_sub.add_parser("create")
    prompt_create.add_argument("--role", required=True)
    prompt_create.add_argument("--agent", default=None, help="agent name to hydrate with live state")
    prompt_create.add_argument("--json", action="store_true")
    prompt_create.set_defaults(func=ctx.cmd_prompt_create)
