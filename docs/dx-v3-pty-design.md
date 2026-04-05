# Design: dx v3 PTY Runtime Model

## Overview

This document defines the PTY-backed embedded terminal runtime for dx v3. This model enables dx to act as a prompt-first hypervisor managing multiple live coding sessions (Claude, Codex, Gemini, or raw shell) as horizontally stacked, collapsible slices.

## 1. Core Architecture

The runtime is divided into three primary layers:

1.  **`PTYManager`**: The low-level engine handling PTY allocation, process lifecycle, and async I/O.
2.  **`TerminalRegistry`**: The stateful container for active sessions and their metadata.
3.  **`RoutingController`**: The logic layer that maps main prompt input to specific PTY sessions or Lex service calls.

## 2. Data Model

### `TerminalSession` (Metadata)

Each embedded terminal is represented by a `TerminalSession` object:

| Field | Type | Description |
| :--- | :--- | :--- |
| `id` | `int` | Stable identifier within the current dx session. |
| `title` | `str` | Human-readable label (e.g., "claude-dev", "local-shell"). |
| `kind` | `enum` | One of: `claude`, `codex`, `gemini`, `shell`. |
| `pid` | `int` | PID of the spawned process. |
| `status` | `enum` | `starting`, `running`, `stalled`, `exited`. |
| `cwd` | `Path` | Current working directory of the session. |
| `active_task_id` | `int?` | Optional link to an active Lex task. |
| `agent_id` | `int?` | Optional link to an active Lex agent. |
| `unread_count` | `int` | Number of output lines received since last focus. |
| `attention_flag` | `bool` | True if the runtime detects a prompt/input-required state. |
| `display_state` | `enum` | `collapsed` (summary), `expanded` (focused). |

### `OutputBuffer`

Each session maintains an incremental output buffer:
- **Full Scrollback**: Limited to 10,000 lines for expanded view.
- **Summary View**: A sliding window of the last 3-5 lines for the collapsed slice view.
- **VT100 State**: A virtual terminal state (e.g., cursor position, colors) to handle rendering escape codes.

## 3. PTY Runtime Mechanics

### Spawning Primitives

- **Master/Slave Allocation**: Use `pty.openpty()` to create a pseudo-terminal pair.
- **Subprocess Execution**: `subprocess.Popen` with `stdin`, `stdout`, and `stderr` redirected to the slave PTY.
- **Environment**: Inject `TERM=xterm-256color` and Lex-specific environment variables (`LEX_ROOT`, `LEX_SESSION_ID`).

### I/O Loop

- Use a non-blocking `Selector` or `asyncio` loop to monitor master PTYs for readable data.
- **Read Logic**:
    1.  Chunked read from master PTY.
    2.  Append to `OutputBuffer`.
    3.  Scan for "Intervention Demand" patterns (e.g., `(y/n)`, `> `, `input:`).
    4.  Update `unread_count` and `attention_flag`.
    5.  Trigger TUI re-render notification.
- **Write Logic**:
    1.  `RoutingController` receives user string.
    2.  Append `\n` if not present.
    3.  `os.write(master_fd, data)`.

## 4. Collapsible Slice Lifecycle

Terminals render as horizontally stacked slices.

### State Transitions

1.  **Creation**: `/spawn <kind>` -> New session in `expanded` state.
2.  **Auto-Collapse**: After 30 seconds of inactivity or user focus switch -> Transition to `collapsed`.
3.  **Unread Activity**: New output arrives -> Update `unread_count` (render indicator in collapsed slice).
4.  **Attention Required**: Pattern matching detects prompt -> Set `attention_flag = True` (render prominent indicator).
5.  **User Intervention**:
    - User `/focus <id>` or clicks slice -> Transition to `expanded`.
    - User types message in main prompt -> `RoutingController` sends to focused terminal.
    - Post-write -> Reset `unread_count` and `attention_flag`.
6.  **Closure**: `/close <id>` or process exit -> Remove from registry and layout.

### Post-Intervention Auto-Collapse

To support horizontally stacked slices without cluttering the screen, dx v3 implements an **Auto-Collapse Policy**:

- **Focus-Out**: When the user focuses on a different terminal slice, the previously focused slice transitions to `collapsed`.
- **Successful Intervention**: If the user sends a message to an `attention_flag=True` terminal and the terminal subsequently produces output that *clears* the attention pattern (e.g., the CLI process resumes work and starts scrolling), the slice should automatically transition back to `collapsed` after a 2-second grace period.
- **Manual Toggle**: User can always `/collapse <id>` to force compact mode.

## 5. Routing Boundary

The `RoutingController` enforces the following rules:

- **Command Route**: If input starts with `/`, parse as a dx command (e.g., `/spawn`, `/tasks`).
- **Directed Message Route**: If input starts with `@<id> <msg>`, route `<msg>` to terminal `<id>`.
- **Default Route**: Route freeform text to the `selected terminal`.
- **Broadcast Route**: If `/broadcast <msg>`, loop through all active `running` terminals and write `<msg>`.

## 6. Constraints & Risks

- **Escaping Curses/TUI**: Embedded terminals often use complex escape sequences. dx must either use a robust virtual terminal emulator (like `pyte`) or fallback to a "clean" line-mode for some runtimes.
- **Interactive Prompts**: Runtimes like Claude CLI might use raw mode or custom input handlers. dx PTYs must correctly signal terminal size and handle non-canonical input.
- **Resource Leakage**: Orphaned PTYs and hung processes. `PTYManager` must implement a strict cleanup on `SIGTERM` or TUI exit.
- **Performance**: High-volume output from multiple terminals can lag the TUI. Output processing must be offloaded from the main render thread.

## 7. Next Steps

1.  Prototype `pty_runtime.py` with a simple ping-pong echo test.
2.  Integrate `pyte` or similar for escape code handling.
3.  Draft `shell.py` layout with horizontally stacked slices.
