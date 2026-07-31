"""The onion path end to end — a real tor daemon, real SOCKS5, real node processes.

``test_mesh_tor.py`` covers the node's Tor *decisions* (what it advertises, learns,
throttles, refuses) with the dialer injected, so it never finds out whether an onion
service actually comes up or whether the bytes the transport writes are the bytes a
daemon expects. That is the gap this file closes, at two altitudes:

**The transport** (``TorTransport`` against a daemon). The class is unmodified and the
daemon is a separate process, so the torrc it renders is parsed by the thing that
receives it, its stdout pump drains a real pipe, ``_read_hostname`` reads a file
another process wrote, and ``_socks5_connect`` puts real SOCKS5 bytes on a socket
that answers them.

**The mesh** (whole ``python -m szpontnet`` processes). Each node has its own state
directory, spawns its own tor, and — because every node in a test runs on a
*different multicast port* — cannot discover any other on the LAN. So a link that
forms here came over an onion, and that is the claim: several LANs, one mesh.

Both altitudes run against either backend (``SZPONTNET_TEST_TOR=sim|real|both``, see
``tornet.py``): a simulated onion network by default, and the live Tor network on
request. The bodies are identical — a test that passes only against the simulation is
a test that has learned something about the simulation.

Run with ``python -m pytest packages/szpontnet-core/tests/test_tor_e2e.py``, or
``SZPONTNET_TEST_TOR=real`` for the real thing (needs a ``tor`` binary; ~2 min).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket

import pytest

import tornet as tornet_module
from szpontnet import onioncache, protocol, tor

pytestmark = pytest.mark.tor_e2e


@pytest.fixture(params=tornet_module.pytest_backend_params())
def tornet(request, tmp_path, monkeypatch):
    """One Tor world per test — sim or real, with every process it started torn
    down afterwards whether the test passed or blew up."""
    net = tornet_module.TorNet(request.param, tmp_path, monkeypatch)
    try:
        yield net
    finally:
        net.stop_all()


def _run(coro, timeout: float):
    """Run one transport-level scenario under a hard backstop, so a wedged daemon
    fails the test rather than hanging the suite."""
    async def main():
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(main())


class _Echo:
    """A stand-in for the node's accept path: records what arrived over the onion
    and answers, so a test can prove bytes crossed in both directions."""

    def __init__(self) -> None:
        self.seen: list[bytes] = []

    async def __call__(self, reader, writer):
        line = await reader.readline()
        self.seen.append(line)
        writer.write(b"answered\n")
        with contextlib.suppress(ConnectionError, OSError):
            await writer.drain()
        writer.close()


# MARK: - the transport: an onion service that actually comes up


def test_the_onion_service_comes_up_and_is_a_valid_v3_address(tornet):
    """The floor everything else stands on, and the one thing an injected dialer can
    never show: ``start()`` spawns a daemon, waits out a real bootstrap, and reads
    back an address the node will put on the wire. A daemon that never reaches 100%,
    a torrc it cannot parse, or a hostname file in the wrong place all land here."""
    async def scenario():
        transport = tornet.transport("solo")
        assert await transport.start(_Echo(),
                                     bootstrap_timeout=tornet.backend.bootstrap)
        try:
            address = transport.onion_address()
            assert address and tor.is_onion(address), address
            # The same address the advert would carry — normalize_onion is what the
            # node runs it through, and it must be a no-op on a live one.
            assert tor.normalize_onion(address) == address
        finally:
            await transport.stop()

    _run(scenario(), tornet.backend.bootstrap + 30.0)


def test_the_onion_is_the_same_address_after_a_restart(tornet):
    """The address is a *permanent handle* — the whole reason a peer can cache it
    and redial from anywhere. It is permanent because the service key is persisted,
    so this restarts the transport on the same state directory and demands the same
    onion back."""
    async def scenario():
        first = tornet.transport("stable")
        assert await first.start(_Echo(), bootstrap_timeout=tornet.backend.bootstrap)
        before = first.onion_address()
        await first.stop()

        # A new transport object on the same mesh_dir — a node restart, as far as
        # the onion service is concerned.
        second = tornet.transport("stable")
        assert await second.start(_Echo(), bootstrap_timeout=tornet.backend.bootstrap)
        try:
            assert second.onion_address() == before
        finally:
            await second.stop()

    _run(scenario(), tornet.backend.bootstrap * 2 + 60.0)


def test_a_dial_carries_bytes_end_to_end_through_the_onion(tornet):
    """The outbound primitive, for real: a SOCKS5 CONNECT through one daemon to
    another's onion, landing on the *dedicated forward listener* the receiving
    transport owns. Both directions are asserted — a half-working relay that carries
    the request and drops the answer would otherwise read as success."""
    async def scenario():
        listener = _Echo()
        server = tornet.transport("server")
        client = tornet.transport("client")
        assert await server.start(listener, bootstrap_timeout=tornet.backend.bootstrap)
        assert await client.start(_Echo(), bootstrap_timeout=tornet.backend.bootstrap)
        try:
            reader, writer = await _dial_with_patience(
                client, server.onion_address(), tornet.backend.link)
            writer.write(b"over the onion\n")
            await writer.drain()
            answer = await asyncio.wait_for(reader.readline(), timeout=30.0)
            assert answer == b"answered\n"
            assert listener.seen == [b"over the onion\n"]
            writer.close()
        finally:
            await client.stop()
            await server.stop()

    _run(scenario(), tornet.backend.link + tornet.backend.bootstrap * 2 + 60.0)


def test_dialing_an_onion_nobody_serves_is_refused_not_hung(tornet):
    """An address that was never published (a typo, a peer that is off) must fail the
    dial — the node's backoff path depends on ``dial`` *raising* rather than blocking
    the redial loop forever."""
    async def scenario():
        client = tornet.transport("client")
        assert await client.start(_Echo(), bootstrap_timeout=tornet.backend.bootstrap)
        try:
            with pytest.raises((OSError, asyncio.IncompleteReadError)):
                await asyncio.wait_for(
                    client.dial("2" * 56 + ".onion"), timeout=tornet.backend.link)
        finally:
            await client.stop()

    _run(scenario(), tornet.backend.link + tornet.backend.bootstrap + 60.0)


def test_a_malformed_onion_is_refused_before_any_daemon_is_touched(tornet):
    """``dial`` validates the address before it opens a socket, so a bad paste is a
    ValueError rather than a SOCKS round-trip against a hostname tor would reject
    anyway. The transport is deliberately **never started** here — that is the claim:
    on an unstarted transport a bad onion must still fail as a bad onion, not as
    ``tor transport is not started``, because the operator pasting it needs to be told
    which of the two is wrong."""
    async def scenario():
        with pytest.raises(ValueError):
            await tornet.transport("unstarted").dial("not-an-onion")

    _run(scenario(), 30.0)


def test_a_dead_daemon_stops_the_onion_being_advertised(tornet):
    """Degradation past bootstrap, against a process that really dies: kill the
    daemon and the transport must stop claiming an address, so the node stops
    advertising and dialing an onion nothing answers."""
    async def scenario():
        transport = tornet.transport("doomed")
        assert await transport.start(_Echo(),
                                     bootstrap_timeout=tornet.backend.bootstrap)
        try:
            assert transport.onion_address() is not None
            transport._proc.kill()
            await transport._proc.wait()
            assert transport.onion_address() is None
        finally:
            await transport.stop()

    _run(scenario(), tornet.backend.bootstrap + 60.0)


def test_stop_reaps_the_daemon_and_frees_its_data_directory(tornet):
    """``stop()`` has to leave nothing behind: a surviving daemon would hold the
    ``DataDirectory`` lock and keep the *next* node LAN-only. Proven by starting a
    second transport on the same directory — which is exactly what a restart does,
    and what a lingering child would break."""
    async def scenario():
        first = tornet.transport("reaped")
        assert await first.start(_Echo(), bootstrap_timeout=tornet.backend.bootstrap)
        proc = first._proc
        await first.stop()
        assert proc.returncode is not None, "the tor child outlived stop()"
        assert first._forward_server is None

        second = tornet.transport("reaped")
        assert await second.start(_Echo(), bootstrap_timeout=tornet.backend.bootstrap), (
            "a second transport could not start — the first left its lock held")
        await second.stop()

    _run(scenario(), tornet.backend.bootstrap * 2 + 60.0)


# MARK: - the transport: failures of the daemon itself


def _sim_only(tornet, why: str) -> None:
    if tornet.backend.is_real:
        pytest.skip(f"{why} — staged through the simulated daemon only")


def test_a_daemon_that_never_finishes_bootstrapping_gives_up(tornet, monkeypatch):
    """A tor that comes up, binds, and then stalls below 100% (a censored network, a
    broken consensus). ``start()`` must return False on its own timeout and leave
    nothing running — the node then runs LAN-only instead of waiting forever."""
    _sim_only(tornet, "a real tor cannot be told to stall")
    monkeypatch.setenv("SIMTOR_FAIL", "bootstrap")

    async def scenario():
        transport = tornet.transport("stalled")
        assert await transport.start(_Echo(), bootstrap_timeout=2.0) is False
        assert transport.onion_address() is None
        assert transport._proc is None, "the stalled daemon was left running"

    _run(scenario(), 60.0)


def test_a_daemon_that_dies_during_bootstrap_fails_fast(tornet, monkeypatch):
    """The bad-torrc / crash-on-start case: the process exits before 100%. The
    transport must notice the exit rather than wait out the whole bootstrap budget,
    so this gives it a 60s budget and demands an answer inside 15."""
    _sim_only(tornet, "a real tor cannot be told to die on cue")
    monkeypatch.setenv("SIMTOR_FAIL", "exit")

    async def scenario():
        transport = tornet.transport("crashed")
        return await asyncio.wait_for(
            transport.start(_Echo(), bootstrap_timeout=60.0), timeout=15.0)

    assert _run(scenario(), 60.0) is False


def test_a_bootstrapped_daemon_with_no_hostname_is_not_usable(tornet, monkeypatch):
    """Bootstrap is not the same event as "the service exists". A daemon that reports
    100% but never writes its hostname leaves the node with no address to advertise,
    which is a failed start, not a Tor-enabled node with an empty onion."""
    _sim_only(tornet, "a real tor always writes its hostname")
    monkeypatch.setenv("SIMTOR_FAIL", "nohostname")

    async def scenario():
        transport = tornet.transport("nameless")
        assert await transport.start(_Echo(), bootstrap_timeout=5.0) is False
        assert transport._proc is None

    _run(scenario(), 60.0)


def test_an_onion_that_answers_but_carries_nothing_still_answers(tornet, monkeypatch):
    """An onion can be reachable and still be useless: the address answers the SOCKS
    dial and the stream then dies without a mesh link ever binding (a rotated join
    secret, a reassigned address, an answer-then-drop). ``dial`` **succeeds** here —
    which is exactly why the node resets a peer's Tor backoff when a link *binds*
    rather than when a dial returns. A dial that raised on this would make that
    distinction unobservable, and the throttle would look like it worked for the
    wrong reason."""
    _sim_only(tornet, "a real onion cannot be told to answer-then-drop")
    monkeypatch.setenv("SIMTOR_FAIL", "dropsocks")

    async def scenario():
        server = tornet.transport("mute")
        client = tornet.transport("client")
        assert await server.start(_Echo(), bootstrap_timeout=tornet.backend.bootstrap)
        assert await client.start(_Echo(), bootstrap_timeout=tornet.backend.bootstrap)
        try:
            reader, writer = await _dial_with_patience(
                client, server.onion_address(), tornet.backend.link)
            # The SOCKS CONNECT was accepted — the dial is a success…
            assert await asyncio.wait_for(reader.readline(), timeout=30.0) == b"", (
                "expected the answered stream to die without carrying a link")
            writer.close()
        finally:
            await client.stop()
            await server.stop()

    _run(scenario(), tornet.backend.link + tornet.backend.bootstrap * 2 + 60.0)


def test_a_hostname_written_after_bootstrap_is_still_picked_up(tornet, monkeypatch):
    """The complement, and the reason ``_read_hostname`` retries at all: the file can
    lag the bootstrap line. A start that gave up on the first miss would fail here."""
    _sim_only(tornet, "the lag is staged, not waited for")
    monkeypatch.setenv("SIMTOR_FAIL", "latehostname")

    async def scenario():
        transport = tornet.transport("late")
        assert await transport.start(_Echo(), bootstrap_timeout=10.0)
        try:
            assert tor.is_onion(transport.onion_address() or "")
        finally:
            await transport.stop()

    _run(scenario(), 60.0)


# MARK: - the mesh: whole node processes that can only reach each other over Tor


def test_a_node_brings_up_its_onion_with_no_configuration_at_all(tornet):
    """Tor is on by default, so the node started here is given no Tor setting
    whatsoever (see ``tornet._clean_environ``) — and still comes up with an onion
    service, advertises the address inside its own advert, and reports it ready."""
    node = tornet.node("solo").start().await_running()
    onion = node.await_onion()

    assert tor.is_onion(onion), onion
    state = node.snapshot()
    assert state["tor"]["enabled"] is True
    assert state["tor"]["ready"] is True
    assert state["tor"]["onion"] == onion
    # The advert peers receive carries it — the field that makes the address
    # learnable at all.
    assert state["self"]["onion"] == onion


def test_a_node_told_not_to_run_tor_stays_lan_only(tornet):
    """The off switch, end to end: the node starts and serves exactly as before, with
    no onion service, and an advert that is wire-identical to one from a node that has
    never heard of Tor."""
    node = tornet.node("lanonly", SZPONTNET_TOR="0").start().await_running()

    state = node.snapshot()
    assert state["tor"]["enabled"] is False
    assert state["tor"]["ready"] is False
    assert state["tor"]["onion"] is None
    assert "onion" not in state["self"]


def test_a_node_on_a_machine_with_no_tor_installed_still_runs(tornet):
    """The ordinary machine, now that the transport is on by default: Tor is *wanted*
    and there is no ``tor`` to run it. That must be a node that comes up LAN-only, not
    a node that fails to start or blocks waiting for a daemon that will never exist.

    Note the state combination — ``enabled`` true, ``ready`` false — which is the
    honest one for "asked for, unavailable" and the pair a UI needs to say so.
    """
    node = tornet.node(
        "tor-less",
        SZPONTNET_TOR_BINARY=str(tornet.binary) + "-does-not-exist",
    ).start().await_running()

    state = node.snapshot()
    assert state["tor"]["enabled"] is True     # the operator did not turn it off…
    assert state["tor"]["ready"] is False      # …there is simply no daemon to run
    assert state["tor"]["onion"] is None
    assert "onion" not in state["self"]
    # And it is a working node: it answers control, and it is serving on its port.
    assert state["tcpPort"] > 0


def test_two_nodes_off_each_others_lan_link_over_tor_and_run_a_dispatch(
        tornet, tmp_path):
    """The capstone, and the whole point of the transport: two machines that cannot
    see each other on any LAN (different multicast ports — they never exchange a
    beacon) become one mesh because one pastes the other's onion. The link runs the
    same hello/auth/trust handshake, both sides prove their device keys, and a
    dispatch rides it and *executes* on the far node."""
    landed = tmp_path / "landed.txt"
    executor = tornet.node(
        "executor", SZPONTNET_SPAWN=f"cp {{prompt_file}} {landed}").start()
    executor.await_running()
    onion = executor.await_onion()

    dispatcher = tornet.node("dispatcher").start().await_running()
    dispatcher.await_onion()  # both ends need a live tor before either can dial

    # The manual introduction an operator makes: paste the peer's onion.
    assert dispatcher.ctl({"t": "tor-connect", "onion": onion})["onion"] == onion

    peer = dispatcher.await_linked(executor)
    assert peer["transport"] == "tor", (
        f"linked, but not over Tor: {peer!r}")
    assert peer["addr"] == onion, "the peer's address should be the onion we dialed"
    assert peer["verified"] is True, "device keys were not proven over the Tor link"
    # And the far side sees it as a Tor link too, not as the loopback it lands on.
    assert executor.await_linked(dispatcher)["transport"] == "tor"

    results = dispatcher.ctl(
        {"t": "dispatch", "duty": "audit", "prompt": "hello over tor",
         "target": executor.id}, timeout=60.0)["results"]
    assert results and results[0]["status"] == "spawned", results
    executor.until(landed.exists, tornet.backend.link, "the job never landed")
    assert "hello over tor" in landed.read_text(encoding="utf-8")


def test_a_node_auto_redials_a_known_peer_over_tor_with_no_lan(tornet):
    """The reconnect story, which is what makes the transport more than a manual
    paste: a node that already knows a personal peer's onion — the state it holds
    after they met on a LAN and moved apart — dials it *by itself*, with nobody
    pasting anything. Only the persisted cache is seeded; the redial loop does the
    rest.

    The dialer is the smaller id, because that is the rule that stops both sides
    dialing at once.
    """
    executor = tornet.node("far", node_id="z" * 32).start().await_running()
    onion = executor.await_onion()

    # What a prior LAN meeting leaves behind: the peer's permanent onion, paired
    # with the device fingerprint it was signed by.
    dialer = tornet.node("near", node_id="a" * 32)
    (dialer.dir / "onions.json").write_text(json.dumps({
        executor.id: {"onion": onion,
                      "fingerprint": executor.snapshot()["self"]["fingerprint"]},
    }), encoding="utf-8")

    dialer.start().await_running()
    dialer.await_onion()

    peer = dialer.await_linked(executor)
    assert peer["transport"] == "tor"
    assert peer["verified"] is True


def test_a_peer_met_over_tor_has_its_onion_learned_and_persisted(tornet):
    """A link is also an introduction: the onion rides inside the peer's signed
    advert, so after linking each side holds the other's address on disk and could
    redial it after a restart with nothing pasted a second time."""
    accepter = tornet.node("accepter").start().await_running()
    accepter_onion = accepter.await_onion()
    opener = tornet.node("opener").start().await_running()
    opener_onion = opener.await_onion()

    opener.ctl({"t": "tor-connect", "onion": accepter_onion})
    opener.await_linked(accepter)
    accepter.await_linked(opener)

    # The ACCEPTER learned the opener's onion from the hello it received — it was
    # never told the address, and it is the side that could not have dialed first.
    # Read off disk in the node's own on-disk shape, since surviving a restart is
    # the whole reason the file exists.
    def learned() -> dict:
        try:
            return json.loads(
                (accepter.dir / "onions.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    accepter.until(lambda: opener.id in learned(),
                   tornet.backend.link, "the peer's onion was never persisted")
    entry = learned()[opener.id]
    assert entry["onion"] == opener_onion
    # Persisted against the fingerprint it was signed by, not merely the id.
    assert entry["fingerprint"] == opener.snapshot()["self"]["fingerprint"]
    # And it loads back as the cache the redial loop reads.
    assert onioncache.OnionEntry(**entry).onion == opener_onion


def test_the_onion_serves_peer_links_but_refuses_operator_control(tornet):
    """The security property that most needs a real circuit behind it: the onion is
    advertised to every peer and can be pasted anywhere, so it must carry ``hello``
    and refuse ``ctl`` — otherwise stop/trust/dispatch/set-attr are reachable,
    unauthenticated, by anyone holding the address.

    ``test_mesh_tor.py`` proves the refusal with the transport tag set by hand. This
    proves the tag is really set, by opening both kinds of session through an actual
    onion connection and watching what comes back.
    """
    node = tornet.node("guarded").start().await_running()
    onion = node.await_onion()
    client = tornet.transport("client")

    async def scenario():
        assert await client.start(_Echo(), bootstrap_timeout=tornet.backend.bootstrap)
        try:
            # (a) a control session over the onion is refused: closed, no reply.
            reader, writer = await _dial_with_patience(
                client, onion, tornet.backend.link)
            writer.write(protocol.encode({"t": "ctl"})
                         + protocol.encode({"t": "status"}))
            await writer.drain()
            assert await asyncio.wait_for(reader.readline(), timeout=30.0) == b"", (
                "the node answered a ctl session arriving over its onion")
            writer.close()

            # (b) a peer link over the same onion IS served — so (a) is a refusal of
            # ctl specifically, not an onion that carries nothing.
            reader, writer = await _dial_with_patience(
                client, onion, tornet.backend.link)
            writer.write(protocol.encode({"t": "hello", "node": _stranger_advert()}))
            await writer.drain()
            answer = protocol.decode(
                await asyncio.wait_for(reader.readline(), timeout=30.0))
            assert answer and answer.get("t") in ("hello", "auth"), answer
            writer.close()
        finally:
            await client.stop()

    _run(scenario(), tornet.backend.link * 2 + tornet.backend.bootstrap + 60.0)

    # And the node is still there and still answering: `snapshot()` IS a ctl session,
    # over loopback. So (a) refused control without the node having stopped serving
    # it — and without the refused session having taken the node down with it.
    assert node.snapshot()["tor"]["ready"] is True


def test_the_operators_own_control_channel_is_still_served_on_loopback(tornet):
    """The other half of the refusal above, and what stops it being vacuous: the very
    same ``ctl`` session, on the very same node, is served when it arrives on the
    node's real TCP port. So the onion refuses control because of *where the
    connection came from* — not because the node has stopped serving control."""
    node = tornet.node("guarded").start().await_running()
    node.await_onion()
    port = node.snapshot()["tcpPort"]

    with socket.create_connection(("127.0.0.1", port), timeout=10.0) as sock:
        sock.sendall(protocol.encode({"t": "ctl"}) + protocol.encode({"t": "status"}))
        reply = protocol.decode(sock.makefile("rb").readline(protocol.MAX_LINE_BYTES))
    assert reply and reply.get("t") == "state", reply
    assert reply["state"]["tor"]["ready"] is True


def _stranger_advert() -> dict:
    """A keyless advert from a node nobody has met — enough to open a peer link and
    see the accept path answer, without needing a second real node."""
    return protocol.NodeInfo(
        id="s" * 32, name="stranger", platform="linux", tier=3, tokens="ok",
        tcp_port=1, pubkey="", onion="").to_dict()


async def _dial_with_patience(transport, onion: str, budget: float):
    """Dial until it works or the budget runs out.

    A first dial can legitimately fail: against the real network the service side
    publishes its descriptor some seconds after it reports bootstrapped, and a client
    that asks before then gets 'host unreachable'. That is precisely why the node
    retries on a backoff rather than dialing once, so a test that dialed once would
    be testing something the node does not do.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    last: Exception | None = None
    while loop.time() < deadline:
        try:
            return await asyncio.wait_for(transport.dial(onion), timeout=60.0)
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            last = exc
            await asyncio.sleep(1.0)
    raise AssertionError(f"never reached {onion} within {budget:g}s (last: {last!r})")
