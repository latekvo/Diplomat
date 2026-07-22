"""The SzpontNet wire codec — a clean-room, spec-faithful NDJSON encoder/decoder.

This is the tester's own reference implementation of chapter 04's message
catalog and chapter 03's framing, extended with the chapter-11 additions: the
optional ``pubkey``/``stats`` NodeInfo fields (omitted when empty), the trust
``nonce``/``auth`` proof-of-possession exchange over a domain-separated
challenge, the ``apiKey`` credential on ``ctl``/``dispatch``, the
``trust``/``untrust`` control commands, and **authenticated gossip**: a self-
signed advert ``sig`` and a self-signed override ``sig``, each covering the
domain-separated canonical bytes (:func:`advert_signing_bytes` /
:func:`overrides_signing_bytes`). It is written from the specification so
the tester can (a) *speak* the protocol correctly as a peer / control client /
adversary and (b) *validate* every message a candidate emits against a strict
schema. Where this codec and a candidate disagree, one of them violates the
spec — which is exactly what the tester is for.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, replace

from .model import MAX_LINE_BYTES, NEUTRAL_SURPLUS, PROTOCOL_VERSION


# MARK: - Authenticated-gossip signing bytes (11 — self-signed adverts + overrides)
#
# The two gossiped, self-signed payloads (a NodeInfo advertisement, and a
# placement-overrides edit) are each authenticated by a base64 Ed25519 signature
# over ``<domain tag> ‖ <canonical JSON of the payload without its own `sig`>``.
# A signature is therefore meaningless outside its exact context and cannot be
# lifted from one payload type to the other. The canonical form (sorted keys +
# compact separators, taken over the RAW dict with `sig` removed) MUST be
# byte-identical to the reference (``protocol._canonical`` /
# ``advert_signing_bytes`` / ``overrides_signing_bytes``) so both sides sign and
# verify the exact same bytes; where they disagree, one violates the spec.
ADVERT_CONTEXT = b"szpontnet-nodeinfo-v1:"
OVERRIDES_CONTEXT = b"szpontnet-overrides-v1:"
# A gossiped work-claim (an origination lease on a unit of work, ch 12) is signed
# the same way under its OWN domain tag, so a claim signature can never be lifted
# onto an advert/override or vice versa. Must match the reference's
# ``protocol._CLAIM_CONTEXT`` byte-for-byte. See docs/szpontnet/12-work-claims.md.
CLAIM_CONTEXT = b"szpontnet-workclaim-v1:"
# A `job-result` — the computed artifact a FOREIGN, confined SzpontRequest returns
# to its originator (who then performs any social action itself, ch 13) — is signed
# under its OWN domain tag over the canonical ``{id,node,result}``, so a result
# signature can never be lifted onto an advert/override/claim or vice versa. Must
# match the reference's ``protocol._RESULT_CONTEXT`` byte-for-byte. See
# docs/szpontnet/13-foreign-execution.md.
RESULT_CONTEXT = b"szpontnet-jobresult-v1:"


def _canonical(payload: dict) -> bytes:
    """Deterministic JSON of a payload with its ``sig`` field removed — the exact
    bytes a signature is computed/verified over. Sorted keys + compact separators
    so it is identical across implementations, and taken over the raw dict so any
    unknown extra field the signer covered is covered here too."""
    body = {k: v for k, v in payload.items() if k != "sig"}
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def advert_signing_bytes(node_dict: dict) -> bytes:
    """The exact bytes a NodeInfo advertisement's ``sig`` covers (11)."""
    return ADVERT_CONTEXT + _canonical(node_dict)


def overrides_signing_bytes(overrides_dict: dict) -> bytes:
    """The exact bytes a placement-overrides ``sig`` covers, signed by the
    ``updatedBy`` editor (11)."""
    return OVERRIDES_CONTEXT + _canonical(overrides_dict)


