"""The scenario table behind :mod:`diplomat_runtime.agentstate`.

One named case per situation the resolver has to get right, each fed through
``resolve_one`` and asserted on the state AND the reason — the reason because it is
what the debug dump prints, and a rung that reaches the right answer by the wrong
route is a rung that will reach the wrong answer next time.

Every case here is also a parity case: ``tests/test_agent_state_parity.py`` feeds the
same table through ``diplomat-core agent-state`` and diffs, so the Swift twin cannot
answer any of them differently.

The cases are grouped by the claim they defend. The two that matter most, and that
this whole module exists for:

- ``*_unavailable_*`` — a probe that could not answer never yields FINISHED. Reading
  "I could not look" as "it is gone" is what produced already-complete verdicts on
  agents that were still working.
- ``peer_*`` — a run on another machine is judged by the mesh claim, never by a local
  process table that structurally cannot see it.
"""

from __future__ import annotations

import dataclasses

import pytest

from diplomat_runtime import agentstate as A
from diplomat_runtime import completion

# A fixed clock. Every offset below is integral so the formatted seconds in a reason
# string are the same text in both languages (see the parity test).
T0 = 1_000_000.0

# Real CLI buffers: the interrupt hint on the live status bar means mid-turn, its
# absence means back at the prompt.
WORKING = "● Reading files…\n⏵⏵ bypass permissions on · esc to interrupt · ← for agents"
AT_PROMPT = "● Posted the review.\n❯\n⏵⏵ bypass permissions on (shift+tab to cycle)"

# The same finished screen as a terminal actually dumps it on a box whose shells wrap
# themselves in tmux: the multiplexer's status line, wall clock and all, sits under the
# agent's own output. Those five characters are the only thing on it that moves.
TMUX_WRAPPED = AT_PROMPT + '\n[159] 0:zsh*  "OpenCode" 16:31 24-sie-26'


def rec(**kw) -> A.RunRecord:
    """A local, auto-triggered run dispatched 60s ago on PR #337, pid 4242, tty
    pts/3 — overridden per case."""
    base = dict(run_id="r1", dispatched_at=T0 - 60, pr_number=337,
                pr_url="https://github.com/o/r/pull/337", kind="review",
                label="Auto · Review · #337", source=A.SOURCE_AUTO,
                placement=A.PLACEMENT_LOCAL, pid=4242, tty="pts/3")
    base.update(kw)
    return A.RunRecord(**base)


def proc(elapsed: float = 60.0, tty: str = "pts/3", is_agent: bool = True):
    return A.ProcInfo(tty=tty, elapsed=elapsed, is_agent=is_agent)


def ev(*, processes=None, sentinels=None, tails=None, claims=None,
       merged=None, live_agents=None, sessions=None, activity=None) -> A.Evidence:
    """An evidence bundle where anything not named is PRESENT-and-empty.

    Empty, not unavailable: these cases are about a machine that was successfully
    looked at and had nothing on it. A case that wants "the probe failed" says so by
    passing an explicit ``Observation.unavailable``.
    """
    def obs(v, empty):
        if v is None:
            return A.Observation.present(empty)
        return v if isinstance(v, A.Observation) else A.Observation.present(v)

    return A.Evidence(
        processes=obs(processes, {}),
        sentinels=obs(sentinels, set()),
        tails=obs(tails, {}),
        claims=obs(claims, set()),
        merged_prs=obs(merged, set()),
        live_agents=obs(live_agents, {}),
        sessions=obs(sessions, {}),
        activity=obs(activity, {}),
    )


