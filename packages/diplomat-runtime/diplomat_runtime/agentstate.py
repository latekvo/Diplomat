"""What every dispatched agent is doing right now — one answer, from typed evidence.

Four questions used to be answered four separate times, each from its own subset of
the same evidence: is this PR in flight, how many bays of the device's cap are full,
what rows does the panel draw, and which record is retired. Patching one moved the
bug into the others, and the macOS front-end answered all four differently again.

Here they are one function and four projections of its result:

    resolve(records, evidence, now) -> {run_id: Resolution}

    in_flight(...)  cap_load(...)  rows(...)  retirable(...)

Everything in this module is **pure** — no clock, no subprocess, no filesystem. The
impure half is each front-end's own probe layer — :mod:`diplomat_app.probes` on Linux,
``AgentProbes.swift`` on macOS — whose only job is to turn the outside world into an
:class:`Evidence` bundle. That split is what makes a scenario a dict
literal instead of a machine in a particular state.

Swift twin: ``DiplomatCore/AgentState.swift``. The scenario table in
``tests/test_agent_state.py`` is fed through both (``tests/test_agent_state_parity.py``,
via ``diplomat-core agent-state``), so the two front-ends cannot drift again.

The one rule the whole ladder is built to keep
---------------------------------------------
**Absence of evidence never resolves to FINISHED.** A run is finished only on
positive evidence — its sentinel exists, its process was looked for in a table we
actually read and was not there, or its mesh claim was seen and has since been
released. Every other gap resolves to :data:`UNKNOWN`, which holds its bay and says
so. Reading "I could not look" as "it is gone" is what produced years of
already-complete verdicts on agents that were still working.

The mirror rule costs a bay rather than correctness: a live process whose screen
cannot be read is RUNNING, because working and waiting-at-the-prompt are genuinely
indistinguishable from outside. The probe layer reports how often that happens rather
than letting it pass silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from . import apiwatch

# MARK: - Observations: evidence, or a named reason there is none


#: A probe answered, and this is what it saw.
PRESENT = "present"
#: A probe could not answer — tmux is down, the process table would not decode, the
#: terminal refused automation. Distinct from an empty answer, because an empty
#: answer is a fact and this is the absence of one.
UNAVAILABLE = "unavailable"
#: This platform has no such probe at all (window ids on Linux). Never a defect, and
#: never a reason to warn about a silent probe.
UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class Observation:
    """One probe's answer: a value, or a named reason there isn't one.

    The type exists because the two collapse in every collection: ``{}`` from
    :func:`tmuxwatch.pane_tails_for_ttys` means both "tmux is not installed" and
    "tmux is wedged", and an empty ``ps`` set means both "no agents" and "the dump
    would not decode". Callers then cannot degrade differently for the two, so they
    degrade wrongly for one of them.
    """

    status: str
    value: Any = None
    reason: str = ""

    @staticmethod
    def present(value: Any) -> "Observation":
        return Observation(PRESENT, value)

    @staticmethod
    def unavailable(reason: str) -> "Observation":
        return Observation(UNAVAILABLE, None, reason)

    @staticmethod
    def unsupported(reason: str = "not available on this platform") -> "Observation":
        return Observation(UNSUPPORTED, None, reason)

    @property
    def ok(self) -> bool:
        return self.status == PRESENT

    def to_json(self) -> dict:
        return {"status": self.status, "value": _jsonable(self.value),
                "reason": self.reason}

    @staticmethod
    def from_json(obj: dict | None, coerce=lambda v: v) -> "Observation":
        """Decode one observation. A missing key reads as unavailable rather than as
        an empty answer — a payload that forgot a probe must not be mistaken for a
        machine where that probe saw nothing, and neither must one whose value did
        not survive the trip. PRESENT is what lets every reader use ``.value``
        without a second guard, so nothing may reach that state without one."""
        if not isinstance(obj, dict):
            return Observation.unavailable("absent from payload")
        status = obj.get("status", UNAVAILABLE)
        if status != PRESENT:
            return Observation(status, None, obj.get("reason", ""))
        value = coerce(obj.get("value"))
        if value is None:
            return Observation.unavailable("value did not decode")
        return Observation.present(value)


def _jsonable(value: Any) -> Any:
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (ProcInfo, SessionState)):
        return value.to_json()
    return value


# MARK: - The states a run can be in


MERGED = "merged"  # the PR landed — terminal, and outranks whatever the process does
FINISHED = "finished"  # positive evidence the agent ended
AWAITING_INPUT = "awaiting_input"  # alive, and its screen shows it back at the prompt
RUNNING = "running"  # alive, and either working or unreadable
STARTING = "starting"  # dispatched so recently that nothing could have observed it yet
UNKNOWN = "unknown"  # the evidence this run turns on was unavailable

#: Reading order for the panel, matching ``AgentTaskStatus`` in the Swift core: a
#: finished outcome first because it is the only row asking to be read, then the
#: sessions, then the ones nothing is known about.
STATE_ORDER = [MERGED, FINISHED, AWAITING_INPUT, RUNNING, STARTING, UNKNOWN]

#: States in which a run still holds a bay of the device's automatic-task cap.
#:
#: AWAITING_INPUT is deliberately absent. The cap bounds concurrent LOAD, and a
#: session sitting at its prompt is spending none — left counted, a machine whose
#: finished windows are all still open defers automatic work indefinitely while doing
#: nothing. UNKNOWN is deliberately present: a bay released on missing evidence is
#: exactly the burst this cap exists to stop.
OCCUPYING = frozenset({RUNNING, STARTING, UNKNOWN})

#: States that block a second dispatch onto the same PR — every state that is not
#: over. Wider than :data:`OCCUPYING` by AWAITING_INPUT, and the difference is the
#: point: that session still holds the PR's context and is waiting to be typed at, so
#: it must not get a second agent beside it even though it has given its bay back.
BLOCKING = OCCUPYING | {AWAITING_INPUT}


# MARK: - Timing constants


#: How long after dispatch a run with no observed process still reads as STARTING.
#: The inner shell writes its pid before it execs the agent, but a terminal emulator,
#: a tmux server and the user's rc all run first. Past this the run is not called
#: finished — it becomes UNKNOWN, because a spawn that never landed and a pid file we
#: have not read yet look identical from here.
SPAWN_GRACE = 20.0

#: How much younger than its own record a process may be and still be that record's
#: agent. Pids are recycled, and a run that dispatched an hour ago cannot be a
#: process that started a minute ago; the slack only absorbs the seconds between the
#: dispatch stamp and the exec, plus ``etime`` rounding.
PID_ADOPTION_SLACK = 30.0

#: How long a mesh origination claim may go unseen before the peer's run reads as
#: over.
#:
#: Absence is only evidence once it has had time to be evidence. The claim travels the
#: executor's link BEFORE the dispatch ack, but reaches a front-end through a file the
#: node rewrites every couple of seconds, read by a poll of its own — and a node restart
#: empties the book until its peers re-assert. This window outlasts all three, and is
#: short enough that a finished run leaves the list while the operator is still looking
#: at it.
CLAIM_SETTLE = 45.0


# MARK: - Placements


PLACEMENT_LOCAL = "local"  # this applet opened the terminal
PLACEMENT_MESH_HERE = "mesh-here"  # the mesh placed the run back on this machine
PLACEMENT_MESH_PEER = "mesh-peer"  # the run is a process on somebody else's box

SOURCE_PANEL = "panel"
SOURCE_AUTO = "auto"


# MARK: - Inputs


@dataclass(frozen=True)
class ProcInfo:
    """One live process, as the process-table probe reports it."""

    tty: str
    #: Seconds since the process started (``ps`` ``etime``), for the adoption guard.
    elapsed: float
    #: Whether its argv still looks like an agent — the second half of the guard, so
    #: a recycled pid belonging to something else can never be adopted.
    is_agent: bool

    def to_json(self) -> dict:
        return {"tty": self.tty, "elapsed": self.elapsed, "isAgent": self.is_agent}

    @staticmethod
    def from_json(obj: dict) -> "ProcInfo":
        return ProcInfo(tty=obj.get("tty", ""),
                        elapsed=float(obj.get("elapsed", 0.0)),
                        is_agent=bool(obj.get("isAgent", False)))


@dataclass(frozen=True)
class SessionState:
    """What an agent's own session says about it, for a runner that keeps one.

    The typed answer to the question :func:`_classify_activity` otherwise has to read
    off a status bar. An OpenCode agent serves its session over loopback while it
    works (:mod:`diplomat_runtime.opencodeapi`) and a Hermes agent writes its own to
    SQLite (:mod:`diplomat_runtime.hermesstore`); Claude Code serves nothing, so its runs
    are absent from the evidence and are still read from the screen.

    Only the one fact, because it is the only one this evidence can carry honestly:
    an OpenCode run's spend is a sum over its whole transcript and the poll reads one
    message. A finished run is priced from its runner's own store instead.
    """

    #: Is a turn in flight? Whichever way its runner says so.
    busy: bool

    def to_json(self) -> dict:
        return {"busy": self.busy}

    @staticmethod
    def from_json(obj: dict) -> "SessionState":
        return SessionState(busy=bool(obj.get("busy", False)))


@dataclass(frozen=True)
class RunRecord:
    """One dispatched agent run, as the registry persists it.

    Identity is ``run_id``, not the PR: two runs on one PR are two records, an
    applet restart keeps them both, and nothing has to be inferred from the wording
    of a prompt.
    """

    run_id: str
    dispatched_at: float
    pr_number: int | None = None
    pr_url: str = ""
    kind: str = ""
    label: str = ""
    source: str = SOURCE_AUTO
    placement: str = PLACEMENT_LOCAL
    node: str = ""
    work_key: str = ""
    ledger_key: str = ""
    #: The agent's real pid, written by the inner shell before it execs (see
    #: ``review.shell_command``). ``None`` until the registry has read the pid file.
    pid: int | None = None
    tty: str = ""
    #: When this device last saw the executor's claim for :attr:`work_key`, for a
    #: mesh-peer run. ``None`` when it has never been seen.
    claim_seen_at: float | None = None
    #: True for a run nothing dispatched — a live agent found in the process table
    #: with no record behind it. It gets a row and blocks a second dispatch, but
    #: carries no label, no ledger key and no start time.
    untracked: bool = False

    @property
    def runs_here(self) -> bool:
        """Does this run's agent execute on THIS machine? What the device's cap
        counts — the cap bounds what this box runs, not what it dispatched."""
        return self.placement != PLACEMENT_MESH_PEER

    def to_json(self) -> dict:
        return {
            "runId": self.run_id, "dispatchedAt": self.dispatched_at,
            "prNumber": self.pr_number, "prUrl": self.pr_url, "kind": self.kind,
            "label": self.label, "source": self.source, "placement": self.placement,
            "node": self.node, "workKey": self.work_key,
            "ledgerKey": self.ledger_key, "pid": self.pid, "tty": self.tty,
            "claimSeenAt": self.claim_seen_at, "untracked": self.untracked,
        }

    @staticmethod
    def from_json(obj: dict) -> "RunRecord":
        return RunRecord(
            run_id=obj.get("runId", ""),
            dispatched_at=float(obj.get("dispatchedAt", 0.0)),
            pr_number=obj.get("prNumber"),
            pr_url=obj.get("prUrl", ""),
            kind=obj.get("kind", ""),
            label=obj.get("label", ""),
            source=obj.get("source", SOURCE_AUTO),
            placement=obj.get("placement", PLACEMENT_LOCAL),
            node=obj.get("node", ""),
            work_key=obj.get("workKey", ""),
            ledger_key=obj.get("ledgerKey", ""),
            pid=obj.get("pid"),
            tty=obj.get("tty", ""),
            claim_seen_at=obj.get("claimSeenAt"),
            untracked=bool(obj.get("untracked", False)),
        )


@dataclass(frozen=True)
class Evidence:
    """Everything the outside world had to say this tick, each part able to say it
    had nothing to say.

    Defaults are UNAVAILABLE rather than empty, so a caller that forgets to wire a
    probe gets rows reading "unknown" instead of a machine that confidently believes
    every agent finished.
    """

    #: pid → what the process table says about it.
    processes: Observation = field(
        default_factory=lambda: Observation.unavailable("not probed"))
    #: The run ids whose completion sentinel exists.
    sentinels: Observation = field(
        default_factory=lambda: Observation.unavailable("not probed"))
    #: tty → that session's visible buffer.
    tails: Observation = field(
        default_factory=lambda: Observation.unavailable("not probed"))
    #: Work keys currently claimed somewhere on the mesh.
    claims: Observation = field(
        default_factory=lambda: Observation.unavailable("not probed"))
    #: PR numbers GitHub reports as MERGED.
    merged_prs: Observation = field(
        default_factory=lambda: Observation.unavailable("not probed"))
    #: PR number → the tty of an agent found by its prompt text in the process table.
    #: The pre-registry identity mechanism, and still the only evidence about a run
    #: whose terminal this applet did not open — a mesh placement that landed back
    #: here, whose pid file belongs to the node that spawned it.
    live_agents: Observation = field(
        default_factory=lambda: Observation.unavailable("not probed"))
    #: run id → what that run's own agent session says about it. Only a runner that
    #: serves one appears here, so a run's absence is ordinary and reads as "ask the
    #: screen" rather than as anything about the run.
    sessions: Observation = field(
        default_factory=lambda: Observation.unavailable("not probed"))

    def to_json(self) -> dict:
        return {"processes": self.processes.to_json(),
                "sentinels": self.sentinels.to_json(),
                "tails": self.tails.to_json(),
                "claims": self.claims.to_json(),
                "mergedPrs": self.merged_prs.to_json(),
                "liveAgents": self.live_agents.to_json(),
                "sessions": self.sessions.to_json()}

    @staticmethod
    def from_json(obj: dict) -> "Evidence":
        return Evidence(
            processes=Observation.from_json(
                obj.get("processes"),
                lambda v: {int(k): ProcInfo.from_json(p) for k, p in (v or {}).items()}),
            sentinels=Observation.from_json(obj.get("sentinels"),
                                            lambda v: set(v or [])),
            tails=Observation.from_json(obj.get("tails"), lambda v: dict(v or {})),
            claims=Observation.from_json(obj.get("claims"), lambda v: set(v or [])),
            merged_prs=Observation.from_json(obj.get("mergedPrs"),
                                             lambda v: {int(n) for n in (v or [])}),
            live_agents=Observation.from_json(
                obj.get("liveAgents"),
                lambda v: {int(k): str(t) for k, t in (v or {}).items()}),
            sessions=Observation.from_json(
                obj.get("sessions"),
                lambda v: {str(k): SessionState.from_json(s)
                           for k, s in (v or {}).items()}),
        )


# MARK: - Output


@dataclass(frozen=True)
class Resolution:
    """What one run resolved to, and the single fact that decided it.

    ``reason`` is not decoration: it is what the debug dump prints, and it is how a
    wrong verdict is diagnosed in one read instead of by re-deriving the ladder by
    hand. Every rung writes one.
    """

    run_id: str
    state: str
    reason: str

    @property
    def occupying(self) -> bool:
        return self.state in OCCUPYING

    def to_json(self) -> dict:
        return {"runId": self.run_id, "state": self.state, "reason": self.reason}


# MARK: - Claim sightings (pure, but stateful across ticks)


def observe_claims(records: list[RunRecord], claims: Observation,
                   now: float) -> list[RunRecord]:
    """Refresh each mesh-peer run's claim sighting, returning updated records.

    Split out of :func:`resolve` because it is the one input that is a memory rather
    than an observation: absence only becomes evidence relative to when the claim was
    last present, so somebody has to remember that. Keeping it a separate pure step
    means :func:`resolve` stays a function of its arguments alone.

    An unavailable claim book updates nothing — a node we could not read must not
    age out a peer's run.
    """
    if not claims.ok:
        return records
    live = claims.value
    out = []
    for r in records:
        if r.placement == PLACEMENT_MESH_PEER and r.work_key and r.work_key in live:
            out.append(replace(r, claim_seen_at=now))
        else:
            out.append(r)
    return out


# MARK: - The resolver


def resolve(records: list[RunRecord], evidence: Evidence,
            now: float) -> dict[str, Resolution]:
    """Every run's state, from one pass of evidence. Pure."""
    return {r.run_id: resolve_one(r, evidence, now) for r in records}


