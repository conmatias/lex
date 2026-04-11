"""Agent service — identify and register agents."""
from __future__ import annotations

import sqlite3

from lex.db import log_event
from lex.services.results import AgentIdentifyResult, AgentRegisterResult


def _wrap(exc: SystemExit) -> ValueError:
    return ValueError(str(exc))


def identify_agent(
    conn: sqlite3.Connection,
    *,
    kind: str,
    name: str | None = None,
    role: str | None = None,
    specialty: str | None = None,
) -> AgentIdentifyResult:
    try:
        from lex.cli_legacy import (
            AGENT_NAME_RE,
            allocate_agent_name,
            normalize_agent_role,
            normalize_agent_specialty,
        )
        resolved_name = name or allocate_agent_name(conn, kind)
        if not AGENT_NAME_RE.match(resolved_name):
            raise ValueError("agent names must match <agent>-<adjective>-<noun>")
        normalized_role = normalize_agent_role(role)
        normalized_specialty = normalize_agent_specialty(conn, specialty)
        try:
            conn.execute(
                "INSERT INTO agents (name, kind, role, specialty, status) VALUES (?, ?, ?, ?, 'active')",
                (resolved_name, kind, normalized_role, normalized_specialty),
            )
        except Exception as exc:
            raise ValueError(f"failed to identify agent: {exc}") from exc
        agent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        log_event(
            conn, "agent.registered",
            agent_id=agent_id,
            payload={"name": resolved_name, "kind": kind, "role": normalized_role,
                     "specialty": normalized_specialty, "identified": True},
        )
        conn.commit()
        return AgentIdentifyResult(
            agent_id=agent_id, name=resolved_name, kind=kind,
            role=normalized_role, specialty=normalized_specialty,
        )
    except SystemExit as exc:
        raise _wrap(exc) from exc


def register_agent(
    conn: sqlite3.Connection,
    *,
    name: str,
    kind: str,
    role: str | None = None,
    specialty: str | None = None,
) -> AgentRegisterResult:
    try:
        from lex.cli_legacy import (
            AGENT_NAME_RE,
            normalize_agent_role,
            normalize_agent_specialty,
        )
        if not AGENT_NAME_RE.match(name):
            raise ValueError("agent names must match <agent>-<adjective>-<noun>")
        normalized_role = normalize_agent_role(role)
        normalized_specialty = normalize_agent_specialty(conn, specialty)
        try:
            conn.execute(
                "INSERT INTO agents (name, kind, role, specialty, status) VALUES (?, ?, ?, ?, 'active')",
                (name, kind, normalized_role, normalized_specialty),
            )
        except Exception as exc:
            raise ValueError(f"failed to register agent: {exc}") from exc
        agent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        log_event(
            conn, "agent.registered",
            agent_id=agent_id,
            payload={"name": name, "kind": kind, "role": normalized_role,
                     "specialty": normalized_specialty},
        )
        conn.commit()
        return AgentRegisterResult(
            agent_id=agent_id, name=name, kind=kind,
            role=normalized_role, specialty=normalized_specialty,
        )
    except SystemExit as exc:
        raise _wrap(exc) from exc
