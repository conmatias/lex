import os
import time
from pathlib import Path
from lex.dx.pty_runtime import PTYManager


def test_pty_manager_spawn_and_read():
    mgr = PTYManager()
    # Use 'echo' to test simple output
    sid = mgr.spawn("shell", "echo 'hello world'")
    
    # Wait for output
    time.sleep(0.5)
    
    session = mgr.get_session(sid)
    assert session is not None
    assert any("hello world" in line for line in session.output)
    
    # Check exit status
    time.sleep(0.5)
    assert session.status == "exited"
    mgr.stop()


def test_pty_manager_write():
    mgr = PTYManager()
    # Use 'cat' to test input/output
    # On some systems cat might need a tty to behave predictably in this test
    # We'll use a simple python script that reads and echoes
    python_script = "import sys; print('ready'); line = sys.stdin.readline(); print(f'echo: {line.strip()}')"
    sid = mgr.spawn("python", f"python3 -c \"{python_script}\"")
    
    # Wait for 'ready'
    found_ready = False
    for _ in range(10):
        time.sleep(0.2)
        session = mgr.get_session(sid)
        if any("ready" in line for line in session.output):
            found_ready = True
            break
    
    assert found_ready
    
    mgr.write(sid, "hello cat")
    
    # Wait for echo
    found_echo = False
    for _ in range(10):
        time.sleep(0.2)
        session = mgr.get_session(sid)
        if any("echo: hello cat" in line for line in session.output):
            found_echo = True
            break
            
    assert found_echo
    mgr.stop()


def test_pty_attention_flag():
    mgr = PTYManager()
    # Test pattern matching for attention
    sid = mgr.spawn("shell", "echo 'Continue (y/n)?'")
    
    found_attention = False
    for _ in range(10):
        time.sleep(0.2)
        session = mgr.get_session(sid)
        if session.attention_flag:
            found_attention = True
            break
            
    assert found_attention
    mgr.stop()
