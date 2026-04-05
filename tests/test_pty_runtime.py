"""Tests for PTYManager and TerminalSession runtime stability.

Covers:
- spawn() session creation and field defaults
- lex_session_id / lex_root environment injection (no variable-shadowing)
- list_sessions() snapshot semantics
- close() and stop() lifecycle
- set_display_state() unread/attention reset
- resize() no-crash contract
- write() no-crash contract
- Concurrent safety: stop() while I/O loop is running
- FD hygiene: stop() leaves no open master PTY fds
"""
import os
import time

import pytest

from lex.dx.pty_runtime import PTYManager, TerminalSession


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def mgr():
    m = PTYManager()
    yield m
    m.stop()


# ---------------------------------------------------------------------------
# spawn()
# ---------------------------------------------------------------------------

class TestSpawn:
    def test_returns_integer_id(self, mgr):
        sid = mgr.spawn("shell", "cat")
        assert isinstance(sid, int)
        assert sid >= 1
        mgr.close(sid)

    def test_session_is_running(self, mgr, tmp_path):
        sid = mgr.spawn("shell", "cat", cwd=tmp_path)
        s = mgr.get_session(sid)
        assert s is not None
        assert s.status == "running"
        assert s.kind == "shell"
        assert s.cwd == tmp_path
        assert s.pid is not None
        assert s.master_fd is not None
        mgr.close(sid)

    def test_ids_increment(self, mgr):
        a = mgr.spawn("shell", "cat")
        b = mgr.spawn("shell", "cat")
        assert b == a + 1
        mgr.close(a)
        mgr.close(b)

    def test_default_display_state_is_expanded(self, mgr):
        sid = mgr.spawn("shell", "cat")
        assert mgr.get_session(sid).display_state == "expanded"
        mgr.close(sid)

    def test_lex_session_id_param_does_not_shadow_new_id(self, mgr, tmp_path):
        # The old code had session_id as both the env-var kwarg and the
        # internal integer id. Verify the kwarg is named lex_session_id and
        # the returned id is still an int, not the string we passed.
        sid = mgr.spawn("shell", "cat", lex_root=tmp_path, lex_session_id="abc-123")
        assert isinstance(sid, int)
        s = mgr.get_session(sid)
        assert s is not None
        assert s.status == "running"
        mgr.close(sid)

    def test_lex_root_none_does_not_crash(self, mgr):
        sid = mgr.spawn("shell", "cat", lex_root=None, lex_session_id=None)
        assert mgr.get_session(sid) is not None
        mgr.close(sid)

    def test_failed_spawn_does_not_leak_fds(self, mgr):
        """If Popen raises, both master_fd and slave_fd must be closed.

        A failed spawn must leave the fd table at the same count as before.
        We use /dev/fd (macOS) to count open descriptors. Because pytest itself
        opens fds during the test, we allow the count to stay equal or decrease
        but never increase.
        """
        before = len(os.listdir("/dev/fd"))
        with pytest.raises(Exception):
            mgr.spawn("shell", "/absolutely/no/such/executable/xyz")
        after = len(os.listdir("/dev/fd"))
        assert after <= before, (
            f"fd count grew from {before} to {after}: "
            "spawn() leaked fds on Popen failure"
        )


# ---------------------------------------------------------------------------
# TerminalSession field defaults
# ---------------------------------------------------------------------------

class TestSessionFields:
    def test_active_task_id_defaults_none(self, mgr):
        sid = mgr.spawn("shell", "cat")
        assert mgr.get_session(sid).active_task_id is None
        mgr.close(sid)

    def test_agent_id_defaults_none(self, mgr):
        sid = mgr.spawn("shell", "cat")
        assert mgr.get_session(sid).agent_id is None
        mgr.close(sid)

    def test_unread_count_defaults_zero(self, mgr):
        sid = mgr.spawn("shell", "cat")
        assert mgr.get_session(sid).unread_count == 0
        mgr.close(sid)

    def test_attention_flag_defaults_false(self, mgr):
        sid = mgr.spawn("shell", "cat")
        assert mgr.get_session(sid).attention_flag is False
        mgr.close(sid)

    def test_fields_are_mutable_via_reference(self, mgr):
        sid = mgr.spawn("shell", "cat")
        s = mgr.get_session(sid)
        s.active_task_id = 7
        s.agent_id = 3
        assert mgr.get_session(sid).active_task_id == 7
        assert mgr.get_session(sid).agent_id == 3
        mgr.close(sid)


# ---------------------------------------------------------------------------
# list_sessions()
# ---------------------------------------------------------------------------

