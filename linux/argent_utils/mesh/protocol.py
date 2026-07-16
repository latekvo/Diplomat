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
- ``status``    (TCP)  ctl request: reply with one ``state`` message (the snapshot)
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace

PROTOCOL_VERSION = 1

# A guard against garbage/hostile blobs on the mesh port, not a real limit —
# a dispatch carries a whole review prompt (tens of KB).
MAX_LINE_BYTES = 512 * 1024


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
    # Fraction of the heuristic token budget still remaining (1.0 = fresh, 0.0 =
    # out), so the console shows a live "quota NN%" for every node, not just self.
    tokens_pct: float = 1.0
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
    # Load-balancing accounting, additive: {"plan", "usageAvg", "quotaLeft"}.
    # Empty when a node advertises no stats — its dispatch surplus is then 0
    # (neutral), so surplus-first ranking degrades to weakest-first. See stats.py.
    stats: dict = field(default_factory=dict)
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
        if self.pubkey:
            d["pubkey"] = self.pubkey
        if self.stats:
            d["stats"] = self.stats
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
                tokens_pct=float(d.get("tokensPct", 1.0)),
                tcp_port=int(d.get("tcpPort", 0)),
                epoch=float(d.get("epoch", 0.0)),
                seq=int(d.get("seq", 0)),
                sees=tuple(str(s) for s in d.get("sees", [])),
                duties_enabled=dict(d.get("dutiesEnabled", {})),
                pubkey=str(d.get("pubkey", "")),
                stats=dict(d.get("stats", {})) if isinstance(d.get("stats"), dict) else {},
                version=int(d.get("v", PROTOCOL_VERSION)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def surplus(self) -> float:
        """Spare quota this node advertises for load balancing:
        ``quotaLeft − usageAvg`` in plan-relative capacity units. 0.0 when the
        node advertises no stats (neutral — ranks like today's weakest-first)."""
        if not self.stats:
            return 0.0
        try:
            return float(self.stats.get("quotaLeft", 0.0)) - float(
                self.stats.get("usageAvg", 0.0))
        except (TypeError, ValueError):
            return 0.0

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

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "duty": self.duty,
            "prompt": self.prompt,
            "requestedBy": self.requested_by,
            "requestedAt": self.requested_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Job | None":
        try:
            return cls(
                id=str(d["id"]),
                duty=str(d["duty"]),
                prompt=str(d.get("prompt", "")),
                requested_by=str(d.get("requestedBy", "?")),
                requested_at=float(d.get("requestedAt", time.time())),
            )
        except (KeyError, TypeError, ValueError):
            return None


# MARK: - Envelope encode / decode


def encode(msg: dict) -> bytes:
    """One NDJSON line. The version rides on every message so a future rev can
    branch on it without a handshake change."""
    msg.setdefault("v", PROTOCOL_VERSION)
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes) -> dict | None:
    """Parse one line; None for garbage (oversized, non-JSON, non-object, or
    missing the type tag) — callers drop and move on."""
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


def overrides_update(overrides_dict: dict) -> dict:
    return {"t": "overrides", "overrides": overrides_dict}


def set_attr(target_id: str, attrs: dict) -> dict:
    return {"t": "set-attr", "target": target_id, "attrs": attrs}


def dispatch(job: Job, api_key: str = "") -> dict:
    msg = {"t": "dispatch", "job": job.to_dict()}
    if api_key:
        # A dispatcher presents the target server's API key (if any) so an
        # API-key-gated server accepts the request. Omitted when unset.
        msg["apiKey"] = api_key
    return msg


def job_status(job_id: str, status: str, reason: str = "", node_id: str = "") -> dict:
    return {"t": "job-status", "id": job_id, "status": status,
            "reason": reason, "node": node_id}


def status_request() -> dict:
    return {"t": "status"}
