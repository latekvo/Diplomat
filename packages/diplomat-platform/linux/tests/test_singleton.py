"""Tests for the newest-wins singleton.

The property that matters: newest-wins terminates *every older live tray
instance of the applet, under any name it has launched as* — so the
``argent_utils`` -> ``diplomat_app`` rename can never again leave two wrenches in
the tray (with the orphan stuck on "Restarting…"), and two trays launched together
leave one rather than none. The matchers below decide who counts as "another tray
instance"; the acquire tests prove an old instance is signaled and the pidfile is
claimed regardless of the old instance's name, and that a newer one is spared.
"""

from __future__ import annotations

import os
import signal

import pytest

from diplomat_app import procscan, singleton
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
        ["python3", "-m", "szpontnet"],  # mesh node — must NOT match
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
        b"DIPLOMAT_WATCHDOG=1\0",
        b"DIPLOMAT_DUMP=1\0",
        b"ARGENT_UTILS_SELF_UPDATE=1\0",  # legacy prefix
        b"DIPLOMAT_PRINT_PROMPT=mine\0",
        b"DIPLOMAT_RENDER=panel\0",
        b"DIPLOMAT_AGENTS=1\0",
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


def _start_times(monkeypatch, times: dict[int, int], default: int = 100):
    """Stub each pid's start time; this process started at 200 unless ``times``
    says otherwise, every other pid at ``default`` (older)."""
    times = {os.getpid(): 200, **times}
    monkeypatch.setattr(procscan, "start_time", lambda pid: times.get(pid, default))


@pytest.fixture
def others_are_older(monkeypatch):
    _start_times(monkeypatch, {})


def test_acquire_signals_recorded_instance_by_pidfile(
    isolated_runtime, monkeypatch, others_are_older
):
    """Even if the /proc scan finds nothing (old instance under a name we can't
    see), the pid recorded in the pidfile is still SIGTERM'd and replaced — provided
    it still verifies as a GUI tray of this applet (identity, not liveness alone)."""
    signalled: list[tuple[int, int]] = []

    # Pretend an old instance (pid 424242) is alive and owns the pidfile, the /proc
    # scan surfaces nobody (a scan blind spot), but the recorded pid DOES still
    # verify as one of our tray instances.
    monkeypatch.setattr(singleton, "_other_instances", lambda: set())
    monkeypatch.setattr(singleton, "_alive", lambda pid: pid == 424242)
    monkeypatch.setattr(singleton, "_is_applet_gui", lambda pid: pid == 424242)
    # The reap escalation lives in procscan; it dies on the first poll here.
    monkeypatch.setattr(procscan, "alive", lambda pid: False)
    monkeypatch.setattr(procscan.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        procscan.os, "kill", lambda pid, sig: signalled.append((pid, sig))
    )

    pf = singleton._pidfile()
    pf.write_text("424242")

    SingleInstance.acquire_newest_wins()

    assert (424242, signal.SIGTERM) in signalled
    assert pf.read_text().strip() == str(os.getpid())


def test_acquire_spares_recycled_non_applet_pid(
    isolated_runtime, monkeypatch, others_are_older
):
    """A tray that exited uncleanly leaves a stale pidfile; the OS recycles that pid
    to an unrelated same-uid process (an editor, a shell). The fold-in must NOT
    signal it — a pid may become a victim only if it still verifies as an applet
    GUI tray, never on liveness alone."""
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(singleton, "_other_instances", lambda: set())
    monkeypatch.setattr(singleton, "_alive", lambda pid: True)  # the recycled pid is alive
    monkeypatch.setattr(singleton, "_is_applet_gui", lambda pid: False)  # but it is not our tray
    monkeypatch.setattr(procscan.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        procscan.os, "kill", lambda pid, sig: signalled.append((pid, sig))
    )

    singleton._pidfile().write_text("424242")

    SingleInstance.acquire_newest_wins()

    assert signalled == []  # the innocent recycled-pid process was left untouched
    assert singleton._pidfile().read_text().strip() == str(os.getpid())


def test_acquire_terminates_scanned_instance_under_any_name(
    isolated_runtime, monkeypatch, others_are_older
):
    """The rename case: an old tray running under a *different* module name is
    found by the /proc scan and terminated even though no pidfile names it."""
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(singleton, "_other_instances", lambda: {999001})
    monkeypatch.setattr(procscan, "alive", lambda pid: False)  # dies at once
    monkeypatch.setattr(
        procscan.os, "kill", lambda pid, sig: signalled.append((pid, sig))
    )

    SingleInstance.acquire_newest_wins()

    assert (999001, signal.SIGTERM) in signalled
    # It reported dead immediately, so no SIGKILL escalation.
    assert (999001, signal.SIGKILL) not in signalled
    assert singleton._pidfile().read_text().strip() == str(os.getpid())


