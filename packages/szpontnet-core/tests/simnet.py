"""A virtual LAN, so a whole mesh can be run — and broken — inside one test.

The node's own integration tests reach the network through real loopback sockets,
which is honest but only ever exercises a *healthy* network: you cannot drop a
beacon, cut a link, or partition two machines through 127.0.0.1. Everything this
suite is about — packet loss, split brains, simultaneous claims, a peer that goes
silent without closing its socket — lives in the failures, so the network itself
has to become something a test can steer.

So this module virtualizes the two transports the node uses, and nothing else:

* ``asyncio.start_server`` / ``asyncio.open_connection`` become an in-memory
  switch. Every simulated node gets its own address (``10.0.0.N``), so they all
  bind the same TCP port exactly as separate machines would, and a connection is
  a pair of byte pipes this module owns.
* the beacon sockets become a multicast bus that delivers a datagram to every
  node that can currently hear the sender — including the sender itself, as
  ``IP_MULTICAST_LOOP`` does.

Both consult one reachability model, so :meth:`SimNet.cut` and
:meth:`SimNet.isolate` stop delivery **without closing anything**, which is what
a real partition does and what leaves the node's heartbeat reaper as the thing
under test rather than a socket error.

Everything above the transports is the real node: the real dial rule, the real
handshake, the real gossip, the real claim book.

**One process, many nodes.** A node reads its state directory, join secret and
protocol constants from the environment, which is process-global — so two nodes
in one interpreter would share a state directory and clobber each other. Every
environment read goes through :func:`szpontnet.env.get`, so this module points
that at a per-node overlay selected by a :class:`~contextvars.ContextVar` and
runs each node's entry points with that variable set. Tasks inherit the context
that created them, so a node's beacon/heartbeat/snapshot loops keep resolving to
their own node for as long as they live.

Usage::

    def test_two_nodes_link(simnet):
        async def scenario():
            a = await simnet.node("a")
            b = await simnet.node("b")
            await simnet.linked(a, b)
        simnet.run(scenario())
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import errno
import json
import random
from dataclasses import dataclass, field

from szpontnet import env as envmod
from szpontnet import node as nodemod, protocol, spawnjob, usage

# The node currently on this task's stack. Set around every entry point into a
# node (construction, start, a test's direct call) and around every callback this
# module delivers into one, so per-node environment and per-node state resolve
# correctly while many nodes share an interpreter.
CURRENT: contextvars.ContextVar["SimNode | None"] = contextvars.ContextVar(
    "szpontnet_sim_node", default=None)

# Protocol constants every simulated mesh runs on, in SZPONTNET_* suffix form.
# Fast enough that a heartbeat timeout is a fraction of a second, slack enough
# that a loaded machine doesn't reap a healthy link mid-test.
FAST_PROTOCOL = {
    "BEACON_SECS": "0.05",
    "HEARTBEAT_SECS": "0.05",
    "REDIAL_SECS": "0.1",
    "STALE_SECS": "0.3",
    "TIMEOUT_SECS": "0.6",
    "ACK_SECS": "1.5",
    "STATE_SECS": "0.2",
    "RESULT_RETRY_SECS": "0.1",
    "RESULT_MAX_SECS": "2.0",
    "FOREIGN_TIMEOUT_SECS": "5.0",
    "COMPLETION_DEADLINE_SECS": "0.5",
    "REMINDER_GRACE_SECS": "0.5",
}


# MARK: - frames


def _frames(chunk: bytes) -> list[bytes]:
    """Split a write into whole NDJSON frames, keeping the terminator. A trailing
    fragment comes back unterminated, for the caller to hold until it completes."""
    out, start = [], 0
    while True:
        nl = chunk.find(b"\n", start)
        if nl < 0:
            break
        out.append(chunk[start:nl + 1])
        start = nl + 1
    if start < len(chunk):
        out.append(chunk[start:])
    return out


def message_type(frame: bytes) -> str:
    """The ``t`` of an NDJSON frame, or ``""`` when it isn't a typed message."""
    try:
        msg = json.loads(frame)
    except (ValueError, UnicodeDecodeError):
        return ""
    return msg.get("t", "") if isinstance(msg, dict) else ""


@dataclass
class Frame:
    """One message this switch carried (or dropped), for assertions."""

    src: str  # source node id
    dst: str  # destination node id
    kind: str  # message type tag
    raw: bytes

    def payload(self) -> dict:
        return json.loads(self.raw)


# MARK: - virtual TCP


class _Pipe:
    """One direction of a virtual TCP connection: a byte buffer with a reader."""

    def __init__(self) -> None:
        self._buf = bytearray()
        self._eof = False
        self._waiter: asyncio.Future | None = None

    def feed(self, data: bytes) -> None:
        if self._eof:
            return
        self._buf.extend(data)
        self._wake()

    def feed_eof(self) -> None:
        self._eof = True
        self._wake()

    def _wake(self) -> None:
        if self._waiter is not None and not self._waiter.done():
            self._waiter.set_result(None)

    async def readline(self) -> bytes:
        """Bytes up to and including the next ``\\n``; ``b""`` at EOF.

        Mirrors ``StreamReader.readline``, including its one surprising edge: at
        EOF with an unterminated tail it returns that tail rather than raising.
        """
        while True:
            nl = self._buf.find(b"\n")
            if nl >= 0:
                line = bytes(self._buf[:nl + 1])
                del self._buf[:nl + 1]
                return line
            if self._eof:
                line, self._buf = bytes(self._buf), bytearray()
                return line
            self._waiter = asyncio.get_running_loop().create_future()
            try:
                await self._waiter
            finally:
                self._waiter = None


class _Writer:
    """The ``StreamWriter`` surface the node uses, over a virtual link.

    A ``write`` after ``close`` is dropped rather than raised, because that is
    what a real ``StreamWriter`` does — the node's heartbeat loop writes to peers
    it has not reaped yet and would die on anything else.
    """

    def __init__(self, link: "_Link", *, from_client: bool) -> None:
        self._link = link
        self._from_client = from_client
        self._closed = False
        self._tail = bytearray()

    def write(self, data: bytes) -> None:
        if self._closed:
            return
        self._tail.extend(data)
        pending = bytes(self._tail)
        self._tail = bytearray()
        for frame in _frames(pending):
            if not frame.endswith(b"\n"):
                self._tail.extend(frame)  # hold an incomplete frame back
                continue
            self._link.carry(frame, from_client=self._from_client)

    async def drain(self) -> None:
        if self._link.stalled(self._from_client):
            await asyncio.Event().wait()  # a peer that stopped reading
        if self._link.broken:
            raise ConnectionResetError("virtual link reset")
        await asyncio.sleep(0)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._link.close_from(from_client=self._from_client)

    def is_closing(self) -> bool:
        return self._closed

    def get_extra_info(self, key: str, default=None):
        if key == "peername":
            return self._link.peername(from_client=self._from_client)
        return default