def resolve_one(record: RunRecord, evidence: Evidence, now: float) -> Resolution:
    """One run's state, by a fixed ladder.

    The order is the precedence, and each rung is either positive evidence or an
    explicit refusal to guess:

    1. the PR landed — a terminal outcome that outranks whatever the process is doing;
    2. the completion sentinel exists — the agent returned an exit code;
    3. a mesh-peer run is judged by the executor's claim, because no probe on this
       machine can see a process on another one;
    4. a local run is judged by its pid, and its screen only classifies a pid that is
       already known to be alive.
    """
    def done(state: str, reason: str) -> Resolution:
        return Resolution(record.run_id, state, reason)

    if evidence.merged_prs.ok and record.pr_number is not None:
        if record.pr_number in evidence.merged_prs.value:
            return done(MERGED, f"PR #{record.pr_number} is merged")

    if evidence.sentinels.ok and record.run_id in evidence.sentinels.value:
        return done(FINISHED, "completion sentinel present")

    if record.placement == PLACEMENT_MESH_PEER:
        return _resolve_peer(record, evidence, now, done)
    return _resolve_local(record, evidence, now, done)


def _resolve_peer(record: RunRecord, evidence: Evidence, now: float,
                  done) -> Resolution:
    """A run on somebody else's machine, judged by the origination lease.

    The executor claims the work key when it spawns the agent and releases it when
    the agent exits (szpontnet-spec/docs/12), and the claim is republished in every
    snapshot the local node writes. So a claimed key is a running agent — and this is
    the only evidence there is, because ``ps`` on this box structurally cannot see a
    process on that one. Judging a peer's run by a local process table, as the Linux
    front-end used to, retires every peer run the moment its grace expires.
    """
    if not evidence.claims.ok:
        return done(UNKNOWN, f"mesh claims {evidence.claims.reason or 'unavailable'}")
    if record.work_key and record.work_key in evidence.claims.value:
        node = record.node or "a peer"
        return done(RUNNING, f"claim held on {node}")
    # Absence is only evidence once it has had time to be evidence. A run whose claim
    # has never been seen counts from its dispatch, which covers both the lag before
    # the first snapshot carries the key and the executor that deduped our dispatch
    # against an agent of its own and so never took a lease at all.
    since = now - (record.claim_seen_at
                   if record.claim_seen_at is not None else record.dispatched_at)
    if since < CLAIM_SETTLE:
        if record.claim_seen_at is None:
            return done(STARTING, f"dispatched {since:.0f}s ago, claim not seen yet")
        return done(RUNNING, f"claim last seen {since:.0f}s ago")
    return done(FINISHED, f"claim released {since:.0f}s ago")


