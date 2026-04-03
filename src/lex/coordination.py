from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from lex.db import fetch_one, log_event, run_roster_preflight
from lex.role_contracts import get_role_contract

SESSION_STALE_MINUTES = 15
WORKER_RUNTIME_STALE_MINUTES = 2


def get_agent(conn: sqlite3.Connection, name: str):
    row = fetch_one(conn, "SELECT * FROM agents WHERE name = ?", (name,))
    if row is None:
        raise SystemExit(f"unknown agent: {name}")
    return row


def get_task(conn: sqlite3.Connection, task_id: int):
    row = fetch_one(conn, "SELECT * FROM tasks WHERE id = ?", (task_id,))
    if row is None:
        raise SystemExit(f"unknown task: {task_id}")
    return row


def get_task_with_owner(conn: sqlite3.Connection, task_id: int):
    row = fetch_one(
        conn,
        """
        SELECT
            t.*,
            a.name AS owner_name,
            a.role AS owner_role,
            a.specialty AS owner_specialty,
            creator.name AS created_by_name
        FROM tasks t
        LEFT JOIN agents a ON a.id = t.owner_agent_id
        LEFT JOIN agents creator ON creator.id = t.created_by_agent_id
        WHERE t.id = ?
        """,
        (task_id,),
    )
    if row is None:
        raise SystemExit(f"unknown task: {task_id}")
    return row


def get_active_lease(conn: sqlite3.Connection, task_id: int):
    return fetch_one(
        conn,
        """
        SELECT * FROM task_leases
        WHERE task_id = ? AND state = 'active' AND released_at IS NULL
          AND expires_at > CURRENT_TIMESTAMP
        ORDER BY id DESC
        LIMIT 1
        """,
        (task_id,),
    )


def get_active_lease_for_session(conn: sqlite3.Connection, session_id: int):
    return fetch_one(
        conn,
        """
        SELECT * FROM task_leases
        WHERE session_id = ? AND state = 'active' AND released_at IS NULL
          AND expires_at > CURRENT_TIMESTAMP
        ORDER BY id DESC
        LIMIT 1
        """,
        (session_id,),
    )


def get_session(conn: sqlite3.Connection, session_id: int):
    row = fetch_one(
        conn,
        """
        SELECT
            s.*,
            a.name AS agent_name,
            a.kind AS agent_kind
        FROM sessions s
        JOIN agents a ON a.id = s.agent_id
        WHERE s.id = ?
        """,
        (session_id,),
    )
    if row is None:
        raise SystemExit(f"unknown session: {session_id}")
    return row


def get_active_session_for_agent(conn: sqlite3.Connection, agent_id: int):
    return fetch_one(
        conn,
        """
        SELECT
            s.*,
            a.name AS agent_name,
            a.kind AS agent_kind
        FROM sessions s
        JOIN agents a ON a.id = s.agent_id
        WHERE s.agent_id = ? AND s.status = 'active' AND s.ended_at IS NULL
        ORDER BY s.id DESC
        LIMIT 1
        """,
        (agent_id,),
    )


def get_latest_session_for_agent(conn: sqlite3.Connection, agent_id: int):
    return fetch_one(
        conn,
        """
        SELECT
            s.*,
            a.name AS agent_name,
            a.kind AS agent_kind
        FROM sessions s
        JOIN agents a ON a.id = s.agent_id
        WHERE s.agent_id = ?
        ORDER BY s.id DESC
        LIMIT 1
        """,
        (agent_id,),
    )


def get_session_bootstrap(conn: sqlite3.Connection, session_id: int):
    row = fetch_one(
        conn,
        """
        SELECT *
        FROM session_bootstraps
        WHERE session_id = ?
        """,
        (session_id,),
    )
    if row is not None:
        return row
    session = get_session(conn, session_id)
    agent = get_agent(conn, session["agent_name"])
    try:
        create_session_bootstrap(conn, session_id=session_id, agent=agent)
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
    row = fetch_one(
        conn,
        """
        SELECT *
        FROM session_bootstraps
        WHERE session_id = ?
        """,
        (session_id,),
    )
    if row is None:
        raise SystemExit(f"missing session bootstrap for session: {session_id}")
    return row


