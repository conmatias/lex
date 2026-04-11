from __future__ import annotations

import curses
from pathlib import Path

from lex.dashboard import DashboardState, load_dashboard_state


# ── color pair IDs ─────────────────────────────────────────────────────────────
_C_IN_PROGRESS   = 1
_C_CLAIMED       = 2
_C_DONE          = 3
_C_BLOCKED       = 4
_C_REVIEW        = 5
_C_HANDOFF       = 6
_C_DIM           = 7
_C_CODEX         = 8
_C_CLAUDE        = 9
_C_CURSOR        = 10
_C_HIGHLIGHT     = 11
_C_HEADER        = 12
_C_BORDER        = 13
_C_FOCUS_BORDER  = 14
_C_STATUS_BAR    = 15
_C_GEMINI        = 16


def _init_colors() -> bool:
    if not curses.has_colors():
        return False
    try:
        curses.start_color()
        curses.use_default_colors()
    except curses.error:
        return False
    bg = -1
    try:
        curses.init_pair(_C_IN_PROGRESS,  curses.COLOR_CYAN,    bg)
        curses.init_pair(_C_CLAIMED,      curses.COLOR_YELLOW,  bg)
        curses.init_pair(_C_DONE,         curses.COLOR_GREEN,   bg)
        curses.init_pair(_C_BLOCKED,      curses.COLOR_RED,     bg)
        curses.init_pair(_C_REVIEW,       curses.COLOR_MAGENTA, bg)
        curses.init_pair(_C_HANDOFF,      curses.COLOR_BLUE,    bg)
        curses.init_pair(_C_DIM,          curses.COLOR_WHITE,   bg)
        curses.init_pair(_C_CODEX,        curses.COLOR_CYAN,    bg)
        curses.init_pair(_C_CLAUDE,       curses.COLOR_MAGENTA, bg)
        curses.init_pair(_C_CURSOR,       curses.COLOR_YELLOW,  bg)
        curses.init_pair(_C_HIGHLIGHT,    curses.COLOR_BLACK,   curses.COLOR_CYAN)
        curses.init_pair(_C_HEADER,       curses.COLOR_WHITE,   bg)
        curses.init_pair(_C_BORDER,       curses.COLOR_WHITE,   bg)
        curses.init_pair(_C_FOCUS_BORDER, curses.COLOR_CYAN,    bg)
        curses.init_pair(_C_STATUS_BAR,   curses.COLOR_BLACK,   curses.COLOR_WHITE)
        curses.init_pair(_C_GEMINI,       curses.COLOR_BLUE,    bg)
    except curses.error:
        return False
    return True


def _safe_curs_set(visibility: int) -> None:
    try:
        curses.curs_set(visibility)
    except curses.error:
        pass


STATUS_COLOR: dict[str, int] = {
    "open":             _C_DIM,
    "claimed":          _C_CLAIMED,
    "in_progress":      _C_IN_PROGRESS,
    "blocked":          _C_BLOCKED,
    "review_requested": _C_REVIEW,
    "handoff_pending":  _C_HANDOFF,
    "done":             _C_DONE,
    "abandoned":        _C_DIM,
}

STATUS_ICON: dict[str, str] = {
    "open":             "◌",
    "claimed":          "⊙",
    "in_progress":      "●",
    "blocked":          "⊘",
    "review_requested": "⊛",
    "handoff_pending":  "⇢",
    "done":             "✔",
    "abandoned":        "✘",
}

AGENT_COLOR: dict[str, int] = {
    "codex":  _C_CODEX,
    "claude": _C_CLAUDE,
    "cursor": _C_CURSOR,
    "gemini": _C_GEMINI,
}

MSG_TYPE_COLOR: dict[str, int] = {
    "handoff":         _C_HANDOFF,
    "review_request":  _C_REVIEW,
    "review_result":   _C_REVIEW,
    "blocker":         _C_BLOCKED,
    "note":            _C_DIM,
    "question":        _C_CLAIMED,
    "answer":          _C_DONE,
    "decision":        _C_IN_PROGRESS,
    "artifact_notice": _C_IN_PROGRESS,
}