def _resolve_local(record: RunRecord, evidence: Evidence, now: float,
                   done) -> Resolution:
    """A run whose agent is a process on this machine.

    The pid is the identity — written by the inner shell before it execs the agent,
    so it is the agent's own, not a wrapper's. Matching on it replaces reading
    ``PR #<n> in <owner>/<repo>`` out of a prompt in ``ps`` output, which could not
    tell two runs on one PR apart and matched any unrelated session that mentioned
    the number.
    """
    if not evidence.processes.ok:
        return done(UNKNOWN,
                    f"process table {evidence.processes.reason or 'unavailable'}")
    table: dict[int, ProcInfo] = evidence.processes.value
    age = now - record.dispatched_at

    if record.pid is None:
        # An untracked run IS its process-table sighting, so it has no pid of its own
        # and no dispatch stamp to be young against; it is alive by construction.
        if record.untracked:
            return _classify_activity(record, evidence, done, "found in process table")
        return _resolve_without_pid(record, evidence, age, done)

    proc = table.get(record.pid)
    if proc is None:
        return done(FINISHED, f"pid {record.pid} absent from the process table")
    if not proc.is_agent:
        return done(FINISHED, f"pid {record.pid} was recycled by another process")
    # A recycled pid can also be re-taken by another agent. The genuine one started
    # just after this record did, so anything materially younger is a stranger.
    if proc.elapsed < age - PID_ADOPTION_SLACK:
        return done(FINISHED,
                    f"pid {record.pid} is {proc.elapsed:.0f}s old but the run is "
                    f"{age:.0f}s old")
    return _classify_activity(record, evidence, done, f"pid {record.pid} alive")


