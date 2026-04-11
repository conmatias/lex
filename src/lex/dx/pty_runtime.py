from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import shlex
import struct
import subprocess
import termios
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyte

class DxScreen(pyte.Screen):
    """Compat wrapper around pyte.Screen for parser/API mismatches.

    Some pyte releases route certain CSI sequences through handlers with a
    ``private=...`` keyword even when the base Screen method only accepts
    positional parameters. Accept and ignore that flag so a malformed or
    nonstandard sequence cannot crash the PTY reader thread.
    """

    def select_graphic_rendition(self, *attrs: int, private: bool = False) -> None:
        del private
        return super().select_graphic_rendition(*attrs)


# Matches: OSC sequences (ESC ] ... BEL), CSI sequences (ESC [ ... letter),
# and 2-char Fe sequences (ESC @-_).  OSC must come before the Fe catch-all
# because ']' (0x5D) falls inside the [@-_] range.  Used to strip VT/ANSI
# codes from the scrollback log and for attention pattern matching on the
# decoded text path.
_ANSI_ESC = re.compile(
    r"\x1b(?:"
    r"\][^\x07\x1b]*\x07"    # OSC  — ESC ] ... BEL  (must precede Fe catch-all)
    r"|\[[0-?]*[ -/]*[@-~]"  # CSI  — ESC [ ... final-byte
    r"|[@-Z\\-_]"             # Fe   — ESC + one byte 0x40-0x5F
    r")"
)


def strip_ansi(text: str) -> str:
    """Remove ANSI/VT escape sequences, returning plain text."""
    return _ANSI_ESC.sub("", text)


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
    # Scrollback log — plain text lines accumulated over the session lifetime.
    output: list[str] = field(default_factory=list)
    unread_count: int = 0
    attention_flag: bool = False
    display_state: str = "expanded"  # "collapsed", "expanded"
    active_task_id: int | None = None
    agent_id: int | None = None
    rows: int = 24
    cols: int = 80
    # VT screen buffer (set by PTYManager.spawn; None for manually-constructed
    # sessions such as those created by FakePTYManager in tests).
    # Not part of the dataclass __init__ — set as plain instance attributes.

    def screen_lines(self) -> list[str]:
        """Current VT screen content as plain-text lines (trailing spaces stripped).

        Returns the pyte screen's display if available, otherwise falls back
        to the scrollback tail sized to self.rows.
        """
        screen: pyte.Screen | None = getattr(self, "_screen", None)
        if screen is not None:
            return [line.rstrip() for line in screen.display]
        return self.output[-self.rows:]


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
        rows: int = 24,
        cols: int = 80,
    ) -> int:
        cwd = cwd or Path.cwd()
        master_fd, slave_fd = pty.openpty()

        # Set initial window size before the child process starts so it sees
        # the correct dimensions from the very first TIOCGWINSZ call.
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(master_fd, termios.TIOCSWINSZ, winsize)
        except OSError:
            pass  # best-effort; resize() can correct it later

        # Set non-blocking
        os.set_blocking(master_fd, False)

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        if lex_root is not None:
            env["LEX_ROOT"] = str(lex_root)
        if lex_session_id is not None:
            env["LEX_SESSION_ID"] = lex_session_id

        try:
            proc = subprocess.Popen(
                shlex.split(cmd),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=cwd,
                env=env,
                close_fds=True,
                start_new_session=True,
            )
        except Exception:
            # Popen failed before the session was registered. Neither fd has
            # been handed off anywhere, so we must close both here. If we only
            # closed slave_fd and re-raised, master_fd would leak permanently
            # because stop() never sees unregistered fds.
            os.close(master_fd)
            os.close(slave_fd)
            raise

        # Close slave_fd in parent (only reached on Popen success)
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
            status="running",
            rows=rows,
            cols=cols,
        )

        # Attach a pyte VT screen buffer. All VT control sequences (cursor
        # movement, in-place rewrites, color) are handled here so screen_lines()
        # always returns the current rendered state of the terminal.
        screen = DxScreen(cols, rows)
        stream = pyte.ByteStream(screen)
        session._screen = screen  # type: ignore[attr-defined]
        session._stream = stream  # type: ignore[attr-defined]

        with self._lock:
            self.sessions[new_id] = session

        return new_id

    def write(self, session_id: int, text: str):
        with self._lock:
            session = self.sessions.get(session_id)
            if not session or session.master_fd is None:
                return

            # Don't send bare enters for empty messages.
            if not text:
                return

            # PTY-backed coding CLIs generally expect Enter/submit semantics
            # rather than a literal linefeed. Preserve any explicit caller
            # terminator, otherwise send carriage return as the default submit.
            if not text.endswith(("\r", "\n")):
                text += "\r"

            try:
                os.write(session.master_fd, text.encode("utf-8"))
            except OSError:
                pass

    def _io_loop(self):
        while not self._stop_event.is_set():
            with self._lock:
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
                    raw = os.read(fd, 4096)
                    if raw:
                        self._handle_output(session_id, raw)
                    else:
                        # Empty read usually means EOF
                        self._handle_exit(session_id)
                except OSError:
                    # Session likely closed
                    self._handle_exit(session_id)

    def _handle_output(self, session_id: int, data: bytes):
        with self._lock:
            session = self.sessions.get(session_id)
            if not session:
                return

            # Feed raw bytes to the VT screen buffer first so cursor-addressing
            # sequences, in-place rewrites, and color attributes are applied
            # correctly before anything tries to read screen.display.
            stream: pyte.ByteStream | None = getattr(session, "_stream", None)
            if stream is not None:
                stream.feed(data)

            # Decode for scrollback log and attention detection.
            text = data.decode("utf-8", errors="replace")

            # Attention detection runs on raw text (before stripping) so
            # prompts wrapped in color codes are still detected.
            if any(p in text for p in ["(y/n)", "> ", "input:", "? "]):
                session.attention_flag = True

            # Scrollback: strip escape sequences and append non-blank lines.
            # This is intentionally separate from the VT screen — it accumulates
            # history even as the screen overwrites its own lines.
            clean = strip_ansi(text)
            new_lines = [line for line in clean.splitlines() if line.strip()]
            if new_lines:
                session.output.extend(new_lines)
                if len(session.output) > 10000:
                    session.output = session.output[-10000:]
                if session.display_state == "collapsed":
                    session.unread_count += len(new_lines)

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
        """Update terminal window size and send SIGWINCH to the session process."""
        with self._lock:
            session = self.sessions.get(session_id)
            if not session or session.master_fd is None:
                return
            fd = session.master_fd

        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
            with self._lock:
                session = self.sessions.get(session_id)
                if session:
                    session.rows = rows
                    session.cols = cols
                    screen: pyte.Screen | None = getattr(session, "_screen", None)
                    if screen is not None:
                        screen.resize(rows, cols)
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
        self._thread.join(timeout=2.0)
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
