"""Pure-logic mesh tests: assignment, protocol codec, identity, LWW overrides.

Offline, no sockets, no Qt. Run with ``python -m pytest linux/tests`` or
dependency-free via ``python linux/tests/test_mesh_logic.py``.
"""

from __future__ import annotations

import itertools
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dataclasses import replace as _dc_replace  # noqa: E402

from diplomat_app.mesh import (  # noqa: E402
    assign, banned, config, crypto, identity, protocol, spawnjob, stats, trust,
    usage,
)
from diplomat_app import core  # noqa: E402
from diplomat_app.mesh.config import Placement, PlacementOverrides  # noqa: E402
from diplomat_app.mesh.protocol import NodeInfo  # noqa: E402


def _node(id: str, platform: str = "linux", tier: int = 3, tokens: str = "ok",
          duties: dict | None = None) -> NodeInfo:
    return NodeInfo(id=id, name=f"n-{id}", platform=platform, tier=tier,
                    tokens=tokens, duties_enabled=duties or {})


# The user's actual fleet: one Linux box + a strong and a weak MacBook.
LIN = _node("a", "linux", tier=4)
MAC_STRONG = _node("b", "macos", tier=1)
MAC_WEAK = _node("c", "macos", tier=4)
FLEET = [LIN, MAC_STRONG, MAC_WEAK]


# MARK: assignment strategies


def test_weakest_first_prefers_high_tier_number():
    a = assign.assign_duty("review", FLEET)
    # tier 4 beats tier 1; the linux/mac tie at tier 4 breaks on node id.
    assert a.assigned == ("a",)
    assert a.satisfied


def test_strongest_first_override():
    o = PlacementOverrides().with_duty(
        "review", Placement(strategy="strongest-first", token_aware=True), by="a")
    a = assign.assign_duty("review", FLEET, o)
    assert a.assigned == ("b",)


def test_local_first_prefers_the_dispatching_node():
    o = PlacementOverrides().with_duty(
        "review", Placement(strategy="local-first", token_aware=True), by="x")
    assert assign.assign_duty("review", FLEET, o, local_id="b").assigned == ("b",)
    # A non-eligible local node falls back to weakest-first.
    out = _node("b", "macos", tier=1, tokens="out")
    a = assign.assign_duty("review", [LIN, out, MAC_WEAK], o, local_id="b")
    assert a.assigned == ("a",)


def test_audit_spreads_one_linux_one_macos():
    a = assign.assign_duty("audit", FLEET)
    assert a.assigned == ("a", "c")  # the weak mac wins the macos slot
    assert a.satisfied


def test_spread_shortfall_reported_but_partial_coverage_kept():
    a = assign.assign_duty("audit", [MAC_STRONG, MAC_WEAK])
    assert a.assigned == ("c",)
    assert a.shortfall == (("linux", 1),)
    assert not a.satisfied


def test_token_out_nodes_are_skipped_and_low_deprioritized():
    low = _node("a", "linux", tier=4, tokens="low")
    out = _node("c", "macos", tier=4, tokens="out")
    # review: 'a' is weakest but low on tokens → the ok-token weak node wins.
    a = assign.assign_duty("review", [low, MAC_STRONG, out])
    assert a.assigned == ("b",)
    # audit's macos slot can't use an out-of-tokens mac.
    a = assign.assign_duty("audit", [LIN, out])
    assert a.assigned == ("a",)
    assert a.shortfall == (("macos", 1),)


def test_token_awareness_can_be_disabled_per_duty():
    out = _node("c", "macos", tier=4, tokens="out")
    o = PlacementOverrides().with_duty(
        "review", Placement(strategy="weakest-first", token_aware=False), by="a")
    a = assign.assign_duty("review", [MAC_STRONG, out], o)
    # tokens still rank behind ok-token peers, but 'out' is no longer excluded —
    # and the weakest machine wins only among the same token rank.
    assert a.assigned in (("c",), ("b",))
    eligible_ids = {n.id for n in [MAC_STRONG, out]}
    assert set(a.assigned) <= eligible_ids


def test_per_node_duty_disable():
    no_audit = _node("c", "macos", tier=4, duties={"audit": False})
    a = assign.assign_duty("audit", [LIN, MAC_STRONG, no_audit])
    assert a.assigned == ("a", "b")


def test_assignment_is_permutation_invariant():
    # The leaderless design hinges on every node computing the same answer:
    # input order must never matter.
    for perm in itertools.permutations(FLEET):
        for duty in config.duty_ids():
            assert assign.assign_duty(duty, list(perm)).assigned == \
                assign.assign_duty(duty, FLEET).assigned


def test_slot_candidates_provide_per_platform_failover():
    slots = assign.slot_candidates("audit", FLEET)
    assert slots == [("linux", ["a"]), ("macos", ["c", "b"])]
    # No-spread duties get a single slot over every eligible node, ranked:
    # the assignee first, then the failover order behind it.
    slots = assign.slot_candidates("review", FLEET)
    assert slots == [("any", ["a", "c", "b"])]


def test_assign_all_covers_every_duty():
    assert set(assign.assign_all(FLEET).keys()) == set(config.duty_ids())


def test_no_nodes_means_unsatisfied_not_crash():
    a = assign.assign_duty("review", [])
    assert a.assigned == ()
    assert not a.satisfied


# MARK: protocol codec


def test_encode_decode_roundtrip():
    msg = protocol.node_update(LIN)
    out = protocol.decode(protocol.encode(msg))
    assert out["t"] == "node"
    assert NodeInfo.from_dict(out["node"]) == LIN


def test_decode_rejects_garbage():
    assert protocol.decode(b"") is None
    assert protocol.decode(b"not json\n") is None
    assert protocol.decode(b"[1,2,3]\n") is None  # non-object
    assert protocol.decode(b'{"no_type": 1}\n') is None
    assert protocol.decode(b"\xff\xfe\n") is None  # invalid utf-8
    assert protocol.decode(b"x" * (protocol.MAX_LINE_BYTES + 1)) is None


def test_nodeinfo_tolerates_missing_and_junk_fields():
    assert NodeInfo.from_dict({}) is None  # id is the only hard requirement
    n = NodeInfo.from_dict({"id": "x"})
    assert n is not None and n.tier == 3 and n.tokens == "ok"
    assert NodeInfo.from_dict({"id": "x", "tier": "not-a-number"}) is None


def test_nodeinfo_drops_non_finite_floats_from_the_wire():
    """A peer advert carrying a non-finite float (JSON ``1e999`` parses to ∞, or ``NaN``)
    must be DROPPED. ``float(inf)``/``float(nan)`` do NOT raise (unlike the swept
    ``int(inf)`` OverflowError), so an unclamped decode let ∞ into the model, where an ∞
    ``epoch`` out-freshes every honest advert AND ∞/NaN serialize as the bare tokens
    ``Infinity``/``NaN`` — RFC 8259-invalid JSON that a strict reader (the Swift snapshot
    decoder) rejects WHOLESALE, blanking the topology for every node. tokensPct, epoch,
    every stats float, and every dutiesEnabled value (recursively) are guarded — each is
    re-serialized verbatim into the snapshot and the ctl status reply."""
    import json
    for bad in ({"id": "x", "tokensPct": 1e999},
                {"id": "x", "tokensPct": float("nan")},
                {"id": "x", "epoch": 1e999},
                {"id": "x", "stats": {"quotaLeft": 1e999, "usageAvg": 0.0}},
                {"id": "x", "stats": {"surplus": 10 ** 400}},            # bigint: float() OverflowError
                {"id": "x", "stats": {"cfg": {"w": 10 ** 400}}},         # nested bigint
                {"id": "x", "stats": {"plan": {"weight": float("inf")}}},
                {"id": "x", "stats": {"buckets": [1.0, float("nan")]}},  # nested list
                {"id": "x", "dutiesEnabled": {"review": 1e999}},
                {"id": "x", "dutiesEnabled": {"review": float("nan")}},
                {"id": "x", "dutiesEnabled": {"cfg": {"weight": float("inf")}}},  # nested
                {"id": "x", "dutiesEnabled": {"cfg": [1.0, -1e999]}}):  # nested list
        assert NodeInfo.from_dict(bad) is None, bad
    # A well-formed advert still decodes, and its snapshot serializes as strict RFC JSON.
    good = NodeInfo.from_dict({"id": "x", "tokensPct": 0.4, "epoch": 1784.5,
                               "dutiesEnabled": {"review": False, "audit": True},
                               "stats": {"surplus": 8.0, "quotaLeft": 10.0, "usageAvg": 2.0}})
    assert good is not None and good.surplus() == 8.0
    assert good.duties_enabled == {"review": False, "audit": True}  # finite values preserved
    ser = json.dumps({"peers": [dict(good.to_dict(), surplus=round(good.surplus(), 3))]})
    assert "Infinity" not in ser and "NaN" not in ser
    json.loads(ser, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))  # strict


def test_nodeinfo_drops_non_finite_numeric_string_stats():
    """Round-19: a stats value can be a numeric STRING, not just a float. float("1e400")/
    float("inf")/float("nan") return non-finite WITHOUT raising, so a crafted advert like
    {"stats": {"quotaLeft": "1e400"}} slipped past the float-INSTANCE guard and drove
    surplus()'s float() to ±inf/nan -> round(inf,3)=inf -> the bare Infinity/NaN token in the
    snapshot that a strict Swift reader rejects WHOLESALE. Such an advert must be dropped at
    ingestion; a FINITE numeric string is still accepted and a non-numeric string (plan name)
    left untouched. Discriminates the fix: an instance-only guard leaks these."""
    import json
    for bad in ("1e400", "1e999", "inf", "-inf", "nan"):
        assert NodeInfo.from_dict({"id": "x", "stats": {"quotaLeft": bad, "usageAvg": 0.0}}) is None, bad
        assert NodeInfo.from_dict({"id": "x", "stats": {"usageAvg": bad}}) is None, bad
        assert NodeInfo.from_dict({"id": "x", "stats": {"cfg": {"w": bad}}}) is None, bad      # nested dict
        assert NodeInfo.from_dict({"id": "x", "dutiesEnabled": {"cfg": [bad]}}) is None, bad    # nested list
    # A FINITE numeric string is accepted; a non-numeric string (a plan name) is untouched.
    good = NodeInfo.from_dict({"id": "x", "stats": {"plan": "max-5x", "surplus": "8.0", "quotaLeft": "9.0", "usageAvg": "1.0"}})
    assert good is not None and good.surplus() == 8.0
    ser = json.dumps({"surplus": round(good.surplus(), 3)})
    assert "Infinity" not in ser and "NaN" not in ser


def test_nodeinfo_freshness_epoch_beats_seq():
    old = NodeInfo(id="x", name="x", platform="linux", tier=3, tokens="ok",
                   epoch=100.0, seq=50)
    restarted = NodeInfo(id="x", name="x", platform="linux", tier=3, tokens="ok",
                         epoch=200.0, seq=1)
    assert restarted.newer_than(old)
    assert not old.newer_than(restarted)
    assert old.bumped().newer_than(old)


def test_job_roundtrip():
    job = protocol.Job(id="j1", duty="audit", prompt="p" * 10_000,
                       requested_by="a", requested_at=123.0)
    out = protocol.Job.from_dict(protocol.decode(protocol.encode(
        protocol.dispatch(job)))["job"])
    assert out == job


# MARK: LWW placement overrides


def test_overrides_lww_higher_rev_wins():
    base = PlacementOverrides()
    a = base.with_duty("review", Placement("strongest-first", True), by="node-a")
    b = base.with_duty("review", Placement("local-first", True), by="node-b")
    assert a.rev == b.rev == 1
    # Same rev: the id tie-break picks one winner on EVERY node.
    assert a.wins_over(b) != b.wins_over(a)
    c = a.with_duty("audit", Placement("weakest-first", False), by="node-b")
    assert c.rev == 2 and c.wins_over(a) and c.wins_over(b)
    # Roundtrip through the wire dict form.
    again = PlacementOverrides.from_dict(c.to_dict())
    assert again == c


def test_placement_for_falls_back_to_core_defaults():
    p = config.placement_for("audit", None)
    assert p.spread == (("linux", 1), ("macos", 1))
    assert p.token_aware
    o = PlacementOverrides().with_duty("audit", Placement("strongest-first", False), by="x")
    p = config.placement_for("audit", o)
    assert p.strategy == "strongest-first" and not p.token_aware and p.spread == ()


# MARK: identity


def test_identity_minted_and_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    n1 = identity.load()
    assert len(n1.id) == 32
    n2 = identity.load()
    assert n2.id == n1.id  # stable across loads


def test_apply_attrs_clamps_and_ignores_junk(tmp_path, monkeypatch):
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    monkeypatch.setenv("DIPLOMAT_MESH_TIER", "3")  # deterministic auto-detect
    n = identity.load()
    lo, hi, _ = config.tier_bounds()
    assert n.tokens == "auto" and n.strength_auto  # fresh node: auto everything
    n = identity.apply_attrs(n, {"tier": 99, "tokens": "banana", "name": "  "})
    # invalid tokens ignored (stays auto); a tier edit clamps AND pins strength.
    assert n.tier == hi and n.tokens == "auto" and not n.strength_auto
    n = identity.apply_attrs(n, {"tier": -3, "tokens": "out", "name": "box",
                                 "dutiesEnabled": {"audit": False}, "junk": 1})
    assert n.tier == lo and n.tokens == "out" and n.name == "box"
    assert not n.duty_enabled("audit") and n.duty_enabled("review")
    # re-enabling auto re-detects the tier from hardware (the forced value here).
    n = identity.apply_attrs(n, {"strengthAuto": True})
    assert n.strength_auto and n.tier == 3


# MARK: trust - device keys + the local allowlist


def test_trust_allowlist_classifies():
    # Trust keys on a VERIFIED fingerprint against a LOCAL allowlist - never on
    # anything a peer advertises. A listed fingerprint is always personal; an
    # unknown one falls to the default level (ships foreign - zero-trust).
    assert trust.classify("abc", {"abc": "mine"}) == "personal"          # listed → personal
    assert trust.classify("abc", {"abc": "mine"}, "foreign") == "personal"  # listed wins over default
    assert trust.classify("xyz", {"abc": "mine"}) == "foreign"           # unlisted → default (foreign)
    assert trust.classify("", {"abc": "mine"}) == "foreign"              # unverified (no fp) → default
    assert trust.classify("abc", {}) == "foreign"                        # empty allowlist → foreign default
    # The default level is configurable: 'personal' restores the full-trust mesh.
    assert trust.classify("abc", {}, "personal") == "personal"           # full-trust mode
    assert trust.classify("xyz", {"abc": "mine"}, "personal") == "personal"  # unlisted personal-default


def test_trust_allowlist_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    trust.save({"fp1": "mbp", "fp2": ""}, "foreign")
    loaded = trust.load()
    assert loaded == {"fp1": "mbp", "fp2": ""}
    assert trust.load_default_level() == "foreign"          # default persisted alongside the list
    assert trust.classify("fp1", loaded) == "personal"
    assert trust.classify("nope", loaded) == "foreign"
    # Flipping the persisted default to personal is round-tripped.
    trust.save(loaded, "personal")
    assert trust.load_default_level() == "personal"
    assert trust.classify("nope", trust.load(), trust.load_default_level()) == "personal"


def test_promotion_does_not_pin_env_baseline_default_trust(tmp_path, monkeypatch):
    """An allowlist edit must persist only the operator's EXPLICIT default-trust choice
    — never the boot-resolved env/mesh.json baseline. Otherwise promoting one device
    while DIPLOMAT_MESH_DEFAULT_TRUST=personal pins 'personal' into trusted.json, and a
    later foreign lockdown (env→foreign) is silently ignored: the persisted value wins
    at the next boot, so an unlisted, unverified device stays classified personal
    (run-on-host / set-attr / can own work) despite the operator's intent."""
    from diplomat_app.mesh.node import MeshNode
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))

    # Boot #1: env baseline personal, no persisted defaultLevel; operator promotes a device.
    monkeypatch.setenv("DIPLOMAT_MESH_DEFAULT_TRUST", "personal")
    node = MeshNode()
    assert node._default_trust == "personal"                 # resolved from the env baseline
    node.add_trusted("ABCDEF0123456789", "laptop")
    assert trust.load_default_level() == ""                  # baseline must NOT be pinned

    # Boot #2: operator flips env to foreign to lock the mesh down — it MUST take effect.
    monkeypatch.setenv("DIPLOMAT_MESH_DEFAULT_TRUST", "foreign")
    node2 = MeshNode()
    assert node2._default_trust == "foreign"
    assert trust.classify("UNKNOWN_FP", trust.load(), node2._default_trust) == "foreign"


def test_explicit_default_trust_choice_survives_allowlist_edits(tmp_path, monkeypatch):
    """Complement of the above: an EXPLICIT set_default_trust choice IS persisted, wins
    over env at the next boot, and survives later add/remove_trusted edits."""
    from diplomat_app.mesh.node import MeshNode
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("DIPLOMAT_MESH_DEFAULT_TRUST", "foreign")
    node = MeshNode()
    assert node.set_default_trust("personal")                # explicit operator choice
    assert trust.load_default_level() == "personal"
    node.add_trusted("FEDCBA9876543210", "desktop")          # later allowlist edits
    node.remove_trusted("FEDCBA9876543210")
    assert trust.load_default_level() == "personal"          # explicit choice preserved
    node2 = MeshNode()                                       # env=foreign, but choice wins
    assert node2._default_trust == "personal"


def test_ban_list_matches_by_key_and_falls_back_to_id_for_keyless():
    entries = banned.add([], banned.entry("fp-banned", "nodeA", label="flaky",
                                          reason="broke it", job_id="j1"))
    entries = banned.add(entries, banned.entry("", "nodeK", reason="keyless"))
    # A keyed device is judged by its key alone…
    assert banned.is_banned(entries, "fp-banned")
    assert not banned.is_banned(entries, "fp-other")
    # …so a spoofed id can never inherit a keyed device's ban…
    assert not banned.is_banned(entries, "fp-other", "nodeA")
    # …while a keyless device (no fingerprint) matches its id, best-effort.
    assert banned.is_banned(entries, "", "nodeK")
    assert not banned.is_banned(entries, "", "nodeA")  # keyed ban ≠ id ban


def test_ban_list_newest_mark_wins_and_removal():
    entries = banned.add([], banned.entry("fp1", "nodeA", reason="first"))
    entries = banned.add(entries, banned.entry("fp1", "nodeA", reason="second"))
    assert len(entries) == 1 and entries[0]["reason"] == "second"
    entries, removed = banned.remove(entries, fingerprint="fp1")
    assert removed and not entries
    entries, removed = banned.remove(entries, fingerprint="fp1")
    assert not removed


def test_ban_list_persists_and_drops_junk(tmp_path, monkeypatch):
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    banned.save([banned.entry("fp1", "nodeA", label="box", reason="late", job_id="j1"),
                 {"label": "names nobody"},  # no fingerprint, no node → dropped
                 "not even a dict"])
    loaded = banned.load()
    assert len(loaded) == 1
    e = loaded[0]
    assert e["fingerprint"] == "fp1" and e["node"] == "nodeA"
    assert e["reason"] == "late" and e["jobId"] == "j1" and e["bannedAt"] > 0


