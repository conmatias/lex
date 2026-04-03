# dx Product Brief

## Overview

`dx` is the interactive TUI companion to `lx`.

`lx` remains the command surface for agent-driven development. It is where work is initiated, scripted, composed, and executed. `dx` is the live intervention layer. It is where the human drills into active agent work, inspects what is happening at the file level, makes targeted edits, and keeps the system aligned while work is in motion.

This distinction matters. `dx` is not a dashboard, and it should not be designed like one. A dashboard implies passive observation. Agent-driven development requires active supervision, interruption, redirection, and tactical file-level intervention. `dx` exists to make that mode of work natural.

## Core Product Thesis

`dx` is a file-centric supervision environment for agent-driven development.

It is not trying to replace a full traditional editor, and it is not a status console for watching agents run. It is the surface where the human in the loop can:

* browse the project quickly
* tab open files for targeted edits
* inspect exactly what an agent is touching
* understand which agents are active and what they are doing
* jump directly into the files, changes, and tasks that matter
* redirect or correct work without losing context

The product is built around the idea that human oversight still matters, especially when multiple agents are operating in parallel. The human needs a tactical environment that makes it easy to drill into work, intervene surgically, and return control back to the agents.

## Product Boundary

The distinction between `lx` and `dx` should stay clean.

### `lx`

`lx` is the headless command and automation surface.

It is responsible for:

* running commands
* scripting workflows
* launching tasks
* delegating work to agents
* handling composable CLI operations
* fitting into shells, scripts, and automation chains

### `dx`

`dx` is the interactive supervision surface.

It is responsible for:

* file browsing
* tab-based file inspection and quick edits
* showing active agents and current work
* surfacing what files agents are touching
* drilling into live work in progress
* enabling human-in-the-loop intervention
* connecting tasks, agents, files, and changes in one environment

The most important design constraint is that `dx` should not become a second implementation of the system’s core logic. The core orchestration, task execution, and agent behaviors should live in shared services underneath both interfaces. `dx` should be a first-class client over that shared core, not a forked world with its own duplicated logic.

## What `dx` Is Not

To keep the product focused, it helps to define what `dx` is not.

`dx` is not:

* a metrics dashboard
* a passive monitoring console
* a generic terminal file manager with branding
* a clone of a traditional IDE
* an attempt to replace a power editor like Neovim or VS Code

The moment the product starts drifting toward generic dashboard thinking, it loses its reason to exist. The value of `dx` is not that it has panes. The value is that it gives a human operator command over live agent work in a file-centric environment.

## User Model

The user is a developer operating in an agent-driven workflow.

That user is not manually authoring every line of code from scratch, but they are also not absent. They are piloting. They are assigning work, watching for drift, dropping into files when precision matters, and making judgment calls on changes in flight.

That means the user’s core loop is something like this:

1. launch or select active work
2. see which agents are currently operating
3. understand what each agent is trying to do
4. jump to the files those agents are touching
5. inspect, edit, or redirect as needed
6. review resulting changes
7. allow work to continue or reroute it

This is not a passive observer role. It is a human pilot role.

## Design Philosophy

The design philosophy of `dx` should be based on four principles.

### 1. File-centric, but not file-only

Files are the main drill-down mechanism. The user needs to browse the project and open files in tabs for quick edits. But the product is not just a file browser. Files are the place where agent activity becomes concrete.

A file in `dx` should not feel like an isolated artifact. It should feel like a live surface tied to active work.

### 2. Agent-aware everywhere

Agent context should be attached to files, tabs, changes, and tasks wherever possible.

If a file is open, the user should be able to understand:

* which agent is touching it
* under which task or instruction thread
* what the agent is trying to accomplish
* what has changed recently
* whether there are proposed edits, applied edits, or conflicts

This is what separates `dx` from a normal editor or file browser.

### 3. Fast intervention over deep editing

