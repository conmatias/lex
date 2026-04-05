# dx v3 Layout Spec: Horizontal Slice Feed

## Design Premise

The v2 shell is organized around a file browser with a workspace on the right. That model is fine for inspecting one agent working one file at a time. It does not scale to multiple agents in parallel, and it forces the operator to navigate into the tool rather than letting the tool surface what needs attention.

The v3 shell inverts this. The default view is a vertical feed of agent slices — one horizontal row per active agent — with a persistent prompt at the bottom. The operator does not navigate to find work; work surfaces automatically by expanding slices when attention is required.

The closest analogy is not a terminal multiplexer. It is a messaging app in a terminal: a live feed of status rows that expand when something needs a response, plus a composer at the bottom.

---

## Layout Overview

```
┌──────────────────────────────────────────────────────────────┐
│  dx  ›  claude-brisk-falcon  ›  #25 design dx v3 layout      │  header
├──────────────────────────────────────────────────────────────┤
│  ● claude-brisk-falcon  [active]   app.py          2m ago    │  slice (collapsed)
├──────────────────────────────────────────────────────────────┤
│▶ ╔ codex-brisk-falcon   [REVIEW]  ══════════════════════════╗│  slice (expanded, focused)
│  ║ Task #21: Build dx action strip                          ║│
│  ║                                                          ║│
│  ║  Running pytest tests/test_dx_shell.py ...               ║│
│  ║  14 passed in 0.8s                                       ║│
│  ║  Ready to commit. Proceed? [y/n]                         ║│
│  ║                                                          ║│
│  ╠── actions ─────────────────────────────────────────────── ║│
│  ║  [y] approve  [n] deny  [m] message  [o] files           ║│
│  ╚══════════════════════════════════════════════════════════╝│
├──────────────────────────────────────────────────────────────┤
│  ○ gemini-brisk-badger  [idle]                     12m ago   │  slice (collapsed)
├──────────────────────────────────────────────────────────────┤
│  ⚠ codex-brisk-otter   [blocked]  waiting for task  5m ago  │  slice (collapsed, warn)
├──────────────────────────────────────────────────────────────┤
│                                                              │
│                                                              │  empty space
├──────────────────────────────────────────────────────────────┤
│ ›  /send codex approve and push_                             │  prompt bar
└──────────────────────────────────────────────────────────────┘
```

**Three fixed regions, top to bottom:**

1. **Header bar** — 1 row. Shows `dx › <routing target> › <task context>`.
2. **Slice feed** — fills remaining height. One slice per active agent. Slices collapse and expand in place.
3. **Prompt bar** — 1 row, always visible. Accepts slash commands and routed messages.

---

## Slice Anatomy

Each agent occupies a horizontal slice. A slice has two display modes: collapsed and expanded.

### Collapsed (default)

One row. Contains:

```
  <state-icon> <agent-name>  [<state-label>]  <last-file-or-step>  <time-ago>
```

State icon:
- `●` active
- `○` idle
- `⊙` thinking / running tool
- `▲` review requested
- `!` permission needed
- `⚠` blocked
- `–` stale

The row is dim for idle/stale. Full brightness for active. Bold + color for review/permission.

### Expanded

Multiple rows. The slice grows to show:

- **Header row** — same as collapsed but with `▼` or `▶` indicator and a heavier border
- **Task context line** — task title and status
- **Terminal output** — last N lines of the agent's stdout/stderr feed, scrollable
- **Action strip** — 1 row at the bottom of the expanded area, context-sensitive, max 3 actions

The expanded height is dynamic: review/permission states get more rows; thinking/active get fewer.

### Expansion rules

| State | Expansion behavior |
|---|---|
| `idle` | Never auto-expands. Expands only on explicit user focus. |
| `thinking` | Never auto-expands. One-line summary shows abbreviated current step. |
| `active` | Never auto-expands. Collapsed by default. |
| `review` | Auto-expands when state is entered. Stays expanded until acknowledged. |
| `permission` | Auto-expands and moves to top of feed (above other expanded slices). Bold border. |
| `escalation` | Same as permission but with alert marker. |
| `blocked` | Never auto-expands. Warning icon in collapsed row. |
| `stale` | Never auto-expands. Dim text. |

The feed is sorted: attention states first (permission, escalation, review), then active/thinking, then idle/stale.

---

## Prompt Bar

The prompt bar is persistent. It never hides. It accepts two input categories:

**1. Slash commands** — input starting with `/`

Parsed and dispatched by dx. Examples:

```
/focus codex          — set routing target to codex-brisk-falcon
/send gemini check the auth module
/broadcast all agents: switching to feature/auth branch
/expand codex         — expand that agent's slice
/collapse codex
/files                — open context drawer to files view
/tasks                — open context drawer to task view
/diff src/lex/db.py   — open context drawer to diff view
/spawn claude         — spawn a new claude session
/stop codex           — stop the focused agent
/resume codex
/help                 — show command list in context drawer
```

**2. Freeform messages** — everything else

Routed to the current routing target (shown in the header). If no target is set, the prompt shows a hint.

**Routing target:**

The current routing target is always visible in the header:

```
dx  ›  codex-brisk-falcon  ›  #21 build dx action strip
```

The target changes via:
- `/focus <agent>`
- Pressing `enter` on a collapsed slice (sets target without expanding)
- Pressing `space` on a collapsed slice (expands and sets target)