def test_job_status_direct_flag_is_additive():
    # Omitted when false, so a plain job-status is byte-identical to before…
    assert "direct" not in protocol.job_status("j1", "spawned", "", "n1")
    # …and carried only by a personal-path spawn (no job-result will follow).
    assert protocol.job_status("j1", "spawned", "", "n1", direct=True)["direct"] is True


def test_reminder_and_progress_builders():
    rem = protocol.job_reminder("j1", "origin")
    assert (rem["t"], rem["id"], rem["node"]) == ("job-reminder", "j1", "origin")
    prog = protocol.job_progress("j1", "exec", "70% done")
    assert (prog["t"], prog["id"], prog["node"], prog["note"]) == \
        ("job-progress", "j1", "exec", "70% done")
    # Both survive the wire round-trip like any other message.
    assert protocol.decode(protocol.encode(dict(prog)))["note"] == "70% done"


def test_accountability_constants_ship_in_the_model():
    proto = config.protocol()
    assert proto["foreignCompletionDeadlineSecs"] == 21600.0  # the 6-hour floor
    assert proto["foreignReminderGraceSecs"] == 900.0


def test_ban_of_a_keyed_but_unverified_executor_still_enforces(tmp_path, monkeypatch):
    """The ban must record the same identity enforcement later checks: an executor
    that ADVERTISED a key but never proved it (auth never completed) is banned by
    that advertised fingerprint — banning it by node id alone would let it slip
    its own ban on the very next classification (which checks the advertised key
    first and never falls back to the id for a keyed peer)."""
    if not crypto.AVAILABLE:
        return
    import time as _time
    from diplomat_app.mesh import node as node_mod
    node = _fresh_node(tmp_path, monkeypatch)
    node._trusted = {"someone-else": ""}  # boundary on → an unverified peer is foreign
    k = _mk_key()
    node._learn_node(_peer_info("shady", 1, pubkey=k.public_b64), "1.2.3.4",
                     _FakeWriter(), raw=_signed_advert(k, "shady"))
    peer = node.peers["shady"]
    assert peer.verified_fp is None  # advertised a key, never proved it
    node._awaiting_result["j1"] = node_mod._Awaiting(
        executor_id="shady", duty="review", added=_time.monotonic())
    node._ban_for_broken_promise("j1", "no response to readiness reminder")
    assert node._banned and node._banned[0]["fingerprint"] == k.fingerprint
    assert node._peer_trust(peer) == "banned"


def test_reminder_resends_across_the_grace_window(tmp_path, monkeypatch):
    """Reminder delivery is best-effort: if the link is down at the deadline
    instant, the ask is re-sent once the link heals — with the grace clock
    unmoved — so an executor holding a result tombstone gets to revive it
    instead of eating a false silence ban."""
    if not crypto.AVAILABLE:
        return
    import time as _time
    from diplomat_app.mesh import node as node_mod
    monkeypatch.setenv("DIPLOMAT_MESH_REMINDER_GRACE_SECS", "60")
    node = _fresh_node(tmp_path, monkeypatch)
    node._trusted = {"someone-else": ""}  # boundary on → bob is foreign
    peer, w = _link_peer(node, "bob", _mk_key())
    aw = node_mod._Awaiting(executor_id="bob", duty="review",
                            added=_time.monotonic(),
                            deadline=_time.monotonic() - 1)
    node._awaiting_result["j9"] = aw

    # Deadline crosses while the link is DOWN: the grace clock starts anyway
    # (vanishing is not an excuse), but nothing could be delivered.
    peer.writer = None
    node._check_foreign_deadlines()
    first_ask = aw.reminded_at
    assert first_ask is not None and not w.of("job-reminder")

    # The link heals mid-grace: the next due tick re-asks on the healed link
    # WITHOUT restarting the grace clock.
    peer.writer = w
    aw.next_remind = _time.monotonic() - 1
    node._check_foreign_deadlines()
    assert w.of("job-reminder")
    assert aw.reminded_at == first_ask


def test_device_key_proof_of_possession(tmp_path, monkeypatch):
    if not crypto.AVAILABLE:  # dependency-free run without `cryptography`
        return
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    k = crypto.load_or_create()
    assert crypto.fingerprint_of(k.public_b64) == k.fingerprint
    nonce = b"per-connection-nonce"
    sig = k.sign(nonce)
    assert crypto.verify(k.public_b64, nonce, sig)          # holder verifies
    assert not crypto.verify(k.public_b64, b"other", sig)   # wrong challenge fails
    # The whole point: an attacker who copies the advertised pubkey but signs the
    # challenge with a DIFFERENT key cannot be verified as that identity.
    other = crypto.load_or_create.__globals__  # noqa: F841 - keep import side effects
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    forged = crypto.DeviceKey(Ed25519PrivateKey.generate()).sign(nonce)
    assert not crypto.verify(k.public_b64, nonce, forged)   # spoof rejected


def test_device_key_is_stable_across_loads(tmp_path, monkeypatch):
    if not crypto.AVAILABLE:
        return
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    a = crypto.load_or_create()
    b = crypto.load_or_create()
    assert a.fingerprint == b.fingerprint  # minted once, persisted
    assert len(a.fingerprint) == 64        # sha256 hex


# MARK: per-node stats (usage EMA + quota) and account types


def test_surplus_first_is_the_default_strategy_everywhere():
    """Both defaults: the dispatcher's target ranking AND a duty's placement,
    so work follows relative spare capacity unless a duty is explicitly pinned."""
    assert config.dispatch_strategy() == "surplus-first"
    assert core.mesh()["defaultStrategy"] == "surplus-first"
    # An unpinned duty resolves to it through the real placement path.
    assert config.placement_for("review", PlacementOverrides()).strategy == "surplus-first"


def test_dispatch_strategy_and_plan_weights():
    assert config.dispatch_strategy() == "surplus-first"
    assert config.plan_weight("pro") == 1.0
    assert config.plan_weight("max-5x") == 5.0
    assert config.plan_weight("max-20x") == 20.0
    assert config.plan_weight("nonexistent") == 1.0  # unknown → Pro-equivalent, safe


def test_stats_ema_decays_over_time_constant(tmp_path, monkeypatch):
    import math

    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    now, day = 1_000_000.0, 86_400.0
    st = stats.load(now=now)
    assert st.plan  # a default plan (from the model)
    st = stats.record(st, 3.0, now=now)
    # usageAvg is the reservoir over the ~21-day time constant.
    assert abs(st.usage_avg() - 3.0 / 21.0) < 1e-9
    # After one time constant of idle, the average decays by 1/e.
    aged = st.decayed(now + 21 * day)
    assert abs(aged.usage_avg() - st.usage_avg() * math.exp(-1)) < 1e-9


def test_stats_quota_is_account_type_aware_and_windowed(tmp_path, monkeypatch):
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    now, day = 1_000_000.0, 86_400.0
    st = stats.load(now=now)
    st = stats.apply_stat_attrs(st, {"plan": "max-20x"}, now=now)
    assert st.capacity() == 20.0  # Max 20× has 4× the room of Max 5×
    st = stats.record(st, 3.0, now=now)
    assert abs(st.quota_left() - 17.0) < 1e-9
    # The quota window (7 d default) rolls forward and resets what's been used.
    rolled = st.decayed(now + 8 * day)
    assert rolled.quota_used == 0.0 and rolled.quota_left() == 20.0


def test_stats_apply_attrs_edits_and_surplus(tmp_path, monkeypatch):
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    now = 1_000_000.0
    st = stats.apply_stat_attrs(stats.load(now=now),
                                {"plan": "max-20x", "quotaLeft": 12.0, "usageAvg": 2.0},
                                now=now)
    assert st.plan == "max-20x"
    assert abs(st.quota_left() - 12.0) < 1e-9
    assert abs(st.usage_avg() - 2.0) < 1e-9
    # Surplus is the burn-down ratio, not quotaLeft − usageAvg: the edit set
    # quotaLeft to 12 of 20 capacity and restarted the window, so 60% of the
    # budget remains with the full 7 days still to cover — behind the line.
    assert abs(st.surplus(now=now) - 0.6) < 1e-9
    # A 'usage' delta books against the quota.
    st2 = stats.apply_stat_attrs(st, {"usage": 1.0}, now=now)
    assert st2.quota_left() < st.quota_left()
    # quotaLeft can't exceed the plan capacity (set-too-high clamps).
    st3 = stats.apply_stat_attrs(st, {"quotaLeft": 999.0}, now=now)
    assert st3.quota_left() == st3.capacity() == 20.0


def test_stats_persist_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    now = 1_000_000.0
    st = stats.record(stats.load(now=now), 2.0, now=now)
    stats.save(st)
    again = stats.load(now=now)
    assert again.plan == st.plan
    assert abs(again.usage_avg() - st.usage_avg()) < 1e-9
    assert abs(again.quota_left() - st.quota_left()) < 1e-9


def test_stats_load_and_apply_reject_non_finite_floats(tmp_path, monkeypatch):
    """A non-finite float (∞/NaN — which float() accepts, unlike the swept int(inf)
    OverflowError) must not enter NodeStats: a non-finite acc drives surplus() to ±inf/nan
    (so the node mis-ranks itself in surplus-first dispatch) AND rides advertise() into the
    snapshot as a bare RFC-8259-invalid Infinity/NaN that a strict reader rejects WHOLESALE.
    A corrupt stats.json falls back to the clean default; a set-attr edit's non-finite value
    is skipped (the max(0.0, x) clamp folds NaN but not +inf)."""
    import json
    import math
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    now = 1_000_000.0
    for bad in (float("inf"), float("nan"), float("-inf")):
        stats.stats_path().write_text(json.dumps(
            {"plan": "max-5x", "acc": bad, "quotaUsed": 0.0,
             "windowStart": 0.0, "updatedAt": 0.0}))
        st = stats.load(now=now)
        assert st.acc == 0.0 and math.isfinite(st.surplus())     # fell back to _default
        adv = st.advertise()
        assert all(math.isfinite(v) for v in adv.values() if isinstance(v, float))
        assert "Infinity" not in json.dumps({"stats": adv}) and "NaN" not in json.dumps(adv)
    base = stats._default(now)
    for key in ("usageAvg", "usage", "quotaLeft"):
        st = stats.apply_stat_attrs(base, {key: float("inf")}, now=now)
        assert math.isfinite(st.acc) and math.isfinite(st.surplus())
        assert math.isfinite(st.advertise()["usageAvg"])
    ok = stats.apply_stat_attrs(base, {"usageAvg": 2.0}, now=now)   # a finite edit still applies
    assert abs(ok.usage_avg() - 2.0) < 1e-9


def test_stats_apply_rejects_a_finite_input_that_overflows_the_field(tmp_path, monkeypatch):
    """Round-16: _finite guarding only the INPUT is not enough — a FINITE set-attr value can
    still OVERFLOW the stored field to +inf (a float product/sum overflows silently, with NO
    OverflowError, unlike int). The two acc sinks are usageAvg * _tau_days() and record()'s
    running sum; an overflowed acc poisons surplus()/advertise() and rides into the snapshot as
    a bare RFC-8259-invalid Infinity that a strict Swift reader rejects WHOLESALE. The edit must
    be SKIPPED (prior finite value kept), exactly like a non-finite input. Discriminates the
    Round-16 fix: an input-only guard leaks acc=inf here."""
    import json
    import math
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    now = 1_000_000.0
    base = stats._default(now)
    # usageAvg: 1e307 is finite (passes an input-only guard) but 1e307 * 21.0 overflows to inf.
    st = stats.apply_stat_attrs(base, {"usageAvg": 1e307}, now=now)
    assert math.isfinite(st.acc), "usageAvg overflow leaked a non-finite acc"
    assert math.isfinite(st.surplus())
    adv = st.advertise()
    assert all(math.isfinite(v) for v in adv.values() if isinstance(v, float))
    assert "Infinity" not in json.dumps({"stats": adv}) and "NaN" not in json.dumps(adv)
    # usage/record: two near-max books at the same instant overflow the running sum.
    st2 = stats.apply_stat_attrs(base, {"usage": 1.7e308}, now=now)
    st2 = stats.apply_stat_attrs(st2, {"usage": 1.7e308}, now=now)
    assert math.isfinite(st2.acc) and math.isfinite(st2.quota_used), \
        "record() running-sum overflow leaked a non-finite field"
    # A legitimate in-range edit still lands — the guard rejects only the overflow.
    ok = stats.apply_stat_attrs(base, {"usageAvg": 3.0}, now=now)
    assert abs(ok.usage_avg() - 3.0) < 1e-9


# MARK: surplus-first load balancing


def _pace(frac_left: float, days_to_reset: float, window_days: float = 7.0) -> float:
    """The burn-down ratio a node with ``frac_left`` of its budget and
    ``days_to_reset`` left on a ``window_days`` window advertises."""
    now = 1_000_000.0
    w = usage.QuotaWindow(frac_left, now + days_to_reset * 86400.0,
                          window_days * 86400.0)
    return w.pace(now)


def _snode(id: str, plan: str = "max-5x", surplus: float = 1.0, tier: int = 3,
           platform: str = "linux", tokens: str = "ok") -> NodeInfo:
    weight = {"pro": 1.0, "max-5x": 5.0, "max-20x": 20.0}[plan]
    return NodeInfo(id=id, name=id, platform=platform, tier=tier, tokens=tokens,
                    stats={"plan": plan, "quotaLeft": weight, "usageAvg": 0.0,
                           "surplus": surplus})


def test_pace_measures_budget_against_the_clock_not_in_absolute_terms():
    """The two cases that a raw remaining-percentage comparison gets backwards.

    More budget left does NOT mean more spare capacity: what matters is how much
    budget remains per unit of time it still has to cover."""
    # 60% left but only 2 of 7 days to burn it — flush, and it expires at the
    # reset, so this is the node to drain.
    assert abs(_pace(0.60, days_to_reset=2) - 2.1) < 1e-9
    # 70% left with 6 of 7 days still to cover — a bigger number, but genuinely
    # low: it is behind the burn-down line and has to ration.
    assert abs(_pace(0.70, days_to_reset=6) - 0.8166666) < 1e-6
    # So despite holding LESS budget, the first node ranks as the flusher one.
    assert _pace(0.60, days_to_reset=2) > _pace(0.70, days_to_reset=6)
    # 1.0 is exactly on the line: budget left proportional to time left.
    assert abs(_pace(0.5, days_to_reset=3.5) - 1.0) < 1e-9
    assert abs(_pace(1.0, days_to_reset=7) - 1.0) < 1e-9


def test_pace_edges_are_bounded_and_never_divide_by_zero():
    # Exhausted is exhausted, however imminent the reset — no free lunch.
    assert _pace(0.0, days_to_reset=0.001) == 0.0
    # A reset that is due (or overdue, e.g. a stale advert) makes the whole
    # remaining balance free to spend: saturates rather than dividing by zero.
    assert _pace(0.5, days_to_reset=0.0) == usage.PACE_CAP
    assert _pace(0.5, days_to_reset=-3) == usage.PACE_CAP
    # Vanishing clock saturates at the cap instead of running off to infinity.
    assert _pace(0.9, days_to_reset=0.0001) == usage.PACE_CAP
    assert _pace(1.0, days_to_reset=7) < usage.PACE_CAP  # ordinary values unaffected


def test_nodeinfo_pubkey_and_stats_roundtrip():
    n = NodeInfo(id="a", name="a", platform="linux", tier=3, tokens="ok",
                 pubkey="QUJDRA==",
                 stats={"plan": "max-20x", "quotaLeft": 18.0, "usageAvg": 2.0,
                        "surplus": 1.6})
    d = n.to_dict()
    assert d["pubkey"] == "QUJDRA==" and d["stats"]["plan"] == "max-20x"
    assert NodeInfo.from_dict(d) == n
    assert abs(NodeInfo.from_dict(d).surplus() - 1.6) < 1e-9
    # A bare node omits the additive fields entirely (v1 wire-compat) and still
    # roundtrips; its surplus is a neutral 1.0 — on the burn-down line. Advertising
    # a pubkey grants nothing on its own - trust needs proof of possession + a
    # local allowlist entry.
    bare = NodeInfo(id="b", name="b", platform="linux", tier=3, tokens="ok")
    bd = bare.to_dict()
    assert "pubkey" not in bd and "stats" not in bd
    assert NodeInfo.from_dict(bd) == bare
    assert bare.surplus() == protocol.NEUTRAL_SURPLUS == 1.0


def test_legacy_stats_without_a_surplus_field_rank_neutrally():
    """A peer on an older build advertises only the absolute quotaLeft/usageAvg
    pair. Those are a different scale (capacity units, commonly >1), so converting
    them would let one legacy advert outrank every paced node."""
    legacy = NodeInfo(id="old", name="old", platform="linux", tier=3, tokens="ok",
                      stats={"plan": "max-20x", "quotaLeft": 18.0, "usageAvg": 2.0})
    assert legacy.surplus() == protocol.NEUTRAL_SURPLUS
    # Garbage in the field degrades the same way, never raises.
    assert _snode("junk", surplus="lots").surplus() == protocol.NEUTRAL_SURPLUS


def test_surplus_first_ranks_by_pace_not_by_raw_remaining_budget():
    # The headline behaviour change, in the user's own terms: `drain` holds LESS
    # budget than `hoard` but its window resets in 2 days, so it must win.
    drain = _snode("drain", surplus=_pace(0.60, days_to_reset=2))    # 2.10
    hoard = _snode("hoard", surplus=_pace(0.70, days_to_reset=6))    # 0.82
    mid = _snode("mid", surplus=_pace(0.50, days_to_reset=3.5))      # 1.00
    # Through the public assign path (an override naming the strategy)…
    o = PlacementOverrides().with_duty("review", Placement("surplus-first", True), by="x")
    assert assign.assign_duty("review", [hoard, drain, mid], o).assigned == ("drain",)
    # …and through the dispatch-time ranking override.
    slots = assign.slot_candidates("review", [hoard, drain, mid], strategy="surplus-first")
    assert slots == [("any", ["drain", "mid", "hoard"])]


def test_surplus_first_no_longer_favours_a_bigger_plan_on_size_alone():
    """Deliberate change: two idle accounts are equally flush in relative terms —
    each has all of its budget and all of its window. Plan size used to decide
    (absolute units), which starved the smaller account even when it had just as
    much room to spend proportionally. Now the stable tier tie-break decides."""
    big = _snode("big", "max-20x", surplus=_pace(1.0, days_to_reset=7), tier=1)
    small = _snode("small", "max-5x", surplus=_pace(1.0, days_to_reset=7), tier=4)
    slots = assign.slot_candidates("review", [big, small], strategy="surplus-first")
    assert slots[0][1] == ["small", "big"]  # tied on pace → weakest-first breaks it
    # And a small plan that is genuinely ahead of pace beats a big idle one.
    ahead = _snode("ahead", "pro", surplus=_pace(0.9, days_to_reset=1), tier=1)
    slots = assign.slot_candidates("review", [big, ahead], strategy="surplus-first")
    assert slots[0][1][0] == "ahead"


