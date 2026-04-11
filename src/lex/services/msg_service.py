"""Message service — send messages between agents."""
from __future__ import annotations

import sqlite3

from lex.coordination import (
    complete_session_action,
    enforce_role_contract,
    get_active_session_for_agent,
    get_agent,
    get_task,
)
from lex.db import log_event
from lex.services.results import MessageSendResult

VALID_MESSAGE_TYPES = {
    "note",
    "question",
    "answer",
    "blocker",
    "handoff",
    "review_request",
    "review_result",
    "decision",
    "artifact_notice",
}


def _wrap(exc: SystemExit) -> ValueError:
    return ValueError(str(exc))


def send_message(
    conn: sqlite3.Connection,
    *,
    task_id: int | None = None,
    from_agent: str,
    to_agent: str | None = None,
    message_type: str,
    subject: str | None = None,
    body: str,
) -> MessageSendResult:
    if message_type not in VALID_MESSAGE_TYPES:
        raise ValueError(f"invalid message type: {message_type}")
    try:
        from_ag = get_agent(conn, from_agent)
        enforce_role_contract(conn, agent=from_ag, verb="msg_send", allow_override=False)
        to_ag = get_agent(conn, to_agent) if to_agent else None
        if task_id is not None:
            get_task(conn, task_id)
        conn.execute(
            """
            INSERT INTO messages (task_id, from_agent_id, to_agent_id, type, subject, body)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (task_id, from_ag["id"], to_ag["id"] if to_ag else None,
             message_type, subject or "", body),
        )
        log_event(
            conn, "message.sent",
            task_id=task_id, agent_id=from_ag["id"],
            payload={"type": message_type, "to": to_agent},
        )
        active_session = get_active_session_for_agent(conn, from_ag["id"])
        if active_session is not None:
            role_action = {
                "dev": "report_execution_plan",
                "auditor": "record_review_plan",
                "infra": "record_integration_plan",
            }.get(from_ag["role"])
            if role_action is not None:
                complete_session_action(
                    conn, session_id=active_session["id"],
                    action_key=role_action,
                    detail={"message_type": message_type, "task_id": task_id},
                )
        conn.commit()
        return MessageSendResult(
            task_id=task_id, from_agent=from_agent,
            to_agent=to_agent, message_type=message_type,
        )
    except SystemExit as exc:
        raise _wrap(exc) from exc
