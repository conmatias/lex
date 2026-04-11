import time

from lex.db import connect, ensure_workspace, initialize_database
from lex.dx.daemon import daemon_running, stop_daemon
from lex.dx.runtime_service import DaemonRuntimeClient


def _wait_until(predicate, *, timeout: float = 4.0, step: float = 0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def test_daemon_client_spawn_and_list_sessions(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    client = DaemonRuntimeClient(tmp_path, autostart=True)
    try:
        session_id = client.spawn(
            "shell",
            "python3 -c \"import time; print('ready'); time.sleep(0.8)\"",
            cwd=tmp_path,
            lex_root=tmp_path,
        )

        def has_session():
            sessions = client.list_sessions()
            return any(session.id == session_id for session in sessions)

        assert _wait_until(has_session)
    finally:
        stop_daemon(tmp_path)


def test_daemon_sessions_survive_client_detach(tmp_path):
    paths = ensure_workspace(tmp_path)
    conn = connect(paths.db_path)
    initialize_database(conn)

    client_one = DaemonRuntimeClient(tmp_path, autostart=True)
    try:
        session_id = client_one.spawn(
            "shell",
            "python3 -c \"import time; print('running'); time.sleep(1.2)\"",
            cwd=tmp_path,
            lex_root=tmp_path,
        )
        client_two = DaemonRuntimeClient(tmp_path, autostart=False)

        def visible_from_second_client():
            sessions = client_two.list_sessions()
            return any(session.id == session_id for session in sessions)

        assert _wait_until(visible_from_second_client)
        assert daemon_running(client_two.socket_path)
        client_two.close(session_id)
    finally:
        stop_daemon(tmp_path)
