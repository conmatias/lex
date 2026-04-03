# dx v1 Screen Contract

This document turns the `dx` product brief into an implementable v1 interaction contract.

`dx` is the live intervention layer for agent-driven development. It is not a dashboard and it is not a general-purpose editor. The screen contract below is built to keep the operator anchored to live agent work, file-level context, and traceable intervention actions.

## Product Scope

v1 supports:

- active agent supervision
- agent-scoped file browsing
- tabbed file inspection
- diff and change attribution
- lightweight intervention actions
- constrained quick edits when precision matters

v1 does not support:

- full editor parity
- task creation or delegation
- session lifecycle control
- metrics dashboards
- generalized analytics views

## Top-Level Layout

The default layout has three primary regions plus one context-sensitive action strip.

1. Agent roster
2. Agent-scoped file browser
3. Tabbed file and diff workspace
4. Action strip

The operator should be able to move from left to right through these regions without opening secondary screens for the common supervision flow.

## Region 1: Agent Roster

Purpose: choose the live workstream that needs attention.

Each row shows:

- agent name
- role or kind
- current task title
- task status
- session health
- claimed file count
- changed file count
- last activity time

Row states:

- `idle`: session active, no current claimed task
- `active`: session active and task claimed
- `waiting`: active but no recent file or message activity
- `blocked`: active session with blocked task
- `stale`: no active session but task still claimed

Primary actions from roster focus:

- open agent context
- jump to current task thread
- send task message
- stop following this agent

## Region 2: Agent-Scoped File Browser

Purpose: show where the selected agent's work is becoming concrete.

The file browser is filtered by the selected roster item. It does not show the entire repository by default.

Sections:

- `Claimed Paths`
- `Recently Changed Files`
- `Files With Open Intervention Tabs`

Each file row shows:

- relative path
- change state
- owning task
- last change time
- conflict marker when multiple agents touch the path

File states:

- `claimed_only`
- `changed_unreviewed`
- `changed_reviewed`
- `flagged`
- `conflicted`

Primary actions from file focus:

- open in tab
- reveal task context
- reveal diff summary

## Region 3: Tabbed File And Diff Workspace

Purpose: inspect a concrete file in the context of the agent and task that touched it.

Each tab is keyed by:

- file path
- agent id
- task id

If the same file is opened from two different agents or tasks, `dx` should be able to represent distinct tab contexts even if the initial implementation collapses them visually.

Tab header metadata:

- file name
- agent name
- task title
- change badge
- dirty badge for local human edits

The workspace has three display modes:

1. `diff`
2. `file`
3. `quick_edit`

### Diff Mode

Default mode for opened files.

Shows:

- unified diff hunks against the active base ref
- hunk attribution metadata
- inline markers for flagged or annotated hunks

Each hunk shows:

- task id
- agent name
- session id
- timestamp or relative recency
- intervention markers

### File Mode

Read-first source view with change overlay.

Shows:

- current file contents
- inline highlights for changed lines
- optional gutter markers that map to diff hunks

This mode exists so the operator can understand the current file, not just the patch.

### Quick Edit Mode

This is intentionally narrow. It exists for surgical human correction, not open-ended authoring.

Rules:

- entered explicitly from a file tab
- limited to the currently focused file
- preserves task and agent context while editing
- always produces a local diff preview before save
- offers `save`, `discard`, and `send note` actions on exit

If quick edit is not implemented in the first code slice, the mode should still exist as a reserved state in the contract so the rest of the UI model does not assume read-only tabs forever.

## Region 4: Action Strip

Purpose: surface only the intervention actions that make sense for the current focus.

The action strip sits below the main workspace and changes with focus context.

Roster-focused actions:

- `Message Task`
- `Open Task Thread`

File-focused actions:

- `Open Diff`
- `Open Task`
- `Flag Conflict`

Hunk-focused actions:

- `Message Task`
- `Annotate Hunk`
- `Flag For Review`

Quick-edit-focused actions:

- `Save`
- `Discard`
- `Send Note`

The strip should never expose more than three primary actions at once.

## Core Navigation Flow

The primary supervision flow is:

1. Select an active agent in the roster.
2. Inspect the agent-scoped file browser.
3. Open a touched file into a tab.
4. Review the diff or file view.
5. Intervene with a message, annotation, flag, or constrained edit.
6. Return to the roster or move to another touched file.

This is the default path through the product. Any interaction that pulls the user into detached dashboards, hidden modal flows, or unrelated repo browsing is a design failure for v1.

## View States

### Empty States

Roster empty:

- message: `No active agents`
- hint: `Launch or resume work in lx`

Agent file list empty:

- message: `No claimed or changed files for this agent`
- hint: `Wait for file activity or switch agents`

No tabs open:

- message: `Open a touched file to inspect live work`

### Loading States

Roster loading:

- placeholder rows

File list loading:

- placeholder file rows scoped to the selected agent

Diff loading:

- placeholder hunks plus current file path and task metadata

### Error States

Session data missing:

- show stale warning on roster row

Diff generation failed:

- show inline error with retry action

File missing on disk:

- keep tab open with missing-file state and task attribution intact

Writeback failed:

- keep operator input visible and show retry or copy action

## Data Contract Per Region

Agent roster requires:

- agent id
- agent name
- role or kind
- active session id
- session health
- current task id
- current task title
- claimed path count
- changed file count
- last activity timestamp

Agent-scoped file browser requires:

- task id
- file path
- claim source
- change status
- last modified timestamp
- conflict indicator

Workspace tab requires:

- file path
- task id
- task title
- agent id
- agent name
- session id
- base ref
- diff hunks
- annotations or flags attached to hunks

Quick edit requires:

- current file content
- editable buffer
- unsaved state
- resulting diff preview

## Writeback Contract

`dx` may write back through shared Lex verbs only.

Allowed write actions:

- task-thread messages
- file or hunk annotations as events
- review flags as events

Quick edits should write to the filesystem, but any operator intervention taken because of that edit should still record a Lex message or event so the action remains attributable.

`dx` should not directly mutate:

- task ownership
- session lifecycle
- leases
- delegation state

## Interaction Rules

- Agent context follows the operator until they intentionally switch agents.
- File browsing defaults to touched files, not the whole repo tree.
- Tabs retain task and agent attribution at all times.
- Diff is the default tab mode.
- Quick edit is always explicit and never the default.
- Every intervention action should be traceable to a task or hunk context.

## v1 Acceptance Criteria

The screen contract is satisfied when an operator can:

1. see active agents and understand who is working on what
2. open only the files relevant to a selected agent
3. inspect current changes with task and agent attribution
4. send a task message or annotate a hunk without leaving the screen
5. perform a constrained quick edit and see the resulting diff
6. move back to the roster without losing open tab context

## Notes For Sequencing

The safest first implementation slice is:

1. roster
2. agent-scoped file list
3. read-only tabbed diff view
4. message and annotation actions
5. constrained quick edit mode

This preserves the supervision identity of `dx` while leaving room for the lightweight edit capability described in the product brief.