def test_surplus_ranking_is_bucketed_so_drift_cannot_reshuffle_peers():
    """Pace slides continuously as the reset clock runs down. Without hysteresis
    that would churn the ranking — and the gossiped adverts — on noise alone."""
    a = _snode("a", surplus=1.500, tier=4)
    b = _snode("b", surplus=1.51, tier=1)  # within one bucket of `a`
    slots = assign.slot_candidates("review", [a, b], strategy="surplus-first")
    assert slots[0][1] == ["a", "b"]  # same bucket → the stable tier break decides
    # A difference bigger than the bucket does move the ranking.
    c = _snode("c", surplus=1.5 + 3 * protocol.SURPLUS_RANK_BUCKET, tier=1)
    slots = assign.slot_candidates("review", [a, c], strategy="surplus-first")
    assert slots[0][1] == ["c", "a"]


def test_surplus_bucket_clamps_a_hostile_out_of_range_advert_instead_of_crashing():
    """main added surplus_bucket() = round(value / SURPLUS_RANK_BUCKET) and made
    surplus-first the DEFAULT ranking. A peer's advertised surplus is attacker-shaped: a
    FINITE-but-huge value (1e307 slips past the non-finite ingestion guard — it is a real
    float, not a bigint) or a negative one drives value / SURPLUS_RANK_BUCKET to ±inf and
    makes round() raise an uncaught OverflowError on the ranking path (a keyless peer on an
    open mesh -> assign -> _ranked). Clamping to [0, SURPLUS_RANK_CAP] can't, and denies the
    attacker a bucket ABOVE a genuinely maximally-flush peer."""
    assert protocol.surplus_bucket(0.0) == 0
    top = protocol.surplus_bucket(protocol.SURPLUS_RANK_CAP)
    for hostile in (1e307, 1e308, float("inf"), -1e307, float("-inf")):
        b = protocol.surplus_bucket(hostile)               # must not raise
        assert 0 <= b <= top                               # pinned to the legit range
    assert protocol.surplus_bucket(1e307) == top           # cannot out-bucket max flush
    # End-to-end: a hostile peer does not crash the (default) surplus-first ranking.
    honest = _snode("honest", surplus=2.0, tier=1)
    attacker = _snode("attacker", surplus=1e307, tier=1)
    slots = assign.slot_candidates("review", [honest, attacker], strategy="surplus-first")
    assert set(slots[0][1]) == {"honest", "attacker"}      # completed, no OverflowError


def test_pr_agent_running_fails_open_on_undecodable_ps_output(tmp_path, monkeypatch):
    """The executor's ps ground-truth floor (`_pr_agent_running`) promises to FAIL OPEN
    like Store._live_pr_agents — a ps error reads as "not seen" so a transient failure
    never drops work. But `ps -Ao args=` under text=True decodes strict UTF-8, so any
    process on the box with a non-UTF-8 byte in its argv makes the output undecodable and
    raises UnicodeDecodeError — a ValueError, NOT an OSError/SubprocessError. Uncaught it
    escapes the guard, up through _spawn_local -> _run_local_request -> _take_job, tearing
    the dispatching peer's link (or failing a self-dispatch)."""
    from diplomat_app.mesh import node as nodemod

    node = _fresh_node(tmp_path, monkeypatch)

    def boom(*a, **k):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(nodemod.subprocess, "run", boom)
    wk = "review:github.com/owner/repo#7@abc123"
    assert node._pr_agent_running(wk) is False  # fails open, never raises


def test_advertise_quota_left_capped_by_real_binding_window(tmp_path, monkeypatch):
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    now = 1_000_000.0
    st = stats.apply_stat_attrs(stats.load(now=now), {"plan": "max-20x"}, now=now)
    assert st.quota_left() == 20.0  # bookkeeping alone: a full window
    # Real probe live: the binding window (min of session/week) caps the advert.
    assert st.advertise(real_frac=0.02)["quotaLeft"] == 0.4
    # The cap only ever lowers — real room above the booked value doesn't inflate.
    booked = stats.record(st, 15.0, now=now)  # bookkept quotaLeft 5.0
    assert booked.advertise(real_frac=0.8)["quotaLeft"] == 5.0
    # Heuristic fallback (no real probe) leaves the bookkeeping untouched.
    assert booked.advertise()["quotaLeft"] == 5.0
    # Out-of-range probe fractions clamp instead of corrupting the advert.
    assert st.advertise(real_frac=-0.5)["quotaLeft"] == 0.0
    assert st.advertise(real_frac=1.5)["quotaLeft"] == 20.0


def test_surplus_first_avoids_host_with_drained_binding_window(tmp_path, monkeypatch):
    # The regression: a Max 20× host with 2% of its 5-hour session left (but 80%
    # of its week) must NOT win dispatch — it would run out mid-task. The session
    # window binds, and pacing it against its own near-term reset keeps it low.
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    now = 1_000_000.0
    big = stats.apply_stat_attrs(stats.load(now=now), {"plan": "max-20x"}, now=now)
    session = usage.QuotaWindow(0.02, now + 2.5 * 3600, 5 * 3600.0)  # 2%, half a window
    week = usage.QuotaWindow(0.8, now + 3.5 * 86400, 7 * 86400.0)    # 80%, half a window
    pace = usage.binding_pace(session, week, now=now)
    assert abs(pace - 0.04) < 1e-9  # the session (0.02/0.5) binds, not the week (1.6)
    drained = NodeInfo(id="big", name="big", platform="linux", tier=1, tokens="low",
                       tokens_pct=0.02, tokens_session_pct=0.02, tokens_week_pct=0.8,
                       stats=big.advertise(real_frac=0.02, pace=pace, now=now))
    fresh = _snode("fresh", "max-5x", surplus=_pace(0.5, days_to_reset=3.5))  # 1.0
    slots = assign.slot_candidates("review", [drained, fresh], strategy="surplus-first")
    assert slots == [("any", ["fresh", "big"])]
    # Had only the WEEK been consulted the same host would look flush (1.6) and
    # wrongly win — which is why the binding pace is a minimum across windows.
    from dataclasses import replace
    week_only = replace(drained,
                        stats=big.advertise(pace=week.pace(now), now=now))
    slots = assign.slot_candidates("review", [week_only, fresh], strategy="surplus-first")
    assert slots[0][1][0] == "big"


def test_advertised_surplus_paces_the_local_window_when_the_probe_is_dark(
        tmp_path, monkeypatch):
    """No real probe ⇒ no reset instants from the endpoint, but the bookkeeping
    window has its own start and a fixed span, so the node still advertises a
    comparable ratio rather than dropping out of the ranking."""
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    now, day = 1_000_000.0, 86_400.0
    st = stats.apply_stat_attrs(stats.load(now=now), {"plan": "max-20x"}, now=now)
    # Fresh window: all the budget, all the clock → exactly on pace.
    assert st.advertise(now=now)["surplus"] == 1.0
    # Half the budget spent with 2 of 7 days left ⇒ 0.5 / (2/7) = 1.75: flush,
    # because what is left expires at the reset.
    spent = stats.record(st, 10.0, now=now)
    assert spent.advertise(now=now + 5 * day)["surplus"] == 1.75
    # The same balance with 6 of 7 days still to cover is behind the line
    # (7/12, rounded to the 4dp the advert carries).
    assert spent.advertise(now=now + day)["surplus"] == 0.5833


def test_node_advert_wires_real_probe_pace_into_stats(tmp_path, monkeypatch):
    # MeshNode.info must thread the live probe's pace into the advertised stats —
    # the metric is useless if the advert path skips it and paces bookkeeping.
    node = _fresh_node(tmp_path, monkeypatch)
    node.stats = stats.apply_stat_attrs(node.stats, {"plan": "max-20x"})
    node._token_state, node._token_frac = "low", 0.02
    node._token_session, node._token_week = 0.02, 0.8
    node._token_pace = 0.04
    assert node.info.stats["surplus"] == 0.04
    assert node.info.stats["quotaLeft"] == 0.4  # fraction still caps the display field
    # Heuristic fallback (no real reading): the local window is paced instead, and
    # a node that has booked nothing sits exactly on the line.
    node._token_session = node._token_week = node._token_pace = None
    assert node.info.stats["surplus"] == 1.0
    assert node.info.stats["quotaLeft"] == 20.0


def test_gossip_key_tracks_pace_at_bucket_granularity(tmp_path, monkeypatch):
    """Pace drifts every second toward the reset. Re-gossiping on raw float
    changes would re-advertise the node on every refresh tick for no routing
    benefit, so change detection uses the same buckets the ranking does."""
    node = _fresh_node(tmp_path, monkeypatch)
    node._token_pace = 1.50
    before = node._gossiped_tokens()
    node._token_pace = 1.51  # same bucket → peers see nothing new
    assert node._gossiped_tokens() == before
    node._token_pace = 1.50 + 3 * protocol.SURPLUS_RANK_BUCKET
    assert node._gossiped_tokens() != before


def test_surplus_first_neutral_stats_fall_back_to_weakest_first():
    # No stats advertised ⇒ a neutral 1.0 for all ⇒ ranking degrades to
    # weakest-first (highest tier number), preserving today's behavior for v1 nodes.
    hi = NodeInfo(id="hi", name="hi", platform="linux", tier=1, tokens="ok")
    lo = NodeInfo(id="lo", name="lo", platform="linux", tier=4, tokens="ok")
    slots = assign.slot_candidates("review", [hi, lo], strategy="surplus-first")
    assert slots == [("any", ["lo", "hi"])]


# MARK: machine-strength auto-detection (hardware.py)


def test_cpu_class_buckets_apple_silicon_and_boost_clocks():
    from diplomat_app.mesh import hardware
    # Apple Silicon tops the scale; Pro/Max/Ultra bins above the base part.
    assert hardware.cpu_class("Apple M4 Pro", None) == 4
    assert hardware.cpu_class("Apple M3 Max", None) == 4
    assert hardware.cpu_class("Apple M2 Ultra", None) == 4
    assert hardware.cpu_class("Apple M1", None) == 3
    # Non-Apple parts bucket by boost clock; unreadable clocks score neutral.
    assert hardware.cpu_class(None, 5.7) == 2
    assert hardware.cpu_class(None, 4.4) == 1
    assert hardware.cpu_class(None, 3.5) == 0
    assert hardware.cpu_class(None, None) == 0


def test_strength_score_ranks_stronger_boxes_higher():
    from diplomat_app.mesh import hardware
    weak = hardware.strength_score(ram_gb=8, cores=4, dgpu=False)
    strong = hardware.strength_score(ram_gb=64, cores=16, dgpu=True,
                                     cpu=hardware.cpu_class(None, 5.7))
    assert strong > weak
    # A maxed box scores at the top of the 0..8 range; a tiny one at the bottom.
    assert hardware.strength_score(128, 16, True, cpu=4) == 8
    assert hardware.strength_score(4, 2, False) == 0


def test_strength_score_apple_silicon_outranks_big_ram_smt_laptop():
    """Regression: an M-series Pro box (24 GB unified, 14 real cores, no dGPU)
    must outrank a 64 GB SMT laptop with a dGPU — RAM gigabytes and logical
    threads used to dominate and invert the ranking."""
    from diplomat_app.mesh import hardware
    lo, hi, _ = config.tier_bounds()
    m_pro = hardware.strength_score(
        ram_gb=24, cores=14, dgpu=False, cpu=hardware.cpu_class("Apple M4 Pro", None))
    laptop = hardware.strength_score(
        ram_gb=64, cores=8, dgpu=True, cpu=hardware.cpu_class(None, 5.0))
    assert m_pro > laptop
    assert hardware._score_to_tier(m_pro, lo, hi) == lo  # "Very strong"
    assert hardware._score_to_tier(laptop, lo, hi) > lo


def test_strength_score_maps_to_tier_bounds_inverted():
    from diplomat_app.mesh import hardware
    lo, hi, _ = config.tier_bounds()
    # 1 = strongest, so the strongest box lands on `lo` and the weakest on `hi`.
    assert hardware._score_to_tier(8, lo, hi) == lo
    assert hardware._score_to_tier(4, lo, hi) == 3
    assert hardware._score_to_tier(0, lo, hi) == hi


def test_detect_tier_honours_env_override(monkeypatch):
    from diplomat_app.mesh import hardware
    monkeypatch.setenv("DIPLOMAT_MESH_TIER", "2")
    assert hardware.detect_tier() == 2
    monkeypatch.setenv("DIPLOMAT_MESH_TIER", "999")  # clamped to bounds
    _, hi, _ = config.tier_bounds()
    assert hardware.detect_tier() == hi


# MARK: automatic token budget from real usage (usage.py)


def _write_usage(dir_path, entries):
    """Write one Claude-style transcript file; entries = [(iso_ts, in, out, cache)]."""
    import json as _json
    proj = dir_path / ".claude" / "projects" / "demo"
    proj.mkdir(parents=True, exist_ok=True)
    lines = []
    for ts, i, o, c in entries:
        lines.append(_json.dumps({
            "timestamp": ts,
            "message": {"usage": {"input_tokens": i, "output_tokens": o,
                                  "cache_creation_input_tokens": c,
                                  "cache_read_input_tokens": 9_999_999}},
        }))
    (proj / "session.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_window_tokens_sums_recent_and_excludes_cache_reads(tmp_path, monkeypatch):
    from datetime import datetime, timezone, timedelta
    from diplomat_app.mesh import usage
    monkeypatch.setenv("HOME", str(tmp_path))
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(hours=9)).isoformat()  # outside a 5h window
    _write_usage(tmp_path, [(recent, 100, 50, 25), (old, 1000, 1000, 1000)])
    got = usage.window_tokens(now.timestamp(), window_hours=5.0)
    # only the recent turn, and cache_read (9.9M) is NOT counted.
    assert got == 175.0


def test_token_state_thresholds(monkeypatch):
    from diplomat_app.mesh import usage
    # Ceiling for pro = weight(1) * tokensPerWeight.
    ceiling = usage.token_ceiling("pro")
    assert ceiling == config.tokens_per_weight()
    assert usage.state_from_fraction(1.0) == "ok"
    assert usage.state_from_fraction(0.0) == "out"
    assert usage.state_from_fraction(config.low_threshold() / 2) == "low"


def test_token_state_prefers_real_quota_over_heuristic(monkeypatch):
    """When the OAuth probe answers, the state comes from the account's REAL
    windows — the tighter (binding) one — and both fractions are surfaced."""
    from diplomat_app.mesh import usage
    now = 1_000_000.0
    session = usage.QuotaWindow(0.64, now + 2.5 * 3600, 5 * 3600.0)   # half the clock left
    week = usage.QuotaWindow(0.27, now + 3.5 * 86400, 7 * 86400.0)    # half the clock left
    monkeypatch.setattr(usage, "windows", lambda: (session, week))
    state, frac, sess, wk, pace = usage.token_state("pro", now=now)
    assert (sess, wk) == (0.64, 0.27)
    assert frac == 0.27              # the week window binds
    assert state == "low"            # 0.27 < lowThreshold 0.34
    # Pace is the tighter burn-down ratio: the week at 0.27/0.5 = 0.54 binds over
    # the session at 0.64/0.5 = 1.28.
    assert abs(pace - 0.54) < 1e-9


def test_token_state_falls_back_to_heuristic_when_probe_dark(tmp_path, monkeypatch):
    from diplomat_app.mesh import usage
    monkeypatch.setenv("HOME", str(tmp_path))  # empty logs → fresh heuristic
    monkeypatch.setattr(usage, "windows", lambda: (None, None))
    state, frac, sess, week, pace = usage.token_state("pro")
    assert (state, frac) == ("ok", 1.0)
    # All three None mark the fraction an estimate; with no reset instants there
    # is nothing to pace against, so the node paces its bookkeeping window instead.
    assert sess is None and week is None and pace is None


def test_quota_left_parses_utilization_and_caches(monkeypatch):
    from diplomat_app.mesh import usage
    monkeypatch.delenv("DIPLOMAT_MESH_OAUTH_PROBE", raising=False)
    calls = []
    payload = {"five_hour": {"utilization": 36.0}, "seven_day": {"utilization": 27}}
    monkeypatch.setattr(usage, "_fetch_usage_payload",
                        lambda: calls.append(1) or payload)
    usage._reset_probe_cache()
    try:
        assert usage.quota_left() == (0.64, 0.73)
        assert usage.quota_left() == (0.64, 0.73)  # within TTL → served from cache
        assert len(calls) == 1
        # A malformed window (or an over-100 utilization) degrades per-field, never raises.
        assert usage._window({"utilization": 250}, 100.0).frac_left == 0.0
        assert usage._window({"utilization": "high"}, 100.0) is None
        assert usage._window(None, 100.0) is None
    finally:
        usage._reset_probe_cache()  # never leak a fake probe result to other tests


def test_probe_reads_the_reset_instant_each_window_reports(monkeypatch):
    """The endpoint's ``resets_at`` is what makes surplus relative — without it
    there is no clock to divide the remaining budget by."""
    from diplomat_app.mesh import usage
    monkeypatch.delenv("DIPLOMAT_MESH_OAUTH_PROBE", raising=False)
    payload = {
        "five_hour": {"utilization": 36.0,
                      "resets_at": "2026-07-20T19:09:59.816900+00:00"},
        "seven_day": {"utilization": 27,
                      "resets_at": "2026-07-21T06:59:59.816926+00:00"},
    }
    monkeypatch.setattr(usage, "_fetch_usage_payload", lambda: payload)
    usage._reset_probe_cache()
    try:
        session, week = usage.windows()
        assert session.frac_left == 0.64 and week.frac_left == 0.73
        assert session.length_secs == 5 * 3600.0
        assert week.length_secs == 7 * 86400.0
        # Parsed as real instants, not dropped on the floor (the pre-fix behaviour).
        from datetime import datetime, timezone
        assert session.resets_at == datetime(2026, 7, 20, 19, 9, 59, 816900,
                                             tzinfo=timezone.utc).timestamp()
        assert week.resets_at > session.resets_at
    finally:
        usage._reset_probe_cache()


def test_window_without_a_reset_instant_assumes_a_full_span(monkeypatch):
    """A window the endpoint reports without ``resets_at`` must still be paceable:
    assuming a full span ahead paces it at its raw fraction — the neutral reading,
    and exactly how the metric behaved before reset instants were read."""
    from diplomat_app.mesh import usage
    now = 1_000_000.0
    w = usage._window({"utilization": 30.0}, 100.0, now=now)
    assert w.resets_at == now + 100.0
    assert abs(w.pace(now) - 0.7) < 1e-9


def test_quota_probe_disabled_by_env(monkeypatch):
    from diplomat_app.mesh import usage

    def _boom():
        raise AssertionError("probe must not run when disabled")

    monkeypatch.setenv("DIPLOMAT_MESH_OAUTH_PROBE", "0")
    monkeypatch.setattr(usage, "_fetch_usage_payload", _boom)
    usage._reset_probe_cache()
    assert usage.quota_left() == (None, None)


# MARK: identity — auto-detect + manual pin + token override


def test_identity_auto_detects_strength_on_first_run(tmp_path, monkeypatch):
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    monkeypatch.setenv("DIPLOMAT_MESH_TIER", "2")
    n = identity.load()
    assert n.strength_auto and n.tier == 2 and n.tokens == "auto"
    # Persisted with the auto flag; a reload with a different detected tier follows it.
    monkeypatch.setenv("DIPLOMAT_MESH_TIER", "4")
    assert identity.load().tier == 4


