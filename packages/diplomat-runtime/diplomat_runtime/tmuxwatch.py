"""tmux terminal I/O for the Claude-API-error watcher — the Linux stand-in for the
iTerm/Terminal AppleScript in ApiErrorWatcher.swift.

macOS can read a terminal window's visible buffer and type into it through the
scriptable iTerm/Terminal apps. Linux has no such hook for arbitrary emulators
(gnome-terminal, konsole, …) — you can neither read what's rendered nor inject input.
tmux is the one portable mechanism that does both: ``capture-pane`` returns a pane's
visible screen, ``send-keys`` submits a line to it. So the watcher drives tmux panes;
an agent must be running inside tmux to be watched (the feature is simply inert
otherwise, exactly as the macOS watcher is when no terminal app it can read is
running).

macOS runs the same mechanism beside its two scripts rather than instead of them
(``TerminalFocus.paneScreens`` / ``sendLine``), because Ghostty is scriptable in every
way but this one: it will open a window on command and close one, but it reports
neither the text on a surface nor the tty it is on. So a Ghostty agent's screen has
exactly one reader, and it is this one.

Panes are keyed by their tmux ``pane_id`` (``%N``) — unique and never recycled for
the life of the server, unlike a ``/dev/pts`` tty which is reused as panes close.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from .apiwatch import last_lines

# A listing's fields are space-separated, none of them free text: a control byte
# does not survive tmux's output. 3.4 escapes it as octal, and a client with no $TMUX
# and no UTF-8 in LC_ALL/LC_CTYPE/LANG - what launchd, an autostart entry and CI give
# the applet - has it sanitized to "_".


@dataclass(frozen=True)
class Pane:
    pane_id: str  # tmux "%N" — the stable key
    tty: str  # "/dev/pts/N" — for the audit line only
    tail: str  # last SCANNED_TAIL_LINES non-empty visible rows


def is_available() -> bool:
    """tmux is installed AND a server is running (there are panes to watch)."""
    if shutil.which("tmux") is None:
        return False
    return _server_running()


def _server_running() -> bool:
    try:
        r = subprocess.run(
            ["tmux", "has-session"],
            capture_output=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def dump_panes() -> list[Pane] | None:
    """Every tmux pane's last visible lines, keyed by pane_id.

    Returns ``None`` when tmux is present but a command FAILED unexpectedly — the
    caller treats that as "unknown" and skips the scan rather than clearing all
    backoff state (mirrors ApiErrorWatcher.dumpSessions returning nil). Returns an
    empty list — a *known* "no panes" — when tmux isn't installed or no server is
    running; those are ordinary inert states, not failures.
    """
    if shutil.which("tmux") is None:
        return []
    listing = _run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_tty}"]
    )
    if listing is None:
        # Distinguish "no server running" (inert, known-empty) from a real failure.
        return [] if not _server_running() else None
    out: list[Pane] = []
    for line in listing.splitlines():
        if " " not in line:
            continue
        pane_id, tty = line.split(" ", 1)
        pane_id, tty = pane_id.strip(), tty.strip()
        if not pane_id:
            continue
        captured = _run(["tmux", "capture-pane", "-p", "-t", pane_id])
        if captured is None:  # pane vanished between list + capture — skip it
            continue
        out.append(Pane(pane_id=pane_id, tty=tty, tail=last_lines(captured)))
    return out


def pane_tails_for_ttys(ttys: set[str]) -> dict[str, str] | None:
    """The visible tail of each pane running on one of ``ttys``, keyed by that tty —
    the join column between a tmux pane and the ``claude`` process ``ps`` reports on
    that same tty. ``None`` when tmux could not be asked at all.

    Selective on purpose, unlike :func:`dump_panes`: this runs on the panel's
    8-second tick, and the callers want two panes out of however many the developer
    has open. One ``list-panes`` plus one ``capture-pane`` per *agent*, not per pane.

    Keys and lookups carry no ``/dev/`` prefix, because the two sources spell a tty
    differently: tmux gives ``/dev/pts/13``, ``ps`` gives ``pts/13``. Normalising here
    means the callers can pass and read a tty as ``ps`` spells it.

    The ``None`` matters, and used to be a ``{}`` shared with "no pane matched": those
    are "we could not look" and "we looked and this agent has no pane", and reading
    the first as the second is how an agent whose screen went unreadable was mistaken
    for one sitting idle at its prompt. :func:`probes.pane_tails` turns the
    distinction into an Observation and the resolver acts on it.

    ANY failure collapses to ``None``, not just the ones :func:`_run` knows to expect.
    The callers are a poll worker and the mesh node's capacity hook, and neither can
    afford an exception: one would silently die for the rest of the applet's life (the
    way the watcher itself once did — see :func:`_run`), the other would fail a peer's
    job over a screen it could not read.
    """
    if shutil.which("tmux") is None:
        return None
    if not ttys:
        return {}
    try:
        listing = _run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_id} #{pane_tty}"]
        )
        if listing is None:
            return None
        out: dict[str, str] = {}
        for line in listing.splitlines():
            if " " not in line:
                continue
            pane_id, tty = (s.strip() for s in line.split(" ", 1))
            tty = tty.removeprefix("/dev/")
            if not pane_id or tty not in ttys:
                continue
            captured = _run(["tmux", "capture-pane", "-p", "-t", pane_id])
            if captured is not None:  # pane vanished between list + capture — skip it
                out[tty] = last_lines(captured)
        return out
    except Exception:  # noqa: BLE001 - see above; no tmux failure is worth either cost
        return None


def send_continue(pane_id: str, message: str) -> bool:
    """Type ``message`` into the pane and submit it (send the literal text, then
    Enter). Returns whether the pane accepted it — False when the pane no longer
    exists, so the caller doesn't count a nudge that never landed."""
    if _run(["tmux", "send-keys", "-t", pane_id, "-l", message]) is None:
        return False
    return _run(["tmux", "send-keys", "-t", pane_id, "Enter"]) is not None


