"""Task service — create, claim, update, handoff, delegate."""
from __future__ import annotations

import json
import sqlite3

from lex.coordination import (
    complete_session_action,
    enforce_role_contract,
    get_active_session_for_agent,
    get_agent,
    get_task,
    release_stale_leases,
)
from lex.db import detect_path_conflicts, fetch_one, log_event
from lex.services.results import (
    TaskClaimResult,
    TaskCreateResult,
    TaskDelegateResult,
    TaskHandoffResult,
    TaskStatusResult,
)

VALID_TASK_STATES = {
    "open",
    "claimed",
    "in_progress",
    "blocked",
    "review_requested",
    "handoff_pending",
    "done",
    "abandoned",
}


def _wrap(exc: SystemExit) -> ValueError:
    return ValueError(str(exc))


def create_task(
    conn: sqlite3.Connection,
    *,
    title: str,
    slug: str | None = None,
    description: str = "",
    priority: int = 2,
    created_by: str | None = None,
    parent_task: int | None = None,
    delegation_mode: str = "direct",
    paths: list[str] | None = None,
    force_role_override: bool = False,
) -> TaskCreateResult:
    try:
        creator_id = None
        if created_by:
            creator = get_agent(conn, created_by)
            enforce_role_contract(conn, agent=creator, verb="task_create", allow_override=force_role_override)
            creator_id = creator["id"]
        claimed_paths = paths or []
        conn.execute(
            """
            INSERT INTO tasks (
                slug, title, description, status, priority, parent_task_id,
                delegation_mode, claimed_paths_json, created_by_agent_id
            )
            VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)
            """,
            (slug, title, description, priority, parent_task, delegation_mode,
             json.dumps(claimed_paths), creator_id),
        )
        task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        log_event(
            conn, "task.created",
            task_id=task_id, agent_id=creator_id,
            payload={"title": title, "parent_task_id": parent_task},
        )
        conn.commit()
        return TaskCreateResult(task_id=task_id, title=title)
    except SystemExit as exc:
        raise _wrap(exc) from exc


def claim_task(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    agent: str,
    ttl_minutes: int = 30,
    strict: bool = False,
    force_role_override: bool = False,
) -> TaskClaimResult:
    try:
        release_stale_leases(conn)
        task = get_task(conn, task_id)
        ag = get_agent(conn, agent)
        enforce_role_contract(conn, agent=ag, verb="task_claim", allow_override=force_role_override)
        session = get_active_session_for_agent(conn, ag["id"])
        active_lease = fetch_one(
            conn,
            """
            SELECT * FROM task_leases
            WHERE task_id = ? AND state = 'active' AND released_at IS NULL
              AND expires_at > CURRENT_TIMESTAMP
            ORDER BY id DESC LIMIT 1
            """,
            (task_id,),
        )
        if active_lease is not None and active_lease["agent_id"] != ag["id"]:
            raise ValueError(f"task {task_id} is already leased by another agent")
        candidate_paths = json.loads(task["claimed_paths_json"])
        claiming_branch = session["git_branch"] if session else None
        conflicts = detect_path_conflicts(
            conn, candidate_paths, exclude_task_id=task_id, claiming_branch=claiming_branch
        )
        session_id = session["id"] if session else None
        for conflict in conflicts:
            log_event(
                conn, "task.conflict_detected",
                task_id=task_id, agent_id=ag["id"], session_id=session_id,
                payload={
                    "conflicting_task_id": conflict["task_id"],
                    "owner_agent_name": conflict["owner_agent_name"],
                    "conflicting_path": conflict["conflicting_path"],
                    "candidate_path": conflict["candidate_path"],
                    "cross_branch": conflict["cross_branch"],
                },
            )
        hard_conflicts = [c for c in conflicts if not c["cross_branch"]]
        if hard_conflicts and strict:
            owners = ", ".join(
                f"task {c['task_id']} ({c['owner_agent_name']}) @ {c['conflicting_path']}"
                for c in hard_conflicts
            )
            conn.commit()
            raise ValueError(f"path conflict detected — blocked by: {owners}")
        if active_lease is None:
            conn.execute(
                """
                INSERT INTO task_leases (task_id, agent_id, session_id, expires_at)
                VALUES (?, ?, ?, datetime('now', ?))
                """,
                (task_id, ag["id"], session_id, f"+{ttl_minutes} minutes"),
            )
        conn.execute(
            """
            UPDATE tasks
            SET owner_agent_id = ?, status = CASE WHEN status = 'open' THEN 'claimed' ELSE status END
            WHERE id = ?
            """,
            (ag["id"], task_id),
        )
        log_event(
            conn, "task.claimed",
            task_id=task_id, agent_id=ag["id"], session_id=session_id,
            payload={"ttl_minutes": ttl_minutes, "previous_status": task["status"]},
        )
        conn.commit()
        return TaskClaimResult(task_id=task_id, agent=agent, conflicts=list(conflicts))
    except SystemExit as exc:
        raise _wrap(exc) from exc


