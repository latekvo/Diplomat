"""The panel's Agent-tasks list: the agents filling the bays of this machine's
automatic-task cap, the queue behind it, and the bays standing empty.

The store's half of this is pinned in ``test_autofix.py``; what is asserted here is
the part a passing store cannot promise — that the rows are actually drawn, that an
agent that IS running holds a row where its bay was rather than emptying the section
at the moment the machine is busiest, that the row a *paused* monitor owns says so (a
bare "queued" would promise a start that is never coming), that a task whose spawn is
under way keeps a row of its own rather than leaving a gap, and that the two handles a
queued row carries reach the store: the click that runs it past the cap, and the drop
that reorders the queue.
"""

from __future__ import annotations

import time

import pytest

from diplomat_runtime import autofix
from diplomat_app.panel import Panel
from diplomat_app.store import Store
from test_autofix import AT_PROMPT, WORKING, fake_probes, register_run
from diplomat_app.widgets import (
    FreeSlotRow,
    QueuedTaskRow,
    RunningTaskRow,
    StartingTaskRow,
)

pytest.importorskip("PySide6")


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _live(monkeypatch, *records, idle=False, elapsed=60.0):
    """Make the machine agree that these registered runs are live processes.

    A record alone is not an agent: the resolver asks the process table whether its pid
    is there, and the screen whether that pid is working or waiting. Both are said
    here, so a row's state is the resolver's answer rather than something the test
    poked into the store."""
    from diplomat_runtime import agentstate as A

    fake_probes(
        monkeypatch,
        processes={r.pid: A.ProcInfo(tty=r.tty, elapsed=elapsed, is_agent=True)
                   for r in records},
        tails={r.tty: (AT_PROMPT if idle else WORKING) for r in records},
    )


@pytest.fixture
def panel(app, monkeypatch):
    # Opening the panel otherwise shells the allocator installer, starts a mesh node
    # and scans `ps`; none of that belongs in a test about the rows it draws.
    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    fake_probes(monkeypatch)
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


def test_a_queued_task_gets_a_row_and_a_bay_for_each_slot_left(panel, monkeypatch):
    panel.store.queued_tasks = [_queued(512)]
    fake_probes(monkeypatch, live_prs={404})
    panel._rebuild_agent_tasks()

    rows = _rows(panel, QueuedTaskRow)
    assert len(rows) == 1
    assert len(_rows(panel, FreeSlotRow)) == 1  # cap 2, one agent up


def test_a_running_agent_holds_the_bay_it_took(panel, monkeypatch):
    """The complaint this list was failing: a bay that reads *free slot* while empty
    and then vanishes with nothing in its place the moment an agent starts, so the
    section empties out exactly when the machine is busiest. The agent stands in the
    bay it spent, above the free ones."""
    _live(monkeypatch, register_run(
        512, pid=5121, tty="pts/1",
        label="Auto · Review-req · #512 (@octocat)", kind="review"))
    panel._rebuild_agent_tasks()

    rows = _rows(panel, RunningTaskRow)
    assert len(rows) == 1
    assert "Auto · Review-req · #512 (@octocat)" in _row_text(rows[0])
    assert "running" in _row_text(rows[0])
    # Cap of two: the agent takes one bay and the other is still drawn free.
    assert _row_kinds(panel) == ["RunningTaskRow", "FreeSlotRow"]


def test_a_finished_agent_keeps_its_row_and_gives_the_bay_back(panel, monkeypatch):
    """An agent runs in an interactive session, so finishing its work leaves the
    window open at a prompt rather than closing it. Both halves have to be on screen:
    the bay comes back (there is nothing running in it), and the row stays to say what
    the open terminal is — a row that vanished would leave the operator a window with
    no explanation, which is how the wedge went unnoticed for twelve hours.

    So this is the one case where the rows outnumber the cap, and it says why."""
    _live(monkeypatch, register_run(
        512, pid=5121, tty="pts/1",
        label="Auto · Review-req · #512 (@octocat)", kind="review"), idle=True)
    panel._rebuild_agent_tasks()

    rows = _rows(panel, RunningTaskRow)
    assert len(rows) == 1
    assert "awaiting input" in _row_text(rows[0])
    assert "running" not in _row_text(rows[0])
    # Cap of two with nothing working: BOTH bays are free, under the row that explains
    # the terminal still on screen.
    assert _row_kinds(panel) == ["RunningTaskRow", "FreeSlotRow", "FreeSlotRow"]


