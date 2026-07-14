"""Deterministic duty assignment — the mesh's leaderless brain.

Every node runs this same pure function over the same gossiped inputs (the
live node set + the LWW placement overrides) and lands on the same answer, so
the mesh needs no election and has no split-brain window: when a node dies or
runs out of tokens, every survivor recomputes and the duty has *already*
moved. Determinism comes from total ordering — every ranking ends in the node
id tie-break.

Eligibility for a duty:
- the node has the duty enabled (per-node toggle), and
- if the placement is token-aware, the node is not out of tokens
  (``tokens == "out"``); low-token nodes stay eligible but rank behind
  same-strategy peers with full tokens.

Strategies (ranking among eligible nodes):
- ``weakest-first``    highest tier number first (tier 1 = strongest machine)
- ``strongest-first``  lowest tier number first
- ``local-first``      the given local node first, the rest weakest-first
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config
from .config import Placement, PlacementOverrides
from .protocol import NodeInfo

_TOKEN_RANK = {"ok": 0, "low": 1, "out": 2}


@dataclass(frozen=True)
class DutyAssignment:
    duty: str
    assigned: tuple[str, ...]  # node ids, in rank order
    # Unmet platform requirements: [(platform, missing_count)].
    shortfall: tuple[tuple[str, int], ...] = ()

    @property
    def satisfied(self) -> bool:
        return not self.shortfall

    def to_dict(self) -> dict:
        return {
            "duty": self.duty,
            "assigned": list(self.assigned),
            "shortfall": [{"platform": p, "missing": m} for p, m in self.shortfall],
        }


def _eligible(nodes: list[NodeInfo], duty_id: str, placement: Placement) -> list[NodeInfo]:
    out = []
    for n in nodes:
        if not n.duty_enabled(duty_id):
            continue
        if placement.token_aware and n.tokens == "out":
            continue
        out.append(n)
    return out


def _ranked(nodes: list[NodeInfo], strategy: str, local_id: str) -> list[NodeInfo]:
    def key(n: NodeInfo):
        tok = _TOKEN_RANK.get(n.tokens, 1)
        if strategy == "strongest-first":
            return (tok, n.tier, n.id)
        if strategy == "local-first":
            return (tok, n.id != local_id, -n.tier, n.id)
        # weakest-first (and any unknown strategy from a newer peer)
        return (tok, -n.tier, n.id)

    return sorted(nodes, key=key)


def assign_duty(
    duty_id: str,
    nodes: list[NodeInfo],
    overrides: PlacementOverrides | None = None,
    local_id: str = "",
) -> DutyAssignment:
    """Assign one duty over the given live nodes.

    With a platform ``spread`` (e.g. the bundle E2E's one-linux-plus-one-macos)
    each requirement is filled from that platform's ranked candidates; a node
    fills at most one slot. Without a spread, the single best-ranked node owns
    the duty. Requirements that can't be met are reported as ``shortfall`` —
    the duty still gets whatever coverage exists.
    """
    placement = config.placement_for(duty_id, overrides)
    pool = _ranked(_eligible(nodes, duty_id, placement), placement.strategy, local_id)

    if not placement.spread:
        return DutyAssignment(duty_id, (pool[0].id,) if pool else (),
                              () if pool else (("any", 1),))

    assigned: list[str] = []
    shortfall: list[tuple[str, int]] = []
    taken: set[str] = set()
    for platform, count in placement.spread:
        got = 0
        for n in pool:
            if got == count:
                break
            if n.platform == platform and n.id not in taken:
                taken.add(n.id)
                assigned.append(n.id)
                got += 1
        if got < count:
            shortfall.append((platform, count - got))
    return DutyAssignment(duty_id, tuple(assigned), tuple(shortfall))


def assign_all(
    nodes: list[NodeInfo],
    overrides: PlacementOverrides | None = None,
    local_id: str = "",
) -> dict[str, DutyAssignment]:
    """Every duty in the shared catalog, assigned. The topology snapshot and
    the dispatch router both come through here, so what the panel shows is by
    construction what dispatch will do."""
    return {
        duty_id: assign_duty(duty_id, nodes, overrides, local_id)
        for duty_id in config.duty_ids()
    }


def dispatch_candidates(
    duty_id: str,
    nodes: list[NodeInfo],
    overrides: PlacementOverrides | None = None,
    local_id: str = "",
) -> list[str]:
    """The failover order for actually running a job: the assigned node(s)
    first, then every remaining eligible node by rank — so a dispatch survives
    the owner dropping between gossip rounds."""
    placement = config.placement_for(duty_id, overrides)
    a = assign_duty(duty_id, nodes, overrides, local_id)
    rest = [
        n.id
        for n in _ranked(_eligible(nodes, duty_id, placement), placement.strategy, local_id)
        if n.id not in a.assigned
    ]
    return list(a.assigned) + rest


def slot_candidates(
    duty_id: str,
    nodes: list[NodeInfo],
    overrides: PlacementOverrides | None = None,
    local_id: str = "",
) -> list[tuple[str, list[str]]]:
    """Per-slot failover lists for executing a dispatch.

    A spread duty runs one job per slot (the bundle E2E = a linux slot AND a
    macos slot); each slot gets its own ranked candidate list so a failed
    target falls over to the next machine *of the required platform*. The
    executor is responsible for not landing two slots on one node. A no-spread
    duty is a single ``("any", ranked)`` slot.
    """
    placement = config.placement_for(duty_id, overrides)
    pool = _ranked(_eligible(nodes, duty_id, placement), placement.strategy, local_id)
    if not placement.spread:
        return [("any", [n.id for n in pool])]
    slots: list[tuple[str, list[str]]] = []
    for platform, count in placement.spread:
        of_platform = [n.id for n in pool if n.platform == platform]
        slots.extend((platform, of_platform) for _ in range(count))
    return slots
