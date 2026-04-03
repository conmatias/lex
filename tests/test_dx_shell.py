import json
import subprocess

from lex.dashboard import load_dashboard_state
from lex.db import connect, ensure_workspace, initialize_database
from lex.dx.app import build_diff, build_dx_view


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
