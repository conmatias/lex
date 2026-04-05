from __future__ import annotations

import os
import pty
import select
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class TerminalSession:
    id: int
    title: str
    kind: str
    cwd: Path
    pid: int | None = None
    proc: subprocess.Popen | None = None
    master_fd: int | None = None
    status: str = "starting"
    output: list[str] = field(default_factory=list)
    unread_count: int = 0
    attention_flag: bool = False
    display_state: str = "expanded"  # "collapsed", "expanded"
    active_task_id: int | None = None
    agent_id: int | None = None


class PTYManager:
    def __init__(self):
        self.sessions: dict[int, TerminalSession] = {}
        self._next_id = 1
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._io_loop, daemon=True)
        self._thread.start()

    def spawn(
        self,
        kind: str,
        cmd: str,
        cwd: Path | None = None,
        lex_root: Path | None = None,
        lex_session_id: str | None = None,
    ) -> int:
        cwd = cwd or Path.cwd()
        master_fd, slave_fd = pty.openpty()

        # Set non-blocking
        os.set_blocking(master_fd, False)

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        if lex_root is not None:
            env["LEX_ROOT"] = str(lex_root)
        if lex_session_id is not None:
            env["LEX_SESSION_ID"] = lex_session_id

        proc = subprocess.Popen(
            shlex.split(cmd),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=cwd,
            env=env,
            close_fds=True,
            start_new_session=True
        )

        # Close slave_fd in parent
        os.close(slave_fd)

        new_id = self._next_id
        self._next_id += 1

        session = TerminalSession(
            id=new_id,
            title=f"{kind}-{new_id}",
            kind=kind,
            cwd=cwd,
            pid=proc.pid,
            proc=proc,
            master_fd=master_fd,
            status="running"
        )

        with self._lock:
            self.sessions[new_id] = session

        return new_id

    def write(self, session_id: int, text: str):
        with self._lock:
            session = self.sessions.get(session_id)
            if not session or session.master_fd is None:
                return
            
            if not text.endswith("\n"):
                text += "\n"
            
            try:
                os.write(session.master_fd, text.encode("utf-8"))
            except OSError:
                pass

    def _io_loop(self):
        while not self._stop_event.is_set():
            with self._lock:
                # Check for process exits
                for sid, session in list(self.sessions.items()):
                    if session.status == "running" and session.proc and session.proc.poll() is not None:
                        # Process exited, but we might still have data to read
                        # We'll mark it as exited once we hit EOF or OSError
                        pass

                fds = {s.master_fd: sid for sid, s in self.sessions.items() if s.master_fd is not None}
            
            if not fds:
                time.sleep(0.1)
                continue
            
            try:
                readable, _, _ = select.select(list(fds.keys()), [], [], 0.1)
            except (OSError, ValueError):
                # A fd was closed between building the snapshot and calling
                # select (e.g. close() or stop() ran concurrently). Skip this
                # iteration; the next pass will build a fresh fd list.
                continue

            for fd in readable:
                session_id = fds[fd]
                try:
                    data = os.read(fd, 4096).decode("utf-8", errors="replace")
                    if data:
                        self._handle_output(session_id, data)
                    else:
                        # Empty read usually means EOF
                        self._handle_exit(session_id)
                except OSError:
                    # Session likely closed
                    self._handle_exit(session_id)

    def _handle_output(self, session_id: int, data: str):
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return
            
            # Simple line-based buffering for the prototype
            # Use splitlines(keepends=True) to preserve formatting better if needed,
            # but for now we'll stick to a simple list of lines.
            new_lines = data.splitlines()
            session.output.extend(new_lines)
            
            # Limit scrollback
            if len(session.output) > 10000:
                session.output = session.output[-10000:]
            
            if session.display_state == "collapsed":
                session.unread_count += len(new_lines)
            
            # Basic pattern matching for attention
            if any(p in data for p in ["(y/n)", "> ", "input:", "? "]):
                session.attention_flag = True

    def _handle_exit(self, session_id: int):
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return
            session.status = "exited"
            if session.master_fd is not None:
                try:
                    os.close(session.master_fd)
                except OSError:
                    pass
                session.master_fd = None

    def get_session(self, session_id: int) -> TerminalSession | None:
        with self._lock:
            return self.sessions.get(session_id)

    def close(self, session_id: int):
        """Terminate a session and its associated process."""
        with self._lock:
            session = self.sessions.get(session_id)
        if not session:
            return
        self._terminate_session(session)
        with self._lock:
            if session_id in self.sessions:
                del self.sessions[session_id]

    def list_sessions(self) -> list[TerminalSession]:
        """Return a snapshot of all active sessions."""
        with self._lock:
            return list(self.sessions.values())

    def resize(self, session_id: int, rows: int, cols: int) -> None:
        """Send SIGWINCH with new terminal dimensions to the session process."""
        import fcntl
        import struct
        import termios

        with self._lock:
            session = self.sessions.get(session_id)
            if not session or session.master_fd is None:
                return
            fd = session.master_fd

        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass

    def set_display_state(self, session_id: int, state: str):
        """Update display state (e.g. 'expanded', 'collapsed')."""
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return
            session.display_state = state
            if state == "expanded":
                session.unread_count = 0
                session.attention_flag = False

    def stop(self):
        self._stop_event.set()
        with self._lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            self._terminate_session(session)
        self._thread.join(timeout=1.0)
        with self._lock:
            self.sessions.clear()

    def _terminate_session(self, session: TerminalSession) -> None:
        # Terminate the process outside the lock — proc.wait() can take time.
        if session.proc and session.proc.poll() is None:
            try:
                session.proc.terminate()
                session.proc.wait(timeout=1.0)
            except Exception:
                try:
                    session.proc.kill()
                    session.proc.wait(timeout=1.0)
                except Exception:
                    pass
        # Close the master fd under the lock to avoid racing with _handle_exit,
        # which also mutates master_fd under the lock. Without this, two callers
        # could both see master_fd != None and attempt os.close() on the same fd,
        # potentially closing a fd that was reallocated to a different file.
        with self._lock:
            if session.master_fd is not None:
                try:
                    os.close(session.master_fd)
                except OSError:
                    pass
                session.master_fd = None
            session.status = "exited"
