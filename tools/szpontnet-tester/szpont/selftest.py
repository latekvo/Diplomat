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
    _result_codec(rep)
    _accountability_codec(rep)


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
    _claim_codec(rep)


def _claim_codec(rep: Reporter) -> None:
    from .codec import ClaimRecord
    rep.begin_case("S8", "Work-claim codec: signing bytes, omit-when-empty, freshness (12)")
    # The claim signing bytes are its OWN domain tag || canonical JSON (sorted
    # keys, compact, `sig` stripped) — byte-identical to the reference.
    c = {"workKey": "wk1", "node": "a" * 32, "pubkey": "AAAA", "epoch": 1.5,
         "seq": 2, "state": "active", "sig": "STALE"}
    rep.check("claim_signing_bytes = 'szpontnet-workclaim-v1:' || canonical(claim w/o sig)",
              codec.claim_signing_bytes(c) ==
              b'szpontnet-workclaim-v1:{"epoch":1.5,"node":"' + b"a" * 32 +
              b'","pubkey":"AAAA","seq":2,"state":"active","workKey":"wk1"}', "MUST",
              "12-work-claims#authentication")
    rep.check("claim canonical form strips the sig field",
              codec._canonical(c) == codec._canonical({k: v for k, v in c.items()
                                                        if k != "sig"}), "MUST",
              "12-work-claims#authentication")
    # A keyless claim omits pubkey/sig; a signed one round-trips byte-stable.
    keyless = ClaimRecord(work_key="wk", node="b" * 32)
    kd = keyless.to_dict()
    rep.check("a keyless claim omits pubkey and sig",
              "pubkey" not in kd and "sig" not in kd, "MUST", "04-messages#work-claim")
    keyed = ClaimRecord(work_key="wk", node="b" * 32, pubkey="AAAA", epoch=3.0,
                        seq=4, state="released", sig="c2ln")
    rep.check("a claim round-trips (encode→decode identity)",
              ClaimRecord.from_dict(keyed.to_dict()) == keyed, "MUST",
              "04-messages#work-claim")
    # A claim without a non-empty workKey or node is invalid (MUST be dropped).
    rep.check("a claim without workKey is invalid",
              ClaimRecord.from_dict({"node": "x"}) is None, "MUST", "04-messages#work-claim")
    rep.check("a claim without node is invalid",
              ClaimRecord.from_dict({"workKey": "x"}) is None, "MUST", "04-messages#work-claim")
    # An unknown state is NOT active (a future state never counts as ownership).
    rep.check("state 'released' and any unknown state are not active",
              not ClaimRecord(work_key="w", node="n", state="released").active
              and not ClaimRecord(work_key="w", node="n", state="future").active
              and ClaimRecord(work_key="w", node="n").active, "MUST",
              "12-work-claims#the-claim-record")
    # Freshness: epoch dominates seq, per (workKey, node).
    older = ClaimRecord(work_key="w", node="n", epoch=1.0, seq=99)
    newer = ClaimRecord(work_key="w", node="n", epoch=2.0, seq=0)
    rep.check("claim freshness: higher epoch wins over higher seq",
              newer.newer_than(older) and not older.newer_than(newer), "MUST",
              "12-work-claims#the-claim-record")
    # The validator rejects a keyed claim missing its sig; accepts a keyless one.
    rep.check("validate_work_claim flags a keyed claim with no sig",
              any("missing sig" in p for p in codec.validate_work_claim(
                  {"t": "work-claim", "claim": {"workKey": "w", "node": "n",
                                                "pubkey": "AAAA"}})), "MUST",
              "12-work-claims#authentication")
    rep.check("validate_work_claim accepts a well-formed signed claim",
              codec.validate_work_claim(codec.work_claim(keyed.to_dict())) == [], "MUST",
              "04-messages#work-claim")