# (name, record, evidence, expected state, expected substring of the reason)
CASES = [
    # --- terminal outcomes, in precedence order -----------------------------
    ("merged outranks a live process",
     rec(), ev(processes={4242: proc()}, tails={"pts/3": WORKING}, merged={337}),
     A.MERGED, "merged"),
    ("the completion sentinel ends a run whatever else is true",
     rec(), ev(processes={4242: proc()}, tails={"pts/3": WORKING},
               sentinels={"r1"}),
     A.FINISHED, "sentinel"),
    ("a sentinel for another run does not end this one",
     rec(), ev(processes={4242: proc()}, sentinels={"someone-else"}),
     A.RUNNING, "pid 4242 alive"),

    # --- a local run, judged by its pid -------------------------------------
    ("a live pid whose screen shows the interrupt hint is working",
     rec(), ev(processes={4242: proc()}, tails={"pts/3": WORKING}),
     A.RUNNING, "working"),
    ("a live pid back at its prompt is awaiting input",
     rec(), ev(processes={4242: proc()}, tails={"pts/3": AT_PROMPT}),
     A.AWAITING_INPUT, "at the prompt"),
    # The same bare prompt, seconds after dispatch, is an agent that has not started
    # its first turn rather than one that has finished its last. Read as idle it hands
    # its bay straight back to the poll that filled it, and the next dispatch of that
    # poll is seconds behind — which is a cap of 1 running two agents.
    ("a live pid whose first turn has not reached the screen yet is not idle",
     rec(dispatched_at=T0 - 6), ev(processes={4242: proc(elapsed=1)},
                                   tails={"pts/3": AT_PROMPT}),
     A.RUNNING, "dispatched 6s ago, no turn on screen yet"),

    # --- a run that reports its own turn boundaries -------------------------
    #
    # The mechanism that finally answers the question. A finished agent is alive at
    # its prompt, so every rung above this one sees exactly what a working agent
    # shows; the CLI saying so itself is the only thing that separates them.
    ("its CLI reporting the turn over ends a run whose process is still alive",
     rec(), ev(processes={4242: proc()}, tails={"pts/3": AT_PROMPT},
               activity={"r1": ("idle", T0 - 5)}),
     A.FINISHED, "its CLI reported the turn over"),
    ("its CLI reporting a turn in flight outranks a screen that looks idle",
     rec(), ev(processes={4242: proc()}, tails={"pts/3": AT_PROMPT},
               activity={"r1": ("busy", T0 - 5)}),
     A.RUNNING, "its CLI reported a turn in flight"),
    ("a session that ended outright is over too",
     rec(), ev(processes={4242: proc()}, tails={"pts/3": WORKING},
               activity={"r1": ("ended", T0 - 5)}),
     A.FINISHED, "its CLI reported the session ended"),
    ("another run's report says nothing about this one",
     rec(), ev(processes={4242: proc()}, tails={"pts/3": WORKING},
               activity={"other": ("idle", T0 - 5)}),
     A.RUNNING, "working"),
    ("a run that reports nothing is still read off its screen",
     rec(), ev(processes={4242: proc()}, tails={"pts/3": WORKING},
               activity=A.Observation.unavailable("could not be read")),
     A.RUNNING, "working"),

    # --- the quiescence backstop, for the runs no report reaches ------------
    ("a screen unchanged for twenty minutes is over whatever the status bar says",
     rec(quiet_digest="d", quiet_since=T0 - A.QUIET_TIMEOUT),
     ev(processes={4242: proc()}, tails={"pts/3": WORKING}),
     A.FINISHED, "its screen has not changed in 20m"),
    ("a screen still for nineteen minutes is not over yet",
     rec(quiet_digest="d", quiet_since=T0 - A.QUIET_TIMEOUT + 60),
     ev(processes={4242: proc()}, tails={"pts/3": WORKING}),
     A.RUNNING, "working"),
    ("stillness outranks the CLI's own claim to be working, which is the point",
     rec(quiet_digest="d", quiet_since=T0 - A.QUIET_TIMEOUT),
     ev(processes={4242: proc()}, tails={"pts/3": WORKING},
        activity={"r1": ("busy", T0 - 3000)}),
     A.FINISHED, "its screen has not changed in 20m"),

    # --- a run that serves its own session, which outranks its screen -------
    #
    # The session's answer is positive evidence — a runner saying so — where the screen
    # is an inference from whether someone else's interrupt hint was drawn. The two
    # disagreeing cases are the ones that matter: they are what a redrawn or reworded
    # status bar looks like, and each is a mistake the applet used to make with no way
    # to tell it was making one.
    #
    # It ENDS a run, exactly as the hook report above does. Both are the agent's own
    # word for the same fact; read as merely idle, every OpenCode and Hermes run stayed
    # in the book until somebody closed its window by hand.
    ("a session mid-turn is working even though its screen looks idle",
     rec(), ev(processes={4242: proc()}, tails={"pts/3": AT_PROMPT},
               sessions={"r1": A.SessionState(busy=True)}),
     A.RUNNING, "its session is mid-turn"),
    ("a session that finished its turn ends the run though the hint is stale",
     rec(), ev(processes={4242: proc()}, tails={"pts/3": WORKING},
               sessions={"r1": A.SessionState(busy=False)}),
     A.FINISHED, "its runner reported the turn over"),
    ("a run with no session of its own is still read off its screen",
     rec(), ev(processes={4242: proc()}, tails={"pts/3": WORKING},
               sessions={"someone-else": A.SessionState(busy=False)}),
     A.RUNNING, "working"),
    ("a session probe that could not answer falls back to the screen",
     rec(), ev(processes={4242: proc()}, tails={"pts/3": AT_PROMPT},
               sessions=A.Observation.unavailable("no run has an OpenCode server")),
     A.AWAITING_INPUT, "at the prompt"),
    ("a session answer needs no screen at all",
     rec(), ev(processes={4242: proc()},
               tails=A.Observation.unavailable("is unreadable"),
               sessions={"r1": A.SessionState(busy=False)}),
     A.FINISHED, "its runner reported the turn over"),
    ("a pid missing from a table we did read has finished",
     rec(), ev(processes={9999: proc()}),
     A.FINISHED, "absent from the process table"),
    ("a pid retaken by something that is not an agent has finished",
     rec(), ev(processes={4242: proc(is_agent=False)}),
     A.FINISHED, "recycled"),
    ("a pid retaken by a much younger agent is not adopted",
     rec(dispatched_at=T0 - 3600), ev(processes={4242: proc(elapsed=12)}),
     A.FINISHED, "12s old but the run is 3600s old"),
    ("a pid a little younger than its record is still its own agent",
     rec(), ev(processes={4242: proc(elapsed=45)}),
     A.RUNNING, "pid 4242 alive"),

    # --- the spawn window ---------------------------------------------------
    ("a just-dispatched run with no pid yet is starting",
     rec(dispatched_at=T0 - 5, pid=None), ev(),
     A.STARTING, "no pid yet"),
    # --- a run whose agent this applet did not open ------------------------
    # The mesh routes a job and the NODE opens the terminal, so the pid file it
    # writes belongs to a run directory this applet never created. Judged on the
    # prompt scan instead, or such a run reads "unknown" for ever, holds its bay
    # for ever, and can never be retired — seen in production, twice, the first
    # time the monitors ran.
    # tty as `adopt_ttys` leaves it: for a pid-less run that is whatever tty the
    # prompt scan found its agent on, which is what makes its screen readable at all.
    ("a pid-less run whose PR has a live agent is running",
     rec(placement=A.PLACEMENT_MESH_HERE, pid=None, tty="pts/5",
         dispatched_at=T0 - 600),
     ev(live_agents={337: "pts/5"}, tails={"pts/5": WORKING}),
     A.RUNNING, "an agent is up on PR #337; working"),
    ("a pid-less run whose agent is at its prompt gives its bay back too",
     rec(placement=A.PLACEMENT_MESH_HERE, pid=None, tty="pts/5",
         dispatched_at=T0 - 600),
     ev(live_agents={337: "pts/5"}, tails={"pts/5": AT_PROMPT}),
     A.AWAITING_INPUT, "an agent is up on PR #337; at the prompt"),
    ("a pid-less run whose PR has no agent in a scan that WORKED has finished",
     rec(pid=None, dispatched_at=T0 - 600), ev(live_agents={}),
     A.FINISHED, "no agent for PR #337 in the process table"),
    ("a pid-less run is not ended by a scan that failed",
     rec(pid=None, dispatched_at=T0 - 600),
     ev(live_agents=A.Observation.unavailable("ps could not be read")),
     A.UNKNOWN, "ps could not be read"),
    ("a pid-less run with no PR either cannot be looked for at all",
     rec(pid=None, pr_number=None, dispatched_at=T0 - 600), ev(live_agents={}),
     A.UNKNOWN, "no pid recorded"),

    # --- missing evidence never means finished ------------------------------
    ("an unreadable process table leaves a local run unknown",
     rec(), ev(processes=A.Observation.unavailable("ps output would not decode")),
     A.UNKNOWN, "ps output would not decode"),
    ("an unreadable screen leaves a live agent running, not idle",
     rec(), ev(processes={4242: proc()},
               tails=A.Observation.unavailable("no tmux server")),
     A.RUNNING, "no tmux server"),
    ("an agent on a tty no screen covers is running, not idle",
     rec(), ev(processes={4242: proc()}, tails={"pts/9": AT_PROMPT}),
     A.RUNNING, "no screen for tty pts/3"),
    ("an agent with no tty recorded is running, not idle",
     rec(tty=""), ev(processes={4242: proc()}, tails={"pts/3": AT_PROMPT}),
     A.RUNNING, "no screen for tty ?"),
    ("an unreadable merge probe falls through to the process evidence",
     rec(), ev(processes={4242: proc()}, tails={"pts/3": AT_PROMPT},
               merged=A.Observation.unavailable("gh failed")),
     A.AWAITING_INPUT, "at the prompt"),
    ("an unreadable sentinel probe falls through to the process evidence",
     rec(), ev(processes={4242: proc()},
               sentinels=A.Observation.unavailable("run dir unreadable")),
     A.RUNNING, "pid 4242 alive"),

    # --- a mesh-peer run, judged by the claim -------------------------------
    ("a held claim means a peer's agent is running",
     rec(placement=A.PLACEMENT_MESH_PEER, work_key="review:337:abc", node="brick",
         pid=None, tty=""),
     ev(claims={"review:337:abc"}),
     A.RUNNING, "claim held on brick"),
    ("an empty local process table never retires a peer's run",
     rec(placement=A.PLACEMENT_MESH_PEER, work_key="review:337:abc", node="brick",
         dispatched_at=T0 - 3600, claim_seen_at=T0 - 10, pid=None, tty=""),
     ev(processes={}, claims={"review:337:abc"}),
     A.RUNNING, "claim held on brick"),
    ("an unreadable claim book leaves a peer's run unknown, NOT finished",
     rec(placement=A.PLACEMENT_MESH_PEER, work_key="review:337:abc",
         claim_seen_at=T0 - 10, pid=None, tty=""),
     ev(claims=A.Observation.unavailable("mesh node not running")),
     A.UNKNOWN, "mesh node not running"),
    ("a peer run whose claim has not appeared yet is starting",
     rec(placement=A.PLACEMENT_MESH_PEER, work_key="review:337:abc",
         dispatched_at=T0 - 10, pid=None, tty=""),
     ev(claims=set()),
     A.STARTING, "claim not seen yet"),
    ("a peer run whose claim never appeared is finished once it has had time",
     rec(placement=A.PLACEMENT_MESH_PEER, work_key="review:337:abc",
         dispatched_at=T0 - 50, pid=None, tty=""),
     ev(claims=set()),
     A.FINISHED, "claim released 50s ago"),
    ("a claim seen recently keeps a peer run alive through one missed snapshot",
     rec(placement=A.PLACEMENT_MESH_PEER, work_key="review:337:abc",
         dispatched_at=T0 - 3600, claim_seen_at=T0 - 20, pid=None, tty=""),
     ev(claims=set()),
     A.RUNNING, "claim last seen 20s ago"),
    ("a claim released past the settle window ends a peer run",
     rec(placement=A.PLACEMENT_MESH_PEER, work_key="review:337:abc",
         dispatched_at=T0 - 3600, claim_seen_at=T0 - 50, pid=None, tty=""),
     ev(claims=set()),
     A.FINISHED, "claim released 50s ago"),
    ("an hour-long peer run is not ended by its own age",
     rec(placement=A.PLACEMENT_MESH_PEER, work_key="review:337:abc", node="brick",
         dispatched_at=T0 - 7200, claim_seen_at=T0 - 1, pid=None, tty=""),
     ev(claims={"review:337:abc"}),
     A.RUNNING, "claim held on brick"),

    # --- a mesh placement that landed back here is a local run --------------
    ("a mesh run placed back here is judged by its pid, not by the claim",
     rec(placement=A.PLACEMENT_MESH_HERE, work_key="review:337:abc"),
     ev(processes={4242: proc()}, tails={"pts/3": AT_PROMPT}, claims=set()),
     A.AWAITING_INPUT, "at the prompt"),

    # --- untracked agents ---------------------------------------------------
    #
    # The scan is the whole of the evidence about one: it is the only thing that says
    # the agent exists, so it is also the only thing that can say it has gone. The
    # record outlives the tick that made it — the stillness backstop needs a screen to
    # compare against — and one that outlived its agent too would hold that PR against
    # a fresh agent, and a bay of the cap, for the life of the applet.
    ("an untracked agent found in the table is running",
     rec(run_id="untracked:337", pid=None, tty="pts/3", untracked=True,
         dispatched_at=T0),
     ev(processes={}, tails={"pts/3": WORKING}, live_agents={337: "pts/3"}),
     A.RUNNING, "found in process table"),
    ("an untracked agent is never aged out for having no pid",
     rec(run_id="untracked:337", pid=None, tty="pts/3", untracked=True,
         dispatched_at=T0 - 99999),
     ev(processes={}, tails={"pts/3": WORKING}, live_agents={337: "pts/3"}),
     A.RUNNING, "found in process table"),
    ("an untracked agent at its prompt gives its bay back like any other",
     rec(run_id="untracked:337", pid=None, tty="pts/3", untracked=True,
         dispatched_at=T0),
     ev(processes={}, tails={"pts/3": AT_PROMPT}, live_agents={337: "pts/3"}),
     A.AWAITING_INPUT, "found in process table; at the prompt"),
    ("an untracked agent gone from the table is over",
     rec(run_id="untracked:337", pid=None, tty="pts/3", untracked=True,
         dispatched_at=T0),
     ev(processes={}, tails={"pts/3": WORKING}, live_agents={}),
     A.FINISHED, "gone from the process table"),
    ("an untracked agent whose scan could not be read is unknown",
     rec(run_id="untracked:337", pid=None, tty="pts/3", untracked=True,
         dispatched_at=T0),
     ev(processes={}, tails={"pts/3": WORKING},
        live_agents=A.Observation.unavailable("ps failed")),
     A.UNKNOWN, "the agent scan ps failed"),

    # --- the terminal's own furniture is on the screen too -------------------
    ("a screen still but for the terminal's clock is still still",
     rec(quiet_digest=A.pane_digest(TMUX_WRAPPED.replace("16:31", "16:12")),
         quiet_since=T0 - A.QUIET_TIMEOUT),
     ev(processes={4242: proc()}, tails={"pts/3": TMUX_WRAPPED}),
     A.FINISHED, "its screen has not changed in 20m"),

    # --- an hour-long local run ---------------------------------------------
    ("an hour-long local run is not ended by its own age",
     rec(dispatched_at=T0 - 7200), ev(processes={4242: proc(elapsed=7200)},
                                      tails={"pts/3": WORKING}),
     A.RUNNING, "working"),
]


