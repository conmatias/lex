# dx v3 Command Grammar And Routing Spec

This document defines the command grammar, routing semantics, and prompt-state model for the dx v3 prompt-first hypervisor shell.

It is the command-plane companion to:

- `docs/dx-v3-hypervisor-plan.md`
- `docs/dx-v3-layout-spec.md`

The layout spec defines where prompt, slices, and drawers appear.
This document defines what the prompt means, how input is parsed, and how dx resolves command intent versus routed terminal input.

## Goals

The v3 prompt model must:

- keep `dx` separate from `lx`
- treat the bottom composer as the primary control surface
- support both slash commands and freeform routed messages
- make routing state visible and predictable
- avoid re-implementing full `lx` command semantics inside `dx`
- provide enough structure for a first implementation slice without locking the product into a giant command language

## Non-Goals

The v3 command surface does not try to:

- mirror every `lx` subcommand
- become a shell scripting language
- support nested quoting or shell-style pipes in the first slice
- parse arbitrary task or git commands directly
- hide whether an action is local to `dx`, routed to a PTY, or written back through Lex

## Input Classification

Every prompt submission is classified into exactly one of three categories:

1. slash command
2. routed freeform message
3. local prompt error

Classification rule:

- If the first non-space character is `/`, parse as a slash command.
- Otherwise, treat the input as a routed freeform message.
- If parsing fails or routing is impossible, emit a local prompt error instead of guessing.

dx must never silently reinterpret a malformed slash command as a routed freeform message.

## Prompt Grammar

The first slice uses a shallow grammar with predictable tokenization.

## Tokenization Rules

- Leading and trailing whitespace are ignored.
- The first token selects the command.
- Arguments are split on spaces.
- Double quotes may group a single argument containing spaces.
- Backslash escaping is not required in the first slice beyond allowing `\"` inside quoted strings.
- Unclosed quotes are a parse error.
- Empty command input after `/` is equivalent to `/help`.

Examples:

- `/focus codex`
- `/send codex "please inspect auth/session.py"`
- `/broadcast all agents: rebase onto main`
- `/diff src/lex/db.py`

## Canonical Command Shape

Commands follow this general form:

```text
/<verb> [target] [arguments...]
```

Where:

- `<verb>` is required
- `[target]` is optional and command-specific
- remaining text is interpreted according to the command's argument model

## Command Registry Model

Each command entry should define:

- canonical verb
- aliases
- argument schema
- whether a routing target is required
- whether the command acts on slice focus, routing target, or an explicit target
- whether confirmation is required
- dispatch kind

Dispatch kinds:

- `dx_local`: affects only dx UI/runtime state
- `pty_runtime`: sends control to a dx-managed PTY pane
- `lex_write`: writes through Lex shared services
- `drawer_view`: changes context drawer state

## First-Slice Command Set

The first slice should ship a deliberately small set.

### Help And Discovery

- `/help`
- `/help <command>`
- `/commands`

### Routing And Slice Selection

- `/focus <slice-ref>`
- `/route <slice-ref>`
- `/clear`
- `/where`

### Terminal Runtime Control

- `/spawn <runtime-kind>`
- `/split [runtime-kind]`
- `/close <slice-ref>`
- `/stop <slice-ref>`
- `/resume <slice-ref>`
- `/terminals`

### Message Routing

- `/send <slice-ref> <message>`
- `/broadcast <message>`
- `/reply <message>`

### Context Drawer

- `/tasks`
- `/agents`
- `/files`
- `/diff <path>`
- `/help`

### Optional First-Slice Review Helpers

Only if the PTY/runtime lane supports them cleanly:

- `/approve [slice-ref]`
- `/deny [slice-ref]`

These are runtime actions, not general task-state commands.

## Command Semantics

### `/help`

Purpose:
- show discoverability surface

Forms:
- `/help`
- `/help <command>`

Behavior:
- Opens the context drawer in `help` view.
- Without arguments, lists all supported commands grouped by category.
- With a command argument, shows usage, aliases, examples, and whether the command acts locally, on a PTY, or through Lex.

### `/focus <slice-ref>`

Purpose:
- set the routing target

Behavior:
- Resolves `<slice-ref>` to a visible or known slice.
- Sets the routing target to that slice.
- Does not expand the slice by default.
- Does not move drawer state unless the drawer is configured to follow the routing target.

Alias:
- `/route <slice-ref>`

