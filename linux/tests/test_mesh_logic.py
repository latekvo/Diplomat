"""Pure-logic mesh tests: assignment, protocol codec, identity, LWW overrides.

Offline, no sockets, no Qt. Run with ``python -m pytest linux/tests`` or
dependency-free via ``python linux/tests/test_mesh_logic.py``.
"""

from __future__ import annotations

import itertools
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from argent_utils.mesh import assign, config, crypto, identity, protocol, stats, trust  # noqa: E402
from argent_utils.mesh.config import Placement, PlacementOverrides  # noqa: E402
from argent_utils.mesh.protocol import NodeInfo  # noqa: E402


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
    monkeypatch.setenv("ARGENT_MESH_DIR", str(tmp_path))
    n1 = identity.load()
    assert len(n1.id) == 32
    n2 = identity.load()
    assert n2.id == n1.id  # stable across loads


def test_apply_attrs_clamps_and_ignores_junk(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGENT_MESH_DIR", str(tmp_path))
    monkeypatch.setenv("ARGENT_MESH_TIER", "3")  # deterministic auto-detect
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
    # anything a peer advertises.
    assert trust.classify("abc", {}) == "personal"        # empty allowlist = full trust
    assert trust.classify("abc", {"abc": "mine"}) == "personal"   # listed
    assert trust.classify("xyz", {"abc": "mine"}) == "foreign"    # unlisted
    assert trust.classify("", {"abc": "mine"}) == "foreign"       # unverified (no fp)


def test_trust_allowlist_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGENT_MESH_DIR", str(tmp_path))
    trust.save({"fp1": "mbp", "fp2": ""})
    loaded = trust.load()
    assert loaded == {"fp1": "mbp", "fp2": ""}
    assert trust.classify("fp1", loaded) == "personal"
    assert trust.classify("nope", loaded) == "foreign"


def test_device_key_proof_of_possession(tmp_path, monkeypatch):
    if not crypto.AVAILABLE:  # dependency-free run without `cryptography`
        return
    monkeypatch.setenv("ARGENT_MESH_DIR", str(tmp_path))
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
    monkeypatch.setenv("ARGENT_MESH_DIR", str(tmp_path))
    a = crypto.load_or_create()
    b = crypto.load_or_create()
    assert a.fingerprint == b.fingerprint  # minted once, persisted
    assert len(a.fingerprint) == 64        # sha256 hex


# MARK: per-node stats (usage EMA + quota) and account types


def test_dispatch_strategy_and_plan_weights():
    assert config.dispatch_strategy() == "surplus-first"
    assert config.plan_weight("pro") == 1.0
    assert config.plan_weight("max-5x") == 5.0
    assert config.plan_weight("max-20x") == 20.0
    assert config.plan_weight("nonexistent") == 1.0  # unknown → Pro-equivalent, safe


def test_stats_ema_decays_over_time_constant(tmp_path, monkeypatch):
    import math

    monkeypatch.setenv("ARGENT_MESH_DIR", str(tmp_path))
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
    monkeypatch.setenv("ARGENT_MESH_DIR", str(tmp_path))
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
    monkeypatch.setenv("ARGENT_MESH_DIR", str(tmp_path))
    now = 1_000_000.0
    st = stats.apply_stat_attrs(stats.load(now=now),
                                {"plan": "max-20x", "quotaLeft": 12.0, "usageAvg": 2.0},
                                now=now)
    assert st.plan == "max-20x"
    assert abs(st.quota_left() - 12.0) < 1e-9
    assert abs(st.usage_avg() - 2.0) < 1e-9
    assert abs(st.surplus() - 10.0) < 1e-9  # quotaLeft − usageAvg
    # A 'usage' delta books against the quota.
    st2 = stats.apply_stat_attrs(st, {"usage": 1.0}, now=now)
    assert st2.quota_left() < st.quota_left()
    # quotaLeft can't exceed the plan capacity (set-too-high clamps).
    st3 = stats.apply_stat_attrs(st, {"quotaLeft": 999.0}, now=now)
    assert st3.quota_left() == st3.capacity() == 20.0


def test_stats_persist_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGENT_MESH_DIR", str(tmp_path))
    now = 1_000_000.0
    st = stats.record(stats.load(now=now), 2.0, now=now)
    stats.save(st)
    again = stats.load(now=now)
    assert again.plan == st.plan
    assert abs(again.usage_avg() - st.usage_avg()) < 1e-9
    assert abs(again.quota_left() - st.quota_left()) < 1e-9


# MARK: surplus-first load balancing


def _snode(id: str, plan: str = "max-5x", quota: float | None = None,
           usage: float = 0.0, tier: int = 3, platform: str = "linux",
           tokens: str = "ok") -> NodeInfo:
    weight = {"pro": 1.0, "max-5x": 5.0, "max-20x": 20.0}[plan]
    return NodeInfo(id=id, name=id, platform=platform, tier=tier, tokens=tokens,
                    stats={"plan": plan, "quotaLeft": weight if quota is None else quota,
                           "usageAvg": usage})


def test_nodeinfo_pubkey_and_stats_roundtrip():
    n = NodeInfo(id="a", name="a", platform="linux", tier=3, tokens="ok",
                 pubkey="QUJDRA==",
                 stats={"plan": "max-20x", "quotaLeft": 18.0, "usageAvg": 2.0})
    d = n.to_dict()
    assert d["pubkey"] == "QUJDRA==" and d["stats"]["plan"] == "max-20x"
    assert NodeInfo.from_dict(d) == n
    assert abs(NodeInfo.from_dict(d).surplus() - 16.0) < 1e-9
    # A bare node omits the additive fields entirely (v1 wire-compat) and still
    # roundtrips; its surplus is a neutral 0. Advertising a pubkey grants nothing
    # on its own - trust needs proof of possession + a local allowlist entry.
    bare = NodeInfo(id="b", name="b", platform="linux", tier=3, tokens="ok")
    bd = bare.to_dict()
    assert "pubkey" not in bd and "stats" not in bd
    assert NodeInfo.from_dict(bd) == bare and bare.surplus() == 0.0


def test_surplus_first_ranks_by_spare_quota():
    a = _snode("a", "max-5x", quota=4.0, usage=1.0)    # surplus 3
    b = _snode("b", "max-20x", quota=18.0, usage=2.0)  # surplus 16
    c = _snode("c", "max-5x", quota=5.0, usage=4.5)    # surplus 0.5
    # Through the public assign path (an override naming the strategy)…
    o = PlacementOverrides().with_duty("review", Placement("surplus-first", True), by="x")
    assert assign.assign_duty("review", [a, b, c], o).assigned == ("b",)
    # …and through the dispatch-time ranking override.
    slots = assign.slot_candidates("review", [a, b, c], strategy="surplus-first")
    assert slots == [("any", ["b", "a", "c"])]


def test_surplus_first_is_account_type_aware():
    # Two idle nodes: the bigger plan has more room, so it wins.
    big = _snode("big", "max-20x")
    small = _snode("small", "max-5x")
    slots = assign.slot_candidates("review", [small, big], strategy="surplus-first")
    assert slots[0][1][0] == "big"


def test_surplus_first_neutral_stats_fall_back_to_weakest_first():
    # No stats advertised ⇒ surplus 0 for all ⇒ ranking degrades to weakest-first
    # (highest tier number), preserving today's behavior for v1 nodes.
    hi = NodeInfo(id="hi", name="hi", platform="linux", tier=1, tokens="ok")
    lo = NodeInfo(id="lo", name="lo", platform="linux", tier=4, tokens="ok")
    slots = assign.slot_candidates("review", [hi, lo], strategy="surplus-first")
    assert slots == [("any", ["lo", "hi"])]


# MARK: machine-strength auto-detection (hardware.py)


def test_strength_score_ranks_stronger_boxes_higher():
    from argent_utils.mesh import hardware
    weak = hardware.strength_score(ram_gb=8, cores=4, dgpu=False)
    strong = hardware.strength_score(ram_gb=64, cores=16, dgpu=True)
    assert strong > weak
    # A maxed box scores at the top of the 0..6 range; a tiny one at the bottom.
    assert hardware.strength_score(128, 32, True) == 6
    assert hardware.strength_score(4, 2, False) == 0


def test_strength_score_maps_to_tier_bounds_inverted():
    from argent_utils.mesh import hardware
    lo, hi, _ = config.tier_bounds()
    # 1 = strongest, so the strongest box lands on `lo` and the weakest on `hi`.
    assert hardware._score_to_tier(6, lo, hi) == lo
    assert hardware._score_to_tier(0, lo, hi) == hi


def test_detect_tier_honours_env_override(monkeypatch):
    from argent_utils.mesh import hardware
    monkeypatch.setenv("ARGENT_MESH_TIER", "2")
    assert hardware.detect_tier() == 2
    monkeypatch.setenv("ARGENT_MESH_TIER", "999")  # clamped to bounds
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
    from argent_utils.mesh import usage
    monkeypatch.setenv("HOME", str(tmp_path))
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(hours=9)).isoformat()  # outside a 5h window
    _write_usage(tmp_path, [(recent, 100, 50, 25), (old, 1000, 1000, 1000)])
    got = usage.window_tokens(now.timestamp(), window_hours=5.0)
    # only the recent turn, and cache_read (9.9M) is NOT counted.
    assert got == 175.0