**History:**

Up/down arrows scroll through prompt history. The history shows previous slash commands and routed messages. It is not the terminal output of any agent.

---

## Focus Model

Three distinct concepts, always visible:

| Concept | What it means | How it changes |
|---|---|---|
| **Slice focus** | Which slice the keyboard cursor is on | `j`/`k` |
| **Routing target** | Which agent the prompt sends to | `/focus`, `enter` on slice |
| **Expanded slice** | Which slice is currently showing full output | `space` on slice, auto |

These can differ. The operator can read codex output while routing messages to claude.

---

## Keyboard Map

### Global

| Key | Action |
|---|---|
| `j` / ↓ | Move slice focus down |
| `k` / ↑ | Move slice focus up |
| `enter` | Set routing target to focused slice (no expand) |
| `space` | Toggle expand/collapse on focused slice |
| `tab` | Switch keyboard focus between slice feed and prompt bar |
| `c` | Toggle context drawer |
| `r` | Refresh all slice data |
| `q` | Quit |

### When a review/permission slice is focused

| Key | Action |
|---|---|
| `y` | Approve (sends `y` to agent's stdin) |
| `n` | Deny (sends `n`) |
| `m` | Open message prompt for a freeform response |
| `o` | Open context drawer to files touched by this agent |
| `esc` | Dismiss / collapse without responding |

### In the prompt bar

| Key | Action |
|---|---|
| `↑` / `↓` | History navigation |
| `enter` | Submit |
| `esc` | Clear prompt, return slice focus to feed |
| `tab` | Autocomplete slash command |

---

## Context Drawer

A toggleable panel that appears above the prompt bar. Activated by `c` or by slash commands (`/files`, `/tasks`, `/diff`).

The drawer is not a primary navigation surface. It is reference material — the operator drops into it briefly and returns to the feed.

Drawer views:

| View | Contents |
|---|---|
| `tasks` | Active task list, filtered to routing target's task if set |
| `files` | Claimed + changed files for routing target agent |
| `diff` | `git diff <base_ref>...HEAD -- <path>` for selected file |
| `help` | Slash command reference |

The drawer occupies the bottom third of the terminal height. The slice feed compresses upward to accommodate it. Slices that do not fit collapse automatically while the drawer is open.

---

## State Markers in the Header

The header row shows one-line context for the routing target:

```
dx  ›  claude-brisk-falcon  ›  #25 design dx v3 layout  [stale: 3]
```

The trailing `[stale: 3]` indicates 3 slices have stale heartbeats. Other possible indicators:

- `[review: 1]` — one slice needs review
- `[blocked: 2]` — two agents are blocked
- `[permission: 1]` — one permission request pending (always shown first)

This lets the operator know the system state at a glance without reading every slice.

---

## Transition from v2

v2 and v3 are incompatible layouts. The transition plan:

**What carries forward:**
- `build_dx_view()` — data model for agents and files remains correct
- All writeback helpers (`dx_send_message`, `dx_log_annotation`, `dx_flag_file`, `dx_log_edit_event`, `dx_update_task_priority`, `dx_update_task_status`) — unchanged
- `build_diff()` and `read_file_contents()` — move to the context drawer
- The `dx` binary entry point — unchanged

**What changes:**
- `DxTui` class in `app.py` is replaced by the new shell
- The three-pane layout (roster / files / workspace) becomes the context drawer
- The action strip becomes slash commands and per-slice inline actions
- Quick-edit mode moves into the context drawer (opened via `/diff` + `e`)

**Module split (per v3 plan):**

```
src/lex/dx/
├── app.py          — entry point, unchanged interface
├── shell.py        — new: top-level TUI orchestration (replaces DxTui)
├── commands.py     — new: slash-command parsing and dispatch
├── pty_runtime.py  — new: PTY session management
├── models.py       — new: TerminalPane, SliceState, RoutingTarget
└── context.py      — new: drawer state (tasks, files, diff)
```

The v2 `DxTui` class in `app.py` stays functional until `shell.py` reaches feature parity. The `run_dx()` function in `app.py` will switch from `DxTui` to the new shell once the v3 slice is ready.

---

## Non-Goals for First v3 Slice

- Full PTY multiplexer (no arbitrary window splits)
- Automatic task attribution from terminal output
- Read/write of stdin on agents that don't support it
- Persisting terminal output across dx restarts
- Scroll inside collapsed slices

---

## Open Questions

**Agent stdin routing:** Sending `y` to approve a permission request requires writing to the agent's stdin. This only works if dx spawned the agent under a PTY it controls. For agents started outside dx (e.g., a Claude Code session already running), dx can only write to the task thread — it cannot inject keystrokes. The spec should treat native PTY control and message-only routing as distinct modes.

**Slice ordering:** The sort order (permission > review > active > idle > stale) assumes the feed is short enough that important slices stay visible. With 10+ agents, the feed will scroll and attention items can still be off-screen. A future slice considers a fixed attention zone at the top plus a scrollable lower section.

**Quick-edit in v3:** The line-by-line editor in v2 quick_edit mode is clunky. In v3 it should either become a drawer view with a real `$EDITOR` handoff, or be dropped in favor of just opening the file in an embedded terminal running the user's editor.
