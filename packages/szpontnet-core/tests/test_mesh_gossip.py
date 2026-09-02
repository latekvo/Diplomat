"""Gossip: convergence across a topology that isn't a full mesh, and the
authentication that keeps a relay from rewriting what it forwards.

Two properties are load-bearing here and pull against each other. An update has
to travel further than the link it arrived on, or a three-machine mesh where two
of them can't see each other never agrees on anything. And a relay must be unable
to change a single field of what it passes on, or the cheapest attack on this
protocol is to be in the middle of it.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from szpontnet import config, crypto, protocol
from szpontnet.protocol import NodeInfo


def _stranger_advert(node_id: str, **kw) -> dict:
    """A well-formed, correctly self-signed advert from a key nobody has met."""
    key = crypto.DeviceKey(Ed25519PrivateKey.generate())
    info = NodeInfo(id=node_id, name=kw.pop("name", "stranger"), platform="linux",
                    tier=3, tokens="ok", pubkey=key.public_b64, **kw)
    return replace(
        info, sig=key.sign(protocol.advert_signing_bytes(info.to_dict()))).to_dict()


def _signed_override(sim, raw: dict) -> dict:
    """An overrides payload signed by ``sim``'s device key."""
    return {**raw, "sig": sim.node.key.sign(protocol.overrides_signing_bytes(raw))}


# MARK: - convergence across a partial topology


def test_an_advert_reaches_a_node_it_has_no_link_to(simnet):
    """A—B—C with no A–C link: without the relay, C's view of A is frozen at
    whatever it happened to hear first, which is to say never."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        c = await simnet.node("c")
        simnet.cut(a, c)
        await simnet.linked(a, b)
        await simnet.linked(b, c)

        a.node.apply_local_attrs({"name": "renamed-over-two-hops"})
        await simnet.until(
            lambda: (a.id in c.node.peers
                     and c.node.peers[a.id].info.name == "renamed-over-two-hops"),
            5.0, "the advert never crossed the second hop")
        assert not c.linked_to(a), "the scenario needs A and C to have no link"

    simnet.run(scenario())


def test_a_relayed_advert_keeps_the_signature_it_was_minted_with(simnet):
    """Relaying is verbatim for a reason: re-serialising from the parsed model
    drops any field the relay's build doesn't know about, and the originator
    signed over those too — so the advert would arrive unverifiable one hop on."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        c = await simnet.node("c")
        simnet.cut(a, c)
        await simnet.linked(a, b)
        await simnet.linked(b, c)
        # Gossip carries CHANGES (03-transport#gossip-fan-out), so give it one.
        a.node.apply_local_attrs({"name": "two-hops-away"})
        await simnet.until(lambda: a.id in c.node.peers, 5.0,
                           "the relay never reached C")

        relayed = [f for f in simnet.frames("node", src=b, dst=c)
                   if f.payload().get("node", {}).get("id") == a.id]
        assert relayed, "B never relayed A's advert to C"
        raw = relayed[-1].payload()["node"]
        with c.active():
            assert c.node._advert_authentic(raw)
            assert not c.node._advert_authentic({**raw, "name": "tampered"})

    simnet.run(scenario())


def test_a_node_advertises_the_peers_it_can_see(simnet):
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)

        await simnet.until(lambda: b.id in a.node.info.sees, 3.0,
                           "the link never showed up in `sees`")
        await simnet.until(lambda: a.id in b.node.info.sees, 3.0,
                           "the link never showed up in `sees`")

    simnet.run(scenario())


# MARK: - freshness


