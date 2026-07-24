"""The mesh node: discovery, peer links, gossip, duty failover, job dispatch.

One asyncio event loop drives everything:

- a **beacon** task adverts this node over UDP (multicast + subnet broadcast —
  receivers dedupe; Wi-Fi APs regularly eat one or the other);
- a UDP listener learns peers from their beacons and **dials** the ones whose
  id sorts above ours (the deterministic smaller-id-dials rule, so exactly one
  TCP link exists per pair);
- a **redial** task re-dials known-but-unlinked peers from their last
  authenticated address (persisted in ``peers.json``), so a mesh whose beacon
  channel died (AP multicast filtering, an OS privacy gate) still heals over
  unicast;
- each TCP **link** exchanges ``hello`` (full NodeInfo + LWW overrides), then
  heartbeats and gossip; a peer missing heartbeats past the timeout is marked
  down, its links closed, and duties recomputed — the takeover is logged to
  the shared activity feed;
- the same TCP port doubles as the **control** endpoint: a client opening with
  ``{"t":"ctl"}`` (the topology panel, the CLI) can read status, edit any
  node's attributes, edit placement overrides, and dispatch jobs;
- a **snapshot** task mirrors the topology to ``~/.diplomat/mesh/state.json``
  every couple of seconds for the UIs.

Peers stay visible in the snapshot for a few minutes after going down (link
``"down"``) so the topology panel shows *what* died rather than a silently
shrinking list.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import hmac
import json
import os
import secrets
import socket
import struct
import subprocess
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace

from .. import activity
from . import (
    assign, banned, config, crypto, identity, onioncache, peercache, protocol,
    spawnjob, statefile, stats, tor, trust, usage,
)
from .config import PlacementOverrides
from .protocol import Job, NodeInfo

# How long a dead peer stays in the snapshot (link "down") before it's dropped.
_DOWN_RETENTION_SECS = 300.0

# The errnos a macOS Local Network privacy denial (or a host firewall) raises when it
# refuses a LAN send while the socket itself is healthy. macOS 15's Local Network gate
# fails multicast/broadcast/same-subnet sends with EHOSTUNREACH; a packet filter may
# instead return EACCES/EPERM. All three mean "the OS refused this send" — as opposed to
# ENETDOWN/ENETUNREACH, which mean the network stack itself is gone. Used to tell an
# actionable permission problem apart from a genuinely downed network in the operator
# message for a total beacon-send outage (see _classify_beacon_block).
_LAN_GATE_ERRNOS = frozenset({errno.EHOSTUNREACH, errno.EACCES, errno.EPERM})

# How often the node re-measures real token usage from the local logs. The budget
# doesn't move second-to-second, and scanning ~/.claude costs real file I/O, so this
# is decoupled from the (fast) snapshot cadence rather than run every write.
_TOKEN_REFRESH_SECS = 30.0

# A sane home/office LAN has a handful of machines; this only bounds a beacon
# flood of spoofed ids from ballooning the peers table + snapshot, not real use.
_MAX_PEERS = 256

# The sibling bound for the redial-from-memory cache (peers.json). _MAX_PEERS caps
# the live table, but the cache persists an address per distinct AUTHENTICATED id
# and is never reaped with the table, so without this it grows unbounded under a
# churn of ids — an on-mesh flooder cycling ids across reap windows, or ephemeral-id
# peers (CI runners regenerating node.json each boot) — ballooning peers.json and the
# redial fan-out that iterates it. The cache is a best-effort accelerator, so it keeps
# the most-recently-contacted addresses and evicts the coldest past this bound.
_MAX_PEER_CACHE = _MAX_PEERS

# Hard cap on concurrent in-flight outbound dials. A beacon is UNAUTHENTICATED, so the
# _MAX_PEERS backstop in _on_beacon (which counts self.peers) never fires against a
# flooder that answers our dial but never completes a hello — self.peers stays empty.
# Capping the dial fan-out directly is what the author intended _MAX_PEERS to do for it;
# real use dials a handful of peers at once, far under this.
_MAX_INFLIGHT_DIALS = _MAX_PEERS

# How long an outbound dialed link (talking to whoever answered a spoofable beacon)
# waits for its first valid hello before being dropped — the mirror of the inbound
# path's 10s first-read timeout in _on_tcp_connection. Without it a silent/slowloris
# peer pins the fd + Task + _dialing entry forever (an unbounded fd leak under a flood,
# since nothing at runtime reaps a dialed link that never entered self.peers).
_LINK_HELLO_TIMEOUT_SECS = 10.0

# Upper bound on stored work-claim records (work_key × claimant). Real use holds a
# handful of in-flight work items; this only stops a gossip flood of spoofed
# work_keys from ballooning the claim book without bound. See _store_claim.
_MAX_CLAIMS = 4096

# Upper bounds on the foreign request/response bookkeeping (executor's unacked
# results, originator's awaited results + already-acted job ids). Real use holds a
# few in-flight foreign jobs; these just stop a flood from growing memory without
# bound. See the foreign-execution section. Entries also expire on their own
# deadline, so the caps are a backstop, not the primary reclaim.
_MAX_FOREIGN = 1024

# A confined job's returned artifact travels inside a single `job-result` NDJSON
# line, so cap it well under the wire line limit (MAX_LINE_BYTES, 512 KiB) — the
# JSON envelope + escaping needs headroom. A larger artifact is truncated.
_MAX_RESULT_BYTES = 400 * 1024

# How many GIVEN-UP result tombstones the executor keeps around so a later
# `job-reminder` can revive their delivery (accountability). Each holds a full
# built result line, so this is deliberately much tighter than _MAX_FOREIGN —
# reviving is a courtesy to an originator that was unreachable, not a queue.
_MAX_TOMBSTONES = 64

# Domain-separation prefix for the trust proof-of-possession signature. The peer
# signs this tag + the challenge nonce, never the bare nonce — so a captured
# signature is meaningless outside SzpontNet's auth exchange and the device key
# can't be coaxed into acting as a general signing oracle over attacker-chosen
# bytes. The exact byte construction is normative (see docs/szpontnet/11).
_AUTH_CONTEXT = b"szpontnet-auth-v1:"

# Tor reconnect backoff — per known peer we hold an onion for but can't currently
# see on the LAN. Start small, grow geometrically to a ceiling, and reset the
# moment the onion answers. The LAN↔Tor quality gap is small, so probing is
# deliberately unhurried and a peer already linked (over EITHER transport) is never
# probed — no aggressive switching. See the Tor transport section.
_TOR_BACKOFF_MIN_SECS = 10.0
_TOR_BACKOFF_MAX_SECS = 600.0
_TOR_BACKOFF_FACTOR = 2.0
# How often the Tor reconnect loop wakes to check which known peers are due.
_TOR_REDIAL_TICK_SECS = 5.0
# Upper bound on one Tor dial + SOCKS handshake before it's abandoned (and the
# backoff grows). A cold onion connect can legitimately take double-digit seconds.
_TOR_DIAL_TIMEOUT_SECS = 30.0


def _auth_challenge(nonce: str) -> bytes:
    """The exact bytes signed/verified for a proof-of-possession `auth`:
    the domain tag followed by the UTF-8 challenge nonce."""
    return _AUTH_CONTEXT + nonce.encode()


def _utf8(s: str) -> bytes:
    """UTF-8-encode a possibly-hostile wire string TOTALLY. A JSON string can decode to
    a lone surrogate (e.g. ``"\\ud800"``), which a plain ``.encode("utf-8")`` rejects with
    UnicodeEncodeError (a ValueError subclass). Encoding with ``surrogatepass`` makes it
    total, so a hostile ``secret``/``apiKey`` compares UNEQUAL via ``hmac.compare_digest``
    instead of RAISING into — and thus orphaning the socket on — the pre-auth accept path
    (``_on_tcp_connection`` runs outside any try, so an escaping raise leaks the fd). An
    operator's own valid config value encodes byte-identically either way. Mirrors the
    ``surrogatepass`` the work_key/job_id staging paths already use."""
    return s.encode("utf-8", "surrogatepass")


def _own_addresses() -> set[str]:
    """This machine's own IP addresses (loopback + LAN), so a node can tell its
    own looped-back beacon from a genuine peer sharing its id. Best-effort; an
    address we miss only risks a spurious one-time clone warning, never a
    functional failure."""
    addrs = {"127.0.0.1", "::1"}
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None):
            addrs.add(info[4][0])
    except OSError:
        pass
    # The primary outbound IP — getaddrinfo(hostname) often misses the DHCP LAN
    # address, but this UDP-connect trick resolves it without sending a packet.
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        addrs.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return addrs


@dataclass
class _PendingResult:
    """An executor's unacked ``job-result`` owed to a foreign requester. Held until
    the originator ``job-ack``s it or the deadline passes, and re-emitted on the
    heartbeat tick — reliable delivery, not fire-and-forget. ``msg`` is the fully
    built (signed) job-result dict, re-sent verbatim each retry; ``to_node`` is the
    originator whose link we send it on (looked up fresh each time so a flapped link
    heals). Past the deadline the entry becomes a **tombstone** (``gave_up``):
    retries stop, but the result is kept for the accountability window so a
    `job-reminder` from the originator revives its delivery instead of getting us
    banned for work we actually did (docs/szpontnet/13)."""

    msg: dict
    to_node: str
    next_retry: float  # monotonic; re-emit when reached
    deadline: float    # monotonic; stop retrying (originator presumed gone) past this
    created: float = 0.0  # monotonic; bounds the tombstone's total lifetime
    gave_up: bool = False  # tombstone: kept for reminder-revival only


@dataclass
class _Awaiting:
    """An originator's record of one remote dispatch it will accept a
    ``job-result`` for — plus, when the executor is **foreign** and accepted
    (``spawned`` without ``direct``), the accountability clock over its promise
    (docs/szpontnet/13#accountability-deadline-reminder-ban): the completion
    ``deadline`` (armed = not None), when the "is this ready?" reminder went out,
    how many agent-approved extensions it has already received, and whether an
    extension decision is currently in flight."""

    executor_id: str
    duty: str
    added: float  # monotonic
    prompt_head: str = ""  # truncated prompt — context for the extension decider
    executor_fp: str = ""  # fingerprint the executor PROVED at accept time — the
    # ban binds to this even after the peer is reaped, so a keyed executor that goes
    # silent can't drop its ban by reconnecting (a fingerprint-less, id-only ban is
    # bypassed the instant the device presents any key — see _ban_executor).
    deadline: float | None = None  # monotonic; None = accountability not armed
    reminded_at: float | None = None  # monotonic; grace runs from here
    next_remind: float = 0.0  # monotonic; reminders re-send across the grace window
    extensions: int = 0
    deciding: bool = False


@dataclass
class _TorBackoff:
    """Per-peer Tor reconnect schedule: when the next probe is due (monotonic) and
    the current interval — doubled on each miss up to a ceiling, and dropped entirely
    the moment a Tor link to the peer actually BINDS (a valid signed hello), not on a
    bare SOCKS answer — so a reachable peer reconnects promptly while an onion that
    answers TCP but never links stays throttled. See the Tor transport section in
    :class:`MeshNode`."""

    next_attempt: float = 0.0
    interval: float = _TOR_BACKOFF_MIN_SECS


class Peer:
    """One known remote node: its gossiped info + the (single) live link."""

    def __init__(self, info: NodeInfo, addr: str) -> None:
        self.info = info
        self.addr = addr
        self.last_seen = time.monotonic()
        self.writer: asyncio.StreamWriter | None = None
        self.down_since: float | None = None
        # When the CURRENT link came up (monotonic), for the uptime badge. Set when
        # a link is established, cleared when it drops; survives up↔stale flapping
        # (stale is just heartbeat-age — the socket is still open) so uptime counts
        # continuous connection, not "seconds since the last packet".
        self.linked_since: float | None = None
        # Fingerprint of the key this peer PROVED it holds on the link (signed our
        # challenge). None until verified. Trust keys on this, never on info.pubkey
        # alone - a peer can advertise any pubkey but only sign for its own.
        self.verified_fp: str | None = None
        # Which transport the CURRENT link runs over: "lan" (direct TCP) or "tor"
        # (an onion circuit). Set when the link binds; display/diagnostic only —
        # trust and behavior are transport-agnostic.
        self.transport = "lan"

    @property
    def linked(self) -> bool:
        return self.writer is not None

    def link_state(self, stale_secs: float, timeout_secs: float) -> str:
        if not self.linked:
            return "down"
        age = time.monotonic() - self.last_seen
        if age > timeout_secs:
            return "down"
        return "stale" if age > stale_secs else "up"


class MeshNode:
    def __init__(self) -> None:
        self.proto = config.protocol()
        self.local = identity.load()
        self.stats = stats.load()  # per-node usage/quota accounting for load balancing
        self.key = crypto.load_or_create()  # this device's Ed25519 trust identity
        self._trusted = trust.load()  # local allowlist of trusted (personal) fingerprints
        # Local ban list: devices that accepted a SzpontRequest of ours and failed
        # to deliver it (or were banned manually). Machine-local, never gossiped.
        self._banned = banned.load()
        # Trust level for an UNKNOWN device (not in the allowlist / unverified): the
        # operator's persisted panel choice if any, else the node baseline (env /
        # core/mesh.json, ships 'foreign' → a new device is zero-trust until promoted).
        self._default_trust = trust.load_default_level() or config.default_trust()
        self.platform = identity.detect_platform()
        self.epoch = time.time()
        # Cached automatic token-budget read (refreshed on a throttle, so neither the
        # hot `info` path nor the 2s snapshot tick touches the filesystem/network).
        # See _refresh_tokens / _TOKEN_REFRESH_SECS. session/week are the REAL
        # remaining fractions per rate-limit window (None on the heuristic fallback).
        self._token_state = "ok"
        self._token_frac = 1.0
        self._token_session: float | None = None
        self._token_week: float | None = None
        # Burn-down ratio across those real windows — budget left over clock left
        # until they reset. The figure dispatch ranks on; None on the heuristic
        # fallback, where the local bookkeeping window is paced instead.
        self._token_pace: float | None = None
        self._last_token_refresh = 0.0  # monotonic; 0 => refresh on the first tick
        self.tcp_port = 0  # bound in start()
        self.peers: dict[str, Peer] = {}
        self.overrides = PlacementOverrides()
        self._assignments: dict[str, assign.DutyAssignment] = {}
        self._seq = 0
        self._tasks: list[asyncio.Task] = []
        self._server: asyncio.base_events.Server | None = None
        self._udp_send: socket.socket | None = None
        self._udp_recv: socket.socket | None = None
        self._stopping = asyncio.Event()
        # In-flight remote dispatches awaiting a job-status answer, by job id.
        # Each entry is (future, target_node_id) so a job-status is only honored
        # from the peer we actually dispatched to (not any other linked peer).
        self._job_futures: dict[str, tuple[asyncio.Future, str]] = {}
        # Peers we're currently dialing — beacons repeat faster than a handshake
        # completes, and the peers map only learns the link at hello time.
        self._dialing: set[str] = set()
        # Strong refs to fire-and-forget dial coroutines, so the loop can't GC a
        # task mid-handshake (the asyncio create_task footgun).
        self._dial_tasks: set[asyncio.Task] = set()
        self._warned_id_clone = False
        # This machine's own addresses, so a self-beacon looping back (source =
        # loopback OR the real LAN IP) isn't mistaken for a cloned-id peer.
        self._local_addrs = _own_addresses()
        # Last-known dialable addresses of authenticated peers (persisted), so a
        # dropped link can be redialed even while the beacon channel is dead.
        self._peer_cache = peercache.load()
        # Known peers' PERMANENT onion addresses (onions.json), learned from their
        # SIGNED adverts — the WAN sibling of _peer_cache. Once two nodes have met
        # (on the LAN, or by a manual paste) either can redial the other over Tor
        # from anywhere, no public IP or DNS. See onioncache / mesh/tor.py.
        self._onion_cache = onioncache.load()
        # The Tor transport (a persistent onion service + SOCKS dialer). Created in
        # start() only when DIPLOMAT_MESH_TOR=1 and the `tor` binary is present;
        # None keeps the node LAN-only, exactly as before.
        self.tor: tor.TorTransport | None = None
        # Onions currently being dialed over Tor (auto-redial or a manual paste),
        # so a repeat tick / paste never opens a second circuit to the same peer.
        self._tor_dialing: set[str] = set()
        # Per-peer Tor reconnect backoff, keyed by peer id (see _TorBackoff).
        self._tor_backoff: dict[str, _TorBackoff] = {}
        # Transport of each live link, keyed by its writer ("lan" default, "tor" for
        # an onion circuit) — set on dial / Tor-inbound accept, read when the link
        # binds a peer, cleared on teardown. It is what lets an INBOUND Tor link
        # (which lands on loopback) be told apart from a loopback LAN link, so a Tor
        # link's address never pollutes the LAN redial cache.
        self._link_transport: dict[asyncio.StreamWriter, str] = {}
        # Whether the last beacon tick failed EVERY send — the node is then
        # undiscoverable and says so (activity feed + snapshot) instead of failing
        # silently. Flips back on the first successful send. See _note_beacon_sends.
        self._beacon_blocked = False
        # Why it is blocked, mirrored into the snapshot so a front-end banner shows the
        # SAME diagnosis the activity log does: "local-network" (an OS/firewall gate the
        # operator can fix) or "network-down" (the stack itself is gone). "" while up.
        self._beacon_block_reason = ""
        # The trust challenge nonce THIS node issued on each link, keyed by writer,
        # so an inbound `auth` can be verified against the nonce we chose.
        self._issued_nonce: dict[asyncio.StreamWriter, str] = {}
        # Work-claim book: work_key -> {claimant node id -> freshest ClaimRecord},
        # plus our own per-key seq counter. A claim is an origination lease; the
        # owner of a key is the lowest-id live+personal active claimant. See the
        # work-claims section below and docs/szpontnet/12-work-claims.md.
        self._claims: dict[str, dict[str, protocol.ClaimRecord]] = {}
        self._claim_seq: dict[str, int] = {}
        # work_key -> monotonic time we last emitted a 'released' self-claim. Drives
        # reaping of our own settled tombstones so a long-lived node's book doesn't
        # grow one permanent released record per distinct work_key ever handled
        # (which also, before the cap counted only peer records, could starve new
        # peer claims). Cleared when the key is re-claimed 'active'.
        self._released_at: dict[str, float] = {}
        # Optional hook fired when a better (lower-id) peer preempts a work_key this
        # node was originating, so a caller (e.g. an auto-poller) can abort the
        # local work it started. None = no-op (the default; origination is manual).
        self.on_claim_lost: Callable[[str], None] | None = None
        # Agents this node is EXECUTING for a dedup key, work_key -> {done, at}. The
        # executor claims the key when it spawns the agent and releases it when the
        # agent's completion sentinel (`done`) appears — so a re-scan of the same
        # work is suppressed while it runs and freed when it finishes
        # (docs/szpontnet/12). Presence is also the local idempotency guard: a
        # second dispatch of a key we already run never spawns a duplicate.
        self._agents: dict[str, dict] = {}
        # Backstop: free an executor's claim if its completion sentinel is ever lost
        # (a SIGKILL'd terminal, say), so a claim can never pin a key forever. Well
        # above any real agent runtime; the sentinel is the normal path.
        self._agent_max_secs = float(os.environ.get("DIPLOMAT_MESH_AGENT_MAX_SECS", "7200"))
        # Foreign zero-trust request/response bookkeeping (docs/szpontnet/13):
        #  - as EXECUTOR: results we computed for a foreign requester and owe back to
        #    it, keyed by job id, re-emitted until job-ack'd (reliable delivery);
        #  - as ORIGINATOR: remote dispatches we're still willing to receive a
        #    job-result for (job id → (executor id, duty, added_monotonic)), and the
        #    job ids we've already acted on, so a retried result is re-acked but never
        #    acted on twice (idempotent). Both expire by time and are capped.
        self._pending_results: dict[str, _PendingResult] = {}
        self._awaiting_result: dict[str, _Awaiting] = {}
        self._acted_results: dict[str, float] = {}
        # Confined jobs currently computing here, job id → (requester id, started
        # monotonic) — so a `job-reminder` for a still-running job can be answered
        # truthfully with a `job-progress` instead of silence (which gets us banned).
        self._confined_running: dict[str, tuple[str, float]] = {}
        # Strong refs to the per-job confined-result watcher coroutines, so the loop
        # can't GC one mid-wait (the asyncio create_task footgun).
        self._result_tasks: set[asyncio.Task] = set()

    # MARK: - identity / gossip source of truth

    @property
    def info(self) -> NodeInfo:
        info = NodeInfo(
            id=self.local.id,
            name=self.local.name,
            platform=self.platform,
            tier=self.local.tier,
            tokens=self.current_tokens(),
            strength_auto=self.local.strength_auto,
            tokens_auto=(self.local.tokens == "auto"),
            tokens_pct=self._token_frac,
            tokens_session_pct=self._token_session,
            tokens_week_pct=self._token_week,
            tcp_port=self.tcp_port,
            epoch=self.epoch,
            seq=self._seq,
            sees=tuple(sorted(pid for pid, p in self.peers.items() if p.linked)),
            duties_enabled=self.local.duties_enabled,
            pubkey=self.key.public_b64 if self.key else "",
            onion=self._onion_address(),
            stats=self.stats.advertise(real_frac=self._real_quota_frac(),
                                       pace=self._token_pace),
        )
        return self._sign_advert(info)

    def _onion_address(self) -> str:
        """This node's advertised permanent onion, or '' when Tor is off / not yet
        bootstrapped. Rides inside the signed advert (bound to the device key)."""
        return (self.tor.onion_address() or "") if self.tor else ""

    def _sign_advert(self, info: NodeInfo) -> NodeInfo:
        """Attach our Ed25519 signature over the advert's canonical form. This
        authenticates the advertisement end to end: any peer may relay it, but none
        can forge or tamper with it without our private key (the receiver verifies
        the `sig` against the advert's own `pubkey`). A keyless node returns the
        advert unsigned — it can never be verified, hence foreign under any
        allowlist, exactly as before."""
        if self.key is None:
            return info
        sig = self.key.sign(protocol.advert_signing_bytes(info.to_dict()))
        return replace(info, sig=sig)

    # MARK: - automatic token budget

    def current_tokens(self) -> str:
        """This node's EFFECTIVE token state: the manual override when pinned, else
        the state auto-derived from real local usage (cached in ``_token_state``)."""
        override = self.local.tokens
        return override if override in ("ok", "low", "out") else self._token_state

    def _real_quota_frac(self) -> float | None:
        """The REAL remaining fraction of the binding rate-limit window (min of
        the 5-hour session and 7-day week) when the OAuth probe is live, else
        None (heuristic fallback). Caps the advertised ``stats.quotaLeft`` so
        dispatch surplus reflects the account's true room — a node with 2% of
        its session left must not out-rank peers on bookkeeping alone."""
        return self._token_frac if self._token_session is not None else None

    def _gossiped_tokens(self) -> tuple:
        """What peers currently know of our token budget, at wire granularity —
        the change-detection key for re-gossiping."""
        def r(v: float | None) -> float | None:
            return None if v is None else round(v, 2)
        # Pace is compared at bucket granularity, so re-gossip at that granularity
        # too — otherwise its steady drift toward the reset would re-advertise the
        # node every refresh tick without changing any routing decision.
        pace = (None if self._token_pace is None
                else protocol.surplus_bucket(self._token_pace))
        return (self.current_tokens(), r(self._token_frac),
                r(self._token_session), r(self._token_week), pace)

    async def _refresh_tokens(self) -> bool:
        """Re-probe the token budget (real quota endpoint, else local-log heuristic)
        and recompute the auto state + remaining fractions. Returns True when what
        peers see changed — the state flipped or a percentage moved — so the caller
        can re-gossip. Runs the probe in a worker thread: it may touch the network
        (~1s, worst case a few seconds' timeout) and must not stall the event loop."""
        before = self._gossiped_tokens()
        try:
            state, frac, session, week, pace = await asyncio.to_thread(
                usage.token_state, self.stats.plan)
        except Exception:  # noqa: BLE001 — a broken probe must never take the node down
            state, frac = self._token_state, self._token_frac
            session, week = self._token_session, self._token_week
            pace = self._token_pace
        self._token_state, self._token_frac = state, frac
        self._token_session, self._token_week = session, week
        self._token_pace = pace
        return self._gossiped_tokens() != before

    @property
    def fingerprint(self) -> str:
        return self.key.fingerprint if self.key else ""

    def _alive_nodes(self) -> list[NodeInfo]:
        """The assignment input: self + every peer whose link is up or stale.
        (A stale peer still owns its duties — flapping Wi-Fi shouldn't bounce
        assignments; only a full timeout moves work.)"""
        stale, timeout = self.proto["peerStaleSecs"], self.proto["peerTimeoutSecs"]
        nodes = [self.info]
        nodes += [
            p.info for p in self.peers.values()
            if p.link_state(stale, timeout) != "down"
        ]
        return nodes

    # MARK: - lifecycle

    async def run(self) -> None:
        await self.start()
        try:
            await self._stopping.wait()
        finally:
            await self.stop()

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._sweep_stale_sentinels()  # clear prior-incarnation orphan sentinels
        await self._start_tcp()
        self._start_udp(loop)
        self._tasks = [
            loop.create_task(self._beacon_loop(), name="mesh-beacon"),
            loop.create_task(self._redial_loop(), name="mesh-redial"),
            loop.create_task(self._heartbeat_loop(), name="mesh-heartbeat"),
            loop.create_task(self._snapshot_loop(), name="mesh-snapshot"),
        ]
        if config.tor_enabled():
            # The WAN transport is an ATOMIC add-on: it brings up a persistent onion
            # service (bootstrapped in the background so the LAN stays usable
            # immediately) and a reconnect loop that Tor-dials known-but-unseen peers
            # with exponential backoff. Nothing else in the node changes.
            tor_binary = tor.binary()
            if tor_binary:
                self.tor = tor.TorTransport(identity.mesh_dir(),
                                            binary_path=tor_binary)
                self._tasks.append(loop.create_task(self._tor_serve(),
                                                    name="mesh-tor-serve"))
                self._tasks.append(loop.create_task(self._tor_redial_loop(),
                                                    name="mesh-tor-redial"))
            else:
                activity.log("mesh", "warn",
                             "Mesh/Tor: DIPLOMAT_MESH_TOR=1 but no 'tor' binary "
                             "found — running LAN-only.")
        await self._refresh_tokens()  # seed the auto token state before the first advert
        self._last_token_refresh = time.monotonic()
        self._recompute("start")
        activity.log("mesh", "mesh-up",
                     f"Mesh node up: {self.local.name} ({self.platform}) :{self.tcp_port}")

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._tasks = []
        for t in list(self._dial_tasks):
            t.cancel()
        # Await the cancelled dial tasks (incl. in-flight Tor dials) rather than just
        # dropping them, so a non-CancelledError raised during their teardown surfaces
        # here instead of as a swallowed "Task exception was never retrieved". Tor is
        # still alive at this point (stopped last, below), so an outbound dial's writer
        # close has a live SOCKS to close against.
        for t in list(self._dial_tasks):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
        self._dial_tasks.clear()
        # Confined-result watcher tasks live in their own set (created per foreign
        # job); cancel them too or they outlive the node — waking to touch links we
        # just closed and leaving "Task was destroyed but is pending" noise.
        for t in list(self._result_tasks):
            t.cancel()
        self._result_tasks.clear()
        for p in self.peers.values():
            self._close_link(p)
        if self._server:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        # Unregister the UDP reader and close both datagram sockets (the recv
        # socket also drops its multicast membership on close).
        if self._udp_recv is not None:
            loop = asyncio.get_running_loop()
            with contextlib.suppress(Exception):
                loop.remove_reader(self._udp_recv)
            self._udp_recv.close()
            self._udp_recv = None
        if self._udp_send:
            self._udp_send.close()
            self._udp_send = None
        # Terminate our Tor child last, so any in-flight link teardown above still
        # had a live SOCKS/onion to close against.
        if self.tor is not None:
            await self.tor.stop()
            self.tor = None

    def request_stop(self) -> None:
        self._stopping.set()

    # MARK: - sockets

    async def _start_tcp(self) -> None:
        """Bind the first free port in the shared range; the beacon tells peers
        which one we got (several nodes share one host in the tests)."""
        host = "127.0.0.1" if config.loopback_only() else "0.0.0.0"
        base, span = self.proto["tcpPortBase"], self.proto["tcpPortSpan"]
        last_err: Exception | None = None
        for port in range(base, base + span):
            try:
                self._server = await asyncio.start_server(
                    self._on_tcp_connection, host, port,
                    limit=protocol.MAX_LINE_BYTES,
                )
                self.tcp_port = port
                return
            except OSError as exc:
                last_err = exc
        raise RuntimeError(f"no free mesh TCP port in {base}..{base + span - 1}: {last_err}")

    def _start_udp(self, loop: asyncio.AbstractEventLoop) -> None:
        recv = self._make_udp_recv()
        loop.add_reader(recv, self._on_udp_readable, recv)
        self._udp_recv = recv
        self._udp_send = self._make_udp_send()

    def _make_udp_recv(self) -> socket.socket:
        """Receive socket: all nodes (across hosts AND within one host, via
        SO_REUSEPORT) bind the shared discovery port and join the group."""
        group, port = self.proto["multicastGroup"], self.proto["multicastPort"]
        iface_ip = "127.0.0.1" if config.loopback_only() else "0.0.0.0"
        recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        recv.bind(("", port))
        mreq = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(iface_ip))
        recv.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        recv.setblocking(False)
        return recv

    def _make_udp_send(self) -> socket.socket:
        """Send socket: multicast (+ broadcast off-loopback, for APs that drop
        multicast)."""
        lo = config.loopback_only()
        iface_ip = "127.0.0.1" if lo else "0.0.0.0"
        send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        send.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        send.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface_ip)
        )
        if not lo:
            send.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        send.setblocking(False)
        return send

    def _rebuild_udp_send(self) -> None:
        """Swap in a fresh send socket. macOS pins the Local Network verdict to a
        socket when it is created, so a socket built while the permission was
        denied keeps failing FOREVER — even after the user re-allows the app in
        System Settings. Recovering therefore requires a new socket; rebuilding
        while still denied just fails the same way (cheap and harmless). The new
        socket is built before the old one is closed so a failed rebuild never
        leaves the node with no send socket at all."""
        try:
            send = self._make_udp_send()
        except OSError:
            return
        if self._udp_send is not None:
            with contextlib.suppress(OSError):
                self._udp_send.close()
        self._udp_send = send

    def _rebuild_udp_recv(self) -> None:
        """Swap in a fresh receive socket, same reason as ``_rebuild_udp_send``:
        one created under a denial stays deaf after the grant returns. Called on
        the blocked→recovered transition (from the beacon task, so the loop is
        running). Build-first ordering keeps the old socket on a failed rebuild."""
        loop = asyncio.get_running_loop()
        try:
            recv = self._make_udp_recv()
        except OSError:
            return
        if self._udp_recv is not None:
            with contextlib.suppress(Exception):
                loop.remove_reader(self._udp_recv)
            with contextlib.suppress(OSError):
                self._udp_recv.close()
        loop.add_reader(recv, self._on_udp_readable, recv)
        self._udp_recv = recv

    # MARK: - discovery

    async def _beacon_loop(self) -> None:
        group, port = self.proto["multicastGroup"], self.proto["multicastPort"]
        while True:
            # While blocked, try a FRESH send socket each tick: the OS pins the
            # Local Network verdict to the socket at creation, so only a new
            # socket can observe a restored permission. This makes recovery
            # automatic (within one beacon interval) instead of needing a node
            # restart after the user flips the setting back on.
            if self._beacon_blocked:
                self._rebuild_udp_send()
            payload = protocol.encode(protocol.beacon(self.info))
            sent, err = 0, None
            try:
                self._udp_send.sendto(payload, (group, port))
                sent += 1
            except OSError as exc:
                err = exc
            if not config.loopback_only():
                try:
                    self._udp_send.sendto(payload, ("255.255.255.255", port))
                    sent += 1
                except OSError as exc:
                    err = exc
            was_blocked = self._beacon_blocked
            self._note_beacon_sends(sent, err)
            if was_blocked and not self._beacon_blocked:
                # Sends recovered — the recv socket may hold the same pinned
                # denial (deaf to inbound beacons); rebuild it too so discovery
                # resumes in both directions.
                self._rebuild_udp_recv()
            await asyncio.sleep(self.proto["beaconIntervalSecs"])

    def _note_beacon_sends(self, sent: int, err: OSError | None) -> None:
        """Track whether beacons reach the network at all, and surface a TOTAL
        send outage to the operator instead of swallowing it. A node whose every
        beacon send fails is undiscoverable — peers will never (re)dial it — which
        reads as "the mesh silently broke" with no visible cause. The classic
        trigger is not a network fault but an OS privacy gate (macOS 15's Local
        Network permission fails LAN sends with EHOSTUNREACH) while unicast links
        still work, so the node otherwise looks healthy.

        The diagnosed reason is re-evaluated EVERY blocked tick (not just on the
        entry transition) so the snapshot/banner always reflect the CURRENT cause:
        a downed network that recovers into a Local Network gate — sends never
        succeeding in between — must flip network-down → local-network, or the
        banner would keep telling the operator "the net is down" and hide the one
        fix (the permission toggle) that now applies. But it is LOGGED only on the
        block/recover transition and whenever the reason changes, never per tick."""
        if sent == 0:
            reason = self._classify_beacon_block(err)
            if not self._beacon_blocked or reason != self._beacon_block_reason:
                # Entering the outage, or the cause changed under a continuous outage
                # (a different fix applies) — surface the CURRENT diagnosis so the log
                # and the banner agree. Steady ticks on an unchanged cause stay quiet.
                activity.log("mesh", "warn", self._beacon_block_message(reason, err))
            self._beacon_blocked = True
            self._beacon_block_reason = reason
        elif self._beacon_blocked:
            self._beacon_blocked = False
            self._beacon_block_reason = ""
            activity.log("mesh", "mesh-up", "Mesh: beacon sending recovered")

    def _loopback_send_ok(self) -> bool:
        """Can this process put a datagram on the wire at all? A send to 127.0.0.1
        never leaves the host, so it bypasses the macOS Local Network privacy gate
        and any LAN firewall. If it succeeds while every real beacon send fails, the
        socket layer is healthy and only OFF-host traffic is being refused — a
        permission/firewall gate the operator can fix, not a downed network stack.
        A FRESH socket each call: the OS pins the Local Network verdict at socket
        creation, so a reused (possibly denied) socket would misreport."""
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError:
            return False
        try:
            probe.setblocking(False)
            probe.sendto(b"", ("127.0.0.1", self.proto["multicastPort"]))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def _classify_beacon_block(self, err: OSError | None) -> str:
        """Diagnose a TOTAL beacon-send outage from the actual failure signature
        instead of a fixed guess. A LAN-gate errno WITH a working loopback send means
        the socket is fine and only OFF-host traffic is refused — an OS/firewall gate
        the operator can fix ("local-network"). Anything else (a non-gate errno, or a
        loopback that also fails) means the network stack itself looks gone
        ("network-down"). Cheap enough to run every blocked tick: one throwaway
        loopback datagram. The value is mirrored into the snapshot so the app's banner
        shows the same diagnosis the log does."""
        gated = (err is not None and err.errno in _LAN_GATE_ERRNOS
                 and self._loopback_send_ok())
        return "local-network" if gated else "network-down"

    def _beacon_block_message(self, reason: str, err: OSError | None) -> str:
        """The operator-facing line for a total beacon-send outage, built from the
        diagnosed ``reason``. The old message always told macOS users to "allow Python"
        in Local Network settings — useless (and infuriating) when it is already
        allowed, and simply wrong when the network is down."""
        if reason == "network-down":
            return (f"Mesh: every beacon send is failing ({err}) and even a loopback "
                    "send does not — the network stack looks down (no usable "
                    "interface). This node is undiscoverable until it recovers.")
        base = (f"Mesh: every LAN beacon send is failing ({err}) while loopback works "
                "— the OS or a firewall is blocking this node's LAN traffic, so peers "
                "cannot discover it.")
        if self.platform == "macos":
            base += (" Fix in System Settings → Privacy & Security → Local Network. If "
                     "this Python already appears enabled there, the grant has not "
                     "taken effect — common for an unsigned/Homebrew interpreter: "
                     "toggle it off and back on, or point the node at a signed Python "
                     "via DIPLOMAT_PYTHON.")
        else:
            base += (" Check the host firewall is not dropping this node's multicast/"
                     "broadcast on the mesh port.")
        return base

    def _on_udp_readable(self, sock: socket.socket) -> None:
        # Drain everything queued; each datagram is one beacon line.
        while True:
            try:
                data, (host, _) = sock.recvfrom(4096)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            msg = protocol.decode(data)
            if not msg or msg.get("t") != "beacon":
                continue
            self._on_beacon(msg, host)

    def _on_beacon(self, msg: dict, host: str) -> None:
        peer_id = str(msg.get("id", ""))
        if not peer_id:
            return
        if peer_id == self.local.id:
            # A beacon carrying OUR id from a DIFFERENT machine means two machines
            # share a cloned node.json — they'd never link (each ignores the
            # other's beacon) and a third node would flip-flop between them. Warn
            # once so the collision is diagnosable instead of silent. But our own
            # multicast/broadcast beacon loops back with the source set to one of
            # THIS machine's own addresses (loopback OR its real LAN IP off the
            # real interface), which is not a clone — suppress those.
            if host not in self._local_addrs and not self._warned_id_clone:
                self._warned_id_clone = True
                activity.log("mesh", "warn",
                             f"Mesh: another host ({host}) advertises our node id — "
                             f"duplicate node.json? give each machine its own.")
            return
        tcp_port = msg.get("tcpPort")
        # An out-of-range port is not just invalid — asyncio.open_connection() would
        # raise OverflowError (not OSError) from the C bind, escaping _dial's except
        # and crashing the dial task. Reject anything a socket can't hold up front.
        # `bool` is an int subclass, so a JSON `true`/`false` would otherwise pass as
        # port 1/0; a boolean is not a "positive integer" tcpPort, so reject it too.
        if (isinstance(tcp_port, bool) or not isinstance(tcp_port, int)
                or not 0 < tcp_port <= 65535):
            return
        peer = self.peers.get(peer_id)
        if peer is not None and peer.linked:
            # A higher epoch means the peer *may* have restarted behind our back.
            # But a beacon is UNAUTHENTICATED — anything on the LAN can forge one
            # carrying a peer's id, a bogus tcpPort, and a huge epoch. Honoring it
            # against a HEALTHY link would let an attacker evict a live,
            # cryptographically-verified link and redirect our redial to the
            # attacker's advertised address (a link-hijack / persistent DoS).
            # A genuine restart makes the old link go quiet within peerStaleSecs
            # (the dead process's socket closes, or heartbeats simply stop), so we
            # only act on the restart hint once the link is no longer fresh — and
            # we do NOT let a beacon rewrite a live peer's address at all.
            try:
                epoch = float(msg.get("epoch", 0.0))
            except (TypeError, ValueError, OverflowError):
                return  # a malformed beacon epoch must never raise out of the reader
            quiet = (time.monotonic() - peer.last_seen) > self.proto["peerStaleSecs"]
            if epoch > peer.info.epoch and quiet:
                peer.addr = host
                self._drop_peer(peer_id, reason="restarted")
                # fall through to redial the new incarnation
            else:
                return
        elif peer is not None:
            peer.addr = host  # known but unlinked: refresh for the (re)dial below
        elif len(self.peers) >= _MAX_PEERS:
            # Backstop against a beacon flood (spoofed random ids): once the
            # table is full, ignore ids we've never linked rather than dialing
            # and gossiping an unbounded set.
            return
        # Smaller id dials: exactly one connection per pair, no dial races.
        if self.local.id < peer_id:
            if len(self._dial_tasks) >= _MAX_INFLIGHT_DIALS:
                # Hard-bound the outbound dial fan-out. The _MAX_PEERS backstop above
                # only counts self.peers, which a silent-hello beacon flooder keeps
                # empty, so without this cap every distinct spoofed id would spawn a
                # dial without limit (fd/Task exhaustion → node-disabling DoS). The
                # per-dial hello timeout below frees these slots for real peers.
                return
            task = asyncio.get_running_loop().create_task(
                self._dial(peer_id, host, tcp_port), name=f"mesh-dial-{peer_id[:6]}"
            )
            self._dial_tasks.add(task)
            task.add_done_callback(self._dial_tasks.discard)

    def _send_hello(self, writer: asyncio.StreamWriter) -> None:
        """Send our hello carrying a fresh trust-challenge nonce, remembering the
        nonce so the peer's later `auth` (a signature over it) can be checked."""
        nonce = secrets.token_hex(16)
        self._issued_nonce[writer] = nonce
        writer.write(protocol.encode(
            protocol.hello(self.info, self.overrides.to_dict(), config.secret(), nonce)))

    async def _dial(self, peer_id: str, host: str, port: int) -> None:
        peer = self.peers.get(peer_id)
        if (peer is not None and peer.linked) or peer_id in self._dialing:
            return
        # Held for the whole life of a dial-originated link: while this
        # coroutine runs (handshake AND pump), repeat beacons must not redial.
        self._dialing.add(peer_id)
        try:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port, limit=protocol.MAX_LINE_BYTES),
                    timeout=5.0,
                )
            except (OSError, asyncio.TimeoutError, OverflowError):
                return  # next beacon retries (OverflowError: an out-of-range port —
                # _on_beacon already screens these, this is defense-in-depth)
            self._send_hello(writer)
            try:
                await writer.drain()
            except (ConnectionError, OSError):
                self._issued_nonce.pop(writer, None)
                writer.close()
                return
            # Dialed link: we reached whoever answered a beacon (spoofable), so
            # nothing is trusted until their first message is a valid hello.
            await self._run_link(reader, writer, host, authenticated=False)
        finally:
            self._dialing.discard(peer_id)

    # MARK: - redial from memory (survives a dead beacon channel)

    def _remember_peer(self, peer_id: str, addr: str, tcp_port: int) -> None:
        """Persist a peer's last-known dialable address, learned from an
        authenticated hello on its own link (never from a spoofable beacon), so
        redial-from-memory survives both a link drop and a node restart. Written
        only on change — a steady link costs no I/O.

        Bounded like the peer table (_MAX_PEER_CACHE): the cache is never reaped in
        lockstep with self.peers (a reaped-but-known peer is exactly what we still
        want to redial), so a churn of distinct authenticated ids would otherwise grow
        peers.json — and the redial fan-out that iterates it — without bound. The cache
        keeps the most-recently-contacted addresses (insertion order = recency, refreshed
        on each hello) and evicts the coldest entry past the bound; it is a best-effort
        accelerator (see peercache.py), so dropping a cold entry only costs a fallback to
        a beacon-triggered dial."""
        if not addr or tcp_port <= 0:
            return
        entry = (addr, tcp_port)
        if self._peer_cache.get(peer_id) == entry:
            # Unchanged address on a fresh hello: refresh recency in memory only (this
            # peer is live, and there is no on-disk change, so no I/O), so a peer we keep
            # hearing from is never the eviction victim under a flood of new ids.
            self._peer_cache[peer_id] = self._peer_cache.pop(peer_id)
            return
        # New or changed address: (re)insert at most-recent and evict the coldest
        # entries until the persisted cache is back within its bound.
        self._peer_cache.pop(peer_id, None)
        self._peer_cache[peer_id] = entry
        while len(self._peer_cache) > _MAX_PEER_CACHE:
            del self._peer_cache[next(iter(self._peer_cache))]
        peercache.save(self._peer_cache)

    def _redial_targets(self) -> list[tuple[str, str, int]]:
        """Cached peers this node should be dialing right now: an address is
        remembered, the id is ours to dial (smaller id dials, 02-discovery), and
        the peer is neither linked nor already mid-dial."""
        out = []
        for peer_id, (addr, port) in self._peer_cache.items():
            if not self.local.id < peer_id:
                continue
            peer = self.peers.get(peer_id)
            if (peer is not None and peer.linked) or peer_id in self._dialing:
                continue
            out.append((peer_id, addr, port))
        return out

    async def _redial_loop(self) -> None:
        """Dial known-but-unlinked peers from the last-known-address cache.

        Beacons are the normal (re)dial trigger, but they ride multicast/
        broadcast, which can die under a live mesh while unicast still works
        (AP multicast filtering; an OS privacy gate such as macOS 15's Local
        Network permission). Without this loop, a node that loses a link during
        such an outage never gets it back — nothing re-triggers the dial. The
        dial rule is unchanged (only the smaller id dials) and ``_dial``'s
        hello fence still authenticates whoever answers, so a stale or poisoned
        cache entry costs one failed dial per interval, nothing more."""
        interval = float(self.proto.get("redialIntervalSecs", 10.0))
        while True:
            await asyncio.sleep(interval)
            for peer_id, addr, port in self._redial_targets():
                task = asyncio.get_running_loop().create_task(
                    self._dial(peer_id, addr, port),
                    name=f"mesh-redial-{peer_id[:6]}",
                )
                self._dial_tasks.add(task)
                task.add_done_callback(self._dial_tasks.discard)

    # MARK: - Tor transport (WAN reachability over onion services)

    async def _tor_serve(self) -> None:
        """Bring our onion service up in the background, then advertise it. The node
        is fully usable on the LAN while Tor bootstraps (which can take tens of
        seconds). Once the onion is live, gossip our new advert so currently-linked
        peers record where to reach us over Tor after we part ways on the LAN."""
        if self.tor is None:
            return
        if await self.tor.start(self._on_tor_inbound,
                                bootstrap_timeout=config.tor_bootstrap_timeout()):
            self._bump_and_gossip()  # our advert now carries an `onion`

    async def _on_tor_inbound(self, reader: asyncio.StreamReader,
                              writer: asyncio.StreamWriter) -> None:
        """A connection arriving over our onion service (via the Tor forward
        listener). Tag it ``tor`` before handing it to the normal accept path, so
        the link is known to be over Tor even though it lands on loopback."""
        self._link_transport[writer] = "tor"
        try:
            await self._on_tcp_connection(reader, writer)
        finally:
            # _run_link pops this on the peer-link path; the finally covers the
            # accept paths that close the writer BEFORE _run_link (a bad/absent first
            # line, a secret mismatch, a ctl session) so a tor-tagged writer never
            # leaks the map. pop is idempotent, so the double-pop is harmless.
            self._link_transport.pop(writer, None)

    def _remember_onion(self, peer_id: str, onion: str, fingerprint: str) -> None:
        """Persist a peer's permanent onion (from its SIGNED advert). Written on
        change only; the WAN sibling of :meth:`_remember_peer`, bounded the same way
        so a churn of ids can't grow onions.json without limit."""
        onion = tor.normalize_onion(onion)
        if not onion:
            return
        prev = self._onion_cache.get(peer_id)
        if prev is not None and prev.onion == onion and prev.fingerprint == fingerprint:
            # Unchanged content on a fresh hello: refresh recency in memory only (no
            # on-disk change, so no I/O), so a peer we keep hearing from is never the
            # eviction victim under a flood of new ids — parity with _remember_peer.
            self._onion_cache[peer_id] = self._onion_cache.pop(peer_id)
            return
        self._onion_cache.pop(peer_id, None)
        self._onion_cache[peer_id] = onioncache.OnionEntry(
            onion=onion, fingerprint=fingerprint)
        while len(self._onion_cache) > _MAX_PEER_CACHE:
            # Evict a FOREIGN entry first (the oldest such), so a churn of foreign
            # onions — a single linked foreign peer can advertise many distinct signed
            # adverts — can't push out the onions of the PERSONAL peers we actually
            # redial (an isolation DoS on our WAN reconnect). Oldest overall only when
            # every entry is personal.
            evicted = next((pid for pid, e in self._onion_cache.items()
                            if not self._onion_is_personal(pid, e)), None)
            if evicted is None:
                evicted = next(iter(self._onion_cache))
            del self._onion_cache[evicted]
            # An evicted peer is no longer a Tor redial target, so drop its backoff
            # too — otherwise _tor_backoff accretes orphaned entries under id churn
            # (the unbounded-growth class the caches are already bounded against).
            self._tor_backoff.pop(evicted, None)
        onioncache.save(self._onion_cache)

    def _onion_is_personal(self, peer_id: str, entry: onioncache.OnionEntry) -> bool:
        """Whether an onion-cache entry belongs to a peer we trust as PERSONAL — the
        gate for two Tor decisions a linked FOREIGN peer must not get leverage over:
        (1) ORIGINATING an auto-dial — else a foreign peer that advertised an
        arbitrary, attacker-chosen onion turns our node into a Tor-dial reflector
        (and leaks our signed hello to a destination it picked); and (2) surviving
        cache eviction — else a churn of foreign onions evicts the onions of the
        personal peers we do redial. Prefer the live peer's verified classification;
        fall back to the fingerprint the onion was signed-paired with, judged against
        the ban list then the allowlist / default-trust (so full-altruism, default
        ``personal``, keeps redialing everyone — the operator's explicit choice)."""
        peer = self.peers.get(peer_id)
        if peer is not None and peer.verified_fp is not None:
            return self._peer_trust(peer) == "personal"
        fp = entry.fingerprint or ""
        if banned.is_banned(self._banned, fp, peer_id):
            return False
        return trust.classify(fp, self._trusted, self._default_trust) == "personal"

    def _tor_reset_backoff(self, peer_id: str) -> None:
        """A Tor link to this peer actually BOUND — clear the backoff so a reachable
        peer that flaps reconnects promptly instead of waiting out a grown interval.
        Fired on link establishment (see _learn_node), not on a bare SOCKS answer."""
        self._tor_backoff.pop(peer_id, None)

    def _tor_grow_backoff(self, peer_id: str) -> None:
        """Schedule the peer's next Tor probe further out, doubling the interval up to
        the ceiling. Called before each dial attempt (a link binding then clears it),
        so an onion that never links is probed geometrically less often."""
        b = self._tor_backoff.get(peer_id) or _TorBackoff()
        b.next_attempt = time.monotonic() + b.interval
        b.interval = min(b.interval * _TOR_BACKOFF_FACTOR, _TOR_BACKOFF_MAX_SECS)
        self._tor_backoff[peer_id] = b

    def _tor_redial_targets(self, now: float) -> list[tuple[str, str]]:
        """Known peers to probe over Tor right now: we hold an onion for them, we
        trust them as PERSONAL (we never auto-dial a foreign onion — see
        _onion_is_personal), our id sorts below theirs (the same smaller-id-dials rule
        as the LAN, so exactly one side dials), they are neither linked nor already
        being dialed, and their backoff is due. A peer linked over EITHER transport is
        skipped — that is the whole of "no aggressive switching": a live link is never
        disturbed."""
        out: list[tuple[str, str]] = []
        for peer_id, entry in self._onion_cache.items():
            onion = tor.normalize_onion(entry.onion)
            if not onion or not self.local.id < peer_id:
                continue
            # Only ORIGINATE a Tor dial to a peer we trust as PERSONAL. Auto-dialing a
            # foreign, attacker-advertised onion would let a linked foreign peer aim
            # our node at arbitrary (third-party) onions — a dial reflector + a leak of
            # our signed hello. Foreign peers reach us INBOUND; we don't chase them
            # over the WAN. The manual `tor-connect` paste (peer_id=None in _tor_dial)
            # still bypasses this for a deliberate one-shot introduction.
            if not self._onion_is_personal(peer_id, entry):
                continue
            peer = self.peers.get(peer_id)
            if (peer is not None and peer.linked) or onion in self._tor_dialing:
                continue
            b = self._tor_backoff.get(peer_id)
            if b is not None and now < b.next_attempt:
                continue
            out.append((peer_id, onion))
        return out

    async def _tor_redial_loop(self) -> None:
        """Probe known-but-unseen peers over Tor with per-peer exponential backoff.
        A no-op until the onion service is up (bootstrap runs in ``_tor_serve``),
        and it never touches a peer that already has a live link."""
        while True:
            await asyncio.sleep(_TOR_REDIAL_TICK_SECS)
            if self.tor is None or self.tor.onion_address() is None:
                continue
            now = time.monotonic()
            for peer_id, onion in self._tor_redial_targets(now):
                task = asyncio.get_running_loop().create_task(
                    self._tor_dial(onion, peer_id=peer_id),
                    name=f"mesh-tor-dial-{peer_id[:6]}")
                self._dial_tasks.add(task)
                task.add_done_callback(self._dial_tasks.discard)

    async def _tor_dial(self, onion: str, peer_id: str | None = None) -> None:
        """Open a Tor link to ``onion`` and run it EXACTLY like a LAN-dialed link
        (same hello/auth/trust handshake, same message pump). ``peer_id`` (when
        known) dedups against an existing link and drives the reachability backoff;
        a manual paste passes None and dials unconditionally — reaching a peer you
        may never have met on the LAN."""
        if self.tor is None:
            return
        onion = tor.normalize_onion(onion)
        if not onion:
            return
        peer = self.peers.get(peer_id) if peer_id else None
        if (peer is not None and peer.linked) or onion in self._tor_dialing:
            return
        self._tor_dialing.add(onion)
        # Pre-schedule the NEXT probe before dialing: assume this attempt is a miss and
        # let a genuine Tor link BINDING clear it (in _learn_node). Resetting the
        # backoff on the bare SOCKS answer instead would let an onion that answers TCP
        # but never links — a rotated join secret, a squatted/reassigned onion, an
        # answer-then-drop — defeat the backoff entirely and thrash a fresh (expensive)
        # circuit every tick forever. A manual paste (no peer_id) is unthrottled.
        if peer_id:
            self._tor_grow_backoff(peer_id)
        try:
            try:
                reader, writer = await asyncio.wait_for(
                    self.tor.dial(onion), timeout=_TOR_DIAL_TIMEOUT_SECS)
            except (OSError, ValueError, RuntimeError, asyncio.TimeoutError,
                    asyncio.IncompleteReadError):
                # Onion unreachable (down, descriptor not published, tor busy) — the
                # next probe is already scheduled above, so just let the loop retry.
                # A manual paste (no peer_id) simply reports nothing; the operator can
                # re-issue it. IncompleteReadError (an EOFError subclass, NOT an
                # OSError) is the SOCKS peer closing mid-handshake — a reachability
                # failure like any other, not a crash the dial task should escape with.
                return
            self._link_transport[writer] = "tor"  # this link runs over Tor
            self._send_hello(writer)
            try:
                await writer.drain()
            except (ConnectionError, OSError):
                self._issued_nonce.pop(writer, None)
                self._link_transport.pop(writer, None)  # never reached _run_link
                writer.close()
                return
            # A Tor-dialed link is talking to whoever answered the onion, so — as on
            # a LAN dial — nothing is trusted until the first message is a valid
            # hello. ``host`` is the onion: it shows in the snapshot as the peer's
            # address but is kept OUT of the LAN redial cache (see _learn_node).
            await self._run_link(reader, writer, onion, authenticated=False)
        finally:
            self._tor_dialing.discard(onion)

    # MARK: - TCP links + control sessions

    async def _on_tcp_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        host = (writer.get_extra_info("peername") or ("?",))[0]
        try:
            first = protocol.decode(await asyncio.wait_for(reader.readline(), timeout=10.0))
        except (asyncio.TimeoutError, ConnectionError, OSError, asyncio.LimitOverrunError):
            writer.close()
            return
        if not first:
            writer.close()
            return
        # The join fence: with DIPLOMAT_MESH_SECRET set, an opener (peer OR control
        # client) that doesn't present the token gets silently dropped.
        if first.get("t") in ("ctl", "hello") and not hmac.compare_digest(
                _utf8(str(first.get("secret", ""))), _utf8(config.secret())):
            writer.close()
            return
        if first.get("t") == "ctl":
            # `ctl` is the operator's LOCAL control channel (status, dispatch,
            # set-attr, trust/ban, set-default-trust, tor-connect, stop). It is driven
            # over loopback by the operator's own CLI/panel and MUST NOT be reachable
            # over the Tor onion: the onion is advertised to every mesh peer (and can
            # be pasted around), so serving ctl over it would expose the full
            # node-control surface to anyone holding the onion — and in an OPEN mesh
            # (no join secret, the documented home-LAN default) with no authentication
            # at all. Peer LINKS (`hello`) legitimately arrive over Tor; control
            # sessions do not. An inbound Tor connection is tagged `tor` by
            # _on_tor_inbound BEFORE this runs, so refuse ctl on it outright. See
            # docs/szpontnet/14#security-notes.
            if self._link_transport.get(writer) == "tor":
                writer.close()
                return
            # A server configured with an API key requires it on the opening ctl,
            # on top of the join secret: the secret admits mesh members, the key
            # authenticates who may drive/submit work to this node.
            if config.api_key() and not hmac.compare_digest(
                    _utf8(str(first.get("apiKey", ""))), _utf8(config.api_key())):
                writer.close()
                return
            await self._run_ctl(reader, writer)
            return
        if first.get("t") == "hello":
            # Answer with our own hello (+ challenge nonce), then treat like any
            # link. Our hello goes first so our nonce is in flight before we
            # process theirs and answer their challenge.
            self._send_hello(writer)
            with contextlib.suppress(ConnectionError, OSError):
                await writer.drain()
            # Process the opening hello INSIDE _run_link's try/finally (not here): a
            # malformed field that raises — e.g. a lone-surrogate `nonce`, whose
            # _auth_challenge nonce.encode() raises UnicodeEncodeError (a ValueError
            # subclass) — must not escape this asyncio callback, or the finally that
            # pops _issued_nonce[writer] and closes the link never runs, orphaning the
            # issued nonce (unbounded remote memory leak) and the transport.
            await self._run_link(reader, writer, host, authenticated=True, first=first)
            return
        writer.close()

    async def _run_link(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: str,
        authenticated: bool, first: dict | None = None,
    ) -> None:
        """Pump one peer link until EOF/error.

        ``authenticated`` is the join fence: an *inbound* link had its opening
        hello secret-checked in ``_on_tcp_connection`` (True), but an *outbound*
        dialed link (False) is talking to whoever answered a beacon — which the
        attacker can spoof. So an unauthenticated link accepts *nothing* until
        its first message is a valid hello with the matching secret; any other
        first message (a naked ``dispatch``/``set-attr``/``overrides``) tears
        the link down. Without this gate, a spoofed beacon that makes us dial an
        attacker would let unauthenticated dispatches spawn agents on this box.
        """
        peer_id: str | None = None
        # A CUMULATIVE deadline for the pre-hello phase (consumed in the loop): set once,
        # never re-armed per read, so a trickle-slowloris cannot keep resetting it.
        hello_deadline = time.monotonic() + _LINK_HELLO_TIMEOUT_SECS
        try:
            if first is not None:
                # The opening inbound hello, already decoded + secret-checked by
                # _on_tcp_connection; handled here so a raise (e.g. a surrogate nonce)
                # is caught below and the finally cleans up (mirrors the loop).
                got = self._on_message(first, host, writer)
                if got and peer_id is None:
                    peer_id = got
            while True:
                # A link is reclaimed by the heartbeat reaper only once its writer is
                # bound to a self.peers entry. Until then nothing else will ever reap it,
                # so bound the whole pre-hello phase by the CUMULATIVE deadline above. This
                # covers every link with no reapable peer:
                #  - an OUTBOUND leg still awaiting the hello that will bind a peer;
                #  - an INBOUND hello that binds NONE — its id is ours (_on_message
                #    short-circuits at info.id == self.local.id) or the peer table is full
                #    (_learn_node refuses the Peer, yet _on_message still returns the id, so
                #    peer_id alone is NOT a safe signal — the writer is what stays unbound).
                # The deadline is CUMULATIVE, not per-read: a per-read timeout is reset by
                # every line that arrives, so a trickle-slowloris feeding one decode-rejected
                # /no-op line every < timeout would never trip it, never bind, and never be
                # reaped — pinning the fd + Task + _issued_nonce entry FOREVER (an unbounded,
                # node-disabling leak bypassing the join secret; inbound has no fan-in cap).
                if self._peer_by_writer(writer) is None:
                    remaining = hello_deadline - time.monotonic()
                    if remaining <= 0:
                        break  # never bound a peer in time — reap (the finally cleans up)
                    line = await asyncio.wait_for(reader.readline(), timeout=remaining)
                else:
                    line = await reader.readline()
                if not line:
                    break
                msg = protocol.decode(line)
                if not msg:
                    continue
                if not authenticated:
                    if msg.get("t") != "hello":
                        raise ValueError("first link message was not a hello")
                    authenticated = True  # _on_message re-checks the secret
                try:
                    got = self._on_message(msg, host, writer)
                except (ConnectionError, OSError):
                    raise  # a real socket failure in a handler → tear the link down
                except Exception as exc:  # noqa: BLE001
                    # A malformed/hostile message MUST NOT wedge or drop the link
                    # (conformance rule 10 / docs/szpontnet/09). Handlers normalize
                    # their own input, but this makes the "never crash on a peer's
                    # message" invariant structural rather than per-handler: any
                    # unexpected KeyError/TypeError/OverflowError/ValueError from one
                    # message is logged and that message dropped, the link kept.
                    activity.log("mesh", "mesh-msg-error",
                                 f"Mesh: dropped a message raising {exc!r}; link kept")
                    continue
                if got and peer_id is None:
                    peer_id = got
        except (ConnectionError, OSError, asyncio.TimeoutError,
                asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            self._issued_nonce.pop(writer, None)
            self._link_transport.pop(writer, None)
            writer.close()
            # Only tear down the peer if THIS writer is still its live link
            # (a reconnect may already have replaced it).
            for pid, p in list(self.peers.items()):
                if p.writer is writer:
                    self._drop_peer(pid, reason="link lost")

    def _on_message(
        self, msg: dict, host: str, writer: asyncio.StreamWriter
    ) -> str | None:
        """Handle one link message; returns the peer id it bound to (if any)."""
        t = msg.get("t")
        # Any message from a bound peer is proof of life — refresh liveness so a
        # link busy with gossip/dispatch stays `up` even if a heartbeat is missed.
        bound = self._peer_by_writer(writer)
        if bound is not None:
            bound.last_seen = time.monotonic()
        if t == "hello" and not hmac.compare_digest(
                _utf8(str(msg.get("secret", ""))), _utf8(config.secret())):
            # A dialed "peer" that can't present the join token isn't one of
            # ours — tear the link down (ValueError ends _run_link's pump). Constant-time
            # (and surrogate-safe) like the accept-path fence: the sole secret check on the
            # OUTBOUND-dial path, and now meaningful over Tor where the token isn't on-wire.
            raise ValueError("mesh secret mismatch")
        if t in ("hello", "node"):
            raw = msg.get("node")
            if not isinstance(raw, dict):
                return None
            # Authenticate the advertisement END TO END: a relay may forward it, but
            # only the holder of the advert's key could have produced its signature,
            # so a forged or tampered keyed advert is dropped (never adopted). A
            # keyless advert has nothing to verify — accepted, but stays unverified
            # (foreign under any allowlist), exactly as before.
            if not self._advert_authentic(raw):
                return None
            info = NodeInfo.from_dict(raw)
            if info is None or info.id == self.local.id:
                return None
            self._learn_node(info, host, writer if t == "hello" else None, raw=raw)
            if t == "hello":
                self._merge_overrides(msg.get("overrides"))
                self._answer_challenge(msg, writer)  # prove we hold our own key
            return info.id
        if t == "auth":
            self._verify_auth(msg, writer)
            peer = self._peer_by_writer(writer)
            return peer.info.id if peer else None
        if t == "heartbeat":
            peer = self._peer_by_writer(writer)
            if peer:
                peer.last_seen = time.monotonic()
                return peer.info.id
            return None
        if t == "overrides":
            self._merge_overrides(msg.get("overrides"))
            peer = self._peer_by_writer(writer)
            return peer.info.id if peer else None
        if t == "set-attr":
            # A set-attr MUTATES advertised identity/attrs (tier/tokens/duties/
            # plan/quota) — reshaping placement and load balancing mesh-wide, and
            # it is even FORWARDED to a named peer. That is strictly more powerful
            # than a dispatch, which is already trust-gated. So classify the sender
            # from its VERIFIED link (never the message) and act only for a
            # personal device; a foreign one is ignored. A control-session set-attr
            # (the local operator, already secret-fenced) calls _on_set_attr
            # directly and is unaffected. Empty allowlist = full trust, so an
            # unconfigured mesh behaves exactly as before.
            if self._peer_trust(self._peer_by_writer(writer)) == "personal":
                self._on_set_attr(msg)
            else:
                activity.log("mesh", "warn",
                             "Mesh: ignored set-attr from a foreign device")
            return None
        if t == "dispatch":
            job = Job.from_dict(msg.get("job") or {})
            if job is None:
                return None
            if not self._api_key_ok(msg):
                # A server with an API key configured refuses a request that
                # doesn't present it — reported as a decline so the dispatcher
                # fails the slot over exactly like any other refusal.
                with contextlib.suppress(ConnectionError, OSError):
                    writer.write(protocol.encode(protocol.job_status(
                        job.id, "declined", "invalid or missing API key",
                        self.local.id)))
                return None
            self._take_job(job, writer)
            return None
        if t == "job-status":
            self._resolve_job_future(msg, writer)
            return None
        if t == "job-result":
            # A foreign executor returned the artifact we dispatched to it. Ack it and
            # perform the social action ourselves — the executor never acts as us.
            self._on_job_result(msg, writer)
            peer = self._peer_by_writer(writer)
            return peer.info.id if peer else None
        if t == "job-ack":
            # Our foreign requester acknowledged a result — stop re-sending it.
            self._on_job_ack(msg, writer)
            peer = self._peer_by_writer(writer)
            return peer.info.id if peer else None
        if t == "job-reminder":
            # Our foreign requester asks "is this ready?" — answer truthfully with
            # the result (reviving its delivery) or a progress note, or be banned.
            self._on_job_reminder(msg, writer)
            peer = self._peer_by_writer(writer)
            return peer.info.id if peer else None
        if t == "job-progress":
            # A late foreign executor pleads "still working" — hand the plea to the
            # extension decision (an agent's call, never the executor's).
            self._on_job_progress(msg, writer)
            peer = self._peer_by_writer(writer)
            return peer.info.id if peer else None
        if t == "work-claim":
            # A gossiped origination lease. Authenticate it, merge by freshness,
            # relay it, and yield our own claim if a better peer now owns the key.
            self._on_work_claim(msg)
            peer = self._peer_by_writer(writer)
            return peer.info.id if peer else None
        return None

    def _peer_by_writer(self, writer: asyncio.StreamWriter) -> Peer | None:
        return next((p for p in self.peers.values() if p.writer is writer), None)

    def _api_key_ok(self, msg: dict) -> bool:
        """True unless this node has an API key configured and the message fails
        to present a matching ``apiKey`` (the server request-authentication gate)."""
        key = config.api_key()
        return not key or hmac.compare_digest(
            _utf8(str(msg.get("apiKey", ""))), _utf8(key))

    # MARK: - gossip authentication (self-signed adverts + overrides)

    def _advert_authentic(self, raw: dict) -> bool:
        """Whether a received advertisement is authentic. A **keyed** advert (it
        carries a `pubkey`) MUST carry a `sig` that verifies against that `pubkey`
        over the advert's canonical bytes — otherwise it is a forgery or was
        tampered with in relay, and is dropped. A **keyless** advert (no `pubkey`)
        has nothing to verify: it is accepted but can never be verified, so it stays
        foreign under any allowlist — identical to the pre-signing degradation."""
        pubkey = str(raw.get("pubkey", ""))
        if not pubkey:
            return True
        sig = str(raw.get("sig", ""))
        return bool(sig) and crypto.verify(
            pubkey, protocol.advert_signing_bytes(raw), sig)

    def _pinned_pubkey(self, node_id: str) -> str:
        """The advertised pubkey we currently hold for ``node_id`` (our own, or a
        known peer's), or ``""`` if unknown/keyless. Used to verify an `overrides`
        signature against the editor's key."""
        if node_id == self.local.id:
            return self.key.public_b64 if self.key else ""
        peer = self.peers.get(node_id)
        return peer.info.pubkey if peer else ""

    # MARK: - trust handshake (proof of possession)

    def _answer_challenge(self, msg: dict, writer: asyncio.StreamWriter) -> None:
        """The peer's hello carried a challenge nonce; sign it with our private
        key so the peer can bind our advertised pubkey to a key we actually hold."""
        nonce = msg.get("nonce")
        if isinstance(nonce, str) and nonce and self.key is not None:
            with contextlib.suppress(ConnectionError, OSError):
                writer.write(protocol.encode(
                    protocol.auth(self.key.sign(_auth_challenge(nonce)))))

    def _verify_auth(self, msg: dict, writer: asyncio.StreamWriter) -> None:
        """The peer answered OUR challenge. If the signature checks out against the
        pubkey it advertised, we now believe it holds that key: record the verified
        fingerprint (what trust keys on). A bad/absent signature leaves the peer
        unverified, so it stays foreign under any configured allowlist."""
        peer = self._peer_by_writer(writer)
        my_nonce = self._issued_nonce.get(writer)
        if peer is None or not my_nonce:
            return
        sig = str(msg.get("sig", ""))
        if crypto.verify(peer.info.pubkey, _auth_challenge(my_nonce), sig):
            fp = crypto.fingerprint_of(peer.info.pubkey)
            if fp and peer.verified_fp != fp:
                peer.verified_fp = fp
                level = trust.classify(fp, self._trusted, self._default_trust)
                activity.log("mesh", "mesh-peer-up",
                             f"Mesh: verified {peer.info.name} device {fp[:16]} ({level})")

    def _peer_trust(self, peer: Peer | None) -> str:
        """personal / foreign / banned for a peer. The ban check runs first: a
        banned device is never anything else. Otherwise the classification comes
        from the VERIFIED fingerprint against the local allowlist, falling back
        to the node's default trust level (ships foreign, so an unlisted or
        unverified peer is untrusted until promoted)."""
        if peer is not None and self._peer_banned(peer):
            return "banned"
        fp = peer.verified_fp if peer else None
        return trust.classify(fp or "", self._trusted, self._default_trust)

    def _peer_banned(self, peer: Peer) -> bool:
        """Whether a peer is on the local ban list. Judged by its VERIFIED
        fingerprint when it proved one; else by the fingerprint of the key it
        merely advertises (safe for a DENY decision — claiming a banned identity
        only ever costs the claimant); else, keyless, by node id (best-effort)."""
        fp = peer.verified_fp or crypto.fingerprint_of(peer.info.pubkey)
        return banned.is_banned(self._banned, fp, peer.info.id)

    def _node_banned(self, node_id: str) -> bool:
        """The dispatch-side ban gate: never pick a banned device as a target."""
        if node_id == self.local.id:
            return False
        peer = self.peers.get(node_id)
        return peer is not None and self._peer_banned(peer)

    def _learn_node(
        self, info: NodeInfo, host: str, link_writer: asyncio.StreamWriter | None,
        raw: dict | None = None,
    ) -> None:
        peer = self.peers.get(info.id)
        if peer is None and len(self.peers) >= _MAX_PEERS:
            # The peer-table bound applies to EVERY path that grows the table, not
            # just the beacon path: a single linked peer relaying a flood of `node`
            # gossip with spoofed ids (or an accepter opening many hellos) would
            # otherwise balloon the table, snapshot, and gossip fan-out unbounded.
            # Past the cap we refuse to learn ids we've never seen.
            return
        # id → key pinning. Once we know a peer's `pubkey`, a fresher **gossiped**
        # advert claiming a DIFFERENT key (even one self-signed by that other key)
        # is a third party trying to hijack the id — reject it. Only the peer's OWN
        # link (a hello, ``link_writer`` set) may re-key. This is what stops a relay
        # from replacing a known node's key (and thus its identity/trust) or
        # downgrading it to keyless.
        if (peer is not None and link_writer is None and peer.info.pubkey
                and info.pubkey != peer.info.pubkey):
            return
        fresh = peer is None or info.newer_than(peer.info)
        # An own-link hello (``link_writer`` set) is the peer ITSELF on its
        # authenticated link, and it REPLACES the peer's writer below regardless of
        # freshness. So a KEY CHANGE it carries must be adopted here regardless of the
        # (epoch, seq) — otherwise two things break when the hello is non-fresh:
        #   (1) a forged GOSSIP advert (link_writer=None) that reached us first with an
        #       inflated ``epoch`` keeps the wrong key pinned forever, and the real
        #       peer can never re-key or re-verify (permanent trust-DoS); and
        #   (2) an attacker opening a link as a verified-personal peer's id with a
        #       lower epoch would take over its writer while the STALE verified_fp is
        #       kept (the clear below is under ``if fresh``), inheriting that peer's
        #       personal trust — a privilege escalation. Adopting the advertised key
        #       (incl. keyless) re-keys the pin and drops the now-void verification, so
        #       the new link must re-prove possession; an unproven/keyless peer lands
        #       foreign. The id→key reject above still blocks the gossip (relay) path.
        if (not fresh and link_writer is not None and peer is not None
                and info.pubkey != peer.info.pubkey):
            fresh = True
        prev_pubkey = "" if peer is None else peer.info.pubkey
        if peer is None:
            peer = Peer(info, host)
            self.peers[info.id] = peer
            activity.log("mesh", "mesh-peer-up",
                         f"Mesh: discovered {info.name} ({info.platform}, tier {info.tier})")
        if fresh:
            # A verified fingerprint is bound to the exact pubkey the peer PROVED it
            # holds. If the peer re-advertises a DIFFERENT pubkey **on its own link**
            # (a fresh hello, ``link_writer`` set), the proof no longer applies —
            # drop the verification so it must re-prove possession of the new key
            # (the accompanying `auth` re-establishes it). We MUST NOT clear it from
            # a THIRD-PARTY gossip relay (``link_writer is None``): an authenticated
            # peer could otherwise relay a spoofed `node` for a personal peer P
            # (bogus pubkey, inflated seq) to force P personal→foreign, and the
            # inflated seq would outrank P's honest gossip and block recovery until P
            # restarts — a persistent trust-DoS. Trust keys on the proven fingerprint,
            # so an advertised-but-unproven pubkey drift is harmless to the decision.
            if (link_writer is not None and peer.verified_fp is not None
                    and crypto.fingerprint_of(info.pubkey) != peer.verified_fp):
                peer.verified_fp = None
            peer.info = info
            # When this node's key first becomes known (or changes), purge any
            # work-claim record under its id that doesn't carry that key. An attacker
            # who reaches us before the real advert can plant a keyless/wrong-key
            # claim `{node: P}` (the id→key pin has nothing to match yet) with a
            # spoofed-high (epoch, seq); it is inert (never authoritative — the
            # ownership binding rejects it), but left in place it would out-fresh P's
            # real signed claim forever and defeat dedup for that key. Purging on the
            # key we now trust closes that cold-join window. See _forget_claims.
            if info.pubkey and info.pubkey != prev_pubkey:
                self._evict_unbound_claims(info.id, info.pubkey)
        peer.addr = host or peer.addr
        # Refresh the liveness clock only from a proof-of-life this node can trust: the
        # peer's OWN direct link (link_writer set), or gossip about a peer we are NOT
        # directly linked to (a gossip-only phantom, whose last_seen is its ONLY liveness
        # signal — consumed by _reapable). For an already-LINKED peer, third-party `node`
        # gossip is NOT proof its link to us is alive: a keyless on-mesh attacker (or even a
        # benign echo) replaying its genuine public advert would otherwise keep a dead-but-
        # half-open peer's clock fresh forever, so _heartbeat_loop's `now - last_seen >
        # timeout` reap — the ONLY reaper for a bound peer, since the Round-15 read-timeout
        # skips bound writers and drain errors are suppressed — never trips, and the dead
        # peer's work-claims stay authoritative for the whole OS TCP window (~minutes)
        # instead of peerTimeoutSecs (~seconds). A live linked peer needs no gossip refresh:
        # its own heartbeats already advance last_seen via _on_message. A gossip-only
        # PHANTOM is refreshed on ANY advert (even a non-fresh relay): its last_seen is its
        # only liveness signal, and an alive-but-idle phantom holds a stable (epoch, seq),
        # so gating the refresh on freshness would wrongly reap it. The residual (a replay
        # keeps a truly-dead phantom in the snapshot) is bounded by _MAX_PEERS and never
        # affects routing (a down peer is already out of the assignment input).
        if link_writer is not None or not peer.linked:
            peer.last_seen = time.monotonic()
            peer.down_since = None
        if link_writer is not None:
            if peer.writer is not link_writer:
                # A DIFFERENT physical link than the one we last bound (a reconnect, a
                # dial-race duplicate, or a hostile takeover). Any verification was
                # proven on the OTHER link and does NOT carry to this one — drop it so
                # this link must answer THIS connection's challenge (its own `auth`)
                # before it is trusted. Without this, a captured, validly-signed advert
                # REPLAYED verbatim on a fresh link (SAME key, so non-fresh and the
                # fresh-force above never fires) would inherit the peer's verified
                # fingerprint and its personal trust with no private key — a full trust
                # hijack (run-on-host / mesh-wide set-attr). Legitimate reconnects
                # re-prove in one round-trip; the id→key pin still blocks the relay path.
                peer.verified_fp = None
                if peer.writer is not None:
                    # Duplicate/zombie old link: keep the new one, close the old quietly.
                    with contextlib.suppress(Exception):
                        peer.writer.close()
            if peer.linked_since is None:
                peer.linked_since = time.monotonic()  # link came up: start the uptime clock
            peer.writer = link_writer
            peer.transport = self._link_transport.get(link_writer, "lan")
            if peer.transport == "tor":
                # A real Tor link BOUND (not merely a SOCKS answer) is the true "this
                # onion is usable" signal — clear the reconnect backoff so a reachable
                # peer that flaps reconnects promptly. An onion that answers but never
                # gets here (secret rotated, address squatted) keeps its pre-scheduled
                # backoff and is throttled, exactly as intended. See _tor_dial.
                self._tor_reset_backoff(info.id)
            # A hello on the peer's OWN link (a direct LAN link or a manual paste) is
            # the one authenticated source of a peer's permanent onion — persist it so
            # we can redial over Tor after this link drops. (Deliberately NOT from
            # third-party gossip: onions are remembered only for peers we've actually
            # met, matching the LAN "first sight" model.)
            if info.onion:
                self._remember_onion(info.id, info.onion,
                                     crypto.fingerprint_of(info.pubkey))
            # A hello on a LAN link is also the one authenticated source of a dialable
            # LAN address. A link over TOR must NOT feed the LAN redial cache: that
            # cache dials host:tcpPort directly, and a Tor link's endpoint is either
            # an .onion (outbound) or loopback (inbound) — neither is redialable that
            # way. Gate on the tracked transport, which is right for BOTH directions.
            if peer.transport == "lan":
                self._remember_peer(info.id, host, info.tcp_port)
            self._bump_and_gossip()  # our `sees` changed
        if fresh:
            # Relay a genuinely-newer advertisement learned via GOSSIP onward, so a
            # NodeInfo update converges across a multi-hop topology (A—B—C where A
            # and C don't link directly), not just a full mesh. Mirrors the
            # overrides relay. The freshness gate on every receiver stops the echo
            # from looping. A hello-learned peer is directly connected, so its info
            # is broadcast by the normal channels — only gossip needs the relay.
            if link_writer is None:
                # Relay the advert VERBATIM (the exact received dict) so its
                # signature — which covers that dict's canonical bytes — survives
                # the hop. Re-serializing from the parsed NodeInfo would drop any
                # field this node doesn't know and break the signature downstream.
                self._broadcast(protocol.node_update_raw(raw) if raw is not None
                                else protocol.node_update(info))
            self._recompute("gossip")

    def _drop_peer(self, peer_id: str, reason: str) -> None:
        peer = self.peers.get(peer_id)
        if peer is None:
            return
        self._close_link(peer)
        if peer.down_since is None:
            peer.down_since = time.monotonic()
            activity.log("mesh", "mesh-peer-down",
                         f"Mesh: lost {peer.info.name} ({reason})")
        self._bump_and_gossip()
        self._recompute(f"peer down: {peer.info.name}")

    def _close_link(self, peer: Peer) -> None:
        if peer.writer is not None:
            with contextlib.suppress(Exception):
                peer.writer.close()
            peer.writer = None
        peer.linked_since = None  # link gone: uptime resets for the next connection

    # MARK: - heartbeats + liveness

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.proto["heartbeatIntervalSecs"])
            beat = protocol.encode(protocol.heartbeat())
            timeout = self.proto["peerTimeoutSecs"]
            now = time.monotonic()
            for pid, peer in list(self.peers.items()):
                if peer.linked:
                    peer.writer.write(beat)
                    # Bound the drain: a peer that stops READING (a full/zero-window
                    # TCP buffer, alive but wedged) would otherwise block this loop
                    # and defer liveness detection for EVERY other peer. Cap the wait
                    # at one interval; the timeout check below then reaps it anyway.
                    with contextlib.suppress(ConnectionError, OSError,
                                             asyncio.TimeoutError):
                        await asyncio.wait_for(
                            peer.writer.drain(),
                            timeout=self.proto["heartbeatIntervalSecs"])
                    if now - peer.last_seen > timeout:
                        self._drop_peer(pid, reason="heartbeat timeout")
                elif self._reapable(peer, now):
                    del self.peers[pid]  # long dead / stale phantom — drop it
                    self._forget_claims(pid)  # its origination leases lapse with it
            # Reliable foreign-result delivery + expiry ride the same tick: re-send
            # any unacked job-result whose retry is due, hold late foreign
            # executors to their acceptance (reminder → extension or ban), and
            # reap stale bookkeeping.
            self._retry_pending_results()
            self._check_foreign_deadlines()
            self._reap_foreign()
            self._reap_released_claims(time.monotonic())

    def _reapable(self, peer: "Peer", now: float) -> bool:
        """Whether an *unlinked* peer should be dropped from the snapshot: a peer
        that went down past the retention window, OR a gossip-only phantom (learned
        via multi-hop `node` relay, never linked, so ``down_since`` was never
        stamped) whose last gossip is that old. Without the phantom case such a
        peer would linger forever as a zombie if it stopped being gossiped.
        (``now`` is monotonic, matching ``last_seen``/``down_since``.)"""
        ref = peer.down_since if peer.down_since is not None else peer.last_seen
        return now - ref > _DOWN_RETENTION_SECS

    # MARK: - gossip

    def _bump_and_gossip(self) -> None:
        """Our own info changed (attrs or link set): bump seq, tell every peer."""
        self._seq += 1
        self._broadcast(protocol.node_update(self.info))

    def _broadcast(self, msg: dict) -> None:
        payload = protocol.encode(msg)
        for peer in self.peers.values():
            if peer.linked:
                with contextlib.suppress(ConnectionError, OSError):
                    peer.writer.write(payload)

    def _overrides_authentic(self, raw: dict) -> bool:
        """Whether a gossiped placement-override is authentic. The default (empty,
        ``rev`` 0) override needs no signature. Any real (``rev > 0``) edit MUST be
        signed by its ``updatedBy`` editor and verify against that editor's pinned
        key — so a relay can neither forge an edit (even under an *unknown* id, which
        would otherwise let a huge forged ``rev`` permanently mask real edits) nor
        tamper with a real one. An edit whose editor we don't know a key for is
        **rejected** as unauthenticatable; it re-propagates and is adopted once we
        learn that editor's signed advertisement. The one exception is a node
        without a crypto library at all: it can verify nothing, so it stays in the
        legacy accept-everything mode (it is itself keyless → foreign to everyone)."""
        # Coerce rev tolerantly: a gossiped override with a malformed rev (null, a
        # list, a non-numeric string) defaults to 0 — the default (unsigned) override
        # — rather than raising an uncaught TypeError that would tear the link (and,
        # on the first-hello path, leak the issued nonce). Mirrors from_dict.
        if PlacementOverrides._as_rev(raw.get("rev", 0)) <= 0:
            # rev 0 is the unsigned DEFAULT (empty) override — it needs no signature.
            # But a REAL edit bumps rev >= 1 (with_duty) and MUST be signed, so a rev-0
            # override carrying actual duties is a forgery trying to skip the signature
            # scheme entirely: on the open (secret-less) mesh any foreign peer could
            # push arbitrary mesh-wide placement that way (it still win_overs the
            # default via the updatedBy tie-break). Accept rev 0 ONLY when it is empty.
            return not raw.get("duties")
        if not crypto.AVAILABLE:
            return True  # no crypto here — can't verify anything (keyless legacy node)
        editor = str(raw.get("updatedBy", ""))
        pin = self._pinned_pubkey(editor)
        if not pin:
            return False  # a real edit from an unknown/keyless editor is unauthenticatable
        sig = str(raw.get("sig", ""))
        return bool(sig) and crypto.verify(
            pin, protocol.overrides_signing_bytes(raw), sig)

    def _merge_overrides(self, raw: object) -> None:
        if not isinstance(raw, dict):
            return
        if not self._overrides_authentic(raw):
            return  # forged/tampered mesh-wide placement edit — drop it
        incoming = PlacementOverrides.from_dict(raw)
        if incoming.wins_over(self.overrides):
            self.overrides = incoming
            # Relay verbatim so the editor's signature survives the hop.
            self._broadcast(protocol.overrides_update(self.overrides.to_dict()))
            self._recompute("overrides")

    def set_overrides_duty(self, duty_id: str, placement_dict: dict) -> None:
        """A local edit (panel/CLI): bump the LWW rev, sign it, and gossip."""
        placement = config.Placement.from_dict(placement_dict)
        ov = self.overrides.with_duty(duty_id, placement, by=self.local.id)
        if self.key is not None:
            ov = ov.signed(self.key.sign(protocol.overrides_signing_bytes(ov.to_dict())))
        self.overrides = ov
        self._broadcast(protocol.overrides_update(self.overrides.to_dict()))
        self._recompute("overrides edited")

    def apply_local_attrs(self, attrs: dict) -> None:
        changed = False
        new = identity.apply_attrs(self.local, attrs)
        if new != self.local:
            self.local = new
            identity.save(new)
            changed = True
        if stats.touches_stats(attrs):  # plan / quotaLeft / usageAvg / usage edits
            self.stats = stats.apply_stat_attrs(self.stats, attrs)
            stats.save(self.stats)
            changed = True
        if changed:
            self._bump_and_gossip()
            self._recompute("attrs")

    def _on_set_attr(self, msg: dict) -> None:
        target = str(msg.get("target", ""))
        attrs = msg.get("attrs")
        if not isinstance(attrs, dict):
            return
        if target in ("", "self", self.local.id):
            self.apply_local_attrs(attrs)
            return
        peer = self.peers.get(target)  # forward: the panel edits any node from here
        if peer and peer.linked:
            with contextlib.suppress(ConnectionError, OSError):
                peer.writer.write(protocol.encode(protocol.set_attr(target, attrs)))

    # MARK: - work claims (leaderless origination leases)

    def claim(self, work_key: str) -> bool:
        """Try to become the originator for ``work_key``. Returns True if this node
        should proceed (it already owns the key, or has now taken/announced it),
        False if a **better** (lower-id) live, trusted peer already owns it — in
        which case the caller MUST NOT originate.

        This is the leaderless dedup: there is no query round. We consult the
        current owner (a pure function of the gossiped claim book + live set), and
        either announce our own active claim or stand down. A simultaneous double
        claim is reconciled by :meth:`_maybe_yield` on the loser when it hears the
        winner's claim. Owning the key already is idempotent — re-claiming just
        re-asserts, so a legitimate retry by the owner is never suppressed."""
        if not work_key:
            return True
        holder = self._claim_holder(work_key)
        if holder is not None and holder != self.local.id and holder < self.local.id:
            return False  # a lower-id live+trusted peer owns it → stand down
        self._emit_claim(work_key, "active")
        return True

    def release(self, work_key: str) -> None:
        """Voluntarily give up a key this node holds (work finished, or aborted),
        freeing it for another node without waiting for this node to go down."""
        if work_key and self._own_claim(work_key) is not None:
            self._emit_claim(work_key, "released")

    def _own_claim(self, work_key: str) -> protocol.ClaimRecord | None:
        return self._claims.get(work_key, {}).get(self.local.id)

    def _emit_claim(self, work_key: str, state: str) -> None:
        """Mint, store, and gossip our own claim on ``work_key`` in ``state``,
        bumping the per-key seq so it supersedes our previous record everywhere."""
        seq = self._claim_seq.get(work_key, -1) + 1
        self._claim_seq[work_key] = seq
        if state == "released":
            self._released_at[work_key] = time.monotonic()  # arm tombstone reaping
        else:
            self._released_at.pop(work_key, None)            # re-claimed: not a tombstone
        rec = self._sign_claim(protocol.ClaimRecord(
            work_key=work_key, node=self.local.id, epoch=self.epoch,
            seq=seq, state=state))
        self._store_claim(rec)
        self._broadcast(protocol.work_claim(rec.to_dict()))

    def _sign_claim(self, rec: protocol.ClaimRecord) -> protocol.ClaimRecord:
        """Stamp our pubkey and sign the record so peers can authenticate and pin
        it. A keyless node returns it unsigned (never authoritative to others)."""
        if self.key is None:
            return rec
        rec = replace(rec, pubkey=self.key.public_b64)
        return replace(rec, sig=self.key.sign(protocol.claim_signing_bytes(rec.to_dict())))

    def _claim_authentic(self, raw: dict) -> bool:
        """A **keyed** claim (carries a `pubkey`) MUST carry a `sig` verifying
        against that pubkey over the claim's canonical bytes, else it is a forgery
        or was tampered in relay and is dropped. A **keyless** claim has nothing to
        verify: accepted, but it can never be authoritative (its claimant is never
        trusted-personal), so it cannot suppress work — the safe degradation."""
        pubkey = str(raw.get("pubkey", ""))
        if not pubkey:
            return True
        sig = str(raw.get("sig", ""))
        return bool(sig) and crypto.verify(
            pubkey, protocol.claim_signing_bytes(raw), sig)

    def _on_work_claim(self, msg: dict) -> None:
        raw = msg.get("claim")
        if not isinstance(raw, dict):
            return
        if not self._claim_authentic(raw):
            return  # forged/tampered lease — drop it
        rec = protocol.ClaimRecord.from_dict(raw)
        if rec is None or rec.node == self.local.id:
            # Our own claim echoed back has nothing to teach us — we are its source
            # of truth (and re-storing it can't beat our own seq anyway).
            return
        # id→key pinning: a claim naming a node whose key we know MUST carry that
        # exact key. This rejects both a *different* key (a relay re-keying the id)
        # and — critically — a **keyless** claim minted under a keyed peer's id: a
        # third party could otherwise assert `{node: P}` with no key at all, which
        # (P being a real trusted peer) would be believed as P's lease and suppress
        # work P never claimed. So the guard fires whenever the embedded key differs
        # from the pin, empty included (mirrors the advert id-hijack guard, which is
        # likewise unconditional once a key is pinned).
        pinned = self._pinned_pubkey(rec.node)
        if pinned and rec.pubkey != pinned:
            return
        if not self._store_claim(rec):
            return  # stale (older than what we hold) or the book is capped
        # Relay VERBATIM so the claimant's signature survives the next hop. The
        # freshness gate in _store_claim stops the echo from looping.
        self._broadcast(protocol.work_claim(raw))
        self._maybe_yield(rec.work_key)

    def _store_claim(self, rec: protocol.ClaimRecord) -> bool:
        """Merge a claim into the book by per-claimant freshness. Returns True iff
        it was adopted (new or newer). Enforces the table cap on genuinely new
        (work_key, claimant) records only, so a fresher update to an existing claim
        is never dropped by the cap."""
        book = self._claims.setdefault(rec.work_key, {})
        cur = book.get(rec.node)
        if cur is not None and not rec.newer_than(cur):
            return False
        # The cap fences a gossip flood of spoofed PEER work_keys. Our OWN claim is
        # authoritative locally and MUST always be stored — dropping it silently (via
        # _emit_claim, which ignores this return) leaves _own_claim None, so the executor
        # watcher releases the lease and a re-dispatch double-spawns. Self is never a
        # flood source, so exempt it AND never count its records toward the cap.
        #
        # At the cap, refusing every new record let a verified-but-FOREIGN device (or any
        # keyed intruder inside the join fence) fill the book with 4096 spoofed workKeys
        # and, since a refused new (workKey, claimant) never stores, starve EVERY genuine
        # personal claim thereafter — breaking origination dedup mesh-wide and violating
        # the spec's "a foreign or keyless node can never deny you work"
        # (docs/szpontnet/12#security-properties). So at the cap an AUTHORITATIVE incoming
        # claim — one that can actually win ownership: a live, personal, key-bound
        # claimant — may EVICT one expendable stored record to make room: a `released`
        # tombstone, or a record that is not [authoritative](_claim_authoritative) (a
        # foreign/down/keyless/stale claimant that can never win ownership here). A
        # NON-authoritative incoming claim is still refused outright, exactly as before —
        # so a foreign flood can never displace anything (it only ever occupies evictable
        # slots), yet can never starve a real personal claim either (which evicts one of
        # those slots). We refuse only when the book is full of live authoritative claims
        # (genuine saturation) or the incomer itself is non-authoritative.
        if cur is None and rec.node != self.local.id:
            peer_count = 0
            victim: tuple[str, str] | None = None
            for wk, b in self._claims.items():
                for n, r in b.items():
                    if n == self.local.id:
                        continue
                    peer_count += 1
                    if victim is None and (not r.active
                                           or not self._claim_authoritative(n, r)):
                        victim = (wk, n)
                if peer_count >= _MAX_CLAIMS and victim is not None:
                    break  # cap reached and a victim found — no need to scan further
            if peer_count >= _MAX_CLAIMS:
                if victim is None or not self._claim_authoritative(rec.node, rec):
                    if not book:
                        self._claims.pop(rec.work_key, None)  # don't leave an empty book
                    return False
                vk, vn = victim
                del self._claims[vk][vn]
                if not self._claims[vk]:
                    self._claims.pop(vk, None)
                book = self._claims.setdefault(rec.work_key, {})  # victim may have been us
        book[rec.node] = rec
        return True

    def _claim_authoritative(self, node_id: str, rec: protocol.ClaimRecord) -> bool:
        """Whether ``node_id``'s claim ``rec`` counts toward ownership: self always;
        a peer only while its link is **live** (up or stale, never down), it is
        **trusted-personal**, AND the claim is **cryptographically bound to that
        peer** — it carries the peer's pinned key (so its signature was already
        verified under that key at ingestion). A dead owner's lease lapses
        (self-healing), a foreign/unverified node can never suppress (anti-DoS), and
        a **keyless or wrong-key claim minted under a trusted peer's id by a third
        party is never authoritative** — ownership requires proof the named peer
        actually signed the lease, not merely that the name it bears is trusted."""
        if node_id == self.local.id:
            return True
        peer = self.peers.get(node_id)
        if peer is None:
            return False
        stale, timeout = self.proto["peerStaleSecs"], self.proto["peerTimeoutSecs"]
        if peer.link_state(stale, timeout) == "down":
            return False
        if self._peer_trust(peer) != "personal":
            return False
        # The binding: an authoritative claim MUST be signed by the peer it names.
        # `_claim_authentic` verified rec.sig under rec.pubkey; requiring
        # rec.pubkey == the peer's advertised (pinned) key closes the loop, so only
        # the holder of that peer's private key could have produced this record.
        if not (bool(rec.pubkey) and rec.pubkey == peer.info.pubkey):
            return False
        # Liveness-scoped lease: a claim minted by a PRIOR incarnation must lapse once
        # the peer restarts. The device key survives a restart, so the pubkey binding
        # alone would keep a stale lease authoritative — and a quick reconnect (6AM
        # self-update) beats the 300s down-reap, so _forget_claims never runs. The
        # node's epoch (time.time() at construction) only advances on restart, so a
        # claim from before the peer's current incarnation (rec.epoch < the peer's
        # advertised epoch) no longer reflects held work and must not suppress it.
        return rec.epoch >= peer.info.epoch

    def _claim_holder(self, work_key: str) -> str | None:
        """The node that currently owns ``work_key``: the lowest-id claimant among
        all **active** claims whose claimant is [authoritative](_claim_authoritative).
        None when the key is unclaimed. A pure function of the claim book and the
        live set — every node computes the same owner, leaderlessly."""
        book = self._claims.get(work_key)
        if not book:
            return None
        owners = [node for node, rec in book.items()
                  if rec.active and self._claim_authoritative(node, rec)]
        return min(owners) if owners else None

    def _claim_owners(self) -> dict[str, str]:
        """Every currently-owned work_key mapped to its owner id (for the snapshot).
        Unowned keys — all claimants dead/foreign/released — are omitted."""
        out: dict[str, str] = {}
        for work_key in self._claims:
            holder = self._claim_holder(work_key)
            if holder is not None:
                out[work_key] = holder
        return out

    def _maybe_yield(self, work_key: str) -> None:
        """After adopting a peer's claim, stand down if it means a better (lower-id)
        peer now owns a key we were originating: withdraw our claim and fire the
        loss hook so the caller can abort the work it started. This is how a
        simultaneous double-claim converges on the single lowest-id owner."""
        own = self._own_claim(work_key)
        if own is None or not own.active:
            return
        holder = self._claim_holder(work_key)
        if holder is not None and holder != self.local.id and holder < self.local.id:
            self._emit_claim(work_key, "released")
            activity.log("mesh", "mesh-claim-yield",
                         f"Mesh: yielded {work_key} to {self._node_name(holder)}")
            if self.on_claim_lost is not None:
                with contextlib.suppress(Exception):
                    self.on_claim_lost(work_key)

    def _forget_claims(self, node_id: str) -> None:
        """Drop every claim a now-reaped node held, and any book left empty. Called
        when a peer is fully removed from the table (its leases can no longer be
        authoritative, and keeping them just wastes memory)."""
        for book in self._claims.values():
            book.pop(node_id, None)
        self._claims = {k: v for k, v in self._claims.items() if v}

    def _evict_unbound_claims(self, node_id: str, pubkey: str) -> None:
        """Drop any claim record under ``node_id`` whose key doesn't match its now-
        known ``pubkey`` — a keyless or wrong-key record a third party planted before
        we learned this node's advertisement (cold join), which would otherwise
        out-fresh the node's real signed claim indefinitely. Only records that are
        already non-authoritative (unbound) are removed, so a genuine claim is never
        touched. Runs only when the key first appears or changes, not per refresh."""
        for work_key, book in list(self._claims.items()):
            rec = book.get(node_id)
            if rec is not None and rec.pubkey != pubkey:
                del book[node_id]
                if not book:
                    del self._claims[work_key]

    def _reap_released_claims(self, now: float) -> None:
        """Drop our own long-settled ``released`` tombstones. A released self-record
        only needs to live long enough to gossip the release and out-fresh a stale
        ``active`` a briefly-disconnected peer might echo; past a few peer-timeout
        windows every still-live peer has converged (and a longer-gone one is reaped,
        its book forgotten, its restart bumping the epoch that lapses old claims). The
        record is dead weight after that: [_claim_holder] already ignores non-active
        records, so removing it changes no ownership decision. Without this, a
        long-lived node accretes one permanent released record per distinct work_key
        it ever handled (an unbounded leak). ``_claim_seq`` is deliberately KEPT so a
        later re-claim of the same key still supersedes any peer's stale copy; a peer
        echoing our reaped claim is dropped at ingestion ([_on_work_claim]), so the
        tombstone can't be resurrected."""
        ttl = self.proto["peerTimeoutSecs"] * 3
        me = self.local.id
        for work_key, ts in list(self._released_at.items()):
            if now - ts < ttl:
                continue
            book = self._claims.get(work_key)
            rec = book.get(me) if book else None
            if rec is not None and not rec.active:
                del book[me]
                if not book:
                    self._claims.pop(work_key, None)
            self._released_at.pop(work_key, None)

    # MARK: - assignments

    def _recompute(self, why: str) -> None:
        new = assign.assign_all(self._alive_nodes(), self.overrides, self.local.id)
        old = self._assignments
        self._assignments = new
        for duty_id, a in new.items():
            before = old.get(duty_id)
            if before is not None and before.assigned != a.assigned:
                names = [self._node_name(nid) for nid in a.assigned] or ["nobody"]
                activity.log("mesh", "mesh-takeover",
                             f"Mesh: {duty_id} → {', '.join(names)} ({why})")

    def _node_name(self, node_id: str) -> str:
        if node_id == self.local.id:
            return self.local.name
        peer = self.peers.get(node_id)
        return peer.info.name if peer else node_id[:8]

    # MARK: - dispatch

    async def dispatch(self, duty_id: str, prompt: str,
                       target: str | None = None,
                       api_key: str | None = None,
                       work_key: str = "") -> list[dict]:
        """Run a SzpontRequest under a duty's placement: one spawn per slot,
        failing over within each slot's candidate list. Returns one result dict
        per slot.

        Target selection is the dispatcher's own load-balancing call (no
        consensus): candidates are ranked ``surplus-first`` by default
        (``config.dispatch_strategy``), so work flows to whoever has the most
        spare quota. ``target`` overrides that entirely — the client names one
        node and the request goes there with no failover; if that node declines
        (foreign, over quota, …) the decline is reported as-is. This is the
        "Alice may forward everything to Bob, and Bob may refuse" case.

        ``work_key`` (optional) opts this request into **origination dedup**: the
        node first [claims](claim) the key, and if a better peer already owns it
        returns a single ``suppressed`` slot instead of double-originating. Only
        the leaderless P2P path dedupes — a ``server`` node (runs locally) and an
        explicit ``target`` (the client overrode placement) bypass the claim. See
        docs/szpontnet/12-work-claims.md.
        """
        # The credential presented to an API-key-gated target: the per-request key
        # (from a control client / CLI) when given, else this node's own env key.
        req_key = api_key if api_key is not None else config.api_key()
        # Banned devices are never dispatch targets. Filtered from the candidate
        # input only — NOT from the assignment view (_alive_nodes), which must stay
        # identical mesh-wide (a ban is this node's local mark, never gossiped).
        nodes = [n for n in self._alive_nodes() if not self._node_banned(n.id)]
        if config.server_mode():
            # A dedicated server never routes work to peers: a request it is asked
            # to dispatch runs on ITSELF, or is refused if aimed explicitly
            # elsewhere. This realizes the 'accepts requests, never dispatches'
            # role — the server is a sink for work, never a source.
            if target is not None and target != self.local.id:
                return [{"slot": "server", "node": None, "nodeName": None,
                         "status": "declined",
                         "reason": "server node does not dispatch to peers"}]
            slots = [("server", [self.local.id])]
        elif target is not None:
            if self._node_banned(target):
                # An explicit target is the client's unilateral pick — but a banned
                # device broke a promise here; refuse locally rather than ask it.
                return [{"slot": "target", "node": target,
                         "nodeName": self._node_name(target),
                         "status": "declined", "reason": "target is banned here"}]
            slots = [("target", [target])]
        else:
            # Origination dedup (docs/szpontnet/12): only the leaderless surplus-first
            # path can race a peer to the same external event, so the gate lives here
            # (not on the server/target paths). We do NOT claim on the dispatcher —
            # the EXECUTOR claims the key for its agent's lifetime, so the key stays
            # held exactly while the work is actually running (and is freed when it
            # finishes, so a retry after a crash isn't suppressed). Here we only READ
            # the owner: if a live authoritative node already holds it, its agent is
            # on the work → suppress rather than route a second run.
            if work_key:
                holder = self._claim_holder(work_key)
                if holder is not None:
                    name = self._node_name(holder)
                    return [{"slot": "claim", "node": holder, "nodeName": name,
                             "status": "suppressed",
                             "reason": f"work already claimed by {name}" if name
                             else "work already claimed"}]
            slots = assign.slot_candidates(duty_id, nodes, self.overrides,
                                           self.local.id, config.dispatch_strategy())
        used: set[str] = set()
        results: list[dict] = []
        for slot_platform, candidates in slots:
            outcome = {"slot": slot_platform, "node": None, "nodeName": None,
                       "status": "failed", "reason": "no eligible node"}
            for node_id in candidates:
                if node_id in used:
                    continue
                status, reason = await self._dispatch_to(node_id, duty_id, prompt,
                                                         req_key, work_key)
                if status == "spawned":
                    used.add(node_id)
                    outcome = {"slot": slot_platform, "node": node_id,
                               "nodeName": self._node_name(node_id),
                               "status": "spawned", "reason": ""}
                    break
                outcome = {"slot": slot_platform, "node": node_id,
                           "nodeName": self._node_name(node_id),
                           "status": status, "reason": reason}
            results.append(outcome)
        detail = ", ".join(
            f"{r['slot']}→{r['nodeName'] or '∅'}({r['status']})" for r in results
        )
        action = "mesh-dispatch" if all(r["status"] == "spawned" for r in results) \
            else "mesh-dispatch-failed"
        activity.log("mesh", action, f"Mesh dispatch {duty_id}: {detail}")
        return results

    async def _dispatch_to(self, node_id: str, duty_id: str, prompt: str,
                           api_key: str = "", work_key: str = "") -> tuple[str, str]:
        job = Job(id=uuid.uuid4().hex, duty=duty_id, prompt=prompt,
                  requested_by=self.local.id, requested_at=time.time(),
                  work_key=work_key)
        if node_id == self.local.id:
            status, reason, _ = self._run_local_request(job, "personal")  # to myself
            return status, reason
        peer = self.peers.get(node_id)
        if peer is None or not peer.linked:
            return "failed", "no link"
        # Remember this remote dispatch so that if the executor turns out to be a
        # zero-trust node — running our request confined and returning the artifact
        # rather than acting on it — we recognize its later `job-result` and act on
        # it ourselves. Harmless for a personal executor that never responds: the
        # entry just expires. (docs/szpontnet/13)
        self._register_awaiting(job.id, node_id, duty_id, prompt)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._job_futures[job.id] = (fut, node_id)
        try:
            peer.writer.write(protocol.encode(protocol.dispatch(job, api_key)))
            await peer.writer.drain()
            msg = await asyncio.wait_for(fut, timeout=self.proto["dispatchAckTimeoutSecs"])
            status = str(msg.get("status", "failed"))
            if status == "spawned":
                self._maybe_arm_deadline(job.id, node_id, bool(msg.get("direct")))
            else:
                # Explicit refusal (declined/failed): the executor spawns nothing, so it
                # owes no result — stop awaiting one.
                self._forget_dispatch(job.id)
            return status, str(msg.get("reason", ""))
        except (asyncio.TimeoutError, ConnectionError, OSError):
            # The ack never came back (timeout) or the link flapped mid-dispatch. We
            # report `failed`, and the caller re-runs the work locally / fails it over —
            # so we MUST NOT later act on a result for THIS job: a FOREIGN executor that
            # DID receive + spawn before the flap re-delivers its result on the healed
            # link, and _on_job_result would otherwise find the still-armed awaiting entry
            # and perform the same unit of work a SECOND time under our identity (a
            # duplicate PR review). Its foreign claim is non-authoritative here, so it
            # never suppressed the local re-run, and the job ids differ so per-job-id
            # dedup can't catch it — forgetting the dispatch is what makes it act-once.
            self._forget_dispatch(job.id)
            return "failed", "peer did not answer"
        finally:
            self._job_futures.pop(job.id, None)

    def _forget_dispatch(self, job_id: str) -> None:
        """Abandon a remote dispatch whose hand-off did not succeed (ack lost, link flap,
        or an explicit decline/fail). Drop the awaiting entry and mark the job handled, so
        a foreign executor's LATE ``job-result`` (it may have received + spawned before the
        flap) is still ACKed — stopping its reliable-delivery retries — but is NEVER acted
        on: the originator has already re-run or failed the work over, and acting again
        would perform the social action twice. Mirrors the duplicate-result path in
        [_on_job_result] (ack, don't re-act)."""
        self._awaiting_result.pop(job_id, None)
        self._acted_results[job_id] = time.monotonic()

    def _maybe_arm_deadline(self, job_id: str, node_id: str, direct: bool) -> None:
        """Arm the accountability clock over an acceptance: a **foreign** executor
        that replied ``spawned`` (without ``direct`` — the personal path never owes
        a result) now owes us a ``job-result`` within the completion deadline
        (docs/szpontnet/13#the-completion-deadline). Personal executors are devices
        the operator vouched for — never tracked."""
        aw = self._awaiting_result.get(job_id)
        if aw is None or direct:
            # `direct` is taken at the executor's word (job-status is unsigned): a
            # liar dodges the deadline, but gains nothing over the pre-v0.4.0
            # world where EVERY acceptance was untracked — and arming anyway would
            # false-ban every honest asymmetric-trust executor, whose personal
            # path never owes a result. Deliberate; see ch13 "Limitations".
            return
        peer = self.peers.get(node_id)
        if self._peer_trust(peer) != "foreign":
            return
        aw.deadline = (time.monotonic()
                       + float(self.proto["foreignCompletionDeadlineSecs"]))
        # Pin the key the executor PROVED right now, while its link is live. If it
        # goes silent and is reaped before the deadline fires, _ban_executor has no
        # peer left to read a fingerprint from — without this the ban would fall back
        # to an id-only (fingerprint-less) mark, which any keyed reconnect bypasses.
        if peer is not None and peer.verified_fp:
            aw.executor_fp = peer.verified_fp

    def _resolve_job_future(self, msg: dict, writer: asyncio.StreamWriter) -> None:
        entry = self._job_futures.get(str(msg.get("id", "")))
        if entry is None:
            return
        fut, target_id = entry
        if fut.done():
            return
        # Only the peer we dispatched this job to may report its outcome. Job ids
        # are 128-bit random and not gossiped, so guessing is infeasible, but a
        # peer that legitimately shares the link mustn't be able to resolve a
        # dispatch aimed at someone else — verify the responder is the target.
        peer = self._peer_by_writer(writer)
        if peer is None or peer.info.id != target_id:
            return
        fut.set_result(msg)

    def _admit(self, job: Job, trust_level: str) -> tuple[str, str]:
        """Refusal / execution-mode policy — the receiving node's own call, no
        consensus needed. Returns ``(mode, reason)`` where ``mode`` is one of:

        - ``"run"`` — execute **directly** on the host (a **personal** requester,
          full trust: the work may take social actions under our identity);
        - ``"confined"`` — the requester is **foreign** but we have a confinement
          runner configured, so we run the compute **sandboxed and response-only**
          and return the result for the requester to act on
          ([_run_confined], docs/szpontnet/13);
        - ``"decline"`` — refuse (the dispatcher fails the slot over like a dead
          node): a **foreign** requester with **no** confinement runner (the safe
          v1 default), a **disabled** duty, or being **out of tokens**.

        ``trust_level`` is the requester's classification from the **verified
        link** ([_peer_trust]) — never from anything in the job, which is spoofable.
        Duty/token refusals apply regardless of trust: a node that can't serve the
        work declines it outright rather than sandboxing it."""
        if trust_level == "banned":
            # A banned device broke the accountability contract; the confined path
            # is a favor, and favors end here (docs/szpontnet/13#the-ban).
            return "decline", "banned device"
        if not self.local.duty_enabled(job.duty):
            return "decline", f"duty {job.duty} disabled here"
        if self.current_tokens() == "out":
            return "decline", "out of tokens"
        if trust_level == "foreign":
            if config.foreign_spawn():
                return "confined", ""
            return "decline", "foreign device (no confinement runner configured)"
        return "run", ""

    def _run_local_request(self, job: Job, trust_level: str,
                           requester_id: str = "") -> tuple[str, str, bool]:
        """Admit-or-decline, then run. Shared by the remote-receive path
        (``_take_job``) and a local/self dispatch, so both apply the same policy.
        A ``"confined"`` admission (foreign, zero-trust) runs sandboxed and routes
        its result back to ``requester_id``; the personal/self path never confines
        (``requester_id`` unused there). Returns ``(status, reason, no_result)`` where
        ``no_result`` marks a ``spawned`` that owes NO later ``job-result`` (a personal
        fire-and-forget run, or a confined dispatch deduped against an already-running
        agent) — see [_take_job]."""
        mode, reason = self._admit(job, trust_level)
        if mode == "decline":
            activity.log("mesh", "mesh-dispatch-failed",
                         f"Mesh: declined {job.duty} from "
                         f"{self._node_name(job.requested_by)} — {reason}")
            return "declined", reason, False
        if mode == "confined":
            return self._run_confined(job, requester_id)
        return self._spawn_local(job)

    def _take_job(self, job: Job, writer: asyncio.StreamWriter) -> None:
        """A peer asked us to run a SzpontRequest. Classify the requester from the
        VERIFIED link (not the job's self-reported requestedBy), admit-or-decline,
        and answer with the outcome so the dispatcher can act on it. The result of a
        *confined* (foreign) job is delivered later as a ``job-result``; the
        ``spawned`` here is only the hand-off ack."""
        peer = self._peer_by_writer(writer)
        trust_level = self._peer_trust(peer)
        status, out_reason, no_result = self._run_local_request(
            job, trust_level, requester_id=peer.info.id if peer else "")
        # A spawn that owes no later job-result is fire-and-forget: say so (`direct`,
        # additive) so an accountability-tracking requester that happens to classify US
        # foreign doesn't arm a deadline over a result we never owed — and then ban us
        # for keeping it. True for a personal (direct) run AND for a confined dispatch
        # deduped against an already-running agent (the original job carries the one
        # result); a fresh confined spawn owes a result, so it is NOT direct.
        direct = status == "spawned" and no_result
        with contextlib.suppress(ConnectionError, OSError):
            writer.write(protocol.encode(
                protocol.job_status(job.id, status, out_reason, self.local.id,
                                    direct=direct)
            ))

    def _record_usage(self, units: float) -> None:
        """Book quota against this node's accounting and re-advertise the fresher
        surplus so the mesh's load balancing tracks real consumption."""
        self.stats = stats.record(self.stats, units)
        stats.save(self.stats)
        self._bump_and_gossip()

    def _spawn_local(self, job: Job) -> tuple[str, str, bool]:
        """Run a personal request directly on the host. Returns
        ``(status, reason, no_result)``; ``no_result`` is True for every ``spawned``
        outcome because the personal path is fire-and-forget — no ``job-result`` ever
        follows — which [_take_job] reports as ``direct`` so a requester that happens
        to classify us foreign doesn't arm a completion deadline over a result we
        never owed."""
        wk = job.work_key
        # Idempotency + the executor-claim's atomicity: if our own agent for this
        # key is already live, don't spawn a second one — report success (the work
        # IS being handled here). This runs synchronously with the claim below (no
        # await between the check and `_emit_claim`), so two dispatches of the same
        # key arriving back-to-back can never both pass it.
        if wk and wk in self._agents:
            return "spawned", "", True  # deduped against our own live agent — owes no result
        # Ground-truth floor for the EXECUTOR — the same one the ORIGINATING side
        # has always had (Store._in_flight). `_agents` only remembers agents THIS
        # node incarnation spawned; an agent can be live on the host yet absent
        # from it — the applet's fail-open local spawn (no claim, no book entry), a
        # node restart / singleton respawn after a deploy (book wiped, agent lives
        # on), or a manual SPAWN. A peer routing that same work here sees no claim
        # and its own ps-scan can't see our host, so without this check we'd launch
        # a duplicate onto a PR already under review. Keyed on the PR, not the exact
        # work key, so a fresh push (new @sha) can't dodge it either.
        if wk and self._pr_agent_running(wk):
            activity.log("mesh", "mesh-dedup",
                         f"Already running {job.duty} for this PR here — not double-spawning")
            return "spawned", "", True  # deduped against a live host agent — owes no result
        done_path = self._agent_done_path(wk) if wk else None
        try:
            spawnjob.spawn_job(job.prompt, done_path=done_path)
        except spawnjob.JobSpawnError as exc:
            activity.log("mesh", "spawn-failed", f"Mesh job {job.duty} failed here: {exc}")
            return "failed", str(exc), False
        if wk:
            # The executor owns the key for the agent's lifetime: claim it now, and
            # free it when the agent's completion sentinel appears (docs/szpontnet/12).
            self._agents[wk] = {"done": done_path, "at": time.monotonic()}
            self._emit_claim(wk, "active")
            with contextlib.suppress(RuntimeError):  # no running loop → tests w/o watch
                asyncio.get_running_loop().create_task(self._watch_agent(wk, done_path))
        self._record_usage(config.job_cost_units())
        activity.log("mesh", "mesh-spawn",
                     f"Mesh: running {job.duty} (from {self._node_name(job.requested_by)})")
        return "spawned", "", True

    def _pr_agent_running(self, work_key: str) -> bool:
        """Is a live ``claude`` agent for this work key's PR already running on THIS
        host? The executor's ground-truth floor against a double-spawn (see
        `_spawn_local`). Reuses the ORIGINATING side's matcher (`live_pr_numbers`)
        so both sides agree on what "an agent is on this PR" means. Fails OPEN — a
        ps error reads as "not seen" so a transient failure never drops work — the
        same trade the store's `_live_pr_agents` makes.

        ``ps -Ao args=`` is the portable spelling: on macOS ``-e`` prints the
        environment, not every process, so the store's Linux-only ``-eo`` can't be
        reused in this cross-platform node (it runs on both OSes)."""
        from .. import autofix

        ref = autofix.parse_work_key(work_key)
        if ref is None:
            return False
        _kind, owner, repo, number = ref
        try:
            out = subprocess.run(["ps", "-Ao", "args="],
                                 capture_output=True, text=True, timeout=10).stdout
        except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
            # UnicodeDecodeError: text=True decodes strict UTF-8, so any process on the
            # box with a non-UTF-8 byte in its argv makes `ps` output undecodable. It is a
            # ValueError, not an OSError/SubprocessError, so without it here the exception
            # escapes this fail-open guard and tears the caller's link / self-dispatch —
            # the same catch Store._live_pr_agents makes for its identical ps scan.
            return False
        return number in autofix.live_pr_numbers(out, owner, repo)

    def _agent_done_path(self, work_key: str) -> str:
        """A per-agent completion-sentinel path under the mesh dir (NOT /tmp, which
        macOS purges). Its existence later == THIS agent finished.

        The incarnation (``epoch``) is stamped into the path: agents are detached
        (``start_new_session``) and OUTLIVE a node restart, writing their exit
        sentinel long after. Without the epoch, a prior incarnation's agent for the
        same work_key wrote to the same path (``_claim_seq`` resets to 0 on
        restart), so its late sentinel was misread as the NEW agent's — the watcher
        released a still-held claim on the first poll, re-opening the work to a
        double-dispatch. Epoch makes the two paths disjoint. ``_claim_seq`` still
        disambiguates sequential re-dispatches within one incarnation."""
        from . import statefile

        agents = statefile.state_path().parent / "agents"
        with contextlib.suppress(OSError):
            agents.mkdir(parents=True, exist_ok=True)
        # The readable prefix is truncated for a sane filename, but the discriminating
        # part of a work_key (`…#<n>@<sha>`) sits at the END, so two long keys sharing a
        # 96-char sanitized prefix (two PRs of one long owner/repo) would collide onto
        # ONE sentinel — one agent's exit would then release the OTHER's still-held claim
        # (double-dispatch) and strand the finished one. Bind a digest of the FULL key so
        # distinct keys are always distinct paths, regardless of prefix length.
        prefix = "".join(c if c.isalnum() else "_" for c in work_key)[:96]
        digest = hashlib.sha1(work_key.encode("utf-8", "surrogatepass")).hexdigest()[:12]
        inc = int(self.epoch * 1_000_000)  # this node run, unique per process start
        seq = self._claim_seq.get(work_key, 0)
        return str(agents / f"{prefix}.{digest}.{inc}.{seq}.done")

    def _sweep_stale_sentinels(self) -> None:
        """At startup this node owns no agents yet, so every completion sentinel on
        disk is an orphan from a prior incarnation (a detached agent that outlived a
        restart, or a crash before ``_watch_agent``'s cleanup). Remove them so they
        can never be misread — and so the epoch-stamped paths don't accumulate
        without bound across restarts. A prior-incarnation agent still running will
        just re-create its own epoch-stamped file, which no watcher here tracks."""
        from . import statefile

        agents = statefile.state_path().parent / "agents"
        with contextlib.suppress(OSError):
            for p in agents.glob("*.done"):
                with contextlib.suppress(OSError):
                    p.unlink()

    async def _watch_agent(self, work_key: str, done_path: str | None) -> None:
        """Hold the executor's claim on ``work_key`` until its agent finishes.

        The agent writes ``done_path`` on exit (``review.shell_command``'s exit-code
        sentinel), which frees the key so the SAME work can be re-run if it wasn't
        actually resolved (a crashed review) — the retry path. A backstop frees the
        key if that signal is ever lost, and a yield (a better peer preempts us)
        stops the watch early. Freeing on node death is the liveness lease, handled
        elsewhere."""
        deadline = time.monotonic() + self._agent_max_secs
        try:
            while time.monotonic() < deadline:
                if done_path and os.path.exists(done_path):
                    break
                own = self._own_claim(work_key)
                if own is None or not own.active:
                    break  # preempted (yield) — our claim is already gone
                await asyncio.sleep(0.1)
        finally:
            self._agents.pop(work_key, None)
            if done_path:
                with contextlib.suppress(OSError):
                    os.unlink(done_path)
            self.release(work_key)

    # MARK: - foreign zero-trust execution (confined compute + response-back)
    #
    # A foreign (untrusted) SzpontRequest is never run on the host. When a
    # confinement runner is configured ([config.foreign_spawn]) we run the compute
    # SANDBOXED and RESPONSE-ONLY: the sandbox writes its artifact to a result file,
    # which we return to the originator as a signed `job-result` (re-sent until
    # `job-ack`d — reliable delivery). The ORIGINATOR then performs any social action
    # itself, under its own identity. This realizes the normative foreign-execution
    # security contract (docs/szpontnet/11 + the full flow in docs/szpontnet/13):
    # sandboxed compute, no host-identity action here, request-in / response-out.

    def _result_path(self, job_id: str, incoming: bool = False):
        """Where a confined job's artifact is staged: ``out-*`` is what our sandbox
        writes and we return; ``in-*`` is what we hand our own result handler when a
        foreign executor returns to us. Under the per-node mesh dir (isolated).

        The filename is derived from a SHA-256 of the job id, never the id itself: on
        the executor side ``job.id`` is a fully attacker-controlled string from a
        foreign SzpontRequest, and interpolating it raw let ``id = "../../.."`` steer
        the confined artifact's path outside ``results/`` (a runner that shares the host
        FS and creates its parent dir would then write the sandbox's output to an
        operator-chosen location). Hashing yields a collision-free hex token — no path
        separators, no ``..`` — so the staging path is always inside ``results/`` while
        still deterministic per job (executor write and host read agree on it)."""
        # surrogatepass: job_id is attacker-controlled and a JSON lone surrogate would
        # make a plain .encode("utf-8") raise (mirrors the work_key `.done` sentinel), so
        # hashing stays total for ANY id — the whole point of accepting arbitrary ids.
        token = hashlib.sha256(job_id.encode("utf-8", "surrogatepass")).hexdigest()[:32]
        d = identity.mesh_dir() / "results"
        d.mkdir(parents=True, exist_ok=True)
        return d / (f"in-{token}.json" if incoming else f"out-{token}.json")

    def _run_confined(self, job: Job, requester_id: str) -> tuple[str, str, bool]:
        """Run a foreign request under zero trust: launch the confinement runner on
        the (untrusted) prompt, book usage, and start watching for its result file to
        return to ``requester_id``. Returns ``(status, reason, no_result)``; a fresh
        confined spawn is the one path that DOES owe a later `job-result`, so it
        reports ``no_result=False`` (the requester may arm a completion deadline). The
        actual artifact is delivered later as that `job-result`."""
        if not requester_id:
            # No verified requester link to return the result to — we can't honor
            # response-only, so decline rather than run a stranger's code for nobody.
            return "failed", "no verified requester for confined result", False
        wk = job.work_key
        # Idempotency + the executor-claim (docs/szpontnet/12), mirroring _spawn_local:
        # the executor that spawns the agent mints the work-claim and holds it for the
        # agent's lifetime. Without this the confined path spawned a fresh sandbox on
        # EVERY (re-)dispatch of the same key — so an originator's same-poll double
        # dispatch (event + reconcile) ran the work twice and acted on two results
        # (e.g. a duplicate PR review under its identity), and a re-poll never saw a
        # holder to suppress against. If a confined (or personal) agent for this key is
        # already live here, don't spawn a second — report ``spawned`` with
        # ``no_result=True`` so the originator neither acts twice nor arms a deadline
        # over a result that won't come (the original job.id carries the one result).
        if wk and wk in self._agents:
            return "spawned", "", True
        # Bound the in-flight confined set like the other two foreign maps
        # (_pending_results / _awaiting_result), so a burst of foreign dispatches within
        # one job-timeout window can't grow it without limit (docs/szpontnet/13 — "both
        # ends bound their bookkeeping"). At the cap, decline so the originator fails over.
        if (job.id not in self._confined_running
                and len(self._confined_running) >= _MAX_FOREIGN):
            return "failed", "confined-execution capacity reached", False
        result_path = self._result_path(job.id)
        try:
            spawnjob.spawn_confined(job.prompt, str(result_path))
        except spawnjob.JobSpawnError as exc:
            activity.log("mesh", "spawn-failed",
                         f"Mesh confined {job.duty} failed here: {exc}")
            return "failed", str(exc), False
        self._record_usage(config.job_cost_units())
        # Register the run so a `job-reminder` while it computes gets a truthful
        # `job-progress` answer instead of silence (docs/szpontnet/13).
        self._confined_running[job.id] = (requester_id, time.monotonic())
        if wk:
            # LOCAL idempotency only: track the confined agent in _agents so a repeat
            # dispatch of the same key HERE dedups (the `wk in self._agents` guard above),
            # tagged with THIS job.id so only this run's completion frees it.
            #
            # Deliberately NO _emit_claim here (unlike the personal _spawn_local path): wk
            # comes from a FOREIGN requester's SzpontRequest — attacker-controlled — so
            # gossiping a mesh-authoritative claim under our trusted device key would let a
            # foreign peer LAUNDER-suppress the personal mesh's own origination of any key it
            # names, violating "a foreign or keyless node can never deny you work"
            # (docs/szpontnet/12#security-properties). We only compute the sandboxed result
            # and return it to the requester; we are NOT the originator of wk, so we must not
            # claim ownership of it on the mesh's behalf. Local dedup still prevents us
            # double-spawning the same key here.
            self._agents[wk] = {"confined": job.id, "at": time.monotonic()}
        task = asyncio.get_running_loop().create_task(
            self._await_confined_result(job, requester_id, result_path))
        self._result_tasks.add(task)
        task.add_done_callback(self._result_task_done)
        activity.log("mesh", "mesh-spawn",
                     f"Mesh: running {job.duty} CONFINED for foreign "
                     f"{self._node_name(requester_id)} (result routes back)")
        return "spawned", "", False

    async def _await_confined_result(self, job: Job, requester_id: str,
                                     result_path) -> None:
        """Poll for the sandbox's result file, then return it to the requester. On
        timeout, return an explicit failure result so the requester isn't left
        hanging. Runs as a background task per confined job."""
        poll = max(0.1, float(self.proto["heartbeatIntervalSecs"]))
        deadline = time.monotonic() + float(self.proto["foreignJobTimeoutSecs"])
        last_size = -1
        try:
            while time.monotonic() < deadline:
                with contextlib.suppress(OSError):
                    size = result_path.stat().st_size if result_path.exists() else 0
                    if size > 0 and size == last_size:
                        # Non-empty and unchanged since the previous poll → fully written
                        # (guards against reading a still-growing file; a runner SHOULD
                        # also write atomically via a temp file + rename).
                        output = await asyncio.to_thread(self._read_result_file, result_path)
                        self._emit_result(job.id, requester_id, {
                            "ok": True, "duty": job.duty, "output": output, "error": ""})
                        return
                    last_size = size
                await asyncio.sleep(poll)
            activity.log("mesh", "mesh-dispatch-failed",
                         f"Mesh: confined {job.duty} timed out, returning failure")
            self._emit_result(job.id, requester_id, {
                "ok": False, "duty": job.duty, "output": "",
                "error": "confined execution timed out"})
        finally:
            # However this watcher ends (result, timeout, crash), the job is no
            # longer "running" for reminder purposes.
            self._confined_running.pop(job.id, None)
            # Reap the sandbox's staging artifact: it was already read into the
            # (re-sent-from-memory) `job-result`, so the file is spent. Left behind,
            # ``results/out-*.json`` accretes one file per distinct foreign job id — an
            # attacker-amplifiable disk leak, since the id is theirs (the in-flight cap
            # bounds concurrency, not the total ever staged).
            with contextlib.suppress(OSError):
                result_path.unlink(missing_ok=True)
            # Free the LOCAL dedup slot for this key (the confined path holds no mesh
            # claim to release — see _run_confined). Guard on THIS job.id so a later run's
            # entry, or a personal agent that took over the shared slot, is never freed by
            # ours. `release` is a defensive no-op here unless a personal _spawn_local for
            # the same key later minted a real claim — then it belongs to that agent and is
            # freed via its own _watch_agent, so we deliberately do NOT release it.
            wk = job.work_key
            if wk and self._agents.get(wk, {}).get("confined") == job.id:
                self._agents.pop(wk, None)

    def _result_task_done(self, task: asyncio.Task) -> None:
        """Reap a finished watcher task, surfacing a crash instead of letting it
        vanish into asyncio's default 'exception never retrieved' at GC time — a
        silent watcher death would strand the foreign requester."""
        self._result_tasks.discard(task)
        if not task.cancelled():
            exc = task.exception()
            if exc is not None:
                activity.log("mesh", "mesh-dispatch-failed",
                             f"Mesh: confined result watcher crashed: {exc!r}")

    @staticmethod
    def _read_result_file(path) -> str:
        """Read a confined artifact as text, truncated to the wire cap. Reads at most
        ``_MAX_RESULT_BYTES + 1`` bytes rather than slurping the whole file: the sandbox
        writes this file and its size is influenced by the (untrusted) foreign prompt, so
        a hostile runner emitting a multi-GB artifact must not be pulled entirely into
        memory just to be truncated away."""
        with open(path, "rb") as f:
            raw = f.read(_MAX_RESULT_BYTES + 1)
        if len(raw) > _MAX_RESULT_BYTES:
            raw = raw[:_MAX_RESULT_BYTES]
        return raw.decode("utf-8", errors="replace")

    def _sign_result(self, job_id: str, result_payload: dict) -> str:
        """Sign a job-result over its canonical bytes so the originator can bind the
        artifact to our key. Keyless node returns '' (accepted unsigned by peers)."""
        if self.key is None:
            return ""
        payload = {"id": job_id, "node": self.local.id, "result": result_payload}
        return self.key.sign(protocol.result_signing_bytes(payload))

    def _fit_result(self, job_id: str, result_payload: dict) -> dict:
        """Shrink an over-large artifact so the whole `job-result` line stays under
        the wire cap ([MAX_LINE_BYTES]) *after* JSON escaping — a receiver drops an
        over-length line, so an un-fitted result would never arrive. The output is
        halved until the encoded (sig-less) message fits, with headroom for the
        signature and envelope; truncation is flagged in `error`."""
        budget = protocol.MAX_LINE_BYTES - 512  # headroom for the sig + envelope
        output = str(result_payload.get("output", ""))
        truncated = False
        while output and len(protocol.encode(protocol.job_result(
                job_id, self.local.id, {**result_payload, "output": output}))) > budget:
            output = output[: len(output) // 2]
            truncated = True
        if not truncated:
            return result_payload
        return {**result_payload, "output": output,
                "error": result_payload.get("error") or "result truncated to fit the wire limit"}

    def _emit_result(self, job_id: str, to_node: str, result_payload: dict) -> None:
        """Build + sign the job-result and register it for reliable delivery to
        ``to_node``, sending the first copy now. Retried on the heartbeat tick until
        acked or its deadline (docs/szpontnet/13)."""
        result_payload = self._fit_result(job_id, result_payload)
        sig = self._sign_result(job_id, result_payload)
        msg = protocol.job_result(job_id, self.local.id, result_payload, sig)
        now = time.monotonic()
        if (job_id not in self._pending_results
                and len(self._pending_results) >= _MAX_FOREIGN):
            oldest = min(self._pending_results,
                         key=lambda k: self._pending_results[k].deadline)
            self._pending_results.pop(oldest, None)
        self._pending_results[job_id] = _PendingResult(
            msg=msg, to_node=to_node, next_retry=now,
            deadline=now + float(self.proto["foreignResultMaxSecs"]),
            created=now)
        self._send_pending(job_id)

    def _send_pending(self, job_id: str) -> None:
        """Send (or re-send) a pending result on the requester's current link, and
        schedule the next retry. The link is looked up fresh so a flapped-and-healed
        link resumes delivery."""
        pending = self._pending_results.get(job_id)
        if pending is None:
            return
        peer = self.peers.get(pending.to_node)
        if peer is not None and peer.linked:
            with contextlib.suppress(ConnectionError, OSError):
                peer.writer.write(protocol.encode(pending.msg))
        pending.next_retry = (time.monotonic()
                              + float(self.proto["foreignResultRetryIntervalSecs"]))

    def _retry_pending_results(self) -> None:
        """Heartbeat-tick maintenance: re-send any due unacked result; one whose
        deadline passed (the requester never acked — presumed gone) stops retrying
        but stays as a TOMBSTONE, so a later `job-reminder` from the requester can
        revive its delivery instead of finding nothing and banning us. Tombstones
        are bounded (count + age, see [_reap_foreign])."""
        now = time.monotonic()
        for job_id, pending in list(self._pending_results.items()):
            if pending.gave_up:
                continue
            if now >= pending.deadline:
                pending.gave_up = True
                activity.log("mesh", "mesh-dispatch-failed",
                             f"Mesh: gave up delivering job-result {job_id[:8]} "
                             "(unacked; kept for a reminder)")
            elif now >= pending.next_retry:
                self._send_pending(job_id)

    def _on_job_ack(self, msg: dict, writer: asyncio.StreamWriter) -> None:
        """A requester acknowledged a result we owed it — stop re-sending. Only the
        node we actually owe the result to may ack it (verified from the link)."""
        job_id = str(msg.get("id", ""))
        pending = self._pending_results.get(job_id)
        if pending is None:
            return
        peer = self._peer_by_writer(writer)
        if peer is None or peer.info.id != pending.to_node:
            return
        del self._pending_results[job_id]

    def _register_awaiting(self, job_id: str, node_id: str, duty: str,
                           prompt: str = "") -> None:
        """Record a remote dispatch we'll accept a later ``job-result`` for. Keeps
        the head of the prompt as context for a possible extension decision."""
        if (job_id not in self._awaiting_result
                and len(self._awaiting_result) >= _MAX_FOREIGN):
            oldest = min(self._awaiting_result,
                         key=lambda k: self._awaiting_result[k].added)
            self._awaiting_result.pop(oldest, None)
        self._awaiting_result[job_id] = _Awaiting(
            executor_id=node_id, duty=duty, added=time.monotonic(),
            prompt_head=prompt[:protocol.MAX_PROGRESS_NOTE_BYTES])

    def _result_authentic(self, msg: dict, peer: Peer) -> bool:
        """Whether a returned result is bound to the executor's key. A **keyed**
        executor MUST carry a `sig` verifying over the canonical `{id,node,result}`
        (so a third peer on the link — or a tamper in flight — can't forge it); a
        **keyless** executor carries none (accepted, still gated by the responder-link
        check in [_on_job_result])."""
        pubkey = peer.info.pubkey
        if not pubkey:
            return True
        sig = str(msg.get("sig", ""))
        payload = {"id": str(msg.get("id", "")), "node": str(msg.get("node", "")),
                   "result": msg.get("result")}
        return bool(sig) and crypto.verify(
            pubkey, protocol.result_signing_bytes(payload), sig)

    def _ack_result(self, job_id: str, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(ConnectionError, OSError):
            writer.write(protocol.encode(protocol.job_ack(job_id, self.local.id)))

    def _on_job_result(self, msg: dict, writer: asyncio.StreamWriter) -> None:
        """A foreign executor returned the artifact for a request we dispatched to
        it. Verify it's genuinely from that executor, ack it, and perform the social
        action ourselves — exactly once, under our own identity. A duplicate (the
        executor retried before our ack landed) is re-acked but never re-acted."""
        job_id = str(msg.get("id", ""))
        entry = self._awaiting_result.get(job_id)
        if entry is None:
            # Not an outstanding dispatch. If we already acted on it, re-ack so the
            # executor stops retrying; otherwise it's unsolicited — drop it.
            if job_id in self._acted_results:
                self._ack_result(job_id, writer)
            return
        executor_id, duty = entry.executor_id, entry.duty
        peer = self._peer_by_writer(writer)
        if peer is None or peer.info.id != executor_id:
            return  # only the node we dispatched to may return its result
        if not self._result_authentic(msg, peer):
            return  # forged/tampered result — drop, do NOT ack
        # Ack first (idempotent): stop the executor's retries whether this is the
        # first delivery or a duplicate.
        self._ack_result(job_id, writer)
        if job_id in self._acted_results:
            return  # already performed the action — never twice
        self._acted_results[job_id] = time.monotonic()
        self._awaiting_result.pop(job_id, None)
        result = msg.get("result")
        if not isinstance(result, dict):
            # A malformed (non-object) result is not a fulfilling answer. Coerce it
            # to an empty dict so it flows through the ok:false path (logged, not
            # acted on) instead of raising AttributeError on `.get` — a wire peer
            # must never be able to crash the link with a bad `result` type.
            result = {}
        if entry.reminded_at is not None and not bool(result.get("ok", False)):
            # We asked "is this ready?" after six-plus hours and the answer is a
            # failure — a response that does not fulfill the task is a broken
            # promise (docs/szpontnet/13#resolution-fulfilled-extended-or-banned).
            # (A timely ok:false, before any reminder, is an honest answer and is
            # handled below like always.)
            self._ban_executor(entry, job_id,
                               "reminder answered with a non-fulfilling result "
                               f"({result.get('error') or 'ok=false'})")
            return
        self._act_on_result(job_id, executor_id, duty, result)

    def _act_on_result(self, job_id: str, executor_id: str, duty: str,
                       result: dict) -> None:
        """Perform the social action for a returned foreign result under OUR identity
        (the reference hands it to ``DIPLOMAT_MESH_ON_RESULT``, where e.g. `gh` runs).
        A failed compute is logged, not acted on."""
        if not bool(result.get("ok", False)):
            activity.log("mesh", "mesh-dispatch-failed",
                         f"Mesh: {duty} from {self._node_name(executor_id)} returned "
                         f"no result ({result.get('error') or 'failed'})")
            return
        path = self._result_path(job_id, incoming=True)
        try:
            path.write_text(json.dumps({
                "jobId": job_id, "duty": duty, "from": executor_id,
                "output": str(result.get("output", ""))}), encoding="utf-8")
            spawnjob.run_result_handler(str(path))
        except (OSError, spawnjob.JobSpawnError) as exc:
            activity.log("mesh", "spawn-failed",
                         f"Mesh: result handler for {duty} failed: {exc}")
            return
        activity.log("mesh", "mesh-spawn",
                     f"Mesh: acting on {duty} result from "
                     f"{self._node_name(executor_id)} (under our identity)")

    # MARK: - foreign accountability (deadline → reminder → extension or ban)
    #
    # An acceptance is the only promise a foreign device ever makes, and this
    # section makes it binding (docs/szpontnet/13#accountability-deadline-
    # reminder-ban). ORIGINATOR side: a foreign `spawned` (without `direct`) arms
    # a completion deadline ([_maybe_arm_deadline]); the heartbeat tick checks it
    # ([_check_foreign_deadlines]); past it we send a `job-reminder`, and within
    # the grace window the executor either delivers (fulfilled), pleads
    # `job-progress` (judged by the extension decider — an agent's call), or is
    # BANNED ([_ban_executor]: persisted to banned.json, marked for the operator,
    # declined + excluded from dispatch from then on). EXECUTOR side: answer
    # reminders truthfully ([_on_job_reminder]) so honest lateness never bans us.

    def _check_foreign_deadlines(self) -> None:
        """Heartbeat-tick pass over the armed accountability entries."""
        now = time.monotonic()
        grace = float(self.proto["foreignReminderGraceSecs"])
        for job_id, aw in list(self._awaiting_result.items()):
            if aw.deadline is None or aw.deciding:
                continue
            peer = self.peers.get(aw.executor_id)
            if peer is not None and self._peer_trust(peer) == "personal":
                # Promoted mid-flight: trusting a device and holding it to the
                # foreign contract are contradictory — the promotion wins.
                aw.deadline = None
                continue
            if aw.reminded_at is None:
                if now >= aw.deadline:
                    self._send_reminder(job_id, aw)
            elif now >= aw.reminded_at + grace:
                self._ban_for_broken_promise(
                    job_id, "no response to readiness reminder")
            elif now >= aw.next_remind:
                # Re-ask across the grace window: delivery is best-effort and
                # links flap — a single send at the deadline instant would turn
                # an executor that reconnects mid-grace (still holding its
                # result tombstone) into a false silence ban.
                self._send_reminder(job_id, aw)

    def _send_reminder(self, job_id: str, aw: _Awaiting) -> None:
        """Ask the late executor "is this ready?". The grace clock starts at the
        FIRST ask whether or not the link is up — a device that accepted work and
        vanished is not excused by being unreachable — but the ask itself repeats
        on the result-retry cadence until the window resolves."""
        now = time.monotonic()
        first = aw.reminded_at is None
        if first:
            aw.reminded_at = now
        aw.next_remind = now + float(self.proto["foreignResultRetryIntervalSecs"])
        peer = self.peers.get(aw.executor_id)
        if peer is not None and peer.linked:
            with contextlib.suppress(ConnectionError, OSError):
                peer.writer.write(protocol.encode(
                    protocol.job_reminder(job_id, self.local.id)))
        if first:
            activity.log("mesh", "warn",
                         f"Mesh: reminding {self._node_name(aw.executor_id)} — "
                         f"{aw.duty} job {job_id[:8]} passed its completion deadline")

    def _on_job_reminder(self, msg: dict, writer: asyncio.StreamWriter) -> None:
        """Our requester asks whether its job is ready. Truthful answers only:
        the result if we computed it (reviving a given-up delivery), a progress
        note if it is still running, silence for a job we don't recognize (losing
        accepted work IS failing to deliver — the spec's call, and honest)."""
        job_id = str(msg.get("id", ""))
        peer = self._peer_by_writer(writer)
        if peer is None or not job_id:
            return
        pending = self._pending_results.get(job_id)
        if pending is not None:
            if peer.info.id != pending.to_node:
                return  # only the originator we owe the result to may ask
            # Computed already — re-arm delivery (the originator may have been
            # unreachable past our give-up window; it is clearly back now).
            pending.gave_up = False
            pending.deadline = (time.monotonic()
                                + float(self.proto["foreignResultMaxSecs"]))
            self._send_pending(job_id)
            activity.log("mesh", "mesh-spawn",
                         f"Mesh: reminded about job {job_id[:8]} — re-delivering "
                         "its result")
            return
        running = self._confined_running.get(job_id)
        if running is not None and running[0] == peer.info.id:
            elapsed = int(time.monotonic() - running[1])
            budget = int(float(self.proto["foreignJobTimeoutSecs"]))
            with contextlib.suppress(ConnectionError, OSError):
                writer.write(protocol.encode(protocol.job_progress(
                    job_id, self.local.id,
                    f"confined compute still running ({elapsed}s elapsed of "
                    f"{budget}s budget)")))

    def _on_job_progress(self, msg: dict, writer: asyncio.StreamWriter) -> None:
        """A late executor pleads "still working". Only meaningful from the exact
        executor of a job we reminded; the plea goes to the extension decider —
        never taken at face value."""
        job_id = str(msg.get("id", ""))
        aw = self._awaiting_result.get(job_id)
        if aw is None or aw.deadline is None or aw.reminded_at is None or aw.deciding:
            return
        peer = self._peer_by_writer(writer)
        if peer is None or peer.info.id != aw.executor_id:
            return
        note = (str(msg.get("note", "")).encode("utf-8", "surrogatepass")
                [:protocol.MAX_PROGRESS_NOTE_BYTES].decode("utf-8", "ignore"))
        aw.deciding = True
        task = asyncio.get_running_loop().create_task(
            self._decide_extension(job_id, note))
        self._result_tasks.add(task)
        task.add_done_callback(self._result_task_done)

    async def _decide_extension(self, job_id: str, note: str) -> None:
        """Judge a late executor's plea: hand the case to the operator's extension
        decider (an agent — DIPLOMAT_MESH_EXTEND_DECIDER); exit 0 re-arms the full
        deadline window, anything else — including no decider configured, a crash,
        or a timeout — bans. The verdict is the originator's alone."""
        aw = self._awaiting_result.get(job_id)
        if aw is None:
            return
        decider = config.extend_decider()
        granted = False
        if decider:
            try:
                granted = await self._run_extend_decider(decider, job_id, aw, note)
            except Exception as exc:  # noqa: BLE001 — a crashed decider grants nothing;
                # the decision must still conclude (extend or ban), or `deciding`
                # would wedge the entry past every check until the reap backstop.
                activity.log("mesh", "warn",
                             f"Mesh: extension decider crashed: {exc!r}")
                granted = False
        # Re-fetch: the result may have arrived (entry resolved) while we judged.
        aw = self._awaiting_result.get(job_id)
        if aw is None:
            return
        aw.deciding = False
        if granted:
            aw.extensions += 1
            aw.reminded_at = None
            aw.deadline = (time.monotonic()
                           + float(self.proto["foreignCompletionDeadlineSecs"]))
            activity.log("mesh", "mesh-spawn",
                         f"Mesh: extension granted to {self._node_name(aw.executor_id)} "
                         f"for {aw.duty} job {job_id[:8]} (#{aw.extensions}) — "
                         f"plea: {note[:120]}")
        else:
            cause = ("still incomplete at reminder and no extension decider "
                     "configured" if not decider else
                     "extension declined by the decider")
            self._ban_for_broken_promise(job_id, f"{cause}; plea: {note[:200]}")

    async def _run_extend_decider(self, decider: str, job_id: str, aw: _Awaiting,
                                  note: str) -> bool:
        """Run the decider on the case file; True = extend. Bounded by the grace
        window so a hung decider can't stall the ban forever."""
        peer = self.peers.get(aw.executor_id)
        case_path = self._result_path(job_id).with_name(f"extend-{job_id}.json")
        now = time.monotonic()
        case = {
            "jobId": job_id,
            "duty": aw.duty,
            "prompt": aw.prompt_head,
            "executor": {
                "node": aw.executor_id,
                "name": self._node_name(aw.executor_id),
                "fingerprint": (peer.verified_fp or "") if peer else "",
            },
            "acceptedSecsAgo": round(now - aw.added, 1),
            "extensionsGranted": aw.extensions,
            "progressNote": note,
        }
        try:
            case_path.write_text(json.dumps(case, indent=2), encoding="utf-8")
        except OSError:
            return False
        cmd = decider.replace("{job_file}", str(case_path))
        try:
            proc = await asyncio.create_subprocess_shell(
                cmd, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            rc = await asyncio.wait_for(
                proc.wait(), timeout=float(self.proto["foreignReminderGraceSecs"]))
            return rc == 0
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.kill()
            activity.log("mesh", "warn",
                         f"Mesh: extension decider timed out for job {job_id[:8]}")
            return False
        except OSError as exc:
            activity.log("mesh", "warn",
                         f"Mesh: extension decider failed to run: {exc}")
            return False

    def _ban_for_broken_promise(self, job_id: str, cause: str) -> None:
        aw = self._awaiting_result.pop(job_id, None)
        if aw is not None:
            self._ban_executor(aw, job_id, cause)

    def _ban_executor(self, aw: _Awaiting, job_id: str, cause: str) -> None:
        """The accountability verdict: mark the device banned, for the operator.
        Never fires on a device now classified personal (the promotion wins), and
        one broken promise resolves every entry we still held for the device."""
        peer = self.peers.get(aw.executor_id)
        if peer is not None and self._peer_trust(peer) == "personal":
            return
        # Record the same identity _peer_banned later checks: verified key first,
        # else the ADVERTISED key (signed adverts mean the peer actually holds it,
        # and a deny-side mark on a claimed identity only ever costs the claimant),
        # else keyless → node id. Recording only the verified key would let a
        # keyed-but-never-authed executor slip its own ban on the next check.
        # If the peer was already reaped, fall back to the fingerprint we pinned when
        # the deadline was armed (executor_fp) — otherwise a keyed executor that goes
        # silent gets an id-only ban that its own key defeats on reconnect.
        fp = ((peer.verified_fp or crypto.fingerprint_of(peer.info.pubkey))
              if peer else aw.executor_fp)
        label = peer.info.name if peer else ""
        reason = (f"accepted SzpontRequest {job_id[:8]} ({aw.duty}) "
                  f"and failed to deliver: {cause}")
        self.ban_device(fp, aw.executor_id, label=label, reason=reason,
                        job_id=job_id)
        for jid, other in list(self._awaiting_result.items()):
            if other.executor_id == aw.executor_id:
                self._awaiting_result.pop(jid, None)
        self._flush_state()  # the operator's panel shows the mark immediately

    def _reap_foreign(self) -> None:
        """Expire stale foreign bookkeeping. An awaited result may take the whole
        confined compute budget plus the delivery window to arrive, so its TTL spans
        both; an acted-on id only needs to outlive the executor's retry window."""
        now = time.monotonic()
        # The awaited entry must outlive the executor's whole worst case: the full
        # confined compute budget, then the retry-until-give-up delivery window —
        # PLUS a margin, so a result delivered right at the executor's give-up edge
        # isn't reaped the same tick and dropped as unsolicited (a zero-margin TTL
        # races the last legitimate re-send).
        await_ttl = (float(self.proto["foreignJobTimeoutSecs"])
                     + float(self.proto["foreignResultMaxSecs"])
                     + float(self.proto["peerTimeoutSecs"]))
        acted_ttl = float(self.proto["foreignResultMaxSecs"])
        grace = float(self.proto["foreignReminderGraceSecs"])
        margin = float(self.proto["peerTimeoutSecs"])

        def keep_awaiting(aw: _Awaiting) -> bool:
            if aw.deadline is None:
                return now - aw.added < await_ttl
            # An ARMED entry lives until its accountability cycle resolves it
            # (fulfilled / extended / banned) — the deadline check owns it. The
            # age bound here is only a backstop against a wedged decision; the
            # deadline moves on every extension, so it self-renews.
            return now < aw.deadline + 2 * grace + margin

        self._awaiting_result = {k: v for k, v in self._awaiting_result.items()
                                 if keep_awaiting(v)}
        self._acted_results = {k: t for k, t in self._acted_results.items()
                               if now - t < acted_ttl}
        # Result tombstones (gave-up deliveries kept for reminder revival): bound
        # by the accountability window an originator could still ask within, and
        # by count — newest first, since a reminder asks about the recent past.
        tomb_ttl = (float(self.proto["foreignCompletionDeadlineSecs"])
                    + grace + margin)
        self._pending_results = {k: p for k, p in self._pending_results.items()
                                 if now - p.created < tomb_ttl}
        tombs = sorted((k for k, p in self._pending_results.items() if p.gave_up),
                       key=lambda k: self._pending_results[k].created)
        for job_id in tombs[:-_MAX_TOMBSTONES] if len(tombs) > _MAX_TOMBSTONES else []:
            self._pending_results.pop(job_id, None)

    # MARK: - control sessions (panel / CLI)

    async def _run_ctl(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                msg = protocol.decode(line)
                if not msg:
                    continue
                reply = await self._ctl_command(msg)
                writer.write(protocol.encode(reply))
                await writer.drain()
        except (ConnectionError, OSError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            writer.close()

    async def _ctl_command(self, msg: dict) -> dict:
        t = msg.get("t")
        if t == "status":
            # Stamp the live snapshot with the same updatedAt/pid/v envelope the
            # on-disk state.json carries, so the control reply is byte-identical to
            # what a disk reader sees (08-state promises the two are one object).
            return {"t": "state", "state": statefile.stamp(self.snapshot())}
        if t == "set-attr":
            self._on_set_attr(msg)
            self._flush_state()
            return {"t": "ok"}
        if t == "set-overrides":
            duty = str(msg.get("duty", ""))
            placement = msg.get("placement")
            if duty in config.duty_ids() and isinstance(placement, dict):
                self.set_overrides_duty(duty, placement)
                self._flush_state()
                return {"t": "ok"}
            return {"t": "error", "reason": f"unknown duty {duty!r}"}
        if t == "dispatch":
            duty = str(msg.get("duty", ""))
            if duty not in config.duty_ids():
                return {"t": "error", "reason": f"unknown duty {duty!r}"}
            target = msg.get("target")
            target = str(target) if target else None
            # A control client may present the target server's API key per request
            # (forwarded on the outbound dispatch); absent, the node's own env key
            # is used.
            api_key = str(msg.get("apiKey", "")) or None
            # Optional origination-dedup key: when present the node claims it first
            # and reports `suppressed` if a better peer already owns the work.
            work_key = str(msg.get("workKey", ""))
            results = await self.dispatch(duty, str(msg.get("prompt", "")),
                                          target, api_key, work_key)
            return {"t": "dispatch-result", "duty": duty, "results": results}
        if t == "claim":
            # The origination claim gate, stand-alone (docs/szpontnet/12): a client
            # that will run the work ITSELF (e.g. the applet's auto-monitor spawning
            # a local, tracked agent) claims the key without dispatching. `owned`
            # False → a better live personal peer holds the lease; don't originate.
            work_key = str(msg.get("workKey", "")).strip()
            if not work_key:
                return {"t": "error", "reason": "claim needs a workKey"}
            owned = self.claim(work_key)
            holder = self._claim_holder(work_key)
            return {"t": "claim-result", "owned": owned, "owner": holder,
                    "ownerName": self._node_name(holder) if holder else None}
        if t == "trust":
            fp = str(msg.get("fingerprint", "")).strip()
            if not fp:
                return {"t": "error", "reason": "trust needs a fingerprint"}
            self.add_trusted(fp, str(msg.get("label", "")))
            self._flush_state()
            return {"t": "ok"}
        if t == "untrust":
            self.remove_trusted(str(msg.get("fingerprint", "")).strip())
            self._flush_state()
            return {"t": "ok"}
        if t == "ban":
            fp = str(msg.get("fingerprint", "")).strip()
            node_id = str(msg.get("node", "")).strip()
            if not fp and not node_id:
                return {"t": "error", "reason": "ban needs a fingerprint or node"}
            self.ban_device(fp, node_id, label=str(msg.get("label", "")),
                            reason=str(msg.get("reason", "")) or "manual")
            self._flush_state()
            return {"t": "ok"}
        if t == "unban":
            fp = str(msg.get("fingerprint", "")).strip()
            node_id = str(msg.get("node", "")).strip()
            if not fp and not node_id:
                return {"t": "error", "reason": "unban needs a fingerprint or node"}
            if not self.unban_device(fp, node_id):
                return {"t": "error", "reason": "no matching ban"}
            self._flush_state()
            return {"t": "ok"}
        if t == "set-default-trust":
            level = str(msg.get("level", "")).strip().lower()
            if not self.set_default_trust(level):
                return {"t": "error", "reason": "level must be 'personal' or 'foreign'"}
            self._flush_state()
            return {"t": "ok"}
        if t == "tor-connect":
            # Manual paste: initiate a Tor link to a peer's onion, even one we never
            # met on the LAN. Dials unconditionally (bypasses smaller-id-dials), as a
            # background one-shot so this reply returns immediately — the operator
            # watches --status for the peer to appear.
            onion = tor.normalize_onion(msg.get("onion"))
            if not onion:
                return {"t": "error",
                        "reason": "tor-connect needs a valid v3 onion address"}
            if self.tor is None or self.tor.onion_address() is None:
                return {"t": "error",
                        "reason": "the Tor transport is not enabled or not ready "
                                  "on this node (set DIPLOMAT_MESH_TOR=1)"}
            task = asyncio.get_running_loop().create_task(
                self._tor_dial(onion), name="mesh-tor-connect")
            self._dial_tasks.add(task)
            task.add_done_callback(self._dial_tasks.discard)
            return {"t": "ok", "onion": onion}
        if t == "stop":
            self.request_stop()
            return {"t": "ok"}
        return {"t": "error", "reason": f"unknown command {t!r}"}

    # MARK: - trust allowlist (operator-managed, local, never gossiped)

    def add_trusted(self, fingerprint: str, label: str = "") -> None:
        self._trusted[fingerprint] = label
        # Persist the operator's EXPLICIT default-trust choice (what is already on
        # disk), NOT self._default_trust. At boot self._default_trust is resolved as
        # `load_default_level() or config.default_trust()`, so when the operator has
        # never toggled the default it holds the env/mesh.json BASELINE. Writing that
        # baseline here as `defaultLevel` would pin it — a later
        # DIPLOMAT_MESH_DEFAULT_TRUST change (e.g. a foreign lockdown) is then silently
        # shadowed forever, since the persisted value wins over env at the next boot.
        # `load_default_level()` is "" unless the operator explicitly set the default
        # (set_default_trust), so an allowlist edit never converts a baseline into an
        # authoritative persisted choice.
        trust.save(self._trusted, trust.load_default_level())
        # An explicit promotion is the operator's newest word — it lifts any ban
        # (trusted and banned are mutually exclusive states).
        if self.unban_device(fingerprint):
            activity.log("mesh", "mesh-up",
                         f"Mesh: promotion lifted the ban on {fingerprint[:16]}")
        activity.log("mesh", "mesh-up",
                     f"Mesh: trusting device {fingerprint[:16]}"
                     f"{' (' + label + ')' if label else ''}")

    def remove_trusted(self, fingerprint: str) -> None:
        if self._trusted.pop(fingerprint, None) is not None:
            # Preserve only the operator's EXPLICIT persisted default (see add_trusted):
            # an allowlist edit must never pin the env/mesh.json baseline as a choice.
            trust.save(self._trusted, trust.load_default_level())
            activity.log("mesh", "mesh-up", f"Mesh: untrusting device {fingerprint[:16]}")

    def set_default_trust(self, level: str) -> bool:
        """Set the trust level applied to UNKNOWN devices (the panel's default-trust
        toggle). Persisted in ``trusted.json`` so it survives a restart, and applied
        live: every unlisted/unverified peer re-classifies on the next snapshot.
        Returns False for an unrecognised level (caller reports the error)."""
        if level not in ("personal", "foreign") or level == self._default_trust:
            return level in ("personal", "foreign")
        self._default_trust = level
        trust.save(self._trusted, self._default_trust)
        activity.log("mesh", "mesh-up", f"Mesh: default trust level for new devices → {level}")
        return True

    # MARK: - ban list (accountability verdicts + operator-managed, local, never gossiped)

    def ban_device(self, fingerprint: str, node_id: str = "", label: str = "",
                   reason: str = "", job_id: str = "") -> None:
        """Mark a device banned (the automatic accountability verdict, or the
        operator's manual mark). The newest word wins: a banned fingerprint can't
        stay on the trusted allowlist."""
        if fingerprint and fingerprint in self._trusted:
            self.remove_trusted(fingerprint)
        self._banned = banned.add(
            self._banned,
            banned.entry(fingerprint, node_id, label=label, reason=reason,
                         job_id=job_id))
        banned.save(self._banned)
        who = label or (node_id[:8] if node_id else fingerprint[:16])
        activity.log("mesh", "warn", f"Mesh: BANNED device {who} — {reason or 'manual'}")

    def unban_device(self, fingerprint: str = "", node_id: str = "") -> bool:
        """Lift a ban (the operator's recovery path). True if one matched."""
        self._banned, removed = banned.remove(self._banned, fingerprint, node_id)
        if removed:
            banned.save(self._banned)
            activity.log("mesh", "mesh-up",
                         f"Mesh: unbanned device "
                         f"{fingerprint[:16] or node_id[:8]}")
        return removed

    # MARK: - snapshot

    def _flush_state(self) -> None:
        """Persist the snapshot to the statefile NOW.

        A control-channel edit (set-attr / set-overrides / trust) is applied to
        memory synchronously, but the UI only ever sees state via the statefile.
        Without an immediate flush the change wouldn't land on disk until the
        next `_snapshot_loop` write (up to stateWriteIntervalSecs away), so the
        panel's post-reply re-read would show stale values. Flushing here makes a
        local edit visible the instant the ctl reply returns."""
        statefile.write_state(self.snapshot())

    def snapshot(self) -> dict:
        stale, timeout = self.proto["peerStaleSecs"], self.proto["peerTimeoutSecs"]
        now = time.monotonic()
        peers = []
        for p in sorted(self.peers.values(), key=lambda p: (p.info.name, p.info.id)):
            d = p.info.to_dict()
            d["link"] = p.link_state(stale, timeout)
            d["addr"] = p.addr
            d["lastSeenSecsAgo"] = round(now - p.last_seen, 1)
            # This node's view of the peer: whether it PROVED a key (verified), the
            # fingerprint it proved (or merely claims, if unverified), its trust
            # classification against the local allowlist, and its dispatch surplus.
            d["verified"] = p.verified_fp is not None
            d["fingerprint"] = p.verified_fp or crypto.fingerprint_of(p.info.pubkey)
            # Which transport the current link runs over ("lan" | "tor"), tracked
            # per link at bind time — accurate for both inbound and outbound Tor.
            d["transport"] = p.transport if p.linked else "lan"
            d["trust"] = self._peer_trust(p)
            d["surplus"] = round(p.info.surplus(), 3)
            # Real connection uptime for the badge (seconds since the link came up);
            # None while down, so the UI shows "last seen" instead.
            d["uptimeSecs"] = (round(now - p.linked_since, 1)
                               if p.linked and p.linked_since is not None else None)
            peers.append(d)
        me = self.info.to_dict()
        me["fingerprint"] = self.fingerprint
        me["uptimeSecs"] = round(time.time() - self.epoch, 1)  # how long this node has run
        return {
            "tcpPort": self.tcp_port,
            "self": me,
            "peers": peers,
            # How many peers are mid-handshake right now — drives the "scanning the
            # LAN" affordance so a slow first link isn't a silent 20s of nothing.
            "linking": len(self._dialing),
            # True while every beacon send fails (the node is undiscoverable —
            # e.g. an OS privacy gate); lets a UI say so instead of showing an
            # inexplicably empty mesh. `beaconBlockReason` carries WHY, so the banner
            # shows the same diagnosis the log does: "local-network" (an OS/firewall
            # gate the operator can fix) or "network-down" (the stack is gone); "" up.
            "beaconBlocked": self._beacon_blocked,
            "beaconBlockReason": self._beacon_block_reason,
            # The Tor transport's state: whether it's enabled, whether the onion
            # service is live yet, and this node's permanent onion (also on `self`).
            # Lets a UI show WAN reachability and give the operator an address to
            # share for a manual `tor-connect`.
            "tor": {
                "enabled": config.tor_enabled(),
                "ready": self.tor is not None and self.tor.onion_address() is not None,
                "onion": self.tor.onion_address() if self.tor is not None else None,
            },
            "trusted": [{"fingerprint": fp, "label": lbl}
                        for fp, lbl in sorted(self._trusted.items())],
            # The local ban list, mirrored read-only (like `trusted`) so the
            # operator's UI shows who was marked banned and why. Never gossiped.
            "banned": [dict(e) for e in self._banned],
            # Trust level applied to an UNKNOWN device (not in `trusted`): 'foreign'
            # by default (a new device is untrusted until promoted), or 'personal'
            # for a full-trust mesh. Drives the panel's default-trust toggle.
            "defaultTrust": self._default_trust,
            "assignments": {k: a.to_dict() for k, a in self._assignments.items()},
            "overrides": self.overrides.to_dict(),
            # Active origination leases this node currently observes: work_key →
            # owning node id (the lowest-id live+personal active claimant). Lets a
            # UI/CLI show what work is already spoken for. Only owned keys appear.
            "claims": self._claim_owners(),
            # Foreign zero-trust request/response in flight (docs/szpontnet/13):
            # results we've computed and owe back to a foreign requester (unacked),
            # and remote dispatches we're still willing to receive a result for.
            "foreign": {"pendingResults": len(self._pending_results),
                        "awaiting": len(self._awaiting_result)},
        }

    async def _snapshot_loop(self) -> None:
        while True:
            # Age the local accounting so the displayed usageAvg/quota decay even
            # while idle (local only — no gossip churn; peers hear on real change).
            self.stats = self.stats.decayed(time.time())
            # Re-probe the budget on a throttle (not every 2s write); if what peers
            # see changed (state flip, or a remaining-percentage moved), tell them
            # so their consoles and load balancing stay current.
            if time.monotonic() - self._last_token_refresh >= _TOKEN_REFRESH_SECS:
                self._last_token_refresh = time.monotonic()
                if await self._refresh_tokens():
                    self._bump_and_gossip()
                    self._recompute("token state")
            statefile.write_state(self.snapshot())
            await asyncio.sleep(self.proto["stateWriteIntervalSecs"])