def test_identity_explicit_tier_in_file_is_a_pin(tmp_path, monkeypatch):
    import json as _json
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    monkeypatch.setenv("DIPLOMAT_MESH_TIER", "1")  # would auto-detect strong…
    (tmp_path / "node.json").write_text(_json.dumps(
        {"id": "abc123", "name": "box", "tier": 5}))  # …but the file pins weak
    n = identity.load()
    assert n.tier == 5 and not n.strength_auto  # explicit tier wins, auto off


# MARK: - server mode + API key (config + wire)


def test_server_mode_and_api_key_config(monkeypatch):
    monkeypatch.delenv("DIPLOMAT_MESH_SERVER", raising=False)
    monkeypatch.delenv("DIPLOMAT_MESH_API_KEY", raising=False)
    assert config.server_mode() is False
    assert config.api_key() == ""
    monkeypatch.setenv("DIPLOMAT_MESH_SERVER", "1")
    monkeypatch.setenv("DIPLOMAT_MESH_API_KEY", "sekret")
    assert config.server_mode() is True
    assert config.api_key() == "sekret"


def test_dispatch_and_ctl_carry_api_key_only_when_set():
    j = protocol.Job(id="j", duty="review", prompt="p", requested_by="a", requested_at=0.0)
    assert "apiKey" not in protocol.dispatch(j)          # omitted when empty (v1 compat)
    assert protocol.dispatch(j, "k")["apiKey"] == "k"
    assert "apiKey" not in protocol.ctl_hello("")
    assert protocol.ctl_hello("s", "k")["apiKey"] == "k"


# MARK: - trust hardening (auth domain separation, verified-fp binding, bounds)


def test_auth_signature_is_domain_separated(tmp_path, monkeypatch):
    if not crypto.AVAILABLE:  # dependency-free run without `cryptography`
        return
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    from diplomat_app.mesh import node as node_mod

    k = crypto.load_or_create()
    nonce = "a1b2c3d4e5f60718"
    assert node_mod._auth_challenge(nonce).startswith(b"szpontnet-auth-v1:")
    good = k.sign(node_mod._auth_challenge(nonce))
    assert crypto.verify(k.public_b64, node_mod._auth_challenge(nonce), good)
    # A signature over the BARE nonce must NOT verify against the domain-tagged
    # construction — this is what stops the device key being usable as a generic
    # signing oracle over attacker-chosen bytes.
    bare = k.sign(nonce.encode())
    assert not crypto.verify(k.public_b64, node_mod._auth_challenge(nonce), bare)


def _fresh_node(tmp_path, monkeypatch):
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    from diplomat_app.mesh.node import MeshNode
    return MeshNode()


def _peer_info(node_id: str, seq: int, pubkey: str = "") -> NodeInfo:
    return NodeInfo(id=node_id, name="p", platform="linux", tier=3, tokens="ok",
                    epoch=1.0, seq=seq, pubkey=pubkey)


class _FakeWriter:
    """A stand-in StreamWriter for driving `_learn_node` as if a hello arrived on
    the peer's own link (no real socket)."""
    def write(self, *a):
        pass

    def close(self, *a):
        pass


def test_verified_fp_rekey_only_via_own_link_not_third_party_gossip(tmp_path, monkeypatch):
    node = _fresh_node(tmp_path, monkeypatch)
    node._learn_node(_peer_info("peer1", 1, pubkey="QUJDRA=="), "1.2.3.4", None)
    peer = node.peers["peer1"]
    peer.verified_fp = crypto.fingerprint_of("QUJDRA==")  # pretend it proved this key

    # A THIRD-PARTY gossip relay (link_writer=None) advertising a DIFFERENT pubkey
    # with an inflated seq must NOT clear the verification — otherwise a relayed
    # spoof could force a personal peer to foreign and (via the inflated seq) block
    # its recovery. Trust keys on the proven fingerprint, so the drift is harmless.
    node._learn_node(_peer_info("peer1", 999, pubkey="RUZHSA=="), "1.2.3.4", None)
    assert node.peers["peer1"].verified_fp == crypto.fingerprint_of("QUJDRA==")

    # A re-key the peer advertises ON ITS OWN LINK (a hello, link_writer set) DOES
    # drop the verification, forcing re-proof of the new key.
    w = _FakeWriter()
    node._learn_node(_peer_info("peer1", 1000, pubkey="SUpLTA=="), "1.2.3.4", w)
    assert node.peers["peer1"].verified_fp is None

    # A same-pubkey fresher advertisement on its own link keeps the verification.
    node.peers["peer1"].verified_fp = crypto.fingerprint_of("SUpLTA==")
    node._learn_node(_peer_info("peer1", 1001, pubkey="SUpLTA=="), "1.2.3.4", w)
    assert node.peers["peer1"].verified_fp == crypto.fingerprint_of("SUpLTA==")


def test_malformed_beacon_epoch_is_ignored_not_fatal(tmp_path, monkeypatch):
    node = _fresh_node(tmp_path, monkeypatch)
    node._learn_node(_peer_info("peer1", 1), "1.2.3.4", _FakeWriter())  # linked peer
    assert node.peers["peer1"].linked
    # A beacon carrying a non-numeric epoch for a linked peer must be dropped, never
    # raise out of the UDP reader callback.
    node._on_beacon({"t": "beacon", "id": "peer1", "tcpPort": 5, "epoch": "pwn"},
                    "9.9.9.9")
    node._on_beacon({"t": "beacon", "id": "peer1", "tcpPort": 5, "epoch": {"x": 1}},
                    "9.9.9.9")
    # The live link and its address survived (a malformed beacon changed nothing).
    assert node.peers["peer1"].linked and node.peers["peer1"].addr == "1.2.3.4"


def test_peer_table_is_bounded_on_gossip(tmp_path, monkeypatch):
    from diplomat_app.mesh import node as node_mod
    node = _fresh_node(tmp_path, monkeypatch)
    for i in range(node_mod._MAX_PEERS + 25):  # a gossip flood of spoofed ids
        node._learn_node(_peer_info(f"p{i:05d}", 1), "1.2.3.4", None)
    assert len(node.peers) == node_mod._MAX_PEERS  # capped, not unbounded


def test_reapable_covers_downed_and_gossip_only_phantoms(tmp_path, monkeypatch):
    import time as _time
    from diplomat_app.mesh import node as node_mod
    node = _fresh_node(tmp_path, monkeypatch)
    node._learn_node(_peer_info("phantom", 1), "1.2.3.4", None)  # gossip-only, never linked
    peer = node.peers["phantom"]
    now = _time.monotonic()
    assert not node._reapable(peer, now)  # fresh phantom stays
    # A phantom whose last gossip is older than the retention window is reaped even
    # though it never went through _drop_peer (down_since is None).
    assert node._reapable(peer, now + node_mod._DOWN_RETENTION_SECS + 1)
    peer.down_since = now  # a normally-downed peer uses down_since as the reference
    assert not node._reapable(peer, now)
    assert node._reapable(peer, now + node_mod._DOWN_RETENTION_SECS + 1)


def test_gossip_does_not_refresh_a_linked_peers_heartbeat_clock(tmp_path, monkeypatch):
    """Round-16: a non-fresh third-party `node` gossip must NOT refresh a DIRECTLY-LINKED
    peer's last_seen. For a bound peer, `now - last_seen > peerTimeoutSecs` in _heartbeat_loop
    is the ONLY reaper (the Round-15 read-timeout skips bound writers; drain errors are
    suppressed), so if a replayed public advert keeps last_seen fresh, a dead-but-half-open
    peer reads link_state != "down" forever and its work-claims stay authoritative — an
    advert-replay work-suppression DoS. Own-link hellos and gossip-only PHANTOMS must still
    refresh (the phantom's last_seen is its only liveness signal, consumed by _reapable)."""
    import time as _time
    node = _fresh_node(tmp_path, monkeypatch)
    stale, timeout = node.proto["peerStaleSecs"], node.proto["peerTimeoutSecs"]

    # A linked peer that has gone silent (half-open link) reads "down"...
    node._learn_node(_peer_info("P", 5), "1.2.3.4", _FakeWriter())
    P = node.peers["P"]
    assert P.linked
    P.last_seen = _time.monotonic() - (timeout + 10.0)
    assert P.link_state(stale, timeout) == "down"
    # ...and a non-fresh third-party gossip replay (link_writer=None, same seq) must NOT
    # resurrect it — the fix. Pre-fix, lines 1209-1210 refreshed last_seen unconditionally.
    node._learn_node(_peer_info("P", 5), "9.9.9.9", None)
    assert P.link_state(stale, timeout) == "down", "replayed gossip resurrected a dead linked peer"

    # No over-gating: a gossip-only PHANTOM (never linked) still has its clock refreshed,
    # so _reapable keeps using a live last_seen.
    node._learn_node(_peer_info("ghost", 1), "1.2.3.4", None)
    ghost = node.peers["ghost"]
    assert not ghost.linked
    ghost.last_seen = _time.monotonic() - 5.0
    node._learn_node(_peer_info("ghost", 1), "1.2.3.4", None)  # non-fresh gossip
    assert _time.monotonic() - ghost.last_seen < 1.0, "phantom gossip refresh was wrongly suppressed"

    # No over-gating: a hello on the peer's OWN link still refreshes a linked peer's clock.
    P.last_seen = _time.monotonic() - (timeout + 10.0)
    node._learn_node(_peer_info("P", 6), "1.2.3.4", _FakeWriter())  # own-link hello (fresh)
    assert P.link_state(stale, timeout) == "up", "own-link hello must refresh a linked peer"


# MARK: - authenticated gossip (self-signed adverts + overrides)


def _mk_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    return crypto.DeviceKey(Ed25519PrivateKey.generate())


def _signed_advert(key, node_id: str, seq: int = 1, **fields) -> dict:
    info = NodeInfo(id=node_id, name="p", platform="linux", tier=3, tokens="ok",
                    epoch=1.0, seq=seq, pubkey=key.public_b64, **fields)
    d = info.to_dict()
    d["sig"] = key.sign(protocol.advert_signing_bytes(d))
    return d


def test_advert_signature_rejects_forgery_and_tamper(tmp_path, monkeypatch):
    if not crypto.AVAILABLE:  # dependency-free run without `cryptography`
        return
    node = _fresh_node(tmp_path, monkeypatch)
    k = _mk_key()
    assert node._advert_authentic(_signed_advert(k, "peerX"))            # valid self-signature
    # A keyless advert has nothing to verify — accepted (stays unauthenticated).
    assert node._advert_authentic(
        NodeInfo(id="peerY", name="p", platform="linux", tier=3, tokens="ok").to_dict())
    # Keyed but unsigned → rejected.
    unsigned = _signed_advert(k, "peerX")
    del unsigned["sig"]
    assert not node._advert_authentic(unsigned)
    # Tampered after signing (a field changed in relay) → rejected.
    tampered = _signed_advert(k, "peerX")
    tampered["tier"] = 1
    assert not node._advert_authentic(tampered)
    # Signed by a DIFFERENT key than the advertised pubkey → rejected.
    forged = _signed_advert(k, "peerX")
    forged["pubkey"] = _mk_key().public_b64
    assert not node._advert_authentic(forged)


def test_gossip_cannot_hijack_a_pinned_node_id(tmp_path, monkeypatch):
    if not crypto.AVAILABLE:
        return
    node = _fresh_node(tmp_path, monkeypatch)
    k = _mk_key()
    a1 = _signed_advert(k, "peerX", seq=1)
    node._learn_node(NodeInfo.from_dict(a1), "1.2.3.4", None, raw=a1)  # pin peerX → key k
    assert node.peers["peerX"].info.pubkey == k.public_b64
    # A third party self-signs an advert for peerX with ITS OWN key and a higher seq.
    # The signature is valid (self-consistent), so the PIN is what must reject it.
    a2 = _signed_advert(_mk_key(), "peerX", seq=999)
    assert node._advert_authentic(a2)
    node._learn_node(NodeInfo.from_dict(a2), "9.9.9.9", None, raw=a2)
    assert node.peers["peerX"].info.pubkey == k.public_b64  # unchanged — id hijack blocked


def test_own_link_hello_rekeys_past_a_forged_inflated_epoch(tmp_path, monkeypatch):
    """A forged GOSSIP advert for a peer's id, carrying an INFLATED epoch, must not
    permanently hijack the id→key pin. The real peer's own-link hello + proof of
    possession still re-keys and verifies (docs/szpontnet/11 — only the own link may
    re-key), so it becomes personal once allowlisted. Regression: the re-key was
    gated behind `if fresh:`, and the forged epoch made the honest hello non-fresh,
    leaving the victim foreign forever."""
    if not crypto.AVAILABLE:
        return
    from diplomat_app.mesh import node as node_mod
    node = _fresh_node(tmp_path, monkeypatch)
    attacker, real = _mk_key(), _mk_key()

    def advert(key, epoch, seq):
        info = NodeInfo(id="peerX", name="p", platform="linux", tier=3, tokens="ok",
                        epoch=epoch, seq=seq, pubkey=key.public_b64)
        d = info.to_dict()
        d["sig"] = key.sign(protocol.advert_signing_bytes(d))
        return d

    # 1. Forged cold-join gossip pins peerX to the attacker key with a huge epoch.
    forged = advert(attacker, epoch=1e18, seq=999)
    node._learn_node(NodeInfo.from_dict(forged), "9.9.9.9", None, raw=forged)
    assert node.peers["peerX"].info.pubkey == attacker.public_b64
    assert node.peers["peerX"].verified_fp is None

    # 2. The real peerX connects on its OWN link with a realistic (smaller) epoch.
    w = _FakeWriter()
    real_advert = advert(real, epoch=1.75e9, seq=1)
    node._learn_node(NodeInfo.from_dict(real_advert), "1.2.3.4", w, raw=real_advert)
    assert node.peers["peerX"].info.pubkey == real.public_b64  # re-keyed past the forged epoch

    # 3. peerX proves possession of its real key → verified fingerprint recorded.
    node._issued_nonce[w] = "nonce-xyz"
    node._verify_auth({"sig": real.sign(node_mod._auth_challenge("nonce-xyz"))}, w)
    assert node.peers["peerX"].verified_fp == crypto.fingerprint_of(real.public_b64)

    # 4. Allowlisting the real fingerprint promotes the recovered peer to personal.
    node._trusted[crypto.fingerprint_of(real.public_b64)] = "personal"
    assert node._peer_trust(node.peers["peerX"]) == "personal"


def test_non_fresh_own_link_hello_cannot_inherit_personal_trust(tmp_path, monkeypatch):
    """An attacker opening a link as a verified-personal peer's id with a LOWER epoch
    takes over the peer's writer (inherent to newest-link-wins), but must NOT inherit
    its personal trust: the stale verified_fp is dropped so the new link must re-prove
    possession, and an unproven/keyless peer lands foreign. Regression for a
    privilege-escalation hijack (writer reassigned while verified_fp was kept)."""
    if not crypto.AVAILABLE:
        return
    from diplomat_app.mesh import node as node_mod
    real = _mk_key()

    def advert(key, epoch, seq):
        pub = key.public_b64 if key else ""
        info = NodeInfo(id="peerX", name="p", platform="linux", tier=3, tokens="ok",
                        epoch=epoch, seq=seq, pubkey=pub)
        d = info.to_dict()
        if key:
            d["sig"] = key.sign(protocol.advert_signing_bytes(d))
        return d

    for attacker in (None, _mk_key()):  # keyless, and a different real key
        node = _fresh_node(tmp_path, monkeypatch)
        # Establish peerX verified + personal on its own link (higher epoch).
        w1 = _FakeWriter()
        a_real = advert(real, epoch=1.75e9, seq=5)
        node._learn_node(NodeInfo.from_dict(a_real), "1.2.3.4", w1, raw=a_real)
        node._issued_nonce[w1] = "n1"
        node._verify_auth({"sig": real.sign(node_mod._auth_challenge("n1"))}, w1)
        node._trusted[crypto.fingerprint_of(real.public_b64)] = "personal"
        assert node._peer_trust(node.peers["peerX"]) == "personal"

        # Attacker connects as peerX on a NEW link with a LOWER epoch (non-fresh).
        w2 = _FakeWriter()
        a_att = advert(attacker, epoch=1.0, seq=1)
        node._learn_node(NodeInfo.from_dict(a_att), "6.6.6.6", w2, raw=a_att)
        assert node._peer_trust(node.peers["peerX"]) == "foreign"  # no personal inheritance
        assert node.peers["peerX"].verified_fp is None


def test_placement_from_dict_tolerates_malformed_spread():
    """A placement dict can arrive over gossip (overrides) or a ctl edit and is
    resolved on every _recompute; a malformed spread entry must be SKIPPED, not
    crash assignment. Regression for the KeyError/ValueError/TypeError."""
    from diplomat_app.mesh import config
    assert config.Placement.from_dict({"spread": [{"count": 2}]}).spread == ()  # no platform
    assert config.Placement.from_dict(
        {"spread": [{"platform": "linux", "count": "lots"}]}).spread == (("linux", 1),)
    assert config.Placement.from_dict({"spread": ["linux"]}).spread == ()   # entry not a dict
    assert config.Placement.from_dict({"spread": "nope"}).spread == ()       # spread not a list
    # A non-positive count is a bad count too: it must fall back to the schema default 1,
    # not pass through — a valid -1/0 diverges assign_duty (reports satisfied) from
    # slot_candidates (range(count) == 0 slots -> dispatches to nobody).
    assert config.Placement.from_dict(
        {"spread": [{"platform": "linux", "count": -1}]}).spread == (("linux", 1),)
    assert config.Placement.from_dict(
        {"spread": [{"platform": "linux", "count": 0}]}).spread == (("linux", 1),)
    # Valid entries still parse; a missing count defaults to 1.
    assert config.Placement.from_dict(
        {"spread": [{"platform": "linux", "count": 2}, {"platform": "macos"}]}
    ).spread == (("linux", 2), ("macos", 1))


def test_negative_spread_count_does_not_falsely_satisfy_a_duty():
    """A negative/zero spread count must normalize to 1 so the two placement consumers,
    both fed by _parse_spread, stay consistent. The bug: assign_duty reported the duty
    SATISFIED (its `got == count` loop never trips for count<=0 and `got < count` is false,
    so no shortfall) while slot_candidates yielded range(count) == 0 slots and dispatched
    to NOBODY — a duty shown placed/satisfied that silently never ran. Reachable via a
    signed gossiped override on an open mesh (Round 6 bounded only the upper side)."""
    linA, linB = _node("a", "linux"), _node("d", "linux")
    for bad in (-1, 0):
        ov = PlacementOverrides(rev=1, updated_by="op", duties={
            "review": {"strategy": "weakest-first", "tokenAware": True,
                       "spread": [{"platform": "linux", "count": bad}]}})
        da = assign.assign_duty("review", [linA, linB], ov, "op")
        slots = assign.slot_candidates("review", [linA, linB], ov, "op")
        # count normalized to 1: one linux node assigned, satisfied, no shortfall, ONE slot.
        assert len(da.assigned) == 1 and da.satisfied and da.shortfall == (), (bad, da)
        assert len(slots) == 1 and slots[0][0] == "linux", (bad, slots)
        # The invariant the divergence broke: a duty reported satisfied must be dispatchable.
        assert not (da.satisfied and len(slots) == 0), (bad, da, slots)


