"""Reading a node's snapshot.

Two halves, and the second is the one that matters. Parsing a well-formed snapshot
is table stakes; the reason these types exist is that a snapshot is not always
well-formed. It can be truncated mid-write, written by a newer protocol version,
or hand-edited by someone hostile - and the library's own contract is that such a
file degrades to "no node" rather than crashing whoever renders it. These types
inherit that contract, so every field has a test for what it does with junk.
"""

from __future__ import annotations

import math

import pytest

from szpont import models
from szpont.models import (NEUTRAL_SURPLUS, Assignment, Claim, Device, Dispatch,
                           Node, Peer, Quota, Slot, Snapshot, Tor)


# ---- a well-formed snapshot ----------------------------------------------


def test_the_snapshot_reads_as_the_node_published_it(snapshot_dict):
    snap = Snapshot.from_dict(snapshot_dict)

    assert snap.node.name == "mbp"
    assert snap.node.tier == 2
    assert snap.tcp_port == 40878
    assert snap.pid == 4242
    assert snap.version == 1
    assert snap.linking == 1
    assert snap.default_trust == "foreign"
    assert [p.name for p in snap.peers] == ["tower", "stranger"]


def test_wire_names_become_python_ones(snapshot_dict):
    """``lastSeenSecsAgo`` is the wire's spelling and nothing a caller should type."""
    peer = Snapshot.from_dict(snapshot_dict).peer("tower")

    assert peer.last_seen_secs == 1.4
    assert peer.uptime_secs == 903.0
    assert peer.tokens_pct == 0.2
    assert Snapshot.from_dict(snapshot_dict).node.tokens_session_pct == 0.8


def test_a_peers_own_claims_and_this_nodes_view_of_them_stay_apart(snapshot_dict):
    """Advertising a key is a claim; ``verified`` is this node's finding, and
    ``trust`` is what the local allowlist makes of it. Collapsing the three is how
    an unproved advertisement gets treated as an identity."""
    tower = Snapshot.from_dict(snapshot_dict).peer("tower")
    stranger = Snapshot.from_dict(snapshot_dict).peer("stranger")

    assert (tower.verified, tower.trust, tower.personal) == (True, "personal", True)
    assert (stranger.verified, stranger.trust, stranger.personal) == (False, "foreign", False)
    assert stranger.keyless and not tower.keyless


def test_link_and_transport_are_read_per_peer(snapshot_dict):
    snap = Snapshot.from_dict(snapshot_dict)

    assert snap.peer("tower").up is True
    assert snap.peer("stranger").up is False
    assert [p.name for p in snap.up] == ["tower"]
    assert snap.peer("stranger").over_tor is True
    assert snap.peer("tower").over_tor is False


def test_out_of_tokens_is_the_state_that_excludes_a_node(snapshot_dict):
    snap = Snapshot.from_dict(snapshot_dict)

    assert snap.peer("stranger").out_of_tokens is True
    # "low" still competes, it just ranks behind a full peer - so it is not "out".
    assert snap.peer("tower").out_of_tokens is False


def test_a_peer_is_found_by_id_short_id_or_name(snapshot_dict):
    snap = Snapshot.from_dict(snapshot_dict)

    assert snap.peer("bbbbbbbb2222").name == "tower"
    assert snap.peer("bbbbbbbb").name == "tower"
    assert snap.peer("tower").id == "bbbbbbbb2222"
    assert snap.peer("nobody") is None


def test_ids_resolve_to_the_names_the_nodes_own_status_prints(snapshot_dict):
    snap = Snapshot.from_dict(snapshot_dict)

    assert snap.node_named("bbbbbbbb2222") == "tower"
    assert snap.node_named("aaaaaaaa1111") == "mbp"
    # An id in an assignment for a node that has since gone: the short id, which
    # is what the CLI falls back to rather than printing nothing.
    assert snap.node_named("eeeeeeee5555") == "eeeeeeee"


def test_assignments_carry_their_shortfall(snapshot_dict):
    snap = Snapshot.from_dict(snapshot_dict)

    review = snap.assignment("review")
    assert review.assigned == ("bbbbbbbb2222",)
    assert review.satisfied is True
    assert review.unowned is False

    audit = snap.assignment("audit")
    assert audit.satisfied is False
    assert audit.shortfall[0].platform == "linux"
    assert audit.shortfall[0].missing == 1


def test_the_trusted_and_banned_lists_are_read(snapshot_dict):
    snap = Snapshot.from_dict(snapshot_dict)

    assert snap.trusted[0].label == "tower"
    assert snap.trusted[0].fingerprint == "b" * 64
    # A keyless device is banned by node id, since it has no fingerprint to name.
    assert snap.banned[0].node == "dddddddd4444"
    assert snap.banned[0].reason == "accepted work and went silent"


