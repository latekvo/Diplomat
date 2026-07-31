"""Discovery and linking: who dials whom, how many links that leaves, and what
happens when the beacon channel — or the peer — stops being honest.

Discovery is the one part of the mesh with no authentication at all: a beacon is
a UDP datagram anybody on the LAN can forge, carrying an id, a port and an epoch.
Everything here is therefore either about the deterministic rule that keeps a
pair to exactly one link, or about a forged beacon failing to buy anything.
"""

from __future__ import annotations

import time

from szpontnet import node as nodemod, protocol

FOREIGN_IP = "10.9.9.9"  # an address no simulated machine owns


def _beacon(node_id: str, *, port: int = 40878, epoch: float = 0.0,
            name: str = "stranger") -> dict:
    return {"t": "beacon", "id": node_id, "name": name, "platform": "linux",
            "tcpPort": port, "epoch": epoch}


def _dial_attempts(net) -> int:
    return net.connects + net.connects_refused


# MARK: - the dial rule


def test_the_smaller_id_dials_and_the_pair_holds_one_link(simnet):
    """02-discovery's whole answer to the dial race: both machines see each
    other's beacon, and the id ordering — not timing — decides who connects."""
    async def scenario():
        a = await simnet.node("a")          # id "a"*32, sorts below
        b = await simnet.node("b")          # id "b"*32
        await simnet.linked(a, b)

        assert simnet.connects == 1, "exactly one connection should exist"
        assert simnet.links[0].client is a, "the smaller id must be the dialer"
        assert simnet.links[0].server is b

    simnet.run(scenario())


def test_repeated_beacons_never_open_a_second_link(simnet):
    """Beacons repeat far faster than a handshake completes, so the dedupe has to
    hold for the whole life of a link, not just until the peer table learns it."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        await simnet.quiet(0.4)  # ~8 beacon intervals

        assert simnet.connects == 1
        assert a.linked_to(b) and b.linked_to(a)

    simnet.run(scenario())


def test_a_beacon_whose_port_no_socket_could_hold_is_never_dialled(simnet):
    """An out-of-range port is not merely invalid: ``open_connection`` raises
    OverflowError (not OSError) from the C bind, which escapes the dial task's
    guard. And ``True`` is an int, so a JSON boolean would pass a naive check as
    port 1."""
    async def scenario():
        a = await simnet.node("a")
        before = _dial_attempts(simnet)
        for port in (0, -1, 70000, True, False, "40878", None):
            simnet.inject_beacon(a, _beacon("z" * 32, port=port), FOREIGN_IP)
        await simnet.quiet(0.2)

        assert _dial_attempts(simnet) == before, "a nonsense port was dialled"
        assert "z" * 32 not in a.node.peers

    simnet.run(scenario())


def test_a_beacon_flood_never_grows_the_peer_table(simnet):
    """A beacon is unauthenticated, so the peer table must stay a record of who
    actually completed a handshake — otherwise a flooder owns the snapshot, the
    gossip fan-out and the assignment input for free."""
    async def scenario():
        a = await simnet.node("a")
        for i in range(50):
            simnet.inject_beacon(a, _beacon(f"{i:032d}"), FOREIGN_IP)
        await simnet.quiet(0.3)

        assert a.node.peers == {}
        assert a.node._dial_tasks == set(), "dial tasks leaked from a flood"

    simnet.run(scenario())


def test_gossip_cannot_grow_the_peer_table_past_the_cap(simnet, monkeypatch):
    """The cap belongs to every path that grows the table, not just the beacon
    one: a single linked peer relaying spoofed `node` gossip is the cheaper
    flood, and it arrives inside the join fence."""
    monkeypatch.setattr(nodemod, "_MAX_PEERS", 3)

    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)

        for i in range(20):
            info = protocol.NodeInfo(id=f"{i:032d}", name=f"ghost-{i}",
                                     platform="linux", tier=3, tokens="ok")
            b.inject_to(a, {"t": "node", "node": info.to_dict()})
        await simnet.quiet(0.2)

        assert len(a.node.peers) == 3, sorted(a.node.peers)
        assert b.id in a.node.peers, "the real peer must not be crowded out"

    simnet.run(scenario())


# MARK: - a beacon carrying our own id


def test_a_cloned_node_id_is_reported_once(simnet):
    """Two machines sharing a node.json never link (each ignores the other's
    beacon) and make a third flip-flop between them, so the collision has to be
    diagnosable rather than silent — and said once, not once per beacon."""
    async def scenario():
        a = await simnet.node("a")
        for _ in range(5):
            simnet.inject_beacon(a, _beacon(a.id), FOREIGN_IP)
        await simnet.quiet(0.1)

        warnings = [d for n, _, d in simnet.log
                    if n == "a" and "advertises our node id" in d]
        assert len(warnings) == 1, warnings

    simnet.run(scenario())


def test_our_own_beacon_looping_back_is_not_a_clone(simnet):
    """Multicast loopback means every node hears itself on every tick. Reporting
    that as a duplicate node.json would make the warning noise, and noise is how
    a real duplicate gets ignored."""
    async def scenario():
        a = await simnet.node("a")
        await simnet.node("b")
        await simnet.quiet(0.3)

        assert simnet.beacon_hops(a, a) > 0, "the loopback path was never exercised"
        assert not [d for _, _, d in simnet.log if "advertises our node id" in d]

    simnet.run(scenario())


# MARK: - forged restart hints


def test_a_forged_restart_beacon_cannot_evict_a_healthy_link(simnet):
    """A higher epoch means the peer *may* have restarted — but anything on the
    LAN can forge one carrying a peer's id and its own address. Honouring it
    against a live, cryptographically verified link would hand an attacker a
    link hijack: evict the real peer, then answer the redial."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)
        link = a.link_to(b)
        attempts = _dial_attempts(simnet)

        simnet.inject_beacon(
            a, _beacon(b.id, port=b.node.tcp_port, epoch=time.time() + 10_000),
            FOREIGN_IP)
        await simnet.quiet(0.2)

        assert a.linked_to(b) and a.link_to(b) is link
        assert a.peer(b).addr == b.ip, "a forged beacon rewrote a live peer's address"
        assert _dial_attempts(simnet) == attempts

    simnet.run(scenario())