def test_identity_load_tolerates_malformed_duties_enabled(tmp_path, monkeypatch):
    """A corrupt/hand-edited node.json whose dutiesEnabled is a non-mapping (null, a
    list, a scalar) must fall back to {} rather than crash identity.load with a
    dict(None) TypeError."""
    import json
    from diplomat_app.mesh import identity
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    for bad in (None, ["review"], 5):
        (tmp_path / "node.json").write_text(json.dumps(
            {"id": "n1", "name": "x", "tier": 3, "tokens": "auto",
             "dutiesEnabled": bad, "strengthAuto": False}))
        assert identity.load().duties_enabled == {}   # must not raise
    (tmp_path / "node.json").write_text(json.dumps(
        {"id": "n1", "name": "x", "tier": 3, "tokens": "auto",
         "dutiesEnabled": {"review": False}, "strengthAuto": False}))
    assert identity.load().duties_enabled == {"review": False}  # valid mapping preserved


def test_overrides_signature_required_from_a_known_editor(tmp_path, monkeypatch):
    if not crypto.AVAILABLE:
        return
    node = _fresh_node(tmp_path, monkeypatch)
    k = _mk_key()
    ed = _signed_advert(k, "editor", seq=1)
    node._learn_node(NodeInfo.from_dict(ed), "1.2.3.4", None, raw=ed)  # pin editor's key
    ov = {"rev": 5, "updatedBy": "editor",
          "duties": {"review": {"strategy": "strongest-first", "tokenAware": True, "spread": []}}}
    assert not node._overrides_authentic(ov)  # unsigned, but editor key known → required
    signed = dict(ov)
    signed["sig"] = k.sign(protocol.overrides_signing_bytes(ov))
    assert node._overrides_authentic(signed)  # correctly signed by the editor
    tampered = dict(signed)
    tampered["rev"] = 99  # a relay bumps rev to win LWW → signature no longer covers it
    assert not node._overrides_authentic(tampered)
    # The default (rev 0) override needs no signature.
    assert node._overrides_authentic({"rev": 0, "updatedBy": "", "duties": {}})
    # A real (rev>0) edit from an UNKNOWN editor is unauthenticatable → rejected, so a
    # forged huge-rev override under an unknown name can't mask real signed edits.
    assert not node._overrides_authentic({"rev": 3, "updatedBy": "stranger", "duties": {}})
    assert not node._overrides_authentic(
        {"rev": 2 ** 62, "updatedBy": "stranger", "duties": {}})


# MARK: - work claims (leaderless origination leases)


_WK = "review:github.com/acme/app#123@abc123"


def _signed_claim(key, work_key, node_id, seq=0, state="active", epoch=1.0) -> dict:
    rec = protocol.ClaimRecord(work_key=work_key, node=node_id, pubkey=key.public_b64,
                               epoch=epoch, seq=seq, state=state)
    d = rec.to_dict()
    d["sig"] = key.sign(protocol.claim_signing_bytes(d))
    return d


def _claim_node(tmp_path, monkeypatch, local_id="m-local"):
    """A fresh node whose id is fixed to `local_id`, so a test controls whether a
    peer sorts below (wins races) or above it."""
    from dataclasses import replace
    node = _fresh_node(tmp_path, monkeypatch)
    node.local = replace(node.local, id=local_id)
    node.epoch = 1.0
    return node


def _link_verified_claimant(node, node_id, key):
    """Learn `node_id` as a LIVE (linked), pinned, VERIFIED peer holding `key` — but
    NOT trusted. Whether it then classifies personal or foreign is up to the
    allowlist / default trust level (foreign unless promoted)."""
    node._learn_node(_peer_info(node_id, 1, pubkey=key.public_b64), "1.2.3.4",
                     _FakeWriter(), raw=_signed_advert(key, node_id))
    node.peers[node_id].verified_fp = key.fingerprint
    return node.peers[node_id]


def _link_personal_claimant(node, node_id, key):
    """A verified claimant that is also TRUSTED — a valid authoritative *personal*
    claimant. Its proven fingerprint is explicitly promoted into the local allowlist,
    since the default trust level is now foreign (a peer is not personal until the
    operator marks it so)."""
    peer = _link_verified_claimant(node, node_id, key)
    node._trusted[key.fingerprint] = node_id  # explicit promotion → personal
    return peer


def test_work_claim_signature_rejects_forgery_and_tamper(tmp_path, monkeypatch):
    if not crypto.AVAILABLE:
        return
    node = _claim_node(tmp_path, monkeypatch)
    k = _mk_key()
    assert node._claim_authentic(_signed_claim(k, _WK, "aaa"))          # valid self-signature
    # A keyless claim has nothing to verify — accepted (stays non-authoritative).
    assert node._claim_authentic({"workKey": _WK, "node": "aaa", "state": "active"})
    unsigned = _signed_claim(k, _WK, "aaa"); del unsigned["sig"]
    assert not node._claim_authentic(unsigned)                          # keyed but unsigned
    tampered = _signed_claim(k, _WK, "aaa"); tampered["workKey"] = _WK + "EVIL"
    assert not node._claim_authentic(tampered)                          # tampered after signing
    forged = _signed_claim(k, _WK, "aaa"); forged["pubkey"] = _mk_key().public_b64
    assert not node._claim_authentic(forged)                            # signed by a different key


def test_claim_owner_is_lowest_id_live_personal_claimant(tmp_path, monkeypatch):
    if not crypto.AVAILABLE:
        return
    node = _claim_node(tmp_path, monkeypatch, local_id="m-local")
    lo, hi = _mk_key(), _mk_key()
    _link_personal_claimant(node, "aaa-low", lo)
    _link_personal_claimant(node, "zzz-high", hi)
    node._on_work_claim(protocol.work_claim(_signed_claim(hi, _WK, "zzz-high")))
    assert node._claim_holder(_WK) == "zzz-high"                        # only claimant so far
    node._on_work_claim(protocol.work_claim(_signed_claim(lo, _WK, "aaa-low")))
    assert node._claim_holder(_WK) == "aaa-low"                         # lowest id wins


def test_claim_gate_stands_down_below_and_proceeds_above(tmp_path, monkeypatch):
    if not crypto.AVAILABLE:
        return
    # A lower-id peer already owns it → we (higher id) stand down.
    node = _claim_node(tmp_path, monkeypatch, local_id="z-local")
    lo = _mk_key(); _link_personal_claimant(node, "a-peer", lo)
    node._on_work_claim(protocol.work_claim(_signed_claim(lo, _WK, "a-peer")))
    assert node.claim(_WK) is False
    assert node._own_claim(_WK) is None                                 # never even claimed

    # A higher-id peer owns it → we (lower id) take over and proceed.
    node2 = _claim_node(tmp_path, monkeypatch, local_id="a-local")
    hi = _mk_key(); _link_personal_claimant(node2, "z-peer", hi)
    node2._on_work_claim(protocol.work_claim(_signed_claim(hi, _WK, "z-peer")))
    assert node2.claim(_WK) is True
    assert node2._claim_holder(_WK) == "a-local"


def test_claim_yields_to_lower_id_on_race(tmp_path, monkeypatch):
    if not crypto.AVAILABLE:
        return
    node = _claim_node(tmp_path, monkeypatch, local_id="z-local")
    lost = []
    node.on_claim_lost = lambda wk: lost.append(wk)
    assert node.claim(_WK) is True                                      # we announce first
    assert node._claim_holder(_WK) == "z-local"
    lo = _mk_key(); _link_personal_claimant(node, "a-peer", lo)
    node._on_work_claim(protocol.work_claim(_signed_claim(lo, _WK, "a-peer")))
    assert node._own_claim(_WK).state == "released"                     # we withdrew
    assert node._claim_holder(_WK) == "a-peer"                          # they own it now
    assert lost == [_WK]                                                # loss hook fired


def test_dead_claimant_lease_lapses_and_frees_work(tmp_path, monkeypatch):
    if not crypto.AVAILABLE:
        return
    import time as _time
    node = _claim_node(tmp_path, monkeypatch, local_id="z-local")
    lo = _mk_key(); peer = _link_personal_claimant(node, "a-peer", lo)
    node._on_work_claim(protocol.work_claim(_signed_claim(lo, _WK, "a-peer")))
    assert node._claim_holder(_WK) == "a-peer"
    peer.last_seen = _time.monotonic() - 999                           # link times out → down
    assert node._claim_holder(_WK) is None                             # dead lease lapses
    assert node.claim(_WK) is True                                     # we take the freed key


def test_foreign_claimant_cannot_suppress_work(tmp_path, monkeypatch):
    if not crypto.AVAILABLE:
        return
    node = _claim_node(tmp_path, monkeypatch, local_id="z-local")
    node._trusted = {"some-other-fingerprint": "x"}                     # non-empty allowlist
    lo = _mk_key(); _link_verified_claimant(node, "a-peer", lo)         # verified but not listed
    assert node._peer_trust(node.peers["a-peer"]) == "foreign"          # unlisted → foreign
    node._on_work_claim(protocol.work_claim(_signed_claim(lo, _WK, "a-peer")))
    assert node._claim_holder(_WK) is None                             # foreign never owns
    assert node.claim(_WK) is True                                     # anti-starvation guard


def test_work_claim_cannot_hijack_a_pinned_node_id(tmp_path, monkeypatch):
    if not crypto.AVAILABLE:
        return
    node = _claim_node(tmp_path, monkeypatch, local_id="z-local")
    k = _mk_key(); _link_personal_claimant(node, "a-peer", k)          # pin a-peer → key k
    # A claim for a-peer self-signed by a DIFFERENT key (valid self-signature) must
    # be dropped by the pin, exactly like an advert id-hijack.
    hijack = _signed_claim(_mk_key(), _WK, "a-peer")
    assert node._claim_authentic(hijack)                              # self-consistent sig
    node._on_work_claim(protocol.work_claim(hijack))
    assert node._claim_holder(_WK) is None                            # pin rejected it


def test_claim_book_is_bounded(tmp_path, monkeypatch):
    if not crypto.AVAILABLE:
        return
    from diplomat_app.mesh import node as node_mod
    node = _claim_node(tmp_path, monkeypatch)
    k = _mk_key(); _link_personal_claimant(node, "a-peer", k)
    for i in range(node_mod._MAX_CLAIMS + 25):                        # a flood of spoofed keys
        node._on_work_claim(protocol.work_claim(
            _signed_claim(k, f"wk-{i:05d}", "a-peer")))
    total = sum(len(b) for b in node._claims.values())
    assert total <= node_mod._MAX_CLAIMS                              # capped, not unbounded


def test_release_frees_key_and_forget_drops_reaped_leases(tmp_path, monkeypatch):
    if not crypto.AVAILABLE:
        return
    node = _claim_node(tmp_path, monkeypatch, local_id="m-local")
    assert node.claim(_WK) is True and node._claim_holder(_WK) == "m-local"
    assert node.claim(_WK) is True                                    # re-claim is idempotent
    node.release(_WK)
    assert node._claim_holder(_WK) is None                            # released → unowned
    # A reaped claimant's leases are forgotten wholesale (on a distinct key, so our
    # own lingering released record for _WK doesn't confuse the assertion).
    wk2 = "audit:github.com/acme/app#9@def456"
    k = _mk_key(); _link_personal_claimant(node, "a-peer", k)
    node._on_work_claim(protocol.work_claim(_signed_claim(k, wk2, "a-peer")))
    assert node._claim_holder(wk2) == "a-peer"
    node._forget_claims("a-peer")
    assert node._claim_holder(wk2) is None
    assert all("a-peer" not in book for book in node._claims.values())


def test_keyless_claim_under_a_keyed_peers_id_cannot_suppress(tmp_path, monkeypatch):
    """A third party mustn't forge a lease under a trusted peer's id by omitting the
    key: authority requires the claim be SIGNED by the peer it names, not merely to
    bear that peer's (trusted) name. A keyless claim naming a keyed peer is dropped
    at the pin and, even if it slipped through, is not authoritative."""
    if not crypto.AVAILABLE:
        return
    node = _claim_node(tmp_path, monkeypatch, local_id="z-local")
    k = _mk_key(); _link_personal_claimant(node, "a-peer", k)          # a-peer's key pinned
    # A forged KEYLESS claim naming the lower-id personal peer (no pubkey, no sig).
    forged = {"workKey": _WK, "node": "a-peer", "state": "active", "epoch": 1.0, "seq": 0}
    node._on_work_claim(protocol.work_claim(forged))
    assert node._claim_holder(_WK) is None                            # forgery is powerless
    assert node.claim(_WK) is True                                    # we still originate
    # And a claim that carries a *different* key than the peer's pin is likewise
    # never authoritative even if self-consistently signed (covered structurally
    # by the pin; asserted here at the authority layer for the keyless case).
    assert "a-peer" not in node._claims.get(_WK, {})                  # never even stored


def test_inflated_epoch_forgery_cannot_block_the_real_owner(tmp_path, monkeypatch):
    """The prior-bug analogue: a forged keyless claim with a huge epoch must not
    poison the freshness slot and lock out the true owner's real, signed claim."""
    if not crypto.AVAILABLE:
        return
    node = _claim_node(tmp_path, monkeypatch, local_id="z-local")
    k = _mk_key(); _link_personal_claimant(node, "a-peer", k)
    # Attacker forges a keyless claim for (WK, a-peer) with an enormous epoch.
    node._on_work_claim(protocol.work_claim(
        {"workKey": _WK, "node": "a-peer", "state": "active", "epoch": 1e18, "seq": 10**9}))
    # It was rejected (pin), so the real owner's later signed claim adopts cleanly
    # and wins — the forgery never blocked it. Assert the *real signed* record is
    # what owns (not merely that some a-peer record exists).
    node._on_work_claim(protocol.work_claim(_signed_claim(k, _WK, "a-peer", epoch=1.0)))
    assert node._claim_holder(_WK) == "a-peer"
    assert node._claims[_WK]["a-peer"].pubkey == k.public_b64


def test_coldjoin_forgery_is_purged_when_the_real_key_is_learned(tmp_path, monkeypatch):
    """The cold-join residual: a forged keyless high-epoch claim that arrives BEFORE
    the claimant's advert (nothing to pin against yet) is stored but inert. It must
    not out-fresh the real owner's claim forever — learning the claimant's real key
    purges it, so dedup for that key recovers."""
    if not crypto.AVAILABLE:
        return
    node = _claim_node(tmp_path, monkeypatch, local_id="z-local")
    k = _mk_key()
    # Forgery lands first (a-peer is unknown, so the pin can't reject it): stored.
    node._on_work_claim(protocol.work_claim(
        {"workKey": _WK, "node": "a-peer", "state": "active", "epoch": 1e18, "seq": 10**9}))
    assert node._claims.get(_WK, {}).get("a-peer") is not None       # inert but present
    assert node._claim_holder(_WK) is None                           # unbound → not owner
    # Learning a-peer's real advertisement pins its key and purges the forgery.
    _link_personal_claimant(node, "a-peer", k)
    assert "a-peer" not in node._claims.get(_WK, {})                 # cold-join poison evicted
    # a-peer's real signed claim now stores and owns — the huge forged epoch is gone.
    node._on_work_claim(protocol.work_claim(_signed_claim(k, _WK, "a-peer", epoch=1.0)))
    assert node._claim_holder(_WK) == "a-peer"
    assert node._claims[_WK]["a-peer"].pubkey == k.public_b64


def test_keyless_peer_cannot_suppress_even_under_full_trust(tmp_path, monkeypatch):
    """In the personal-default (full-trust) mode every verified peer is personal, but
    a peer that proved NO key is not a trusted device — its claim isn't bound to any
    key, so it must not own work even in full-trust mode. Otherwise a keyless node
    (which may not even run the work) could starve origination by default."""
    if not crypto.AVAILABLE:
        return
    node = _claim_node(tmp_path, monkeypatch, local_id="z-local")
    node._default_trust = "personal"                                  # full-trust mesh mode
    assert node._trusted == {}                                        # empty allowlist
    # A genuinely keyless linked peer (no pubkey, never verified).
    node._learn_node(_peer_info("a-peer", 1), "1.2.3.4", _FakeWriter())
    assert node.peers["a-peer"].info.pubkey == "" and node.peers["a-peer"].verified_fp is None
    assert node._peer_trust(node.peers["a-peer"]) == "personal"       # full-trust default
    node._on_work_claim(protocol.work_claim(
        {"workKey": _WK, "node": "a-peer", "state": "active", "epoch": 1.0, "seq": 0}))
    assert node._claim_holder(_WK) is None                            # unbound → not authoritative
    assert node.claim(_WK) is True                                    # we still originate


# MARK: - foreign zero-trust execution (confined compute + response-back)


_RESULT = {"ok": True, "duty": "review", "output": "LGTM — ship it", "error": ""}


class _RecWriter:
    """A StreamWriter stand-in that records every message written to it, so a test
    can assert which job-result/job-ack lines a node emitted on a link."""
    def __init__(self):
        self.sent: list[dict] = []

    def write(self, payload):
        msg = protocol.decode(payload)
        if msg is not None:
            self.sent.append(msg)

    async def drain(self):
        pass

    def close(self, *a):
        pass

    def of(self, t: str) -> list[dict]:
        return [m for m in self.sent if m.get("t") == t]


def _signed_result(key, job_id, node_id, result=None) -> dict:
    """A `job-result` message signed by ``key`` over its canonical {id,node,result}."""
    result = _RESULT if result is None else result
    sig = key.sign(protocol.result_signing_bytes(
        {"id": job_id, "node": node_id, "result": result})) if key else ""
    return protocol.job_result(job_id, node_id, result, sig)


def _link_peer(node, node_id, key):
    """Learn ``node_id`` as a LIVE, verified, pinned peer holding ``key`` on a
    recording writer, and return (peer, writer). Clears the writer's link-setup
    chatter so a test sees only what it triggers afterwards."""
    w = _RecWriter()
    node._learn_node(_peer_info(node_id, 1, pubkey=key.public_b64), "1.2.3.4",
                     w, raw=_signed_advert(key, node_id))
    node.peers[node_id].verified_fp = key.fingerprint
    w.sent.clear()
    return node.peers[node_id], w


def _job(duty="review", job_id="j1") -> protocol.Job:
    return protocol.Job(id=job_id, duty=duty, prompt="do it",
                        requested_by="somebody", requested_at=1.0)


def test_result_signature_binds_to_executor(tmp_path, monkeypatch):
    """A job-result is authentic only when a keyed executor signed it over its exact
    {id,node,result}; keyless is accepted (still link-gated), keyed-but-forged is not."""
    if not crypto.AVAILABLE:
        return
    node = _fresh_node(tmp_path, monkeypatch)
    k = _mk_key()
    peer, _w = _link_peer(node, "bob", k)
    assert node._result_authentic(_signed_result(k, "j1", "bob"), peer)      # valid
    unsigned = _signed_result(k, "j1", "bob"); del unsigned["sig"]
    assert not node._result_authentic(unsigned, peer)                        # keyed, unsigned
    tampered = _signed_result(k, "j1", "bob")
    tampered["result"] = {**_RESULT, "output": "rm -rf /"}                    # payload swapped
    assert not node._result_authentic(tampered, peer)
    forged = _signed_result(_mk_key(), "j1", "bob")                          # someone else's sig
    assert not node._result_authentic(forged, peer)
    # A keyless executor (peer advertises no pubkey) has nothing to verify → accepted.
    node._learn_node(_peer_info("carol", 1), "1.2.3.4", _FakeWriter())
    assert node._result_authentic(
        {"id": "j2", "node": "carol", "result": _RESULT}, node.peers["carol"])


