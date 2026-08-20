"""Partition and recovery: what the mesh does while it is broken, and what it
looks like afterwards.

A leaderless mesh has no repair step — every node recomputes the same pure
function over whatever it can currently see, so "healing" is just the inputs
coming back. That makes the interesting assertions the ones about the interval:
work moves while a machine is gone, the machine is still *visible* while gone,
nothing that comes back is trusted on the strength of what it was before, and
one wedged peer never becomes everybody's problem.
"""

from __future__ import annotations

import errno
from pathlib import Path


def _peer_row(sim, other) -> dict:
    return next((p for p in sim.snapshot()["peers"] if p["id"] == other.id), {})


# MARK: - losing a machine


def test_a_silent_peer_is_marked_down_and_its_work_moves(simnet):
    """The only failure a mesh can actually detect: heartbeats stop. Nothing is
    closed, no error is raised — the peer simply stops being in the live set, and
    every survivor recomputes without it."""
    async def scenario():
        a = await simnet.node("a", platform="linux")
        b = await simnet.node("b", platform="macos")
        await simnet.linked(a, b)
        await simnet.until(lambda: a.assigned("audit") == (a.id, b.id), 4.0,
                           "the two machines never covered both platforms")

        # The machine, not just its link: a frozen link is redialled, and the state
        # below then holds only for the ~50ms between the reap and the reconnect.
        simnet.isolate(b)
        await simnet.until(lambda: a.link_state(b) == "down", 4.0,
                           "a silent peer was never marked down")
        await simnet.until(lambda: a.assigned("audit") == (a.id,), 4.0,
                           "the dead machine's slot was never given up")

        shortfall = a.snapshot()["assignments"]["audit"]["shortfall"]
        assert shortfall == [{"platform": "macos", "missing": 1}], \
            "an uncoverable slot must be reported, not silently dropped"

    simnet.run(scenario())


def test_a_dead_peer_stays_visible_so_the_operator_can_see_what_died(simnet):
    """A shrinking list answers "is anything wrong?" with silence. The snapshot
    keeps the corpse, marked down, with how long ago it was last heard from.

    Timed so the assertion lands in the gap the reaper leaves: a peer reads
    `down` the moment its heartbeats age out, while its socket survives until the
    next heartbeat tick — here seconds later. Everything the snapshot says about
    the peer has to agree during that gap, not only after it closes.
    """
    async def scenario():
        a = await simnet.node("a", TIMEOUT_SECS="0.3", HEARTBEAT_SECS="2.0")
        b = await simnet.node("b")
        await simnet.linked(a, b)

        a.link_to(b).freeze()
        await simnet.until(lambda: _peer_row(a, b).get("link") == "down", 4.0,
                           "the peer never went down")
        assert a.peer(b).linked, "the reaper beat the assertion to it"

        row = _peer_row(a, b)
        assert row["id"] == b.id and row["name"] == "b"
        assert row["lastSeenSecsAgo"] > 0
        assert row["uptimeSecs"] is None, \
            "the same snapshot calls this peer down and shows it a live-link badge"

    simnet.run(scenario())


def test_a_wedged_peer_does_not_stall_the_loop_that_serves_every_other(simnet):
    """A peer that stops *reading* is alive and talking but cannot be written to.
    An unbounded flush to it would park the one loop that heartbeats, times out
    and reaps every other peer — one wedged machine freezing the whole mesh."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        c = await simnet.node("c")
        await simnet.linked(a, b, c)

        a.link_to(b).stall_writes_from(a)
        mark = len(simnet.carried)
        await simnet.until(
            lambda: len(simnet.frames("heartbeat", src=a, dst=c,
                                      of=simnet.carried[mark:])) >= 3, 4.0,
            "the wedged peer stalled the heartbeat loop for everybody")
        assert a.link_state(c) == "up"

    simnet.run(scenario())


# MARK: - getting it back


def test_a_partition_heals_and_the_mesh_reconverges(simnet):
    async def scenario():
        a = await simnet.node("a", platform="linux")
        b = await simnet.node("b", platform="macos")
        await simnet.linked(a, b)
        agreed = await _wait_agreed(simnet, a, b)

        simnet.cut(a, b)
        await simnet.until(lambda: a.assigned("audit") == (a.id,), 4.0,
                           "the partition never moved the work")

        simnet.heal_all()
        await simnet.linked(a, b, timeout=8.0)
        await simnet.until(lambda: a.assigned("audit") == agreed
                           and b.assigned("audit") == agreed, 6.0,
                           "the healed mesh never returned to the same placement")

    simnet.run(scenario())


async def _wait_agreed(simnet, *sims) -> tuple:
    """The audit assignment once every given node agrees on it."""
    await simnet.until(
        lambda: len({s.assigned("audit") for s in sims}) == 1
        and len(sims[0].assigned("audit")) == 2, 6.0,
        "the mesh never agreed on an assignment to begin with")
    return sims[0].assigned("audit")


def test_two_halves_of_a_split_each_carry_on_and_then_agree_again(simnet):
    """Split-brain is not an error state here: each half is a smaller mesh that
    places work over what it can see. What matters is that no half needs to be
    told it lost, and that reuniting needs no reconciliation step."""
    async def scenario():
        a = await simnet.node("a", platform="linux")
        b = await simnet.node("b", platform="macos")
        c = await simnet.node("c", platform="linux")
        d = await simnet.node("d", platform="macos")
        await simnet.linked(a, b, c, d)
        whole = await _wait_agreed(simnet, a, b, c, d)

        simnet.partition([a, b], [c, d])
        await simnet.until(
            lambda: a.assigned("audit") == (a.id, b.id)
            and c.assigned("audit") == (c.id, d.id), 6.0,
            "each half never settled on its own placement")
        assert a.assigned("audit") != c.assigned("audit")

        simnet.heal_all()
        await simnet.until(
            lambda: len({s.assigned("audit") for s in (a, b, c, d)}) == 1, 10.0,
            "the reunited mesh never agreed again")
        assert a.assigned("audit") == whole

    simnet.run(scenario())


def test_a_reconnecting_peer_proves_its_key_again(simnet):
    """One end can give up on the other while the network is still eating the
    FIN, so the survivor holds a half-open link and then sees a *second*
    connection for a peer it thinks it already has. Trust proven on the old link
    says nothing about who is on the new one — a captured advert replayed there
    would otherwise inherit a personal machine's standing with no private key."""
    async def scenario():
        a = await simnet.node("a", TIMEOUT_SECS="0.4")
        # B is far more patient, and keeps an allowlist — so what it believes
        # about A is worth something rather than being everyone's default.
        b = await simnet.node("b", TIMEOUT_SECS="30", trust="foreign")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)
        b.trusts(a)
        assert b.trust_of(a) == "personal"
        first = a.link_to(b)

        simnet.cut(a, b)
        await simnet.until(lambda: not a.linked_to(b), 4.0,
                           "the impatient side never gave up")
        assert b.linked_to(a), "the scenario needs B still holding the old link"

        # The reconnection's proof of possession never lands, which is exactly
        # what an attacker replaying A's captured advert could not produce.
        hellos = len(simnet.frames("hello", src=a, dst=b))
        simnet.drop_kind("auth", src=a, dst=b)
        simnet.heal_all()
        await simnet.until(
            lambda: len(simnet.frames("hello", src=a, dst=b)) > hellos, 8.0,
            "the reconnection's hello never reached B")
        assert b.link_to(a) is not first
        await simnet.quiet(0.3)

        assert not b.verified(a), "the new link inherited a proof it never gave"
        assert b.trust_of(a) == "foreign", \
            "an unproven link took over a personal peer's standing"

    simnet.run(scenario())


