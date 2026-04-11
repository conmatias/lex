"""Tests for Git awareness: snapshot capture, session storage, and branch-aware conflict detection."""
import json
import subprocess

import pytest

from lex.cli import capture_git_snapshot, main
from lex.db import connect, detect_path_conflicts, ensure_workspace, initialize_database


def init_workspace(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    return paths, conn


def register_agents(conn):
    conn.execute("INSERT INTO agents (name, kind) VALUES (?, ?)", ("codex-brisk-otter", "codex"))
    conn.execute("INSERT INTO agents (name, kind) VALUES (?, ?)", ("claude-steady-ibis", "claude"))


def _setup_claimed_task_with_session_branch(conn, agent_id, paths, branch):
    """Insert a claimed task with an active lease and an active session with a known branch."""
    conn.execute(
        "INSERT INTO sessions (agent_id, label, status, cwd, capabilities_json, git_branch)"
        " VALUES (?, 'primary', 'active', ?, '{}', ?)",
        (agent_id, str(paths), branch),
    )
    session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO tasks (title, status, owner_agent_id, claimed_paths_json)"
        " VALUES (?, 'claimed', ?, ?)",
        ("Blocking task", agent_id, '["src/lex"]'),
    )
    task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO task_leases (task_id, agent_id, session_id, expires_at)"
        " VALUES (?, ?, ?, datetime('now', '+30 minutes'))",
        (task_id, agent_id, session_id),
    )
    return task_id, session_id


# --- capture_git_snapshot ---

def test_capture_git_snapshot_returns_null_outside_repo(tmp_path):
    snapshot = capture_git_snapshot(str(tmp_path))
    assert snapshot["git_branch"] is None
    assert snapshot["git_base_ref"] is None
    assert snapshot["git_dirty"] is None
    assert snapshot["git_staged_files_json"] is None
    assert snapshot["git_changed_files_json"] is None


def test_capture_git_snapshot_returns_branch_inside_repo(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    snapshot = capture_git_snapshot(str(tmp_path))
    assert snapshot["git_branch"] is not None
    assert snapshot["git_dirty"] == 0
    assert json.loads(snapshot["git_staged_files_json"]) == []


def test_capture_git_snapshot_detects_staged_file(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "base.txt").write_text("base")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "new.txt").write_text("new")
    subprocess.run(["git", "add", "new.txt"], cwd=tmp_path, check=True, capture_output=True)

    snapshot = capture_git_snapshot(str(tmp_path))
    staged = json.loads(snapshot["git_staged_files_json"])
    assert "new.txt" in staged


def test_capture_git_snapshot_detects_dirty_worktree(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "file.txt").write_text("modified")

    snapshot = capture_git_snapshot(str(tmp_path))
    assert snapshot["git_dirty"] == 1


def test_capture_git_snapshot_detects_untracked_file_as_dirty(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)

    (tmp_path / "untracked.txt").write_text("new")
    snapshot = capture_git_snapshot(str(tmp_path))
    assert snapshot["git_dirty"] == 1


# --- session start stores git fields ---

def test_session_start_stores_git_fields(tmp_path):
    paths, conn = init_workspace(tmp_path)
    register_agents(conn)
    conn.commit()

    main(["--root", str(tmp_path), "session", "start", "codex-brisk-otter",
          "--cwd", str(tmp_path), "--label", "test"])

    session = conn.execute("SELECT * FROM sessions ORDER BY id DESC LIMIT 1").fetchone()
    # tmp_path is not a git repo — all git fields should be None
    assert session["git_branch"] is None
    assert session["git_dirty"] is None
    assert session["git_staged_files_json"] is None


# --- heartbeat refreshes dirty/staged ---

def test_heartbeat_updates_git_dirty_and_staged(tmp_path):
    paths, conn = init_workspace(tmp_path)
    register_agents(conn)
    codex_id = conn.execute("SELECT id FROM agents WHERE name = ?", ("codex-brisk-otter",)).fetchone()["id"]
    conn.execute(
        "INSERT INTO sessions (agent_id, label, status, cwd, capabilities_json, git_dirty, git_staged_files_json)"
        " VALUES (?, 'test', 'active', ?, '{}', 1, '[\"old.txt\"]')",
        (codex_id, str(tmp_path)),
    )
    session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()

    main(["--root", str(tmp_path), "session", "heartbeat", str(session_id)])

    session = conn.execute("SELECT git_dirty, git_staged_files_json FROM sessions WHERE id = ?", (session_id,)).fetchone()
    # tmp_path is not a git repo — heartbeat should write None
    assert session["git_dirty"] is None
    assert session["git_staged_files_json"] is None


