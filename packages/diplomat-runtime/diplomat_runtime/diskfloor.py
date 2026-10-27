"""The free space below which this machine starts none of the disk-hungry work it
starts on its own: no automatic agent (each makes a worktree and installs into it)
and no scheduled self-update (a merge that hits ENOSPC halfway leaves a
half-updated checkout). The floor is ``minFreeDiskGiB`` in ``assets/config.json``,
shared with the Swift twin ``DiskFloor``; ``DIPLOMAT_MIN_FREE_DISK_MIB`` overrides it.

Free space is what ``statvfs`` reports available to an unprivileged writer
(``f_bavail``), which is what :func:`shutil.disk_usage` returns and the number
``DiskFloor`` reads with ``statfs`` on macOS. Not the figure that counts purgeable
space, which no write can use until macOS has purged it.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from . import core

ENV_OVERRIDE = "DIPLOMAT_MIN_FREE_DISK_MIB"


@dataclass(frozen=True)
class Reading:
    path: str  # the path asked about; the reading is for the volume holding it
    free: int
    floor: int

    @property
    def low(self) -> bool:
        return self.free < self.floor


def floor(env: dict[str, str] | None = None) -> int | None:
    """The floor in bytes, or None when neither the override nor the asset names one."""
    raw = (os.environ if env is None else env).get(ENV_OVERRIDE)
    if raw is not None and raw.isascii() and raw.isdigit():
        return int(raw) << 20
    try:
        gib = core.config().get("minFreeDiskGiB")
    except core.CoreError:
        return None
    if isinstance(gib, bool) or not isinstance(gib, (int, float)) or not 0 <= gib < float("inf"):
        return None
    return int(gib * (1 << 30))


def free(path: str) -> int | None:
    """Bytes free on the volume holding ``path``, asked of its nearest existing
    ancestor: a directory not made yet will be made on that volume. None when none
    is readable."""
    p = Path(os.path.abspath(path))
    while True:
        try:
            return shutil.disk_usage(p).free
        except OSError:
            if p.parent == p:
                return None
            p = p.parent


def lowest(paths: list[str], env: dict[str, str] | None = None) -> Reading | None:
    """The reading for whichever of ``paths`` has the least room, the volume work
    that writes to all of them runs out on first. None when there is no floor or no
    path is readable, which leaves the work ungated."""
    limit = floor(env)
    if limit is None:
        return None
    readings = [Reading(p, n, limit) for p in paths if (n := free(p)) is not None]
    return min(readings, key=lambda r: r.free, default=None)


def describe(r: Reading) -> str:
    """Why work is held: "1.2 GiB free on /path, below the 5.0 GiB floor"."""
    return f"{size(r.free)} free on {r.path}, below the {size(r.floor)} floor"


def size(n: int) -> str:
    """GiB to one decimal, or whole MiB under one GiB. Swift twin: ``DiskFloor.size``."""
    if n >= 1 << 30:
        return f"{n / (1 << 30):.1f} GiB"
    return f"{n >> 20} MiB"
