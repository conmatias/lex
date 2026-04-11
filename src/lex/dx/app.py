from __future__ import annotations

import argparse
import curses
import difflib
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from lex.coordination import get_agent, get_task
from lex.dashboard import DashboardState, load_dashboard_state
from lex.db import connect, ensure_workspace, initialize_database, log_event


@dataclass(frozen=True)
class DxFileItem:
    path: str
    state: str
    task_id: int | None
    task_title: str | None
    last_activity_at: str | None
    conflict: bool


@dataclass(frozen=True)
class DxAgentItem:
    agent_id: int
    agent_name: str
    agent_kind: str
    agent_role: str | None
    roster_state: str
    session_id: int | None
    task_id: int | None
    task_title: str | None
    task_status: str | None
    last_activity_at: str | None
    changed_file_count: int
    claimed_path_count: int
    files: tuple[DxFileItem, ...]


@dataclass(frozen=True)
class DxTab:
    path: str
    agent_id: int
    agent_name: str
    task_id: int | None
    task_title: str | None
    state: str
    base_ref: str | None


@dataclass(frozen=True)
class DxAction:
    key: str
    label: str


@dataclass(frozen=True)
class DxView:
    root: Path
    agents: tuple[DxAgentItem, ...]


def _parse_json_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [str(item) for item in data]
    return []


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _resolve_roster_state(row: dict) -> str:
    last_activity = _parse_timestamp(row.get("last_activity_at"))
    if row.get("is_stale") and row.get("task_id") is not None:
        return "stale"
    if row.get("task_status") == "blocked":
        return "blocked"
    if row.get("session_id") is None:
        return "stale" if row.get("task_id") is not None else "idle"
    if row.get("task_id") is None:
        return "idle"
    if last_activity is not None and last_activity < datetime.now(last_activity.tzinfo) - timedelta(minutes=10):
        return "waiting"
    return "active"


def build_dx_view(root: Path, state: DashboardState | None = None) -> DxView:
    state = state or load_dashboard_state(root)
    session_by_agent = {session["agent_name"]: session for session in state.sessions}
    task_by_id = state.task_details

    all_claimed_paths: dict[str, set[int]] = {}
    for task in task_by_id.values():
        for path in _parse_json_list(task.get("claimed_paths_json")):
            all_claimed_paths.setdefault(path, set()).add(task["id"])

    agents: list[DxAgentItem] = []
    for row in state.dx_roster:
        session = session_by_agent.get(row["agent_name"], {})
        task = task_by_id.get(row["task_id"]) if row.get("task_id") is not None else None
        claimed_paths = _parse_json_list(task.get("claimed_paths_json") if task else None)
        changed_paths = _parse_json_list(session.get("git_changed_files_json"))
        combined_paths = sorted(set(claimed_paths) | set(changed_paths))
        files: list[DxFileItem] = []
        for path in combined_paths:
            if path in changed_paths:
                file_state = "changed_unreviewed"
            elif path in claimed_paths:
                file_state = "claimed_only"
            else:
                file_state = "flagged"
            conflict = len(all_claimed_paths.get(path, set())) > 1
            if conflict:
                file_state = "conflicted"
            files.append(
                DxFileItem(
                    path=path,
                    state=file_state,
                    task_id=row.get("task_id"),
                    task_title=row.get("task_title"),
                    last_activity_at=row.get("last_activity_at"),
                    conflict=conflict,
                )
            )
        agents.append(
            DxAgentItem(
                agent_id=row["agent_id"],
                agent_name=row["agent_name"],
                agent_kind=row["agent_kind"],
                agent_role=row.get("agent_role"),
                roster_state=_resolve_roster_state(row),
                session_id=row.get("session_id"),
                task_id=row.get("task_id"),
                task_title=row.get("task_title"),
                task_status=row.get("task_status"),
                last_activity_at=row.get("last_activity_at"),
                changed_file_count=row.get("changed_file_count") or 0,
                claimed_path_count=row.get("claimed_path_count") or 0,
                files=tuple(files),
            )
        )
    return DxView(root=state.root, agents=tuple(agents))


def read_file_contents(root: Path, path: str) -> str:
    target = root / path
    try:
        return target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return f"Missing file on disk: {path}"
    except IsADirectoryError:
        return f"Directory claim: {path or '.'}"
    except UnicodeDecodeError:
        return f"Binary or non-UTF-8 file: {path}"


