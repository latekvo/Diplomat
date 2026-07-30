"""Dispatch: where a request goes, what happens when it is refused, and what the
dispatcher is told when the answer never comes back.

Routing is one node's unilateral call — there is no consensus round and no
acknowledgement that the *mesh* agrees. That makes two things worth pinning: the
ranking really is what picks the machine, and every way a target can say no ends
as a slot outcome the caller can act on rather than as a hang.
"""

from __future__ import annotations

import asyncio


def _slot(results: list[dict], name: str) -> dict:
    return next(r for r in results if r["slot"] == name)


# MARK: - placement into slots


def test_a_spread_duty_runs_one_job_per_platform_slot(simnet):
    """The audit duty asks for one linux and one macOS machine; a spread is the
    one case where a single request becomes several jobs."""
    async def scenario():
        a = await simnet.node("a", platform="linux")
        b = await simnet.node("b", platform="macos")
        await simnet.linked(a, b)

        results = await a.dispatch("audit", "bundle e2e")
        assert _slot(results, "linux") == {
            "slot": "linux", "node": a.id, "nodeName": "a",
            "status": "spawned", "reason": ""}
        assert _slot(results, "macos")["node"] == b.id
        assert _slot(results, "macos")["status"] == "spawned"
        assert [p for p, _ in a.jobs] == ["bundle e2e"]
        assert [p for p, _ in b.jobs] == ["bundle e2e"]

    simnet.run(scenario())


def test_a_platform_with_no_machine_is_a_failed_slot_not_a_silent_one(simnet):
    """A shortfall has to surface: a duty that looks placed but never ran is the
    failure mode that costs a day of not noticing."""
    async def scenario():
        a = await simnet.node("a", platform="linux")
        b = await simnet.node("b", platform="linux")
        await simnet.linked(a, b)

        results = await a.dispatch("audit", "bundle e2e")
        macos = _slot(results, "macos")
        assert macos["status"] == "failed" and macos["node"] is None
        assert macos["reason"] == "no eligible node"

    simnet.run(scenario())


def test_work_flows_to_the_node_with_the_most_spare_quota(simnet):
    """Surplus-first is the load balancer. It ranks on pace — budget left over
    clock left — so the machine that can most afford the work gets it."""
    async def scenario():
        a = await simnet.node("a", quota=("ok", 1.0, None, None, 0.4))  # rationing
        b = await simnet.node("b", quota=("ok", 1.0, None, None, 3.0))  # flush
        c = await simnet.node("c", quota=("ok", 1.0, None, None, 1.0))  # on pace
        await simnet.linked(a, b, c)
        await simnet.until(lambda: a.node.peers[b.id].info.surplus() == 3.0, 3.0,
                           "the flush node's surplus never reached its peers")

        results = await a.dispatch("review", "who has room")
        assert _slot(results, "any")["node"] == b.id, results
        assert [p for p, _ in b.jobs] == ["who has room"]
        assert a.jobs == [] and c.jobs == []

    simnet.run(scenario())


def test_a_node_out_of_tokens_is_not_a_target(simnet):
    async def scenario():
        a = await simnet.node("a", quota=("ok", 1.0, None, None, 0.1))
        b = await simnet.node("b", quota=("out", 0.0, None, None, 9.0))
        await simnet.linked(a, b)
        await simnet.until(lambda: a.node.peers[b.id].info.tokens == "out", 3.0,
                           "the exhausted node never advertised it")

        results = await a.dispatch("review", "spend nothing")
        # Ranked first on surplus, and still not a candidate: an exhausted node
        # is dropped before ranking, so it is never even asked. Letting it be
        # asked and decline would land the same job in the same place — and cost
        # a round trip per dispatch to a machine that advertised it was empty.
        assert _slot(results, "any")["node"] == a.id, results
        assert not simnet.frames("dispatch", src=a, dst=b)
        assert b.jobs == []

    simnet.run(scenario())


# MARK: - failing over