### `/clear`

Purpose:
- clear routing target

Behavior:
- Removes the current routing target.
- Does not change slice focus or expanded state.
- Freeform input is invalid until a new routing target is selected.

### `/where`

Purpose:
- explain current prompt state

Behavior:
- Prints a one-line system confirmation summarizing:
  - current routing target
  - current slice focus
  - currently expanded slice
  - active drawer view, if any

### `/spawn <runtime-kind>`

Purpose:
- create a new PTY-backed terminal slice

Arguments:
- `claude`
- `codex`
- `gemini`
- `shell`

Behavior:
- Delegates to the PTY runtime manager.
- Creates a new slice with a stable id.
- The new slice becomes:
  - focused
  - expanded
  - the routing target

Confirmation:
- success message with slice id and runtime kind

### `/split [runtime-kind]`

Purpose:
- create a second visible pane without forcing the user to leave the prompt flow

Behavior:
- Without arguments, splits using the current routing target's runtime kind if available.
- With an explicit runtime kind, spawns that runtime and displays it as a second visible slice.

### `/close <slice-ref>`

Purpose:
- close a dx-managed PTY slice

Behavior:
- Requires an explicit target.
- If the slice is still running, dx should require confirmation.
- If the closed slice is the routing target, dx clears or reassigns the routing target according to fallback rules.

### `/stop <slice-ref>`

Purpose:
- stop PTY execution for a runtime slice

Behavior:
- Runtime action only.
- Not a task-state mutation.
- If the slice is external or message-only, return a structured error instead of pretending dx can stop it.

### `/resume <slice-ref>`

Purpose:
- resume or reopen a runtime slice where supported

Behavior:
- Runtime-specific.
- If unsupported, return an explicit capability error.

### `/terminals`

Purpose:
- summarize active runtime slices

Behavior:
- Opens or refreshes the help/drawer surface with a terminal registry summary:
  - slice id
  - runtime kind
  - state
  - current task if known
  - unread / attention markers

### `/send <slice-ref> <message>`

Purpose:
- route a freeform message to a non-default slice

Behavior:
- Resolves target slice explicitly.
- Sends `<message>` through the routing controller to that slice.
- Does not change persistent routing target by default.

This distinction matters:
- `/focus codex` changes future freeform sends
- `/send claude ...` is a one-shot dispatch

### `/broadcast <message>`

Purpose:
- route one prompt submission to several slices

Behavior:
- Sends the message to all broadcast-eligible active slices.
- The confirmation surface must list the slices that actually received the message.
- Slices in unsupported states are skipped and included in the confirmation result.

### `/reply <message>`

Purpose:
- respond to the current attention-needed slice

Behavior:
- Resolves to the highest-priority expanded attention slice if exactly one exists.
- If zero or multiple candidates exist, returns an error asking the user to focus or send explicitly.

### `/tasks`, `/agents`, `/files`

Purpose:
- open the context drawer to a given reference view

Behavior:
- Drawer follows the routing target when that is meaningful.
- These commands are navigation commands, not write commands.

### `/diff <path>`

Purpose:
- open the drawer directly to a diff view for a path

Behavior:
- Uses the routing target's git context when available.
- If no routing target is set and the path is ambiguous, return an error asking for a target first.

### `/approve [slice-ref]` and `/deny [slice-ref]`

Purpose:
- answer an interactive PTY prompt that is already in a review/permission state

Behavior:
- If a target is omitted, act on the focused attention-needed slice.
- These commands send a structured yes/no response to PTY stdin.
- They are not generic task approvals and must not map directly to arbitrary Lex task-state transitions.

## Slice Reference Grammar

Commands that target a slice accept a `slice-ref`.

First-slice resolution order:

1. exact slice id
2. exact agent/runtime label
3. unique prefix match
4. error on ambiguity

Accepted examples:

- `3`
- `codex`
- `codex-brisk-falcon`
- `claude`

Ambiguous examples must fail:

- `/focus co` when both `codex` and `codex-review` exist

Error shape:
- `ambiguous target: co → codex-brisk-falcon, codex-steady-otter`

## Freeform Routing Semantics

Freeform input is not command parsing.
It is a message-routing operation.

Example:

```text
please rerun the failing auth tests with verbose output
```

Behavior:

- If a routing target exists, the message is dispatched to that slice.
- If no routing target exists, dx rejects the submission with a local error and guidance.
- dx must never choose a target implicitly based only on slice focus.

