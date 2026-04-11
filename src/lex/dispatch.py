from __future__ import annotations

import json
import shlex

from lex.runtime.inbox import runtime_root, worker_runtime_dir
from lex.runtime.process_manager import launch_worker_supervisor, stop_runtime_process


VALID_WORKER_APPROVAL_POLICIES = ("always", "on_sensitive", "never")
VALID_WORKER_RUNTIME_STATUSES = (
    "pending_approval",
    "approved",
    "launching",
    "running",
    "exited",
    "failed",
    "stopped",
    "rejected",
)
VALID_PACKET_STATUSES = (
    "draft",
    "pending_approval",
    "ready",
    "delivered",
    "acknowledged",
    "completed",
    "failed",
    "cancelled",
)
SENSITIVE_ACTIONS_REQUIRING_APPROVAL = {"spawn_worker", "final_integration", "merge"}


def decode_json_list(raw: str, *, field_name: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{field_name} must be valid JSON") from exc
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SystemExit(f"{field_name} must be a JSON array of strings")
    return value


def decode_json_object(raw: str, *, field_name: str) -> dict[str, str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{field_name} must be valid JSON") from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) and isinstance(val, str) for key, val in value.items()):
        raise SystemExit(f"{field_name} must be a JSON object of string keys and values")
    return value


def command_preview(command: list[str]) -> str:
    return shlex.join(command)


def should_require_runtime_approval(*, policy: str, sensitive_action: str | None) -> bool:
    if policy == "always":
        return True
    if policy == "never":
        return False
    return (sensitive_action or "") in SENSITIVE_ACTIONS_REQUIRING_APPROVAL


def should_require_packet_approval(*, requested: bool, sensitive_action: str | None) -> bool:
    return requested or (sensitive_action or "") in SENSITIVE_ACTIONS_REQUIRING_APPROVAL
