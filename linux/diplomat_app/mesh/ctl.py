"""Synchronous control client — how the panel and the CLI talk to a node.

A control session is a plain TCP connection to the local node's port (found in
``state.json``) that opens with ``{"t":"ctl"}`` instead of a peer hello. Every
command gets exactly one reply line. Blocking sockets on purpose: callers are
the CLI (already synchronous) and the panel (which calls from worker threads).
"""

from __future__ import annotations

import socket

from . import config, protocol, statefile


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
            f.write(protocol.encode(
                protocol.ctl_hello(config.secret(), config.api_key())))
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


def dispatch(duty: str, prompt: str, target: str | None = None,
             api_key: str = "", work_key: str = "", timeout: float = 60.0) -> list[dict]:
    """Route a SzpontRequest through the mesh; returns the per-slot outcomes.
    Generous timeout: the node may walk several failover candidates. ``target``
    names one node directly (the dispatcher's unilateral pick, no failover).
    ``api_key`` is the credential forwarded to an API-key-gated (server) target.
    ``work_key`` opts into origination dedup: the node claims it first and, if a
    peer already owns the work, returns a single ``suppressed`` slot."""
    msg = {"t": "dispatch", "duty": duty, "prompt": prompt}
    if target:
        msg["target"] = target
    if api_key:
        msg["apiKey"] = api_key
    if work_key:
        msg["workKey"] = work_key
    reply = request(msg, timeout=timeout)
    return list(reply.get("results", []))


def claim_work(work_key: str, timeout: float = 5.0) -> dict:
    """Run the origination claim gate for one unit of external work WITHOUT
    dispatching (docs/szpontnet/12) — for a client that will run the work itself,
    like the applet's auto-monitor spawning a local tracked agent. Returns
    ``{"owned": bool, "owner": id | None, "ownerName": str | None}``; ``owned``
    False means a better live personal peer already holds the lease and the
    caller MUST NOT originate."""
    reply = request({"t": "claim", "workKey": work_key}, timeout=timeout)
    return {
        "owned": bool(reply.get("owned")),
        "owner": reply.get("owner"),
        "ownerName": reply.get("ownerName"),
    }


def trust_device(fingerprint: str, label: str = "", timeout: float = 5.0) -> None:
    """Add a device fingerprint to the local trusted allowlist (personal)."""
    request({"t": "trust", "fingerprint": fingerprint, "label": label}, timeout=timeout)


def untrust_device(fingerprint: str, timeout: float = 5.0) -> None:
    """Remove a device fingerprint from the local trusted allowlist."""
    request({"t": "untrust", "fingerprint": fingerprint}, timeout=timeout)


def ban_device(fingerprint: str = "", node: str = "", label: str = "",
               reason: str = "", timeout: float = 5.0) -> None:
    """Add a device to the local ban list (the manual counterpart of the
    automatic foreign-accountability ban). ``fingerprint`` for a keyed device,
    ``node`` (id) for a keyless one."""
    msg: dict = {"t": "ban"}
    if fingerprint:
        msg["fingerprint"] = fingerprint
    if node:
        msg["node"] = node
    if label:
        msg["label"] = label
    if reason:
        msg["reason"] = reason
    request(msg, timeout=timeout)


def unban_device(fingerprint: str = "", node: str = "", timeout: float = 5.0) -> None:
    """Lift a ban — the operator's recovery path."""
    msg: dict = {"t": "unban"}
    if fingerprint:
        msg["fingerprint"] = fingerprint
    if node:
        msg["node"] = node
    request(msg, timeout=timeout)


def set_default_trust(level: str, timeout: float = 5.0) -> None:
    """Set the default trust level for UNKNOWN devices ('personal' | 'foreign')."""
    request({"t": "set-default-trust", "level": level}, timeout=timeout)


def tor_connect(onion: str, timeout: float = 10.0) -> str:
    """Ask the local node to initiate a Tor link to a peer's ``.onion`` address —
    reaching a peer you may never have met on the LAN. The node dials in the
    background; returns the normalized onion it is dialing (watch ``--status`` for
    the peer to appear)."""
    reply = request({"t": "tor-connect", "onion": onion}, timeout=timeout)
    return str(reply.get("onion", ""))


def stop(timeout: float = 5.0) -> None:
    request({"t": "stop"}, timeout=timeout)
