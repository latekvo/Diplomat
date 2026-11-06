"""Is the node a snapshot names still running? (szpontnet.statefile.node_running)

A snapshot names its node by pid, and after an unclean exit the OS hands that pid to
whatever starts next. Every caller trusts the answer: the launcher starts no node,
the control client dials a port nobody holds, a front-end offers routing through a
mesh that is not there.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import types
from datetime import datetime, timezone

from szpontnet import __main__ as mesh_main
from szpontnet import statefile


def _at(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def _stranger():
    """A live process of this user that is no node, started now."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


def test_a_pid_handed_to_a_process_started_after_the_snapshot_is_not_the_node(tmp_path):
    proc = _stranger()
    try:
        state = {"pid": proc.pid, "updatedAt": _at(time.time() - 3600), "v": 1}
        assert statefile.node_running(state) is False
        state["updatedAt"] = _at(time.time() + 5)
        assert statefile.node_running(state) is True, "a process older than the snapshot is"
    finally:
        proc.kill()
        proc.wait()


def test_the_launcher_starts_a_node_over_a_recycled_pid(tmp_path, monkeypatch, capsys):
    """The issue's repro: ``--daemon`` said "already running" and started nothing."""
    proc = _stranger()
    started = []
    # The launcher's module alone: node_running itself may run `ps`.
    monkeypatch.setattr(mesh_main, "subprocess", types.SimpleNamespace(
        DEVNULL=subprocess.DEVNULL,
        Popen=lambda *a, **k: started.append(a) or types.SimpleNamespace(pid=1)))
    try:
        statefile.state_path().parent.mkdir(parents=True, exist_ok=True)
        statefile.state_path().write_text(
            f'{{"pid": {proc.pid}, "v": 1, "updatedAt": "{_at(time.time() - 3600)}"}}')
        assert mesh_main._daemonize() == 0
    finally:
        proc.kill()
        proc.wait()
    assert len(started) == 1, capsys.readouterr().out


def test_a_snapshot_this_process_wrote_names_a_running_node():
    state = statefile.stamp({})
    assert state["pid"] == os.getpid()
    assert statefile.node_running(state) is True


def test_a_snapshot_with_no_readable_write_time_is_judged_by_its_pid():
    """Hand-written snapshots (front-end fixtures, older writers) carry no usable
    ``updatedAt``; with nothing to compare a start time against, a live pid is all
    there is."""
    for written in (None, "now", 7, "2026-13-45T99:00:00"):
        state = {"pid": os.getpid()}
        if written is not None:
            state["updatedAt"] = written
        assert statefile.node_running(state) is True, written
    assert statefile.node_running({"pid": 2**40}) is False
