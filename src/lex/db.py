from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_LEX_DIRNAME = ".lex"
DEFAULT_DB_NAME = "lex.db"
_OPEN_CONNECTIONS: list[sqlite3.Connection] = []
ROLE_MIGRATIONS: dict[str, tuple[str, str]] = {
    "engineer": ("dev", "engineer"),
    "devops": ("dev", "devops"),
    "tech_lead": ("dev", "tech_lead"),
    "product_manager": ("pm", "product_manager"),
    "designer": ("pm", "designer"),
    "release_manager": ("pm", "release_manager"),
    "qa": ("auditor", "qa"),
    "reviewer": ("auditor", "reviewer"),
}
CANONICAL_ROLES = {"dev", "pm", "auditor", "infra"}
BUILTIN_SPECIALTIES = ("frontend", "infra", "ux", "security", "release")
VALID_AGENT_KINDS = ("codex", "claude", "cursor", "gemini", "ci", "automated")
# agents.status values:
#   active  — canonical, live agent; valid routing target
#   retired — former agent; preserved for audit/history but must not receive
#             new work. Roster preflight blocks routing to retired agents.
VALID_AGENT_STATUSES = ("active", "retired")
VALID_SESSION_STATUSES = ("active", "ended")
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
VALID_TASK_STATUSES = (
    "open",
    "claimed",
    "in_progress",
    "blocked",
    "review_requested",
    "handoff_pending",
    "done",
    "abandoned",
)
ACTIVE_OWNER_TASK_STATUSES = (
    "claimed",
    "in_progress",
    "blocked",
    "review_requested",
    "handoff_pending",
)
VALID_DELEGATION_MODES = ("direct", "hypervisor")
VALID_LEASE_STATES = ("active", "released")
VALID_DEPENDENCY_KINDS = ("blocks",)
VALID_MESSAGE_TYPES = (
    "note",
    "question",
    "answer",
    "blocker",
    "handoff",
    "review_request",
    "review_result",
    "decision",
    "artifact_notice",
)


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    PRAGMA journal_mode = WAL;
    """,
    """
    CREATE TABLE IF NOT EXISTS agents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ('codex', 'claude', 'cursor', 'gemini', 'ci', 'automated')),
        role TEXT NOT NULL DEFAULT '',
        specialty TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id INTEGER NOT NULL,
        label TEXT,
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'ended')),
        cwd TEXT,
        capabilities_json TEXT NOT NULL DEFAULT '{}',
        started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        heartbeat_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ended_at TEXT,
        FOREIGN KEY (agent_id) REFERENCES agents(id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        slug TEXT UNIQUE,
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'claimed', 'in_progress', 'blocked', 'review_requested', 'handoff_pending', 'done', 'abandoned')),
        priority INTEGER NOT NULL DEFAULT 2 CHECK (priority BETWEEN 1 AND 4),
        owner_agent_id INTEGER,
        parent_task_id INTEGER,
        delegation_mode TEXT NOT NULL DEFAULT 'direct' CHECK (delegation_mode IN ('direct', 'hypervisor')),
        claimed_paths_json TEXT NOT NULL DEFAULT '[]',
        created_by_agent_id INTEGER,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        completed_at TEXT,
        FOREIGN KEY (owner_agent_id) REFERENCES agents(id),
        FOREIGN KEY (parent_task_id) REFERENCES tasks(id),
        FOREIGN KEY (created_by_agent_id) REFERENCES agents(id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS task_leases (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        agent_id INTEGER NOT NULL,
        session_id INTEGER,
        state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'released')),
        acquired_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        heartbeat_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        expires_at TEXT NOT NULL,
        released_at TEXT,
        FOREIGN KEY (task_id) REFERENCES tasks(id),
        FOREIGN KEY (agent_id) REFERENCES agents(id),
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS task_dependencies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER NOT NULL,
        depends_on_task_id INTEGER NOT NULL,
        kind TEXT NOT NULL DEFAULT 'blocks' CHECK (kind IN ('blocks')),
        FOREIGN KEY (task_id) REFERENCES tasks(id),
        FOREIGN KEY (depends_on_task_id) REFERENCES tasks(id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER,
        from_agent_id INTEGER NOT NULL,
        to_agent_id INTEGER,
        type TEXT NOT NULL CHECK (type IN ('note', 'question', 'answer', 'blocker', 'handoff', 'review_request', 'review_result', 'decision', 'artifact_notice')),
        subject TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        read_at TEXT,
        FOREIGN KEY (task_id) REFERENCES tasks(id),
        FOREIGN KEY (from_agent_id) REFERENCES agents(id),
        FOREIGN KEY (to_agent_id) REFERENCES agents(id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS artifacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER,
        agent_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        path TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (task_id) REFERENCES tasks(id),
        FOREIGN KEY (agent_id) REFERENCES agents(id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        task_id INTEGER,
        agent_id INTEGER,
        session_id INTEGER,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (task_id) REFERENCES tasks(id),
        FOREIGN KEY (agent_id) REFERENCES agents(id),
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS watches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        agent_id INTEGER NOT NULL,
        task_id INTEGER NOT NULL,
        last_sent_event_id INTEGER NOT NULL DEFAULT 0,
        last_ack_event_id INTEGER NOT NULL DEFAULT 0,
        last_acknowledged_at TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(agent_id, task_id),
        FOREIGN KEY (agent_id) REFERENCES agents(id),
        FOREIGN KEY (task_id) REFERENCES tasks(id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS specialties (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS worker_definitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL CHECK (kind IN ('codex', 'claude', 'cursor', 'gemini')),
        role TEXT NOT NULL DEFAULT '',
        specialty TEXT NOT NULL DEFAULT '',
        command_json TEXT NOT NULL DEFAULT '[]',
        cwd TEXT,
        env_json TEXT NOT NULL DEFAULT '{}',
        approval_policy TEXT NOT NULL DEFAULT 'always' CHECK (approval_policy IN ('always', 'on_sensitive', 'never')),
        created_by_agent_id INTEGER,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (created_by_agent_id) REFERENCES agents(id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS worker_runtimes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        worker_id INTEGER NOT NULL,
        task_id INTEGER,
        requested_by_agent_id INTEGER,
        reason TEXT NOT NULL DEFAULT '',
        sensitive_action TEXT NOT NULL DEFAULT '',
        approval_required INTEGER NOT NULL DEFAULT 1 CHECK (approval_required IN (0, 1)),
        approval_status TEXT NOT NULL DEFAULT 'pending_approval' CHECK (approval_status IN ('pending_approval', 'approved', 'rejected', 'not_required')),
        approved_by TEXT,
        approved_at TEXT,
        status TEXT NOT NULL DEFAULT 'pending_approval' CHECK (status IN ('pending_approval', 'approved', 'launching', 'running', 'exited', 'failed', 'stopped', 'rejected')),
        pid INTEGER,
        supervisor_pid INTEGER,
        child_pid INTEGER,
        command_json TEXT NOT NULL DEFAULT '[]',
        cwd TEXT,
        inbox_path TEXT,
        log_path TEXT,
        error_path TEXT,
        started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        heartbeat_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        ended_at TEXT,
        exit_code INTEGER,
        FOREIGN KEY (worker_id) REFERENCES worker_definitions(id),
        FOREIGN KEY (task_id) REFERENCES tasks(id),
        FOREIGN KEY (requested_by_agent_id) REFERENCES agents(id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS dispatch_packets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        task_id INTEGER,
        runtime_id INTEGER,
        to_worker_id INTEGER,
        from_agent_id INTEGER NOT NULL,
        packet_json TEXT NOT NULL DEFAULT '{}',
        sensitive_action TEXT NOT NULL DEFAULT '',
        requires_human_approval INTEGER NOT NULL DEFAULT 0 CHECK (requires_human_approval IN (0, 1)),
        approval_status TEXT NOT NULL DEFAULT 'ready' CHECK (approval_status IN ('pending_approval', 'approved', 'rejected', 'not_required')),
        approved_by TEXT,
        approved_at TEXT,
        delivery_status TEXT NOT NULL DEFAULT 'draft' CHECK (delivery_status IN ('draft', 'pending_approval', 'ready', 'delivered', 'acknowledged', 'completed', 'failed', 'cancelled')),
        delivery_path TEXT,
        delivered_at TEXT,
        acknowledged_at TEXT,
        completed_at TEXT,
        completion_note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (task_id) REFERENCES tasks(id),
        FOREIGN KEY (runtime_id) REFERENCES worker_runtimes(id),
        FOREIGN KEY (to_worker_id) REFERENCES worker_definitions(id),
        FOREIGN KEY (from_agent_id) REFERENCES agents(id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS session_bootstraps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL UNIQUE,
        agent_id INTEGER NOT NULL,
        continuity_from_session_id INTEGER,
        role_contract_json TEXT NOT NULL DEFAULT '{}',
        memory_json TEXT NOT NULL DEFAULT '{}',
        system_prompt TEXT NOT NULL DEFAULT '',
        workflow_template_json TEXT NOT NULL DEFAULT '[]',
        required_actions_json TEXT NOT NULL DEFAULT '[]',
        acknowledged_at TEXT,
        acknowledged_by TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (session_id) REFERENCES sessions(id),
        FOREIGN KEY (agent_id) REFERENCES agents(id),
        FOREIGN KEY (continuity_from_session_id) REFERENCES sessions(id)
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS session_action_receipts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER NOT NULL,
        action_key TEXT NOT NULL,
        detail_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(session_id, action_key),
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    );
    """,
    """
    CREATE TRIGGER IF NOT EXISTS tasks_updated_at
    AFTER UPDATE ON tasks
    FOR EACH ROW
    BEGIN
        UPDATE tasks
        SET updated_at = CURRENT_TIMESTAMP
        WHERE id = OLD.id;
    END;
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS task_leases_one_active_per_task
    ON task_leases(task_id)
    WHERE state = 'active' AND released_at IS NULL;
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS task_dependencies_unique_edge
    ON task_dependencies(task_id, depends_on_task_id, kind);
    """,
    """
    CREATE INDEX IF NOT EXISTS worker_runtimes_by_status
    ON worker_runtimes(status, id DESC);
    """,
    """
    CREATE INDEX IF NOT EXISTS dispatch_packets_by_status
    ON dispatch_packets(delivery_status, id DESC);
    """,
    # sessions_validate_insert / sessions_validate_update are intentionally
    # identical in logic. SQLite does not support BEFORE INSERT OR UPDATE syntax,
    # so both triggers must be declared separately.
    """
    CREATE TRIGGER IF NOT EXISTS sessions_validate_insert
    BEFORE INSERT ON sessions
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'invalid session status')
        WHERE NEW.status NOT IN ('active', 'ended');
        SELECT RAISE(ABORT, 'active session cannot have ended_at')
        WHERE NEW.status = 'active' AND NEW.ended_at IS NOT NULL;
        SELECT RAISE(ABORT, 'ended session requires ended_at')
        WHERE NEW.status = 'ended' AND NEW.ended_at IS NULL;
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS sessions_validate_update
    BEFORE UPDATE ON sessions
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'invalid session status')
        WHERE NEW.status NOT IN ('active', 'ended');
        SELECT RAISE(ABORT, 'active session cannot have ended_at')
        WHERE NEW.status = 'active' AND NEW.ended_at IS NOT NULL;
        SELECT RAISE(ABORT, 'ended session requires ended_at')
        WHERE NEW.status = 'ended' AND NEW.ended_at IS NULL;
    END;
    """,
    # tasks_validate_insert / tasks_validate_update are intentionally identical.
    # SQLite requires separate triggers for INSERT and UPDATE.
    """
    CREATE TRIGGER IF NOT EXISTS tasks_validate_insert
    BEFORE INSERT ON tasks
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'invalid task status')
        WHERE NEW.status NOT IN ('open', 'claimed', 'in_progress', 'blocked', 'review_requested', 'handoff_pending', 'done', 'abandoned');
        SELECT RAISE(ABORT, 'invalid task priority')
        WHERE NEW.priority NOT BETWEEN 1 AND 4;
        SELECT RAISE(ABORT, 'invalid delegation mode')
        WHERE NEW.delegation_mode NOT IN ('direct', 'hypervisor');
        SELECT RAISE(ABORT, 'claimed_paths_json must be a JSON array')
        WHERE json_valid(NEW.claimed_paths_json) = 0 OR json_type(NEW.claimed_paths_json) != 'array';
        SELECT RAISE(ABORT, 'active task statuses require an owner')
        WHERE NEW.status IN ('claimed', 'in_progress', 'blocked', 'review_requested', 'handoff_pending')
          AND NEW.owner_agent_id IS NULL;
        SELECT RAISE(ABORT, 'done task requires completed_at')
        WHERE NEW.status = 'done' AND NEW.completed_at IS NULL;
        SELECT RAISE(ABORT, 'non-done task cannot have completed_at')
        WHERE NEW.status != 'done' AND NEW.completed_at IS NOT NULL;
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS tasks_validate_update
    BEFORE UPDATE ON tasks
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'invalid task status')
        WHERE NEW.status NOT IN ('open', 'claimed', 'in_progress', 'blocked', 'review_requested', 'handoff_pending', 'done', 'abandoned');
        SELECT RAISE(ABORT, 'invalid task priority')
        WHERE NEW.priority NOT BETWEEN 1 AND 4;
        SELECT RAISE(ABORT, 'invalid delegation mode')
        WHERE NEW.delegation_mode NOT IN ('direct', 'hypervisor');
        SELECT RAISE(ABORT, 'claimed_paths_json must be a JSON array')
        WHERE json_valid(NEW.claimed_paths_json) = 0 OR json_type(NEW.claimed_paths_json) != 'array';
        SELECT RAISE(ABORT, 'active task statuses require an owner')
        WHERE NEW.status IN ('claimed', 'in_progress', 'blocked', 'review_requested', 'handoff_pending')
          AND NEW.owner_agent_id IS NULL;
        SELECT RAISE(ABORT, 'done task requires completed_at')
        WHERE NEW.status = 'done' AND NEW.completed_at IS NULL;
        SELECT RAISE(ABORT, 'non-done task cannot have completed_at')
        WHERE NEW.status != 'done' AND NEW.completed_at IS NOT NULL;
    END;
    """,
    # task_leases_validate_insert / task_leases_validate_update are intentionally
    # identical. SQLite requires separate triggers for INSERT and UPDATE.
    """
    CREATE TRIGGER IF NOT EXISTS task_leases_validate_insert
    BEFORE INSERT ON task_leases
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'invalid lease state')
        WHERE NEW.state NOT IN ('active', 'released');
        SELECT RAISE(ABORT, 'active lease cannot have released_at')
        WHERE NEW.state = 'active' AND NEW.released_at IS NOT NULL;
        SELECT RAISE(ABORT, 'released lease requires released_at')
        WHERE NEW.state = 'released' AND NEW.released_at IS NULL;
        SELECT RAISE(ABORT, 'lease session must belong to the same agent')
        WHERE NEW.session_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM sessions s
              WHERE s.id = NEW.session_id AND s.agent_id = NEW.agent_id
          );
        SELECT RAISE(ABORT, 'active lease session must be active')
        WHERE NEW.state = 'active'
          AND NEW.session_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM sessions s
              WHERE s.id = NEW.session_id
                AND s.agent_id = NEW.agent_id
                AND s.status = 'active'
                AND s.ended_at IS NULL
          );
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS task_leases_validate_update
    BEFORE UPDATE ON task_leases
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'invalid lease state')
        WHERE NEW.state NOT IN ('active', 'released');
        SELECT RAISE(ABORT, 'active lease cannot have released_at')
        WHERE NEW.state = 'active' AND NEW.released_at IS NOT NULL;
        SELECT RAISE(ABORT, 'released lease requires released_at')
        WHERE NEW.state = 'released' AND NEW.released_at IS NULL;
        SELECT RAISE(ABORT, 'lease session must belong to the same agent')
        WHERE NEW.session_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM sessions s
              WHERE s.id = NEW.session_id AND s.agent_id = NEW.agent_id
          );
        SELECT RAISE(ABORT, 'active lease session must be active')
        WHERE NEW.state = 'active'
          AND NEW.session_id IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM sessions s
              WHERE s.id = NEW.session_id
                AND s.agent_id = NEW.agent_id
                AND s.status = 'active'
                AND s.ended_at IS NULL
          );
    END;
    """,
    # task_dependencies_validate_insert / task_dependencies_validate_update are
    # intentionally identical. SQLite requires separate triggers for INSERT and UPDATE.
    """
    CREATE TRIGGER IF NOT EXISTS task_dependencies_validate_insert
    BEFORE INSERT ON task_dependencies
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'task cannot depend on itself')
        WHERE NEW.task_id = NEW.depends_on_task_id;
        SELECT RAISE(ABORT, 'invalid dependency kind')
        WHERE NEW.kind NOT IN ('blocks');
    END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS task_dependencies_validate_update
    BEFORE UPDATE ON task_dependencies
    FOR EACH ROW
    BEGIN
        SELECT RAISE(ABORT, 'task cannot depend on itself')
        WHERE NEW.task_id = NEW.depends_on_task_id;
        SELECT RAISE(ABORT, 'invalid dependency kind')
        WHERE NEW.kind NOT IN ('blocks');
    END;
    """,
)


SESSION_REQUIRED_COLUMNS: dict[str, str] = {
    "label": "TEXT",
    "heartbeat_at": "TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP",
    "fingerprint": "TEXT",
    "fingerprint_label": "TEXT",
    "git_branch": "TEXT",
    "git_base_ref": "TEXT",
    "git_dirty": "INTEGER",
    "git_staged_files_json": "TEXT",
    "git_changed_files_json": "TEXT",
}
AGENT_REQUIRED_COLUMNS: dict[str, str] = {
    "role": "TEXT NOT NULL DEFAULT ''",
    "specialty": "TEXT NOT NULL DEFAULT ''",
}
WATCH_REQUIRED_COLUMNS: dict[str, str] = {
    "last_sent_event_id": "INTEGER NOT NULL DEFAULT 0",
    "last_ack_event_id": "INTEGER NOT NULL DEFAULT 0",
    "last_acknowledged_at": "TEXT",
}


@dataclass(frozen=True)
class LexPaths:
    root: Path
    lex_dir: Path
    db_path: Path


def resolve_paths(root: str | Path | None = None) -> LexPaths:
    workspace_root = Path(root or Path.cwd()).resolve()
    lex_dir = workspace_root / DEFAULT_LEX_DIRNAME
    return LexPaths(root=workspace_root, lex_dir=lex_dir, db_path=lex_dir / DEFAULT_DB_NAME)


def find_lex_root(start: Path | None = None) -> LexPaths | None:
    """Walk up from start (or cwd) looking for a .lex directory with a database."""
    current = (start or Path.cwd()).resolve()
    while True:
        candidate = current / DEFAULT_LEX_DIRNAME
        if candidate.is_dir() and (candidate / DEFAULT_DB_NAME).exists():
            return resolve_paths(current)
        parent = current.parent
        if parent == current:
            return None
        current = parent


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    _OPEN_CONNECTIONS.append(conn)
    return conn


def connection_checkpoint() -> int:
    return len(_OPEN_CONNECTIONS)


def close_connections_since(checkpoint: int) -> None:
    if checkpoint < 0:
        checkpoint = 0
    tail = _OPEN_CONNECTIONS[checkpoint:]
    del _OPEN_CONNECTIONS[checkpoint:]
    for conn in tail:
        try:
            conn.close()
        except Exception:
            pass


def initialize_database(conn: sqlite3.Connection) -> None:
    for statement in SCHEMA_STATEMENTS:
        conn.execute(statement)
    migrate_database(conn)
    conn.commit()


def migrate_database(conn: sqlite3.Connection) -> None:
    agent_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(agents)").fetchall()
    }
    for column_name, definition in AGENT_REQUIRED_COLUMNS.items():
        if column_name not in agent_columns:
            try:
                conn.execute(f"ALTER TABLE agents ADD COLUMN {column_name} {definition}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
    migrate_agent_kinds(conn)
    migrate_agent_roles(conn)
    session_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
    }
    for column_name, definition in SESSION_REQUIRED_COLUMNS.items():
        if column_name not in session_columns:
            try:
                conn.execute(f"ALTER TABLE sessions ADD COLUMN {column_name} {definition}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise
    watch_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(watches)").fetchall()
    }
    for column_name, definition in WATCH_REQUIRED_COLUMNS.items():
        if column_name not in watch_columns:
            try:
                conn.execute(f"ALTER TABLE watches ADD COLUMN {column_name} {definition}")
            except sqlite3.OperationalError as exc:
                if "duplicate column name" not in str(exc):
                    raise


def migrate_agent_kinds(conn: sqlite3.Connection) -> None:
    """Rebuild agents table if the kind or status CHECK constraints are out of date."""
    schema = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='agents'"
    ).fetchone()
    if schema is None:
        return
    sql = schema["sql"]
    # Rebuild if any expected values are missing from CHECK constraints
    needs_rebuild = (
        "'ci'" not in sql
        or "'automated'" not in sql
        or "'retired'" not in sql
    )
    if not needs_rebuild:
        return
    conn.execute("PRAGMA foreign_keys = OFF;")
    conn.execute("""
        CREATE TABLE agents_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            kind TEXT NOT NULL CHECK (kind IN ('codex', 'claude', 'cursor', 'gemini', 'ci', 'automated')),
            role TEXT NOT NULL DEFAULT '',
            specialty TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'retired')),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("INSERT INTO agents_new SELECT * FROM agents")
    conn.execute("DROP TABLE agents")
    conn.execute("ALTER TABLE agents_new RENAME TO agents")
    conn.execute("PRAGMA foreign_keys = ON;")


def migrate_agent_roles(conn: sqlite3.Connection) -> None:
    rows = conn.execute("SELECT id, role, specialty FROM agents").fetchall()
    for row in rows:
        role = (row["role"] or "").strip()
        specialty = (row["specialty"] or "").strip()
        if not role or role in CANONICAL_ROLES:
            continue
        mapped = ROLE_MIGRATIONS.get(role)
        if mapped is None:
            continue
        canonical_role, default_specialty = mapped
        conn.execute(
            "UPDATE agents SET role = ?, specialty = ? WHERE id = ?",
            (canonical_role, specialty or default_specialty, row["id"]),
        )


def log_event(
    conn: sqlite3.Connection,
    event_type: str,
    *,
    task_id: int | None = None,
    agent_id: int | None = None,
    session_id: int | None = None,
    payload: dict | None = None,
) -> None:
    cursor = conn.execute(
        """
        INSERT INTO events (event_type, task_id, agent_id, session_id, payload_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (event_type, task_id, agent_id, session_id, json.dumps(payload or {})),
    )
    if task_id is not None and not event_type.startswith("watch."):
        event_id = cursor.lastrowid
        conn.execute(
            """
            UPDATE watches
            SET last_sent_event_id = CASE
                WHEN last_sent_event_id < ? THEN ?
                ELSE last_sent_event_id
            END
            WHERE task_id = ?
            """,
            (event_id, event_id, task_id),
        )


def run_roster_preflight(
    conn: sqlite3.Connection,
    *,
    exclude_reconnecting_agent_id: int | None = None,
) -> list[dict]:
    """Scan the agent roster for drift issues that could cause mis-routing.

    Returns a list of issue dicts, each with:
      kind         — issue category (see below)
      agent_id     — the agent involved
      agent_name   — human-readable name
      detail       — description of the problem

    Issue kinds:
      orphaned_task   — active task owned by an agent with no live session;
                        the task may be stranded and routing is ambiguous.
      retired_leased  — a retired agent still holds an active lease; the lock
                        must be released before that agent can be safely removed.
      duplicate_role  — two or more active agents share the same kind+role,
                        making routing ambiguous for that role.
    """
    issues: list[dict] = []

    # orphaned_task: active-status tasks whose owner has no active session.
    # Exclude tasks owned by the reconnecting agent — their session start IS the resolution.
    rows = conn.execute(
        """
        SELECT t.id AS task_id, t.title, a.id AS agent_id, a.name AS agent_name
        FROM tasks t
        JOIN agents a ON a.id = t.owner_agent_id
        WHERE t.status IN ('claimed', 'in_progress', 'blocked', 'review_requested', 'handoff_pending')
          AND a.id != COALESCE(?, -1)
          AND NOT EXISTS (
              SELECT 1 FROM sessions s
              WHERE s.agent_id = a.id AND s.status = 'active' AND s.ended_at IS NULL
          )
        """,
        (exclude_reconnecting_agent_id,),
    ).fetchall()
    for row in rows:
        issues.append({
            "kind": "orphaned_task",
            "agent_id": row["agent_id"],
            "agent_name": row["agent_name"],
            "detail": f"task {row['task_id']} ({row['title']!r}) is active but owner has no live session",
        })

    # retired_leased: retired agents holding active leases
    rows = conn.execute(
        """
        SELECT a.id AS agent_id, a.name AS agent_name, tl.task_id
        FROM task_leases tl
        JOIN agents a ON a.id = tl.agent_id
        WHERE tl.state = 'active' AND tl.released_at IS NULL AND a.status = 'retired'
        """
    ).fetchall()
    for row in rows:
        issues.append({
            "kind": "retired_leased",
            "agent_id": row["agent_id"],
            "agent_name": row["agent_name"],
            "detail": f"retired agent still holds active lease on task {row['task_id']}",
        })

    # duplicate_role: multiple active agents with same non-empty kind+role
    rows = conn.execute(
        """
        SELECT kind, role, COUNT(*) AS cnt
        FROM agents
        WHERE status = 'active' AND role != ''
        GROUP BY kind, role
        HAVING cnt > 1
        """
    ).fetchall()
    for row in rows:
        agents = conn.execute(
            "SELECT id, name FROM agents WHERE status = 'active' AND kind = ? AND role = ?",
            (row["kind"], row["role"]),
        ).fetchall()
        names = ", ".join(a["name"] for a in agents)
        for agent in agents:
            issues.append({
                "kind": "duplicate_role",
                "agent_id": agent["id"],
                "agent_name": agent["name"],
                "detail": f"multiple active {row['kind']}/{row['role']} agents: {names}",
            })

    return issues


def derive_event_provenance(
    *,
    agent_kind: str | None,
    session_id: int | None,
    task_parent_id: int | None = None,
) -> str:
    """Derive the provenance category for a recorded action.

    Categories (evaluated in priority order):
    - automated: agent kind is 'ci' or 'automated'
    - delegated: action is in a child task (parent_task_id set) and session-backed
    - interactive: action is backed by a live session
    - loose: no session — one-off CLI call, still attributed to the agent
    """
    if agent_kind in ("ci", "automated"):
        return "automated"
    if session_id is not None:
        if task_parent_id is not None:
            return "delegated"
        return "interactive"
    return "loose"


def _normalize_path(p: str) -> tuple[str, ...]:
    """Return path components after resolving . and .. and stripping trailing slashes."""
    return tuple(Path(p.rstrip("/")).parts)


def _paths_overlap(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    """True when one path is an ancestor of (or identical to) the other.

    Uses component-wise prefix comparison to avoid false matches between
    siblings like src/foo and src/foobar.
    """
    min_len = min(len(a), len(b))
    if min_len == 0:
        return False
    return a[:min_len] == b[:min_len]


def detect_path_conflicts(
    conn: sqlite3.Connection,
    candidate_paths: list[str],
    exclude_task_id: int | None = None,
    claiming_branch: str | None = None,
) -> list[dict]:
    """Return conflict records for active tasks whose claimed paths overlap with candidate_paths.

    Each record contains task_id, owner_agent_name, conflicting_path, candidate_path,
    owner_git_branch, and cross_branch. cross_branch is True when both the claiming
    session and the conflicting owner's latest active session have known but different
    branches — these conflicts should be downgraded from hard blocks to warnings.

    Returns an empty list when candidate_paths is empty or no conflicts exist.
    """
    if not candidate_paths:
        return []

    candidate_norm = [(p, _normalize_path(p)) for p in candidate_paths]

    rows = conn.execute(
        """
        SELECT t.id, t.claimed_paths_json, a.name AS owner_agent_name,
               (SELECT s.git_branch FROM sessions s
                WHERE s.agent_id = a.id AND s.status = 'active' AND s.ended_at IS NULL
                ORDER BY s.id DESC LIMIT 1) AS owner_git_branch
        FROM tasks t
        JOIN agents a ON a.id = t.owner_agent_id
        JOIN task_leases tl ON tl.task_id = t.id
        WHERE t.status IN ('claimed', 'in_progress', 'blocked', 'review_requested', 'handoff_pending')
          AND tl.state = 'active'
          AND tl.released_at IS NULL
          AND tl.expires_at > CURRENT_TIMESTAMP
          AND t.id != ?
        """,
        (exclude_task_id if exclude_task_id is not None else -1,),
    ).fetchall()

    conflicts = []
    for row in rows:
        other_paths = json.loads(row["claimed_paths_json"])
        owner_branch = row["owner_git_branch"]
        cross_branch = (
            claiming_branch is not None
            and owner_branch is not None
            and claiming_branch != owner_branch
        )
        for other_raw in other_paths:
            other_norm = _normalize_path(other_raw)
            for cand_raw, cand_norm in candidate_norm:
                if _paths_overlap(cand_norm, other_norm):
                    conflicts.append({
                        "task_id": row["id"],
                        "owner_agent_name": row["owner_agent_name"],
                        "conflicting_path": other_raw,
                        "candidate_path": cand_raw,
                        "owner_git_branch": owner_branch,
                        "cross_branch": cross_branch,
                    })

    return conflicts


def ensure_workspace(root: str | Path | None = None) -> LexPaths:
    paths = resolve_paths(root)
    paths.lex_dir.mkdir(parents=True, exist_ok=True)
    return paths


def fetch_one(conn: sqlite3.Connection, query: str, params: Iterable | tuple = ()) -> sqlite3.Row | None:
    return conn.execute(query, tuple(params)).fetchone()


def list_specialties(conn: sqlite3.Connection) -> list[str]:
    custom = [row["name"] for row in conn.execute("SELECT name FROM specialties ORDER BY name").fetchall()]
    return sorted(set(BUILTIN_SPECIALTIES).union(custom))
