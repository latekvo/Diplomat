"""Work claims: two machines noticing the same external event, and exactly one
of them acting on it.

This is the only genuinely concurrent decision in the protocol. Dispatch routes
one job to one executor, but nothing before this stops two nodes that both poll
the same pull request from *both* originating a review. The claim is a gossiped,
self-signed lease and the owner is a pure function of the book and the live set —
so the tests that matter are the ones where two nodes decide at the same instant,
where the winner dies, and where somebody who is not who they say they are tries
to claim work in order to stop it happening at all.
"""

from __future__ import annotations

from dataclasses import replace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from szpontnet import crypto, node as nodemod, protocol

KEY = "review:github.com/acme/app#123@abc123"


def _claim(node_id: str, work_key: str = KEY, *, key=None, epoch: float = 0.0,
           seq: int = 0, state: str = "active") -> dict:
    """A claim record naming ``node_id``, optionally signed by ``key``."""
    rec = protocol.ClaimRecord(work_key=work_key, node=node_id, epoch=epoch,
                               seq=seq, state=state,
                               pubkey=key.public_b64 if key else "")
    if key is None:
        return rec.to_dict()
    return replace(rec, sig=key.sign(
        protocol.claim_signing_bytes(rec.to_dict()))).to_dict()


# MARK: - the race


def test_the_lowest_id_wins_a_simultaneous_double_claim(simnet):
    """Both machines see the work, both find the key unowned, both announce. No
    bidding round resolves this — the loser hears the winner and stands down."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        lost: list[str] = []
        b.node.on_claim_lost = lost.append

        # Neither has heard the other yet: this is the simultaneous case.
        assert a.claim(KEY) is True
        assert b.claim(KEY) is True

        await simnet.until(
            lambda: a.claim_owner(KEY) == a.id and b.claim_owner(KEY) == a.id,
            4.0, "the mesh never converged on one owner")
        assert lost == [KEY], "the loser must be told to abort what it started"
        assert b.node._own_claim(KEY).state == "released"
        assert a.node._own_claim(KEY).state == "active"

    simnet.run(scenario())


def test_re_claiming_a_key_you_already_own_is_idempotent(simnet):
    """A legitimate retry by the owner must never be suppressed by its own lease."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)

        assert a.claim(KEY) is True
        first = a.node._own_claim(KEY)
        assert a.claim(KEY) is True
        assert a.node._own_claim(KEY).seq > first.seq, "a re-claim re-asserts"
        assert a.node._own_claim(KEY).active

    simnet.run(scenario())


def test_a_higher_id_owner_is_outranked_rather_than_obeyed(simnet):
    """The gate stands down only for a *better* owner. Facing a higher id, the
    node claims anyway — and the other one yields when it hears."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)

        assert b.claim(KEY) is True
        await simnet.until(lambda: a.claim_owner(KEY) == b.id, 4.0,
                           "A never learned B's claim")
        assert a.claim(KEY) is True, "a lower id must not stand down"
        await simnet.until(lambda: b.claim_owner(KEY) == a.id, 4.0,
                           "B never yielded to the better claimant")

    simnet.run(scenario())


# MARK: - who is allowed to suppress work


def test_a_foreign_claim_is_stored_and_relayed_but_owns_nothing(simnet):
    """The anti-starvation rule: a stranger must not be able to claim your work
    keys and then never run them. It is not a matter of dropping the record —
    the record is kept and passed on, it simply never wins."""
    async def scenario():
        a = await simnet.node("a", trust="foreign")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)
        assert a.trust_of(b) == "foreign"

        assert b.claim(KEY) is True
        await simnet.until(lambda: b.id in a.node._claims.get(KEY, {}), 4.0,
                           "the foreign claim was not even stored")
        assert a.claim_owner(KEY) is None
        assert a.claim(KEY) is True, "a foreign claim must never deny us work"

    simnet.run(scenario())


def test_a_claim_minted_under_a_peers_id_by_someone_else_is_dropped(simnet):
    """Trusting the *name* on a claim is not enough. Both shapes of the attack —
    a key the attacker controls, and no key at all — must fail against the pin."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        c = await simnet.node("c")
        await simnet.linked(a, b, c)
        await simnet.all_verified(a, b, c)
        stranger = crypto.DeviceKey(Ed25519PrivateKey.generate())

        c.inject_to(a, {"t": "work-claim", "claim": _claim(b.id, seq=9)})
        c.inject_to(a, {"t": "work-claim",
                        "claim": _claim(b.id, key=stranger, seq=9)})
        await simnet.quiet(0.2)

        assert b.id not in a.node._claims.get(KEY, {})
        assert a.claim_owner(KEY) is None
        assert a.claim(KEY) is True

    simnet.run(scenario())


