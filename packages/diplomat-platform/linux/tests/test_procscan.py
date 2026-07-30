"""The process-scan / reap primitives, held to one contract across two copies.

Both newest-wins singletons — the tray's (:mod:`diplomat_app.procscan`) and the
node's (:mod:`szpontnet.procscan`, the library's own, because a library that
reaches into its consumer for the routine that picks SIGKILL targets is not one
you can install alone) — reap by sending SIGTERM and then SIGKILL to pids they
inferred from ``/proc``. The guards that decide *who* can become a victim matter
as much as the escalation itself:

* never this process,
* never a process owned by another uid,
* never an entry that can't be read (the safe direction — "not mine").

Every case below runs against **both** copies. That is the whole point of the
file: a guard added to one and missed in the other means the copy that missed it
force-kills an unrelated process of the same user, and nothing else in either
suite would notice.

The two singleton test modules stub the scan out to exercise their own identity
matchers; these exercise the scan itself.
"""

from __future__ import annotations

import os
import signal

import pytest

from diplomat_app import procscan as diplomat_procscan
from szpontnet import procscan as szpontnet_procscan


@pytest.fixture(params=[diplomat_procscan, szpontnet_procscan],
                ids=["diplomat", "szpontnet"])
def procscan(request):
    return request.param


def test_the_two_copies_are_not_the_same_module():
    """Anti-vacuity for the parametrisation: if one ever re-exported the other,
    every case here would assert one implementation twice while the second rotted."""
    assert diplomat_procscan is not szpontnet_procscan
    assert diplomat_procscan.terminate is not szpontnet_procscan.terminate


# ---- module_arg: the ``python -m <module>`` extractor ---------------------


def test_module_arg_reads_the_module_after_dash_m(procscan):
    assert procscan.module_arg(["python3", "-m", "diplomat_app"]) == "diplomat_app"
    assert procscan.module_arg(["/usr/bin/python3.14", "-m", "a.b", "--x"]) == "a.b"


def test_module_arg_is_none_without_a_usable_module(procscan):
    assert procscan.module_arg(["python3", "script.py"]) is None  # no -m at all
    assert procscan.module_arg(["python3", "-m"]) is None  # -m with nothing after
    assert procscan.module_arg([]) is None


# ---- scan_own_pids: who may become a victim ------------------------------


def _fake_proc(procscan, monkeypatch, *, entries, uid_of, my_uid=1000, my_pid=4242):
    """Stand in a synthetic ``/proc`` (the real one is Linux-only, and the test
    box is macOS). ``uid_of`` maps pid -> owning uid; a pid absent from it has an
    unreadable ``/proc`` entry and raises, as a process exiting mid-scan does.

    ``procscan.os`` *is* the stdlib ``os`` in either copy, so the stubs must
    delegate anything
    outside ``/proc`` to the real call — pytest itself stats files throughout the
    test, and a stub that swallowed those would break the run rather than the
    assertion.
    """
    real_listdir, real_stat = os.listdir, os.stat
    monkeypatch.setattr(procscan.os, "getpid", lambda: my_pid)
    monkeypatch.setattr(procscan.os, "getuid", lambda: my_uid)

    def listdir(path=".", *args, **kwargs):
        if path == "/proc":
            return [str(e) for e in entries]
        return real_listdir(path, *args, **kwargs)

    def stat(path, *args, **kwargs):
        prefix = "/proc/"
        if not isinstance(path, str) or not path.startswith(prefix):
            return real_stat(path, *args, **kwargs)
        pid = int(path[len(prefix):])
        if pid not in uid_of:
            raise OSError("no such process")
        return os.stat_result((0, 0, 0, 0, uid_of[pid], 0, 0, 0, 0, 0))

    monkeypatch.setattr(procscan.os, "listdir", listdir)
    monkeypatch.setattr(procscan.os, "stat", stat)


def test_scan_finds_matching_same_uid_pids(procscan, monkeypatch):
    _fake_proc(procscan, monkeypatch, entries=[10, 11, 12],
               uid_of={10: 1000, 11: 1000, 12: 1000})
    assert procscan.scan_own_pids(lambda pid: pid != 11) == {10, 12}


def test_scan_never_returns_this_process(procscan, monkeypatch):
    """Self-exclusion: a starting instance that reaped itself would SIGKILL the
    very process doing the reaping."""
    _fake_proc(procscan, monkeypatch, entries=[10, 4242], uid_of={10: 1000, 4242: 1000})
    assert procscan.scan_own_pids(lambda pid: True) == {10}


