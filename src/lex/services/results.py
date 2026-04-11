"""Typed result objects for lex service operations."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TaskCreateResult:
    task_id: int
    title: str


@dataclass
class TaskClaimResult:
    task_id: int
    agent: str
    conflicts: list[dict] = field(default_factory=list)


@dataclass
class TaskStatusResult:
    task_id: int
    previous_status: str
    new_status: str


@dataclass
class TaskHandoffResult:
    task_id: int
    from_agent: str
    to_agent: str


@dataclass
class TaskDelegateResult:
    child_task_id: int
    parent_task_id: int
    assignee: str


@dataclass
class SessionStartResult:
    session_id: int
    agent: str
    fingerprint: str
    fingerprint_label: str
    conflicting_session_id: int | None = None


@dataclass
class SessionHeartbeatResult:
    session_id: int


@dataclass
class SessionEndResult:
    session_id: int
    released_leases: int


@dataclass
class AgentIdentifyResult:
    agent_id: int
    name: str
    kind: str
    role: str
    specialty: str


@dataclass
class AgentRegisterResult:
    agent_id: int
    name: str
    kind: str
    role: str
    specialty: str


@dataclass
class MessageSendResult:
    task_id: int | None
    from_agent: str
    to_agent: str | None
    message_type: str
