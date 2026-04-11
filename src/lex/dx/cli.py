from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from lex.dx.daemon import daemon_running, default_socket_path, start_daemon, stop_daemon
from lex.dx.runtime_service import DaemonRuntimeClient
from lex.dx.shell import run_shell


def _default_runtime_command(kind: str) -> str:
    if kind == "shell":
        return os.environ.get("SHELL", "bash")
    binary = shutil.which(kind)
    if binary is None:
        raise SystemExit(
            f"Cannot spawn '{kind}' runtime: '{kind}' binary not found in PATH."
        )
    return binary


def _resolve_session_ref(client: DaemonRuntimeClient, ref: str) -> tuple[int, str]:
    sessions = client.list_sessions()
    exact = [s for s in sessions if s.title == ref]
    if exact:
        return exact[0].id, exact[0].title
    prefixed = [s for s in sessions if s.title.startswith(ref)]
    if not prefixed:
        raise SystemExit(f"unknown session: {ref}")
    if len(prefixed) > 1:
        matches = ", ".join(s.title for s in prefixed)
        raise SystemExit(f"ambiguous session {ref}: {matches}")
    return prefixed[0].id, prefixed[0].title


def _cmd_daemon(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    socket_path = default_socket_path(root)
    if args.daemon_command == "start":
        start_daemon(root, socket_path=socket_path)
        print("dx daemon started")
        return
    if args.daemon_command == "stop":
        stopped = stop_daemon(root, socket_path=socket_path)
        print("dx daemon stopped" if stopped else "dx daemon not running")
        return
    print("running" if daemon_running(socket_path) else "stopped")


def _cmd_spawn(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    client = DaemonRuntimeClient(root, autostart=True)
    command = args.command or _default_runtime_command(args.runtime)
    session_id = client.spawn(
        args.runtime,
        command,
        cwd=root,
        lex_root=root,
    )
    session = client.get_session(session_id)
    title = session.title if session is not None else f"{args.runtime}-{session_id}"
    print(f"spawned {title}")


def _cmd_list(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    client = DaemonRuntimeClient(root, autostart=True)
    sessions = client.list_sessions()
    if not sessions:
        print("no active dx sessions")
        return
    for s in sessions:
        print(
            f"{s.title:<16} kind={s.kind:<7} status={s.status:<10} "
            f"pid={s.pid or '-':<8} unread={s.unread_count}"
        )


def _cmd_send(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    client = DaemonRuntimeClient(root, autostart=True)
    session_id, title = _resolve_session_ref(client, args.session)
    client.write(session_id, args.message)
    print(f"sent to {title}")


def _snapshot_lines(client: DaemonRuntimeClient, session_id: int, *, lines: int) -> list[str]:
    session = client.get_session(session_id)
    if session is None:
        return []
    snapshot = session.screen_lines()
    return snapshot[-max(lines, 1):]


def _cmd_tail(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    client = DaemonRuntimeClient(root, autostart=True)
    session_id, title = _resolve_session_ref(client, args.session)
    if not args.follow:
        for line in _snapshot_lines(client, session_id, lines=args.lines):
            print(line)
        return
    print(f"attaching to {title} (ctrl-c to detach)")
    last: list[str] = []
    try:
        while True:
            current = _snapshot_lines(client, session_id, lines=args.lines)
            if current != last:
                if current:
                    print("\n".join(current))
                    print("---")
                last = current
            time.sleep(0.25)
    except KeyboardInterrupt:
        print("detached")


def _cmd_stop(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    client = DaemonRuntimeClient(root, autostart=True)
    session_id, title = _resolve_session_ref(client, args.session)
    client.close(session_id)
    print(f"stopped {title}")


def _cmd_resume(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    client = DaemonRuntimeClient(root, autostart=True)
    session_id, title = _resolve_session_ref(client, args.session)
    session = client.get_session(session_id)
    if session is None:
        raise SystemExit(f"session disappeared: {title}")
    if session.status != "exited":
        print(f"{title} is already {session.status}")
        return
    command = _default_runtime_command(session.kind)
    new_id = client.spawn(
        session.kind,
        command,
        cwd=root,
        lex_root=root,
    )
    resumed = client.get_session(new_id)
    resumed_title = resumed.title if resumed else f"{session.kind}-{new_id}"
    print(f"resumed as {resumed_title}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dx", description="dx runtime control surface")
    parser.add_argument("--root", default=".", help="workspace root (default: cwd)")
    sub = parser.add_subparsers(dest="command")

    daemon_parser = sub.add_parser("daemon", help="manage dx daemon")
    daemon_sub = daemon_parser.add_subparsers(dest="daemon_command", required=True)
    daemon_sub.add_parser("start")
    daemon_sub.add_parser("stop")
    daemon_sub.add_parser("status")
    daemon_parser.set_defaults(func=_cmd_daemon)

    spawn = sub.add_parser("spawn", help="spawn runtime session")
    spawn.add_argument("runtime", choices=("claude", "codex", "gemini", "shell"))
    spawn.add_argument("--command", help="override runtime command")
    spawn.set_defaults(func=_cmd_spawn)

    list_parser = sub.add_parser("list", help="list runtime sessions")
    list_parser.set_defaults(func=_cmd_list)

    send = sub.add_parser("send", help="send message to runtime")
    send.add_argument("session", help="session id/title/prefix")
    send.add_argument("message")
    send.set_defaults(func=_cmd_send)

    tail = sub.add_parser("tail", help="print runtime screen snapshot")
    tail.add_argument("session", help="session id/title/prefix")
    tail.add_argument("--lines", type=int, default=20)
    tail.add_argument("--follow", action="store_true")
    tail.set_defaults(func=_cmd_tail)

    attach = sub.add_parser("attach", help="attach to runtime output (tail --follow)")
    attach.add_argument("session", help="session id/title/prefix")
    attach.add_argument("--lines", type=int, default=20)
    attach.set_defaults(func=_cmd_tail, follow=True)

    stop = sub.add_parser("stop", help="stop runtime session")
    stop.add_argument("session", help="session id/title/prefix")
    stop.set_defaults(func=_cmd_stop)

    resume = sub.add_parser("resume", help="resume exited runtime session")
    resume.add_argument("session", help="session id/title/prefix")
    resume.set_defaults(func=_cmd_resume)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        root = Path(args.root).resolve()
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            raise SystemExit("dx requires an interactive terminal — run it from a real TTY")
        run_shell(root)
        return
    args.func(args)