def test_admit_confines_foreign_only_with_a_runner(tmp_path, monkeypatch):
    """The mode a request runs in: personal → run on host; foreign → confined iff a
    confinement runner is configured, else declined; duty/token refusals win first."""
    node = _fresh_node(tmp_path, monkeypatch)
    monkeypatch.delenv("DIPLOMAT_MESH_FOREIGN_SPAWN", raising=False)
    assert node._admit(_job(), "personal") == ("run", "")
    # Foreign with no runner → the safe v1 decline.
    mode, reason = node._admit(_job(), "foreign")
    assert mode == "decline" and "foreign device" in reason
    # Foreign with a runner configured → confined, response-only.
    monkeypatch.setenv("DIPLOMAT_MESH_FOREIGN_SPAWN", "sandbox {prompt_file} {result_file}")
    assert node._admit(_job(), "foreign") == ("confined", "")
    # Duty/token refusals apply regardless of trust and take precedence.
    node.local = _dc_replace(node.local, duties_enabled={"review": False})
    assert node._admit(_job(), "foreign")[0] == "decline"
    node.local = _dc_replace(node.local, duties_enabled={})
    monkeypatch.setattr(node, "current_tokens", lambda: "out")
    assert node._admit(_job(), "foreign") == ("decline", "out of tokens")


def test_run_confined_needs_a_requester_link(tmp_path, monkeypatch):
    """Response-only is meaningless with nobody to answer: a confined run without a
    verified requester fails outright rather than running a stranger's code for no one."""
    node = _fresh_node(tmp_path, monkeypatch)
    monkeypatch.setenv("DIPLOMAT_MESH_FOREIGN_SPAWN", "sandbox {prompt_file} {result_file}")
    assert node._run_confined(_job(), requester_id="")[0] == "failed"


def test_confined_executor_dedups_and_claims_the_work_key(tmp_path, monkeypatch):
    """The confined (foreign) executor must mint the work-claim and dedup by key, just
    like _spawn_local (docs/szpontnet/12: "the executor — the node that spawns the
    agent — mints the active claim, holds it for that agent's lifetime, and releases it
    when the agent finishes"). Without it, an originator's same-poll double dispatch of
    ONE work_key spawned TWO confined agents → two results → a DUPLICATE social action
    under the originator's identity, and no claim ever suppressed a re-poll.

    A second dispatch of a key already running here must (a) spawn no second sandbox,
    (b) report ``no_result`` so [_take_job] marks it ``direct`` — the originator neither
    acts twice nor arms a completion deadline over a result the original job carries —
    and (c) the claim must be minted for the run and released when it finishes."""
    import asyncio
    if not crypto.AVAILABLE:
        return
    node = _fresh_node(tmp_path, monkeypatch)
    node.proto["heartbeatIntervalSecs"] = 0.05
    node.proto["foreignJobTimeoutSecs"] = 5.0
    calls: list[str] = []

    def fake_spawn_confined(prompt, result_file):
        calls.append(result_file)
        with open(result_file, "w") as f:
            f.write("REVIEW-BY-EXECUTOR")  # sandbox artifact, written immediately

    monkeypatch.setattr(spawnjob, "spawn_confined", fake_spawn_confined)
    _link_peer(node, "alice", _mk_key())  # the (foreign) requester the result routes to
    wk = "review:github.com/acme/app#7@abc"

    def j(job_id):
        return protocol.Job(id=job_id, duty="review", prompt="do it",
                            requested_by="alice", requested_at=1.0, work_key=wk)

    async def go():
        r1 = node._run_confined(j("job-1"), "alice")
        r2 = node._run_confined(j("job-2"), "alice")   # same-poll re-dispatch of wk
        assert len(calls) == 1                          # (a) exactly one sandbox spawned
        assert r1 == ("spawned", "", False)             # fresh run owes a job-result
        assert r2 == ("spawned", "", True)              # (b) dedup owes none → direct
        own = node._own_claim(wk)
        assert own is not None and own.active           # (c) claim held for the agent
        assert wk in node._agents
        # Drive the confined watcher to completion; the claim is then released so the
        # work is ownable again (re-run if unresolved, failover on node death).
        for _ in range(200):
            await asyncio.sleep(0.05)
            cur = node._own_claim(wk)
            if cur is None or not cur.active:
                break
        cur = node._own_claim(wk)
        assert cur is None or not cur.active
        assert wk not in node._agents

    asyncio.run(go())


def test_emit_result_retries_until_acked_by_the_owed_node(tmp_path, monkeypatch):
    """An executor's job-result is re-sent on demand until the node it's owed to acks
    it; an ack from any OTHER node is ignored, and a passed deadline drops it."""
    if not crypto.AVAILABLE:
        return
    import time as _time
    node = _fresh_node(tmp_path, monkeypatch)
    alice, aw = _link_peer(node, "alice", _mk_key())
    other, ow = _link_peer(node, "mallory", _mk_key())
    node._emit_result("j1", "alice", _RESULT)
    assert len(aw.of("job-result")) == 1 and "j1" in node._pending_results
    # A retry re-sends on the owed link once the retry time is reached.
    node._pending_results["j1"].next_retry = _time.monotonic() - 1
    node._retry_pending_results()
    assert len(aw.of("job-result")) == 2
    # An ack from the WRONG node doesn't clear it.
    node._on_job_ack(protocol.job_ack("j1", "mallory"), ow)
    assert "j1" in node._pending_results
    # The ack from the owed node clears it → no more retries.
    node._on_job_ack(protocol.job_ack("j1", "alice"), aw)
    assert "j1" not in node._pending_results
    # A pending whose deadline passed stops retrying but becomes a TOMBSTONE —
    # kept so a later `job-reminder` from the requester can revive its delivery
    # (accountability) instead of finding nothing and banning us.
    node._emit_result("j2", "alice", _RESULT)
    node._pending_results["j2"].deadline = _time.monotonic() - 1
    node._retry_pending_results()
    pending = node._pending_results["j2"]
    assert pending.gave_up
    sent_before = len(aw.of("job-result"))
    node._retry_pending_results()
    assert len(aw.of("job-result")) == sent_before  # no more retries on the tick
    # A reminder from the owed requester revives the delivery…
    node._on_job_reminder({"t": "job-reminder", "id": "j2", "node": "alice"}, aw)
    assert not node._pending_results["j2"].gave_up
    assert len(aw.of("job-result")) == sent_before + 1
    # …and the tombstone is finally reaped once even the requester's
    # accountability window (deadline + grace) could no longer ask about it.
    node._pending_results["j2"].gave_up = True
    node._pending_results["j2"].created = _time.monotonic() - 10**9
    node._reap_foreign()
    assert "j2" not in node._pending_results


def test_originator_acks_and_acts_exactly_once(tmp_path, monkeypatch):
    """On a correlated, authentic result the originator acks and performs the social
    action once; a duplicate (executor retried) is re-acked but never acted on twice."""
    if not crypto.AVAILABLE:
        return
    acted = []
    monkeypatch.setattr(spawnjob, "run_result_handler", lambda p: acted.append(p))
    node = _fresh_node(tmp_path, monkeypatch)
    k = _mk_key()
    bob, bw = _link_peer(node, "bob", k)
    node._register_awaiting("j1", "bob", "review")
    msg = _signed_result(k, "j1", "bob")
    node._on_job_result(msg, bw)
    assert len(bw.of("job-ack")) == 1                       # acked
    assert len(acted) == 1                                  # acted once
    assert "j1" in node._acted_results and "j1" not in node._awaiting_result
    # Duplicate delivery: re-acked (executor is still retrying) but NOT re-acted.
    node._on_job_result(msg, bw)
    assert len(bw.of("job-ack")) == 2 and len(acted) == 1


def test_foreign_result_dropped_from_wrong_link_or_when_forged(tmp_path, monkeypatch):
    """A result is honored only from the exact executor the job went to and only when
    authentic; a wrong-link, forged, or unsolicited result is dropped without an ack."""
    if not crypto.AVAILABLE:
        return
    acted = []
    monkeypatch.setattr(spawnjob, "run_result_handler", lambda p: acted.append(p))
    node = _fresh_node(tmp_path, monkeypatch)
    kb, kc = _mk_key(), _mk_key()
    bob, bw = _link_peer(node, "bob", kb)
    charlie, cw = _link_peer(node, "charlie", kc)
    node._register_awaiting("j1", "bob", "review")
    # Delivered on the wrong peer's link (charlie relaying bob's job id) → dropped.
    node._on_job_result(_signed_result(kb, "j1", "bob"), cw)
    assert not cw.of("job-ack") and not acted and "j1" in node._awaiting_result
    # Forged: right link, but signed by a different key → dropped, not acked.
    node._on_job_result(_signed_result(kc, "j1", "bob"), bw)
    assert not bw.of("job-ack") and not acted
    # Unsolicited: a result for a job we never dispatched → dropped.
    node._on_job_result(_signed_result(kb, "unknown", "bob"), bw)
    assert not bw.of("job-ack") and not acted


def test_failed_dispatch_does_not_act_on_a_late_foreign_result(tmp_path, monkeypatch):
    """A dispatch whose ack is lost (a link flap / timeout — the routine transient the
    redial subsystem exists for) is reported ``failed`` and the caller re-runs the work
    locally. A FOREIGN executor that had already received + spawned before the flap later
    re-delivers its result on the healed link; that late result MUST be ACKed (to stop its
    reliable-delivery retries) but NEVER acted on — else the unit of work is performed
    TWICE under our identity (a duplicate PR review). The executor's foreign claim is
    non-authoritative here so it never suppressed the local re-run, and the job ids differ
    so per-job-id dedup can't catch it: forgetting the failed dispatch is what makes it
    act-once."""
    import asyncio
    if not crypto.AVAILABLE:
        return
    acted = []
    monkeypatch.setattr(spawnjob, "run_result_handler", lambda p: acted.append(p))
    node = _fresh_node(tmp_path, monkeypatch)
    node.proto["dispatchAckTimeoutSecs"] = 0.05  # make the lost-ack timeout fast
    k = _mk_key()
    _ex, ew = _link_peer(node, "exec", k)          # a keyed executor on a recording link

    async def go():
        # Dispatch to exec; the ack never comes back (the future stays unresolved) so
        # wait_for times out → ('failed', ...), exactly the lost-ack transient.
        status, _ = await node._dispatch_to("exec", "review", "review #7",
                                            work_key="review:o/r#7@sha")
        assert status == "failed"
        jid = next(iter(node._acted_results))       # the fix forgot it (ack-don't-act)
        assert jid not in node._awaiting_result
        # The executor's confined agent finishes and re-delivers on the healed link.
        node._on_job_result(_signed_result(k, jid, "exec"), ew)
        assert len(ew.of("job-ack")) >= 1           # acked → its retries stop
        assert acted == []                          # but NOT acted on → work acts once

    asyncio.run(go())


def test_stop_cancels_inflight_confined_watchers(tmp_path, monkeypatch):
    """A confined job's result-watcher task lives in its own set; shutting the node
    down must cancel it (not leak it past the node's life to touch closed links)."""
    import asyncio as _aio

    node = _fresh_node(tmp_path, monkeypatch)

    async def scenario():
        t = _aio.get_running_loop().create_task(_aio.sleep(3600))
        node._result_tasks.add(t)
        t.add_done_callback(node._result_task_done)
        await node.stop()
        try:
            await t
        except _aio.CancelledError:
            pass
        return t

    t = _aio.run(scenario())
    assert t.cancelled() and node._result_tasks == set()


def test_oversized_result_is_truncated_to_fit_the_wire(tmp_path, monkeypatch):
    """A confined artifact larger than the wire line limit is truncated (not dropped
    by the receiver): the emitted job-result stays under MAX_LINE_BYTES, the executor
    still signs the truncated payload validly, and the truncation is flagged."""
    if not crypto.AVAILABLE:
        return
    node = _fresh_node(tmp_path, monkeypatch)
    _peer, w = _link_peer(node, "alice", _mk_key())
    huge = {"ok": True, "duty": "review", "output": "X" * (2 * protocol.MAX_LINE_BYTES),
            "error": ""}
    node._emit_result("j1", "alice", huge)
    sent = w.of("job-result")[-1]
    assert len(protocol.encode(sent)) <= protocol.MAX_LINE_BYTES
    assert "truncated" in sent["result"]["error"]
    # The executor (this node) signed the truncated payload it actually carries.
    assert crypto.verify(node.key.public_b64, protocol.result_signing_bytes(
        {"id": "j1", "node": node.local.id, "result": sent["result"]}), sent["sig"])


def test_confined_env_is_scrubbed_of_host_credentials(monkeypatch):
    """The confined child must never inherit the host's credentials — that is what
    programmatically prevents a foreign agent from acting as us (using `gh`, cloud
    APIs, the mesh secret). Benign config survives; explicit overlays are applied."""
    creds = ("GH_TOKEN", "GITHUB_TOKEN", "ANTHROPIC_API_KEY",
             "AWS_SECRET_ACCESS_KEY", "MY_SSH_KEY", "DIPLOMAT_MESH_SECRET",
             "DIPLOMAT_MESH_API_KEY")
    for k in creds:
        monkeypatch.setenv(k, "SEKRIT")
    monkeypatch.setenv("PATH", "/usr/bin")            # benign — must survive
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", "/m")       # benign — must survive
    env = spawnjob._scrubbed_env(DIPLOMAT_MESH_RESULT_FILE="/r")
    assert env["PATH"] == "/usr/bin" and env["DIPLOMAT_MESH_DIR"] == "/m"
    assert env["DIPLOMAT_MESH_RESULT_FILE"] == "/r"     # overlay applied
    for k in creds:
        assert k not in env, f"credential {k} leaked into the confined child"


def test_fill_substitutes_tokens_and_quotes():
    """The command template substitutes {prompt_file}/{result_file} with shell-quoted
    values, and appends the prompt path when the template omits the token (the
    back-compat shape DIPLOMAT_MESH_SPAWN has always accepted)."""
    assert spawnjob._fill("run {prompt_file} {result_file}",
                          prompt_file="/a b", result_file="/c") == "run '/a b' /c"
    assert spawnjob._fill("run", prompt_file="/p") == "run /p"


def test_foreign_registries_expire_and_are_capped(tmp_path, monkeypatch):
    """The originator's awaited/acted bookkeeping is bounded and self-expiring, so a
    flood or a never-returning executor can't grow memory without bound."""
    import time as _time
    from diplomat_app.mesh import node as node_mod
    node = _fresh_node(tmp_path, monkeypatch)
    monkeypatch.setattr(node_mod, "_MAX_FOREIGN", 3)
    for i in range(5):
        node._register_awaiting(f"j{i}", "bob", "review")
    assert len(node._awaiting_result) == 3 and "j0" not in node._awaiting_result
    # TTL reap: an entry older than the compute+delivery window is dropped.
    node._awaiting_result["j4"].added = _time.monotonic() - 10**9
    node._acted_results["old"] = _time.monotonic() - 10**9
    node._reap_foreign()
    assert "j4" not in node._awaiting_result and "old" not in node._acted_results
    # …but an entry whose accountability clock is ARMED (a foreign executor
    # accepted and owes a result) does NOT expire on the short TTL — it lives
    # until its cycle resolves it (fulfilled / extended / banned).
    node._awaiting_result["j3"].added = _time.monotonic() - 10**9
    node._awaiting_result["j3"].deadline = _time.monotonic() + 60
    node._reap_foreign()
    assert "j3" in node._awaiting_result


# MARK: - redial from memory (peer-address cache + beacon-outage surfacing)