def test_a_restart_comes_back_as_a_new_incarnation(simnet):
    """The epoch is what tells a survivor that this is the same machine but not
    the same process — the difference between a flap and everything the old
    process was holding being gone."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        before = a.node.peers[b.id].info.epoch

        await b.restart()
        await simnet.linked(a, b, timeout=8.0)
        assert a.node.peers[b.id].info.epoch > before
        await simnet.all_verified(a, b)

    simnet.run(scenario())


def test_a_result_owed_across_a_flap_is_delivered_when_the_link_returns(
        simnet, monkeypatch):
    """Delivery is retried on the originator's *current* link, looked up fresh
    each time — the link that was there when the work finished is precisely the
    one that may not be there any more."""
    from szpontnet import spawnjob

    sandbox: list[tuple[str, str]] = []
    monkeypatch.setattr(spawnjob, "spawn_confined",
                        lambda prompt, result_file: sandbox.append(
                            (prompt, result_file)) or "/sim/staged.txt")

    async def scenario():
        a = await simnet.node("a", TIMEOUT_SECS="0.4")
        b = await simnet.node("b", trust="foreign", TIMEOUT_SECS="0.4",
                              FOREIGN_SPAWN="sandbox {prompt_file}",
                              RESULT_MAX_SECS="30")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)

        results = await a.dispatch("review", "compute across a flap", target=b.id)
        assert results[0]["status"] == "spawned", results
        await simnet.until(lambda: bool(sandbox), 3.0, "the sandbox never started")

        simnet.cut(a, b)
        await simnet.until(lambda: not b.linked_to(a), 4.0,
                           "the executor never noticed the link go")
        Path(sandbox[0][1]).write_text("finished while apart", encoding="utf-8")
        await simnet.until(lambda: bool(b.node._pending_results), 5.0,
                           "the executor never took ownership of the artifact")
        assert not simnet.frames("job-result", src=b, dst=a)

        simnet.heal_all()
        await simnet.linked(a, b, timeout=8.0)
        await simnet.until(lambda: simnet.frames("job-ack", src=a, dst=b), 8.0,
                           "the owed artifact was never delivered after healing")
        assert simnet.frames("job-result", src=b, dst=a)[-1].payload()[
            "result"]["output"] == "finished while apart"

    simnet.run(scenario())


# MARK: - a node that cannot be discovered says so


def test_a_total_beacon_outage_is_diagnosed_rather_than_silent(simnet):
    """Every beacon send failing makes a node undiscoverable while its existing
    links keep working — so it looks healthy and the mesh looks broken. The
    classic cause is an OS privacy gate, which is a thing the operator can fix,
    but only if anybody tells them."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        # Off-host sends are refused while the stack itself is fine: that pair of
        # facts is the whole diagnosis, so pin the loopback probe rather than
        # letting a test reach for this machine's real network.
        a.node._loopback_send_ok = lambda: True
        a.beacon_send_errno = errno.EHOSTUNREACH

        await simnet.until(lambda: a.snapshot()["beaconBlocked"], 4.0,
                           "a total beacon outage went unreported")
        assert a.snapshot()["beaconBlockReason"] == "local-network"
        assert [d for n, _, d in simnet.log
                if n == "a" and "cannot discover it" in d], \
            "the operator was never told, or was told the wrong thing"
        assert a.linked_to(b), "existing links are unaffected by the send outage"

        a.beacon_send_errno = None
        await simnet.until(lambda: not a.snapshot()["beaconBlocked"], 4.0,
                           "the node never noticed its beacons working again")
        assert a.snapshot()["beaconBlockReason"] == ""

    simnet.run(scenario())


def test_a_downed_network_is_not_reported_as_a_permission_problem(simnet):
    """The message names the fix, so naming the wrong one costs the operator an
    afternoon in a settings pane that was never the problem."""
    async def scenario():
        a = await simnet.node("a")
        a.node._loopback_send_ok = lambda: False  # even loopback is gone
        a.beacon_send_errno = errno.ENETDOWN

        await simnet.until(lambda: a.snapshot()["beaconBlocked"], 4.0,
                           "a total beacon outage went unreported")
        assert a.snapshot()["beaconBlockReason"] == "network-down"
        assert [d for n, _, d in simnet.log
                if n == "a" and "network stack looks down" in d]

    simnet.run(scenario())
