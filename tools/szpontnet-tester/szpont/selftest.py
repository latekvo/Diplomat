"""Pure self-tests for the tester's own reference codec + placement oracle.

These need no candidate: they prove the *tester itself* implements the spec's
codec round-trips (V2), freshness/LWW ordering (V3) and placement vectors (V1)
correctly, so that when it judges a candidate it is judging against a trustworthy
oracle. Run with ``--selftest``. If these fail, fix the tester before trusting
any candidate verdict.
"""

from __future__ import annotations

from . import assign, codec
from .codec import NodeInfo
from .model import load_model
from .report import Reporter


def run(rep: Reporter) -> None:
    model = load_model()
    _codec_roundtrips(rep)
    _decode_dropset(rep)
    _freshness(rep)
    _placement_vectors(rep, model)
    _permutation_invariance(rep, model)
    _trust_codec(rep)
    _surplus_first_oracle(rep, model)


def _codec_roundtrips(rep: Reporter) -> None:
    rep.begin_case("S1", "Codec round-trips every message type (V2)")
    info = NodeInfo(id="a" * 32, name="n", platform="linux", tier=4, tokens="ok",
                    tcp_port=40878, epoch=1000.0, seq=3, sees=("b" * 32,),
                    duties_enabled={"audit": False})
    back = NodeInfo.from_dict(info.to_dict())
    rep.check("NodeInfo encode→decode is identity", back == info, "MUST", "04-messages#nodeinfo")
    for name, msg in [
        ("beacon", codec.beacon(info)),
        ("hello", codec.hello(info, {"rev": 0, "updatedBy": "", "duties": {}})),
        ("heartbeat", codec.heartbeat()),
        ("node", codec.node_update(info)),
    ]:
        decoded = codec.decode(codec.encode(msg))
        rep.check(f"{name} encode→decode preserves the object",
                  decoded is not None and decoded.get("t") == msg["t"], "MUST",
                  "03-transport#framing")


def _decode_dropset(rep: Reporter) -> None:
    rep.begin_case("S2", "decode() drops exactly the malformed set (V2)")
    cases = {
        "empty": b"",
        "non-JSON": b"{not json}\n",
        "JSON array (non-object)": b"[1,2,3]\n",
        "object without string t": b'{"x":1}\n',
        "invalid UTF-8": b"\xff\xfe\n",
        "over 512 KiB": b'{"t":"x","p":"' + b"a" * (512 * 1024) + b'"}\n',
    }
    for label, raw in cases.items():
        rep.check(f"drops: {label}", codec.decode(raw) is None, "MUST", "03-transport#framing")
    rep.check("accepts a valid object", codec.decode(b'{"t":"heartbeat"}\n') is not None,
              "MUST", "03-transport#framing")
    rep.check("NodeInfo without id is invalid",
              NodeInfo.from_dict({"name": "x"}) is None, "MUST", "04-messages#nodeinfo")
    rep.check("NodeInfo with non-numeric tier is invalid",
              NodeInfo.from_dict({"id": "x", "tier": "abc"}) is None, "MUST",
              "04-messages#nodeinfo")
    only_id = NodeInfo.from_dict({"id": "x"})
    rep.check("NodeInfo with only id fills defaults",
              only_id is not None and only_id.tokens == "ok" and only_id.tier == 3, "MUST",
              "04-messages#nodeinfo")


def _freshness(rep: Reporter) -> None:
    rep.begin_case("S3", "Freshness ordering: epoch dominates seq (V3)")
    a = NodeInfo(id="x", epoch=200.0, seq=1)
    b = NodeInfo(id="x", epoch=100.0, seq=50)
    rep.check("(epoch=200,seq=1) supersedes (epoch=100,seq=50)", a.newer_than(b), "MUST",
              "04-messages#nodeinfo")
    c = NodeInfo(id="x", epoch=100.0, seq=51)
    rep.check("within an epoch, higher seq wins", c.newer_than(b), "MUST", "04-messages#nodeinfo")


