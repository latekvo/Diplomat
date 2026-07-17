#!/usr/bin/env python3
"""Candidate adapter for the reference node (``linux/co_maintainer/mesh``).

The conformance tester launches a candidate purely through the ``SZPONTNET_*``
environment (the *candidate contract*). The reference node predates that contract
and reads its own ``CO_MAINTAINER_MESH_*`` variables + a ``node.json`` identity file, so
this thin adapter translates one into the other and then execs the real node. It
is the worked example every other implementation copies: read ``SZPONTNET_*``,
configure your node, run it.

Usage (as the tester's --node-cmd):

    python -m szpont --node-cmd "python tools/szpontnet-tester/adapters/reference.py"
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# repo/tools/szpontnet-tester/adapters/reference.py → repo root is parents[3].
REPO = Path(__file__).resolve().parents[3]
LINUX = REPO / "linux"

# SZPONTNET_* → CO_MAINTAINER_MESH_* protocol/discovery knobs (names differ, values 1:1).
_MAP = {
    "SZPONTNET_LOOPBACK": "CO_MAINTAINER_MESH_LOOPBACK",
    "SZPONTNET_MCAST_GROUP": "CO_MAINTAINER_MESH_MCAST_GROUP",
    "SZPONTNET_MCAST_PORT": "CO_MAINTAINER_MESH_MCAST_PORT",
    "SZPONTNET_TCP_BASE": "CO_MAINTAINER_MESH_TCP_BASE",
    "SZPONTNET_TCP_SPAN": "CO_MAINTAINER_MESH_TCP_SPAN",
    "SZPONTNET_BEACON_SECS": "CO_MAINTAINER_MESH_BEACON_SECS",
    "SZPONTNET_HEARTBEAT_SECS": "CO_MAINTAINER_MESH_HEARTBEAT_SECS",
    "SZPONTNET_STALE_SECS": "CO_MAINTAINER_MESH_STALE_SECS",
    "SZPONTNET_TIMEOUT_SECS": "CO_MAINTAINER_MESH_TIMEOUT_SECS",
    "SZPONTNET_ACK_SECS": "CO_MAINTAINER_MESH_ACK_SECS",
    "SZPONTNET_STATE_SECS": "CO_MAINTAINER_MESH_STATE_SECS",
    "SZPONTNET_SECRET": "CO_MAINTAINER_MESH_SECRET",
    "SZPONTNET_PLATFORM": "CO_MAINTAINER_MESH_PLATFORM",
    "SZPONTNET_SPAWN": "CO_MAINTAINER_MESH_SPAWN",
    # Chapter-11 role knobs (11-trust-and-balancing).
    "SZPONTNET_SERVER": "CO_MAINTAINER_MESH_SERVER",     # accept-only server role
    "SZPONTNET_API_KEY": "CO_MAINTAINER_MESH_API_KEY",   # inbound ctl/dispatch gate
    # Chapter-13 foreign zero-trust execution: the confinement runner that turns a
    # foreign request from declined into confined, response-only, plus fast foreign
    # reliable-delivery timings so a loopback scenario observes retry/ack quickly.
    "SZPONTNET_FOREIGN_SPAWN": "CO_MAINTAINER_MESH_FOREIGN_SPAWN",  # confinement runner
    "SZPONTNET_RESULT_RETRY_SECS": "CO_MAINTAINER_MESH_RESULT_RETRY_SECS",
    "SZPONTNET_RESULT_MAX_SECS": "CO_MAINTAINER_MESH_RESULT_MAX_SECS",
    "SZPONTNET_FOREIGN_TIMEOUT_SECS": "CO_MAINTAINER_MESH_FOREIGN_TIMEOUT_SECS",
}


def main() -> None:
    work_dir = Path(os.environ["SZPONTNET_DIR"])
    work_dir.mkdir(parents=True, exist_ok=True)

    # The reference persists identity in node.json; write the tester's chosen id.
    duties = {}
    try:
        duties = json.loads(os.environ.get("SZPONTNET_DUTIES", "{}"))
    except ValueError:
        pass
    (work_dir / "node.json").write_text(json.dumps({
        "id": os.environ["SZPONTNET_NODE_ID"],
        "name": os.environ.get("SZPONTNET_NODE_NAME", "cand"),
        "tier": int(os.environ.get("SZPONTNET_TIER", "3")),
        "tokens": os.environ.get("SZPONTNET_TOKENS", "ok"),
        "dutiesEnabled": duties,
    }))

    # Optional ch-11 stat seed: the tester passes {"plan","quotaLeft","usageAvg"}
    # (the advertised view); translate it into the reference's persisted
    # stats.json (plan + a decaying reservoir acc = usageAvg·τ and quotaUsed =
    # capacity − quotaLeft) so the node advertises exactly those figures on boot.
    stats_env = os.environ.get("SZPONTNET_STATS")
    if stats_env:
        try:
            st = json.loads(stats_env)
            plan = str(st.get("plan", "max-5x"))
            weight = {"pro": 1.0, "max-5x": 5.0, "max-20x": 20.0}.get(plan, 1.0)
            tau, now = 21.0, __import__("time").time()
            (work_dir / "stats.json").write_text(json.dumps({
                "plan": plan,
                "acc": float(st.get("usageAvg", 0.0)) * tau,
                "quotaUsed": max(0.0, weight - float(st.get("quotaLeft", weight))),
                "windowStart": now,
                "updatedAt": now,
            }))
        except (ValueError, TypeError):
            pass

    env = dict(os.environ)
    for src, dst in _MAP.items():
        if src in os.environ:
            env[dst] = os.environ[src]
    env["CO_MAINTAINER_MESH_DIR"] = str(work_dir)
    # Keep the reference's activity feed inside the scenario dir, not real ~/.argent.
    env["HOME"] = str(work_dir)
    # A conformance candidate must be deterministic: no live OAuth quota probe.
    # (On macOS the Keychain resolves even under the sandboxed HOME, and a live
    # read would cap the advertised quotaLeft with this machine's real budget,
    # skewing seeded ch-11 stats.)
    env["CO_MAINTAINER_MESH_OAUTH_PROBE"] = "0"
    env["PYTHONPATH"] = os.pathsep.join([str(LINUX), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)

    os.chdir(str(LINUX))
    os.execvpe(sys.executable, [sys.executable, "-m", "co_maintainer.mesh"], env)


if __name__ == "__main__":
    main()