@pytest.mark.parametrize("name,record,evidence,want_state,want_reason", CASES,
                         ids=[c[0] for c in CASES])
def test_resolve_one(name, record, evidence, want_state, want_reason):
    got = A.resolve_one(record, evidence, T0)
    assert got.state == want_state, f"{name}: reason was {got.reason!r}"
    assert want_reason in got.reason, name


def test_every_state_is_reachable_from_the_table():
    """A state no case produces is a state nothing pins — the table's own coverage
    gate, so a rung cannot be added without a scenario that reaches it."""
    reached = {A.resolve_one(r, e, T0).state for _n, r, e, _s, _r in CASES}
    assert reached == set(A.STATE_ORDER)


# MARK: - Stillness, and what a screen dump really carries


def test_a_screen_that_only_moved_its_clock_never_moved():
    """The backstop's one real adversary, and for a long time its silent defeat: a
    terminal dump carries the multiplexer's status line as well as the agent's output,
    and tmux's default status-right is a wall clock. So a pane showing nothing but a
    finished agent changed once a minute, the twenty-minute window restarted every
    sixty seconds, and the reaper could not fire on any box whose shells wrap
    themselves in tmux — measured on one, zero window closes in 4763 audit entries."""
    later = TMUX_WRAPPED.replace("16:31", "16:32")
    assert later != TMUX_WRAPPED
    assert A.pane_digest(later) == A.pane_digest(TMUX_WRAPPED)