The editing layer should prioritize quick, tactical changes. The goal is not to build a full-featured editor war project. The goal is to make human intervention easy when an agent is in motion.

That means editing should feel responsive and competent, but the product should resist getting dragged into every feature expected from a traditional editor ecosystem.

### 4. Context preservation

The user should not have to mentally reconstruct the chain of work every time they jump into a file. `dx` should preserve context across task intent, agent activity, file changes, and active sessions.

The human should be able to move between:

* the task being attempted
* the agent currently working it
* the files being touched
* the diffs being produced
* the decisions waiting to be made

without falling out of the flow.

## Primary Objects in the System

A key insight is that the primary object in `dx` is not just the file and not just the agent. It is the relationship between the two.

The interface should help answer these questions immediately:

* what agents are active right now
* what is each one trying to do
* what files are they touching
* what changed
* where should the human jump in

From that perspective, the primary nouns in `dx` are likely:

* Files
* Tabs
* Agents
* Tasks
* Changes

These objects should be tightly linked in the interface.

## Core Interface Model

The interface should be driven by drill-down and intervention, not by passive status display.

A likely top-level model looks like this:

### File Tree

The file tree is the spatial map of the project.

But unlike a generic file tree, it should be aware of live agent activity. It should not only show structure. It should help communicate where work is happening.

Examples of useful behaviors:

* files or folders touched by active agents are visibly marked
* changed files are distinguishable from untouched files
* the user can filter to active or recently modified files
* files touched by a selected task or agent can be highlighted

### Tabs

Tabs are the intervention workspace.

The user should be able to rapidly open, inspect, and edit files without losing track of why they are open.

Tabs should ideally carry contextual metadata such as:

* associated task
* associated agent
* change status
* recent activity
* whether the file is part of a proposed patch or an applied change

### Active Agents View

This is not a dashboard panel in the passive sense. It is the live work roster.

It should communicate:

* which agents are active
* what each is currently assigned to
* what files they are touching
* what their current state is
* whether they are blocked, waiting, running, or producing changes

The purpose is not to admire status. The purpose is to make it obvious where intervention might be needed.

### Task or Instruction Context

The human needs to stay anchored to intent.

A selected agent or selected file should expose the relevant task or instruction thread so the human can see what the work is supposed to accomplish. This keeps edits and decisions grounded.

### Changes and Diffs

Changes are the trust mechanism.

The user should be able to see what is changing as a result of agent activity and human intervention. Diffs should be easy to inspect and tied back to agents and tasks where possible.

## Core User Actions

The product should be optimized around a small set of high-value actions.

At minimum, `dx` should make these actions natural:

* browse files quickly
* open files in tabs
* jump from an agent to the files it is touching
* jump from a file to the agent or task affecting it
* make a quick edit
* review changes
* redirect agent work
* stop or kill an active agent
* resume or continue work after intervention
* compare competing outputs or work streams

The system should reduce friction in the transitions between human action and agent action.

## Product Promise

The product promise of `dx` should be simple and specific:

`dx` is the place where a developer drills into live agent work, makes targeted interventions, and stays in command of the codebase while agents operate.`

That framing is stronger than calling it a dashboard and more precise than calling it a generic IDE.

## Why This Matters

Without something like `dx`, the user gets scattered across too many surfaces:

* terminal commands
* editor windows
* diffs
* logs
* task notes
* agent outputs
* branch state

That fragmentation creates context loss. The human spends too much time reconstructing what is happening and not enough time steering it.

`dx` solves that by giving the human one coherent place to:

* locate live work
* understand what is happening
* step into the exact file where intervention is needed
* act quickly
* return control back to the system

That is the real value.

## MVP Direction

The MVP should stay disciplined and avoid pretending to be a complete editor or a full orchestration platform on day one.

A credible first version of `dx` should likely include:

* project file browser
* tabbed file opening
* lightweight quick-edit capability
* active agent list
* mapping from agents to touched files
* current task visibility
* change and diff inspection
* human intervention actions such as redirect, stop, or resume

This is enough to prove the core interaction model.

What should be avoided in the MVP:

* overbuilt metrics and analytics views
* generalized dashboard abstractions
* deep editor parity features unrelated to agent supervision
* duplicated orchestration logic living only in `dx`

## Long-Term Direction

Over time, `dx` can become the default interactive environment for agent-driven development, but only if it stays aligned with its true purpose.

The long-term vision is not “terminal IDE with AI sprinkled in.”

The long-term vision is:

* an agent-aware file browser
* a tabbed intervention surface
* a live map of active development work
* a control point for human judgment in an agent-run system

That is a meaningful category position.

## Suggested Internal Framing

For internal teams, the cleanest way to talk about the relationship is:

* `lx` issues work
* `dx` supervises and intervenes in work

Or more explicitly:

* `lx` is the command surface
* `dx` is the live intervention layer

That framing keeps the two tools distinct, complementary, and strategically coherent.

## Expanded Product Vision

A deeper framing of `dx` is that it should be designed for a future where humans are no longer the primary writers of code.

In that world, agents produce most implementation work. The human does not disappear, but their role changes. They become a supervisor, editor of intent, arbiter of priority, and final authority when the system needs intervention.

That future changes what an IDE should be.

A traditional IDE assumes the human is directly authoring code and occasionally using tools to accelerate that work. `dx` should assume the opposite. It should assume that agents are the primary code producers and that the human needs a first-class environment for steering those agents.

This means `dx` is not simply a better terminal UI. It is an IDE for a post-manual-coding workflow.

The human still needs to inspect files, make occasional direct edits, and understand the state of the project. But the dominant interaction is no longer just editing source files. The dominant interaction becomes supervising agent behavior, shaping intent, correcting direction, and stepping in when automated execution needs human judgment.

## `lx` as Transport Layer, `dx` as Human Hypervisor

If `lx` is the transportation layer between agents, tasks, and execution surfaces, then `dx` becomes the human-facing hypervisor over that system.

That framing is important.

`lx` moves work through the system. It launches operations, routes instructions, coordinates execution, and provides the programmable surface for chaining agent-driven workflows together.

`dx` sits above that activity and gives the human operator a way to:

* see what agents are doing
* read the messages moving through the system
* edit or rewrite those messages
* update priority or scope
* redirect work mid-flight
* intervene when the automated hypervisor needs to be overridden

This makes `dx` more than a file browser with agent awareness. It becomes the place where the human can step into the control loop itself.

In practical terms, that means `dx` should likely support direct inspection and manipulation of the communication layer that agents rely on. If agents are receiving messages, prompts, instructions, or structured tasks, the human should be able to inspect those objects, edit them, and reissue them without leaving the environment.

That is a much more powerful concept than a static task list or passive status view.

## Messaging as a First-Class Surface

If agents are driven by messages, prompts, tasks, and structured instructions, then those things should be first-class objects inside `dx`.

The human should be able to move fluidly between:

* the file an agent is editing
* the task it is trying to satisfy
* the message or prompt that caused the work
* the broader priority context that explains why the work matters

This matters because in an agent-native development model, many problems are not caused by bad coding. They are caused by bad instructions, stale context, conflicting priorities, or drift in the communication chain.

A strong `dx` should allow the human to correct the source of the behavior, not just patch the resulting code.

That means useful future capabilities might include:

* reading agent messages and task payloads
* editing messages before or during execution
* reprioritizing queued work
* redirecting or reassigning work across agents
* adjusting the level of urgency or confidence required
* stepping in manually when the system needs explicit human override

In that sense, the code view is only one layer of supervision. The message and instruction layer is equally important.

## Scaffolding as Intent Compression

Another major role for `dx` is fast scaffolding.

But scaffolding in this model is not just code generation in the old sense. It is intent compression.

The idea is to let a human create a structured starting point for an application or subsystem using concise pseudocode, templates, and prebuilt prompt patterns. Instead of writing long natural-language prompts every time, the user can instantiate a known pattern that already encodes the work to be done.

This offers two strategic benefits.

First, it accelerates creation. If a developer wants to build a common structure such as a controller, service layer, view wrapper, stateful component pattern, or other repeatable architecture unit, `dx` can scaffold that quickly.

Second, it reduces token waste. Rather than constantly sending verbose prompts that restate the same conventions and instructions, the system can rely on templates with embedded semantics. The human selects the pattern, fills in the specifics, and the agents read the scaffolded prompt structures as part of the implementation workflow.

This turns scaffolding into a way of encoding reusable development intent.

## Template-Driven Development

A useful long-term capability for `dx` is a template model for common application structures.

These templates are not only code snippets. They are hybrid artifacts that may include:

* boilerplate code structure
* project conventions
* embedded prompts or instructions
* placeholders for app-specific intent
* hooks for agent handoff
* structured expectations about what should be filled in

This allows the user to build out application skeletons quickly while still keeping agents tightly guided.

For example, a template might define:

* the fixed boilerplate for a view controller
* the expected data flow boundaries
* a prompt telling an agent what logic belongs in each placeholder
* constraints about style, state handling, or API interaction
* markers that let downstream agents know where implementation is needed

In this model, scaffolding is no longer just “generate starter files.” It becomes “instantiate a partially specified system that agents can complete.”

That is a much more agent-native concept.

## Why This Lowers Token Pressure

A major advantage of template-driven scaffolding is efficiency.

Long, repetitive prompts are expensive, noisy, and error-prone. They also create inconsistency when the same architectural intention is restated slightly differently across tasks.

By moving common instructions into templates, `dx` can help compress repeated intent into reusable structures. The human only needs to supply what is unique to the current application or component.

The result is:

* fewer redundant tokens
* more consistent guidance across implementations
* less prompt drift
* faster setup of common patterns
* clearer handoff to agents

This is especially valuable if `lx` and `dx` are meant to coordinate many small agent actions over time.

## Evolving the Definition of an IDE

Taken together, these ideas suggest that `dx` should not be framed as a traditional IDE with agent features bolted on.

It should be framed as an agent-native IDE where the primary development materials are:

* files
  n- changes
* tasks
* messages
* templates
* priorities
* interventions

Code is still present, but it is no longer the only or even always the main thing being edited.

The human is editing the system’s intent and behavior as much as they are editing source code.

That is a real shift in what the environment is for.

## Implications for Product Design

This expanded vision has several design implications.

First, `dx` should support drill-down into both file-level activity and message-level activity.

Second, agent communication objects should be inspectable and editable.

Third, priority and task routing should likely become native control surfaces rather than external metadata.

Fourth, scaffolding should be treated as a strategic feature, not just a convenience utility.

Fifth, templates should be able to carry both code structure and instruction structure.

The strongest version of `dx` is one where the human can move between these layers without friction:

* architecture intent
* scaffolded template
* agent instruction
* live execution
* touched files
* resulting changes
* final intervention or approval

That is a compelling product direction because it treats agents as the default builders and the human as the default supervisor.

## Closing Summary

`dx` should be built as a file-centric supervision environment for human-in-the-loop, agent-driven development, but that should be understood as only the beginning.

At its fullest, `dx` is an IDE for a future where humans do not spend most of their time writing raw code. Agents do that work. The human instead supervises execution, edits the intent layer, controls messages and priorities, drills into files when necessary, and overrides the system when judgment is required.

Within that model, `lx` serves as the transport and execution layer, while `dx` becomes the live human hypervisor.

That hypervisor role is what makes the product interesting. It is where the user reads what is happening, edits what should happen, scaffolds what could happen next, and keeps the whole system aligned.

That is the product.
