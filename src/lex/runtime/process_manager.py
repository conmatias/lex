from __future__ import annotations

import os
import signal
import subprocess
import sys

from lex.db import LexPaths
from lex.runtime.inbox import worker_runtime_dir


def process_is_alive(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def launch_worker_supervisor(paths: LexPaths, runtime_id: int) -> subprocess.Popen[bytes]:
    log_dir = worker_runtime_dir(paths, runtime_id)
    stdout_path = log_dir / "supervisor.log"
    stderr_path = log_dir / "supervisor.err.log"
    env = os.environ.copy()
    src_path = str(paths.root / "src")
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = src_path if not existing_pythonpath else f"{src_path}{os.pathsep}{existing_pythonpath}"
    stdout_handle = stdout_path.open("ab")
    stderr_handle = stderr_path.open("ab")
    try:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "lex.worker_runtime",
                "--root",
                str(paths.root),
                str(runtime_id),
            ],
            cwd=str(paths.root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            start_new_session=True,
            close_fds=True,
        )
    finally:
        stdout_handle.close()
        stderr_handle.close()


def stop_runtime_process(pid: int, sig: int = signal.SIGTERM) -> None:
    os.kill(pid, sig)
