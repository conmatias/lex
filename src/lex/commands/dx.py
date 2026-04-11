from __future__ import annotations


def attach(subparsers, *, ctx) -> None:
    dx_parser = subparsers.add_parser("dx")
    dx_parser.set_defaults(func=ctx.cmd_dx)