def _placement_vectors(rep: Reporter, model) -> None:
    rep.begin_case("S4", "Placement oracle matches the spec vectors (V1)")
    A = NodeInfo(id="a" * 32, platform="linux", tier=4, tokens="ok")
    B = NodeInfo(id="b" * 32, platform="macos", tier=1, tokens="ok")
    C = NodeInfo(id="c" * 32, platform="macos", tier=4, tokens="ok")
    fleet = [A, B, C]

    def assigned(duty, nodes, overrides=None):
        return tuple(assign.assign_duty(model, duty, nodes, overrides, local_id=A.id)["assigned"])

    rep.check("review → [A]", assigned("review", fleet) == (A.id,), "MUST", "10-conformance")
    rep.check("conflicts → [A]", assigned("conflicts", fleet) == (A.id,), "MUST", "10-conformance")
    rep.check("audit → [A, C]", assigned("audit", fleet) == (A.id, C.id), "MUST", "10-conformance")
    ov = {"rev": 1, "updatedBy": "z", "duties": {
        "review": {"strategy": "strongest-first", "tokenAware": True, "spread": []}}}
    rep.check("review strongest-first → [B]", assigned("review", fleet, ov) == (B.id,), "MUST",
              "10-conformance")
    only_bc = [B, C]
    a = assign.assign_duty(model, "audit", only_bc, local_id=A.id)
    rep.check("audit with {B,C} → [C], linux shortfall",
              tuple(a["assigned"]) == (C.id,) and a["shortfall"] == [{"platform": "linux", "missing": 1}],
              "MUST", "10-conformance")
    A_out = NodeInfo(id=A.id, platform="linux", tier=4, tokens="out")
    rep.check("review, A tokens=out → [C]", assigned("review", [A_out, B, C]) == (C.id,), "MUST",
              "10-conformance")
    A_low = NodeInfo(id=A.id, platform="linux", tier=4, tokens="low")
    rep.check("review, A tokens=low → [C]", assigned("review", [A_low, B, C]) == (C.id,), "MUST",
              "10-conformance")
    empty = assign.assign_duty(model, "review", [], local_id=A.id)
    rep.check("empty fleet → [], unsatisfied", empty["assigned"] == [] and empty["shortfall"],
              "MUST", "10-conformance")


def _permutation_invariance(rep: Reporter, model) -> None:
    rep.begin_case("S5", "Placement is permutation-invariant (V1)")
    A = NodeInfo(id="a" * 32, platform="linux", tier=4, tokens="ok")
    B = NodeInfo(id="b" * 32, platform="macos", tier=1, tokens="ok")
    C = NodeInfo(id="c" * 32, platform="macos", tier=4, tokens="ok")
    import itertools
    base = assign.assign_all(model, [A, B, C], local_id=A.id)
    all_same = all(
        assign.assign_all(model, list(perm), local_id=A.id) == base
        for perm in itertools.permutations([A, B, C]))
    rep.check("shuffling the input order never changes any assignment", all_same, "MUST",
              "06-coordination#determinism-requirements")


def _trust_codec(rep: Reporter) -> None:
    rep.begin_case("S6", "Trust/stats codec: omit-when-empty + domain-separated auth + signed gossip (11)")
    # pubkey/stats are omitted from the wire when empty (byte-compat with core v1).
    plain = NodeInfo(id="a" * 32, name="n", platform="linux", tier=4)
    d = plain.to_dict()
    rep.check("empty pubkey/stats are omitted from the advertisement",
              "pubkey" not in d and "stats" not in d, "MUST",
              "11-trust-and-balancing#conformance")
    # A populated node round-trips both additive fields exactly.
    rich = NodeInfo(id="b" * 32, platform="macos", tier=1, pubkey="AAAA",
                    stats={"plan": "max-20x", "usageAvg": 1.0, "quotaLeft": 20.0})
    back = NodeInfo.from_dict(rich.to_dict())
    rep.check("pubkey/stats round-trip (encode→decode identity)", back == rich, "MUST",
              "11-trust-and-balancing#conformance")
    rd = rich.to_dict()
    rep.check("populated pubkey/stats appear on the wire",
              rd.get("pubkey") == "AAAA" and rd.get("stats", {}).get("plan") == "max-20x",
              "MUST", "11-trust-and-balancing")
    # surplus = quotaLeft − usageAvg; 0.0 when no stats.
    rep.check("surplus() = quotaLeft − usageAvg", abs(rich.surplus() - 19.0) < 1e-9, "MUST",
              "11-trust-and-balancing#stats")
    rep.check("no-stats node has surplus 0 (neutral)", plain.surplus() == 0.0, "MUST",
              "11-trust-and-balancing#stats")
    # A malformed stats blob degrades to empty rather than invalidating the node.
    tolerant = NodeInfo.from_dict({"id": "x", "stats": "not-an-object"})
    rep.check("malformed stats degrades to empty, node still valid",
              tolerant is not None and tolerant.stats == {}, "MUST",
              "09-extensibility#the-compatibility-contract")
    # The auth challenge is domain-separated: tag || nonce, never the bare nonce.
    rep.check("auth_challenge is 'szpontnet-auth-v1:' || nonce",
              codec.auth_challenge("deadbeef") == b"szpontnet-auth-v1:deadbeef", "MUST",
              "11-trust-and-balancing#trust-is-never-derived-from-an-advertisement")
    rep.check("auth builder shape {t:auth, sig}",
              codec.auth("Zg==") == {"t": "auth", "sig": "Zg=="}, "MUST",
              "04-messages#auth")
    # Authenticated gossip (11): the two signing-byte constructions are the
    # domain tag || canonical JSON (sorted keys, compact, `sig` stripped).
    d = {"id": "a" * 32, "pubkey": "AAAA", "tier": 4, "sig": "STALE"}
    rep.check("advert_signing_bytes = tag || canonical(dict w/o sig)",
              codec.advert_signing_bytes(d) ==
              b'szpontnet-nodeinfo-v1:{"id":"' + b"a" * 32 +
              b'","pubkey":"AAAA","tier":4}', "MUST",
              "11-trust-and-balancing#conformance")
    ov = {"rev": 2, "updatedBy": "b" * 32, "duties": {}, "sig": "X"}
    rep.check("overrides_signing_bytes = tag || canonical(dict w/o sig)",
              codec.overrides_signing_bytes(ov) ==
              b'szpontnet-overrides-v1:{"duties":{},"rev":2,"updatedBy":"' +
              b"b" * 32 + b'"}', "MUST", "11-trust-and-balancing#conformance")
    rep.check("canonical form is independent of the sig field (strips it)",
              codec._canonical(d) == codec._canonical({k: v for k, v in d.items()
                                                       if k != "sig"}), "MUST",
              "11-trust-and-balancing#conformance")
    # A populated advert's sig round-trips (dataclass field + omit-when-empty).
    signed = NodeInfo(id="a" * 32, pubkey="AAAA", sig="c2ln")
    rep.check("advert sig round-trips and is omitted when empty",
              NodeInfo.from_dict(signed.to_dict()) == signed
              and "sig" not in NodeInfo(id="a" * 32).to_dict(), "MUST",
              "11-trust-and-balancing#conformance")