def test_acquire_escalates_to_sigkill_when_sigterm_ignored(
    isolated_runtime, monkeypatch, others_are_older
):
    """A wedged instance that ignores SIGTERM is forced down, so the guarantee
    can never degrade to two wrenches."""
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(singleton, "_other_instances", lambda: {999002})
    monkeypatch.setattr(procscan, "alive", lambda pid: True)  # never dies
    monkeypatch.setattr(procscan.time, "sleep", lambda _s: None)  # no real wait
    monkeypatch.setattr(
        procscan.os, "kill", lambda pid, sig: signalled.append((pid, sig))
    )

    SingleInstance.acquire_newest_wins()

    assert (999002, signal.SIGTERM) in signalled
    assert (999002, signal.SIGKILL) in signalled


# ---- acquire_newest_wins: only instances older than this one --------------


def _reap_stubs(monkeypatch, others: set[int]) -> list[tuple[int, int]]:
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(singleton, "_other_instances", lambda: set(others))
    monkeypatch.setattr(procscan, "alive", lambda pid: False)
    monkeypatch.setattr(
        procscan.os, "kill", lambda pid, sig: signalled.append((pid, sig))
    )
    return signalled


def test_acquire_spares_a_newer_instance(isolated_runtime, monkeypatch):
    """Two trays launched together each see the other; the older one must leave
    the newer alone, or each ends the other and no tray is left."""
    signalled = _reap_stubs(monkeypatch, {999003, 999004})
    _start_times(monkeypatch, {999003: 100, 999004: 300})

    SingleInstance.acquire_newest_wins()

    assert signalled == [(999003, signal.SIGTERM)]
    assert singleton._pidfile().read_text().strip() == str(os.getpid())


def test_acquire_breaks_a_start_time_tie_by_pid(isolated_runtime, monkeypatch):
    """Within one clock tick the lower pid counts as older."""
    me = os.getpid()
    signalled = _reap_stubs(monkeypatch, {me - 1, me + 1})
    _start_times(monkeypatch, {}, default=200)  # everyone, me included

    SingleInstance.acquire_newest_wins()

    assert signalled == [(me - 1, signal.SIGTERM)]


def test_acquire_spares_a_newer_recorded_instance(isolated_runtime, monkeypatch):
    """The pidfile fold-in passes the same age check as the scan: a newer tray that
    claimed the pidfile first is not this instance's to end."""
    signalled = _reap_stubs(monkeypatch, set())
    monkeypatch.setattr(singleton, "_alive", lambda pid: pid == 424242)
    monkeypatch.setattr(singleton, "_is_applet_gui", lambda pid: pid == 424242)
    _start_times(monkeypatch, {424242: 300})
    singleton._pidfile().write_text("424242")

    SingleInstance.acquire_newest_wins()

    assert signalled == []


@pytest.mark.parametrize("a_start, b_start", [(100, 116), (100, 100)])
def test_two_simultaneous_launches_leave_exactly_one(
    isolated_runtime, monkeypatch, a_start, b_start
):
    """Run newest-wins as each of two trays A (pid 1000) and B (pid 1001) that see
    each other: exactly one of them is ended, whether they started 160 ms apart or
    in one tick."""
    times = {1000: a_start, 1001: b_start}
    monkeypatch.setattr(procscan, "start_time", times.__getitem__)
    monkeypatch.setattr(singleton, "_alive", lambda pid: False)  # scan only
    ended = set()
    for me, other in ((1000, 1001), (1001, 1000)):
        monkeypatch.setattr(singleton.os, "getpid", lambda me=me: me)
        signalled = _reap_stubs(monkeypatch, {other})
        SingleInstance.acquire_newest_wins()
        ended |= {pid for pid, sig in signalled if sig == signal.SIGTERM}

    assert ended == {1000}


def test_running_pid_is_pidfile_only(isolated_runtime, monkeypatch):
    """running_pid names the tray that claimed the pidfile, never one a /proc scan
    turns up (that is any_running's job). It still verifies the ONE recorded pid is
    a GUI tray (a targeted check, not a scan)."""
    monkeypatch.setattr(singleton, "_alive", lambda pid: pid == 555)
    monkeypatch.setattr(singleton, "_is_applet_gui", lambda pid: pid == 555)
    monkeypatch.setattr(singleton, "_other_instances",
                        lambda: pytest.fail("running_pid must not enumerate /proc"))
    singleton._pidfile().write_text("555")
    assert SingleInstance.running_pid() == 555

    singleton._pidfile().write_text("777")  # not alive per the stub above
    assert SingleInstance.running_pid() == 0


def test_running_pid_ignores_recycled_non_applet_pid(isolated_runtime, monkeypatch):
    """The running_pid analogue of test_acquire_spares_recycled_non_applet_pid: a
    stale pidfile whose pid the OS recycled to an unrelated same-uid process is alive
    but is NOT our GUI tray. running_pid must report 0 (no tray) so the 6AM updater
    does not relaunch from that unrelated process's env."""
    monkeypatch.setattr(singleton, "_alive", lambda pid: True)           # recycled pid is alive
    monkeypatch.setattr(singleton, "_is_applet_gui", lambda pid: False)  # but not our tray
    singleton._pidfile().write_text("424242")
    assert SingleInstance.running_pid() == 0
