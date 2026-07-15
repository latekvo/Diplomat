"""Mesh model + runtime configuration.

Layers, weakest to strongest:

1. the shared ``core/mesh.json`` (protocol constants, duty catalog, strategies);
2. ``ARGENT_MESH_*`` environment overrides for the protocol knobs — how the
   tests run whole meshes on loopback with fast timeouts without touching the
   shared file;
3. gossiped last-writer-wins *placement overrides* (per-duty strategy /
   token-awareness / platform spread, edited live from the topology panel) —
   see :class:`PlacementOverrides`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .. import core

# Env override names, mapped onto core/mesh.json "protocol" keys. Values are
# parsed with the type of the default they replace.
_ENV_KEYS = {
    "ARGENT_MESH_MCAST_GROUP": "multicastGroup",
    "ARGENT_MESH_MCAST_PORT": "multicastPort",
    "ARGENT_MESH_TCP_BASE": "tcpPortBase",
    "ARGENT_MESH_TCP_SPAN": "tcpPortSpan",
    "ARGENT_MESH_BEACON_SECS": "beaconIntervalSecs",
    "ARGENT_MESH_HEARTBEAT_SECS": "heartbeatIntervalSecs",
    "ARGENT_MESH_STALE_SECS": "peerStaleSecs",
    "ARGENT_MESH_TIMEOUT_SECS": "peerTimeoutSecs",
    "ARGENT_MESH_ACK_SECS": "dispatchAckTimeoutSecs",
    "ARGENT_MESH_STATE_SECS": "stateWriteIntervalSecs",
}


def protocol() -> dict:
    """The protocol constants with any ARGENT_MESH_* env overrides applied."""
    out = dict(core.mesh()["protocol"])
    for env, key in _ENV_KEYS.items():
        raw = os.environ.get(env)
        if raw is None:
            continue
        default = out.get(key)
        try:
            if isinstance(default, bool):  # not used today; guard against int() eating it
                out[key] = raw == "1"
            elif isinstance(default, int):
                out[key] = int(raw)
            elif isinstance(default, float):
                out[key] = float(raw)
            else:
                out[key] = raw
        except ValueError:
            pass  # a malformed override falls back to the shared default
    return out


def loopback_only() -> bool:
    """ARGENT_MESH_LOOPBACK=1 keeps every socket on 127.0.0.1 — used by the
    integration tests (and demos) to run a whole mesh on one machine without
    touching the real LAN."""
    return os.environ.get("ARGENT_MESH_LOOPBACK") == "1"


def secret() -> str:
    """Optional pre-shared join token (ARGENT_MESH_SECRET, same value on every
    machine + in the CLI/panel environment). A node with a secret refuses peer
    links, control sessions, and therefore dispatches that don't present it.

    This is a fence, not cryptography — the token rides plaintext on the LAN.
    It keeps a stray machine (or a colleague's mesh on the same office network)
    from joining yours and receiving jobs; it does not defend against a hostile
    network. Empty (the default) = open mesh, fine for a home LAN.
    """
    return os.environ.get("ARGENT_MESH_SECRET", "")


def accounts() -> dict:
    """The subscription-plan + accounting knobs (plan weights, capacity, quota
    window, usage time-constant) behind per-node load balancing."""
    return core.mesh().get("accounts", {})


def plan_weight(plan_id: str) -> float:
    """Quota capacity of a plan relative to Pro (Max 5× → 5, Max 20× → 20).
    An unknown plan weighs 1.0 (Pro-equivalent) — safe, never an error."""
    for p in accounts().get("plans", []):
        if p.get("id") == plan_id:
            try:
                return float(p.get("weight", 1.0))
            except (TypeError, ValueError):
                return 1.0
    return 1.0


def job_cost_units() -> float:
    """How much quota one spawned SzpontRequest books, in capacity units."""
    try:
        return float(accounts().get("jobCostUnits", 1.0))
    except (TypeError, ValueError):
        return 1.0


def dispatch_strategy() -> str:
    """The ranking a dispatcher uses to pick a target — the load-balancing
    decision, made unilaterally from its own view (no consensus). Defaults to
    surplus-first so requests flow to whoever has the most spare quota."""
    return str(core.mesh().get("dispatchStrategy", "surplus-first"))


def duty_ids() -> list[str]:
    return [d["id"] for d in core.mesh()["duties"]]


def duty_by_id(duty_id: str) -> dict | None:
    return next((d for d in core.mesh()["duties"] if d["id"] == duty_id), None)


def tier_bounds() -> tuple[int, int, int]:
    """(min, max, default) machine tier from the shared model."""
    t = core.mesh()["tiers"]
    return t["min"], t["max"], t["default"]


# MARK: - Placement (per-duty policy) + LWW overrides


@dataclass(frozen=True)
class Placement:
    """The resolved placement policy for one duty."""

    strategy: str
    token_aware: bool
    # [(platform, count)] the duty must cover; empty = any one node.
    spread: tuple[tuple[str, int], ...] = ()

    @classmethod
    def from_dict(cls, d: dict) -> "Placement":
        return cls(
            strategy=d.get("strategy", core.mesh()["defaultStrategy"]),
            token_aware=bool(d.get("tokenAware", True)),
            spread=tuple(
                (s["platform"], int(s.get("count", 1))) for s in d.get("spread", [])
            ),
        )

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "tokenAware": self.token_aware,
            "spread": [{"platform": p, "count": c} for p, c in self.spread],
        }


@dataclass(frozen=True)
class PlacementOverrides:
    """Mesh-wide placement edits, gossiped last-writer-wins.

    ``rev`` is a lamport-ish counter: every edit bumps it past the highest rev
    seen anywhere, so concurrent edits converge on the same winner everywhere
    (ties broken by ``updated_by``). ``duties`` maps duty id → placement dict
    (the full policy, not a diff).
    """

    rev: int = 0
    updated_by: str = ""
    duties: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict | None) -> "PlacementOverrides":
        d = d or {}
        return cls(
            rev=int(d.get("rev", 0)),
            updated_by=str(d.get("updatedBy", "")),
            duties=dict(d.get("duties", {})),
        )

    def to_dict(self) -> dict:
        return {"rev": self.rev, "updatedBy": self.updated_by, "duties": self.duties}

    def wins_over(self, other: "PlacementOverrides") -> bool:
        return (self.rev, self.updated_by) > (other.rev, other.updated_by)

    def with_duty(self, duty_id: str, placement: Placement, by: str) -> "PlacementOverrides":
        duties = dict(self.duties)
        duties[duty_id] = placement.to_dict()
        return PlacementOverrides(rev=self.rev + 1, updated_by=by, duties=duties)


def placement_for(duty_id: str, overrides: PlacementOverrides | None = None) -> Placement:
    """The effective placement for a duty: the gossiped override if present,
    else the core/mesh.json default."""
    if overrides and duty_id in overrides.duties:
        return Placement.from_dict(overrides.duties[duty_id])
    duty = duty_by_id(duty_id)
    return Placement.from_dict(duty["placement"] if duty else {})