def test_every_bay_of_the_cap_is_drawn_whatever_is_in_it(panel, monkeypatch):
    """Running, starting and free are three states of the same bay, so their rows
    always add up to the cap. Drop any one of them and the list stops being a picture
    of the machine's capacity."""
    panel.store.auto_task_limit = 3  # one bay per state
    _live(monkeypatch, register_run(512, pid=5121, tty="pts/1", label="Auto · #512"))
    task = _queued(508)
    panel.store.queued_tasks = [task]
    panel.store._begin_starting(task)
    panel._rebuild_agent_tasks()

    assert _row_kinds(panel) == ["RunningTaskRow", "StartingTaskRow", "FreeSlotRow"]


def test_an_agent_only_ps_can_see_is_still_given_a_row(panel, monkeypatch):
    """An applet restart loses the book, not the agents — and such an agent counts
    against the cap for as long as it runs. Drawn from its PR number alone, and
    saying so: a bare "#512" beside a dispatched row's label would otherwise read as
    a dispatch that lost its own name."""
    fake_probes(monkeypatch, live_prs={512})
    panel._rebuild_agent_tasks()

    rows = _rows(panel, RunningTaskRow)
    assert len(rows) == 1
    assert "#512" in _row_text(rows[0])
    assert "running · untracked" in _row_text(rows[0])


def test_a_running_row_says_how_long_it_has_been_going(panel, monkeypatch):
    """Whether an agent has been up two minutes or two hours is the difference
    between waiting for it and going to look at it."""
    _live(monkeypatch, register_run(512, pid=5121, tty="pts/1", label="Auto · #512",
                                    dispatched_at=time.time() - 95 * 60),
          elapsed=95 * 60)
    panel._rebuild_agent_tasks()

    assert "running · for 1h 35m" in _row_text(_rows(panel, RunningTaskRow)[0])


def test_a_run_the_mesh_placed_here_says_where_it_came_from(panel, monkeypatch):
    """It spends this machine's bay like any other agent, but nothing on this screen
    dispatched it — without the note, an agent appears in a bay the operator watched
    the mesh route away."""
    from diplomat_runtime import agentstate as A
    _live(monkeypatch, register_run(512, pid=5121, tty="pts/1", label="Auto · #512",
                                    kind="review",
                                    placement=A.PLACEMENT_MESH_HERE))
    panel._rebuild_agent_tasks()

    assert "via mesh" in _row_text(_rows(panel, RunningTaskRow)[0])


def test_a_manual_spawn_gets_a_row_but_takes_no_bay(panel, monkeypatch):
    """A panel click spends none of the automatic budget, so every bay of the cap is
    still free. It is drawn anyway: the list answers "what is this machine doing about
    my PRs", and an agent the operator started by hand is part of that answer. Hiding
    it was this front-end's own divergence — macOS always drew one — and it meant the
    rows and the cap were answering different questions."""
    _live(monkeypatch, register_run(512, source=autofix.SOURCE_PANEL, pid=5121,
                                    tty="pts/1", label="Review · #512"))
    panel._rebuild_agent_tasks()

    assert len(_rows(panel, RunningTaskRow)) == 1
    assert len(_rows(panel, FreeSlotRow)) == panel.store.auto_task_limit


def test_the_task_count_covers_what_is_running(panel, monkeypatch):
    """The count is the tasks, wherever they are in their life — bays standing empty
    are not tasks. A count that dropped as work STARTED would read as work lost."""
    task = _queued(512)
    panel.store.queued_tasks = [task]
    panel._rebuild_agent_tasks()
    assert _task_count(panel) == 1

    _live(monkeypatch, register_run(508, pid=5081, tty="pts/1", label="Auto · #508"))
    panel._rebuild_agent_tasks()
    assert _task_count(panel) == 2


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
    kinds = (RunningTaskRow, StartingTaskRow, FreeSlotRow, QueuedTaskRow)
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