def claim_signing_bytes(claim_dict: dict) -> bytes:
    """The exact bytes a work-claim's ``sig`` covers (12): the claim's own domain
    tag followed by the canonical JSON of the claim dict with its ``sig`` removed.
    Signed by the claimant ``node`` and verified against the inline ``pubkey``.
    Byte-identical to the reference's ``protocol.claim_signing_bytes``."""
    return CLAIM_CONTEXT + _canonical(claim_dict)


def result_signing_bytes(result_payload: dict) -> bytes:
    """The exact bytes a ``job-result``'s ``sig`` covers (13): the result's own
    domain tag followed by the canonical JSON of ``{"id","node","result"}`` with any
    ``sig`` removed. Signed by the executor ``node`` and verified against the
    executor's pinned key so a relay or a third peer on the link can neither forge
    nor tamper with the returned artifact. Byte-identical to the reference's
    ``protocol.result_signing_bytes``."""
    return RESULT_CONTEXT + _canonical(result_payload)


# MARK: - NodeInfo (04-messages.md#nodeinfo)


@dataclass(frozen=True)
class NodeInfo:
    id: str
    name: str = "?"
    platform: str = "unknown"
    tier: int = 3
    tokens: str = "ok"
    tcp_port: int = 0
    epoch: float = 0.0
    seq: int = 0
    sees: tuple[str, ...] = ()
    duties_enabled: dict = field(default_factory=dict)
    # Chapter 11 additive fields. ``pubkey`` is the node's advertised Ed25519
    # public key (base64) — its *claimed* trust identity, believed only once the
    # node proves possession with an ``auth`` signature. ``stats`` is the
    # load-balancing view ({"plan","usageAvg","quotaLeft","surplus"}, where
    # surplus is the burn-down ratio ranked on and the others are display-only).
    # BOTH are omitted from to_dict() when empty so a node that uses neither is
    # byte-identical to a core-v1 advertisement (11-trust-and-balancing MUST).
    pubkey: str = ""
    stats: dict = field(default_factory=dict)
    # Base64 Ed25519 signature by THIS node's device key over the advert's
    # canonical form (:func:`advert_signing_bytes`). It authenticates the
    # advertisement end to end: any relay may forward it, but none can forge or
    # tamper with it without the private key (the receiver verifies ``sig`` against
    # the advert's own ``pubkey``). Empty for a keyless node — then unauthenticated
    # and foreign under any allowlist. Omitted from to_dict() when empty so an
    # unsigned advert stays byte-identical to a core-v1 one (11 MUST).
    sig: str = ""
    version: int = PROTOCOL_VERSION

    def to_dict(self) -> dict:
        d = {
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
        # Omit the additive ch-11 fields when empty (byte-compat with core v1).
        if self.pubkey:
            d["pubkey"] = self.pubkey
        if self.stats:
            d["stats"] = self.stats
        if self.sig:
            d["sig"] = self.sig
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "NodeInfo | None":
        """Parse a NodeInfo the way a conformant receiver must: a missing ``id``
        or an unparseable numeric field invalidates the whole object (04). The
        additive ``pubkey``/``stats`` (11) are optional and tolerant: a malformed
        ``stats`` degrades to empty rather than invalidating the whole object."""
        if not isinstance(d, dict) or "id" not in d:
            return None
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
                pubkey=str(d.get("pubkey", "")),
                stats=dict(d.get("stats", {})) if isinstance(d.get("stats"), dict) else {},
                sig=str(d.get("sig", "")),
                version=int(d.get("v", PROTOCOL_VERSION)),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def surplus(self) -> float:
        """The burn-down ratio this node advertises for load balancing: budget
        left ÷ clock left until its quota resets (``stats.surplus``). 1.0 is on
        pace, above is flush, below is rationing. NEUTRAL_SURPLUS (1.0, on the
        line) when the node advertises no stats, or a legacy advert carrying only
        the absolute quotaLeft/usageAvg pair — those are a different scale and are
        NOT converted. Mirrors the reference NodeInfo.surplus() (11)."""
        if not self.stats:
            return NEUTRAL_SURPLUS
        try:
            return float(self.stats["surplus"])
        except (KeyError, TypeError, ValueError):
            return NEUTRAL_SURPLUS

    def newer_than(self, other: "NodeInfo") -> bool:
        return (self.epoch, self.seq) > (other.epoch, other.seq)

    def duty_enabled(self, duty_id: str) -> bool:
        return bool(self.duties_enabled.get(duty_id, True))

    def bumped(self, **changes) -> "NodeInfo":
        return replace(self, seq=self.seq + 1, **changes)


# MARK: - Job (04-messages.md#job)


@dataclass(frozen=True)
class Job:
    id: str
    duty: str
    prompt: str = ""
    requested_by: str = "?"
    requested_at: float = 0.0

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
        if not isinstance(d, dict) or "id" not in d or "duty" not in d:
            return None
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


# MARK: - ClaimRecord (04-messages.md#work-claim, 12-work-claims.md)


@dataclass(frozen=True)
class ClaimRecord:
    """One node's self-signed origination lease on a unit of external work (12).

    A work-claim deduplicates *origination*: nodes that independently observe the
    same external event derive the same ``work_key`` and each claim it; a
    deterministic rule (the lowest node id among live, trusted, active claimants)
    elects a single owner and the losers yield. It is a **liveness-scoped lease** —
    it counts only while its claimant is a live node.

    ``pubkey``/``sig`` are carried inline so the record is self-authenticating and
    are **omitted when empty** (byte-identical to the reference ``ClaimRecord`` and
    to a keyless claim's compact form; a signed one round-trips byte-stable because
    the sig covers the sig-less canonical form)."""

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
        """Parse a claim the way a conformant receiver must: a claim without a
        non-empty ``workKey`` or ``node`` is meaningless and MUST be dropped."""
        if not isinstance(d, dict):
            return None
        work_key, node = str(d.get("workKey", "")), str(d.get("node", ""))
        if not work_key or not node:
            return None
        try:
            return cls(
                work_key=work_key,
                node=node,
                pubkey=str(d.get("pubkey", "")),
                epoch=float(d.get("epoch", 0.0)),
                seq=int(d.get("seq", 0)),
                state=str(d.get("state", "active")),
                sig=str(d.get("sig", "")),
            )
        except (TypeError, ValueError):
            return None

    @property
    def active(self) -> bool:
        # An unknown state MUST be treated as NOT active (12): a future state
        # never counts as ownership.
        return self.state == "active"

    def newer_than(self, other: "ClaimRecord") -> bool:
        """Freshness for merging two records from the SAME claimant: a new
        incarnation always wins, then the per-key update counter (mirrors
        NodeInfo)."""
        return (self.epoch, self.seq) > (other.epoch, other.seq)


# MARK: - Envelope encode / decode (03-transport.md#framing)


def encode(msg: dict) -> bytes:
    """One compact NDJSON line, ``\\n``-terminated, with ``v`` defaulted."""
    out = dict(msg)
    out.setdefault("v", PROTOCOL_VERSION)
    return (json.dumps(out, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: bytes) -> dict | None:
    """Parse one line; ``None`` (drop) for: empty, over-length, invalid UTF-8,
    non-JSON, a non-object, or an object without a string ``t``. This is the
    exact drop-set the conformance vector V2 enumerates."""
    if not line or len(line) > MAX_LINE_BYTES:
        return None
    try:
        msg = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
        return None
    return msg


# MARK: - Message builders


def beacon(info: NodeInfo) -> dict:
    return {
        "t": "beacon",
        "id": info.id,
        "name": info.name,
        "platform": info.platform,
        "tcpPort": info.tcp_port,
        "epoch": info.epoch,
    }


# Domain-separation prefix for the trust proof-of-possession signature (11). The
# peer signs this tag + the challenge nonce, NEVER the bare nonce — so a captured
# signature is meaningless outside SzpontNet's auth exchange. Must match the
# reference's ``_AUTH_CONTEXT`` byte-for-byte.
AUTH_CONTEXT = b"szpontnet-auth-v1:"


def auth_challenge(nonce: str) -> bytes:
    """The exact bytes signed/verified for a proof-of-possession ``auth``: the
    domain tag followed by the UTF-8 challenge nonce (11-trust-and-balancing)."""
    return AUTH_CONTEXT + nonce.encode()


def hello(info: NodeInfo, overrides: dict, secret: str = "", nonce: str = "") -> dict:
    msg = {"t": "hello", "node": info.to_dict(), "overrides": overrides}
    if secret:
        msg["secret"] = secret
    if nonce:
        # The trust challenge (11): whoever receives this hello must sign the
        # domain-separated ``nonce`` with the private key for the advertised
        # ``pubkey`` to be believed (proof of possession, bound to this link).
        msg["nonce"] = nonce
    return msg


def auth(sig_b64: str) -> dict:
    """Proof of possession (11): a base64 signature over the peer's hello nonce,
    domain-separated (:func:`auth_challenge`)."""
    return {"t": "auth", "sig": sig_b64}


def ctl_hello(secret: str = "", api_key: str = "") -> dict:
    msg: dict = {"t": "ctl"}
    if secret:
        msg["secret"] = secret
    if api_key:
        # Optional per-server credential (11): a node configured with an API key
        # requires it to open a control session. Independent of the join secret.
        msg["apiKey"] = api_key
    return msg


def heartbeat() -> dict:
    return {"t": "heartbeat", "ts": time.time()}


def node_update(info: NodeInfo) -> dict:
    return {"t": "node", "node": info.to_dict()}


def overrides_update(overrides: dict) -> dict:
    return {"t": "overrides", "overrides": overrides}


def work_claim(claim_dict: dict) -> dict:
    """Gossip a work-claim (12). Always carries the VERBATIM signed claim dict —
    whether minted here or relayed — so its signature (over that dict's canonical
    bytes) survives every hop unchanged, exactly like a relayed advertisement."""
    return {"t": "work-claim", "claim": claim_dict}


def set_attr(target_id: str, attrs: dict) -> dict:
    return {"t": "set-attr", "target": target_id, "attrs": attrs}


def dispatch_job(job: Job, api_key: str = "") -> dict:
    msg = {"t": "dispatch", "job": job.to_dict()}
    if api_key:
        # A dispatcher presents the target server's API key (if any) so an
        # API-key-gated server accepts the request. Omitted when unset (11).
        msg["apiKey"] = api_key
    return msg


def dispatch_route(duty: str, prompt: str, target: str = "", api_key: str = "") -> dict:
    msg: dict = {"t": "dispatch", "duty": duty, "prompt": prompt}
    if target:
        # Name one node directly — the dispatcher's unilateral pick, no failover.
        msg["target"] = target
    if api_key:
        # The credential forwarded to an API-key-gated (server) target (11).
        msg["apiKey"] = api_key
    return msg


def trust(fingerprint: str, label: str = "") -> dict:
    """Control command (11): add a device fingerprint to the local trusted
    allowlist so its verified requests classify as *personal*."""
    return {"t": "trust", "fingerprint": fingerprint, "label": label}


def untrust(fingerprint: str) -> dict:
    """Control command (11): remove a device fingerprint from the allowlist."""
    return {"t": "untrust", "fingerprint": fingerprint}


def job_status(job_id: str, status: str, reason: str = "", node_id: str = "",
               direct: bool = False) -> dict:
    """``direct`` (additive, v0.4.0; OMITTED when false so a plain status stays
    byte-identical to a pre-v0.4.0 one) marks a ``spawned`` job the executor ran
    on the PERSONAL path — fire-and-forget, no ``job-result`` will follow — so an
    accountability-tracking originator MUST NOT arm a completion deadline for it
    (13-foreign-execution#the-completion-deadline)."""
    msg = {"t": "job-status", "id": job_id, "status": status, "reason": reason, "node": node_id}
    if direct:
        msg["direct"] = True
    return msg


def job_result(job_id: str, node_id: str, result: dict, sig: str = "") -> dict:
    """The computed artifact a FOREIGN, confined SzpontRequest returns to its
    originator (13): correlated by Job ``id``, carrying the executor's ``node`` id
    and the ``result`` payload ({ok, duty, output, error}). A KEYED executor MUST
    sign it over :func:`result_signing_bytes`; ``sig`` is OMITTED when empty (a
    keyless executor carries none, accepted on the responder-link gate alone), so a
    keyless result stays byte-identical to a bare one."""
    msg = {"t": "job-result", "id": job_id, "node": node_id, "result": result}
    if sig:
        msg["sig"] = sig
    return msg


def job_ack(job_id: str, node_id: str) -> dict:
    """The originator's acknowledgement of a ``job-result`` (13), by Job ``id``,
    carrying the acknowledging (originator) ``node`` id. Stops the executor's
    reliable-delivery retries."""
    return {"t": "job-ack", "id": job_id, "node": node_id}


# Receiver-side cap on a `job-progress` note (appendix B, v0.4.0): the note is a
# plea for an extension, not a payload channel — a receiver truncates past this.
MAX_PROGRESS_NOTE_BYTES = 4096


def job_reminder(job_id: str, node_id: str) -> dict:
    """The originator's **"is this ready?"** (13 v0.4.0 accountability): sent when
    a FOREIGN executor's accepted SzpontRequest passes its completion deadline
    without a ``job-result``. Correlated by Job ``id``, carrying the asking
    (originator) ``node`` id, sent on the executor's link."""
    return {"t": "job-reminder", "id": job_id, "node": node_id}


def job_progress(job_id: str, node_id: str, note: str) -> dict:
    """The executor's reply to a ``job-reminder`` when the work is still running
    (13 v0.4.0): a human-readable ``note`` — its case for a deadline extension,
    judged by the originator's configured decider, never taken at face value.
    Unsigned like ``job-status``: gated by the responder link alone, it only ever
    influences the originator's local extension decision."""
    return {"t": "job-progress", "id": job_id, "node": node_id, "note": note}


def status_request() -> dict:
    return {"t": "status"}


def set_overrides(duty: str, placement: dict) -> dict:
    return {"t": "set-overrides", "duty": duty, "placement": placement}


def stop() -> dict:
    return {"t": "stop"}


# MARK: - Strict validators (checking messages a candidate EMITS)
#
# These enforce the field/type contract from chapter 04. Each returns a list of
# human-readable problems ([] == valid). Used by the codec-conformance suite to
# prove a candidate's emitted beacon / hello / node / heartbeat are on-spec.


def _is_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _is_num(x) -> bool:
    return _is_int(x) or isinstance(x, float)


def validate_envelope(msg: dict) -> list[str]:
    problems = []
    if not isinstance(msg.get("t"), str):
        problems.append("missing/non-string 't'")
    if "v" in msg and not _is_int(msg["v"]):
        problems.append("'v' present but not an int")
    return problems


def validate_nodeinfo(d: dict) -> list[str]:
    problems = []
    if not isinstance(d, dict):
        return ["NodeInfo is not an object"]
    if not isinstance(d.get("id"), str) or not d.get("id"):
        problems.append("NodeInfo.id missing or not a non-empty string")
    if "platform" in d and not isinstance(d["platform"], str):
        problems.append("NodeInfo.platform not a string")
    if "tier" in d and not _is_int(d["tier"]):
        problems.append("NodeInfo.tier not an int")
    if "tokens" in d and not isinstance(d["tokens"], str):
        problems.append("NodeInfo.tokens not a string")
    if "tcpPort" in d and not _is_int(d["tcpPort"]):
        problems.append("NodeInfo.tcpPort not an int")
    if "epoch" in d and not _is_num(d["epoch"]):
        problems.append("NodeInfo.epoch not a number")
    if "seq" in d and not _is_int(d["seq"]):
        problems.append("NodeInfo.seq not an int")
    if "sees" in d and not isinstance(d["sees"], list):
        problems.append("NodeInfo.sees not an array")
    if "dutiesEnabled" in d and not isinstance(d["dutiesEnabled"], dict):
        problems.append("NodeInfo.dutiesEnabled not an object")
    # Chapter-11 additive fields: present-and-non-empty means they must be
    # well-shaped, but they are always optional (never required, never fatal to
    # a receiver — a candidate that emits neither is a valid core-v1 node).
    if "pubkey" in d and not isinstance(d["pubkey"], str):
        problems.append("NodeInfo.pubkey not a string")
    if "sig" in d and not isinstance(d["sig"], str):
        problems.append("NodeInfo.sig not a string")
    if "stats" in d:
        st = d["stats"]
        if not isinstance(st, dict):
            problems.append("NodeInfo.stats not an object")
        else:
            if "plan" in st and not isinstance(st["plan"], str):
                problems.append("NodeInfo.stats.plan not a string")
            # usageAvg/quotaLeft are retained for display; surplus (11) is the
            # burn-down ratio ranked on. All optional, all numeric when present.
            for k in ("usageAvg", "quotaLeft", "surplus"):
                if k in st and not _is_num(st[k]):
                    problems.append(f"NodeInfo.stats.{k} not a number")
    return problems


def validate_auth(msg: dict) -> list[str]:
    problems = validate_envelope(msg)
    if msg.get("t") != "auth":
        problems.append(f"expected t=auth, got {msg.get('t')!r}")
    if not isinstance(msg.get("sig"), str) or not msg.get("sig"):
        problems.append("auth.sig missing or not a non-empty string")
    return problems


def validate_beacon(msg: dict) -> list[str]:
    problems = validate_envelope(msg)
    if msg.get("t") != "beacon":
        problems.append(f"expected t=beacon, got {msg.get('t')!r}")
    if not isinstance(msg.get("id"), str) or not msg.get("id"):
        problems.append("beacon.id missing or not a non-empty string")
    port = msg.get("tcpPort")
    if not _is_int(port) or port <= 0:
        problems.append("beacon.tcpPort missing or not a positive int (02-discovery MUST)")
    if "epoch" in msg and not _is_num(msg["epoch"]):
        problems.append("beacon.epoch not a number")
    if "platform" in msg and not isinstance(msg["platform"], str):
        problems.append("beacon.platform not a string")
    return problems


def validate_hello(msg: dict) -> list[str]:
    problems = validate_envelope(msg)
    if msg.get("t") != "hello":
        problems.append(f"expected t=hello, got {msg.get('t')!r}")
    if "node" not in msg:
        problems.append("hello.node (NodeInfo) is required")
    else:
        problems += [f"hello.{p}" for p in validate_nodeinfo(msg["node"])]
    if "overrides" in msg and not isinstance(msg["overrides"], dict):
        problems.append("hello.overrides not an object")
    return problems


def validate_heartbeat(msg: dict) -> list[str]:
    problems = validate_envelope(msg)
    if msg.get("t") != "heartbeat":
        problems.append(f"expected t=heartbeat, got {msg.get('t')!r}")
    return problems


def validate_work_claim(msg: dict) -> list[str]:
    """Strict schema for a ``work-claim`` message a candidate EMITS (04/12): a
    ``claim`` object whose required ``workKey``/``node`` are non-empty strings and
    whose optional fields carry their spec types. ``pubkey``/``sig`` are optional
    (a keyless claim omits them) but must be strings when present; a **keyed** claim
    (non-empty ``pubkey``) MUST also carry a non-empty ``sig``."""
    problems = validate_envelope(msg)
    if msg.get("t") != "work-claim":
        problems.append(f"expected t=work-claim, got {msg.get('t')!r}")
    claim = msg.get("claim")
    if not isinstance(claim, dict):
        problems.append("work-claim.claim missing or not an object")
        return problems
    if not isinstance(claim.get("workKey"), str) or not claim.get("workKey"):
        problems.append("claim.workKey missing or not a non-empty string")
    if not isinstance(claim.get("node"), str) or not claim.get("node"):
        problems.append("claim.node missing or not a non-empty string")
    if "epoch" in claim and not _is_num(claim["epoch"]):
        problems.append("claim.epoch not a number")
    if "seq" in claim and not _is_int(claim["seq"]):
        problems.append("claim.seq not an int")
    if "state" in claim and not isinstance(claim["state"], str):
        problems.append("claim.state not a string")
    if "pubkey" in claim and not isinstance(claim["pubkey"], str):
        problems.append("claim.pubkey not a string")
    if "sig" in claim and not isinstance(claim["sig"], str):
        problems.append("claim.sig not a string")
    # A keyed claim (carries a pubkey) MUST be signed, or a receiver drops it (12).
    if claim.get("pubkey") and not claim.get("sig"):
        problems.append("keyed claim (has pubkey) missing sig")
    return problems


def validate_job_reminder(msg: dict) -> list[str]:
    """Strict schema for a ``job-reminder`` an originator EMITS (04/13 v0.4.0):
    the Job ``id`` being asked about and the asking (originator) ``node`` id,
    both non-empty strings."""
    problems = validate_envelope(msg)
    if msg.get("t") != "job-reminder":
        problems.append(f"expected t=job-reminder, got {msg.get('t')!r}")
    if not isinstance(msg.get("id"), str) or not msg.get("id"):
        problems.append("job-reminder.id missing or not a non-empty string")
    if not isinstance(msg.get("node"), str) or not msg.get("node"):
        problems.append("job-reminder.node missing or not a non-empty string")
    return problems


def validate_job_progress(msg: dict) -> list[str]:
    """Strict schema for a ``job-progress`` an executor EMITS (04/13 v0.4.0): the
    Job ``id`` it reports on, the reporting (executor) ``node`` id, and a
    non-empty human-readable ``note`` — the executor's case for an extension (an
    empty plea pleads nothing). The 4 KiB note cap is the RECEIVER's truncation,
    not an emitter requirement."""
    problems = validate_envelope(msg)
    if msg.get("t") != "job-progress":
        problems.append(f"expected t=job-progress, got {msg.get('t')!r}")
    if not isinstance(msg.get("id"), str) or not msg.get("id"):
        problems.append("job-progress.id missing or not a non-empty string")
    if not isinstance(msg.get("node"), str) or not msg.get("node"):
        problems.append("job-progress.node missing or not a non-empty string")
    if not isinstance(msg.get("note"), str) or not msg.get("note"):
        problems.append("job-progress.note missing or not a non-empty string")
    return problems


def is_single_line_json(raw: bytes) -> list[str]:
    """A wire frame MUST be one compact UTF-8 JSON line: no interior newline,
    exactly one trailing ``\\n``, decodable as UTF-8 (03-transport)."""
    problems = []
    if not raw.endswith(b"\n"):
        problems.append("frame not newline-terminated")
    body = raw[:-1] if raw.endswith(b"\n") else raw
    if b"\n" in body:
        problems.append("frame has an interior newline")
    try:
        body.decode("utf-8")
    except UnicodeDecodeError:
        problems.append("frame is not valid UTF-8")
    return problems