def get_active_bootstrap_for_agent(conn: sqlite3.Connection, agent_id: int):
    return fetch_one(
        conn,
        """
        SELECT sb.*
        FROM session_bootstraps sb
        JOIN sessions s ON s.id = sb.session_id
        WHERE sb.agent_id = ?
          AND s.status = 'active'
          AND s.ended_at IS NULL
        ORDER BY sb.id DESC
        LIMIT 1
        """,
        (agent_id,),
    )


def get_worker_definition(conn: sqlite3.Connection, name: str):
    row = fetch_one(conn, "SELECT * FROM worker_definitions WHERE name = ?", (name,))
    if row is None:
        raise SystemExit(f"unknown worker definition: {name}")
    return row


def get_worker_runtime(conn: sqlite3.Connection, runtime_id: int):
    row = fetch_one(
        conn,
        """
        SELECT
            wr.*,
            wd.name AS worker_name,
            wd.kind AS worker_kind,
            wd.role AS worker_role,
            wd.specialty AS worker_specialty
        FROM worker_runtimes wr
        JOIN worker_definitions wd ON wd.id = wr.worker_id
        WHERE wr.id = ?
        """,
        (runtime_id,),
    )
    if row is None:
        raise SystemExit(f"unknown worker runtime: {runtime_id}")
    return row


def get_dispatch_packet(conn: sqlite3.Connection, packet_id: int):
    row = fetch_one(
        conn,
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
    )
    if row is None:
        raise SystemExit(f"unknown dispatch packet: {packet_id}")
    return row


def _fetch_bootstrap_memory(conn: sqlite3.Connection, agent_id: int) -> dict:
    owned_tasks = [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, title, status, priority
            FROM tasks
            WHERE owner_agent_id = ?
              AND status IN ('claimed', 'in_progress', 'blocked', 'review_requested', 'handoff_pending', 'open')
            ORDER BY id DESC
            LIMIT 8
            """,
            (agent_id,),
        ).fetchall()
    ]
    watched_tasks = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                w.task_id,
                t.title,
                w.last_sent_event_id,
                w.last_ack_event_id
            FROM watches w
            JOIN tasks t ON t.id = w.task_id
            WHERE w.agent_id = ?
            ORDER BY w.id DESC
            LIMIT 8
            """,
            (agent_id,),
        ).fetchall()
    ]
    inbox = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                m.id,
                m.task_id,
                sender.name AS from_name,
                m.type,
                m.subject,
                m.body,
                m.created_at
            FROM messages m
            JOIN agents sender ON sender.id = m.from_agent_id
            WHERE m.to_agent_id = ?
               OR (
                    m.to_agent_id IS NULL
                    AND m.task_id IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM tasks t WHERE t.id = m.task_id AND t.owner_agent_id = ?
                    )
               )
            ORDER BY m.id DESC
            LIMIT 8
            """,
            (agent_id, agent_id),
        ).fetchall()
    ]
    recent_decisions = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                e.id,
                e.event_type,
                e.task_id,
                e.created_at,
                e.payload_json
            FROM events e
            WHERE e.event_type IN ('message.sent', 'task.delegated', 'task.priority_changed', 'task.handoff', 'dispatch.packet_completed')
            ORDER BY e.id DESC
            LIMIT 8
            """
        ).fetchall()
    ]
    return {
        "active_tasks": owned_tasks,
        "subscriptions": watched_tasks,
        "inbox": inbox,
        "recent_decisions": recent_decisions,
    }


