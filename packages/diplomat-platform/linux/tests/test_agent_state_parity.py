"""`diplomat_runtime.agentstate` vs `DiplomatCore/AgentState.swift`, over one table.

The two are two implementations of one decision, and neither can delegate to the
other: this resolver runs on the panel's 8-second poll and on every dispatch, so a
subprocess per tick is not an option. A drift would be invisible in the worst way —
both applets keep drawing rows, they just quietly disagree about whether your agent
is still running.

So every case in ``test_agent_state.py`` is driven through both here, and the whole
tick is diffed: the resolved state, the *reason text*, the display order of the rows,
the cap load, what is retirable, the free-slot count and the per-PR dedup answer.
Reasons are compared verbatim rather than loosely, because the reason is what a
future debugging session reads — two sides that agree on the verdict and disagree on
why are two sides that will diverge on the next rung.

Mirrors `test_tooldata_parity.py` / `test_telemetry_parity.py`, including the
`DIPLOMAT_CORE_BIN` skip guard.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from diplomat_runtime import agentstate as A

# The scenario table itself, so the two languages are pinned against exactly the
# cases the Python side already asserts rather than a second, drifting copy.
from test_agent_state import (
    AT_PROMPT, CASES, PAST_DEADLINE, T0, WORKING, ev, proc, rec,
)

CORE_BIN = os.environ.get("DIPLOMAT_CORE_BIN")
pytestmark = pytest.mark.skipif(
    not CORE_BIN,
    reason="DIPLOMAT_CORE_BIN not set (build it with "
           "packages/diplomat-platform/linux/install/build-core.sh)")

#: The cap the fixture compares free slots against. Two, so a single occupied bay is
#: neither zero nor the whole cap and an off-by-one shows up.
LIMIT = 2


def _payload(records, evidence, now=T0, limit=LIMIT,
             deadline=A.RUN_DEADLINE) -> dict:
    return {
        "now": now,
        "limit": limit,
        "deadline": deadline,
        "records": [r.to_json() for r in records],
        "evidence": evidence.to_json(),
    }


def _swift(payload: dict) -> dict:
    proc_ = subprocess.run([CORE_BIN, "agent-state"],
                           input=json.dumps(payload).encode("utf-8"),
                           capture_output=True, timeout=60, check=False)
    assert proc_.returncode == 0, \
        f"diplomat-core agent-state failed: {proc_.stderr.decode('utf-8', 'replace')}"
    return json.loads(proc_.stdout)


def _python(records, evidence, now=T0, limit=LIMIT,
            deadline=A.RUN_DEADLINE) -> dict:
    t = A.tick(records, evidence, now, limit, deadline)
    return {
        "rows": [{"runId": r.run_id, "state": s.state, "reason": s.reason,
                  "wedged": s.wedged, "expired": s.expired,
                  "unfindable": s.unfindable, "lapsed": s.lapsed}
                 for r, s in t.rows],
        "capLoad": sorted(t.cap_load),
        "retirable": sorted(r.run_id for r in t.retirable),
        "reapable": sorted(r.run_id for r in t.reapable),
        "freeSlots": t.free_slots,
        "constants": {"runDeadline": A.RUN_DEADLINE,
                      "quietTimeout": A.QUIET_TIMEOUT,
                      "spawnGrace": A.SPAWN_GRACE},
        "inFlight": {str(pr): t.in_flight(pr)
                     for pr in {r.pr_number for r in t.records
                                if r.pr_number is not None}},
        # quietDigest is compared because the two languages COMPUTE it, rather than
        # merely carrying it: it is persisted into the one book both front-ends read,
        # so a digest that differed would restart the stillness clock on every
        # hand-over. The mixed fixture gives several runs a screen, so a drift in
        # either implementation of `pane_digest` fails here. reapRefusedAt is merely
        # carried, and is here because a decode that dropped it would silence nothing
        # and show nothing: the applet that lost it would simply never wait before
        # retrying a window it cannot close, and would go on retiring the run. pid, tty,
        # dispatchedAt and source are what a released record inherits from the run it
        # replaces, and the pid is its identity from then on.
        "records": [{"runId": r.run_id, "claimSeenAt": r.claim_seen_at,
                     "untracked": r.untracked, "released": r.released,
                     "source": r.source,
                     "pid": r.pid, "tty": r.tty, "dispatchedAt": r.dispatched_at,
                     "placement": r.placement,
                     "quietDigest": r.quiet_digest, "quietSince": r.quiet_since,
                     "reapRefusedAt": r.reap_refused_at}
                    for r in t.records],
    }


def _python_decoding(payload: dict) -> dict:
    """The same answer, but with Python's own decoders in the path rather than the
    objects the payload was built from.

    Every other run here hands Python the records and evidence directly, which is right
    for diffing the RESOLVER but leaves the two decoders of this wire format untested
    against each other. They are the other half of the parity claim: two sides that read
    one payload differently disagree before a rung has run.
    """
    return _python([A.RunRecord.from_json(r) for r in payload["records"]],
                   A.Evidence.from_json(payload["evidence"]),
                   now=payload["now"], limit=payload["limit"],
                   deadline=payload["deadline"])


#: Values a hand-written payload puts where a JSON boolean belongs. `1` and `1.0` are
#: the ones that mattered: `JSONSerialization` bridges a JSON number to `NSNumber`, and
#: `NSNumber(1) as? Bool` is `true`, so this exact fixture used to resolve `finished`
#: and reapable on the Swift side and `running` on the Python one.
_NOT_A_FLAG = [1, 1.0, 0, 2, "true", "", None, []]


@pytest.mark.parametrize("value", _NOT_A_FLAG, ids=[repr(v) for v in _NOT_A_FLAG])
def test_a_token_reading_that_is_not_a_boolean_is_read_the_same_by_both(value):
    """The deadline's precondition, and the one flag both decoders are strict about by
    name. A run five hours old with a positive reading is finished, reaped and priced;
    with anything else it is a run that is still going."""
    record = rec(run_id="r1", pid=1, tty="pts/3", dispatched_at=T0 - PAST_DEADLINE,
                 pr_number=301)
    evidence = ev(processes={1: proc(elapsed=PAST_DEADLINE)},
                  tails={"pts/3": WORKING}, tokens=True)
    payload = _payload([record], evidence)
    payload["evidence"]["tokensLeft"]["value"] = value

    swift, python = _swift(payload), _python_decoding(payload)
    assert swift == python
    assert python["reapable"] == [], "only a real reading may end a run on the clock"


@pytest.mark.parametrize("value", _NOT_A_FLAG, ids=[repr(v) for v in _NOT_A_FLAG])
def test_a_record_flag_that_is_not_a_boolean_is_read_the_same_by_both(value):
    """`untracked` decides whether the deadline may look at a record at all, so it is
    the same hazard one field over."""
    record = rec(run_id="r1", pid=1, tty="pts/3", dispatched_at=T0 - PAST_DEADLINE,
                 pr_number=301)
    evidence = ev(processes={1: proc(elapsed=PAST_DEADLINE)},
                  tails={"pts/3": WORKING}, tokens=True)
    payload = _payload([record], evidence)
    payload["records"][0]["untracked"] = value

    assert _swift(payload) == _python_decoding(payload)


@pytest.mark.parametrize("wide", ["1e300", "99999999999999999999", "1e19"])
def test_a_wide_integer_in_a_record_is_read_the_same_by_both(wide):
    """The CLI decodes its records by a hand of its own, and every other payload here
    carries records Python normalized first. A pid or PR number outside Int64 is
    absent on both sides - the run has no pid, and the PR is not in flight - while
    a finite double stays what it is."""
    raw = json.loads(f'{{"runId": "r1", "dispatchedAt": {T0 - 60}, "tty": "pts/3", '
                     f'"pid": {wide}, "prNumber": {wide}, "quietSince": 1e300}}')
    # No screen: a read one restarts the stillness clock, and the clock is what
    # shows a finite double staying what it is.
    payload = _payload([], ev(processes={4242: proc()}, tokens=True))
    payload["records"] = [raw]

    swift, python = _swift(payload), _python_decoding(payload)
    assert swift == python
    assert python["inFlight"] == {}
    assert python["records"][0]["quietSince"] == 1e300


@pytest.mark.parametrize("key", ["pid", "dispatchedAt", "prNumber"])
def test_a_record_number_that_is_a_boolean_is_read_the_same_by_both(key):
    """The other direction: `true` is not pid 1, nor a run dispatched in 1970. Both
    decoders see a `Flag`/`bool` there and read the field's default."""
    record = rec(run_id="r1", pid=1, tty="pts/3", dispatched_at=T0 - PAST_DEADLINE,
                 pr_number=1)
    evidence = ev(processes={1: proc(elapsed=PAST_DEADLINE)},
                  tails={"pts/3": WORKING}, tokens=True)
    payload = _payload([record], evidence)
    payload["records"][0][key] = True

    assert _swift(payload) == _python_decoding(payload)