def test_a_screen_whose_agent_wrote_something_new_did_move():
    """The other half: masking a time of day must not mask the agent."""
    assert A.pane_digest(TMUX_WRAPPED + "\n● Pushed.") != A.pane_digest(TMUX_WRAPPED)


@pytest.mark.parametrize("moved", [
    "5 files changed",       # a count is not a clock
    "· 1h 34m",              # nor is an elapsed turn
    "esc to interrupt · 9s",
])
def test_only_a_time_of_day_is_masked(moved):
    """Narrow on purpose. Everything else on that screen is something the agent itself
    wrote, and a digest that ignored those would call a working agent still."""
    one = AT_PROMPT + "\n" + moved
    two = one.replace("5", "6").replace("34", "35").replace("9s", "8s")
    assert A.pane_digest(one) != A.pane_digest(two)


def test_the_stillness_clock_restarts_only_when_the_screen_changes():
    r = rec(quiet_digest="", quiet_since=None)
    first = A.observe_quiescence([r], A.Observation.present({"pts/3": TMUX_WRAPPED}),
                                 T0)[0]
    assert first.quiet_since == T0
    later = TMUX_WRAPPED.replace("16:31", "16:45")
    ticked = A.observe_quiescence([first], A.Observation.present({"pts/3": later}),
                                  T0 + 900)[0]
    assert ticked.quiet_since == T0, "a clock tick restarted the twenty-minute window"


