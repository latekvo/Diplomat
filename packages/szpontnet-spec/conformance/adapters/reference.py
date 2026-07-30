#!/usr/bin/env python3
"""Candidate adapter for the reference node (``packages/szpontnet-core``).

The conformance tester launches a candidate purely through the ``SZPONTNET_*``
environment (the *candidate contract*). The reference node reads those same
variables directly, so all this adapter does is the part the contract leaves to
each implementation: turn the tester's chosen identity (and optional seeded
quota stats) into whatever persisted form the node expects, then exec it. It is
the worked example every other implementation copies: read ``SZPONTNET_*``,
configure your node, run it.

There used to be a translation table here, because the node read its variables
under an older ``DIPLOMAT_MESH_*`` spelling from when it lived inside an
application. That the table is gone is the point: the tester and the node now
agree on one namespace, so nothing sits between them to disagree with the spec.

Usage (as the tester's --node-cmd):

    python -m szpont --node-cmd "python adapters/reference.py"
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# packages/szpontnet-spec/conformance/adapters/reference.py → packages/ is parents[3].
PACKAGES = Path(__file__).resolve().parents[3]
# The library's project dir, where `szpontnet` imports from — a sibling package, which
# is the only thing this adapter knows about the reference beyond the wire contract.
PROJECT = PACKAGES / "szpontnet-core"


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

    # Every SZPONTNET_* knob the tester set is already in this environment and is
    # already the name the node reads, so it is inherited as-is; only the ones the
    # adapter itself decides are set here.
    env = dict(os.environ)
    env["SZPONTNET_DIR"] = str(work_dir)
    # Keep anything the node reads out of $HOME inside the scenario dir.
    env["HOME"] = str(work_dir)
    # A conformance candidate must be deterministic: no live OAuth quota probe.
    # (On macOS the Keychain resolves even under the sandboxed HOME, and a live
    # read would cap the advertised quotaLeft with this machine's real budget,
    # skewing seeded ch-11 stats.)
    env["SZPONTNET_OAUTH_PROBE"] = "0"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(PROJECT), env.get("PYTHONPATH", "")]).rstrip(os.pathsep)

    os.chdir(str(PROJECT))
    os.execvpe(sys.executable, [sys.executable, "-m", "szpontnet"], env)


if __name__ == "__main__":
    main()