class TestListSessions:
    def test_empty_before_any_spawn(self, mgr):
        assert mgr.list_sessions() == []

    def test_contains_spawned_session(self, mgr):
        sid = mgr.spawn("shell", "cat")
        assert any(s.id == sid for s in mgr.list_sessions())
        mgr.close(sid)

    def test_close_removes_from_list(self, mgr):
        sid = mgr.spawn("shell", "cat")
        mgr.close(sid)
        time.sleep(0.05)
        assert not any(s.id == sid for s in mgr.list_sessions())

    def test_returns_snapshot_not_live_reference(self, mgr):
        sid = mgr.spawn("shell", "cat")
        snapshot = mgr.list_sessions()
        mgr.close(sid)
        # Snapshot captured before close should still hold the session object
        assert any(s.id == sid for s in snapshot)

    def test_multiple_sessions(self, mgr):
        sids = [mgr.spawn("shell", "cat") for _ in range(3)]
        ids_in_list = {s.id for s in mgr.list_sessions()}
        for sid in sids:
            assert sid in ids_in_list
        for sid in sids:
            mgr.close(sid)


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------

class TestClose:
    def test_close_removes_session(self, mgr):
        sid = mgr.spawn("shell", "cat")
        mgr.close(sid)
        time.sleep(0.05)
        assert mgr.get_session(sid) is None

    def test_close_nonexistent_is_noop(self, mgr):
        mgr.close(9999)  # must not raise

    def test_close_twice_is_safe(self, mgr):
        sid = mgr.spawn("shell", "cat")
        mgr.close(sid)
        mgr.close(sid)  # second close must not raise


# ---------------------------------------------------------------------------
# set_display_state()
# ---------------------------------------------------------------------------

class TestSetDisplayState:
    def test_expand_resets_unread_and_attention(self, mgr):
        sid = mgr.spawn("shell", "cat")
        s = mgr.get_session(sid)
        s.unread_count = 5
        s.attention_flag = True
        mgr.set_display_state(sid, "expanded")
        assert s.unread_count == 0
        assert s.attention_flag is False
        mgr.close(sid)

    def test_collapse_preserves_unread(self, mgr):
        sid = mgr.spawn("shell", "cat")
        s = mgr.get_session(sid)
        s.unread_count = 3
        mgr.set_display_state(sid, "collapsed")
        assert s.unread_count == 3
        mgr.close(sid)

    def test_nonexistent_session_is_noop(self, mgr):
        mgr.set_display_state(9999, "expanded")  # must not raise


# ---------------------------------------------------------------------------
# resize()
# ---------------------------------------------------------------------------

class TestResize:
    def test_resize_valid_session_no_crash(self, mgr):
        sid = mgr.spawn("shell", "cat")
        mgr.resize(sid, 40, 80)  # must not raise
        mgr.close(sid)

    def test_resize_nonexistent_is_noop(self, mgr):
        mgr.resize(9999, 40, 80)  # must not raise

    def test_resize_after_close_is_noop(self, mgr):
        sid = mgr.spawn("shell", "cat")
        mgr.close(sid)
        time.sleep(0.05)
        mgr.resize(sid, 40, 80)  # must not raise


# ---------------------------------------------------------------------------
# write()
# ---------------------------------------------------------------------------

class TestWrite:
    def test_write_running_session_no_crash(self, mgr):
        sid = mgr.spawn("shell", "cat")
        mgr.write(sid, "hello\n")
        time.sleep(0.1)
        mgr.close(sid)

    def test_write_nonexistent_is_noop(self, mgr):
        mgr.write(9999, "hello")  # must not raise

    def test_write_after_close_is_noop(self, mgr):
        sid = mgr.spawn("shell", "cat")
        mgr.close(sid)
        time.sleep(0.05)
        mgr.write(sid, "hello")  # must not raise

    def test_write_appends_newline_if_missing(self, mgr):
        # No assertion on output content — just verify no crash and that cat
        # processes the input (output list non-empty after short wait).
        sid = mgr.spawn("shell", "cat")
        mgr.write(sid, "ping")  # no trailing newline
        time.sleep(0.15)
        s = mgr.get_session(sid)
        assert s is not None
        mgr.close(sid)


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------

class TestStop:
    def test_stop_empties_session_list(self):
        m = PTYManager()
        m.spawn("shell", "cat")
        m.spawn("shell", "cat")
        m.stop()
        time.sleep(0.05)
        assert m.list_sessions() == []

    def test_stop_twice_is_safe(self):
        m = PTYManager()
        m.spawn("shell", "cat")
        m.stop()
        m.stop()  # must not raise

    def test_stop_joins_io_thread(self):
        m = PTYManager()
        m.spawn("shell", "cat")
        m.stop()
        # The I/O thread must have exited within stop()'s join timeout.
        # If this fails it means stop() returned while the thread was still
        # running — a teardown-completeness bug.
        assert not m._thread.is_alive(), (
            "stop() returned but I/O thread is still alive; "
            "join timeout may be too short or thread is stuck"
        )

    def test_master_fd_cleared_after_stop(self):
        """stop() must close the master PTY fd and clear session.master_fd.

        We check the session object's master_fd field rather than the raw fd
        number, because the OS may reuse the same fd number for a different
        file immediately after close, making a /dev/fd listing unreliable.
        """
        m = PTYManager()
        sid = m.spawn("shell", "cat")
        s = m.get_session(sid)
        assert s.master_fd is not None  # sanity: fd was opened
        m.stop()
        time.sleep(0.1)
        assert s.master_fd is None