This is a deliberate product rule:
- slice focus is for navigation
- routing target is for prompt submission

## State Model

The command surface depends on four independent pieces of prompt state.

### 1. Slice Focus

Meaning:
- which slice the user is currently navigating in the feed

Changes via:
- `j` / `k`
- mouse or direct slice selection, if added later

Does not imply:
- prompt routing
- expansion
- drawer binding

### 2. Routing Target

Meaning:
- where freeform prompt submissions go

Changes via:
- `/focus`
- `enter` on a slice
- `/spawn`
- explicit clear operation

Must always be visible in the header.

### 3. Expanded Slice

Meaning:
- which slice currently shows full output / review context

Changes via:
- `space`
- auto-expansion on review or permission state
- explicit expand/collapse commands, if added later

Does not imply:
- routing target

### 4. Drawer Context

Meaning:
- what supporting reference surface is open

Fields:
- `drawer_open: bool`
- `drawer_view: tasks | agents | files | diff | help`
- `drawer_subject: routing-target | focused-slice | explicit-path | none`

## Routing Resolution Algorithm

On prompt submit:

1. Trim input.
2. If empty, no-op.
3. If input begins with `/`, parse as command.
4. If parse succeeds, dispatch according to command registry.
5. If parse fails, emit structured command error.
6. Otherwise treat input as freeform message.
7. If routing target is unset, emit routing error.
8. If routing target supports PTY input, send to PTY runtime.
9. If routing target is message-only, send through that slice's routing adapter.
10. Emit a confirmation entry into command history.

## Confirmation Model

Every successful submission should produce a compact confirmation line in command history.

Examples:

- `focused codex-brisk-falcon`
- `sent to claude-brisk-falcon`
- `broadcast to 3 slices (1 skipped)`
- `opened drawer: diff src/lex/db.py`
- `spawned codex slice #4`

Confirmations should be short, consistent, and attributable.

## Error Model

Errors are local to dx and do not mutate routing state unless explicitly stated.

Error classes:

- parse error
- unknown command
- missing argument
- ambiguous target
- no routing target
- unsupported capability
- runtime unavailable
- confirmation required

Examples:

- `unknown command: /foqus`
- `missing argument: /send <slice-ref> <message>`
- `no routing target; use /focus <slice-ref> or press enter on a slice`
- `slice codex is not dx-managed; stdin routing is unavailable`

## Discoverability Rules

The command surface must be learnable in-product.

First-slice rules:

- `/help` is always available
- typing `/` with an empty composer opens command suggestions
- `tab` autocompletes the current command token
- ambiguous targets show candidate completions
- command errors should suggest the nearest valid command when edit distance is small

Examples:

- `/brodcast` → `unknown command: /brodcast; did you mean /broadcast?`
- `/focus c` + `tab` → cycles through matching targets

## Boundary Rules With Lex

Slash commands must not quietly duplicate `lx`.

Allowed:

- dx-local navigation and runtime commands
- routing messages to PTY slices
- opening files/tasks/diffs in the dx drawer
- task-thread writebacks through shared Lex services when the command explicitly represents an intervention

Not allowed:

- cloning the entire `lx task ...` command tree
- silent direct DB mutation of task/session/lease ownership state
- shell-style pass-through where `/task status 5 done` becomes a hidden `lx` invocation

If dx needs richer operational control later, it should expose a narrow set of hypervisor verbs, not a second copy of the Lex CLI.

## Minimal Implementation Data Types

The first implementation should be able to work from these models:

```text
CommandParseResult
- kind: command | freeform | error
- verb: str | None
- target_ref: str | None
- args: list[str]
- message: str | None
- error: CommandError | None

RoutingState
- focused_slice_id: str | None
- routing_target_id: str | None
- expanded_slice_id: str | None
- drawer_open: bool
- drawer_view: str | None
- drawer_subject: str | None

CommandDispatchResult
- status: ok | error | confirm_required
- confirmation: str | None
- error_message: str | None
- state_patch: partial routing-state update
```

## Open Implementation Notes

- The parser should stay deterministic and shallow in v3 slice one.
- Explicit commands should win over inference.
- The routing target must remain visible at all times.
- Freeform prompt submission must feel fast, but dx should prefer a clear error over implicit retargeting.
- PTY-native control and message-only routing must be represented as separate capabilities on the slice model.