def _resolve_without_pid(record: RunRecord, evidence: Evidence, age: float,
                         done) -> Resolution:
    """A run this applet booked but has no pid for.

    Two things produce one. A spawn whose shell has not written its pid file yet — the
    ordinary first seconds of a run. And a placement the mesh routed back to this
    machine, where the NODE opened the terminal, so the pid file it wrote belongs to a
    run directory this applet never created and never will.

    The second is why this rung is not simply "unknown until a pid appears". A
    mesh-here run has no pid ever, so that answer would hold its bay and refuse its PR
    a fresh agent for the rest of the applet's life — the exact wedge this module
    exists to remove, arriving by a different road. Seen in production the first time
    the monitors ran: two conflict fixes the mesh placed back here, both reading
    "unknown", both bays held, nothing able to retire either.

    So the fallback is the pre-registry evidence: the agent's own prompt in the process
    table. It cannot tell two runs on one PR apart, which is exactly why it is the
    fallback and not the identity — but "an agent for this PR is up" and "no agent for
    this PR is up" are both positive answers, and the second is what finally ends the
    run.
    """
    if not evidence.live_agents.ok:
        return done(UNKNOWN,
                    f"no pid, and the agent scan {evidence.live_agents.reason or 'failed'}")
    if record.pr_number is not None and record.pr_number in evidence.live_agents.value:
        return _classify_activity(record, evidence, done,
                                  f"an agent is up on PR #{record.pr_number}")
    if age <= SPAWN_GRACE:
        return done(STARTING, f"dispatched {age:.0f}s ago, no pid yet")
    if record.pr_number is None:
        # Nothing to look for: a run with neither a pid nor a PR cannot be found by
        # either mechanism, so its absence is not evidence of anything.
        return done(UNKNOWN, f"no pid recorded {age:.0f}s after dispatch")
    return done(FINISHED, f"no agent for PR #{record.pr_number} in the process table")


def _classify_activity(record: RunRecord, evidence: Evidence, done,
                       alive_reason: str) -> Resolution:
    """Working, or finished its turn and waiting at the prompt?

    An agent is spawned into an INTERACTIVE session, so finishing its work is not
    exiting: it sits at the prompt until a human closes the window, and the process
    table shows the same live agent either way. Something has to separate the two.

    Two things can, and the agent's own session is asked first because it is the only
    one that is positive evidence: a turn carries a completion stamp, set when it
    ends. The screen is the fallback, and it is an inference — it reads whether the
    CLI's interrupt hint was on the status bar when we looked, which is a string from
    someone else's UI that says nothing at all if they reword it.

    Every gap here reads as RUNNING, which costs a bay rather than correctness — but
    it is also the one rung that fails silently, so the probe layer counts
    how often the tail is missing and says so out loud.
    """
    if evidence.sessions.ok:
        session: SessionState | None = evidence.sessions.value.get(record.run_id)
        if session is not None:
            if session.busy:
                return done(RUNNING, f"{alive_reason}; its session is mid-turn")
            return done(AWAITING_INPUT, f"{alive_reason}; its session finished its "
                                        f"turn")
    if not evidence.tails.ok:
        return done(RUNNING,
                    f"{alive_reason}; screen {evidence.tails.reason or 'unavailable'}")
    tails: dict[str, str] = evidence.tails.value
    tail = tails.get(record.tty) if record.tty else None
    if tail is None:
        return done(RUNNING, f"{alive_reason}; no screen for tty {record.tty or '?'}")
    if apiwatch.looks_busy(tail):
        return done(RUNNING, f"{alive_reason}; working")
    return done(AWAITING_INPUT, f"{alive_reason}; at the prompt")


