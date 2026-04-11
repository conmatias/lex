from __future__ import annotations

import sqlite3


def get_worker_runtime(conn: sqlite3.Connection, runtime_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
            wr.*,
            wd.name AS worker_name,
            wd.kind AS worker_kind,
            wd.role AS worker_role,
            wd.specialty AS worker_specialty,
            wd.env_json
        FROM worker_runtimes wr
        JOIN worker_definitions wd ON wd.id = wr.worker_id
        WHERE wr.id = ?
        """,
        (runtime_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"unknown worker runtime: {runtime_id}")
    return row


def get_latest_routable_runtime(conn: sqlite3.Connection, worker_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT
            wr.*,
            wd.name AS worker_name,
            wd.kind AS worker_kind,
            wd.role AS worker_role,
            wd.specialty AS worker_specialty,
            wd.env_json
        FROM worker_runtimes wr
        JOIN worker_definitions wd ON wd.id = wr.worker_id
        WHERE wr.worker_id = ? AND wr.status IN ('approved', 'launching', 'running')
        ORDER BY wr.id DESC
        LIMIT 1
        """,
        (worker_id,),
    ).fetchone()


def mark_runtime_launching(conn: sqlite3.Connection, runtime_id: int) -> None:
    conn.execute(
        "UPDATE worker_runtimes SET status = 'launching', heartbeat_at = CURRENT_TIMESTAMP WHERE id = ?",
        (runtime_id,),
    )


def set_runtime_supervisor_pid(conn: sqlite3.Connection, runtime_id: int, supervisor_pid: int) -> None:
    conn.execute(
        "UPDATE worker_runtimes SET supervisor_pid = ? WHERE id = ?",
        (supervisor_pid, runtime_id),
    )


def mark_runtime_stopped(conn: sqlite3.Connection, runtime_id: int) -> None:
    conn.execute(
        """
        UPDATE worker_runtimes
        SET status = 'stopped',
            heartbeat_at = CURRENT_TIMESTAMP,
            ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP)
        WHERE id = ?
        """,
        (runtime_id,),
    )


def get_dispatch_packet(conn: sqlite3.Connection, packet_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
            dp.*,
            sender.name AS from_agent_name,
            wd.name AS worker_name,
            wr.status AS runtime_status
        FROM dispatch_packets dp
        JOIN agents sender ON sender.id = dp.from_agent_id
        LEFT JOIN worker_definitions wd ON wd.id = dp.to_worker_id
        LEFT JOIN worker_runtimes wr ON wr.id = dp.runtime_id
        WHERE dp.id = ?
        """,
        (packet_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"unknown dispatch packet: {packet_id}")
    return row


def mark_packet_delivered(conn: sqlite3.Connection, *, packet_id: int, runtime_id: int, packet_path: str) -> None:
    conn.execute(
        """
        UPDATE dispatch_packets
        SET runtime_id = ?, delivery_path = ?, delivery_status = 'delivered', delivered_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (runtime_id, packet_path, packet_id),
    )


def stale_runtime_candidates(conn: sqlite3.Connection, *, stale_minutes: int) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT
            wr.id,
            wr.task_id,
            wr.requested_by_agent_id,
            wr.status,
            wr.pid,
            wr.supervisor_pid,
            wr.child_pid,
            wd.name AS worker_name
        FROM worker_runtimes wr
        JOIN worker_definitions wd ON wd.id = wr.worker_id
        WHERE wr.status IN ('launching', 'running')
          AND wr.heartbeat_at < datetime('now', ?)
        ORDER BY wr.id ASC
        """,
        (f"-{stale_minutes} minutes",),
    ).fetchall()
    return list(rows)


def mark_runtime_failed(conn: sqlite3.Connection, runtime_id: int) -> None:
    conn.execute(
        """
        UPDATE worker_runtimes
        SET status = 'failed',
            ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP)
        WHERE id = ?
        """,
        (runtime_id,),
    )


def list_blocked_packets(conn: sqlite3.Connection, runtime_id: int) -> list[sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT id, task_id
        FROM dispatch_packets
        WHERE runtime_id = ?
          AND delivery_status IN ('delivered', 'acknowledged')
        ORDER BY id ASC
        """,
        (runtime_id,),
    ).fetchall()
    return list(rows)


def fail_blocked_packets(conn: sqlite3.Connection, *, runtime_id: int, note: str) -> None:
    conn.execute(
        """
        UPDATE dispatch_packets
        SET delivery_status = 'failed',
            completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP),
            completion_note = ?
        WHERE runtime_id = ?
          AND delivery_status IN ('delivered', 'acknowledged')
        """,
        (note, runtime_id),
    )


def load_runtime_execution(conn: sqlite3.Connection, runtime_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
            wr.id,
            wr.worker_id,
            wr.task_id,
            wr.requested_by_agent_id,
            wr.status,
            wr.command_json,
            wr.cwd,
            wr.inbox_path,
            wr.log_path,
            wr.error_path,
            wd.name AS worker_name,
            wd.kind AS worker_kind,
            wd.env_json
        FROM worker_runtimes wr
        JOIN worker_definitions wd ON wd.id = wr.worker_id
        WHERE wr.id = ?
        """,
        (runtime_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"unknown worker runtime: {runtime_id}")
    return row


def record_runtime_process_context(
    conn: sqlite3.Connection,
    *,
    runtime_id: int,
    pid: int,
    cwd: str,
    inbox_path: str,
    stdout_path: str,
    stderr_path: str,
) -> None:
    conn.execute(
        """
        UPDATE worker_runtimes
        SET pid = ?,
            supervisor_pid = ?,
            cwd = ?,
            inbox_path = ?,
            log_path = ?,
            error_path = ?,
            heartbeat_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (pid, pid, cwd, inbox_path, stdout_path, stderr_path, runtime_id),
    )


def mark_runtime_running(conn: sqlite3.Connection, *, runtime_id: int, child_pid: int) -> None:
    conn.execute(
        """
        UPDATE worker_runtimes
        SET status = 'running',
            child_pid = ?,
            heartbeat_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (child_pid, runtime_id),
    )


def touch_runtime_heartbeat(conn: sqlite3.Connection, runtime_id: int) -> None:
    conn.execute(
        "UPDATE worker_runtimes SET heartbeat_at = CURRENT_TIMESTAMP WHERE id = ?",
        (runtime_id,),
    )


def mark_runtime_finished(
    conn: sqlite3.Connection,
    *,
    runtime_id: int,
    status: str,
    exit_code: int | None = None,
    ended: bool = False,
) -> None:
    params: list[object] = [status]
    query = """
        UPDATE worker_runtimes
        SET status = ?,
            heartbeat_at = CURRENT_TIMESTAMP
    """
    if exit_code is not None:
        query += ", exit_code = ?"
        params.append(exit_code)
    if ended:
        query += ", ended_at = CURRENT_TIMESTAMP"
    query += " WHERE id = ?"
    params.append(runtime_id)
    conn.execute(query, tuple(params))
