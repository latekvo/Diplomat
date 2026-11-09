"""A freed bay, an ask, or a raised cap brings the next monitor poll forward.

The queue drains only at the top of a poll, and the poll runs every three minutes, so
without this a bay that frees sits empty for the rest of the interval with work
waiting in the list. Twin of QueueTest section 20 (Store.swift `bringPollForward`).
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from diplomat_runtime import autofix
from diplomat_app.store import Store

FLOOR = 0.5


def _ask(number: int) -> autofix.QueuedTask:
    return autofix.QueuedTask(
        id=autofix.queue_key("review", number),
        job=autofix.AgentJob(
            kind="review", audit_action="review", label=f"Review · #{number}",
            prompt="P", pr_url=f"https://github.com/o/r/pull/{number}",
            pr_number=number, counter=None,
        ),
        attempt=1,
    )


def _store(monkeypatch, *, limit: int = 3) -> tuple[Store, list[float], dict]:
    """A store with one ask queued, polls recorded rather than run, and the cap load
    the settles find set by the test."""
    store = Store()
    store.polls_early = True
    store.early_poll_floor = FLOOR
    store.queue_auto_run = True
    store.auto_task_limit = limit
    store.queued_tasks = [_ask(5)]
    polls: list[float] = []
    monkeypatch.setattr(store, "_autofix_poll_once", lambda: polls.append(time.monotonic()))
    machine = {"load": 0}

    def settle():
        store._tick = SimpleNamespace(rows=[], cap_load=set(range(machine["load"])))

    monkeypatch.setattr(store, "_settle_agents", settle)
    monkeypatch.setattr(store, "_auto_tasks_running", lambda: machine["load"])
    return store, polls, machine


def _poll_count(polls: list[float], n: int, within: float) -> bool:
    deadline = time.monotonic() + within
    while len(polls) < n and time.monotonic() < deadline:
        time.sleep(0.01)
    return len(polls) >= n


def _settle_at(store: Store, machine: dict, load: int) -> None:
    machine["load"] = load
    store.refresh_auto_task_count()


def test_a_freed_bay_brings_one_poll_forward_to_the_floor(monkeypatch):
    store, polls, machine = _store(monkeypatch)
    _settle_at(store, machine, 3)
    store.run_autofix_poll_async()
    assert _poll_count(polls, 1, 2)
    started = polls[0]
    for load in (2, 1, 0):  # three bays free while the early poll waits
        _settle_at(store, machine, load)
    assert _poll_count(polls, 2, FLOOR + 1), "no poll was brought forward"
    assert polls[1] - started >= FLOOR, "the poll came sooner than the floor"
    assert not _poll_count(polls, 3, 3 * FLOOR), "the freed bays were not one poll"


def test_a_freed_bay_with_the_cap_still_full_brings_nothing_forward(monkeypatch):
    store, polls, machine = _store(monkeypatch, limit=1)
    _settle_at(store, machine, 3)
    _settle_at(store, machine, 2)
    assert not _poll_count(polls, 1, 2 * FLOOR)


def test_a_freed_bay_with_nothing_queued_brings_nothing_forward(monkeypatch):
    store, polls, machine = _store(monkeypatch)
    store.queued_tasks = []
    _settle_at(store, machine, 3)
    _settle_at(store, machine, 2)
    assert not _poll_count(polls, 1, 2 * FLOOR)


def test_an_ask_with_a_bay_free_brings_the_next_poll_forward(monkeypatch):
    store, polls, machine = _store(monkeypatch)
    store.queued_tasks = []
    monkeypatch.setattr(store, "_requested_task", lambda entry: _ask(entry.number))
    store._request([autofix.RequestedWork(
        number=7, url="https://github.com/o/r/pull/7", author="",
        config={"kind": "review"})])
    assert _poll_count(polls, 1, 2 * FLOOR)


def test_a_raised_cap_brings_the_next_poll_forward(monkeypatch):
    store, polls, machine = _store(monkeypatch, limit=1)
    _settle_at(store, machine, 1)
    store.auto_task_limit = 2
    assert _poll_count(polls, 1, 2 * FLOOR)


def test_a_poll_still_running_at_the_floor_is_waited_out(monkeypatch):
    """Not bounced off: the running poll may have drained before the bay freed."""
    store, polls, machine = _store(monkeypatch)
    release = threading.Event()
    monkeypatch.setattr(store, "_autofix_poll_once",
                        lambda: (polls.append(time.monotonic()), release.wait(5)))
    _settle_at(store, machine, 1)
    store.run_autofix_poll_async()
    _settle_at(store, machine, 0)
    time.sleep(FLOOR + 1.5)
    release.set()
    assert _poll_count(polls, 2, 2), "the poll brought forward was dropped"


def test_the_poll_timer_restarts_on_every_poll(monkeypatch):
    """The next periodic poll is an interval after the last one, whatever started it,
    so one a freed bay brought forward does not have another follow straight on."""
    from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal
    from PySide6.QtWidgets import QApplication

    from diplomat_app import app as app_module

    QApplication.instance() or QApplication([])

    class _Store(QObject):
        autofix_poll_started = Signal()

        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    class _Timer(QTimer):
        @staticmethod
        def singleShot(*args, **kwargs):
            pass

    class _Anything:
        def __getattr__(self, name):
            return _Anything()

        def __call__(self, *args, **kwargs):
            return _Anything()

    monkeypatch.setattr(app_module, "Store", _Store)
    monkeypatch.setattr(app_module, "QTimer", _Timer)
    for name in ("QApplication", "Panel", "_app_icon"):
        monkeypatch.setattr(app_module, name, _Anything())
    monkeypatch.setattr(app_module.DiplomatApp, "_build_tray", lambda self: None)
    monkeypatch.setattr(app_module.DiplomatApp, "trigger_refresh", lambda self: None)
    monkeypatch.delenv("DIPLOMAT_PREFILL", raising=False)

    app = app_module.DiplomatApp()
    try:
        assert app.store.polls_early is True
        # A restart re-registers the timer, under a new id.
        before = app.autofix_timer.timerId()
        # From a worker thread, as a poll brought forward starts.
        t = threading.Thread(target=app.store.autofix_poll_started.emit)
        t.start()
        t.join()
        for _ in range(20):
            QCoreApplication.processEvents()
        assert app.autofix_timer.isActive()
        assert app.autofix_timer.timerId() != before
    finally:
        for timer in vars(app).values():
            if isinstance(timer, QTimer):
                timer.stop()
