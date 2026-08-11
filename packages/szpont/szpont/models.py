"""The snapshot and its replies, as objects instead of dictionaries.

A node publishes everything it knows as JSON - ``state.json`` on disk and the
identically-shaped ``status`` reply on a control session (08-state). That is the
right wire format and an awkward Python API: every field is reached by string,
every optional one has to be guarded at the call site, and the spelling is
``lastSeenSecsAgo`` rather than anything a Python caller would write.

These types are a reading of that JSON, not a second definition of it. Three
rules hold everywhere:

* **Parsing never raises.** A snapshot can be stale, truncated mid-write, from a
  newer protocol version, or hostile - the library itself treats a corrupt one as
  "no node" rather than crashing its readers, and so does this. A field that is
  missing or the wrong type falls back to the neutral value the node itself uses
  for it, so a bad snapshot renders as an empty mesh instead of a traceback.
* **Nothing is dropped.** Every object keeps the dict it was read from as
  ``raw``, because the protocol grows by adding fields (``onion``, ``stats``,
  ``sig`` all arrived that way) and a wrapper that only exposes what it knew
  about at build time would hide the next one.
* **Names are the Python ones.** ``last_seen_secs``, not ``lastSeenSecsAgo``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from szpontnet.protocol import NEUTRAL_SURPLUS

# NEUTRAL_SURPLUS is re-exported, not restated: it is what a node ranks a peer at
# when that peer advertises no accounting at all - "on the pace line", which is
# what makes surplus-first degrade to weakest-first rather than to last place
# (11-trust-and-balancing). A copy here would be a second definition of a protocol
# constant, free to drift from the one the mesh actually uses.


def _str(d: dict, key: str, default: str = "") -> str:
    value = d.get(key)
    return value if isinstance(value, str) else default


def _int(d: dict, key: str, default: int = 0) -> int:
    value = d.get(key)
    # bool is an int subclass and never a meaningful tier or port; excluded so a
    # `true` in a hand-edited snapshot reads as the default rather than as 1.
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _float(d: dict, key: str, default: float) -> float:
    value = d.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


def _opt_float(d: dict, key: str) -> float | None:
    value = d.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _dict(d: dict, key: str) -> dict:
    value = d.get(key)
    return value if isinstance(value, dict) else {}


def _objects(d: dict, key: str) -> list[dict]:
    """The list at ``key``, keeping only its dict entries - a snapshot whose
    ``peers`` holds a string among the objects loses that entry, not the mesh."""
    value = d.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


@dataclass(frozen=True, kw_only=True)
class Quota:
    """A node's advertised accounting, the input to surplus-first dispatch.

    ``surplus`` is the ranked value and a *ratio*: budget left divided by clock
    left until that budget resets, so 1.0 is on pace and higher is flush. The
    rest is display. A node that advertises nothing here is
    :attr:`NEUTRAL_SURPLUS`, which is why :attr:`advertised` exists - it tells a
    neutral default apart from a node that really is exactly on pace.
    """

    plan: str = ""
    surplus: float = NEUTRAL_SURPLUS
    quota_left: float | None = None
    usage_avg: float | None = None
    advertised: bool = False

    @classmethod
    def from_dict(cls, stats: dict | None) -> "Quota":
        if not stats:
            return cls()
        return cls(
            plan=_str(stats, "plan"),
            surplus=_float(stats, "surplus", NEUTRAL_SURPLUS),
            quota_left=_opt_float(stats, "quotaLeft"),
            usage_avg=_opt_float(stats, "usageAvg"),
            advertised=True,
        )


@dataclass(frozen=True, kw_only=True)
class Node:
    """One machine in the mesh, as it advertises itself.

    The fields every node publishes about itself. What a *reader* additionally
    knows about a peer - the link, the address, whether the key was proved -
    lives on :class:`Peer`, because none of it is the peer's to claim.
    """

    id: str = ""
    name: str = ""
    platform: str = ""
    tier: int = 3
    tokens: str = "ok"
    tokens_pct: float = 1.0
    tokens_session_pct: float | None = None
    tokens_week_pct: float | None = None
    tokens_auto: bool = True
    strength_auto: bool = True
    fingerprint: str = ""
    pubkey: str = ""
    endpoint: str = ""
    onion: str = ""
    duties_enabled: dict = field(default_factory=dict)
    quota: Quota = field(default_factory=Quota)
    raw: dict = field(default_factory=dict)

    @property
    def short_id(self) -> str:
        """The eight-character prefix every one of the node's own logs and the
        CLI's ``--status`` print, so a caller rendering an id matches them."""
        return self.id[:8]

    @property
    def keyless(self) -> bool:
        """True when this node advertises no device key. A keyless node can never
        be verified and so is foreign to any peer with a trust allowlist."""
        return not self.pubkey

    @property
    def out_of_tokens(self) -> bool:
        """True when this node is excluded from every token-aware duty."""
        return self.tokens == "out"

    @classmethod
    def from_dict(cls, d: dict | None) -> "Node":
        d = d or {}
        return cls(**_node_fields(d), raw=d)


