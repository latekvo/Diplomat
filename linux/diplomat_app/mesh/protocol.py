"""Wire protocol: NDJSON messages over TCP links, JSON beacons over UDP.

Pure encode/decode — no sockets. Everything is tolerant of unknown fields and
newer minor revisions (a peer running a newer build must not wedge an older
one); a message that doesn't parse is dropped, never fatal.

Message types (``t``):

- ``beacon``    (UDP)  presence advert: id, name, platform, tcpPort, epoch
- ``hello``     (TCP)  first message on a peer link, both directions: NodeInfo +
                       overrides + a per-connection ``nonce`` (the trust challenge)
- ``auth``      (TCP)  proof of possession: a signature over the peer's hello nonce,
                       so trust binds to a key the peer can't fake, not a claimed field
- ``ctl``       (TCP)  first message on a *control* connection (the panel / CLI
                       talking to its local node) — not a peer
- ``heartbeat`` (TCP)  link liveness
- ``node``      (TCP)  gossiped NodeInfo update (attrs changed, peers-seen changed)
- ``overrides`` (TCP)  gossiped LWW placement overrides
- ``set-attr``  (TCP)  edit a node's local attrs (from a peer's panel or the CLI)
- ``dispatch``  (TCP)  run a job on the receiving node
- ``job-status``(TCP)  dispatch outcome: ``spawned`` | ``declined`` | ``failed`` (+ reason)
- ``job-result``(TCP)  the computed artifact a FOREIGN request returns to its originator
                       (who then performs any social action itself); re-sent until acked
- ``job-ack``   (TCP)  the originator's acknowledgement of a ``job-result`` (reliable delivery)
- ``job-reminder``(TCP) the originator's "is this ready?" — a foreign-accepted job passed
                       its completion deadline without a result (accountability)
- ``job-progress``(TCP) the executor's reply to a reminder when the work is still
                       running: a status note, its case for a deadline extension
- ``work-claim``(TCP)  gossiped, self-signed origination lease on a unit of work
- ``status``    (TCP)  ctl request: reply with one ``state`` message (the snapshot)
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field, replace

PROTOCOL_VERSION = 1

# What an advert carrying no usable surplus signal ranks as. 1.0 is definitional,
# not a tunable: surplus is a burn-down ratio (budget left ÷ clock left), so 1.0
# is exactly on the line — the honest neutral prior for "unknown", ordering such a
# node between the peers that are ahead of pace and those that are behind.
NEUTRAL_SURPLUS = 1.0

# Granularity at which surpluses are compared. Pace drifts continuously — the
# clock-left denominator shrinks every second even when nothing is spent — so
# comparing raw floats would reshuffle rankings, and re-gossip adverts, on noise
# alone. Quantising gives the ordering hysteresis: a node only overtakes a peer
# on a difference big enough to mean something, and otherwise the stable tier/id
# tie-breaks decide, which keeps displayed duty ownership from flapping.
SURPLUS_RANK_BUCKET = 0.05


# The widest surplus the ranking distinguishes. surplus is a burn-down ratio the
# account owner caps at usage.PACE_CAP (10.0): a peer advertising more cannot be
# "more than maximally flush", and a hostile finite-but-huge advert (1e307 — a FINITE
# float, so it passes the non-finite ingestion guard) or a negative one (-1e307) would
# make ``value / SURPLUS_RANK_BUCKET`` overflow to ±inf and ``round()`` raise an
# uncaught OverflowError on the (default) surplus-first ranking path. Kept a local
# literal mirroring usage.PACE_CAP so this ranking-side module needs no probe import.
SURPLUS_RANK_CAP = 10.0


def surplus_bucket(value: float) -> int:
    """A surplus quantised to :data:`SURPLUS_RANK_BUCKET`, as a comparable index.

    Clamped to ``[0, SURPLUS_RANK_CAP]`` first: a legitimate surplus never leaves that
    range, but a hostile finite-but-huge (or negative) advertised value would otherwise
    overflow ``round(value / SURPLUS_RANK_BUCKET)`` and crash surplus-first ranking. The
    clamp also stops such a value from out-ranking a genuinely maximally-flush peer."""
    return round(max(0.0, min(value, SURPLUS_RANK_CAP)) / SURPLUS_RANK_BUCKET)

# A guard against garbage/hostile blobs on the mesh port, not a real limit —
# a dispatch carries a whole review prompt (tens of KB).
MAX_LINE_BYTES = 512 * 1024

# Domain-separation tags for the two gossiped, self-signed payloads. A signature
# always covers <tag> || <canonical JSON of the payload without its own `sig`>, so
# a signature is meaningless outside its exact context and can't be lifted from one
# payload type to another. Canonical = sorted keys + compact separators, so every
# implementation signs and verifies byte-identical input.
_ADVERT_CONTEXT = b"szpontnet-nodeinfo-v1:"
_OVERRIDES_CONTEXT = b"szpontnet-overrides-v1:"
# A gossiped work-claim (an origination lease on a unit of work) is signed the
# same way, under its own tag — so a claim signature can't be lifted onto an
# advert/override or vice versa. See docs/szpontnet/12-work-claims.md.
_CLAIM_CONTEXT = b"szpontnet-workclaim-v1:"
# A `job-result` — the computed artifact a **foreign** SzpontRequest returns to its
# originator (who then performs any social action itself) — is signed under its own
# tag so the originator can bind the result to the executor's key. Same canonical
# construction. See docs/szpontnet/13-foreign-execution.md.
_RESULT_CONTEXT = b"szpontnet-jobresult-v1:"


def _canonical(payload: dict) -> bytes:
    """Deterministic JSON of a payload with its `sig` field removed — the bytes a
    signature is computed/verified over. Sorted keys + compact so it is identical
    across implementations, and taken over the RAW received dict (never a re-parse)
    so any unknown future field the signer covered is covered here too."""
    body = {k: v for k, v in payload.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def advert_signing_bytes(node_dict: dict) -> bytes:
    """The exact bytes a NodeInfo advertisement's `sig` covers."""
    return _ADVERT_CONTEXT + _canonical(node_dict)