def test_the_real_booleans_still_survive_both_decoders():
    """Anti-vacuity for the two above: strictness that dropped every flag would agree
    just as well, and say nothing."""
    record = rec(run_id="r1", pid=1, tty="pts/3", dispatched_at=T0 - PAST_DEADLINE,
                 pr_number=301)
    evidence = ev(processes={1: proc(elapsed=PAST_DEADLINE)},
                  tails={"pts/3": WORKING}, tokens=True)
    payload = _payload([record], evidence)

    swift, python = _swift(payload), _python_decoding(payload)
    assert swift == python
    assert python["reapable"] == ["r1"], "a genuine reading must still arm the deadline"


@pytest.mark.parametrize("name,record,evidence,_state,_reason", CASES,
                         ids=[c[0] for c in CASES])
def test_each_scenario_resolves_identically(name, record, evidence, _state, _reason):
    payload = _payload([record], evidence)
    assert _swift(payload) == _python([record], evidence), name


# MARK: - One fixture that exercises every projection at once
#
# The per-case runs above each hold a single record, so they cannot catch a
# disagreement about ORDER, about which records are summed into the cap, or about the
# claim-then-synthesize sequence. This one does.


def _mixed():
    """Records covering every state and every placement/source combination, plus a
    live PR with no record so the untracked synthesis runs."""
    records = [
        rec(run_id="working", pid=1, dispatched_at=T0 - 300, pr_number=301),
        rec(run_id="at-prompt", pid=2, tty="pts/4", dispatched_at=T0 - 400,
            pr_number=302),
        rec(run_id="clicked", pid=3, tty="pts/5", source=A.SOURCE_PANEL,
            dispatched_at=T0 - 500, pr_number=303),
        rec(run_id="exited", pid=99, dispatched_at=T0 - 600, pr_number=304),
        rec(run_id="landed", pid=4, tty="pts/6", dispatched_at=T0 - 700,
            pr_number=305),
        rec(run_id="on-a-peer", placement=A.PLACEMENT_MESH_PEER, node="brick",
            work_key="review:306:sha", pid=None, tty="", dispatched_at=T0 - 800,
            pr_number=306, claim_seen_at=None),
        rec(run_id="peer-gone", placement=A.PLACEMENT_MESH_PEER, node="brick",
            work_key="review:307:sha", pid=None, tty="", dispatched_at=T0 - 900,
            pr_number=307, claim_seen_at=T0 - 200),
        rec(run_id="mesh-here", placement=A.PLACEMENT_MESH_HERE, pid=5, tty="pts/7",
            work_key="review:308:sha", dispatched_at=T0 - 1000, pr_number=308),
        rec(run_id="just-spawned", pid=None, dispatched_at=T0 - 3, pr_number=309),
        # Mesh-here, so no pid file was ever owed, and no PR for the scan to look
        # for: nothing can be asked about it — the fixture's one `unknown`.
        rec(run_id="lost", placement=A.PLACEMENT_MESH_HERE, pid=None, tty="",
            dispatched_at=T0 - 5000, pr_number=None),
        # Local and pid-less long past the grace: the spawn's own report that its
        # terminal never ran the command. Carries a PR the scan cannot find, so the
        # two sides also have to agree on which of the two rungs answers first.
        rec(run_id="never-ran", pid=None, tty="", dispatched_at=T0 - 5000,
            pr_number=310),
        # A pid-less run the mesh placed back here, found by the prompt scan.
        rec(run_id="mesh-no-pid", pid=None, tty="", placement=A.PLACEMENT_MESH_HERE,
            dispatched_at=T0 - 5000, pr_number=311),
        # Alive, working, and past the deadline — so the fixture carries the one rung
        # that overrules a screen rather than reading it, and the two sides have to
        # agree on the age it quotes as well as on the verdict.
        rec(run_id="over-deadline", pid=6, tty="pts/10",
            dispatched_at=T0 - PAST_DEADLINE, pr_number=312),
        # Its CLI reported the turn over with its agent still up and seen by the scan,
        # so this tick also releases that agent.
        rec(run_id="reported", pid=7, tty="pts/11", dispatched_at=T0 - 1100,
            pr_number=313),
        # Untracked, working, and first seen past the deadline: RUNNING without a bay.
        rec(run_id="untracked:314", pid=None, tty="pts/12", untracked=True,
            dispatched_at=T0 - PAST_DEADLINE, pr_number=314),
        # Released on an earlier tick, and somebody typed into it since. The scan names a
        # second session on its PR, which a pid-held record does not follow.
        rec(run_id="untracked:315", pid=8, tty="pts/13", untracked=True, released=True,
            dispatched_at=T0 - 1200, pr_number=315),
        # Reported its turn over on a PR "working" still holds: released all the same.
        rec(run_id="reported-beside", pid=9, tty="pts/15", dispatched_at=T0 - 1300,
            pr_number=301),
        # The same with no pid, which only the scan could hold it to: not released.
        rec(run_id="pidless-beside", pid=None, tty="", dispatched_at=T0 - 1600,
            pr_number=301),
        # Reported over beside the released "untracked:315", whose run id is taken.
        rec(run_id="reported-past-released", pid=12, tty="pts/19",
            dispatched_at=T0 - 1250, pr_number=315),
        # A peer's run whose PR landed seconds after dispatch, while this box's scan
        # sees a session on it: nothing is released.
        rec(run_id="peer-landed", placement=A.PLACEMENT_MESH_PEER, node="brick",
            work_key="review:316:sha", pid=None, tty="", dispatched_at=T0 - 10,
            pr_number=316, claim_seen_at=T0 - 1),
        # Reported over after twenty still minutes: its agent is released and closed on
        # this tick.
        rec(run_id="reported-still", pid=10, tty="pts/17", dispatched_at=T0 - 1400,
            pr_number=317, quiet_digest=A.pane_digest(AT_PROMPT),
            quiet_since=T0 - A.QUIET_TIMEOUT),
        # The same, clicked, and its window refused a close a minute ago: the released
        # agent keeps the click's source and waits out the refusal.
        rec(run_id="reported-refused", pid=11, tty="pts/18", source=A.SOURCE_PANEL,
            dispatched_at=T0 - 1500, pr_number=318,
            quiet_digest=A.pane_digest(AT_PROMPT), quiet_since=T0 - A.QUIET_TIMEOUT,
            reap_refused_at=T0 - 60),
        # Placed here by the mesh on a released agent's PR, which the scan still names
        # by the released agent's tty: it adopts none.
        rec(run_id="untracked:319", pid=13, tty="pts/20", untracked=True, released=True,
            dispatched_at=T0 - 1700, pr_number=319),
        rec(run_id="placed-beside-released", pid=None, tty="",
            placement=A.PLACEMENT_MESH_HERE, dispatched_at=T0 - 30, pr_number=319),
        # A pid-less untracked record whose PR's sighting is another record's tty: it
        # stays where it is.
        rec(run_id="untracked:320", pid=None, tty="pts/21", untracked=True,
            dispatched_at=T0 - 1800, pr_number=320),
        rec(run_id="placed-holding", pid=None, tty="pts/22",
            placement=A.PLACEMENT_MESH_HERE, dispatched_at=T0 - 40, pr_number=320),
    ]
    evidence = ev(
        processes={1: proc(elapsed=300), 2: proc(elapsed=400, tty="pts/4"),
                   3: proc(elapsed=500, tty="pts/5"), 4: proc(elapsed=700, tty="pts/6"),
                   5: proc(elapsed=1000, tty="pts/7"),
                   6: proc(elapsed=PAST_DEADLINE, tty="pts/10"),
                   7: proc(elapsed=1100, tty="pts/11"),
                   8: proc(elapsed=1200, tty="pts/13"),
                   9: proc(elapsed=1300, tty="pts/15"),
                   10: proc(elapsed=1400, tty="pts/17"),
                   11: proc(elapsed=1500, tty="pts/18"),
                   12: proc(elapsed=1250, tty="pts/19"),
                   13: proc(elapsed=1700, tty="pts/20")},
        tails={"pts/3": WORKING, "pts/4": AT_PROMPT, "pts/5": WORKING,
               "pts/6": WORKING, "pts/7": AT_PROMPT, "pts/9": WORKING,
               "pts/10": WORKING, "pts/11": AT_PROMPT, "pts/12": WORKING,
               "pts/13": WORKING, "pts/15": AT_PROMPT, "pts/16": WORKING,
               "pts/17": AT_PROMPT, "pts/18": AT_PROMPT, "pts/19": AT_PROMPT,
               "pts/20": AT_PROMPT, "pts/21": AT_PROMPT, "pts/22": WORKING},
        claims={"review:306:sha", "review:316:sha"},
        merged={305, 316},
        live_agents={404: "pts/8", 311: "pts/9", 313: "pts/11", 314: "pts/12",
                     315: "pts/14", 301: "pts/15", 316: "pts/16", 317: "pts/17",
                     318: "pts/18", 319: "pts/20", 320: "pts/22"},
        activity={"reported": ("idle", T0 - 5), "reported-beside": ("idle", T0 - 5),
                  "pidless-beside": ("idle", T0 - 5),
                  "reported-past-released": ("idle", T0 - 5),
                  "reported-still": ("idle", T0 - 5),
                  "reported-refused": ("idle", T0 - 5)},
    )
    return records, evidence