def build_diff(root: Path, base_ref: str | None, path: str) -> str:
    if not base_ref:
        return "No base ref available for diff generation."
    diff_commands = (
        ["git", "diff", f"{base_ref}...HEAD", "--", path],
        ["git", "diff", base_ref, "--", path],
    )
    diff = ""
    last_error = ""
    for cmd in diff_commands:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            last_error = proc.stderr.strip() or "git diff failed"
            continue
        diff = proc.stdout.strip()
        if diff:
            break
    if not diff and last_error:
        return f"Diff unavailable: {last_error}"
    return diff or f"No diff for {path} against {base_ref}."


def current_actions(focus: str, mode: str, selected_file: DxFileItem | None = None) -> tuple[DxAction, ...]:
    if mode == "quick_edit":
        return (
            DxAction("save", "Save"),
            DxAction("discard", "Discard"),
            DxAction("send_note", "Send Note"),
        )
    if focus == "roster":
        return (
            DxAction("message_task", "Message Task (m)"),
            DxAction("task_details", "Task Context (t)"),
            DxAction("priority", "Priority (p)"),
        )
    if focus == "files":
        actions: list[DxAction] = [DxAction("open_diff", "Open Diff (enter)")]
        if selected_file is not None and selected_file.conflict:
            actions.append(DxAction("flag_conflict", "Flag (g)"))
        else:
            actions.append(DxAction("task_details", "Task Context (t)"))
        actions.append(DxAction("priority", "Priority (p)"))
        return tuple(actions[:3])
    if focus == "tabs":
        return (
            DxAction("annotate", "Annotate (a)"),
            DxAction("task_details", "Task Context (t)"),
            DxAction("priority", "Priority (p)"),
        )
    return ()


def dx_send_message(root: Path, *, from_agent: str, task_id: int, body: str, subject: str = "dx intervention") -> None:
    paths = ensure_workspace(root)
    conn = connect(paths.db_path)
    try:
        initialize_database(conn)
        agent = get_agent(conn, from_agent)
        get_task(conn, task_id)
        conn.execute(
            """
            INSERT INTO messages (task_id, from_agent_id, type, subject, body)
            VALUES (?, ?, 'note', ?, ?)
            """,
            (task_id, agent["id"], subject, body),
        )
        log_event(
            conn,
            "message.sent",
            task_id=task_id,
            agent_id=agent["id"],
            payload={"type": "note", "to": None, "provenance": "dx"},
        )
        conn.commit()
    finally:
        conn.close()


def dx_log_annotation(root: Path, *, agent_name: str, task_id: int, path: str, note: str) -> None:
    paths = ensure_workspace(root)
    conn = connect(paths.db_path)
    try:
        initialize_database(conn)
        agent = get_agent(conn, agent_name)
        get_task(conn, task_id)
        log_event(
            conn,
            "dx.annotation",
            task_id=task_id,
            agent_id=agent["id"],
            payload={"hunk": path, "note": note, "provenance": "dx"},
        )
        conn.commit()
    finally:
        conn.close()


def dx_flag_file(root: Path, *, agent_name: str, task_id: int, path: str, reason: str) -> None:
    paths = ensure_workspace(root)
    conn = connect(paths.db_path)
    try:
        initialize_database(conn)
        agent = get_agent(conn, agent_name)
        get_task(conn, task_id)
        log_event(
            conn,
            "dx.flag",
            task_id=task_id,
            agent_id=agent["id"],
            payload={"file": path, "reason": reason, "provenance": "dx"},
        )
        conn.commit()
    finally:
        conn.close()


def dx_log_edit_event(
    root: Path,
    *,
    agent_name: str,
    task_id: int,
    path: str,
    diff_summary: str,
    session_id: int | None = None,
) -> None:
    paths = ensure_workspace(root)
    conn = connect(paths.db_path)
    try:
        initialize_database(conn)
        agent = get_agent(conn, agent_name)
        log_event(
            conn,
            "dx.edit",
            task_id=task_id,
            agent_id=agent["id"],
            session_id=session_id,
            payload={"file": path, "diff_summary": diff_summary, "provenance": "dx"},
        )
        conn.commit()
    finally:
        conn.close()


