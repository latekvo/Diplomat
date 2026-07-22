"""Newest-wins singleton (the Linux analogue of macOS
SingleInstance.terminateOthers). A freshly launched tray instance terminates
*every other live instance of the applet* — matched by process identity, not by
the name of a pidfile — then claims the pidfile. So there's never more than one
wrench in the tray.

Why identity and not just the pidfile: the pidfile path is derived from the
product name (``.../diplomat/diplomat.pid``). Keying the whole guarantee on it
means a *rename* silently breaks it — a self-update that crosses the rename
boundary launches an instance that writes the *new* pidfile and never signals
the still-running instance recorded in the *old* one, leaving two wrenches (and
the orphan stuck on "Restarting…" forever, waiting to be terminated). That is
exactly how the ``argent_utils`` -> ``diplomat_app`` rename orphaned a tray.
Scanning ``/proc`` for other instances of the applet — under any name it has
ever launched as — makes the guarantee hold across renames. The pidfile remains
as a cheap record for the headless 6AM updater (``running_pid``).
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

# Every module name this applet's tray has launched under. A rename appends a
# new name but keeps the old ones, so newest-wins still terminates a pre-rename
# instance across the boundary.
_APPLET_MODULES = frozenset({"diplomat_app", "argent_utils"})

# Env-var suffixes that mark a ``python -m <module>`` process as a headless
# one-shot (self-update / dump / lookup / prompt / render) rather than the GUI
# tray — see ``__main__.py``. We never terminate these: they exit on their own
# and are not a wrench in the tray. Matched under both the current and the
# legacy env prefix.
_HEADLESS_SUFFIXES = frozenset(
    {"SELF_UPDATE", "DUMP", "LOOKUP", "PRINT_PROMPT", "RENDER"}
)
_ENV_PREFIXES = ("DIPLOMAT_", "ARGENT_UTILS_")


def _pidfile() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    d = Path(base) / "diplomat"
    d.mkdir(parents=True, exist_ok=True)
    return d / "diplomat.pid"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _cmdline_is_applet_gui(tokens: list[str]) -> bool:
    """Whether an argv is a tray launch of this applet: ``python -m <module>``
    where <module> is *exactly* an applet module. The exact match excludes
    submodules like ``diplomat_app.mesh`` (the mesh node is a separate long-lived
    process that must never be terminated here)."""
    try:
        i = tokens.index("-m")
    except ValueError:
        return False
    return i + 1 < len(tokens) and tokens[i + 1] in _APPLET_MODULES


def _environ_is_headless(raw: bytes) -> bool:
    """Whether a ``/proc/<pid>/environ`` blob carries a headless-mode marker."""
    for entry in raw.split(b"\0"):
        key, sep, val = entry.partition(b"=")
        if not sep or not val:
            continue
        k = key.decode("utf-8", "replace")
        for prefix in _ENV_PREFIXES:
            if k.startswith(prefix) and k[len(prefix):] in _HEADLESS_SUFFIXES:
                return True
    return False


def _is_applet_gui(pid: int) -> bool:
    """Whether a live pid is a *GUI tray* instance of the applet (any name)."""
    try:
        parts = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return False
    tokens = [p.decode("utf-8", "replace") for p in parts if p]
    if not _cmdline_is_applet_gui(tokens):
        return False
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError:
        raw = b""
    return not _environ_is_headless(raw)


def _other_instances() -> set[int]:
    """PIDs of every *other* live GUI tray instance of the applet, by any name.

    Restricted to processes owned by this uid; unreadable ``/proc`` entries are
    skipped. Best-effort — a scan failure just falls back to the pidfile path.
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
        if _is_applet_gui(pid):
            found.add(pid)
    return found


class SingleInstance:
    @staticmethod
    def acquire_newest_wins() -> None:
        me = os.getpid()
        # Terminate every other live tray instance — whatever it's named and
        # whichever pidfile it wrote. The recorded pidfile pid is folded in for the
        # common case (the scan catches it too), but it MUST pass the same identity
        # check: a tray that exited uncleanly leaves a stale pidfile, and the OS
        # recycles that pid to an unrelated same-uid process (an editor, a shell, a
        # build). Folding it in on liveness ALONE would SIGTERM/SIGKILL that innocent
        # process. _is_applet_gui verifies the pid is really a GUI tray of this applet
        # before it can become a victim.
        victims = _other_instances()
        pf = _pidfile()
        try:
            old = int(pf.read_text().strip())
            if old and old != me and _alive(old) and _is_applet_gui(old):
                victims.add(old)
        except (OSError, ValueError):
            pass

        for pid in victims:
            try:
                os.kill(pid, signal.SIGTERM)  # ask the older instance to quit
            except OSError:
                pass
        for _ in range(20):  # up to ~2s grace for a clean Qt shutdown
            victims = {p for p in victims if _alive(p)}
            if not victims:
                break
            time.sleep(0.1)
        # Anything that ignored SIGTERM is forced down, so the guarantee holds
        # even against a wedged instance rather than degrading to two wrenches.
        for pid in victims:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

        try:
            pf.write_text(str(me))
        except OSError:
            pass

    @staticmethod
    def running_pid() -> int:
        """PID of the live tray instance, or 0 if none is running.

        Lets the headless 6AM updater decide whether to relaunch (swap a running
        tray onto the new build) or just leave the checkout updated in place —
        it must never spawn a GUI on a session that isn't already showing one.

        Deliberately pidfile-only: the updater itself runs as
        ``python -m diplomat_app`` (headless), so a ``/proc`` scan would find
        *itself* and wrongly conclude a tray is up.
        """
        pf = _pidfile()
        try:
            pid = int(pf.read_text().strip())
        except (OSError, ValueError):
            return 0
        # Liveness alone is not enough: a tray that exited uncleanly leaves a stale
        # pidfile, and the OS recycles that pid to an unrelated same-uid process. A
        # bare _alive check would then report a "running tray" that is really a shell
        # or an editor, and the 6AM updater would relaunch a GUI on a session that
        # has none. _is_applet_gui verifies the pidfile pid really is a GUI tray of
        # this applet (the same identity gate acquire_newest_wins uses before it
        # kills). This stays pidfile-only — it verifies the ONE recorded pid, it does
        # not /proc-scan — so the headless updater never detects itself as a tray.
        return pid if pid and _alive(pid) and _is_applet_gui(pid) else 0

    @staticmethod
    def release() -> None:
        pf = _pidfile()
        try:
            if int(pf.read_text().strip()) == os.getpid():
                pf.unlink()
        except (OSError, ValueError):
            pass
