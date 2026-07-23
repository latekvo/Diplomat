"""Last-known peer onion addresses — ``~/.diplomat/mesh/onions.json``.

The sibling of :mod:`peercache`, for the WAN. ``peers.json`` remembers a peer's
last *LAN* address (an IP that changes with the network); this remembers a peer's
permanent *Tor* onion address, which does not. A node learns a peer's onion from
an authenticated ``hello`` (the signed advert carries it, so it is bound to the
peer's device key — never taken from a spoofable beacon), and persists it here so
that once two nodes have met — on the LAN, or by a manual paste — either can
redial the other over Tor from anywhere, across restarts and networks, with no
public IP or DNS. See mesh/tor.py and the Tor reconnect loop in mesh/node.py.

Each entry also records the device ``fingerprint`` the onion was last seen paired
with (from the same signed advert), so a reconnect over Tor can be sanity-checked
against the identity we expect — though the trust handshake re-proves the device
key regardless, so a stale/wrong entry only ever costs a fenced dial, never trust.
The cache is a best-effort accelerator (like :mod:`peercache`): a missing or
malformed entry just falls back to LAN discovery or a manual paste.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import identity
from .atomicjson import write_atomic


def path() -> Path:
    return identity.mesh_dir() / "onions.json"


@dataclass(frozen=True)
class OnionEntry:
    """A peer's persisted onion + the device fingerprint it was paired with."""

    onion: str
    fingerprint: str = ""


def load() -> dict[str, OnionEntry]:
    """The persisted cache: node id → :class:`OnionEntry`. Malformed entries (or a
    malformed/missing file) are dropped silently — the cache is an accelerator,
    never a correctness dependency (mirrors :mod:`peercache`)."""
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, OnionEntry] = {}
    for peer_id, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        onion = entry.get("onion")
        if isinstance(onion, str) and onion:
            fp = entry.get("fingerprint")
            out[str(peer_id)] = OnionEntry(
                onion=onion, fingerprint=fp if isinstance(fp, str) else "")
    return out


def save(cache: dict[str, OnionEntry]) -> None:
    """Atomic write (tmp + rename); best-effort, never raises into the node."""
    body = {pid: {"onion": e.onion, "fingerprint": e.fingerprint}
            for pid, e in sorted(cache.items())}
    write_atomic(path(), body, indent=1)
