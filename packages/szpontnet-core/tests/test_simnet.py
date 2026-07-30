"""The virtual LAN's own tests — a harness that lies is worse than no harness.

Everything else in this suite reads "the mesh did X under condition Y", and every
one of those verdicts is only worth what the simulator underneath is worth. So
each control this module offers is pinned here against its own opposite: a cut
really stops traffic (and healing really restores it), a drop filter really drops
(and only what it names), a frozen link really goes silent without closing, and
two nodes in one interpreter really do keep separate state.
"""

from __future__ import annotations

import simnet


# MARK: - the environment overlay (what lets many nodes share one interpreter)


def test_each_node_gets_its_own_state_directory(simnet):
    """The whole many-nodes-one-process trick. If this fails, every other test in
    the suite is two nodes wearing one identity."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")

        assert a.id != b.id
        with a.active():
            from szpontnet import identity
            a_dir = identity.mesh_dir()
        with b.active():
            from szpontnet import identity
            b_dir = identity.mesh_dir()
        assert a_dir != b_dir
        assert (a_dir / "node.json").exists() and (b_dir / "node.json").exists()
        # Device keys are per machine, so trust can distinguish them at all.
        assert a.node.fingerprint and a.node.fingerprint != b.node.fingerprint

    simnet.run(scenario())


def test_a_nodes_own_loops_keep_resolving_to_its_own_state(simnet):
    """A node's background tasks outlive the call that started them, so the
    context they inherited is what they will resolve against forever. The
    snapshot loop is the visible one: each node must write ITS state.json."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        await simnet.until(
            lambda: all((sim.env["DIR"] and _state_of(sim)) for sim in (a, b)),
            3.0, "a node never wrote its own snapshot")
        assert _state_of(a)["self"]["id"] == a.id
        assert _state_of(b)["self"]["id"] == b.id

    simnet.run(scenario())


def _state_of(sim):
    from pathlib import Path
    import json

    path = Path(sim.env["DIR"]) / "state.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


# MARK: - discovery and delivery actually work


def test_nodes_find_each_other_over_the_virtual_beacon_bus(simnet):
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)
        # Awaited, not asserted outright: one beacon is enough to link (the node
        # that hears it dials), so on a fast machine the pair can be linked and
        # verified before the other side's next beacon interval comes round.
        await simnet.until(
            lambda: simnet.beacon_hops(a, b) > 0 and simnet.beacon_hops(b, a) > 0,
            4.0, "beacons never crossed the bus in both directions")
        assert simnet.frames("hello"), "no hello crossed the virtual switch"

    simnet.run(scenario())


def test_a_frame_the_switch_carries_is_the_frame_the_peer_reads(simnet):
    """The recorder is used for assertions all over this suite, so it must record
    what actually crossed rather than what was offered."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        await simnet.until(lambda: simnet.frames("heartbeat", src=a, dst=b), 3.0,
                           "no heartbeat recorded from a to b")
        beat = simnet.frames("heartbeat", src=a, dst=b)[-1]
        assert beat.payload()["t"] == "heartbeat"

    simnet.run(scenario())


# MARK: - the failure controls


def test_a_cut_stops_delivery_without_closing_the_link(simnet):
    """What a partition is: the socket is still open at both ends, and not one
    byte gets across. If a cut closed links instead, every recovery test here
    would be testing a socket error rather than a silent peer."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        link = a.link_to(b)
        assert link is not None and not link.closed

        simnet.cut(a, b)
        before = len(simnet.carried)
        await simnet.quiet(0.3)  # many heartbeat intervals
        assert not link.closed, "a cut must not close the connection"
        assert [f for f in simnet.carried[before:]
                if f.src in (a.id, b.id) and f.dst in (a.id, b.id)] == []
        assert simnet.dropped, "the cut frames were not recorded as dropped"

        simnet.heal(a, b)
        healed = len(simnet.carried)
        await simnet.until(
            lambda: any(f.src == a.id and f.dst == b.id
                        for f in simnet.carried[healed:]), 3.0,
            "healing did not restore delivery")

    simnet.run(scenario())


