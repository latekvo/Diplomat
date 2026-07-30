"""Process identification and newest-wins reaping, for the node daemon.

:mod:`.singleton` enforces "newest wins" over the node: on startup, find every
*other* live node of this uid and terminate it. Reading a pid's argv, walking
``/proc`` restricted to this uid, and the SIGTERM/grace/SIGKILL escalation are
that guarantee's machinery, and the half that decides which process receives a
signal.

This is deliberately the library's own copy rather than a shared helper borrowed
from whatever application is hosting the node: a library that reaches into its
consumer for the routine that picks SIGKILL targets is not one you can install on
its own. An application that reaps its *own* processes wants the identical
routine, and the risk in two copies is real — a guard added to one and missed in
the other force-kills an unrelated process of the same user — so an application
that keeps its own copy is expected to pin the two against each other by
behaviour, running both over one table of cases, rather than by sharing a file.

Linux-only, by way of ``/proc``: on a host without it the scan finds nobody and
the caller starts without reaping, which is the intended best-effort degradation.
"""

from __future__ import annotations

import os
import signal
import time
from collections.abc import Callable
from pathlib import Path

# The reap escalation: poll liveness this many times, sleeping between polls, and
# force down whatever is still up. ~2s of grace for a clean shutdown (an asyncio
# ``stop()``) before the guarantee is enforced the hard way.
_GRACE_POLLS = 20
_GRACE_POLL_SECS = 0.1


def alive(pid: int) -> bool:
    """Whether a pid exists (signal 0 probes without delivering)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def cmdline_tokens(pid: int) -> list[str]:
    """A pid's argv as text tokens; empty when ``/proc`` can't be read for it.

    An unreadable entry (the process exited mid-scan, or belongs to a namespace
    we can't see into) yields no tokens, so every identity test built on this
    answers "not mine" — the safe direction, since the answer gates a SIGKILL.
    """
    try:
        parts = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return []
    return [p.decode("utf-8", "replace") for p in parts if p]


def module_arg(tokens: list[str]) -> str | None:
    """The module name in a ``python -m <module>`` argv, or ``None``.

    Callers match the result *exactly* against their own module set, so neither a
    look-alike top-level (``szpontnetty``) nor a deeper submodule
    (``szpontnet.ctl``) can pass as the module it resembles.
    """
    try:
        i = tokens.index("-m")
    except ValueError:
        return None
    return tokens[i + 1] if i + 1 < len(tokens) else None


def scan_own_pids(match: Callable[[int], bool]) -> set[int]:
    """PIDs of every *other* live process of this uid for which ``match`` holds.

    Excludes this process, and skips any ``/proc`` entry owned by another uid or
    unreadable. Best-effort throughout: a scan failure returns nothing rather
    than raising, so a caller degrades to "reaped nobody".
    """
    me = os.getpid()
    uid = os.getuid()
    found: set[int] = set()
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for name in entries:
        if not name.isdigit():
            continue
        pid = int(name)
        if pid == me:
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
        except OSError:
            continue
        if match(pid):
            found.add(pid)
    return found


def terminate(pids: set[int]) -> None:
    """SIGTERM every pid, allow ~2s to exit, then SIGKILL whatever survives.

    The escalation is the point: without it a wedged instance that ignores
    SIGTERM would leave the caller's "there is only ever one of me" guarantee
    silently degraded to two. ``pids`` is not mutated — callers that report what
    they targeted keep their set intact.
    """
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)  # ask it to quit
        except OSError:
            pass
    remaining = set(pids)
    for _ in range(_GRACE_POLLS):
        remaining = {p for p in remaining if alive(p)}
        if not remaining:
            break
        time.sleep(_GRACE_POLL_SECS)
    for pid in remaining:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