def test_peer_cache_round_trip_and_malformed_entries(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    from diplomat_app.mesh import peercache
    assert peercache.load() == {}  # no file yet
    peercache.save({"bb": ("192.168.1.7", 40878), "cc": ("10.0.0.9", 40880)})
    assert peercache.load() == {"bb": ("192.168.1.7", 40878),
                                "cc": ("10.0.0.9", 40880)}
    # Malformed entries are dropped, never fatal; a corrupt file loads empty.
    peercache.path().write_text(json.dumps({
        "ok": {"addr": "1.2.3.4", "tcpPort": 5},
        "noport": {"addr": "1.2.3.4"},
        "zeroport": {"addr": "1.2.3.4", "tcpPort": 0},
        "badshape": ["1.2.3.4", 5],
    }))
    assert peercache.load() == {"ok": ("1.2.3.4", 5)}
    peercache.path().write_text("not json")
    assert peercache.load() == {}


def test_hello_on_own_link_remembers_dialable_address(tmp_path, monkeypatch):
    from diplomat_app.mesh import peercache
    node = _fresh_node(tmp_path, monkeypatch)
    info = NodeInfo(id="peerR", name="p", platform="linux", tier=3, tokens="ok",
                    epoch=1.0, seq=1, tcp_port=41000)
    # Gossip-learned (no link) must NOT be remembered — a relayed advert's source
    # address is the relay, not the peer, and would poison the redial cache.
    node._learn_node(info, "9.9.9.9", None)
    assert "peerR" not in peercache.load()
    # A hello on the peer's own link is authoritative: source IP + the listen
    # port the hello advertises.
    node._learn_node(info, "192.168.1.30", _FakeWriter())
    assert peercache.load()["peerR"] == ("192.168.1.30", 41000)


def test_peer_cache_is_bounded_and_evicts_coldest(tmp_path, monkeypatch):
    """peers.json must stay bounded like the peer table (_MAX_PEER_CACHE): the cache
    persists an address per distinct AUTHENTICATED id and is never reaped in lockstep
    with self.peers, so a churn of distinct ids — an on-mesh flooder cycling ids across
    reap windows, or ephemeral-id peers (CI runners regenerating node.json each boot) —
    would otherwise grow the file, and the _redial_targets fan-out that iterates it,
    without bound. The most-recently-contacted addresses are kept, the coldest evicted."""
    from diplomat_app.mesh import peercache
    from diplomat_app.mesh.node import _MAX_PEER_CACHE
    node = _fresh_node(tmp_path, monkeypatch)
    ids = [f"zzzz-{i:05d}" for i in range(_MAX_PEER_CACHE + 50)]  # all sort above local id
    for pid in ids:
        node._remember_peer(pid, "10.0.0.5", 5000)
    # In-memory, on disk, and the redial fan-out are all bounded.
    assert len(node._peer_cache) == _MAX_PEER_CACHE
    assert len(peercache.load()) == _MAX_PEER_CACHE
    assert len(node._redial_targets()) == _MAX_PEER_CACHE
    # LRU: the newest _MAX_PEER_CACHE ids survived; the oldest 50 were evicted.
    assert set(node._peer_cache) == set(ids[-_MAX_PEER_CACHE:])
    assert ids[-1] in node._peer_cache and ids[0] not in node._peer_cache
    # A peer we keep hearing from (same address, refreshed each hello) is never the
    # eviction victim: it survives a flood that fully cycles the rest of the cache.
    live = ids[-1]
    for pid in (f"flood-{i:05d}" for i in range(_MAX_PEER_CACHE)):  # 'f' < 'z': fresh churn
        node._remember_peer(live, "10.0.0.5", 5000)   # refresh the live peer's recency
        node._remember_peer(pid, "10.0.0.6", 5000)    # ...while adding a new cold id
    assert live in node._peer_cache
    assert len(node._peer_cache) == _MAX_PEER_CACHE


def test_redial_targets_respect_dial_rule_link_state_and_inflight(tmp_path, monkeypatch):
    from diplomat_app.mesh.node import Peer
    node = _fresh_node(tmp_path, monkeypatch)
    lo, linked, dialing, free = "0", "zz-linked", "zz-dialing", "zz-free"
    # "0" sorts below any hex id and "zz…" above ("z" > "f"), regardless of the
    # random local id this fresh node minted.
    node._peer_cache = {
        lo: ("10.0.0.1", 40878),       # sorts below us — theirs to dial, not ours
        linked: ("10.0.0.2", 40878),   # link is up — nothing to redial
        dialing: ("10.0.0.3", 40878),  # a dial is already in flight
        free: ("10.0.0.4", 40878),     # unlinked, not dialing → the one target
    }
    up = Peer(_peer_info(linked, 1), "10.0.0.2")
    up.writer = _FakeWriter()
    node.peers[linked] = up
    node._dialing.add(dialing)
    assert node._redial_targets() == [(free, "10.0.0.4", 40878)]


def test_dialed_link_drops_a_silent_peer_on_the_hello_timeout(tmp_path, monkeypatch):
    """An OUTBOUND dialed link answers whoever replied to a spoofable beacon and must
    not wait forever for its first hello: a silent/slowloris peer (an attacker answering
    a spoofed-beacon dial and never speaking) would otherwise pin the fd + Task +
    _dialing entry FOREVER — an unbounded fd leak under a flood, since the outbound leg
    never enters self.peers for the heartbeat reaper to reclaim. The wait is bounded like
    the inbound path's first read (_on_tcp_connection's 10s)."""
    import asyncio
    from diplomat_app.mesh import node as node_mod
    monkeypatch.setattr(node_mod, "_LINK_HELLO_TIMEOUT_SECS", 0.2)
    node = _fresh_node(tmp_path, monkeypatch)

    class _SilentReader:
        async def readline(self):
            await asyncio.Event().wait()   # never yields a line

    w = _FakeWriter()

    async def go():
        # Returns (does not hang) once the hello timeout fires; a TimeoutError out of
        # wait_for would mean the link hung past 2s (the bug).
        await asyncio.wait_for(
            node._run_link(_SilentReader(), w, "10.0.0.5", authenticated=False),
            timeout=2.0)
    asyncio.run(go())


def test_beacon_flood_dial_fanout_is_bounded(tmp_path, monkeypatch):
    """An unauthenticated beacon flood (distinct spoofed ids) must not spawn unbounded
    outbound dials: the _MAX_PEERS backstop only counts self.peers, which a silent-hello
    flooder keeps empty, so concurrent in-flight dials are capped directly at
    _MAX_INFLIGHT_DIALS — otherwise every distinct id pins an fd + Task (exhaustion →
    node-disabling DoS)."""
    import asyncio
    from diplomat_app.mesh.node import _MAX_INFLIGHT_DIALS
    node = _fresh_node(tmp_path, monkeypatch)

    async def _hang(*a, **k):
        await asyncio.Event().wait()

    async def go():
        node._dial = lambda *a, **k: _hang()          # stuck (silent-peer) dials
        for i in range(_MAX_INFLIGHT_DIALS + 200):    # "zzzz-…" sorts above the uuid local id
            node._on_beacon({"id": f"zzzz-{i:05d}", "tcpPort": 9999}, "10.0.0.5")
        assert len(node._dial_tasks) == _MAX_INFLIGHT_DIALS
        for t in list(node._dial_tasks):
            t.cancel()
        await asyncio.sleep(0)                         # let the cancellations settle
    asyncio.run(go())


def test_inbound_link_binding_no_peer_is_dropped_on_the_hello_timeout(tmp_path, monkeypatch):
    """An INBOUND link (authenticated=True) whose opening hello binds NO reapable peer must
    still be time-bounded: the heartbeat reaper only reclaims self.peers entries, so an
    unbound link has no other reaper and a silent peer pins its fd + Task + _issued_nonce
    entry FOREVER (a remote fd-exhaustion DoS bypassing the join secret). Two triggers bind
    no peer — a hello carrying OUR OWN id (_on_message short-circuits before _learn_node),
    and a distinct-id hello once the peer table is full (_learn_node refuses the Peer, yet
    _on_message still returns the id) — so the guard keys on the unbound WRITER, not peer_id."""
    import asyncio
    from diplomat_app.mesh import node as node_mod
    from diplomat_app.mesh.node import Peer, _MAX_PEERS
    monkeypatch.setattr(node_mod, "_LINK_HELLO_TIMEOUT_SECS", 0.2)
    node = _fresh_node(tmp_path, monkeypatch)

    class _SilentReader:
        async def readline(self):
            await asyncio.Event().wait()

    async def go():
        # (A) a hello carrying our own id — binds no peer, must still time out (not hang).
        await asyncio.wait_for(
            node._run_link(_SilentReader(), _FakeWriter(), "9.9.9.9", authenticated=True,
                           first={"t": "hello", "node": {"id": node.local.id}, "overrides": {}}),
            timeout=2.0)
        # (B) a distinct-id hello with the peer table already full: _learn_node refuses the
        # Peer but _on_message returns the id, so peer_id binds while the writer stays unbound.
        for i in range(_MAX_PEERS):
            node.peers[f"filler-{i}"] = Peer(_peer_info(f"filler-{i}", 1), "1.1.1.1")
        await asyncio.wait_for(
            node._run_link(_SilentReader(), _FakeWriter(), "9.9.9.9", authenticated=True,
                           first={"t": "hello", "node": {"id": "zzzz-distinct"}, "overrides": {}}),
            timeout=2.0)
    asyncio.run(go())


def test_trickle_slowloris_link_is_reaped_by_the_cumulative_hello_deadline(tmp_path, monkeypatch):
    """Round-19: the pre-hello reap deadline is CUMULATIVE, not per-readline. A trickle-
    slowloris that feeds one decode-rejected/no-op line every < timeout would reset a per-read
    timeout FOREVER, never bind a self.peers entry (so the heartbeat reaper never sees it), and
    never be reaped — an unbounded inbound fd/Task/_issued_nonce leak (inbound has no fan-in
    cap). It must be dropped within the cumulative deadline regardless of the trickle, while a
    link whose hello DOES bind a peer must NOT be reaped (no over-reaping)."""
    import asyncio
    from diplomat_app.mesh import node as node_mod
    monkeypatch.setattr(node_mod, "_LINK_HELLO_TIMEOUT_SECS", 0.3)
    node = _fresh_node(tmp_path, monkeypatch)

    class _TrickleReader:
        def __init__(self): self.n = 0
        async def readline(self):
            await asyncio.sleep(0.05)    # << the 0.3s deadline; would reset a per-read timeout
            self.n += 1
            return b"x\n"                # protocol.decode -> None -> continue; binds nothing

    async def go():
        # (A) An UNBOUND trickle link is reaped within the cumulative deadline, not hung.
        w = _FakeWriter()
        node._issued_nonce[w] = "nonce"                      # as _send_hello populates it
        reader = _TrickleReader()
        await asyncio.wait_for(                              # TimeoutError here = the trickle hung it
            node._run_link(reader, w, "9.9.9.9", authenticated=True,
                           first={"t": "hello", "node": {}, "overrides": {}}),
            timeout=2.0)
        assert reader.n >= 2, "the reader should have trickled several lines before the reap"
        assert w not in node._issued_nonce, "the finally must free the leaked nonce/fd"

        # (B) No over-reaping: a link whose hello BINDS a peer survives well past the deadline.
        w2 = _FakeWriter()
        bound = asyncio.ensure_future(
            node._run_link(_TrickleReader(), w2, "9.9.9.9", authenticated=True,
                           first={"t": "hello", "node": {"id": "peerZ"}, "overrides": {}}))
        await asyncio.sleep(node_mod._LINK_HELLO_TIMEOUT_SECS * 3)
        assert node._peer_by_writer(w2) is not None
        assert not bound.done(), "a bound peer's link was wrongly reaped by the hello deadline"
        bound.cancel()
        try:
            await bound
        except asyncio.CancelledError:
            pass

    asyncio.run(go())


def test_rebuild_udp_sockets_swap_in_fresh_ones(tmp_path, monkeypatch):
    """Recovery from an OS Local Network denial requires NEW sockets (the OS pins
    the verdict at socket creation), so the rebuilders must produce a fresh socket
    and close the old one — never leave the node socketless."""
    import asyncio
    monkeypatch.setenv("DIPLOMAT_MESH_LOOPBACK", "1")
    monkeypatch.setenv("DIPLOMAT_MESH_MCAST_PORT", str(44300 + os.getpid() % 400))
    node = _fresh_node(tmp_path, monkeypatch)
    node._rebuild_udp_send()  # no socket yet — builds the first one
    first = node._udp_send
    node._rebuild_udp_send()
    assert node._udp_send is not first
    assert first.fileno() == -1  # the replaced socket was closed

    async def go():
        node._rebuild_udp_recv()
        first_recv = node._udp_recv
        node._rebuild_udp_recv()
        assert node._udp_recv is not first_recv
        assert first_recv.fileno() == -1
        asyncio.get_running_loop().remove_reader(node._udp_recv)
    asyncio.run(go())
    node._udp_send.close()
    node._udp_recv.close()


def test_beacon_outage_surfaced_once_and_recovery_logged(tmp_path, monkeypatch):
    from diplomat_app import activity
    node = _fresh_node(tmp_path, monkeypatch)

    def beacon_lines() -> list[str]:
        try:
            text = activity.audit_path().read_text()
        except OSError:
            return []
        return [ln for ln in text.splitlines() if "beacon" in ln]

    node._note_beacon_sends(0, OSError(65, "No route to host"))
    node._note_beacon_sends(0, OSError(65, "No route to host"))  # no re-log
    assert node._beacon_blocked
    assert len(beacon_lines()) == 1  # surfaced exactly once, not per tick
    node._note_beacon_sends(1, None)
    node._note_beacon_sends(2, None)  # steady state — no re-log either
    assert not node._beacon_blocked
    assert len(beacon_lines()) == 2  # the outage line + the recovery line


def test_non_object_job_result_is_dropped_not_crashing(tmp_path, monkeypatch):
    """A correlated, authentic job-result whose `result` field is a non-object (a
    string/list/number) must be treated as a non-fulfilling answer and NOT raise
    AttributeError on `.get` — an uncaught AttributeError escapes _run_link's except
    tuple and tears the link (docs/szpontnet/13: a malformed reply is simply dropped)."""
    if not crypto.AVAILABLE:
        return
    acted = []
    monkeypatch.setattr(spawnjob, "run_result_handler", lambda p: acted.append(p))
    node = _fresh_node(tmp_path, monkeypatch)
    k = _mk_key()
    bob, bw = _link_peer(node, "bob", k)
    node._register_awaiting("j1", "bob", "review")
    node._on_job_result(_signed_result(k, "j1", "bob", result="x"), bw)  # must not raise
    assert len(bw.of("job-ack")) == 1                    # still acked (idempotent)
    assert not acted                                     # ok:false path — never acted on
    assert "j1" in node._acted_results and "j1" not in node._awaiting_result
    # The reminded → ban branch also reads `result.get`; a non-object must not crash it.
    banned = []
    monkeypatch.setattr(node, "_ban_executor", lambda *a, **k: banned.append(a))
    node._register_awaiting("j2", "bob", "review")
    node._awaiting_result["j2"].reminded_at = 1.0
    node._on_job_result(_signed_result(k, "j2", "bob", result=[1, 2]), bw)  # must not raise
    assert banned                                        # branch reached, no crash


def test_merge_overrides_tolerates_malformed_rev_and_duties(tmp_path, monkeypatch):
    """A gossiped placement-override with a malformed rev (null/list/non-numeric) or a
    non-mapping duties must be tolerated, never raise an uncaught TypeError/ValueError
    that tears the link (and, on the first-hello path, leaks the issued nonce). Mirrors
    the tolerance of Placement._parse_spread / NodeInfo.from_dict / ClaimRecord.from_dict."""
    node = _fresh_node(tmp_path, monkeypatch)
    assert node._overrides_authentic({"rev": None}) is True   # no crash, default path
    assert node._overrides_authentic({"rev": [1]}) is True
    for raw in ({"rev": None}, {"rev": [1]}, {"rev": 0, "duties": "x"},
                {"duties": 5}, {"rev": "abc"}):
        node._merge_overrides(raw)                            # must not raise
    ov = PlacementOverrides.from_dict({"rev": None, "duties": 5})
    assert ov.rev == 0 and ov.duties == {}                    # garbage → defaults
    ov2 = PlacementOverrides.from_dict({"rev": "12", "duties": {"review": {}}})
    assert ov2.rev == 12 and ov2.duties == {"review": {}}     # a numeric string still parses


def test_stale_agent_sentinel_does_not_release_a_live_claim(tmp_path, monkeypatch):
    """A detached agent that OUTLIVES a node restart writes its exit-sentinel long
    after. Without the incarnation stamp, the fresh node's agent for the SAME work_key
    shared that path (_claim_seq resets to 0 on restart), so the watcher saw the stale
    sentinel on its first poll and released a still-held claim → double-dispatch. The
    sentinel path must be unique per incarnation so a prior incarnation never collides,
    and startup must sweep orphan sentinels."""
    prior = _fresh_node(tmp_path, monkeypatch)
    fresh = _fresh_node(tmp_path, monkeypatch)
    prior.epoch, fresh.epoch = 1_000_000.0, 2_000_000.0      # two node runs
    wk = "review:github.com/o/r#5@sha_abc"
    assert prior._agent_done_path(wk) != fresh._agent_done_path(wk)  # disjoint paths
    orphan = prior._agent_done_path(wk)                      # incarnation-1 leftover
    with open(orphan, "w") as f:
        f.write("0")
    from diplomat_app.mesh import node as node_mod
    monkeypatch.setattr(node_mod.spawnjob, "spawn_job", lambda *a, **k: None)
    job = protocol.Job(id="j1", duty="review", prompt="p", requested_by="me",
                       requested_at=1.0, work_key=wk)
    fresh._spawn_local(job)
    own = fresh._own_claim(wk)
    assert own is not None and own.active                    # claim held after spawn
    assert not os.path.exists(fresh._agents[wk]["done"])     # the stale sentinel isn't ours
    fresh._sweep_stale_sentinels()
    assert not os.path.exists(orphan)                        # orphan cleaned at startup


def test_placement_override_tolerates_non_dict_duty_value(tmp_path, monkeypatch):
    """A gossiped override whose per-duty VALUE is a non-object (junk) must be dropped
    at ingestion and never crash placement_for at assign time — otherwise one
    unauthenticated frame permanently poisons self.overrides and takes the whole
    assignment engine (and, via verbatim relay, the mesh) down. The Round-2 fix guarded
    the duties FIELD type; this guards the per-duty VALUE, dereferenced later."""
    ov = PlacementOverrides.from_dict(
        {"rev": 3, "updatedBy": "z",
         "duties": {"review": "x", "conflicts": {"strategy": "round-robin"}}})
    assert ov.duties == {"conflicts": {"strategy": "round-robin"}}  # junk value dropped
    assert config.placement_for("review", ov).strategy              # default, no crash
    assert config.placement_for("conflicts", ov).strategy == "round-robin"  # valid kept
    # Placement.from_dict itself tolerates a non-dict argument (the assign-time site).
    assert config.Placement.from_dict("x").strategy
    assert config.Placement.from_dict(None).spread == ()


def test_placement_override_drops_duty_carrying_a_non_finite_float():
    """A gossiped override's per-duty dict is kept VERBATIM and re-serialized into the
    shared snapshot, so a signed peer that slips a bare ∞/NaN into ANY key (a non-schema
    key, or nested inside spread) would write the RFC 8259-invalid tokens Infinity/NaN
    into state.json and blank a strict reader's topology mesh-wide — the override-path
    twin of the advert dutiesEnabled/stats guard. The offending duty is dropped at
    ingestion (falls back to its catalog default); clean sibling duties survive."""
    import json
    ov = PlacementOverrides.from_dict(
        {"rev": 4, "updatedBy": "z",
         "duties": {
             "review": {"strategy": "weakest-first", "tokenAware": True, "junk": float("inf")},
             "audit": {"strategy": "round-robin",
                       "spread": [{"platform": "linux", "count": 1, "x": float("nan")}]},
             "conflicts": {"strategy": "round-robin"}}})
    assert set(ov.duties) == {"conflicts"}                       # both poisoned duties dropped
    assert config.placement_for("review", ov).strategy           # default, no crash
    assert config.placement_for("conflicts", ov).strategy == "round-robin"  # clean one kept
    ser = json.dumps({"overrides": ov.to_dict()})                # snapshot serialization
    assert "Infinity" not in ser and "NaN" not in ser
    json.loads(ser, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)))  # strict
    # A wholly-clean override is untouched (the guard only drops poisoned duties).
    clean = PlacementOverrides.from_dict(
        {"rev": 1, "updatedBy": "z",
         "duties": {"review": {"strategy": "weakest-first", "tokenAware": True}}})
    assert set(clean.duties) == {"review"}


def test_emit_claim_stores_own_claim_even_when_book_is_full(tmp_path, monkeypatch):
    """The anti-flood claim cap fences spoofed PEER work_keys; the node's OWN claim is
    authoritative locally and must always store. If _emit_claim silently drops it
    (ignoring _store_claim's cap refusal) _own_claim is None, so the executor lease is
    released on the watcher's first poll and a re-dispatch double-spawns the same work,
    while the gossiped 'active' claim is never withdrawn."""
    from diplomat_app.mesh import node as node_mod
    node = _fresh_node(tmp_path, monkeypatch)
    cap = node_mod._MAX_CLAIMS
    for i in range(cap):
        node._claims[f"k{i}"] = {f"n{i}": protocol.ClaimRecord(work_key=f"k{i}", node=f"n{i}")}
    assert sum(len(b) for b in node._claims.values()) == cap
    wk = "review:github.com/o/r#1@sha"
    node._emit_claim(wk, "active")
    own = node._own_claim(wk)
    assert own is not None and own.active                # stored despite the full book
    # A PEER's new claim is still refused at the cap — the anti-flood is intact.
    assert node._store_claim(
        protocol.ClaimRecord(work_key="peerkey", node="peerX")) is False


def test_banned_and_trust_load_tolerate_scalar_values(tmp_path, monkeypatch):
    """A corrupt/hand-edited banned.json / trusted.json whose "banned"/"trusted" key
    holds a non-iterable scalar (null/int/bool/float) must be treated as empty (the
    module contract), not raise an uncaught TypeError that aborts node startup — the
    `.get(key, [])` default does NOT cover a present-but-scalar value."""
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    banned.banned_path().parent.mkdir(parents=True, exist_ok=True)
    for value in ("null", "5", "true", "3.14"):
        banned.banned_path().write_text('{"banned": %s}' % value)
        assert banned.load() == []
        trust.trusted_path().write_text('{"trusted": %s}' % value)
        assert trust.load() == {}
    # A well-formed list still loads normally.
    banned.banned_path().write_text('{"banned": [{"fingerprint": "fp1", "reason": "x"}]}')
    assert [e["fingerprint"] for e in banned.load()] == ["fp1"]
    trust.trusted_path().write_text('{"trusted": [{"fingerprint": "fp1", "label": "mbp"}]}')
    assert trust.load() == {"fp1": "mbp"}