def test_a_screen_that_could_not_be_read_advances_nothing():
    """What keeps a tmux server going down from looking like twenty minutes of
    stillness across every run at once."""
    r = rec(quiet_digest="d", quiet_since=T0 - 10)
    out = A.observe_quiescence([r], A.Observation.unavailable("tmux is down"), T0)
    assert out[0].quiet_since == T0 - 10


# MARK: - Claim sightings


def test_a_seen_claim_refreshes_only_peer_runs():
    peer = rec(run_id="p", placement=A.PLACEMENT_MESH_PEER, work_key="w",
               claim_seen_at=None)
    here = rec(run_id="h", placement=A.PLACEMENT_MESH_HERE, work_key="w")
    out = {r.run_id: r for r in
           A.observe_claims([peer, here], A.Observation.present({"w"}), T0)}
    assert out["p"].claim_seen_at == T0
    assert out["h"].claim_seen_at is None


def test_an_unreadable_claim_book_ages_nothing_out():
    """The sighting is what absence is measured against, so a node we could not read
    must not advance — nor freeze — the clock by writing a bogus one."""
    peer = rec(run_id="p", placement=A.PLACEMENT_MESH_PEER, work_key="w",
               claim_seen_at=T0 - 10)
    out = A.observe_claims([peer], A.Observation.unavailable("node down"), T0)
    assert out[0].claim_seen_at == T0 - 10


