import json
import subprocess
import tomllib

from lex.dashboard import load_dashboard_state
from lex.db import connect, ensure_workspace, initialize_database
from lex.dx.app import (
    DxTui,
    build_diff,
    build_dx_view,
    current_actions,
    dx_flag_file,
    dx_log_annotation,
    dx_send_message,
    dx_update_task_priority,
    dx_update_task_status,
    main,
    read_file_contents,
)


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
        VALUES ('Build dx shell', ?, 1, ?, 'direct', ?)
        """,
        (status, agent_id, json.dumps(claimed or [])),
    )


def test_build_dx_view_combines_claimed_and_changed_files(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    agent_id = _setup_agent(conn)
    _setup_session(conn, agent_id, tmp_path, changed=["src/lex/dx/app.py"])
    _setup_task(conn, agent_id, claimed=["src/lex/dx/app.py", "src/lex/cli.py"])
    conn.commit()

    view = build_dx_view(tmp_path, load_dashboard_state(tmp_path))

    assert len(view.agents) == 1
    assert [item.path for item in view.agents[0].files] == ["src/lex/cli.py", "src/lex/dx/app.py"]
    states = {item.path: item.state for item in view.agents[0].files}
    assert states["src/lex/cli.py"] == "claimed_only"
    assert states["src/lex/dx/app.py"] == "changed_unreviewed"


def test_build_dx_view_marks_conflicted_paths(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    first = _setup_agent(conn, name="codex-brisk-otter")
    second = _setup_agent(conn, name="claude-steady-ibis", kind="claude")
    _setup_session(conn, first, tmp_path)
    _setup_session(conn, second, tmp_path)
    _setup_task(conn, first, claimed=["src/shared.py"])
    _setup_task(conn, second, claimed=["src/shared.py"])
    conn.commit()

    view = build_dx_view(tmp_path, load_dashboard_state(tmp_path))

    assert view.agents[0].files[0].state == "conflicted"
    assert view.agents[0].files[0].conflict is True


def test_build_diff_uses_git_base_ref(tmp_path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True, text=True)
    target = tmp_path / "demo.txt"
    target.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "demo.txt"], cwd=tmp_path, check=True, capture_output=True, text=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, check=True, capture_output=True, text=True)
    target.write_text("base\nchange\n", encoding="utf-8")

    diff = build_diff(tmp_path, "HEAD", "demo.txt")

    assert "diff --git" in diff
    assert "+change" in diff


def test_read_file_contents_handles_directory_claim(tmp_path):
    message = read_file_contents(tmp_path, ".")
    assert message == "Directory claim: ."


def test_dx_pyproject_exposes_console_script():
    with open("pyproject.toml", "rb") as handle:
        data = tomllib.load(handle)

    assert data["project"]["scripts"]["dx"] == "lex.dx.app:main"


def test_dx_main_dispatches_to_runner(tmp_path, monkeypatch):
    called = {}

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)

    def fake_run_dx(root):
        called["root"] = root

    monkeypatch.setattr("lex.dx.app.run_dx", fake_run_dx)

    main(["--root", str(tmp_path)])

    assert called["root"] == tmp_path.resolve()


def test_dx_main_requires_interactive_tty(tmp_path):
    try:
        main(["--root", str(tmp_path)])
    except SystemExit as exc:
        assert "interactive terminal" in str(exc)
    else:
        raise AssertionError("expected dx main to reject non-interactive invocation")


def test_current_actions_match_focus_contract():
    roster_actions = [action.label for action in current_actions("roster", "diff")]
    tab_actions = [action.label for action in current_actions("tabs", "diff")]

    assert "Message Task (m)" in roster_actions
    assert "Task Context (t)" in roster_actions
    assert "Annotate (a)" in tab_actions
    assert "Priority (p)" in tab_actions


def test_dx_update_task_priority_writes_db_and_event(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent_id = _setup_agent(conn)
    _setup_task(conn, agent_id)
    conn.commit()

    dx_update_task_priority(tmp_path, agent_name="codex-brisk-otter", task_id=1, priority=3)

    task = conn.execute("SELECT priority FROM tasks WHERE id = 1").fetchone()
    event = conn.execute("SELECT event_type, payload_json FROM events WHERE event_type = 'task.priority_changed'").fetchone()

    assert task["priority"] == 3
    assert json.loads(event["payload_json"])["to"] == 3


def test_dx_update_task_status_writes_db_and_event(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent_id = _setup_agent(conn)
    _setup_task(conn, agent_id)
    conn.commit()

    dx_update_task_status(tmp_path, agent_name="codex-brisk-otter", task_id=1, status="blocked")

    task = conn.execute("SELECT status FROM tasks WHERE id = 1").fetchone()
    event = conn.execute("SELECT event_type, payload_json FROM events WHERE event_type = 'task.status_changed'").fetchone()

    assert task["status"] == "blocked"
    assert json.loads(event["payload_json"])["to"] == "blocked"


def test_dx_send_message_writes_task_thread_and_event(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent_id = _setup_agent(conn)
    _setup_task(conn, agent_id, claimed=["src/demo.py"])
    conn.commit()

    dx_send_message(tmp_path, from_agent="codex-brisk-otter", task_id=1, body="Please check edge handling")

    message = conn.execute("SELECT task_id, subject, body FROM messages").fetchone()
    event = conn.execute("SELECT event_type, payload_json FROM events WHERE event_type = 'message.sent'").fetchone()

    assert message["task_id"] == 1
    assert "edge handling" in message["body"]
    assert json.loads(event["payload_json"])["provenance"] == "dx"


def test_dx_annotation_and_flag_helpers_write_events(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent_id = _setup_agent(conn)
    _setup_task(conn, agent_id, claimed=["src/demo.py"])
    conn.commit()

    dx_log_annotation(tmp_path, agent_name="codex-brisk-otter", task_id=1, path="src/demo.py:12", note="looks risky")
    dx_flag_file(tmp_path, agent_name="codex-brisk-otter", task_id=1, path="src/demo.py", reason="needs_review")

    rows = conn.execute(
        "SELECT event_type, payload_json FROM events WHERE event_type IN ('dx.annotation', 'dx.flag') ORDER BY id"
    ).fetchall()

    assert [row["event_type"] for row in rows] == ["dx.annotation", "dx.flag"]
    assert json.loads(rows[0]["payload_json"])["provenance"] == "dx"
    assert json.loads(rows[1]["payload_json"])["file"] == "src/demo.py"


def test_dx_enter_from_roster_moves_focus_to_files_without_opening(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent_id = _setup_agent(conn)
    _setup_session(conn, agent_id, tmp_path)
    _setup_task(conn, agent_id, claimed=["."])
    conn.commit()

    tui = DxTui(tmp_path)

    assert tui.focus == "roster"
    assert tui.tabs == []

    tui._open_selected_file()

    assert tui.focus == "files"
    assert tui.tabs == []
    assert tui.status == "select a file claim to open"


def test_dx_quick_edit_rejects_directory_claim(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent_id = _setup_agent(conn)
    _setup_session(conn, agent_id, tmp_path)
    _setup_task(conn, agent_id, claimed=["."])
    conn.commit()

    tui = DxTui(tmp_path)
    tui.focus = "files"

    tui._enter_quick_edit(None)

    assert tui.mode == "diff"
    assert tui.status == "cannot quick-edit directory claim: ."
