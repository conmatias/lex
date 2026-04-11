# dx 0.2 Plan

## Summary

`dx 0.2` should turn the current `0.1` MVP from a promising shell into a
dependable command-first operating surface.

The biggest correction for `0.2` is conceptual as much as technical:

- `dx` should not feel like a dashboard with a prompt bolted onto it
- `dx` should feel like a command ribbon first, with live terminal context
  arranged around that command surface

The product should become more usable in two complementary modes:

1. interactive TUI mode
2. command-line and daemon mode

That means `dx` should no longer be treated only as a foreground screen. It
should also become a background runtime that can manage spawned coding sessions
while the user drops in and out through either the TUI or CLI commands.

## 0.2 Thesis

`dx 0.2` is the phase where dx stops being “an interesting embedded terminal
demo” and starts becoming “the command plane for active coding runtimes.”

The product thesis for `0.2` is:

- the prompt is primary
- terminals are managed resources
- the UI is an operator view over that runtime
- the CLI is another operator view over the same runtime

The TUI and CLI should not be separate products. They should be two clients
over the same dx runtime.

## What Needs To Change From 0.1

`0.1` proved that prompt-first supervision is the right direction, but the
current shell still carries too much visual and structural baggage from the
earlier dashboard-like model.

The main problems to address are:

- the screen still reads too much like “status panes plus prompt”
- the prompt is not visually or architecturally dominant enough
- the presentation of data is too passive and not opinionated enough
- `dx` still assumes the user is mostly inside the TUI
- runtime sessions are not yet exposed as first-class command-line objects

## Product Direction

### 1. Command Ribbon First

The prompt surface should become the main object in the UI.

That means:

- the bottom command area should feel like the center of gravity
- the current routing target should always be obvious
- recent commands and routed messages should be visible as part of the shell
- the user should be able to operate dx for long stretches from the prompt
  alone

The rest of the UI should support the command ribbon, not compete with it.

### 2. Runtime Slices As Context, Not Dashboard Blocks

The runtime slices should feel like active working contexts, not generic
panels.

That means:

- the focused runtime should dominate the view
- non-focused runtimes should compress aggressively
- state should be signaled with strong, minimal affordances
- the screen should optimize for steering the current active runtime, not for
  surveying everything equally

### 3. CLI And TUI As Peers

The user should not be forced into the TUI to operate dx.

`dx` should expose a CLI command set that can operate against the same runtime
state as the TUI. The TUI is the rich interactive client, but it should not be
the only client.

This means the user should be able to do things like:

- `dx daemon start`
- `dx daemon status`
- `dx spawn claude`
- `dx spawn codex`
- `dx list`
- `dx send codex-1 "fix the failing tests"`
- `dx attach codex-1`
- `dx tail claude-1`
- `dx stop gemini-1`

The same underlying sessions should remain visible inside the TUI.

## Runtime Model For 0.2

### dx Daemon

`dx` should gain a background daemon process that owns runtime sessions.

Responsibilities:

- spawn and manage PTY-backed coding runtimes
- hold the registry of active runtime sessions
- preserve session metadata while the TUI is not attached
- expose a control surface for CLI and TUI clients

The daemon should become the runtime authority for dx-managed sessions.

### TUI Client

The TUI should become a client of the daemon.

Responsibilities:

- render the current session registry and active viewport
- submit commands and routed messages
- attach to live runtime slices
- provide rich interactive supervision

### CLI Client

The CLI should also become a client of the daemon.

Responsibilities:

- create and control sessions without entering the TUI
- inspect session state from normal shell workflows
- attach, tail, list, send, stop, and resume sessions
- make dx usable from scripts and command-line loops

## Architectural Rule For 0.2

`dx` should stop coupling “screen is running” with “runtime exists.”

In `0.1`, the TUI itself is effectively the host of runtime activity.

In `0.2`, the runtime should survive independently of whether the TUI is
currently attached.

That implies:

- PTY ownership moves to a daemon/service layer
- TUI attachment is optional
- CLI commands can operate when the TUI is closed
- session state must be queryable and controllable outside curses

## UI Direction For 0.2

The UI should be deliberately less dashboard-like.

### Main UI Priorities

- stronger command ribbon treatment
- clearer current-target and current-mode display
- better prompt history and command feedback
- more expressive active-slice presentation
- more compact non-focused slices
- less equal-weight paneling

### Presentation Principles

- prefer one strong active surface over many equally loud surfaces
- make command routing visible without forcing verbose labels everywhere
- treat supporting data as contextual overlays or drawers
- reduce passive chrome and improve action clarity

## CLI Surface For 0.2

The initial daemon-aware CLI should stay small.

Recommended first commands:

- `dx daemon start`
- `dx daemon stop`
- `dx daemon status`
- `dx spawn <runtime>`
- `dx list`
- `dx send <session> <message>`
- `dx attach <session>`
- `dx tail <session>`
- `dx stop <session>`
- `dx resume <session>`

The point is not to rebuild `lx`. The point is to expose the dx runtime as a
commandable system.

## Non-Goals For 0.2

`0.2` should not try to do everything at once.

Out of scope:

- full tmux replacement
- complete editor-grade file workflows
- full Lex-wide agent/task supervision parity
- broad analytics/dashboard surfaces
- generalized plugin/runtime marketplace work

The focus should stay on making dx-managed runtime sessions excellent.

## Suggested 0.2 Execution Slices

### Slice 1: Command-First UI Refinement

- rework shell presentation around the command ribbon
- make the prompt and current target visually dominant
- reduce dashboard feel in the layout
- improve focus and slice-state readability

### Slice 2: Daemon Runtime Extraction

- extract PTY session ownership into a daemon/service layer
- define a stable session registry and control API
- allow runtime sessions to survive TUI exit

### Slice 3: CLI Control Surface

- add daemon-aware dx CLI commands
- support list/send/attach/tail/spawn/stop flows
- ensure CLI and TUI operate on the same sessions

### Slice 4: Interaction Hardening

- clean up runtime-specific input semantics
- improve attach/detach flows
- improve command feedback and error reporting
- harden terminal rendering edge cases

## 0.2 Exit Criteria

`dx 0.2` should feel like one coherent runtime system, not just one good TUI.

That means:

- the prompt is clearly the primary control surface
- the TUI no longer reads like a dashboard-first layout
- the daemon can own sessions while the TUI is detached
- the CLI can fully operate dx-managed sessions
- users can move between shell commands and TUI attachment without losing
  runtime continuity

## Framing

The right internal framing is:

- `0.1` proved the interaction model
- `0.2` should prove the operating model

That is the difference between “a good terminal app” and “a real control
plane.”