# MARK: - Untracked agents


def adopt_ttys(records: list[RunRecord], processes: Observation,
               live_agents: Observation) -> list[RunRecord]:
    """Fill in each run's tty, from whichever source can reach its agent.

    Nothing tells the applet a run's tty at spawn time — it opens a terminal and walks
    away — so the only place it exists is on the agent process itself. Without it a run
    has no screen, and no screen means it reads as working from the moment it starts
    until the moment its window closes: exactly the "still running" verdict on an agent
    that finished hours ago.

    Two sources, because two kinds of run reach their agent differently. A run with a
    pid takes the tty off that process, which is exact. A run WITHOUT one — a placement
    the mesh routed back here, where the node opened the terminal — has only the prompt
    scan, which is looser but is the same evidence that says it is alive at all.

    A tty is adopted once and then left alone: it is a property of the process, and a
    process does not change ttys.
    """
    table = processes.value if processes.ok else {}
    scan = live_agents.value if live_agents.ok else {}
    out = []
    for r in records:
        if r.tty:
            out.append(r)
            continue
        proc = table.get(r.pid) if r.pid is not None else None
        found = proc.tty if proc is not None else scan.get(r.pr_number, "")
        out.append(replace(r, tty=found) if found else r)
    return out


def synthesize_untracked(records: list[RunRecord], live_agents: Observation,
                         now: float) -> list[RunRecord]:
    """Records for live agents nobody dispatched, so they are deduped against and
    drawn rather than merely subtracted from a slot count.

    ``live_agents`` maps a PR number to the tty its agent runs on. The tty is what
    lets one of these be classified as working or idle at all — without it every
    untracked agent would read as running and hold a bay until its window closed,
    which is the state the cap exists to prevent.

    Three things produce one: an applet upgraded while agents ran, an agent a peer's
    node started on this box, and a session the operator opened by hand. They are
    found the old way — the prompt's ``PR #<n> in <owner>/<repo>`` in the process
    table — which is why they are a *fallback* and not the identity mechanism: that
    scan cannot tell two runs on one PR apart, so at most one record per PR is made.

    They count as automatic. An agent whose trigger is unknown spending a bay defers
    work; the opposite error dispatches a second agent onto a PR that has one.
    """
    if not live_agents.ok:
        return records
    known = {r.pr_number for r in records if r.pr_number is not None}
    out = list(records)
    for pr in sorted(set(live_agents.value) - known):
        out.append(RunRecord(run_id=f"untracked:{pr}", dispatched_at=now,
                             pr_number=pr, source=SOURCE_AUTO,
                             placement=PLACEMENT_LOCAL,
                             tty=live_agents.value[pr], untracked=True))
    return out


