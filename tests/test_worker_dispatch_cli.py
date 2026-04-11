import json
import sys
import time
from pathlib import Path

from lex.cli import main
from lex.db import connect, ensure_workspace, initialize_database


def init_workspace(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)
    return paths, conn


def register_pm_agent(conn):
    conn.execute(
        "INSERT INTO agents (name, kind, role, specialty, status) VALUES (?, 'codex', 'pm', '', 'active')",
        ("codex-pm-dalton",),
    )
    conn.commit()


def wait_for_runtime_status(conn, runtime_id: int, allowed: set[str], timeout: float = 8.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        row = conn.execute("SELECT * FROM worker_runtimes WHERE id = ?", (runtime_id,)).fetchone()
        if row is not None and row["status"] in allowed:
            return dict(row)
        time.sleep(0.2)
    row = conn.execute("SELECT * FROM worker_runtimes WHERE id = ?", (runtime_id,)).fetchone()
    assert row is not None
    raise AssertionError(f"runtime {runtime_id} did not reach {allowed}; last status={row['status']}")


def test_session_list_renders_role_columns(tmp_path, capsys):
    _, conn = init_workspace(tmp_path)
    register_pm_agent(conn)
    agent_id = conn.execute("SELECT id FROM agents WHERE name = 'codex-pm-dalton'").fetchone()["id"]
    conn.execute(
        """
        INSERT INTO sessions (agent_id, label, status, cwd, capabilities_json)
        VALUES (?, 'primary', 'active', ?, '{}')
        """,
        (agent_id, str(tmp_path)),
    )
    conn.commit()

    main(["--root", str(tmp_path), "session", "list", "--active-only"])
    out = capsys.readouterr().out

    assert "codex-pm-dalton" in out
    assert "pm" in out


def test_worker_runtime_supervisor_tracks_execution(tmp_path):
    _, conn = init_workspace(tmp_path)
    register_pm_agent(conn)
    command_json = json.dumps(
        [
            sys.executable,
            "-c",
            (
                "import os,time,pathlib;"
                "pathlib.Path(os.environ['LEX_WORKER_INBOX']).joinpath('ready.txt').write_text('ok');"
                "time.sleep(0.3)"
            ),
        ]
    )

    main(
        [
            "--root",
            str(tmp_path),
            "worker",
            "register",
            "codex-dispatch-dev",
            "codex",
            "--role",
            "dev",
            "--command-json",
            command_json,
            "--approval-policy",
            "always",
            "--created-by",
            "codex-pm-dalton",
        ]
    )
    main(
        [
            "--root",
            str(tmp_path),
            "worker",
            "request-start",
            "codex-dispatch-dev",
            "--requested-by",
            "codex-pm-dalton",
            "--reason",
            "dispatch foundation test",
            "--approved-by",
            "human",
        ]
    )

    runtime_id = conn.execute("SELECT id FROM worker_runtimes ORDER BY id DESC LIMIT 1").fetchone()["id"]
    main(["--root", str(tmp_path), "worker", "start", str(runtime_id)])
    runtime = wait_for_runtime_status(conn, runtime_id, {"exited", "failed"})

    runtime_dir = Path(tmp_path) / ".lex" / "runtime" / "workers" / f"runtime-{runtime_id}"
    assert runtime["status"] == "exited"
    assert runtime["exit_code"] == 0
    assert runtime["child_pid"] is not None
    assert (runtime_dir / "inbox" / "ready.txt").read_text() == "ok"
    assert (runtime_dir / "stdout.log").exists()


def test_worker_runtime_spawn_failure_marks_runtime_failed(tmp_path):
    _, conn = init_workspace(tmp_path)
    register_pm_agent(conn)
    command_json = json.dumps(["definitely-not-a-real-binary"])

    main(
        [
            "--root",
            str(tmp_path),
            "worker",
            "register",
            "codex-bad-command",
            "codex",
            "--role",
            "dev",
            "--command-json",
            command_json,
            "--approval-policy",
            "always",
            "--created-by",
            "codex-pm-dalton",
        ]
    )
    main(
        [
            "--root",
            str(tmp_path),
            "worker",
            "request-start",
            "codex-bad-command",
            "--requested-by",
            "codex-pm-dalton",
            "--reason",
            "spawn failure test",
            "--approved-by",
            "human",
        ]
    )

    runtime_id = conn.execute("SELECT id FROM worker_runtimes ORDER BY id DESC LIMIT 1").fetchone()["id"]
    main(["--root", str(tmp_path), "worker", "start", str(runtime_id)])
    runtime = wait_for_runtime_status(conn, runtime_id, {"failed"})

    event = conn.execute(
        "SELECT event_type, payload_json FROM events WHERE event_type = 'worker.runtime_finished' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert runtime["status"] == "failed"
    assert runtime["ended_at"] is not None
    assert event["event_type"] == "worker.runtime_finished"
    assert json.loads(event["payload_json"])["status"] == "failed"


def test_dispatch_packet_lifecycle_writes_worker_inbox(tmp_path):
    _, conn = init_workspace(tmp_path)
    register_pm_agent(conn)
    conn.execute(
        """
        INSERT INTO tasks (title, description, status, priority, delegation_mode)
        VALUES ('Dispatch task', '', 'open', 2, 'direct')
        """
    )
    conn.commit()
    task_id = conn.execute("SELECT id FROM tasks ORDER BY id DESC LIMIT 1").fetchone()["id"]

    command_json = json.dumps([sys.executable, "-c", "import time; time.sleep(2.0)"])
    main(
        [
            "--root",
            str(tmp_path),
            "worker",
            "register",
            "codex-long-dev",
            "codex",
            "--role",
            "dev",
            "--command-json",
            command_json,
            "--approval-policy",
            "never",
            "--created-by",
            "codex-pm-dalton",
        ]
    )
    main(
        [
            "--root",
            str(tmp_path),
            "worker",
            "request-start",
            "codex-long-dev",
            "--requested-by",
            "codex-pm-dalton",
            "--task-id",
            str(task_id),
            "--reason",
            "run packet delivery test",
        ]
    )
    runtime_id = conn.execute("SELECT id FROM worker_runtimes ORDER BY id DESC LIMIT 1").fetchone()["id"]
    main(["--root", str(tmp_path), "worker", "start", str(runtime_id)])
    wait_for_runtime_status(conn, runtime_id, {"running", "exited", "failed"})

    main(
        [
            "--root",
            str(tmp_path),
            "dispatch",
            "create",
            "--task-id",
            str(task_id),
            "--from",
            "codex-pm-dalton",
            "--to-worker",
            "codex-long-dev",
            "--summary",
            "Implement worker inbox read",
            "--body",
            "Consume packets from the runtime inbox and report status back into Lex.",
            "--artifact",
            "docs/spec.md",
            "--metadata-json",
            '{"priority":"p1"}',
            "--require-approval",
        ]
    )
    packet_id = conn.execute("SELECT id FROM dispatch_packets ORDER BY id DESC LIMIT 1").fetchone()["id"]
    main(
        [
            "--root",
            str(tmp_path),
            "dispatch",
            "approve",
            str(packet_id),
            "approved",
            "--approved-by",
            "human",
        ]
    )
    main(
        [
            "--root",
            str(tmp_path),
            "dispatch",
            "send",
            str(packet_id),
            "--runtime-id",
            str(runtime_id),
        ]
    )
    main(
        [
            "--root",
            str(tmp_path),
            "dispatch",
            "ack",
            str(packet_id),
            "--runtime-id",
            str(runtime_id),
            "--note",
            "accepted",
        ]
    )
    main(
        [
            "--root",
            str(tmp_path),
            "dispatch",
            "complete",
            str(packet_id),
            "completed",
            "--note",
            "worker finished",
        ]
    )

    packet = conn.execute("SELECT * FROM dispatch_packets WHERE id = ?", (packet_id,)).fetchone()
    delivered_path = Path(packet["delivery_path"])
    payload = json.loads(delivered_path.read_text())

    assert packet["approval_status"] == "approved"
    assert packet["delivery_status"] == "completed"
    assert packet["runtime_id"] == runtime_id
    assert payload["packet"]["summary"] == "Implement worker inbox read"
    assert payload["packet"]["artifacts"] == ["docs/spec.md"]
    assert payload["packet"]["metadata"]["priority"] == "p1"


def test_worker_cleanup_marks_stale_runtime_failed(tmp_path):
    _, conn = init_workspace(tmp_path)
    register_pm_agent(conn)
    conn.execute(
        """
        INSERT INTO worker_definitions (name, kind, role, specialty, command_json, approval_policy, created_by_agent_id)
        VALUES ('codex-dispatch-dev', 'codex', 'dev', '', '[]', 'never', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO worker_runtimes (
            worker_id, task_id, requested_by_agent_id, reason, approval_required,
            approval_status, status, started_at, heartbeat_at
        )
        VALUES (1, NULL, 1, 'stale runtime', 0, 'not_required', 'running', CURRENT_TIMESTAMP, datetime('now', '-10 minutes'))
        """
    )
    conn.commit()

    main(["--root", str(tmp_path), "worker", "cleanup", "--stale-minutes", "1"])

    runtime = conn.execute("SELECT status, ended_at FROM worker_runtimes WHERE id = 1").fetchone()
    event = conn.execute(
        "SELECT event_type FROM events WHERE event_type = 'worker.runtime_failed' ORDER BY id DESC LIMIT 1"
    ).fetchone()

    assert runtime["status"] == "failed"
    assert runtime["ended_at"] is not None
    assert event["event_type"] == "worker.runtime_failed"


def test_worker_cleanup_fails_delivered_packets_for_stale_runtime(tmp_path):
    _, conn = init_workspace(tmp_path)
    register_pm_agent(conn)
    conn.execute("INSERT INTO tasks (title, status, priority, owner_agent_id, delegation_mode) VALUES ('Dispatch task', 'claimed', 2, 1, 'direct')")
    conn.execute(
        """
        INSERT INTO worker_definitions (name, kind, role, specialty, command_json, approval_policy, created_by_agent_id)
        VALUES ('codex-dispatch-dev', 'codex', 'dev', '', '[]', 'never', 1)
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

    main(["--root", str(tmp_path), "worker", "cleanup", "--stale-minutes", "1"])

    packet = conn.execute(
        "SELECT delivery_status, completed_at, completion_note FROM dispatch_packets WHERE id = 1"
    ).fetchone()
    event = conn.execute(
        "SELECT event_type FROM events WHERE event_type = 'dispatch.packet_completed' ORDER BY id DESC LIMIT 1"
    ).fetchone()

    assert packet["delivery_status"] == "failed"
    assert packet["completed_at"] is not None
    assert "stale heartbeat" in packet["completion_note"]
    assert event["event_type"] == "dispatch.packet_completed"
