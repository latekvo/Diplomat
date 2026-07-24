"""The public topology snapshot — ``~/.diplomat/mesh/state.json``.

The mesh node rewrites this atomically every couple of seconds (and on every
topology change); UIs poll it the way they poll the device-allocator's
``state.json`` — a cheap file read, no live socket needed to *render*. The
snapshot also carries the node's TCP port, which is how a UI or the CLI finds
the local control socket for edits/dispatch (see :mod:`diplomat_app.mesh.ctl`).

Shape::

    {
      "updatedAt": iso8601,
      "pid": 1234,                      # the node process (staleness check)
      "tcpPort": 40878,                 # local control endpoint
      "self": NodeInfo dict,
      "peers": [ { ...NodeInfo, "link": "up"|"stale"|"down",
                   "addr": "192.168.…", "lastSeenSecsAgo": 1.4 } ],
      "assignments": { duty: {"duty", "assigned": [ids], "shortfall": […]} },
      "overrides": PlacementOverrides dict,
      "v": 1
    }
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import identity
from .atomicjson import write_atomic
from .protocol import PROTOCOL_VERSION


def state_path() -> Path:
    return identity.mesh_dir() / "state.json"


def stamp(snapshot: dict) -> dict:
    """Add the envelope fields every published snapshot carries — ``updatedAt``
    (ISO-8601 write time), ``pid`` (liveness), and ``v`` (version). Applied both
    when writing ``state.json`` and when a node answers a control-session
    ``status`` live, so the two channels are the same object (08-state)."""
    snapshot["updatedAt"] = datetime.now(timezone.utc).isoformat()
    snapshot["pid"] = os.getpid()
    snapshot["v"] = PROTOCOL_VERSION
    return snapshot


def write_state(snapshot: dict) -> None:
    """Atomic write (tmp + rename); best-effort, never raises into the node."""
    stamp(snapshot)
    write_atomic(state_path(), snapshot, indent=1)


def read_state() -> dict | None:
    """Decode the snapshot; None if the node has never run here, OR the file is
    corrupt/hostile (unreadable, non-JSON, or valid JSON that is NOT an object).
    Every caller treats None as "no live node", so a bad file degrades instead of
    crashing them on ``state.get(...)`` / ``state.items()`` (mirrors appconfig.read)."""
    try:
        data = json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # exists but not ours
    except (OverflowError, ValueError):
        # A pid outside the OS's pid_t range (e.g. an oversized int in a corrupt/hostile
        # state.json) makes os.kill raise OverflowError — not an OSError, so it would
        # otherwise escape node_running() and crash the tray/launcher/panel. Such a value
        # can never name a live process, so treat it as dead.
        return False
    return True


def node_running(state: dict | None = None) -> bool:
    """True when a local mesh node appears to be alive: the snapshot names a
    live pid. (Freshness beyond that is the reader's call — a suspended laptop
    resumes with a stale ``updatedAt`` but a live node that recovers.)"""
    if state is None:
        state = read_state()
    if not isinstance(state, dict):
        return False  # never run here, or a caller passed a corrupt/non-object snapshot
    pid = state.get("pid")
    return isinstance(pid, int) and pid > 0 and _pid_alive(pid)