def test_a_tampered_claim_is_dropped(simnet):
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)

        forged = _claim(b.id, key=b.node.key, seq=5, epoch=b.node.epoch)
        forged["workKey"] = "review:github.com/acme/app#999@rewritten"
        b.inject_to(a, {"t": "work-claim", "claim": forged})
        await simnet.quiet(0.2)

        assert forged["workKey"] not in a.node._claims

    simnet.run(scenario())


# MARK: - the liveness lease


def test_an_owner_that_dies_frees_the_work_for_a_survivor(simnet):
    """The payoff over plain dispatch: originated work fails over. No lease
    timer, no renewal traffic — node liveness *is* the lease."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)

        assert a.claim(KEY) is True
        await simnet.until(lambda: b.claim_owner(KEY) == a.id, 4.0,
                           "B never saw A's claim")
        assert b.claim(KEY) is False, "a live better owner is not raced"

        simnet.isolate(a)  # A's machine falls off the LAN mid-flight
        await simnet.until(lambda: b.link_state(a) == "down", 4.0,
                           "the dead claimant was never marked down")
        assert b.claim_owner(KEY) is None
        assert b.claim(KEY) is True

    simnet.run(scenario())


def test_a_voluntary_release_frees_the_key_without_waiting_for_a_death(simnet):
    """The lease is scoped to liveness, but a node that finishes (or abandons)
    the work should not make the mesh wait for it to die to find out."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)

        assert a.claim(KEY) is True
        await simnet.until(lambda: b.claim_owner(KEY) == a.id, 4.0,
                           "B never saw A's claim")
        assert b.claim(KEY) is False

        a.release(KEY)
        await simnet.until(lambda: b.claim_owner(KEY) is None, 4.0,
                           "the withdrawal never reached the other machine")
        assert b.claim(KEY) is True
        await simnet.until(lambda: a.claim_owner(KEY) == b.id, 4.0,
                           "the machine that let go never accepted the new owner")

    simnet.run(scenario())


def test_two_halves_of_a_split_reconverge_on_one_owner(simnet):
    """Both sides of a partition originate the same work — each sees no other
    claimant, which is the correct call with the information it has. When the
    partition heals the rule decides without a negotiation, and the loser is told
    to abort even though it is the one that was doing the work."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        lost: list[str] = []
        b.node.on_claim_lost = lost.append

        simnet.isolate(b)
        await simnet.until(lambda: not a.linked_to(b), 4.0, "the split never took")
        assert a.claim(KEY) is True and b.claim(KEY) is True
        assert a.claim_owner(KEY) == a.id and b.claim_owner(KEY) == b.id

        simnet.rejoin(b)
        await simnet.linked(a, b, timeout=8.0)
        await simnet.until(
            lambda: a.claim_owner(KEY) == a.id and b.claim_owner(KEY) == a.id, 6.0,
            "the reunited mesh never settled on a single owner")
        assert lost == [KEY]
        assert b.node._own_claim(KEY).state == "released"

    simnet.run(scenario())


def test_a_claim_the_reunion_lost_is_corrected_when_the_newcomer_announces(simnet):
    """What a joining machine is told about existing leases is messages on a
    network that loses them. A node that hears nothing originates — correctly, on
    what it knows — and that announcement is itself the signal the real owner needs
    to answer, so the loss costs a round rather than the deduplication."""
    async def scenario():
        a = await simnet.node("a")
        assert a.claim(KEY) is True

        deaf = simnet.drop_kind("work-claim")  # nothing about leases survives the join
        b = await simnet.node("b")
        await simnet.linked(a, b)
        lost: list[str] = []
        b.node.on_claim_lost = lost.append
        await simnet.until(lambda: b.verified(a), 4.0, "B never proved A's key")
        assert b.claim_owner(KEY) is None, "the join was supposed to tell B nothing"

        deaf.remove()  # the loss was momentary; the mesh is whole again
        assert b.claim(KEY) is True  # B knows of no owner, so it takes the work
        await simnet.until(lambda: b.claim_owner(KEY) == a.id, 4.0,
                           "the owner never answered the newcomer's claim")
        assert lost == [KEY]
        assert b.node._own_claim(KEY).state == "released"
        assert a.claim_owner(KEY) == a.id

    simnet.run(scenario())


def test_a_stranger_claiming_our_key_does_not_make_us_speak(simnet):
    """We answer a competing claim by re-stating our lease, but only one that
    could actually take the key. A foreign claimant owns nothing here whatever it
    says, so answering it would hand an on-mesh flooder a broadcast amplifier and
    change nothing about who holds the work."""
    async def scenario():
        a = await simnet.node("a", trust="foreign")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)
        assert a.trust_of(b) == "foreign"
        assert a.claim(KEY) is True
        spoken = a.node._claim_seq[KEY]

        assert b.claim(KEY) is True  # the stranger claims it anyway
        await simnet.until(lambda: b.id in a.node._claims.get(KEY, {}), 4.0,
                           "the foreign claim was not even stored")
        await simnet.quiet()
        assert a.node._claim_seq[KEY] == spoken, \
            "a stranger's claim provoked a re-announcement"
        assert a.claim_owner(KEY) == a.id

    simnet.run(scenario())


def test_a_machine_that_comes_back_is_told_what_it_missed(simnet):
    """A restart empties the claim book, so a returning machine knows of no lease
    at all and would take work already under way. The link it returns on is what
    tells it — and that link is new even to a peer that never noticed it leave,
    which is the ordinary case: the departure was silent."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b", TIMEOUT_SECS="30")  # B never reaps A
        await simnet.linked(a, b)
        assert b.claim(KEY) is True
        await simnet.until(lambda: a.claim_owner(KEY) == b.id, 4.0,
                           "A never saw B's claim")

        b.link_to(a).freeze()  # B is blind to the departure and keeps its link
        held_since = b.peer(a).linked_since
        await a.restart()
        await simnet.linked(a, b, timeout=8.0)
        assert b.peer(a).linked_since == held_since, \
            "the scenario needs B's uptime clock to have run through the restart"

        await simnet.until(lambda: a.claim_owner(KEY) == b.id, 6.0,
                           "the returning machine was never told about the lease")

    simnet.run(scenario())


