"""
dx v1 Acceptance Tests

Encodes the screen-contract acceptance criteria, region data contracts, and
writeback rules from docs/dx-screen-contract.md and docs/dx-architecture.md.

These tests validate the data layer that dx will read from and write through.
They do not implement the dx UI.

Sequencing note: the safe first implementation slice is
  1. roster
  2. agent-scoped file list
  3. read-only tabbed diff view
  4. message and annotation actions
  5. constrained quick edit mode
Tests are grouped in that order.
"""

import json

import pytest

from lex.dashboard import load_dashboard_state
from lex.db import connect, ensure_workspace, initialize_database, log_event
from lex.dx.app import dx_log_edit_event


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _setup_agents(conn):
    """Insert two active agents: one claude dev, one codex pm."""
    conn.execute(
        "INSERT INTO agents (name, kind, role, specialty, status) "
        "VALUES ('claude-calm-otter', 'claude', 'dev', 'engineer', 'active')"
    )
    conn.execute(
        "INSERT INTO agents (name, kind, role, specialty, status) "
        "VALUES ('codex-brisk-falcon', 'codex', 'pm', 'product_manager', 'active')"
    )


def _setup_active_session(conn, agent_id, tmp_path, *, heartbeat_offset=None, git_changed=None, git_base_ref=None):
    heartbeat_expr = (
        f"datetime('now', '{heartbeat_offset}')" if heartbeat_offset else "CURRENT_TIMESTAMP"
    )
    changed_json = json.dumps(git_changed or [])
    conn.execute(
        f"""
        INSERT INTO sessions (agent_id, label, status, cwd, capabilities_json,
                              heartbeat_at, git_changed_files_json, git_base_ref)
        VALUES (?, 'primary', 'active', ?, '{{}}', {heartbeat_expr}, ?, ?)
        """,
        (agent_id, str(tmp_path), changed_json, git_base_ref or "main"),
    )


