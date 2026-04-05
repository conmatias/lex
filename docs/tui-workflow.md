# dx Workflow

`dx` is the standalone live intervention layer for agent-driven development.

## Launching dx

```bash
dx
```

Or with an explicit workspace root:

```bash
dx --root /path/to/repo
```

If the installed script is not in PATH yet, the module entrypoint works in any dev checkout:

```bash
python3 -m lex.dx
```

`dx` requires an interactive terminal. It exits immediately if stdin or stdout is not a TTY.

`lx` is the headless CLI surface for scripting, automation, and agent commands. It is a separate tool and does not launch `dx`.

## Layout

`dx` shows three panels side by side:

| Panel | Content |
|---|---|
| Agent Roster | Active agents, roster state, task title, file counts |
| Agent Files | Files claimed or recently changed by the selected agent |
| Workspace | Tabbed diff or file view for the currently open file |

An action strip above the status bar shows context-sensitive actions for the focused panel.

## Navigation

| Key | Action |
|---|---|
| `tab` | Switch focus between roster → files → workspace |
| `j` / ↓ | Move selection down |
| `k` / ↑ | Move selection up |
| `enter` / → | Open selected file in a workspace tab |
| `r` | Refresh data from the database |
| `q` / ESC | Quit |

## Workspace Actions

| Key | Action |
|---|---|
| `d` | Diff mode — show `git diff <base_ref>...HEAD` for the open file |
| `f` | File mode — show current file contents |
| `x` | Close the selected tab |

## Intervention Actions

| Key | Context | Action |
|---|---|---|
| `m` | Roster or tab focused | Prompt for a note and send it to the task thread |
| `a` | Tab focused | Prompt for an annotation and record a `dx.annotation` event |
| `g` | File or tab focused | Flag the current file for review (`dx.flag` event) |

## Roster States

| State | Meaning |
|---|---|
| `active` | Agent has a task in progress with recent activity |
| `waiting` | Agent has a task but no recent file or message activity |
| `blocked` | Task status is `blocked` |
| `stale` | Heartbeat older than 15 minutes, or no active session |
| `idle` | Agent has a session but no current task |

## File States

| Marker | State | Meaning |
|---|---|---|
| `*` | `changed_unreviewed` | File appears in the agent's `git_changed_files_json` |
| ` ` | `claimed_only` | File is claimed by the agent's task but not yet changed |
| `!` | `conflicted` | File is claimed by more than one active task |

## Invariants

`dx` does not directly mutate task ownership, session lifecycle, or lease state.
All writes go through Lex verbs: messages table, events table, and the filesystem.