#: What every tmux session opened for a run is named for. Matched exactly on the way
#: out (see :func:`kill_session`), and distinctive enough that nothing an operator
#: would type by hand collides with it — the name is the whole permission to end a
#: session rather than detach from it. `TerminalFocus.sessionPrefix` is the macOS twin.
SESSION_PREFIX = "diplomat-"


def session_name(run_id: str) -> str:
    """The tmux session a run's window is opened as.

    Derived from the run id rather than recorded, so there is nothing to write at spawn
    and nothing to lose: the reaper computes the same name from the same record. The
    run id is ``<epoch>-<hex8>`` (:func:`agentregistry.new_run_id`), which carries none
    of the characters a tmux session name may not (``.`` and ``:``).
    """
    return SESSION_PREFIX + run_id


def kill_session(name: str) -> bool:
    """Close the tmux session called ``name``. Returns whether it was killed.

    The route a run has to its own window when it has no tty — which is every run whose
    pid was never adopted, and the case the run deadline is the first backstop able to
    reach. :func:`kill_window_for_tty` refuses an empty tty outright, so before this
    such a run was retired from the book with its window left open and its agent still
    in it.

    ``=`` prefixes the target because tmux's default matching is exact, then PREFIX,
    then fnmatch: a bare name would let one run's reap end a session whose name merely
    starts the same. Run ids are fixed-width, so no two can collide that way today, and
    that is not a property to leave a destructive call resting on.
    """
    if not name or shutil.which("tmux") is None:
        return False
    return _run(["tmux", "kill-session", "-t", f"={name}"]) is not None


def kill_window_for_tty(tty: str) -> bool:
    """Close the tmux window whose pane runs on ``tty``. Returns whether it was
    killed.

    The fallback route to a run's window, for the runs :func:`kill_session` cannot
    name: one spawned before sessions were named, and one the mesh placed here, whose
    terminal the node opened.

    The window is reaped only for a run a BACKSTOP ended (:func:`agentstate.reapable`),
    which is two verdicts. The quiescence one is twenty minutes of a screen that has not
    moved, so nothing is being read and nothing is being typed. The run deadline is the
    operator's own instruction to give up on a task at four hours — and there the agent
    may well be working, which is the point: they asked for the bay back anyway. A run
    that ends the ordinary way keeps its window either way: its agent is alive at its
    prompt with the whole task in context, and that is a session the operator may still
    want to read or type into.

    The WINDOW, not the session. A session this applet or a mesh node opened holds
    that one window (:func:`review.terminal_argv`), and tmux ends a session with its
    last window, so nothing of those is left behind; an agent the operator ran by
    hand inside their own session loses its window and nothing else of theirs. Panes
    are matched on the tty rather than the pane id because the tty is what a run
    records - the two sources spell it differently, so the comparison is normalised
    the way :func:`pane_tails_for_ttys` normalises it.
    """
    if not tty or shutil.which("tmux") is None:
        return False
    want = tty.removeprefix("/dev/")
    window = _window_on(want)
    if window is None:
        return False
    return _run(["tmux", "kill-window", "-t", window]) is not None


def _window_on(tty: str) -> str | None:
    """The id of the window whose pane runs on ``tty`` (the ``ps`` spelling, no
    ``/dev/``), or None - for a tty no pane is on, and for no server at all."""
    listing = _run(
        ["tmux", "list-panes", "-a", "-F", "#{pane_tty} #{window_id}"])
    for line in (listing or "").splitlines():
        if " " not in line:
            continue
        pane_tty, window = (x.strip() for x in line.split(" ", 1))
        if pane_tty.removeprefix("/dev/") == tty and window:
            return window
    return None


def _run(argv: list[str]) -> str | None:
    """Run a tmux command; ``None`` on ANY failure (missing binary, non-zero exit,
    timeout), stdout otherwise — so a broken/absent tmux is distinguishable from a
    clean empty result.

    Decode leniently (``errors="replace"``): ``capture-pane -p`` emits pane content
    VERBATIM, so a single non-UTF-8 byte in a watched pane (a Latin-1 filename, a raw
    high byte from a binary dump, a tmux server started under a C locale) would make a
    strict decode raise ``UnicodeDecodeError`` — a ``ValueError``, NOT an ``OSError``/
    ``SubprocessError``, so it escaped the guard, propagated out of ``dump_panes`` and
    the ``run_apiwatch_poll_async`` worker (which has no ``except``), and silently
    killed the whole API-error watcher every poll for as long as that pane existed.
    Replacement decoding keeps the crash out AND still scans the pane — a stalled agent
    showing ``API Error`` (a static pane, most likely to carry a stray byte) is exactly
    the one we must still be able to nudge."""
    try:
        r = subprocess.run(argv, capture_output=True, text=True,
                           errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout
