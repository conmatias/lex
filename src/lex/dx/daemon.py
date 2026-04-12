from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

from lex.db import ensure_workspace
from lex.dx.pty_runtime import PTYManager, TerminalSession


def _runtime_dir(root: Path) -> Path:
    paths = ensure_workspace(root)
    runtime_dir = paths.lex_dir / "runtime" / "dx"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def default_socket_path(root: Path) -> Path:
    candidate = _runtime_dir(root) / "daemon.sock"
    if len(str(candidate)) < 90:
        return candidate
    digest = hashlib.sha1(str(root).encode("utf-8")).hexdigest()[:12]
    return Path("/tmp") / f"dxd-{digest}.sock"


def _default_log_paths(root: Path) -> tuple[Path, Path]:
    runtime_dir = _runtime_dir(root)
    return runtime_dir / "daemon.log", runtime_dir / "daemon.err.log"


def _readline(conn: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = conn.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    data = b"".join(chunks)
    if not data:
        return b""
    return data.split(b"\n", 1)[0]


def _session_to_dict(session: TerminalSession) -> dict:
    return {
        "id": session.id,
        "title": session.title,
        "kind": session.kind,
        "cwd": str(session.cwd),
        "pid": session.pid,
        "status": session.status,
        "unread_count": session.unread_count,
        "attention_flag": session.attention_flag,
        "display_state": session.display_state,
        "rows": session.rows,
        "cols": session.cols,
        "screen_lines": session.screen_lines(),
    }


class DxRuntimeDaemon:
    def __init__(self, root: Path, socket_path: Path):
        self.root = root
        self.socket_path = socket_path
        self.manager = PTYManager()
        self._stop = False
        self._server: socket.socket | None = None

    def _handle(self, request: dict) -> dict:
        op = request.get("op")
        if op == "ping":
            return {"ok": True, "payload": {"status": "ok"}}
        if op == "shutdown":
            self._stop = True
            return {"ok": True, "payload": {"status": "shutting_down"}}
        if op == "list":
            sessions = [_session_to_dict(s) for s in self.manager.list_sessions()]
            return {"ok": True, "payload": {"sessions": sessions}}
        if op == "spawn":
            session_id = self.manager.spawn(
                request["kind"],
                request["cmd"],
                cwd=Path(request["cwd"]),
                lex_root=Path(request["lex_root"]),
                lex_session_id=request.get("lex_session_id"),
                rows=int(request.get("rows", 24)),
                cols=int(request.get("cols", 80)),
            )
            return {"ok": True, "payload": {"session_id": session_id}}
        if op == "write":
            self.manager.write(int(request["session_id"]), request["text"])
            return {"ok": True, "payload": {}}
        if op == "resize":
            self.manager.resize(int(request["session_id"]), int(request["rows"]), int(request["cols"]))
            return {"ok": True, "payload": {}}
        if op == "set_display_state":
            self.manager.set_display_state(int(request["session_id"]), request["state"])
            return {"ok": True, "payload": {}}
        if op == "close":
            self.manager.close(int(request["session_id"]))
            return {"ok": True, "payload": {}}
        return {"ok": False, "error": f"unknown op: {op}"}

    def serve_forever(self) -> None:
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self.socket_path.exists():
            self.socket_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        server.listen(16)
        server.settimeout(0.2)
        self._server = server
        try:
            while not self._stop:
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                except OSError:
                    continue
                with conn:
                    try:
                        raw = _readline(conn)
                        if not raw:
                            continue
                        request = json.loads(raw.decode("utf-8"))
                        response = self._handle(request)
                    except Exception as exc:
                        response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                    conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
        finally:
            self.manager.stop()
            try:
                server.close()
            except OSError:
                pass
            if self.socket_path.exists():
                self.socket_path.unlink()


def daemon_running(socket_path: Path, *, timeout: float = 0.3) -> bool:
    if not socket_path.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(socket_path))
            sock.sendall(b'{"op":"ping"}\n')
            raw = _readline(sock)
            if not raw:
                return False
            response = json.loads(raw.decode("utf-8"))
            return bool(response.get("ok"))
    except OSError:
        return False


def start_daemon(root: Path, *, socket_path: Path | None = None, wait_timeout: float = 3.0) -> None:
    socket_path = socket_path or default_socket_path(root)
    if daemon_running(socket_path):
        return
    stdout_path, stderr_path = _default_log_paths(root)
    env = os.environ.copy()
    src_path = str(Path(__file__).resolve().parents[2])
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src_path if not current_pythonpath else f"{src_path}{os.pathsep}{current_pythonpath}"
    with stdout_path.open("ab") as out, stderr_path.open("ab") as err:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "lex.dx.daemon",
                "--root",
                str(root),
                "--socket",
                str(socket_path),
                "serve",
            ],
            cwd=str(root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
            close_fds=True,
        )
    deadline = time.time() + wait_timeout
    while time.time() < deadline:
        if daemon_running(socket_path):
            return
        time.sleep(0.05)
    hint = ""
    try:
        hint = stderr_path.read_text().strip()[-500:]
    except Exception:
        pass
    raise RuntimeError(
        f"dx runtime daemon failed to start (socket: {socket_path})"
        + (f"\ndaemon stderr: {hint}" if hint else "")
    )


def stop_daemon(root: Path, *, socket_path: Path | None = None) -> bool:
    socket_path = socket_path or default_socket_path(root)
    if not daemon_running(socket_path):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(str(socket_path))
            sock.sendall(b'{"op":"shutdown"}\n')
            _readline(sock)
    except OSError:
        return False
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lex.dx.daemon")
    parser.add_argument("--root", default=".")
    parser.add_argument("--socket", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve")
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("status")
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    socket_path = Path(args.socket) if args.socket else default_socket_path(root)
    if args.command == "serve":
        daemon = DxRuntimeDaemon(root, socket_path)
        daemon.serve_forever()
        return
    if args.command == "start":
        start_daemon(root, socket_path=socket_path)
        print("dx daemon started")
        return
    if args.command == "stop":
        stopped = stop_daemon(root, socket_path=socket_path)
        if stopped:
            print("dx daemon stopped")
        else:
            print("dx daemon not running")
        return
    if args.command == "status":
        print("running" if daemon_running(socket_path) else "stopped")
        return


if __name__ == "__main__":
    main()