# --- branch-aware conflict detection ---

def test_same_branch_conflict_is_not_cross_branch(tmp_path):
    _, conn = init_workspace(tmp_path)
    register_agents(conn)
    codex_id = conn.execute("SELECT id FROM agents WHERE name = ?", ("codex-brisk-otter",)).fetchone()["id"]
    _setup_claimed_task_with_session_branch(conn, codex_id, tmp_path, "feature-x")

    conflicts = detect_path_conflicts(conn, ["src/lex/cli.py"], claiming_branch="feature-x")
    assert len(conflicts) == 1
    assert conflicts[0]["cross_branch"] is False


def test_different_branch_conflict_is_cross_branch(tmp_path):
    _, conn = init_workspace(tmp_path)
    register_agents(conn)
    codex_id = conn.execute("SELECT id FROM agents WHERE name = ?", ("codex-brisk-otter",)).fetchone()["id"]
    _setup_claimed_task_with_session_branch(conn, codex_id, tmp_path, "feature-y")

    conflicts = detect_path_conflicts(conn, ["src/lex/cli.py"], claiming_branch="feature-x")
    assert len(conflicts) == 1
    assert conflicts[0]["cross_branch"] is True


def test_strict_blocks_same_branch_conflict(tmp_path):
    _, conn = init_workspace(tmp_path)
    register_agents(conn)
    codex_id = conn.execute("SELECT id FROM agents WHERE name = ?", ("codex-brisk-otter",)).fetchone()["id"]
    claude_id = conn.execute("SELECT id FROM agents WHERE name = ?", ("claude-steady-ibis",)).fetchone()["id"]
    _setup_claimed_task_with_session_branch(conn, codex_id, tmp_path, "main")
    # Create a session for claude on the same branch
    conn.execute(
        "INSERT INTO sessions (agent_id, label, status, cwd, capabilities_json, git_branch)"
        " VALUES (?, 'test', 'active', ?, '{}', 'main')",
        (claude_id, str(tmp_path)),
    )
    conn.execute(
        "INSERT INTO tasks (title, status, owner_agent_id, claimed_paths_json)"
        " VALUES (?, 'open', ?, ?)",
        ("New task", claude_id, '["src/lex/cli.py"]'),
    )
    new_task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()

    with pytest.raises(SystemExit):
        main(["--root", str(tmp_path), "task", "claim", str(new_task_id), "claude-steady-ibis", "--strict"])


def test_strict_does_not_block_cross_branch_conflict(tmp_path, capsys):
    _, conn = init_workspace(tmp_path)
    register_agents(conn)
    codex_id = conn.execute("SELECT id FROM agents WHERE name = ?", ("codex-brisk-otter",)).fetchone()["id"]
    claude_id = conn.execute("SELECT id FROM agents WHERE name = ?", ("claude-steady-ibis",)).fetchone()["id"]
    _setup_claimed_task_with_session_branch(conn, codex_id, tmp_path, "feature-a")
    # Claude is on a different branch
    conn.execute(
        "INSERT INTO sessions (agent_id, label, status, cwd, capabilities_json, git_branch)"
        " VALUES (?, 'test', 'active', ?, '{}', 'feature-b')",
        (claude_id, str(tmp_path)),
    )
    conn.execute(
        "INSERT INTO tasks (title, status, owner_agent_id, claimed_paths_json)"
        " VALUES (?, 'open', ?, ?)",
        ("Cross-branch task", claude_id, '["src/lex/cli.py"]'),
    )
    new_task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()

    # Should not raise even with --strict because it's a cross-branch conflict
    main(["--root", str(tmp_path), "task", "claim", str(new_task_id), "claude-steady-ibis", "--strict"])
    out = capsys.readouterr().out
    assert "cross-branch" in out
