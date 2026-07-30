"""Low-level UDP multicast + TCP helpers the tester drives sockets through.

Mirrors the socket options the spec mandates (02-discovery, 03-transport):
``SO_REUSEADDR``/``SO_REUSEPORT`` + ``IP_ADD_MEMBERSHIP`` on the receive
socket, ``IP_MULTICAST_TTL=1`` + loopback on the send socket. In loopback mode
every socket is pinned to ``127.0.0.1`` exactly as a node's loopback-only mode
does, so the tester and a candidate share one self-contained mesh on one host.
"""

from __future__ import annotations

import socket
import struct
import time


def make_beacon_rx(group: str, port: int, loopback: bool) -> socket.socket:
    """A discovery receive socket: bound to the wildcard on the multicast port,
    joined to the group. SO_REUSEPORT lets it coexist with the candidate's own
    receiver on the same host (02-discovery 'several nodes on one host')."""
    iface = "127.0.0.1" if loopback else "0.0.0.0"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    s.bind(("", port))
    mreq = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(iface))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    s.setblocking(False)
    return s


def make_beacon_tx(loopback: bool) -> socket.socket:
    """A discovery send socket: TTL 1 (link-local), loopback on so co-located
    nodes hear each other, broadcast enabled off-loopback."""
    iface = "127.0.0.1" if loopback else "0.0.0.0"
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface))
    if not loopback:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    return s


def send_beacon(tx: socket.socket, payload: bytes, group: str, port: int, loopback: bool) -> None:
    try:
        tx.sendto(payload, (group, port))
    except OSError:
        pass
    if not loopback:
        try:
            tx.sendto(payload, ("255.255.255.255", port))
        except OSError:
            pass


class LineReader:
    """Buffered newline-framed reader over a blocking socket, with a deadline.

    Yields whole NDJSON frames (including the trailing ``\\n`` on request) so the
    tester can both parse a message and inspect its raw bytes for framing
    conformance (03-transport)."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = bytearray()
        self.closed = False

    def read_line(self, timeout: float) -> bytes | None:
        """Return the next frame (with trailing newline) or None on
        timeout/EOF."""
        deadline = time.monotonic() + timeout
        while True:
            nl = self.buf.find(b"\n")
            if nl >= 0:
                line = bytes(self.buf[: nl + 1])
                del self.buf[: nl + 1]
                return line
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            self.sock.settimeout(remaining)
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            except OSError:
                self.closed = True
                return None
            if not chunk:
                self.closed = True
                return None
            self.buf.extend(chunk)


def connect_tcp(host: str, port: int, timeout: float = 5.0) -> socket.socket:
    return socket.create_connection((host, port), timeout=timeout)
