# State of the Codebase: Lex & dx

## Overview

**Lex** is a repo-local coordination and supervision layer for multi-agent coding workflows. It provides a shared state system (SQLite), a task management engine, and a supervised worker infrastructure.

**dx** (Developer Experience) is the interactive companion to Lex. It serves as the "live intervention layer" where humans supervise, redirect, and tactilely intervene in agent-driven work.

---

## 1. Core Architecture

### Shared Core (Lex)
The foundation of the system is the shared core, which enforces the **Human Hypervisor** model.
- **Durable Memory**: Markdown files in `.lex/` (e.g., `ROUTER.md`, `context/`, `patterns/`) store shared project knowledge.
- **Operational State**: A SQLite database (`.lex/lex.db`) manages:
    - **Agents**: Unique identities (e.g., `codex-brisk-otter`).
    - **Tasks**: Claimable units of work with priority, status, and threading.
    - **Sessions**: Explicit presence tracking with heartbeats and bootstrap packets.
    - **Messages**: Task-scoped communication between agents and humans.
    - **Events**: An append-only log of all system actions (provenance).
    - **Workers**: Supervised local runtimes for executing agent code.

### Git Awareness
Lex is deeply integrated with the repository's Git state:
- **Session Snapshots**: Captures branch, base ref, and dirty state upon session start/heartbeat.
- **Conflict Detection**: Detects overlapping claimed paths across different agent tasks.
- **Diff Attribution**: Correlates filesystem changes with specific tasks and events.

---

## 2. Command Surfaces

### `lx` (The Headless Engine)
The primary CLI for automation, scripting, and core coordination.
- **Key Verbs**: `init`, `agent`, `session`, `task`, `msg`, `watch`, `worker`, `dispatch`.
- **Merge Workflow**: Handles assisted semantic merges of agent files (`AGENTS.md`, `CLAUDE.md`).
- **Discovery**: Local-network peer discovery via UDP multicast (`239.20.20.20:1901`).

### `dx` (The Supervision Interface)
A standalone TUI designed for active intervention.
- **v1/v2 (Pane-First)**: Optimized for file inspection, diff viewing, and lightweight editing.
- **v3 (Prompt-First)**: Current experimental direction. A hypervisor shell where the command prompt is primary, and agent sessions run in embedded PTY-backed terminal slices.

---

## 3. The dx v3 Evolution

The codebase is currently transitioning to the **v3 Prompt-First Hypervisor Shell** (documented in `docs/dx-v3-hypervisor-plan.md`).

### New Components:
- **PTY Runtime (`lex.dx.pty_runtime`)**: Manages multiple background subprocesses using pseudo-terminals. Supports collapsible slices and "attention required" detection.
- **Slash Commands**: A structured control plane (e.g., `/spawn`, `/focus`, `/broadcast`) for managing multiple agent runtimes.
- **Horizontal Feed Layout**: A shift from vertical panes to horizontally stacked terminal slices that expand/collapse based on activity.
- **VT Screen Buffer**: Implements full terminal emulation inside dx panes to support complex CLI tools (Claude Code, etc.).

---

## 4. Codebase Structure

```text
/
├── build.sh                # Global installation script (lx and dx)
├── pyproject.toml          # Project metadata and entrypoints
├── src/
│   └── lex/
│       ├── cli.py          # lx CLI entrypoint and command logic
│       ├── coordination.py # Shared core coordination verbs
│       ├── dashboard.py    # Read models for TUIs
│       ├── db.py           # SQLite schema and low-level access
│       ├── discovery.py    # LAN discovery protocol
│       ├── dx/             # dx supervision shell module
│       │   ├── app.py      # dx entrypoint
│       │   ├── pty_runtime.py # v3 PTY session manager
│       │   └── tui.py      # Curses-based TUI implementation
│       ├── installer.py    # Repository scaffolding logic
│       ├── merge_workflow.py # Agent file integration
│       └── worker_runtime.py # Supervised worker engine
└── tests/                  # Exhaustive test suite (110+ tests)
```

---

## 5. Current Project Status (Registry Snapshot)

The project is currently in the execution phase of **dx v3**. 
- **Completed**: PTY runtime design, slash-command grammar, TUI layout design, and initial PTY-to-shell wiring.
- **In Progress**: Full VT screen buffer implementation and stabilization of the global install surface.
- **Active Branch**: `dx-experimental` contains the v3 work, while `main` holds the stable core.

*Last Updated: 2026-04-05*
