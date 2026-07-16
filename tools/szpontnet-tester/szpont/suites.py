"""The conformance suites: concrete TCP/UDP scenarios that check a candidate.

Every case maps to the interop vectors in ``docs/szpontnet/10-conformance.md``
and the MUST/SHOULD requirements of the chapters — the core protocol (01–10) in
categories A–H, and **chapter 11** (the trust / load-balancing layer and the
server / API-key role) in categories **I** (trust & load balancing) and **J**
(server role & API key). Each drives the candidate over real sockets via the
probe mesh, observes it (snapshot + wire captures), and records per-requirement
checks with the spec section that mandates them.

Cases skip cleanly (rather than fail) when the candidate does not claim a role
the case needs — e.g. dispatch cases skip a pure Participant that serves no
control session and executes no jobs.

Note on setup order: ``Scenario`` builds its probe peers when the ``with`` block
is entered, so every case constructs the scenario, calls ``add_peer`` for the
fleet, and only *then* enters the context.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

from . import assign, codec
from .codec import Job, NodeInfo
from .harness import ID_A, ID_B, ID_C, Scenario
from .model import Model
from .probe import wait_until
from .report import Reporter

ZERO_ID = "0" * 32  # sorts below ID_A ("a"*32): a peer the candidate must NOT dial


@dataclass
class Context:
    node_cmd: str
    model: Model
    loopback: bool = True


# MARK: - snapshot helpers


def _assignments(snap: dict | None) -> dict[str, tuple]:
    if not snap:
        return {}
    return {k: tuple(v.get("assigned", []))
            for k, v in (snap.get("assignments") or {}).items()}


def _up_peer_ids(snap: dict | None) -> set[str]:
    if not snap:
        return set()
    return {p.get("id") for p in snap.get("peers", []) if p.get("link") == "up"}


def _wait_snapshot(scn, pred, timeout: float):
    return wait_until(
        lambda: (lambda s: s if s and pred(s) else None)(scn.candidate.snapshot()), timeout)


def _need_port(rep: Reporter, scn) -> bool:
    if not scn.discover_port():
        rep.failed("candidate emits a beacon we can discover", "MUST",
                   "02-discovery#beacons",
                   "no valid beacon heard in 12s — candidate never started or never beaconed.\n"
                   + scn.candidate.log_tail())
        return False
    return True


def scn_self_info(scn) -> NodeInfo:
    """The candidate's own advertisement (from its snapshot, else the launch config)."""
    snap = scn.candidate.snapshot()
    if snap and snap.get("self"):
        got = NodeInfo.from_dict(snap["self"])
        if got:
            return got
    return NodeInfo(id=scn.candidate_id, name=scn.name, platform=scn.platform,
                    tier=scn.tier, tokens=scn.tokens, duties_enabled=scn.duties)


# MARK: - A. Discovery & linking