def test_a_stale_advert_never_overwrites_a_newer_one(simnet):
    """(epoch, seq) is the only ordering gossip has; a mesh that lets an old copy
    win oscillates forever between two views of the same node."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        await simnet.until(lambda: b.id in a.node.peers, 3.0, "never learned B")

        base = b.node.info
        b.inject_to(a, {"t": "node", "node": b.advert(name="fresher",
                                                      seq=base.seq + 50)})
        await simnet.until(lambda: a.node.peers[b.id].info.name == "fresher", 3.0,
                           "a newer seq was not adopted")

        b.inject_to(a, {"t": "node", "node": b.advert(name="stale",
                                                      seq=base.seq + 1)})
        await simnet.quiet(0.2)
        assert a.node.peers[b.id].info.name == "fresher"

        b.inject_to(a, {"t": "node", "node": b.advert(name="reincarnated",
                                                      epoch=base.epoch + 100,
                                                      seq=0)})
        await simnet.until(
            lambda: a.node.peers[b.id].info.name == "reincarnated", 3.0,
            "a new incarnation must supersede regardless of seq")

    simnet.run(scenario())


# MARK: - what a relay must not be able to do


def test_a_relay_cannot_re_key_a_node_it_forwards(simnet):
    """The id→key pin. Once a key is known for an id, a *gossiped* advert
    claiming a different one — even self-signed by that other key — is a third
    party trying to become that node."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        c = await simnet.node("c")
        await simnet.linked(a, b, c)
        await simnet.all_verified(a, b, c)
        pinned = a.node.peers[c.id].info.pubkey
        assert pinned

        b.inject_to(a, {"t": "node", "node": _stranger_advert(
            c.id, name="hijacked", seq=9999, epoch=c.node.epoch + 999)})
        await simnet.quiet(0.2)

        assert a.node.peers[c.id].info.pubkey == pinned
        assert a.node.peers[c.id].info.name != "hijacked"

    simnet.run(scenario())


def test_third_party_gossip_cannot_revoke_a_proven_verification(simnet):
    """A verification is a fact about a signature this node checked itself. If a
    relayed advert could drop it, any peer inside the join fence could force a
    trusted machine to foreign — and the inflated seq would keep it there."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        c = await simnet.node("c")
        await simnet.linked(a, b, c)
        await simnet.all_verified(a, b, c)
        proven = a.node.peers[c.id].verified_fp

        b.inject_to(a, {"t": "node", "node": _stranger_advert(
            c.id, seq=9999, epoch=c.node.epoch + 999)})
        await simnet.quiet(0.2)

        assert a.node.peers[c.id].verified_fp == proven
        assert a.trust_of(c) == "personal"

    simnet.run(scenario())


def test_a_tampered_advert_is_dropped_whole(simnet):
    """Not "the changed field is ignored" — the whole record, because a signature
    covers everything and a receiver cannot tell which field was rewritten."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        await simnet.until(lambda: b.id in a.node.peers, 3.0, "never learned B")

        tampered = b.advert(name="honest", seq=b.node.info.seq + 10)
        tampered["name"] = "tampered-in-flight"
        b.inject_to(a, {"t": "node", "node": tampered})
        await simnet.quiet(0.2)
        assert a.node.peers[b.id].info.name == "b"

        unsigned = b.advert(name="unsigned", seq=b.node.info.seq + 11)
        unsigned.pop("sig")
        b.inject_to(a, {"t": "node", "node": unsigned})
        await simnet.quiet(0.2)
        assert a.node.peers[b.id].info.name == "b"

    simnet.run(scenario())


