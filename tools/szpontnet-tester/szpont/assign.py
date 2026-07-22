"""The deterministic placement function — an independent oracle (06-coordination).

This is a clean-room reimplementation of ``assign_all`` from the specification.
The tester uses it two ways:

1. as an **oracle**: given a fleet, it computes the assignment the spec
   requires, which the tester compares against what a candidate actually
   publishes in its snapshot;
2. as a **permutation-invariance probe**: shuffling the input order MUST NOT
   change the result (06 determinism requirement) — the tester asserts this on
   the oracle *and* observes it on the candidate.

Every ranking key ends in the node ``id`` so the total order has no ties, which
is what makes two conformant nodes agree byte-for-byte.

Beyond the core strategies (weakest-first, strongest-first, local-first) this
oracle also implements chapter 11's ``surplus-first`` — ranking by descending
dispatch surplus (the advertised burn-down ratio: budget left ÷ clock left to
reset), quantised into buckets, with a weakest-first tie-break — so the tester
can judge a load-balanced dispatch placement. It is the default strategy.
"""

from __future__ import annotations

from .codec import NodeInfo
from .model import DEFAULT_STRATEGY, SURPLUS_RANK_BUCKET, TOKEN_RANK, Model


def surplus_bucket(value: float) -> int:
    """A surplus quantised to SURPLUS_RANK_BUCKET, as a comparable index — the
    hysteresis that keeps continuous pace drift from reshuffling the ranking.
    Mirrors the reference ``protocol.surplus_bucket``."""
    return round(value / SURPLUS_RANK_BUCKET)


def placement(model: Model, duty_id: str, overrides: dict | None) -> dict:
    """Effective policy: the gossiped override if present, else model default."""
    if overrides:
        duties = overrides.get("duties") or {}
        if duty_id in duties:
            return duties[duty_id]
    return model.placement_for(duty_id)


def _spread_of(policy: dict) -> list[tuple[str, int]]:
    return [(s["platform"], int(s.get("count", 1))) for s in policy.get("spread", [])]


def eligible(nodes: list[NodeInfo], duty_id: str, policy: dict) -> list[NodeInfo]:
    token_aware = bool(policy.get("tokenAware", True))
    out = []
    for n in nodes:
        if not n.duty_enabled(duty_id):
            continue
        if token_aware and n.tokens == "out":
            continue
        out.append(n)
    return out


def ranked(nodes: list[NodeInfo], strategy: str, local_id: str) -> list[NodeInfo]:
    def key(n: NodeInfo):
        tok = TOKEN_RANK.get(n.tokens, 1)  # unknown tokens rank as "low", never excluded
        if strategy == "strongest-first":
            return (tok, n.tier, n.id)
        if strategy == "local-first":
            return (tok, n.id != local_id, -n.tier, n.id)
        if strategy == "surplus-first":
            # Most spare quota first (11 load balancing), where "spare" is
            # RELATIVE: budget left ÷ clock left to reset, not absolute units.
            # Surplus leads; token rank then tier break ties (and make
            # neutral-surplus nodes fall back to weakest-first). Compared in
            # buckets so continuous pace drift can't reorder otherwise-equal
            # nodes — the exact reference key.
            return (-surplus_bucket(n.surplus()), tok, -n.tier, n.id)
        # weakest-first, and any UNKNOWN strategy falls back here (06 ranking)
        return (tok, -n.tier, n.id)

    return sorted(nodes, key=key)


def assign_duty(
    model: Model, duty_id: str, nodes: list[NodeInfo],
    overrides: dict | None = None, local_id: str = "",
) -> dict:
    policy = placement(model, duty_id, overrides)
    pool = ranked(eligible(nodes, duty_id, policy), policy.get("strategy", DEFAULT_STRATEGY), local_id)
    spread = _spread_of(policy)

    if not spread:
        if not pool:
            return {"duty": duty_id, "assigned": [], "shortfall": [{"platform": "any", "missing": 1}]}
        return {"duty": duty_id, "assigned": [pool[0].id], "shortfall": []}

    assigned: list[str] = []
    shortfall: list[dict] = []
    taken: set[str] = set()
    for plat, count in spread:
        got = 0
        for n in pool:
            if got == count:
                break
            if n.platform == plat and n.id not in taken:
                taken.add(n.id)
                assigned.append(n.id)
                got += 1
        if got < count:
            shortfall.append({"platform": plat, "missing": count - got})
    return {"duty": duty_id, "assigned": assigned, "shortfall": shortfall}


def assign_all(
    model: Model, nodes: list[NodeInfo], overrides: dict | None = None, local_id: str = "",
) -> dict[str, dict]:
    return {d: assign_duty(model, d, nodes, overrides, local_id) for d in model.duty_ids}


def slot_candidates(
    model: Model, duty_id: str, nodes: list[NodeInfo],
    overrides: dict | None = None, local_id: str = "",
    strategy: str | None = None,
) -> list[tuple[str, list[str]]]:
    """Per-slot failover lists for executing a dispatch. ``strategy`` overrides
    only the *ranking* (not eligibility or spread): the dispatcher passes
    ``surplus-first`` here so target selection is the load-balancing decision,
    independent of the duty's displayed placement strategy (07/11)."""
    policy = placement(model, duty_id, overrides)
    rank_strategy = strategy or policy.get("strategy", DEFAULT_STRATEGY)
    pool = ranked(eligible(nodes, duty_id, policy), rank_strategy, local_id)
    spread = _spread_of(policy)
    if not spread:
        return [("any", [n.id for n in pool])]
    slots: list[tuple[str, list[str]]] = []
    for plat, count in spread:
        of_plat = [n.id for n in pool if n.platform == plat]
        slots.extend((plat, of_plat) for _ in range(count))
    return slots
