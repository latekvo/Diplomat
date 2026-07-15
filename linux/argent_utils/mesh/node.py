"""The mesh node: discovery, peer links, gossip, duty failover, job dispatch.

One asyncio event loop drives everything:

- a **beacon** task adverts this node over UDP (multicast + subnet broadcast —
  receivers dedupe; Wi-Fi APs regularly eat one or the other);
- a UDP listener learns peers from their beacons and **dials** the ones whose
  id sorts above ours (the deterministic smaller-id-dials rule, so exactly one
  TCP link exists per pair);
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
import socket
import struct
import time
import uuid

from .. import activity
from . import assign, config, identity, protocol, spawnjob, statefile, stats
from .config import PlacementOverrides
from .protocol import Job, NodeInfo

# How long a dead peer stays in the snapshot (link "down") before it's dropped.
_DOWN_RETENTION_SECS = 300.0

# A sane home/office LAN has a handful of machines; this only bounds a beacon
# flood of spoofed ids from ballooning the peers table + snapshot, not real use.
_MAX_PEERS = 256


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
        self.platform = identity.detect_platform()
        self.epoch = time.time()
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
        self._job_futures: dict[str, asyncio.Future] = {}
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

    # MARK: - identity / gossip source of truth

    @property
    def info(self) -> NodeInfo:
        return NodeInfo(
            id=self.local.id,
            name=self.local.name,
            platform=self.platform,
            tier=self.local.tier,
            tokens=self.local.tokens,
            tcp_port=self.tcp_port,
            epoch=self.epoch,
            seq=self._seq,
            sees=tuple(sorted(pid for pid, p in self.peers.items() if p.linked)),
            duties_enabled=self.local.duties_enabled,
            owner=self.local.owner,
            stats=self.stats.advertise(),
        )

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
            loop.create_task(self._heartbeat_loop(), name="mesh-heartbeat"),
            loop.create_task(self._snapshot_loop(), name="mesh-snapshot"),
        ]
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
        group, port = self.proto["multicastGroup"], self.proto["multicastPort"]
        lo = config.loopback_only()
        iface_ip = "127.0.0.1" if lo else "0.0.0.0"

        # Receive: all nodes (across hosts AND within one host, via SO_REUSEPORT)
        # bind the shared discovery port and join the group.
        recv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            recv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        recv.bind(("", port))
        mreq = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(iface_ip))
        recv.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        recv.setblocking(False)
        loop.add_reader(recv, self._on_udp_readable, recv)
        self._udp_recv = recv

        # Send: multicast (+ broadcast off-loopback, for APs that drop multicast).
        send = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        send.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        send.setsockopt(
            socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface_ip)
        )
        if not lo:
            send.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        send.setblocking(False)
        self._udp_send = send

    # MARK: - discovery

    async def _beacon_loop(self) -> None:
        group, port = self.proto["multicastGroup"], self.proto["multicastPort"]
        while True:
            payload = protocol.encode(protocol.beacon(self.info))
            with contextlib.suppress(OSError):
                self._udp_send.sendto(payload, (group, port))
            if not config.loopback_only():
                with contextlib.suppress(OSError):
                    self._udp_send.sendto(payload, ("255.255.255.255", port))
            await asyncio.sleep(self.proto["beaconIntervalSecs"])

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
        if peer is not None:
            peer.addr = host
            if peer.linked:
                # A higher epoch in the beacon means the peer restarted behind
                # our back — drop the dead link so redial happens below.
                epoch = float(msg.get("epoch", 0.0))
                if epoch > peer.info.epoch:
                    self._drop_peer(peer_id, reason="restarted")
                else:
                    return
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
            writer.write(protocol.encode(
                protocol.hello(self.info, self.overrides.to_dict(), config.secret())
            ))
            try:
                await writer.drain()
            except (ConnectionError, OSError):
                writer.close()
                return
            # Dialed link: we reached whoever answered a beacon (spoofable), so
            # nothing is trusted until their first message is a valid hello.
            await self._run_link(reader, writer, host, authenticated=False)
        finally:
            self._dialing.discard(peer_id)

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
            await self._run_ctl(reader, writer)
            return
        if first.get("t") == "hello":
            # Answer with our own hello, then treat like any link.
            writer.write(protocol.encode(
                protocol.hello(self.info, self.overrides.to_dict(), config.secret())
            ))
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
            info = NodeInfo.from_dict(msg.get("node") or {})
            if info is None or info.id == self.local.id:
                return None
            self._learn_node(info, host, writer if t == "hello" else None)
            if t == "hello":
                self._merge_overrides(msg.get("overrides"))
            return info.id
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
            self._on_set_attr(msg)
            return None
        if t == "dispatch":
            job = Job.from_dict(msg.get("job") or {})
            if job:
                self._take_job(job, writer)
            return None
        if t == "job-status":
            self._resolve_job_future(msg)
            return None
        return None

    def _peer_by_writer(self, writer: asyncio.StreamWriter) -> Peer | None:
        return next((p for p in self.peers.values() if p.writer is writer), None)

    def _learn_node(
        self, info: NodeInfo, host: str, link_writer: asyncio.StreamWriter | None
    ) -> None:
        peer = self.peers.get(info.id)
        fresh = peer is None or info.newer_than(peer.info)
        if peer is None:
            peer = Peer(info, host)
            self.peers[info.id] = peer
            activity.log("mesh", "mesh-peer-up",
                         f"Mesh: discovered {info.name} ({info.platform}, tier {info.tier})")
        if fresh:
            peer.info = info
        peer.addr = host or peer.addr
        peer.last_seen = time.monotonic()
        peer.down_since = None
        if link_writer is not None:
            if peer.writer is not None and peer.writer is not link_writer:
                # Duplicate link (dial race despite the id rule, or a zombie):
                # keep the new one, close the old quietly.
                with contextlib.suppress(Exception):
                    peer.writer.close()
            peer.writer = link_writer
            self._bump_and_gossip()  # our `sees` changed
        if fresh:
            # Relay a genuinely-newer advertisement learned via GOSSIP onward, so a
            # NodeInfo update converges across a multi-hop topology (A—B—C where A
            # and C don't link directly), not just a full mesh. Mirrors the
            # overrides relay. The freshness gate on every receiver stops the echo
            # from looping. A hello-learned peer is directly connected, so its info
            # is broadcast by the normal channels — only gossip needs the relay.
            if link_writer is None:
                self._broadcast(protocol.node_update(info))
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
                    with contextlib.suppress(ConnectionError, OSError):
                        await peer.writer.drain()
                    if now - peer.last_seen > timeout:
                        self._drop_peer(pid, reason="heartbeat timeout")
                elif (peer.down_since is not None
                      and now - peer.down_since > _DOWN_RETENTION_SECS):
                    del self.peers[pid]  # long dead — drop from the snapshot too

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

    def _merge_overrides(self, raw: object) -> None:
        if not isinstance(raw, dict):
            return
        incoming = PlacementOverrides.from_dict(raw)
        if incoming.wins_over(self.overrides):
            self.overrides = incoming
            self._broadcast(protocol.overrides_update(self.overrides.to_dict()))
            self._recompute("overrides")

    def set_overrides_duty(self, duty_id: str, placement_dict: dict) -> None:
        """A local edit (panel/CLI): bump the LWW rev and gossip."""
        placement = config.Placement.from_dict(placement_dict)
        self.overrides = self.overrides.with_duty(duty_id, placement, by=self.local.id)
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
                       target: str | None = None) -> list[dict]:
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
        """
        nodes = self._alive_nodes()
        if target is not None:
            slots = [("target", [target])]
        else:
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
                status, reason = await self._dispatch_to(node_id, duty_id, prompt)
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

    async def _dispatch_to(self, node_id: str, duty_id: str, prompt: str) -> tuple[str, str]:
        job = Job(id=uuid.uuid4().hex, duty=duty_id, prompt=prompt,
                  requested_by=self.local.id, requested_at=time.time())
        if node_id == self.local.id:
            return self._run_local_request(job)
        peer = self.peers.get(node_id)
        if peer is None or not peer.linked:
            return "failed", "no link"
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._job_futures[job.id] = fut
        try:
            peer.writer.write(protocol.encode(protocol.dispatch(job)))
            await peer.writer.drain()
            msg = await asyncio.wait_for(fut, timeout=self.proto["dispatchAckTimeoutSecs"])
            return str(msg.get("status", "failed")), str(msg.get("reason", ""))
        except (asyncio.TimeoutError, ConnectionError, OSError):
            return "failed", "peer did not answer"
        finally:
            self._job_futures.pop(job.id, None)

    def _resolve_job_future(self, msg: dict) -> None:
        fut = self._job_futures.get(str(msg.get("id", "")))
        if fut is not None and not fut.done():
            fut.set_result(msg)

    def _trust_of(self, node_id: str) -> str:
        """Personal (my own device) vs foreign (someone else's), from the
        requester's advertised owner. The local node is always personal."""
        if node_id == self.local.id:
            return "personal"
        peer = self.peers.get(node_id)
        peer_owner = peer.info.owner if peer else ""
        return identity.trust_of(peer_owner, self.local.owner)

    def _admit(self, job: Job) -> tuple[bool, str]:
        """Refusal policy — the receiving node's own call, no consensus needed.
        A declined job fails the dispatcher's slot over exactly like a dead one.

        v1 refuses when:
        - the requester is **foreign** (a different owner). The zero-trust path
          — run the compute here but route any social action back through a
          personal node — is not built yet, so we decline rather than act on a
          stranger's behalf.
        - we have this duty **disabled** locally (opted out of the class of work).
        - we are **out of tokens** (can't serve — this is Bob refusing the job
          Alice sent anyway, which the protocol expressly allows).
        """
        if self._trust_of(job.requested_by) == "foreign":
            return False, "foreign requester (zero-trust path not implemented)"
        if not self.local.duty_enabled(job.duty):
            return False, f"duty {job.duty} disabled here"
        if self.local.tokens == "out":
            return False, "out of tokens"
        return True, ""

    def _run_local_request(self, job: Job) -> tuple[str, str]:
        """Admit-or-decline, then run locally. Shared by the remote-receive path
        (``_take_job``) and a local/self dispatch, so both apply the same policy."""
        admit, reason = self._admit(job)
        if not admit:
            activity.log("mesh", "mesh-dispatch-failed",
                         f"Mesh: declined {job.duty} from "
                         f"{self._node_name(job.requested_by)} — {reason}")
            return "declined", reason
        return self._spawn_local(job)

    def _take_job(self, job: Job, writer: asyncio.StreamWriter) -> None:
        """A peer asked us to run a SzpontRequest. Admit-or-decline, and either
        way answer with the outcome so the dispatcher can act on it."""
        status, out_reason = self._run_local_request(job)
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
            return {"t": "state", "state": self.snapshot()}
        if t == "set-attr":
            self._on_set_attr(msg)
            return {"t": "ok"}
        if t == "set-overrides":
            duty = str(msg.get("duty", ""))
            placement = msg.get("placement")
            if duty in config.duty_ids() and isinstance(placement, dict):
                self.set_overrides_duty(duty, placement)
                return {"t": "ok"}
            return {"t": "error", "reason": f"unknown duty {duty!r}"}
        if t == "dispatch":
            duty = str(msg.get("duty", ""))
            if duty not in config.duty_ids():
                return {"t": "error", "reason": f"unknown duty {duty!r}"}
            target = msg.get("target")
            target = str(target) if target else None
            results = await self.dispatch(duty, str(msg.get("prompt", "")), target)
            return {"t": "dispatch-result", "duty": duty, "results": results}
        if t == "stop":
            self.request_stop()
            return {"t": "ok"}
        return {"t": "error", "reason": f"unknown command {t!r}"}

    # MARK: - snapshot

    def snapshot(self) -> dict:
        stale, timeout = self.proto["peerStaleSecs"], self.proto["peerTimeoutSecs"]
        now = time.monotonic()
        peers = []
        for p in sorted(self.peers.values(), key=lambda p: (p.info.name, p.info.id)):
            d = p.info.to_dict()
            d["link"] = p.link_state(stale, timeout)
            d["addr"] = p.addr
            d["lastSeenSecsAgo"] = round(now - p.last_seen, 1)
            # Decorate with this node's view of the peer: is it one of mine
            # (personal) or someone else's (foreign), and its dispatch surplus.
            d["trust"] = identity.trust_of(p.info.owner, self.local.owner)
            d["surplus"] = round(p.info.surplus(), 3)
            peers.append(d)
        return {
            "tcpPort": self.tcp_port,
            "self": self.info.to_dict(),
            "peers": peers,
            "assignments": {k: a.to_dict() for k, a in self._assignments.items()},
            "overrides": self.overrides.to_dict(),
        }

    async def _snapshot_loop(self) -> None:
        while True:
            # Age the local accounting so the displayed usageAvg/quota decay even
            # while idle (local only — no gossip churn; peers hear on real change).
            self.stats = self.stats.decayed(time.time())
            statefile.write_state(self.snapshot())
            await asyncio.sleep(self.proto["stateWriteIntervalSecs"])