def test_tor_state_is_read(snapshot_dict):
    tor = Snapshot.from_dict(snapshot_dict).tor
    assert (tor.enabled, tor.ready, tor.onion) == (True, True, "abc.onion")


def test_by_id_covers_this_machine_as_well_as_its_peers(snapshot_dict):
    by_id = Snapshot.from_dict(snapshot_dict).by_id

    assert set(by_id) == {"aaaaaaaa1111", "bbbbbbbb2222", "cccccccc3333"}
    assert by_id["aaaaaaaa1111"].name == "mbp"


def test_short_id_is_the_eight_characters_every_log_line_prints(snapshot_dict):
    assert Snapshot.from_dict(snapshot_dict).node.short_id == "aaaaaaaa"


def test_nothing_is_dropped_on_the_way_through(snapshot_dict):
    """The protocol grows by adding fields. A wrapper that exposed only what it
    knew about would hide the next one, so the source dict is kept."""
    snapshot_dict["peers"][0]["somethingNewer"] = 7
    snap = Snapshot.from_dict(snapshot_dict)

    assert snap.peer("tower").raw["somethingNewer"] == 7
    assert snap.raw["overrides"] == {"rev": 0, "duties": {}}


# ---- accounting -----------------------------------------------------------


def test_quota_reads_the_ratio_the_mesh_actually_ranks_on(snapshot_dict):
    quota = Snapshot.from_dict(snapshot_dict).node.quota

    assert quota.plan == "max-20x"
    assert quota.surplus == 2.5
    assert quota.quota_left == 12.0
    assert quota.usage_avg == 3.0
    assert quota.advertised is True


def test_a_node_advertising_no_accounting_is_neutral_not_last():
    """No stats means "on the pace line", which is what makes surplus-first
    degrade to weakest-first instead of ranking a quiet node dead last."""
    quota = Node.from_dict({"id": "x"}).quota

    assert quota.surplus == NEUTRAL_SURPLUS == 1.0
    assert quota.advertised is False
    assert quota.quota_left is None


def test_advertised_tells_a_missing_stats_block_from_an_on_pace_one():
    assert Quota.from_dict({"surplus": 1.0}).advertised is True
    assert Quota.from_dict({}).advertised is False
    assert Quota.from_dict(None).advertised is False


# ---- malformed, stale and hostile snapshots -------------------------------


def test_an_empty_snapshot_is_an_empty_mesh_not_an_exception():
    snap = Snapshot.from_dict({})

    assert snap.peers == ()
    assert snap.assignments == {}
    assert snap.node.id == ""
    assert snap.tcp_port == 0
    assert snap.default_trust == "foreign"


def test_from_dict_accepts_none_everywhere():
    """Every parser is reachable from a snapshot field that can be absent, so
    none of them may make its caller check first."""
    assert Snapshot.from_dict(None).peers == ()
    assert Node.from_dict(None).id == ""
    assert Peer.from_dict(None).link == "down"
    assert Slot.from_dict(None).status == "failed"
    assert Claim.from_dict(None).owned is False
    assert Tor.from_dict(None).enabled is False
    assert Device.from_dict(None).fingerprint == ""
    assert Assignment.from_dict("review", None).duty == "review"
    assert Dispatch.from_results(None).slots == ()


@pytest.mark.parametrize("junk", [None, 42, 1.5, [], {"deep": 1}, True])
def test_a_text_field_that_is_not_text_reads_as_empty(junk):
    node = Node.from_dict({"id": junk, "name": junk, "platform": junk,
                           "tokens": junk, "fingerprint": junk, "pubkey": junk,
                           "onion": junk})

    assert node.id == ""
    assert node.name == ""
    assert node.platform == ""
    assert node.fingerprint == ""
    assert node.onion == ""
    assert node.tokens == "ok"      # the schema default, not the empty string


@pytest.mark.parametrize("junk", [None, "nope", 2.5, [], {"deep": 1}])
def test_a_tier_that_is_not_a_whole_number_is_the_schema_default(junk):
    assert Node.from_dict({"tier": junk}).tier == 3


@pytest.mark.parametrize("junk", [None, "nope", [], {"deep": 1}])
def test_a_fraction_that_is_not_a_number_reads_as_full(junk):
    node = Node.from_dict({"tokensPct": junk, "tokensSessionPct": junk})

    assert node.tokens_pct == 1.0
    # The per-window figures are absent-or-real, never invented: a node on the
    # heuristic fallback omits them, and reading junk as 1.0 would advertise a
    # fresh quota it never measured.
    assert node.tokens_session_pct is None


@pytest.mark.parametrize("junk", [None, "nope", 42, [], True])
def test_a_mapping_field_that_is_not_a_mapping_reads_as_empty(junk):
    node = Node.from_dict({"dutiesEnabled": junk, "stats": junk})

    assert node.duties_enabled == {}
    assert node.quota.surplus == NEUTRAL_SURPLUS
    assert node.quota.advertised is False


