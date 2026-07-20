"""Last-known peer addresses — ``~/.diplomat/mesh/peers.json``.

Discovery normally finds peers via UDP beacons (02-discovery). But the beacon
channel can silently die under a live node — an AP that filters multicast, or
an OS privacy gate (macOS 15's Local Network permission makes every LAN
``sendto`` fail with EHOSTUNREACH) — while unicast TCP still works. A node
that only ever met its peers through beacons then loses them *permanently* on
the first link drop: nothing ever re-triggers a dial.

This cache remembers where each peer was last actually reached (id → addr +
advertised TCP port), learned only from an authenticated hello on the peer's
own link (never from a spoofable beacon), and persists it across restarts so
the node can periodically redial known peers directly over unicast
(02-discovery "redial from memory"). A stale or wrong entry costs one failed
or fenced dial per interval, nothing more — the hello handshake authenticates
whoever answers exactly as it does for a beacon-triggered dial.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from . import identity


def path() -> Path:
    return identity.mesh_dir() / "peers.json"


def load() -> dict[str, tuple[str, int]]:
    """The persisted cache: id → (addr, tcpPort). Malformed entries (or a
    malformed/missing file) are dropped silently — the cache is an accelerator,
    never a correctness dependency."""
    try:
        raw = json.loads(path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, tuple[str, int]] = {}
    for peer_id, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        addr, port = entry.get("addr"), entry.get("tcpPort")
        if isinstance(addr, str) and addr and isinstance(port, int) and port > 0:
            out[str(peer_id)] = (addr, port)
    return out


def save(cache: dict[str, tuple[str, int]]) -> None:
    """Atomic write (tmp + rename); best-effort, never raises into the node."""
    p = path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        body = {pid: {"addr": addr, "tcpPort": port}
                for pid, (addr, port) in sorted(cache.items())}
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass
