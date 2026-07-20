"""Diplomat Mesh — LAN P2P coordination between the machines running Diplomat.

Nodes on the local network self-discover over UDP (multicast + subnet
broadcast), hold TCP links with heartbeats, gossip their status (platform,
machine tier, token availability), and deterministically agree on which node
owns each *duty* (Review PRs / Resolve conflicts / Full E2E test) — so a duty
moves to the next machine the moment its owner goes down or runs out of tokens.

Everything in this package is **stdlib-only** (no Qt): the same node runs

- embedded under the Linux applet's supervision (auto-started, topology panel), and
- standalone/headless on any machine with Python 3.10+::

      python -m diplomat_app.mesh              # foreground node
      python -m diplomat_app.mesh --daemon     # detach
      python -m diplomat_app.mesh --status     # print the live topology
      python -m diplomat_app.mesh --set tokens=out tier=2
      python -m diplomat_app.mesh --dispatch review --prompt "…"

The protocol constants, duty catalog and placement strategies are shared
language-neutral assets in ``core/mesh.json``; node-local attributes persist in
``~/.diplomat/mesh/node.json``; the live topology snapshot every UI renders is
``~/.diplomat/mesh/state.json`` (the device-allocator ``state.json`` pattern).
"""

__all__ = ["assign", "config", "ctl", "identity", "node", "protocol", "spawnjob", "statefile"]