def test_a_boolean_is_not_a_number():
    """``True`` is an ``int`` in Python and never a meaningful tier or port. Read
    as one, a hand-edited ``"tier": true`` would silently mean tier 1 - the
    strongest machine in the mesh."""
    node = Node.from_dict({"tier": True, "tokensPct": True})

    assert node.tier == 3
    assert node.tokens_pct == 1.0
    assert Snapshot.from_dict({"tcpPort": True}).tcp_port == 0


def test_a_non_object_among_the_peers_costs_that_entry_and_not_the_mesh():
    snap = Snapshot.from_dict({
        "peers": ["nonsense", {"id": "bbbb", "name": "real"}, None, 7],
        "trusted": [{"fingerprint": "a"}, "junk"],
        "assignments": {"review": {"assigned": ["bbbb"]}, "audit": "junk"},
    })

    assert [p.name for p in snap.peers] == ["real"]
    assert len(snap.trusted) == 1
    assert set(snap.assignments) == {"review"}


def test_an_assigned_list_of_junk_keeps_only_the_ids():
    assignment = Assignment.from_dict("review", {"assigned": ["good", 7, None, {}]})
    assert assignment.assigned == ("good",)


def test_a_non_finite_number_survives_as_itself():
    """A signed peer can slip a bare NaN into an advert. It is a float and is kept
    as one - the point is that reading it does not raise, so a caller comparing it
    gets a false rather than a traceback."""
    assert math.isnan(Quota.from_dict({"surplus": float("nan")}).surplus)


def test_an_assignment_with_no_owner_is_flagged():
    """Work routed to a duty nobody holds fails, so "unowned" is a state a caller
    has to be able to see rather than infer from an empty tuple."""
    assignment = Assignment.from_dict("review", {"assigned": []})
    assert assignment.unowned is True
    assert assignment.satisfied is True   # nothing was asked for, so nothing is missing


# ---- dispatch outcomes ----------------------------------------------------


def test_every_slot_of_a_dispatch_is_reported():
    result = Dispatch.from_results([
        {"slot": "linux", "node": "b", "nodeName": "tower", "status": "spawned",
         "reason": ""},
        {"slot": "macos", "node": None, "nodeName": None, "status": "failed",
         "reason": "no eligible node"},
    ])

    assert len(result) == 2
    assert [s.slot for s in result] == ["linux", "macos"]
    assert result[0].spawned is True
    assert result[1].reason == "no eligible node"
    assert result.ok is False
    assert [s.node_name for s in result.spawned] == ["tower"]


def test_suppressed_is_a_success_because_it_is_what_a_work_key_asks_for():
    """A work key means "only if nobody else is on it". A peer already owning the
    work is the gate working, and reporting it as a failure would have callers
    retry against the very deduplication they opted into."""
    result = Dispatch.from_results([
        {"slot": "claim", "node": "b", "nodeName": "tower", "status": "suppressed",
         "reason": "work already claimed by tower"},
    ])

    assert result.ok is True
    assert result.suppressed is True
    assert result[0].ok is True
    assert result[0].spawned is False
    assert result.spawned == ()


def test_a_dispatch_that_produced_no_slots_is_not_a_success():
    """A duty whose placement staffed nothing routed the request nowhere. All-of
    an empty sequence is vacuously true, and calling that ok is how work goes
    missing without anyone being told."""
    assert Dispatch.from_results([]).ok is False
    assert Dispatch.from_results([]).suppressed is False


def test_a_declined_slot_carries_the_nodes_reason():
    slot = Slot.from_dict({"slot": "target", "node": "c", "nodeName": "stranger",
                           "status": "declined", "reason": "target is banned here"})

    assert slot.ok is False
    assert slot.reason == "target is banned here"


# ---- the claim gate -------------------------------------------------------


def test_a_lost_claim_is_an_answer_not_a_failure():
    lost = Claim.from_dict({"owned": False, "owner": "bbbb", "ownerName": "tower"})

    assert bool(lost) is False
    assert lost.owner_name == "tower"

    won = Claim.from_dict({"owned": True, "owner": None, "ownerName": None})
    assert bool(won) is True
    assert won.owner is None


# ---- ergonomics -----------------------------------------------------------


def test_the_models_are_frozen(snapshot_dict):
    """They are a reading of a snapshot taken at one instant. Letting a caller
    edit one would make it look like a way to change the node."""
    peer = Snapshot.from_dict(snapshot_dict).peer("tower")
    with pytest.raises(Exception):
        peer.name = "something else"


def test_the_neutral_surplus_is_the_librarys_own():
    """Hard-coding 1.0 here would be a second definition of a protocol constant.
    It is pinned to the node's, so a retune moves both."""
    from szpontnet import protocol

    assert models.NEUTRAL_SURPLUS == protocol.NEUTRAL_SURPLUS