@pytest.fixture(scope="module")
def mixed_results():
    records, evidence = _mixed()
    return _swift(_payload(records, evidence)), _python(records, evidence)


def test_the_whole_tick_agrees(mixed_results):
    swift, python = mixed_results
    assert swift == python


def test_the_fixture_actually_reaches_every_state(mixed_results):
    """Anti-vacuity: a fixture that only ever produces `running` would diff clean
    while every other rung drifted freely."""
    _swift_out, python = mixed_results
    assert {r["state"] for r in python["rows"]} == set(A.STATE_ORDER)


def test_the_fixture_exercises_every_projection(mixed_results):
    """Anti-vacuity, again: each projection has to be non-trivial, or agreeing about
    it proves nothing."""
    _swift_out, python = mixed_results
    assert python["capLoad"], "no run holds a bay — the cap projection is untested"
    assert python["retirable"], "nothing retires — the retirement projection is untested"
    assert python["reapable"], "no window is reaped — the destructive projection is untested"
    assert any(python["inFlight"].values()) and not all(python["inFlight"].values()), \
        "the dedup answer must be both True and False somewhere in the fixture"
    assert any(r["untracked"] for r in python["records"]), \
        "the untracked synthesis never ran"
    assert any(r["claimSeenAt"] is not None for r in python["records"]), \
        "no claim sighting was taken — observe_claims is untested"
    assert any(r["runId"] == "untracked:313" and r["released"] and r["pid"] == 7
               for r in python["records"]), "no ended run released its agent"
    heirs = {r["runId"]: r["pid"] for r in python["records"] if r["released"]}
    assert (heirs.get("untracked:301"), heirs.get("untracked:315:12")) == (9, 12), \
        "no run that ended beside another record released its agent"
    assert None not in heirs.values(), "a pid-less run was released beside another"
    assert any(r["lapsed"] for r in python["rows"]), "no untracked bay lapsed"
    assert python["inFlight"]["315"] is False and "untracked:315" not in python["capLoad"], \
        "a released agent mid-turn must hold neither its PR nor a bay"
    assert "untracked:317" in python["reapable"], "no released agent was closed at once"
    refused = next(r for r in python["records"] if r["runId"] == "untracked:318")
    assert (refused["source"], refused["reapRefusedAt"]) == (A.SOURCE_PANEL, T0 - 60)
    assert "untracked:318" not in python["reapable"], \
        "a released agent must wait out its run's refused close"
    ttys = {r["runId"]: r["tty"] for r in python["records"]}
    assert (ttys["placed-beside-released"], ttys["untracked:320"]) == ("", "pts/21"), \
        "a pid-less record took another record's tty off the scan"


