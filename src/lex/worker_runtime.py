from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

from lex.db import connect, initialize_database, resolve_paths
<<<<<<< HEAD
from lex.dispatch import worker_runtime_dir


def _load_runtime(conn: sqlite3.Connection, runtime_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
            wr.id,
            wr.worker_id,
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


def _update_runtime(conn: sqlite3.Connection, runtime_id: int, *, status: str, exit_code: int | None = None, ended: bool = False) -> None:
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
=======
from lex.discovery import LexDiscovery
from lex.runtime.inbox import worker_runtime_dir
from lex.runtime.repository import (
    load_runtime_execution,
    mark_runtime_finished,
    mark_runtime_running,
    record_runtime_process_context,
    touch_runtime_heartbeat,
)
>>>>>>> 8ed20f4 (refactor(core): split cli.py into modular commands and services, improve worker runtime and git awareness)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="lex.worker_runtime")
    parser.add_argument("--root", default=".")
    parser.add_argument("runtime_id", type=int)
    args = parser.parse_args(argv)

    paths = resolve_paths(Path(args.root).resolve())
    conn = connect(paths.db_path)
    initialize_database(conn)
    runtime = load_runtime_execution(conn, args.runtime_id)
    runtime_dir = worker_runtime_dir(paths, args.runtime_id)
    inbox_path = Path(runtime["inbox_path"] or runtime_dir / "inbox")
    inbox_path.mkdir(parents=True, exist_ok=True)
    stdout_path = Path(runtime["log_path"] or runtime_dir / "stdout.log")
    stderr_path = Path(runtime["error_path"] or runtime_dir / "stderr.log")
    env = os.environ.copy()
    env.update(json.loads(runtime["env_json"] or "{}"))
    env["LEX_ROOT"] = str(paths.root)
    env["LEX_DB_PATH"] = str(paths.db_path)
    env["LEX_WORKER_RUNTIME_ID"] = str(args.runtime_id)
    env["LEX_WORKER_NAME"] = runtime["worker_name"]
    env["LEX_WORKER_KIND"] = runtime["worker_kind"]
    env["LEX_WORKER_INBOX"] = str(inbox_path)

    command = json.loads(runtime["command_json"])
    cwd = runtime["cwd"] or str(paths.root)

    record_runtime_process_context(
        conn,
        runtime_id=args.runtime_id,
        pid=os.getpid(),
        cwd=cwd,
        inbox_path=str(inbox_path),
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
    )
    conn.commit()

    with stdout_path.open("ab") as stdout_handle, stderr_path.open("ab") as stderr_handle:
<<<<<<< HEAD
        child = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
            close_fds=True,
        )
        conn.execute(
            "UPDATE worker_runtimes SET child_pid = ?, heartbeat_at = CURRENT_TIMESTAMP WHERE id = ?",
            (child.pid, args.runtime_id),
        )
        conn.commit()

        exit_code: int | None = None
        while exit_code is None:
            exit_code = child.poll()
            conn.execute(
                "UPDATE worker_runtimes SET heartbeat_at = CURRENT_TIMESTAMP WHERE id = ?",
                (args.runtime_id,),
            )
            conn.commit()
            if exit_code is None:
                time.sleep(1.0)
=======
        try:
            try:
                child = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    start_new_session=True,
                    close_fds=True,
                )
            except Exception as exc:
                mark_runtime_finished(conn, runtime_id=args.runtime_id, status="failed", exit_code=-1, ended=True)
                conn.execute(
                    """
                    INSERT INTO events (event_type, task_id, agent_id, session_id, payload_json)
                    SELECT 'worker.runtime_finished', wr.task_id, wr.requested_by_agent_id, NULL,
                           json_object(
                               'runtime_id', wr.id,
                               'worker_name', wd.name,
                               'status', 'failed',
                               'exit_code', -1,
                               'reason', 'spawn_failed',
                               'error', ?
                           )
                    FROM worker_runtimes wr
                    JOIN worker_definitions wd ON wd.id = wr.worker_id
                    WHERE wr.id = ?
                    """,
                    (str(exc), args.runtime_id),
                )
                conn.commit()
                return
            mark_runtime_running(conn, runtime_id=args.runtime_id, child_pid=child.pid)
            conn.execute(
                """
                INSERT INTO events (event_type, task_id, agent_id, session_id, payload_json)
                SELECT 'worker.runtime_started', wr.task_id, wr.requested_by_agent_id, NULL,
                       json_object('runtime_id', wr.id, 'worker_name', wd.name, 'cwd', ?)
                FROM worker_runtimes wr
                JOIN worker_definitions wd ON wd.id = wr.worker_id
                WHERE wr.id = ?
                """,
                (cwd, args.runtime_id),
            )
            conn.commit()

            exit_code: int | None = None
            while exit_code is None:
                exit_code = child.poll()
                touch_runtime_heartbeat(conn, args.runtime_id)
                conn.commit()
                if exit_code is None:
                    time.sleep(1.0)
        finally:
            if discovery:
                discovery.stop()
>>>>>>> 8ed20f4 (refactor(core): split cli.py into modular commands and services, improve worker runtime and git awareness)

    final_status = "exited" if exit_code == 0 else "failed"
    mark_runtime_finished(conn, runtime_id=args.runtime_id, status=final_status, exit_code=exit_code, ended=True)
    conn.execute(
        """
        INSERT INTO events (event_type, task_id, agent_id, session_id, payload_json)
        SELECT 'worker.runtime_finished', wr.task_id, wr.requested_by_agent_id, NULL,
               json_object('runtime_id', wr.id, 'worker_name', wd.name, 'status', ?, 'exit_code', ?)
        FROM worker_runtimes wr
        JOIN worker_definitions wd ON wd.id = wr.worker_id
        WHERE wr.id = ?
        """,
        (final_status, exit_code, args.runtime_id),
    )
    conn.commit()


if __name__ == "__main__":
    main()
