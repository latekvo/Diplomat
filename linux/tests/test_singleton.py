"""Tests for the newest-wins singleton.

The property that matters: newest-wins terminates *every other live tray
instance of the applet, under any name it has launched as* — so the
``argent_utils`` -> ``diplomat_app`` rename can never again leave two wrenches in
the tray (with the orphan stuck on "Restarting…"). The matchers below decide who
counts as "another tray instance"; the acquire test proves an old instance is
signaled and the pidfile is claimed regardless of the old instance's name.
"""

from __future__ import annotations

import os
import signal

import pytest

from diplomat_app import singleton
from diplomat_app.singleton import (
    SingleInstance,
    _cmdline_is_applet_gui,
    _environ_is_headless,
)


# ---- cmdline matcher: who is a tray launch of this applet ----------------


@pytest.mark.parametrize(
    "tokens",
    [
        ["python3", "-m", "diplomat_app"],  # current name
        ["python3", "-m", "argent_utils"],  # legacy name (rename boundary)
        ["/usr/bin/python3", "-m", "diplomat_app", "--flag"],
    ],
)
def test_cmdline_matches_applet_gui(tokens):
    assert _cmdline_is_applet_gui(tokens)


@pytest.mark.parametrize(
    "tokens",
    [
        ["python3", "-m", "diplomat_app.mesh"],  # mesh node — must NOT match
        ["python3", "-m", "argent_utils.mesh"],  # legacy mesh node
        ["python3", "-m", "something_else"],
        ["python3", "script.py"],  # no -m at all
        ["python3", "-m"],  # -m with nothing after it
        [],
    ],
)
def test_cmdline_rejects_non_applet(tokens):
    assert not _cmdline_is_applet_gui(tokens)


# ---- environ matcher: headless one-shots are never terminated ------------


@pytest.mark.parametrize(
    "raw",
    [
        b"PATH=/usr/bin\0DIPLOMAT_SELF_UPDATE=1\0",
        b"DIPLOMAT_DUMP=1\0",
        b"ARGENT_UTILS_SELF_UPDATE=1\0",  # legacy prefix
        b"DIPLOMAT_PRINT_PROMPT=mine\0",
        b"DIPLOMAT_RENDER=panel\0",
    ],
)
def test_environ_headless_detected(raw):
    assert _environ_is_headless(raw)


@pytest.mark.parametrize(
    "raw",
    [
        b"PATH=/usr/bin\0DISPLAY=:0\0",  # a plain GUI launch
        b"DIPLOMAT_SELF_UPDATE=\0",  # marker present but empty -> not headless
        b"DIPLOMAT_SELF_REPO=/x\0",  # unrelated DIPLOMAT_ var
        b"",
    ],
)
def test_environ_gui_not_flagged_headless(raw):
    assert not _environ_is_headless(raw)


# ---- acquire_newest_wins: signals the old instance, claims the pidfile ----


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    return tmp_path


def test_acquire_signals_recorded_instance_by_pidfile(isolated_runtime, monkeypatch):
    """Even if the /proc scan finds nothing (old instance under a name we can't
    see), the pid recorded in the pidfile is still SIGTERM'd and replaced."""
    signalled: list[tuple[int, int]] = []

    # Pretend an old instance (pid 424242) is alive and owns the pidfile, and
    # that the /proc scan surfaces nobody (simulating a scan blind spot).
    monkeypatch.setattr(singleton, "_other_instances", lambda: set())
    monkeypatch.setattr(singleton, "_alive", lambda pid: pid == 424242)
    monkeypatch.setattr(singleton.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        singleton.os, "kill", lambda pid, sig: signalled.append((pid, sig))
    )

    pf = singleton._pidfile()
    pf.write_text("424242")

    SingleInstance.acquire_newest_wins()

    assert (424242, signal.SIGTERM) in signalled
    assert pf.read_text().strip() == str(os.getpid())


def test_acquire_terminates_scanned_instance_under_any_name(
    isolated_runtime, monkeypatch
):
    """The rename case: an old tray running under a *different* module name is
    found by the /proc scan and terminated even though no pidfile names it."""
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(singleton, "_other_instances", lambda: {999001})
    monkeypatch.setattr(singleton, "_alive", lambda pid: False)  # dies at once
    monkeypatch.setattr(
        singleton.os, "kill", lambda pid, sig: signalled.append((pid, sig))
    )

    SingleInstance.acquire_newest_wins()

    assert (999001, signal.SIGTERM) in signalled
    # It reported dead immediately, so no SIGKILL escalation.
    assert (999001, signal.SIGKILL) not in signalled
    assert singleton._pidfile().read_text().strip() == str(os.getpid())


def test_acquire_escalates_to_sigkill_when_sigterm_ignored(
    isolated_runtime, monkeypatch
):
    """A wedged instance that ignores SIGTERM is forced down, so the guarantee
    can never degrade to two wrenches."""
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(singleton, "_other_instances", lambda: {999002})
    monkeypatch.setattr(singleton, "_alive", lambda pid: True)  # never dies
    monkeypatch.setattr(singleton.time, "sleep", lambda _s: None)  # no real wait
    monkeypatch.setattr(
        singleton.os, "kill", lambda pid, sig: signalled.append((pid, sig))
    )

    SingleInstance.acquire_newest_wins()

    assert (999002, signal.SIGTERM) in signalled
    assert (999002, signal.SIGKILL) in signalled


def test_running_pid_is_pidfile_only(isolated_runtime, monkeypatch):
    """running_pid must not /proc-scan: the 6AM updater runs as the applet
    module itself and would otherwise detect itself as a live tray."""
    monkeypatch.setattr(singleton, "_alive", lambda pid: pid == 555)
    singleton._pidfile().write_text("555")
    assert SingleInstance.running_pid() == 555

    singleton._pidfile().write_text("777")  # not alive per the stub above
    assert SingleInstance.running_pid() == 0
