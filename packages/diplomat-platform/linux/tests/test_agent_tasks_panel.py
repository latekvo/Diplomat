"""The panel's Agent-tasks list: the queue behind the automatic-task cap, and the
bays of that cap standing empty.

The store's half of this is pinned in ``test_autofix.py``; what is asserted here is
the part a passing store cannot promise — that the rows are actually drawn, that the
row a *paused* monitor owns says so (a bare "queued" would promise a start that is
never coming), that a task whose spawn is under way keeps a row of its own rather
than leaving a gap, and that the two handles a queued row carries reach the store:
the click that runs it past the cap, and the drop that reorders the queue.
"""

from __future__ import annotations

import pytest

from diplomat_app import autofix
from diplomat_app.panel import Panel
from diplomat_app.store import Store
from diplomat_app.widgets import FreeSlotRow, QueuedTaskRow, StartingTaskRow

pytest.importorskip("PySide6")


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(app, monkeypatch):
    # Opening the panel otherwise shells the allocator installer, starts a mesh node
    # and scans `ps`; none of that belongs in a test about the rows it draws.
    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    monkeypatch.setattr(Store, "_live_pr_agents", lambda self: set())
    store = Store()
    store.me = "alice"
    p = Panel(store)
    yield p
    p.deleteLater()


def _rows(panel, kind) -> list:
    layout = panel.tasks_col
    return [
        layout.itemAt(i).widget()
        for i in range(layout.count())
        if isinstance(layout.itemAt(i).widget(), kind)
    ]


def _queued(number: int, *, counter="review_requests", action="review-req", kind="review"):
    return autofix.QueuedTask(
        id=autofix.queue_key(action, number),
        job=autofix.AgentJob(
            kind=kind,
            audit_action=action,
            label=f"Review-req · #{number} (@octocat)",
            prompt="P",
            pr_url=f"https://github.com/o/r/pull/{number}",
            pr_number=number,
            counter=counter,
        ),
        attempt=1,
    )


def test_an_idle_machine_shows_its_empty_bays(panel):
    """The list is always there, and on a machine with nothing to do it is the whole
    cap: the panel is where the cap is visible, rather than something to remember."""
    assert panel.store.free_auto_slots == panel.store.auto_task_limit == 2
    assert len(_rows(panel, FreeSlotRow)) == 2
    assert _rows(panel, QueuedTaskRow) == []


def test_a_queued_task_gets_a_row_and_a_bay_for_each_slot_left(panel):
    panel.store.queued_tasks = [_queued(512)]
    panel.store._auto_tasks_measured = 1
    panel._rebuild_agent_tasks()

    rows = _rows(panel, QueuedTaskRow)
    assert len(rows) == 1
    assert len(_rows(panel, FreeSlotRow)) == 1  # cap 2, one agent up


def test_a_paused_monitors_row_says_nothing_will_start_it(panel):
    """"queued" alone promises a start that is never coming while the toggle is off."""
    panel.store.review_requests_enabled = False
    panel.store.queued_tasks = [_queued(512)]
    panel._rebuild_agent_tasks()
    row = _rows(panel, QueuedTaskRow)[0]
    assert "monitor off" in _row_text(row)

    panel.store.review_requests_enabled = True
    panel._rebuild_agent_tasks()
    assert "monitor off" not in _row_text(_rows(panel, QueuedTaskRow)[0])


def test_a_row_reads_as_the_session_it_will_become(panel):
    """Same label the activity feed and the agent will carry — a queued retry that
    dropped its "retry 2" would read as a first attempt."""
    task = _queued(512)
    panel.store.queued_tasks = [autofix.QueuedTask(task.id, task.job, attempt=2)]
    panel._rebuild_agent_tasks()
    assert "Auto · Review-req · #512 (@octocat) · retry 2" in _row_text(
        _rows(panel, QueuedTaskRow)[0]
    )


