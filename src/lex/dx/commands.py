"""dx v3 command parser and routing state.

Implements the command grammar and routing model from docs/dx-v3-command-routing-spec.md.

Responsibilities:
- Classify prompt input: slash command vs freeform message vs error
- Parse slash commands into CommandParseResult
- Maintain RoutingState (focused_slice, routing_target, expanded_slice, drawer)
- Dispatch commands to the correct handler category
- Produce CommandDispatchResult with confirmation strings for history

This module does NOT own:
- PTY session management (see pty_runtime.py)
- Lex DB writes (callers import dx.app writeback helpers)
- TUI rendering (callers own curses)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass
class CommandError:
    kind: str          # parse_error | unknown_command | missing_argument |
                       # ambiguous_target | no_routing_target |
                       # unsupported_capability | runtime_unavailable |
                       # confirm_required
    message: str
    suggestion: str | None = None


@dataclass
class CommandParseResult:
    kind: str                       # "command" | "freeform" | "error"
    verb: str | None = None
    target_ref: str | None = None
    args: list[str] = field(default_factory=list)
    message: str | None = None      # freeform body or remaining message arg
    error: CommandError | None = None


@dataclass
class RoutingState:
    focused_slice_id: str | None = None
    routing_target_id: str | None = None
    expanded_slice_id: str | None = None
    drawer_open: bool = False
    drawer_view: str | None = None   # tasks | agents | files | diff | help
    drawer_subject: str | None = None


@dataclass
class CommandDispatchResult:
    status: str                     # "ok" | "error" | "confirm_required"
    confirmation: str | None = None
    error_message: str | None = None
    state_patch: dict = field(default_factory=dict)
    # state_patch keys mirror RoutingState field names for fields that changed


# ---------------------------------------------------------------------------
# Command registry
# ---------------------------------------------------------------------------

# Each entry: verb → (aliases, requires_target, dispatch_kind, help_text)
# dispatch_kind: dx_local | pty_runtime | lex_write | drawer_view
_COMMAND_REGISTRY: dict[str, dict] = {
    "help":      {"aliases": [], "requires_target": False, "kind": "drawer_view",
                  "usage": "/help [command]",
                  "help": "Show command list or usage for a specific command."},
    "commands":  {"aliases": [], "requires_target": False, "kind": "drawer_view",
                  "usage": "/commands",
                  "help": "List all supported commands."},
    "focus":     {"aliases": ["route"], "requires_target": True, "kind": "dx_local",
                  "usage": "/focus <slice-ref>",
                  "help": "Set the routing target for freeform prompt submissions."},
    "clear":     {"aliases": [], "requires_target": False, "kind": "dx_local",
                  "usage": "/clear",
                  "help": "Clear the current routing target."},
    "where":     {"aliases": [], "requires_target": False, "kind": "dx_local",
                  "usage": "/where",
                  "help": "Show current routing target, slice focus, and drawer state."},
    "spawn":     {"aliases": [], "requires_target": True, "kind": "pty_runtime",
                  "usage": "/spawn <runtime-kind>",
                  "help": "Spawn a new PTY-backed terminal slice (claude|codex|gemini|shell)."},
    "split":     {"aliases": [], "requires_target": False, "kind": "pty_runtime",
                  "usage": "/split [runtime-kind]",
                  "help": "Create a second visible pane alongside the current routing target."},
    "close":     {"aliases": [], "requires_target": True, "kind": "pty_runtime",
                  "usage": "/close <slice-ref>",
                  "help": "Close a dx-managed PTY slice."},
    "stop":      {"aliases": [], "requires_target": True, "kind": "pty_runtime",
                  "usage": "/stop <slice-ref>",
                  "help": "Stop PTY execution for a runtime slice."},
    "resume":    {"aliases": [], "requires_target": True, "kind": "pty_runtime",
                  "usage": "/resume <slice-ref>",
                  "help": "Resume or reopen a runtime slice where supported."},
    "terminals": {"aliases": [], "requires_target": False, "kind": "drawer_view",
                  "usage": "/terminals",
                  "help": "Show active runtime slice summary in the drawer."},
    "send":      {"aliases": [], "requires_target": True, "kind": "pty_runtime",
                  "usage": "/send <slice-ref> <message>",
                  "help": "Route a message to a specific slice without changing the routing target."},
    "broadcast": {"aliases": [], "requires_target": False, "kind": "pty_runtime",
                  "usage": "/broadcast <message>",
                  "help": "Route one message to all broadcast-eligible active slices."},
    "reply":     {"aliases": [], "requires_target": False, "kind": "pty_runtime",
                  "usage": "/reply <message>",
                  "help": "Respond to the current attention-needed slice."},
    "tasks":     {"aliases": [], "requires_target": False, "kind": "drawer_view",
                  "usage": "/tasks",
                  "help": "Open the context drawer to the tasks view."},
    "agents":    {"aliases": [], "requires_target": False, "kind": "drawer_view",
                  "usage": "/agents",
                  "help": "Open the context drawer to the agents view."},
    "files":     {"aliases": [], "requires_target": False, "kind": "drawer_view",
                  "usage": "/files",
                  "help": "Open the context drawer to the files view."},
    "diff":      {"aliases": [], "requires_target": False, "kind": "drawer_view",
                  "usage": "/diff <path>",
                  "help": "Open the context drawer to a diff view for a specific path."},
    "approve":   {"aliases": [], "requires_target": False, "kind": "pty_runtime",
                  "usage": "/approve [slice-ref]",
                  "help": "Approve the focused attention-needed slice's pending prompt."},
    "deny":      {"aliases": [], "requires_target": False, "kind": "pty_runtime",
                  "usage": "/deny [slice-ref]",
                  "help": "Deny the focused attention-needed slice's pending prompt."},
}

_VALID_SPAWN_KINDS = {"claude", "codex", "gemini", "shell"}
_VALID_DRAWER_VIEWS = {"tasks", "agents", "files", "diff", "help", "terminals", "commands"}


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse(raw: str) -> CommandParseResult:
    """Classify and parse a prompt submission.

    Returns a CommandParseResult with kind="command", "freeform", or "error".
    dx must never silently reinterpret a malformed slash command as freeform.
    """
    text = raw.strip()
    if not text:
        return CommandParseResult(kind="freeform", message="")

    if not text.startswith("/"):
        return CommandParseResult(kind="freeform", message=text)

    # Slash command path
    body = text[1:]
    if not body.strip():
        # bare "/" → treat as /help
        return CommandParseResult(kind="command", verb="help")

    tokens = _tokenize(body)
    if isinstance(tokens, CommandError):
        return CommandParseResult(kind="error", error=tokens)

    verb_token = tokens[0].lower()
    rest = tokens[1:]

    # Resolve canonical verb (handles direct registry entries and aliases)
    canonical: str | None = None
    if verb_token in _COMMAND_REGISTRY:
        canonical = verb_token
    else:
        for k, v in _COMMAND_REGISTRY.items():
            if verb_token in v.get("aliases", []):
                canonical = k
                break

    if canonical is None:
        suggestion = _nearest_verb(verb_token)
        hint = f"; did you mean /{suggestion}?" if suggestion else ""
        return CommandParseResult(
            kind="error",
            error=CommandError(
                kind="unknown_command",
                message=f"unknown command: /{verb_token}{hint}",
                suggestion=suggestion,
            ),
        )

    target_ref: str | None = None
    args: list[str] = []
    message: str | None = None

    if canonical in ("send", "broadcast", "reply"):
        # /send <target> <message...>
        if canonical == "send":
            if not rest:
                return CommandParseResult(
                    kind="error",
                    error=CommandError("missing_argument",
                                       "missing argument: /send <slice-ref> <message>"),
                )
            target_ref = rest[0]
            message = " ".join(rest[1:]) if len(rest) > 1 else None
            if not message:
                return CommandParseResult(
                    kind="error",
                    error=CommandError("missing_argument",
                                       "missing argument: /send <slice-ref> <message>"),
                )
        elif canonical == "broadcast":
            message = " ".join(rest) if rest else None
            if not message:
                return CommandParseResult(
                    kind="error",
                    error=CommandError("missing_argument",
                                       "missing argument: /broadcast <message>"),
                )
        else:
            # reply: everything is the message
            message = " ".join(rest) if rest else None
    elif canonical == "diff":
        target_ref = rest[0] if rest else None
        args = rest[1:]
    elif canonical in ("focus", "spawn", "close", "stop", "resume",
                       "approve", "deny", "split"):
        target_ref = rest[0] if rest else None
        args = rest[1:]
    elif canonical == "help":
        target_ref = rest[0] if rest else None  # optional command name
    else:
        args = rest

    return CommandParseResult(
        kind="command",
        verb=canonical,
        target_ref=target_ref,
        args=args,
        message=message,
    )


def _tokenize(body: str) -> list[str] | CommandError:
    """Split command body respecting double-quoted strings."""
    tokens: list[str] = []
    current: list[str] = []
    in_quotes = False

    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and in_quotes and i + 1 < len(body) and body[i + 1] == '"':
            current.append('"')
            i += 2
            continue
        if ch == '"':
            in_quotes = not in_quotes
            i += 1
            continue
        if ch == " " and not in_quotes:
            if current:
                tokens.append("".join(current))
                current = []
            i += 1
            continue
        current.append(ch)
        i += 1

    if in_quotes:
        return CommandError("parse_error", "unclosed quote in command")

    if current:
        tokens.append("".join(current))

    return tokens if tokens else [""]


def _nearest_verb(typed: str) -> str | None:
    """Return the closest known command verb by edit distance, or None."""
    best: str | None = None
    best_dist = 3  # only suggest if distance <= 2
    for verb in _COMMAND_REGISTRY:
        d = _edit_distance(typed, verb)
        if d < best_dist:
            best_dist = d
            best = verb
    return best


def _edit_distance(a: str, b: str) -> int:
    if len(a) > len(b):
        a, b = b, a
    row = list(range(len(a) + 1))
    for j, cb in enumerate(b):
        new_row = [j + 1]
        for i, ca in enumerate(a):
            new_row.append(min(row[i] + (0 if ca == cb else 1),
                               row[i + 1] + 1, new_row[-1] + 1))
        row = new_row
    return row[-1]


# ---------------------------------------------------------------------------
# Routing state manager
# ---------------------------------------------------------------------------

class RoutingController:
    """Manages the four-part routing state and dispatches parsed commands.

    Callers provide resolver callbacks for slice lookup and PTY/Lex operations.
    This keeps the controller testable without a live TUI or database.
    """

    def __init__(
        self,
        *,
        resolve_slice: Callable[[str], str | None],
        list_slices: Callable[[], list[str]],
    ):
        """
        Args:
            resolve_slice: Given a slice-ref string, return the canonical slice id
                           or None if not found / ambiguous. Should return a special
                           "ambiguous:<a>,<b>" string when multiple matches exist.
            list_slices:   Return all currently known slice ids.
        """
        self.state = RoutingState()
        self._resolve_slice = resolve_slice
        self._list_slices = list_slices

    # ── public api ────────────────────────────────────────────────────────────

    def submit(self, raw: str) -> CommandDispatchResult:
        """Process a raw prompt submission and return a dispatch result."""
        parsed = parse(raw)

        if parsed.kind == "error":
            assert parsed.error is not None
            return CommandDispatchResult(
                status="error",
                error_message=parsed.error.message,
            )

        if parsed.kind == "freeform":
            return self._dispatch_freeform(parsed.message or "")

        # kind == "command"
        assert parsed.verb is not None
        return self._dispatch_command(parsed)

    def set_slice_focus(self, slice_id: str | None) -> None:
        """Called by TUI key navigation (j/k)."""
        self.state.focused_slice_id = slice_id

    def set_expanded_slice(self, slice_id: str | None) -> None:
        """Called by TUI expand/collapse (space)."""
        self.state.expanded_slice_id = slice_id

    def set_routing_target(self, slice_id: str | None) -> None:
        """Called by TUI enter-on-slice or /focus."""
        self.state.routing_target_id = slice_id

    def where_summary(self) -> str:
        """Return a one-line state summary for /where and status bar."""
        parts = []
        if self.state.routing_target_id:
            parts.append(f"target={self.state.routing_target_id}")
        else:
            parts.append("no target")
        if self.state.focused_slice_id:
            parts.append(f"focus={self.state.focused_slice_id}")
        if self.state.expanded_slice_id:
            parts.append(f"expanded={self.state.expanded_slice_id}")
        if self.state.drawer_open and self.state.drawer_view:
            parts.append(f"drawer={self.state.drawer_view}")
        return "  ".join(parts)

    def autocomplete_verb(self, partial: str) -> list[str]:
        """Return sorted list of command verbs that start with partial."""
        return sorted(v for v in _COMMAND_REGISTRY if v.startswith(partial))

    def autocomplete_target(self, partial: str) -> list[str]:
        """Return slice ids that start with partial."""
        return sorted(s for s in self._list_slices() if s.startswith(partial))

    def command_help(self, verb: str | None = None) -> str:
        """Return a help string for one command or a grouped listing of all."""
        if verb:
            canonical = _resolve_alias(verb)
            if canonical not in _COMMAND_REGISTRY:
                return f"unknown command: /{verb}"
            e = _COMMAND_REGISTRY[canonical]
            lines = [e["usage"], "", e["help"]]
            if e.get("aliases"):
                lines.append(f"aliases: {', '.join('/' + a for a in e['aliases'])}")
            lines.append(f"dispatch: {e['kind']}")
            return "\n".join(lines)

        groups = {
            "Help & Discovery": ["help", "commands", "where"],
            "Routing": ["focus", "clear"],
            "Terminal Runtime": ["spawn", "split", "close", "stop", "resume", "terminals"],
            "Message Routing": ["send", "broadcast", "reply", "approve", "deny"],
            "Context Drawer": ["tasks", "agents", "files", "diff"],
        }
        lines: list[str] = []
        for group, verbs in groups.items():
            lines.append(f"── {group}")
            for v in verbs:
                if v in _COMMAND_REGISTRY:
                    lines.append(f"  {_COMMAND_REGISTRY[v]['usage']}")
            lines.append("")
        return "\n".join(lines).rstrip()

    # ── internal dispatch ─────────────────────────────────────────────────────

    def _dispatch_freeform(self, message: str) -> CommandDispatchResult:
        if not self.state.routing_target_id:
            return CommandDispatchResult(
                status="error",
                error_message="no routing target; use /focus <slice-ref> or press enter on a slice",
            )
        target = self.state.routing_target_id
        # Caller is responsible for the actual send; we return ok with routing info
        return CommandDispatchResult(
            status="ok",
            confirmation=f"sent to {target}",
            state_patch={"last_sent_to": target},
        )

    def _dispatch_command(self, parsed: CommandParseResult) -> CommandDispatchResult:
        verb = parsed.verb
        dispatch = {
            "help":      self._cmd_help,
            "commands":  self._cmd_commands,
            "focus":     self._cmd_focus,
            "clear":     self._cmd_clear,
            "where":     self._cmd_where,
            "spawn":     self._cmd_spawn,
            "split":     self._cmd_split,
            "close":     self._cmd_close,
            "stop":      self._cmd_stop,
            "resume":    self._cmd_resume,
            "terminals": self._cmd_terminals,
            "send":      self._cmd_send,
            "broadcast": self._cmd_broadcast,
            "reply":     self._cmd_reply,
            "tasks":     lambda p: self._cmd_drawer(p, "tasks"),
            "agents":    lambda p: self._cmd_drawer(p, "agents"),
            "files":     lambda p: self._cmd_drawer(p, "files"),
            "diff":      self._cmd_diff,
            "approve":   lambda p: self._cmd_review_answer(p, "y"),
            "deny":      lambda p: self._cmd_review_answer(p, "n"),
        }
        handler = dispatch.get(verb)
        if handler is None:
            return CommandDispatchResult(status="error",
                                         error_message=f"unknown command: /{verb}")
        return handler(parsed)

    def _resolve(self, ref: str) -> str | CommandDispatchResult:
        """Resolve a slice-ref, returning the id or an error result."""
        result = self._resolve_slice(ref)
        if result is None:
            return CommandDispatchResult(
                status="error",
                error_message=f"unknown slice: {ref}",
            )
        if result.startswith("ambiguous:"):
            candidates = result[len("ambiguous:"):]
            return CommandDispatchResult(
                status="error",
                error_message=f"ambiguous target: {ref} → {candidates}",
            )
        return result

    def _cmd_help(self, parsed: CommandParseResult) -> CommandDispatchResult:
        self.state.drawer_open = True
        self.state.drawer_view = "help"
        self.state.drawer_subject = parsed.target_ref
        patch = {"drawer_open": True, "drawer_view": "help",
                 "drawer_subject": parsed.target_ref}
        return CommandDispatchResult(status="ok", confirmation="opened drawer: help",
                                     state_patch=patch)

    def _cmd_commands(self, parsed: CommandParseResult) -> CommandDispatchResult:
        self.state.drawer_open = True
        self.state.drawer_view = "help"
        self.state.drawer_subject = None
        patch = {"drawer_open": True, "drawer_view": "help", "drawer_subject": None}
        return CommandDispatchResult(status="ok", confirmation="opened drawer: commands",
                                     state_patch=patch)

    def _cmd_focus(self, parsed: CommandParseResult) -> CommandDispatchResult:
        if not parsed.target_ref:
            return CommandDispatchResult(status="error",
                                         error_message="missing argument: /focus <slice-ref>")
        resolved = self._resolve(parsed.target_ref)
        if isinstance(resolved, CommandDispatchResult):
            return resolved
        self.state.routing_target_id = resolved
        patch = {"routing_target_id": resolved}
        return CommandDispatchResult(status="ok", confirmation=f"focused {resolved}",
                                     state_patch=patch)

    def _cmd_clear(self, parsed: CommandParseResult) -> CommandDispatchResult:
        self.state.routing_target_id = None
        return CommandDispatchResult(status="ok", confirmation="routing target cleared",
                                     state_patch={"routing_target_id": None})

    def _cmd_where(self, parsed: CommandParseResult) -> CommandDispatchResult:
        return CommandDispatchResult(status="ok", confirmation=self.where_summary())

    def _cmd_spawn(self, parsed: CommandParseResult) -> CommandDispatchResult:
        kind = parsed.target_ref
        if not kind:
            return CommandDispatchResult(
                status="error",
                error_message=f"missing argument: /spawn <runtime-kind>  "
                              f"(valid: {', '.join(sorted(_VALID_SPAWN_KINDS))})",
            )
        if kind not in _VALID_SPAWN_KINDS:
            return CommandDispatchResult(
                status="error",
                error_message=f"unknown runtime kind: {kind}  "
                              f"(valid: {', '.join(sorted(_VALID_SPAWN_KINDS))})",
            )
        # Actual spawn delegated to caller; signal intent via state_patch
        return CommandDispatchResult(
            status="ok",
            confirmation=f"spawning {kind} slice",
            state_patch={"spawn_kind": kind},
        )

    def _cmd_split(self, parsed: CommandParseResult) -> CommandDispatchResult:
        kind = parsed.target_ref  # optional
        patch: dict = {"split_kind": kind or self.state.routing_target_id}
        return CommandDispatchResult(status="ok",
                                     confirmation="splitting pane",
                                     state_patch=patch)

    def _cmd_close(self, parsed: CommandParseResult) -> CommandDispatchResult:
        if not parsed.target_ref:
            return CommandDispatchResult(status="error",
                                         error_message="missing argument: /close <slice-ref>")
        resolved = self._resolve(parsed.target_ref)
        if isinstance(resolved, CommandDispatchResult):
            return resolved
        return CommandDispatchResult(
            status="confirm_required",
            confirmation=f"close slice {resolved}? press enter to confirm or esc to cancel",
            state_patch={"close_target": resolved},
        )

    def _cmd_stop(self, parsed: CommandParseResult) -> CommandDispatchResult:
        if not parsed.target_ref:
            return CommandDispatchResult(status="error",
                                         error_message="missing argument: /stop <slice-ref>")
        resolved = self._resolve(parsed.target_ref)
        if isinstance(resolved, CommandDispatchResult):
            return resolved
        return CommandDispatchResult(status="ok",
                                     confirmation=f"stopping {resolved}",
                                     state_patch={"stop_target": resolved})

    def _cmd_resume(self, parsed: CommandParseResult) -> CommandDispatchResult:
        if not parsed.target_ref:
            return CommandDispatchResult(status="error",
                                         error_message="missing argument: /resume <slice-ref>")
        resolved = self._resolve(parsed.target_ref)
        if isinstance(resolved, CommandDispatchResult):
            return resolved
        return CommandDispatchResult(status="ok",
                                     confirmation=f"resuming {resolved}",
                                     state_patch={"resume_target": resolved})

    def _cmd_terminals(self, parsed: CommandParseResult) -> CommandDispatchResult:
        self.state.drawer_open = True
        self.state.drawer_view = "terminals"
        patch = {"drawer_open": True, "drawer_view": "terminals"}
        return CommandDispatchResult(status="ok", confirmation="opened drawer: terminals",
                                     state_patch=patch)

    def _cmd_send(self, parsed: CommandParseResult) -> CommandDispatchResult:
        if not parsed.target_ref or not parsed.message:
            return CommandDispatchResult(
                status="error",
                error_message="missing argument: /send <slice-ref> <message>",
            )
        resolved = self._resolve(parsed.target_ref)
        if isinstance(resolved, CommandDispatchResult):
            return resolved
        # Routing target does NOT change on /send
        return CommandDispatchResult(
            status="ok",
            confirmation=f"sent to {resolved}",
            state_patch={"one_shot_target": resolved, "one_shot_message": parsed.message},
        )

    def _cmd_broadcast(self, parsed: CommandParseResult) -> CommandDispatchResult:
        if not parsed.message:
            return CommandDispatchResult(status="error",
                                         error_message="missing argument: /broadcast <message>")
        all_slices = self._list_slices()
        if not all_slices:
            return CommandDispatchResult(status="error",
                                         error_message="no active slices to broadcast to")
        return CommandDispatchResult(
            status="ok",
            confirmation=f"broadcast to {len(all_slices)} slices",
            state_patch={"broadcast_message": parsed.message,
                         "broadcast_targets": all_slices},
        )

    def _cmd_reply(self, parsed: CommandParseResult) -> CommandDispatchResult:
        if not parsed.message:
            return CommandDispatchResult(status="error",
                                         error_message="missing argument: /reply <message>")
        # Resolve to expanded attention slice — caller provides this via routing state
        target = self.state.expanded_slice_id or self.state.routing_target_id
        if not target:
            return CommandDispatchResult(
                status="error",
                error_message="no attention slice to reply to; use /send <slice-ref> to be explicit",
            )
        return CommandDispatchResult(
            status="ok",
            confirmation=f"reply sent to {target}",
            state_patch={"one_shot_target": target, "one_shot_message": parsed.message},
        )

    def _cmd_drawer(self, parsed: CommandParseResult, view: str) -> CommandDispatchResult:
        self.state.drawer_open = True
        self.state.drawer_view = view
        self.state.drawer_subject = self.state.routing_target_id
        patch = {"drawer_open": True, "drawer_view": view,
                 "drawer_subject": self.state.routing_target_id}
        return CommandDispatchResult(status="ok",
                                     confirmation=f"opened drawer: {view}",
                                     state_patch=patch)

    def _cmd_diff(self, parsed: CommandParseResult) -> CommandDispatchResult:
        path = parsed.target_ref
        if not path:
            return CommandDispatchResult(status="error",
                                         error_message="missing argument: /diff <path>")
        self.state.drawer_open = True
        self.state.drawer_view = "diff"
        self.state.drawer_subject = path
        patch = {"drawer_open": True, "drawer_view": "diff", "drawer_subject": path}
        return CommandDispatchResult(status="ok",
                                     confirmation=f"opened drawer: diff {path}",
                                     state_patch=patch)

    def _cmd_review_answer(self, parsed: CommandParseResult,
                           answer: str) -> CommandDispatchResult:
        ref = parsed.target_ref
        if ref:
            resolved = self._resolve(ref)
            if isinstance(resolved, CommandDispatchResult):
                return resolved
            target = resolved
        else:
            target = self.state.expanded_slice_id or self.state.focused_slice_id
            if not target:
                return CommandDispatchResult(
                    status="error",
                    error_message="no attention slice; use /approve <slice-ref> to be explicit",
                )
        verb = "approve" if answer == "y" else "deny"
        return CommandDispatchResult(
            status="ok",
            confirmation=f"{verb}d {target}",
            state_patch={"review_answer": answer, "review_target": target},
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_alias(verb: str) -> str:
    """Return canonical verb for an alias, or the verb itself."""
    for canonical, entry in _COMMAND_REGISTRY.items():
        if verb in entry.get("aliases", []):
            return canonical
    return verb
