"""The iroh transport against real endpoints — no fakes below the seam.

``test_mesh_iroh.py`` injects a dialer, which makes the node-level logic sharp but
can never show that an endpoint binds, that discovery resolves an id, or that the
bytes the stream adapter writes survive a real QUIC connection. This file does: every
transport here is a real :class:`irohnet.IrohTransport`, and the capstone is two real
mesh nodes that can only reach each other over one.

**These tests use the network.** iroh publishes each endpoint to the n0 discovery
service and hole-punches (or relays) between them, so a sandbox with no outbound
443 will fail them. They are marked ``iroh_e2e`` and excluded from the default run
(``-m 'not iroh_e2e'``), the same split ``tor_e2e`` uses.

Run with ``python -m pytest packages/szpontnet-core/tests/test_iroh_e2e.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace as _dc_replace

import pytest

from szpontnet import irohnet, node as nodemod

pytestmark = [
    pytest.mark.iroh_e2e,
    pytest.mark.skipif(not irohnet.available(),
                       reason="the optional `iroh` package is not installed"),
]

# Generous next to the sub-second times these take on a warm connection: a cold CI
# runner has to reach the discovery service and may fall back to a relay.
_ONLINE_TIMEOUT = 60.0
_DIAL_TIMEOUT = 60.0


@pytest.fixture(autouse=True)
def _no_getaddrinfo_hang(monkeypatch):
    monkeypatch.setattr(nodemod, "_own_addresses", lambda: {"127.0.0.1", "::1"})


class _Echo:
    """An inbound handler that echoes each NDJSON line back, for driving the
    transport without a node behind it."""

    def __init__(self):
        self.lines: list[bytes] = []
        self.got = asyncio.Event()

    async def __call__(self, reader, writer):
        while True:
            line = await reader.readline()
            if not line:
                return
            self.lines.append(line)
            self.got.set()
            writer.write(line)
            await writer.drain()


async def _started(directory, handler):
    t = irohnet.IrohTransport(directory)
    assert await t.start(handler, online_timeout=_ONLINE_TIMEOUT), (
        "the iroh endpoint never came online — is outbound 443 reachable?")
    return t


def test_two_endpoints_exchange_ndjson_dialed_by_key_alone(tmp_path):
    """The whole transport contract: a peer is dialed by its endpoint id with no
    address, port or relay named, and the resulting stream carries NDJSON both ways
    through the adapter the link pump consumes."""
    async def scenario():
        echo = _Echo()
        server = await _started(tmp_path / "s", echo)
        client = await _started(tmp_path / "c", _Echo())
        try:
            reader, writer = await asyncio.wait_for(
                client.dial(server.address()), _DIAL_TIMEOUT)
            writer.write(b'{"t":"hello"}\n')
            await writer.drain()
            assert await asyncio.wait_for(reader.readline(), 30) == b'{"t":"hello"}\n'
            # A second line on the same stream: framing survives reuse, and a write
            # that is never drained still reaches the wire (asyncio's semantics, which
            # the many undrained writes in the link layer depend on).
            writer.write(b'{"t":"ping"}\n')
            assert await asyncio.wait_for(reader.readline(), 30) == b'{"t":"ping"}\n'
            # The writer reports the remote endpoint id as its peername, which is what
            # the node records as the link's address.
            assert writer.get_extra_info("peername")[0] == server.address()
            await writer.wait_closed()
        finally:
            await client.stop()
            await server.stop()

    asyncio.run(scenario())


def test_the_line_budget_is_enforced_over_quic(tmp_path):
    """MAX_LINE_BYTES is the NDJSON framing budget, and it has to bite identically on
    every transport — a stream that silently accepted an oversized line would let a
    peer allocate without limit. The adapter feeds a real StreamReader, so the
    over-limit ValueError is the same one the TCP path raises.

    The flood runs server→client: the receiving side is the one under test, and a
    client-sent flood would instead die in the server's own reader (which is the same
    protection, just not the one this asserts)."""
    class _Flood:
        async def __call__(self, reader, writer):
            await reader.readline()   # the dialer's opening line, well within budget
            writer.write(b"x" * (nodemod.protocol.MAX_LINE_BYTES + 10) + b"\n")
            await writer.drain()

    async def scenario():
        server = await _started(tmp_path / "s", _Flood())
        client = await _started(tmp_path / "c", _Echo())
        try:
            reader, writer = await asyncio.wait_for(
                client.dial(server.address()), _DIAL_TIMEOUT)
            writer.write(b'{"t":"hello"}\n')
            await writer.drain()
            with pytest.raises(ValueError):
                await asyncio.wait_for(reader.readline(), 30)
            await writer.wait_closed()
        finally:
            await client.stop()
            await server.stop()

    asyncio.run(scenario())


def test_the_endpoint_id_is_permanent_across_restarts(tmp_path):
    """The id is the handle peers redial, so it must survive a restart — it is the
    public half of a key persisted under the state dir, not a per-run identity."""
    async def scenario():
        first = await _started(tmp_path / "n", _Echo())
        before = first.address()
        assert irohnet.is_endpoint(before or "")
        await first.stop()
        second = await _started(tmp_path / "n", _Echo())
        try:
            assert second.address() == before
        finally:
            await second.stop()
        # A different state dir is a different node, hence a different id.
        other = await _started(tmp_path / "other", _Echo())
        try:
            assert other.address() != before
        finally:
            await other.stop()

    asyncio.run(scenario())


def test_address_is_none_before_start_and_after_stop(tmp_path):
    """The node advertises whatever ``address()`` returns, so it must report None
    whenever the endpoint cannot actually be reached — otherwise peers dial an
    endpoint that is gone and ``--status`` still claims it is ready."""
    async def scenario():
        t = irohnet.IrohTransport(tmp_path / "n")
        assert t.address() is None          # never started
        assert await t.start(_Echo(), online_timeout=_ONLINE_TIMEOUT)
        assert t.address() is not None      # live
        await t.stop()
        assert t.address() is None          # closed → degrade to LAN-only

    asyncio.run(scenario())


def test_dialing_a_malformed_endpoint_raises_rather_than_hanging(tmp_path):
    """A bad paste must fail fast and loudly at the transport, not stall the dial
    task until the caller's timeout."""
    async def scenario():
        t = await _started(tmp_path / "n", _Echo())
        try:
            with pytest.raises(ValueError):
                await t.dial("not-an-endpoint")
        finally:
            await t.stop()

    asyncio.run(scenario())


