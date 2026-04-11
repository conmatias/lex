from __future__ import annotations

import curses
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from lex.dashboard import load_dashboard_state
from lex.dx.app import build_diff, build_dx_view
from lex.dx.commands import RoutingController
from lex.dx.pty_runtime import PTYManager, TerminalSession
from lex.dx.runtime_service import DaemonRuntimeClient, LocalRuntimeClient


@dataclass(frozen=True)
class SliceSummary:
    id: str
    title: str
    state: str
    task_label: str
    detail: str
    expanded: bool = False
    source: str = "agent"
    runtime_kind: str | None = None
    runtime_session_id: int | None = None
    attention_flag: bool = False
    unread_count: int = 0
    screen_snapshot: tuple[str, ...] = ()


@dataclass
class PromptState:
    buffer: str = ""
    history: list[str] = field(default_factory=list)
    history_index: int | None = None
    last_command: str = ""


def build_shell_summaries(
    root: Path,
    terminal_sessions: Sequence[TerminalSession] | None = None,
) -> list[SliceSummary]:
    view = build_dx_view(root)
    summaries: list[SliceSummary] = []
    for agent in view.agents:
        task_label = f"#{agent.task_id} {agent.task_title}" if agent.task_id and agent.task_title else "no task"
        detail = f"changed={agent.changed_file_count} claimed={agent.claimed_path_count}"
        summaries.append(
            SliceSummary(
                id=agent.agent_name,
                title=agent.agent_name,
                state=agent.roster_state,
                task_label=task_label,
                detail=detail,
            )
        )
    for session in terminal_sessions or ():
        cwd_label = session.cwd.name or str(session.cwd)
        pid_label = f"pid={session.pid}" if session.pid is not None else "pid=?"
        attention = "attention" if session.attention_flag else session.status
        detail = f"{cwd_label}  {pid_label}  unread={session.unread_count}"
        summaries.append(
            SliceSummary(
                id=session.title,
                title=session.title,
                state=attention,
                task_label=f"PTY {session.kind}",
                detail=detail,
                source="runtime",
                runtime_kind=session.kind,
                runtime_session_id=session.id,
                attention_flag=session.attention_flag,
                unread_count=session.unread_count,
                screen_snapshot=tuple(session.screen_lines()),
            )
        )
    summaries.sort(key=_slice_sort_key)
    return summaries


@dataclass(frozen=True)
class FeedLayout:
    dominant: SliceSummary | None
    summaries: tuple[SliceSummary, ...]
    dominant_height: int