class _Link:
    """A virtual TCP connection between two simulated nodes."""

    def __init__(self, net: "SimNet", client: "SimNode", server: "SimNode",
                 client_port: int, server_port: int) -> None:
        self.net = net
        self.client = client
        self.server = server
        self.client_port = client_port
        self.server_port = server_port
        self.to_server = _Pipe()
        self.to_client = _Pipe()
        self.broken = False
        # Per-direction "the bytes go nowhere" (a wedged peer), independent of
        # the net-wide partition model.
        self.blackhole_to_server = False
        self.blackhole_to_client = False
        self.stall_client_drain = False
        self.stall_server_drain = False
        self.closed = False

    def _ends(self, from_client: bool) -> tuple["SimNode", "SimNode"]:
        return (self.client, self.server) if from_client else (self.server, self.client)

    def peername(self, from_client: bool):
        return ((self.server.ip, self.server_port) if from_client
                else (self.client.ip, self.client_port))

    def stalled(self, from_client: bool) -> bool:
        return self.stall_client_drain if from_client else self.stall_server_drain

    def carry(self, frame: bytes, from_client: bool) -> None:
        src, dst = self._ends(from_client)
        blackholed = (self.blackhole_to_server if from_client
                      else self.blackhole_to_client)
        record = Frame(src=src.id, dst=dst.id, kind=message_type(frame), raw=frame)
        if (blackholed or not self.net.reachable(src, dst)
                or self.net.should_drop(record)):
            self.net.dropped.append(record)
            return
        self.net.carried.append(record)
        pipe = self.to_server if from_client else self.to_client
        if self.net.link_delay > 0:
            asyncio.get_running_loop().call_later(self.net.link_delay, pipe.feed, frame)
        else:
            pipe.feed(frame)

    def close_from(self, from_client: bool) -> None:
        """One end closed. Its own pending read ends immediately; the peer only
        learns of it if the network can still carry the FIN.

        That asymmetry is the point: across a partition (or a frozen link) a close
        is invisible to the other side, which keeps a **half-open** connection
        until its own heartbeat reaper notices — the state a mesh actually lands
        in when one machine gives up on another before the network heals.
        """
        self.closed = True
        src, dst = self._ends(from_client)
        blackholed = (self.blackhole_to_server if from_client
                      else self.blackhole_to_client)
        (self.to_client if from_client else self.to_server).feed_eof()
        if not blackholed and self.net.reachable(src, dst):
            (self.to_server if from_client else self.to_client).feed_eof()

    def close(self) -> None:
        """Tear the whole connection down, both directions — a cable pull that
        both ends see at once."""
        self.closed = True
        self.to_server.feed_eof()
        self.to_client.feed_eof()

    # -- test controls -----------------------------------------------------

    def freeze(self) -> None:
        """Silent death: the socket stays open, nothing crosses it again. What a
        crashed machine or a suspended laptop looks like from the other end — and
        the only failure a heartbeat reaper can be the one to catch."""
        self.blackhole_to_server = True
        self.blackhole_to_client = True

    def thaw(self) -> None:
        self.blackhole_to_server = False
        self.blackhole_to_client = False

    def reset(self) -> None:
        """An RST: pending reads end and the next ``drain`` raises, the way a
        connection reset by the peer surfaces to a writer."""
        self.broken = True
        self.close()

    def stall_writes_from(self, sim: "SimNode") -> None:
        """Wedge ``sim``'s side: its ``drain`` never returns, as when a peer stops
        reading and the send buffer fills. The socket is alive and the peer may
        still be talking — it just cannot be written to any more."""
        if sim.id == self.client.id:
            self.stall_client_drain = True
        else:
            self.stall_server_drain = True


class _Server:
    """What ``asyncio.start_server`` hands back, as far as the node uses it."""

    def __init__(self, net: "SimNet", key: tuple[str, int]) -> None:
        self._net = net
        self._key = key

    def close(self) -> None:
        self._net.listeners.pop(self._key, None)

    async def wait_closed(self) -> None:
        return None


# MARK: - virtual UDP (the beacon bus)


class _UdpRecv:
    """The receive socket, reduced to what ``_on_udp_readable`` calls on it."""

    def __init__(self) -> None:
        self.queue: list[tuple[bytes, tuple[str, int]]] = []
        self.closed = False

    def recvfrom(self, _bufsize: int):
        if not self.queue:
            raise BlockingIOError
        return self.queue.pop(0)

    def close(self) -> None:
        self.closed = True


class _UdpSend:
    """The send socket: hands each datagram to the bus for its owner."""

    def __init__(self, net: "SimNet", owner: "SimNode") -> None:
        self._net = net
        self._owner = owner
        self.closed = False

    def sendto(self, data: bytes, _addr) -> int:
        if self._owner.beacon_send_errno is not None:
            raise OSError(self._owner.beacon_send_errno, "simulated send failure")
        self._net.deliver_beacon(self._owner, data)
        return len(data)

    def close(self) -> None:
        self.closed = True


# MARK: - a simulated node


@dataclass
class SimNode:
    """One machine on the virtual LAN: its address, its environment, its node."""

    net: "SimNet"
    name: str
    ip: str
    env: dict[str, str | None]
    id: str = ""
    node: nodemod.MeshNode | None = None
    udp_recv: _UdpRecv = field(default_factory=_UdpRecv)
    udp_send: _UdpSend | None = None
    # An errno here makes every beacon send fail, the way an OS privacy gate
    # does. A node that hits it diagnoses the cause with a real loopback probe
    # (`_loopback_send_ok`), so a test using this should pin that too.
    beacon_send_errno: int | None = None
    # What the patched quota probe answers for this node:
    # ``(state, fraction, session, week, pace)``. ``pace`` is the surplus that
    # surplus-first dispatch ranks on.
    quota: tuple = ("ok", 1.0, None, None, 1.0)
    # Personal jobs the host runner was asked to run here: (prompt, done_path).
    jobs: list = field(default_factory=list)
    # Set to a reason to make this machine unable to take a personal job — the
    # "no runner here" answer a dispatcher fails a slot over on.
    runner_error: str | None = None
    # Work keys this machine reports as already under way by some route the mesh
    # never saw — the host's ground-truth floor against a double spawn.
    running_work: set = field(default_factory=set)
    started: bool = False

    # -- context -----------------------------------------------------------

    @contextlib.contextmanager
    def active(self):
        """Run a block as this node: its environment, its state directory.

        Tasks created inside inherit it, which is how a node's own loops keep
        resolving to their own state for the rest of their lives.
        """
        token = CURRENT.set(self)
        try:
            yield self
        finally:
            CURRENT.reset(token)

    # -- lifecycle ---------------------------------------------------------

    def _build(self) -> None:
        with self.active():
            self.node = nodemod.MeshNode()
            # The real one asks the OS; a simulated machine simply knows.
            self.node._local_addrs = {self.ip}
            self.node._start_udp = self._install_udp  # type: ignore[method-assign]

    def _install_udp(self, _loop=None) -> None:
        """Give the node its virtual sockets, including the two rebuild paths —
        which otherwise construct real ones (and register a real reader) the
        moment a blocked beacon send recovers."""
        self.udp_send = _UdpSend(self.net, self)
        self.node._udp_recv = self.udp_recv  # type: ignore[assignment]
        self.node._udp_send = self.udp_send  # type: ignore[assignment]
        self.node._rebuild_udp_send = self._rebuild_send  # type: ignore[method-assign]
        self.node._rebuild_udp_recv = self._rebuild_recv  # type: ignore[method-assign]

    def _rebuild_send(self) -> None:
        self.udp_send = _UdpSend(self.net, self)
        self.node._udp_send = self.udp_send  # type: ignore[assignment]

    def _rebuild_recv(self) -> None:
        self.udp_recv = _UdpRecv()
        self.node._udp_recv = self.udp_recv  # type: ignore[assignment]

    async def start(self) -> "SimNode":
        with self.active():
            await self.node.start()
        self.started = True
        return self

    async def stop(self) -> None:
        if self.node is None or not self.started:
            return
        with self.active():
            await self.node.stop()
        self.started = False

    async def restart(self, **env: str) -> "SimNode":
        """Bring the same machine back up: same state directory, so the same node
        id and device key — but a new incarnation, so a fresh ``epoch``."""
        await self.stop()
        self.env.update(env)
        self.udp_recv = _UdpRecv()
        self._build()
        return await self.start()

    # -- what a test asks a node -------------------------------------------

    def peer(self, other: "SimNode"):
        return self.node.peers.get(other.id)

    def linked_to(self, other: "SimNode") -> bool:
        peer = self.peer(other)
        return peer is not None and peer.linked

    def verified(self, other: "SimNode") -> bool:
        peer = self.peer(other)
        return peer is not None and peer.verified_fp is not None

    def trust_of(self, other: "SimNode") -> str:
        with self.active():
            return self.node._peer_trust(self.peer(other))

    def link_state(self, other: "SimNode") -> str:
        peer = self.peer(other)
        if peer is None:
            return "unknown"
        return peer.link_state(self.node.proto["peerStaleSecs"],
                               self.node.proto["peerTimeoutSecs"])

    def snapshot(self) -> dict:
        with self.active():
            return self.node.snapshot()

    def assigned(self, duty: str) -> tuple:
        a = self.node._assignments.get(duty)
        return a.assigned if a else ()

    def trusts(self, *others: "SimNode") -> None:
        """Promote other simulated machines' device keys on this one."""
        with self.active():
            for other in others:
                self.node.add_trusted(other.node.fingerprint, other.name)

    async def dispatch(self, duty: str, prompt: str, **kw) -> list[dict]:
        with self.active():
            return await self.node.dispatch(duty, prompt, **kw)

    async def ctl(self, msg: dict) -> dict:
        with self.active():
            return await self.node._ctl_command(msg)

    def claim(self, work_key: str) -> bool:
        with self.active():
            return self.node.claim(work_key)

    def release(self, work_key: str) -> None:
        with self.active():
            self.node.release(work_key)

    def claim_owner(self, work_key: str) -> str | None:
        with self.active():
            return self.node._claim_holder(work_key)

    def link_to(self, other: "SimNode") -> _Link | None:
        """The virtual connection currently joining these two, either direction."""
        for link in reversed(self.net.links):
            if link.closed:
                continue
            if {link.client.id, link.server.id} == {self.id, other.id}:
                return link
        return None

    # -- playing a peer this node would rather not have --------------------

    def inject_to(self, other: "SimNode", msg: dict | bytes) -> None:
        """Put a raw message on the link this node holds to ``other``, as if this
        node had sent it.

        How a test plays a hostile, buggy or newer peer without writing a second
        protocol implementation: the receiver has a real, verified link to a real
        peer, and the message arrives on it exactly as any other would.
        """
        link = self.link_to(other)
        assert link is not None, f"{self.name} holds no link to {other.name}"
        raw = msg if isinstance(msg, bytes) else protocol.encode(msg)
        pipe = link.to_server if link.client.id == self.id else link.to_client
        pipe.feed(raw)

    def advert(self, **changes) -> dict:
        """This node's own advertisement with fields changed and re-signed — a
        version of itself it never actually published."""
        from dataclasses import replace

        with self.active():
            info = replace(self.node.info, **changes)
            sig = self.node.key.sign(protocol.advert_signing_bytes(info.to_dict()))
            return replace(info, sig=sig).to_dict()


