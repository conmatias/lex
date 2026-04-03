# Architecture Note: dx Service Boundaries

This note defines the shared-core service and data boundaries for **dx (Developer Experience)** to operate as a first-class client over Lex.

## 1. Shared Data Surface (Read)

dx reads from `lex.db` to drive its three-pane layout. It should utilize the existing `lex.dashboard.DashboardState` (or a specialized extension) to access:

- **Active Agent Roster**: From `agents` table (status='active').
- **Agent Context**:
    - **Current Task**: Linked via `tasks.owner_agent_id` or `task_leases`.
    - **Claimed Paths**: From `tasks.claimed_paths_json`.
    - **Changed Files**: From `sessions.git_changed_files_json` (populated via `capture_git_snapshot`).
    - **Git Context**: `git_branch`, `git_base_ref`, `git_dirty` from the `sessions` table.
- **Task Interaction**:
    - **Messages**: From `messages` table (task-scoped).
    - **Events**: From `events` table (task and session scoped).

## 2. Shared Write Surface (Actions)

dx intervenes in the workflow by writing back into Lex. It MUST NOT mutate core Lex state (leases, sessions, task status) directly, but rather through defined Lex verbs:

- **Intervention Messages**: Use `msg_send` on the task thread.
- **Event Annotations**: Log `dx.annotation` or `dx.flag` events into the `events` table.
- **Status Updates**: dx can trigger `task_status` updates when a review/intervention is completed.

## 3. Filesystem & Diff Generation

Diffs are NOT stored in the database. dx is responsible for generating the diff overlay on-demand:

- **Base Ref**: Read `git_base_ref` from the active session.
- **Generation**: dx uses `git diff <base_ref>...HEAD -- <path>` to generate hunks for the file view.
- **Attribution**: dx maps hunk line numbers back to the `events` or `messages` that produced them by correlating with the session's event history.

## 4. Proposed Client Interface

dx should be implemented as a separate module (e.g., `lex.dx`) or a standalone application that imports `lex.db`, `lex.dashboard`, and `lex.coordination`.

### Key Service Boundaries:
- **Lex DB**: The single source of truth for operational state.
- **Git Repository**: The source for file content and diffs.
- **Lex Verbs**: The only way to mutate state from dx.

## 5. Summary for Implementation

v1 implementation should focus on:
1. Extending `DashboardState` to include `git_changed_files_json` and `git_base_ref` if not already fully exposed for UI consumption.
2. Building the three-pane TUI/GUI that reads this state.
3. Mapping UI "Intervene" actions to `lex msg send` and `lex event` logging.
