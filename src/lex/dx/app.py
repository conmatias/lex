from __future__ import annotations

import curses
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from lex.dashboard import DashboardState, load_dashboard_state


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
        self.status = "tab=switch  j/k=move  enter=open tab  d=diff  f=file  x=close tab  r=refresh  q=quit"

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
            elif ch == ord("x"):
                self._close_selected_tab()
            elif ch == ord("r"):
                self._refresh(force=True)

    def _refresh(self, force: bool = False) -> None:
        if force or not self.view.agents:
            self.view = build_dx_view(self.root)
        else:
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
        stdscr.addnstr(height - 1, 0, self.status.ljust(width), width, curses.A_REVERSE)
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
        if not self.tabs:
            stdscr.addnstr(y, x, "Open a touched file to inspect live work", w)
            return
        tab = self.tabs[self.selected_tab]
        header = f"{tab.path}  {tab.agent_name}  {self.mode}"
        stdscr.addnstr(y, x, header, w, curses.A_BOLD)
        body = build_diff(self.root, tab.base_ref, tab.path) if self.mode == "diff" else read_file_contents(self.root, tab.path)
        lines = body.splitlines() or [body]
        for idx, line in enumerate(lines[: max(h - 2, 0)]):
            stdscr.addnstr(y + 2 + idx, x, line, w)


def run_dx(root: Path) -> None:
    DxTui(root).run()
