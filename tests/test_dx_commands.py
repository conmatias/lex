"""Tests for dx v3 command parser and routing state (commands.py).

Covers:
- Input classification (slash command / freeform / error)
- Tokenizer (quoted strings, unclosed quotes)
- Verb recognition, alias resolution, typo suggestions
- Argument extraction for every command family
- RoutingController state transitions
- Dispatch results and confirmations
- Error messages match spec
- Autocomplete helpers
"""
import pytest

from lex.dx.commands import (
    CommandParseResult,
    RoutingController,
    RoutingState,
    parse,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_controller(slices: list[str] | None = None):
    known = list(slices or [])

    def resolve(ref: str) -> str | None:
        matches = [s for s in known if s == ref or s.startswith(ref)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return "ambiguous:" + ",".join(matches)
        return None

    return RoutingController(resolve_slice=resolve, list_slices=lambda: list(known))


# ---------------------------------------------------------------------------
# Input classification
# ---------------------------------------------------------------------------

class TestInputClassification:
    def test_empty_string_is_freeform(self):
        r = parse("")
        assert r.kind == "freeform"
        assert r.message == ""

    def test_whitespace_only_is_freeform(self):
        r = parse("   ")
        assert r.kind == "freeform"

    def test_plain_text_is_freeform(self):
        r = parse("please check the auth module")
        assert r.kind == "freeform"
        assert r.message == "please check the auth module"

    def test_slash_prefix_is_command(self):
        r = parse("/focus codex")
        assert r.kind == "command"

    def test_bare_slash_becomes_help(self):
        r = parse("/")
        assert r.kind == "command"
        assert r.verb == "help"

    def test_unknown_command_is_error_not_freeform(self):
        r = parse("/totally_unknown_verb")
        assert r.kind == "error"
        assert r.error is not None
        assert r.error.kind == "unknown_command"

    def test_malformed_command_never_reclassified_as_freeform(self):
        r = parse('/send "unclosed')
        assert r.kind == "error"


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

class TestTokenizer:
    def test_simple_tokens(self):
        r = parse("/focus codex")
        assert r.verb == "focus"
        assert r.target_ref == "codex"

    def test_quoted_argument_with_spaces(self):
        r = parse('/send claude "please check auth/session.py"')
        assert r.verb == "send"
        assert r.target_ref == "claude"
        assert r.message == "please check auth/session.py"

    def test_escaped_quote_inside_string(self):
        r = parse(r'/broadcast say "hello world" to all')
        assert r.kind == "command"
        assert r.verb == "broadcast"

    def test_unclosed_quote_is_error(self):
        r = parse('/send claude "unclosed')
        assert r.kind == "error"
        assert r.error.kind == "parse_error"

    def test_multiple_spaces_collapsed(self):
        r = parse("/focus   codex")
        assert r.target_ref == "codex"


# ---------------------------------------------------------------------------
# Verb recognition and aliases
# ---------------------------------------------------------------------------

class TestVerbRecognition:
    def test_all_canonical_verbs_recognized(self):
        samples = {
            "help": "/help",
            "commands": "/commands",
            "focus": "/focus codex",
            "route": "/route codex",
            "clear": "/clear",
            "where": "/where",
            "spawn": "/spawn codex",
            "split": "/split codex",
            "close": "/close codex",
            "stop": "/stop codex",
            "resume": "/resume codex",
            "terminals": "/terminals",
            "send": "/send codex hello",
            "broadcast": "/broadcast hello",
            "reply": "/reply hello",
            "tasks": "/tasks",
            "agents": "/agents",
            "files": "/files",
            "diff": "/diff src/lex/db.py",
            "approve": "/approve codex",
            "deny": "/deny codex",
        }
        for v, sample in samples.items():
            r = parse(sample)
            assert r.kind != "error", f"/{v} should be recognized"

    def test_route_is_alias_for_focus(self):
        r = parse("/route codex")
        assert r.kind == "command"
        assert r.verb == "focus"

    def test_typo_suggests_nearest(self):
        r = parse("/foqus codex")
        assert r.kind == "error"
        assert r.error.suggestion == "focus"

    def test_broadcast_typo_suggests(self):
        r = parse("/brodcast hello")
        assert r.kind == "error"
        assert "broadcast" in (r.error.suggestion or "")

    def test_no_suggestion_for_very_different_word(self):
        r = parse("/zzzzzzz")
        assert r.kind == "error"
        assert r.error.suggestion is None


# ---------------------------------------------------------------------------
# Argument extraction
# ---------------------------------------------------------------------------

class TestArgumentExtraction:
    def test_focus_extracts_target_ref(self):
        r = parse("/focus codex-brisk-falcon")
        assert r.target_ref == "codex-brisk-falcon"

    def test_send_extracts_target_and_message(self):
        r = parse("/send codex rerun the failing auth tests")
        assert r.target_ref == "codex"
        assert r.message == "rerun the failing auth tests"

    def test_send_missing_message_is_error(self):
        r = parse("/send codex")
        assert r.kind == "error"
        assert r.error.kind == "missing_argument"

    def test_send_missing_both_args_is_error(self):
        r = parse("/send")
        assert r.kind == "error"

    def test_broadcast_captures_full_message(self):
        r = parse("/broadcast all agents: rebase onto main")
        assert r.message == "all agents: rebase onto main"

    def test_broadcast_missing_message_is_error(self):
        r = parse("/broadcast")
        assert r.kind == "error"
        assert r.error.kind == "missing_argument"

    def test_spawn_extracts_kind(self):
        r = parse("/spawn claude")
        assert r.target_ref == "claude"

    def test_diff_extracts_path(self):
        r = parse("/diff src/lex/db.py")
        assert r.target_ref == "src/lex/db.py"

    def test_help_with_verb_argument(self):
        r = parse("/help focus")
        assert r.verb == "help"
        assert r.target_ref == "focus"

    def test_approve_with_explicit_target(self):
        r = parse("/approve codex")
        assert r.target_ref == "codex"

    def test_approve_without_target(self):
        r = parse("/approve")
        assert r.kind == "command"
        assert r.target_ref is None


# ---------------------------------------------------------------------------
# RoutingController: state transitions
# ---------------------------------------------------------------------------

class TestRoutingControllerState:
    def test_initial_state_has_no_target(self):
        ctrl = _make_controller(["codex", "claude"])
        assert ctrl.state.routing_target_id is None
        assert ctrl.state.focused_slice_id is None
        assert ctrl.state.expanded_slice_id is None
        assert ctrl.state.drawer_open is False

    def test_focus_command_sets_routing_target(self):
        ctrl = _make_controller(["codex", "claude"])
        result = ctrl.submit("/focus codex")
        assert result.status == "ok"
        assert ctrl.state.routing_target_id == "codex"

    def test_clear_removes_routing_target(self):
        ctrl = _make_controller(["codex"])
        ctrl.submit("/focus codex")
        result = ctrl.submit("/clear")
        assert result.status == "ok"
        assert ctrl.state.routing_target_id is None

    def test_set_slice_focus_updates_state(self):
        ctrl = _make_controller(["codex"])
        ctrl.set_slice_focus("codex")
        assert ctrl.state.focused_slice_id == "codex"

    def test_set_routing_target_updates_state(self):
        ctrl = _make_controller(["claude"])
        ctrl.set_routing_target("claude")
        assert ctrl.state.routing_target_id == "claude"

    def test_set_expanded_slice_updates_state(self):
        ctrl = _make_controller(["claude"])
        ctrl.set_expanded_slice("claude")
        assert ctrl.state.expanded_slice_id == "claude"

    def test_focus_and_routing_target_are_independent(self):
        ctrl = _make_controller(["codex", "claude"])
        ctrl.set_slice_focus("codex")
        ctrl.submit("/focus claude")
        assert ctrl.state.focused_slice_id == "codex"
        assert ctrl.state.routing_target_id == "claude"

    def test_drawer_commands_open_drawer(self):
        ctrl = _make_controller()
        for cmd, view in [("/tasks", "tasks"), ("/agents", "agents"),
                           ("/files", "files"), ("/terminals", "terminals")]:
            result = ctrl.submit(cmd)
            assert result.status == "ok"
            assert ctrl.state.drawer_open is True
            assert ctrl.state.drawer_view == view

    def test_diff_sets_drawer_subject(self):
        ctrl = _make_controller()
        result = ctrl.submit("/diff src/lex/db.py")
        assert result.status == "ok"
        assert ctrl.state.drawer_view == "diff"
        assert ctrl.state.drawer_subject == "src/lex/db.py"


# ---------------------------------------------------------------------------
# RoutingController: freeform dispatch
# ---------------------------------------------------------------------------

class TestFreeformDispatch:
    def test_freeform_without_target_is_error(self):
        ctrl = _make_controller(["codex"])
        result = ctrl.submit("please check the auth module")
        assert result.status == "error"
        assert "no routing target" in result.error_message

    def test_freeform_with_target_succeeds(self):
        ctrl = _make_controller(["codex"])
        ctrl.submit("/focus codex")
        result = ctrl.submit("please check the auth module")
        assert result.status == "ok"
        assert "codex" in result.confirmation

    def test_freeform_confirmation_names_target(self):
        ctrl = _make_controller(["claude"])
        ctrl.set_routing_target("claude")
        result = ctrl.submit("rerun the failing tests")
        assert result.confirmation == "sent to claude"


# ---------------------------------------------------------------------------
# RoutingController: slice resolution
# ---------------------------------------------------------------------------

class TestSliceResolution:
    def test_exact_match_resolves(self):
        ctrl = _make_controller(["codex-brisk-falcon"])
        result = ctrl.submit("/focus codex-brisk-falcon")
        assert result.status == "ok"

    def test_prefix_match_resolves_when_unambiguous(self):
        ctrl = _make_controller(["codex-brisk-falcon"])
        result = ctrl.submit("/focus codex")
        assert result.status == "ok"
        assert ctrl.state.routing_target_id == "codex-brisk-falcon"

    def test_ambiguous_prefix_is_error(self):
        ctrl = _make_controller(["codex-brisk-falcon", "codex-steady-otter"])
        result = ctrl.submit("/focus codex")
        assert result.status == "error"
        assert "ambiguous" in result.error_message

    def test_unknown_slice_is_error(self):
        ctrl = _make_controller(["codex"])
        result = ctrl.submit("/focus claude")
        assert result.status == "error"
        assert "unknown slice" in result.error_message


# ---------------------------------------------------------------------------
# RoutingController: specific command behaviours
# ---------------------------------------------------------------------------

class TestCommandBehaviours:
    def test_where_returns_state_summary(self):
        ctrl = _make_controller(["codex"])
        ctrl.set_routing_target("codex")
        result = ctrl.submit("/where")
        assert result.status == "ok"
        assert "codex" in result.confirmation

    def test_where_with_no_target(self):
        ctrl = _make_controller()
        result = ctrl.submit("/where")
        assert result.status == "ok"
        assert "no target" in result.confirmation

    def test_spawn_valid_kind_returns_ok(self):
        ctrl = _make_controller()
        for kind in ["claude", "codex", "gemini", "shell"]:
            result = ctrl.submit(f"/spawn {kind}")
            assert result.status == "ok"
            assert result.state_patch.get("spawn_kind") == kind

    def test_spawn_invalid_kind_is_error(self):
        ctrl = _make_controller()
        result = ctrl.submit("/spawn vim")
        assert result.status == "error"
        assert "vim" in result.error_message

    def test_spawn_missing_kind_is_error(self):
        ctrl = _make_controller()
        result = ctrl.submit("/spawn")
        assert result.status == "error"
        assert result.error_message is not None

    def test_send_does_not_change_routing_target(self):
        ctrl = _make_controller(["codex", "claude"])
        ctrl.submit("/focus codex")
        ctrl.submit("/send claude please review this")
        assert ctrl.state.routing_target_id == "codex"

    def test_send_result_contains_target(self):
        ctrl = _make_controller(["claude"])
        result = ctrl.submit("/send claude please check this")
        assert result.status == "ok"
        assert "claude" in result.confirmation

    def test_broadcast_to_empty_slices_is_error(self):
        ctrl = _make_controller([])
        result = ctrl.submit("/broadcast hello everyone")
        assert result.status == "error"

    def test_broadcast_includes_slice_count(self):
        ctrl = _make_controller(["codex", "claude", "gemini"])
        result = ctrl.submit("/broadcast switch branch to feature/auth")
        assert result.status == "ok"
        assert "3" in result.confirmation

    def test_close_requires_confirmation(self):
        ctrl = _make_controller(["codex"])
        result = ctrl.submit("/close codex")
        assert result.status == "confirm_required"
        assert result.state_patch.get("close_target") == "codex"

    def test_reply_uses_expanded_slice(self):
        ctrl = _make_controller(["codex"])
        ctrl.set_expanded_slice("codex")
        result = ctrl.submit("/reply yes go ahead")
        assert result.status == "ok"
        assert "codex" in result.confirmation

    def test_reply_without_any_context_is_error(self):
        ctrl = _make_controller(["codex"])
        result = ctrl.submit("/reply yes go ahead")
        assert result.status == "error"

    def test_approve_sends_y_to_target(self):
        ctrl = _make_controller(["codex"])
        ctrl.set_expanded_slice("codex")
        result = ctrl.submit("/approve")
        assert result.status == "ok"
        assert result.state_patch.get("review_answer") == "y"
        assert result.state_patch.get("review_target") == "codex"

    def test_deny_sends_n_to_target(self):
        ctrl = _make_controller(["codex"])
        ctrl.set_focused_slice = lambda x: None
        ctrl.set_expanded_slice("codex")
        result = ctrl.submit("/deny")
        assert result.state_patch.get("review_answer") == "n"


# ---------------------------------------------------------------------------
# Autocomplete
# ---------------------------------------------------------------------------

class TestAutocomplete:
    def test_verb_autocomplete_returns_matches(self):
        ctrl = _make_controller()
        matches = ctrl.autocomplete_verb("fo")
        assert "focus" in matches

    def test_verb_autocomplete_empty_returns_all(self):
        ctrl = _make_controller()
        matches = ctrl.autocomplete_verb("")
        assert len(matches) > 5

    def test_target_autocomplete_matches_prefix(self):
        ctrl = _make_controller(["codex-brisk-falcon", "claude-steady-ibis"])
        matches = ctrl.autocomplete_target("cod")
        assert matches == ["codex-brisk-falcon"]

    def test_target_autocomplete_no_match(self):
        ctrl = _make_controller(["codex"])
        assert ctrl.autocomplete_target("zzz") == []


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

class TestHelpText:
    def test_help_for_known_command(self):
        ctrl = _make_controller()
        text = ctrl.command_help("focus")
        assert "/focus" in text
        assert "routing target" in text.lower()

    def test_help_for_unknown_command(self):
        ctrl = _make_controller()
        text = ctrl.command_help("notaverb")
        assert "unknown" in text

    def test_help_all_lists_groups(self):
        ctrl = _make_controller()
        text = ctrl.command_help()
        assert "Routing" in text
        assert "Terminal Runtime" in text

    def test_help_command_dispatches_to_drawer(self):
        ctrl = _make_controller()
        result = ctrl.submit("/help")
        assert result.status == "ok"
        assert ctrl.state.drawer_open is True
        assert ctrl.state.drawer_view == "help"
