"""Wire protocol: NDJSON messages over TCP links, JSON beacons over UDP.

Pure encode/decode — no sockets. Everything is tolerant of unknown fields and
newer minor revisions (a peer running a newer build must not wedge an older
one); a message that doesn't parse is dropped, never fatal.

Message types (``t``):

- ``beacon``    (UDP)  presence advert: id, name, platform, tcpPort, epoch
- ``hello``     (TCP)  first message on a peer link, both directions: NodeInfo + overrides
- ``ctl``       (TCP)  first message on a *control* connection (the panel / CLI
                       talking to its local node) — not a peer
- ``heartbeat`` (TCP)  link liveness
- ``node``      (TCP)  gossiped NodeInfo update (attrs changed, peers-seen changed)
- ``overrides`` (TCP)  gossiped LWW placement overrides
- ``set-attr``  (TCP)  edit a node's local attrs (from a peer's panel or the CLI)
- ``dispatch``  (TCP)  run a job on the receiving node
- ``job-status``(TCP)  dispatch outcome: ``spawned`` | ``failed`` (+ reason)
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
    tokens: str  # "ok" | "low" | "out"
    tcp_port: int = 0
    epoch: float = 0.0  # process start time — a restart bumps it (new incarnation)
    seq: int = 0  # per-node update counter; receivers keep the highest
    sees: tuple[str, ...] = ()  # peer ids this node currently holds links to
    duties_enabled: dict = field(default_factory=dict)
    version: int = PROTOCOL_VERSION

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "platform": self.platform,
            "tier": self.tier,
            "tokens": self.tokens,
            "tcpPort": self.tcp_port,
            "epoch": self.epoch,
            "seq": self.seq,
            "sees": list(self.sees),
            "dutiesEnabled": self.duties_enabled,
            "v": self.version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "NodeInfo | None":
        try:
            return cls(
                id=str(d["id"]),
                name=str(d.get("name", "?")),
                platform=str(d.get("platform", "unknown")),
                tier=int(d.get("tier", 3)),
                tokens=str(d.get("tokens", "ok")),
                tcp_port=int(d.get("tcpPort", 0)),
                epoch=float(d.get("epoch", 0.0)),
                seq=int(d.get("seq", 0)),
                sees=tuple(str(s) for s in d.get("sees", [])),
                duties_enabled=dict(d.get("dutiesEnabled", {})),
                version=int(d.get("v", PROTOCOL_VERSION)),
            )
        except (KeyError, TypeError, ValueError):
            return None

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


def hello(info: NodeInfo, overrides_dict: dict, secret: str = "") -> dict:
    msg = {"t": "hello", "node": info.to_dict(), "overrides": overrides_dict}
    if secret:
        msg["secret"] = secret
    return msg


def ctl_hello(secret: str = "") -> dict:
    msg: dict = {"t": "ctl"}
    if secret:
        msg["secret"] = secret
    return msg


def heartbeat() -> dict:
    return {"t": "heartbeat", "ts": time.time()}


def node_update(info: NodeInfo) -> dict:
    return {"t": "node", "node": info.to_dict()}


def overrides_update(overrides_dict: dict) -> dict:
    return {"t": "overrides", "overrides": overrides_dict}


def set_attr(target_id: str, attrs: dict) -> dict:
    return {"t": "set-attr", "target": target_id, "attrs": attrs}


def dispatch(job: Job) -> dict:
    return {"t": "dispatch", "job": job.to_dict()}


def job_status(job_id: str, status: str, reason: str = "", node_id: str = "") -> dict:
    return {"t": "job-status", "id": job_id, "status": status,
            "reason": reason, "node": node_id}


def status_request() -> dict:
    return {"t": "status"}