# MARK: - the capstone: two real nodes linked only over iroh, with a dispatch on it


def test_nodes_link_over_real_iroh_and_a_dispatch_runs_on_the_peer(
        tmp_path, monkeypatch):
    """The whole user story, with nothing faked: A holds only B's endpoint id
    (no LAN, no address), the link comes up over real QUIC with the identical
    hello/auth/trust handshake, and a dispatch A→B rides that link and executes on
    B — proving an iroh link behaves exactly like a LAN link."""
    monkeypatch.setenv("SZPONTNET_LOOPBACK", "1")
    monkeypatch.setenv("SZPONTNET_OAUTH_PROBE", "0")
    out_file = tmp_path / "landed.txt"
    monkeypatch.setenv("SZPONTNET_SPAWN", f"cp {{prompt_file}} {out_file}")

    def _node(subdir):
        d = tmp_path / subdir
        d.mkdir(exist_ok=True)
        monkeypatch.setenv("SZPONTNET_DIR", str(d))
        monkeypatch.setenv("SZPONTNET_DEFAULT_TRUST", "personal")
        return nodemod.MeshNode(), d

    async def _attach(node, directory):
        """Bring a real iroh transport up on ``node`` and register it the way
        MeshNode.start() would."""
        impl = irohnet.IrohTransport(directory)
        ok = await impl.start(
            lambda r, w: node._on_wan_inbound("iroh", r, w),
            online_timeout=_ONLINE_TIMEOUT)
        assert ok, "the iroh endpoint never came online"
        node.iroh = impl
        record = nodemod._Wan(name="iroh", impl=impl, field="endpoint",
                              normalize=irohnet.normalize_endpoint)
        node._wan = [record]
        return record

    async def scenario():
        b, bdir = _node("b")
        await b._start_tcp()
        a, adir = _node("a")
        await a._start_tcp()
        b_wan = await _attach(b, bdir)
        a_wan = await _attach(a, adir)
        b_endpoint = b_wan.impl.address()
        # A's advert carries its own endpoint, so B learns where to reach A too.
        assert a.info.to_dict()["endpoint"] == a_wan.impl.address()

        dial = asyncio.get_running_loop().create_task(
            a._wan_dial(a_wan, b_endpoint))
        try:
            await _await_until(
                lambda: (b.local.id in a.peers and a.peers[b.local.id].linked
                         and a.local.id in b.peers and b.peers[a.local.id].linked),
                _DIAL_TIMEOUT, "link never came up over iroh")
            # Both proved their device keys — trust is established exactly as on a LAN,
            # and over a key that is NOT the endpoint key.
            await _await_until(
                lambda: (a.peers[b.local.id].verified_fp is not None
                         and b.peers[a.local.id].verified_fp is not None),
                30.0, "device keys were not mutually verified")
            assert a.peers[b.local.id].addr == b_endpoint
            assert a.peers[b.local.id].transport == "iroh"
            assert b.peers[a.local.id].transport == "iroh"
            # The endpoint each side learned is the peer's real one, from the signed
            # advert — this is what makes the link redialable after it drops.
            assert a._wan_cache[b.local.id].endpoint == b_endpoint
            assert b._wan_cache[a.local.id].endpoint == a_wan.impl.address()
            # An iroh link must not poison the LAN redial cache.
            assert b.local.id not in a._peer_cache
            # A dispatch rides the iroh link and runs on B, just like over the LAN.
            results = await a.dispatch("audit", "hello over iroh",
                                       target=b.local.id)
            assert results and results[0]["status"] == "spawned", results
            await _await_until(out_file.exists, 30.0, "the job never landed on B")
            assert "hello over iroh" in out_file.read_text(encoding="utf-8")
        finally:
            dial.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await dial
            await a.stop()
            await b.stop()

    asyncio.run(scenario())


async def _await_until(pred, timeout: float, msg: str) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if pred():
            return
        if loop.time() > deadline:
            raise AssertionError(msg)
        await asyncio.sleep(0.02)