def case_a_beacon(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("A1", "Beacon emission, shape and cadence (02-discovery)")
    scn = Scenario(ctx.node_cmd, ctx.model, loopback=ctx.loopback)
    with scn:
        if not _need_port(rep, scn):
            return
        wait_until(lambda: len(scn.mesh.candidate_beacons) >= 3, 6.0)
        beacons, raws = scn.mesh.candidate_beacons, scn.mesh.candidate_beacon_raw
        rep.check("at least one beacon received", bool(beacons), "MUST",
                  "02-discovery#beacons", f"received {len(beacons)}")
        if not beacons:
            return
        rep.check("beacon is well-formed (id, positive tcpPort, epoch)",
                  not codec.validate_beacon(beacons[-1]), "MUST", "04-messages#beacon",
                  "; ".join(codec.validate_beacon(beacons[-1])))
        rep.check("beacon is one compact UTF-8 line",
                  not codec.is_single_line_json(raws[-1]), "MUST", "03-transport#framing",
                  "; ".join(codec.is_single_line_json(raws[-1])))
        rep.check("beacon advertises the real TCP listen port",
                  beacons[-1].get("tcpPort") == scn.candidate.tcp_port, "MUST",
                  "02-discovery#why-the-beacon-carries-the-tcp-port",
                  f"beacon={beacons[-1].get('tcpPort')} listen={scn.candidate.tcp_port}")
        if len(beacons) >= 3:
            rep.passed("beacons repeat at the configured interval", "SHOULD",
                       "02-discovery#beacons", f"{len(beacons)} beacons in ~6s")


def case_a_dial_rule(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("A2", "Dial rule: smaller id dials, exactly one link (02-discovery)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, loopback=ctx.loopback)
    # Peer id ("b"*32) sorts ABOVE the candidate ("a"*32) → the candidate must dial us.
    scn.add_peer(id=ID_B, name="hi", platform="macos", tier=1)
    with scn:
        if not _need_port(rep, scn):
            return
        peer = scn.mesh.peers[0]
        rep.check("candidate dials a higher-id peer (smaller id dials)",
                  bool(wait_until(lambda: peer.linked, 8.0)), "MUST",
                  "02-discovery#the-dial-rule-smaller-id-dials",
                  f"peer.linked={peer.linked} accept_count={peer.accept_count}")
        # Many beacons went out; a conformant node dials exactly once and guards
        # against re-dialing while the link is held (dedupe + single link per pair).
        time.sleep(2.0)
        rep.check("exactly one link per pair (no double-dial / dedupe)",
                  peer.accept_count == 1, "MUST",
                  "02-discovery#the-dial-rule-smaller-id-dials",
                  f"inbound dials from candidate = {peer.accept_count} (expected 1)")


def case_a_wait_rule(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("A3", "Larger-id node waits, does not dial (02-discovery)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, loopback=ctx.loopback)
    # Peer id "0"*32 sorts BELOW the candidate → the candidate must NOT dial it;
    # the peer (smaller id) dials the candidate instead.
    scn.add_peer(id=ZERO_ID, name="lo", platform="macos", tier=1)
    with scn:
        if not _need_port(rep, scn):
            return
        peer = scn.mesh.peers[0]
        rep.check("link still forms (smaller-id peer dials the candidate)",
                  bool(wait_until(lambda: peer.linked, 8.0)), "MUST",
                  "03-transport#inbound-the-accepter", f"linked={peer.linked}")
        rep.check("candidate did NOT dial the smaller-id peer", peer.accept_count == 0,
                  "MUST", "02-discovery#the-dial-rule-smaller-id-dials",
                  f"candidate made {peer.accept_count} outbound dials (expected 0)")


# MARK: - B. Handshake, framing tolerance, gossip, liveness


def case_b_handshake(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("B1", "Hello handshake and sees-gossip (03/04)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, loopback=ctx.loopback)
    scn.add_peer(id=ID_B, name="peer", platform="macos", tier=1)
    with scn:
        if not _need_port(rep, scn):
            return
        peer = scn.mesh.peers[0]
        if not wait_until(lambda: peer.linked, 8.0):
            rep.failed("peer link established", "MUST", "03-transport#link-lifecycle")
            return
        hellos = wait_until(lambda: peer.messages("hello"), 4.0) or []
        rep.check("candidate sends a hello on the link", bool(hellos), "MUST",
                  "03-transport#link-lifecycle")
        if hellos:
            rep.check("candidate's hello carries a well-formed NodeInfo",
                      not codec.validate_hello(hellos[-1]), "MUST", "04-messages#hello",
                      "; ".join(codec.validate_hello(hellos[-1])))
        rep.check("candidate lists the peer as an up link in its snapshot",
                  _wait_snapshot(scn, lambda s: ID_B in _up_peer_ids(s), 6.0) is not None,
                  "MUST", "08-state#statejson--the-snapshot")
        sees_ok = wait_until(
            lambda: ID_B in set((scn.candidate.snapshot() or {}).get("self", {}).get("sees", [])),
            5.0)
        rep.check("candidate advertises the new peer in its `sees` set", bool(sees_ok),
                  "SHOULD", "04-messages#nodeinfo")


def case_b_tolerance(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("B2", "Malformed input is never fatal; valid gossip still flows (03/09)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, loopback=ctx.loopback)
    scn.add_peer(id=ID_B, name="peer", platform="macos", tier=1, tokens="ok")
    with scn:
        if not _need_port(rep, scn):
            return
        peer = scn.mesh.peers[0]
        if not wait_until(lambda: peer.linked, 8.0):
            rep.skip_case("peer never linked")
            return
        base = _wait_snapshot(scn, lambda s: _assignments(s).get("audit") == (ID_A, ID_B), 8.0)
        rep.check("baseline: audit macos slot is the peer", base is not None, "MUST",
                  "06-coordination#the-assignment-algorithm",
                  f"audit={_assignments(scn.candidate.snapshot()).get('audit')}")
        conn = peer._conn
        for junk in (b"{not json\n", b"[1,2,3]\n", b'{"no":"type"}\n',
                     b'{"t":"zzz-unknown"}\n', b'{"t":123}\n'):
            peer._send_raw(conn, junk)
        # Then a VALID gossip carrying unknown extra fields (must be ignored, msg adopted).
        node_msg = codec.node_update(peer.info.bumped(tokens="out"))
        node_msg["node"]["futureField"] = {"anything": 1}
        node_msg["extraTopLevel"] = "ignore me"
        peer.send(node_msg)
        moved = _wait_snapshot(scn, lambda s: ID_B not in _assignments(s).get("audit", ()), 6.0)
        rep.check("link survived garbage AND processed the later valid gossip",
                  moved is not None, "MUST", "09-extensibility#the-compatibility-contract",
                  "candidate should drop the junk lines, keep the link, then adopt the "
                  "tokens=out update and drop the peer from the token-aware audit slot")
        rep.check("candidate still holds the peer link (not torn down by garbage)",
                  peer.linked, "MUST", "03-transport#framing")


def case_b_liveness(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("B4", "Liveness: heartbeat timeout marks a silent peer down (03/08)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, loopback=ctx.loopback)
    scn.add_peer(id=ID_B, name="peer", platform="macos", tier=1)
    with scn:
        if not _need_port(rep, scn):
            return
        peer = scn.mesh.peers[0]
        if not wait_until(lambda: peer.linked, 8.0):
            rep.skip_case("peer never linked")
            return
        _wait_snapshot(scn, lambda s: ID_B in _up_peer_ids(s), 6.0)
        beats_before = len(peer.messages("heartbeat"))
        time.sleep(1.0)
        rep.check("candidate sends heartbeats on the link",
                  len(peer.messages("heartbeat")) > beats_before, "MUST",
                  "03-transport#link-state")
        peer.freeze()  # silent death: stop heartbeating but keep the socket open
        timeout = scn.proto["peerTimeoutSecs"]
        down = _wait_snapshot(
            scn, lambda s: any(p.get("id") == ID_B and p.get("link") == "down"
                               for p in s.get("peers", [])), timeout + 4.0)
        rep.check("a silent peer is marked down after peerTimeoutSecs", down is not None,
                  "MUST", "03-transport#link-state")
        gone = _wait_snapshot(scn, lambda s: ID_B not in _assignments(s).get("audit", ()), 3.0)
        rep.check("the downed peer's duties are reassigned", gone is not None, "MUST",
                  "06-coordination#the-live-node-set")
        still_listed = any(p.get("id") == ID_B
                           for p in (scn.candidate.snapshot() or {}).get("peers", []))
        rep.check("downed peer is retained in the snapshot (marked down)", still_listed,
                  "SHOULD", "08-state#down-peer-retention")


def case_b_freshness(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("B5", "Freshness: an older (epoch,seq) never overwrites a newer one (04/08)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, loopback=ctx.loopback)
    scn.add_peer(id=ID_B, name="peer", platform="macos", tier=1, tokens="ok")
    with scn:
        if not _need_port(rep, scn):
            return
        peer = scn.mesh.peers[0]
        if not wait_until(lambda: peer.linked, 8.0):
            rep.skip_case("peer never linked")
            return
        e = peer.info.epoch

        def macos_ok(**kw):
            return NodeInfo(id=ID_B, name="peer", platform="macos", tier=1,
                            tcp_port=peer.tcp_port, **kw)
        peer.send(codec.node_update(macos_ok(tokens="out", epoch=e, seq=5)))
        rep.check("newer NodeInfo (higher seq) is adopted",
                  _wait_snapshot(scn, lambda s: ID_B not in _assignments(s).get("audit", ()), 6.0)
                  is not None, "MUST", "04-messages#nodeinfo")
        peer.send(codec.node_update(macos_ok(tokens="ok", epoch=e, seq=2)))
        time.sleep(2.0)
        rep.check("older NodeInfo (lower seq) does NOT overwrite the newer one",
                  ID_B not in _assignments(scn.candidate.snapshot()).get("audit", ()), "MUST",
                  "04-messages#nodeinfo",
                  "a stale seq=2 tokens=ok must not resurrect the peer into the audit slot")
        peer.send(codec.node_update(macos_ok(tokens="ok", epoch=e + 100.0, seq=0)))
        rep.check("a higher epoch supersedes regardless of seq",
                  _wait_snapshot(scn, lambda s: ID_B in _assignments(s).get("audit", ()), 6.0)
                  is not None, "MUST", "08-state#liveness--incarnations")


# MARK: - C. Placement determinism (V1)

_STD_TABLE = {"review": (ID_A,), "conflicts": (ID_A,), "audit": (ID_A, ID_C)}


def _std_fleet(ctx: Context, **overrides) -> Scenario:
    """Spec V1 fleet: candidate A=linux/t4, peers B=macos/t1, C=macos/t4."""
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, name="lin",
                   platform="linux", tier=4, loopback=ctx.loopback, **overrides)
    scn.add_peer(id=ID_B, name="mac-strong", platform="macos", tier=1)
    scn.add_peer(id=ID_C, name="mac-weak", platform="macos", tier=4)
    return scn


def case_c_placement(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("C1", "Deterministic placement matches the spec vectors (06 / V1)")
    with _std_fleet(ctx) as scn:
        if not _need_port(rep, scn):
            return
        if _wait_snapshot(scn, lambda s: len(_up_peer_ids(s)) >= 2, 12.0) is None:
            rep.failed("candidate links the full fleet", "MUST", "02-discovery")
            return
        snap = _wait_snapshot(
            scn, lambda s: _assignments(s).get("audit") == _STD_TABLE["audit"], 8.0) \
            or scn.candidate.snapshot()
        got = _assignments(snap)
        fleet = [scn_self_info(scn)] + [p.info for p in scn.mesh.peers]
        oracle = {d: tuple(a["assigned"])
                  for d, a in assign.assign_all(ctx.model, fleet, local_id=ID_A).items()}
        for duty, expected in _STD_TABLE.items():
            rep.check(f"{duty} → {list(expected)}", got.get(duty) == expected, "MUST",
                      "06-coordination#the-assignment-algorithm",
                      f"candidate={got.get(duty)} expected={expected} oracle={oracle.get(duty)}")
        rep.check("candidate agrees with the independent placement oracle", got == oracle,
                  "MUST", "06-coordination#determinism-requirements",
                  f"candidate={got} oracle={oracle}")


def case_c_override(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("C2", "Gossiped strongest-first override moves the owner (06 / V1)")
    with _std_fleet(ctx) as scn:
        if not _need_port(rep, scn):
            return
        if _wait_snapshot(scn, lambda s: len(_up_peer_ids(s)) >= 2, 12.0) is None:
            rep.skip_case("fleet never fully linked")
            return
        scn.mesh.peers[0].send(codec.overrides_update({
            "rev": 1, "updatedBy": ID_B,
            "duties": {"review": {"strategy": "strongest-first", "tokenAware": True, "spread": []}}}))
        rep.check("review → [B] after a strongest-first override is gossiped in",
                  _wait_snapshot(scn, lambda s: _assignments(s).get("review") == (ID_B,), 8.0)
                  is not None, "MUST", "06-coordination#placement-overrides",
                  f"review={_assignments(scn.candidate.snapshot()).get('review')}")


def case_c_tokens(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("C3", "Token exclusion and de-prioritization (05/06 / V1)")
    for tok in ("out", "low"):
        with _std_fleet(ctx, tokens=tok) as scn:
            if not _need_port(rep, scn):
                return
            if _wait_snapshot(scn, lambda s: len(_up_peer_ids(s)) >= 2, 12.0) is None:
                rep.skip_case("fleet never fully linked")
                return
            moved = _wait_snapshot(scn, lambda s: _assignments(s).get("review") == (ID_C,), 8.0)
            level = "excluded" if tok == "out" else "de-prioritized"
            rep.check(f"candidate tokens={tok}: review → [C] (self {level})",
                      moved is not None, "MUST", "05-resources#tokens",
                      f"review={_assignments(scn.candidate.snapshot()).get('review')}")


def case_c_shortfall(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("C4", "Spread shortfall when a platform is missing (06 / V1)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_C, name="mac-weak",
                   platform="macos", tier=4, loopback=ctx.loopback)
    scn.add_peer(id=ID_B, name="mac-strong", platform="macos", tier=1)
    with scn:
        if not _need_port(rep, scn):
            return
        if _wait_snapshot(scn, lambda s: len(_up_peer_ids(s)) >= 1, 12.0) is None:
            rep.skip_case("peer never linked")
            return
        snap = _wait_snapshot(scn, lambda s: _assignments(s).get("audit") == (ID_C,), 8.0) \
            or scn.candidate.snapshot()
        got = _assignments(snap)
        rep.check("audit → [C] (weakest macos), no linux available",
                  got.get("audit") == (ID_C,), "MUST",
                  "06-coordination#the-assignment-algorithm", f"audit={got.get('audit')}")
        audit = (snap.get("assignments") or {}).get("audit", {})
        short = {(s.get("platform"), s.get("missing")) for s in audit.get("shortfall", [])}
        rep.check("audit reports a linux shortfall of 1", ("linux", 1) in short, "MUST",
                  "06-coordination#the-assignment-algorithm", f"shortfall={audit.get('shortfall')}")


# MARK: - D. Dispatch (V4)


def case_d_executor(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("D1", "Executor: runs an inbound dispatch, replies job-status (07 / V4)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, name="cand",
                   platform="linux", tier=4, loopback=ctx.loopback)
    scn.add_peer(id=ID_B, name="peer", platform="macos", tier=1)
    with scn:
        if not _need_port(rep, scn):
            return
        peer = scn.mesh.peers[0]
        if not wait_until(lambda: peer.linked, 8.0):
            rep.skip_case("peer never linked")
            return
        job = Job(id="job-exec-1", duty="review", prompt="please run this",
                  requested_by=ID_B, requested_at=time.time())
        peer.send(codec.dispatch_job(job))
        reply = wait_until(lambda: next((m for m in peer.messages("job-status")
                                         if m.get("id") == job.id), None), 8.0)
        if reply is None:
            rep.skip_case("no job-status reply — candidate is not an Executor")
            return
        rep.check("candidate replies job-status for the dispatched job", True, "MUST",
                  "07-dispatch#execution")
        rep.check("job-status reports 'spawned' (work started)",
                  reply.get("status") == "spawned", "MUST", "04-messages#job-status",
                  f"status={reply.get('status')} reason={reply.get('reason')}")
        marker = scn.spawn_marker / "cand.txt"
        landed = wait_until(lambda: marker.exists() and marker.read_text() == "please run this", 6.0)
        rep.check("candidate actually ran the job (spawn side effect observed)", bool(landed),
                  "MUST", "07-dispatch#execution",
                  "the SZPONTNET_SPAWN template must have staged the prompt")


def case_d_router(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("D2", "Dispatcher: control-session dispatch routes per slot (07 / V4)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, name="cand",
                   platform="linux", tier=4, loopback=ctx.loopback)
    scn.add_peer(id=ID_B, name="mac", platform="macos", tier=1)
    with scn:
        if not _need_port(rep, scn):
            return
        if scn.candidate.ctl_status() is None:
            rep.skip_case("candidate serves no control session — not Controllable/Dispatcher")
            return
        peer = scn.mesh.peers[0]
        if not wait_until(lambda: peer.linked, 8.0):
            rep.skip_case("macos peer never linked")
            return
        _wait_snapshot(scn, lambda s: _assignments(s).get("audit") == (ID_A, ID_B), 8.0)
        try:
            sess = scn.candidate.open_ctl()
            res = sess.command(codec.dispatch_route("audit", "bundle e2e"), timeout=10.0)
        except OSError:
            rep.skip_case("could not open control session")
            return
        finally:
            try:
                sess.close()
            except Exception:
                pass
        if not res or res.get("t") != "dispatch-result":
            rep.skip_case(f"no dispatch-result (got {res.get('t') if res else None})")
            return
        by_slot = {r.get("slot"): r for r in res.get("results", [])}
        rep.check("dispatch-result has one entry per slot (linux + macos)",
                  {"linux", "macos"} <= set(by_slot), "MUST", "07-dispatch#routing-a-job",
                  f"slots={list(by_slot)}")
        rep.check("linux slot spawned on the candidate itself",
                  by_slot.get("linux", {}).get("node") == ID_A
                  and by_slot.get("linux", {}).get("status") == "spawned", "MUST",
                  "07-dispatch#placing-on-a-node", f"linux={by_slot.get('linux')}")
        rep.check("macos slot routed to the remote peer and spawned",
                  by_slot.get("macos", {}).get("node") == ID_B
                  and by_slot.get("macos", {}).get("status") == "spawned", "MUST",
                  "07-dispatch#placing-on-a-node", f"macos={by_slot.get('macos')}")
        rep.check("the remote peer actually received the job over its link",
                  any(j.duty == "audit" for j in peer.jobs), "MUST",
                  "07-dispatch#placing-on-a-node")


def case_d_failover(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("D3", "Dispatch fails a slot over to the next candidate (07 / V4)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, name="cand",
                   platform="linux", tier=4, loopback=ctx.loopback)
    # C (t4) ranks before B (t1) for the macos slot; C declines → fail over to B.
    scn.add_peer(id=ID_C, name="mac-weak", platform="macos", tier=4, dispatch_reply="failed")
    scn.add_peer(id=ID_B, name="mac-strong", platform="macos", tier=1, dispatch_reply="spawned")
    with scn:
        if not _need_port(rep, scn):
            return
        if scn.candidate.ctl_status() is None:
            rep.skip_case("candidate serves no control session — not a Dispatcher")
            return
        if _wait_snapshot(scn, lambda s: len(_up_peer_ids(s)) >= 2, 12.0) is None:
            rep.skip_case("fleet never fully linked")
            return
        _wait_snapshot(scn, lambda s: _assignments(s).get("audit") == (ID_A, ID_C), 8.0)
        try:
            sess = scn.candidate.open_ctl()
            res = sess.command(codec.dispatch_route("audit", "e2e"), timeout=12.0)
        except OSError:
            rep.skip_case("control session failed")
            return
        finally:
            try:
                sess.close()
            except Exception:
                pass
        macos = {r.get("slot"): r for r in (res or {}).get("results", [])}.get("macos", {})
        rep.check("macos slot failed over from the declining C to B",
                  macos.get("node") == ID_B and macos.get("status") == "spawned", "MUST",
                  "07-dispatch#routing-a-job", f"macos slot outcome={macos}")


def case_d_unknown_duty(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("D4", "Control dispatch of an unknown duty yields an error (04/07)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, loopback=ctx.loopback)
    with scn:
        if not _need_port(rep, scn):
            return
        if scn.candidate.ctl_status() is None:
            rep.skip_case("candidate serves no control session")
            return
        try:
            sess = scn.candidate.open_ctl()
            res = sess.command(codec.dispatch_route("no-such-duty", "x"), timeout=6.0)
            sess.close()
        except OSError:
            rep.skip_case("control session failed")
            return
        rep.check("unknown duty → error reply", res is not None and res.get("t") == "error",
                  "MUST", "04-messages#dispatch", f"reply={res}")


# MARK: - E. The join fence (V5)


def case_e_wrong_secret_peer(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("E1", "A wrong-secret peer never links (03 / V5)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, platform="linux", tier=4,
                   secret="s3cr3t", mesh_secret="wrong", loopback=ctx.loopback)
    scn.add_peer(id=ID_B, name="intruder", platform="macos", tier=1)
    with scn:
        if not _need_port(rep, scn):
            return
        peer = scn.mesh.peers[0]
        time.sleep(4.0)  # ample beacon + dial rounds
        rep.check("wrong-secret peer is never an up link", not peer.linked, "MUST",
                  "03-transport#the-join-fence", f"peer.linked={peer.linked}")
        up = _up_peer_ids(scn.candidate.snapshot())
        rep.check("candidate snapshot shows no up peer for the intruder", ID_B not in up,
                  "MUST", "03-transport#the-join-fence", f"up peers={up}")


def case_e_wrong_secret_ctl(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("E2", "A wrong-secret control client cannot drive the node (03 / V5)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, secret="s3cr3t",
                   loopback=ctx.loopback)
    with scn:
        if not _need_port(rep, scn):
            return
        scn.candidate.secret = "s3cr3t"
        right = scn.candidate.ctl_status(timeout=4.0)
        if right is None:
            rep.skip_case("candidate serves no control session to fence")
            return
        scn.candidate.secret = "wrong"
        wrong = scn.candidate.ctl_status(timeout=4.0)
        scn.candidate.secret = "s3cr3t"
        rep.check("correct secret opens a control session", right is not None, "MUST",
                  "04-messages#ctl")
        rep.check("wrong secret is refused (no state snapshot returned)", wrong is None,
                  "MUST", "03-transport#the-join-fence")


def _naked_dispatch_handler(conn, peer):
    """Adversary: the candidate dialed us and sent its hello; we ignore it and
    push a NAKED dispatch (no hello, no secret). A conformant node MUST reject
    this — the outbound-dial fence bypass (03-transport / V5)."""
    try:
        conn.recv(65536)  # swallow the candidate's hello
        job = {"t": "dispatch", "v": 1, "job": {
            "id": "evil", "duty": "review", "requestedBy": peer.info.id,
            "requestedAt": 0, "prompt": "attacker payload"}}
        conn.sendall((json.dumps(job) + "\n").encode())
        conn.recv(4096)
    except OSError:
        pass


def _run_fence_bypass(rep: Reporter, ctx: Context, secret: str, case_id: str, label: str) -> None:
    rep.begin_case(case_id, label)
    # The candidate is the VICTIM with a LOW id ("0"*32) so, by the smaller-id-dials
    # rule, it DIALS the higher-id attacker ("f"*32) — creating the outbound,
    # still-unauthenticated link the bypass targets. The attacker only accepts
    # (dial_mode="never") and, via its raw handler, ignores the victim's hello and
    # pushes a naked dispatch.
    victim_id, attacker_id = "0" * 32, "f" * 32
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=victim_id, name="victim",
                   platform="linux", tier=4, secret=secret, loopback=ctx.loopback)
    scn.add_peer(id=attacker_id, name="attacker", platform="linux", tier=1,
                 dial_mode="never", raw_accept_handler=_naked_dispatch_handler)
    with scn:
        if not _need_port(rep, scn):
            return
        peer = scn.mesh.peers[0]
        dialed = wait_until(lambda: peer.accept_count >= 1, 8.0)
        rep.check("candidate dials the higher-id attacker (sets up the outbound link)",
                  bool(dialed), "MUST", "02-discovery#the-dial-rule-smaller-id-dials",
                  f"inbound dials seen by attacker = {peer.accept_count}")
        marker = scn.spawn_marker / "victim.txt"
        landed = wait_until(lambda: marker.exists(), 6.0)
        rep.check("naked dispatch on a dialed link does NOT spawn work", not landed, "MUST",
                  "03-transport#the-join-fence",
                  "a first message that is a bare dispatch (no hello) MUST tear the outbound "
                  "link down, never execute")
        up = _up_peer_ids(scn.candidate.snapshot())
        rep.check("the attacker never becomes an up peer", attacker_id not in up, "MUST",
                  "03-transport#the-join-fence", f"up peers={up}")


def case_e_fence_bypass_secret(rep: Reporter, ctx: Context) -> None:
    _run_fence_bypass(rep, ctx, "s3cr3t", "E3",
                      "Outbound-dial fence: naked dispatch rejected WITH a secret (03 / V5)")


def case_e_fence_bypass_open(rep: Reporter, ctx: Context) -> None:
    _run_fence_bypass(rep, ctx, "", "E4",
                      "Outbound-dial ordering: naked dispatch rejected on an OPEN mesh (03)")


# MARK: - F. Codec conformance on emitted messages (V2)


def case_f_emitted(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("F1", "Every message the candidate emits is on-spec (03/04 / V2)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, loopback=ctx.loopback)
    scn.add_peer(id=ID_B, name="peer", platform="macos", tier=1)
    with scn:
        if not _need_port(rep, scn):
            return
        peer = scn.mesh.peers[0]
        wait_until(lambda: peer.linked and peer.messages("heartbeat"), 8.0)
        time.sleep(1.0)
        frame_probs = []
        for raw in list(peer.raw_received):
            frame_probs += codec.is_single_line_json(raw)
        rep.check("all link frames are compact newline-terminated UTF-8 JSON",
                  not frame_probs, "MUST", "03-transport#framing",
                  "; ".join(sorted(set(frame_probs))))
        rep.check("all messages carry a string type tag `t`",
                  all(not codec.validate_envelope(m) for m in peer.messages()), "MUST",
                  "04-messages")
        hb = peer.messages("heartbeat")
        rep.check("emitted heartbeats are well-formed",
                  bool(hb) and not codec.validate_heartbeat(hb[-1]), "MUST",
                  "04-messages#heartbeat")
        node_probs = []
        for m in peer.messages("node") + peer.messages("hello"):
            node_probs += codec.validate_nodeinfo(m.get("node", {}))
        rep.check("emitted hello/node NodeInfos are well-formed", not node_probs, "MUST",
                  "04-messages#nodeinfo", "; ".join(sorted(set(node_probs))))


# MARK: - G. Snapshot shape (08)


def case_g_snapshot(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("G1", "State snapshot conforms to the schema (08)")
    with _std_fleet(ctx) as scn:
        if not _need_port(rep, scn):
            return
        snap = _wait_snapshot(scn, lambda s: len(_up_peer_ids(s)) >= 1, 12.0)
        if snap is None:
            rep.skip_case("candidate exposes no snapshot (no ctl status, no state.json)")
            return
        for key in ("tcpPort", "self", "peers", "assignments", "overrides"):
            rep.check(f"snapshot has `{key}`", key in snap, "MUST",
                      "08-state#statejson--the-snapshot")
        rep.check("snapshot.self is a valid NodeInfo",
                  not codec.validate_nodeinfo(snap.get("self", {})), "MUST",
                  "08-state#statejson--the-snapshot")
        peers_ok = all(
            p.get("link") in ("up", "stale", "down") and "addr" in p and "lastSeenSecsAgo" in p
            for p in snap.get("peers", []))
        rep.check("each peer carries link/addr/lastSeenSecsAgo decoration", peers_ok, "MUST",
                  "08-state#statejson--the-snapshot")
        assigns = snap.get("assignments") or {}
        shape_ok = all(
            isinstance(a.get("assigned"), list) and isinstance(a.get("shortfall"), list)
            and a.get("duty") == d for d, a in assigns.items())
        rep.check("assignments map duty → {duty, assigned[], shortfall[]}", shape_ok, "MUST",
                  "08-state#statejson--the-snapshot")
        rep.check("overrides carry rev/updatedBy/duties",
                  {"rev", "updatedBy", "duties"} <= set(snap.get("overrides") or {}), "MUST",
                  "06-coordination#placement-overrides")


# MARK: - H. Overrides LWW gossip (V3)


def case_h_overrides_lww(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("H1", "Placement overrides converge last-writer-wins (06 / V3)")
    with _std_fleet(ctx) as scn:
        if not _need_port(rep, scn):
            return
        if _wait_snapshot(scn, lambda s: len(_up_peer_ids(s)) >= 2, 12.0) is None:
            rep.skip_case("fleet never fully linked")
            return
        peer_b, peer_c = scn.mesh.peers[0], scn.mesh.peers[1]
        peer_b.send(codec.overrides_update({
            "rev": 2, "updatedBy": ID_B,
            "duties": {"review": {"strategy": "strongest-first", "tokenAware": True, "spread": []}}}))
        rep.check("higher-rev override is adopted",
                  _wait_snapshot(scn, lambda s: _assignments(s).get("review") == (ID_B,), 8.0)
                  is not None, "MUST", "06-coordination#placement-overrides")
        rep.check("adopted override is re-gossiped to other peers",
                  bool(wait_until(lambda: any(m.get("overrides", {}).get("rev") == 2
                                              for m in peer_c.messages("overrides")), 6.0)),
                  "MUST", "03-transport#gossip-fan-out")
        peer_b.send(codec.overrides_update({
            "rev": 1, "updatedBy": ID_B,
            "duties": {"review": {"strategy": "weakest-first", "tokenAware": True, "spread": []}}}))
        time.sleep(2.0)
        rep.check("a lower-rev override is ignored",
                  _assignments(scn.candidate.snapshot()).get("review") == (ID_B,), "MUST",
                  "06-coordination#placement-overrides",
                  f"review={_assignments(scn.candidate.snapshot()).get('review')}")


# MARK: - I. Trust & load balancing (ch 11)


def _peer_snap(snap: dict | None, peer_id: str) -> dict:
    for p in (snap or {}).get("peers", []):
        if p.get("id") == peer_id:
            return p
    return {}


def _dispatch_status(peer, job, timeout: float = 6.0):
    """Send a dispatch job from a probe to the candidate; return the job-status."""
    peer.send(codec.dispatch_job(job))
    return wait_until(lambda: next((m for m in peer.messages("job-status")
                                    if m.get("id") == job.id), None), timeout)


def case_i_empty_allowlist_trust(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("I1", "Empty allowlist = full trust; a verified peer's dispatch runs (11)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, platform="linux", tier=4,
                   loopback=ctx.loopback)
    scn.add_peer(id=ID_B, name="peer", platform="macos", tier=1, trust_peer=True)
    with scn:
        if not _need_port(rep, scn):
            return
        peer = scn.mesh.peers[0]
        if not wait_until(lambda: peer.linked, 8.0):
            rep.skip_case("peer never linked")
            return
        verified = wait_until(lambda: _peer_snap(scn.candidate.snapshot(), ID_B).get("verified"), 6.0)
        rep.check("candidate verifies the peer's proof of possession (auth over the nonce)",
                  bool(verified), "MUST", "11-trust-and-balancing#conformance",
                  "the candidate must sign & verify the domain-separated challenge")
        psnap = _peer_snap(scn.candidate.snapshot(), ID_B)
        rep.check("candidate records the peer's proven fingerprint",
                  psnap.get("fingerprint") == peer.fingerprint, "MUST",
                  "11-trust-and-balancing#the-fingerprint",
                  f"snap={psnap.get('fingerprint','')[:16]} probe={peer.fingerprint[:16]}")
        rep.check("candidate proved possession of ITS key back to the peer",
                  bool(wait_until(lambda: peer.candidate_verified_ok, 4.0)), "MUST",
                  "11-trust-and-balancing#conformance")
        rep.check("empty allowlist classifies the verified peer as personal",
                  psnap.get("trust") == "personal", "MUST",
                  "11-trust-and-balancing#the-empty-allowlist")
        job = Job(id="i1-run", duty="review", prompt="run me", requested_by=ID_B,
                  requested_at=time.time())
        reply = _dispatch_status(peer, job)
        rep.check("a personal (verified, full-trust) peer's dispatch is spawned",
                  reply is not None and reply.get("status") == "spawned", "MUST",
                  "11-trust-and-balancing#conformance",
                  f"status={reply.get('status') if reply else None}")


def case_i_proof_of_possession(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("I2", "Proof of possession: foreign until the operator trusts the proven key (11)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, platform="linux", tier=4,
                   loopback=ctx.loopback)
    scn.add_peer(id=ID_B, name="peer", platform="macos", tier=1, trust_peer=True)
    with scn:
        if not _need_port(rep, scn):
            return
        peer = scn.mesh.peers[0]
        if not wait_until(lambda: peer.linked, 8.0):
            rep.skip_case("peer never linked")
            return
        snap = wait_until(lambda: (scn.candidate.snapshot() if
                                   (scn.candidate.snapshot() or {}).get("self", {}).get("fingerprint")
                                   else None), 6.0)
        if not snap or scn.candidate.ctl_status() is None:
            rep.skip_case("candidate exposes no fingerprint / control session — not trust-capable")
            return
        self_fp = snap["self"]["fingerprint"]
        wait_until(lambda: _peer_snap(scn.candidate.snapshot(), ID_B).get("verified"), 6.0)
        # Enable the allowlist by trusting ONLY the candidate itself → the verified
        # peer, though it proved a key, is now unlisted and therefore foreign.
        try:
            sess = scn.candidate.open_ctl()
            ok = sess.command(codec.trust(self_fp, "self"))
            sess.close()
        except OSError:
            rep.skip_case("could not drive trust control command")
            return
        rep.check("`trust` control command is accepted",
                  ok is not None and ok.get("t") == "ok", "MUST", "04-messages#ctl",
                  f"reply={ok}")
        became_foreign = wait_until(
            lambda: _peer_snap(scn.candidate.snapshot(), ID_B).get("trust") == "foreign", 4.0)
        rep.check("with a non-empty allowlist, an unlisted (though verified) peer is foreign",
                  bool(became_foreign), "MUST",
                  "11-trust-and-balancing#conformance",
                  f"trust={_peer_snap(scn.candidate.snapshot(), ID_B).get('trust')}")
        job = Job(id="i2-foreign", duty="review", prompt="x", requested_by=ID_B,
                  requested_at=time.time())
        reply = _dispatch_status(peer, job)
        rep.check("a foreign device's dispatch is declined (not spawned)",
                  reply is not None and reply.get("status") == "declined", "MUST",
                  "11-trust-and-balancing#conformance",
                  f"status={reply.get('status') if reply else None} reason={reply.get('reason') if reply else ''}")
        # Now trust the peer's PROVEN fingerprint → it becomes personal and runs.
        try:
            sess = scn.candidate.open_ctl()
            sess.command(codec.trust(peer.fingerprint, "peer"))
            sess.close()
        except OSError:
            rep.skip_case("could not trust the peer fingerprint")
            return
        became_personal = wait_until(
            lambda: _peer_snap(scn.candidate.snapshot(), ID_B).get("trust") == "personal", 4.0)
        rep.check("trusting the peer's proven fingerprint promotes it to personal",
                  bool(became_personal), "MUST",
                  "11-trust-and-balancing#trust-is-never-derived-from-an-advertisement")
        job2 = Job(id="i2-personal", duty="review", prompt="y", requested_by=ID_B,
                   requested_at=time.time())
        reply2 = _dispatch_status(peer, job2)
        rep.check("once trusted, the same peer's dispatch is spawned",
                  reply2 is not None and reply2.get("status") == "spawned", "MUST",
                  "11-trust-and-balancing#conformance",
                  f"status={reply2.get('status') if reply2 else None}")


def case_i_keyless_foreign(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("I3", "A keyless peer proves nothing → foreign under any allowlist (11)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, platform="linux", tier=4,
                   loopback=ctx.loopback)
    # A keyless probe: advertises no pubkey, answers no challenge — can never be
    # verified, so it has no fingerprint and is foreign the moment trust is on.
    scn.add_peer(id=ID_B, name="keyless", platform="macos", tier=1, trust_peer=False)
    with scn:
        if not _need_port(rep, scn):
            return
        peer = scn.mesh.peers[0]
        if not wait_until(lambda: peer.linked, 8.0):
            rep.skip_case("peer never linked")
            return
        snap = wait_until(lambda: (scn.candidate.snapshot() if
                                   (scn.candidate.snapshot() or {}).get("self", {}).get("fingerprint")
                                   else None), 6.0)
        if not snap or scn.candidate.ctl_status() is None:
            rep.skip_case("candidate exposes no fingerprint / control session")
            return
        time.sleep(1.0)
        rep.check("a keyless peer is never verified",
                  _peer_snap(scn.candidate.snapshot(), ID_B).get("verified") is False, "MUST",
                  "11-trust-and-balancing#conformance")
        self_fp = snap["self"]["fingerprint"]
        try:
            sess = scn.candidate.open_ctl()
            sess.command(codec.trust(self_fp, "self"))
            sess.close()
        except OSError:
            rep.skip_case("could not drive trust control command")
            return
        foreign = wait_until(
            lambda: _peer_snap(scn.candidate.snapshot(), ID_B).get("trust") == "foreign", 4.0)
        rep.check("keyless peer is foreign under a non-empty allowlist", bool(foreign), "MUST",
                  "11-trust-and-balancing#conformance")
        job = Job(id="i3-foreign", duty="review", prompt="x", requested_by=ID_B,
                  requested_at=time.time())
        reply = _dispatch_status(peer, job)
        rep.check("a keyless (unverifiable) peer's dispatch is declined",
                  reply is not None and reply.get("status") == "declined", "MUST",
                  "11-trust-and-balancing#conformance",
                  f"status={reply.get('status') if reply else None}")


def case_i_requester_from_link(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("I4", "Requester classified from the verified link, not `requestedBy` (11)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, platform="linux", tier=4,
                   loopback=ctx.loopback)
    scn.add_peer(id=ID_B, name="peer", platform="macos", tier=1, trust_peer=True)
    with scn:
        if not _need_port(rep, scn):
            return
        peer = scn.mesh.peers[0]
        if not wait_until(lambda: peer.linked, 8.0):
            rep.skip_case("peer never linked")
            return
        snap = wait_until(lambda: (scn.candidate.snapshot() if
                                   (scn.candidate.snapshot() or {}).get("self", {}).get("fingerprint")
                                   else None), 6.0)
        if not snap or scn.candidate.ctl_status() is None:
            rep.skip_case("candidate exposes no fingerprint / control session")
            return
        self_fp = snap["self"]["fingerprint"]
        wait_until(lambda: _peer_snap(scn.candidate.snapshot(), ID_B).get("verified"), 6.0)
        # Turn on the allowlist (trust self only) so the peer is foreign on its link.
        try:
            sess = scn.candidate.open_ctl()
            sess.command(codec.trust(self_fp, "self"))
            sess.close()
        except OSError:
            rep.skip_case("could not drive trust control command")
            return
        wait_until(lambda: _peer_snap(scn.candidate.snapshot(), ID_B).get("trust") == "foreign", 4.0)
        # The foreign peer LIES: it claims requestedBy = the candidate's own id (a
        # trusted-looking value). Classification must ignore this and stay foreign.
        job = Job(id="i4-spoof", duty="review", prompt="x", requested_by=ID_A,
                  requested_at=time.time())
        reply = _dispatch_status(peer, job)
        rep.check("a spoofed requestedBy does NOT grant trust (link identity wins)",
                  reply is not None and reply.get("status") == "declined", "MUST",
                  "11-trust-and-balancing#conformance",
                  f"status={reply.get('status') if reply else None} — requestedBy was spoofed to a "
                  "trusted id but the request rides a foreign link")


def case_i_declined_failover(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("I5", "`declined` job-status fails a slot over, like `failed` (11)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, name="cand",
                   platform="linux", tier=4, loopback=ctx.loopback)
    # C (t4) ranks before B (t1) weakest-first for the macos slot; C DECLINES →
    # the dispatcher must advance to B exactly as it would for a `failed`.
    scn.add_peer(id=ID_C, name="mac-weak", platform="macos", tier=4, dispatch_reply="declined")
    scn.add_peer(id=ID_B, name="mac-strong", platform="macos", tier=1, dispatch_reply="spawned")
    with scn:
        if not _need_port(rep, scn):
            return
        if scn.candidate.ctl_status() is None:
            rep.skip_case("candidate serves no control session — not a Dispatcher")
            return
        if _wait_snapshot(scn, lambda s: len(_up_peer_ids(s)) >= 2, 12.0) is None:
            rep.skip_case("fleet never fully linked")
            return
        _wait_snapshot(scn, lambda s: _assignments(s).get("audit") == (ID_A, ID_C), 8.0)
        try:
            sess = scn.candidate.open_ctl()
            res = sess.command(codec.dispatch_route("audit", "e2e"), timeout=12.0)
            sess.close()
        except OSError:
            rep.skip_case("control session failed")
            return
        macos = {r.get("slot"): r for r in (res or {}).get("results", [])}.get("macos", {})
        rep.check("a `declined` reply fails the macos slot over from C to B",
                  macos.get("node") == ID_B and macos.get("status") == "spawned", "MUST",
                  "11-trust-and-balancing#conformance",
                  f"macos slot outcome={macos} (C declined, must advance to B)")
        rep.check("the declining node C actually received (and declined) the job",
                  any(j.duty == "audit" for j in scn.mesh.peers[0].jobs), "MUST",
                  "07-dispatch#routing-a-job")


def case_i_surplus_first(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("I6", "surplus-first dispatch picks the most-surplus node (11)")
    # Candidate has the LEAST surplus; two peers advertise more. The default
    # dispatch strategy is surplus-first, so `review` (no spread, single slot)
    # must route to the highest-surplus node — here peer C (surplus 18).
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, platform="linux", tier=4,
                   loopback=ctx.loopback,
                   stats={"plan": "pro", "usageAvg": 0.0, "quotaLeft": 0.5})   # surplus 0.5
    scn.add_peer(id=ID_B, name="mid", platform="macos", tier=1,
                 stats={"plan": "max-5x", "usageAvg": 1.0, "quotaLeft": 6.0})   # surplus 5
    scn.add_peer(id=ID_C, name="hi", platform="macos", tier=4,
                 stats={"plan": "max-20x", "usageAvg": 2.0, "quotaLeft": 20.0})  # surplus 18
    with scn:
        if not _need_port(rep, scn):
            return
        if scn.candidate.ctl_status() is None:
            rep.skip_case("candidate serves no control session — not a Dispatcher")
            return
        if _wait_snapshot(scn, lambda s: len(_up_peer_ids(s)) >= 2, 12.0) is None:
            rep.skip_case("fleet never fully linked")
            return
        # Confirm the candidate actually ingested the peers' advertised surplus,
        # else the dispatch pick would be meaningless.
        surplus_seen = wait_until(
            lambda: _peer_snap(scn.candidate.snapshot(), ID_C).get("surplus") == 18.0, 6.0)
        rep.check("candidate ingests a peer's advertised stats (surplus)",
                  bool(surplus_seen), "MUST", "11-trust-and-balancing#stats",
                  f"peer C surplus in snapshot={_peer_snap(scn.candidate.snapshot(), ID_C).get('surplus')}")
        try:
            sess = scn.candidate.open_ctl()
            res = sess.command(codec.dispatch_route("review", "load"), timeout=10.0)
            sess.close()
        except OSError:
            rep.skip_case("control session failed")
            return
        if not res or res.get("t") != "dispatch-result":
            rep.skip_case(f"no dispatch-result (got {res.get('t') if res else None})")
            return
        outcome = {r.get("slot"): r for r in res.get("results", [])}.get("any", {})
        rep.check("surplus-first routes `review` to the highest-surplus node (C)",
                  outcome.get("node") == ID_C and outcome.get("status") == "spawned", "MUST",
                  "11-trust-and-balancing#conformance",
                  f"picked={outcome.get('node','')[:6]} status={outcome.get('status')} "
                  f"(expected C={ID_C[:6]}, surplus 18)")


def case_i_omit_when_empty(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("I7", "Byte-compat: emitted advert omits pubkey/stats when unused (11)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, loopback=ctx.loopback)
    scn.add_peer(id=ID_B, name="peer", platform="macos", tier=1)
    with scn:
        if not _need_port(rep, scn):
            return
        peer = scn.mesh.peers[0]
        wait_until(lambda: peer.linked and peer.messages("hello"), 8.0)
        time.sleep(0.5)
        adverts = [m.get("node", {}) for m in peer.messages("hello") + peer.messages("node")]
        if not adverts:
            rep.skip_case("no hello/node advertisement observed")
            return
        # Every emitted NodeInfo stays schema-valid, and the additive fields obey
        # the omit-when-empty rule: if `stats`/`pubkey` are present they must be
        # non-empty (a node that carries neither is byte-identical to core v1).
        node_probs = []
        for node in adverts:
            node_probs += codec.validate_nodeinfo(node)
        rep.check("emitted NodeInfos remain schema-valid with the ch-11 fields",
                  not node_probs, "MUST", "04-messages#nodeinfo",
                  "; ".join(sorted(set(node_probs))))
        empty_field = any(
            ("pubkey" in n and n["pubkey"] == "") or ("stats" in n and n["stats"] == {})
            for n in adverts)
        rep.check("pubkey/stats are OMITTED when empty (never present-but-empty)",
                  not empty_field, "MUST", "11-trust-and-balancing#conformance",
                  "an empty pubkey/stats key on the wire breaks byte-compat with core v1")


# MARK: - J. Server role & API key (ch 11)


def case_j_server_no_dispatch(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("J1", "Server mode never originates a dispatch to a peer (11)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, name="srv",
                   platform="linux", tier=4, loopback=ctx.loopback, server=True)
    scn.add_peer(id=ID_B, name="mac", platform="macos", tier=1)
    with scn:
        if not _need_port(rep, scn):
            return
        if scn.candidate.ctl_status() is None:
            rep.skip_case("candidate serves no control session — cannot exercise server routing")
            return
        peer = scn.mesh.peers[0]
        if not wait_until(lambda: peer.linked, 8.0):
            rep.skip_case("peer never linked")
            return
        _wait_snapshot(scn, lambda s: ID_B in _up_peer_ids(s), 8.0)
        # An unqualified dispatch (no target) must run on the server ITSELF, never
        # fan out to the linked macos peer.
        try:
            sess = scn.candidate.open_ctl()
            res = sess.command(codec.dispatch_route("audit", "e2e"), timeout=10.0)
            sess.close()
        except OSError:
            rep.skip_case("control session failed")
            return
        if not res or res.get("t") != "dispatch-result":
            rep.skip_case(f"no dispatch-result (got {res.get('t') if res else None})")
            return
        results = res.get("results", [])
        all_self = bool(results) and all(
            r.get("node") in (ID_A, None) and r.get("node") != ID_B for r in results)
        rep.check("a routed request runs on the server itself, never on a peer",
                  all_self, "MUST", "11-trust-and-balancing#the-server-role",
                  f"results={[(r.get('slot'), (r.get('node') or '')[:6], r.get('status')) for r in results]}")
        time.sleep(1.0)
        rep.check("the peer received NO dispatch from the server node",
                  not peer.jobs, "MUST", "11-trust-and-balancing#the-server-role",
                  f"peer.jobs={len(peer.jobs)}")
        # An explicit peer target is refused rather than dispatched.
        try:
            sess = scn.candidate.open_ctl()
            res2 = sess.command(codec.dispatch_route("review", "e2e", target=ID_B), timeout=10.0)
            sess.close()
        except OSError:
            res2 = None
        if res2 and res2.get("t") == "dispatch-result":
            refused = all(r.get("status") != "spawned" for r in res2.get("results", []))
            rep.check("an explicit peer target is refused (server never pushes work out)",
                      refused, "MUST", "11-trust-and-balancing#the-server-role",
                      f"results={[(r.get('status'), r.get('reason')) for r in res2.get('results', [])]}")


def case_j_api_key_dispatch(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("J2", "API key gates inbound dispatch: declined without, spawned with (11)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, platform="linux", tier=4,
                   loopback=ctx.loopback, api_key="sekret-key")
    scn.add_peer(id=ID_B, name="peer", platform="macos", tier=1, trust_peer=True)
    with scn:
        if not _need_port(rep, scn):
            return
        peer = scn.mesh.peers[0]
        if not wait_until(lambda: peer.linked, 8.0):
            rep.skip_case("peer never linked (api-key gate applies only to dispatch, not join)")
            return
        # No apiKey on the dispatch → refused as declined, reason names the key.
        job = Job(id="j2-nokey", duty="review", prompt="x", requested_by=ID_B,
                  requested_at=time.time())
        no_key = _dispatch_status(peer, job)
        rep.check("inbound dispatch WITHOUT the API key is declined",
                  no_key is not None and no_key.get("status") == "declined", "MUST",
                  "11-trust-and-balancing#the-api-key",
                  f"status={no_key.get('status') if no_key else None} "
                  f"reason={no_key.get('reason') if no_key else ''}")
        # Correct apiKey → the request runs.
        job2 = Job(id="j2-withkey", duty="review", prompt="y", requested_by=ID_B,
                   requested_at=time.time())
        peer.send(codec.dispatch_job(job2, api_key="sekret-key"))
        with_key = wait_until(lambda: next((m for m in peer.messages("job-status")
                                            if m.get("id") == job2.id), None), 6.0)
        rep.check("inbound dispatch WITH the matching API key is spawned",
                  with_key is not None and with_key.get("status") == "spawned", "MUST",
                  "11-trust-and-balancing#the-api-key",
                  f"status={with_key.get('status') if with_key else None}")


def case_j_api_key_ctl(rep: Reporter, ctx: Context) -> None:
    rep.begin_case("J3", "API key gates the control session: wrong/absent key is refused (11)")
    scn = Scenario(ctx.node_cmd, ctx.model, candidate_id=ID_A, platform="linux", tier=4,
                   loopback=ctx.loopback, api_key="sekret-key")
    with scn:
        if not _need_port(rep, scn):
            return
        # The Scenario wires the correct key into the candidate's ctl helper.
        right = scn.candidate.ctl_status(timeout=4.0)
        if right is None:
            rep.skip_case("candidate serves no control session to gate")
            return
        rep.check("correct API key opens a control session", right is not None, "MUST",
                  "11-trust-and-balancing#the-api-key")
        scn.candidate.api_key = "wrong-key"
        wrong = scn.candidate.ctl_status(timeout=4.0)
        scn.candidate.api_key = ""
        absent = scn.candidate.ctl_status(timeout=4.0)
        scn.candidate.api_key = "sekret-key"
        rep.check("a wrong API key is refused (no snapshot returned)", wrong is None, "MUST",
                  "11-trust-and-balancing#the-api-key")
        rep.check("an absent API key is refused on an API-key server", absent is None, "MUST",
                  "11-trust-and-balancing#the-api-key")


# MARK: - registry


SUITES = {
    "A": [case_a_beacon, case_a_dial_rule, case_a_wait_rule],
    "B": [case_b_handshake, case_b_tolerance, case_b_liveness, case_b_freshness],
    "C": [case_c_placement, case_c_override, case_c_tokens, case_c_shortfall],
    "D": [case_d_executor, case_d_router, case_d_failover, case_d_unknown_duty],
    "E": [case_e_wrong_secret_peer, case_e_wrong_secret_ctl,
          case_e_fence_bypass_secret, case_e_fence_bypass_open],
    "F": [case_f_emitted],
    "G": [case_g_snapshot],
    "H": [case_h_overrides_lww],
    "I": [case_i_empty_allowlist_trust, case_i_proof_of_possession, case_i_keyless_foreign,
          case_i_requester_from_link, case_i_declined_failover, case_i_surplus_first,
          case_i_omit_when_empty],
    "J": [case_j_server_no_dispatch, case_j_api_key_dispatch, case_j_api_key_ctl],
}

CATEGORY_TITLES = {
    "A": "Discovery & linking",
    "B": "Handshake, framing tolerance, gossip, liveness",
    "C": "Deterministic placement (V1)",
    "D": "Dispatch (V4)",
    "E": "The join fence (V5)",
    "F": "Codec conformance on emitted messages (V2)",
    "G": "State snapshot shape",
    "H": "Overrides last-writer-wins (V3)",
    "I": "Trust & load balancing (ch 11)",
    "J": "Server role & API key (ch 11)",
}
