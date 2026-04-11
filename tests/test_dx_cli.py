from __future__ import annotations

from lex.dx.cli import main
from lex.dx.pty_runtime import TerminalSession


def test_dx_cli_daemon_status_reports_stopped(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("lex.dx.cli.daemon_running", lambda *_args, **_kwargs: False)
    main(["--root", str(tmp_path), "daemon", "status"])
    out = capsys.readouterr().out
    assert "stopped" in out


def test_dx_cli_spawn_uses_runtime_client(tmp_path, monkeypatch, capsys):
    calls = {}

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def spawn(self, kind, cmd, **kwargs):
            calls["kind"] = kind
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs
            return 7

        def get_session(self, session_id):
            return TerminalSession(
                id=session_id,
                title="shell-7",
                kind="shell",
                cwd=tmp_path,
                status="running",
            )

    monkeypatch.setattr("lex.dx.cli.DaemonRuntimeClient", FakeClient)

    main(["--root", str(tmp_path), "spawn", "shell", "--command", "bash"])
    out = capsys.readouterr().out

    assert calls["kind"] == "shell"
    assert calls["cmd"] == "bash"
    assert "spawned shell-7" in out


def test_dx_cli_send_routes_message(tmp_path, monkeypatch, capsys):
    sent = {}

    class FakeClient:
        def __init__(self, *_args, **_kwargs):
            pass

        def list_sessions(self):
            return [
                TerminalSession(
                    id=3,
                    title="codex-3",
                    kind="codex",
                    cwd=tmp_path,
                    status="running",
                )
            ]

        def write(self, session_id, text):
            sent["session_id"] = session_id
            sent["text"] = text

    monkeypatch.setattr("lex.dx.cli.DaemonRuntimeClient", FakeClient)

    main(["--root", str(tmp_path), "send", "codex", "ship it"])
    out = capsys.readouterr().out

    assert sent == {"session_id": 3, "text": "ship it"}
    assert "sent to codex-3" in out


def test_dx_cli_attach_enables_follow(tmp_path, monkeypatch):
    seen = {}

    def fake_tail(args):
        seen["follow"] = args.follow
        seen["session"] = args.session

    monkeypatch.setattr("lex.dx.cli._cmd_tail", fake_tail)
    parser_run = main
    parser_run(["--root", str(tmp_path), "attach", "shell-1"])

    assert seen["follow"] is True
    assert seen["session"] == "shell-1"