def test_a_starting_task_keeps_a_row_where_its_agent_will_be(panel):
    """The seconds between the click and the spawn are the whole complaint: with no
    row for them the task vanishes on the press and reappears later, which is what a
    dropped task looks like. It stands above the bays and the queue, where the agent
    it becomes will be — and carries neither handle, since the order no longer decides
    when it runs and it is already running past the cap."""
    task = _queued(512)
    panel.store.queued_tasks = [task, _queued(508)]
    panel.store._begin_starting(task)
    panel._rebuild_agent_tasks()

    rows = _rows(panel, StartingTaskRow)
    assert len(rows) == 1
    assert "Auto · Review-req · #512 (@octocat)" in _row_text(rows[0])
    assert "starting" in _row_text(rows[0])
    from PySide6.QtWidgets import QPushButton

    assert rows[0].findChildren(QPushButton) == []
    assert [t.id for t in panel.store.queued_tasks] == ["review-req:508"]
    # One bay left of the cap of two, because the starting task holds the other.
    assert _row_kinds(panel) == ["StartingTaskRow", "FreeSlotRow", "QueuedTaskRow"]


def test_the_task_count_carries_a_starting_row(panel):
    """Clicking the last queued row would otherwise drop the count to zero for as
    long as its spawn takes."""
    task = _queued(512)
    panel.store.queued_tasks = [task]
    panel._rebuild_agent_tasks()
    assert _task_count(panel) == 1

    panel.store._begin_starting(task)
    panel._rebuild_agent_tasks()
    assert _task_count(panel) == 1


def test_execute_now_asks_the_store_to_run_that_task(panel):
    ran = []
    panel.store.execute_queued_task_async = ran.append
    panel.store.queued_tasks = [_queued(512), _queued(508)]
    panel._rebuild_agent_tasks()

    _run_button(_rows(panel, QueuedTaskRow)[1]).click()
    assert ran == ["review-req:508"]


def test_a_drop_reorders_the_queue_around_the_row_it_landed_on(panel):
    moved = []
    panel.store.move_queued_task = lambda dragged, onto: moved.append((dragged, onto))
    panel.store.queued_tasks = [_queued(512), _queued(508)]
    panel._rebuild_agent_tasks()

    # The row that receives the drop names the dragged one; the pair is what the
    # store's pure reorder takes, in that order.
    _rows(panel, QueuedTaskRow)[1].dropped.emit("review-req:512")
    assert moved == [("review-req:512", "review-req:508")]


def test_a_row_refuses_a_drop_of_itself(app):
    """Not a rearrangement — and accepting it would redraw the list for nothing."""
    row = QueuedTaskRow(task_id="review-req:512", label="l", glyph="☑",
                        hex_color="#FF2D78", paused=False)
    dropped = []
    row.dropped.connect(dropped.append)
    itself, mime_self = _drop_event("review-req:512")
    row.dropEvent(itself)
    assert dropped == []
    other, mime_other = _drop_event("review-req:508")
    row.dropEvent(other)
    assert dropped == ["review-req:508"]
    assert mime_self.text() and mime_other.text()  # kept alive: the event borrows them
    row.deleteLater()


def _row_kinds(panel) -> list[str]:
    """The task rows in the order the list draws them."""
    layout = panel.tasks_col
    kinds = (StartingTaskRow, FreeSlotRow, QueuedTaskRow)
    return [
        type(layout.itemAt(i).widget()).__name__
        for i in range(layout.count())
        if isinstance(layout.itemAt(i).widget(), kinds)
    ]


def _task_count(panel) -> int:
    """The number in the section header's count capsule."""
    from diplomat_app.widgets import SectionHeader

    header = _rows(panel, SectionHeader)[0]
    from PySide6.QtWidgets import QLabel

    return int(
        next(c.text() for c in header.findChildren(QLabel) if c.text().isdigit())
    )


def _row_text(row) -> str:
    from PySide6.QtWidgets import QLabel

    from diplomat_app.widgets import ElidedLabel

    out = []
    for child in row.findChildren(QLabel):
        out.append(child._full if isinstance(child, ElidedLabel) else child.text())
    return " ".join(out)


def _run_button(row):
    from PySide6.QtWidgets import QPushButton

    return row.findChildren(QPushButton)[0]


def _drop_event(key: str):
    """A real QDropEvent carrying one queue key, plus the QMimeData it BORROWS — the
    event does not own it, so a caller that drops the reference gets a use-after-free
    the moment the widget reads it."""
    from PySide6.QtCore import QMimeData, QPointF, Qt
    from PySide6.QtGui import QDropEvent

    mime = QMimeData()
    mime.setText(key)
    event = QDropEvent(QPointF(1, 1), Qt.DropAction.MoveAction, mime,
                       Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
    return event, mime