def test_a_slot_fails_over_to_the_next_candidate(simnet):
    """A refusal is not an error — it is the dispatcher's cue to try the next
    machine. A slot only fails once every candidate has said no.

    The refusal here is one the *ranking cannot see*: B advertises a duty it is
    willing to run and the most spare quota in the mesh, and only turns out to
    have no way to run the job when it is actually asked."""
    async def scenario():
        a = await simnet.node("a", quota=("ok", 1.0, None, None, 0.5))
        b = await simnet.node("b", quota=("ok", 1.0, None, None, 5.0))
        b.runner_error = "no runner on this machine"
        await simnet.linked(a, b)
        await simnet.until(lambda: a.node.peers[b.id].info.surplus() == 5.0, 3.0,
                           "B's surplus never arrived, so it would not rank first")

        results = await a.dispatch("review", "somebody run this")
        assert simnet.frames("dispatch", src=a, dst=b), \
            "the preferred candidate was never asked"
        assert _slot(results, "any")["node"] == a.id
        assert _slot(results, "any")["status"] == "spawned"
        assert b.jobs == [] and [p for p, _ in a.jobs] == ["somebody run this"]

    simnet.run(scenario())


def test_a_duty_nobody_will_run_is_reported_failed(simnet):
    async def scenario():
        a = await simnet.node("a", duties={"review": False})
        b = await simnet.node("b", duties={"review": False})
        await simnet.linked(a, b)

        results = await a.dispatch("review", "nobody wants this")
        assert _slot(results, "any")["status"] == "failed"
        assert a.jobs == [] and b.jobs == []

    simnet.run(scenario())


# MARK: - an explicit target


def test_an_explicit_target_is_asked_and_never_failed_over(simnet):
    """"Alice may forward everything to Bob, and Bob may refuse." A named target
    is the client overriding placement, so a refusal is the answer — not the
    start of a search for someone more willing."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b", duties={"review": False})
        c = await simnet.node("c")
        await simnet.linked(a, b, c)

        results = await a.dispatch("review", "you specifically", target=b.id)
        assert results == [{"slot": "target", "node": b.id, "nodeName": "b",
                            "status": "declined",
                            "reason": "duty review disabled here"}]
        assert c.jobs == [] and a.jobs == [], "a named target must not fail over"

    simnet.run(scenario())


def test_dispatching_to_a_node_we_have_no_link_to_fails_the_slot(simnet):
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        simnet.cut(a, b)
        await simnet.until(lambda: not a.linked_to(b), 4.0, "the link never dropped")

        results = await a.dispatch("review", "anyone there", target=b.id)
        assert results[0]["status"] == "failed"
        assert results[0]["reason"] == "no link"

    simnet.run(scenario())


# MARK: - the answer that never comes


def test_a_target_that_never_answers_is_reported_failed_not_awaited_forever(simnet):
    """The ack timeout is what keeps a wedged executor from wedging its caller.
    The dispatcher then re-runs the work elsewhere, which is exactly why it must
    also stop being willing to act on that job's late result."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        simnet.drop_kind("job-status", src=b, dst=a)

        results = await a.dispatch("review", "silence", target=b.id)
        assert results[0]["status"] == "failed"
        assert results[0]["reason"] == "peer did not answer"
        assert [p for p, _ in b.jobs] == ["silence"], \
            "the job did reach B — this is about the ANSWER being lost"
        assert a.node._awaiting_result == {}, \
            "an unanswered dispatch must not stay armed for a result"
        assert a.node._acted_results, \
            "the job must be marked handled so a late result is never acted on"

    simnet.run(scenario())


def test_a_link_reset_mid_dispatch_fails_the_slot_at_once(simnet):
    """A connection reset is not a timeout: the write fails immediately, and the
    caller should hear about it then rather than after the whole ack window."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        a.link_to(b).reset()

        started = asyncio.get_running_loop().time()
        results = await a.dispatch("review", "into a broken pipe", target=b.id)
        elapsed = asyncio.get_running_loop().time() - started

        assert results[0]["status"] == "failed"
        assert elapsed < a.node.proto["dispatchAckTimeoutSecs"], \
            "a reset link was waited out as if it might still answer"
        assert b.jobs == []
        assert a.node._awaiting_result == {}

    simnet.run(scenario())


def test_a_dispatch_crosses_a_slow_link(simnet):
    """Latency is not failure. A mesh over a slow link (or an onion circuit)
    must still hand work over rather than time its own ack out."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        simnet.link_delay = 0.1  # every frame, both ways

        results = await a.dispatch("review", "worth the wait", target=b.id)
        assert results[0]["status"] == "spawned", results
        assert [p for p, _ in b.jobs] == ["worth the wait"]

    simnet.run(scenario())


