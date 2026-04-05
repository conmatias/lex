# dx v3 PTY Runtime — Review Artifact (Task #31)

**Reviewer:** Claude
**Reviewed file:** `src/lex/dx/pty_runtime.py`
**Source:** Gemini prototype submitted as part of v3 PTY design slice
**Reference spec:** `docs/dx-v3-pty-design.md`, `docs/dx-v3-layout-spec.md`

---

## Summary Verdict

The PTY prototype is **structurally sound** and largely follows the design spec. The spawn mechanics, I/O loop, session lifecycle, and attention-flag detection are correct. A small set of issues required corrective edits before the module is usable by the rest of the v3 stack.

---

## Salvageable (kept as-is)

| Component | Assessment |
|---|---|
| `TerminalSession` dataclass | Core fields correct. PTY plumbing fields (master_fd, proc, pid) and display state fields (display_state, unread_count, attention_flag) match spec. |
| `PTYManager.spawn()` | PTY allocation with `pty.openpty()`, non-blocking via `os.set_blocking()`, `start_new_session=True`, slave_fd closed in parent — all correct. |
| `PTYManager.write()` | Lock-guarded, appends `\n`, handles OSError — correct. |
| `PTYManager._io_loop()` | Background thread with `select.select()`, non-blocking read, routes to `_handle_output` / `_handle_exit` on OSError or empty read — correct. |
| `PTYManager._handle_output()` | Lock-guarded, splitlines, unread_count gating on collapsed state, attention pattern matching — correct. |
| `PTYManager._handle_exit()` | Lock-guarded, closes master_fd safely, marks status="exited" — correct. |
| `PTYManager.close()` | Terminates process, delegates to `_handle_exit`, removes from registry — correct. |
| `PTYManager.set_display_state()` | Resets unread_count and attention_flag on expand — correct. |
| `PTYManager.stop()` | Closes all fds on shutdown — correct. |

---

## Issues Found and Fixed

### 1. Missing `active_task_id` and `agent_id` fields on `TerminalSession`

**Spec (section 2):** Both fields are required for the shell layer to correlate PTY sessions with Lex DB records.

**Fix applied:** Added `active_task_id: int | None = None` and `agent_id: int | None = None` to `TerminalSession`.

---

### 2. Scrollback limit 1000 vs. 10000

**Spec (section 2, OutputBuffer):** "Full Scrollback: Limited to 10,000 lines."
**Found:** `_handle_output()` capped at 1000 lines.

**Fix applied:** Changed limit to 10000.

---

### 3. Missing `LEX_ROOT` / `LEX_SESSION_ID` environment injection

**Spec (section 3, Spawning Primitives):** "Inject `TERM=xterm-256color` and Lex-specific environment variables (`LEX_ROOT`, `LEX_SESSION_ID`)."
**Found:** Only `TERM` was injected. Agents spawned inside dx would have no way to locate the workspace root.

**Fix applied:** `spawn()` now accepts optional `lex_root: Path | None` and `session_id: str | None` params. When provided, they are written to `env["LEX_ROOT"]` and `env["LEX_SESSION_ID"]` respectively.

---

### 4. Missing `list_sessions()` method

**Impact:** `RoutingController` (in `commands.py`) requires a `list_slices` callback that returns known slice IDs. Without `list_sessions()`, the PTYManager cannot serve as the slice registry.

**Fix applied:** Added `list_sessions() -> list[TerminalSession]` — returns a lock-guarded snapshot of all sessions.

---

### 5. Missing `resize()` method

**Impact:** Terminal programs depend on correct window size. Without SIGWINCH delivery, output will be formatted for the wrong dimensions and may corrupt the TUI.

**Fix applied:** Added `resize(session_id, rows, cols)` using `fcntl.ioctl(fd, termios.TIOCSWINSZ, ...)`. The shell layer is responsible for calling this on terminal resize events.

---

## Superseded (not in prototype, not added)

### Section 5 — `RoutingController`

The PTY design doc (section 5) describes a `RoutingController` with `@<id>` directed message syntax. This was superseded by the full command grammar designed in task #25 and implemented in `src/lex/dx/commands.py`. The `commands.py` `RoutingController` is authoritative.

**Do not** add the section 5 routing logic to `pty_runtime.py`. The PTY layer is purely a session manager. Routing is owned by the command layer.

---

## Out of Scope for This Slice (per spec non-goals)

- **VT100 / pyte integration**: Raw escape code handling. Not added — the prototype's `splitlines()` approach is acceptable for first slice.
- **Auto-collapse timer**: Belongs in the shell rendering layer, not pty_runtime. The `set_display_state()` API is the correct hook for the shell to use when implementing auto-collapse.
- **`TerminalRegistry` as a separate class**: Embedding sessions in `PTYManager` is fine for first slice. Separation can happen if the registry needs independent testability.

---

## Post-Review State

`src/lex/dx/pty_runtime.py` is ready for integration with the shell rendering layer. The five corrective edits above are applied. All existing tests pass (219/220, with the 1 remaining failure being a pre-existing test contradiction in `test_dx_commands.py` unrelated to `pty_runtime`).
