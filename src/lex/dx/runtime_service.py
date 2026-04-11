from __future__ import annotations

import json
import socket
from pathlib import Path

from lex.dx.daemon import default_socket_path, start_daemon
from lex.dx.pty_runtime import PTYManager, TerminalSession


def _readline(sock: socket.socket) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    data = b"".join(chunks)
    if not data:
        return b""
    return data.split(b"\n", 1)[0]


def _session_from_payload(payload: dict) -> TerminalSession:
    return TerminalSession(
        id=int(payload["id"]),
        title=str(payload["title"]),
        kind=str(payload["kind"]),
        cwd=Path(payload["cwd"]),
        pid=payload.get("pid"),
        status=str(payload.get("status", "running")),
        output=[str(line) for line in payload.get("screen_lines", [])],
        unread_count=int(payload.get("unread_count", 0)),
        attention_flag=bool(payload.get("attention_flag", False)),
        display_state=str(payload.get("display_state", "expanded")),
        rows=int(payload.get("rows", 24)),
        cols=int(payload.get("cols", 80)),
    )


class LocalRuntimeClient:
    def __init__(self, manager: PTYManager):
        self.manager = manager

    def list_sessions(self) -> list[TerminalSession]:
        return self.manager.list_sessions()

    def spawn(self, kind: str, cmd: str, *, cwd: Path, lex_root: Path, lex_session_id: str | None = None, rows: int = 24, cols: int = 80) -> int:
        try:
            return self.manager.spawn(
                kind,
                cmd,
                cwd=cwd,
                lex_root=lex_root,
                lex_session_id=lex_session_id,
                rows=rows,
                cols=cols,
            )
        except TypeError:
            # Compatibility path for test doubles and older PTYManager signatures.
            return self.manager.spawn(
                kind,
                cmd,
                cwd=cwd,
                lex_root=lex_root,
                lex_session_id=lex_session_id,
            )

    def get_session(self, session_id: int) -> TerminalSession | None:
        return self.manager.get_session(session_id)

    def write(self, session_id: int, text: str) -> None:
        self.manager.write(session_id, text)

    def resize(self, session_id: int, rows: int, cols: int) -> None:
        self.manager.resize(session_id, rows, cols)

    def set_display_state(self, session_id: int, state: str) -> None:
        self.manager.set_display_state(session_id, state)

    def close(self, session_id: int) -> None:
        self.manager.close(session_id)

    def stop(self) -> None:
        self.manager.stop()


class DaemonRuntimeClient:
    def __init__(self, root: Path, *, socket_path: Path | None = None, autostart: bool = True):
        self.root = root
        self.socket_path = socket_path or default_socket_path(root)
        if autostart:
            start_daemon(self.root, socket_path=self.socket_path)

    def _rpc(self, payload: dict) -> dict:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            sock.connect(str(self.socket_path))
            sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            raw = _readline(sock)
            if not raw:
                raise RuntimeError("dx daemon returned an empty response")
            response = json.loads(raw.decode("utf-8"))
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "dx daemon request failed"))
        return response.get("payload", {})

    def list_sessions(self) -> list[TerminalSession]:
        payload = self._rpc({"op": "list"})
        return [_session_from_payload(item) for item in payload.get("sessions", [])]

    def spawn(self, kind: str, cmd: str, *, cwd: Path, lex_root: Path, lex_session_id: str | None = None, rows: int = 24, cols: int = 80) -> int:
        payload = self._rpc(
            {
                "op": "spawn",
                "kind": kind,
                "cmd": cmd,
                "cwd": str(cwd),
                "lex_root": str(lex_root),
                "lex_session_id": lex_session_id,
                "rows": rows,
                "cols": cols,
            }
        )
        return int(payload["session_id"])

    def get_session(self, session_id: int) -> TerminalSession | None:
        for session in self.list_sessions():
            if session.id == session_id:
                return session
        return None

    def write(self, session_id: int, text: str) -> None:
        self._rpc({"op": "write", "session_id": session_id, "text": text})

    def resize(self, session_id: int, rows: int, cols: int) -> None:
        self._rpc({"op": "resize", "session_id": session_id, "rows": rows, "cols": cols})

    def set_display_state(self, session_id: int, state: str) -> None:
        self._rpc({"op": "set_display_state", "session_id": session_id, "state": state})

    def close(self, session_id: int) -> None:
        self._rpc({"op": "close", "session_id": session_id})

    def stop(self) -> None:
        # Shell/TUI detach should not tear down the daemon.
        return