def _node_fields(d: dict) -> dict:
    """The part of a NodeInfo that self and peers share, read once so the two
    cannot drift into reading the same wire field differently."""
    return {
        "id": _str(d, "id"),
        "name": _str(d, "name"),
        "platform": _str(d, "platform"),
        "tier": _int(d, "tier", 3),
        "tokens": _str(d, "tokens", "ok"),
        "tokens_pct": _float(d, "tokensPct", 1.0),
        "tokens_session_pct": _opt_float(d, "tokensSessionPct"),
        "tokens_week_pct": _opt_float(d, "tokensWeekPct"),
        "tokens_auto": bool(d.get("tokensAuto", True)),
        "strength_auto": bool(d.get("strengthAuto", True)),
        "fingerprint": _str(d, "fingerprint"),
        "pubkey": _str(d, "pubkey"),
        "endpoint": _str(d, "endpoint"),
        "onion": _str(d, "onion"),
        "duties_enabled": _dict(d, "dutiesEnabled"),
        "quota": Quota.from_dict(_dict(d, "stats")),
    }


@dataclass(frozen=True, kw_only=True)
class Peer(Node):
    """A node as *this* node currently sees it.

    Everything :class:`Node` carries, plus the reader's own view: the state of the
    link, where it is, which transport carries it, whether the device key was
    actually proved, and what the local allowlist makes of it. ``trust`` and
    ``verified`` are deliberately separate - a peer can prove a key
    (``verified``) and still be ``foreign``, because proving a key says who you
    are and the allowlist says whether that matters here.
    """

    link: str = "down"
    addr: str = ""
    transport: str = "lan"
    trust: str = "foreign"
    verified: bool = False
    surplus: float = NEUTRAL_SURPLUS
    last_seen_secs: float | None = None
    uptime_secs: float | None = None

    @property
    def up(self) -> bool:
        return self.link == "up"

    @property
    def banned(self) -> bool:
        return self.trust == "banned"

    @property
    def personal(self) -> bool:
        """True when this peer is trusted to hand this machine real work."""
        return self.trust == "personal"

    @property
    def over_tor(self) -> bool:
        return self.transport == "tor"

    @classmethod
    def from_dict(cls, d: dict | None) -> "Peer":
        d = d or {}
        return cls(
            **_node_fields(d),
            link=_str(d, "link", "down"),
            addr=_str(d, "addr"),
            transport=_str(d, "transport", "lan"),
            trust=_str(d, "trust", "foreign"),
            verified=bool(d.get("verified", False)),
            surplus=_float(d, "surplus", NEUTRAL_SURPLUS),
            last_seen_secs=_opt_float(d, "lastSeenSecsAgo"),
            uptime_secs=_opt_float(d, "uptimeSecs"),
            raw=d,
        )


@dataclass(frozen=True, kw_only=True)
class Shortfall:
    """A platform a duty asked for and the mesh could not staff."""

    platform: str
    missing: int


@dataclass(frozen=True, kw_only=True)
class Assignment:
    """Who owns one duty, as every node in the mesh independently computed it.

    ``assigned`` is in rank order and holds node ids; resolve them against
    :meth:`Snapshot.node_named` or :attr:`Snapshot.by_id` to get names.
    """

    duty: str
    assigned: tuple[str, ...] = ()
    shortfall: tuple[Shortfall, ...] = ()

    @property
    def satisfied(self) -> bool:
        """True when every platform the duty's placement asked for was staffed."""
        return not self.shortfall

    @property
    def unowned(self) -> bool:
        """True when nobody at all holds this duty - work routed to it fails."""
        return not self.assigned

    @classmethod
    def from_dict(cls, duty: str, d: dict | None) -> "Assignment":
        d = d or {}
        assigned = [item for item in (d.get("assigned") or []) if isinstance(item, str)]
        return cls(
            duty=_str(d, "duty") or duty,
            assigned=tuple(assigned),
            shortfall=tuple(
                Shortfall(platform=_str(s, "platform"), missing=_int(s, "missing"))
                for s in _objects(d, "shortfall")
            ),
        )