# MARK: - the switch


class SimNet:
    """The virtual LAN: nodes, reachability, and the two patched transports."""

    def __init__(self, tmp_path, monkeypatch, *, seed: int = 1234,
                 protocol_env: dict | None = None) -> None:
        self._tmp = tmp_path
        self._mp = monkeypatch
        self.rng = random.Random(seed)
        self.nodes: list[SimNode] = []
        self.by_id: dict[str, SimNode] = {}
        self.listeners: dict[tuple[str, int], tuple[SimNode, object]] = {}
        self.links: list[_Link] = []
        self.log: list[tuple[str, str, str]] = []  # (node name, action, detail)
        # Reachability: unordered node-id pairs that cannot exchange anything,
        # plus wholly isolated nodes.
        self._cuts: set[frozenset] = set()
        self._isolated: set[str] = set()
        self._accepts: set[asyncio.Task] = set()
        # Loss and inspection.
        self.beacon_loss = 0.0
        self.link_delay = 0.0
        self._filters: list = []
        self.carried: list[Frame] = []
        self.dropped: list[Frame] = []
        # Every beacon hop, as (sender id, receiver id) — so a test can say which
        # machine heard which, not merely how many datagrams moved.
        self.beacons_delivered: list[tuple[str, str]] = []
        self.beacons_dropped: list[tuple[str, str]] = []
        self.connects = 0
        self.connects_refused = 0
        self._next_port = 51000
        self._protocol = {**FAST_PROTOCOL, **(protocol_env or {})}
        self._install_patches()

    # -- patching ----------------------------------------------------------

    def _install_patches(self) -> None:
        mp = self._mp
        real_env_get = envmod.get

        def sim_env_get(suffix: str, default: str | None = None):
            sim = CURRENT.get()
            if sim is not None and suffix in sim.env:
                value = sim.env[suffix]
                return default if value is None else value
            return real_env_get(suffix, default)

        mp.setattr(envmod, "get", sim_env_get)
        # spawnjob imported the accessor by name, so it needs its own rebind.
        mp.setattr(spawnjob, "env_get", sim_env_get)
        # Constructing a node otherwise blocks on getaddrinfo; a simulated machine
        # is told its address instead (see SimNode._build).
        mp.setattr(nodemod, "_own_addresses", lambda: set())
        mp.setattr(asyncio, "start_server", self._start_server)
        mp.setattr(asyncio, "open_connection", self._open_connection)
        mp.setattr(usage, "token_state", self._token_state)

    def _token_state(self, _plan, _now=None, *, insist=False):
        """The quota probe, answered per node instead of from this machine."""
        sim = CURRENT.get()
        return sim.quota if sim is not None else ("ok", 1.0, None, None, 1.0)

    async def _start_server(self, handler, _host, port, **_kw) -> _Server:
        sim = CURRENT.get()
        if sim is None:
            raise RuntimeError("a node bound a port outside its own context")
        # Every simulated machine has its own address, so the bind address the
        # node computed is irrelevant — it binds ITS port on ITS interface.
        key = (sim.ip, int(port))
        if key in self.listeners:
            raise OSError(errno.EADDRINUSE, "address already in use")
        self.listeners[key] = (sim, handler)
        return _Server(self, key)

    async def _open_connection(self, host, port, **_kw):
        src = CURRENT.get()
        if src is None:
            raise RuntimeError("a node dialled outside its own context")
        entry = self.listeners.get((host, int(port)))
        if entry is None:
            self.connects_refused += 1
            raise ConnectionRefusedError(f"nothing listening on {host}:{port}")
        dst, handler = entry
        if not self.reachable(src, dst):
            # A partition eats the SYN. The dialer sees an unreachable host, one
            # of the errnos `_dial` already treats as "try again next beacon".
            self.connects_refused += 1
            raise ConnectionRefusedError(f"{host}:{port} unreachable")
        self.connects += 1
        self._next_port += 1
        link = _Link(self, src, dst, client_port=self._next_port,
                     server_port=int(port))
        self.links.append(link)
        # The accept side must run as the LISTENING node whatever task opened the
        # connection, and create_task snapshots the context — so set it around it.
        token = CURRENT.set(dst)
        try:
            task = asyncio.get_running_loop().create_task(
                handler(link.to_server, _Writer(link, from_client=False)),
                name=f"sim-accept-{dst.name}")
        finally:
            CURRENT.reset(token)
        self._accepts.add(task)
        task.add_done_callback(self._accepts.discard)
        return link.to_client, _Writer(link, from_client=True)

    # -- reachability ------------------------------------------------------

    def reachable(self, a: SimNode, b: SimNode) -> bool:
        if a.id == b.id:
            return True  # a node always hears its own multicast
        if a.id in self._isolated or b.id in self._isolated:
            return False
        return frozenset((a.id, b.id)) not in self._cuts

    def cut(self, a: SimNode, b: SimNode) -> None:
        """Stop delivering between these two. Nothing is closed — an established
        link just goes quiet, exactly as it does across a real partition."""
        self._cuts.add(frozenset((a.id, b.id)))

    def heal(self, a: SimNode, b: SimNode) -> None:
        self._cuts.discard(frozenset((a.id, b.id)))

    def isolate(self, sim: SimNode) -> None:
        """Cut one machine off from everything (its cable is out)."""
        self._isolated.add(sim.id)

    def rejoin(self, sim: SimNode) -> None:
        self._isolated.discard(sim.id)

    def partition(self, *groups) -> None:
        """Cut every pair that spans two of the given groups."""
        for i, group in enumerate(groups):
            for other in groups[i + 1:]:
                for a in group:
                    for b in other:
                        self.cut(a, b)

    def heal_all(self) -> None:
        self._cuts.clear()
        self._isolated.clear()
        for link in self.links:
            link.thaw()

    # -- loss --------------------------------------------------------------

    def drop_when(self, predicate):
        """Drop every frame ``predicate(frame)`` accepts. Returns a handle whose
        ``remove()`` restores delivery — how a test heals targeted loss."""
        self._filters.append(predicate)
        net = self

        class _Handle:
            def remove(self) -> None:
                with contextlib.suppress(ValueError):
                    net._filters.remove(predicate)

        return _Handle()

    def drop_kind(self, kind: str, *, src: SimNode | None = None,
                  dst: SimNode | None = None, times: int | None = None):
        """Drop messages of one type, optionally only on one path and only for
        the first ``times`` of them."""
        state = {"left": times}

        def predicate(frame: Frame) -> bool:
            if frame.kind != kind:
                return False
            if src is not None and frame.src != src.id:
                return False
            if dst is not None and frame.dst != dst.id:
                return False
            if state["left"] is None:
                return True
            if state["left"] <= 0:
                return False
            state["left"] -= 1
            return True

        return self.drop_when(predicate)

    def should_drop(self, frame: Frame) -> bool:
        return any(f(frame) for f in list(self._filters))

    def frames(self, kind: str | None = None, *, src: SimNode | None = None,
               dst: SimNode | None = None, of: list | None = None) -> list[Frame]:
        """Every frame carried (or, with ``of=net.dropped``, dropped) matching."""
        return [f for f in (self.carried if of is None else of)
                if (kind is None or f.kind == kind)
                and (src is None or f.src == src.id)
                and (dst is None or f.dst == dst.id)]

    # -- the beacon bus ----------------------------------------------------

    def deliver_beacon(self, sender: SimNode, data: bytes) -> None:
        loop = asyncio.get_running_loop()
        for target in list(self.nodes):
            if target.node is None or target.udp_recv.closed:
                continue
            if not self.reachable(sender, target):
                self.beacons_dropped.append((sender.id, target.id))
                continue
            if (target is not sender and self.beacon_loss > 0
                    and self.rng.random() < self.beacon_loss):
                self.beacons_dropped.append((sender.id, target.id))
                continue
            self.beacons_delivered.append((sender.id, target.id))
            loop.call_soon(self._receive_beacon, target, data, sender.ip)

    def beacon_hops(self, src: SimNode, dst: SimNode, *, since: int = 0) -> int:
        """How many of ``src``'s beacons reached ``dst`` after ``since``."""
        return sum(1 for s, d in self.beacons_delivered[since:]
                   if s == src.id and d == dst.id)

    @staticmethod
    def _receive_beacon(target: SimNode, data: bytes, src_ip: str) -> None:
        if target.node is None or target.udp_recv.closed:
            return
        token = CURRENT.set(target)
        try:
            target.udp_recv.queue.append((data, (src_ip, 40877)))
            target.node._on_udp_readable(target.udp_recv)
        finally:
            CURRENT.reset(token)

    def inject_beacon(self, target: SimNode, payload: dict, src_ip: str) -> None:
        """Put a beacon on a node's receive path from an address no simulated
        node owns — how a test plays an off-mesh spoofer."""
        self._receive_beacon(target, protocol.encode(payload), src_ip)

    # -- nodes -------------------------------------------------------------

    async def node(self, name: str, *, node_id: str | None = None,
                   platform: str = "linux", tier: int = 3, tokens: str = "auto",
                   duties: dict | None = None, trust: str = "personal",
                   start: bool = True, quota: tuple | None = None,
                   **env: str) -> SimNode:
        """Add a machine to the LAN and (by default) start its node.

        ``node_id`` defaults to a letter run (``"a"*32``, ``"b"*32``, …) in
        creation order, so the id ordering that the dial rule and claim ownership
        turn on is whatever the test wrote down rather than whatever a uuid
        happened to be.

        ``trust`` defaults to ``personal`` — the full-altruism mesh — because
        most scenarios are about routing rather than about trust. A test that is
        about trust passes ``trust="foreign"``, the shipped default.
        """
        index = len(self.nodes)
        assert index < 26, "simulated node ids run a..z"
        ip = f"10.0.0.{index + 1}"
        node_id = node_id or (chr(ord("a") + index) * 32)
        state = self._tmp / f"node-{index}-{name}"
        state.mkdir(parents=True, exist_ok=True)
        (state / "node.json").write_text(json.dumps({
            "id": node_id,
            "name": name,
            "tier": tier,
            "tokens": tokens,
            "strengthAuto": False,  # no hardware probe on a simulated machine
            "dutiesEnabled": duties or {},
        }), encoding="utf-8")

        overlay: dict[str, str | None] = {
            "DIR": str(state),
            "PLATFORM": platform,
            "DEFAULT_TRUST": trust,
            # A simulated machine has no WAN: each transport binds real sockets this
            # switch does not own, so a simulated mesh would quietly grow one endpoint
            # (or one tor child) per node and a transport reaching past the virtual
            # LAN — iroh would publish every simulated node to a real discovery
            # service besides. Off explicitly rather than by omission, since iroh is
            # on by default.
            "IROH": "0",
            "TOR": "0",
            # Explicitly unset, so a developer's own exported SZPONTNET_* values
            # can never leak into a simulated mesh.
            "LOOPBACK": None,
            "SECRET": None,
            "API_KEY": None,
            "SERVER": None,
            "SPAWN": None,
            "FOREIGN_SPAWN": None,
            "ON_RESULT": None,
            "EXTEND_DECIDER": None,
            "HOST": None,
            **self._protocol,
            **env,
        }
        sim = SimNode(net=self, name=name, ip=ip, env=overlay, id=node_id)
        if quota is not None:
            sim.quota = quota
        sim._build()
        self.nodes.append(sim)
        self.by_id[node_id] = sim
        if start:
            await sim.start()
        return sim

    async def stop_all(self) -> None:
        for sim in list(self.nodes):
            with contextlib.suppress(Exception):
                await sim.stop()
        for task in list(self._accepts):
            task.cancel()
        for task in list(self._accepts):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._accepts.clear()

    # -- waiting -----------------------------------------------------------

    async def until(self, predicate, timeout: float = 5.0, why: str = "") -> None:
        """Poll ``predicate`` until it holds, or fail saying what never happened."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            if predicate():
                return
            if loop.time() > deadline:
                raise AssertionError(why or "condition never held")
            await asyncio.sleep(0.01)

    async def linked(self, *sims: SimNode, timeout: float = 5.0) -> None:
        """Wait for every pair among ``sims`` to hold a live link, both ways."""
        pairs = [(a, b) for i, a in enumerate(sims) for b in sims[i + 1:]]
        await self.until(
            lambda: all(a.linked_to(b) and b.linked_to(a) for a, b in pairs),
            timeout,
            "peers never linked: " + ", ".join(
                f"{a.name}->{b.name}, {b.name}->{a.name}" for a, b in pairs))

    async def all_verified(self, *sims: SimNode, timeout: float = 5.0) -> None:
        """Wait for every pair among ``sims`` to have PROVEN their device keys."""
        pairs = [(a, b) for i, a in enumerate(sims) for b in sims[i + 1:]]
        await self.until(
            lambda: all(a.verified(b) and b.verified(a) for a, b in pairs),
            timeout, "device keys were never mutually proven")

    async def quiet(self, seconds: float = 0.4) -> None:
        """Let the mesh run a while — for asserting something does NOT happen."""
        await asyncio.sleep(seconds)

    # -- driving -----------------------------------------------------------

    def run(self, coro, timeout: float = 30.0):
        """Run one scenario, then tear every node down.

        The timeout is a backstop: a wedged mesh test should fail with the
        scenario's own assertion message, never by hanging a CI job.
        """
        async def main():
            try:
                return await asyncio.wait_for(coro, timeout)
            finally:
                await self.stop_all()

        return asyncio.run(main())


# MARK: - the host behind every simulated node


def _sim_host_class():
    """The host every simulated node resolves to.

    A node finds its host through a process-global, so a per-node host object
    would be exactly the cross-contamination the environment overlay exists to
    prevent. This one reads the same context variable instead: ``run_job``
    records against the node that called it, and ``work_already_running``
    answers from that node's own set.
    """
    from szpontnet import host as hostmod

    class _SimHost(hostmod.Host):
        def log(self, action, detail):
            sim = CURRENT.get()
            if sim is not None:
                sim.net.log.append((sim.name, action, detail))

        def run_job(self, prompt, done_path):
            sim = CURRENT.get()
            if sim is None:
                raise hostmod.NoRunner("a job ran outside any node's context")
            if sim.runner_error is not None:
                raise hostmod.NoRunner(sim.runner_error)
            sim.jobs.append((prompt, done_path))
            return f"/sim/{sim.name}/prompt-{len(sim.jobs)}.txt"

        def work_already_running(self, work_key):
            sim = CURRENT.get()
            return sim is not None and work_key in sim.running_work

    return _SimHost


def build(tmp_path, monkeypatch, **kw) -> SimNet:
    """The ``simnet`` fixture's factory: a patched LAN with a host behind it."""
    from szpontnet import host as hostmod

    net = SimNet(tmp_path, monkeypatch, **kw)
    hostmod.set_host(_sim_host_class()())
    return net
