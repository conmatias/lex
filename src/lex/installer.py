from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


MANAGED_START = "<!-- lex:begin -->"
MANAGED_END = "<!-- lex:end -->"


SCAFFOLD_FILES: dict[str, str] = {
    ".lex/ROUTER.md": """# REX Router

Start here before reading deeper context.

## Load Order

1. Read `.lex/adapters/shared/protocol.md`
2. Read the tool-specific adapter for the current agent
3. Read relevant files in `.lex/context/`
4. Read task-specific patterns in `.lex/patterns/`

## Purpose

`lex` separates durable project memory from live operational state:

- Markdown files in `.lex/` store shared project knowledge
- `.lex/lex.db` stores agents, tasks, leases, messages, sessions, and events

Use the CLI for operational updates. Update markdown files intentionally when project knowledge changes.
""",
    ".lex/context/architecture.md": """# Architecture

`lex` is split into three layers:

- Durable memory: markdown files under `.lex/context/` and `.lex/patterns/`
- Coordination state: SQLite database at `.lex/lex.db`
- Adapters: tool-specific instructions under `.lex/adapters/`

Operational state includes agent sessions and heartbeats so task ownership can be correlated with live presence.
""",
    ".lex/context/conventions.md": """# Conventions

- Agent names must match `<agent>-<adjective>-<noun>`
- Tasks are single-owner by default
- Parent tasks may delegate to child tasks in `hypervisor` mode
- Operational changes should go through the `lex` CLI
- Markdown memory should be concise and durable, not used as a chat log
""",
    ".lex/context/decisions.md": """# Decisions

## Initial Install

- Chose Python for v1 due to built-in SQLite support and low dependency overhead
- Chose SQLite plus append-only events instead of pure event sourcing
- Chose single-owner task leases with optional delegated child tasks
""",
    ".lex/context/setup.md": """# Setup

## Bootstrap

```bash
lx init
```

## Register Agents

```bash
lx agent register codex-brisk-otter codex
lx agent register claude-steady-ibis claude
```
""",
    ".lex/context/active-work.md": """# Active Work

Use `lx task list` to inspect live operational state.

This file should only capture durable summaries worth keeping in version control.
""",
    ".lex/patterns/INDEX.md": """# Pattern Index

No patterns are registered yet.
""",
    ".lex/adapters/shared/protocol.md": """# Shared Protocol

All adapters map into the same `lex` coordination model.

## Core Rules

- Claim a task before making substantive changes
- Renew leases while active on a claimed task
- Use task-threaded messages for progress, blockers, and handoffs
- Only the current owner may change task status
- Parent task owners are responsible for delegated child task integration
- Hypervisor delegation should create child tasks instead of sharing write ownership on the parent
- Agents may tail inboxes, task threads, and events in follow mode for lightweight live coordination
- Agents should register active sessions and send heartbeats during longer work intervals
""",
    ".lex/adapters/shared/task-lifecycle.md": """# Task Lifecycle

- `open`
- `claimed`
- `in_progress`
- `blocked`
- `review_requested`
- `handoff_pending`
- `done`
- `abandoned`
""",
    ".lex/adapters/codex/AGENTS.md": """# Codex Adapter

Read `.lex/ROUTER.md` before starting work.

Use the shared protocol in `.lex/adapters/shared/protocol.md`.
""",
    ".lex/adapters/claude/CLAUDE.md": """# Claude Adapter

Read `.lex/ROUTER.md` before starting work.

Use the shared protocol in `.lex/adapters/shared/protocol.md`.
""",
    ".lex/adapters/cursor/cursor-rules.md": """# Cursor Adapter

Cursor participates through the same task and message model.
""",
    ".lex/adapters/gemini/GEMINI.md": """# Gemini Adapter

Read `.lex/ROUTER.md` before starting work.

Use the shared protocol in `.lex/adapters/shared/protocol.md`.
""",
}


@dataclass
class InstallResult:
    created_files: list[str]
    updated_files: list[str]
    skipped_files: list[str]
    warnings: list[str]


@dataclass(frozen=True)
class InstallContext:
    root: Path
    has_git_dir: bool
    has_gitignore: bool
    has_agents_file: bool
    has_claude_file: bool


def inspect_install_context(root: Path) -> InstallContext:
    return InstallContext(
        root=root,
        has_git_dir=(root / ".git").exists(),
        has_gitignore=(root / ".gitignore").exists(),
        has_agents_file=(root / "AGENTS.md").exists(),
        has_claude_file=(root / "CLAUDE.md").exists(),
    )


def codex_bridge_block() -> str:
    return (
        "## lex\n\n"
        f"{MANAGED_START}\n"
        "Read `.lex/adapters/codex/AGENTS.md` before starting work.\n"
        f"{MANAGED_END}\n"
    )


def claude_bridge_block() -> str:
    return (
        "## lex\n\n"
        f"{MANAGED_START}\n"
        "Read `.lex/adapters/claude/CLAUDE.md` before starting work.\n"
        f"{MANAGED_END}\n"
    )


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_if_missing(path: Path, content: str) -> bool:
    if path.exists():
        return False
    ensure_parent(path)
    path.write_text(content, encoding="utf-8")
    return True