@dataclass(frozen=True, kw_only=True)
class Slot:
    """One placement slot's outcome from a dispatch.

    A dispatch fills every slot its duty's placement asks for, walking failover
    candidates within each. ``status`` is the node's own word: ``spawned``,
    ``suppressed`` (a peer already owns the work behind the key), ``declined``,
    or ``failed``.
    """

    slot: str = ""
    node: str | None = None
    node_name: str | None = None
    status: str = "failed"
    reason: str = ""

    @property
    def spawned(self) -> bool:
        return self.status == "spawned"

    @property
    def suppressed(self) -> bool:
        """True when a peer already holds the work claim, so this node stood down.

        Asking for that is the whole point of a ``work_key``, which is why
        :attr:`ok` counts it as a success rather than a failure.
        """
        return self.status == "suppressed"

    @property
    def ok(self) -> bool:
        return self.spawned or self.suppressed

    @classmethod
    def from_dict(cls, d: dict | None) -> "Slot":
        d = d or {}
        node = d.get("node")
        name = d.get("nodeName")
        return cls(
            slot=_str(d, "slot"),
            node=node if isinstance(node, str) else None,
            node_name=name if isinstance(name, str) else None,
            status=_str(d, "status", "failed"),
            reason=_str(d, "reason"),
        )


@dataclass(frozen=True, kw_only=True)
class Dispatch:
    """Every slot a routed request produced.

    Iterable and indexable as its slots, so ``for slot in result`` reads the way
    the node's own ``--dispatch`` output does.
    """

    slots: tuple[Slot, ...] = ()

    @property
    def ok(self) -> bool:
        """True when every slot ended somewhere acceptable.

        An empty dispatch is not ok: a duty whose placement produced no slots at
        all routed the request nowhere, and reporting that as success is how work
        goes missing silently.
        """
        return bool(self.slots) and all(slot.ok for slot in self.slots)

    @property
    def spawned(self) -> tuple[Slot, ...]:
        return tuple(slot for slot in self.slots if slot.spawned)

    @property
    def suppressed(self) -> bool:
        """True when the request was stood down whole because a peer already owns
        the work key - the single-slot answer the claim gate returns."""
        return bool(self.slots) and all(slot.suppressed for slot in self.slots)

    def __iter__(self):
        return iter(self.slots)

    def __len__(self) -> int:
        return len(self.slots)

    def __getitem__(self, index: int) -> Slot:
        return self.slots[index]

    @classmethod
    def from_results(cls, results: list[dict] | None) -> "Dispatch":
        return cls(slots=tuple(Slot.from_dict(r) for r in (results or [])))


@dataclass(frozen=True, kw_only=True)
class Claim:
    """The claim gate's verdict on one unit of external work (12-work-claims).

    :attr:`owned` false is a normal, expected answer and not an error: a better
    live personal peer holds the lease, and the caller MUST NOT originate the
    work. ``owner`` is that peer's node id when there is one.
    """

    owned: bool = False
    owner: str | None = None
    owner_name: str | None = None

    def __bool__(self) -> bool:
        """``if mesh.claim(key):`` reads as "did I win it", which is the only
        question a caller asks here."""
        return self.owned

    @classmethod
    def from_dict(cls, d: dict | None) -> "Claim":
        d = d or {}
        owner = d.get("owner")
        name = d.get("ownerName")
        return cls(
            owned=bool(d.get("owned")),
            owner=owner if isinstance(owner, str) else None,
            owner_name=name if isinstance(name, str) else None,
        )


@dataclass(frozen=True, kw_only=True)
class Iroh:
    """The node's iroh transport: whether it is on, whether the endpoint is online
    yet, and the address to hand a peer for a manual dial."""

    enabled: bool = False
    ready: bool = False
    endpoint: str | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "Iroh":
        d = d or {}
        endpoint = d.get("endpoint")
        return cls(
            enabled=bool(d.get("enabled")),
            ready=bool(d.get("ready")),
            endpoint=endpoint if isinstance(endpoint, str) else None,
        )


