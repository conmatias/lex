# dx 0.1 MVP Checkpoint

## Summary

`dx` has reached a real `0.1` MVP stage.

It is not polished, but it is now a usable prompt-first supervision shell for
running multiple coding agents inside one terminal window.

The product is no longer just a design concept or a static TUI mock. It now
supports live PTY-backed runtime slices, prompt routing, and embedded terminal
rendering strongly enough to be used as the primary operator surface for early
agent-driven development workflows.

## What 0.1 Means

`dx 0.1` means:

- the core interaction model is real
- the prompt-first shell direction is validated
- spawned runtime slices are the primary surface
- multiple coding CLIs can be launched and supervised inside one TUI
- routed messages can be sent from the main prompt into those runtimes
- the shell is useful enough to replace juggling several terminal windows

It does not mean the product is complete, stable under every edge case, or
ready to broaden scope aggressively.

## Current Product Shape

The current dx shell is built around:

- a bottom-anchored prompt
- slash commands for control and routing
- horizontally stacked runtime slices
- a dominant active runtime pane
- collapsed non-focused panes
- PTY-backed embedded sessions
- VT screen-buffer rendering for runtime output

The main user loop is now:

1. launch `dx`
2. spawn `claude`, `codex`, `gemini`, or `shell`
3. focus the active runtime slice
4. send instructions from the dx prompt
5. watch output update live in-place
6. switch targets without leaving the TUI

## What Works In 0.1

- standalone `dx` command surface separate from `lx`
- prompt-first shell in `src/lex/dx/shell.py`
- slash-command routing model in `src/lex/dx/commands.py`
- PTY-backed runtime sessions in `src/lex/dx/pty_runtime.py`
- dominant active runtime slice layout
- live polling refresh instead of keypress-only repaint
- VT screen-buffer rendering using `pyte`
- runtime launch for `claude`, `codex`, `gemini`, and `shell`
- routed prompt submission into spawned runtimes
- working submit behavior across current runtime quirks

## What Is Still Rough

- runtime-specific behavior still needs tactical handling
- embedded terminal behavior is improved but not yet a full mature emulator
- the shell interaction model is functional before it is elegant
- lx-managed agent supervision remains secondary to dx-spawned runtimes
- prompt affordances and focus state are still understated visually
- there are still likely edge cases around terminal control flows and input

## What 0.1 Is Not

`dx 0.1` is not yet:

- a polished terminal IDE
- a complete human hypervisor over all Lex-managed work
- a fully generalized terminal multiplexer
- a finished message/task control surface
- a stable release with hardened cross-runtime behavior

## Why This Milestone Matters

This is the first point where the product direction feels materially correct.

Earlier dx slices proved pieces of the architecture, but they still felt closer
to an agent-aware inspector. `0.1` is where dx starts behaving like the thing
it is supposed to become: one terminal-native control surface for supervising
multiple active coding runtimes.

That matters because it validates the central thesis:

- `lx` issues work
- `dx` supervises and intervenes in work

## Recommended 0.2 Focus

`0.2` should be a stabilization and usability pass, not a broad product
expansion.

Recommended priorities:

- harden runtime input semantics and per-CLI behavior
- continue improving embedded terminal rendering correctness
- improve slice focus, prompt visibility, and interaction clarity
- tighten command ergonomics and routing feedback
- keep lx-managed agent integration secondary until spawned-runtime UX is solid

## Exit Criteria For 0.2

`dx 0.2` should aim to feel dependable, not just promising.

That likely means:

- spawned runtimes behave consistently across `claude`, `codex`, and `gemini`
- prompt submission semantics are reliable and unsurprising
- slice updates feel live and responsive
- active focus and routing state are visually obvious
- the dominant runtime workflow feels better than using multiple separate
  terminal windows

## Current Framing

The right internal framing at this checkpoint is:

- `dx 0.1` proves the interaction model
- `dx 0.2` should make that model dependable

That keeps the next cycle focused on turning a promising prototype into a tool
that wants to stay open all day.