def test_the_tick_after_a_release_agrees_too():
    """The release only pays off on the NEXT tick, when the run is gone from the book
    and the scan still sees its agent. Fed the book both stores would leave, the two
    sides must agree that nothing is re-booked and no bay is taken."""
    records, evidence = _mixed()
    t = A.tick(records, evidence, T0, LIMIT, A.RUN_DEADLINE)
    gone = {r.run_id for r in t.retirable}
    book = [r for r in t.records if r.run_id not in gone]
    later = T0 + 8
    swift, again = _swift(_payload(book, evidence, now=later)), \
        _python(book, evidence, now=later)
    assert swift == again
    assert [r["runId"] for r in again["records"]].count("untracked:313") == 1
    assert "untracked:313" not in again["capLoad"] and again["inFlight"]["313"] is False


def test_the_deadline_being_off_agrees_too():
    """The switch is the caller's, not the resolver's, so it is a parity surface of its
    own: a side that read an absent deadline as zero would retire the whole fixture."""
    records, evidence = _mixed()
    payload = _payload(records, evidence, deadline=None)
    del payload["deadline"]  # absent, the way a front-end with the switch off sends it
    assert _swift(payload) == _python(records, evidence, deadline=None)


def test_the_fixture_reaches_the_deadline_rung(mixed_results):
    """Anti-vacuity: the fixture must actually contain a run the deadline ended, or the
    rung is unpinned in both languages at once."""
    _swift_out, python = mixed_results
    assert any(r["runId"] == "over-deadline" and "deadline" in r["reason"]
               for r in python["rows"])


def test_a_case_the_two_disagree_on_would_actually_fail(mixed_results):
    """The diff has teeth: perturbing one field of the Python answer must break the
    comparison the other tests rely on."""
    swift, python = mixed_results
    tampered = json.loads(json.dumps(python))
    tampered["rows"][0]["reason"] += " (tampered)"
    assert swift != tampered
