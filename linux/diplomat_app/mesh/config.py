"""Mesh model + runtime configuration.

Layers, weakest to strongest:

1. the shared ``core/mesh.json`` (protocol constants, duty catalog, strategies);
2. ``DIPLOMAT_MESH_*`` environment overrides for the protocol knobs — how the
   tests run whole meshes on loopback with fast timeouts without touching the
   shared file;
3. gossiped last-writer-wins *placement overrides* (per-duty strategy /
   token-awareness / platform spread, edited live from the topology panel) —
   see :class:`PlacementOverrides`.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field, replace

from .. import core


def _has_non_finite(v: object) -> bool:
    """Whether ``v`` contains a non-finite float (∞/NaN) anywhere, recursing through
    dicts and lists. A gossiped placement override's duty dict is kept VERBATIM (see
    :meth:`PlacementOverrides.from_dict`) and re-serialized into the shared snapshot, so
    a signed peer that slips a non-finite float into any key would poison ``state.json``:
    ``json.dumps`` (allow_nan=True default) writes it as the bare RFC 8259-invalid token
    ``Infinity``/``NaN`` that a strict reader rejects WHOLESALE. Mirrors the advert-side
    guard in ``protocol.NodeInfo.from_dict``."""
    if isinstance(v, float):
        return not math.isfinite(v)
    if isinstance(v, dict):
        return any(_has_non_finite(x) for x in v.values())
    if isinstance(v, list):
        return any(_has_non_finite(x) for x in v)
    return False


# Env override names, mapped onto core/mesh.json "protocol" keys. Values are
# parsed with the type of the default they replace.
_ENV_KEYS = {
    "DIPLOMAT_MESH_MCAST_GROUP": "multicastGroup",
    "DIPLOMAT_MESH_MCAST_PORT": "multicastPort",
    "DIPLOMAT_MESH_TCP_BASE": "tcpPortBase",
    "DIPLOMAT_MESH_TCP_SPAN": "tcpPortSpan",
    "DIPLOMAT_MESH_BEACON_SECS": "beaconIntervalSecs",
    "DIPLOMAT_MESH_REDIAL_SECS": "redialIntervalSecs",
    "DIPLOMAT_MESH_HEARTBEAT_SECS": "heartbeatIntervalSecs",
    "DIPLOMAT_MESH_STALE_SECS": "peerStaleSecs",
    "DIPLOMAT_MESH_TIMEOUT_SECS": "peerTimeoutSecs",
    "DIPLOMAT_MESH_ACK_SECS": "dispatchAckTimeoutSecs",
    "DIPLOMAT_MESH_STATE_SECS": "stateWriteIntervalSecs",
    "DIPLOMAT_MESH_RESULT_RETRY_SECS": "foreignResultRetryIntervalSecs",
    "DIPLOMAT_MESH_RESULT_MAX_SECS": "foreignResultMaxSecs",
    "DIPLOMAT_MESH_FOREIGN_TIMEOUT_SECS": "foreignJobTimeoutSecs",
    "DIPLOMAT_MESH_COMPLETION_DEADLINE_SECS": "foreignCompletionDeadlineSecs",
    "DIPLOMAT_MESH_REMINDER_GRACE_SECS": "foreignReminderGraceSecs",
}


def protocol() -> dict:
    """The protocol constants with any DIPLOMAT_MESH_* env overrides applied."""
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
    """DIPLOMAT_MESH_LOOPBACK=1 keeps every socket on 127.0.0.1 — used by the
    integration tests (and demos) to run a whole mesh on one machine without
    touching the real LAN."""
    return os.environ.get("DIPLOMAT_MESH_LOOPBACK") == "1"


def secret() -> str:
    """Optional pre-shared join token (DIPLOMAT_MESH_SECRET, same value on every
    machine + in the CLI/panel environment). A node with a secret refuses peer
    links, control sessions, and therefore dispatches that don't present it.

    This is a fence, not cryptography — the token rides plaintext on the LAN.
    It keeps a stray machine (or a colleague's mesh on the same office network)
    from joining yours and receiving jobs; it does not defend against a hostile
    network. Empty (the default) = open mesh, fine for a home LAN.
    """
    return os.environ.get("DIPLOMAT_MESH_SECRET", "")


def tor_enabled() -> bool:
    """DIPLOMAT_MESH_TOR=1 turns on the Tor onion-service transport: the node runs
    a persistent onion service (a permanent ``.onion`` it advertises) and dials
    known-but-unseen peers over Tor with exponential backoff — WAN reachability
    with no public IP or domain. Off by default; when off, or when the ``tor``
    binary is missing, the node is LAN-only exactly as before. See mesh/tor.py."""
    return os.environ.get("DIPLOMAT_MESH_TOR") == "1"


def tor_bootstrap_timeout() -> float:
    """How long to wait for Tor to bootstrap before giving up and staying LAN-only
    (DIPLOMAT_MESH_TOR_BOOTSTRAP_SECS). Tor's first bootstrap can be slow; the node
    stays fully usable on the LAN in the meantime."""
    try:
        v = float(os.environ.get("DIPLOMAT_MESH_TOR_BOOTSTRAP_SECS", "90"))
    except ValueError:
        return 90.0
    # Reject non-finite / non-positive (e.g. "inf" from 1e999, "nan", "-1", "0"): a
    # non-finite timeout makes asyncio.wait block FOREVER — the opposite of the
    # docstring's "give up and stay LAN-only" — and a non-positive one is meaningless.
    return v if math.isfinite(v) and v > 0 else 90.0


def server_mode() -> bool:
    """DIPLOMAT_MESH_SERVER=1 makes this node a dedicated **server**: it accepts and
    runs requests but NEVER originates a dispatch to peers. A request it is asked
    to route (via a control session or the CLI) runs on **itself** instead of
    being fanned out. Combined with :func:`api_key` this is the spec's
    accept-only, optionally API-key-authenticated server role — a beefy shared
    box that takes work but never pushes work onto anyone else."""
    return os.environ.get("DIPLOMAT_MESH_SERVER") == "1"


def api_key() -> str:
    """Optional per-node API key (DIPLOMAT_MESH_API_KEY). When set, this node
    requires a matching ``apiKey`` field on inbound control sessions and inbound
    dispatch requests, refusing any that lack it. It is **independent of** the
    mesh-wide join :func:`secret`: the secret fences who may *join* the mesh; the
    API key authenticates who may submit *work* to this (typically server) node,
    without granting mesh membership or device trust. Empty = no API-key gate."""
    return os.environ.get("DIPLOMAT_MESH_API_KEY", "")


def foreign_spawn() -> str:
    """Optional **confinement runner** (DIPLOMAT_MESH_FOREIGN_SPAWN) — the command
    template a node uses to run a *foreign* SzpontRequest under zero trust. Its
    presence is what turns a foreign request from **declined** into **confined,
    response-only execution**: the untrusted ``prompt`` runs inside the operator's
    own sandbox (a container/VM/jailed process — *the node's own responsibility to
    isolate*), which the template names, with ``{prompt_file}`` and ``{result_file}``
    substituted. The node then returns the sandbox's ``{result_file}`` to the
    originator as a ``job-result``; the originator performs any social action itself.

    Empty (the default) = **no foreign execution**: a foreign request is declined,
    exactly as before. A node only ever runs a stranger's compute when the operator
    has explicitly supplied the jail to run it in. See
    docs/szpontnet/13-foreign-execution.md."""
    return os.environ.get("DIPLOMAT_MESH_FOREIGN_SPAWN", "")


def on_result() -> str:
    """Optional **result handler** (DIPLOMAT_MESH_ON_RESULT) — the command template an
    originator runs when a foreign executor returns a ``job-result`` for a request it
    dispatched. This is where the **social action runs under the originator's own
    identity** (e.g. ``gh pr review``): ``{result_file}`` holds the executor's
    computed artifact plus the job metadata. Empty = the originator just records the
    result (no automatic action). See docs/szpontnet/13-foreign-execution.md."""
    return os.environ.get("DIPLOMAT_MESH_ON_RESULT", "")


def extend_decider() -> str:
    """Optional **extension decider** (DIPLOMAT_MESH_EXTEND_DECIDER) — the command
    template an originator runs to judge a late foreign executor's ``job-progress``
    plea: whether "still working" is a valid reason to extend its completion
    deadline. ``{job_file}`` is substituted with a JSON file carrying the case (the
    job, who the executor is, when it accepted, extensions so far, and the plea);
    exit status 0 extends (re-arms the full deadline window), anything else — a
    non-zero exit, a crash, or a timeout — bans. The operator typically points this
    at an agent, the same pattern as DIPLOMAT_MESH_FOREIGN_SPAWN / ON_RESULT.

    Empty (the default) = **no extension is ever granted**: a progress plea then
    cannot save a late executor, and it is banned — the zero-trust default. See
    docs/szpontnet/13-foreign-execution.md#the-extension-decision."""
    return os.environ.get("DIPLOMAT_MESH_EXTEND_DECIDER", "")


def default_trust() -> str:
    """The trust level a node applies to an **unknown** device — one whose proven
    fingerprint is not in the local allowlist (and to any unverified/keyless peer,
    which can never match the allowlist). ``DIPLOMAT_MESH_DEFAULT_TRUST`` overrides the
    shipped baseline in ``core/mesh.json`` (``trust.default``).

    Ships as **foreign** (zero-trust by default): a device you have not explicitly
    marked *personal* is untrusted — its requests are declined (or, with a
    confinement runner, run confined and response-only), it cannot mutate this node
    via ``set-attr``, and it can never own work. The operator promotes the devices
    they trust one at a time (the allowlist is that set of exceptions). Set this to
    ``personal`` to restore the pre-trust **full-altruism** mode — every unlisted
    peer is trusted, exactly as a fresh mesh behaved before the default became
    configurable. An unrecognised value falls back to ``foreign`` (the safe default).
    This is the node-wide baseline; the operator's live choice is persisted in
    ``trusted.json`` (:func:`trust.load_default_level`) and edited from the panel."""
    raw = os.environ.get("DIPLOMAT_MESH_DEFAULT_TRUST", "").strip().lower()
    if raw in ("personal", "foreign"):
        return raw
    baseline = str(core.mesh().get("trust", {}).get("default", "foreign")).strip().lower()
    return baseline if baseline in ("personal", "foreign") else "foreign"


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
            except (TypeError, ValueError, OverflowError):
                return 1.0
    return 1.0


def job_cost_units() -> float:
    """How much quota one spawned SzpontRequest books, in capacity units."""
    try:
        return float(accounts().get("jobCostUnits", 1.0))
    except (TypeError, ValueError, OverflowError):
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


def tier_label(tier: int) -> str:
    """Human word for a strength tier ('Very strong' … 'Very light'), from the
    shared model's ``tiers.labels``; falls back to ``tier N`` if unlabelled."""
    labels = core.mesh()["tiers"].get("labels", {})
    return labels.get(str(tier), f"tier {tier}")


def tokens_per_weight() -> float:
    """Heuristic per-window token ceiling for a weight-1 (Pro) plan. Scaled by a
    plan's weight to get its ceiling (see usage.py)."""
    try:
        return float(accounts().get("tokensPerWeight", 2_000_000))
    except (TypeError, ValueError, OverflowError):
        return 2_000_000.0


def usage_window_hours() -> float:
    """Trailing window over which local token consumption is measured."""
    try:
        return float(accounts().get("usageWindowHours", 5.0)) or 5.0
    except (TypeError, ValueError, OverflowError):
        return 5.0


def low_threshold() -> float:
    """Remaining-fraction boundary below which the token state drops to 'low'."""
    try:
        return float(accounts().get("lowThreshold", 0.34))
    except (TypeError, ValueError, OverflowError):
        return 0.34


# MARK: - Placement (per-duty policy) + LWW overrides


@dataclass(frozen=True)
class Placement:
    """The resolved placement policy for one duty."""

    strategy: str
    token_aware: bool
    # [(platform, count)] the duty must cover; empty = any one node.
    spread: tuple[tuple[str, int], ...] = ()

    @classmethod
    def from_dict(cls, d: object) -> "Placement":
        if not isinstance(d, dict):
            # A per-duty placement VALUE can arrive over gossip (overrides) as junk
            # (a non-object) and is dereferenced here at assign time via placement_for
            # — tolerate it exactly like _parse_spread tolerates its entries, or one
            # poisoned override crashes every _recompute (a persistent, mesh-wide DoS).
            # A non-mapping resolves to the schema default.
            d = {}
        return cls(
            strategy=d.get("strategy", core.mesh()["defaultStrategy"]),
            token_aware=bool(d.get("tokenAware", True)),
            spread=cls._parse_spread(d.get("spread", [])),
        )

    @staticmethod
    def _parse_spread(raw: object) -> tuple[tuple[str, int], ...]:
        """Parse the ``[(platform, count)]`` spread, tolerating malformed entries
        the way the rest of the wire layer tolerates garbage. This resolves on
        every ``_recompute`` from placement dicts that can arrive over gossip
        (``overrides``) or a ctl edit, so an entry that isn't a mapping, names no
        platform, or carries a non-integer count must be SKIPPED / defaulted, not
        allowed to crash assignment. A bad or non-positive count falls back to the
        schema default 1."""
        out: list[tuple[str, int]] = []
        if not isinstance(raw, list):
            return ()
        for s in raw:
            if not isinstance(s, dict):
                continue
            platform = s.get("platform")
            if not isinstance(platform, str) or not platform:
                continue
            try:
                count = int(s.get("count", 1))
            except (TypeError, ValueError, OverflowError):
                count = 1
            if count < 1:
                # A non-positive count is a semantically bad count — a spread entry
                # dispatches to >= 1 node — so fall back to the schema default 1, the
                # same as the parse-exception branch above. Left unnormalized, a valid
                # negative/zero int diverges the two placement consumers that both read
                # this spread: assign_duty counts the duty SATISFIED (its `got == count`
                # loop never trips and `got < count` is false, so it records no
                # shortfall) while slot_candidates yields range(count) == 0 slots and
                # dispatches to NOBODY — a duty that looks placed but silently never runs.
                count = 1
            out.append((platform, count))
        return tuple(out)

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
    # Base64 Ed25519 signature by the ``updated_by`` node over this override's
    # canonical form, so a relay can't forge or tamper with a mesh-wide placement
    # edit. Empty for a legacy/keyless editor (then unauthenticated). See node.py.
    sig: str = ""

    @classmethod
    def from_dict(cls, d: dict | None) -> "PlacementOverrides":
        d = d or {}
        raw_duties = d.get("duties")
        return cls(
            rev=cls._as_rev(d.get("rev", 0)),
            updated_by=str(d.get("updatedBy", "")),
            # Keep only mapping duty VALUES, and only ones free of non-finite floats:
            # a non-object value is junk that would crash placement_for at assign time,
            # and a signed peer can slip a bare ∞/NaN into any key of an otherwise-valid
            # duty dict (kept VERBATIM here and re-serialized into the snapshot) to write
            # the RFC 8259-invalid tokens Infinity/NaN into state.json and blank a strict
            # reader's topology mesh-wide — the override-path twin of the advert-side
            # dutiesEnabled/stats guard. Either way the offending duty is dropped at
            # ingestion and falls back to its catalog default.
            duties={k: v for k, v in raw_duties.items()
                    if isinstance(v, dict) and not _has_non_finite(v)}
            if isinstance(raw_duties, dict) else {},
            sig=str(d.get("sig", "")),
        )

    @staticmethod
    def _as_rev(raw: object) -> int:
        """The LWW rev, tolerating garbage the way the rest of the wire layer does
        (cf. :meth:`Placement._parse_spread`): a gossiped override arrives from a
        peer/hello and a non-numeric ``rev`` (null, a list, a string) must default
        to 0, not raise — a malformed edit is simply the default (no) edit."""
        try:
            return int(raw)
        except (TypeError, ValueError, OverflowError):
            # OverflowError: a JSON rev of 1e999 parses to float('inf'), and int(inf)
            # raises it (an ArithmeticError, not a ValueError) — treat as the default 0.
            return 0

    def to_dict(self) -> dict:
        d = {"rev": self.rev, "updatedBy": self.updated_by, "duties": self.duties}
        if self.sig:
            d["sig"] = self.sig
        return d

    def wins_over(self, other: "PlacementOverrides") -> bool:
        return (self.rev, self.updated_by) > (other.rev, other.updated_by)

    def with_duty(self, duty_id: str, placement: Placement, by: str) -> "PlacementOverrides":
        duties = dict(self.duties)
        duties[duty_id] = placement.to_dict()
        return PlacementOverrides(rev=self.rev + 1, updated_by=by, duties=duties)

    def signed(self, sig: str) -> "PlacementOverrides":
        return replace(self, sig=sig)


def placement_for(duty_id: str, overrides: PlacementOverrides | None = None) -> Placement:
    """The effective placement for a duty: the gossiped override if present,
    else the core/mesh.json default."""
    if overrides and duty_id in overrides.duties:
        return Placement.from_dict(overrides.duties[duty_id])
    duty = duty_by_id(duty_id)
    return Placement.from_dict(duty["placement"] if duty else {})
