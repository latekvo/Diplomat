"""Newest-wins singleton via a pidfile (the Linux analogue of macOS
SingleInstance.terminateOthers). A freshly launched instance signals any older
live one to quit, then claims the pidfile — so there's never more than one
wrench in the tray.
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path


def _pidfile() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    d = Path(base) / "argent-utils"
    d.mkdir(parents=True, exist_ok=True)
    return d / "argent-utils.pid"


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class SingleInstance:
    @staticmethod
    def acquire_newest_wins() -> None:
        pf = _pidfile()
        try:
            old = int(pf.read_text().strip())
        except (OSError, ValueError):
            old = 0
        if old and old != os.getpid() and _alive(old):
            try:
                os.kill(old, signal.SIGTERM)  # ask the older instance to quit
            except OSError:
                pass
            for _ in range(20):  # up to ~2s grace
                if not _alive(old):
                    break
                time.sleep(0.1)
        try:
            pf.write_text(str(os.getpid()))
        except OSError:
            pass

    @staticmethod
    def running_pid() -> int:
        """PID of the live tray instance, or 0 if none is running.

        Lets the headless 6AM updater decide whether to relaunch (swap a running
        tray onto the new build) or just leave the checkout updated in place —
        it must never spawn a GUI on a session that isn't already showing one.
        """
        pf = _pidfile()
        try:
            pid = int(pf.read_text().strip())
        except (OSError, ValueError):
            return 0
        return pid if pid and _alive(pid) else 0

    @staticmethod
    def release() -> None:
        pf = _pidfile()
        try:
            if int(pf.read_text().strip()) == os.getpid():
                pf.unlink()
        except (OSError, ValueError):
            pass
