"""The impure half of agent-state detection: the outside world, typed.

:mod:`diplomat_runtime.agentstate` decides; this module is the only thing that looks.
Every probe here returns an :class:`~diplomat_runtime.agentstate.Observation`, so a
failure to look is carried to the resolver as a failure to look rather than as an
empty answer — which is the whole reason the resolver can refuse to guess.

The split is also what makes the resolver testable: a scenario is a dict literal
because nothing below is in the call path.

Nothing here raises. A probe that cannot answer says so and the tick continues; the
worst outcome of a broken probe is rows that read "unknown" and a cap that holds its
bays, never an agent declared finished.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass

from diplomat_runtime import apiwatch, core, runner, tmuxwatch
from diplomat_runtime.agentstate import UNAVAILABLE, Evidence, Observation, ProcInfo, RunRecord

#: How long a probe's answer is reused. The resolver re-runs for every question the
#: applet asks — that is deliberate, so no answer is ever stale — which makes THIS the
#: only place the cost is paid. Short enough that a poll never acts on an old machine,
#: long enough that one cycle spawns one ``ps`` and one ``tmux`` rather than a dozen.
_CACHE_SECS = 5

_ps_cache: tuple[float, str] | None = None
_tails_cache: tuple[float, frozenset, Observation] | None = None
_sessions_cache: tuple[float, frozenset, Observation] | None = None

#: How each probe last answered, and how long it has been failing. A probe that goes
#: quiet is the failure mode with no symptom of its own — the applet keeps drawing
#: rows and simply believes something untrue — so the fact is kept and reported
#: rather than left to be inferred from behaviour.
_health: dict[str, "ProbeHealth"] = {}

#: How many agent screens have been read, and how many of them showed a CLI's
#: interrupt hint. The hints are literal strings from someone else's UI
#: (:data:`apiwatch.BUSY_MARKERS`), and if they ever stop matching, every agent reads
#: as idle at once: the cap empties and the monitors burst. Nothing else would say so
#: — the applet would look like it was working perfectly — so the ratio is counted.
_tails_read = 0
_marker_seen = 0


@dataclass
class ProbeHealth:
    """One probe's standing: what it last said, and for how long."""

    name: str
    status: str = UNAVAILABLE
    reason: str = ""
    last_ok_at: float | None = None
    consecutive_failures: int = 0

    @property
    def silent(self) -> bool:
        """Has this probe failed for long enough to be worth saying out loud?

        UNSUPPORTED never counts: a machine without tmux, or without the mesh add-on,
        is an ordinary machine and warning about it every few minutes would be noise
        that trains the operator to ignore the channel.
        """
        return self.status == UNAVAILABLE and self.consecutive_failures >= _SILENT_AFTER


#: Consecutive failed ticks before a probe is called silent. The panel resolves every
#: few seconds, so this is under a minute — long enough that a tmux restart or a
#: momentary `ps` failure passes unremarked.
_SILENT_AFTER = 10


def _note(name: str, obs: Observation, now: float) -> Observation:
    """Record how a probe answered, and pass the answer through."""
    h = _health.setdefault(name, ProbeHealth(name=name))
    h.status, h.reason = obs.status, obs.reason
    if obs.ok:
        h.last_ok_at = now
        h.consecutive_failures = 0
    elif obs.status == UNAVAILABLE:
        h.consecutive_failures += 1
    else:
        h.consecutive_failures = 0
    return obs


def health() -> list[ProbeHealth]:
    """Every probe's standing, in a stable order."""
    return [_health[k] for k in sorted(_health)]


def marker_stats() -> tuple[int, int]:
    """``(screens read, screens that showed the interrupt hint)``."""
    return _tails_read, _marker_seen


