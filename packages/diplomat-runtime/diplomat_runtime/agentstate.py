"""What every dispatched agent is doing right now — one answer, from typed evidence.

Four questions used to be answered four separate times, each from its own subset of
the same evidence: is this PR in flight, how many bays of the device's cap are full,
what rows does the panel draw, and which record is retired. Patching one moved the
bug into the others, and the macOS front-end answered all four differently again.

Here they are one function and five projections of its result:

    resolve(records, evidence, now, deadline) -> {run_id: Resolution}

    in_flight(...)  cap_load(...)  rows(...)  retirable(...)  reapable(...)

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
positive evidence — its runner said the turn is over, its sentinel exists, its
process was looked for in a table we actually read and was not there, or its mesh
claim was seen and has since been released. Every other gap resolves to
:data:`UNKNOWN`, which holds its bay and says so. Reading "I could not look" as "it
is gone" is what produced years of already-complete verdicts on agents that were
still working.

The mirror rule costs a bay rather than correctness: a live process whose screen
cannot be read is RUNNING, because working and waiting-at-the-prompt are genuinely
indistinguishable from outside. The probe layer reports how often that happens rather
than letting it pass silently.

The one deliberate exception is :data:`RUN_DEADLINE`, which ends a run on its age
rather than on evidence about it — the outermost backstop, for the runs every other
rung is structurally unable to end. It fires only when a caller passes one (the
operator's switch, Settings → STALLED AGENTS) and only on a positive reading that
the account still has tokens to spend.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Any

from . import apiwatch, completion

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


def _flag(value: Any, default: bool = False) -> bool:
    """A JSON boolean out of a decoded payload, or ``default``.

    Strict, and the same rule :meth:`Observation.from_json` applies to ``tokensLeft``:
    a flag that arrived as a number or a string is a field that did not survive its
    trip, and coercing one answers out of whatever happened to be truthy. Everything
    that writes this format writes real booleans (:meth:`to_json`), so the only payload
    this refuses is a malformed one.

    It is also what keeps the two decoders of this format agreeing. ``JSONInput`` in the
    parity CLI is strict for the same reason and by the same rule; ``bool(...)`` here
    would read ``1`` as a flag that ``JSONInput`` refuses, and a string as one that no
    Swift cast can produce at all.
    """
    return value if isinstance(value, bool) else default


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

#: Rank for the panel, matching ``AgentTaskStatus`` in the Swift core: an outcome,
#: then a local exit, then the sessions that want a human, then the ones that don't,
#: then the ones nothing is known about. The two :data:`ENDED` states head the rank
#: and no front-end draws a row in one, so the list itself starts at AWAITING_INPUT.
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
#: :data:`ENDED`. Wider than :data:`OCCUPYING` by AWAITING_INPUT, and the difference
#: is the point: that session still holds the PR's context and is waiting to be typed
#: at, so it must not get a second agent beside it even though it has given its bay
#: back.
BLOCKING = OCCUPYING | {AWAITING_INPUT}

#: The states a run is over in, both of them positive evidence.
#:
#: The pass that resolves a run into one of these retires it (:func:`retirable`), so
#: both front-ends leave it out of the list they draw: a row for it would be on screen
#: for one redraw and gone the next, and which redraw caught it would depend on when
#: the poll landed. What the run leaves behind is its activity line and its ledger
#: entry.
ENDED = frozenset({MERGED, FINISHED})


# MARK: - Timing constants


#: How long after dispatch a run with no observed process still reads as STARTING.
#: The inner shell writes its pid before the agent starts, but a terminal emulator, a
#: tmux server and the user's rc all run first — and the process table is one `ps` pass
#: reused for several seconds, so it can predate the pid file naming what to look for.
#: Past this the run is judged on the evidence there is: a known pid the table does not
#: hold has ended, while a run that produced neither a pid nor a PR to scan for becomes
#: UNKNOWN, because a spawn that never landed and a pid file we have not read yet look
#: identical from here.
SPAWN_GRACE = 20.0

#: How long after dispatch a live run whose screen has not shown a turn yet reads as
#: working rather than as back at its prompt.
#:
#: The pid exists as soon as the inner shell runs, but the agent then has to boot, read
#: its prompt file and draw its first status bar — and until it does, its screen is the
#: screen of an agent that has FINISHED, the interrupt hint absent from both. Read as
#: idle there, a run hands its bay straight back to the poll that started it, and the
#: next dispatch of that poll is seconds behind: a cap of one, two agents.
#:
#: Well past the twelve seconds measured from dispatch to first status bar, because being
#: too short is that burst while being too long only defers the next task by seconds —
#: and only for a run whose own report never arrives, since a sentinel, a merged PR, the
#: CLI's turn report and a runner's session each end one inside this window untouched.
FIRST_TURN_GRACE = 45.0

#: How much younger than its own record a process may be and still be that record's
#: agent. Pids are recycled, and a run that dispatched an hour ago cannot be a
#: process that started a minute ago; the slack only absorbs the seconds between the
#: dispatch stamp and the exec, plus ``etime`` rounding.
PID_ADOPTION_SLACK = 30.0

#: How long a run's screen may sit perfectly unchanged before it is called over.
#:
#: The backstop, for the runs the turn report cannot reach: a runner with no hooks, a
#: spawn whose settings could not be staged, an agent wedged mid-turn with its status
#: bar frozen. It is INDEPENDENT of that report rather than derived from it, which is
#: the only thing that makes it a fallback — a backstop that fails whenever the
#: primary fails is not one.
#:
#: Twenty minutes because a working agent's screen is never still for anywhere near
#: that long: the CLI redraws a spinner, a token count and an elapsed timer every
#: second it is thinking, so a pane still for this whole window — its terminal's own
#: clock aside, see :data:`_CLOCK` — means nothing is happening in it. Long enough that
#: a slow tool call, a long build or a human reading the window is not mistaken for a
#: dead one.
QUIET_TIMEOUT = 20 * 60.0

#: How long a run this device executes may go on before it is called over whatever
#: else the evidence says — the outermost backstop, offered as a switch
#: (:func:`appconfig.run_deadline`) rather than applied unconditionally.
#:
#: Beneath :data:`QUIET_TIMEOUT` sits the same argument one rung further out. The
#: stillness clock ends a wedged run by reading its screen, so it ends nothing on a run
#: whose screen cannot be read — a pane the multiplexer will not dump, a terminal that
#: refuses automation, a run whose tty was never adopted. Those runs hold a bay until a
#: human closes the window, and nothing in the ladder above says otherwise.
#:
#: Four hours because it has to clear the longest run that is genuinely work and not a
#: wedge: a swarm review of a large PR, an issue reproduced from scratch, an E2E sweep.
#: Those run in hours, not in fractions of one, so a deadline in minutes would retire
#: working agents and this one is deliberately far past anything measured here.
#:
#: It is the one rung that ends a run on the CLOCK rather than on evidence about that
#: run, which is why it is switched off by an operator who would rather a stuck bay than
#: an early verdict — and why it asks :attr:`Evidence.tokens_left` first. An account with
#: nothing left to spend parks every agent it has: they sit there accumulating age while
#: doing no work at all, and reading that as four hours of wedged run would retire the
#: whole board on the day a limit ran out.
RUN_DEADLINE = 4 * 60 * 60.0

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
                        is_agent=_flag(obj.get("isAgent")))


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
        return SessionState(busy=_flag(obj.get("busy")))


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
    #: The agent's pid, written by the inner shell before the agent starts (see
    #: ``review.shell_command`` for what "the agent's" rests on, and where it is
    #: instead the shell wrapping it). ``None`` until the registry has read the file.
    pid: int | None = None
    tty: str = ""
    #: When this device last saw the executor's claim for :attr:`work_key`, for a
    #: mesh-peer run. ``None`` when it has never been seen.
    claim_seen_at: float | None = None
    #: A digest of this run's screen when it last CHANGED, with the time it changed.
    #: The memory behind :data:`QUIET_TIMEOUT` — absence of motion is only measurable
    #: against when there was last some. ``""``/``None`` until its screen is first read.
    quiet_digest: str = ""
    quiet_since: float | None = None
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
            "claimSeenAt": self.claim_seen_at, "quietDigest": self.quiet_digest,
            "quietSince": self.quiet_since, "untracked": self.untracked,
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
            quiet_digest=obj.get("quietDigest", ""),
            quiet_since=obj.get("quietSince"),
            untracked=_flag(obj.get("untracked")),
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
    #: run id → ``(verb, when)`` the run's own CLI last reported for itself, via the
    #: hooks staged into its settings (:mod:`completion`). The only evidence here that
    #: is a REPORT rather than an observation: everything else in this bundle is
    #: something a probe went and looked at, and this is the agent saying so itself at
    #: the instant it happened. A run is absent when it has reported nothing yet.
    activity: Observation = field(
        default_factory=lambda: Observation.unavailable("not probed"))
    #: run id → what that run's own agent session says about it. Only a runner that
    #: serves one appears here, so a run's absence is ordinary and reads as "ask the
    #: screen" rather than as anything about the run.
    sessions: Observation = field(
        default_factory=lambda: Observation.unavailable("not probed"))
    #: Does the account this device's agents draw on still have room to spend? The one
    #: item here that is about the MACHINE rather than about any run, and the precondition
    #: :data:`RUN_DEADLINE` turns on. UNSUPPORTED on a machine whose account publishes no
    #: limit this applet can read, which is an ordinary machine and not a broken probe.
    tokens_left: Observation = field(
        default_factory=lambda: Observation.unavailable("not probed"))

    def to_json(self) -> dict:
        return {"activity": self.activity.to_json(),
                "processes": self.processes.to_json(),
                "sentinels": self.sentinels.to_json(),
                "tails": self.tails.to_json(),
                "claims": self.claims.to_json(),
                "mergedPrs": self.merged_prs.to_json(),
                "liveAgents": self.live_agents.to_json(),
                "sessions": self.sessions.to_json(),
                "tokensLeft": self.tokens_left.to_json()}

    @staticmethod
    def from_json(obj: dict) -> "Evidence":
        return Evidence(
            activity=Observation.from_json(
                obj.get("activity"),
                lambda v: {str(k): (str(t[0]), float(t[1]))
                           for k, t in (v or {}).items() if len(t) == 2}),
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
            # Strictly a bool: a number or a string in a hand-written payload is a
            # field that did not survive its trip, and coercing one would answer this
            # rung's precondition out of whatever happened to be truthy.
            tokens_left=Observation.from_json(
                obj.get("tokensLeft"),
                lambda v: v if isinstance(v, bool) else None),
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
    #: Whether the STILLNESS BACKSTOP is what ended this run — set by that rung and
    #: by nothing else.
    #:
    #: A verdict, not a restatement of one: a run reaches FINISHED by many roads, and
    #: only this one says its agent was alive with a frozen screen. The window reaper
    #: is the consumer, and the distinction is the whole of its licence to close a
    #: terminal, so it cannot be left to be re-derived from ``state`` plus a matured
    #: :func:`went_quiet` — a clock keeps maturing while its pane is unreadable
    #: (:func:`observe_quiescence` only advances on ticks that SAW the screen), so a
    #: run whose process left the machine during an evidence outage comes back
    #: FINISHED-because-gone carrying twenty minutes of stillness. Reaping that closes
    #: whatever holds its tty now.
    wedged: bool = False
    #: Whether the RUN DEADLINE is what ended this run — set by that rung and by
    #: nothing else.
    #:
    #: The other half of the window reaper's licence, and a separate field for the
    #: same reason :attr:`wedged` is one: a clock answers about a record whatever
    #: ended it. :func:`past_deadline` still returns an age for a run that a rung
    #: ABOVE the deadline ended — a sentinel, or the agent's own turn report — and
    #: that run finished the ordinary way, alive at its prompt with the task on the
    #: screen. Reaping it closes the window over the very thing the operator asked
    #: for.
    expired: bool = False

    @property
    def occupying(self) -> bool:
        return self.state in OCCUPYING

    def to_json(self) -> dict:
        return {"runId": self.run_id, "state": self.state, "reason": self.reason,
                "wedged": self.wedged, "expired": self.expired}


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


#: FNV-1a 64-bit, as :func:`pane_digest` computes it.
_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_FNV_MASK = 0xFFFFFFFFFFFFFFFF

#: Clock-shaped text, blanked out of a screen before it is fingerprinted.
#:
#: A dump carries the whole terminal, the multiplexer's furniture included, and tmux's
#: default status-right is a wall clock. So a pane showing nothing but a finished agent
#: changed once a minute forever, and :data:`QUIET_TIMEOUT` was unreachable on any box
#: whose shells wrap themselves in tmux — seven dumps of one idle agent over 72 seconds
#: gave three digests, differing in nothing but those five characters.
#:
#: A time of day and nothing else: not a token count, not an elapsed ``1h 34m``.
#: Everything else on that screen is something the agent itself wrote, and a screen
#: where only those digits move is the one thing this fingerprint is for.
_CLOCK = re.compile(r"[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?")


def pane_digest(tail: str) -> str:
    """A screen's fingerprint, for telling "unchanged" from "changed".

    FNV-1a rather than a cryptographic hash for one reason: both front-ends persist
    this into the SAME book, so the two must agree byte for byte or a hand-over
    restarts the stillness clock — and the Swift core is a Foundation-only target that
    builds on Linux, where ``CryptoKit`` does not exist. FNV-1a is a dozen lines in
    either language and needs nothing imported.

    Collision resistance is not a property this needs: the question asked of it is only
    whether THIS pane differs from what the last tick saw of the SAME pane.
    """
    h = _FNV_OFFSET
    for byte in _CLOCK.sub("~", tail).encode("utf-8", "replace"):
        h = ((h ^ byte) * _FNV_PRIME) & _FNV_MASK
    return f"{h:016x}"


def observe_quiescence(records: list[RunRecord], tails: Observation,
                       now: float) -> list[RunRecord]:
    """Refresh each run's record of when its screen last CHANGED, returning updated
    records.

    Beside :func:`observe_claims` and for the same reason: absence is only measurable
    against a memory of presence, and :func:`resolve` stays a function of its
    arguments alone. What is remembered is a digest rather than the screen itself —
    the book is rewritten every tick and read by other processes, so storing every
    watched pane's contents in it would be both large and pointless.

    A tail that could not be read updates nothing. That is what keeps a tmux server
    going down from looking like twenty minutes of stillness across every run at once:
    the clock only advances on ticks that actually SAW the screen, and it restarts
    from the first one that does.
    """
    if not tails.ok:
        return records
    seen: dict[str, str] = tails.value
    out = []
    for r in records:
        tail = seen.get(r.tty) if r.tty else None
        if tail is None:
            out.append(r)
            continue
        digest = pane_digest(tail)
        if digest != r.quiet_digest:
            out.append(replace(r, quiet_digest=digest, quiet_since=now))
        elif r.quiet_since is None:
            out.append(replace(r, quiet_since=now))
        else:
            out.append(r)
    return out


def went_quiet(record: RunRecord, now: float) -> float | None:
    """How long this run's screen has been perfectly still, once that is long enough
    to call it over — ``None`` otherwise.

    Asked by the resolver alone. The reaper reads the verdict that came out of it
    (:attr:`Resolution.wedged`) rather than asking again, because the two questions
    are not the same one: this clock only advances on ticks that SAW the screen, so
    it keeps maturing through an evidence outage and can be long past the timeout on
    a run that ended some other way entirely.
    """
    if record.quiet_since is None:
        return None
    quiet = now - record.quiet_since
    return quiet if quiet >= QUIET_TIMEOUT else None


# MARK: - The resolver


#: What each terminal verb means, for the reason line the debug dump prints.
_REPORTED_REASON = {
    completion.IDLE: "its CLI reported the turn over",
    completion.ENDED: "its CLI reported the session ended",
}


def _reported(record: RunRecord, evidence: Evidence) -> tuple[str, float] | None:
    """What this run last reported about itself, or ``None`` if it reports nothing.

    ``None`` covers three ordinary cases and is never evidence about the run: the
    probe could not read the directory, the run was spawned without hooks (a foreign
    runner, or settings that would not stage), and the seconds before a fresh run's
    first hook fires.
    """
    if not evidence.activity.ok:
        return None
    return evidence.activity.value.get(record.run_id)


def resolve(records: list[RunRecord], evidence: Evidence, now: float,
            deadline: float | None = None) -> dict[str, Resolution]:
    """Every run's state, from one pass of evidence. Pure."""
    return {r.run_id: resolve_one(r, evidence, now, deadline) for r in records}


def resolve_one(record: RunRecord, evidence: Evidence, now: float,
                deadline: float | None = None) -> Resolution:
    """One run's state, by a fixed ladder.

    The order is the precedence, and each rung is either positive evidence or an
    explicit refusal to guess:

    1. the PR landed — a terminal outcome that outranks whatever the process is doing;
    2. the completion sentinel exists — the agent returned an exit code;
    3. the agent's CLI reported its turn over — a report rather than an inference,
       and above what follows because a run that finished is alive at its prompt and
       every rung below this one sees a live process either way. A runner that keeps a
       session instead of running hooks says the same thing further down, once its pid
       is known alive;
    4. a mesh-peer run is judged by the executor's claim, because no probe on this
       machine can see a process on another one;
    5. a local run is judged by its pid, and its screen only classifies a pid that is
       already known to be alive;
    6. the deadline, when the operator has one, and LAST: it overrules only a RUNNING —
       the answer that means "this bay is spoken for and nothing here can say when it
       will come back". Every other answer the ladder reaches is a better one than a
       clock, and each of them is a reason this rung must not fire: ENDED already named
       how the run stopped, AWAITING_INPUT is a session at its prompt that gave its bay
       back and still holds a task worth reading, and UNKNOWN is the tick where the
       evidence could not be read at all — ending a run on that would retire it for
       being old on the one pass that saw nothing.
    """
    def done(state: str, reason: str) -> Resolution:
        return Resolution(record.run_id, state, reason)

    if evidence.merged_prs.ok and record.pr_number is not None:
        if record.pr_number in evidence.merged_prs.value:
            return done(MERGED, f"PR #{record.pr_number} is merged")

    if evidence.sentinels.ok and record.run_id in evidence.sentinels.value:
        return done(FINISHED, "completion sentinel present")

    reported = _reported(record, evidence)
    if reported is not None and completion.is_over(reported[0]):
        return done(FINISHED, _REPORTED_REASON[reported[0]])

    if record.placement == PLACEMENT_MESH_PEER:
        out = _resolve_peer(record, evidence, now, done)
    else:
        out = _resolve_local(record, evidence, now, done)
    if out.state != RUNNING:
        return out
    expired = past_deadline(record, evidence.tokens_left, now, deadline)
    if expired is None:
        return out
    # The one rung that stamps `expired`; see :attr:`Resolution.expired`. The answer it
    # overruled is kept in the reason: it is what the run looked like right up to the
    # moment a clock ended it, and the only account of that anyone gets.
    return replace(done(FINISHED,
                        f"{out.reason}; has run for {apiwatch.human_interval(expired)}"
                        f", past the {apiwatch.human_interval(deadline)} deadline"),
                   expired=True)