def test_only_the_node_we_dispatched_to_may_report_the_outcome(simnet):
    """Job ids are random and never gossiped, so guessing one is infeasible — but
    a peer that shares the link must still not be able to resolve a dispatch
    aimed at somebody else, including by reporting a success that never happened."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        c = await simnet.node("c")
        await simnet.linked(a, b, c)
        simnet.drop_kind("job-status", src=b, dst=a)

        pending = asyncio.ensure_future(
            a.dispatch("review", "for b only", target=b.id))
        await simnet.until(lambda: simnet.frames("dispatch", src=a, dst=b), 3.0,
                           "the dispatch never went out")
        job_id = simnet.frames("dispatch", src=a, dst=b)[-1].payload()["job"]["id"]
        c.inject_to(a, {"t": "job-status", "id": job_id, "status": "spawned",
                        "reason": "", "node": c.id})

        results = await pending
        assert results[0]["status"] == "failed", \
            "a third peer resolved a dispatch it was not the target of"
        assert results[0]["reason"] == "peer did not answer"

    simnet.run(scenario())


# MARK: - the roles that refuse


def test_a_server_node_accepts_work_and_never_pushes_any(simnet):
    """The accept-only role: a shared box that takes requests but is never a
    source of them, so asking it to route work makes it run the work."""
    async def scenario():
        a = await simnet.node("a", SERVER="1")
        b = await simnet.node("b")
        await simnet.linked(a, b)

        results = await a.dispatch("audit", "run it here")
        assert results == [{"slot": "server", "node": a.id, "nodeName": "a",
                            "status": "spawned", "reason": ""}]
        assert b.jobs == []

        aimed = await a.dispatch("review", "send it away", target=b.id)
        assert aimed[0]["status"] == "declined"
        assert aimed[0]["reason"] == "server node does not dispatch to peers"
        assert b.jobs == []

    simnet.run(scenario())


def test_an_api_key_gated_node_refuses_a_request_that_lacks_the_key(simnet):
    """The key authenticates who may submit *work*, independently of the join
    secret that admits mesh members — so a member is not automatically a client."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b", API_KEY="hunter2")
        await simnet.linked(a, b)

        refused = await a.dispatch("review", "let me in", target=b.id)
        assert refused[0]["status"] == "declined"
        assert refused[0]["reason"] == "invalid or missing API key"
        assert b.jobs == []

        allowed = await a.dispatch("review", "here is the key", target=b.id,
                                   api_key="hunter2")
        assert allowed[0]["status"] == "spawned"
        assert [p for p, _ in b.jobs] == ["here is the key"]

    simnet.run(scenario())


def test_a_foreign_requester_is_declined_when_no_sandbox_is_configured(simnet):
    """Zero trust, and the safe default: a machine only ever runs a stranger's
    compute when its operator has supplied the jail to run it in."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b", trust="foreign")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)

        results = await a.dispatch("review", "run my code", target=b.id)
        assert results[0]["status"] == "declined"
        assert results[0]["reason"] == \
            "foreign device (no confinement runner configured)"
        assert b.jobs == []

    simnet.run(scenario())


def test_a_banned_device_is_neither_a_target_nor_a_requester(simnet):
    """A ban is this node's local mark on a device that broke a promise. It has
    to bite in both directions, and without asking the banned machine anything."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)

        reply = await a.ctl({"t": "ban", "fingerprint": b.node.fingerprint,
                             "reason": "test"})
        assert reply == {"t": "ok"}

        outgoing = await a.dispatch("review", "still willing?", target=b.id)
        assert outgoing[0]["status"] == "declined"
        assert outgoing[0]["reason"] == "target is banned here"
        assert not simnet.frames("dispatch", src=a, dst=b), \
            "a banned device must not even be asked"

        incoming = await b.dispatch("review", "how about now", target=a.id)
        assert incoming[0]["status"] == "declined"
        assert incoming[0]["reason"] == "banned device"
        assert a.jobs == []

    simnet.run(scenario())


def test_a_ban_never_changes_what_the_mesh_agrees_is_assigned(simnet):
    """A ban is local and never gossiped, so it must not move the assignment
    view — two machines would then disagree about who owns a duty, which is the
    one thing the leaderless design cannot tolerate."""
    async def scenario():
        a = await simnet.node("a", platform="linux")
        b = await simnet.node("b", platform="macos")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)
        await simnet.until(lambda: a.assigned("audit") == b.assigned("audit")
                           and len(a.assigned("audit")) == 2, 4.0,
                           "the two nodes never agreed on the audit assignment")
        agreed = a.assigned("audit")

        await a.ctl({"t": "ban", "fingerprint": b.node.fingerprint,
                     "reason": "test"})
        await simnet.quiet(0.3)
        assert a.assigned("audit") == agreed == b.assigned("audit")

    simnet.run(scenario())
