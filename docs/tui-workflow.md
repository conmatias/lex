# lex TUI Workflow

The preferred entrypoint for day-to-day use is:

```bash
lx
```

When a real terminal is available, this launches the lex TUI. Otherwise lex falls back to the simpler menu shell.

For the standalone dx surface, use:

```bash
dx
```

If the standalone script is not installed in the current dev checkout yet, use:

```bash
python3 -m lex.dx
```

## Core Navigation

- `tab`: switch focus between the task pane and the session pane
- `j` / `k`: move the current selection within the focused pane
- `r`: refresh the dashboard
- `q`: quit

## Task Actions

- `n`: create a task
- `c`: claim the selected task
- `m`: send a message on the selected task
- `t`: change the selected task status
- `d`: delegate a child task from the selected task
- `f`: hand off the selected task to another agent

The selected-task pane shows:

- task title and status
- owner and delegation mode
- recent child tasks
- recent task-thread messages

Execution note:
- The current repo now includes a supervised worker and dispatch control plane in the CLI.
- Worker launch approval, runtime lifecycle, and packet delivery still run through `lex worker ...` and `lex dispatch ...` commands while TUI integration catches up.
- Session bootstrap acknowledgement, required-action completion, and watch acknowledgements currently run through `lex session ...` and `lex watch ...` commands.

## Session Actions

- `a`: register an agent
- `s`: start a session
- `h`: send a heartbeat for the selected session (selected in the session pane)
- `x`: end the selected session (selected in the session pane)

## Visual Warnings

- `!` in the task list indicates a lease that is close to expiring
- `!` in the session list indicates a stale heartbeat or a compliance problem such as pending bootstrap, incomplete first actions, unacked subscriptions, or recorded role drift

## Usage Notes

- The TUI operates on the same `.lex/lex.db` state as the CLI commands.
- If another agent is active in the same repository, use lex task ownership and session state before making overlapping changes.
- For complex root-agent-file integration, use the assisted merge workflow outside the TUI:

```bash
python3 -m lex.cli merge diff
python3 -m lex.cli merge apply
```
