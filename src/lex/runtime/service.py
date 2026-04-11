from __future__ import annotations

import signal
import sqlite3

from lex.db import LexPaths, log_event
from lex.runtime.inbox import write_packet_to_inbox
from lex.runtime.process_manager import launch_worker_supervisor, process_is_alive, stop_runtime_process
from lex.runtime.repository import (
    fail_blocked_packets,
    get_dispatch_packet,
    get_latest_routable_runtime,
    get_worker_runtime,
    list_blocked_packets,
    mark_packet_delivered,
    mark_runtime_failed,
    mark_runtime_launching,
    mark_runtime_stopped,
    set_runtime_supervisor_pid,
    stale_runtime_candidates,
)


def start_runtime(conn: sqlite3.Connection, *, paths: LexPaths, runtime_id: int) -> int:
    runtime = get_worker_runtime(conn, runtime_id)
    if runtime["approval_required"] and runtime["approval_status"] != "approved":
        raise SystemExit("runtime requires human approval before start")
    if runtime["status"] in {"launching", "running"}:
        raise SystemExit(f"runtime {runtime_id} is already {runtime['status']}")
    mark_runtime_launching(conn, runtime_id)
    supervisor = launch_worker_supervisor(paths, runtime_id)
    set_runtime_supervisor_pid(conn, runtime_id, supervisor.pid)
    log_event(
        conn,
        "worker.runtime_launching",
        task_id=runtime["task_id"],
        agent_id=runtime["requested_by_agent_id"],
        payload={"runtime_id": runtime_id, "worker_name": runtime["worker_name"]},
    )
    return supervisor.pid


def stop_runtime(
    conn: sqlite3.Connection,
    *,
    runtime_id: int,
    sig: int = signal.SIGTERM,
    signal_name: str = "TERM",
) -> None:
    runtime = get_worker_runtime(conn, runtime_id)
    target_pid = runtime["child_pid"] or runtime["pid"] or runtime["supervisor_pid"]
    if target_pid is not None:
        try:
            stop_runtime_process(target_pid, sig)
        except ProcessLookupError:
            pass
    mark_runtime_stopped(conn, runtime_id)
    log_event(
        conn,
        "worker.runtime_stopped",
        task_id=runtime["task_id"],
        agent_id=runtime["requested_by_agent_id"],
        payload={"runtime_id": runtime_id, "signal": signal_name},
    )


def cleanup_stale_worker_runtimes(
    conn: sqlite3.Connection,
    *,
    stale_minutes: int = 2,
) -> list[dict[str, object]]:
    stale_runtimes = stale_runtime_candidates(conn, stale_minutes=stale_minutes)
    cleaned: list[dict[str, object]] = []
    for runtime in stale_runtimes:
        pid_candidates = []
        for pid in (runtime["child_pid"], runtime["pid"], runtime["supervisor_pid"]):
            if pid and pid not in pid_candidates:
                pid_candidates.append(pid)
        live_pids = [pid for pid in pid_candidates if process_is_alive(pid)]
        if live_pids:
            continue
        runtime_id = int(runtime["id"])
        note = f"runtime {runtime_id} failed after stale heartbeat"
        mark_runtime_failed(conn, runtime_id)
        blocked_packets = list_blocked_packets(conn, runtime_id)
        fail_blocked_packets(conn, runtime_id=runtime_id, note=note)
        log_event(
            conn,
            "worker.runtime_failed",
            task_id=runtime["task_id"],
            agent_id=runtime["requested_by_agent_id"],
            payload={
                "runtime_id": runtime_id,
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
                    "note": note,
                },
            )
        cleaned.append(
            {
                "runtime_id": runtime_id,
                "worker_name": runtime["worker_name"],
                "stale_minutes": stale_minutes,
                "packet_count": len(blocked_packets),
            }
        )
    return cleaned


def deliver_packet(
    conn: sqlite3.Connection,
    *,
    paths: LexPaths,
    packet_id: int,
    runtime_id: int | None = None,
) -> tuple[int, str]:
    packet = get_dispatch_packet(conn, packet_id)
    if packet["requires_human_approval"] and packet["approval_status"] != "approved":
        raise SystemExit("dispatch packet requires human approval before delivery")
    runtime = get_worker_runtime(conn, runtime_id) if runtime_id else get_latest_routable_runtime(conn, packet["to_worker_id"])
    if runtime is None:
        raise SystemExit("no running worker runtime available for packet delivery")
    if runtime["status"] not in {"approved", "launching", "running"}:
        raise SystemExit(f"worker runtime {runtime['id']} is not accepting packets")
    packet_path = write_packet_to_inbox(paths, packet_id=packet_id, packet=dict(packet), runtime=dict(runtime))
    mark_packet_delivered(conn, packet_id=packet_id, runtime_id=runtime["id"], packet_path=str(packet_path))
    log_event(
        conn,
        "dispatch.packet_delivered",
        task_id=packet["task_id"],
        agent_id=packet["from_agent_id"],
        payload={"packet_id": packet_id, "runtime_id": runtime["id"], "path": str(packet_path)},
    )
    return runtime["id"], str(packet_path)
