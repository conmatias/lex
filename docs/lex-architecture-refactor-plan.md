# Lex Architecture Refactor Plan

## Summary

Lex needs a structural cleanup before deeper daemon work and any serious Rust
evaluation.

The immediate issue is not performance. The immediate issue is boundary
clarity.

Right now, `cli.py` is carrying too many responsibilities at once:

- argument parsing
- interactive shell behavior
- command handlers
- validation
- orchestration
- database setup
- rendering dispatch
- runtime/process control

The TUI also reaches into CLI command functions directly, which means the CLI
layer is effectively acting as an application service layer. That is the wrong
shape for continued product development, and it is the wrong shape for future
daemon or Rust extraction.

## Architectural Target

Lex should move toward four explicit layers.

### Entrypoints

Entrypoints should be thin surfaces only.

Examples:

- CLI parser
- TUI startup
- future daemon entrypoint

Responsibilities:

- parse input
- dispatch to the correct application service
- format output for the surface

### Application Services

Application services should own operational workflows.

Examples:

- task service
- session service
- worker service
- dispatch service
- runtime service

Responsibilities:

- execute operations
- coordinate repositories and policy
- return structured results

### Domain And Policy

Policy is part of the product and must not stay trapped in CLI handlers or raw
database helpers.

Examples:

- ownership rules
- lease rules
- role contract enforcement
- delegation rules
- conflict policy
- bootstrap requirements

### Adapters

Adapters should talk to external systems.

Examples:

- SQLite
- Git
- subprocess/process control
- PTY and terminal I/O
- rich/textual/curses rendering

This is the correct preparation for a later Rust boundary. Rust should replace
an adapter or a well-defined service boundary, not an arbitrary section of a
monolithic file.

## Phase A: De-monolith The CLI

The first move should not be a grand abstraction framework.

It should be a practical split of `cli.py` by command family.

Suggested structure:

- `lex/commands/agent.py`
- `lex/commands/session.py`
- `lex/commands/task.py`
- `lex/commands/msg.py`
- `lex/commands/watch.py`
- `lex/commands/worker.py`
- `lex/commands/dispatch.py`
- `lex/commands/install.py`
- `lex/commands/merge.py`
- `lex/commands/event.py`
- `lex/commands/prompt.py`

After this split, `cli.py` should be responsible only for:

- building the parser
- attaching subcommands
- dispatching to handlers
- launching the TUI or interactive fallback

This gives an immediate structural win without forcing behavior changes.

## Phase B: Introduce Service Modules

After the CLI split, move business operations out of command handlers and into
service modules.

Examples:

- session bootstrap/start/end/heartbeat -> `session_service`
- task create/claim/delegate/handoff/status/priority -> `task_service`
- worker request/approve/start/stop/cleanup -> `worker_service`
- dispatch create/approve/send/ack/complete -> `dispatch_service`

Target shape:

- command handlers parse args and print output
- services perform operations and return typed results
- adapters talk to SQLite, Git, subprocesses, PTYs, and files

This is also the step that lets the TUI stop calling CLI functions.

## Phase C: Decouple TUI From CLI

The TUI should not import CLI command handlers.

Instead:

- TUI calls service functions
- CLI calls the same service functions
- both receive structured results
- each surface renders those results appropriately

This is the point where CLI and TUI become peer clients instead of one
piggybacking on the other.

## Phase D: Introduce Typed Result Objects

Major operations should return stable shapes.

Examples:

- `TaskClaimResult`
- `TaskDelegationResult`
- `SessionStartResult`
- `WorkerRuntimeStartResult`
- `DispatchDeliveryResult`

These result objects matter for two reasons:

- they improve Python structure now
- they create stable contracts for future daemon or Rust boundaries

Rust migration goes well when the contracts already exist. It goes badly when
the contract is “whatever this function printed and whatever side effects it
happened to trigger.”

## Phase E: Add Repository Boundaries

Lex does not need a full ORM, but it does need repository seams around SQLite.

