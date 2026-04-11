from __future__ import annotations

import json
import socket
import struct
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


# Lex-specific multicast group and port.
# Avoids SSDP (239.255.255.250:1900) to prevent interference and misuse.
MCAST_GRP = '239.20.20.20'
MCAST_PORT = 1901
MCAST_TTL = 2


@dataclass(frozen=True)
class LexPeer:
    agent_name: str
    session_id: int | None
    git_branch: str | None
    git_base_ref: str | None
    root_path: str
    last_seen: float


class LexDiscovery:
    def __init__(self, announcement_payload: dict | None = None):
        self.payload = announcement_payload
        self.peers: dict[str, LexPeer] = {}
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self.last_error: str | None = None

    def _record_error(self, where: str, exc: Exception) -> None:
        message = f"{where}: {type(exc).__name__}: {exc}"
        with self._lock:
            self.last_error = message

    def start_announcing(self, interval: int = 5):
        """Starts a background thread to announce this Lex instance on the local network."""
        if not self.payload:
            return
        
        def announce():
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, MCAST_TTL)
            
            message = json.dumps({
                "type": "lex.announcement",
                "version": "1",
                **self.payload
            }).encode('utf-8')
            
            while not self._stop_event.is_set():
                try:
                    sock.sendto(message, (MCAST_GRP, MCAST_PORT))
                except Exception as exc:
                    self._record_error("announce", exc)
                time.sleep(interval)
            sock.close()

        thread = threading.Thread(target=announce, daemon=True)
        thread.start()
        return thread

    def listen(self, timeout: float = 2.0, callback: Callable[[LexPeer], None] | None = None):
        """Listens for nearby Lex peers and updates the internal roster."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        
        sock.bind(('', MCAST_PORT))
        
        # Multicast membership
        mreq = struct.pack("4sl", socket.inet_aton(MCAST_GRP), socket.INADDR_ANY)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.settimeout(timeout)

        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                data, addr = sock.recvfrom(4096)
                try:
                    payload = json.loads(data.decode('utf-8'))
                    if payload.get("type") == "lex.announcement":
                        peer = LexPeer(
                            agent_name=payload.get("agent_name", "unknown"),
                            session_id=payload.get("session_id"),
                            git_branch=payload.get("git_branch"),
                            git_base_ref=payload.get("git_base_ref"),
                            root_path=payload.get("root_path", ""),
                            last_seen=time.time()
                        )
                        # Identify peer by name and root path to handle multiple instances on same host
                        key = f"{peer.agent_name}:{peer.root_path}"
                        with self._lock:
                            self.peers[key] = peer
                        if callback:
                            callback(peer)
                except (json.JSONDecodeError, KeyError):
                    pass
            except socket.timeout:
                break
            except Exception as exc:
                self._record_error("listen", exc)
                break
        sock.close()

    def stop(self):
        self._stop_event.set()

    def get_peers(self) -> list[LexPeer]:
        with self._lock:
            return sorted(self.peers.values(), key=lambda p: p.agent_name)
