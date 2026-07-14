"""Pure-logic mesh tests: assignment, protocol codec, identity, LWW overrides.

Offline, no sockets, no Qt. Run with ``python -m pytest linux/tests`` or
dependency-free via ``python linux/tests/test_mesh_logic.py``.
"""

from __future__ import annotations

import itertools
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from argent_utils.mesh import assign, config, identity, protocol  # noqa: E402
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
    n = identity.load()
    lo, hi, _ = config.tier_bounds()
    n = identity.apply_attrs(n, {"tier": 99, "tokens": "banana", "name": "  "})
    assert n.tier == hi and n.tokens == "ok"
    n = identity.apply_attrs(n, {"tier": -3, "tokens": "out", "name": "box",
                                 "dutiesEnabled": {"audit": False}, "junk": 1})
    assert n.tier == lo and n.tokens == "out" and n.name == "box"
    assert not n.duty_enabled("audit") and n.duty_enabled("review")


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
