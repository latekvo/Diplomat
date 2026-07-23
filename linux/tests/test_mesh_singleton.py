"""Tests for the mesh NODE's newest-wins singleton.

The property that matters: when a mesh node starts, it terminates *every other
live mesh node of this uid, under any name it has launched as* — so a detached
pre-rename node (``argent_utils.mesh``) can never again outlive its replacement
as a ghost, independent of which state dir each incarnation writes. The cmdline
matcher below decides who counts as "another node"; the terminate test proves the
others are signalled (and force-killed if they ignore SIGTERM).

The discriminator is precise: a live node is ``python -m <mesh-module>`` with NO
flags (``mesh.__main__.main`` only falls through to ``_run_node`` when every
one-shot CLI branch is absent), so a one-shot invocation (``--status``, the
launcher's ``--daemon``, …) must neither be reaped as the node nor match it.
"""

from __future__ import annotations

import signal

import pytest

from diplomat_app.mesh import singleton
from diplomat_app.mesh.singleton import _cmdline_is_mesh_node, terminate_other_nodes


# ---- cmdline matcher: who is a live mesh node ----------------------------


@pytest.mark.parametrize(
    "tokens",
    [
        ["python3", "-m", "diplomat_app.mesh"],  # current name
        ["python3", "-m", "argent_utils.mesh"],  # legacy name (rename boundary)
        ["/usr/bin/python3.14", "-m", "argent_utils.mesh"],  # the real ghost's argv
    ],
)
def test_cmdline_matches_mesh_node(tokens):
    assert _cmdline_is_mesh_node(tokens)


@pytest.mark.parametrize(
    "tokens",
    [
        # One-shot CLI modes carry a flag and exit before _run_node — never the node.
        ["python3", "-m", "diplomat_app.mesh", "--daemon"],  # the short-lived launcher
        ["python3", "-m", "diplomat_app.mesh", "--status"],
        ["python3", "-m", "diplomat_app.mesh", "--stop"],
        ["python3", "-m", "argent_utils.mesh", "--dispatch", "audit", "--prompt", "x"],
        ["python3", "-m", "diplomat_app.mesh", "--set", "tokens=out"],
        # The tray GUI is the *other* singleton's job, never this one's.
        ["python3", "-m", "diplomat_app"],
        ["python3", "-m", "argent_utils"],
        # Look-alikes and deeper submodules must not masquerade as the node.
        ["python3", "-m", "diplomat_app.meshery"],
        ["python3", "-m", "diplomat_app.mesh.ctl"],
        ["python3", "-m", "something_else.mesh"],
        ["python3", "script.py"],  # no -m at all
        ["python3", "-m"],  # -m with nothing after it
        [],
    ],
)
def test_cmdline_rejects_non_node(tokens):
    assert not _cmdline_is_mesh_node(tokens)


# ---- terminate_other_nodes: signals the others, escalates when ignored ----


def test_terminate_signals_every_other_node(monkeypatch):
    """A node found by the scan (under any name, any state dir) is SIGTERM'd — the
    rename case that let the ghost survive is exactly this path."""
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(singleton, "_other_nodes", lambda: {999001, 999002})
    monkeypatch.setattr(singleton, "_alive", lambda pid: False)  # both die at once
    monkeypatch.setattr(singleton.os, "kill",
                        lambda pid, sig: signalled.append((pid, sig)))

    reaped = terminate_other_nodes()

    assert reaped == {999001, 999002}
    assert (999001, signal.SIGTERM) in signalled
    assert (999002, signal.SIGTERM) in signalled
    # They reported dead immediately, so no SIGKILL escalation.
    assert not any(sig == signal.SIGKILL for _pid, sig in signalled)


def test_terminate_escalates_to_sigkill_when_sigterm_ignored(monkeypatch):
    """A wedged node that ignores SIGTERM is forced down, so the guarantee can
    never degrade to two live nodes."""
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(singleton, "_other_nodes", lambda: {999003})
    monkeypatch.setattr(singleton, "_alive", lambda pid: True)  # never dies
    monkeypatch.setattr(singleton.time, "sleep", lambda _s: None)  # no real wait
    monkeypatch.setattr(singleton.os, "kill",
                        lambda pid, sig: signalled.append((pid, sig)))

    terminate_other_nodes()

    assert (999003, signal.SIGTERM) in signalled
    assert (999003, signal.SIGKILL) in signalled


def test_terminate_stands_down_in_loopback_mode(monkeypatch):
    """Loopback-only mode is a single-host multi-node simulation (the test fleet):
    many isolated nodes legitimately share one uid, so the machine-level singleton
    must NOT fire — otherwise the nodes reap each other by identical argv. The scan
    is never even reached."""
    monkeypatch.setenv("DIPLOMAT_MESH_LOOPBACK", "1")
    monkeypatch.setattr(
        singleton, "_other_nodes",
        lambda: pytest.fail("loopback mode must not scan for or reap other nodes"),
    )
    monkeypatch.setattr(
        singleton.os, "kill",
        lambda pid, sig: pytest.fail("loopback mode must not signal any node"),
    )

    assert terminate_other_nodes() == set()


def test_terminate_is_noop_when_no_other_node(monkeypatch):
    """The steady state: no other node, so nothing is signalled and the starting
    node proceeds untouched."""
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(singleton, "_other_nodes", lambda: set())
    monkeypatch.setattr(singleton.os, "kill",
                        lambda pid, sig: signalled.append((pid, sig)))

    assert terminate_other_nodes() == set()
    assert signalled == []