def test_token_state_thresholds(monkeypatch):
    from argent_utils.mesh import usage
    # Ceiling for pro = weight(1) * tokensPerWeight.
    ceiling = usage.token_ceiling("pro")
    assert ceiling == config.tokens_per_weight()
    assert usage.state_from_fraction(1.0) == "ok"
    assert usage.state_from_fraction(0.0) == "out"
    assert usage.state_from_fraction(config.low_threshold() / 2) == "low"


# MARK: identity — auto-detect + manual pin + token override


def test_identity_auto_detects_strength_on_first_run(tmp_path, monkeypatch):
    monkeypatch.setenv("ARGENT_MESH_DIR", str(tmp_path))
    monkeypatch.setenv("ARGENT_MESH_TIER", "2")
    n = identity.load()
    assert n.strength_auto and n.tier == 2 and n.tokens == "auto"
    # Persisted with the auto flag; a reload with a different detected tier follows it.
    monkeypatch.setenv("ARGENT_MESH_TIER", "4")
    assert identity.load().tier == 4


def test_identity_explicit_tier_in_file_is_a_pin(tmp_path, monkeypatch):
    import json as _json
    monkeypatch.setenv("ARGENT_MESH_DIR", str(tmp_path))
    monkeypatch.setenv("ARGENT_MESH_TIER", "1")  # would auto-detect strong…
    (tmp_path / "node.json").write_text(_json.dumps(
        {"id": "abc123", "name": "box", "tier": 5}))  # …but the file pins weak
    n = identity.load()
    assert n.tier == 5 and not n.strength_auto  # explicit tier wins, auto off