# MARK: - The four projections
#
# Each is a fold over the resolved map. Nothing below re-reads evidence or re-derives
# a state, which is the whole point: the four answers can disagree with each other
# only if this file is wrong, not if one of four call sites drifted.


def in_flight(records: list[RunRecord], states: dict[str, Resolution],
              pr_number: int) -> bool:
    """Does this PR already have an agent, for the dispatch gate's dedup?"""
    return any(r.pr_number == pr_number and states[r.run_id].state in BLOCKING
               for r in records if r.run_id in states)


def cap_load(records: list[RunRecord], states: dict[str, Resolution]) -> set[str]:
    """The run ids holding a bay of this device's automatic-task cap.

    Counted by where a run EXECUTES and who triggered it: a peer's agent spends the
    peer's budget, and a panel click is the operator's own act and spends none of the
    automatic one.
    """
    return {r.run_id for r in records
            if r.runs_here and r.source == SOURCE_AUTO
            and r.run_id in states and states[r.run_id].occupying}


def rows(records: list[RunRecord],
         states: dict[str, Resolution]) -> list[tuple[RunRecord, Resolution]]:
    """Every run the panel draws, in reading order: by state, then oldest first.

    Every run — both sources, both platforms, tracked and not. The front-ends used to
    disagree about this (Linux hid panel spawns and drew untracked agents, macOS did
    the reverse), which meant the list and the cap were answering different
    questions.
    """
    pairs = [(r, states[r.run_id]) for r in records if r.run_id in states]
    return sorted(pairs, key=lambda p: (STATE_ORDER.index(p[1].state),
                                        p[0].dispatched_at, p[0].run_id))