def overrides_signing_bytes(overrides_dict: dict) -> bytes:
    """The exact bytes a placement-overrides `sig` covers (signed by `updatedBy`)."""
    return _OVERRIDES_CONTEXT + _canonical(overrides_dict)


def claim_signing_bytes(claim_dict: dict) -> bytes:
    """The exact bytes a work-claim's `sig` covers (signed by the claimant `node`
    over the record's canonical form)."""
    return _CLAIM_CONTEXT + _canonical(claim_dict)


def result_signing_bytes(result_payload: dict) -> bytes:
    """The exact bytes a `job-result`'s `sig` covers (signed by the executor over
    the canonical form of ``{"id", "node", "result"}`` — the correlation id, the
    executor id, and the computed payload). Binds the returned artifact to the
    executor's key so a relay or a third peer on the link can't forge it."""
    return _RESULT_CONTEXT + _canonical(result_payload)


def _opt_frac(v: object) -> float | None:
    """An optional [0, 1]-clamped fraction from the wire; None when absent/garbage.
    ``max``/``min`` also fold a non-finite input to a finite bound (∞→1.0, −∞/NaN→…),
    so an optional display fraction can never carry ∞/NaN into serialization."""
    if v is None:
        return None
    try:
        return max(0.0, min(1.0, float(v)))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return None


def _finite(v: object) -> float:
    """``float(v)`` for a REQUIRED numeric field, but a **non-finite** result raises
    ``ValueError`` so the caller's malformed-input guard drops the whole record.

    ``float('inf')`` / ``float('nan')`` do NOT raise (unlike ``int(inf)``, the
    OverflowError class), so without this check a peer's advert with ``tokensPct: 1e999``
    or ``epoch: NaN`` (JSON ``1e999`` parses to ∞) yields a *valid* NodeInfo carrying ∞.
    That poisons two things: (1) ``epoch`` is the freshness key, so an ∞ epoch
    out-freshes every honest advert forever (a gossip-poisoning DoS), and (2) ∞/NaN
    serialize (``json.dumps`` defaults to ``allow_nan=True``) as the bare tokens
    ``Infinity``/``NaN`` — **RFC 8259-invalid** JSON that a strict reader rejects
    WHOLESALE, so the shared ``state.json`` snapshot and the ctl ``status`` reply
    become undecodable and the Swift topology panel blanks for EVERY node. Dropping the
    malformed advert (return None, as from_dict already does for OverflowError) keeps
    non-finite out of the model, the snapshot, and the verbatim gossip relay alike."""
    f = float(v)  # type: ignore[arg-type]  # TypeError/ValueError/OverflowError propagate
    if not math.isfinite(f):
        raise ValueError("non-finite float in a required numeric field")
    return f


def _frac(v: object) -> float:
    """A REQUIRED [0, 1] fraction from the wire (``tokensPct``): finite via ``_finite`` (a
    non-finite value still drops the whole advert), then CLAMPED to [0, 1]. The clamp
    closes the finite-but-huge sibling of the non-finite hazard above: a peer's
    ``tokensPct: 1e300`` is valid RFC-8259 JSON, so unlike ∞/NaN it does NOT blank the
    strict Swift decoder — but the snapshot renders it as ``Int((pct * 100).rounded())``,
    a TRAPPING Double→Int conversion that crashes the macOS app on an out-of-range
    magnitude. Clamping keeps the advertised fraction inside its documented range."""
    return min(1.0, max(0.0, _finite(v)))


def _reject_non_finite(v: object) -> None:
    """Raise ``ValueError`` if ``v`` contains a non-finite float anywhere (recursing
    through dicts and lists) — an advert's ``stats`` and ``dutiesEnabled`` are
    attacker-shaped blobs that ``to_dict`` serializes verbatim, so a nested/list ∞/NaN
    would poison the snapshot just as a top-level one does. A numeric STRING that
    coerces to a non-finite float (``"1e400"``/``"1e999"``/``"inf"``/``"nan"``) is
    rejected too: it slips past the float-instance check yet drives ``surplus()``'s
    ``float()`` to ±inf/nan (``float(str)`` returns non-finite WITHOUT raising), writing
    the same bare ``Infinity``/``NaN`` token into the snapshot. A non-numeric string (a
    plan name, a duty id) doesn't parse as a float and is left untouched."""
    if isinstance(v, float):
        if not math.isfinite(v):
            raise ValueError("non-finite float in a verbatim wire dict")
    elif isinstance(v, str):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return
        if not math.isfinite(f):
            raise ValueError("non-finite numeric string in a verbatim wire dict")
    elif isinstance(v, int):
        # A JSON integer literal too big to hold in a float (a 400-digit number)
        # decodes to a Python bigint: it is neither a float instance nor a string,
        # so it slips past both guards above, yet surplus()'s float(stats["surplus"])
        # — whose except is only KeyError/TypeError/ValueError — raises an UNcaught
        # OverflowError on it (and round(surplus(), 3) into the snapshot the same).
        # Same "drop the advert" class as a non-finite float or numeric string.
        # (bool is an int subclass; float(True/False) is finite, so JSON true/false
        # in dutiesEnabled passes untouched.)
        try:
            if not math.isfinite(float(v)):
                raise ValueError("non-finite integer in a verbatim wire dict")
        except OverflowError:
            raise ValueError("integer too large for a finite float in a verbatim wire dict")
    elif isinstance(v, dict):
        for x in v.values():
            _reject_non_finite(x)
    elif isinstance(v, list):
        for x in v:
            _reject_non_finite(x)


def _finite_stats(raw: object) -> dict:
    """The advert's optional ``stats`` sub-dict, requiring every float value (recursively,
    through nested dicts AND lists) be finite — a non-finite one raises ``ValueError`` so
    from_dict drops the advert. Keeps ∞/NaN out of ``surplus()`` (an ∞ ``quotaLeft`` would
    make an attacker always rank surplus-first) and out of the serialized snapshot."""
    if not isinstance(raw, dict):
        return {}
    _reject_non_finite(raw)
    return dict(raw)


def _finite_duties(raw: object) -> dict:
    """The advert's ``dutiesEnabled`` map (duty id → enabled bool). Coerced with
    ``dict()`` so a non-mapping still drops the whole advert exactly as before, but with
    every value required FINITE (recursing dicts/lists via ``_reject_non_finite``): the
    map is re-serialized VERBATIM by ``to_dict`` into the snapshot, so a non-finite float
    here — which ``float()`` accepts but ``json.dumps`` (allow_nan=True default) writes as
    the bare RFC 8259-invalid token ``Infinity``/``NaN`` — would blank the strict Swift
    topology reader and spread via the verbatim gossip relay, exactly like a poisoned
    ``tokensPct``/``epoch``/``stats`` would. Round 11 guarded those three; ``dutiesEnabled``
    is the same class."""
    out = dict(raw)  # a non-mapping raises TypeError → from_dict drops the advert (unchanged)
    _reject_non_finite(out)
    return out


# MARK: - NodeInfo (the gossiped view of one node)


@dataclass(frozen=True)
class NodeInfo:
    id: str
    name: str
    platform: str  # "linux" | "macos" | ...
    tier: int
    tokens: str  # the EFFECTIVE token state: "ok" | "low" | "out"
    # Display hints (additive): whether tier was auto-detected from hardware, and
    # whether the token state is auto-derived from real usage (vs a manual pin).
    strength_auto: bool = True
    tokens_auto: bool = True
    # Fraction of the token budget still remaining (1.0 = fresh, 0.0 = out), so
    # the console shows a live "quota NN%" for every node, not just self. The
    # binding value: min(session, week) when the real probe answers, else the
    # local heuristic estimate.
    tokens_pct: float = 1.0
    # Real remaining-quota fractions per rate-limit window (5-hour session,
    # 7-day week) when the node's OAuth usage probe has them. None when the node
    # is on the heuristic fallback — then OMITTED from the wire (additive fields,
    # like pubkey/stats, so older builds interop unchanged).
    tokens_session_pct: float | None = None
    tokens_week_pct: float | None = None
    tcp_port: int = 0
    epoch: float = 0.0  # process start time — a restart bumps it (new incarnation)
    seq: int = 0  # per-node update counter; receivers keep the highest
    sees: tuple[str, ...] = ()  # peer ids this node currently holds links to
    duties_enabled: dict = field(default_factory=dict)
    # The node's advertised Ed25519 public key (base64). It is the node's *claimed*
    # trust identity - but advertising it grants NOTHING: a peer is only believed
    # to hold this key once it signs a fresh per-connection nonce with the matching
    # private key ([crypto]/[node] handshake). Trust then keys on this key's
    # fingerprint against a LOCAL allowlist ([trust]), never on any claimed field.
    pubkey: str = ""
    # This node's permanent Tor v3 onion address ("<56-base32>.onion"), when it
    # runs an onion service (DIPLOMAT_MESH_TOR). Additive and OMITTED from the wire
    # when empty, like pubkey/stats, so a LAN-only or older node interops unchanged.
    # It is the node's stable, NAT-independent reachability handle: a peer that met
    # this node on the LAN learns it here (in the very first signed hello) and can
    # redial it over Tor from anywhere. Carried INSIDE the signed advert, so it is
    # bound to the device key end to end — a relay cannot swap it to redirect a Tor
    # dial (the onion is self-authenticating too, but the handshake still re-proves
    # the device key, so a wrong onion just lands foreign). See mesh/tor.py.
    onion: str = ""
    # Load-balancing accounting, additive: {"plan", "usageAvg", "quotaLeft",
    # "surplus"} — surplus is the burn-down ratio ranked on, the rest display-only.
    # Empty when a node advertises no stats — its dispatch surplus is then
    # NEUTRAL_SURPLUS (1.0, on the pace line), so surplus-first ranking degrades to
    # weakest-first. See stats.py.
    stats: dict = field(default_factory=dict)
    # Base64 Ed25519 signature by THIS node's device key over the canonical form of
    # this advertisement ([advert_signing_bytes]). It authenticates the advert end
    # to end: any relay can forward it, but none can forge or tamper with it without
    # the private key. Empty for a keyless node (no `pubkey`), which is then
    # unauthenticated and treated as foreign under any allowlist. See node.py.
    sig: str = ""
    version: int = PROTOCOL_VERSION

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "platform": self.platform,
            "tier": self.tier,
            "tokens": self.tokens,
            "strengthAuto": self.strength_auto,
            "tokensAuto": self.tokens_auto,
            "tokensPct": round(self.tokens_pct, 3),
            "tcpPort": self.tcp_port,
            "epoch": self.epoch,
            "seq": self.seq,
            "sees": list(self.sees),
            "dutiesEnabled": self.duties_enabled,
            "v": self.version,
        }
        # Omit the additive fields when empty so v1 advertisements stay
        # byte-identical to before (and interop traces don't churn).
        if self.tokens_session_pct is not None:
            d["tokensSessionPct"] = round(self.tokens_session_pct, 3)
        if self.tokens_week_pct is not None:
            d["tokensWeekPct"] = round(self.tokens_week_pct, 3)
        if self.pubkey:
            d["pubkey"] = self.pubkey
        if self.onion:
            d["onion"] = self.onion
        if self.stats:
            d["stats"] = self.stats
        if self.sig:
            d["sig"] = self.sig
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "NodeInfo | None":
        try:
            return cls(
                id=str(d["id"]),
                name=str(d.get("name", "?")),
                platform=str(d.get("platform", "unknown")),
                tier=int(d.get("tier", 3)),
                tokens=str(d.get("tokens", "ok")),
                strength_auto=bool(d.get("strengthAuto", True)),
                tokens_auto=bool(d.get("tokensAuto", True)),
                tokens_pct=_frac(d.get("tokensPct", 1.0)),
                tokens_session_pct=_opt_frac(d.get("tokensSessionPct")),
                tokens_week_pct=_opt_frac(d.get("tokensWeekPct")),
                tcp_port=int(d.get("tcpPort", 0)),
                epoch=_finite(d.get("epoch", 0.0)),
                seq=int(d.get("seq", 0)),
                sees=tuple(str(s) for s in d.get("sees", [])),
                duties_enabled=_finite_duties(d.get("dutiesEnabled", {})),
                pubkey=str(d.get("pubkey", "")),
                onion=str(d.get("onion", "")),
                stats=_finite_stats(d.get("stats")),
                sig=str(d.get("sig", "")),
                version=int(d.get("v", PROTOCOL_VERSION)),
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            # A malformed advert is dropped (return None), never tears the link. Covers:
            # OverflowError — a JSON literal like 1e999 parses to float('inf') and int(inf)
            # raises it (an ArithmeticError, NOT a ValueError); and ValueError from
            # _finite/_finite_stats/_finite_duties — a NON-FINITE float (∞/NaN) that
            # float() accepts but must not enter the model or the serialized snapshot
            # (see _finite); tokensPct/epoch, stats, and dutiesEnabled are all guarded.
            return None

    def surplus(self) -> float:
        """Spare quota this node advertises for load balancing, as a burn-down
        ratio: budget left over clock left until its quota resets (see
        ``stats.NodeStats.surplus``). 1.0 is exactly on pace, above is flush,
        below is rationing.

        The figure is computed by the node that owns the account, because only it
        holds the reset instants — pacing a peer's numbers here would compare
        timestamps across machines whose clocks disagree.

        ``NEUTRAL_SURPLUS`` when the node advertises nothing usable: no stats at
        all, or a peer on a build old enough to still advertise only the absolute
        ``quotaLeft``/``usageAvg`` pair. Those absolute figures are deliberately
        NOT converted — they are a different scale (plan-relative capacity units,
        commonly >1) and mixing the two in one ordering would let a legacy advert
        outrank every paced node. Such a peer ranks neutrally until it upgrades."""
        if not self.stats:
            return NEUTRAL_SURPLUS
        try:
            return float(self.stats["surplus"])
        # OverflowError: a bigint surplus (a too-large JSON integer) — from_dict's
        # _finite_stats already drops such an advert at ingestion, so this only
        # catches a locally-constructed NodeInfo, ranking it neutrally not crashing.
        except (KeyError, TypeError, ValueError, OverflowError):
            return NEUTRAL_SURPLUS

    def newer_than(self, other: "NodeInfo") -> bool:
        """Freshness for gossip merges: a new incarnation always wins, then the
        per-incarnation update counter."""
        return (self.epoch, self.seq) > (other.epoch, other.seq)

    def bumped(self, **changes) -> "NodeInfo":
        return replace(self, seq=self.seq + 1, **changes)

    def duty_enabled(self, duty_id: str) -> bool:
        return bool(self.duties_enabled.get(duty_id, True))


# MARK: - Jobs


@dataclass(frozen=True)
class Job:
    id: str
    duty: str
    prompt: str
    requested_by: str  # node id
    requested_at: float
    # The origination-dedup key this job is an execution of, when it was routed
    # with one (docs/szpontnet/12). The EXECUTOR claims it for the spawned agent's
    # lifetime, so a re-observation of the same work is suppressed while it runs
    # and freed when it finishes. Empty = an undeduped dispatch (server/target/
    # manual "Run on mesh"). Additive: a pre-claims node just ignores it.
    work_key: str = ""

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "duty": self.duty,
            "prompt": self.prompt,
            "requestedBy": self.requested_by,
            "requestedAt": self.requested_at,
        }
        if self.work_key:
            d["workKey"] = self.work_key
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Job | None":
        try:
            return cls(
                id=str(d["id"]),
                duty=str(d["duty"]),
                prompt=str(d.get("prompt", "")),
                requested_by=str(d.get("requestedBy", "?")),
                requested_at=_finite(d.get("requestedAt", time.time())),
                work_key=str(d.get("workKey", "")),
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            return None


# MARK: - Work claims (origination leases)


@dataclass(frozen=True)
class ClaimRecord:
    """One node's self-signed lease on a unit of external work.

    A work-claim deduplicates **origination**: when several nodes independently
    observe the same external event (e.g. a review request on a PR), each derives
    the same ``work_key`` and claims it; a deterministic rule (the lowest node id
    among live, trusted, active claimants) elects a single owner and the losers
    yield, with no negotiation round. The claim is a **liveness-scoped lease** — it
    counts only while its claimant is a live node — so an owner that dies frees the
    work for a survivor without any timer of its own. See
    docs/szpontnet/12-work-claims.md.

    ``pubkey`` is carried inline so the record is **self-authenticating**: a
    receiver can verify ``sig`` (over the canonical bytes, [claim_signing_bytes])
    without having first seen the claimant's advertisement, and relay it verbatim.
    A keyless claim (no ``pubkey``) carries no ``sig`` and can never be
    authoritative — exactly the keyless-advert degradation.
    """

    work_key: str
    node: str  # claimant node id
    pubkey: str = ""
    epoch: float = 0.0  # claimant incarnation — aligns the lease with node liveness
    seq: int = 0  # per-(node, work_key) counter; the freshest same-node record wins
    state: str = "active"  # "active" | "released"
    sig: str = ""

    def to_dict(self) -> dict:
        d = {
            "workKey": self.work_key,
            "node": self.node,
            "epoch": self.epoch,
            "seq": self.seq,
            "state": self.state,
        }
        # Omit the crypto fields when empty so a keyless claim is compact and a
        # signed one round-trips byte-stable (the sig covers the sig-less form).
        if self.pubkey:
            d["pubkey"] = self.pubkey
        if self.sig:
            d["sig"] = self.sig
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "ClaimRecord | None":
        if not isinstance(d, dict):
            return None
        work_key, node = str(d.get("workKey", "")), str(d.get("node", ""))
        if not work_key or not node:
            return None  # a claim without a key or a claimant is meaningless
        try:
            return cls(
                work_key=work_key,
                node=node,
                pubkey=str(d.get("pubkey", "")),
                epoch=_finite(d.get("epoch", 0.0)),
                seq=int(d.get("seq", 0)),
                state=str(d.get("state", "active")),
                sig=str(d.get("sig", "")),
            )
        except (TypeError, ValueError, OverflowError):
            # `epoch` runs through _finite (mirroring NodeInfo.epoch): a non-finite
            # freshness key would out-fresh every honest claim forever AND re-serialize as
            # the RFC 8259-invalid `Infinity`/`NaN` token through the verbatim gossip
            # relay — a hazard a claim shares with an advert. OverflowError: a JSON
            # `seq`/`epoch` of 1e999 parses to inf, and int(inf) raises it (not a
            # ValueError) — drop the claim, keep the link alive.
            return None

    @property
    def active(self) -> bool:
        return self.state == "active"

    def newer_than(self, other: "ClaimRecord") -> bool:
        """Freshness for merging two records from the SAME claimant: a new
        incarnation always wins, then the per-key update counter (mirrors
        NodeInfo)."""
        return (self.epoch, self.seq) > (other.epoch, other.seq)


# MARK: - Envelope encode / decode


def encode(msg: dict) -> bytes:
    """One NDJSON line. The version rides on every message so a future rev can
    branch on it without a handshake change."""
    msg.setdefault("v", PROTOCOL_VERSION)
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes) -> dict | None:
    """Parse one line; None for garbage (oversized, non-JSON, non-object, or
    missing the type tag) — callers drop and move on. Non-finite numbers are
    tolerated at parse (``1e999``/``Infinity`` decode to ∞) and dropped one layer up
    by each payload's ``from_dict`` finite guard (see ``_finite``), so a poisoned
    field drops its record while a coercible one (``overrides.rev``) still normalizes
    — the reference's deliberate per-field layering."""
    if not line or len(line) > MAX_LINE_BYTES:
        return None
    try:
        msg = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
        return None
    return msg


# MARK: - Message builders (the only places field names appear on the send side)


def beacon(info: NodeInfo) -> dict:
    return {
        "t": "beacon",
        "id": info.id,
        "name": info.name,
        "platform": info.platform,
        "tcpPort": info.tcp_port,
        "epoch": info.epoch,
    }


def hello(info: NodeInfo, overrides_dict: dict, secret: str = "",
          nonce: str = "") -> dict:
    msg = {"t": "hello", "node": info.to_dict(), "overrides": overrides_dict}
    if secret:
        msg["secret"] = secret
    if nonce:
        # The trust challenge: whoever receives this hello must sign `nonce` with
        # the private key for the advertised `pubkey` to be believed (proof of
        # possession, bound to this connection so it can't be replayed elsewhere).
        msg["nonce"] = nonce
    return msg


def auth(sig_b64: str) -> dict:
    """Proof of possession: a signature over the peer's hello `nonce`."""
    return {"t": "auth", "sig": sig_b64}


def ctl_hello(secret: str = "", api_key: str = "") -> dict:
    msg: dict = {"t": "ctl"}
    if secret:
        msg["secret"] = secret
    if api_key:
        # Optional per-server credential: a node configured with an API key
        # requires it to open a control session. Independent of the join secret.
        msg["apiKey"] = api_key
    return msg


def heartbeat() -> dict:
    return {"t": "heartbeat", "ts": time.time()}


def node_update(info: NodeInfo) -> dict:
    return {"t": "node", "node": info.to_dict()}


def node_update_raw(node_dict: dict) -> dict:
    """Relay a peer's advertisement **verbatim** — the exact dict as received, so
    its signature (which covers the canonical bytes of that dict) survives the hop
    unchanged. Re-serializing via ``node_update(from_dict(...))`` would drop any
    unknown future field the originator signed over and break the signature."""
    return {"t": "node", "node": node_dict}


def overrides_update(overrides_dict: dict) -> dict:
    return {"t": "overrides", "overrides": overrides_dict}


def work_claim(claim_dict: dict) -> dict:
    """Gossip a work-claim. Always sent VERBATIM — the exact signed dict, whether
    minted here or relayed — so its signature (over that dict's canonical bytes)
    survives every hop unchanged, just like a relayed advertisement."""
    return {"t": "work-claim", "claim": claim_dict}


def set_attr(target_id: str, attrs: dict) -> dict:
    return {"t": "set-attr", "target": target_id, "attrs": attrs}


def dispatch(job: Job, api_key: str = "") -> dict:
    msg = {"t": "dispatch", "job": job.to_dict()}
    if api_key:
        # A dispatcher presents the target server's API key (if any) so an
        # API-key-gated server accepts the request. Omitted when unset.
        msg["apiKey"] = api_key
    return msg


def job_status(job_id: str, status: str, reason: str = "", node_id: str = "",
               direct: bool = False) -> dict:
    """``direct`` (additive, omitted when false) marks a ``spawned`` job the
    executor ran on the PERSONAL path — fire-and-forget, no ``job-result`` will
    follow — so an accountability-tracking originator knows not to arm a
    completion deadline for it. See docs/szpontnet/13-foreign-execution.md."""
    msg = {"t": "job-status", "id": job_id, "status": status,
           "reason": reason, "node": node_id}
    if direct:
        msg["direct"] = True
    return msg


def job_result(job_id: str, node_id: str, result: dict, sig: str = "") -> dict:
    """The computed artifact a **foreign** SzpontRequest returns to its originator.
    Carried back on the same link the dispatch arrived on, correlated by Job ``id``,
    re-sent until the originator ``job-ack``s it. ``sig`` (optional, additive) is the
    executor's signature over [result_signing_bytes]; a keyed executor signs, a
    keyless one omits it. See docs/szpontnet/13-foreign-execution.md."""
    msg = {"t": "job-result", "id": job_id, "node": node_id, "result": result}
    if sig:
        msg["sig"] = sig
    return msg


def job_ack(job_id: str, node_id: str) -> dict:
    """The originator's acknowledgement of a [job_result], by Job ``id``. Stops the
    executor's retry loop; reliable delivery, not fire-and-forget."""
    return {"t": "job-ack", "id": job_id, "node": node_id}


# Receiver-side cap on a job-progress `note` — a plea for an extension, not a
# payload channel (the artifact itself rides job-result, bounded by MAX_LINE_BYTES).
MAX_PROGRESS_NOTE_BYTES = 4096


def job_reminder(job_id: str, node_id: str) -> dict:
    """The originator's "is this ready?" for a foreign-accepted SzpontRequest that
    passed its completion deadline without a result. The executor must answer with
    the ``job-result`` (if computed) or a [job_progress] (still running); silence,
    or an answer that doesn't fulfill the task, gets it banned. See
    docs/szpontnet/13-foreign-execution.md."""
    return {"t": "job-reminder", "id": job_id, "node": node_id}


def job_progress(job_id: str, node_id: str, note: str) -> dict:
    """The executor's reply to a [job_reminder] when the work is still running: a
    human-readable status note, judged by the originator's extension decider (an
    agent's call). Unsigned like job-status — gated by the responder link alone,
    it only ever influences the originator's local extension decision."""
    return {"t": "job-progress", "id": job_id, "node": node_id, "note": note}


def status_request() -> dict:
    return {"t": "status"}
