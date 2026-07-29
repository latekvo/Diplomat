"""SzpontNet — leaderless LAN peer-to-peer resource sharing.

Nodes on the local network self-discover over UDP (multicast + subnet broadcast),
hold TCP links with heartbeats, gossip what each machine is and has (platform,
machine tier, budget left), and — every node running the same deterministic
placement function over that shared view — agree with no election on which node
owns each *duty*. A duty moves the moment its owner goes down or runs dry.

Everything here is **standard library only**, which is what lets a node run as a
detached daemon on a machine that has no application installed on it::

      python -m szpontnet                  # foreground node
      python -m szpontnet --daemon         # detach
      python -m szpontnet --status         # print the live topology
      python -m szpontnet --set tokens=out tier=2
      python -m szpontnet --dispatch review --prompt "…"

The protocol is specified in ``szpontnet/docs/``; its canonical v1 constants and
duty catalog ship in ``netmodel.json``. What the library cannot answer for itself
— which duties a deployment routes, where a node's events go, what *running* a job
means on this machine — it asks of a **host**; see :mod:`.host`. With no host
registered a node runs the canonical model and declines work it has no runner for.

Node-local attributes persist in ``<state dir>/node.json``; the live topology
snapshot every UI renders is ``<state dir>/state.json``.
"""

__all__ = ["assign", "config", "ctl", "host", "identity", "node", "protocol",
           "spawnjob", "statefile"]