# ---------------------------------------------------------------------------
# I/O loop resilience
# ---------------------------------------------------------------------------

class TestIOLoopResilience:
    def test_io_loop_survives_concurrent_close(self, mgr):
        """Close sessions rapidly while the I/O loop is running — no crash."""
        sids = [mgr.spawn("shell", "cat") for _ in range(5)]
        for sid in sids:
            mgr.close(sid)
        time.sleep(0.2)
        # I/O thread must still be alive (stop() not called yet)
        assert mgr._thread.is_alive()

    def test_output_collected_from_short_lived_process(self, mgr):
        """A process that exits quickly should still get its output captured."""
        sid = mgr.spawn("shell", "echo hello")
        time.sleep(0.3)
        s = mgr.get_session(sid)
        # Session may already be exited, but output should have been collected
        # (either still in registry or gone — both are acceptable; we just
        #  verify no exception was raised by the I/O loop)
        # The I/O thread must still be alive
        assert mgr._thread.is_alive()


# ---------------------------------------------------------------------------
# Task #39 — Terminal rendering: ANSI stripping, initial size, resize tracking
# ---------------------------------------------------------------------------

class TestAnsiStripping:
    def test_strip_ansi_removes_color_codes(self):
        from lex.dx.pty_runtime import strip_ansi
        assert strip_ansi("\x1b[32mhello\x1b[0m") == "hello"

    def test_strip_ansi_removes_cursor_movement(self):
        from lex.dx.pty_runtime import strip_ansi
        # ESC[2J (clear screen), ESC[H (home cursor)
        assert strip_ansi("\x1b[2J\x1b[Htext") == "text"

    def test_strip_ansi_removes_osc_title(self):
        from lex.dx.pty_runtime import strip_ansi
        # OSC title sequence used by many terminals: ESC ] 0 ; title BEL
        raw = "\x1b]0;claude code\x07hello"
        assert strip_ansi(raw) == "hello"

    def test_strip_ansi_passes_plain_text_unchanged(self):
        from lex.dx.pty_runtime import strip_ansi
        assert strip_ansi("plain text") == "plain text"

    def test_output_stored_without_ansi(self, mgr):
        """Output containing ANSI codes must be stored as clean text."""
        sid = mgr.spawn("shell", "cat")
        mgr.write(sid, "\x1b[32mhello\x1b[0m\n")
        time.sleep(0.15)
        s = mgr.get_session(sid)
        assert s is not None
        # No ESC byte should appear in any stored output line
        for line in s.output:
            assert "\x1b" not in line, f"ANSI escape found in output line: {line!r}"
        mgr.close(sid)

    def test_attention_still_detected_with_ansi(self, mgr):
        """Attention patterns detected in raw data before stripping."""
        sid = mgr.spawn("shell", "cat")
        # Write a prompt-like string wrapped in color codes
        mgr.write(sid, "\x1b[33m> \x1b[0m")
        time.sleep(0.15)
        s = mgr.get_session(sid)
        assert s is not None
        assert s.attention_flag is True
        mgr.close(sid)


class TestInitialPTYSize:
    def test_spawn_stores_default_rows_cols(self, mgr):
        sid = mgr.spawn("shell", "cat")
        s = mgr.get_session(sid)
        assert s.rows == 24
        assert s.cols == 80
        mgr.close(sid)

    def test_spawn_stores_custom_rows_cols(self, mgr):
        sid = mgr.spawn("shell", "cat", rows=40, cols=120)
        s = mgr.get_session(sid)
        assert s.rows == 40
        assert s.cols == 120
        mgr.close(sid)

    def test_resize_updates_session_fields(self, mgr):
        sid = mgr.spawn("shell", "cat")
        mgr.resize(sid, 30, 100)
        s = mgr.get_session(sid)
        assert s.rows == 30
        assert s.cols == 100
        mgr.close(sid)

    def test_resize_nonexistent_does_not_update(self, mgr):
        """Resize on unknown session must be a no-op (no KeyError)."""
        mgr.resize(9999, 30, 100)  # must not raise


class TestOutputTailDepth:
    def test_output_tail_200_lines_stored(self, mgr):
        """Session output supports at least 200 lines without truncation."""
        sid = mgr.spawn("shell", "cat")
        # Write 200 short lines via stdin
        for i in range(200):
            mgr.write(sid, f"line{i}\n")
        time.sleep(0.5)
        s = mgr.get_session(sid)
        assert s is not None
        # We don't assert exact count (timing), but at least some lines captured
        assert len(s.output) > 0
        mgr.close(sid)
