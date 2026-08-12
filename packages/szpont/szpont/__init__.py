"""szpont - a typed Python API for SzpontNet.

SzpontNet is a leaderless LAN protocol: machines find each other over UDP, gossip
what they can do, and agree with no coordinator on which machine owns each class
of work. Its reference node is the ``szpontnet`` package, and that package's
public surface is the wire format - JSON snapshots, string-keyed dictionaries, and
one exception class for every way a control session can fail.

This package is that surface bound to Python types. It adds nothing to the
protocol and re-implements none of it; every call here is the corresponding
``szpontnet`` call with its answer parsed and its failure classified.

    import szpont

    mesh = szpont.Mesh()
    if mesh.running:
        for peer in mesh.status().peers:
            print(peer.name, peer.link, peer.quota.surplus)

    result = mesh.dispatch("review", prompt, work_key="review:owner/repo#41")
    if result.suppressed:
        ...  # another machine already has this one

Three things it gives you over the dictionaries:

* **Types.** :class:`~szpont.models.Snapshot`, :class:`~szpont.models.Peer`,
  :class:`~szpont.models.Dispatch` and friends, with the questions you actually
  ask as properties - ``peer.up``, ``assignment.satisfied``, ``result.ok``.
  Parsing never raises, and every object keeps the dict it came from as ``raw``,
  so a field this package has not heard of is still reachable.
* **Failures you can branch on.** :class:`~szpont.errors.NodeUnavailable` (there
  was no node, so nothing happened) against
  :class:`~szpont.errors.CommandRejected` (there was, and it did not do it).
  Both remain :class:`szpontnet.ctl.CtlError`.
* **Hosting without a subclass.** :func:`register_host` takes the answers a node
  needs from its host as plain functions.

The library itself stays right there: ``import szpontnet`` for the node, its CLI
(``python -m szpontnet``) and everything this package deliberately does not
duplicate.
"""

from __future__ import annotations

from .errors import CommandRejected, NodeUnavailable, SzpontError
from .hosting import (Host, NoRunner, build_host, duty_model, register_host,
                      unregister_host)
from .mesh import DEFAULT_DISPATCH_TIMEOUT, DEFAULT_TIMEOUT, Mesh
from .models import (NEUTRAL_SURPLUS, Assignment, Claim, Device, Dispatch, Node,
                     Peer, Quota, Shortfall, Slot, Snapshot, Wan, WanTransport)

__version__ = "0.3.0"

__all__ = [
    "__version__",
    # client
    "Mesh", "DEFAULT_TIMEOUT", "DEFAULT_DISPATCH_TIMEOUT",
    # errors
    "SzpontError", "NodeUnavailable", "CommandRejected",
    # models
    "Snapshot", "Node", "Peer", "Quota", "Assignment", "Shortfall", "Dispatch",
    "Slot", "Claim", "Device", "Wan", "WanTransport", "NEUTRAL_SURPLUS",
    # hosting
    "Host", "NoRunner", "build_host", "register_host", "unregister_host",
    "duty_model",
]