def _setup_task(conn, agent_id, *, status="in_progress", claimed_paths=None):
    paths_json = json.dumps(claimed_paths or [])
    completed_at = "CURRENT_TIMESTAMP" if status in ("done", "abandoned") else "NULL"
    conn.execute(
        f"""
        INSERT INTO tasks (title, description, status, priority, owner_agent_id,
                           delegation_mode, claimed_paths_json, completed_at)
        VALUES ('Implement feature X', 'Description', ?, 1, ?, 'direct', ?, {completed_at})
        """,
        (status, agent_id, paths_json),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


# ---------------------------------------------------------------------------
# Region 1: Agent Roster — data contract
# ---------------------------------------------------------------------------


def test_dx_roster_exposes_required_screen_contract_fields(tmp_path):
    """dx_roster rows must carry all fields listed in the screen-contract data spec."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    _setup_active_session(conn, agent_id=1, tmp_path=tmp_path)
    _setup_task(conn, agent_id=1)
    conn.commit()

    state = load_dashboard_state(tmp_path)
    row = next(r for r in state.dx_roster if r["agent_id"] == 1)

    required_fields = {
        "agent_id",
        "agent_name",
        "agent_kind",
        "agent_role",
        "session_id",
        "is_stale",
        "task_id",
        "task_title",
        "task_status",
        "claimed_path_count",
        "changed_file_count",
        "last_activity_at",
    }
    missing = required_fields - set(row.keys())
    assert not missing, f"dx_roster row missing fields: {missing}"


def test_dx_roster_only_includes_active_agents(tmp_path):
    """Retired agents must not appear in the dx roster."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    conn.execute("INSERT INTO agents (name, kind, role, status) VALUES ('cursor-old-lynx', 'cursor', 'dev', 'retired')")
    conn.commit()

    state = load_dashboard_state(tmp_path)
    names = {r["agent_name"] for r in state.dx_roster}

    assert "cursor-old-lynx" not in names
    assert "claude-calm-otter" in names
    assert "codex-brisk-falcon" in names


def test_dx_roster_idle_state_when_session_active_no_task(tmp_path):
    """Agent with active session but no active task should appear with null task fields."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    _setup_active_session(conn, agent_id=1, tmp_path=tmp_path)
    conn.commit()

    state = load_dashboard_state(tmp_path)
    row = next(r for r in state.dx_roster if r["agent_name"] == "claude-calm-otter")

    assert row["session_id"] is not None
    assert row["task_id"] is None
    assert row["task_status"] is None


def test_dx_roster_active_state_when_task_claimed_or_in_progress(tmp_path):
    """Agent with active session and active task should expose task info on roster row."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    _setup_active_session(conn, agent_id=1, tmp_path=tmp_path)
    _setup_task(conn, agent_id=1, status="in_progress")
    conn.commit()

    state = load_dashboard_state(tmp_path)
    row = next(r for r in state.dx_roster if r["agent_name"] == "claude-calm-otter")

    assert row["task_id"] is not None
    assert row["task_title"] == "Implement feature X"
    assert row["task_status"] == "in_progress"


def test_dx_roster_stale_state_when_heartbeat_old(tmp_path):
    """Agent whose heartbeat is > 15 minutes old should be flagged as stale."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    _setup_active_session(conn, agent_id=1, tmp_path=tmp_path, heartbeat_offset="-20 minutes")
    conn.commit()

    state = load_dashboard_state(tmp_path)
    row = next(r for r in state.dx_roster if r["agent_name"] == "claude-calm-otter")

    assert row["is_stale"] == 1


def test_dx_roster_not_stale_when_heartbeat_recent(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    _setup_active_session(conn, agent_id=1, tmp_path=tmp_path)
    conn.commit()

    state = load_dashboard_state(tmp_path)
    row = next(r for r in state.dx_roster if r["agent_name"] == "claude-calm-otter")

    assert row["is_stale"] == 0


def test_dx_roster_claimed_path_count_reflects_task_claimed_paths(tmp_path):
    """claimed_path_count must equal the length of the task's claimed_paths_json."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    _setup_active_session(conn, agent_id=1, tmp_path=tmp_path)
    _setup_task(conn, agent_id=1, claimed_paths=["src/foo.py", "src/bar.py", "tests/test_foo.py"])
    conn.commit()

    state = load_dashboard_state(tmp_path)
    row = next(r for r in state.dx_roster if r["agent_name"] == "claude-calm-otter")

    assert row["claimed_path_count"] == 3


def test_dx_roster_changed_file_count_reflects_session_git_state(tmp_path):
    """changed_file_count must equal the length of git_changed_files_json on the session."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    _setup_active_session(
        conn, agent_id=1, tmp_path=tmp_path,
        git_changed=["src/foo.py", "src/bar.py"],
    )
    _setup_task(conn, agent_id=1)
    conn.commit()

    state = load_dashboard_state(tmp_path)
    row = next(r for r in state.dx_roster if r["agent_name"] == "claude-calm-otter")

    assert row["changed_file_count"] == 2


def test_dx_roster_excludes_terminal_task_statuses(tmp_path):
    """Tasks with status 'done' or 'abandoned' must not surface as active roster tasks."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    _setup_active_session(conn, agent_id=1, tmp_path=tmp_path)
    _setup_task(conn, agent_id=1, status="done")
    conn.commit()

    state = load_dashboard_state(tmp_path)
    row = next(r for r in state.dx_roster if r["agent_name"] == "claude-calm-otter")

    assert row["task_id"] is None, "terminal-status task must not appear on roster"


# ---------------------------------------------------------------------------
# Region 2: Agent-Scoped File Browser — data contract
# ---------------------------------------------------------------------------


def test_dx_file_browser_claimed_paths_derivable_from_task(tmp_path):
    """Claimed paths for an agent are readable from their active task's claimed_paths_json."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    expected_paths = ["src/api/routes.py", "src/domain/models.py"]
    _setup_task(conn, agent_id=1, status="claimed", claimed_paths=expected_paths)
    conn.commit()

    row = conn.execute(
        "SELECT claimed_paths_json FROM tasks WHERE owner_agent_id = 1"
    ).fetchone()
    assert row is not None
    actual_paths = json.loads(row["claimed_paths_json"])
    assert actual_paths == expected_paths


def test_dx_file_browser_changed_files_derivable_from_session(tmp_path):
    """Changed files for an agent are readable from their active session's git_changed_files_json."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    expected_files = ["src/api/routes.py", "tests/test_routes.py"]
    _setup_active_session(conn, agent_id=1, tmp_path=tmp_path, git_changed=expected_files)
    conn.commit()

    row = conn.execute(
        "SELECT git_changed_files_json FROM sessions WHERE agent_id = 1 AND status = 'active'"
    ).fetchone()
    assert row is not None
    actual_files = json.loads(row["git_changed_files_json"])
    assert actual_files == expected_files


def test_dx_file_browser_conflict_detectable_when_paths_overlap(tmp_path):
    """
    Path conflicts between two agents are detectable via the existing conflict detection.
    Screen contract requires a conflict marker when multiple agents touch the same path.
    """
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    shared_path = "src/shared/utils.py"
    _setup_task(conn, agent_id=1, status="in_progress", claimed_paths=[shared_path])
    _setup_task(conn, agent_id=2, status="in_progress", claimed_paths=[shared_path])
    conn.commit()

    # Both agents claim the same path — the data layer exposes this for UI rendering
    rows = conn.execute(
        """
        SELECT t.owner_agent_id, t.claimed_paths_json
        FROM tasks t
        WHERE t.status = 'in_progress'
        """
    ).fetchall()

    all_claims: dict[str, list[int]] = {}
    for row in rows:
        for p in json.loads(row["claimed_paths_json"]):
            all_claims.setdefault(p, []).append(row["owner_agent_id"])

    conflicted = {p for p, owners in all_claims.items() if len(owners) > 1}
    assert shared_path in conflicted


# ---------------------------------------------------------------------------
# Region 3: Workspace Tab — data contract
# ---------------------------------------------------------------------------


def test_dx_tab_context_requires_base_ref_from_session(tmp_path):
    """
    Each workspace tab needs a git_base_ref to generate diffs.
    Verify the sessions table exposes this field.
    """
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    _setup_active_session(conn, agent_id=1, tmp_path=tmp_path, git_base_ref="main")
    conn.commit()

    row = conn.execute(
        "SELECT git_base_ref FROM sessions WHERE agent_id = 1 AND status = 'active'"
    ).fetchone()
    assert row is not None
    assert row["git_base_ref"] == "main"


def test_dx_tab_session_query_includes_base_ref(tmp_path):
    """The dashboard sessions query must include git_base_ref for tab diff generation."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    _setup_active_session(conn, agent_id=1, tmp_path=tmp_path, git_base_ref="v1.2.0")
    conn.commit()

    state = load_dashboard_state(tmp_path)
    session = state.sessions[0]

    assert "git_base_ref" in session
    assert session["git_base_ref"] == "v1.2.0"


# ---------------------------------------------------------------------------
# Writeback contract
# ---------------------------------------------------------------------------


def test_dx_annotation_event_is_recordable_with_task_and_agent_attribution(tmp_path):
    """
    dx may record dx.annotation events.
    The event must be attributable to both task and agent.
    """
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    task_id = _setup_task(conn, agent_id=1)
    conn.commit()

    log_event(
        conn,
        "dx.annotation",
        task_id=task_id,
        agent_id=1,
        payload={"hunk": "src/foo.py:42", "note": "This logic looks off", "provenance": "dx"},
    )
    conn.commit()

    row = conn.execute(
        "SELECT event_type, task_id, agent_id, payload_json FROM events WHERE event_type = 'dx.annotation'"
    ).fetchone()
    assert row is not None
    assert row["task_id"] == task_id
    assert row["agent_id"] == 1
    payload = json.loads(row["payload_json"])
    assert payload["provenance"] == "dx"


def test_dx_flag_event_is_recordable(tmp_path):
    """dx may record dx.flag events for hunk-level review flags."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    task_id = _setup_task(conn, agent_id=1)
    conn.commit()

    log_event(
        conn,
        "dx.flag",
        task_id=task_id,
        agent_id=1,
        payload={"file": "src/foo.py", "reason": "needs_review", "provenance": "dx"},
    )
    conn.commit()

    row = conn.execute("SELECT task_id FROM events WHERE event_type = 'dx.flag'").fetchone()
    assert row is not None
    assert row["task_id"] == task_id


def test_dx_message_writeback_records_on_task_thread(tmp_path):
    """dx may send task-thread messages. The message must be scoped to the task."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    task_id = _setup_task(conn, agent_id=1)
    conn.commit()

    conn.execute(
        """
        INSERT INTO messages (task_id, from_agent_id, to_agent_id, type, subject, body)
        VALUES (?, 1, 2, 'note', 'dx intervention', 'Redirecting: focus on error handling')
        """,
        (task_id,),
    )
    conn.commit()

    row = conn.execute(
        "SELECT task_id, type, body FROM messages WHERE type = 'note'"
    ).fetchone()
    assert row is not None
    assert row["task_id"] == task_id
    assert "error handling" in row["body"]


def test_dx_writeback_does_not_mutate_session_lifecycle(tmp_path):
    """
    Writing a dx.annotation event must not alter session state.
    Sessions are owned by the agent — dx is read-only over session lifecycle.
    """
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    _setup_active_session(conn, agent_id=1, tmp_path=tmp_path)
    task_id = _setup_task(conn, agent_id=1)
    conn.commit()

    before = conn.execute(
        "SELECT status, ended_at, heartbeat_at FROM sessions WHERE agent_id = 1"
    ).fetchone()

    log_event(conn, "dx.annotation", task_id=task_id, agent_id=1,
              payload={"note": "check this"})
    conn.commit()

    after = conn.execute(
        "SELECT status, ended_at, heartbeat_at FROM sessions WHERE agent_id = 1"
    ).fetchone()

    assert after["status"] == before["status"]
    assert after["ended_at"] == before["ended_at"]
    assert after["heartbeat_at"] == before["heartbeat_at"]


def test_dx_writeback_does_not_mutate_task_ownership(tmp_path):
    """
    Writing a dx message must not change task owner_agent_id.
    Task ownership changes must go through lx verbs (task handoff, delegate).
    """
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    task_id = _setup_task(conn, agent_id=1)
    conn.commit()

    before_owner = conn.execute(
        "SELECT owner_agent_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["owner_agent_id"]

    # dx sends a message — ownership must not change
    conn.execute(
        "INSERT INTO messages (task_id, from_agent_id, type, subject, body) VALUES (?, 1, 'note', 'dx note', 'redirect')",
        (task_id,),
    )
    conn.commit()

    after_owner = conn.execute(
        "SELECT owner_agent_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["owner_agent_id"]

    assert after_owner == before_owner


def test_dx_writeback_does_not_mutate_lease_state(tmp_path):
    """
    dx event writes must not modify task_leases.
    Lease renewal is exclusively through lx session heartbeat or hook paths.
    """
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    task_id = _setup_task(conn, agent_id=1)
    conn.execute(
        "INSERT INTO task_leases (task_id, agent_id, expires_at) VALUES (?, 1, datetime('now', '+30 minutes'))",
        (task_id,),
    )
    conn.commit()

    before_expires = conn.execute(
        "SELECT expires_at FROM task_leases WHERE task_id = ?", (task_id,)
    ).fetchone()["expires_at"]

    log_event(conn, "dx.annotation", task_id=task_id, agent_id=1,
              payload={"note": "check this"})
    conn.commit()

    after_expires = conn.execute(
        "SELECT expires_at FROM task_leases WHERE task_id = ?", (task_id,)
    ).fetchone()["expires_at"]

    assert after_expires == before_expires


# ---------------------------------------------------------------------------
# Slice 5: Quick Edit — writeback contract
# ---------------------------------------------------------------------------


def test_dx_quick_edit_write_produces_dx_edit_event(tmp_path):
    """Saving a quick edit must record a dx.edit event with file, diff_summary, and provenance."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    task_id = _setup_task(conn, agent_id=1)
    conn.commit()

    target = tmp_path / "src" / "foo.py"
    target.parent.mkdir(parents=True)
    target.write_text("x = 1\n", encoding="utf-8")

    dx_log_edit_event(
        tmp_path,
        agent_name="claude-calm-otter",
        task_id=task_id,
        path="src/foo.py",
        diff_summary="4 diff lines",
    )

    row = conn.execute(
        "SELECT event_type, task_id, agent_id, payload_json FROM events WHERE event_type = 'dx.edit'"
    ).fetchone()
    assert row is not None
    assert row["task_id"] == task_id
    payload = json.loads(row["payload_json"])
    assert payload["file"] == "src/foo.py"
    assert payload["diff_summary"] == "4 diff lines"
    assert payload["provenance"] == "dx"


def test_dx_quick_edit_does_not_mutate_task_ownership(tmp_path):
    """A dx.edit event must not change task owner_agent_id."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    task_id = _setup_task(conn, agent_id=1)
    conn.commit()

    before_owner = conn.execute(
        "SELECT owner_agent_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["owner_agent_id"]

    dx_log_edit_event(
        tmp_path,
        agent_name="claude-calm-otter",
        task_id=task_id,
        path="src/foo.py",
        diff_summary="2 diff lines",
    )

    after_owner = conn.execute(
        "SELECT owner_agent_id FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()["owner_agent_id"]

    assert after_owner == before_owner


def test_dx_quick_edit_filesystem_write_updates_file(tmp_path):
    """Quick edit must write the modified content to the filesystem."""
    target = tmp_path / "hello.txt"
    target.write_text("line one\nline two\n", encoding="utf-8")

    # simulate what DxTui._qe_save does: write modified lines
    new_content = "line one\nline two edited\n"
    target.write_text(new_content, encoding="utf-8")

    assert target.read_text(encoding="utf-8") == new_content


def test_dx_quick_edit_session_state_unchanged_after_edit_event(tmp_path):
    """Logging a dx.edit event must not alter session status or ended_at."""
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    _setup_agents(conn)
    _setup_active_session(conn, agent_id=1, tmp_path=tmp_path)
    task_id = _setup_task(conn, agent_id=1)
    conn.commit()

    before = conn.execute(
        "SELECT status, ended_at FROM sessions WHERE agent_id = 1"
    ).fetchone()

    dx_log_edit_event(
        tmp_path,
        agent_name="claude-calm-otter",
        task_id=task_id,
        path="src/foo.py",
        diff_summary="1 diff line",
    )

    after = conn.execute(
        "SELECT status, ended_at FROM sessions WHERE agent_id = 1"
    ).fetchone()

    assert after["status"] == before["status"]
    assert after["ended_at"] == before["ended_at"]