def create_session_bootstrap(conn: sqlite3.Connection, *, session_id: int, agent) -> None:
    contract = get_role_contract(agent["role"])
    previous_session = fetch_one(
        conn,
        """
        SELECT id
        FROM sessions
        WHERE agent_id = ? AND id != ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (agent["id"], session_id),
    )
    memory = _fetch_bootstrap_memory(conn, agent["id"])
    role_contract = {
        "role": contract.role if contract else agent["role"],
        "allowed_verbs": list(contract.allowed_verbs) if contract else [],
        "blocked_verbs": list(contract.blocked_verbs) if contract else [],
        "required_first_actions": list(contract.required_first_actions) if contract else [],
    }
    conn.execute(
        """
        INSERT INTO session_bootstraps (
            session_id, agent_id, continuity_from_session_id, role_contract_json, memory_json,
            system_prompt, workflow_template_json, required_actions_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            agent["id"],
            previous_session["id"] if previous_session else None,
            json.dumps(role_contract),
            json.dumps(memory),
            contract.system_prompt if contract else "",
            json.dumps(list(contract.workflow_template) if contract else []),
            json.dumps(list(contract.required_first_actions) if contract else []),
        ),
    )
    log_event(
        conn,
        "session.bootstrap_generated",
        agent_id=agent["id"],
        session_id=session_id,
        payload={
            "continuity_from_session_id": previous_session["id"] if previous_session else None,
            "role": agent["role"],
            "required_actions": list(contract.required_first_actions) if contract else [],
        },
    )


def complete_session_action(conn: sqlite3.Connection, *, session_id: int, action_key: str, detail: dict | None = None) -> bool:
    bootstrap = get_session_bootstrap(conn, session_id)
    required_actions = set(json.loads(bootstrap["required_actions_json"]))
    if action_key not in required_actions:
        return False
    conn.execute(
        """
        INSERT INTO session_action_receipts (session_id, action_key, detail_json)
        VALUES (?, ?, ?)
        ON CONFLICT(session_id, action_key)
        DO UPDATE SET detail_json = excluded.detail_json, created_at = CURRENT_TIMESTAMP
        """,
        (session_id, action_key, json.dumps(detail or {})),
    )
    log_event(
        conn,
        "session.required_action_completed",
        agent_id=bootstrap["agent_id"],
        session_id=session_id,
        payload={"action_key": action_key},
    )
    return True