@dataclass(frozen=True, kw_only=True)
class Tor:
    """The node's Tor transport, the :class:`Iroh` twin: whether it is on, whether
    the onion service is live yet, and the address to hand a peer for a manual
    dial."""

    enabled: bool = False
    ready: bool = False
    onion: str | None = None

    @classmethod
    def from_dict(cls, d: dict | None) -> "Tor":
        d = d or {}
        onion = d.get("onion")
        return cls(
            enabled=bool(d.get("enabled")),
            ready=bool(d.get("ready")),
            onion=onion if isinstance(onion, str) else None,
        )


@dataclass(frozen=True, kw_only=True)
class Device:
    """A device on the local trusted allowlist or ban list.

    Local marks, never gossiped: what this machine has decided about a device,
    keyed to a proved key fingerprint - or, for a keyless device that can have no
    fingerprint, to its node id.
    """

    fingerprint: str = ""
    node: str = ""
    label: str = ""
    reason: str = ""

    @classmethod
    def from_dict(cls, d: dict | None) -> "Device":
        d = d or {}
        return cls(
            fingerprint=_str(d, "fingerprint"),
            node=_str(d, "node"),
            label=_str(d, "label"),
            reason=_str(d, "reason"),
        )


@dataclass(frozen=True, kw_only=True)
class Snapshot:
    """Everything one node knows, at one moment.

    The same object whether it was read from ``state.json`` or answered live on a
    control session - the node stamps both identically, which is what lets a
    caller poll the cheap one and only open a socket when it needs to be current.
    """

    node: Node = field(default_factory=Node)
    peers: tuple[Peer, ...] = ()
    assignments: dict = field(default_factory=dict)
    trusted: tuple[Device, ...] = ()
    banned: tuple[Device, ...] = ()
    default_trust: str = "foreign"
    iroh: Iroh = field(default_factory=Iroh)
    tor: Tor = field(default_factory=Tor)
    tcp_port: int = 0
    pid: int = 0
    updated_at: str = ""
    version: int = 0
    beacon_blocked: bool = False
    beacon_block_reason: str = ""
    linking: int = 0
    raw: dict = field(default_factory=dict)

    @property
    def by_id(self) -> dict:
        """Every node in the snapshot, this machine included, keyed by id."""
        return {self.node.id: self.node, **{p.id: p for p in self.peers}}

    @property
    def up(self) -> tuple[Peer, ...]:
        """The peers this node currently holds a live link to."""
        return tuple(p for p in self.peers if p.up)

    def peer(self, id_or_name: str) -> Peer | None:
        """A peer by node id (full or the eight-character prefix) or by name."""
        for p in self.peers:
            if id_or_name in (p.id, p.short_id, p.name):
                return p
        return None

    def node_named(self, node_id: str) -> str:
        """The display name for a node id, falling back to its short id.

        The resolution the node's own ``--status`` does when it prints an
        assignment, so a caller rendering one produces the same line.
        """
        found = self.by_id.get(node_id)
        return (found.name if found and found.name else "") or node_id[:8]

    def assignment(self, duty: str) -> Assignment | None:
        return self.assignments.get(duty)

    @classmethod
    def from_dict(cls, d: dict | None) -> "Snapshot":
        d = d or {}
        assignments = {
            duty: Assignment.from_dict(duty, value)
            for duty, value in _dict(d, "assignments").items()
            if isinstance(value, dict)
        }
        return cls(
            node=Node.from_dict(_dict(d, "self")),
            peers=tuple(Peer.from_dict(p) for p in _objects(d, "peers")),
            assignments=assignments,
            trusted=tuple(Device.from_dict(e) for e in _objects(d, "trusted")),
            banned=tuple(Device.from_dict(e) for e in _objects(d, "banned")),
            default_trust=_str(d, "defaultTrust", "foreign"),
            iroh=Iroh.from_dict(_dict(d, "iroh")),
            tor=Tor.from_dict(_dict(d, "tor")),
            tcp_port=_int(d, "tcpPort"),
            pid=_int(d, "pid"),
            updated_at=_str(d, "updatedAt"),
            version=_int(d, "v"),
            beacon_blocked=bool(d.get("beaconBlocked")),
            beacon_block_reason=_str(d, "beaconBlockReason"),
            linking=_int(d, "linking"),
            raw=d,
        )