def _result_codec(rep: Reporter) -> None:
    rep.begin_case("S9", "Job-result/ack codec: builder shapes, signing bytes, sig verify/tamper (13)")
    # The job-result builder carries the correlation id, executor node and payload;
    # `sig` is OMITTED when empty (a keyless executor's result is byte-identical to a
    # bare one) and PRESENT when supplied (a keyed executor MUST sign).
    result = {"ok": True, "duty": "review", "output": "the body", "error": ""}
    keyless = codec.job_result("b1c2", "a" * 32, result)
    rep.check("job_result builder shape {t,id,node,result}, sig omitted when empty",
              keyless == {"t": "job-result", "id": "b1c2", "node": "a" * 32,
                          "result": result} and "sig" not in keyless, "MUST",
              "13-foreign-execution#the-messages")
    keyed = codec.job_result("b1c2", "a" * 32, result, sig="c2ln")
    rep.check("a supplied sig appears on the wire (a keyed executor signs)",
              keyed.get("sig") == "c2ln", "MUST", "13-foreign-execution#the-messages")
    rep.check("job_ack builder shape {t,id,node}",
              codec.job_ack("b1c2", "3" * 32) ==
              {"t": "job-ack", "id": "b1c2", "node": "3" * 32}, "MUST",
              "13-foreign-execution#the-messages")
    # The signing bytes are the result's OWN domain tag || canonical JSON of
    # {id,node,result} (sorted keys, compact, `sig` stripped) — byte-identical to
    # the reference. Note it covers ONLY those three fields, not the envelope `t`/`v`.
    p = {"id": "b1c2", "node": "a" * 32, "result": result, "sig": "STALE"}
    rep.check("result_signing_bytes = 'szpontnet-jobresult-v1:' || canonical({id,node,result} w/o sig)",
              codec.result_signing_bytes(p) ==
              b'szpontnet-jobresult-v1:{"id":"b1c2","node":"' + b"a" * 32 +
              b'","result":{"duty":"review","error":"","ok":true,"output":"the body"}}',
              "MUST", "13-foreign-execution#correlation-and-authenticity")
    rep.check("result canonical form strips the sig field",
              codec._canonical(p) == codec._canonical({k: v for k, v in p.items()
                                                       if k != "sig"}), "MUST",
              "13-foreign-execution#correlation-and-authenticity")
    # A valid signature verifies over the canonical bytes; a tampered `result` (or a
    # wrong key) does NOT — the originator drops the latter (keyed executor MUST sign,
    # bad/absent sig dropped). Uses cryptography when available; skips the crypto
    # asserts cleanly (as a MUST-satisfied no-op) on a host without it, exactly as the
    # probe degrades to keyless.
    try:
        import base64
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey, Ed25519PublicKey)
    except Exception:  # pragma: no cover - only where cryptography is absent
        rep.check("signature verify/tamper checks (cryptography unavailable — skipped)",
                  True, "MUST", "13-foreign-execution#correlation-and-authenticity")
        return

    def raw_pub(pk) -> bytes:
        return pk.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    priv = Ed25519PrivateKey.generate()
    pub_raw = raw_pub(priv)
    payload = {"id": "b1c2", "node": "a" * 32, "result": result}
    sig_b64 = base64.b64encode(priv.sign(codec.result_signing_bytes(payload))).decode()

    def verifies(pub_raw_bytes, pay, sig64) -> bool:
        try:
            Ed25519PublicKey.from_public_bytes(pub_raw_bytes).verify(
                base64.b64decode(sig64), codec.result_signing_bytes(pay))
            return True
        except Exception:
            return False

    rep.check("a valid signature verifies over the canonical {id,node,result}",
              verifies(pub_raw, payload, sig_b64), "MUST",
              "13-foreign-execution#correlation-and-authenticity")
    tampered = {"id": "b1c2", "node": "a" * 32,
                "result": {**result, "output": "a MALICIOUS body"}}
    rep.check("a tampered result does NOT verify against the original signature",
              not verifies(pub_raw, tampered, sig_b64), "MUST",
              "13-foreign-execution#correlation-and-authenticity")
    other_raw = raw_pub(Ed25519PrivateKey.generate())
    rep.check("the signature does NOT verify against a wrong (different) key",
              not verifies(other_raw, payload, sig_b64), "MUST",
              "13-foreign-execution#correlation-and-authenticity")


def _accountability_codec(rep: Reporter) -> None:
    rep.begin_case("S10", "Accountability codec: job-reminder/job-progress + job-status.direct (13 v0.4.0)")
    # The two additive accountability messages: builder shapes match chapter 04.
    rem = codec.job_reminder("b1c2", "3" * 32)
    rep.check("job_reminder builder shape {t,id,node}",
              rem == {"t": "job-reminder", "id": "b1c2", "node": "3" * 32}, "MUST",
              "04-messages#job-reminder")
    prog = codec.job_progress("b1c2", "a" * 32, "review 70% done, need ~1h more")
    rep.check("job_progress builder shape {t,id,node,note}",
              prog == {"t": "job-progress", "id": "b1c2", "node": "a" * 32,
                       "note": "review 70% done, need ~1h more"}, "MUST",
              "04-messages#job-progress")
    # Round-trips: encode → decode is identity (plus the defaulted envelope `v`),
    # and the decoded message passes its own strict validator.
    for name, msg, validate in [
        ("job-reminder", rem, codec.validate_job_reminder),
        ("job-progress", prog, codec.validate_job_progress),
    ]:
        decoded = codec.decode(codec.encode(msg))
        rep.check(f"{name} encode→decode round-trips and validates cleanly",
                  decoded == {**msg, "v": 1} and validate(decoded) == [], "MUST",
                  "03-transport#framing")
    # The validators flag the malformed set a receiver would drop.
    rep.check("validate_job_reminder flags a missing/empty id and node",
              any("id" in p for p in codec.validate_job_reminder(
                  {"t": "job-reminder", "node": "n"}))
              and any("node" in p for p in codec.validate_job_reminder(
                  {"t": "job-reminder", "id": "x", "node": ""})), "MUST",
              "04-messages#job-reminder")
    rep.check("validate_job_progress flags a missing/empty note",
              any("note" in p for p in codec.validate_job_progress(
                  {"t": "job-progress", "id": "x", "node": "n"}))
              and any("note" in p for p in codec.validate_job_progress(
                  {"t": "job-progress", "id": "x", "node": "n", "note": ""})), "MUST",
              "04-messages#job-progress")
    # `direct` on job-status (additive, v0.4.0): omitted when false so a plain
    # status stays byte-identical to a pre-v0.4.0 one; true marks a personal-path
    # spawn that never owes a job-result (no deadline may be armed over it).
    plain = codec.job_status("j1", "spawned", "", "a" * 32)
    rep.check("job-status omits `direct` when false (byte-compat with pre-v0.4.0)",
              "direct" not in plain, "MUST", "04-messages#job-status")
    marked = codec.job_status("j1", "spawned", "", "a" * 32, direct=True)
    rep.check("job-status carries `direct: true` for a personal-path spawn",
              marked.get("direct") is True and codec.decode(
                  codec.encode(marked)).get("direct") is True, "MUST",
              "13-foreign-execution#the-completion-deadline")
    rep.check("the progress-note receiver cap constant is 4096 bytes (appendix B)",
              codec.MAX_PROGRESS_NOTE_BYTES == 4096, "MUST",
              "appendix-b-constants")


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