# MARK: - Untracked synthesis


def test_a_live_pr_with_no_record_becomes_an_untracked_run():
    out = A.synthesize_untracked([], A.Observation.present({404: "pts/7"}), T0)
    assert [r.run_id for r in out] == ["untracked:404"]
    assert out[0].untracked and out[0].source == A.SOURCE_AUTO


def test_an_untracked_run_carries_the_tty_its_agent_was_found_on():
    """Without it nothing can read that agent's screen, so it would count as working
    until its window closed however long ago it finished — a bay held for nothing,
    which is the state the cap exists to prevent."""
    out = A.synthesize_untracked([], A.Observation.present({404: "pts/7"}), T0)
    assert out[0].tty == "pts/7"
    states = A.resolve(out, ev(tails={"pts/7": AT_PROMPT},
                               live_agents={404: "pts/7"}), T0)
    assert states["untracked:404"].state == A.AWAITING_INPUT
    assert A.cap_load(out, states) == set(), "an idle untracked agent gives its bay back"


def test_a_live_pr_that_already_has_a_record_is_not_duplicated():
    out = A.synthesize_untracked([rec(pr_number=404)],
                                 A.Observation.present({404: "pts/7"}), T0)
    assert [r.run_id for r in out] == ["r1"]


def test_a_kept_record_follows_its_prs_sighting_to_a_new_tty():
    """The scan reports one agent per PR, so a second session on that PR becomes the
    sighting the moment the first exits. A record left on the gone one has no screen
    to be judged by at all — and must not carry the old screen's stillness onto the
    new window, which would close it on somebody else's twenty minutes."""
    kept = A.synthesize_untracked([], A.Observation.present({404: "pts/7"}), T0)
    still = [dataclasses.replace(kept[0], quiet_digest="abc", quiet_since=T0)]

    (moved,) = A.synthesize_untracked(still, A.Observation.present({404: "pts/9"}),
                                      T0 + 8)

    assert moved.tty == "pts/9"
    assert (moved.quiet_digest, moved.quiet_since) == ("", None)


def test_a_sighting_with_no_tty_does_not_blank_the_one_a_record_has():
    """``ps`` reports "?" for an agent with no controlling terminal, and the scan
    passes that on as an empty tty. Keeping the screen there is is strictly better
    than swapping it for none."""
    kept = A.synthesize_untracked([], A.Observation.present({404: "pts/7"}), T0)
    (same,) = A.synthesize_untracked(kept, A.Observation.present({404: ""}), T0 + 8)
    assert same.tty == "pts/7"


def test_an_unreadable_scan_synthesizes_nothing():
    out = A.synthesize_untracked([], A.Observation.unavailable("ps failed"), T0)
    assert out == []


# MARK: - The projections


def _resolved(records, evidence, now=T0):
    return records, A.resolve(records, evidence, now)


