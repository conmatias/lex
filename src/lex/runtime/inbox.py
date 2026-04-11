from __future__ import annotations

import json
from pathlib import Path

from lex.db import LexPaths


def runtime_root(paths: LexPaths) -> Path:
    root = paths.lex_dir / "runtime"
    root.mkdir(parents=True, exist_ok=True)
    return root


def worker_runtime_dir(paths: LexPaths, runtime_id: int) -> Path:
    root = runtime_root(paths) / "workers" / f"runtime-{runtime_id}"
    root.mkdir(parents=True, exist_ok=True)
    return root


def resolve_runtime_inbox(paths: LexPaths, runtime: dict[str, object]) -> Path:
    runtime_dir = worker_runtime_dir(paths, int(runtime["id"]))
    inbox_path = Path(str(runtime["inbox_path"] or runtime_dir / "inbox"))
    inbox_path.mkdir(parents=True, exist_ok=True)
    return inbox_path


def build_packet_payload(*, packet_id: int, packet: dict[str, object], runtime: dict[str, object]) -> dict[str, object]:
    return {
        "id": packet_id,
        "task_id": packet["task_id"],
        "from_agent": packet["from_agent_name"],
        "to_worker": runtime["worker_name"],
        "sensitive_action": packet["sensitive_action"],
        "created_at": packet["created_at"],
        "packet": json.loads(str(packet["packet_json"])),
    }


def write_packet_to_inbox(
    paths: LexPaths,
    *,
    packet_id: int,
    packet: dict[str, object],
    runtime: dict[str, object],
) -> Path:
    inbox_path = resolve_runtime_inbox(paths, runtime)
    packet_path = inbox_path / f"packet-{packet_id}.json"
    payload = build_packet_payload(packet_id=packet_id, packet=packet, runtime=runtime)
    packet_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return packet_path