Suggested repositories:

- `AgentRepository`
- `TaskRepository`
- `SessionRepository`
- `WorkerRepository`
- `DispatchRepository`
- `EventRepository`

This does three things:

- reduces SQL leakage into business logic
- shrinks duplication
- creates a clean seam for later IPC or Rust-backed replacement

## Phase F: Separate Policy From Persistence

Lex’s value is heavily concentrated in policy.

That policy should be explicit and portable.

Suggested modules:

- `policy/roles.py`
- `policy/leases.py`
- `policy/delegation.py`
- `policy/conflicts.py`
- `policy/bootstrap.py`

This keeps the important product logic independent from storage and UI.

## Phase G: Isolate The Runtime Subsystem

The strongest future Rust candidate is the runtime/process-control path.

This subsystem already contains:

- worker definitions and runtimes in the DB
- stale runtime cleanup
- PID/process liveness checks
- supervisor launch behavior
- dispatch packet delivery into worker inboxes

It should be isolated into its own subsystem, for example:

- `runtime/service.py`
- `runtime/repository.py`
- `runtime/process_manager.py`
- `runtime/inbox.py`

Later, this subsystem could become:

- a Rust daemon
- a Rust sidecar
- a Python service backed by a Rust helper

But only after the contract is clean.

## Internal API Boundary

One internal API boundary should be treated as a service surface immediately,
even if it stays in-process for now.

Best candidate:

- runtime lifecycle
- dispatch delivery

Examples:

- `request_runtime_start`
- `approve_runtime`
- `launch_runtime`
- `deliver_packet`
- `ack_packet`
- `complete_packet`

Today these can be Python calls.
Later they can become daemon RPC or IPC.
Later still they can become Rust-backed.

## Characterization Tests

Before heavy refactors, behavior must be locked down.

High-priority characterization coverage:

- task claim semantics
- lease renewal and release
- delegation and handoff
- session bootstrap enforcement
- worker approval/start/cleanup
- dispatch approval/send/ack/complete
- path conflict warnings vs strict blocking

These tests are what keep architectural cleanup from causing silent behavior
drift.

## Rust Boundary Guidance

The most likely first Rust target should be decided now, but not implemented
yet.

Recommended order:

- first Rust candidate: runtime supervisor / process manager
- second Rust candidate: file/path/conflict watcher
- not a first candidate: task/session/agent policy layer
- definitely not a first candidate: all of CLI or TUI

Why:

- process supervision and PTY/runtime durability are where Rust has the
  strongest payoff
- business workflow rules are still evolving and are easier to iterate in
  Python
- UI/parser rewrites are expensive and provide weaker payoff than isolating the
  runtime core

## Execution Sequence

### Phase A

- split `cli.py` into command modules
- keep behavior unchanged
- add characterization tests around current flows

### Phase B

- create service layer
- move business operations out of command handlers
- return structured results
- have CLI call services

### Phase C

- decouple TUI from CLI
- have TUI and CLI call services directly

### Phase D

- add repository and policy layers
- isolate SQL and business rules cleanly

### Phase E

- extract runtime subsystem
- isolate worker lifecycle, supervisor, and packet delivery
- define a stable service interface

### Phase F

- introduce daemon mode
- start with runtime ownership only
- keep CLI and TUI as clients

### Phase G

- evaluate Rust only after runtime contracts stabilize
- replace the runtime/process core, not the product logic wholesale

## What Not To Do

Do not:

- rewrite `db.py`, `cli.py`, and the TUI all at once
- introduce a daemon before service boundaries exist
- port business rules to Rust before they stabilize
- use Rust as a substitute for missing architecture

## Relationship To dx 0.2

This refactor track should run in parallel with `dx 0.2`, not replace it.

`dx 0.2` still needs:

- command-first UI refinement
- daemon-owned runtime extraction for dx-managed sessions
- CLI control surface for dx-managed sessions

This architecture plan provides the broader Lex-core cleanup that will make
those product moves sustainable instead of one-off patches.