@pytest.mark.parametrize("state_case,blocks", [
    ("a live agent blocks a second dispatch", True),
    ("an agent at its prompt blocks a second dispatch", True),
    ("an agent whose evidence is unavailable blocks a second dispatch", True),
    ("a finished agent does not block", False),
])
def test_in_flight_blocks_on_every_state_that_is_not_over(state_case, blocks):
    evidences = {
        "a live agent blocks a second dispatch":
            ev(processes={4242: proc()}, tails={"pts/3": WORKING}),
        "an agent at its prompt blocks a second dispatch":
            ev(processes={4242: proc()}, tails={"pts/3": AT_PROMPT}),
        "an agent whose evidence is unavailable blocks a second dispatch":
            ev(processes=A.Observation.unavailable("ps failed")),
        "a finished agent does not block": ev(processes={}),
    }
    records, states = _resolved([rec()], evidences[state_case])
    assert A.in_flight(records, states, 337) is blocks, state_case


def test_in_flight_is_scoped_to_the_pr_asked_about():
    records, states = _resolved([rec()], ev(processes={4242: proc()}))
    assert A.in_flight(records, states, 337) is True
    assert A.in_flight(records, states, 999) is False


def test_the_cap_counts_automatic_agents_that_run_here():
    records = [
        rec(run_id="auto-here", pid=1),
        rec(run_id="clicked", pid=2, source=A.SOURCE_PANEL),
        rec(run_id="on-a-peer", placement=A.PLACEMENT_MESH_PEER, work_key="w",
            node="brick", pid=None, tty=""),
        rec(run_id="mesh-here", placement=A.PLACEMENT_MESH_HERE, pid=3),
    ]
    _r, states = _resolved(records, ev(processes={1: proc(), 2: proc(), 3: proc()},
                                       claims={"w"}))
    assert A.cap_load(records, states) == {"auto-here", "mesh-here"}


def test_an_agent_at_its_prompt_gives_its_bay_back():
    records, states = _resolved([rec()], ev(processes={4242: proc()},
                                            tails={"pts/3": AT_PROMPT}))
    assert states["r1"].state == A.AWAITING_INPUT
    assert A.cap_load(records, states) == set()


def test_an_agent_whose_evidence_is_missing_keeps_its_bay():
    """The bay is held precisely because nothing is known: releasing it on missing
    evidence is the burst the cap exists to stop."""
    records, states = _resolved(
        [rec()], ev(processes=A.Observation.unavailable("ps failed")))
    assert A.cap_load(records, states) == {"r1"}


def test_rows_draw_every_run_and_read_finished_first():
    records = [
        rec(run_id="running", pid=1, dispatched_at=T0 - 10),
        rec(run_id="clicked", pid=2, source=A.SOURCE_PANEL, dispatched_at=T0 - 20),
        rec(run_id="over", pid=None, dispatched_at=T0 - 600, pr_number=None),
        rec(run_id="landed", pid=3, pr_number=500, dispatched_at=T0 - 30),
    ]
    _r, states = _resolved(records, ev(processes={1: proc(), 2: proc(), 3: proc()},
                                       merged={500}))
    assert [r.run_id for r, _s in A.rows(records, states)] == [
        "landed",   # merged
        "clicked",  # running, oldest
        "running",  # running
        "over",     # unknown, last
    ]


def test_only_positive_evidence_retires_a_record():
    records = [
        rec(run_id="gone", pid=None, pr_number=None,
            dispatched_at=T0 - 600),                                  # unknown
        rec(run_id="exited", pid=7),                                  # finished
        rec(run_id="landed", pid=1, pr_number=500),                   # merged
        rec(run_id="alive", pid=1),                                   # running
    ]
    _r, states = _resolved(records, ev(processes={1: proc()}, merged={500}))
    assert sorted(r.run_id for r in A.retirable(records, states)) == \
        ["exited", "landed"]


def test_a_runner_that_reported_its_turn_over_is_retired_like_any_other():
    """The whole point of the rung. An agent alive at its prompt is what BOTH a Claude
    Code hook and an OpenCode session describe, so one of them must not be the only one
    that ends a run — the other's runs are then priced by nothing, drop out of no
    ledger, and go on refusing their PR a fresh agent until a human closes the
    window."""
    records = [rec(run_id="hooked", pid=1), rec(run_id="served", pid=2, tty="pts/4")]
    _r, states = _resolved(records,
                           ev(processes={1: proc(), 2: proc(tty="pts/4")},
                              tails={"pts/3": WORKING, "pts/4": WORKING},
                              activity={"hooked": (completion.IDLE, T0 - 5)},
                              sessions={"served": A.SessionState(busy=False)}))
    assert sorted(r.run_id for r in A.retirable(records, states)) == \
        ["hooked", "served"]
    assert A.cap_load(records, states) == set()
    assert A.in_flight(records, states, 337) is False


