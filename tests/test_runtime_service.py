import json
from pathlib import Path

from lex.db import connect, ensure_workspace, initialize_database
from lex.runtime.service import cleanup_stale_worker_runtimes, deliver_packet, start_runtime, stop_runtime


def init_workspace(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    return paths, conn


def register_pm(conn):
    conn.execute(
        "INSERT INTO agents (name, kind, role, specialty, status) VALUES (?, 'codex', 'pm', '', 'active')",
        ("codex-pm-dalton",),
    )
    conn.commit()


def test_start_and_stop_runtime_uses_runtime_service_boundary(tmp_path, monkeypatch):
    paths, conn = init_workspace(tmp_path)
    register_pm(conn)
    conn.execute(
        """
        INSERT INTO worker_definitions (name, kind, role, specialty, command_json, approval_policy, created_by_agent_id)
        VALUES ('codex-dev', 'codex', 'dev', '', '[]', 'never', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO worker_runtimes (
            worker_id, task_id, requested_by_agent_id, reason, approval_required,
            approval_status, status, started_at, heartbeat_at
        )
        VALUES (1, NULL, 1, 'test', 0, 'not_required', 'approved', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    conn.commit()

    class DummyProc:
        pid = 4242

    monkeypatch.setattr("lex.runtime.service.launch_worker_supervisor", lambda *_args, **_kwargs: DummyProc())
    start_runtime(conn, paths=paths, runtime_id=1)
    stop_runtime(conn, runtime_id=1, signal_name="TERM")
    conn.commit()

    runtime = conn.execute("SELECT status, supervisor_pid, ended_at FROM worker_runtimes WHERE id = 1").fetchone()
    assert runtime["supervisor_pid"] == 4242
    assert runtime["status"] == "stopped"
    assert runtime["ended_at"] is not None


def test_cleanup_stale_worker_runtimes_fails_packets(tmp_path):
    _, conn = init_workspace(tmp_path)
    register_pm(conn)
    conn.execute("INSERT INTO tasks (title, status, priority, owner_agent_id, delegation_mode) VALUES ('Dispatch task', 'claimed', 2, 1, 'direct')")
    conn.execute(
        """
        INSERT INTO worker_definitions (name, kind, role, specialty, command_json, approval_policy, created_by_agent_id)
        VALUES ('codex-dev', 'codex', 'dev', '', '[]', 'never', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO worker_runtimes (
            worker_id, task_id, requested_by_agent_id, reason, approval_required,
            approval_status, status, started_at, heartbeat_at
        )
        VALUES (1, 1, 1, 'stale runtime', 0, 'not_required', 'running', CURRENT_TIMESTAMP, datetime('now', '-10 minutes'))
        """
    )
    conn.execute(
        """
        INSERT INTO dispatch_packets (
            task_id, runtime_id, to_worker_id, from_agent_id, packet_json, approval_status, delivery_status, delivered_at
        )
        VALUES (1, 1, 1, 1, '{}', 'approved', 'delivered', CURRENT_TIMESTAMP)
        """
    )
    conn.commit()

    cleaned = cleanup_stale_worker_runtimes(conn, stale_minutes=1)
    conn.commit()

    runtime = conn.execute("SELECT status FROM worker_runtimes WHERE id = 1").fetchone()
    packet = conn.execute("SELECT delivery_status, completion_note FROM dispatch_packets WHERE id = 1").fetchone()
    assert cleaned and cleaned[0]["packet_count"] == 1
    assert runtime["status"] == "failed"
    assert packet["delivery_status"] == "failed"
    assert "stale heartbeat" in packet["completion_note"]


def test_deliver_packet_routes_to_latest_runtime_inbox(tmp_path):
    paths, conn = init_workspace(tmp_path)
    register_pm(conn)
    conn.execute("INSERT INTO tasks (title, status, priority, owner_agent_id, delegation_mode) VALUES ('Dispatch task', 'claimed', 2, 1, 'direct')")
    conn.execute(
        """
        INSERT INTO worker_definitions (name, kind, role, specialty, command_json, approval_policy, created_by_agent_id)
        VALUES ('codex-dev', 'codex', 'dev', '', '[]', 'never', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO worker_runtimes (
            worker_id, task_id, requested_by_agent_id, reason, approval_required,
            approval_status, status, started_at, heartbeat_at
        )
        VALUES (1, 1, 1, 'runtime one', 0, 'not_required', 'running', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    conn.execute(
        """
        INSERT INTO worker_runtimes (
            worker_id, task_id, requested_by_agent_id, reason, approval_required,
            approval_status, status, started_at, heartbeat_at
        )
        VALUES (1, 1, 1, 'runtime two', 0, 'not_required', 'running', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """
    )
    conn.execute(
        """
        INSERT INTO dispatch_packets (
            task_id, to_worker_id, from_agent_id, packet_json, sensitive_action, requires_human_approval, approval_status, delivery_status
        )
        VALUES (1, 1, 1, ?, '', 0, 'not_required', 'ready')
        """,
        (json.dumps({"summary": "route packet", "body": "", "artifacts": [], "metadata": {}}),),
    )
    conn.commit()

    runtime_id, path = deliver_packet(conn, paths=paths, packet_id=1)
    conn.commit()

    packet = conn.execute("SELECT runtime_id, delivery_status, delivery_path FROM dispatch_packets WHERE id = 1").fetchone()
    payload = json.loads(Path(path).read_text())
    assert runtime_id == 2
    assert packet["runtime_id"] == 2
    assert packet["delivery_status"] == "delivered"
    assert packet["delivery_path"] == path
    assert payload["id"] == 1
