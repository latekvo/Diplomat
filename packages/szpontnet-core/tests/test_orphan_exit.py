"""A node stops itself when the process that launched it is killed.

The harnesses that run real node processes (``tornet.py`` here, ``meshsim`` and the
``Fleet`` in the application's suite) all stop their fleet from a ``finally``. A
pytest killed outright never reaches one, and every node it would have stopped keeps
beaconing on the fleet's ports for as long as the machine stays up — eight of them,
from three separate runs, were found alive four days later. ``SZPONTNET_EXIT_WITH_PARENT``
is the node's own half of that contract, and it is opt-in: ``--daemon`` orphans a
shipped node deliberately, so an unconditional version would kill the product.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from szpontnet import __main__ as mesh_main

_PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def test_stop_when_orphaned_asks_the_node_to_stop(monkeypatch):
    """The watchdog itself: it waits on the parent it started under, not on pid 1."""

    class FakeNode:
        stopped = False

        def request_stop(self) -> None:
            self.stopped = True

    node = FakeNode()
    # Adopted by 4242 — a subreaper, not init. A bare `== 1` test would wait forever.
    reads = iter([1000, 4242])
    monkeypatch.setattr(os, "getppid", lambda: next(reads))
    asyncio.run(mesh_main._stop_when_orphaned(node, 1000, interval=0))
    assert node.stopped, "a reparented node must ask itself to stop"


def test_stop_when_orphaned_does_not_wait_on_a_parent_already_gone(monkeypatch):
    """Armed after the launcher died: the pid handed in is already init's, and there
    is no later change to wait for. The node must stop rather than watch pid 1."""

    class FakeNode:
        stopped = False

        def request_stop(self) -> None:
            self.stopped = True

    node = FakeNode()
    monkeypatch.setattr(os, "getppid", lambda: 1)
    asyncio.run(mesh_main._stop_when_orphaned(node, 1, interval=0))
    assert node.stopped, "a node armed with no live parent must stop at once"


def _node_env(state: Path) -> dict:
    """A lone node on its own loopback ports — no peer, no WAN, no real ~/.diplomat."""
    port = 47000 + (os.getpid() % 200) * 4
    state.mkdir(parents=True, exist_ok=True)
    (state / "node.json").write_text(json.dumps({
        "id": "e" * 32, "name": "orphan-test", "tier": 3, "tokens": "ok",
        "strengthAuto": False, "dutiesEnabled": {},
    }))
    return {
        **os.environ,
        "SZPONTNET_DIR": str(state),
        "HOME": str(state),
        # Loopback also stands the cross-directory singleton reaper down, so this
        # node never SIGTERMs a node the developer is running.
        "SZPONTNET_LOOPBACK": "1",
        "SZPONTNET_IROH": "0",
        "SZPONTNET_TOR": "0",
        "SZPONTNET_OAUTH_PROBE": "0",
        "SZPONTNET_MCAST_PORT": str(port),
        "SZPONTNET_TCP_BASE": str(port + 1),
        "SZPONTNET_TCP_SPAN": "2",
        "SZPONTNET_STATE_SECS": "0.2",
        "PYTHONPATH": str(_PACKAGE_ROOT),
    }


def _launcher(env: dict, pid_file: Path) -> subprocess.Popen:
    """Stand in for the harness: start a node, publish its pid, then sit there until
    killed — the node has to survive us, then notice that it hasn't."""
    code = (
        "import os, subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-m', 'szpontnet'],\n"
        "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(120)\n"
    )
    return subprocess.Popen([sys.executable, "-c", code], env=env,
                            cwd=str(_PACKAGE_ROOT),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _await_pid(pid_file: Path, timeout: float = 20.0) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            return int(pid_file.read_text())
        except (OSError, ValueError):
            time.sleep(0.05)
    raise AssertionError("the launcher never published a node pid")


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _await_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _running(pid):
            return True
        time.sleep(0.1)
    return False


def _published_pid(pid_file: Path) -> int:
    """Whatever pid the launcher published, or ``-1`` when it never got that far.

    Never raises: this is read from a ``finally``, where an exception would both mask
    the failure that brought us there and abandon the node it was about to reap.
    """
    try:
        return int(pid_file.read_text())
    except (OSError, ValueError):
        return -1


def _reap(launcher: subprocess.Popen, pid: int) -> None:
    for target, sig in ((launcher.pid, signal.SIGKILL), (pid, signal.SIGKILL)):
        # `os.kill(-1, ...)` is a BROADCAST — POSIX sends the signal to every process
        # of this uid — and a pid nobody published arrives here as -1.
        if target <= 0:
            continue
        try:
            os.kill(target, sig)
        except ProcessLookupError:
            pass
    launcher.wait(timeout=10)


def test_reaping_a_launcher_that_published_no_pid_signals_nobody(monkeypatch):
    """The teardown below runs from a ``finally``, and the run that reaches it without a
    pid is the one whose launcher died before publishing one. ``os.kill(-1, ...)`` is a
    broadcast, so an unknown pid passed through would SIGKILL the developer's whole
    session, or the CI runner's, out of a test that had merely failed."""
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signalled.append((pid, sig)))

    class _Launcher:
        pid = 4242

        def wait(self, timeout=None):
            return 0

    _reap(_Launcher(), _published_pid(Path("/nonexistent-dir/node.pid")))

    assert signalled == [(4242, signal.SIGKILL)]


def test_orphaned_node_stops_itself(tmp_path):
    """End to end: kill the launcher outright and the node goes too."""
    env = _node_env(tmp_path / "state")
    env["SZPONTNET_EXIT_WITH_PARENT"] = "1"
    pid_file = tmp_path / "node.pid"
    launcher = _launcher(env, pid_file)
    try:
        pid = _await_pid(pid_file)
        # Alive under a live launcher first, or "it exited" proves nothing.
        time.sleep(3.0)
        assert _running(pid), "the node died on its own before the launcher was killed"
        os.kill(launcher.pid, signal.SIGKILL)
        assert _await_exit(pid, timeout=20.0), (
            "an orphaned node kept running — the leak this flag exists to stop")
    finally:
        _reap(launcher, _published_pid(pid_file))


def test_a_node_without_the_flag_survives_its_parent(tmp_path):
    """The opt-in half. ``--daemon`` detaches a shipped node on purpose, so the
    default must be to outlive the launcher; making the watchdog unconditional
    would stop every daemon the moment its launcher returned."""
    env = _node_env(tmp_path / "state")  # deliberately no SZPONTNET_EXIT_WITH_PARENT
    pid_file = tmp_path / "node.pid"
    launcher = _launcher(env, pid_file)
    try:
        pid = _await_pid(pid_file)
        time.sleep(3.0)
        assert _running(pid), "the node died on its own before the launcher was killed"
        os.kill(launcher.pid, signal.SIGKILL)
        assert not _await_exit(pid, timeout=8.0), (
            "an unflagged node stopped itself — that would kill every --daemon node")
    finally:
        _reap(launcher, _published_pid(pid_file))
