"""One settle of the run book at a time.

The 3-minute poll and the panel's 8-second refresh both settle: write back what a
tick learned, retire what finished, price it, audit it. Two settles overlapping -
the poll worker and a panel tick, or two panel ticks stacked behind a slow probe -
retired and priced the same run twice.
"""

from __future__ import annotations

import threading
import time

from diplomat_app.store import Store


def test_a_display_refresh_skips_a_settle_already_under_way(monkeypatch):
    store = Store()
    settles: list[float] = []

    def slow_settle():
        settles.append(time.monotonic())
        time.sleep(0.3)

    monkeypatch.setattr(store, "_settle_agents", slow_settle)
    threads = [threading.Thread(target=store.refresh_auto_task_count) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert len(settles) == 1


def test_the_poll_waits_for_the_settle_it_would_have_raced(monkeypatch):
    store = Store()
    order: list[str] = []
    store._settle_lock.acquire()
    monkeypatch.setattr(store, "_settle_agents", lambda: order.append("settled"))

    def poll_settle():
        with store._settle_lock:
            store._settle_agents()

    # What _autofix_poll_once does around its settle, run on a worker while the
    # panel-side lock is held: it waits rather than settling alongside.
    t = threading.Thread(target=poll_settle)
    t.start()
    time.sleep(0.2)
    order.append("released")
    store._settle_lock.release()
    t.join(timeout=5)
    assert order == ["released", "settled"]