def retirable(records: list[RunRecord],
              states: dict[str, Resolution]) -> list[RunRecord]:
    """The runs whose agent has ended — what the registry drops and what the
    telemetry ledger prices.

    Only MERGED and FINISHED, both of which are positive evidence. A record is never
    retired by its own age: an hour-long review is an ordinary one, and a clock that
    ends records ends them mid-run.
    """
    return [r for r in records
            if r.run_id in states and states[r.run_id].state in (MERGED, FINISHED)]


def free_slots(limit: int, occupied: int) -> int:
    """Bays of the cap with nothing in them. Clamped, because a lowered cap and
    untracked agents can both put more agents on the box than the cap allows."""
    return max(0, limit - occupied)


# MARK: - One tick


@dataclass(frozen=True)
class Tick:
    """Everything one pass of evidence produced. What a caller reads instead of
    re-deriving any of it."""

    #: The records as the pipeline left them — claim sightings refreshed, untracked
    #: agents synthesized. The caller persists these.
    records: list[RunRecord]
    states: dict[str, Resolution]
    rows: list[tuple[RunRecord, Resolution]]
    cap_load: set[str]
    retirable: list[RunRecord]
    free_slots: int

    def in_flight(self, pr_number: int) -> bool:
        return in_flight(self.records, self.states, pr_number)


def tick(records: list[RunRecord], evidence: Evidence, now: float,
         limit: int) -> Tick:
    """Fold one pass of evidence into every answer, in the one order that is correct.

    The order is the reason this is a function rather than a convention each caller
    repeats: claims are observed and ttys adopted BEFORE resolving, so both count this
    tick rather than a tick late; and untracked agents are synthesized AFTER, so a
    live agent that already has a record is not drawn twice — and so it keeps the tty
    the scan found it on rather than having one adopted for a pid it does not have.
    Both front-ends and the parity CLI go through here, so neither can get the
    sequence subtly different from the other.
    """
    records = observe_claims(records, evidence.claims, now)
    records = adopt_ttys(records, evidence.processes, evidence.live_agents)
    records = synthesize_untracked(records, evidence.live_agents, now)
    states = resolve(records, evidence, now)
    load = cap_load(records, states)
    return Tick(records=records, states=states, rows=rows(records, states),
                cap_load=load, retirable=retirable(records, states),
                free_slots=free_slots(limit, len(load)))
