"""The impure half of agent-state detection: the outside world, typed.

:mod:`diplomat_app.agentstate` decides; this module is the only thing that looks.
Every probe here returns an :class:`~diplomat_app.agentstate.Observation`, so a
failure to look is carried to the resolver as a failure to look rather than as an
empty answer — which is the whole reason the resolver can refuse to guess.

The split is also what makes the resolver testable: a scenario is a dict literal
because nothing below is in the call path.

Nothing here raises. A probe that cannot answer says so and the tick continues; the
worst outcome of a broken probe is rows that read "unknown" and a cap that holds its
bays, never an agent declared finished.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass

from . import apiwatch, core, tmuxwatch
from .agentstate import UNAVAILABLE, Evidence, Observation, ProcInfo, RunRecord

#: How long a probe's answer is reused. The resolver re-runs for every question the
#: applet asks — that is deliberate, so no answer is ever stale — which makes THIS the
#: only place the cost is paid. Short enough that a poll never acts on an old machine,
#: long enough that one cycle spawns one ``ps`` and one ``tmux`` rather than a dozen.
_CACHE_SECS = 5

_ps_cache: tuple[float, str] | None = None
_tails_cache: tuple[float, frozenset, Observation] | None = None

#: How each probe last answered, and how long it has been failing. A probe that goes
#: quiet is the failure mode with no symptom of its own — the applet keeps drawing
#: rows and simply believes something untrue — so the fact is kept and reported
#: rather than left to be inferred from behaviour.
_health: dict[str, "ProbeHealth"] = {}

#: How many agent screens have been read, and how many of them showed the CLI's
#: interrupt hint. The hint is a literal string from someone else's UI
#: (:data:`apiwatch.BUSY_MARKER`), and if it ever stops matching, every agent reads as
#: idle at once: the cap empties and the monitors burst. Nothing else would say so —
#: the applet would look like it was working perfectly — so the ratio is counted.
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
    """
    global _ps_cache
    if _ps_cache is not None and now - _ps_cache[0] < _CACHE_SECS:
        return Observation.present(_ps_cache[1])
    try:
        out = subprocess.run(["ps", "-eo", "pid=,tty=,etimes=,args="],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        return Observation.unavailable(f"could not be read ({type(exc).__name__})")
    _ps_cache = (now, out)
    return Observation.present(out)


def reset_cache() -> None:
    """Drop every probe cache and every counter — for tests that change the machine
    between assertions inside one cache window."""
    global _ps_cache, _tails_cache, _tails_read, _marker_seen
    _ps_cache = None
    _tails_cache = None
    _tails_read = 0
    _marker_seen = 0
    _health.clear()


def process_table(dump: Observation) -> Observation:
    """pid → what the process table says about it.

    ``etimes`` (whole seconds since start) is what the resolver's pid-adoption guard
    compares against a run's age; the argv decides ``is_agent``, using the same
    "the line mentions claude" test the legacy scan used, because a wrapper shell and
    an agent both carrying the word is exactly what the age half of the guard is for.
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
                              elapsed=elapsed, is_agent="claude" in args)
    return Observation.present(table)


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
    # `autofix.agent_lines`, which reads the tty as the first token of a
    # `tty=,args=` dump. That is still right for its own caller (the mesh node's
    # capacity hook, which spells `ps` the portable way), and was silently wrong
    # here the moment this probe started asking for a pid column too: every agent
    # came back keyed to a tty that was really a pid, so no screen could ever be
    # found for one and no untracked agent ever gave its bay back.
    for line in dump.value.splitlines():
        if "claude" not in line:
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
    """tty → the visible buffer of the pane on it, for the runs we are tracking.

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


def mesh_claims() -> Observation:
    """The work keys currently claimed anywhere on the mesh.

    UNSUPPORTED without the add-on installed, UNAVAILABLE when the node is installed
    but not answering — a peer's run must not be retired because the local node is
    down, which is the difference the resolver reads.
    """
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
    from . import gh
    merged = set()
    for n in sorted(pr_numbers):
        try:
            out = gh.run(["pr", "view", str(n), "--json", "state", "-q", ".state"])
        except Exception:  # noqa: BLE001 - a probe never raises into the tick
            continue
        if (out or "").strip() == "MERGED":
            merged.add(n)
    return Observation.present(merged)


def gather(records: list[RunRecord], now: float, *,
           merged: Observation | None = None) -> Evidence:
    """One pass of every cheap probe.

    ``merged`` is passed in rather than probed here: it costs a ``gh`` call per PR and
    belongs to the slow refresh, so the fast tick carries forward whatever the last
    one found (UNAVAILABLE until the first).
    """
    from . import agentregistry

    from . import agentstate

    dump = _ps_dump(now)
    table = _note("processes", process_table(dump), now)
    # Which panes are worth capturing is decided from the process table, not from the
    # records as they arrived: a run's tty lives on its agent process, and a run
    # spawned since the last tick has not adopted one yet. Asking the records alone
    # would capture nothing for exactly the run that just started, and it would then
    # read as working for a whole tick longer than it was.
    scan = _note("agent scan", live_agents(dump), now)
    looked_up = agentstate.adopt_ttys(records, table, scan)
    return Evidence(
        processes=table,
        sentinels=_note("sentinels", agentregistry.sentinels(records), now),
        tails=_note("screens", pane_tails(looked_up, now), now),
        claims=_note("mesh claims", mesh_claims(), now),
        merged_prs=merged or Observation.unavailable("have not been probed yet"),
        live_agents=scan,
    )
