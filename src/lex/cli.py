from __future__ import annotations

import argparse
import curses
import getpass
import hashlib
import json
import os
import re
import signal
import sqlite3
import socket
import sys
import time
from pathlib import Path
from typing import Callable, Sequence

from lex.coordination import (
    WORKER_RUNTIME_STALE_MINUTES,
    capture_git_snapshot,
    cleanup_stale_worker_runtimes,
    complete_session_action,
    create_session_bootstrap,
    enforce_role_contract,
    get_active_bootstrap_for_agent,
    get_active_lease,
    get_active_lease_for_session,
    get_active_session_for_agent,
    get_agent,
    get_dispatch_packet,
    get_latest_session_for_agent,
    get_pending_required_actions,
    get_session,
    get_session_bootstrap,
    get_task,
    get_task_with_owner,
    get_worker_definition,
    get_worker_runtime,
    enforce_roster_preflight,
    retire_agent,
    release_stale_leases,
)
from lex.dx import run_dx
from lex.discovery import LexDiscovery
from lex.db import BUILTIN_SPECIALTIES, connect, derive_event_provenance, detect_path_conflicts, ensure_workspace, fetch_one, find_lex_root, initialize_database, list_specialties, log_event, resolve_paths, run_roster_preflight
from lex.dispatch import (
    VALID_WORKER_APPROVAL_POLICIES,
    command_preview,
    decode_json_list,
    decode_json_object,
    launch_worker_supervisor,
    should_require_packet_approval,
    should_require_runtime_approval,
    stop_runtime_process,
    worker_runtime_dir,
)
from lex.installer import InstallContext, inspect_install_context, install_scaffold
from lex.merge_workflow import apply_proposal, create_merge_packet, resolve_merge_paths, unified_diff
from lex.role_contracts import ROLE_ACTION_LABELS, ROLE_CONTRACTS
from lex.rich_output import (
    console,
    print_ok,
    print_info,
    render_agent_list,
    render_event_list,
    render_inbox_rows,
    render_session_list,
    render_task_list,
    render_task_message_rows,
    render_task_show,
)
from lex.tui import run_tui

CLI_COMMAND = "lx"

AGENT_NAME_RE = re.compile(r"^(codex|claude|cursor|gemini|ci|automated)-[a-z]+-[a-z]+$")
AGENT_ADJECTIVES = (
    "brisk",
    "calm",
    "clear",
    "eager",
    "keen",
    "nimble",
    "quiet",
    "sharp",
    "steady",
    "swift",
)
AGENT_NOUNS = (
    "badger",
    "falcon",
    "heron",
    "ibis",
    "lynx",
    "otter",
    "raven",
    "sparrow",
    "stoat",
    "wren",
)
CANONICAL_AGENT_ROLES = {"dev", "pm", "auditor", "infra"}
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
VALID_RUNTIME_APPROVAL_STATUSES = {"pending_approval", "approved", "rejected", "not_required"}
VALID_RUNTIME_STOP_SIGNALS = {"TERM": signal.SIGTERM, "KILL": signal.SIGKILL, "INT": signal.SIGINT}
VALID_PACKET_APPROVAL_STATUSES = {"pending_approval", "approved", "rejected", "not_required"}


def emit_json(data: object) -> None:
    print(json.dumps(data, indent=2, sort_keys=True))


def require_agent_name(name: str) -> None:
    if not AGENT_NAME_RE.match(name):
        raise SystemExit("agent names must match <agent>-<adjective>-<noun>")


def require_message_type(message_type: str) -> None:
    if message_type not in VALID_MESSAGE_TYPES:
        raise SystemExit(f"invalid message type: {message_type}")


def normalize_agent_role(role: str | None) -> str:
    normalized = (role or "").strip()
    if not normalized:
        return ""
    if normalized not in CANONICAL_AGENT_ROLES:
        allowed = ", ".join(sorted(CANONICAL_AGENT_ROLES))
        raise SystemExit(f"invalid agent role: {normalized}. expected one of: {allowed}")
    return normalized


def normalize_agent_specialty(conn: sqlite3.Connection, specialty: str | None) -> str:
    normalized = (specialty or "").strip()
    if not normalized:
        return ""
    allowed = list_specialties(conn)
    if normalized not in allowed:
        allowed_str = ", ".join(allowed)
        raise SystemExit(f"invalid agent specialty: {normalized}. expected one of: {allowed_str}")
    return normalized


def allocate_agent_name(conn: sqlite3.Connection, kind: str) -> str:
    existing = {
        row["name"]
        for row in conn.execute("SELECT name FROM agents WHERE name LIKE ?", (f"{kind}-%",)).fetchall()
    }
    for adjective in AGENT_ADJECTIVES:
        for noun in AGENT_NOUNS:
            candidate = f"{kind}-{adjective}-{noun}"
            if candidate not in existing:
                return candidate
    raise SystemExit(f"no available generated agent names remain for kind: {kind}")


def build_session_fingerprint(*, kind: str, cwd: str) -> tuple[str, str]:
    hostname = socket.gethostname()
    username = getpass.getuser()
    try:
        tty = os.ttyname(sys.stdin.fileno())
    except OSError:
        tty = "notty"
    pid = os.getpid()
    start_ns = time.time_ns()
    source = f"{kind}|{hostname}|{username}|{cwd}|{tty}|{pid}|{start_ns}"
    fingerprint = hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]
    tty_label = tty.rsplit("/", 1)[-1]
    return fingerprint, f"{hostname}:{tty_label}:{pid}"


def follow_rows(
    fetcher: Callable[[int | None], list[sqlite3.Row]],
    renderer: Callable[[list[sqlite3.Row]], None],
    *,
    initial_rows: list[sqlite3.Row] | None = None,
    initial_since_id: int | None,
    follow: bool,
    poll_interval: float,
    max_polls: int | None,
) -> None:
    last_seen_id = initial_since_id
    polls = 0
    if initial_rows:
        renderer(initial_rows)
        last_seen_id = initial_rows[-1]["id"]
        polls += 1
        if not follow:
            return
        if max_polls is not None and polls >= max_polls:
            return
    while True:
        rows = fetcher(last_seen_id)
        if rows:
            renderer(rows)
            last_seen_id = rows[-1]["id"]
        polls += 1
        if not follow:
            return
        if max_polls is not None and polls >= max_polls:
            return
        time.sleep(poll_interval)




def prompt_text(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        response = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default or ""
    if response:
        return response
    return default or ""


def prompt_int(prompt: str, default: int) -> int:
    while True:
        response = prompt_text(prompt, str(default))
        if response.isdigit():
            return int(response)
        print("Enter a whole number.")


def print_dashboard(root: Path) -> None:
    paths = ensure_workspace(root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent_count = conn.execute("SELECT COUNT(*) FROM agents").fetchone()[0]
    active_session_count = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE status = 'active' AND ended_at IS NULL"
    ).fetchone()[0]
    open_task_count = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE status NOT IN ('done', 'abandoned')"
    ).fetchone()[0]
    inbox_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    print("")
    print(f"lex dashboard  root={root}")
    print(f"agents={agent_count}  active_sessions={active_session_count}  open_tasks={open_task_count}  messages={inbox_count}")
    print("")


def run_interactive_shell(root: Path) -> None:
    while True:
        print_dashboard(root)
        action = prompt_choice(
            "What do you want to do?",
            [
                ("install", "Install or reconfigure lex in this workspace"),
                ("agent_register", "Register an agent"),
                ("session_start", "Start an agent session"),
                ("task_create", "Create a task"),
                ("task_claim", "Claim a task"),
                ("task_list", "List tasks"),
                ("task_show", "Show one task"),
                ("msg_inbox", "View an agent inbox"),
                ("merge_diff", "Show assisted-merge diffs"),
                ("merge_apply", "Apply assisted-merge proposals"),
                ("quit", "Exit"),
            ],
            "task_list",
        )
        print("")
        if action == "quit":
            return
        if action == "install":
            cmd_install(
                argparse.Namespace(
                    root=str(root),
                    agent_files="merge",
                    ignore_policy="runtime",
                    ignore_target="gitignore",
                    assisted_agent="codex",
                    non_interactive=False,
                )
            )
        elif action == "agent_register":
            cmd_agent_register(
                argparse.Namespace(
                    root=str(root),
                    name=prompt_text("Agent name"),
                    kind=prompt_choice(
                        "Agent kind",
                        [("codex", "Codex"), ("claude", "Claude"), ("cursor", "Cursor"), ("gemini", "Gemini")],
                        "codex",
                    ),
                )
            )
        elif action == "session_start":
            cmd_session_start(
                argparse.Namespace(
                    root=str(root),
                    agent=prompt_text("Agent name"),
                    label=prompt_text("Session label", "primary"),
                    cwd=prompt_text("Working directory", str(root)),
                    capability=[],
                )
            )
        elif action == "task_create":
            created_by = prompt_text("Created by agent (optional)")
            cmd_task_create(
                argparse.Namespace(
                    root=str(root),
                    title=prompt_text("Task title"),
                    slug="",
                    description=prompt_text("Description (optional)"),
                    priority=prompt_int("Priority", 2),
                    created_by=created_by or None,
                    parent_task=None,
                    delegation_mode=prompt_choice(
                        "Delegation mode",
                        [("direct", "Direct"), ("hypervisor", "Hypervisor")],
                        "direct",
                    ),
                    path=[],
                )
            )
        elif action == "task_claim":
            cmd_task_claim(
                argparse.Namespace(
                    root=str(root),
                    task_id=prompt_int("Task id", 1),
                    agent=prompt_text("Agent name"),
                    ttl_minutes=prompt_int("Lease TTL minutes", 30),
                )
            )
        elif action == "task_list":
            cmd_task_list(argparse.Namespace(root=str(root), json=False))
        elif action == "task_show":
            cmd_task_show(
                argparse.Namespace(root=str(root), task_id=prompt_int("Task id", 1), json=False)
            )
        elif action == "msg_inbox":
            cmd_msg_inbox(
                argparse.Namespace(
                    root=str(root),
                    agent=prompt_text("Agent name"),
                    limit=20,
                    json=False,
                    since_id=None,
                    follow=False,
                    poll_interval=2.0,
                    max_polls=None,
                )
            )
        elif action == "merge_diff":
            cmd_merge_diff(argparse.Namespace(root=str(root)))
        elif action == "merge_apply":
            cmd_merge_apply(argparse.Namespace(root=str(root)))
        print("")


def cmd_init(args: argparse.Namespace) -> None:
    paths = ensure_workspace(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    log_event(conn, "workspace.initialized", payload={"root": str(paths.root)})
    conn.commit()
    print(f"initialized lex in {paths.lex_dir}")


def prompt_choice(prompt: str, options: list[tuple[str, str]], default: str) -> str:
    while True:
        print(prompt)
        for index, (value, label) in enumerate(options, start=1):
            suffix = " [default]" if value == default else ""
            print(f"  {index}. {label}{suffix}")
        try:
            response = input("> ").strip()
        except EOFError:
            return "quit" if any(value == "quit" for value, _ in options) else default
        if not response:
            return default
        if response.isdigit():
            selection = int(response)
            if 1 <= selection <= len(options):
                return options[selection - 1][0]
        for value, _label in options:
            if response == value:
                return value
        print("Invalid selection. Enter a number or one of the option values.")


def print_install_summary(context: InstallContext) -> None:
    print(f"Installing lex into {context.root}")
    print("Detected:")
    print(f"  git checkout: {'yes' if context.has_git_dir else 'no'}")
    print(f"  .gitignore: {'yes' if context.has_gitignore else 'no'}")
    print(f"  AGENTS.md: {'yes' if context.has_agents_file else 'no'}")
    print(f"  CLAUDE.md: {'yes' if context.has_claude_file else 'no'}")
    print("")


def choose_agent_file_mode(context: InstallContext) -> str:
    if context.has_agents_file or context.has_claude_file:
        prompt = (
            "How should lex handle existing root agent files?\n"
            "This controls whether AGENTS.md and CLAUDE.md get a small managed lex bridge block."
        )
        default = "merge"
        options = [
            ("preserve", "Preserve existing root agent files exactly as they are and install only the .lex scaffold"),
            ("merge", "Merge a managed lex block into existing root agent files without overwriting other content"),
            ("assisted", "Preserve root files now and prepare an assisted semantic merge packet for an agent"),
            ("overwrite", "Overwrite root agent files with lex bridge files"),
        ]
    else:
        prompt = "How should lex expose itself to root agent files?"
        default = "merge"
        options = [
            ("preserve", "Do not create root bridge files; install only the .lex scaffold"),
            ("merge", "Create root AGENTS.md and CLAUDE.md bridge files that point into .lex"),
            ("assisted", "Install the .lex scaffold and prepare an assisted merge packet before creating root bridge files"),
            ("overwrite", "Write fresh root bridge files even if they appear later in the install"),
        ]
    return prompt_choice(prompt, options, default)


def choose_ignore_policy(context: InstallContext) -> tuple[str, str]:
    if context.has_git_dir:
        prompt = (
            "How should lex handle Git ignore rules?\n"
            "Choose whether to ignore nothing, only runtime state, or the full lex workflow locally."
        )
        default = "runtime:gitignore"
        options = [
            ("runtime:gitignore", "Ignore only .lex/lex.db and .lex/runtime/ in the shared .gitignore"),
            ("all:local-exclude", "Keep lex local-only by writing ignore rules to .git/info/exclude"),
            ("none:gitignore", "Do not add any ignore rules"),
        ]
    else:
        prompt = "How should lex handle ignore rules in this folder?"
        default = "runtime:gitignore"
        options = [
            ("runtime:gitignore", "Create a .gitignore entry for lex runtime state"),
            ("none:gitignore", "Do not create ignore rules"),
        ]
    combined = prompt_choice(prompt, options, default)
    ignore_policy, ignore_target = combined.split(":", 1)
    return ignore_policy, ignore_target


def choose_assisted_agent() -> str:
    return prompt_choice(
        "Which agent should prepare the assisted merge proposal?",
        [
            ("codex", "Codex"),
            ("claude", "Claude"),
            ("manual", "Manual packet only"),
        ],
        "codex",
    )


def resolve_install_options(args: argparse.Namespace, context: InstallContext) -> tuple[str, str, str, str]:
    if args.non_interactive:
        return args.agent_files, args.ignore_policy, args.ignore_target, args.assisted_agent

    print_install_summary(context)
    agent_files = choose_agent_file_mode(context)
    print("")
    ignore_policy, ignore_target = choose_ignore_policy(context)
    assisted_agent = args.assisted_agent
    if agent_files == "assisted":
        print("")
        assisted_agent = choose_assisted_agent()
    print("")
    print("Installer choices:")
    print(f"  agent file mode: {agent_files}")
    print(f"  ignore policy: {ignore_policy}")
    print(f"  ignore target: {ignore_target}")
    if agent_files == "assisted":
        print(f"  assisted agent: {assisted_agent}")
    print("")
    return agent_files, ignore_policy, ignore_target, assisted_agent


def cmd_install(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    context = inspect_install_context(root)
    agent_files, ignore_policy, ignore_target, assisted_agent = resolve_install_options(args, context)
    result = install_scaffold(
        root,
        agent_files=agent_files,
        ignore_policy=ignore_policy,
        ignore_target=ignore_target,
    )
    paths = ensure_workspace(root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    log_event(
        conn,
        "workspace.installed",
        payload={
            "root": str(root),
            "agent_files": agent_files,
            "ignore_policy": ignore_policy,
            "ignore_target": ignore_target,
        },
    )
    if agent_files == "assisted":
        merge_paths = create_merge_packet(root, agent_kind=assisted_agent)
        log_event(
            conn,
            "merge.packet_created",
            payload={"agent_kind": assisted_agent, "plan_path": str(merge_paths.plan_path)},
        )
    conn.commit()
    print(f"installed lex in {root}")
    if result.created_files:
        print("created:")
        for path in result.created_files:
            print(f"  {path}")
    if result.updated_files:
        print("updated:")
        for path in result.updated_files:
            print(f"  {path}")
    if result.skipped_files:
        print("skipped existing scaffold:")
        for path in result.skipped_files:
            print(f"  {path}")
    if result.warnings:
        print("warnings:")
        for warning in result.warnings:
            print(f"  {warning}")
    if agent_files == "assisted":
        print("next steps:")
        print(f"  review {root / '.lex' / 'runtime' / 'install-merge-plan.md'}")
        print("  have the selected agent write proposals into .lex/runtime/install-merge-proposal/")
        print(f"  run `{CLI_COMMAND} merge diff` and then `{CLI_COMMAND} merge apply` once approved")


def cmd_merge_plan(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    paths = create_merge_packet(root, agent_kind=args.agent)
    print(f"created assisted merge packet in {paths.lex_runtime}")
    print(f"plan: {paths.plan_path}")
    print(f"context: {paths.context_dir}")
    print(f"proposal target: {paths.proposal_dir}")


def cmd_merge_diff(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    paths = resolve_merge_paths(root)
    diffs = []
    for filename in ("AGENTS.md", "CLAUDE.md"):
        diff_text = unified_diff(root / filename, paths.proposal_dir / filename, filename)
        if diff_text:
            diffs.append(diff_text)
    if not diffs:
        print("no proposal diffs found")
        return
    print("\n".join(diffs).rstrip())


def cmd_merge_apply(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    applied = apply_proposal(root)
    if not applied:
        print("no proposal files were applied")
        return
    paths = ensure_workspace(root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    log_event(conn, "merge.applied", payload={"files": applied})
    conn.commit()
    print("applied proposal files:")
    for filename in applied:
        print(f"  {filename}")


def cmd_agent_register(args: argparse.Namespace) -> None:
    require_agent_name(args.name)
    paths = ensure_workspace(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    role = normalize_agent_role(args.role)
    specialty = normalize_agent_specialty(conn, args.specialty)
    try:
        conn.execute(
            "INSERT INTO agents (name, kind, role, specialty, status) VALUES (?, ?, ?, ?, 'active')",
            (args.name, args.kind, role, specialty),
        )
    except sqlite3.IntegrityError as exc:
        raise SystemExit(f"failed to register agent: {exc}") from exc
    agent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    log_event(
        conn,
        "agent.registered",
        agent_id=agent_id,
        payload={"name": args.name, "kind": args.kind, "role": role, "specialty": specialty},
    )
    conn.commit()
    print_ok(f"registered {args.name}")


def cmd_agent_identify(args: argparse.Namespace) -> None:
    paths = ensure_workspace(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    name = args.name or allocate_agent_name(conn, args.kind)
    require_agent_name(name)
    role = normalize_agent_role(args.role)
    specialty = normalize_agent_specialty(conn, args.specialty)
    try:
        conn.execute(
            "INSERT INTO agents (name, kind, role, specialty, status) VALUES (?, ?, ?, ?, 'active')",
            (name, args.kind, role, specialty),
        )
    except sqlite3.IntegrityError as exc:
        raise SystemExit(f"failed to identify agent: {exc}") from exc
    agent_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    log_event(
        conn,
        "agent.registered",
        agent_id=agent_id,
        payload={"name": name, "kind": args.kind, "role": role, "specialty": specialty, "identified": True},
    )
    conn.commit()
    if args.json:
        emit_json(
            {"id": agent_id, "name": name, "kind": args.kind, "role": role, "specialty": specialty, "status": "active"}
        )
        return
    print_ok(f"identified {name}")


def cmd_agent_update_role(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent = get_agent(conn, args.agent)
    role = normalize_agent_role(args.role)
    specialty = normalize_agent_specialty(conn, args.specialty)
    conn.execute(
        "UPDATE agents SET role = ?, specialty = ? WHERE id = ?",
        (role, specialty, agent["id"]),
    )
    log_event(
        conn,
        "agent.role_changed",
        agent_id=agent["id"],
        payload={
            "name": args.agent,
            "from": agent["role"],
            "from_specialty": agent["specialty"],
            "to": role,
            "to_specialty": specialty,
        },
    )
    conn.commit()
    label = role if not specialty else f"{role}/{specialty}"
    print_ok(f"{args.agent} role -> {label or '-'}")


def cmd_agent_list(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    rows = conn.execute("SELECT id, name, kind, role, specialty, status, created_at FROM agents ORDER BY id").fetchall()
    if args.json:
        emit_json([dict(row) for row in rows])
        return
    render_agent_list(rows)


def cmd_agent_preflight(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    issues = run_roster_preflight(conn)
    if args.json:
        emit_json(issues)
        return
    if not issues:
        print_ok("roster preflight passed — no drift detected")
        return
    from lex.coordination import PREFLIGHT_FATAL_KINDS
    for issue in issues:
        severity = "FAIL" if issue["kind"] in PREFLIGHT_FATAL_KINDS else "WARN"
        print(f"[{severity}] [{issue['kind']}] {issue['agent_name']}: {issue['detail']}")
    fatal = [i for i in issues if i["kind"] in PREFLIGHT_FATAL_KINDS]
    if fatal:
        raise SystemExit(f"preflight failed: {len(fatal)} fatal issue(s) must be resolved")


def cmd_agent_retire(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent = get_agent(conn, args.agent)
    if agent["status"] == "retired":
        raise SystemExit(f"agent {args.agent} is already retired")
    retiring_agent = get_agent(conn, args.by) if args.by else None
    # Warn if agent still owns active tasks
    active_tasks = conn.execute(
        """
        SELECT id, title FROM tasks
        WHERE owner_agent_id = ?
          AND status IN ('claimed', 'in_progress', 'blocked', 'review_requested', 'handoff_pending')
        """,
        (agent["id"],),
    ).fetchall()
    if active_tasks and not args.force:
        task_list = ", ".join(f"#{t['id']} {t['title']!r}" for t in active_tasks)
        raise SystemExit(
            f"agent {args.agent} owns active tasks: {task_list}\n"
            "Reassign tasks first, or use --force to retire anyway."
        )
    retire_agent(
        conn,
        agent_id=agent["id"],
        agent_name=agent["name"],
        retiring_agent_id=retiring_agent["id"] if retiring_agent else None,
    )
    conn.commit()
    print_ok(f"agent {args.agent} retired")
    if active_tasks:
        print_info(f"note: {len(active_tasks)} active task(s) were left unassigned — reassign them to unblock the roster")


def cmd_specialty_add(args: argparse.Namespace) -> None:
    paths = ensure_workspace(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    specialty = (args.name or "").strip()
    if not specialty:
        raise SystemExit("specialty name is required")
    if specialty in BUILTIN_SPECIALTIES:
        print_ok(f"specialty {specialty} already available")
        return
    try:
        conn.execute("INSERT INTO specialties (name) VALUES (?)", (specialty,))
    except sqlite3.IntegrityError:
        print_ok(f"specialty {specialty} already available")
        return
    log_event(conn, "specialty.added", payload={"name": specialty})
    conn.commit()
    print_ok(f"added specialty {specialty}")


def cmd_specialty_list(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    rows = [{"name": name, "source": "builtin" if name in BUILTIN_SPECIALTIES else "custom"} for name in list_specialties(conn)]
    if args.json:
        emit_json(rows)
        return
    for row in rows:
        print(f"{row['name']:<16} {row['source']}")


def cmd_session_start(args: argparse.Namespace) -> None:
    paths = ensure_workspace(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent = get_agent(conn, args.agent)
    enforce_roster_preflight(conn, exclude_reconnecting_agent_id=agent["id"])
    fingerprint = args.fingerprint
    fingerprint_label = args.fingerprint_label
    if fingerprint is None or fingerprint_label is None:
        fingerprint, fingerprint_label = build_session_fingerprint(kind=agent["kind"], cwd=args.cwd)
    conflicting_sessions = conn.execute(
        """
        SELECT s.id, s.label, s.fingerprint_label
        FROM sessions s
        WHERE s.agent_id = ?
          AND s.status = 'active'
          AND s.ended_at IS NULL
          AND s.fingerprint IS NOT NULL
          AND s.fingerprint != ?
        ORDER BY s.id DESC
        """,
        (agent["id"], fingerprint),
    ).fetchall()
    git = capture_git_snapshot(args.cwd)
    conn.execute(
        """
        INSERT INTO sessions (
            agent_id, label, fingerprint, fingerprint_label, status, cwd, capabilities_json,
            git_branch, git_base_ref, git_dirty, git_staged_files_json, git_changed_files_json
        )
        VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            agent["id"],
            args.label,
            fingerprint,
            fingerprint_label,
            args.cwd,
            json.dumps({"capabilities": args.capability or []}),
            git["git_branch"],
            git["git_base_ref"],
            git["git_dirty"],
            git["git_staged_files_json"],
            git["git_changed_files_json"],
        ),
    )
    session_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    log_event(
        conn,
        "session.started",
        agent_id=agent["id"],
        session_id=session_id,
        payload={
            "label": args.label,
            "cwd": args.cwd,
            "fingerprint": fingerprint,
            "fingerprint_label": fingerprint_label,
            "git_branch": git["git_branch"],
        },
    )
    create_session_bootstrap(conn, session_id=session_id, agent=agent)
    conn.commit()
    print_ok(f"started session {session_id} for {args.agent}")
    print_info(f"instance {fingerprint_label} ({fingerprint})")
    print_info(f"bootstrap packet ready: {CLI_COMMAND} session bootstrap-show {session_id}")
    if conflicting_sessions:
        other = conflicting_sessions[0]
        print_info(
            "warning: another active session exists for this agent "
            f"(session {other['id']} {other['label'] or '-'} @ {other['fingerprint_label'] or '-'})"
        )


def cmd_session_bootstrap_show(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    session = get_session(conn, args.session_id)
    bootstrap = get_session_bootstrap(conn, args.session_id)
    pending_actions = get_pending_required_actions(conn, args.session_id)
    data = {
        "session_id": args.session_id,
        "agent": session["agent_name"],
        "role_contract": json.loads(bootstrap["role_contract_json"]),
        "system_prompt": bootstrap["system_prompt"],
        "workflow_template": json.loads(bootstrap["workflow_template_json"]),
        "memory": json.loads(bootstrap["memory_json"]),
        "required_actions": json.loads(bootstrap["required_actions_json"]),
        "pending_actions": pending_actions,
        "acknowledged_at": bootstrap["acknowledged_at"],
        "acknowledged_by": bootstrap["acknowledged_by"],
        "continuity_from_session_id": bootstrap["continuity_from_session_id"],
    }
    if args.json:
        emit_json(data)
        return
    print(f"session {args.session_id} bootstrap for {session['agent_name']}")
    print(f"role: {json.loads(bootstrap['role_contract_json']).get('role') or '-'}")
    if bootstrap["continuity_from_session_id"] is not None:
        print(f"continuity from session: {bootstrap['continuity_from_session_id']}")
    print("")
    print("system prompt:")
    print(f"  {bootstrap['system_prompt']}")
    print("")
    print("workflow template:")
    for item in json.loads(bootstrap["workflow_template_json"]):
        print(f"  - {item}")
    print("")
    print("required first actions:")
    for action in json.loads(bootstrap["required_actions_json"]):
        label = ROLE_ACTION_LABELS.get(action, action)
        suffix = " (pending)" if action in pending_actions else " (done)"
        print(f"  - {label}{suffix}")
    print("")
    print("hydrated memory:")
    memory = json.loads(bootstrap["memory_json"])
    print(f"  active tasks: {len(memory.get('active_tasks', []))}")
    print(f"  subscriptions: {len(memory.get('subscriptions', []))}")
    print(f"  inbox items: {len(memory.get('inbox', []))}")
    print(f"  recent decisions: {len(memory.get('recent_decisions', []))}")


def cmd_prompt_create(args: argparse.Namespace) -> None:
    contract = ROLE_CONTRACTS.get(args.role.lower())
    if contract is None:
        valid = ", ".join(sorted(ROLE_CONTRACTS))
        raise SystemExit(f"unknown role '{args.role}' — valid roles: {valid}")

    live_state: dict | None = None
    if args.agent:
        paths = resolve_paths(args.root)
        conn = connect(paths.db_path)
        initialize_database(conn)
        agent = fetch_one(conn, "SELECT * FROM agents WHERE name = ?", (args.agent,))
        if agent is None:
            raise SystemExit(f"agent not found: {args.agent}")
        live_state = _fetch_bootstrap_memory(conn, agent["id"])

    if args.json:
        data: dict = {
            "role": contract.role,
            "system_prompt": contract.system_prompt,
            "workflow": list(contract.workflow_template),
            "allowed_verbs": list(contract.allowed_verbs),
            "blocked_verbs": list(contract.blocked_verbs),
            "required_first_actions": [
                {"key": a, "label": ROLE_ACTION_LABELS.get(a, a)}
                for a in contract.required_first_actions
            ],
        }
        if live_state is not None:
            data["live_state"] = live_state
        emit_json(data)
        return

    lines: list[str] = []
    lines.append(f"# Lex — {contract.role.upper()} Prompt")
    lines.append("")
    lines.append("## Identity")
    lines.append(contract.system_prompt)
    lines.append("")
    lines.append("## Protocol")
    for rule in (
        "Claim a task before making substantive changes.",
        "Renew leases while active on a claimed task.",
        "Use task-threaded messages for progress, blockers, and handoffs.",
        "Only the current owner may change task status.",
        "Hypervisor delegation creates child tasks instead of sharing write ownership.",
    ):
        lines.append(f"- {rule}")
    lines.append("")
    lines.append("## Workflow")
    for i, step in enumerate(contract.workflow_template, 1):
        lines.append(f"{i}. {step}")
    lines.append("")
    lines.append("## Required First Actions")
    for action in contract.required_first_actions:
        lines.append(f"- {ROLE_ACTION_LABELS.get(action, action)}")
    lines.append("")
    lines.append("## Permissions")
    allowed = ", ".join(contract.allowed_verbs) if contract.allowed_verbs else "all"
    blocked = ", ".join(contract.blocked_verbs) if contract.blocked_verbs else "none"
    lines.append(f"Allowed verbs: {allowed}")
    lines.append(f"Blocked verbs: {blocked}")

    if live_state is not None:
        lines.append("")
        lines.append("## Live State")
        active = live_state.get("active_tasks", [])
        if active:
            lines.append("")
            lines.append("### Active Tasks")
            for t in active:
                lines.append(f"- [{t['id']}] {t['title']} ({t['status']}, priority {t['priority']})")
        inbox = live_state.get("inbox", [])
        if inbox:
            lines.append("")
            lines.append("### Inbox")
            for m in inbox:
                task_ref = f" [task {m['task_id']}]" if m.get("task_id") else ""
                lines.append(f"- from {m['from_name']}{task_ref}: {m['subject'] or m['type']}")
        subs = live_state.get("subscriptions", [])
        if subs:
            lines.append("")
            lines.append("### Watched Tasks")
            for w in subs:
                lines.append(f"- [{w['task_id']}] {w['title']}")

    print("\n".join(lines))


def cmd_session_bootstrap_ack(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    session = get_session(conn, args.session_id)
    get_session_bootstrap(conn, args.session_id)
    conn.execute(
        """
        UPDATE session_bootstraps
        SET acknowledged_at = CURRENT_TIMESTAMP,
            acknowledged_by = ?
        WHERE session_id = ?
        """,
        (args.by, args.session_id),
    )
    log_event(
        conn,
        "session.bootstrap_acknowledged",
        agent_id=session["agent_id"],
        session_id=args.session_id,
        payload={"acknowledged_by": args.by},
    )
    conn.commit()
    print_ok(f"acknowledged bootstrap for session {args.session_id}")


def cmd_session_action_complete(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    session = get_session(conn, args.session_id)
    completed = complete_session_action(
        conn,
        session_id=args.session_id,
        action_key=args.action_key,
        detail={"note": args.note or ""},
    )
    if not completed:
        raise SystemExit(f"action is not required for this session: {args.action_key}")
    conn.commit()
    pending = get_pending_required_actions(conn, args.session_id)
    print_ok(f"recorded session action {args.action_key} for session {args.session_id}")
    if pending:
        print_info(f"pending actions: {', '.join(pending)}")
    else:
        print_info(f"session {args.session_id} is fully bootstrapped for {session['agent_name']}")


def cmd_session_heartbeat(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    session = get_session(conn, args.session_id)
    git = capture_git_snapshot(session["cwd"])
    conn.execute(
        """
        UPDATE sessions
        SET heartbeat_at = CURRENT_TIMESTAMP,
            git_dirty = ?,
            git_staged_files_json = ?
        WHERE id = ? AND status = 'active' AND ended_at IS NULL
        """,
        (git["git_dirty"], git["git_staged_files_json"], args.session_id),
    )
    log_event(
        conn,
        "session.heartbeat",
        agent_id=session["agent_id"],
        session_id=args.session_id,
        payload={"label": session["label"], "git_branch": session["git_branch"]},
    )
    conn.commit()
    print_ok(f"heartbeat recorded for session {args.session_id}")


# ---------------------------------------------------------------------------
# Hook handlers — Claude Code hook integration
# ---------------------------------------------------------------------------

_HOOK_HEARTBEAT_DEBOUNCE_SECONDS = 30
_HOOK_LEASE_RENEW_THRESHOLD = 0.25  # renew when < 25% TTL remaining


def _hook_resolve_paths() -> tuple[str, LexPaths | None]:
    """Return (mode, paths) where mode is 'attached', 'loose', or 'ignore'."""
    from datetime import datetime, timezone

    lex_root_env = os.environ.get("LEX_ROOT")
    session_id_env = os.environ.get("LEX_SESSION_ID")

    if session_id_env:
        # Attached mode: explicit root or cwd
        paths = resolve_paths(lex_root_env) if lex_root_env else find_lex_root()
        if paths and paths.db_path.exists():
            return "attached", paths
        # Fall through to loose if DB not found

    # Loose mode: walk up from LEX_ROOT or cwd
    start = Path(lex_root_env).resolve() if lex_root_env else None
    paths = find_lex_root(start)
    if paths:
        return "loose", paths

    return "ignore", None


def _hook_read_stdin() -> dict:
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _hook_resolve_agent_id(conn: sqlite3.Connection) -> "int | None":
    agent_name = os.environ.get("LEX_AGENT", "claude")
    row = fetch_one(conn, "SELECT id FROM agents WHERE name = ? AND status = 'active'", (agent_name,))
    if row:
        return row["id"]
    # Fallback: first active claude-kind agent
    row = fetch_one(conn, "SELECT id FROM agents WHERE kind = 'claude' AND status = 'active' ORDER BY id LIMIT 1", ())
    return row["id"] if row else None


def _hook_should_heartbeat(conn: sqlite3.Connection, session_id: int) -> bool:
    from datetime import datetime, timezone, timedelta

    row = fetch_one(conn, "SELECT heartbeat_at FROM sessions WHERE id = ? AND status = 'active'", (session_id,))
    if not row or not row["heartbeat_at"]:
        return True
    try:
        last = datetime.fromisoformat(row["heartbeat_at"]).replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() >= _HOOK_HEARTBEAT_DEBOUNCE_SECONDS
    except ValueError:
        return True


def _hook_write_heartbeat(conn: sqlite3.Connection, session_id: int, agent_id: int) -> None:
    conn.execute(
        """
        UPDATE sessions
        SET heartbeat_at = CURRENT_TIMESTAMP
        WHERE id = ? AND status = 'active' AND ended_at IS NULL
        """,
        (session_id,),
    )
    log_event(conn, "session.heartbeat", agent_id=agent_id, session_id=session_id,
              payload={"source": "cc_hook"})


def _hook_maybe_renew_lease(conn: sqlite3.Connection, session_id: int, agent_id: int) -> None:
    from datetime import datetime, timezone

    lease = get_active_lease_for_session(conn, session_id)
    if not lease:
        return
    try:
        acquired = datetime.fromisoformat(lease["acquired_at"]).replace(tzinfo=timezone.utc)
        expires = datetime.fromisoformat(lease["expires_at"]).replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        total_ttl = (expires - acquired).total_seconds()
        remaining = (expires - now).total_seconds()
        if total_ttl <= 0 or remaining / total_ttl >= _HOOK_LEASE_RENEW_THRESHOLD:
            return
        # Renew to the same TTL duration
        ttl_minutes = max(1, int(total_ttl / 60))
        conn.execute(
            """
            UPDATE task_leases
            SET heartbeat_at = CURRENT_TIMESTAMP,
                expires_at = datetime('now', ?)
            WHERE id = ? AND state = 'active' AND released_at IS NULL
            """,
            (f"+{ttl_minutes} minutes", lease["id"]),
        )
        log_event(conn, "lease.renewed", task_id=lease["task_id"], agent_id=agent_id,
                  session_id=session_id, payload={"ttl_minutes": ttl_minutes, "source": "cc_hook"})
    except (ValueError, TypeError):
        pass


def cmd_hook_claude_stop(args: argparse.Namespace) -> None:
    _hook_read_stdin()  # consume stdin; payload unused for stop
    mode, paths = _hook_resolve_paths()
    if mode == "ignore" or paths is None:
        return

    conn = connect(paths.db_path)
    initialize_database(conn)

    if mode == "attached":
        try:
            session_id = int(os.environ["LEX_SESSION_ID"])
        except (KeyError, ValueError):
            mode = "loose"
        else:
            try:
                session = get_session(conn, session_id)
            except SystemExit:
                mode = "loose"
            else:
                if session["status"] != "active" or session["ended_at"] is not None:
                    mode = "loose"
                else:
                    if _hook_should_heartbeat(conn, session_id):
                        _hook_write_heartbeat(conn, session_id, session["agent_id"])
                        _hook_maybe_renew_lease(conn, session_id, session["agent_id"])
                    conn.commit()
                    return

    # Loose mode
    agent_id = _hook_resolve_agent_id(conn)
    log_event(conn, "claude.hook.stop", agent_id=agent_id,
              payload={"provenance": "loose", "root": str(paths.root)})
    conn.commit()


def cmd_hook_claude_user_prompt_submit(args: argparse.Namespace) -> None:
    _hook_read_stdin()
    mode, paths = _hook_resolve_paths()
    if mode == "ignore" or paths is None:
        return

    conn = connect(paths.db_path)
    initialize_database(conn)

    if mode == "attached":
        try:
            session_id = int(os.environ["LEX_SESSION_ID"])
        except (KeyError, ValueError):
            mode = "loose"
        else:
            try:
                session = get_session(conn, session_id)
            except SystemExit:
                mode = "loose"
            else:
                if session["status"] != "active" or session["ended_at"] is not None:
                    mode = "loose"
                else:
                    if _hook_should_heartbeat(conn, session_id):
                        _hook_write_heartbeat(conn, session_id, session["agent_id"])
                    conn.commit()
                    return

    # Loose mode
    agent_id = _hook_resolve_agent_id(conn)
    log_event(conn, "claude.hook.user_prompt_submit", agent_id=agent_id,
              payload={"provenance": "loose", "root": str(paths.root)})
    conn.commit()


def cmd_hook_claude_post_tool_use(args: argparse.Namespace) -> None:
    payload = _hook_read_stdin()
    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    file_path = tool_input.get("file_path") or tool_input.get("path", "")

    mode, paths = _hook_resolve_paths()
    if mode == "ignore" or paths is None:
        return

    conn = connect(paths.db_path)
    initialize_database(conn)

    event_payload: dict = {"tool": tool_name, "file": file_path}

    if mode == "attached":
        try:
            session_id = int(os.environ["LEX_SESSION_ID"])
        except (KeyError, ValueError):
            mode = "loose"
        else:
            try:
                session = get_session(conn, session_id)
            except SystemExit:
                mode = "loose"
            else:
                if session["status"] != "active" or session["ended_at"] is not None:
                    mode = "loose"
                else:
                    task_id_env = os.environ.get("LEX_TASK_ID")
                    task_id = int(task_id_env) if task_id_env and task_id_env.isdigit() else None
                    event_payload["provenance"] = "attached"
                    log_event(conn, "claude.hook.post_tool_use", agent_id=session["agent_id"],
                              session_id=session_id, task_id=task_id, payload=event_payload)
                    conn.commit()
                    return

    # Loose mode
    agent_id = _hook_resolve_agent_id(conn)
    event_payload["provenance"] = "loose"
    event_payload["root"] = str(paths.root)
    log_event(conn, "claude.hook.post_tool_use", agent_id=agent_id, payload=event_payload)
    conn.commit()


def cmd_session_end(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    session = get_session(conn, args.session_id)
    conn.execute(
        """
        UPDATE sessions
        SET status = 'ended',
            heartbeat_at = CURRENT_TIMESTAMP,
            ended_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (args.session_id,),
    )
    released_count = release_stale_leases(conn)
    log_event(
        conn,
        "session.ended",
        agent_id=session["agent_id"],
        session_id=args.session_id,
        payload={"label": session["label"], "released_leases": released_count},
    )
    conn.commit()
    print_ok(f"ended session {args.session_id}")


def cmd_session_list(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    params: list[object] = []
    query = """
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
            s.started_at,
            s.heartbeat_at,
            s.ended_at
        FROM sessions s
        JOIN agents a ON a.id = s.agent_id
    """
    if args.active_only:
        query += " WHERE s.status = 'active' AND s.ended_at IS NULL"
    query += " ORDER BY s.id DESC"
    rows = conn.execute(query, tuple(params)).fetchall()
    if args.json:
        emit_json([dict(row) for row in rows])
        return
    render_session_list(rows)


def cmd_task_create(args: argparse.Namespace) -> None:
    paths = ensure_workspace(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    creator_id = None
    if args.created_by:
        creator = get_agent(conn, args.created_by)
        enforce_role_contract(conn, agent=creator, verb="task_create", allow_override=args.force_role_override)
        creator_id = creator["id"]
    claimed_paths = args.path or []
    conn.execute(
        """
        INSERT INTO tasks (
            slug, title, description, status, priority, parent_task_id,
            delegation_mode, claimed_paths_json, created_by_agent_id
        )
        VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?)
        """,
        (
            args.slug,
            args.title,
            args.description or "",
            args.priority,
            args.parent_task,
            args.delegation_mode,
            json.dumps(claimed_paths),
            creator_id,
        ),
    )
    task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    log_event(
        conn,
        "task.created",
        task_id=task_id,
        agent_id=creator_id,
        payload={"title": args.title, "parent_task_id": args.parent_task},
    )
    conn.commit()
    print_ok(f"created task {task_id}")


def cmd_task_list(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    release_stale_leases(conn)
    rows = conn.execute(
        """
        SELECT
            t.id,
            t.title,
            t.status,
            t.priority,
            t.parent_task_id,
            a.name AS owner_name
        FROM tasks t
        LEFT JOIN agents a ON a.id = t.owner_agent_id
        ORDER BY t.id
        """
    ).fetchall()
    if args.json:
        emit_json([dict(row) for row in rows])
        return
    render_task_list(rows)


def cmd_task_show(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    release_stale_leases(conn)
    task = get_task_with_owner(conn, args.task_id)
    if args.agent:
        agent = get_agent(conn, args.agent)
        active_session = get_active_session_for_agent(conn, agent["id"])
        if active_session is not None:
            action_key = None
            if agent["role"] == "pm" and task["parent_task_id"] is None:
                action_key = "inspect_open_child_tasks"
            elif agent["role"] == "dev":
                action_key = "inspect_assigned_tasks"
            elif agent["role"] == "auditor":
                action_key = "inspect_review_queue"
            elif agent["role"] == "infra":
                action_key = "inspect_integration_queue"
            if action_key is not None:
                complete_session_action(
                    conn,
                    session_id=active_session["id"],
                    action_key=action_key,
                    detail={"task_id": args.task_id},
                )
                conn.commit()
    lease = get_active_lease(conn, args.task_id)
    owner_session = None
    if task["owner_agent_id"] is not None:
        owner_session = get_active_session_for_agent(conn, task["owner_agent_id"])
    children = conn.execute(
        """
        SELECT
            t.id,
            t.title,
            t.status,
            a.name AS owner_name
        FROM tasks t
        LEFT JOIN agents a ON a.id = t.owner_agent_id
        WHERE t.parent_task_id = ?
        ORDER BY t.id
        """,
        (args.task_id,),
    ).fetchall()
    recent_messages = conn.execute(
        """
        SELECT
            m.id,
            sender.name AS from_name,
            recipient.name AS to_name,
            m.type,
            m.body,
            m.created_at
        FROM messages m
        JOIN agents sender ON sender.id = m.from_agent_id
        LEFT JOIN agents recipient ON recipient.id = m.to_agent_id
        WHERE m.task_id = ?
        ORDER BY m.id DESC
        LIMIT 10
        """,
        (args.task_id,),
    ).fetchall()
    data = {
        "id": task["id"],
        "slug": task["slug"],
        "title": task["title"],
        "description": task["description"],
        "status": task["status"],
        "priority": task["priority"],
        "owner": task["owner_name"],
        "owner_role": task["owner_role"],
        "owner_specialty": task["owner_specialty"],
        "created_by": task["created_by_name"],
        "parent_task_id": task["parent_task_id"],
        "delegation_mode": task["delegation_mode"],
        "claimed_paths": json.loads(task["claimed_paths_json"]),
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
        "completed_at": task["completed_at"],
        "active_lease": dict(lease) if lease else None,
        "active_owner_session": dict(owner_session) if owner_session else None,
        "children": [dict(row) for row in children],
        "recent_messages": [dict(row) for row in recent_messages],
    }
    if args.json:
        emit_json(data)
        return
    render_task_show(data, lease, owner_session, children, recent_messages)


def cmd_task_delegate(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    parent_task = get_task(conn, args.parent_task_id)
    owner = get_agent(conn, args.owner_agent)
    enforce_role_contract(conn, agent=owner, verb="task_delegate", allow_override=args.force_role_override)
    assignee = get_agent(conn, args.assignee_agent)
    assignee_session = get_active_session_for_agent(conn, assignee["id"])
    if parent_task["owner_agent_id"] not in (None, owner["id"]):
        raise SystemExit("only the parent owner may delegate child tasks")
    if parent_task["delegation_mode"] != "hypervisor":
        raise SystemExit("parent task must be in hypervisor delegation mode")
    conn.execute(
        """
        INSERT INTO tasks (
            slug, title, description, status, priority, owner_agent_id, parent_task_id,
            delegation_mode, claimed_paths_json, created_by_agent_id
        )
        VALUES (?, ?, ?, 'claimed', ?, ?, ?, 'direct', ?, ?)
        """,
        (
            args.slug,
            args.title,
            args.description or "",
            args.priority if args.priority is not None else parent_task["priority"],
            assignee["id"],
            args.parent_task_id,
            json.dumps(args.path or []),
            owner["id"],
        ),
    )
    child_task_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        """
        INSERT INTO task_leases (task_id, agent_id, session_id, expires_at)
        VALUES (?, ?, ?, datetime('now', ?))
        """,
        (child_task_id, assignee["id"], assignee_session["id"] if assignee_session else None, f"+{args.ttl_minutes} minutes"),
    )
    conn.execute(
        """
        INSERT INTO messages (task_id, from_agent_id, to_agent_id, type, subject, body)
        VALUES (?, ?, ?, 'handoff', ?, ?)
        """,
        (
            child_task_id,
            owner["id"],
            assignee["id"],
            args.subject or "delegated child task",
            args.body,
        ),
    )
    log_event(
        conn,
        "task.delegated",
        task_id=child_task_id,
        agent_id=owner["id"],
        payload={"parent_task_id": args.parent_task_id, "assignee": args.assignee_agent},
    )
    owner_session = get_active_session_for_agent(conn, owner["id"])
    if owner_session is not None:
        complete_session_action(
            conn,
            session_id=owner_session["id"],
            action_key="assign_or_delegate_work",
            detail={"child_task_id": child_task_id},
        )
    conn.commit()
    print_ok(f"delegated child task {child_task_id} to {args.assignee_agent}")


def cmd_task_claim(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    release_stale_leases(conn)
    task = get_task(conn, args.task_id)
    agent = get_agent(conn, args.agent)
    enforce_role_contract(conn, agent=agent, verb="task_claim", allow_override=args.force_role_override)
    session = get_active_session_for_agent(conn, agent["id"])
    active_lease = fetch_one(
        conn,
        """
        SELECT * FROM task_leases
        WHERE task_id = ? AND state = 'active' AND released_at IS NULL
          AND expires_at > CURRENT_TIMESTAMP
        ORDER BY id DESC
        LIMIT 1
        """,
        (args.task_id,),
    )
    if active_lease is not None and active_lease["agent_id"] != agent["id"]:
        raise SystemExit(f"task {args.task_id} is already leased by another agent")
    candidate_paths = json.loads(task["claimed_paths_json"])
    claiming_branch = session["git_branch"] if session else None
    conflicts = detect_path_conflicts(
        conn, candidate_paths, exclude_task_id=args.task_id, claiming_branch=claiming_branch
    )
    session_id = session["id"] if session else None
    for conflict in conflicts:
        log_event(
            conn,
            "task.conflict_detected",
            task_id=args.task_id,
            agent_id=agent["id"],
            session_id=session_id,
            payload={
                "conflicting_task_id": conflict["task_id"],
                "owner_agent_name": conflict["owner_agent_name"],
                "conflicting_path": conflict["conflicting_path"],
                "candidate_path": conflict["candidate_path"],
                "cross_branch": conflict["cross_branch"],
            },
        )
    # --strict blocks only same-branch (or branch-unknown) conflicts; cross-branch
    # conflicts are always downgraded to warnings per the conflict model.
    hard_conflicts = [c for c in conflicts if not c["cross_branch"]]
    if hard_conflicts and args.strict:
        owners = ", ".join(
            f"task {c['task_id']} ({c['owner_agent_name']}) @ {c['conflicting_path']}"
            for c in hard_conflicts
        )
        conn.commit()
        raise SystemExit(f"path conflict detected — blocked by: {owners}")
    if conflicts:
        for c in conflicts:
            branch_note = f" [cross-branch: {claiming_branch} vs {c['owner_git_branch']}]" if c["cross_branch"] else ""
            print(
                f"warning: path conflict with task {c['task_id']} "
                f"({c['owner_agent_name']}) — {c['candidate_path']} overlaps {c['conflicting_path']}{branch_note}"
            )
    if active_lease is None:
        conn.execute(
            """
            INSERT INTO task_leases (task_id, agent_id, session_id, expires_at)
            VALUES (?, ?, ?, datetime('now', ?))
            """,
            (args.task_id, agent["id"], session["id"] if session else None, f"+{args.ttl_minutes} minutes"),
        )
    conn.execute(
        """
        UPDATE tasks
        SET owner_agent_id = ?, status = CASE WHEN status = 'open' THEN 'claimed' ELSE status END
        WHERE id = ?
        """,
        (agent["id"], args.task_id),
    )
    log_event(
        conn,
        "task.claimed",
        task_id=args.task_id,
        agent_id=agent["id"],
        session_id=session_id,
        payload={"ttl_minutes": args.ttl_minutes, "previous_status": task["status"]},
    )
    conn.commit()
    print_ok(f"claimed task {args.task_id} for {args.agent}")


def cmd_task_update_priority(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    task = get_task(conn, args.task_id)
    agent = get_agent(conn, args.agent)
    enforce_role_contract(conn, agent=agent, verb="task_priority", allow_override=args.force_role_override)
    if task["owner_agent_id"] not in (None, agent["id"]):
        raise SystemExit("only the owner may update task priority")
    session = get_active_session_for_agent(conn, agent["id"])
    conn.execute(
        "UPDATE tasks SET priority = ?, owner_agent_id = ? WHERE id = ?",
        (args.priority, agent["id"], args.task_id),
    )
    log_event(
        conn,
        "task.priority_changed",
        task_id=args.task_id,
        agent_id=agent["id"],
        session_id=session["id"] if session else None,
        payload={"from": task["priority"], "to": args.priority},
    )
    conn.commit()
    print_ok(f"task {args.task_id} priority -> p{args.priority}")


def cmd_task_update_status(args: argparse.Namespace) -> None:
    if args.status not in VALID_TASK_STATES:
        raise SystemExit(f"invalid task status: {args.status}")
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    task = get_task(conn, args.task_id)
    agent = get_agent(conn, args.agent)
    enforce_role_contract(conn, agent=agent, verb="task_status", allow_override=args.force_role_override)
    if task["owner_agent_id"] not in (None, agent["id"]):
        raise SystemExit("only the owner may update task status")
    session = get_active_session_for_agent(conn, agent["id"])
    completed_at = "CURRENT_TIMESTAMP" if args.status == "done" else None
    if completed_at:
        conn.execute(
            "UPDATE tasks SET status = ?, owner_agent_id = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
            (args.status, agent["id"], args.task_id),
        )
    else:
        conn.execute(
            "UPDATE tasks SET status = ?, owner_agent_id = ?, completed_at = NULL WHERE id = ?",
            (args.status, agent["id"], args.task_id),
        )
    log_event(
        conn,
        "task.status_changed",
        task_id=args.task_id,
        agent_id=agent["id"],
        session_id=session["id"] if session else None,
        payload={"from": task["status"], "to": args.status},
    )
    conn.commit()
    print_ok(f"task {args.task_id} → {args.status}")


def cmd_task_handoff(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    task = get_task(conn, args.task_id)
    from_agent = get_agent(conn, args.from_agent)
    enforce_role_contract(conn, agent=from_agent, verb="task_handoff", allow_override=args.force_role_override)
    to_agent = get_agent(conn, args.to_agent)
    if task["owner_agent_id"] not in (None, from_agent["id"]):
        raise SystemExit("only the owner may hand off the task")
    conn.execute(
        "UPDATE tasks SET owner_agent_id = ?, status = 'handoff_pending' WHERE id = ?",
        (to_agent["id"], args.task_id),
    )
    conn.execute(
        """
        INSERT INTO messages (task_id, from_agent_id, to_agent_id, type, subject, body)
        VALUES (?, ?, ?, 'handoff', ?, ?)
        """,
        (args.task_id, from_agent["id"], to_agent["id"], args.subject or "handoff", args.body),
    )
    conn.execute(
        """
        UPDATE task_leases
        SET state = 'released', released_at = CURRENT_TIMESTAMP
        WHERE task_id = ? AND agent_id = ? AND state = 'active' AND released_at IS NULL
        """,
        (args.task_id, from_agent["id"]),
    )
    log_event(
        conn,
        "task.handoff",
        task_id=args.task_id,
        agent_id=from_agent["id"],
        payload={"to_agent": args.to_agent},
    )
    conn.commit()
    print_ok(f"handed off task {args.task_id} to {args.to_agent}")


def cmd_lease_renew(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent = get_agent(conn, args.agent)
    updated = conn.execute(
        """
        UPDATE task_leases
        SET heartbeat_at = CURRENT_TIMESTAMP,
            expires_at = datetime('now', ?)
        WHERE task_id = ? AND agent_id = ? AND state = 'active' AND released_at IS NULL
        """,
        (f"+{args.ttl_minutes} minutes", args.task_id, agent["id"]),
    ).rowcount
    if updated == 0:
        raise SystemExit("no active lease found to renew")
    log_event(
        conn,
        "lease.renewed",
        task_id=args.task_id,
        agent_id=agent["id"],
        payload={"ttl_minutes": args.ttl_minutes},
    )
    conn.commit()
    print_ok(f"renewed lease for task {args.task_id}")


def cmd_worker_register(args: argparse.Namespace) -> None:
    paths = ensure_workspace(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    role = normalize_agent_role(args.role)
    specialty = normalize_agent_specialty(conn, args.specialty)
    created_by_id = get_agent(conn, args.created_by)["id"] if args.created_by else None
    command = decode_json_list(args.command_json, field_name="command_json")
    env = decode_json_object(args.env_json, field_name="env_json")
    if not command:
        raise SystemExit("command_json must not be empty")
    try:
        conn.execute(
            """
            INSERT INTO worker_definitions (
                name, kind, role, specialty, command_json, cwd, env_json, approval_policy, created_by_agent_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                args.name,
                args.kind,
                role,
                specialty,
                json.dumps(command),
                args.cwd,
                json.dumps(env),
                args.approval_policy,
                created_by_id,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise SystemExit(f"failed to register worker definition: {exc}") from exc
    worker_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    log_event(
        conn,
        "worker.definition_registered",
        agent_id=created_by_id,
        payload={"worker_id": worker_id, "name": args.name, "kind": args.kind},
    )
    conn.commit()
    print_ok(f"registered worker {args.name}")


def cmd_worker_list(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    rows = conn.execute(
        """
        SELECT
            wd.id,
            wd.name,
            wd.kind,
            wd.role,
            wd.specialty,
            wd.approval_policy,
            wd.command_json,
            wd.cwd,
            (
                SELECT wr.status
                FROM worker_runtimes wr
                WHERE wr.worker_id = wd.id
                ORDER BY wr.id DESC
                LIMIT 1
            ) AS latest_runtime_status
        FROM worker_definitions wd
        ORDER BY wd.id
        """
    ).fetchall()
    data = []
    for row in rows:
        item = dict(row)
        item["command"] = json.loads(item.pop("command_json"))
        data.append(item)
    if args.json:
        emit_json(data)
        return
    for row in data:
        role = row["role"] or "-"
        if row["specialty"]:
            role = f"{role}/{row['specialty']}"
        print(
            f"{row['id']:>3}  {row['name']:<24} {row['kind']:<7} "
            f"{role:<16} {row['approval_policy']:<12} {row.get('latest_runtime_status') or '-':<16} "
            f"{command_preview(row['command'])}"
        )


def cmd_worker_request_start(args: argparse.Namespace) -> None:
    paths = ensure_workspace(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    worker = get_worker_definition(conn, args.worker)
    requester = get_agent(conn, args.requested_by)
    if args.task_id is not None:
        get_task(conn, args.task_id)
    approval_required = should_require_runtime_approval(
        policy=worker["approval_policy"],
        sensitive_action=args.sensitive_action,
    )
    if approval_required:
        approval_status = "approved" if args.approved_by else "pending_approval"
        runtime_status = "approved" if args.approved_by else "pending_approval"
    else:
        approval_status = "not_required"
        runtime_status = "approved"
    conn.execute(
        """
        INSERT INTO worker_runtimes (
            worker_id, task_id, requested_by_agent_id, reason, sensitive_action,
            approval_required, approval_status, approved_by, approved_at, status,
            command_json, cwd
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END, ?, ?, ?)
        """,
        (
            worker["id"],
            args.task_id,
            requester["id"],
            args.reason or "",
            args.sensitive_action or "",
            1 if approval_required else 0,
            approval_status,
            args.approved_by,
            args.approved_by,
            runtime_status,
            worker["command_json"],
            args.cwd or worker["cwd"] or str(paths.root),
        ),
    )
    runtime_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    log_event(
        conn,
        "worker.runtime_requested",
        task_id=args.task_id,
        agent_id=requester["id"],
        payload={
            "runtime_id": runtime_id,
            "worker_name": worker["name"],
            "approval_required": approval_required,
            "sensitive_action": args.sensitive_action or "",
        },
    )
    conn.commit()
    status_text = "pending approval" if approval_status == "pending_approval" else "ready to start"
    print_ok(f"created worker runtime {runtime_id} for {args.worker}")
    print_info(status_text)


def cmd_worker_approve(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    runtime = get_worker_runtime(conn, args.runtime_id)
    if args.decision not in {"approved", "rejected"}:
        raise SystemExit("decision must be approved or rejected")
    status = "approved" if args.decision == "approved" else "rejected"
    runtime_status = "approved" if status == "approved" else "rejected"
    conn.execute(
        """
        UPDATE worker_runtimes
        SET approval_status = ?, approved_by = ?, approved_at = CURRENT_TIMESTAMP, status = ?
        WHERE id = ?
        """,
        (status, args.approved_by, runtime_status, args.runtime_id),
    )
    log_event(
        conn,
        "worker.runtime_approval_updated",
        task_id=runtime["task_id"],
        agent_id=runtime["requested_by_agent_id"],
        payload={"runtime_id": args.runtime_id, "decision": status, "approved_by": args.approved_by},
    )
    conn.commit()
    print_ok(f"runtime {args.runtime_id} {status}")


def cmd_worker_start(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    enforce_roster_preflight(conn)
    runtime = get_worker_runtime(conn, args.runtime_id)
    if runtime["approval_required"] and runtime["approval_status"] != "approved":
        raise SystemExit("runtime requires human approval before start")
    if runtime["status"] in {"launching", "running"}:
        raise SystemExit(f"runtime {args.runtime_id} is already {runtime['status']}")
    conn.execute(
        "UPDATE worker_runtimes SET status = 'launching', heartbeat_at = CURRENT_TIMESTAMP WHERE id = ?",
        (args.runtime_id,),
    )
    supervisor = launch_worker_supervisor(paths, args.runtime_id)
    conn.execute(
        "UPDATE worker_runtimes SET supervisor_pid = ? WHERE id = ?",
        (supervisor.pid, args.runtime_id),
    )
    log_event(
        conn,
        "worker.runtime_launching",
        task_id=runtime["task_id"],
        agent_id=runtime["requested_by_agent_id"],
        payload={"runtime_id": args.runtime_id, "worker_name": runtime["worker_name"]},
    )
    conn.commit()
    print_ok(f"started worker runtime {args.runtime_id}")


def cmd_worker_stop(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    runtime = get_worker_runtime(conn, args.runtime_id)
    target_pid = runtime["child_pid"] or runtime["pid"] or runtime["supervisor_pid"]
    if target_pid is not None:
        try:
            stop_runtime_process(target_pid, VALID_RUNTIME_STOP_SIGNALS[args.signal])
        except ProcessLookupError:
            pass
    conn.execute(
        """
        UPDATE worker_runtimes
        SET status = 'stopped',
            heartbeat_at = CURRENT_TIMESTAMP,
            ended_at = COALESCE(ended_at, CURRENT_TIMESTAMP)
        WHERE id = ?
        """,
        (args.runtime_id,),
    )
    log_event(
        conn,
        "worker.runtime_stopped",
        task_id=runtime["task_id"],
        agent_id=runtime["requested_by_agent_id"],
        payload={"runtime_id": args.runtime_id, "signal": args.signal},
    )
    conn.commit()
    print_ok(f"stopped worker runtime {args.runtime_id}")


def cmd_worker_runtime_list(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    cleanup_stale_worker_runtimes(conn)
    conn.commit()
    rows = conn.execute(
        """
        SELECT
            wr.id,
            wr.task_id,
            wr.status,
            wr.approval_required,
            wr.approval_status,
            wr.reason,
            wr.sensitive_action,
            wr.pid,
            wr.child_pid,
            wr.exit_code,
            wr.started_at,
            wr.heartbeat_at,
            wr.ended_at,
            wd.name AS worker_name,
            requester.name AS requested_by_name
        FROM worker_runtimes wr
        JOIN worker_definitions wd ON wd.id = wr.worker_id
        LEFT JOIN agents requester ON requester.id = wr.requested_by_agent_id
        ORDER BY wr.id DESC
        """
    ).fetchall()
    data = [dict(row) for row in rows]
    if args.json:
        emit_json(data)
        return
    for row in data:
        print(
            f"{row['id']:>3}  {row['worker_name']:<24} {row['status']:<16} "
            f"approval={row['approval_status']:<16} task={row['task_id'] or '-':<4} "
            f"requested_by={row['requested_by_name'] or '-':<20} pid={row['child_pid'] or row['pid'] or '-'}"
        )


def cmd_worker_cleanup(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    cleaned = cleanup_stale_worker_runtimes(conn, stale_minutes=args.stale_minutes)
    conn.commit()
    if args.json:
        emit_json(cleaned)
        return
    if not cleaned:
        print_info("no stale worker runtimes found")
        return
    for row in cleaned:
        print(
            f"{row['runtime_id']:>3}  {row['worker_name']:<24} failed  "
            f"stale={row['stale_minutes']}m packets={row['packet_count']}"
        )


def cmd_dispatch_create(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    enforce_roster_preflight(conn)
    sender = get_agent(conn, args.from_agent)
    worker = get_worker_definition(conn, args.to_worker)
    if args.task_id is not None:
        get_task(conn, args.task_id)
    requires_approval = should_require_packet_approval(
        requested=args.require_approval,
        sensitive_action=args.sensitive_action,
    )
    approval_status = "approved" if args.approved_by else ("pending_approval" if requires_approval else "not_required")
    delivery_status = "ready" if approval_status in {"approved", "not_required"} else "pending_approval"
    packet = {
        "summary": args.summary,
        "body": args.body,
        "artifacts": args.artifact or [],
        "metadata": decode_json_object(args.metadata_json, field_name="metadata_json"),
    }
    conn.execute(
        """
        INSERT INTO dispatch_packets (
            task_id, to_worker_id, from_agent_id, packet_json, sensitive_action,
            requires_human_approval, approval_status, approved_by, approved_at, delivery_status
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? IS NULL THEN NULL ELSE CURRENT_TIMESTAMP END, ?)
        """,
        (
            args.task_id,
            worker["id"],
            sender["id"],
            json.dumps(packet),
            args.sensitive_action or "",
            1 if requires_approval else 0,
            approval_status,
            args.approved_by,
            args.approved_by,
            delivery_status,
        ),
    )
    packet_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    log_event(
        conn,
        "dispatch.packet_created",
        task_id=args.task_id,
        agent_id=sender["id"],
        payload={"packet_id": packet_id, "to_worker": args.to_worker, "requires_approval": requires_approval},
    )
    conn.commit()
    print_ok(f"created dispatch packet {packet_id}")


def cmd_dispatch_list(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    rows = conn.execute(
        """
        SELECT
            dp.id,
            dp.task_id,
            dp.delivery_status,
            dp.approval_status,
            dp.sensitive_action,
            dp.created_at,
            dp.delivered_at,
            sender.name AS from_agent_name,
            wd.name AS worker_name,
            wr.status AS runtime_status
        FROM dispatch_packets dp
        JOIN agents sender ON sender.id = dp.from_agent_id
        LEFT JOIN worker_definitions wd ON wd.id = dp.to_worker_id
        LEFT JOIN worker_runtimes wr ON wr.id = dp.runtime_id
        ORDER BY dp.id DESC
        """
    ).fetchall()
    data = [dict(row) for row in rows]
    if args.json:
        emit_json(data)
        return
    for row in data:
        print(
            f"{row['id']:>3}  task={row['task_id'] or '-':<4} {row['delivery_status']:<16} "
            f"approval={row['approval_status']:<16} worker={row['worker_name'] or '-':<24} "
            f"runtime={row['runtime_status'] or '-':<10} from={row['from_agent_name']}"
        )


def cmd_dispatch_approve(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    packet = get_dispatch_packet(conn, args.packet_id)
    status = "approved" if args.decision == "approved" else "rejected"
    delivery_status = "ready" if status == "approved" else "cancelled"
    conn.execute(
        """
        UPDATE dispatch_packets
        SET approval_status = ?, approved_by = ?, approved_at = CURRENT_TIMESTAMP, delivery_status = ?
        WHERE id = ?
        """,
        (status, args.approved_by, delivery_status, args.packet_id),
    )
    log_event(
        conn,
        "dispatch.packet_approval_updated",
        task_id=packet["task_id"],
        agent_id=packet["from_agent_id"],
        payload={"packet_id": args.packet_id, "decision": status, "approved_by": args.approved_by},
    )
    conn.commit()
    print_ok(f"dispatch packet {args.packet_id} {status}")


def cmd_dispatch_send(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    cleanup_stale_worker_runtimes(conn)
    packet = get_dispatch_packet(conn, args.packet_id)
    if packet["requires_human_approval"] and packet["approval_status"] != "approved":
        raise SystemExit("dispatch packet requires human approval before delivery")
    runtime = get_worker_runtime(conn, args.runtime_id) if args.runtime_id else None
    if runtime is None:
        runtime = fetch_one(
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
            WHERE wr.worker_id = ? AND wr.status IN ('approved', 'launching', 'running')
            ORDER BY wr.id DESC
            LIMIT 1
            """,
            (packet["to_worker_id"],),
        )
    if runtime is None:
        raise SystemExit("no running worker runtime available for packet delivery")
    if runtime["status"] not in {"approved", "launching", "running"}:
        raise SystemExit(f"worker runtime {runtime['id']} is not accepting packets")
    runtime_dir = worker_runtime_dir(paths, runtime["id"])
    inbox_path = Path(runtime["inbox_path"] or runtime_dir / "inbox")
    inbox_path.mkdir(parents=True, exist_ok=True)
    packet_path = inbox_path / f"packet-{args.packet_id}.json"
    payload = {
        "id": args.packet_id,
        "task_id": packet["task_id"],
        "from_agent": packet["from_agent_name"],
        "to_worker": runtime["worker_name"],
        "sensitive_action": packet["sensitive_action"],
        "created_at": packet["created_at"],
        "packet": json.loads(packet["packet_json"]),
    }
    packet_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    conn.execute(
        """
        UPDATE dispatch_packets
        SET runtime_id = ?, delivery_path = ?, delivery_status = 'delivered', delivered_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (runtime["id"], str(packet_path), args.packet_id),
    )
    log_event(
        conn,
        "dispatch.packet_delivered",
        task_id=packet["task_id"],
        agent_id=packet["from_agent_id"],
        payload={"packet_id": args.packet_id, "runtime_id": runtime["id"], "path": str(packet_path)},
    )
    conn.commit()
    print_ok(f"delivered packet {args.packet_id} to runtime {runtime['id']}")


def cmd_dispatch_ack(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    packet = get_dispatch_packet(conn, args.packet_id)
    runtime = get_worker_runtime(conn, args.runtime_id) if args.runtime_id else None
    if runtime is not None and packet["runtime_id"] not in (None, runtime["id"]):
        raise SystemExit("packet is assigned to another runtime")
    runtime_id = runtime["id"] if runtime is not None else packet["runtime_id"]
    conn.execute(
        """
        UPDATE dispatch_packets
        SET runtime_id = COALESCE(runtime_id, ?),
            delivery_status = 'acknowledged',
            acknowledged_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (runtime_id, args.packet_id),
    )
    log_event(
        conn,
        "dispatch.packet_acknowledged",
        task_id=packet["task_id"],
        agent_id=packet["from_agent_id"],
        payload={"packet_id": args.packet_id, "runtime_id": runtime_id, "note": args.note or ""},
    )
    conn.commit()
    print_ok(f"acknowledged packet {args.packet_id}")


def cmd_dispatch_complete(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    packet = get_dispatch_packet(conn, args.packet_id)
    if args.status not in {"completed", "failed", "cancelled"}:
        raise SystemExit("status must be completed, failed, or cancelled")
    conn.execute(
        """
        UPDATE dispatch_packets
        SET delivery_status = ?, completed_at = CURRENT_TIMESTAMP, completion_note = ?
        WHERE id = ?
        """,
        (args.status, args.note or "", args.packet_id),
    )
    log_event(
        conn,
        "dispatch.packet_completed",
        task_id=packet["task_id"],
        agent_id=packet["from_agent_id"],
        payload={"packet_id": args.packet_id, "status": args.status, "note": args.note or ""},
    )
    conn.commit()
    print_ok(f"packet {args.packet_id} marked {args.status}")


def cmd_msg_send(args: argparse.Namespace) -> None:
    require_message_type(args.type)
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    from_agent = get_agent(conn, args.from_agent)
    enforce_role_contract(conn, agent=from_agent, verb="msg_send", allow_override=False)
    to_agent = get_agent(conn, args.to_agent) if args.to_agent else None
    if args.task_id is not None:
        get_task(conn, args.task_id)
    conn.execute(
        """
        INSERT INTO messages (task_id, from_agent_id, to_agent_id, type, subject, body)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            args.task_id,
            from_agent["id"],
            to_agent["id"] if to_agent else None,
            args.type,
            args.subject or "",
            args.body,
        ),
    )
    log_event(
        conn,
        "message.sent",
        task_id=args.task_id,
        agent_id=from_agent["id"],
        payload={"type": args.type, "to": args.to_agent},
    )
    active_session = get_active_session_for_agent(conn, from_agent["id"])
    if active_session is not None:
        role_action = {
            "dev": "report_execution_plan",
            "auditor": "record_review_plan",
            "infra": "record_integration_plan",
        }.get(from_agent["role"])
        if role_action is not None:
            complete_session_action(
                conn,
                session_id=active_session["id"],
                action_key=role_action,
                detail={"message_type": args.type, "task_id": args.task_id},
            )
    conn.commit()
    print_ok("message sent")


def cmd_msg_inbox(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent = get_agent(conn, args.agent)
    active_session = get_active_session_for_agent(conn, agent["id"])
    if active_session is not None:
        complete_session_action(
            conn,
            session_id=active_session["id"],
            action_key="review_inbox",
            detail={"source": "msg.inbox"},
        )
        conn.commit()
    def fetch_rows(since_id: int | None) -> list[sqlite3.Row]:
        params: list[object] = [agent["id"], agent["id"], agent["id"], agent["id"]]
        query = """
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
            WHERE (
                m.to_agent_id = ?
                OR (
                    m.to_agent_id IS NULL
                    AND m.task_id IS NOT NULL
                    AND (
                        EXISTS (
                            SELECT 1
                            FROM tasks task_scope
                            WHERE task_scope.id = m.task_id
                              AND task_scope.owner_agent_id = ?
                        )
                        OR EXISTS (
                            SELECT 1
                            FROM messages participation
                            WHERE participation.task_id = m.task_id
                              AND (
                                  participation.from_agent_id = ?
                                  OR participation.to_agent_id = ?
                              )
                        )
                    )
                )
            )
        """
        if since_id is not None:
            query += " AND m.id > ?"
            params.append(since_id)
            query += " ORDER BY m.id ASC LIMIT ?"
        else:
            query += " ORDER BY m.id DESC LIMIT ?"
        params.append(args.limit)
        rows = conn.execute(query, tuple(params)).fetchall()
        if since_id is None:
            rows = list(reversed(rows))
        return list(rows)

    rows = fetch_rows(args.since_id)
    if args.json:
        emit_json([dict(row) for row in rows])
        return
    follow_rows(
        fetch_rows,
        render_inbox_rows,
        initial_rows=rows,
        initial_since_id=args.since_id,
        follow=args.follow,
        poll_interval=args.poll_interval,
        max_polls=args.max_polls,
    )


def cmd_watch_add(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent = get_agent(conn, args.agent)
    enforce_role_contract(conn, agent=agent, verb="watch_add", allow_override=args.force_role_override)
    get_task(conn, args.task_id)
    try:
        conn.execute(
            """
            INSERT INTO watches (agent_id, task_id)
            VALUES (?, ?)
            """,
            (agent["id"], args.task_id),
        )
    except sqlite3.IntegrityError:
        print_ok(f"{args.agent} already watches task {args.task_id}")
        return
    log_event(
        conn,
        "watch.added",
        task_id=args.task_id,
        agent_id=agent["id"],
        payload={"task_id": args.task_id},
    )
    conn.commit()
    print_ok(f"{args.agent} now watches task {args.task_id}")


def cmd_watch_list(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    params: list[object] = []
    query = """
        SELECT
            w.id,
            a.name AS agent_name,
            t.id AS task_id,
            t.title,
            w.last_sent_event_id,
            w.last_ack_event_id,
            w.last_acknowledged_at,
            (w.last_sent_event_id - w.last_ack_event_id) AS pending_events
        FROM watches w
        JOIN agents a ON a.id = w.agent_id
        JOIN tasks t ON t.id = w.task_id
    """
    if args.agent:
        agent = get_agent(conn, args.agent)
        query += " WHERE w.agent_id = ?"
        params.append(agent["id"])
    query += " ORDER BY w.id DESC"
    rows = [dict(row) for row in conn.execute(query, tuple(params)).fetchall()]
    if args.json:
        emit_json(rows)
        return
    for row in rows:
        print(
            f"{row['id']:>3}  {row['agent_name']:<22} task={row['task_id']:<4} "
            f"pending={row['pending_events']:<4} last_ack={row['last_acknowledged_at'] or '-'}  {row['title']}"
        )


def cmd_watch_ack(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent = get_agent(conn, args.agent)
    watch = fetch_one(
        conn,
        "SELECT * FROM watches WHERE agent_id = ? AND task_id = ?",
        (agent["id"], args.task_id),
    )
    if watch is None:
        raise SystemExit(f"{args.agent} is not subscribed to task {args.task_id}")
    ack_event_id = args.event_id if args.event_id is not None else watch["last_sent_event_id"]
    conn.execute(
        """
        UPDATE watches
        SET last_ack_event_id = ?,
            last_acknowledged_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (ack_event_id, watch["id"]),
    )
    log_event(
        conn,
        "watch.acknowledged",
        task_id=args.task_id,
        agent_id=agent["id"],
        payload={"task_id": args.task_id, "ack_event_id": ack_event_id},
    )
    conn.commit()
    print_ok(f"acknowledged subscription delivery for task {args.task_id}")


def cmd_msg_task(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    get_task(conn, args.task_id)
    def fetch_rows(since_id: int | None) -> list[sqlite3.Row]:
        params: list[object] = [args.task_id]
        query = """
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
        """
        if since_id is not None:
            query += " AND m.id > ?"
            params.append(since_id)
            query += " ORDER BY m.id ASC LIMIT ?"
        else:
            query += " ORDER BY m.id DESC LIMIT ?"
        params.append(args.limit)
        rows = conn.execute(query, tuple(params)).fetchall()
        if since_id is None:
            rows = list(reversed(rows))
        return list(rows)

    rows = fetch_rows(args.since_id)
    if args.json:
        emit_json([dict(row) for row in rows])
        return
    follow_rows(
        fetch_rows,
        render_task_message_rows,
        initial_rows=rows,
        initial_since_id=args.since_id,
        follow=args.follow,
        poll_interval=args.poll_interval,
        max_polls=args.max_polls,
    )


def cmd_event_list(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    agent = get_agent(conn, args.agent) if args.agent is not None else None

    def fetch_rows(since_id: int | None) -> list[sqlite3.Row]:
        params: list[object] = []
        query = """
            SELECT
                e.id,
                e.event_type,
                e.task_id,
                e.session_id,
                a.name AS agent_name,
                a.kind AS agent_kind,
                a.role AS agent_role,
                a.specialty AS agent_specialty,
                t.parent_task_id,
                e.payload_json,
                e.created_at
            FROM events e
            LEFT JOIN agents a ON a.id = e.agent_id
            LEFT JOIN tasks t ON t.id = e.task_id
        """
        where = []
        if args.task_id is not None:
            where.append("e.task_id = ?")
            params.append(args.task_id)
        if agent is not None:
            where.append("e.agent_id = ?")
            params.append(agent["id"])
        if since_id is not None:
            where.append("e.id > ?")
            params.append(since_id)
        if where:
            query += " WHERE " + " AND ".join(where)
        if since_id is not None:
            query += " ORDER BY e.id ASC LIMIT ?"
        else:
            query += " ORDER BY e.id DESC LIMIT ?"
        params.append(args.limit)
        rows = conn.execute(query, tuple(params)).fetchall()
        if since_id is None:
            rows = list(reversed(rows))
        return list(rows)

    rows = fetch_rows(args.since_id)
    data = [
        {
            **dict(row),
            "payload": json.loads(row["payload_json"]),
        }
        for row in rows
    ]
    if args.json:
        emit_json(data)
        return
    follow_rows(
        fetch_rows,
        render_event_list,
        initial_rows=rows,
        initial_since_id=args.since_id,
        follow=args.follow,
        poll_interval=args.poll_interval,
        max_polls=args.max_polls,
    )


def cmd_discover(args: argparse.Namespace) -> None:
    print_info("listening for nearby Lex peers...")
    discovery = LexDiscovery()
    
    peers = []
    def on_peer(peer):
        print(f"  found peer: {peer.agent_name:<20} | branch: {peer.git_branch or '-':<15} | root: {peer.root_path}")
        peers.append(peer)

    discovery.listen(timeout=args.timeout, callback=on_peer)
    
    if not peers:
        print("no peers discovered")


def cmd_discovery_announce(args: argparse.Namespace) -> None:
    paths = resolve_paths(args.root)
    conn = connect(paths.db_path)
    initialize_database(conn)
    
    # Try to find an active session to announce
    session = None
    if args.session_id:
        session = get_session(conn, args.session_id)
    else:
        # Just pick the most recent active session
        session = conn.execute(
            """
            SELECT s.id, a.name AS agent_name, s.git_branch, s.git_base_ref
            FROM sessions s
            JOIN agents a ON a.id = s.agent_id
            WHERE s.status = 'active' AND s.ended_at IS NULL
            ORDER BY s.id DESC LIMIT 1
            """
        ).fetchone()
    
    if not session:
        print_info("no active session found to announce, announcing as generic peer")
        payload = {
            "agent_name": "anonymous",
            "root_path": str(paths.root)
        }
    else:
        payload = {
            "agent_name": session["agent_name"],
            "session_id": session["id"],
            "git_branch": session["git_branch"],
            "git_base_ref": session["git_base_ref"],
            "root_path": str(paths.root)
        }
    
    print_ok(f"announcing as {payload['agent_name']} (press Ctrl+C to stop)")
    discovery = LexDiscovery(payload)
    discovery.start_announcing(interval=args.interval)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nstopped announcement")


def cmd_dx(args: argparse.Namespace) -> None:
    root = Path(args.root).resolve()
    paths = resolve_paths(root)

    # Optionally announce this dx session on the LAN if a workspace DB exists.
    discovery = None
    if paths.db_path.exists():
        conn = connect(paths.db_path)
        session = conn.execute(
            "SELECT s.id, a.name AS agent_name, s.git_branch, s.git_base_ref "
            "FROM sessions s JOIN agents a ON a.id = s.agent_id "
            "WHERE s.status = 'active' AND s.ended_at IS NULL "
            "ORDER BY s.id DESC LIMIT 1"
        ).fetchone()
        if session:
            discovery = LexDiscovery({
                "agent_name": session["agent_name"],
                "session_id": session["id"],
                "git_branch": session["git_branch"],
                "git_base_ref": session["git_base_ref"],
                "root_path": str(root),
            })
            discovery.start_announcing()

    try:
        run_dx(root)
    finally:
        if discovery:
            discovery.stop()


def add_follow_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--since-id", type=int)
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--poll-interval", type=float, default=2.0)
    parser.add_argument("--max-polls", type=int)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=CLI_COMMAND)
    parser.add_argument("--root", default=".", help="workspace root")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init")
    init_parser.set_defaults(func=cmd_init)

    discovery_parser = subparsers.add_parser("discovery")
    discovery_sub = discovery_parser.add_subparsers(dest="discovery_command", required=True)
    
    discovery_list = discovery_sub.add_parser("list")
    discovery_list.add_argument("--timeout", type=float, default=2.0)
    discovery_list.set_defaults(func=cmd_discover)
    
    discovery_announce = discovery_sub.add_parser("announce")
    discovery_announce.add_argument("--session-id", type=int)
    discovery_announce.add_argument("--interval", type=int, default=5)
    discovery_announce.set_defaults(func=cmd_discovery_announce)

    install_parser = subparsers.add_parser("install")
    install_parser.add_argument("--agent-files", choices=["preserve", "merge", "assisted", "overwrite"], default="merge")
    install_parser.add_argument("--ignore-policy", choices=["none", "runtime", "all"], default="runtime")
    install_parser.add_argument("--ignore-target", choices=["gitignore", "local-exclude"], default="gitignore")
    install_parser.add_argument("--assisted-agent", choices=["codex", "claude", "gemini", "manual"], default="codex")
    install_parser.add_argument("--non-interactive", action="store_true")
    install_parser.set_defaults(func=cmd_install)

    merge_parser = subparsers.add_parser("merge")
    merge_sub = merge_parser.add_subparsers(dest="merge_command", required=True)

    merge_plan = merge_sub.add_parser("plan")
    merge_plan.add_argument("--agent", choices=["codex", "claude", "gemini", "manual"], default="codex")
    merge_plan.set_defaults(func=cmd_merge_plan)

    merge_diff = merge_sub.add_parser("diff")
    merge_diff.set_defaults(func=cmd_merge_diff)

    merge_apply = merge_sub.add_parser("apply")
    merge_apply.set_defaults(func=cmd_merge_apply)

    agent_parser = subparsers.add_parser("agent")
    agent_sub = agent_parser.add_subparsers(dest="agent_command", required=True)

    agent_identify = agent_sub.add_parser("identify")
    agent_identify.add_argument("kind", choices=["codex", "claude", "cursor", "gemini", "ci", "automated"])
    agent_identify.add_argument("--name")
    agent_identify.add_argument("--role")
    agent_identify.add_argument("--specialty")
    agent_identify.add_argument("--json", action="store_true")
    agent_identify.set_defaults(func=cmd_agent_identify)

    agent_register = agent_sub.add_parser("register")
    agent_register.add_argument("name")
    agent_register.add_argument("kind", choices=["codex", "claude", "cursor", "gemini", "ci", "automated"])
    agent_register.add_argument("--role")
    agent_register.add_argument("--specialty")
    agent_register.set_defaults(func=cmd_agent_register)

    agent_role = agent_sub.add_parser("role")
    agent_role.add_argument("agent")
    agent_role.add_argument("role")
    agent_role.add_argument("--specialty")
    agent_role.set_defaults(func=cmd_agent_update_role)

    specialty_parser = subparsers.add_parser("specialty")
    specialty_sub = specialty_parser.add_subparsers(dest="specialty_command", required=True)

    specialty_add = specialty_sub.add_parser("add")
    specialty_add.add_argument("name")
    specialty_add.set_defaults(func=cmd_specialty_add)

    specialty_list = specialty_sub.add_parser("list")
    specialty_list.add_argument("--json", action="store_true")
    specialty_list.set_defaults(func=cmd_specialty_list)

    agent_list = agent_sub.add_parser("list")
    agent_list.add_argument("--json", action="store_true")
    agent_list.set_defaults(func=cmd_agent_list)

    agent_preflight = agent_sub.add_parser("preflight")
    agent_preflight.add_argument("--json", action="store_true")
    agent_preflight.set_defaults(func=cmd_agent_preflight)

    agent_retire = agent_sub.add_parser("retire")
    agent_retire.add_argument("agent")
    agent_retire.add_argument("--by", help="agent performing the retirement (for audit)")
    agent_retire.add_argument("--force", action="store_true", help="retire even if agent owns active tasks")
    agent_retire.set_defaults(func=cmd_agent_retire)

    session_parser = subparsers.add_parser("session")
    session_sub = session_parser.add_subparsers(dest="session_command", required=True)

    session_start = session_sub.add_parser("start")
    session_start.add_argument("agent")
    session_start.add_argument("--label")
    session_start.add_argument("--cwd", default=".")
    session_start.add_argument("--capability", action="append")
    session_start.add_argument("--fingerprint")
    session_start.add_argument("--fingerprint-label")
    session_start.set_defaults(func=cmd_session_start)

    session_heartbeat = session_sub.add_parser("heartbeat")
    session_heartbeat.add_argument("session_id", type=int)
    session_heartbeat.set_defaults(func=cmd_session_heartbeat)

    session_end = session_sub.add_parser("end")
    session_end.add_argument("session_id", type=int)
    session_end.set_defaults(func=cmd_session_end)

    session_bootstrap_show = session_sub.add_parser("bootstrap-show")
    session_bootstrap_show.add_argument("session_id", type=int)
    session_bootstrap_show.add_argument("--json", action="store_true")
    session_bootstrap_show.set_defaults(func=cmd_session_bootstrap_show)

    session_bootstrap_ack = session_sub.add_parser("bootstrap-ack")
    session_bootstrap_ack.add_argument("session_id", type=int)
    session_bootstrap_ack.add_argument("--by", required=True)
    session_bootstrap_ack.set_defaults(func=cmd_session_bootstrap_ack)

    session_action = session_sub.add_parser("action")
    session_action.add_argument("session_id", type=int)
    session_action.add_argument("action_key")
    session_action.add_argument("--note")
    session_action.set_defaults(func=cmd_session_action_complete)

    session_list = session_sub.add_parser("list")
    session_list.add_argument("--active-only", action="store_true")
    session_list.add_argument("--json", action="store_true")
    session_list.set_defaults(func=cmd_session_list)

    worker_parser = subparsers.add_parser("worker")
    worker_sub = worker_parser.add_subparsers(dest="worker_command", required=True)

    worker_register = worker_sub.add_parser("register")
    worker_register.add_argument("name")
    worker_register.add_argument("kind", choices=["codex", "claude", "cursor", "gemini", "ci", "automated"])
    worker_register.add_argument("--role")
    worker_register.add_argument("--specialty")
    worker_register.add_argument("--command-json", required=True)
    worker_register.add_argument("--env-json", default="{}")
    worker_register.add_argument("--cwd")
    worker_register.add_argument("--approval-policy", choices=list(VALID_WORKER_APPROVAL_POLICIES), default="always")
    worker_register.add_argument("--created-by")
    worker_register.set_defaults(func=cmd_worker_register)

    worker_list = worker_sub.add_parser("list")
    worker_list.add_argument("--json", action="store_true")
    worker_list.set_defaults(func=cmd_worker_list)

    worker_runtime_list = worker_sub.add_parser("runtime-list")
    worker_runtime_list.add_argument("--json", action="store_true")
    worker_runtime_list.set_defaults(func=cmd_worker_runtime_list)

    worker_cleanup = worker_sub.add_parser("cleanup")
    worker_cleanup.add_argument("--stale-minutes", type=int, default=WORKER_RUNTIME_STALE_MINUTES)
    worker_cleanup.add_argument("--json", action="store_true")
    worker_cleanup.set_defaults(func=cmd_worker_cleanup)

    worker_request = worker_sub.add_parser("request-start")
    worker_request.add_argument("worker")
    worker_request.add_argument("--requested-by", required=True)
    worker_request.add_argument("--task-id", type=int)
    worker_request.add_argument("--reason")
    worker_request.add_argument("--sensitive-action")
    worker_request.add_argument("--cwd")
    worker_request.add_argument("--approved-by")
    worker_request.set_defaults(func=cmd_worker_request_start)

    worker_approve = worker_sub.add_parser("approve")
    worker_approve.add_argument("runtime_id", type=int)
    worker_approve.add_argument("decision", choices=["approved", "rejected"])
    worker_approve.add_argument("--approved-by", required=True)
    worker_approve.set_defaults(func=cmd_worker_approve)

    worker_start = worker_sub.add_parser("start")
    worker_start.add_argument("runtime_id", type=int)
    worker_start.set_defaults(func=cmd_worker_start)

    worker_stop = worker_sub.add_parser("stop")
    worker_stop.add_argument("runtime_id", type=int)
    worker_stop.add_argument("--signal", choices=sorted(VALID_RUNTIME_STOP_SIGNALS), default="TERM")
    worker_stop.set_defaults(func=cmd_worker_stop)

    task_parser = subparsers.add_parser("task")
    task_sub = task_parser.add_subparsers(dest="task_command", required=True)

    task_create = task_sub.add_parser("create")
    task_create.add_argument("title")
    task_create.add_argument("--slug")
    task_create.add_argument("--description")
    task_create.add_argument("--priority", type=int, default=2)
    task_create.add_argument("--created-by")
    task_create.add_argument("--parent-task", type=int)
    task_create.add_argument("--delegation-mode", default="direct", choices=["direct", "hypervisor"])
    task_create.add_argument("--path", action="append")
    task_create.add_argument("--force-role-override", action="store_true")
    task_create.set_defaults(func=cmd_task_create)

    task_list = task_sub.add_parser("list")
    task_list.add_argument("--json", action="store_true")
    task_list.set_defaults(func=cmd_task_list)

    task_show = task_sub.add_parser("show")
    task_show.add_argument("task_id", type=int)
    task_show.add_argument("--agent")
    task_show.add_argument("--json", action="store_true")
    task_show.set_defaults(func=cmd_task_show)

    task_claim = task_sub.add_parser("claim")
    task_claim.add_argument("task_id", type=int)
    task_claim.add_argument("agent")
    task_claim.add_argument("--ttl-minutes", type=int, default=30)
    task_claim.add_argument("--force-role-override", action="store_true")
    task_claim.add_argument("--strict", action="store_true", help="block claim on path conflict instead of warning")
    task_claim.set_defaults(func=cmd_task_claim)

    task_priority = task_sub.add_parser("priority")
    task_priority.add_argument("task_id", type=int)
    task_priority.add_argument("agent")
    task_priority.add_argument("priority", type=int)
    task_priority.add_argument("--force-role-override", action="store_true")
    task_priority.set_defaults(func=cmd_task_update_priority)

    task_delegate = task_sub.add_parser("delegate")
    task_delegate.add_argument("parent_task_id", type=int)
    task_delegate.add_argument("owner_agent")
    task_delegate.add_argument("assignee_agent")
    task_delegate.add_argument("title")
    task_delegate.add_argument("--slug")
    task_delegate.add_argument("--description")
    task_delegate.add_argument("--subject")
    task_delegate.add_argument("--body", required=True)
    task_delegate.add_argument("--priority", type=int)
    task_delegate.add_argument("--ttl-minutes", type=int, default=30)
    task_delegate.add_argument("--path", action="append")
    task_delegate.add_argument("--force-role-override", action="store_true")
    task_delegate.set_defaults(func=cmd_task_delegate)

    task_status = task_sub.add_parser("status")
    task_status.add_argument("task_id", type=int)
    task_status.add_argument("agent")
    task_status.add_argument("status")
    task_status.add_argument("--force-role-override", action="store_true")
    task_status.set_defaults(func=cmd_task_update_status)

    task_handoff = task_sub.add_parser("handoff")
    task_handoff.add_argument("task_id", type=int)
    task_handoff.add_argument("from_agent")
    task_handoff.add_argument("to_agent")
    task_handoff.add_argument("--subject")
    task_handoff.add_argument("--body", required=True)
    task_handoff.add_argument("--force-role-override", action="store_true")
    task_handoff.set_defaults(func=cmd_task_handoff)

    lease_parser = subparsers.add_parser("lease")
    lease_sub = lease_parser.add_subparsers(dest="lease_command", required=True)
    lease_renew = lease_sub.add_parser("renew")
    lease_renew.add_argument("task_id", type=int)
    lease_renew.add_argument("agent")
    lease_renew.add_argument("--ttl-minutes", type=int, default=30)
    lease_renew.set_defaults(func=cmd_lease_renew)

    msg_parser = subparsers.add_parser("msg")
    msg_sub = msg_parser.add_subparsers(dest="msg_command", required=True)

    msg_send = msg_sub.add_parser("send")
    msg_send.add_argument("--task", dest="task_id", type=int)
    msg_send.add_argument("--from", dest="from_agent", required=True)
    msg_send.add_argument("--to", dest="to_agent")
    msg_send.add_argument("--type", required=True)
    msg_send.add_argument("--subject")
    msg_send.add_argument("--body", required=True)
    msg_send.set_defaults(func=cmd_msg_send)

    msg_inbox = msg_sub.add_parser("inbox")
    msg_inbox.add_argument("agent")
    msg_inbox.add_argument("--limit", type=int, default=20)
    msg_inbox.add_argument("--json", action="store_true")
    add_follow_arguments(msg_inbox)
    msg_inbox.set_defaults(func=cmd_msg_inbox)

    msg_task = msg_sub.add_parser("task")
    msg_task.add_argument("task_id", type=int)
    msg_task.add_argument("--limit", type=int, default=20)
    msg_task.add_argument("--json", action="store_true")
    add_follow_arguments(msg_task)
    msg_task.set_defaults(func=cmd_msg_task)

    watch_parser = subparsers.add_parser("watch")
    watch_sub = watch_parser.add_subparsers(dest="watch_command", required=True)

    watch_add = watch_sub.add_parser("add")
    watch_add.add_argument("agent")
    watch_add.add_argument("task_id", type=int)
    watch_add.add_argument("--force-role-override", action="store_true")
    watch_add.set_defaults(func=cmd_watch_add)

    watch_list = watch_sub.add_parser("list")
    watch_list.add_argument("--agent")
    watch_list.add_argument("--json", action="store_true")
    watch_list.set_defaults(func=cmd_watch_list)

    watch_ack = watch_sub.add_parser("ack")
    watch_ack.add_argument("agent")
    watch_ack.add_argument("task_id", type=int)
    watch_ack.add_argument("--event-id", type=int)
    watch_ack.set_defaults(func=cmd_watch_ack)

    dispatch_parser = subparsers.add_parser("dispatch")
    dispatch_sub = dispatch_parser.add_subparsers(dest="dispatch_command", required=True)

    dispatch_create = dispatch_sub.add_parser("create")
    dispatch_create.add_argument("--task-id", type=int)
    dispatch_create.add_argument("--from", dest="from_agent", required=True)
    dispatch_create.add_argument("--to-worker", required=True)
    dispatch_create.add_argument("--summary", required=True)
    dispatch_create.add_argument("--body", required=True)
    dispatch_create.add_argument("--artifact", action="append")
    dispatch_create.add_argument("--metadata-json", default="{}")
    dispatch_create.add_argument("--sensitive-action")
    dispatch_create.add_argument("--require-approval", action="store_true")
    dispatch_create.add_argument("--approved-by")
    dispatch_create.set_defaults(func=cmd_dispatch_create)

    dispatch_list = dispatch_sub.add_parser("list")
    dispatch_list.add_argument("--json", action="store_true")
    dispatch_list.set_defaults(func=cmd_dispatch_list)

    dispatch_approve = dispatch_sub.add_parser("approve")
    dispatch_approve.add_argument("packet_id", type=int)
    dispatch_approve.add_argument("decision", choices=["approved", "rejected"])
    dispatch_approve.add_argument("--approved-by", required=True)
    dispatch_approve.set_defaults(func=cmd_dispatch_approve)

    dispatch_send = dispatch_sub.add_parser("send")
    dispatch_send.add_argument("packet_id", type=int)
    dispatch_send.add_argument("--runtime-id", type=int)
    dispatch_send.set_defaults(func=cmd_dispatch_send)

    dispatch_ack = dispatch_sub.add_parser("ack")
    dispatch_ack.add_argument("packet_id", type=int)
    dispatch_ack.add_argument("--runtime-id", type=int)
    dispatch_ack.add_argument("--note")
    dispatch_ack.set_defaults(func=cmd_dispatch_ack)

    dispatch_complete = dispatch_sub.add_parser("complete")
    dispatch_complete.add_argument("packet_id", type=int)
    dispatch_complete.add_argument("status", choices=["completed", "failed", "cancelled"])
    dispatch_complete.add_argument("--note")
    dispatch_complete.set_defaults(func=cmd_dispatch_complete)

    prompt_parser = subparsers.add_parser("prompt")
    prompt_sub = prompt_parser.add_subparsers(dest="prompt_command", required=True)

    prompt_create = prompt_sub.add_parser("create")
    prompt_create.add_argument("--role", required=True)
    prompt_create.add_argument("--agent", default=None, help="agent name to hydrate with live state")
    prompt_create.add_argument("--json", action="store_true")
    prompt_create.set_defaults(func=cmd_prompt_create)

    event_parser = subparsers.add_parser("event")
    event_sub = event_parser.add_subparsers(dest="event_command", required=True)

    event_list = event_sub.add_parser("list")
    event_list.add_argument("--task", dest="task_id", type=int)
    event_list.add_argument("--agent")
    event_list.add_argument("--limit", type=int, default=20)
    event_list.add_argument("--json", action="store_true")
    add_follow_arguments(event_list)
    event_list.set_defaults(func=cmd_event_list)

    dx_parser = subparsers.add_parser("dx")
    dx_parser.set_defaults(func=cmd_dx)

    hook_parser = subparsers.add_parser("hook")
    hook_sub = hook_parser.add_subparsers(dest="hook_command", required=True)

    claude_parser = hook_sub.add_parser("claude")
    claude_sub = claude_parser.add_subparsers(dest="claude_hook_command", required=True)

    claude_stop = claude_sub.add_parser("stop")
    claude_stop.set_defaults(func=cmd_hook_claude_stop)

    claude_upr = claude_sub.add_parser("user-prompt-submit")
    claude_upr.set_defaults(func=cmd_hook_claude_user_prompt_submit)

    claude_ptu = claude_sub.add_parser("post-tool-use")
    claude_ptu.set_defaults(func=cmd_hook_claude_post_tool_use)

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        root = Path(args.root).resolve()
        if sys.stdin.isatty() and sys.stdout.isatty():
            try:
                run_tui(root)
            except Exception as exc:
                if os.environ.get("LEX_DEBUG_TUI") == "1":
                    raise
                reason = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, curses.error):
                    print(f"{CLI_COMMAND}: TUI unavailable, falling back to interactive shell ({reason})", file=sys.stderr)
                else:
                    print(f"{CLI_COMMAND}: TUI failed to start, falling back to interactive shell ({reason})", file=sys.stderr)
                run_interactive_shell(root)
        else:
            run_interactive_shell(root)
        return
    args.func(args)


if __name__ == "__main__":
    main()
