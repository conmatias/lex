# dx v3 Plan: Prompt-First Hypervisor Shell

## Summary

`dx` should evolve from a pane-first supervision TUI into a prompt-first hypervisor shell.

The primary interaction model is:

1. one main prompt window
2. slash commands for structured control
3. multiple embedded terminal sessions inside the same TUI
4. message routing from the main prompt into one or more active terminals
5. secondary panes for files, diffs, tasks, and agent context

This is a better fit for the dx product brief than treating the action strip as the center of the product.

## Why This Is The Right Direction

The current dx v2 shell is useful, but it is still optimized around file inspection plus intervention affordances. That makes dx feel like an agent-aware inspector.

The stronger product is a human hypervisor over multiple live coding terminals.

That means:

- the prompt is primary
- embedded terminals are first-class runtime objects
- files and diffs are supporting evidence, not the top-level control surface
- slash commands are the structured control plane

This better matches the intended model:

- `lx` issues work
- `dx` supervises and intervenes in work

## Product Model

### Primary Surface

The top-level dx surface should open into a command composer similar to Claude Code:

- persistent prompt input
- history of issued commands and routed messages
- visible current target context
- slash-command discovery

The user should be able to operate dx for long stretches without touching a file browser.

### Embedded Terminals

dx should manage multiple PTY-backed subterminals inside the same TUI.

Each terminal pane represents a live coding surface such as:

- `claude`
- `codex`
- `gemini`
- raw shell

Each pane needs:

- a terminal id
- a title / runtime label
- process status
- cwd / workspace root
- active task or routing target when known
- unread output state

### Prompt Routing

The main prompt window should be able to:

- send a message to one terminal
- broadcast a message to several terminals
- target the currently focused terminal
- issue dx-native control commands

This lets the user keep one terminal window open while steering many live sessions.

## Core Interaction Model

### Main Prompt

The main prompt accepts two categories of input:

1. slash commands
2. freeform routed messages

If input starts with `/`, dx parses it as a command.

Otherwise, dx sends it to the current routing target.

### Slash Commands

Initial command grammar should be small and explicit.

Recommended first set:

- `/help`
- `/terminals`
- `/spawn <runtime>`
- `/focus <terminal-id>`
- `/send <terminal-id> <message>`
- `/broadcast <message>`
- `/split`
- `/close <terminal-id>`
- `/tasks`
- `/agents`
- `/files`
- `/diff`
- `/stop <terminal-id>`
- `/resume <terminal-id>`

Commands should be discoverable in-product and not depend on memorizing a large command language up front.

### Focus Model

There are three distinct concepts:

- `selected terminal`
- `routing target`
- `visible pane`

They may overlap, but they are not the same thing.

This matters because the user may:

- read output from one terminal
- send instructions to another
- inspect a diff from a third

dx should make those distinctions visible at all times.

## Layout Model

The v3 default layout should be prompt-first, not file-first.

Recommended default regions:

1. prompt and command history
2. terminal grid
3. context drawer

### Prompt And History

A persistent command bar plus a scrollback of recent dx actions:

- slash commands issued
- routed messages sent
- system confirmations
- warnings and failures

### Terminal Grid

A tiled or tabbed set of PTY panes.

The first implementation only needs:

- one focused pane
- one secondary visible pane
- the ability to switch or split

Do not start with a full tmux clone.

### Context Drawer

A toggleable side or bottom drawer that can show:

- current task thread
- active agent/task roster
- touched files
- diff preview
- file viewer

This preserves the useful work from dx v1/v2 without forcing it to remain the primary navigation model.

## Runtime Architecture

### PTY Session Manager

dx needs an internal runtime layer to manage embedded terminals.

Responsibilities:

- spawn subprocesses under PTYs
- read output incrementally
- write user input to the selected terminal
- track exit state
- keep per-terminal metadata

This should be a dedicated dx runtime module, not mixed directly into the screen renderer.

### Terminal Registry

dx should maintain a registry of active panes with stable ids and metadata:

- terminal id
- runtime kind
- command line
- cwd
- pid
- started_at
- state
- associated task / agent if known

### Routing Layer

The prompt must not write directly into curses widgets.

Instead:

- prompt input goes into a routing controller
- the routing controller resolves command vs message
- messages are dispatched to terminal sessions
- dx-native actions call shared Lex services or terminal-runtime APIs

## Shared-Core Boundaries

v3 should preserve the existing dx architectural rule:

- Lex DB remains the source of truth for operational state
- Git remains the source of truth for file contents and diffs
- dx remains a client over shared Lex services

New rule for v3:

- PTY session management belongs to dx runtime, but task, message, and event semantics must still use shared Lex services where appropriate

Examples:

- sending a natural-language steering message to an embedded terminal is a dx runtime action
- recording a task-thread note is a Lex write action
- changing task priority/status should continue to respect Lex boundaries

## Non-Goals For The First v3 Slice

The first v3 slice should not attempt:

- a full terminal multiplexer clone
- arbitrary window management
- editor-grade text editing inside dx
- complete parity across every coding CLI
- perfect automatic task/session attribution

The point is to prove the hypervisor model quickly.

## First Implementation Slice

The first credible v3 slice is:

1. prompt bar with slash-command parsing
2. one embedded terminal pane
3. spawn/focus/send commands
4. second pane via split view
5. simple routing to selected terminal
6. context drawer for tasks and files

This is enough to validate that the prompt-first model is superior to the current action-strip-centric interaction pattern.

## Suggested Module Split

Recommended new modules under `src/lex/dx/`:

- `app.py`
  - top-level entrypoint
- `shell.py`
  - prompt-first TUI orchestration
- `commands.py`
  - slash-command parsing and dispatch
- `pty_runtime.py`
  - PTY session manager
- `models.py`
  - terminal/session/view models
- `context.py`
  - task/file/diff drawer state

The current file/diff helpers can remain reusable, but they should stop owning the entire interaction model.

## Execution Plan

### v3.0a

Goal: prove command-first control.

Deliverables:

- prompt bar
- slash parser
- one terminal pane
- send/focus/spawn commands
- terminal registry model

### v3.0b

Goal: prove multi-terminal supervision.

Deliverables:

- split view
- multi-terminal focus switching
- broadcast/send-to-target behavior
- pane status indicators

### v3.0c

Goal: reconnect the current dx context surfaces to the new shell.

Deliverables:

- task drawer
- files drawer
- diff drawer
- task-thread note integration from prompt commands

## Immediate Task Breakdown

The next planning/execution lanes should be:

1. PTY runtime design and prototype
2. slash-command grammar and routing model
3. TUI layout redesign for prompt + terminal grid + context drawer
4. transition plan from current dx v2 shell to v3 shell without breaking the standalone `dx` command

## Decision

dx v3 should treat the prompt window as the main operating surface and embedded terminals as first-class supervised runtimes.

The existing action strip, file browser, and diff workspace remain useful, but they are supporting surfaces inside the hypervisor shell rather than the primary product identity.