def _surplus_first_oracle(rep: Reporter, model) -> None:
    rep.begin_case("S7", "surplus-first ranking oracle (11 load balancing)")
    # Three eligible linux nodes, ranked by DESCENDING surplus. Tier/id would
    # order them A,B,C weakest-first; surplus must reorder to the most-surplus.
    lo = NodeInfo(id="a" * 32, platform="linux", tier=4, tokens="ok",
                  stats={"plan": "pro", "usageAvg": 0.0, "quotaLeft": 1.0})    # surplus 1
    mid = NodeInfo(id="b" * 32, platform="linux", tier=4, tokens="ok",
                   stats={"plan": "max-5x", "usageAvg": 1.0, "quotaLeft": 6.0})  # surplus 5
    hi = NodeInfo(id="c" * 32, platform="linux", tier=4, tokens="ok",
                  stats={"plan": "max-20x", "usageAvg": 2.0, "quotaLeft": 20.0})  # surplus 18
    order = [n.id for n in assign.ranked([lo, mid, hi], "surplus-first", local_id=lo.id)]
    rep.check("surplus-first ranks most-surplus first", order == [hi.id, mid.id, lo.id],
              "MUST", "11-trust-and-balancing#the-load-balancer")
    # Neutral-stats (no stats) degrades exactly to weakest-first (tier then id).
    n1 = NodeInfo(id="a" * 32, platform="linux", tier=2, tokens="ok")
    n2 = NodeInfo(id="b" * 32, platform="linux", tier=4, tokens="ok")
    neutral = [n.id for n in assign.ranked([n1, n2], "surplus-first", local_id=n1.id)]
    weakest = [n.id for n in assign.ranked([n1, n2], "weakest-first", local_id=n1.id)]
    rep.check("all-neutral surplus-first == weakest-first", neutral == weakest, "MUST",
              "11-trust-and-balancing#conformance")
    # A tie in surplus falls back to weakest-first (tier), not id order alone.
    t1 = NodeInfo(id="a" * 32, platform="linux", tier=2, tokens="ok",
                  stats={"plan": "pro", "usageAvg": 0.0, "quotaLeft": 3.0})
    t2 = NodeInfo(id="b" * 32, platform="linux", tier=4, tokens="ok",
                  stats={"plan": "pro", "usageAvg": 0.0, "quotaLeft": 3.0})
    tie = [n.id for n in assign.ranked([t1, t2], "surplus-first", local_id=t1.id)]
    rep.check("surplus tie → weakest-first (higher tier first)", tie == [t2.id, t1.id],
              "MUST", "11-trust-and-balancing#conformance")
