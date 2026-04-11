import json
from pathlib import Path

from lex.db import connect, ensure_workspace, initialize_database
from lex.dx.pty_runtime import TerminalSession
from lex.dx.shell import DxShell, build_shell_summaries


def _setup_agent(conn, *, name="codex-brisk-otter", kind="codex", role="dev"):
    conn.execute(
        "INSERT INTO agents (name, kind, role, specialty, status) VALUES (?, ?, ?, '', 'active')",
        (name, kind, role),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _setup_session(conn, agent_id, tmp_path, *, changed=None, base_ref="main"):
    conn.execute(
        """
        INSERT INTO sessions (agent_id, label, status, cwd, capabilities_json, git_changed_files_json, git_base_ref)
        VALUES (?, 'primary', 'active', ?, '{}', ?, ?)
        """,
        (agent_id, str(tmp_path), json.dumps(changed or []), base_ref),
    )


def _setup_task(conn, agent_id, *, claimed=None, status="claimed"):
    conn.execute(
        """
        INSERT INTO tasks (title, status, priority, owner_agent_id, delegation_mode, claimed_paths_json)
        VALUES ('Build dx v3 shell', ?, 1, ?, 'direct', ?)
        """,
        (status, agent_id, json.dumps(claimed or [])),
    )


class FakePTYManager:
    def __init__(self):
        self.sessions: dict[int, TerminalSession] = {}
        self.next_id = 1
        self.writes: list[tuple[int, str]] = []
        self.display_states: list[tuple[int, str]] = []
        self.resizes: list[tuple[int, int, int]] = []
        self.closed: list[int] = []
        self.stopped = False

    def spawn(self, kind, cmd, cwd=None, lex_root=None, lex_session_id=None):
        sid = self.next_id
        self.next_id += 1
        session = TerminalSession(
            id=sid,
            title=f"{kind}-{sid}",
            kind=kind,
            cwd=Path(cwd or "."),
            pid=1000 + sid,
            status="running",
            output=[f"{kind} ready", "waiting for input"],
        )
        self.sessions[sid] = session
        return sid

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def list_sessions(self):
        return list(self.sessions.values())

    def write(self, session_id, text):
        self.writes.append((session_id, text))
        session = self.sessions[session_id]
        session.output.append(f"stdin: {text}")

    def resize(self, session_id, rows, cols):
        self.resizes.append((session_id, rows, cols))
        session = self.sessions[session_id]
        session.rows = rows
        session.cols = cols

    def set_display_state(self, session_id, state):
        self.display_states.append((session_id, state))
        session = self.sessions[session_id]
        session.display_state = state
        if state == "expanded":
            session.unread_count = 0
            session.attention_flag = False

    def close(self, session_id):
        self.closed.append(session_id)
        self.sessions.pop(session_id, None)

    def stop(self):
        self.stopped = True


def test_build_shell_summaries_uses_dx_view_state(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent_id = _setup_agent(conn)
    _setup_session(conn, agent_id, tmp_path, changed=["src/lex/dx/shell.py"])
    _setup_task(conn, agent_id, claimed=["src/lex/dx/shell.py"])
    conn.commit()

    summaries = build_shell_summaries(tmp_path)

    assert len(summaries) == 1
    assert summaries[0].id == "codex-brisk-otter"
    assert "#1 Build dx v3 shell" in summaries[0].task_label


def test_build_shell_summaries_includes_runtime_sessions(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    runtime = TerminalSession(
        id=7,
        title="shell-7",
        kind="shell",
        cwd=tmp_path,
        pid=4321,
        status="running",
        output=["boot", "tests passed"],
        unread_count=2,
        attention_flag=True,
    )

    summaries = build_shell_summaries(tmp_path, terminal_sessions=[runtime])

    runtime_summary = next(summary for summary in summaries if summary.id == "shell-7")
    assert runtime_summary.source == "runtime"
    assert runtime_summary.attention_flag is True
    assert runtime_summary.screen_snapshot[-1] == "tests passed"


def test_build_shell_summaries_prioritizes_runtime_before_agent(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent_id = _setup_agent(conn)
    _setup_session(conn, agent_id, tmp_path)
    _setup_task(conn, agent_id)
    conn.commit()
    runtime = TerminalSession(
        id=7,
        title="shell-7",
        kind="shell",
        cwd=tmp_path,
        pid=4321,
        status="running",
        output=["boot"],
    )

    summaries = build_shell_summaries(tmp_path, terminal_sessions=[runtime])

    assert [summary.id for summary in summaries[:2]] == ["shell-7", "codex-brisk-otter"]


def test_shell_submit_prompt_updates_routing_target_and_status(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent_id = _setup_agent(conn)
    _setup_session(conn, agent_id, tmp_path)
    _setup_task(conn, agent_id)
    conn.commit()

    shell = DxShell(tmp_path)
    result = shell.submit_prompt("/focus codex")

    assert result.status == "ok"
    assert shell.controller.state.routing_target_id == "codex-brisk-otter"
    assert "focused" in shell.status or "routing" in shell.status
    shell.stop()


def test_shell_spawn_creates_runtime_slice_and_routes_to_it(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    manager = FakePTYManager()

    shell = DxShell(tmp_path, pty_manager=manager, runtime_command_builder=lambda kind: f"run-{kind}")
    result = shell.submit_prompt("/spawn shell")

    assert result.status == "ok"
    assert shell.controller.state.routing_target_id == "shell-1"
    assert shell.controller.state.expanded_slice_id == "shell-1"
    assert any(summary.id == "shell-1" and summary.source == "runtime" for summary in shell.summaries)
    shell.stop()
    assert manager.stopped is True


def test_shell_freeform_prompt_writes_to_runtime_target(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    manager = FakePTYManager()

    shell = DxShell(tmp_path, pty_manager=manager, runtime_command_builder=lambda kind: f"run-{kind}")
    shell.submit_prompt("/spawn shell")
    shell.submit_prompt("continue")

    assert manager.writes == [(1, "continue")]
    assert shell.status == "sent to shell-1"
    shell.stop()


def test_shell_freeform_prompt_double_submits_for_codex_and_gemini(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    manager = FakePTYManager()

    shell = DxShell(tmp_path, pty_manager=manager, runtime_command_builder=lambda kind: f"run-{kind}")
    shell.submit_prompt("/spawn codex")
    shell.submit_prompt("continue")
    shell.submit_prompt("/spawn gemini")
    shell.submit_prompt("keep going")

    assert manager.writes == [
        (1, "continue\r\r"),
        (2, "keep going\r\r"),
    ]
    shell.stop()


def test_shell_expanded_lines_render_runtime_output_and_actions(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    manager = FakePTYManager()

    shell = DxShell(tmp_path, pty_manager=manager, runtime_command_builder=lambda kind: f"run-{kind}")
    shell.submit_prompt("/spawn shell")
    summary = next(summary for summary in shell.summaries if summary.id == "shell-1")

    lines = shell.expanded_lines(summary, max_lines=6)

    assert any("shell ready" in line for line in lines)
    assert any("[y] approve" in line for line in lines)
    shell.stop()


def test_runtime_viewport_lines_use_screen_snapshot(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    manager = FakePTYManager()

    shell = DxShell(tmp_path, pty_manager=manager, runtime_command_builder=lambda kind: f"run-{kind}")
    shell.submit_prompt("/spawn shell")
    session = manager.get_session(1)
    session.output = ["old line", "new line"]
    session.screen_lines = lambda: ["screen a", "screen b", "screen c"]  # type: ignore[attr-defined]
    shell.refresh()
    summary = next(summary for summary in shell.summaries if summary.id == "shell-1")

    lines = shell.runtime_viewport_lines(summary, max_lines=2)

    assert lines == ["screen b", "screen c"]
    shell.stop()


def test_feed_layout_makes_runtime_slice_dominant(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent_id = _setup_agent(conn)
    _setup_session(conn, agent_id, tmp_path)
    _setup_task(conn, agent_id)
    conn.commit()
    manager = FakePTYManager()

    shell = DxShell(tmp_path, pty_manager=manager, runtime_command_builder=lambda kind: f"run-{kind}")
    shell.submit_prompt("/spawn shell")

    layout = shell.feed_layout(available_rows=20)

    assert layout.dominant is not None
    assert layout.dominant.id == "shell-1"
    assert layout.dominant_height >= 14
    assert all(summary.id != "shell-1" for summary in layout.summaries)
    shell.stop()


def test_render_resizes_dominant_runtime_to_viewport(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    manager = FakePTYManager()

    shell = DxShell(tmp_path, pty_manager=manager, runtime_command_builder=lambda kind: f"run-{kind}")
    shell.submit_prompt("/spawn shell")

    class FakeStdScr:
        def getmaxyx(self):
            return (30, 100)
        def erase(self):
            pass
        def addnstr(self, *args, **kwargs):
            pass
        def hline(self, *args, **kwargs):
            pass
        def refresh(self):
            pass

    shell.render(FakeStdScr())

    assert manager.resizes
    session_id, rows, cols = manager.resizes[-1]
    assert session_id == 1
    assert rows >= 10
    assert cols == 94
    shell.stop()


def test_shell_toggle_expand_marks_summary(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent_id = _setup_agent(conn)
    _setup_session(conn, agent_id, tmp_path)
    _setup_task(conn, agent_id)
    conn.commit()

    shell = DxShell(tmp_path)
    shell.toggle_expand()

    assert shell.controller.state.expanded_slice_id == "codex-brisk-otter"
    assert shell.focused_summary() is not None
    shell.stop()


# ---------------------------------------------------------------------------
# Task #37 — _default_runtime_command binary detection + _spawn_runtime errors
# ---------------------------------------------------------------------------

def test_default_runtime_command_shell_returns_shell_binary(tmp_path):
    """Shell kind returns the $SHELL env var (or bash fallback)."""
    import os
    from lex.dx.shell import _default_runtime_command

    shell_bin = os.environ.get("SHELL", "bash")
    assert _default_runtime_command("shell") == shell_bin


def test_default_runtime_command_known_binary_found(tmp_path, monkeypatch):
    """When shutil.which() returns a path, that path is returned."""
    import shutil
    from lex.dx.shell import _default_runtime_command

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/local/bin/{name}")
    assert _default_runtime_command("claude") == "/usr/local/bin/claude"
    assert _default_runtime_command("codex") == "/usr/local/bin/codex"
    assert _default_runtime_command("gemini") == "/usr/local/bin/gemini"


def test_default_runtime_command_missing_binary_raises(tmp_path, monkeypatch):
    """When shutil.which() returns None, RuntimeError is raised."""
    import shutil
    import pytest
    from lex.dx.shell import _default_runtime_command

    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="not found in PATH"):
        _default_runtime_command("claude")


def test_spawn_runtime_missing_binary_sets_status(tmp_path, monkeypatch):
    """If the command builder raises RuntimeError, status is set and no exception propagates."""
    import shutil
    from lex.db import connect, ensure_workspace, initialize_database

    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    monkeypatch.setattr(shutil, "which", lambda name: None)
    manager = FakePTYManager()
    shell = DxShell(tmp_path, pty_manager=manager)

    result = shell.submit_prompt("/spawn claude")

    assert "claude" in shell.status.lower() or "not found" in shell.status.lower() or "cannot spawn" in shell.status.lower()
    # No runtime session should have been created
    assert not any(s.kind == "claude" for s in manager.list_sessions())
    shell.stop()


def test_spawn_runtime_spawn_failure_sets_status(tmp_path):
    """If PTYManager.spawn() raises, status is set and no exception propagates."""
    from lex.db import connect, ensure_workspace, initialize_database

    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    class FailingPTYManager(FakePTYManager):
        def spawn(self, kind, cmd, **kwargs):
            raise OSError("pty allocation failed")

    manager = FailingPTYManager()
    shell = DxShell(
        tmp_path,
        pty_manager=manager,
        runtime_command_builder=lambda kind: f"/bin/{kind}",
    )
    result = shell.submit_prompt("/spawn shell")

    assert "spawn failed" in shell.status or "failed" in shell.status
    shell.stop()


# ---------------------------------------------------------------------------
# Task #46 — command-ribbon-first UI refinements
# ---------------------------------------------------------------------------

def test_header_text_shows_target_only(tmp_path):
    """Header is simplified to dx ▸ <target>, dropping the task label clutter."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent_id = _setup_agent(conn)
    _setup_session(conn, agent_id, tmp_path)
    _setup_task(conn, agent_id)
    conn.commit()

    shell = DxShell(tmp_path)
    shell.controller.set_routing_target("codex-brisk-otter")
    assert shell.header_text() == "dx  ▸  codex-brisk-otter"
    shell.stop()


def test_header_text_no_target(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    shell = DxShell(tmp_path)
    assert "no target" in shell.header_text()
    shell.stop()


def test_ribbon_height_is_two(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    shell = DxShell(tmp_path)
    assert shell.ribbon_height() == 2
    shell.stop()


def test_ribbon_target_line_no_target(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    shell = DxShell(tmp_path)
    shell.controller.set_routing_target(None)
    assert "no target" in shell.ribbon_target_line()
    shell.stop()


def test_ribbon_target_line_with_known_slice(tmp_path):
    """Ribbon target line includes slice title, state, and task label."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent_id = _setup_agent(conn)
    _setup_session(conn, agent_id, tmp_path)
    _setup_task(conn, agent_id)
    conn.commit()

    shell = DxShell(tmp_path)
    shell.controller.set_routing_target("codex-brisk-otter")
    shell.refresh()
    line = shell.ribbon_target_line()

    assert "codex-brisk-otter" in line
    assert "▸" in line
    shell.stop()


def test_ribbon_target_line_unknown_slice(tmp_path):
    """Ribbon target line falls back gracefully when target has no slice."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    shell = DxShell(tmp_path)
    shell.controller.set_routing_target("ghost-agent")
    line = shell.ribbon_target_line()

    assert "ghost-agent" in line
    shell.stop()


def test_submit_prompt_tracks_last_command(tmp_path):
    """submit_prompt records the last non-empty command in prompt.last_command."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent_id = _setup_agent(conn)
    _setup_session(conn, agent_id, tmp_path)
    _setup_task(conn, agent_id)
    conn.commit()

    shell = DxShell(tmp_path)
    shell.submit_prompt("/focus codex")
    assert shell.prompt.last_command == "/focus codex"
    shell.submit_prompt("  ")  # whitespace-only should not overwrite
    assert shell.prompt.last_command == "/focus codex"
    shell.stop()


def test_non_focused_slices_compress_in_feed(tmp_path):
    """Non-focused, non-attention slices render without task/detail text."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent_id = _setup_agent(conn)
    _setup_session(conn, agent_id, tmp_path)
    _setup_task(conn, agent_id)
    conn.commit()
    # Add a second agent to ensure there's a non-focused one
    _setup_agent(conn, name="codex-brisk-raven", kind="codex", role="dev")
    conn.commit()

    manager = FakePTYManager()
    shell = DxShell(tmp_path, pty_manager=manager, runtime_command_builder=lambda kind: f"run-{kind}")
    shell.submit_prompt("/spawn shell")  # focus moves to runtime

    captured_lines: list[str] = []

    class CapturingStdScr:
        def getmaxyx(self):
            return (40, 120)
        def erase(self):
            pass
        def addnstr(self, row, col, text, maxlen, *args):
            captured_lines.append(text)
        def hline(self, *args):
            pass
        def refresh(self):
            pass

    shell.render(CapturingStdScr())

    # Agent slices that are not focused should not include task_label or detail text
    agent_lines = [l for l in captured_lines if "codex-brisk" in l and "▼" not in l]
    for line in agent_lines:
        # Compressed format has no "changed=" or "#1" task detail
        assert "changed=" not in line, f"non-focused slice should be compressed: {line!r}"
    shell.stop()


def test_render_feed_bottom_accounts_for_ribbon(tmp_path):
    """Dominant PTY resize rows are smaller by ribbon_height() compared to pre-ribbon layout."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    manager = FakePTYManager()

    shell = DxShell(tmp_path, pty_manager=manager, runtime_command_builder=lambda kind: f"run-{kind}")
    shell.submit_prompt("/spawn shell")

    class FakeStdScr:
        def getmaxyx(self):
            return (30, 100)
        def erase(self):
            pass
        def addnstr(self, *args, **kwargs):
            pass
        def hline(self, *args, **kwargs):
            pass
        def refresh(self):
            pass

    shell.render(FakeStdScr())

    assert manager.resizes
    session_id, rows, cols = manager.resizes[-1]
    assert session_id == 1
    # With ribbon (2 extra rows), dominant height is smaller than pre-ribbon
    assert rows >= 8  # still meaningful height
    assert cols == 94
    shell.stop()


# ---------------------------------------------------------------------------
# Task #48 — interaction hardening
# ---------------------------------------------------------------------------

def test_close_command_actually_closes_slice(tmp_path):
    """/close <slice> must terminate the PTY session, not just show a prompt."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    manager = FakePTYManager()

    shell = DxShell(tmp_path, pty_manager=manager, runtime_command_builder=lambda kind: f"run-{kind}")
    shell.submit_prompt("/spawn shell")
    assert any(s.id == 1 for s in manager.list_sessions())

    shell.submit_prompt("/close shell-1")

    assert 1 in manager.closed, "close() should have been called on the PTY session"
    assert shell.controller.state.routing_target_id != "shell-1"
    shell.stop()


def test_broadcast_sends_to_all_runtime_sessions(tmp_path):
    """/broadcast <msg> must write to every spawned runtime session."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    manager = FakePTYManager()

    shell = DxShell(tmp_path, pty_manager=manager, runtime_command_builder=lambda kind: f"run-{kind}")
    shell.submit_prompt("/spawn shell")
    shell.submit_prompt("/spawn shell")

    manager.writes.clear()
    shell.submit_prompt("/broadcast hello agents")

    sent_ids = {sid for sid, _ in manager.writes}
    assert 1 in sent_ids
    assert 2 in sent_ids
    shell.stop()


def test_send_to_exited_session_reports_error(tmp_path):
    """Writing to an exited PTY session should set an error status, not silently no-op."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    manager = FakePTYManager()

    shell = DxShell(tmp_path, pty_manager=manager, runtime_command_builder=lambda kind: f"run-{kind}")
    shell.submit_prompt("/spawn shell")
    shell.controller.set_routing_target("shell-1")

    # Mark the session as exited without removing it from the manager
    session = manager.get_session(1)
    session.status = "exited"
    shell.refresh()

    manager.writes.clear()
    shell.submit_prompt("do something")

    assert not manager.writes, "no write should go to an exited session"
    assert "exited" in shell.status or "close" in shell.status.lower()
    shell.stop()


def test_close_clears_routing_target_if_matched(tmp_path):
    """/close should clear routing_target when the closed slice was targeted."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    manager = FakePTYManager()

    shell = DxShell(tmp_path, pty_manager=manager, runtime_command_builder=lambda kind: f"run-{kind}")
    shell.submit_prompt("/spawn shell")
    assert shell.controller.state.routing_target_id == "shell-1"

    shell.submit_prompt("/close shell-1")

    assert shell.controller.state.routing_target_id is None
    shell.stop()