def test_a_restart_hint_is_acted_on_once_the_link_has_gone_quiet(simnet):
    """The other half: a genuine restart does leave the old link quiet, and that
    is the condition under which the hint is worth acting on. Timeouts here are
    per node — the reaper is pinned out of the way so the *beacon* path is what
    reconnects, not a heartbeat expiry."""
    async def scenario():
        a = await simnet.node("a", STALE_SECS="0.1", TIMEOUT_SECS="30")
        b = await simnet.node("b", STALE_SECS="0.1", TIMEOUT_SECS="30")
        await simnet.linked(a, b)
        first = a.link_to(b)
        first.freeze()  # b died without closing anything
        await simnet.quiet(0.2)  # past peerStaleSecs: the link is no longer fresh

        simnet.inject_beacon(
            a, _beacon(b.id, port=b.node.tcp_port, epoch=b.node.epoch + 1), b.ip)
        await simnet.until(lambda: a.linked_to(b) and a.link_to(b) is not first,
                           5.0, "the restart hint never produced a new link")
        assert a.peer(b).addr == b.ip

    simnet.run(scenario())


# MARK: - redial from memory (a mesh whose beacon channel died)


def test_a_dead_beacon_channel_still_heals_a_dropped_link(simnet):
    """Beacons ride multicast, which an access point or an OS privacy gate can
    kill while unicast keeps working. Without redial-from-memory a link lost
    during such an outage never comes back — nothing re-triggers the dial."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        assert a.node._peer_cache[b.id] == (b.ip, b.node.tcp_port)

        simnet.beacon_loss = 1.0  # discovery is gone from here on
        a.link_to(b).close()
        await simnet.until(lambda: not a.linked_to(b), 3.0, "the link never dropped")

        await simnet.until(lambda: a.linked_to(b) and b.linked_to(a), 5.0,
                           "the link never healed from the address cache")
        assert simnet.beacon_hops(b, a, since=0) > 0  # …and not via a new beacon
        assert simnet.connects >= 2

    simnet.run(scenario())


def test_the_address_cache_is_persisted_so_it_survives_a_restart(simnet):
    """The cache is on disk precisely so a node that comes back into a dead
    beacon channel still knows where its mesh lives."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)

        from szpontnet import peercache

        with a.active():
            assert peercache.load()[b.id] == (b.ip, b.node.tcp_port)

        simnet.beacon_loss = 1.0
        await a.restart()
        await simnet.until(lambda: a.linked_to(b) and b.linked_to(a), 6.0,
                           "a restarted node never redialled from its cache")

    simnet.run(scenario())


def test_the_cache_is_symmetric_but_the_dial_rule_is_not(simnet):
    """Both ends learn each other's address from the hello, so both could redial
    — and only the smaller id may, or the cache quietly becomes a second,
    unordered dial path and the pair races itself back to two links."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)

        assert b.node._peer_cache[a.id] == (a.ip, a.node.tcp_port), \
            "the larger id must still remember where its peer lives"
        with b.active():
            assert b.node._redial_targets() == [], \
                "the larger id must never redial its smaller-id peer"

    simnet.run(scenario())