def test_a_momentary_stall_does_not_free_a_lease(simnet):
    """`stale` is not `down`: a Wi-Fi blip must not bounce work between machines.
    The distinction assignment already draws is the one the lease reuses."""
    async def scenario():
        a = await simnet.node("a", STALE_SECS="0.1", TIMEOUT_SECS="30")
        b = await simnet.node("b", STALE_SECS="0.1", TIMEOUT_SECS="30")
        await simnet.linked(a, b)
        assert a.claim(KEY) is True
        await simnet.until(lambda: b.claim_owner(KEY) == a.id, 4.0,
                           "B never saw A's claim")

        b.link_to(a).freeze()
        await simnet.until(lambda: b.link_state(a) == "stale", 4.0,
                           "the link never went stale")
        assert b.claim_owner(KEY) == a.id
        assert b.claim(KEY) is False

    simnet.run(scenario())


def test_a_lease_from_a_prior_incarnation_lapses_on_restart(simnet):
    """A device key survives a restart, so the binding alone would keep a stale
    lease authoritative — and a quick reconnect beats the reap. The incarnation
    stamp is what makes the restart itself free the work."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        assert b.claim(KEY) is True
        await simnet.until(lambda: a.claim_owner(KEY) == b.id, 4.0,
                           "A never saw B's claim")

        await b.restart()
        await simnet.linked(a, b)
        assert b.id in a.node._claims.get(KEY, {}), \
            "the scenario needs the stale record still on file"
        assert a.claim_owner(KEY) is None
        assert a.claim(KEY) is True

    simnet.run(scenario())


def test_a_reaped_claimant_leaves_no_records_behind(simnet, monkeypatch):
    """Retention keeps a dead peer visible for a while; once it is gone for good
    its leases are memory nobody can ever use."""
    monkeypatch.setattr(nodemod, "_DOWN_RETENTION_SECS", 0.2)

    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        assert b.claim(KEY) is True
        await simnet.until(lambda: a.claim_owner(KEY) == b.id, 4.0,
                           "A never saw B's claim")

        simnet.isolate(b)
        await simnet.until(lambda: b.id not in a.node.peers, 5.0,
                           "the dead peer was never reaped")
        assert KEY not in a.node._claims

    simnet.run(scenario())


def test_a_claim_relayed_from_beyond_our_horizon_owns_nothing_here(simnet):
    """Liveness is scoped to the observer's own live set (12 — `if node not in
    live: return false`), so a claimant this node cannot see holds no lease *for
    this node*, however faithfully the record reached it. Both halves are the
    spec: the record is stored and relayed, and it does not suppress."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        c = await simnet.node("c")
        simnet.cut(a, c)
        await simnet.linked(a, b)
        await simnet.linked(b, c)

        assert c.claim(KEY) is True
        await simnet.until(lambda: c.id in a.node._claims.get(KEY, {}), 4.0,
                           "the claim never crossed the second hop")
        assert not a.linked_to(c)
        assert a.claim_owner(KEY) is None
        assert a.claim(KEY) is True

    simnet.run(scenario())


# MARK: - the cap, and what it must never starve


def test_a_flood_of_spoofed_keys_cannot_starve_a_genuine_claim(simnet, monkeypatch):
    """A bounded book is a denial-of-service surface: refuse every new record at
    the cap and a foreign peer inside the join fence breaks origination dedup for
    the whole mesh by filling it with keys nobody will ever run."""
    monkeypatch.setattr(nodemod, "_MAX_CLAIMS", 4)

    async def scenario():
        a = await simnet.node("a", trust="foreign")
        b = await simnet.node("b")
        c = await simnet.node("c")
        await simnet.linked(a, b, c)
        await simnet.all_verified(a, b, c)
        a.trusts(b)  # B is the operator's own machine; C stays a stranger
        assert a.trust_of(b) == "personal" and a.trust_of(c) == "foreign"

        for i in range(30):
            c.inject_to(a, {"t": "work-claim", "claim": _claim(
                c.id, f"spoofed-key-{i}", key=c.node.key, epoch=c.node.epoch)})
        await simnet.quiet(0.3)
        assert sum(len(book) for book in a.node._claims.values()) <= 4 + 1

        assert b.claim(KEY) is True
        await simnet.until(lambda: a.claim_owner(KEY) == b.id, 4.0,
                           "a genuine personal claim was starved out by the flood")

    simnet.run(scenario())


# MARK: - the executor's claim, and the dispatch gate over it


async def _dispatch_owned(simnet, a, b):
    """Route a deduped request that lands on B, and wait for its claim to reach A."""
    results = await a.dispatch("review", "review the PR", work_key=KEY)
    assert results[0]["status"] == "spawned" and results[0]["node"] == b.id, results
    await simnet.until(lambda: a.claim_owner(KEY) == b.id, 4.0,
                       "the executor's claim never reached the dispatcher")
    return results


def test_the_executor_claims_the_key_and_frees_it_when_its_agent_ends(simnet):
    """The claim tracks the *agent*, not the request: held from the spawn, freed
    by the completion sentinel — so a crashed review becomes re-runnable rather
    than permanently suppressed."""
    async def scenario():
        a = await simnet.node("a", quota=("ok", 1.0, None, None, 0.5))
        b = await simnet.node("b", quota=("ok", 1.0, None, None, 5.0))
        await simnet.linked(a, b)
        await simnet.until(lambda: a.node.peers[b.id].info.surplus() == 5.0, 3.0,
                           "B's surplus never arrived")

        await _dispatch_owned(simnet, a, b)
        assert KEY in b.node._agents

        suppressed = await a.dispatch("review", "review it again", work_key=KEY)
        assert suppressed == [{"slot": "claim", "node": b.id, "nodeName": "b",
                               "status": "suppressed",
                               "reason": "work already claimed by b"}]
        assert len(b.jobs) == 1

        done_path = b.jobs[-1][1]
        assert done_path, "the executor must hand its runner a completion sentinel"
        open(done_path, "w").close()  # the agent exits
        await simnet.until(lambda: KEY not in b.node._agents, 4.0,
                           "the executor never noticed its agent finish")
        await simnet.until(lambda: a.claim_owner(KEY) is None, 4.0,
                           "the lease was never released to the mesh")

        again = await a.dispatch("review", "the retry", work_key=KEY)
        assert again[0]["status"] == "spawned"
        assert len(b.jobs) == 2

    simnet.run(scenario())


def test_an_executor_never_spawns_a_second_agent_for_one_key(simnet):
    """The dispatcher's gate can be bypassed (an explicit target skips it), so
    the executor holds its own idempotency check — and it has to be atomic with
    the claim, or two back-to-back requests both pass it."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)

        first = await a.dispatch("review", "one", target=b.id, work_key=KEY)
        second = await a.dispatch("review", "two", target=b.id, work_key=KEY)
        assert first[0]["status"] == "spawned" and second[0]["status"] == "spawned"
        assert [p for p, _ in b.jobs] == ["one"], \
            "the second request must not start a second agent"

    simnet.run(scenario())


def test_the_machine_gets_the_last_word_on_work_it_can_see_running(simnet):
    """Work can be live on a machine and absent from its claim book — started
    locally, or surviving a node restart. A peer routing that same work cannot
    see the machine, so the executor asks its host before spawning."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        b.running_work.add(KEY)

        results = await a.dispatch("review", "already under way", target=b.id,
                                   work_key=KEY)
        assert results[0]["status"] == "spawned", \
            "the request is handled here, so it is not a failure to fail over"
        assert b.jobs == [], "a duplicate agent was launched onto live work"

    simnet.run(scenario())