KEYBINDINGS = (
    "tab=switch  j/k=move  n=new task  c=claim  t=status  "
    "m=message  d=delegate  f=handoff  s=session  h=heartbeat  x=end session  a=agent  r=refresh  q=quit"
)


class RexTui:
    def __init__(self, root: Path):
        self.root = root
        self.state = load_dashboard_state(root)
        self.selected_task = 0
        self.selected_session = 0
        self.focus = "tasks"
        self.status = KEYBINDINGS
        self.has_color = False

    def _open_conn(self):
        from lex.db import connect, initialize_database, resolve_paths
        paths = resolve_paths(str(self.root))
        conn = connect(paths.db_path)
        initialize_database(conn)
        return conn

    def run(self) -> None:
        curses.wrapper(self._main)

    def _main(self, stdscr) -> None:
        self.has_color = _init_colors()
        _safe_curs_set(0)
        stdscr.nodelay(False)
        stdscr.keypad(True)
        while True:
            self.state = load_dashboard_state(self.root)
            self.selected_task = min(self.selected_task, max(len(self.state.tasks) - 1, 0))
            self.selected_session = min(self.selected_session, max(len(self.state.sessions) - 1, 0))
            self._render(stdscr)
            ch = stdscr.getch()
            if ch in (ord("q"), 27):
                return
            if ch == 9:
                self.focus = "sessions" if self.focus == "tasks" else "tasks"
                self.status = f"focus → {self.focus}"
            elif ch in (ord("j"), curses.KEY_DOWN):
                self._move_selection(1)
                self.status = KEYBINDINGS
            elif ch in (ord("k"), curses.KEY_UP):
                self._move_selection(-1)
                self.status = KEYBINDINGS
            elif ch == ord("a"):
                self._register_agent(stdscr)
            elif ch == ord("r"):
                self.status = "refreshed"
            elif ch == ord("n"):
                self._create_task(stdscr)
            elif ch == ord("c"):
                self._claim_task(stdscr)
            elif ch == ord("s"):
                self._start_session(stdscr)
            elif ch == ord("h"):
                self._heartbeat_session(stdscr)
            elif ch == ord("x"):
                self._end_session(stdscr)
            elif ch == ord("m"):
                self._send_message(stdscr)
            elif ch == ord("d"):
                self._delegate_task(stdscr)
            elif ch == ord("f"):
                self._handoff_task(stdscr)
            elif ch == ord("t"):
                self._update_status(stdscr)

    def _cp(self, pair_id: int, bold: bool = False) -> int:
        if not self.has_color:
            return curses.A_BOLD if bold else curses.A_NORMAL
        attr = curses.color_pair(pair_id)
        if bold:
            attr |= curses.A_BOLD
        return attr

    def _render(self, stdscr) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        left_w = max(44, width // 2)
        right_w = width - left_w

        # Header bar
        header_attr = self._cp(_C_STATUS_BAR)
        stdscr.attron(header_attr)
        stdscr.hline(0, 0, " ", width)
        root_str = f" lex  {self.root} "
        summary = (
            f"  agents={len(self.state.agents)}"
            f"  sessions={len(self.state.sessions)}"
            f"  tasks={len(self.state.tasks)}"
            f"  messages={len(self.state.inbox)}"
            f"  stale={self.state.summary['stale_sessions']}"
            f"  blocked={self.state.summary['blocked_tasks']}"
            f"  review={self.state.summary['review_needed_tasks']}"
            f"  risky={self.state.summary['risky_tasks']}"
        )
        stdscr.addnstr(0, 0, root_str, max(width - len(summary) - 1, 0))
        if len(root_str) + len(summary) < width:
            stdscr.addnstr(0, width - len(summary) - 1, summary, len(summary))
        stdscr.attroff(header_attr)

        split_y = 1
        tasks_h = height - split_y - 2
        detail_h = max(10, tasks_h // 2)
        lower_h = tasks_h - detail_h
        lower_half = max(4, lower_h // 2)

        self._draw_tasks(stdscr, split_y, 0, tasks_h, left_w)
        self._draw_task_detail(stdscr, split_y, left_w, detail_h, right_w)
        self._draw_sessions(stdscr, split_y + detail_h, left_w, lower_half, right_w)
        self._draw_events(stdscr, split_y + detail_h + lower_half, left_w, lower_h - lower_half, right_w)

        # Status bar
        status_attr = self._cp(_C_STATUS_BAR)
        stdscr.attron(status_attr)
        stdscr.hline(height - 2, 0, " ", width)
        stdscr.addnstr(height - 2, 1, self.status, width - 2)
        stdscr.attroff(status_attr)
        stdscr.refresh()

    def _draw_tasks(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        is_focused = self.focus == "tasks"
        title = " tasks ◀ " if is_focused else " tasks "
        self._draw_box(stdscr, y, x, h, w, title, focused=is_focused)
        visible = max(h - 2, 1)
        for idx, task in enumerate(self.state.tasks[:visible]):
            is_sel = idx == self.selected_task
            status = task["status"]
            icon = STATUS_ICON.get(status, "?")
            if task.get("lease_expiring"):
                icon = "!"
            owner = task["owner_name"] or "-"
            label = f" {icon} {task['id']:>3}  {status:<15} {owner:<20} {task['title']}"
            if is_sel:
                attr = self._cp(_C_HIGHLIGHT)
                stdscr.addnstr(y + 1 + idx, x + 1, label, w - 2, attr)
                # fill rest of line with highlight
                filled = min(len(label), w - 2)
                if filled < w - 2:
                    stdscr.addnstr(y + 1 + idx, x + 1 + filled, " " * (w - 2 - filled), w - 2 - filled, attr)
            else:
                status_color = STATUS_COLOR.get(status, _C_DIM)
                stdscr.addnstr(y + 1 + idx, x + 1, f" {icon} ", 3, self._cp(status_color, bold=True))
                stdscr.addnstr(y + 1 + idx, x + 4, f"{task['id']:>3}  {status:<15}", 20)
                agent_color = AGENT_COLOR.get(task.get("owner_kind", ""), _C_DIM)
                owner_role = task.get("owner_role") or ""
                owner_specialty = task.get("owner_specialty") or ""
                role_label = owner_role if not owner_specialty else f"{owner_role}/{owner_specialty}"
                owner_text = owner if not role_label else f"{owner} ({role_label})"
                stdscr.addnstr(y + 1 + idx, x + 24, f"{owner_text:<20}", 20, self._cp(agent_color))
                stdscr.addnstr(y + 1 + idx, x + 44, task["title"], max(w - 46, 1))

    def _draw_sessions(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        if h < 3:
            return
        is_focused = self.focus == "sessions"
        title = " active sessions ◀ " if is_focused else " active sessions "
        self._draw_box(stdscr, y, x, h, w, title, focused=is_focused)
        for idx, session in enumerate(self.state.sessions[: max(h - 2, 1)]):
            is_sel = is_focused and idx == self.selected_session
            label_text = session["label"] or "-"
            kind = session.get("agent_kind", "")
            agent_color = AGENT_COLOR.get(kind, _C_DIM)
            has_contract_issue = (
                session.get("bootstrap_pending")
                or session.get("pending_required_actions")
                or session.get("unacked_watches")
                or session.get("role_drift_events")
            )
            stale_prefix = "!" if session.get("is_stale") or has_contract_issue else " "
            role_text = session.get("agent_role") or "-"
            specialty_text = session.get("agent_specialty") or ""
            role_label = role_text if not specialty_text else f"{role_text}/{specialty_text}"
            compliance = []
            if session.get("bootstrap_pending"):
                compliance.append("bootstrap")
            if session.get("pending_required_actions"):
                compliance.append("actions")
            if session.get("unacked_watches"):
                compliance.append("acks")
            if session.get("role_drift_events"):
                compliance.append("drift")
            compliance_text = ",".join(compliance) if compliance else "-"
            line = f"{stale_prefix}{session['id']:>3}  {session['agent_name']:<22}  {role_label:<12}  {label_text:<14}  {compliance_text:<16}  {session['heartbeat_at']}"
            if is_sel:
                attr = self._cp(_C_HIGHLIGHT)
                stdscr.addnstr(y + 1 + idx, x + 1, line, w - 2, attr)
            else:
                stdscr.addnstr(y + 1 + idx, x + 1, stale_prefix, 1, self._cp(_C_BLOCKED if session.get("is_stale") or has_contract_issue else _C_DIM, bold=session.get("is_stale") or has_contract_issue))
                stdscr.addnstr(y + 1 + idx, x + 2, f"{session['id']:>3}  ", 5)
                stdscr.addnstr(y + 1 + idx, x + 7, f"{session['agent_name']:<22}", 22, self._cp(agent_color))
                rest = f"  {role_label:<12}  {label_text:<14}  {compliance_text:<16}  {session['heartbeat_at']}"
                stdscr.addnstr(y + 1 + idx, x + 29, rest, max(w - 31, 1), self._cp(_C_DIM))

    def _draw_task_detail(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        if h < 3:
            return
        self._draw_box(stdscr, y, x, h, w, " task detail ")
        if not self.state.tasks:
            stdscr.addnstr(y + 1, x + 1, "  No tasks.", w - 2, self._cp(_C_DIM))
            return
        task_id = self.state.tasks[self.selected_task]["id"]
        detail = self.state.task_details.get(task_id, {})
        row = y + 1
        status = detail.get("status", "")
        icon = STATUS_ICON.get(status, "?")
        status_color = STATUS_COLOR.get(status, _C_DIM)

        title_str = f" #{task_id} {detail.get('title', '')}"
        stdscr.addnstr(row, x + 1, title_str, w - 2, self._cp(_C_HEADER, bold=True))
        row += 1
        if row >= y + h - 1:
            return

        stdscr.addnstr(row, x + 1, f" {icon} ", 3, self._cp(status_color, bold=True))
        stdscr.addnstr(row, x + 4, f"{status}", len(status))
        owner = detail.get("owner_name") or "-"
        owner_role = detail.get("owner_role") or "-"
        owner_specialty = detail.get("owner_specialty") or ""
        role_label = owner_role if not owner_specialty else f"{owner_role}/{owner_specialty}"
        delegation = detail.get("delegation_mode") or "-"
        meta = f"   owner={owner} ({role_label or '-'})   delegation={delegation}"
        stdscr.addnstr(row, x + 4 + len(status), meta, max(w - 5 - len(status), 1), self._cp(_C_DIM))
        row += 1

        if detail.get("lease_expires_at") and row < y + h - 2:
            lease_text = f"  lease {detail['lease_expires_at']}"
            lease_color = _C_BLOCKED if detail.get("lease_expiring") else _C_DIM
            if detail.get("lease_expiring"):
                lease_text += "  expiring soon"
            stdscr.addnstr(row, x + 1, lease_text, w - 2, self._cp(lease_color))
            row += 1

        if detail.get("description") and row < y + h - 2:
            desc = detail["description"][:w - 4]
            stdscr.addnstr(row, x + 2, desc, w - 4, self._cp(_C_DIM))
            row += 1

        children = detail.get("children", [])
        if children and row < y + h - 2:
            stdscr.addnstr(row, x + 2, "children", 8, self._cp(_C_DIM))
            row += 1
            for child in children[:3]:
                if row >= y + h - 2:
                    break
                child_icon = STATUS_ICON.get(child["status"], "?")
                child_color = STATUS_COLOR.get(child["status"], _C_DIM)
                stdscr.addnstr(row, x + 2, f"  {child_icon} ", 4, self._cp(child_color))
                stdscr.addnstr(row, x + 6, f"#{child['id']} {child['title']}", max(w - 8, 1))
                row += 1

        messages = detail.get("messages", [])
        if messages and row < y + h - 2:
            stdscr.addnstr(row, x + 2, "messages", 8, self._cp(_C_DIM))
            row += 1
            for msg in messages[-4:]:
                if row >= y + h - 2:
                    break
                msg_color = MSG_TYPE_COLOR.get(msg["type"], _C_DIM)
                prefix = f"  {msg['type']:<16} {msg['from_name']:<20}"
                stdscr.addnstr(row, x + 2, f"  {msg['type']:<16}", 18, self._cp(msg_color))
                stdscr.addnstr(row, x + 20, f"{msg['from_name']:<18}", 18, self._cp(_C_DIM))
                body_start = x + 38
                if body_start < x + w - 2:
                    stdscr.addnstr(row, body_start, msg["body"], max(x + w - 2 - body_start, 1))
                row += 1

    def _draw_events(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        if h < 3:
            return
        self._draw_box(stdscr, y, x, h, w, " events ")
        for idx, event in enumerate(self.state.events[: max(h - 2, 1)]):
            task_str = f"#{event['task_id']}" if event["task_id"] else "   "
            agent_str = event["agent_name"] or "-"
            stdscr.addnstr(y + 1 + idx, x + 1, f"  {task_str:<5}", 8, self._cp(_C_DIM))
            stdscr.addnstr(y + 1 + idx, x + 9, f"{agent_str:<20}", 20, self._cp(_C_DIM))
            stdscr.addnstr(y + 1 + idx, x + 29, event["event_type"], max(w - 31, 1), self._cp(_C_IN_PROGRESS))

    def _draw_box(self, stdscr, y: int, x: int, h: int, w: int, title: str, *, focused: bool = False) -> None:
        if h < 3 or w < 4:
            return
        border_attr = self._cp(_C_FOCUS_BORDER if focused else _C_BORDER)
        stdscr.attron(border_attr)
        stdscr.addch(y, x, curses.ACS_ULCORNER)
        stdscr.hline(y, x + 1, curses.ACS_HLINE, w - 2)
        stdscr.addch(y, x + w - 1, curses.ACS_URCORNER)
        for row in range(y + 1, y + h - 1):
            stdscr.addch(row, x, curses.ACS_VLINE)
            stdscr.addch(row, x + w - 1, curses.ACS_VLINE)
        stdscr.addch(y + h - 1, x, curses.ACS_LLCORNER)
        stdscr.hline(y + h - 1, x + 1, curses.ACS_HLINE, w - 2)
        stdscr.addch(y + h - 1, x + w - 1, curses.ACS_LRCORNER)
        stdscr.attroff(border_attr)
        if title and len(title) <= w - 4:
            title_attr = self._cp(_C_FOCUS_BORDER if focused else _C_HEADER, bold=focused)
            stdscr.addnstr(y, x + 2, title, w - 4, title_attr)

    def _prompt(self, stdscr, label: str) -> str:
        height, width = stdscr.getmaxyx()
        prompt = f"  {label}: "
        curses.echo()
        _safe_curs_set(1)
        status_attr = self._cp(_C_STATUS_BAR)
        stdscr.attron(status_attr)
        stdscr.hline(height - 2, 0, " ", width)
        stdscr.addnstr(height - 2, 0, prompt, width)
        stdscr.attroff(status_attr)
        stdscr.refresh()
        value = stdscr.getstr(height - 2, len(prompt), max(width - len(prompt) - 1, 1))
        curses.noecho()
        _safe_curs_set(0)
        return value.decode("utf-8").strip()

    def _prompt_with_default(self, stdscr, label: str, default: str) -> str:
        value = self._prompt(stdscr, f"{label} [{default}]")
        return value or default

    def _prompt_choice(self, stdscr, label: str, options: list[str], default: str) -> str:
        options_text = "/".join(options)
        while True:
            value = self._prompt_with_default(stdscr, f"{label} ({options_text})", default)
            if value in options:
                return value
            self.status = f"invalid: {value!r} — choose from {options_text}"

    def _move_selection(self, delta: int) -> None:
        if self.focus == "tasks":
            self.selected_task = min(max(self.selected_task + delta, 0), max(len(self.state.tasks) - 1, 0))
        else:
            self.selected_session = min(max(self.selected_session + delta, 0), max(len(self.state.sessions) - 1, 0))

    def _create_task(self, stdscr) -> None:
        from lex.services.task_service import create_task

        title = self._prompt(stdscr, "task title")
        if not title:
            self.status = "task creation cancelled"
            return
        try:
            result = create_task(self._open_conn(), title=title)
            self.status = f"✔ created task {result.task_id}: {title}"
        except ValueError as exc:
            self.status = f"error: {exc}"

    def _claim_task(self, stdscr) -> None:
        from lex.services.task_service import claim_task

        if not self.state.tasks:
            self.status = "no tasks to claim"
            return
        task = self.state.tasks[self.selected_task]
        default_agent = task["owner_name"] or (self.state.sessions[0]["agent_name"] if self.state.sessions else "")
        agent = self._prompt_with_default(stdscr, "agent name", default_agent)
        if not agent:
            self.status = "claim cancelled"
            return
        try:
            claim_task(self._open_conn(), task_id=task["id"], agent=agent)
            self.status = f"✔ claimed task {task['id']} for {agent}"
        except ValueError as exc:
            self.status = f"error: {exc}"

    def _start_session(self, stdscr) -> None:
        from lex.services.session_service import start_session

        default_agent = self.state.agents[0]["name"] if self.state.agents else ""
        agent = self._prompt_with_default(stdscr, "agent name", default_agent)
        if not agent:
            self.status = "session start cancelled"
            return
        label = self._prompt_with_default(stdscr, "session label", "primary")
        try:
            result = start_session(self._open_conn(), agent=agent, label=label, cwd=str(self.root))
            self.status = f"✔ started session {result.session_id} for {agent}"
        except ValueError as exc:
            self.status = f"error: {exc}"

    def _register_agent(self, stdscr) -> None:
        from lex.services.agent_service import identify_agent, register_agent

        kind = self._prompt_choice(stdscr, "agent kind", ["codex", "claude", "cursor", "gemini"], "codex")
        role = self._prompt_choice(stdscr, "agent role", ["dev", "pm", "auditor", "infra"], "dev")
        specialty = self._prompt(stdscr, "agent specialty (optional)")
        name = self._prompt(stdscr, "agent name (blank to generate)")
        try:
            if name:
                result = register_agent(
                    self._open_conn(), name=name, kind=kind,
                    role=role or None, specialty=specialty or None,
                )
                self.status = f"✔ registered {result.name}"
            else:
                result = identify_agent(
                    self._open_conn(), kind=kind,
                    role=role or None, specialty=specialty or None,
                )
                self.status = f"✔ identified {result.name}"
        except ValueError as exc:
            self.status = f"error: {exc}"

    def _heartbeat_session(self, stdscr) -> None:
        from lex.services.session_service import send_heartbeat

        if not self.state.sessions:
            self.status = "no active sessions"
            return
        session_id = self.state.sessions[self.selected_session]["id"]
        try:
            send_heartbeat(self._open_conn(), session_id=int(session_id))
            self.status = f"✔ heartbeat for session {session_id}"
        except ValueError as exc:
            self.status = f"error: {exc}"

    def _end_session(self, stdscr) -> None:
        from lex.services.session_service import end_session

        if not self.state.sessions:
            self.status = "no active sessions"
            return
        session_id = self.state.sessions[self.selected_session]["id"]
        try:
            end_session(self._open_conn(), session_id=int(session_id))
            self.status = f"✔ ended session {session_id}"
        except ValueError as exc:
            self.status = f"error: {exc}"

    def _send_message(self, stdscr) -> None:
        from lex.services.msg_service import send_message

        if not self.state.tasks:
            self.status = "no task selected"
            return
        task = self.state.tasks[self.selected_task]
        default_from = task["owner_name"] or (self.state.sessions[0]["agent_name"] if self.state.sessions else "")
        from_agent = self._prompt_with_default(stdscr, "from agent", default_from)
        if not from_agent:
            self.status = "message cancelled"
            return
        to_agent = self._prompt(stdscr, "to agent (optional)")
        message_type = self._prompt_choice(
            stdscr,
            "type",
            ["note", "question", "answer", "blocker", "handoff",
             "review_request", "review_result", "decision", "artifact_notice"],
            "note",
        )
        body = self._prompt(stdscr, "message body")
        if not body:
            self.status = "message cancelled"
            return
        try:
            send_message(
                self._open_conn(),
                task_id=task["id"], from_agent=from_agent,
                to_agent=to_agent or None, message_type=message_type, body=body,
            )
            self.status = f"✔ sent {message_type} on task {task['id']}"
        except ValueError as exc:
            self.status = f"error: {exc}"

    def _update_status(self, stdscr) -> None:
        from lex.services.task_service import update_task_status

        if not self.state.tasks:
            self.status = "no task selected"
            return
        task = self.state.tasks[self.selected_task]
        default_agent = task["owner_name"] or (self.state.sessions[0]["agent_name"] if self.state.sessions else "")
        agent = self._prompt_with_default(stdscr, "agent name", default_agent)
        if not agent:
            self.status = "status update cancelled"
            return
        status = self._prompt_choice(
            stdscr,
            "new status",
            ["open", "claimed", "in_progress", "blocked",
             "review_requested", "handoff_pending", "done", "abandoned"],
            task["status"],
        )
        try:
            update_task_status(self._open_conn(), task_id=task["id"], agent=agent, status=status)
            self.status = f"✔ task {task['id']} → {status}"
        except ValueError as exc:
            self.status = f"error: {exc}"

    def _delegate_task(self, stdscr) -> None:
        from lex.services.task_service import delegate_task

        if not self.state.tasks:
            self.status = "no task selected"
            return
        task = self.state.tasks[self.selected_task]
        owner = self._prompt_with_default(stdscr, "owner agent", task["owner_name"] or "")
        assignee = self._prompt(stdscr, "assignee agent")
        title = self._prompt(stdscr, "child task title")
        body = self._prompt(stdscr, "handoff body")
        if not owner or not assignee or not title or not body:
            self.status = "delegate cancelled"
            return
        try:
            result = delegate_task(
                self._open_conn(),
                parent_task_id=task["id"], owner_agent=owner,
                assignee_agent=assignee, title=title, body=body,
            )
            self.status = f"✔ delegated child task {result.child_task_id} from #{task['id']} to {assignee}"
        except ValueError as exc:
            self.status = f"error: {exc}"

    def _handoff_task(self, stdscr) -> None:
        from lex.services.task_service import handoff_task

        if not self.state.tasks:
            self.status = "no task selected"
            return
        task = self.state.tasks[self.selected_task]
        from_agent = self._prompt_with_default(stdscr, "from agent", task["owner_name"] or "")
        to_agent = self._prompt(stdscr, "to agent")
        body = self._prompt(stdscr, "handoff body")
        if not from_agent or not to_agent or not body:
            self.status = "handoff cancelled"
            return
        try:
            handoff_task(
                self._open_conn(),
                task_id=task["id"], from_agent=from_agent, to_agent=to_agent, body=body,
            )
            self.status = f"✔ handed off task #{task['id']} to {to_agent}"
        except ValueError as exc:
            self.status = f"error: {exc}"


def run_tui(root: Path) -> None:
    RexTui(root).run()
