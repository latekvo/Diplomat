"""One running node per state directory — a kernel-arbitrated startup guard.

Two mesh node *processes* that share a state directory (``~/.diplomat/mesh``, or
whatever ``DIPLOMAT_MESH_DIR`` points at) share one identity, one ``state.json``
and one ``peers.json`` — so they clobber each other's published snapshot, and
only whichever instance a peer happens to dial is ever truly linked. The others
are dark (they can neither beacon under a blocked send channel nor dial, being
the accepter) yet keep overwriting ``state.json`` with an empty ``sees``, so the
panel usually renders "this node sees nobody" even while a real link exists — a
one-way sighting that looks like a mesh bug.

Nothing stopped that today. The pre-launch ``statefile.node_running`` check — in
both the Swift ``ensureRunning`` and the Python ``_daemonize`` — is a
time-of-check/time-of-use race: several launches fired inside the ~1-2s window
before the first child writes ``state.json`` all read "not running" and each
spawn a node. They do not even collide on the TCP port: ``_start_tcp`` binds the
first FREE port in the range (a deliberate many-nodes-per-host *test*
affordance), so the duplicates coexist on adjacent ports instead of the second
failing to bind.

An advisory ``flock`` on ``<mesh_dir>/node.lock`` closes the race at the one
layer that cannot be raced: the kernel grants the exclusive lock to exactly one
holder and releases it automatically when that process dies — clean exit OR
crash — so there is no stale-lock recovery to get wrong (the trap a pidfile
falls into). A node takes it at process start and holds the fd for its whole
life. The lock is keyed to the state dir, so the test/sim affordance of many
nodes on one host is untouched: each gets its own ``DIPLOMAT_MESH_DIR`` → its
own lockfile → its own lock.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from . import identity


def acquire(mesh_dir: Path | None = None) -> int | None:
    """Try to take the exclusive per-state-dir node lock.

    Returns an open fd that the caller MUST keep alive for the node's whole
    lifetime — closing it (see :func:`release`) or the process exiting releases
    the lock. Returns ``None`` when another live node already holds this state
    dir, so the caller should not start.

    Fails OPEN, never closed: if the lockfile can't be created or the platform
    can't ``flock`` at all, a real fd (unlocked) is still returned so a lone,
    legitimate node is never blocked from starting — the lock guards against a
    race-induced pathology, it is not a correctness gate.
    """
    d = Path(mesh_dir) if mesh_dir is not None else identity.mesh_dir()
    try:
        d.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(d / "node.lock"), os.O_RDWR | os.O_CREAT, 0o644)
    except OSError:
        # Can't even create a lockfile (a broken/unwritable state dir). A duplicate
        # node is the least of the caller's problems here — fail open. os.open
        # failed, so there is no fd to hand back; -1 is a valid "proceed unguarded"
        # sentinel that release() treats as a no-op.
        return -1
    try:
        # LOCK_NB: never block. Two racing starts both open the file (separate open
        # file descriptions), then contend here; the kernel grants exactly one and
        # the loser gets EWOULDBLOCK. flock binds to the open file description, so
        # even a second attempt inside ONE process (two opens) contends — which is
        # what makes this unit-testable without spawning a subprocess.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None  # another live node already owns this state dir
    except OSError:
        # flock unsupported on this fs/platform (e.g. some network mounts). Hold the
        # fd and fail open rather than refuse to run — duplicate protection is
        # best-effort, availability is not.
        return fd
    return fd


def release(fd: int | None) -> None:
    """Drop the lock, closing its fd. Safe to call with the ``-1``/``None``
    fail-open sentinel or a never-acquired lock — a no-op then. The kernel also
    releases the lock when the process exits, so this is only for a clean stop."""
    if fd is None or fd < 0:
        return
    try:
        os.close(fd)  # closing the fd releases the flock
    except OSError:
        pass