def test_a_non_finite_advert_is_dropped_and_the_link_survives(simnet):
    """``1e999`` parses to ∞, and an ∞ epoch out-freshes every honest advert
    forever while re-serialising as a bare ``Infinity`` — RFC 8259-invalid, so a
    strict reader rejects the whole snapshot and every panel goes blank."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        await simnet.until(lambda: b.id in a.node.peers, 3.0, "never learned B")

        for poison in ({"epoch": float("inf")}, {"tokensPct": float("inf")},
                       {"stats": {"surplus": float("nan")}},
                       {"dutiesEnabled": {"review": float("inf")}}):
            raw = b.advert(name="poisoned", seq=b.node.info.seq + 5)
            raw.update(poison)
            b.inject_to(a, {"t": "node", "node": raw})
        await simnet.quiet(0.2)

        assert a.node.peers[b.id].info.name == "b"
        assert a.node.peers[b.id].info.epoch == b.node.epoch
        # The link is untouched by any of it, and still carries real gossip.
        assert a.linked_to(b)
        b.inject_to(a, {"t": "node", "node": b.advert(name="still-listening",
                                                      seq=b.node.info.seq + 6)})
        await simnet.until(
            lambda: a.node.peers[b.id].info.name == "still-listening", 3.0,
            "the link stopped processing gossip after the poisoned adverts")

    simnet.run(scenario())


def test_garbage_frames_never_wedge_or_drop_a_link(simnet):
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        link = a.link_to(b)

        for junk in (b"{not json\n", b"[1,2,3]\n", b'{"no":"type"}\n',
                     b'{"t":123}\n', b'{"t":"zzz-from-the-future"}\n',
                     b'{"t":"node","node":"not-an-object"}\n',
                     b'{"t":"hello","node":{"id":null}}\n',
                     b"[" * (protocol.MAX_LINE_BYTES - 1) + b"\n"):
            b.inject_to(a, junk)
        await simnet.quiet(0.2)

        assert a.linked_to(b) and a.link_to(b) is link
        b.inject_to(a, {"t": "node", "node": b.advert(name="after-the-garbage",
                                                      seq=b.node.info.seq + 3)})
        await simnet.until(
            lambda: a.node.peers[b.id].info.name == "after-the-garbage", 3.0,
            "the link survived the garbage but stopped listening")

    simnet.run(scenario())


# MARK: - placement overrides (last writer wins, and only a real writer)


def test_an_override_converges_across_the_mesh(simnet):
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        c = await simnet.node("c")
        simnet.cut(a, c)
        await simnet.linked(a, b)
        await simnet.linked(b, c)

        reply = await b.ctl({"t": "set-overrides", "duty": "review",
                             "placement": {"strategy": "strongest-first",
                                           "tokenAware": True, "spread": []}})
        assert reply == {"t": "ok"}
        for sim in (a, c):
            await simnet.until(
                lambda s=sim: s.node.overrides.rev == 1
                and "review" in s.node.overrides.duties, 5.0,
                f"{sim.name} never adopted the override")

    simnet.run(scenario())


def test_the_preferred_wan_transport_converges_across_the_mesh(simnet):
    """An edge agrees on ONE transport only because both its ends resolved the same
    mesh-wide pick, so the pick has to reach machines the editor never linked to —
    the same reach a placement override gets, on the same record.

    The duty edit that follows is the trap: both settings ride one record and the
    WHOLE record wins the last-writer-wins comparison, so an edit that rebuilt it
    without carrying the pick forward would quietly move every WAN edge back."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        c = await simnet.node("c")
        simnet.cut(a, c)
        await simnet.linked(a, b)
        await simnet.linked(b, c)

        assert await b.ctl({"t": "set-wan", "transport": "tor"}) == {
            "t": "ok", "transport": "tor"}
        for sim in (a, c):
            await simnet.until(lambda s=sim: s.node.overrides.wan == "tor", 5.0,
                               f"{sim.name} never adopted the transport pick")

        await b.ctl({"t": "set-overrides", "duty": "review",
                     "placement": {"strategy": "strongest-first",
                                   "tokenAware": True, "spread": []}})
        for sim in (a, c):
            await simnet.until(
                lambda s=sim: "review" in s.node.overrides.duties, 5.0,
                f"{sim.name} never adopted the duty edit")
            assert sim.node.overrides.wan == "tor", "the duty edit reset the pick"

    simnet.run(scenario())


def test_an_unpickable_transport_is_refused_rather_than_gossiped(simnet):
    """The pick names the transport every node is to rank first, so a name no node
    can honour must never enter the record — it would rank every real transport
    below an absent one, mesh-wide, from one typo."""
    async def scenario():
        a = await simnet.node("a")
        reply = await a.ctl({"t": "set-wan", "transport": "carrier-pigeon"})
        assert reply["t"] == "error"
        assert a.node.overrides.wan == ""
        assert a.node.overrides.rev == 0  # nothing was published

    simnet.run(scenario())


