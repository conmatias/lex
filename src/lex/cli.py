from __future__ import annotations

import argparse
import curses
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence

import lex.cli_legacy as legacy
from lex.db import close_connections_since, connection_checkpoint
from lex.tui import run_tui

CLI_COMMAND = legacy.CLI_COMMAND
def cmd_dx(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    discovery = None
    if getattr(args, "announce", False):
        paths = legacy.resolve_paths(root)
        conn = legacy.connect(paths.db_path)
        legacy.initialize_database(conn)
        session = conn.execute(
            "SELECT s.id, a.name AS agent_name, s.git_branch, s.git_base_ref "
            "FROM sessions s JOIN agents a ON a.id = s.agent_id "
            "WHERE s.status = 'active' AND s.ended_at IS NULL "
            "ORDER BY s.id DESC LIMIT 1"
        ).fetchone()
        if session:
            discovery = legacy.LexDiscovery(
                {
                    "agent_name": session["agent_name"],
                    "session_id": session["id"],
                    "git_branch": session["git_branch"],
                    "git_base_ref": session["git_base_ref"],
                    "root_path": str(root),
                }
            )
            discovery.start_announcing()

    try:
        legacy.run_dx(root)
    finally:
        if discovery:
            discovery.stop()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=CLI_COMMAND)
    parser.add_argument("--root", default=".", help="workspace root")
    subparsers = parser.add_subparsers(dest="command")

    from lex.commands import agent as agent_commands
    from lex.commands import dispatch as dispatch_commands
    from lex.commands import dx as dx_commands
    from lex.commands import event as event_commands
    from lex.commands import hook as hook_commands
    from lex.commands import install as install_commands
    from lex.commands import msg as msg_commands
    from lex.commands import prompt as prompt_commands
    from lex.commands import session as session_commands
    from lex.commands import task as task_commands
    from lex.commands import watch as watch_commands
    from lex.commands import worker as worker_commands

    ctx_data = dict(vars(legacy))
    ctx_data["cmd_dx"] = cmd_dx
    ctx = SimpleNamespace(**ctx_data)

    install_commands.attach(subparsers, ctx=ctx)
    agent_commands.attach(subparsers, ctx=ctx)
    session_commands.attach(subparsers, ctx=ctx)
    worker_commands.attach(subparsers, ctx=ctx)
    task_commands.attach(subparsers, ctx=ctx)
    msg_commands.attach(subparsers, ctx=ctx)
    watch_commands.attach(subparsers, ctx=ctx)
    dispatch_commands.attach(subparsers, ctx=ctx)
    prompt_commands.attach(subparsers, ctx=ctx)
    event_commands.attach(subparsers, ctx=ctx)
    dx_commands.attach(subparsers, ctx=ctx)
    hook_commands.attach(subparsers, ctx=ctx)

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    checkpoint = connection_checkpoint()
    try:
        if args.command is None:
            root = Path(args.root).resolve()
            if sys.stdin.isatty() and sys.stdout.isatty():
                try:
                    run_tui(root)
                except Exception as exc:
                    if os.environ.get("LEX_DEBUG_TUI") == "1":
                        raise
                    reason = f"{type(exc).__name__}: {exc}"
                    if isinstance(exc, curses.error):
                        print(
                            f"{CLI_COMMAND}: TUI unavailable, falling back to interactive shell ({reason})",
                            file=sys.stderr,
                        )
                    else:
                        print(
                            f"{CLI_COMMAND}: TUI failed to start, falling back to interactive shell ({reason})",
                            file=sys.stderr,
                        )
                    legacy.run_interactive_shell(root)
            else:
                legacy.run_interactive_shell(root)
            return
        args.func(args)
    finally:
        close_connections_since(checkpoint)


if __name__ == "__main__":
    main()