def update_task_status(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    agent: str,
    status: str,
    force_role_override: bool = False,
) -> TaskStatusResult:
    if status not in VALID_TASK_STATES:
        raise ValueError(f"invalid task status: {status}")
    try:
        task = get_task(conn, task_id)
        ag = get_agent(conn, agent)
        enforce_role_contract(conn, agent=ag, verb="task_status", allow_override=force_role_override)
        if task["owner_agent_id"] not in (None, ag["id"]):
            raise ValueError("only the owner may update task status")
        session = get_active_session_for_agent(conn, ag["id"])
        if status == "done":
            conn.execute(
                "UPDATE tasks SET status = ?, owner_agent_id = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, ag["id"], task_id),
            )
        else:
            conn.execute(
                "UPDATE tasks SET status = ?, owner_agent_id = ?, completed_at = NULL WHERE id = ?",
                (status, ag["id"], task_id),
            )
        log_event(
            conn, "task.status_changed",
            task_id=task_id, agent_id=ag["id"],
            session_id=session["id"] if session else None,
            payload={"from": task["status"], "to": status},
        )
        conn.commit()
        return TaskStatusResult(task_id=task_id, previous_status=task["status"], new_status=status)
    except SystemExit as exc:
        raise _wrap(exc) from exc


def handoff_task(
    conn: sqlite3.Connection,
    *,
    task_id: int,
    from_agent: str,
    to_agent: str,
    subject: str | None = None,
    body: str,
    force_role_override: bool = False,
) -> TaskHandoffResult:
    try:
        task = get_task(conn, task_id)
        from_ag = get_agent(conn, from_agent)
        enforce_role_contract(conn, agent=from_ag, verb="task_handoff", allow_override=force_role_override)
        to_ag = get_agent(conn, to_agent)
        if task["owner_agent_id"] not in (None, from_ag["id"]):
            raise ValueError("only the owner may hand off the task")
        conn.execute(
            "UPDATE tasks SET owner_agent_id = ?, status = 'handoff_pending' WHERE id = ?",
            (to_ag["id"], task_id),
        )
        conn.execute(
            """
            INSERT INTO messages (task_id, from_agent_id, to_agent_id, type, subject, body)
            VALUES (?, ?, ?, 'handoff', ?, ?)
            """,
            (task_id, from_ag["id"], to_ag["id"], subject or "handoff", body),
        )
        conn.execute(
            """
            UPDATE task_leases
            SET state = 'released', released_at = CURRENT_TIMESTAMP
            WHERE task_id = ? AND agent_id = ? AND state = 'active' AND released_at IS NULL
            """,
            (task_id, from_ag["id"]),
        )
        log_event(
            conn, "task.handoff",
            task_id=task_id, agent_id=from_ag["id"],
            payload={"to_agent": to_agent},
        )
        conn.commit()
        return TaskHandoffResult(task_id=task_id, from_agent=from_agent, to_agent=to_agent)
    except SystemExit as exc:
        raise _wrap(exc) from exc


def delegate_task(
    conn: sqlite3.Connection,
    *,
    parent_task_id: int,
    owner_agent: str,
    assignee_agent: str,
    title: str,
    slug: str | None = None,
    description: str = "",
    subject: str | None = None,
    body: str,
    priority: int | None = None,
    ttl_minutes: int = 30,
    paths: list[str] | None = None,
    force_role_override: bool = False,
) -> TaskDelegateResult:
    try:
        parent_task = get_task(conn, parent_task_id)
        owner = get_agent(conn, owner_agent)
        enforce_role_contract(conn, agent=owner, verb="task_delegate", allow_override=force_role_override)
        assignee = get_agent(conn, assignee_agent)
        assignee_session = get_active_session_for_agent(conn, assignee["id"])
        if parent_task["owner_agent_id"] not in (None, owner["id"]):
            raise ValueError("only the parent owner may delegate child tasks")
        if parent_task["delegation_mode"] != "hypervisor":
            raise ValueError("parent task must be in hypervisor delegation mode")
        conn.execute(
            """
            INSERT INTO tasks (
                slug, title, description, status, priority, owner_agent_id, parent_task_id,
                delegation_mode, claimed_paths_json, created_by_agent_id
            )
            VALUES (?, ?, ?, 'claimed', ?, ?, ?, 'direct', ?, ?)
            """,
            (
                slug, title, description,
                priority if priority is not None else parent_task["priority"],
                assignee["id"], parent_task_id,
                json.dumps(paths or []), owner["id"],
            ),
        )
        child_task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            """
            INSERT INTO task_leases (task_id, agent_id, session_id, expires_at)
            VALUES (?, ?, ?, datetime('now', ?))
            """,
            (child_task_id, assignee["id"],
             assignee_session["id"] if assignee_session else None,
             f"+{ttl_minutes} minutes"),
        )
        conn.execute(
            """
            INSERT INTO messages (task_id, from_agent_id, to_agent_id, type, subject, body)
            VALUES (?, ?, ?, 'handoff', ?, ?)
            """,
            (child_task_id, owner["id"], assignee["id"], subject or "delegated child task", body),
        )
        log_event(
            conn, "task.delegated",
            task_id=child_task_id, agent_id=owner["id"],
            payload={"parent_task_id": parent_task_id, "assignee": assignee_agent},
        )
        owner_session = get_active_session_for_agent(conn, owner["id"])
        if owner_session is not None:
            complete_session_action(
                conn, session_id=owner_session["id"],
                action_key="assign_or_delegate_work",
                detail={"child_task_id": child_task_id},
            )
        conn.commit()
        return TaskDelegateResult(
            child_task_id=child_task_id,
            parent_task_id=parent_task_id,
            assignee=assignee_agent,
        )
    except SystemExit as exc:
        raise _wrap(exc) from exc