def test_a_pick_this_build_cannot_honour_still_relays_and_still_converges(simnet):
    """A newer mesh picks a transport this build has never heard of.

    Two things have to hold at once, and they pull apart. The record is relayed by
    re-serialising it, so the unknown name has to survive the parse verbatim or the
    editor's signature no longer matches and the hop behind this node loses the whole
    revision — the duty edits riding the same record with it. And this node still has
    to dial *something*: an unhonourable name resolves to its own default order rather
    than ranking every transport it does run below one it doesn't.
    """
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        c = await simnet.node("c")
        simnet.cut(a, c)
        await simnet.linked(a, b)
        await simnet.linked(b, c)
        await simnet.all_verified(a, b)
        # C authenticates the edit against A's pinned key, which only a relayed advert
        # can give it — and gossip carries CHANGES, so give it one.
        a.node.apply_local_attrs({"name": "two-hops-away"})
        await simnet.until(lambda: a.id in c.node.peers, 5.0,
                           "C never learned A's key from the relay")

        b.inject_to(a, {"t": "overrides", "overrides": _signed_override(
            a, {"rev": 1, "updatedBy": a.id, "duties": {}, "wan": "quic2"})})
        await simnet.until(lambda: c.node.overrides.rev == 1, 5.0,
                           "the pick never survived the relay to C")

        for sim in (b, c):
            assert sim.node.overrides.wan == "quic2", "the relay rewrote the record"
            assert config.wan_preferred(sim.node.overrides) == \
                config.wan_transports()[0], "an unrunnable transport was ranked first"

    simnet.run(scenario())


def test_a_forged_or_stale_override_is_refused(simnet):
    """Placement is mesh-wide: an override decides which machines run what. On an
    open mesh the signature is the only thing between that and any peer."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        await simnet.linked(a, b)
        await simnet.all_verified(a, b)

        await b.ctl({"t": "set-overrides", "duty": "review",
                     "placement": {"strategy": "strongest-first",
                                   "tokenAware": True, "spread": []}})
        await simnet.until(lambda: a.node.overrides.rev == 1, 5.0,
                           "the genuine override never landed")
        pinned = a.node.overrides

        strongest = {"strategy": "strongest-first", "tokenAware": True, "spread": []}
        forgeries = [
            # A real edit from an editor whose key we don't know: unauthenticatable.
            {"rev": 99, "updatedBy": "z" * 32, "duties": {"conflicts": strongest}},
            # A real edit under a KNOWN editor, with a signature that isn't theirs.
            {**{"rev": 99, "updatedBy": b.id, "duties": {"conflicts": strongest}},
             "sig": a.node.key.sign(protocol.overrides_signing_bytes(
                 {"rev": 99, "updatedBy": b.id, "duties": {"conflicts": strongest}}))},
            # rev 0 is the unsigned DEFAULT override — carrying duties, it is an
            # attempt to skip the signature scheme entirely.
            {"rev": 0, "updatedBy": b.id, "duties": {"conflicts": strongest}},
            # A genuinely signed edit that is simply older than what we hold.
            _signed_override(b, {"rev": 1, "updatedBy": b.id,
                                 "duties": {"conflicts": strongest}}),
        ]
        for raw in forgeries:
            b.inject_to(a, {"t": "overrides", "overrides": raw})
        await simnet.quiet(0.3)

        assert a.node.overrides == pinned
        assert "conflicts" not in a.node.overrides.duties

    simnet.run(scenario())


# MARK: - liveness is not something a third party can vouch for


def test_relayed_gossip_does_not_keep_a_dead_link_alive(simnet):
    """A linked peer's liveness comes from its own link. If third-party gossip
    refreshed it, any peer replaying a genuine advert would pin a dead machine
    `up` for as long as it kept talking — and the reaper that frees its duties
    and its work-claims would never run."""
    async def scenario():
        a = await simnet.node("a")
        b = await simnet.node("b")
        c = await simnet.node("c")
        await simnet.linked(a, b, c)

        a.link_to(b).freeze()  # B dies, silently, only as far as A can tell

        async def keep_vouching():
            # C keeps telling A about B, with an ever-fresher advert, right
            # through the window in which A ought to give up on it. One burst
            # would prove nothing — the timeout would simply outlast it.
            for i in range(500):
                c.inject_to(a, {"t": "node",
                                "node": b.advert(seq=b.node.info.seq + 10 + i)})
                await asyncio.sleep(0.02)

        vouching = asyncio.ensure_future(keep_vouching())
        try:
            await simnet.until(lambda: a.link_state(b) == "down", 4.0,
                               "a frozen link was kept alive by third-party gossip")
        finally:
            vouching.cancel()

    simnet.run(scenario())
