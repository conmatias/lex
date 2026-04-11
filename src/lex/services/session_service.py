"""Session service — start, heartbeat, end."""
from __future__ import annotations

import json
import sqlite3

from lex.coordination import (
    capture_git_snapshot,
    create_session_bootstrap,
    enforce_roster_preflight,
    get_active_session_for_agent,
    get_agent,
    get_session,
    release_stale_leases,
)
from lex.db import log_event
from lex.services.results import SessionEndResult, SessionHeartbeatResult, SessionStartResult


def _wrap(exc: SystemExit) -> ValueError:
    return ValueError(str(exc))


def start_session(
    conn: sqlite3.Connection,
    *,
    agent: str,
    label: str = "primary",
    cwd: str,
    capabilities: list[str] | None = None,
    fingerprint: str | None = None,
    fingerprint_label: str | None = None,
) -> SessionStartResult:
    try:
        from lex.cli_legacy import build_session_fingerprint
        ag = get_agent(conn, agent)
        enforce_roster_preflight(conn, exclude_reconnecting_agent_id=ag["id"])
        if fingerprint is None or fingerprint_label is None:
            fingerprint, fingerprint_label = build_session_fingerprint(kind=ag["kind"], cwd=cwd)
        conflicting = conn.execute(
            """
            SELECT s.id FROM sessions s
            WHERE s.agent_id = ?
              AND s.status = 'active'
              AND s.ended_at IS NULL
              AND s.fingerprint IS NOT NULL
              AND s.fingerprint != ?
            ORDER BY s.id DESC LIMIT 1
            """,
            (ag["id"], fingerprint),
        ).fetchone()
        git = capture_git_snapshot(cwd)
        conn.execute(
            """
            INSERT INTO sessions (
                agent_id, label, fingerprint, fingerprint_label, status, cwd, capabilities_json,
                git_branch, git_base_ref, git_dirty, git_staged_files_json, git_changed_files_json
            )
            VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ag["id"], label, fingerprint, fingerprint_label, cwd,
                json.dumps({"capabilities": capabilities or []}),
                git["git_branch"], git["git_base_ref"], git["git_dirty"],
                git["git_staged_files_json"], git["git_changed_files_json"],
            ),
        )
        session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        log_event(
            conn, "session.started",
            agent_id=ag["id"], session_id=session_id,
            payload={
                "label": label, "cwd": cwd,
                "fingerprint": fingerprint, "fingerprint_label": fingerprint_label,
                "git_branch": git["git_branch"],
            },
        )
        create_session_bootstrap(conn, session_id=session_id, agent=ag)
        conn.commit()
        return SessionStartResult(
            session_id=session_id,
            agent=agent,
            fingerprint=fingerprint,
            fingerprint_label=fingerprint_label,
            conflicting_session_id=conflicting[0] if conflicting else None,
        )
    except SystemExit as exc:
        raise _wrap(exc) from exc


def send_heartbeat(
    conn: sqlite3.Connection,
    *,
    session_id: int,
) -> SessionHeartbeatResult:
    try:
        session = get_session(conn, session_id)
        from lex.coordination import capture_git_snapshot
        git = capture_git_snapshot(session["cwd"])
        conn.execute(
            """
            UPDATE sessions
            SET heartbeat_at = CURRENT_TIMESTAMP,
                git_dirty = ?,
                git_staged_files_json = ?
            WHERE id = ? AND status = 'active' AND ended_at IS NULL
            """,
            (git["git_dirty"], git["git_staged_files_json"], session_id),
        )
        log_event(
            conn, "session.heartbeat",
            agent_id=session["agent_id"], session_id=session_id,
            payload={"label": session["label"], "git_branch": session["git_branch"]},
        )
        conn.commit()
        return SessionHeartbeatResult(session_id=session_id)
    except SystemExit as exc:
        raise _wrap(exc) from exc


def end_session(
    conn: sqlite3.Connection,
    *,
    session_id: int,
) -> SessionEndResult:
    try:
        session = get_session(conn, session_id)
        conn.execute(
            """
            UPDATE sessions
            SET status = 'ended',
                heartbeat_at = CURRENT_TIMESTAMP,
                ended_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (session_id,),
        )
        released_count = release_stale_leases(conn)
        log_event(
            conn, "session.ended",
            agent_id=session["agent_id"], session_id=session_id,
            payload={"label": session["label"], "released_leases": released_count},
        )
        conn.commit()
        return SessionEndResult(session_id=session_id, released_leases=released_count)
    except SystemExit as exc:
        raise _wrap(exc) from exc
