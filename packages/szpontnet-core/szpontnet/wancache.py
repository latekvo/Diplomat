"""Last-known peer WAN addresses — ``<state dir>/wan.json``.

The sibling of :mod:`peercache`, for the WAN. ``peers.json`` remembers a peer's
last *LAN* address (an IP that changes with the network); this remembers the
permanent addresses that do not: an iroh ``endpoint`` id and a Tor ``onion``, one
per WAN transport. A node learns them from an
authenticated ``hello`` (the signed advert carries both, so they are bound to the
peer's device key — never taken from a spoofable beacon) and persists them here,
so that once two nodes have met — on the LAN, or by a manual paste — either can
redial the other from anywhere, across restarts and networks, with no public IP
or DNS.

Both addresses share one entry because they are one fact about one peer: where to
reach it off-LAN. That keeps a single eviction policy and a single trust check
governing WAN redials however many transports are compiled in.

Each entry also records the device ``fingerprint`` the addresses were last seen
paired with (from the same signed advert), so a reconnect can be sanity-checked
against the identity we expect — though the trust handshake re-proves the device
key regardless, so a stale/wrong entry only ever costs a fenced dial, never trust.
The cache is a best-effort accelerator (like :mod:`peercache`): a missing or
malformed entry just falls back to LAN discovery or a manual paste.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import identity
from .atomicjson import read_object, write_atomic


def path() -> Path:
    return identity.mesh_dir() / "wan.json"


def legacy_path() -> Path:
    """Where the Tor-only cache lived before iroh existed. Read once, never
    written: a node that upgrades keeps the onions it already knew."""
    return identity.mesh_dir() / "onions.json"


@dataclass(frozen=True)
class WanEntry:
    """A peer's persisted WAN addresses + the device fingerprint they were paired
    with. Either address may be empty — a peer running only one transport, or an
    older node that advertised only an onion."""

    endpoint: str = ""
    onion: str = ""
    fingerprint: str = ""

    def reachable(self) -> bool:
        """Whether this entry names anywhere to dial at all. An entry that names
        neither address is dropped rather than kept as a placeholder."""
        return bool(self.endpoint or self.onion)


def _parse(raw: dict) -> dict[str, WanEntry]:
    out: dict[str, WanEntry] = {}
    for peer_id, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        endpoint = entry.get("endpoint")
        onion = entry.get("onion")
        fp = entry.get("fingerprint")
        parsed = WanEntry(
            endpoint=endpoint if isinstance(endpoint, str) else "",
            onion=onion if isinstance(onion, str) else "",
            fingerprint=fp if isinstance(fp, str) else "")
        if parsed.reachable():
            out[str(peer_id)] = parsed
    return out


def load() -> dict[str, WanEntry]:
    """The persisted cache: node id → :class:`WanEntry`. Malformed entries (or a
    malformed/missing file) are dropped silently — the cache is an accelerator,
    never a correctness dependency (mirrors :mod:`peercache`). Falls back to the
    legacy Tor-only ``onions.json`` when this node has not written ``wan.json``
    yet, so upgrading does not forget peers."""
    raw = read_object(path())
    if raw is None:
        raw = read_object(legacy_path())
    if raw is None:
        return {}
    return _parse(raw)


def save(cache: dict[str, WanEntry]) -> None:
    """Atomic write (tmp + rename); best-effort, never raises into the node."""
    body = {pid: {"endpoint": e.endpoint, "onion": e.onion,
                  "fingerprint": e.fingerprint}
            for pid, e in sorted(cache.items())}
    write_atomic(path(), body, indent=1)
