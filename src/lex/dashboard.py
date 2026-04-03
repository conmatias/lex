from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from lex.db import connect, ensure_workspace, initialize_database


@dataclass(frozen=True)
class DashboardState:
    root: Path
    summary: dict
    agents: list[dict]
    sessions: list[dict]
    tasks: list[dict]
    inbox: list[dict]
    events: list[dict]
    task_details: dict[int, dict]
    dx_roster: list[dict]


def load_dashboard_state(root: Path) -> DashboardState:
    paths = ensure_workspace(root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    return DashboardState(
        root=root,
        summary=_query_summary(conn),
        agents=_query_agents(conn),
        sessions=_query_sessions(conn),
        tasks=_query_tasks(conn),
        inbox=_query_inbox(conn),
        events=_query_events(conn),
        task_details=_query_task_details(conn),
        dx_roster=_query_dx_roster(conn),
    )


def _rows(conn: sqlite3.Connection, query: str, params: tuple = ()) -> list[dict]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def _query_summary(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        WITH active_sessions AS (
            SELECT
                s.id,
                s.agent_id,
                s.git_branch,
                COALESCE(s.git_dirty, 0) AS git_dirty,
                CASE
                    WHEN sb.acknowledged_at IS NULL THEN 1
                    ELSE 0
                END AS bootstrap_pending,
                (
                    SELECT COUNT(*)
                    FROM json_each(sb.required_actions_json) req
                    WHERE req.value NOT IN (
                        SELECT sar.action_key
                        FROM session_action_receipts sar
                        WHERE sar.session_id = s.id
                    )
                ) AS pending_required_actions,
                (
                    SELECT COUNT(*)
                    FROM watches w
                    WHERE w.agent_id = s.agent_id
                      AND w.last_sent_event_id > w.last_ack_event_id
                ) AS unacked_watches,
                (
                    SELECT COUNT(*)
                    FROM events e
                    WHERE e.session_id = s.id
                      AND e.event_type = 'role.drift_detected'
                ) AS role_drift_events,
                CASE
                    WHEN s.heartbeat_at < datetime('now', '-15 minutes') THEN 1
                    ELSE 0
                END AS is_stale
            FROM sessions s
            LEFT JOIN session_bootstraps sb ON sb.session_id = s.id
            WHERE s.status = 'active' AND s.ended_at IS NULL
        ),
        active_leases AS (
            SELECT tl.task_id, tl.session_id, tl.expires_at
            FROM task_leases tl
            JOIN (
                SELECT task_id, MAX(id) AS max_id
                FROM task_leases
                WHERE state = 'active' AND released_at IS NULL
                GROUP BY task_id
            ) latest ON latest.max_id = tl.id
        ),
        latest_task_message AS (
            SELECT m.task_id, m.type
            FROM messages m
            JOIN (
                SELECT task_id, MAX(id) AS max_id
                FROM messages
                WHERE task_id IS NOT NULL
                GROUP BY task_id
            ) latest ON latest.max_id = m.id
        ),
        task_flags AS (
            SELECT
                t.id,
                t.status,
                CASE
                    WHEN lease.expires_at IS NOT NULL
                     AND lease.expires_at < datetime('now', '+5 minutes') THEN 1
                    ELSE 0
                END AS lease_expiring,
                CASE
                    WHEN t.status IN ('review_requested', 'handoff_pending') THEN 1
                    WHEN latest_task_message.type IN ('review_request', 'handoff') THEN 1
                    ELSE 0
                END AS review_needed,
                CASE
                    WHEN t.status = 'blocked' THEN 1
                    ELSE 0
                END AS is_blocked,
                CASE
                    WHEN t.status IN ('done', 'abandoned') THEN 0
                    WHEN lease.expires_at IS NOT NULL
                     AND lease.expires_at < datetime('now', '+5 minutes') THEN 1
                    WHEN owner_session.is_stale = 1 THEN 1
                    WHEN owner_session.bootstrap_pending = 1 THEN 1
                    WHEN owner_session.pending_required_actions > 0 THEN 1
                    WHEN owner_session.unacked_watches > 0 THEN 1
                    WHEN owner_session.role_drift_events > 0 THEN 1
                    WHEN owner_session.git_dirty = 1 THEN 1
                    ELSE 0
                END AS is_risky
            FROM tasks t
            LEFT JOIN active_leases lease ON lease.task_id = t.id
            LEFT JOIN active_sessions owner_session ON owner_session.agent_id = t.owner_agent_id
            LEFT JOIN latest_task_message ON latest_task_message.task_id = t.id
        )
        SELECT
            (SELECT COUNT(*) FROM active_sessions WHERE is_stale = 1) AS stale_sessions,
            (SELECT COUNT(*) FROM task_flags WHERE is_blocked = 1) AS blocked_tasks,
            (SELECT COUNT(*) FROM task_flags WHERE review_needed = 1) AS review_needed_tasks,
            (SELECT COUNT(*) FROM task_flags WHERE is_risky = 1) AS risky_tasks,
            (SELECT COUNT(*) FROM active_sessions WHERE git_dirty = 1) AS dirty_sessions,
            (SELECT COUNT(*) FROM active_sessions WHERE bootstrap_pending = 1 OR pending_required_actions > 0 OR unacked_watches > 0 OR role_drift_events > 0) AS sessions_needing_attention
        """
    ).fetchone()
    return dict(row) if row is not None else {
        "stale_sessions": 0,
        "blocked_tasks": 0,
        "review_needed_tasks": 0,
        "risky_tasks": 0,
        "dirty_sessions": 0,
        "sessions_needing_attention": 0,
    }


def _query_agents(conn: sqlite3.Connection) -> list[dict]:
    return _rows(
        conn,
        """
        SELECT id, name, kind, role, specialty, status, created_at
        FROM agents
        ORDER BY id
        """,
    )


def _query_sessions(conn: sqlite3.Connection) -> list[dict]:
    return _rows(
        conn,
        """
        SELECT
            s.id,
            a.name AS agent_name,
            a.kind AS agent_kind,
            a.role AS agent_role,
            a.specialty AS agent_specialty,
            s.label,
            s.fingerprint,
            s.fingerprint_label,
            s.status,
            s.cwd,
            s.git_branch,
            s.git_base_ref,
            s.git_dirty,
            s.git_changed_files_json,
            s.git_staged_files_json,
            s.started_at,
            s.heartbeat_at,
            CASE
                WHEN sb.acknowledged_at IS NULL THEN 1
                ELSE 0
            END AS bootstrap_pending,
            (
                SELECT COUNT(*)
                FROM json_each(sb.required_actions_json) req
                WHERE req.value NOT IN (
                    SELECT sar.action_key
                    FROM session_action_receipts sar
                    WHERE sar.session_id = s.id
                )
            ) AS pending_required_actions,
            (
                SELECT COUNT(*)
                FROM watches w
                WHERE w.agent_id = s.agent_id
                  AND w.last_sent_event_id > w.last_ack_event_id
            ) AS unacked_watches,
            (
                SELECT COUNT(*)
                FROM events e
                WHERE e.session_id = s.id
                  AND e.event_type = 'role.drift_detected'
            ) AS role_drift_events,
            CASE
                WHEN s.heartbeat_at < datetime('now', '-15 minutes') THEN 1
                ELSE 0
            END AS is_stale
        FROM sessions s
        JOIN agents a ON a.id = s.agent_id
        LEFT JOIN session_bootstraps sb ON sb.session_id = s.id
        WHERE s.status = 'active' AND s.ended_at IS NULL
        ORDER BY s.id DESC
        LIMIT 10
        """,
    )


def _query_tasks(conn: sqlite3.Connection) -> list[dict]:
    return _rows(
        conn,
        """
        SELECT
            t.id,
            t.title,
            t.status,
            t.priority,
            t.delegation_mode,
            t.parent_task_id,
            t.claimed_paths_json,
            a.name AS owner_name,
            a.kind AS owner_kind,
            a.role AS owner_role,
            a.specialty AS owner_specialty,
            lease.expires_at AS lease_expires_at,
            CASE
                WHEN lease.expires_at IS NULL THEN 0
                WHEN lease.expires_at < datetime('now', '+5 minutes') THEN 1
                ELSE 0
            END AS lease_expiring
        FROM tasks t
        LEFT JOIN agents a ON a.id = t.owner_agent_id
        LEFT JOIN (
            SELECT tl.task_id, tl.expires_at
            FROM task_leases tl
            JOIN (
                SELECT task_id, MAX(id) AS max_id
                FROM task_leases
                WHERE state = 'active' AND released_at IS NULL
                GROUP BY task_id
            ) latest ON latest.max_id = tl.id
        ) lease ON lease.task_id = t.id
        ORDER BY
            CASE t.status
                WHEN 'in_progress' THEN 0
                WHEN 'claimed' THEN 1
                WHEN 'blocked' THEN 2
                WHEN 'review_requested' THEN 3
                WHEN 'handoff_pending' THEN 4
                WHEN 'open' THEN 5
                ELSE 6
            END,
            t.priority ASC,
            t.id DESC
        LIMIT 30
        """,
    )


def _query_inbox(conn: sqlite3.Connection) -> list[dict]:
    return _rows(
        conn,
        """
        SELECT
            m.id,
            m.task_id,
            t.title AS task_title,
            sender.name AS from_name,
            recipient.name AS to_name,
            m.type,
            m.subject,
            m.body,
            m.created_at
        FROM messages m
        LEFT JOIN tasks t ON t.id = m.task_id
        JOIN agents sender ON sender.id = m.from_agent_id
        LEFT JOIN agents recipient ON recipient.id = m.to_agent_id
        ORDER BY m.id DESC
        LIMIT 12
        """,
    )


def _query_events(conn: sqlite3.Connection) -> list[dict]:
    return _rows(
        conn,
        """
        SELECT
            e.id,
            e.event_type,
            e.task_id,
            a.name AS agent_name,
            a.role AS agent_role,
            a.specialty AS agent_specialty,
            e.created_at
        FROM events e
        LEFT JOIN agents a ON a.id = e.agent_id
        ORDER BY e.id DESC
        LIMIT 12
        """,
    )


def _query_task_details(conn: sqlite3.Connection) -> dict[int, dict]:
    tasks = _rows(
        conn,
        """
        SELECT
            t.id,
            t.title,
            t.description,
            t.status,
            t.priority,
            t.delegation_mode,
            t.parent_task_id,
            t.claimed_paths_json,
            a.name AS owner_name,
            a.kind AS owner_kind,
            a.role AS owner_role,
            a.specialty AS owner_specialty,
            lease.expires_at AS lease_expires_at,
            CASE
                WHEN lease.expires_at IS NULL THEN 0
                WHEN lease.expires_at < datetime('now', '+5 minutes') THEN 1
                ELSE 0
            END AS lease_expiring
        FROM tasks t
        LEFT JOIN agents a ON a.id = t.owner_agent_id
        LEFT JOIN (
            SELECT tl.task_id, tl.expires_at
            FROM task_leases tl
            JOIN (
                SELECT task_id, MAX(id) AS max_id
                FROM task_leases
                WHERE state = 'active' AND released_at IS NULL
                GROUP BY task_id
            ) latest ON latest.max_id = tl.id
        ) lease ON lease.task_id = t.id
        ORDER BY t.id
        """,
    )
    details: dict[int, dict] = {}
    for task in tasks:
        task_id = task["id"]
        messages = _rows(
            conn,
            """
            SELECT
                m.id,
                sender.name AS from_name,
                recipient.name AS to_name,
                m.type,
                m.subject,
                m.body,
                m.created_at
            FROM messages m
            JOIN agents sender ON sender.id = m.from_agent_id
            LEFT JOIN agents recipient ON recipient.id = m.to_agent_id
            WHERE m.task_id = ?
            ORDER BY m.id DESC
            LIMIT 6
            """,
            (task_id,),
        )
        children = _rows(
            conn,
            """
            SELECT
                id,
                title,
                status
            FROM tasks
            WHERE parent_task_id = ?
            ORDER BY id DESC
            LIMIT 6
            """,
            (task_id,),
        )
        details[task_id] = {
            **task,
            "messages": list(reversed(messages)),
            "children": list(reversed(children)),
        }
    return details


def _query_dx_roster(conn: sqlite3.Connection) -> list[dict]:
    return _rows(
        conn,
        """
        SELECT
            a.id AS agent_id,
            a.name AS agent_name,
            a.kind AS agent_kind,
            a.role AS agent_role,
            a.specialty AS agent_specialty,
            s.id AS session_id,
            CASE
                WHEN s.heartbeat_at < datetime('now', '-15 minutes') THEN 1
                ELSE 0
            END AS is_stale,
            t.id AS task_id,
            t.title AS task_title,
            t.status AS task_status,
            json_array_length(t.claimed_paths_json) AS claimed_path_count,
            json_array_length(s.git_changed_files_json) AS changed_file_count,
            s.heartbeat_at AS last_activity_at
        FROM agents a
        LEFT JOIN sessions s ON s.agent_id = a.id AND s.status = 'active' AND s.ended_at IS NULL
        LEFT JOIN tasks t ON t.owner_agent_id = a.id AND t.status IN ('claimed', 'in_progress', 'blocked', 'review_requested', 'handoff_pending')
        WHERE a.status = 'active'
        ORDER BY a.name
        """,
    )
