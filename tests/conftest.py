from __future__ import annotations

import sys


def pytest_sessionstart(session) -> None:
    if sys.platform == "win32":
        return
    try:
        import resource
    except ImportError:
        return
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = 4096
    if hard >= 0:
        target = min(target, hard)
    if soft >= target:
        return
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except OSError:
        pass