def past_deadline(record: RunRecord, tokens: Observation, now: float,
                  deadline: float | None) -> float | None:
    """How long this run has been going, once that is long enough to call it over —
    ``None`` when it is not, or when nothing here may call it over at all.

    Asked by the resolver alone, like :func:`went_quiet`, and for the same reason: an
    age is true of a run whatever ended it, so the window reaper reads the verdict that
    came out of this (:attr:`Resolution.expired`) rather than asking again. The age
    returned is the age the reason line quotes, so the verdict and the number the
    operator reads cannot come apart.

    Five things hold it back, each a case where the clock is measuring something other
    than a bay that will not come back:

    * **no deadline** — the operator switched the backstop off;
    * **no token reading, or none left** — see :data:`RUN_DEADLINE`;
    * **a run on somebody else's machine** — the reading above is THIS account's, and
      the peer's own claim already ends that run;
    * **a run the operator started by hand** — :func:`cap_load` counts only
      ``SOURCE_AUTO``, so a panel click holds no bay of the automatic cap and there is
      nothing here to hand back. All that ending one would buy is the loss of a working
      agent the operator is driving themselves;
    * **an untracked run** — its record comes from the scan rather than from a dispatch,
      so :func:`synthesize_untracked` rebuilds it on the very next tick with a fresh
      stamp: the bay comes back for one tick and the same agent takes it again. Its
      stamp is when the scan first SAW the agent, so the age here would not even be
      the run's.

    Every run this reaches is one :func:`cap_load` is counting — a bay is what there is
    to hand back — but not the reverse, and the gap is deliberate on both sides. An
    untracked run holds a bay and is exempt above. So is a run on a tick that resolved
    UNKNOWN, because OCCUPYING counts that and the RUNNING the caller requires does not:
    a bay held by a run nobody could look at this pass is a bay kept.
    """
    if deadline is None or not record.runs_here or record.untracked:
        return None
    if record.source != SOURCE_AUTO:
        return None
    if not (tokens.ok and tokens.value):
        return None
    age = now - record.dispatched_at
    return age if age >= deadline else None


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

    The pid is the identity — written by the inner shell, and naming the agent itself
    or the shell that wraps it, per ``review.shell_command``. Every rung below reads
    only what holds for both: the process is there, its argv is an agent's, and it is
    no younger than the record. Matching on it replaces reading ``PR #<n> in
    <owner>/<repo>`` out of a prompt in ``ps`` output, which could not tell two runs
    on one PR apart and matched any unrelated session that mentioned the number.
    """
    if not evidence.processes.ok:
        return done(UNKNOWN,
                    f"process table {evidence.processes.reason or 'unavailable'}")
    table: dict[int, ProcInfo] = evidence.processes.value
    age = now - record.dispatched_at

    if record.pid is None:
        if record.untracked:
            return _resolve_untracked(record, evidence, now, done)
        return _resolve_without_pid(record, evidence, now, age, done)

    proc = table.get(record.pid)
    if proc is None:
        # A pid the table has not caught up with, not a dead one: the pid file and the
        # table are read at different instants, and the table is one `ps` pass reused
        # for several seconds, so a pid written after that pass names a process it
        # structurally cannot hold. Read as death, a run is retired seconds into its
        # own spawn and its directory deleted under a working agent. The same record
        # one tick earlier, with no pid at all, had exactly this grace.
        if age <= SPAWN_GRACE:
            return done(STARTING, f"dispatched {age:.0f}s ago, pid {record.pid} "
                                  "not in the process table yet")
        return done(FINISHED, f"pid {record.pid} absent from the process table")
    if not proc.is_agent:
        return done(FINISHED, f"pid {record.pid} was recycled by another process")
    # A recycled pid can also be re-taken by another agent. The genuine one started
    # just after this record did, so anything materially younger is a stranger.
    if proc.elapsed < age - PID_ADOPTION_SLACK:
        return done(FINISHED,
                    f"pid {record.pid} is {proc.elapsed:.0f}s old but the run is "
                    f"{age:.0f}s old")
    return _classify_activity(record, evidence, now, done,
                              f"pid {record.pid} alive")


def _resolve_without_pid(record: RunRecord, evidence: Evidence, now: float,
                         age: float, done) -> Resolution:
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
        return _classify_activity(record, evidence, now, done,
                                  f"an agent is up on PR #{record.pr_number}")
    if age <= SPAWN_GRACE:
        return done(STARTING, f"dispatched {age:.0f}s ago, no pid yet")
    if record.pr_number is None:
        # Nothing to look for: a run with neither a pid nor a PR cannot be found by
        # either mechanism, so its absence is not evidence of anything.
        return done(UNKNOWN, f"no pid recorded {age:.0f}s after dispatch")
    return done(FINISHED, f"no agent for PR #{record.pr_number} in the process table")


def _resolve_untracked(record: RunRecord, evidence: Evidence, now: float,
                       done) -> Resolution:
    """A run synthesized from a sighting in the process table.

    It has no pid of its own and no dispatch stamp to be young against, so the scan
    that made it is also the only thing that can end it — and something must, because
    the record is kept across ticks for the stillness backstop's sake: one that
    outlived its agent would hold that PR against a fresh agent, and a bay of the cap,
    for the life of the applet.

    An unreadable scan ends nothing, like every other rung here. It is the sole
    evidence about this run, so "could not look" must not read as "it is gone".
    """
    if not evidence.live_agents.ok:
        return done(UNKNOWN,
                    f"the agent scan {evidence.live_agents.reason or 'failed'}")
    if record.pr_number in evidence.live_agents.value:
        return _classify_activity(record, evidence, now, done,
                                  "found in process table")
    return done(FINISHED, "gone from the process table")


def _classify_activity(record: RunRecord, evidence: Evidence, now: float, done,
                       alive_reason: str) -> Resolution:
    """Working, or finished its turn and waiting at the prompt?

    An agent is spawned into an INTERACTIVE session, so finishing its work is not
    exiting: it sits at the prompt until a human closes the window, and the process
    table shows the same live agent either way. Something has to separate the two.

    A run that reports its own turns never reaches here still working — the ladder
    above ends it — so what this rung answers for one is the other half: its CLI said a
    turn is in flight, which outranks anything read off a screen.

    For a run that reports nothing, the agent's own session is asked next, and its
    answer ENDS the run exactly as the CLI's own does: a runner that keeps a session
    and one that runs a hook are two spellings of "ask the agent". Read as merely idle,
    every OpenCode and Hermes run stayed in the book until somebody closed its window
    by hand.

    The screen is the last fallback, and it is an inference — it reads whether the
    CLI's interrupt hint was on the status bar when we looked, which is a string from
    someone else's UI that says nothing at all if they reword it. It is the one source
    here that cannot end a run: AWAITING_INPUT is what a stale hint reads as.

    Every gap here reads as RUNNING, which costs a bay rather than correctness — but
    it is also the one rung that fails silently, so the probe layer counts
    how often the tail is missing and says so out loud.

    The quiescence backstop is asked FIRST, ahead of every "it is working" answer,
    because it exists precisely to overrule one: a run whose screen has not changed in
    :data:`QUIET_TIMEOUT` is wedged whatever its status bar still claims, and the
    frozen ``esc to interrupt`` of an agent that died mid-turn is the exact case that
    otherwise holds a bay until a human closes the window.
    """
    quiet = went_quiet(record, now)
    if quiet is not None:
        # The one rung that stamps `wedged`; see :attr:`Resolution.wedged`.
        return replace(done(FINISHED, f"{alive_reason}; its screen has not changed in "
                                      f"{apiwatch.human_interval(quiet)}"), wedged=True)

    reported = _reported(record, evidence)
    if reported is not None and reported[0] == completion.BUSY:
        return done(RUNNING, f"{alive_reason}; its CLI reported a turn in flight")

    if evidence.sessions.ok:
        session: SessionState | None = evidence.sessions.value.get(record.run_id)
        if session is not None:
            if session.busy:
                return done(RUNNING, f"{alive_reason}; its session is mid-turn")
            return done(FINISHED, f"{alive_reason}; its runner reported the turn over")
    if not evidence.tails.ok:
        return done(RUNNING,
                    f"{alive_reason}; screen {evidence.tails.reason or 'unavailable'}")
    tails: dict[str, str] = evidence.tails.value
    tail = tails.get(record.tty) if record.tty else None
    if tail is None:
        return done(RUNNING, f"{alive_reason}; no screen for tty {record.tty or '?'}")
    if apiwatch.looks_busy(tail):
        return done(RUNNING, f"{alive_reason}; working")
    # An agent that has not started its first turn shows the same bare prompt as one
    # that has finished its last, so inside FIRST_TURN_GRACE this joins the "alive, and
    # we cannot yet tell" answers above rather than reading as idle. Only for a run we
    # dispatched: an untracked one is stamped when the scan first saw it, which says
    # nothing about when its agent started.
    age = now - record.dispatched_at
    if not record.untracked and age <= FIRST_TURN_GRACE:
        return done(RUNNING,
                    f"{alive_reason}; dispatched {age:.0f}s ago, no turn on screen yet")
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

    One is made once and then kept in the book like any other run, because the
    stillness backstop measures a screen against the last one seen and a record
    re-derived every tick remembers none: its clock never leaves zero, so it can never
    be found wedged and its window is never closed. The dedup on PR number is what
    keeps the next tick from making a second.

    They count as automatic. An agent whose trigger is unknown spending a bay defers
    work; the opposite error dispatches a second agent onto a PR that has one.
    """
    if not live_agents.ok:
        return records
    live = live_agents.value
    out = []
    for r in records:
        # A kept record follows its PR's current sighting: the scan reports one agent
        # per PR, so an operator's second session becomes that sighting the moment the
        # first exits. Its memory of the old screen goes with it, or the new window
        # inherits the old one's stillness.
        tty = live.get(r.pr_number) if r.untracked else None
        if tty and tty != r.tty:
            r = replace(r, tty=tty, quiet_digest="", quiet_since=None)
        out.append(r)
    known = {r.pr_number for r in out if r.pr_number is not None}
    for pr in sorted(set(live) - known):
        out.append(RunRecord(run_id=f"untracked:{pr}", dispatched_at=now,
                             pr_number=pr, source=SOURCE_AUTO,
                             placement=PLACEMENT_LOCAL,
                             tty=live[pr], untracked=True))
    return out


