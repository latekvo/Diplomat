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
from test_agent_state import AT_PROMPT, CASES, T0, WORKING, ev, proc, rec

CORE_BIN = os.environ.get("DIPLOMAT_CORE_BIN")
pytestmark = pytest.mark.skipif(
    not CORE_BIN,
    reason="DIPLOMAT_CORE_BIN not set (build it with "
           "packages/diplomat-platform/linux/install/build-core.sh)")

#: The cap the fixture compares free slots against. Two, so a single occupied bay is
#: neither zero nor the whole cap and an off-by-one shows up.
LIMIT = 2


def _payload(records, evidence, now=T0, limit=LIMIT) -> dict:
    return {
        "now": now,
        "limit": limit,
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


def _python(records, evidence, now=T0, limit=LIMIT) -> dict:
    t = A.tick(records, evidence, now, limit)
    return {
        "rows": [{"runId": r.run_id, "state": s.state, "reason": s.reason}
                 for r, s in t.rows],
        "capLoad": sorted(t.cap_load),
        "retirable": sorted(r.run_id for r in t.retirable),
        "freeSlots": t.free_slots,
        "inFlight": {str(pr): t.in_flight(pr)
                     for pr in {r.pr_number for r in t.records
                                if r.pr_number is not None}},
        # quietDigest is compared because the two languages COMPUTE it, rather than
        # merely carrying it: it is persisted into the one book both front-ends read,
        # so a digest that differed would restart the stillness clock on every
        # hand-over. The mixed fixture gives several runs a screen, so a drift in
        # either implementation of `pane_digest` fails here.
        "records": [{"runId": r.run_id, "claimSeenAt": r.claim_seen_at,
                     "untracked": r.untracked, "placement": r.placement,
                     "quietDigest": r.quiet_digest, "quietSince": r.quiet_since}
                    for r in t.records],
    }


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
        # No pid AND no PR: neither mechanism can look for it, so its absence is not
        # evidence — the fixture's one `unknown`.
        rec(run_id="lost", pid=None, tty="", dispatched_at=T0 - 5000, pr_number=None),
        # A pid-less run the mesh placed back here, found by the prompt scan.
        rec(run_id="mesh-no-pid", pid=None, tty="", placement=A.PLACEMENT_MESH_HERE,
            dispatched_at=T0 - 5000, pr_number=311),
    ]
    evidence = ev(
        processes={1: proc(elapsed=300), 2: proc(elapsed=400, tty="pts/4"),
                   3: proc(elapsed=500, tty="pts/5"), 4: proc(elapsed=700, tty="pts/6"),
                   5: proc(elapsed=1000, tty="pts/7")},
        tails={"pts/3": WORKING, "pts/4": AT_PROMPT, "pts/5": WORKING,
               "pts/6": WORKING, "pts/7": AT_PROMPT, "pts/9": WORKING},
        claims={"review:306:sha"},
        merged={305},
        live_agents={404: "pts/8", 311: "pts/9"},
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
    assert any(python["inFlight"].values()) and not all(python["inFlight"].values()), \
        "the dedup answer must be both True and False somewhere in the fixture"
    assert any(r["untracked"] for r in python["records"]), \
        "the untracked synthesis never ran"
    assert any(r["claimSeenAt"] is not None for r in python["records"]), \
        "no claim sighting was taken — observe_claims is untested"


def test_a_case_the_two_disagree_on_would_actually_fail(mixed_results):
    """The diff has teeth: perturbing one field of the Python answer must break the
    comparison the other tests rely on."""
    swift, python = mixed_results
    tampered = json.loads(json.dumps(python))
    tampered["rows"][0]["reason"] += " (tampered)"
    assert swift != tampered
