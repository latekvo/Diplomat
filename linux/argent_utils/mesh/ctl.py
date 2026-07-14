"""Synchronous control client — how the panel and the CLI talk to a node.

A control session is a plain TCP connection to the local node's port (found in
``state.json``) that opens with ``{"t":"ctl"}`` instead of a peer hello. Every
command gets exactly one reply line. Blocking sockets on purpose: callers are
the CLI (already synchronous) and the panel (which calls from worker threads).
"""

from __future__ import annotations

import socket

from . import protocol, statefile


class CtlError(RuntimeError):
    pass


def _endpoint() -> tuple[str, int]:
    state = statefile.read_state()
    if not state or not statefile.node_running(state):
        raise CtlError("no local mesh node is running (state.json absent or stale)")
    port = state.get("tcpPort")
    if not isinstance(port, int) or port <= 0:
        raise CtlError("state.json has no usable tcpPort")
    return "127.0.0.1", port


def request(msg: dict, timeout: float = 10.0) -> dict:
    """One command, one reply. Raises :class:`CtlError` when the node is not
    running, not answering, or answers with an error."""
    host, port = _endpoint()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            f = sock.makefile("rwb")
            f.write(protocol.encode(protocol.ctl_hello()))
            f.write(protocol.encode(msg))
            f.flush()
            line = f.readline(protocol.MAX_LINE_BYTES)
    except OSError as exc:
        raise CtlError(f"mesh node unreachable on :{port}: {exc}") from exc
    reply = protocol.decode(line)
    if reply is None:
        raise CtlError("mesh node closed the control session without answering")
    if reply.get("t") == "error":
        raise CtlError(str(reply.get("reason", "unknown error")))
    return reply


def status(timeout: float = 5.0) -> dict:
    """The node's live snapshot (fresher than state.json, same shape)."""
    return request(protocol.status_request(), timeout=timeout)["state"]


def set_attr(target: str, attrs: dict, timeout: float = 5.0) -> None:
    """Edit a node's local attributes. ``target`` is a node id or ``"self"``;
    a remote target is forwarded over that peer's link."""
    request(protocol.set_attr(target, attrs), timeout=timeout)


def set_overrides(duty: str, placement: dict, timeout: float = 5.0) -> None:
    """Edit one duty's mesh-wide placement (gossiped last-writer-wins)."""
    request({"t": "set-overrides", "duty": duty, "placement": placement}, timeout=timeout)


def dispatch(duty: str, prompt: str, timeout: float = 60.0) -> list[dict]:
    """Route a job through the mesh; returns the per-slot outcomes. Generous
    timeout: the node may walk several failover candidates."""
    reply = request({"t": "dispatch", "duty": duty, "prompt": prompt}, timeout=timeout)
    return list(reply.get("results", []))


def stop(timeout: float = 5.0) -> None:
    request({"t": "stop"}, timeout=timeout)