def get_pending_required_actions(conn: sqlite3.Connection, session_id: int) -> list[str]:
    bootstrap = get_session_bootstrap(conn, session_id)
    required_actions = json.loads(bootstrap["required_actions_json"])
    completed = {
        row["action_key"]
        for row in conn.execute(
            "SELECT action_key FROM session_action_receipts WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    }
    return [action for action in required_actions if action not in completed]


def enforce_role_contract(conn: sqlite3.Connection, *, agent, verb: str, allow_override: bool = False) -> None:
    contract = get_role_contract(agent["role"])
    if contract is None:
        return
    active_session = get_active_session_for_agent(conn, agent["id"])
    if active_session is not None:
        bootstrap = get_session_bootstrap(conn, active_session["id"])
        if bootstrap["acknowledged_at"] is None and verb not in {"session_bootstrap_show", "session_bootstrap_ack", "msg_inbox", "task_list", "task_show"}:
            log_event(
                conn,
                "role.drift_detected",
                agent_id=agent["id"],
                session_id=active_session["id"],
                payload={"verb": verb, "reason": "bootstrap_not_acknowledged", "role": agent["role"]},
            )
            conn.commit()
            raise SystemExit("session bootstrap must be acknowledged before acting")
        pending_actions = get_pending_required_actions(conn, active_session["id"])
        if pending_actions and verb not in {"session_bootstrap_show", "session_bootstrap_ack", "session_action", "msg_inbox", "task_list", "task_show", "task_delegate"}:
            log_event(
                conn,
                "role.drift_detected",
                agent_id=agent["id"],
                session_id=active_session["id"],
                payload={"verb": verb, "reason": "required_actions_pending", "pending_actions": pending_actions},
            )
            conn.commit()
            raise SystemExit(f"session bootstrap actions still pending: {', '.join(pending_actions)}")
    if verb in contract.blocked_verbs:
        if allow_override:
            log_event(
                conn,
                "role.override_used",
                agent_id=agent["id"],
                session_id=active_session["id"] if active_session else None,
                payload={"verb": verb, "role": agent["role"]},
            )
            return
        log_event(
            conn,
            "role.drift_detected",
            agent_id=agent["id"],
            session_id=active_session["id"] if active_session else None,
            payload={"verb": verb, "reason": "blocked_by_role_contract", "role": agent["role"]},
        )
        conn.commit()
        raise SystemExit(f"role {agent['role']} is not allowed to perform {verb} without explicit override")


def release_stale_leases(conn: sqlite3.Connection) -> int:
    stale_leases = conn.execute(
        """
        SELECT tl.id, tl.task_id, tl.agent_id, tl.session_id
        FROM task_leases tl
        JOIN sessions s ON s.id = tl.session_id
        WHERE tl.state = 'active'
          AND tl.released_at IS NULL
          AND (
              s.status != 'active'
              OR s.ended_at IS NOT NULL
              OR s.heartbeat_at < datetime('now', ?)
          )
        """,
        (f"-{SESSION_STALE_MINUTES} minutes",),
    ).fetchall()
    if not stale_leases:
        return 0
    lease_ids = [row["id"] for row in stale_leases]
    placeholders = ", ".join("?" for _ in lease_ids)
    conn.execute(
        f"""
        UPDATE task_leases
        SET state = 'released', released_at = CURRENT_TIMESTAMP
        WHERE id IN ({placeholders})
        """,
        tuple(lease_ids),
    )
    for lease in stale_leases:
        log_event(
            conn,
            "lease.released",
            task_id=lease["task_id"],
            agent_id=lease["agent_id"],
            session_id=lease["session_id"],
            payload={"reason": "stale_session"},
        )
    return len(stale_leases)


def _process_is_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def cleanup_stale_worker_runtimes(
    conn: sqlite3.Connection,
    *,
    stale_minutes: int = WORKER_RUNTIME_STALE_MINUTES,
) -> list[dict[str, object]]:
    stale_runtimes = conn.execute(
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
    cleaned: list[dict[str, object]] = []
    for runtime in stale_runtimes:
        pid_candidates = []
        for pid in (runtime["child_pid"], runtime["pid"], runtime["supervisor_pid"]):
            if pid and pid not in pid_candidates:
                pid_candidates.append(pid)
        live_pids = [pid for pid in pid_candidates if _process_is_alive(pid)]
        if live_pids:
            continue
        conn.execute(
            """
            UPDATE worker_runtimes
            SET status = 'failed',
                ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP)
            WHERE id = ?
            """,
            (runtime["id"],),
        )
        blocked_packets = conn.execute(
            """
            SELECT id, task_id
            FROM dispatch_packets
            WHERE runtime_id = ?
              AND delivery_status IN ('delivered', 'acknowledged')
            ORDER BY id ASC
            """,
            (runtime["id"],),
        ).fetchall()
        conn.execute(
            """
            UPDATE dispatch_packets
            SET delivery_status = 'failed',
                completed_at = COALESCE(completed_at, CURRENT_TIMESTAMP),
                completion_note = ?
            WHERE runtime_id = ?
              AND delivery_status IN ('delivered', 'acknowledged')
            """,
            (f"runtime {runtime['id']} failed after stale heartbeat", runtime["id"]),
        )
        log_event(
            conn,
            "worker.runtime_failed",
            task_id=runtime["task_id"],
            agent_id=runtime["requested_by_agent_id"],
            payload={
                "runtime_id": runtime["id"],
                "worker_name": runtime["worker_name"],
                "reason": "stale_heartbeat",
                "stale_minutes": stale_minutes,
            },
        )
        for packet in blocked_packets:
            log_event(
                conn,
                "dispatch.packet_completed",
                task_id=packet["task_id"],
                agent_id=runtime["requested_by_agent_id"],
                payload={
                    "packet_id": packet["id"],
                    "status": "failed",
                    "note": f"runtime {runtime['id']} failed after stale heartbeat",
                },
            )
        cleaned.append(
            {
                "runtime_id": runtime["id"],
                "worker_name": runtime["worker_name"],
                "stale_minutes": stale_minutes,
                "packet_count": len(blocked_packets),
            }
        )
    return cleaned


def capture_git_snapshot(cwd: str | None = None) -> dict:
    import subprocess

    null_snapshot: dict = {
        "git_branch": None,
        "git_base_ref": None,
        "git_dirty": None,
        "git_staged_files_json": None,
        "git_changed_files_json": None,
    }

    cwd_path = Path(cwd) if cwd else Path.cwd()

    def run(cmd: list[str]) -> str | None:
        try:
            return subprocess.check_output(cmd, cwd=cwd_path, stderr=subprocess.DEVNULL, text=True).strip()
        except Exception:
            return None

    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if not branch or branch == "HEAD":
        return null_snapshot

    staged_out = run(["git", "diff", "--name-only", "--cached"]) or ""
    dirty_out = run(["git", "diff", "--name-only"]) or ""

    base_ref: str | None = None
    for remote_ref in ("origin/main", "origin/master"):
        base_ref = run(["git", "merge-base", "HEAD", remote_ref])
        if base_ref:
            break

    changed_files: list[str] = []
    if base_ref:
        changed_out = run(["git", "diff", "--name-only", f"{base_ref}...HEAD"]) or ""
        changed_files = [f for f in changed_out.splitlines() if f]

    return {
        "git_branch": branch,
        "git_base_ref": base_ref,
        "git_dirty": 1 if dirty_out.strip() else 0,
        "git_staged_files_json": json.dumps([f for f in staged_out.splitlines() if f]),
        "git_changed_files_json": json.dumps(changed_files),
    }


# Fatal issue kinds: presence of any of these blocks routing operations.
PREFLIGHT_FATAL_KINDS = frozenset({"orphaned_task", "retired_leased"})


def enforce_roster_preflight(
    conn: sqlite3.Connection,
    *,
    exclude_reconnecting_agent_id: int | None = None,
) -> None:
    """Run the roster preflight and raise SystemExit if fatal drift is detected.

    Fatal conditions (orphaned_task, retired_leased) block session start,
    worker start, and dispatch until resolved. Duplicate-role issues are
    non-fatal: they are surfaced but do not block.

    Pass exclude_reconnecting_agent_id when starting a session for an agent
    that owns orphaned tasks — starting their session resolves the orphan.
    """
    issues = run_roster_preflight(conn, exclude_reconnecting_agent_id=exclude_reconnecting_agent_id)
    fatal = [i for i in issues if i["kind"] in PREFLIGHT_FATAL_KINDS]
    warnings = [i for i in issues if i["kind"] not in PREFLIGHT_FATAL_KINDS]
    for w in warnings:
        print(f"roster warning [{w['kind']}] {w['agent_name']}: {w['detail']}")
    if fatal:
        lines = "\n".join(f"  [{i['kind']}] {i['agent_name']}: {i['detail']}" for i in fatal)
        raise SystemExit(
            f"roster preflight failed — resolve drift before proceeding:\n{lines}"
        )


def retire_agent(
    conn: sqlite3.Connection,
    *,
    agent_id: int,
    agent_name: str,
    retiring_agent_id: int | None,
) -> None:
    """Mark an agent as retired, release its active leases, and emit an audit event.

    Ownership of active tasks is NOT automatically transferred — callers must
    reassign tasks before or after retiring the agent. The audit event captures
    the full context for the event log.
    """
    # Release any active leases held by this agent
    conn.execute(
        """
        UPDATE task_leases
        SET state = 'released', released_at = CURRENT_TIMESTAMP
        WHERE agent_id = ? AND state = 'active' AND released_at IS NULL
        """,
        (agent_id,),
    )
    # End any active sessions
    conn.execute(
        """
        UPDATE sessions
        SET status = 'ended', ended_at = CURRENT_TIMESTAMP, heartbeat_at = CURRENT_TIMESTAMP
        WHERE agent_id = ? AND status = 'active' AND ended_at IS NULL
        """,
        (agent_id,),
    )
    # Mark the agent retired
    conn.execute(
        "UPDATE agents SET status = 'retired' WHERE id = ?",
        (agent_id,),
    )
    log_event(
        conn,
        "agent.retired",
        agent_id=agent_id,
        payload={
            "agent_name": agent_name,
            "retired_by_agent_id": retiring_agent_id,
        },
    )