@pytest.mark.parametrize("limit,occupied,want", [
    (2, 0, 2), (2, 1, 1), (2, 2, 0),
    (2, 5, 0),  # untracked agents and a lowered cap can both overfill the box
])
def test_free_slots_never_goes_negative(limit, occupied, want):
    assert A.free_slots(limit, occupied) == want


# MARK: - Round-tripping (the parity payload rides on this)


def test_records_and_evidence_survive_a_json_round_trip():
    r = rec(claim_seen_at=T0 - 5, work_key="w", node="brick")
    assert A.RunRecord.from_json(r.to_json()) == r
    e = ev(processes={4242: proc()}, tails={"pts/3": WORKING}, merged={1, 2})
    back = A.Evidence.from_json(e.to_json())
    assert back.processes.value == e.processes.value
    assert back.tails.value == e.tails.value
    assert back.merged_prs.value == e.merged_prs.value


def test_an_unavailable_observation_keeps_its_reason_across_json():
    o = A.Observation.unavailable("no tmux server")
    assert A.Observation.from_json(o.to_json()).reason == "no tmux server"


def test_a_probe_missing_from_the_payload_reads_as_unavailable():
    """Not as an empty answer — a payload that forgot a probe must not be mistaken
    for a machine where that probe looked and saw nothing."""
    assert A.Observation.from_json(None).status == A.UNAVAILABLE
    assert A.Evidence.from_json({}).processes.status == A.UNAVAILABLE


# MARK: - Adopting the tty from the process the pid names


def test_a_tracked_run_learns_its_tty_from_its_own_process():
    """Nothing tells the applet a run's tty at spawn — it opens a terminal and walks
    away. Without adopting one, a tracked run has no screen to read, so it reads as
    working from the moment it starts until its window closes: the "still running"
    verdict on an agent that finished hours ago."""
    out = A.adopt_ttys([rec(tty="")],
                       A.Observation.present({4242: proc(tty="pts/9")}),
                       A.Observation.present({}))
    assert out[0].tty == "pts/9"
    states = A.resolve(out, ev(processes={4242: proc(tty="pts/9")},
                               tails={"pts/9": AT_PROMPT}), T0)
    assert states["r1"].state == A.AWAITING_INPUT


def test_an_adopted_tty_is_not_overwritten_later():
    out = A.adopt_ttys([rec(tty="pts/3")],
                       A.Observation.present({4242: proc(tty="pts/9")}),
                       A.Observation.present({}))
    assert out[0].tty == "pts/3"


def test_no_tty_is_adopted_from_a_table_that_could_not_be_read():
    out = A.adopt_ttys([rec(tty="")], A.Observation.unavailable("ps failed"),
                       A.Observation.present({}))
    assert out[0].tty == ""


def test_a_pid_less_run_adopts_the_tty_the_prompt_scan_found_it_on():
    """The mesh-placed case: no pid to look up, so the scan that says it is alive is
    also the only thing that says where its screen is. Without this it has no screen,
    and no screen means it reads as working until its window closes."""
    out = A.adopt_ttys([rec(tty="", pid=None)],
                       A.Observation.present({4242: proc(tty="pts/9")}),
                       A.Observation.present({337: "pts/5"}))
    assert out[0].tty == "pts/5"
    states = A.resolve(out, ev(processes={}, live_agents={337: "pts/5"},
                               tails={"pts/5": AT_PROMPT}), T0)
    assert states["r1"].state == A.AWAITING_INPUT
    assert A.cap_load(out, states) == set(), "and it gives its bay back like any other"


def test_a_run_with_neither_a_pid_nor_a_sighting_adopts_nothing():
    out = A.adopt_ttys([rec(tty="", pid=None, pr_number=None)],
                       A.Observation.present({4242: proc(tty="pts/9")}),
                       A.Observation.present({337: "pts/5"}))
    assert out[0].tty == ""


def test_the_tick_adopts_a_tty_before_it_classifies_activity():
    """The ordering claim: within ONE tick a fresh run must be able to reach
    `awaiting input`, not on the tick after."""
    t = A.tick([rec(tty="")], ev(processes={4242: proc(tty="pts/9")},
                                 tails={"pts/9": AT_PROMPT}), T0, 2)
    assert t.states["r1"].state == A.AWAITING_INPUT
    assert t.records[0].tty == "pts/9", "and the tty is written back for the probe"
