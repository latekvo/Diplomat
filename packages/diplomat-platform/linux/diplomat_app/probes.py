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

import shutil
import subprocess

from . import autofix, core, tmuxwatch
from .agentstate import Evidence, Observation, ProcInfo, RunRecord

#: How long a probe's answer is reused. The resolver re-runs for every question the
#: applet asks — that is deliberate, so no answer is ever stale — which makes THIS the
#: only place the cost is paid. Short enough that a poll never acts on an old machine,
#: long enough that one cycle spawns one ``ps`` and one ``tmux`` rather than a dozen.
_CACHE_SECS = 5

_ps_cache: tuple[float, str] | None = None
_tails_cache: tuple[float, frozenset, Observation] | None = None


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
    """Drop every probe cache — for tests that change the machine between assertions
    inside one cache window."""
    global _ps_cache, _tails_cache
    _ps_cache = None
    _tails_cache = None


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

    The pre-registry identity mechanism, kept for exactly one job: finding agents this
    applet has no record of (:func:`agentstate.synthesize_untracked`). It cannot tell
    two runs on one PR apart and it matches any session that merely mentions the
    number, which is why nothing else is decided by it any more.

    The tty rides along because it is the only handle such an agent has: without it
    nothing can read its screen, so it would count as working until its window closed
    however long ago it finished. First sighting of a PR wins — a set of PR numbers is
    all this scan can honestly produce.
    """
    if not dump.ok:
        return Observation.unavailable(dump.reason)
    cfg = core.config()
    out: dict[int, str] = {}
    for tty, pr in autofix.agent_lines(dump.value, cfg["owner"], cfg["repo"]):
        out.setdefault(pr, tty)
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
    obs = (Observation.unavailable("is unreadable (no tmux server, or it would not "
                                   "answer)")
           if tails is None else Observation.present(tails))
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
    if not state or not statefile.node_running(state):
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
           merged: Observation | None = None) -> tuple[Evidence, Observation]:
    """One pass of every cheap probe, plus the legacy live-agent scan.

    Returns the bundle and the live-agent observation separately because they are used
    at different points of the tick — the second only synthesizes rows for agents that
    have no record.

    ``merged`` is passed in rather than probed here: it costs a ``gh`` call per PR and
    belongs to the slow refresh, so the fast tick carries forward whatever the last
    one found (UNAVAILABLE until the first).
    """
    from . import agentregistry

    dump = _ps_dump(now)
    return (
        Evidence(
            processes=process_table(dump),
            sentinels=agentregistry.sentinels(records),
            tails=pane_tails(records, now),
            claims=mesh_claims(),
            merged_prs=merged or Observation.unavailable("have not been probed yet"),
        ),
        live_agents(dump),
    )