def test_isolating_a_node_cuts_its_beacons_too(simnet):
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        assert simnet.beacon_hops(b, a) > 0  # they heard each other before the cut
        simnet.isolate(b)
        mark = len(simnet.beacons_delivered)
        await simnet.quiet(0.3)  # several beacon intervals
        assert simnet.beacon_hops(b, a, since=mark) == 0
        assert simnet.beacon_hops(a, b, since=mark) == 0
        # It still hears its own multicast loopback — an isolated machine's own
        # stack is fine, it is the LAN around it that is gone.
        assert simnet.beacon_hops(b, b, since=mark) > 0

    simnet.run(scenario())


def test_a_drop_filter_drops_only_what_it_names(simnet):
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        handle = simnet.drop_kind("heartbeat", src=a, dst=b)
        mark = len(simnet.carried)
        await simnet.quiet(0.3)
        assert not simnet.frames("heartbeat", src=a, dst=b, of=simnet.carried[mark:])
        assert simnet.frames("heartbeat", src=b, dst=a, of=simnet.carried[mark:]), \
            "the reverse direction must be untouched"
        handle.remove()
        resumed = len(simnet.carried)
        await simnet.until(
            lambda: simnet.frames("heartbeat", src=a, dst=b,
                                  of=simnet.carried[resumed:]), 3.0,
            "removing the filter did not restore delivery")

    simnet.run(scenario())


def test_drop_kind_can_expire_after_a_fixed_number(simnet):
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        mark = len(simnet.dropped)
        simnet.drop_kind("heartbeat", src=a, dst=b, times=2)
        await simnet.until(
            lambda: len(simnet.frames("heartbeat", src=a, dst=b,
                                      of=simnet.dropped[mark:])) == 2, 3.0,
            "the first two heartbeats were not dropped")
        after = len(simnet.carried)
        await simnet.until(
            lambda: simnet.frames("heartbeat", src=a, dst=b,
                                  of=simnet.carried[after:]), 3.0,
            "delivery did not resume once the drop budget ran out")
        assert len(simnet.frames("heartbeat", src=a, dst=b,
                                 of=simnet.dropped)) == 2

    simnet.run(scenario())


def test_a_frozen_link_goes_silent_but_stays_open(simnet):
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        link = a.link_to(b)
        link.freeze()
        mark = len(simnet.carried)
        await simnet.quiet(0.2)
        assert not [f for f in simnet.carried[mark:]
                    if {f.src, f.dst} == {a.id, b.id}]
        assert not link.closed
        link.thaw()
        thawed = len(simnet.carried)
        await simnet.until(
            lambda: [f for f in simnet.carried[thawed:]
                     if {f.src, f.dst} == {a.id, b.id}], 3.0,
            "thawing did not restore the link")

    simnet.run(scenario())


def test_beacon_loss_drops_between_machines_only(simnet):
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        simnet.beacon_loss = 1.0  # total loss between machines
        mark = len(simnet.beacons_delivered)
        await simnet.quiet(0.3)
        assert simnet.beacon_hops(a, b, since=mark) == 0
        # A node's own multicast loopback never leaves the host, so the loss rate
        # is not the thing that decides whether it arrives.
        assert simnet.beacon_hops(a, a, since=mark) > 0

    simnet.run(scenario())


# MARK: - a whole-mesh smoke, so the harness is proven end to end


def test_a_dispatch_crosses_the_virtual_lan_and_runs_on_the_peer(simnet):
    """The capstone for the harness: discovery, dial, handshake, trust, routing
    and execution all the way through, with nothing real but the node."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)

        results = await a.dispatch("review", "look at this", target=b.id)
        assert [r["status"] for r in results] == ["spawned"], results
        assert b.jobs == [("look at this", None)]
        assert a.jobs == [], "the job must have run on b, not on the dispatcher"

    simnet.run(scenario())