def test_wire_decoders_tolerate_infinity_from_json(tmp_path, monkeypatch):
    """A JSON numeric literal like 1e999 parses to float('inf'), and int(inf) raises
    OverflowError — an ArithmeticError, NOT a ValueError, so the decoders' except tuples
    missed it. A single hello/node/work-claim/overrides frame carrying it must be
    dropped, not tear the link (and leak the first-hello nonce)."""
    for field in ("tier", "tcpPort", "seq", "v"):
        raw = protocol.decode(
            ('{"t":"node","node":{"id":"x","%s":1e999}}' % field).encode())["node"]
        assert raw[field] == float("inf")
        assert NodeInfo.from_dict(raw) is None            # dropped, not OverflowError
    craw = protocol.decode(
        b'{"t":"work-claim","claim":{"workKey":"w","node":"n","seq":1e999}}')["claim"]
    assert protocol.ClaimRecord.from_dict(craw) is None
    node = _fresh_node(tmp_path, monkeypatch)
    oraw = protocol.decode(b'{"t":"overrides","overrides":{"rev":1e999}}')["overrides"]
    assert PlacementOverrides.from_dict(oraw).rev == 0    # inf rev → default 0
    assert node._overrides_authentic(oraw) is True        # no OverflowError
    node._merge_overrides(oraw)                            # must not raise


def test_prior_incarnation_claim_lapses_after_executor_restart(tmp_path, monkeypatch):
    """A work-claim minted by a peer's PRIOR incarnation must stop being authoritative
    once that peer restarts (re-advertises a higher epoch). The device key survives a
    restart, so the pubkey binding alone would keep a stale lease suppressing work
    (indefinitely for a server-mode executor) — ownership must track liveness via epoch."""
    if not crypto.AVAILABLE:
        return
    from dataclasses import replace as _replace
    node = _claim_node(tmp_path, monkeypatch, local_id="aaa")
    k = _mk_key()
    peer = _link_personal_claimant(node, "xxx", k)
    peer.info = _replace(peer.info, epoch=2.0)             # X's CURRENT incarnation E2
    current = protocol.ClaimRecord(work_key="wk", node="xxx", pubkey=k.public_b64,
                                   epoch=2.0, seq=0, state="active")
    assert node._claim_authoritative("xxx", current) is True   # current lease holds
    prior = protocol.ClaimRecord(work_key="wk", node="xxx", pubkey=k.public_b64,
                                 epoch=1.0, seq=0, state="active")
    assert node._claim_authoritative("xxx", prior) is False    # E1 < E2 → lapsed


def test_unsigned_rev0_override_with_duties_is_rejected(tmp_path, monkeypatch):
    """A rev-0 override is the unsigned DEFAULT and must be EMPTY. A rev-0 override that
    carries actual duties is a forgery skipping the signature scheme — on the open mesh a
    foreign peer could otherwise push arbitrary mesh-wide placement (it win_overs the
    default via the updatedBy tie-break). It must be rejected; the empty default stays."""
    node = _fresh_node(tmp_path, monkeypatch)
    assert node._overrides_authentic({"rev": 0, "updatedBy": "", "duties": {}}) is True
    assert node._overrides_authentic({"rev": 0}) is True         # empty default, unsigned OK
    forged = {"rev": 0, "updatedBy": "z",
              "duties": {"review": {"strategy": "strongest-first"}}}
    assert node._overrides_authentic(forged) is False            # rev-0 + duties → rejected
    before = node.overrides
    node._merge_overrides(forged)
    assert node.overrides is before                              # dropped; default kept


def test_all_numeric_coercions_tolerate_overflow(tmp_path, monkeypatch):
    """The systemic OverflowError class: float(bigint) and int(inf) both raise
    OverflowError (an ArithmeticError, NOT a ValueError), so every numeric coercion
    of wire/config input that guarded only (Type/Value)Error let a single crafted
    number crash it. A JSON integer literal parses to a Python bigint (float() of it
    overflows); a JSON 1e999 literal parses to inf (int() of it overflows). Each of
    these real decoders must swallow both, never raise."""
    from diplomat_app.mesh import usage
    _fresh_node(tmp_path, monkeypatch)  # sets DIPLOMAT_MESH_DIR for banned.load()
    huge = 10 ** 400          # float(huge) -> OverflowError
    inf = float("inf")        # int(inf)   -> OverflowError

    # int(inf) sites
    assert Placement._parse_spread([{"platform": "ios", "count": inf}]) == (("ios", 1),)
    assert identity._clamped_tier(inf) == config.tier_bounds()[2]  # -> default

    # float(bigint) sites
    assert protocol._opt_frac(huge) is None
    surplus_info = NodeInfo(id="x", name="x", platform="linux", tier=3, tokens="ok",
                            epoch=1.0, seq=1,
                            stats={"surplus": huge, "quotaLeft": 1.0, "usageAvg": 0.0})
    # surplus() floats stats["surplus"]; a bigint there raises OverflowError, which it
    # must swallow to NEUTRAL (main's except caught only KeyError/TypeError/ValueError),
    # or a hostile advert crashes surplus-first dispatch ranking.
    assert surplus_info.surplus() == protocol.NEUTRAL_SURPLUS     # float(bigint) swallowed, not raised
    assert protocol.Job.from_dict({"id": "j", "duty": "d", "requestedAt": huge}) is None
    st = stats._default(0.0)
    for attr in ("quotaLeft", "usageAvg", "usage"):
        stats.apply_stat_attrs(st, {attr: huge}, now=0.0)          # must not raise
    assert usage._token_cost({"input_tokens": huge, "output_tokens": 1}) == 1.0

    import json as _json
    banned.banned_path().write_text(
        _json.dumps({"banned": [{"fingerprint": "fp", "node": "n", "bannedAt": huge}]}),
        encoding="utf-8")
    assert banned.load()[0]["bannedAt"] == 0.0                    # overflow -> default


def test_beacon_with_overflow_epoch_and_out_of_range_port_is_ignored(tmp_path, monkeypatch):
    """A beacon is unauthenticated LAN input. A crafted epoch given as a huge integer
    literal (float(bigint) -> OverflowError) must be dropped like any malformed epoch,
    never raise out of the UDP reader. And a tcpPort outside 1..65535 must be refused
    before we dial — asyncio.open_connection() raises OverflowError (not OSError) on an
    out-of-range port, which would crash the dial task."""
    node = _fresh_node(tmp_path, monkeypatch)
    node._learn_node(_peer_info("peer1", 1), "1.2.3.4", _FakeWriter())  # linked peer
    assert node.peers["peer1"].linked
    node._on_beacon({"t": "beacon", "id": "peer1", "tcpPort": 5, "epoch": 10 ** 400},
                    "9.9.9.9")
    assert node.peers["peer1"].linked and node.peers["peer1"].addr == "1.2.3.4"
    # An out-of-range port never reaches _dial: no dial task is created for it.
    node._on_beacon({"t": "beacon", "id": "peerNew", "tcpPort": 999999, "epoch": 1.0},
                    "5.6.7.8")
    assert not node._dial_tasks    # rejected up front, never dialed


def test_reaped_keyed_executor_ban_binds_to_proven_key(tmp_path, monkeypatch):
    """Accountability ban of a foreign executor that goes silent: if the executor is
    reaped before the deadline fires, _ban_executor has no live peer to read a key
    from. Without pinning the key it proved at accept time, the ban falls back to an
    id-only (fingerprint-less) mark — which any keyed reconnect defeats, since a keyed
    device is judged by its key alone. The pinned executor_fp binds the ban to that key."""
    if not crypto.AVAILABLE:
        return
    node = _fresh_node(tmp_path, monkeypatch)
    node._trusted = {"someone-else": ""}   # boundary on → the executor is foreign
    k = _mk_key()
    node._learn_node(_peer_info("exec1", 1, pubkey=k.public_b64), "1.2.3.4",
                     _FakeWriter(), raw=_signed_advert(k, "exec1"))
    peer = node.peers["exec1"]
    peer.verified_fp = k.fingerprint       # executor PROVED its key on the link

    node._register_awaiting("job1", "exec1", "review", "prompt")
    node._maybe_arm_deadline("job1", "exec1", direct=False)
    aw = node._awaiting_result["job1"]
    assert aw.executor_fp == k.fingerprint  # key pinned while the link was live

    node.peers.pop("exec1", None)           # executor goes silent and is reaped
    node._ban_executor(aw, "job1", "went silent")

    # The ban is keyed to the proven fingerprint, so the reconnecting key is blocked.
    assert node._banned and node._banned[0]["fingerprint"] == k.fingerprint
    assert banned.is_banned(node._banned, k.fingerprint, "exec1")


def test_slot_candidates_bounds_slots_by_live_node_count():
    """A placement override's spread `count` is attacker-influenceable on an open mesh
    (a signed rev>=1 override from any keyed foreign peer is adopted and relayed). One
    job runs per slot and the executor never lands two on one node, so slot_candidates
    must never materialize more slots than nodes-of-platform — an unbounded range(count)
    would OOM the node the instant it dispatches this duty. Its sibling assign_duty is
    already bounded; slot_candidates must match."""
    nodes = [_node("a", "linux"), _node("b", "linux")]
    ov = PlacementOverrides.from_dict({
        "rev": 1, "updatedBy": "x",
        "duties": {"review": {"strategy": "weakest-first", "tokenAware": True,
                              "spread": [{"platform": "linux", "count": 10_000_000}]}},
    })
    slots = assign.slot_candidates("review", nodes, ov, "a")
    assert len(slots) == 2                        # bounded by the 2 linux nodes, not 10M
    # A platform with zero live nodes still surfaces exactly one failover slot (so the
    # dispatch reports one "no eligible node" failure) — not `count` empty slots.
    macos_ov = PlacementOverrides.from_dict({
        "rev": 1, "updatedBy": "x",
        "duties": {"review": {"spread": [{"platform": "macos", "count": 10_000_000}]}},
    })
    slots = assign.slot_candidates("review", nodes, macos_ov, "a")
    assert slots == [("macos", [])]


def test_agent_done_path_distinguishes_keys_sharing_a_long_prefix(tmp_path, monkeypatch):
    """Two distinct work_keys that sanitize to the same 96-char prefix — two PRs of one
    long owner/repo, whose distinguishing `#<n>@<sha>` tail is truncated away — must get
    DISTINCT completion-sentinel paths. A collision lets one agent's exit sentinel be
    misread as the other's: the first watcher releases a still-held claim (re-opening the
    PR to a double-dispatch) and the finished agent's claim is stranded."""
    from diplomat_app import autofix
    node = _fresh_node(tmp_path, monkeypatch)
    node.epoch = 3_000_000.0
    owner = "my-github-organization"
    repo = "observability-platform-shared-infrastructure-components"
    wk1 = autofix.work_key("review", f"https://github.com/{owner}/{repo}/pull/11", "a" * 40)
    wk2 = autofix.work_key("review", f"https://github.com/{owner}/{repo}/pull/22", "b" * 40)
    assert wk1 and wk2 and wk1 != wk2
    # Precondition: the sanitized+truncated prefixes really do collide (so this test
    # exercises the truncation case, not two trivially-different keys).
    pfx = lambda wk: "".join(c if c.isalnum() else "_" for c in wk)[:96]
    assert pfx(wk1) == pfx(wk2)
    # Yet the full sentinel paths stay distinct (a digest of the whole key disambiguates).
    assert node._agent_done_path(wk1) != node._agent_done_path(wk2)


def test_same_key_advert_replay_on_new_link_drops_verification(tmp_path, monkeypatch):
    """A validly-signed advert is REPLAYABLE — its signature covers a static dict with
    no per-connection nonce. If a key-less attacker replays a verified personal peer's
    advert verbatim over a NEW inbound link, _learn_node takes over the peer's writer;
    it MUST drop verified_fp so the new link re-proves possession (its own `auth`), or
    the attacker's link inherits the peer's personal trust with no private key (run-on-
    host / mesh-wide set-attr). The same-key replay is non-fresh, so the Round-1
    key-change clear never fires — the writer takeover itself must clear."""
    if not crypto.AVAILABLE:
        return
    k = _mk_key()
    node = _fresh_node(tmp_path, monkeypatch)
    node._trusted = {k.fingerprint: ""}           # P is operator-trusted
    w_p = _FakeWriter()
    node._learn_node(_peer_info("peerP", 5, pubkey=k.public_b64), "1.1.1.1", w_p)
    peer = node.peers["peerP"]
    peer.verified_fp = k.fingerprint              # P proved its key on W_P this session
    assert node._peer_trust(peer) == "personal"
    w_a = _FakeWriter()                           # a DIFFERENT physical link
    node._learn_node(_peer_info("peerP", 5, pubkey=k.public_b64), "9.9.9.9", w_a)
    assert peer.writer is w_a                      # the replay took over the writer...
    assert peer.verified_fp is None                # ...so the prior link's proof is void
    assert node._peer_trust(peer) != "personal"    # an unproven link is never personal


def test_inbound_surrogate_nonce_hello_does_not_leak_or_crash(tmp_path, monkeypatch):
    """An inbound first-hello is processed INSIDE _run_link's try/finally. A lone-
    surrogate `nonce` makes _auth_challenge's nonce.encode() raise UnicodeEncodeError
    (a ValueError subclass); if that escaped the connection callback, _run_link's
    finally — the only inbound path that pops _issued_nonce[writer] — would be skipped,
    orphaning the issued nonce (an unbounded, unauthenticated remote memory leak). It
    must be caught and the nonce cleaned up, and the reader must not raise out."""
    import asyncio
    import json as _json
    if not crypto.AVAILABLE:
        return
    node = _fresh_node(tmp_path, monkeypatch)

    class _Reader:
        def __init__(self, line): self._chunks = [line, b""]
        async def readline(self): return self._chunks.pop(0)

    class _Writer:
        def get_extra_info(self, k, default=None):
            return ("9.9.9.9", 5) if k == "peername" else default
        def write(self, *a): pass
        async def drain(self): pass
        def close(self, *a): pass

    hello = {"t": "hello",
             "node": {"id": "atk", "name": "e", "platform": "linux", "tier": 3,
                      "tokens": "ok", "tcpPort": 6001, "epoch": 1, "seq": 1, "v": 1},
             "overrides": {}, "nonce": "\ud800"}
    line = (_json.dumps(hello) + "\n").encode()   # literal \ud800 escape reaches the wire
    asyncio.run(node._on_tcp_connection(_Reader(line), _Writer()))  # must not raise
    assert not node._issued_nonce                 # cleaned up — no orphaned nonce leaked


def test_self_released_tombstones_dont_starve_peers_and_are_reaped(tmp_path, monkeypatch):
    """A long-lived node's own 'released' claim tombstones must neither (a) count toward
    the _MAX_CLAIMS cap — else they starve real peer claims, dropping a live peer's lease
    and breaking origination dedup (double-dispatch) — nor (b) accumulate without bound.
    The cap counts only peer records, and a heartbeat reaper drops settled tombstones
    while keeping _claim_seq so a later re-claim still supersedes."""
    import time as _time
    from diplomat_app.mesh import node as node_mod
    node = _fresh_node(tmp_path, monkeypatch)
    node._broadcast = lambda *a, **k: None
    me = node.local.id
    for i in range(node_mod._MAX_CLAIMS):         # fill the book with self tombstones
        node._store_claim(protocol.ClaimRecord(
            work_key=f"self_{i}", node=me, pubkey="", epoch=node.epoch, seq=1,
            state="released"))
    peer_rec = protocol.ClaimRecord(work_key="peer_wk", node="peerB", pubkey="k",
                                    epoch=1.0, seq=1, state="active")
    assert node._store_claim(peer_rec) is True    # peer claim still adopted (not starved)
    assert node._claims["peer_wk"]["peerB"] is peer_rec

    node._emit_claim("live_wk", "active")
    node._emit_claim("live_wk", "released")
    assert node._own_claim("live_wk") is not None
    node._reap_released_claims(_time.monotonic() + node.proto["peerTimeoutSecs"] * 3 + 1)
    assert node._own_claim("live_wk") is None     # settled tombstone reaped
    node._emit_claim("live_wk", "active")
    assert node._own_claim("live_wk").seq >= 2    # _claim_seq kept → re-claim supersedes


def test_identity_load_tolerates_non_dict_node_json(tmp_path, monkeypatch):
    """A valid-JSON but non-object node.json (a bare scalar/array from a hand-edit or
    corruption) must fall back to a minted default identity, not abort node startup with
    a TypeError/AttributeError — the same corrupt-file tolerance trust/banned/peercache
    already have."""
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    for payload in ("5", "3.14", '"hello"', "[1,2]"):
        (tmp_path / "node.json").write_text(payload)
        node = identity.load()                    # must not raise
        assert node.id and node.tier


def test_stats_load_tolerates_non_dict_stats_json(tmp_path, monkeypatch):
    """A truthy valid-JSON non-object stats.json makes raw.get raise AttributeError —
    which the coercion except tuple doesn't catch — aborting startup. stats.load must
    fall back to _default for any non-dict (the `if not raw` short-circuit only caught
    falsy values)."""
    monkeypatch.setenv("DIPLOMAT_MESH_DIR", str(tmp_path))
    for payload in ("5", '"x"', "[1,2]", "true"):
        (tmp_path / "stats.json").write_text(payload)
        st = stats.load(now=1000.0)               # must not raise
        assert st.plan                            # fell back to _default


if __name__ == "__main__":  # dependency-free smoke run
    import inspect
    import tempfile
    import unittest.mock

    failed = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        params = inspect.signature(fn).parameters
        try:
            if params:
                with tempfile.TemporaryDirectory() as td:
                    mp = unittest.mock.patch.dict(os.environ, {"DIPLOMAT_MESH_DIR": td})
                    with mp:
                        class _MP:  # minimal monkeypatch stand-in
                            def __init__(self):
                                self._saved = []  # (obj, name, old) undo stack

                            def setenv(self, k, v):
                                os.environ[k] = v

                            def delenv(self, k, raising=True):
                                os.environ.pop(k, None)

                            def setattr(self, obj, name, value):
                                self._saved.append((obj, name, getattr(obj, name)))
                                setattr(obj, name, value)

                            def undo(self):
                                for obj, name, old in reversed(self._saved):
                                    setattr(obj, name, old)
                        kwargs = {}
                        if "tmp_path" in params:
                            from pathlib import Path
                            kwargs["tmp_path"] = Path(td)
                        if "monkeypatch" in params:
                            kwargs["monkeypatch"] = _MP()
                        try:
                            fn(**kwargs)
                        finally:
                            # Undo attribute patches so one test's stubs never
                            # leak into the next (env undo is `mp`'s job).
                            if "monkeypatch" in kwargs:
                                kwargs["monkeypatch"].undo()
            else:
                fn()
            print(f"ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
    sys.exit(1 if failed else 0)