# MARK: - The projections
#
# Each is a fold over the resolved map. Nothing below re-reads evidence or re-derives
# a state, which is the whole point: the answers can disagree with each other only if
# this file is wrong, not if one call site of five drifted.


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

    Age alone retires nothing here: an hour-long review is an ordinary one, and a clock
    that ends records ends them mid-run. The one exception is
    :data:`RUN_DEADLINE`, which is on unless an operator turns it off — so this is
    the one thing said here that a default-on switch can make untrue.
    """
    return [r for r in records
            if r.run_id in states and states[r.run_id].state in ENDED]


def reapable(records: list[RunRecord],
             states: dict[str, Resolution]) -> list[RunRecord]:
    """The runs whose terminal is nobody's — ended by a CLOCK rather than by evidence
    that their agent stopped, so the agent may well still be sitting in the window.

    A projection rather than a test each front-end repeats, because it is the one
    destructive consequence a tick has: a window closed under a run this resolver still
    calls working takes the whole task's context with it.

    Which rung fired is ASKED (:attr:`Resolution.wedged`, :attr:`Resolution.expired`)
    rather than re-derived from the clocks, because a clock answers about a record
    whatever ended it. Both of them are still true of runs the rungs above them ended:
    :func:`went_quiet` keeps maturing across an evidence outage, and
    :func:`past_deadline` holds for the whole life of a long run that then reports its
    turn over the ordinary way. Those runs are absent from here — their agent is alive
    at its prompt holding the finished task, and the operator may still want to read
    it. So is a merged one, for the same reason.

    ``runs_here`` is not re-checked: both stamps already imply it — the stillness rung
    only runs inside :func:`_resolve_local`, and :func:`past_deadline` refuses a peer.
    """
    return [r for r in records
            if r.run_id in states
            and (states[r.run_id].wedged or states[r.run_id].expired)]


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
    #: Of those, the ones a backstop ended — whose window is closed as well as
    #: forgotten. See :func:`reapable`.
    reapable: list[RunRecord]
    free_slots: int
    #: The instant every verdict below was resolved against.
    now: float = 0.0

    def in_flight(self, pr_number: int) -> bool:
        return in_flight(self.records, self.states, pr_number)


def tick(records: list[RunRecord], evidence: Evidence, now: float, limit: int,
         deadline: float | None = None) -> Tick:
    """Fold one pass of evidence into every answer, in the one order that is correct.

    The order is the reason this is a function rather than a convention each caller
    repeats: claims are observed and ttys adopted BEFORE resolving, so both count this
    tick rather than a tick late; quiescence is observed after ttys are adopted, since
    a run with no tty yet has no screen to compare; and untracked agents are
    synthesized AFTER, so a live agent that already has a record is not drawn twice — and so it keeps the tty
    the scan found it on rather than having one adopted for a pid it does not have.
    Both front-ends and the parity CLI go through here, so neither can get the
    sequence subtly different from the other.
    """
    records = observe_claims(records, evidence.claims, now)
    records = adopt_ttys(records, evidence.processes, evidence.live_agents)
    records = observe_quiescence(records, evidence.tails, now)
    records = synthesize_untracked(records, evidence.live_agents, now)
    states = resolve(records, evidence, now, deadline)
    load = cap_load(records, states)
    return Tick(records=records, states=states, rows=rows(records, states),
                cap_load=load, retirable=retirable(records, states),
                reapable=reapable(records, states),
                free_slots=free_slots(limit, len(load)), now=now)
