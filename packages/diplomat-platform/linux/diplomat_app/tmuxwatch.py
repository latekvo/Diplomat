"""tmux terminal I/O for the Claude-API-error watcher — the Linux stand-in for the
iTerm/Terminal AppleScript in ApiErrorWatcher.swift.

macOS can read any terminal window's visible buffer and type into it through the
scriptable iTerm/Terminal apps. Linux has no such universal hook for arbitrary
emulators (gnome-terminal, konsole, …) — you can neither read what's rendered nor
inject input. tmux is the one portable mechanism that does both: ``capture-pane``
returns a pane's visible screen, ``send-keys`` submits a line to it. So the watcher
drives tmux panes; an agent must be running inside tmux to be watched (the feature
is simply inert otherwise, exactly as the macOS watcher is when neither terminal app
is running).

Panes are keyed by their tmux ``pane_id`` (``%N``) — unique and never recycled for
the life of the server, unlike a ``/dev/pts`` tty which is reused as panes close.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

from .apiwatch import last_lines

_UNIT = "\x1f"  # between pane_id and its tty in the list-panes format


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
        ["tmux", "list-panes", "-a", "-F", f"#{{pane_id}}{_UNIT}#{{pane_tty}}"]
    )
    if listing is None:
        # Distinguish "no server running" (inert, known-empty) from a real failure.
        return [] if not _server_running() else None
    out: list[Pane] = []
    for line in listing.splitlines():
        if _UNIT not in line:
            continue
        pane_id, tty = line.split(_UNIT, 1)
        pane_id, tty = pane_id.strip(), tty.strip()
        if not pane_id:
            continue
        captured = _run(["tmux", "capture-pane", "-p", "-t", pane_id])
        if captured is None:  # pane vanished between list + capture — skip it
            continue
        out.append(Pane(pane_id=pane_id, tty=tty, tail=last_lines(captured)))
    return out


def send_continue(pane_id: str, message: str) -> bool:
    """Type ``message`` into the pane and submit it (send the literal text, then
    Enter). Returns whether the pane accepted it — False when the pane no longer
    exists, so the caller doesn't count a nudge that never landed."""
    if _run(["tmux", "send-keys", "-t", pane_id, "-l", message]) is None:
        return False
    return _run(["tmux", "send-keys", "-t", pane_id, "Enter"]) is not None


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