def dx_update_task_priority(root: Path, *, agent_name: str, task_id: int, priority: int) -> None:
    paths = ensure_workspace(root)
    conn = connect(paths.db_path)
    try:
        initialize_database(conn)
        agent = get_agent(conn, agent_name)
        task = get_task(conn, task_id)
        conn.execute(
            "UPDATE tasks SET priority = ? WHERE id = ?",
            (priority, task_id),
        )
        log_event(
            conn,
            "task.priority_changed",
            task_id=task_id,
            agent_id=agent["id"],
            payload={"from": task["priority"], "to": priority, "provenance": "dx"},
        )
        conn.commit()
    finally:
        conn.close()


def dx_update_task_status(root: Path, *, agent_name: str, task_id: int, status: str) -> None:
    paths = ensure_workspace(root)
    conn = connect(paths.db_path)
    try:
        initialize_database(conn)
        agent = get_agent(conn, agent_name)
        task = get_task(conn, task_id)
        if status == "done":
            conn.execute(
                "UPDATE tasks SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?",
                (status, task_id),
            )
        else:
            conn.execute(
                "UPDATE tasks SET status = ?, completed_at = NULL WHERE id = ?",
                (status, task_id),
            )
        log_event(
            conn,
            "task.status_changed",
            task_id=task_id,
            agent_id=agent["id"],
            payload={"from": task["status"], "to": status, "provenance": "dx"},
        )
        conn.commit()
    finally:
        conn.close()


VALID_TASK_STATES = (
    "open",
    "claimed",
    "in_progress",
    "blocked",
    "review_requested",
    "handoff_pending",
    "done",
    "abandoned",
)


class DxTui:
    def __init__(self, root: Path):
        self.root = root
        self.view = build_dx_view(root)
        self.focus = "roster"
        self.selected_agent = 0
        self.selected_file = 0
        self.selected_tab = 0
        self.tabs: list[DxTab] = []
        self.mode = "diff"
        self.status = "tab=switch  j/k=move  enter=open tab  d=diff  f=file  e=edit  m=message  a=annotate  g=flag  t=task  p=priority  s=status  x=close tab  r=refresh  q=quit"
        # quick-edit state
        self._qe_lines: list[str] = []
        self._qe_original_lines: list[str] = []
        self._qe_path: str | None = None
        self._qe_cursor: int = 0
        self._qe_agent_name: str | None = None
        self._qe_task_id: int | None = None
        self._qe_session_id: int | None = None
        self._qe_show_diff: bool = False

    def run(self) -> None:
        curses.wrapper(self._main)

    def _main(self, stdscr) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        while True:
            self._refresh()
            self._render(stdscr)
            ch = stdscr.getch()
            if self.mode == "quick_edit":
                if not self._handle_quick_edit_key(stdscr, ch):
                    return
                continue
            if ch in (ord("q"), 27):
                return
            if ch == 9:
                self.focus = {"roster": "files", "files": "tabs", "tabs": "roster"}[self.focus]
            elif ch in (ord("j"), curses.KEY_DOWN):
                self._move(1)
            elif ch in (ord("k"), curses.KEY_UP):
                self._move(-1)
            elif ch in (10, 13, curses.KEY_RIGHT):
                self._open_selected_file()
            elif ch == ord("d"):
                self.mode = "diff"
            elif ch == ord("f"):
                self.mode = "file"
            elif ch == ord("e"):
                self._enter_quick_edit(stdscr)
            elif ch == ord("x"):
                self._close_selected_tab()
            elif ch == ord("m"):
                self._message_task(stdscr)
            elif ch == ord("a"):
                self._annotate_current(stdscr)
            elif ch == ord("g"):
                self._flag_current()
            elif ch == ord("t"):
                self._show_task_details(stdscr)
            elif ch == ord("p"):
                self._update_task_priority(stdscr)
            elif ch == ord("s"):
                self._update_task_status(stdscr)
            elif ch == ord("r"):
                self._refresh(force=True)

    def _refresh(self, force: bool = False) -> None:
        self.view = build_dx_view(self.root)
        self.selected_agent = min(self.selected_agent, max(len(self.view.agents) - 1, 0))
        files = self._selected_files()
        self.selected_file = min(self.selected_file, max(len(files) - 1, 0))
        self.selected_tab = min(self.selected_tab, max(len(self.tabs) - 1, 0))

    def _selected_agent_item(self) -> DxAgentItem | None:
        if not self.view.agents:
            return None
        return self.view.agents[self.selected_agent]

    def _selected_files(self) -> tuple[DxFileItem, ...]:
        agent = self._selected_agent_item()
        return agent.files if agent else ()

    def _selected_session_base_ref(self, agent_name: str) -> str | None:
        state = load_dashboard_state(self.root)
        for session in state.sessions:
            if session["agent_name"] == agent_name:
                return session.get("git_base_ref")
        return None

    def _move(self, delta: int) -> None:
        if self.focus == "roster":
            self.selected_agent = max(0, min(self.selected_agent + delta, len(self.view.agents) - 1))
            self.selected_file = 0
        elif self.focus == "files":
            self.selected_file = max(0, min(self.selected_file + delta, len(self._selected_files()) - 1))
        elif self.focus == "tabs":
            self.selected_tab = max(0, min(self.selected_tab + delta, len(self.tabs) - 1))

    def _open_selected_file(self) -> None:
        if self.focus == "roster":
            self.focus = "files"
            self.status = "select a file claim to open"
            return
        agent = self._selected_agent_item()
        files = self._selected_files()
        if not agent or not files:
            self.status = "no file selected"
            return
        file_item = files[self.selected_file]
        tab = DxTab(
            path=file_item.path,
            agent_id=agent.agent_id,
            agent_name=agent.agent_name,
            task_id=file_item.task_id,
            task_title=file_item.task_title,
            state=file_item.state,
            base_ref=self._selected_session_base_ref(agent.agent_name),
        )
        try:
            self.selected_tab = self.tabs.index(tab)
        except ValueError:
            self.tabs.append(tab)
            self.selected_tab = len(self.tabs) - 1
        self.focus = "tabs"

    def _close_selected_tab(self) -> None:
        if not self.tabs:
            self.status = "no tab selected"
            return
        self.tabs.pop(self.selected_tab)
        self.selected_tab = min(self.selected_tab, max(len(self.tabs) - 1, 0))

    def _selected_task_context(self) -> tuple[str, int] | None:
        if self.focus == "tabs" and self.tabs:
            tab = self.tabs[self.selected_tab]
            if tab.task_id is not None:
                return tab.agent_name, tab.task_id
        agent = self._selected_agent_item()
        if agent and agent.task_id is not None:
            return agent.agent_name, agent.task_id
        return None

    def _selected_path(self) -> str | None:
        if self.focus == "tabs" and self.tabs:
            return self.tabs[self.selected_tab].path
        files = self._selected_files()
        if files:
            return files[self.selected_file].path
        return None

    def _prompt(self, stdscr, prompt: str, prefill: str = "") -> str:
        height, width = stdscr.getmaxyx()
        stdscr.move(height - 1, 0)
        stdscr.clrtoeol()
        display = prompt + prefill
        stdscr.addnstr(height - 1, 0, display, width - 1, curses.A_REVERSE)
        curses.echo()
        curses.curs_set(1)
        try:
            col = min(len(prompt), width - 2)
            # We want the cursor to start at the end of prefill if possible,
            # but curses.getstr doesn't easily support prefilling the input buffer.
            # For now, we'll just let the user type.
            raw = stdscr.getstr(height - 1, col, max(width - col - 1, 1))
        finally:
            curses.noecho()
            try:
                curses.curs_set(0)
            except curses.error:
                pass
        typed = raw.decode("utf-8").strip()
        return typed if typed else prefill

    def _message_task(self, stdscr) -> None:
        context = self._selected_task_context()
        if context is None:
            self.status = "no task context selected"
            return
        agent_name, task_id = context
        body = self._prompt(stdscr, "dx note: ")
        if not body:
            self.status = "message cancelled"
            return
        dx_send_message(self.root, from_agent=agent_name, task_id=task_id, body=body)
        self.status = f"sent task note on #{task_id}"

    def _annotate_current(self, stdscr) -> None:
        context = self._selected_task_context()
        path = self._selected_path()
        if context is None or path is None:
            self.status = "no file/task context selected"
            return
        agent_name, task_id = context
        note = self._prompt(stdscr, "annotation: ")
        if not note:
            self.status = "annotation cancelled"
            return
        dx_log_annotation(self.root, agent_name=agent_name, task_id=task_id, path=path, note=note)
        self.status = f"annotated {path}"

    def _flag_current(self) -> None:
        context = self._selected_task_context()
        path = self._selected_path()
        if context is None or path is None:
            self.status = "no file/task context selected"
            return
        agent_name, task_id = context
        dx_flag_file(self.root, agent_name=agent_name, task_id=task_id, path=path, reason="needs_review")
        self.status = f"flagged {path} for review"

    def _update_task_priority(self, stdscr) -> None:
        context = self._selected_task_context()
        if context is None:
            self.status = "no task context selected"
            return
        agent_name, task_id = context
        raw = self._prompt(stdscr, f"priority for #{task_id} (1-4): ")
        if not raw:
            self.status = "priority update cancelled"
            return
        try:
            priority = int(raw)
            if priority < 1 or priority > 4:
                raise ValueError()
        except ValueError:
            self.status = "invalid priority (must be 1-4)"
            return
        dx_update_task_priority(self.root, agent_name=agent_name, task_id=task_id, priority=priority)
        self.status = f"task #{task_id} priority -> p{priority}"

    def _update_task_status(self, stdscr) -> None:
        context = self._selected_task_context()
        if context is None:
            self.status = "no task context selected"
            return
        agent_name, task_id = context
        status = self._prompt(stdscr, f"status for #{task_id}: ")
        if not status:
            self.status = "status update cancelled"
            return
        if status not in VALID_TASK_STATES:
            self.status = f"invalid status: {status}"
            return
        dx_update_task_status(self.root, agent_name=agent_name, task_id=task_id, status=status)
        self.status = f"task #{task_id} status -> {status}"

    def _show_task_details(self, stdscr) -> None:
        context = self._selected_task_context()
        if context is None:
            self.status = "no task context selected"
            return
        agent_name, task_id = context
        state = load_dashboard_state(self.root)
        task = state.task_details.get(task_id)
        if not task:
            self.status = f"task #{task_id} details not found in dashboard"
            return

        height, width = stdscr.getmaxyx()
        win_h, win_w = height - 4, width - 4
        win = stdscr.derwin(win_h, win_w, 2, 2)
        win.erase()
        win.box()
        win.addnstr(0, 2, f" Task #{task_id} Context ", win_w - 4, curses.A_BOLD)

        lines = [
            f"Title:       {task['title']}",
            f"Status:      {task['status']}",
            f"Priority:    p{task['priority']}",
            f"Owner:       {task.get('owner_name', '?')} ({task.get('owner_role', '?')})",
            "",
            "Description:",
            *(task.get("description") or "No description").splitlines(),
            "",
            "Messages:",
        ]
        for msg in task.get("messages", []):
            lines.append(f"  [{msg['created_at']}] {msg['from_name']}: {msg['subject'] or ''}")
            lines.append(f"    {msg['body'][:win_w - 8]}")

        for idx, line in enumerate(lines[:win_h - 2]):
            win.addnstr(idx + 1, 2, line, win_w - 4)

        win.addnstr(win_h - 1, 2, " press any key to close ", win_w - 4, curses.A_REVERSE)
        win.refresh()
        stdscr.getch()
        self.status = f"viewed task #{task_id} details"

    # ── quick edit ────────────────────────────────────────────────────────────

    def _enter_quick_edit(self, stdscr) -> None:
        path = self._selected_path()
        if path is None:
            self.status = "no file selected"
            return
        target = self.root / path
        if target.is_dir():
            self.status = f"cannot quick-edit directory claim: {path or '.'}"
            return
        try:
            content = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            self.status = f"file not found: {path}"
            return
        except UnicodeDecodeError:
            self.status = f"binary or non-UTF-8 file: {path}"
            return
        context = self._selected_task_context()
        agent = self._selected_agent_item()
        self._qe_lines = content.splitlines(keepends=False)
        self._qe_original_lines = list(self._qe_lines)
        self._qe_path = path
        self._qe_cursor = 0
        self._qe_show_diff = False
        self._qe_agent_name = context[0] if context else (agent.agent_name if agent else None)
        self._qe_task_id = context[1] if context else (agent.task_id if agent else None)
        self._qe_session_id = None
        self.mode = "quick_edit"
        self.status = "j/k=move  enter=edit line  d=diff preview  s=save  x=discard  m=send note"

    def _qe_diff_preview(self) -> str:
        if not self._qe_path:
            return "No file open."
        original = [l + "\n" for l in self._qe_original_lines]
        current = [l + "\n" for l in self._qe_lines]
        diff = list(difflib.unified_diff(
            original, current,
            fromfile=f"a/{self._qe_path}",
            tofile=f"b/{self._qe_path}",
        ))
        return "".join(diff) if diff else "No changes."

    def _qe_exit(self) -> None:
        self._qe_lines = []
        self._qe_original_lines = []
        self._qe_path = None
        self._qe_agent_name = None
        self._qe_task_id = None
        self._qe_session_id = None
        self._qe_show_diff = False
        self.mode = "diff"
        self.status = "tab=switch  j/k=move  enter=open tab  d=diff  f=file  e=edit  m=message  a=annotate  g=flag  x=close tab  r=refresh  q=quit"

    def _qe_save(self) -> None:
        if not self._qe_path:
            return
        path = self._qe_path
        target = self.root / path
        content = "\n".join(self._qe_lines)
        if self._qe_lines:
            content += "\n"
        try:
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            self.status = f"save failed: {exc}  (s=retry  x=discard)"
            return
        original = "\n".join(self._qe_original_lines)
        diff_lines = list(difflib.unified_diff(original.splitlines(), content.splitlines()))
        diff_summary = f"{len(diff_lines)} diff lines"
        if self._qe_agent_name and self._qe_task_id is not None:
            try:
                dx_log_edit_event(
                    self.root,
                    agent_name=self._qe_agent_name,
                    task_id=self._qe_task_id,
                    path=path,
                    diff_summary=diff_summary,
                    session_id=self._qe_session_id,
                )
            except Exception:
                pass
        self.status = f"saved {path} ({diff_summary})"
        self._qe_exit()

    def _handle_quick_edit_key(self, stdscr, ch: int) -> bool:
        """Handle a keypress in quick_edit mode. Returns False to quit the TUI."""
        if ch in (ord("j"), curses.KEY_DOWN):
            self._qe_cursor = min(self._qe_cursor + 1, max(len(self._qe_lines) - 1, 0))
        elif ch in (ord("k"), curses.KEY_UP):
            self._qe_cursor = max(self._qe_cursor - 1, 0)
        elif ch in (10, 13):
            # edit current line via prompt
            if self._qe_lines:
                current_line = self._qe_lines[self._qe_cursor]
                new_line = self._prompt(stdscr, f"line {self._qe_cursor + 1}: ", prefill=current_line)
                self._qe_lines[self._qe_cursor] = new_line
        elif ch == ord("d"):
            self._qe_show_diff = not self._qe_show_diff
        elif ch == ord("s"):
            self._qe_save()
        elif ch in (ord("x"), 27):
            self._qe_exit()
        elif ch == ord("m"):
            context = (self._qe_agent_name, self._qe_task_id) if self._qe_agent_name and self._qe_task_id is not None else None
            if context:
                body = self._prompt(stdscr, "dx note: ")
                if body:
                    dx_send_message(self.root, from_agent=context[0], task_id=context[1], body=body)
            self._qe_exit()
        elif ch == ord("q"):
            return False
        return True

    def _render(self, stdscr) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        roster_w = max(28, width // 4)
        files_w = max(34, width // 4)
        workspace_w = width - roster_w - files_w

        self._draw_box(stdscr, 0, 0, height - 1, roster_w, " Agent Roster ", self.focus == "roster")
        self._draw_box(stdscr, 0, roster_w, height - 1, files_w, " Agent Files ", self.focus == "files")
        self._draw_box(stdscr, 0, roster_w + files_w, height - 1, workspace_w, " Workspace ", self.focus == "tabs")
        self._draw_roster(stdscr, 1, 1, height - 3, roster_w - 2)
        self._draw_files(stdscr, 1, roster_w + 1, height - 3, files_w - 2)
        self._draw_workspace(stdscr, 1, roster_w + files_w + 1, height - 3, workspace_w - 2)
        actions = "  ".join(action.label for action in current_actions(self.focus, self.mode, self._selected_files()[self.selected_file] if self._selected_files() else None))
        strip = actions[: max(width - 1, 0)]
        try:
            stdscr.addnstr(height - 2, 0, strip.ljust(width - 1), width - 1, curses.A_BOLD)
            stdscr.addnstr(height - 1, 0, self.status.ljust(width - 1), width - 1, curses.A_REVERSE)
        except curses.error:
            pass
        stdscr.refresh()

    def _draw_box(self, stdscr, y: int, x: int, h: int, w: int, title: str, focused: bool) -> None:
        attr = curses.A_BOLD if focused else curses.A_NORMAL
        win = stdscr.derwin(h, w, y, x)
        win.box()
        win.addnstr(0, 2, title, max(w - 4, 0), attr)

    def _draw_roster(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        if not self.view.agents:
            stdscr.addnstr(y, x, "No active agents", w)
            return
        for idx, agent in enumerate(self.view.agents[:h]):
            prefix = ">" if idx == self.selected_agent and self.focus == "roster" else " "
            label = f"{prefix} {agent.agent_name} [{agent.roster_state}]"
            stdscr.addnstr(y + idx, x, label, w)

    def _draw_files(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        files = self._selected_files()
        if not files:
            stdscr.addnstr(y, x, "No claimed or changed files", w)
            return
        for idx, file_item in enumerate(files[:h]):
            prefix = ">" if idx == self.selected_file and self.focus == "files" else " "
            marker = "!" if file_item.conflict else "*"
            label = f"{prefix} {marker} {file_item.path}"
            stdscr.addnstr(y + idx, x, label, w)

    def _draw_workspace(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        if self.mode == "quick_edit" and self._qe_path:
            self._draw_quick_edit(stdscr, y, x, h, w)
            return
        if not self.tabs:
            stdscr.addnstr(y, x, "Open a touched file to inspect live work", w)
            return
        tab = self.tabs[self.selected_tab]
        task_info = f"#{tab.task_id} {tab.task_title}" if tab.task_id else "no task"
        header = f"{tab.path}  [{tab.agent_name}]  {task_info}  ({self.mode})"
        stdscr.addnstr(y, x, header, w, curses.A_BOLD)
        body = build_diff(self.root, tab.base_ref, tab.path) if self.mode == "diff" else read_file_contents(self.root, tab.path)
        lines = body.splitlines() or [body]
        for idx, line in enumerate(lines[: max(h - 2, 0)]):
            stdscr.addnstr(y + 2 + idx, x, line, w)

    def _draw_quick_edit(self, stdscr, y: int, x: int, h: int, w: int) -> None:
        mode_label = "diff preview" if self._qe_show_diff else "edit"
        header = f"{self._qe_path}  [quick_edit: {mode_label}]"
        stdscr.addnstr(y, x, header, w, curses.A_BOLD)
        if self._qe_show_diff:
            body = self._qe_diff_preview()
            lines = body.splitlines() or [body]
            for idx, line in enumerate(lines[: max(h - 2, 0)]):
                stdscr.addnstr(y + 2 + idx, x, line, w)
        else:
            visible = self._qe_lines[: max(h - 2, 0)]
            for idx, line in enumerate(visible):
                is_cursor = idx == self._qe_cursor
                attr = curses.A_REVERSE if is_cursor else curses.A_NORMAL
                row_label = f"{idx + 1:>4}  {line}"
                stdscr.addnstr(y + 2 + idx, x, row_label, w, attr)


def run_dx(root: Path) -> None:
    from lex.dx.shell import run_shell

    run_shell(root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dx",
        description="dx — operator shell for lex multi-agent workspaces. "
                    "Runs a full-screen TUI that shows live agent status, "
                    "lets you route messages, review diffs, and manage tasks. "
                    "Must be run from an interactive terminal.",
    )
    parser.add_argument("--root", default=".", help="workspace root (default: cwd)")
    return parser


def main(argv: list[str] | None = None) -> None:
    from lex.dx.cli import main as cli_main

    cli_main(argv)
