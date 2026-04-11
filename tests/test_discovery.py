import json
import socket
import time
from lex.discovery import LexDiscovery, LexPeer


class FakeSocket:
    def __init__(self, responses=None):
        self.responses = responses or []
        self.sent = []
        self.closed = False
        self.timeout = None

    def setsockopt(self, *args):
        pass

    def bind(self, addr):
        pass

    def settimeout(self, timeout):
        self.timeout = timeout

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def recvfrom(self, bufsize):
        if not self.responses:
            raise socket.timeout()
        return self.responses.pop(0)

    def close(self):
        self.closed = True


def test_discovery_announces_payload(monkeypatch):
    sent_messages = []

    class MockSocket:
        def __init__(self, *args, **kwargs):
            pass
        def setsockopt(self, *args):
            pass
        def sendto(self, data, addr):
            sent_messages.append((data, addr))
        def close(self):
            pass

    monkeypatch.setattr("socket.socket", MockSocket)
    
    payload = {"agent_name": "test-agent", "root_path": "/tmp/lex"}
    discovery = LexDiscovery(payload)
    
    # Start announcing and wait briefly
    discovery.start_announcing(interval=0.1)
    time.sleep(0.2)
    discovery.stop()
    
    assert len(sent_messages) > 0
    msg, addr = sent_messages[0]
    data = json.loads(msg.decode('utf-8'))
    assert data["type"] == "lex.announcement"
    assert data["agent_name"] == "test-agent"
    assert addr == ('239.20.20.20', 1901)


def test_discovery_listens_for_peers(monkeypatch):
    peer_data = json.dumps({
        "type": "lex.announcement",
        "agent_name": "peer-1",
        "root_path": "/home/peer1",
        "git_branch": "main"
    }).encode('utf-8')
    
    fake_sock = FakeSocket([(peer_data, ('192.168.1.10', 1901))])
    monkeypatch.setattr("socket.socket", lambda *args, **kwargs: fake_sock)
    
    discovery = LexDiscovery()
    discovery.listen(timeout=0.1)
    
    peers = discovery.get_peers()
    assert len(peers) == 1
    assert peers[0].agent_name == "peer-1"
    assert peers[0].root_path == "/home/peer1"
    assert peers[0].git_branch == "main"


def test_discovery_ignores_malformed_payloads(monkeypatch):
    responses = [
        (b"not json", ('1.1.1.1', 1901)),
        (json.dumps({"type": "other"}).encode('utf-8'), ('2.2.2.2', 1901)),
        (json.dumps({"type": "lex.announcement", "agent_name": "valid"}).encode('utf-8'), ('3.3.3.3', 1901))
    ]
    
    fake_sock = FakeSocket(responses)
    monkeypatch.setattr("socket.socket", lambda *args, **kwargs: fake_sock)
    
    discovery = LexDiscovery()
    discovery.listen(timeout=0.1)
    
    peers = discovery.get_peers()
    assert len(peers) == 1
    assert peers[0].agent_name == "valid"


def test_discovery_records_announce_errors(monkeypatch):
    class FailingSocket:
        def __init__(self, *args, **kwargs):
            pass
        def setsockopt(self, *args):
            pass
        def sendto(self, data, addr):
            raise OSError("network down")
        def close(self):
            pass

    monkeypatch.setattr("socket.socket", FailingSocket)
    discovery = LexDiscovery({"agent_name": "test-agent", "root_path": "/tmp/lex"})
    discovery.start_announcing(interval=0.05)
    time.sleep(0.12)
    discovery.stop()

    assert discovery.last_error is not None
    assert "announce" in discovery.last_error


def test_discovery_records_listen_errors(monkeypatch):
    class FailingListenSocket(FakeSocket):
        def recvfrom(self, bufsize):
            raise OSError("socket failure")

    fake_sock = FailingListenSocket()
    monkeypatch.setattr("socket.socket", lambda *args, **kwargs: fake_sock)

    discovery = LexDiscovery()
    discovery.listen(timeout=0.1)

    assert discovery.last_error is not None
    assert "listen" in discovery.last_error
