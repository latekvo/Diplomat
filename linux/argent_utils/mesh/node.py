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
- a **snapshot** task mirrors the topology to ``~/.argent/mesh/state.json``
  every couple of seconds for the UIs.

Peers stay visible in the snapshot for a few minutes after going down (link
``"down"``) so the topology panel shows *what* died rather than a silently
shrinking list.
"""

from __future__ import annotations

import asyncio
import contextlib
import secrets
import socket
import struct
import time
import uuid
from collections.abc import Callable
from dataclasses import replace

from .. import activity
from . import (
    assign, config, crypto, identity, peercache, protocol, spawnjob, statefile,
    stats, trust, usage,
)
from .config import PlacementOverrides
from .protocol import Job, NodeInfo

# How long a dead peer stays in the snapshot (link "down") before it's dropped.
_DOWN_RETENTION_SECS = 300.0

# How often the node re-measures real token usage from the local logs. The budget
# doesn't move second-to-second, and scanning ~/.claude costs real file I/O, so this
# is decoupled from the (fast) snapshot cadence rather than run every write.
_TOKEN_REFRESH_SECS = 30.0

# A sane home/office LAN has a handful of machines; this only bounds a beacon
# flood of spoofed ids from ballooning the peers table + snapshot, not real use.
_MAX_PEERS = 256

# Upper bound on stored work-claim records (work_key × claimant). Real use holds a
# handful of in-flight work items; this only stops a gossip flood of spoofed
# work_keys from ballooning the claim book without bound. See _store_claim.
_MAX_CLAIMS = 4096

# Domain-separation prefix for the trust proof-of-possession signature. The peer
# signs this tag + the challenge nonce, never the bare nonce — so a captured
# signature is meaningless outside SzpontNet's auth exchange and the device key
# can't be coaxed into acting as a general signing oracle over attacker-chosen
# bytes. The exact byte construction is normative (see docs/szpontnet/11).
_AUTH_CONTEXT = b"szpontnet-auth-v1:"


def _auth_challenge(nonce: str) -> bytes:
    """The exact bytes signed/verified for a proof-of-possession `auth`:
    the domain tag followed by the UTF-8 challenge nonce."""
    return _AUTH_CONTEXT + nonce.encode()


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
        self._trusted = trust.load()  # local allowlist of trusted fingerprints
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
        # Whether the last beacon tick failed EVERY send — the node is then
        # undiscoverable and says so (activity feed + snapshot) instead of failing
        # silently. Flips back on the first successful send. See _note_beacon_sends.
        self._beacon_blocked = False
        # The trust challenge nonce THIS node issued on each link, keyed by writer,
        # so an inbound `auth` can be verified against the nonce we chose.
        self._issued_nonce: dict[asyncio.StreamWriter, str] = {}
        # Work-claim book: work_key -> {claimant node id -> freshest ClaimRecord},
        # plus our own per-key seq counter. A claim is an origination lease; the
        # owner of a key is the lowest-id live+personal active claimant. See the
        # work-claims section below and docs/szpontnet/12-work-claims.md.
        self._claims: dict[str, dict[str, protocol.ClaimRecord]] = {}
        self._claim_seq: dict[str, int] = {}
        # Optional hook fired when a better (lower-id) peer preempts a work_key this
        # node was originating, so a caller (e.g. an auto-poller) can abort the
        # local work it started. None = no-op (the default; origination is manual).
        self.on_claim_lost: Callable[[str], None] | None = None

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
            stats=self.stats.advertise(),
        )
        return self._sign_advert(info)

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

    def _gossiped_tokens(self) -> tuple:
        """What peers currently know of our token budget, at wire granularity —
        the change-detection key for re-gossiping."""
        def r(v: float | None) -> float | None:
            return None if v is None else round(v, 2)
        return (self.current_tokens(), r(self._token_frac),
                r(self._token_session), r(self._token_week))

    async def _refresh_tokens(self) -> bool:
        """Re-probe the token budget (real quota endpoint, else local-log heuristic)
        and recompute the auto state + remaining fractions. Returns True when what
        peers see changed — the state flipped or a percentage moved — so the caller
        can re-gossip. Runs the probe in a worker thread: it may touch the network
        (~1s, worst case a few seconds' timeout) and must not stall the event loop."""
        before = self._gossiped_tokens()
        try:
            state, frac, session, week = await asyncio.to_thread(
                usage.token_state, self.stats.plan)
        except Exception:  # noqa: BLE001 — a broken probe must never take the node down
            state, frac = self._token_state, self._token_frac
            session, week = self._token_session, self._token_week
        self._token_state, self._token_frac = state, frac
        self._token_session, self._token_week = session, week
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
        await self._start_tcp()
        self._start_udp(loop)
        self._tasks = [
            loop.create_task(self._beacon_loop(), name="mesh-beacon"),
            loop.create_task(self._redial_loop(), name="mesh-redial"),
            loop.create_task(self._heartbeat_loop(), name="mesh-heartbeat"),
            loop.create_task(self._snapshot_loop(), name="mesh-snapshot"),
        ]
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
        self._dial_tasks.clear()
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
        still work, so the node otherwise looks healthy. Logged only on the
        blocked/unblocked transition, never per tick."""
        blocked = sent == 0
        if blocked == self._beacon_blocked:
            return
        self._beacon_blocked = blocked
        if blocked:
            hint = (" On macOS check System Settings → Privacy & Security → "
                    "Local Network (Python must be allowed)."
                    if self.platform == "macos" else "")
            activity.log("mesh", "warn",
                         f"Mesh: every beacon send is failing ({err}) — this node "
                         f"is undiscoverable until sends recover.{hint}")
        else:
            activity.log("mesh", "mesh-up", "Mesh: beacon sending recovered")

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
        if not isinstance(tcp_port, int) or tcp_port <= 0:
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
            except (TypeError, ValueError):
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
            except (OSError, asyncio.TimeoutError):
                return  # next beacon retries
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
        only on change — a steady link costs no I/O."""
        if not addr or tcp_port <= 0:
            return
        entry = (addr, tcp_port)
        if self._peer_cache.get(peer_id) == entry:
            return
        self._peer_cache[peer_id] = entry
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
        # The join fence: with ARGENT_MESH_SECRET set, an opener (peer OR control
        # client) that doesn't present the token gets silently dropped.
        if first.get("t") in ("ctl", "hello") and \
                str(first.get("secret", "")) != config.secret():
            writer.close()
            return
        if first.get("t") == "ctl":
            # A server configured with an API key requires it on the opening ctl,
            # on top of the join secret: the secret admits mesh members, the key
            # authenticates who may drive/submit work to this node.
            if config.api_key() and str(first.get("apiKey", "")) != config.api_key():
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
            self._on_message(first, host, writer)
            await self._run_link(reader, writer, host, authenticated=True)
            return
        writer.close()

    async def _run_link(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: str,
        authenticated: bool,
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
        try:
            while True:
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
                got = self._on_message(msg, host, writer)
                if got and peer_id is None:
                    peer_id = got
        except (ConnectionError, OSError, asyncio.LimitOverrunError, ValueError):
            pass
        finally:
            self._issued_nonce.pop(writer, None)
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
        if t == "hello" and str(msg.get("secret", "")) != config.secret():
            # A dialed "peer" that can't present the join token isn't one of
            # ours — tear the link down (ValueError ends _run_link's pump).
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
        return not key or str(msg.get("apiKey", "")) == key

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
                level = trust.classify(fp, self._trusted)
                activity.log("mesh", "mesh-peer-up",
                             f"Mesh: verified {peer.info.name} device {fp[:16]} ({level})")

    def _peer_trust(self, peer: Peer | None) -> str:
        """personal vs foreign for a peer, from its VERIFIED fingerprint against
        the local allowlist (an unverified peer has no fingerprint -> foreign
        whenever an allowlist is configured)."""
        fp = peer.verified_fp if peer else None
        return trust.classify(fp or "", self._trusted)

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
        peer.last_seen = time.monotonic()
        peer.down_since = None
        if link_writer is not None:
            if peer.writer is not None and peer.writer is not link_writer:
                # Duplicate link (dial race despite the id rule, or a zombie):
                # keep the new one, close the old quietly.
                with contextlib.suppress(Exception):
                    peer.writer.close()
            if peer.linked_since is None:
                peer.linked_since = time.monotonic()  # link came up: start the uptime clock
            peer.writer = link_writer
            # A hello on the peer's own link is the one authenticated source of a
            # dialable address (source IP + the listen port it advertises).
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
        if int(raw.get("rev", 0)) <= 0:
            return True
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
        if cur is None and sum(len(b) for b in self._claims.values()) >= _MAX_CLAIMS:
            if not book:
                self._claims.pop(rec.work_key, None)  # don't leave an empty book
            return False
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
        return bool(rec.pubkey) and rec.pubkey == peer.info.pubkey

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
        nodes = self._alive_nodes()
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
            slots = [("target", [target])]
        else:
            # Origination dedup: only the leaderless surplus-first path can race a
            # peer to the same external event, so the claim gate lives here (not on
            # the server/target paths). Suppressed → report the current owner and
            # do not dispatch, so two nodes don't both run the same work.
            if work_key and not self.claim(work_key):
                holder = self._claim_holder(work_key)
                name = self._node_name(holder) if holder else None
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
                status, reason = await self._dispatch_to(node_id, duty_id, prompt, req_key)
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
                           api_key: str = "") -> tuple[str, str]:
        job = Job(id=uuid.uuid4().hex, duty=duty_id, prompt=prompt,
                  requested_by=self.local.id, requested_at=time.time())
        if node_id == self.local.id:
            return self._run_local_request(job, "personal")  # dispatching to myself
        peer = self.peers.get(node_id)
        if peer is None or not peer.linked:
            return "failed", "no link"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._job_futures[job.id] = (fut, node_id)
        try:
            peer.writer.write(protocol.encode(protocol.dispatch(job, api_key)))
            await peer.writer.drain()
            msg = await asyncio.wait_for(fut, timeout=self.proto["dispatchAckTimeoutSecs"])
            return str(msg.get("status", "failed")), str(msg.get("reason", ""))
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return "failed", "peer did not answer"
        finally:
            self._job_futures.pop(job.id, None)

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

    def _admit(self, job: Job, trust_level: str) -> tuple[bool, str]:
        """Refusal policy — the receiving node's own call, no consensus needed.
        A declined job fails the dispatcher's slot over exactly like a dead one.

        ``trust_level`` is the requester's classification from the **verified
        link** ([_peer_trust]) — never from anything in the job, which is
        spoofable. v1 refuses when:
        - the requester's device is **foreign** (its proven key isn't in our
          allowlist, or it proved no key). The zero-trust path — run the compute
          but route any social action back through a personal node — is not built
          yet, so we decline rather than act on a stranger's behalf.
        - we have this duty **disabled** locally (opted out of the class of work).
        - we are **out of tokens** (can't serve — this is Bob refusing the job
          Alice sent anyway, which the protocol expressly allows).
        """
        if trust_level == "foreign":
            return False, "foreign device (zero-trust path not implemented)"
        if not self.local.duty_enabled(job.duty):
            return False, f"duty {job.duty} disabled here"
        if self.current_tokens() == "out":
            return False, "out of tokens"
        return True, ""

    def _run_local_request(self, job: Job, trust_level: str) -> tuple[str, str]:
        """Admit-or-decline, then run locally. Shared by the remote-receive path
        (``_take_job``) and a local/self dispatch, so both apply the same policy."""
        admit, reason = self._admit(job, trust_level)
        if not admit:
            activity.log("mesh", "mesh-dispatch-failed",
                         f"Mesh: declined {job.duty} from "
                         f"{self._node_name(job.requested_by)} — {reason}")
            return "declined", reason
        return self._spawn_local(job)

    def _take_job(self, job: Job, writer: asyncio.StreamWriter) -> None:
        """A peer asked us to run a SzpontRequest. Classify the requester from the
        VERIFIED link (not the job's self-reported requestedBy), admit-or-decline,
        and answer with the outcome so the dispatcher can act on it."""
        trust_level = self._peer_trust(self._peer_by_writer(writer))
        status, out_reason = self._run_local_request(job, trust_level)
        with contextlib.suppress(ConnectionError, OSError):
            writer.write(protocol.encode(
                protocol.job_status(job.id, status, out_reason, self.local.id)
            ))

    def _record_usage(self, units: float) -> None:
        """Book quota against this node's accounting and re-advertise the fresher
        surplus so the mesh's load balancing tracks real consumption."""
        self.stats = stats.record(self.stats, units)
        stats.save(self.stats)
        self._bump_and_gossip()

    def _spawn_local(self, job: Job) -> tuple[str, str]:
        try:
            spawnjob.spawn_job(job.prompt)
        except spawnjob.JobSpawnError as exc:
            activity.log("mesh", "spawn-failed", f"Mesh job {job.duty} failed here: {exc}")
            return "failed", str(exc)
        self._record_usage(config.job_cost_units())
        activity.log("mesh", "mesh-spawn",
                     f"Mesh: running {job.duty} (from {self._node_name(job.requested_by)})")
        return "spawned", ""

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
        if t == "stop":
            self.request_stop()
            return {"t": "ok"}
        return {"t": "error", "reason": f"unknown command {t!r}"}

    # MARK: - trust allowlist (operator-managed, local, never gossiped)

    def add_trusted(self, fingerprint: str, label: str = "") -> None:
        self._trusted[fingerprint] = label
        trust.save(self._trusted)
        activity.log("mesh", "mesh-up",
                     f"Mesh: trusting device {fingerprint[:16]}"
                     f"{' (' + label + ')' if label else ''}")

    def remove_trusted(self, fingerprint: str) -> None:
        if self._trusted.pop(fingerprint, None) is not None:
            trust.save(self._trusted)
            activity.log("mesh", "mesh-up", f"Mesh: untrusting device {fingerprint[:16]}")

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
            # inexplicably empty mesh.
            "beaconBlocked": self._beacon_blocked,
            "trusted": [{"fingerprint": fp, "label": lbl}
                        for fp, lbl in sorted(self._trusted.items())],
            "assignments": {k: a.to_dict() for k, a in self._assignments.items()},
            "overrides": self.overrides.to_dict(),
            # Active origination leases this node currently observes: work_key →
            # owning node id (the lowest-id live+personal active claimant). Lets a
            # UI/CLI show what work is already spoken for. Only owned keys appear.
            "claims": self._claim_owners(),
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
