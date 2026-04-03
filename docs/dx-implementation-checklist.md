# dx v1 Implementation Checklist

Derived from `docs/dx-screen-contract.md` and `docs/dx-architecture.md`.
This checklist defines the acceptance gate for each implementation slice.
Tests live in `tests/test_dx_acceptance.py`.

## Implementation Sequence

The safe order is:

1. **Roster** — read-only, pure DB query
2. **Agent-scoped file list** — read-only, mix of DB and filesystem
3. **Read-only tabbed diff view** — read-only, git + DB
4. **Message and annotation actions** — first write surface
5. **Constrained quick edit mode** — filesystem write + event attribution

Each slice must be shippable independently. Slices 1–3 are read-only.
Slices 4–5 write through Lex verbs only.

---

## Slice 1: Roster

**Data source:** `_query_dx_roster()` in `lex.dashboard`

**Already done:**
- [x] Query exists and returns required fields
- [x] Only active agents appear
- [x] `claimed_path_count` from `tasks.claimed_paths_json`
- [x] `changed_file_count` from `sessions.git_changed_files_json`
- [x] `is_stale` flag when heartbeat > 15 minutes old
- [x] Terminal task statuses (done/abandoned) excluded

**Remaining for UI:**
- [ ] Row state derivation: map `(session_id, task_status, is_stale)` to `idle | active | waiting | blocked | stale`
- [ ] Render roster rows with all required fields (agent name, role/kind, task title, task status, session health, claimed count, changed count, last activity)
- [ ] Selection drives Region 2 (file browser scope)
- [ ] Empty state: "No active agents" + "Launch or resume work in lx"
- [ ] Primary actions: open agent context, jump to task thread, send message, stop following

**Row state mapping:**

| Condition | State |
|---|---|
| `session_id IS NULL` | no session (agent is registered but not active) |
| `session_id IS NOT NULL, task_id IS NULL` | `idle` |
| `task_status IN ('claimed', 'in_progress')` | `active` |
| `task_status = 'blocked'` | `blocked` |
| `is_stale = 1` | `stale` |
| session active, no recent file/message activity | `waiting` (requires additional heuristic) |

---

## Slice 2: Agent-Scoped File Browser

**Data source:** `tasks.claimed_paths_json` + `sessions.git_changed_files_json`

**Already done:**
- [x] `claimed_paths_json` on tasks is a validated JSON array
- [x] `git_changed_files_json` on sessions captured via `capture_git_snapshot`
- [x] Conflict detection: overlapping claimed paths across tasks are identifiable

**Remaining for UI:**
- [ ] Derive the three file browser sections from DB data:
  - `Claimed Paths`: `json_each(tasks.claimed_paths_json)` for selected agent's active task
  - `Recently Changed Files`: `json_each(sessions.git_changed_files_json)` for selected agent's active session
  - `Files With Open Intervention Tabs`: tracked in UI state
- [ ] File state classification: `claimed_only | changed_unreviewed | changed_reviewed | flagged | conflicted`
  - `conflicted`: path appears in claimed_paths_json of more than one active task
  - `flagged`: `dx.flag` event exists for this path
  - `changed_*`: file appears in `git_changed_files_json`
- [ ] Each file row must show: relative path, change state, owning task, last change time, conflict marker
- [ ] Empty state: "No claimed or changed files for this agent"
- [ ] Primary actions: open in tab, reveal task context, reveal diff summary

---

## Slice 3: Tabbed Diff View (Read-Only)

**Data source:** `sessions.git_base_ref` + `git diff <base_ref>...HEAD -- <path>`

**Already done:**
- [x] `git_base_ref` stored on sessions, exposed in `_query_sessions`
- [x] `git_changed_files_json` provides the file list

**Remaining for UI:**
- [ ] Tab is keyed by `(file_path, agent_id, task_id)`
- [ ] Tab header: file name, agent name, task title, change badge
- [ ] Default mode is `diff` — run `git diff <base_ref>...HEAD -- <path>` on demand
- [ ] `file` mode: show current file contents with changed-line highlights
- [ ] `quick_edit` mode: reserved state, not implemented in this slice
- [ ] Each diff hunk must show: task_id, agent name, session_id, timestamp
  - Hunk attribution is approximated by correlating `claude.hook.post_tool_use` events for the file path
- [ ] Loading state: placeholder hunks plus file path and task metadata
- [ ] Error states: diff failed (show inline error + retry), file missing (keep tab open with missing-file state)
- [ ] Retain tab context (task + agent attribution) when user navigates back to roster

---

## Slice 4: Message and Annotation Actions

**Write surface:** `messages` table + `events` table via Lex verbs

**Already done:**
- [x] `log_event("dx.annotation", task_id=..., agent_id=..., payload=...)` works
- [x] `log_event("dx.flag", task_id=..., agent_id=..., payload=...)` works
- [x] `INSERT INTO messages` scoped to task thread works
- [x] These writes do not mutate session lifecycle, task ownership, or lease state

**Remaining for UI:**
- [ ] Action strip shows context-sensitive actions (max 3 at a time):
  - Roster focus: `Message Task`, `Open Task Thread`
  - File focus: `Open Diff`, `Open Task`, `Flag Conflict`
  - Hunk focus: `Message Task`, `Annotate Hunk`, `Flag For Review`
- [ ] `Message Task` invokes `lx msg send --task <id>` equivalent
- [ ] `Annotate Hunk` records `dx.annotation` event with hunk context
- [ ] `Flag For Review` records `dx.flag` event with file + reason

**Invariants (enforced by test):**
- dx may NOT directly update `tasks.owner_agent_id`
- dx may NOT directly update `sessions.status` or `sessions.ended_at`
- dx may NOT directly update `task_leases.expires_at`

---

## Slice 5: Constrained Quick Edit

**Write surface:** filesystem write + `dx.edit` event attribution

**Not yet done (no existing infrastructure):**
- [ ] `quick_edit` mode entered explicitly (never default)
- [ ] Scoped to the currently focused file only
- [ ] Produces a local diff preview before save
- [ ] On save: write to filesystem, then record `dx.edit` event with task + agent context
- [ ] On exit: offer `save`, `discard`, `send note` actions
- [ ] Writeback-failed state: keep operator input visible, show retry or copy action

**Required event schema for quick edit:**
```
event_type: dx.edit
task_id: <task_id>
agent_id: <operator agent or null>
session_id: <session_id if attached>
payload: {
  "file": "<relative path>",
  "diff_summary": "<line count or hunk count>",
  "provenance": "dx"
}
```

---

## Hard Rules (from screen contract)

These must hold across all slices:

- Agent context follows the operator until they intentionally switch agents.
- File browsing defaults to touched files, not the whole repo tree.
- Tabs retain task and agent attribution at all times.
- Diff is the default tab mode.
- Quick edit is always explicit and never the default.
- Every intervention action must be traceable to a task or hunk context.
- dx writes back only through Lex verbs: messages, event log, filesystem.
- dx must NOT directly mutate: task ownership, session lifecycle, leases, delegation state.
- The action strip must never expose more than 3 primary actions at once.