def _ps_dump(now: float) -> Observation:
    """One ``ps`` pass, briefly cached, as ``pid tty etimes args…`` lines.

    ``UnicodeDecodeError`` is caught by name: ``text=True`` decodes strict UTF-8, any
    process on the box with a non-UTF-8 byte in its argv makes the whole dump
    undecodable, and it is a ``ValueError`` — so it escapes an
    ``(OSError, SubprocessError)`` guard and wedges the caller. That is not
    hypothetical; the same bug once killed the API-error watcher every poll for as
    long as one such pane existed.

    A non-zero exit is UNAVAILABLE rather than the stdout it managed to produce: that
    stdout is truncated or empty, and a short table is not "those processes are gone" —
    every local run whose pid fell off it resolves FINISHED in the same tick, so one
    failed ``ps`` empties the book under live agents. ``AgentProbes.run`` drops one for
    the same reason.
    """
    global _ps_cache
    if _ps_cache is not None and now - _ps_cache[0] < _CACHE_SECS:
        return Observation.present(_ps_cache[1])
    try:
        proc = subprocess.run(["ps", "-eo", "pid=,tty=,etimes=,args="],
                              capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        return Observation.unavailable(f"could not be read ({type(exc).__name__})")
    if proc.returncode != 0:
        return Observation.unavailable(f"exited {proc.returncode}")
    _ps_cache = (now, proc.stdout)
    return Observation.present(proc.stdout)


def reset_cache() -> None:
    """Drop every probe cache and every counter — for tests that change the machine
    between assertions inside one cache window."""
    global _ps_cache, _tails_cache, _sessions_cache, _tails_read, _marker_seen
    _ps_cache = None
    _tails_cache = None
    _sessions_cache = None
    _tails_read = 0
    _marker_seen = 0
    _health.clear()


def process_table(dump: Observation) -> Observation:
    """pid → what the process table says about it.

    ``etimes`` (whole seconds since start) is what the resolver's pid-adoption guard
    compares against a run's age; the argv decides ``is_agent``, using the same loose
    "the line mentions a runner's CLI" test the legacy scan used, because a wrapper
    shell and an agent both carrying the word is exactly what the age half of the
    guard is for.
    """
    if not dump.ok:
        return Observation.unavailable(dump.reason)
    table: dict[int, ProcInfo] = {}
    for line in dump.value.splitlines():
        parts = line.split(maxsplit=3)
        if len(parts) < 4:
            continue
        pid_s, tty, elapsed_s, args = parts
        try:
            pid, elapsed = int(pid_s), float(elapsed_s)
        except ValueError:
            continue
        table[pid] = ProcInfo(tty=tty.removeprefix("/dev/") if tty != "?" else "",
                              elapsed=elapsed, is_agent=runner.is_agent_line(args))
    return Observation.present(table)


def ttys_running_an_agent(now: float) -> Observation:
    """The ttys an agent process is on, spelled as ``ps`` spells them (``pts/13``).

    Which screens belong to somebody's agent, for the caller that has to ask it the
    other way round from :func:`pane_tails`: the API-error watcher reads every pane on
    the tmux server, and what it does with a match is TYPE into it. On a pane running
    an agent that is a user turn; on a plain shell it is a command, executed. Nothing
    on a screen can tell those two apart — a pane can be showing a banner because it
    printed one — so the process on the tty is the only thing that can.

    Any runner and any task, unlike :func:`autofix.agent_ttys`, which answers for the
    agents of one repo's PRs: the question here is whether a human's shell is about to
    be typed into, and an agent reviewing nothing in particular is still an agent.

    A process with no controlling tty is left out — it owns no screen to be nudged.
    """
    table = process_table(_ps_dump(now))
    if not table.ok:
        return Observation.unavailable(table.reason)
    return Observation.present(
        {p.tty for p in table.value.values() if p.is_agent and p.tty})


def live_agents(dump: Observation) -> Observation:
    """PR number -> the tty of an agent visible in ``ps`` by its prompt text.

    The pre-registry identity mechanism, kept for the two questions a pid cannot
    answer: agents this applet has no record of at all
    (:func:`agentstate.synthesize_untracked`), and records whose agent has no pid to
    match — a placement the mesh routed back here, where the NODE opened the terminal
    and wrote the pid file into a run directory this applet never created.

    It cannot tell two runs on one PR apart and it matches any session that merely
    mentions the number, which is why it decides nothing that a pid can decide.

    The tty rides along because it is the only handle such an agent has: without it
    nothing can read its screen, so it would count as working until its window closed
    however long ago it finished. First sighting of a PR wins — a set of PR numbers is
    all this scan can honestly produce.
    """
    if not dump.ok:
        return Observation.unavailable(dump.reason)
    cfg = core.config()
    pattern = re.compile(
        r"PR #(\d+) in " + re.escape(f"{cfg['owner']}/{cfg['repo']}"))
    out: dict[int, str] = {}
    # Parsed here against THIS dump's columns rather than through
    # `autofix.agent_lines`, which reads the tty as the FIRST token of a
    # `tty=,etime=,args=` dump. That is still right for its own caller (the mesh
    # node's capacity hook, which spells `ps` the portable way), and was silently
    # wrong here the moment this probe started asking for a pid column too: every
    # agent came back keyed to a tty that was really a pid, so no screen could ever
    # be found for one and no untracked agent ever gave its bay back.
    for line in dump.value.splitlines():
        if not runner.is_agent_line(line):
            continue
        parts = line.split(maxsplit=3)
        if len(parts) < 4:
            continue
        _pid, tty, _elapsed, args = parts
        for m in pattern.finditer(args):
            out.setdefault(int(m.group(1)),
                           "" if tty == "?" else tty.removeprefix("/dev/"))
    return Observation.present(out)


def pane_tails(records: list[RunRecord], now: float = 0.0) -> Observation:
    """tty → the visible buffer of the pane on it, for the runs it is given.

    Selective on purpose: this runs on the panel's 8-second tick, so it is one
    ``capture-pane`` per agent rather than one per pane on the developer's box.

    The three answers are genuinely different and the resolver acts on the difference.
    No tmux at all is UNSUPPORTED — an ordinary machine, not a fault, and not
    something to warn about. A tmux that is there but would not answer is
    UNAVAILABLE, so every live agent reads "running" rather than being mistaken for
    idle and having its bay taken back. Only a real capture is PRESENT.
    """
    global _tails_cache
    if shutil.which("tmux") is None:
        return Observation.unsupported("is not available (tmux is not installed)")
    ttys = frozenset(r.tty for r in records if r.tty)
    # Keyed on the ttys as well as the clock: a run that appeared since the last pass
    # would otherwise be answered from a capture that never asked about it.
    if (_tails_cache is not None and _tails_cache[1] == ttys
            and now - _tails_cache[0] < _CACHE_SECS):
        return _tails_cache[2]
    tails = tmuxwatch.pane_tails_for_ttys(set(ttys))
    if tails is None:
        obs = Observation.unavailable("is unreadable (no tmux server, or it would "
                                      "not answer)")
    else:
        obs = Observation.present(tails)
        global _tails_read, _marker_seen
        _tails_read += len(tails)
        _marker_seen += sum(1 for t in tails.values() if apiwatch.looks_busy(t))
    _tails_cache = (now, ttys, obs)
    return obs


#: Most sessions considered when matching a run to its own. Ordinarily there is one —
#: each runner's ``candidates`` filters are narrow — and the cap only bites when a run
#: never binds at all, where it is what stops a fruitless search costing one opening
#: message read per stale session on every tick, forever.
_MAX_CANDIDATES = 4


def agent_sessions(records: list[RunRecord], directory: str,
                   now: float = 0.0) -> Observation:
    """run id → what that run's own agent says it is doing.

    Positive evidence where the pane gives an inference: a turn the runner itself
    marks finished, rather than whether someone else's status bar happened to have its
    interrupt hint drawn when we looked.

    Two runners answer, from different places — OpenCode over the loopback port its
    spawn reserved (:mod:`opencodeapi`), Hermes out of the SQLite store it keeps every
    session in (:mod:`hermesstore`) — and both come back as the same typed answer, so
    nothing downstream learns which runner it is looking at.

    A run missing from the answer is a run this cannot reach: every Claude Code run,
    an OpenCode run spawned without a port, one whose server has not come up yet, one
    whose session has not been written to yet. The resolver reads its screen instead,
    so absence here costs the older evidence and never a verdict.

    UNSUPPORTED when no tracked run has such a session at all — a machine running
    Claude Code is an ordinary machine, not one whose probe has gone quiet.

    Cached for the same window as the other probes, and for a sharper reason: this one
    dials a socket, the resolver re-runs for every question the applet asks, and the
    panel asks two of them per repaint — so uncached, one unresponsive port costs a
    repaint two full per-run timeouts on the Qt thread. The staleness it buys is the
    one the pane tail already has for the very same busy-or-idle decision.

    The directory is resolved because both stores record the agent's own ``getcwd()``,
    which is physical, while the configured repo root is whatever the operator typed —
    and the match is exact equality, so one symlink between them and no run ever binds.
    """
    from diplomat_runtime import agentregistry

    global _sessions_cache
    asking = [(r, agentregistry.run_runner(r.run_id)) for r in records]
    asking = [(r, name) for r, name in asking if name in _BACKENDS]
    if not asking:
        return Observation.unsupported("are unavailable (no run serves a session of "
                                       "its own)")
    # Keyed on the runs as well as the clock: a run dispatched since the last pass
    # would otherwise be answered from a sweep that never asked about it.
    key = frozenset(r.run_id for r, _ in asking)
    if (_sessions_cache is not None and _sessions_cache[1] == key
            and now - _sessions_cache[0] < _CACHE_SECS):
        return _sessions_cache[2]
    directory = os.path.realpath(directory)
    taken = {agentregistry.bound_session(r.run_id) for r, _ in asking}
    taken.discard("")
    out = {}
    # In dispatch order, so the runs that have already matched a session are out of the
    # way before a newer one goes looking — `taken` is only a useful filter if it is
    # filled in the order the sessions were created.
    for record, name in sorted(asking, key=lambda pair: pair[0].dispatched_at):
        backend = _BACKENDS[name]
        session_id = agentregistry.bound_session(record.run_id)
        if not session_id:
            session_id = backend.bind(record, directory, taken)
            if not session_id:
                continue
            agentregistry.bind_session(record.run_id, session_id)
            taken.add(session_id)
        state = backend.state(record, session_id)
        if state is not None:
            out[record.run_id] = state
    obs = Observation.present(out)
    _sessions_cache = (now, key, obs)
    return obs


class _OpenCodeBackend:
    """A run's own OpenCode server, on the port its spawn reserved."""

    @staticmethod
    def bind(record: RunRecord, directory: str, taken: set[str]) -> str:
        """Which session on this run's server is this run's, by its opening prompt.

        Every run has its own server but they share one session store, so the port
        alone narrows nothing — the same shared history answers whichever port it is
        asked on. The directory narrows it to this checkout, and the server does that
        much itself (:func:`opencodeapi.sessions`). The prompt is what makes the match
        exact, and exact is worth the fetch: the applet runs several agents in one
        checkout at a time, so two sessions a second apart in the same directory is
        the ordinary case, not the pathological one.
        """
        from diplomat_runtime import agentregistry, opencodeapi

        port = agentregistry.port(record.run_id)
        if port is None:
            return ""
        listing = opencodeapi.sessions(port, directory)
        if listing is None:
            return ""
        prompt = _staged_prompt(record.run_id)
        if prompt is None:
            return ""
        found = opencodeapi.candidates(listing, directory,
                                       record.dispatched_at * 1000.0, taken)
        for session_id in found[:_MAX_CANDIDATES]:
            if opencodeapi.is_ours(opencodeapi.messages(port, session_id) or [],
                                   prompt):
                return session_id
        return ""

    @staticmethod
    def state(record: RunRecord, session_id: str):
        """Both halves of the answer — see :func:`opencodeapi.state_of` for which
        blind spot each of them covers."""
        from diplomat_runtime import agentregistry, opencodeapi

        port = agentregistry.port(record.run_id)
        if port is None:
            return None
        statuses = opencodeapi.statuses(port)
        running = (None if statuses is None
                   else opencodeapi.is_running(statuses, session_id))
        # Nothing is fetched to pair with a status that is not there: the answer is
        # already "ask the screen", and an unreachable port charges a full timeout.
        messages = ([] if running is None
                    else opencodeapi.messages(port, session_id, limit=1) or [])
        return opencodeapi.state_of(messages, running)


class _HermesBackend:
    """Hermes' own session store, which it writes as it works."""

    @staticmethod
    def bind(record: RunRecord, directory: str, taken: set[str]) -> str:
        from diplomat_runtime import hermesstore

        prompt = _staged_prompt(record.run_id)
        if prompt is None:
            return ""
        for session_id in hermesstore.candidates(
                directory, record.dispatched_at, taken)[:_MAX_CANDIDATES]:
            if hermesstore.is_ours(session_id, prompt):
                return session_id
        return ""

    @staticmethod
    def state(record: RunRecord, session_id: str):
        from diplomat_runtime import hermesstore

        return hermesstore.state_of(session_id)


def _staged_prompt(run_id: str) -> str | None:
    from diplomat_runtime import agentregistry

    try:
        return agentregistry.prompt_path(run_id).read_text(encoding="utf-8")
    except OSError:
        return None


#: Which store answers for which runner. A runner absent from here serves nothing and
#: is read off its screen — that is Claude Code, and a run whose runner was never
#: recorded.
_BACKENDS = {runner.OPENCODE: _OpenCodeBackend, runner.HERMES: _HermesBackend}


def mesh_claims(enabled: bool = True) -> Observation:
    """The work keys currently claimed anywhere on the mesh.

    UNSUPPORTED without the add-on installed or with the mesh switched off,
    UNAVAILABLE when the node is installed but not answering - a peer's run must not
    be retired because the local node is down, which is the difference the resolver
    reads. Switched off comes first: a machine that ran a node once keeps its last
    snapshot, and a node that was stopped on purpose is not a probe gone quiet.
    """
    if not enabled:
        return Observation.unsupported("are unavailable (the mesh is switched off)")
    try:
        from szpontnet import statefile
    except ImportError:
        return Observation.unsupported("are unavailable (no mesh add-on installed)")
    try:
        state = statefile.read_state()
    except OSError as exc:
        return Observation.unavailable(f"are unreadable ({type(exc).__name__})")
    # No snapshot at all means no node has ever run here, which is an ordinary machine
    # rather than a broken one — UNSUPPORTED, so it is never reported as a probe that
    # has gone quiet. A machine that HAS run one and is not running it now is a real
    # gap, because a peer's run is retired by a released claim and a node we cannot ask
    # must not be mistaken for one that released it.
    if not state:
        return Observation.unsupported("are unavailable (no mesh node has run here)")
    if not statefile.node_running(state):
        return Observation.unavailable("are unavailable (the mesh node is not running)")
    claims = state.get("claims")
    if not isinstance(claims, dict):
        return Observation.unavailable("are missing from the node's snapshot")
    return Observation.present(set(claims))


def merged_prs(pr_numbers: set[int]) -> Observation:
    """Which of these PRs GitHub calls MERGED — the one terminal outcome that
    outranks anything a process is doing.

    One ``gh`` call per PR, so this belongs on the slow refresh, not the 8-second
    tick. A PR whose probe fails is simply absent from the answer; the whole probe is
    UNAVAILABLE only when there was nothing to ask about, so a partial answer is
    still positive evidence about the PRs it covers.
    """
    if not pr_numbers:
        return Observation.present(set())
    from diplomat_runtime import gh
    merged = set()
    for n in sorted(pr_numbers):
        try:
            out = gh.run(["pr", "view", str(n), "--json", "state", "-q", ".state"])
        except Exception:  # noqa: BLE001 - a probe never raises into the tick
            continue
        if (out or "").strip() == "MERGED":
            merged.add(n)
    return Observation.present(merged)


def tokens_left(runners: Iterable[str] = ()) -> Observation:
    """Whether the accounts this machine's in-flight agents spend still have room in
    them — the precondition on the resolver's run deadline.

    ``runners`` is which agent CLI each of those runs was started under; see
    :func:`autobudget.tokens_left` for why the reading is about them rather than about
    the runner the next spawn would use.

    UNSUPPORTED covers every "no reading", including a ceiling that exists but would not
    answer — :func:`autobudget.tokens_left` returns ``None`` for a probe switched off, a
    box with no Claude Code login, and an endpoint that refused alike. That is not a
    distinction lost by accident: nothing downstream makes one. The resolver reads
    UNSUPPORTED and UNAVAILABLE identically ("not the positive answer the deadline
    needs"), and unlike its sibling UNSUPPORTED probes this observation is not
    registered with :func:`_note`, so neither status reaches the probe-health watch.

    The consequence is worth stating out loud, because it is silent: on a machine whose
    usage endpoint is rate-limiting — one small per-account bucket, shared by every
    Claude Code session on the box — the deadline is disarmed while its switch still
    reads ON. That is the safe direction (nothing is retired on a reading nobody took),
    but it is not the visible one.
    """
    from diplomat_runtime import autobudget

    answer = autobudget.tokens_left(runners)
    if answer is None:
        return Observation.unsupported(
            "are unavailable (no spending limit this machine can read)")
    return Observation.present(answer)


def gather(records: list[RunRecord], now: float, *,
           merged: Observation | None = None,
           tokens: Observation | None = None,
           mesh_enabled: bool = True) -> Evidence:
    """One pass of every cheap probe.

    ``merged`` and ``tokens`` are passed in rather than probed here: one costs a ``gh``
    call per PR and the other an HTTPS round trip, and neither belongs on a tick that
    also runs on the panel's repaint. Each is whatever the store last carried, and
    UNAVAILABLE until something refreshes it — which for ``merged`` on this platform is
    nothing at all, see #111.
    """
    from diplomat_runtime import agentregistry, agentstate, review

    dump = _ps_dump(now)
    table = _note("processes", process_table(dump), now)
    # Which panes are worth capturing is decided from the process table, not from the
    # records as they arrived: a run's tty lives on its agent process, and a run
    # spawned since the last tick has not adopted one yet. Asking the records alone
    # would capture nothing for exactly the run that just started, and it would then
    # read as working for a whole tick longer than it was.
    scan = _note("agent scan", live_agents(dump), now)
    looked_up = agentstate.adopt_ttys(records, table, scan)
    # Synthesized here as well as in `tick`, which adds them only AFTER this bundle is
    # built — so the tick that FIRST sees one would resolve it against a screen nobody
    # captured. It has no run directory, so no sentinel and no session to ask either:
    # its screen is the whole of the evidence about it, and left out it reads as
    # working until its window closes.
    looked_up = agentstate.synthesize_untracked(looked_up, scan, now)
    return Evidence(
        activity=_note("turn reports", agentregistry.activity(records), now),
        processes=table,
        sentinels=_note("sentinels", agentregistry.sentinels(records), now),
        tails=_note("screens", pane_tails(looked_up, now), now),
        claims=_note("mesh claims", mesh_claims(mesh_enabled), now),
        merged_prs=merged or Observation.unavailable("have not been probed yet"),
        live_agents=scan,
        sessions=_note("agent sessions",
                       agent_sessions(records, review.repo_path(), now), now),
        tokens_left=tokens or Observation.unavailable("has not been probed yet"),
    )