class DxShell:
    def __init__(
        self,
        root: Path,
        *,
        pty_manager: PTYManager | None = None,
        pty_manager_factory: Callable[[], PTYManager] = PTYManager,
        runtime_command_builder: Callable[[str], str] | None = None,
        runtime_backend: str = "local",
    ):
        self.root = root
        self.prompt = PromptState()
        self.summaries: list[SliceSummary] = []
        self._pty_manager_factory = pty_manager_factory
        self._runtime_command_builder = runtime_command_builder or _default_runtime_command
        self.controller = RoutingController(
            resolve_slice=self._resolve_slice,
            list_slices=lambda: [summary.id for summary in self.summaries],
        )
        self.pty_manager: PTYManager | None = pty_manager
        self.runtime_backend = runtime_backend
        self.runtime_client: LocalRuntimeClient | DaemonRuntimeClient | None = (
            LocalRuntimeClient(pty_manager) if pty_manager is not None else None
        )
        self.focus_index = 0
        self.keyboard_focus = "feed"
        self.status = "tab=prompt  j/k=navigate  enter=target  space=expand  c=drawer  q=quit"
        # Tracks the last (rows, cols) sent to each runtime session so we only
        # call resize() when dimensions actually change.
        self._last_resize: dict[int, tuple[int, int]] = {}
        self.refresh()

    def stop(self) -> None:
        if self.runtime_client is not None:
            self.runtime_client.stop()
            self.runtime_client = None
        self.pty_manager = None

    def refresh(self) -> None:
        self.state = load_dashboard_state(self.root)
        summaries = build_shell_summaries(self.root, terminal_sessions=self._list_runtime_sessions())
        expanded_id = self.controller.state.expanded_slice_id
        focused_id = self.controller.state.focused_slice_id
        self.summaries = [
            SliceSummary(
                id=summary.id,
                title=summary.title,
                state=summary.state,
                task_label=summary.task_label,
                detail=summary.detail,
                expanded=summary.id == expanded_id,
                source=summary.source,
                runtime_kind=summary.runtime_kind,
                runtime_session_id=summary.runtime_session_id,
                attention_flag=summary.attention_flag,
                unread_count=summary.unread_count,
                screen_snapshot=summary.screen_snapshot,
            )
            for summary in summaries
        ]
        if focused_id is not None:
            self.focus_index = next(
                (idx for idx, summary in enumerate(self.summaries) if summary.id == focused_id),
                min(self.focus_index, max(len(self.summaries) - 1, 0)),
            )
        else:
            self.focus_index = min(self.focus_index, max(len(self.summaries) - 1, 0))
        if self.summaries and focused_id is None:
            self.controller.set_slice_focus(self.summaries[self.focus_index].id)

    def _resolve_slice(self, ref: str) -> str | None:
        matches = [summary.id for summary in self.summaries if summary.id == ref or summary.id.startswith(ref)]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return "ambiguous:" + ",".join(matches)
        return None

    def focused_summary(self) -> SliceSummary | None:
        if not self.summaries:
            return None
        return self.summaries[self.focus_index]

    def header_text(self) -> str:
        target = self.controller.state.routing_target_id or "no target"
        return f"dx  ▸  {target}"

    def ribbon_height(self) -> int:
        """Extra rows reserved for the command ribbon (separator + target line)."""
        return 2

    def ribbon_target_line(self) -> str:
        """Single line showing the current routing target and its live state."""
        target_id = self.controller.state.routing_target_id
        if not target_id:
            return "  no target"
        summary = next((s for s in self.summaries if s.id == target_id), None)
        if summary is None:
            return f"  ▸ {target_id}"
        return f"  ▸ {summary.title}  [{summary.state}]  {summary.task_label}"

    def submit_prompt(self, raw: str):
        result = self.controller.submit(raw)
        text = raw.strip()
        if text:
            self.prompt.history.append(text)
            self.prompt.last_command = text
        self.prompt.history_index = None
        self.prompt.buffer = ""
        patch = result.state_patch or {}
        if "spawn_kind" in patch:
            self._spawn_runtime(patch["spawn_kind"])
        elif "split_kind" in patch:
            self._spawn_runtime(self._resolve_split_kind(patch["split_kind"]))
        elif "one_shot_target" in patch and "one_shot_message" in patch:
            self._send_to_runtime(patch["one_shot_target"], patch["one_shot_message"], fallback_status=result.confirmation)
        elif "last_sent_to" in patch:
            self._send_to_runtime(patch["last_sent_to"], text, fallback_status=result.confirmation)
        elif "review_target" in patch and "review_answer" in patch:
            self._send_to_runtime(patch["review_target"], patch["review_answer"], fallback_status=result.confirmation)
        elif "stop_target" in patch:
            self._close_runtime(patch["stop_target"], verb="stopped")
        elif "close_target" in patch:
            self._close_runtime(patch["close_target"], verb="closed")
        elif "broadcast_targets" in patch and "broadcast_message" in patch:
            msg = patch["broadcast_message"]
            for target in patch["broadcast_targets"]:
                self._send_to_runtime(target, msg, fallback_status=None)
            self.status = result.confirmation or f"broadcast to {len(patch['broadcast_targets'])} slices"
        elif result.confirmation:
            self.status = result.confirmation
        elif result.error_message:
            self.status = result.error_message
        if "routing_target_id" in patch or "expanded_slice_id" in patch or "drawer_open" in patch:
            self.refresh()
        return result

    def _list_runtime_sessions(self) -> list[TerminalSession]:
        client = self._ensure_runtime_client()
        if client is None:
            return []
        return client.list_sessions()

    def _find_runtime_session(self, slice_id: str) -> TerminalSession | None:
        client = self._ensure_runtime_client()
        if client is None:
            return None
        for session in client.list_sessions():
            if session.title == slice_id:
                return session
        return None

    def _spawn_runtime(self, kind: str) -> None:
        client = self._ensure_runtime_client()
        if client is None:
            self.status = "runtime backend unavailable"
            return
        try:
            cmd = self._runtime_command_builder(kind)
        except RuntimeError as exc:
            self.status = str(exc)
            return
        try:
            session_id = client.spawn(
                kind,
                cmd,
                cwd=self.root,
                lex_root=self.root,
            )
        except Exception as exc:
            self.status = f"spawn failed: {exc}"
            return
        session = client.get_session(session_id)
        if session is None:
            self.status = f"failed to spawn {kind}"
            return
        self.controller.set_slice_focus(session.title)
        self.controller.set_routing_target(session.title)
        self.controller.set_expanded_slice(session.title)
        self.refresh()
        self.focus_index = next((idx for idx, summary in enumerate(self.summaries) if summary.id == session.title), 0)
        self.status = f"spawned {session.title}"

    def _resolve_split_kind(self, requested: str | None) -> str:
        if requested in {"claude", "codex", "gemini", "shell"}:
            return requested
        focused = self.focused_summary()
        if focused and focused.runtime_kind:
            return focused.runtime_kind
        return "shell"

    def _send_to_runtime(self, slice_id: str, message: str, *, fallback_status: str | None) -> None:
        session = self._find_runtime_session(slice_id)
        if session is None:
            self.status = f"slice {slice_id} has no PTY session"
            return
        if session.status == "exited":
            self.status = f"{slice_id} has exited — use /close to remove it"
            return
        client = self._ensure_runtime_client()
        if client is None:
            self.status = "runtime backend unavailable"
            return
        client.write(session.id, self._runtime_submit_payload(session, message))
        client.set_display_state(session.id, "expanded")
        self.controller.set_expanded_slice(slice_id)
        self.status = fallback_status or f"sent to {slice_id}"
        self.refresh()

    def _runtime_submit_payload(self, session: TerminalSession, message: str) -> str:
        if not message:
            return message
        # Some coding CLIs use a multiline composer where the first Enter adds
        # a newline and the second Enter submits. Fold that second press into
        # the initial send so the main dx prompt behaves like a real submit.
        if session.kind in {"codex", "gemini"}:
            return f"{message}\r\r"
        return message

    def _close_runtime(self, slice_id: str, *, verb: str) -> None:
        session = self._find_runtime_session(slice_id)
        client = self._ensure_runtime_client()
        if session is None or client is None:
            self.status = f"slice {slice_id} has no PTY session"
            return
        client.close(session.id)
        if self.controller.state.routing_target_id == slice_id:
            self.controller.set_routing_target(None)
        if self.controller.state.expanded_slice_id == slice_id:
            self.controller.set_expanded_slice(None)
        if self.controller.state.focused_slice_id == slice_id:
            self.controller.set_slice_focus(None)
        self.refresh()
        self.status = f"{verb} {slice_id}"

    def _ensure_runtime_client(self) -> LocalRuntimeClient | DaemonRuntimeClient | None:
        if self.runtime_client is not None:
            return self.runtime_client
        if self.runtime_backend == "daemon":
            try:
                self.runtime_client = DaemonRuntimeClient(self.root, autostart=True)
                return self.runtime_client
            except Exception as exc:
                self.status = f"daemon unavailable: {exc}; using local runtime"
        manager = self.pty_manager or self._pty_manager_factory()
        self.pty_manager = manager
        self.runtime_client = LocalRuntimeClient(manager)
        return self.runtime_client

    def runtime_viewport_lines(self, summary: SliceSummary, *, max_lines: int) -> list[str]:
        if summary.source != "runtime":
            return []
        lines = [line.rstrip() for line in summary.screen_snapshot]
        if not lines:
            return ["(no terminal output yet)"]
        return lines[-max(max_lines, 1):]

    def expanded_lines(self, summary: SliceSummary, *, max_lines: int) -> list[str]:
        if summary.source != "runtime":
            return [f"{summary.task_label}  {summary.detail}", "No live PTY session attached to this slice."]
        lines: list[str] = [f"{summary.task_label}  {summary.detail}"]
        body_lines = self.runtime_viewport_lines(summary, max_lines=max(max_lines - 2, 1))
        lines.extend(body_lines)
        actions = "[y] approve  [n] deny  [enter] route  [space] collapse"
        if summary.attention_flag:
            actions = "[y] approve  [n] deny  [m] prompt  [space] collapse"
        lines.append(actions)
        return lines[:max_lines]

    def active_runtime_summary(self) -> SliceSummary | None:
        expanded_id = self.controller.state.expanded_slice_id
        if expanded_id:
            expanded = next((summary for summary in self.summaries if summary.id == expanded_id), None)
            if expanded and expanded.source == "runtime":
                return expanded
        focused = self.focused_summary()
        if focused and focused.source == "runtime":
            return focused
        return next((summary for summary in self.summaries if summary.source == "runtime"), None)

    def feed_layout(self, *, available_rows: int) -> FeedLayout:
        dominant = self.active_runtime_summary()
        summaries = [summary for summary in self.summaries if dominant is None or summary.id != dominant.id]
        if dominant is None or available_rows <= 3:
            return FeedLayout(dominant=None, summaries=tuple(self.summaries), dominant_height=0)
        dominant_height = max(int(available_rows * 0.75), min(available_rows - 2, 8))
        dominant_height = min(dominant_height, max(available_rows - len(summaries), 4))
        dominant_height = max(dominant_height, min(available_rows, 4))
        return FeedLayout(dominant=dominant, summaries=tuple(summaries), dominant_height=dominant_height)

    def drawer_lines(self, *, height: int) -> list[str]:
        view = self.controller.state.drawer_view or "help"
        subject = self.controller.state.drawer_subject
        if view == "help":
            return self.controller.command_help(subject).splitlines()[:height]
        if view == "terminals":
            sessions = self._list_runtime_sessions()
            if not sessions:
                return ["No active PTY sessions."]
            lines = [
                f"{session.title}  [{session.status}]  {session.kind}  unread={session.unread_count}"
                for session in sessions
            ]
            return lines[:height]
        if view == "files":
            target = self.controller.state.routing_target_id
            summary = next((item for item in self.summaries if item.id == target), None)
            if summary is None or summary.source != "agent":
                return ["No agent file context for the current routing target."]
            view_state = build_dx_view(self.root)
            agent = next((agent for agent in view_state.agents if agent.agent_name == summary.id), None)
            if agent is None or not agent.files:
                return ["No claimed or changed files."]
            return [file.path for file in agent.files[:height]]
        if view == "tasks":
            target = self.controller.state.routing_target_id or self.controller.state.focused_slice_id
            if target is None:
                return ["No routing target."]
            summary = next((item for item in self.summaries if item.id == target), None)
            if summary is None:
                return ["No slice selected."]
            return [summary.task_label, summary.detail]
        if view == "diff":
            if not subject:
                return ["No diff path selected."]
            target = self.controller.state.routing_target_id
            base_ref = None
            if target:
                view_state = build_dx_view(self.root)
                agent = next((agent for agent in view_state.agents if agent.agent_name == target), None)
                if agent and agent.session_id is not None:
                    session = next((row for row in self.state.sessions if row["agent_name"] == target), None)
                    if session:
                        base_ref = session.get("git_base_ref")
            return build_diff(self.root, base_ref, subject).splitlines()[:height]
        return ["Unsupported drawer view."]

    def move_focus(self, delta: int) -> None:
        if not self.summaries:
            return
        self.focus_index = max(0, min(self.focus_index + delta, len(self.summaries) - 1))
        focused = self.focused_summary()
        if focused is not None:
            self.controller.set_slice_focus(focused.id)

    def route_to_focused(self) -> None:
        summary = self.focused_summary()
        if summary is None:
            self.status = "no slice focused"
            return
        self.controller.set_routing_target(summary.id)
        self.status = f"routing target -> {summary.id}"

    def toggle_expand(self) -> None:
        summary = self.focused_summary()
        if summary is None:
            self.status = "no slice focused"
            return
        new_id = None if self.controller.state.expanded_slice_id == summary.id else summary.id
        self.controller.set_expanded_slice(new_id)
        client = self._ensure_runtime_client()
        if summary.runtime_session_id is not None and client is not None:
            state = "expanded" if new_id is not None else "collapsed"
            client.set_display_state(summary.runtime_session_id, state)
        if new_id is not None and summary.runtime_session_id is not None:
            self.controller.set_routing_target(summary.id)
        self.refresh()
        self.status = "expanded" if new_id else "collapsed"

    def toggle_drawer(self) -> None:
        state = self.controller.state
        state.drawer_open = not state.drawer_open
        if not state.drawer_open:
            state.drawer_view = None
            state.drawer_subject = None
        else:
            state.drawer_view = state.drawer_view or "help"
        self.status = f"drawer {'open' if state.drawer_open else 'closed'}"

    def history_up(self) -> None:
        if not self.prompt.history:
            return
        if self.prompt.history_index is None:
            self.prompt.history_index = len(self.prompt.history) - 1
        else:
            self.prompt.history_index = max(0, self.prompt.history_index - 1)
        self.prompt.buffer = self.prompt.history[self.prompt.history_index]

    def history_down(self) -> None:
        if self.prompt.history_index is None:
            return
        if self.prompt.history_index >= len(self.prompt.history) - 1:
            self.prompt.history_index = None
            self.prompt.buffer = ""
        else:
            self.prompt.history_index += 1
            self.prompt.buffer = self.prompt.history[self.prompt.history_index]

    def handle_key(self, stdscr, ch: int) -> bool:
        if ch in (ord("q"), 27) and self.keyboard_focus == "feed":
            return False
        if ch == 9:
            self.keyboard_focus = "prompt" if self.keyboard_focus == "feed" else "feed"
            return True
        if self.keyboard_focus == "feed":
            if ch in (ord("j"), curses.KEY_DOWN):
                self.move_focus(1)
            elif ch in (ord("k"), curses.KEY_UP):
                self.move_focus(-1)
            elif ch in (10, 13):
                self.route_to_focused()
            elif ch == ord(" "):
                self.toggle_expand()
            elif ch == ord("c"):
                self.toggle_drawer()
            elif ch == ord("r"):
                self.refresh()
            elif ch == ord("y"):
                summary = self.focused_summary()
                if summary is not None:
                    self._send_to_runtime(summary.id, "y", fallback_status=f"approved {summary.id}")
            elif ch == ord("n"):
                summary = self.focused_summary()
                if summary is not None:
                    self._send_to_runtime(summary.id, "n", fallback_status=f"denied {summary.id}")
            elif ch == ord("m"):
                self.keyboard_focus = "prompt"
            return True
        if ch in (curses.KEY_UP,):
            self.history_up()
        elif ch in (curses.KEY_DOWN,):
            self.history_down()
        elif ch in (10, 13):
            self.submit_prompt(self.prompt.buffer)
        elif ch in (curses.KEY_BACKSPACE, 127):
            self.prompt.buffer = self.prompt.buffer[:-1]
        elif ch == 27:
            self.prompt.buffer = ""
            self.keyboard_focus = "feed"
        elif 32 <= ch <= 126:
            self.prompt.buffer += chr(ch)
        return True

    def render(self, stdscr) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        stdscr.addnstr(0, 0, self.header_text().ljust(width - 1), width - 1, curses.A_REVERSE)

        prompt_row = height - 1
        ribbon_h = self.ribbon_height()
        drawer_height = max(height // 3, 4) if self.controller.state.drawer_open else 0
        # Reserve: status line + ribbon (separator + target line)
        feed_bottom = prompt_row - drawer_height - 1 - ribbon_h

        if not self.summaries:
            stdscr.addnstr(2, 2, "No active slices", width - 4)
        else:
            layout = self.feed_layout(available_rows=max(feed_bottom - 1, 0))
            row = 2
            if layout.dominant is not None and row <= feed_bottom:
                dominant = layout.dominant
                # Propagate terminal size to the dominant PTY session whenever
                # dimensions change so embedded TUIs reflow correctly.
                client = self._ensure_runtime_client()
                if dominant.runtime_session_id is not None and client is not None:
                    dom_rows = max(layout.dominant_height - 1, 1)
                    dom_cols = max(width - 6, 1)
                    prev = self._last_resize.get(dominant.runtime_session_id)
                    if prev != (dom_rows, dom_cols):
                        client.resize(dominant.runtime_session_id, dom_rows, dom_cols)
                        self._last_resize[dominant.runtime_session_id] = (dom_rows, dom_cols)
                focused = self.focused_summary()
                dominant_focus = focused is not None and focused.id == dominant.id and self.keyboard_focus == "feed"
                prefix = "▶" if dominant_focus else " "
                line = f"{prefix} ▼ {dominant.title} [{dominant.state}]  {dominant.task_label}  {dominant.detail}"
                attr = curses.A_BOLD if dominant_focus else curses.A_NORMAL
                stdscr.addnstr(row, 1, line, width - 2, attr)
                row += 1
                dominant_lines = self.expanded_lines(dominant, max_lines=max(layout.dominant_height - 1, 1))
                for line in dominant_lines:
                    if row > feed_bottom:
                        break
                    stdscr.addnstr(row, 4, line, width - 6)
                    row += 1
                if row <= feed_bottom:
                    stdscr.hline(row, 1, "-", max(width - 2, 1))
                    row += 1
            visible_summaries = layout.summaries if layout.dominant is not None else tuple(self.summaries)
            for summary in visible_summaries:
                if row > feed_bottom:
                    break
                idx = next((index for index, candidate in enumerate(self.summaries) if candidate.id == summary.id), 0)
                focused = idx == self.focus_index and self.keyboard_focus == "feed"
                prefix = "▶" if focused else " "
                marker = _state_icon(summary)
                # Focused or attention slices get full detail; others compress to a single label.
                if focused or summary.attention_flag:
                    line = f"{prefix} {marker} {summary.title} [{summary.state}]  {summary.task_label}  {summary.detail}"
                else:
                    line = f"{prefix} {marker} {summary.title} [{summary.state}]"
                attr = curses.A_BOLD if focused else curses.A_NORMAL
                stdscr.addnstr(row, 1, line, width - 2, attr)
                row += 1

        if self.controller.state.drawer_open:
            drawer_top = prompt_row - drawer_height
            stdscr.hline(drawer_top, 0, "-", width)
            drawer_title = f" drawer: {self.controller.state.drawer_view or 'help'} "
            stdscr.addnstr(drawer_top, 2, drawer_title, width - 4, curses.A_BOLD)
            drawer_lines = self.drawer_lines(height=max(drawer_height - 1, 0))
            for idx, line in enumerate(drawer_lines[: max(drawer_height - 1, 0)]):
                stdscr.addnstr(drawer_top + 1 + idx, 2, line, width - 4)

        # Command ribbon: separator + routing target breadcrumb
        ribbon_sep_row = prompt_row - 1 - ribbon_h
        if ribbon_sep_row > 0:
            stdscr.hline(ribbon_sep_row, 0, "─", width)
            stdscr.addnstr(ribbon_sep_row + 1, 0, self.ribbon_target_line().ljust(width - 1), width - 1)

        # Status / feedback line
        if prompt_row - 1 > 0:
            stdscr.addnstr(prompt_row - 1, 0, self.status.ljust(width - 1), width - 1)

        # Prompt — prefix shows routing target when active
        target = self.controller.state.routing_target_id
        if self.keyboard_focus == "prompt" and target:
            prompt_prefix = f"› {target} ▶ "
        elif self.keyboard_focus == "prompt":
            prompt_prefix = "›  "
        else:
            prompt_prefix = "   "
        stdscr.addnstr(prompt_row, 0, (prompt_prefix + self.prompt.buffer).ljust(width - 1), width - 1, curses.A_REVERSE)
        stdscr.refresh()

    def run(self) -> None:
        curses.wrapper(self._main)

    def _main(self, stdscr) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        stdscr.timeout(100)
        while True:
            self.refresh()
            self.render(stdscr)
            ch = stdscr.getch()
            if ch == -1:
                continue
            if not self.handle_key(stdscr, ch):
                return


def run_shell(root: Path) -> None:
    shell = DxShell(root, runtime_backend="daemon")
    try:
        shell.run()
    finally:
        shell.stop()


def _slice_sort_key(summary: SliceSummary) -> tuple[int, str]:
    priority = {
        "attention": 0,
        "review": 0,
        "permission": 0,
        "running": 1,
        "active": 1,
        "waiting": 2,
        "blocked": 3,
        "idle": 4,
        "stale": 5,
        "exited": 6,
    }
    source_bias = 0 if summary.source == "runtime" else 1
    return (priority.get(summary.state, 3), source_bias, summary.title)


def _state_icon(summary: SliceSummary) -> str:
    if summary.attention_flag:
        return "▲"
    return {
        "active": "●",
        "running": "⊙",
        "idle": "○",
        "waiting": "⊙",
        "blocked": "⚠",
        "stale": "–",
        "exited": "×",
    }.get(summary.state, "•")


def _default_runtime_command(kind: str) -> str:
    import shutil

    if kind == "shell":
        return os.environ.get("SHELL", "bash")

    # For agent kinds, require the real binary — no sleep placeholders.
    binary = shutil.which(kind)
    if binary is None:
        raise RuntimeError(
            f"Cannot spawn '{kind}' runtime: '{kind}' binary not found in PATH. "
            f"Install it or pass a custom runtime_command_builder to DxShell."
        )
    return binary
