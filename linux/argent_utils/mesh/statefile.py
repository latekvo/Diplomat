"""The public topology snapshot — ``~/.argent/mesh/state.json``.

The mesh node rewrites this atomically every couple of seconds (and on every
topology change); UIs poll it the way they poll the device-allocator's
``state.json`` — a cheap file read, no live socket needed to *render*. The
snapshot also carries the node's TCP port, which is how a UI or the CLI finds
the local control socket for edits/dispatch (see :mod:`argent_utils.mesh.ctl`).

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
    path = state_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snapshot, indent=1) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def read_state() -> dict | None:
    """Decode the snapshot; None if the node has never run here."""
    try:
        return json.loads(state_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True  # exists but not ours
    return True


def node_running(state: dict | None = None) -> bool:
    """True when a local mesh node appears to be alive: the snapshot names a
    live pid. (Freshness beyond that is the reader's call — a suspended laptop
    resumes with a stale ``updatedAt`` but a live node that recovers.)"""
    if state is None:
        state = read_state()
    if not state:
        return False
    pid = state.get("pid")
    return isinstance(pid, int) and pid > 0 and _pid_alive(pid)