def test_scan_skips_other_uids(procscan, monkeypatch):
    """Another user's process may share our argv (a second account running the
    same applet); signalling it is not ours to do."""
    _fake_proc(procscan, monkeypatch, entries=[10, 11], uid_of={10: 1000, 11: 1001})
    assert procscan.scan_own_pids(lambda pid: True) == {10}


def test_scan_skips_unreadable_entries(procscan, monkeypatch):
    """A pid that vanished between listdir and stat is skipped, not guessed at."""
    # 11 is absent from uid_of, so its stat raises.
    _fake_proc(procscan, monkeypatch, entries=[10, 11], uid_of={10: 1000})
    assert procscan.scan_own_pids(lambda pid: True) == {10}


def test_scan_ignores_non_numeric_proc_entries(procscan, monkeypatch):
    """``/proc`` holds ``self``, ``cpuinfo``, ``net``… — only pid dirs are pids."""
    _fake_proc(procscan, monkeypatch, entries=["self", "cpuinfo", 10, "net"],
               uid_of={10: 1000})
    assert procscan.scan_own_pids(lambda pid: True) == {10}


def test_scan_returns_nothing_without_proc(procscan, monkeypatch):
    """No ``/proc`` (macOS, a stripped container): reap nobody rather than raise."""
    real_listdir = os.listdir

    def listdir(path=".", *args, **kwargs):
        if path == "/proc":
            raise OSError("no /proc here")
        return real_listdir(path, *args, **kwargs)

    monkeypatch.setattr(procscan.os, "listdir", listdir)
    assert procscan.scan_own_pids(lambda pid: True) == set()


# ---- terminate: the SIGTERM -> grace -> SIGKILL escalation ----------------


def test_terminate_sigterms_every_pid(procscan, monkeypatch):
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(procscan, "alive", lambda pid: False)
    monkeypatch.setattr(procscan.os, "kill", lambda pid, sig: sent.append((pid, sig)))

    procscan.terminate({1, 2})

    assert sorted(sent) == [(1, signal.SIGTERM), (2, signal.SIGTERM)]


def test_terminate_escalates_only_the_survivor(procscan, monkeypatch):
    """One wedged pid is force-killed; the one that honoured SIGTERM is not
    signalled twice."""
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(procscan, "alive", lambda pid: pid == 2)  # 2 never dies
    monkeypatch.setattr(procscan.time, "sleep", lambda _s: None)
    monkeypatch.setattr(procscan.os, "kill", lambda pid, sig: sent.append((pid, sig)))

    procscan.terminate({1, 2})

    assert (2, signal.SIGKILL) in sent
    assert (1, signal.SIGKILL) not in sent


def test_terminate_waits_before_escalating(procscan, monkeypatch):
    """The grace really is bounded polling, not a single check: a pid that exits
    part-way through the window is never SIGKILLed."""
    sent: list[tuple[int, int]] = []
    polls = {"n": 0}

    def alive(pid):
        polls["n"] += 1
        return polls["n"] < 3  # dies on the third poll, inside the window

    monkeypatch.setattr(procscan, "alive", alive)
    monkeypatch.setattr(procscan.time, "sleep", lambda _s: None)
    monkeypatch.setattr(procscan.os, "kill", lambda pid, sig: sent.append((pid, sig)))

    procscan.terminate({7})

    assert polls["n"] == 3
    assert not any(sig == signal.SIGKILL for _pid, sig in sent)


def test_terminate_does_not_mutate_the_caller_set(procscan, monkeypatch):
    """``szpontnet.singleton.terminate_other_nodes`` returns the pids it targeted
    after calling this — it must still hold every one of them."""
    targeted = {1, 2, 3}
    monkeypatch.setattr(procscan, "alive", lambda pid: False)
    monkeypatch.setattr(procscan.os, "kill", lambda pid, sig: None)

    procscan.terminate(targeted)

    assert targeted == {1, 2, 3}


def test_terminate_survives_a_pid_that_is_already_gone(procscan, monkeypatch):
    """A pid that exits between the scan and the signal makes ``kill`` raise; the
    reap must carry on to the remaining victims rather than abort."""
    sent: list[tuple[int, int]] = []

    def kill(pid, sig):
        if pid == 1:
            raise OSError("no such process")
        sent.append((pid, sig))

    monkeypatch.setattr(procscan, "alive", lambda pid: False)
    monkeypatch.setattr(procscan.os, "kill", kill)

    procscan.terminate({1, 2})

    assert sent == [(2, signal.SIGTERM)]