def overwrite_file(path: Path, content: str) -> str:
    ensure_parent(path)
    action = "updated" if path.exists() else "created"
    path.write_text(content, encoding="utf-8")
    return action


def upsert_managed_block(path: Path, block: str) -> str:
    ensure_parent(path)
    if not path.exists():
        path.write_text(block, encoding="utf-8")
        return "created"

    original = path.read_text(encoding="utf-8")
    if MANAGED_START in original and MANAGED_END in original:
        start = original.index(MANAGED_START)
        end = original.index(MANAGED_END) + len(MANAGED_END)
        replacement = original[:start] + block + original[end:]
        path.write_text(replacement.strip() + "\n", encoding="utf-8")
        return "updated"

    separator = "\n\n" if original.strip() else ""
    path.write_text(original.rstrip() + separator + block + "\n", encoding="utf-8")
    return "updated"


def update_ignore_file(path: Path, entries: list[str]) -> str:
    ensure_parent(path)
    managed_block = "\n".join(
        ["# lex ignore policy", MANAGED_START, *entries, MANAGED_END]
    )
    if not path.exists():
        path.write_text(managed_block + "\n", encoding="utf-8")
        return "created"

    original = path.read_text(encoding="utf-8")
    if MANAGED_START in original and MANAGED_END in original:
        start = original.index(MANAGED_START)
        end = original.index(MANAGED_END) + len(MANAGED_END)
        prefix = original[:start].rstrip()
        suffix = original[end:].lstrip("\n")
        updated = prefix + ("\n\n" if prefix else "") + managed_block
        if suffix:
            updated += "\n\n" + suffix
        path.write_text(updated.rstrip() + "\n", encoding="utf-8")
        return "updated"

    separator = "\n\n" if original.strip() else ""
    path.write_text(original.rstrip() + separator + managed_block + "\n", encoding="utf-8")
    return "updated"


def ignore_entries(ignore_policy: str, *, created_agents: bool) -> list[str]:
    if ignore_policy == "none":
        return []
    if ignore_policy == "runtime":
        return [".lex/lex.db", ".lex/runtime/"]
    entries = [".lex/"]
    if created_agents:
        entries.extend(["AGENTS.md", "CLAUDE.md"])
    return entries


def install_scaffold(
    root: Path,
    *,
    agent_files: str,
    ignore_policy: str,
    ignore_target: str,
) -> InstallResult:
    result = InstallResult(created_files=[], updated_files=[], skipped_files=[], warnings=[])

    for relative_path, content in SCAFFOLD_FILES.items():
        destination = root / relative_path
        if write_if_missing(destination, content):
            result.created_files.append(relative_path)
        else:
            result.skipped_files.append(relative_path)

    created_agents = False
    integrated_agents = False
    if agent_files == "merge":
        for relative_path, block in (
            ("AGENTS.md", codex_bridge_block()),
            ("CLAUDE.md", claude_bridge_block()),
        ):
            destination = root / relative_path
            existed = destination.exists()
            action = upsert_managed_block(destination, block)
            if not existed and action == "created":
                created_agents = True
                result.created_files.append(relative_path)
            else:
                result.updated_files.append(relative_path)
        integrated_agents = True
    elif agent_files == "overwrite":
        for relative_path, block in (
            ("AGENTS.md", codex_bridge_block()),
            ("CLAUDE.md", claude_bridge_block()),
        ):
            destination = root / relative_path
            action = overwrite_file(destination, block)
            if action == "created":
                created_agents = True
                result.created_files.append(relative_path)
            else:
                result.updated_files.append(relative_path)
        integrated_agents = True
        result.warnings.append("Overwrote root AGENTS.md and CLAUDE.md with lex bridge files.")
    elif agent_files == "assisted":
        result.warnings.append("Preserved root agent files and prepared for an assisted semantic merge.")
    else:
        result.warnings.append("Preserved existing AGENTS.md and CLAUDE.md by request.")

    entries = ignore_entries(ignore_policy, created_agents=created_agents)
    if entries:
        ignore_path = root / ".gitignore" if ignore_target == "gitignore" else root / ".git" / "info" / "exclude"
        action = update_ignore_file(ignore_path, entries)
        relative_ignore = str(ignore_path.relative_to(root))
        if action == "created":
            result.created_files.append(relative_ignore)
        else:
            result.updated_files.append(relative_ignore)

    if agent_files in {"preserve", "assisted"}:
        for relative_path in ("AGENTS.md", "CLAUDE.md"):
            if (root / relative_path).exists():
                result.warnings.append(
                    f"{relative_path} was left unchanged. Agents will not auto-discover lex until that file references `.lex/`."
                )
    elif ignore_policy == "all" and integrated_agents and not created_agents:
        result.warnings.append(
            "Full local-only ignore mode cannot hide already tracked root agent files from Git history."
        )
    return result