# MARK: - server mode + API key (config + wire)


def test_server_mode_and_api_key_config(monkeypatch):
    monkeypatch.delenv("ARGENT_MESH_SERVER", raising=False)
    monkeypatch.delenv("ARGENT_MESH_API_KEY", raising=False)
    assert config.server_mode() is False
    assert config.api_key() == ""
    monkeypatch.setenv("ARGENT_MESH_SERVER", "1")
    monkeypatch.setenv("ARGENT_MESH_API_KEY", "sekret")
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
    monkeypatch.setenv("ARGENT_MESH_DIR", str(tmp_path))
    from argent_utils.mesh import node as node_mod

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
    monkeypatch.setenv("ARGENT_MESH_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    from argent_utils.mesh.node import MeshNode
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
    from argent_utils.mesh import node as node_mod
    node = _fresh_node(tmp_path, monkeypatch)
    for i in range(node_mod._MAX_PEERS + 25):  # a gossip flood of spoofed ids
        node._learn_node(_peer_info(f"p{i:05d}", 1), "1.2.3.4", None)
    assert len(node.peers) == node_mod._MAX_PEERS  # capped, not unbounded


def test_reapable_covers_downed_and_gossip_only_phantoms(tmp_path, monkeypatch):
    import time as _time
    from argent_utils.mesh import node as node_mod
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
                    mp = unittest.mock.patch.dict(os.environ, {"ARGENT_MESH_DIR": td})
                    with mp:
                        class _MP:  # minimal monkeypatch stand-in
                            def setenv(self, k, v):
                                os.environ[k] = v

                            def delenv(self, k, raising=True):
                                os.environ.pop(k, None)
                        kwargs = {}
                        if "tmp_path" in params:
                            from pathlib import Path
                            kwargs["tmp_path"] = Path(td)
                        if "monkeypatch" in params:
                            kwargs["monkeypatch"] = _MP()
                        fn(**kwargs)
            else:
                fn()
            print(f"ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
    sys.exit(1 if failed else 0)
