"""Tasks in the Linux Store: the sync period, the CLI calls and their bounds, the sync
worker, and every edit, through the real ``diplomat-core`` in this test's
``DIPLOMAT_TASKS_DIR`` (``conftest.isolated_tasks_dir``) and a stub ``gh``.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest
from diplomat_app.render import gh_pr, write_gh_stub
from diplomat_app.store import Store
from diplomat_runtime import core, tasks
from test_autofix import fake_probes

pytest.importorskip("PySide6")

needs_core = pytest.mark.skipif(not os.environ.get("DIPLOMAT_CORE_BIN"),
                                reason="DIPLOMAT_CORE_BIN not set")

PR7 = "https://github.com/o/r/pull/7"


def watched(n: int, kind: str = "issues") -> str:
    cfg = core.config()
    return f"https://github.com/{cfg['owner']}/{cfg['repo']}/{kind}/{n}"


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def tstore(app, monkeypatch):
    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    fake_probes(monkeypatch)
    st = Store()
    st.me = "alice"
    yield st
    assert st.wait_for_background(30) == []


class GitHub:
    """The stub ``gh`` ``DIPLOMAT_GH`` names: what it answers, and what it was asked."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self.answers: dict = {}

    def answer(self, **items) -> None:
        """``o_r_7=node`` -> ``o/r#7``."""
        for key, node in items.items():
            owner, repo, n = key.rsplit("_", 2)
            self.answers[f"{owner}/{repo}#{n}"] = node
        write_gh_stub(self.dir, self.answers)

    def fail(self, stderr: str) -> None:
        write_gh_stub(self.dir, {"fail": stderr})

    def calls(self) -> list[list[str]]:
        path = self.dir / "gh-calls"
        return [json.loads(ln) for ln in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def github(tmp_path, monkeypatch) -> GitHub:
    gh = GitHub(tmp_path / "gh")
    monkeypatch.setenv("DIPLOMAT_GH", write_gh_stub(gh.dir, {}))
    return gh


def settle(store: Store) -> None:
    """Every worker done and what each signalled delivered, including the work a
    delivery starts (an edit's sync)."""
    from PySide6.QtWidgets import QApplication

    for _ in range(20):
        assert store.wait_for_background(60) == []
        QApplication.processEvents()
        with store._workers_lock:
            if not any(t.is_alive() for t in store._workers):
                QApplication.processEvents()
                return
    raise AssertionError("the tracker's workers never settled")


def _sync(store: Store, *, force: bool = False) -> None:
    store.run_tracker_sync_async(force=force)
    settle(store)


def _edit(store: Store, op: dict) -> str | None:
    """``edit_tracked_task_async``'s answer, once the edit and what follows it landed."""
    origin, answers = object(), []
    store.tracked_task_edited.connect(lambda o, error: o is origin and answers.append(error))
    store.edit_tracked_task_async(op, origin)
    settle(store)
    [error] = answers
    return error


def _create(store: Store, title: str, *, tags=(), links=()) -> str:
    assert _edit(store, {"op": "create", "title": title, "tags": list(tags),
                         "links": list(links)}) is None
    return next(v["task"]["id"] for v in store.tracked_tasks if v["task"]["title"] == title.strip())


def _view(store: Store, task_id: str) -> dict:
    return next(v for v in store.tracked_tasks if v["task"]["id"] == task_id)


def _events_file() -> Path:
    return Path(os.environ["DIPLOMAT_TASKS_DIR"]) / "events.jsonl"


# MARK: - the period and the bounds


@pytest.mark.parametrize("raw, secs", [
    (None, 200.0), ("120", 120.0), ("5", 30.0), ("abc", 200.0), ("nan", 200.0),
    ("inf", 200.0), ("1e12", tasks.MAX_SYNC_SECS),
])
def test_the_sync_period_is_the_asset_held_to_its_floor_and_a_day(monkeypatch, raw, secs):
    monkeypatch.setattr(tasks, "tunables", lambda: {"syncSecs": 200, "minSyncSecs": 30})
    if raw is None:
        monkeypatch.delenv("DIPLOMAT_TASKS_SECS", raising=False)
    else:
        monkeypatch.setenv("DIPLOMAT_TASKS_SECS", raw)
    assert tasks.sync_secs() == secs


def test_the_period_is_read_from_the_shared_asset(monkeypatch):
    monkeypatch.delenv("DIPLOMAT_TASKS_SECS", raising=False)
    assert core.tasks()["syncSecs"] == 300 and core.tasks()["minSyncSecs"] == 60
    assert tasks.sync_secs() == 300.0
    monkeypatch.setenv("DIPLOMAT_TASKS_SECS", "1")
    assert tasks.sync_secs() == 60.0


def test_the_sync_child_is_given_one_gh_call_per_chunk(monkeypatch):
    monkeypatch.setattr(tasks, "tunables", lambda: {"chunk": 25})
    assert [tasks.sync_timeout(n) for n in (0, 25, 26, 51)] == [100, 100, 165, 230]
    seen = []

    class Done:
        returncode, stdout, stderr = 0, '{"events":0,"ok":true}', ""

    monkeypatch.setattr("diplomat_runtime.promptcore.core_bin", lambda: "/bin/core")
    monkeypatch.setattr("subprocess.run",
                        lambda argv, **kw: seen.append((argv[1:], kw["timeout"])) or Done())
    assert tasks.sync(26) == {"events": 0, "ok": True}
    assert seen == [(["tasks-sync"], 165)]


def test_a_sync_the_cli_could_not_answer_is_an_error_not_a_raise(monkeypatch):
    def missing():
        raise tasks.promptcore.CoreBinaryMissing("diplomat-core not found")

    monkeypatch.setattr("diplomat_runtime.promptcore.core_bin", missing)
    assert tasks.sync(1) == {"ok": False, "error": "diplomat-core not found", "events": 0}


# MARK: - edits, through the real CLI


@needs_core
def test_a_created_task_round_trips_through_the_cli(tstore):
    fired = []
    tstore.tracked_tasks_changed.connect(lambda: fired.append(1))
    task_id = _create(tstore, "  Physical iPhone ", tags=["Area:Devices", "kind feature"],
                      links=["#12", f"{PR7}/files", "https://ws.slack.com/archives/C1/p1"])
    assert fired
    # The store's rows are the CLI's own answer, read back from disk by a second call.
    assert tstore.tracked_tasks == tasks.views()
    view = _view(tstore, task_id)
    assert view["task"]["title"] == "Physical iPhone"
    assert view["task"]["tags"] == ["area:devices", "kind-feature"]
    assert [(lk["url"], lk["kind"]) for lk in view["task"]["links"]] == [
        (watched(12), "issue"), (PR7, "pr"), ("https://ws.slack.com/archives/C1/p1", "slack")]
    assert (view["status"], view["reasons"]) == ("in-progress",
                                                 ["#12:unsynced", "o/r#7:unsynced"])
    assert view["history"][-1]["tags"] == ["created", "tag:+area:devices", "tag:+kind-feature"]


@needs_core
def test_each_edit_lands_in_the_task_and_its_history(tstore):
    task_id = _create(tstore, "Tor transport", tags=["area:network", "old"])

    def edit(**op) -> dict:
        assert _edit(tstore, {"id": task_id, **op}) is None
        return _view(tstore, task_id)

    v = edit(op="update", tags=["area:network", "kind:feature"])
    assert v["task"]["tags"] == ["area:network", "kind:feature"]
    assert v["history"][0]["tags"] == ["tag:+kind:feature", "tag:-old"]

    v = edit(op="link", url="o/r#7")
    assert [lk["url"] for lk in v["task"]["links"]] == ["https://github.com/o/r/issues/7"]
    assert (v["history"][0]["tags"], v["history"][0]["url"]) == (
        ["link:+"], "https://github.com/o/r/issues/7")

    v = edit(op="note", text="waiting on review", tags=["Waiting"])
    assert {k: v["history"][0][k] for k in ("source", "tags", "text")} == {
        "source": "note", "tags": ["waiting"], "text": "waiting on review"}

    v = edit(op="update", pinnedStatus="paused")
    assert (v["status"], v["reasons"], v["task"]["pinnedStatus"]) == ("paused", ["pinned"],
                                                                       "paused")
    v = edit(op="update", pinnedStatus="")
    assert "pinnedStatus" not in v["task"] and v["history"][0]["tags"] == ["unpinned"]

    assert _edit(tstore, {"op": "archive", "id": task_id}) is None
    assert tstore.tracked_tasks == []
    tstore.set_tracker_show_archived(True)
    settle(tstore)
    assert [v["task"]["id"] for v in tstore.tracked_tasks] == [task_id]
    assert _view(tstore, task_id)["task"]["archivedAt"]
    assert _edit(tstore, {"op": "unarchive", "id": task_id}) is None
    tstore.set_tracker_show_archived(False)
    settle(tstore)
    assert [v["task"]["id"] for v in tstore.tracked_tasks] == [task_id]


@needs_core
def test_an_edit_the_core_refuses_is_its_reason_and_changes_nothing(tstore):
    task_id = _create(tstore, "Tor transport")
    before = tstore.tracked_tasks
    assert _edit(tstore, {"op": "update", "id": "t-nope", "tags": ["x"]}) == \
        "diplomat-core tasks-edit failed: diplomat-core: no task t-nope"
    assert _edit(tstore, {"op": "link", "id": task_id, "url": "ftp://x"}) == \
        "diplomat-core tasks-edit failed: diplomat-core: not a link: ftp://x"
    assert tstore.tracked_tasks is before
    assert tasks.views() == before


# MARK: - the sync worker


@needs_core
def test_the_tick_syncs_only_while_a_task_is_not_archived(tstore, github, monkeypatch):
    synced = []
    real = tasks.sync
    monkeypatch.setattr(tasks, "sync", lambda n: synced.append(n) or real(n))

    _sync(tstore)
    assert synced == [] and github.calls() == []

    github.answer(o_r_7=gh_pr(PR7, "Seven", ci="FAILURE"))
    task_id = _create(tstore, "Seven", links=[PR7, "https://example.com/doc"])
    # The create's own sync.
    assert synced == [1] and len(github.calls()) == 1
    view = _view(tstore, task_id)
    assert (view["status"], view["reasons"]) == ("blocked", ["o/r#7:ci-failing"])
    assert view["history"][0]["tags"] == ["linked", "pr:open"]
    assert view["task"]["links"][0]["title"] == "Seven"
    assert tstore.tracker_sync_events == 1 and tstore.tracker_sync_error is None
    assert tstore.tracker_last_sync is not None and not tstore.tracker_syncing
    _sync(tstore)
    assert synced == [1, 1]

    assert _edit(tstore, {"op": "archive", "id": task_id}) is None
    _sync(tstore)
    assert synced == [1, 1]
    # Even with the archived ones on screen: they are not the ones the tick asks about.
    tstore.set_tracker_show_archived(True)
    _sync(tstore)
    assert synced == [1, 1]
    _sync(tstore, force=True)
    assert synced == [1, 1, 0]


@needs_core
def test_a_task_another_process_made_is_seen_and_synced_by_the_next_tick(tstore, github):
    github.answer(o_r_7=gh_pr(PR7, "Seven"))
    tasks.edit({"op": "create", "title": "From an agent", "links": ["o/r#7"]})
    assert tstore.tracked_tasks == []
    _sync(tstore)
    assert [v["task"]["title"] for v in tstore.tracked_tasks] == ["From an agent"]
    assert len(github.calls()) == 1
    # The issue URL `o/r#7` was stored as is rewritten once GitHub says it is a PR.
    assert tstore.tracked_tasks[0]["task"]["links"][0]["url"] == PR7


@needs_core
def test_a_gh_failure_is_the_error_and_writes_nothing(tstore, github):
    github.fail("HTTP 502: Bad Gateway")
    _create(tstore, "Seven", links=[PR7])
    lines = _events_file().read_text()
    _sync(tstore)
    assert tstore.tracker_sync_error == "gh exited 1: HTTP 502: Bad Gateway"
    assert tstore.tracker_sync_events == 0
    assert _events_file().read_text() == lines
    assert not (Path(os.environ["DIPLOMAT_TASKS_DIR"]) / "mirror.json").exists()

    github.answer(o_r_7=gh_pr(PR7, "Seven"))
    _sync(tstore)
    assert tstore.tracker_sync_error is None and tstore.tracker_sync_events == 1


@needs_core
def test_the_blocked_count_is_of_unarchived_blocked_tasks(tstore, github):
    pr8 = "https://github.com/o/r/pull/8"
    github.answer(o_r_7=gh_pr(PR7, "Seven", ci="ERROR"),
                  o_r_8=gh_pr(pr8, "Eight", mergeable="CONFLICTING"))
    _create(tstore, "Seven", links=[PR7])
    eight = _create(tstore, "Eight", links=[pr8])
    _create(tstore, "Notes only")
    _sync(tstore)
    assert sorted(v["status"] for v in tstore.tracked_tasks) == ["blocked", "blocked", "open"]
    assert tstore.tracker_blocked == 2
    assert _edit(tstore, {"op": "archive", "id": eight}) is None
    tstore.set_tracker_show_archived(True)
    settle(tstore)
    assert _view(tstore, eight)["status"] == "blocked"
    assert tstore.tracker_blocked == 1


@needs_core
def test_a_list_the_cli_cannot_answer_keeps_the_rows_and_says_why(tstore, monkeypatch):
    _create(tstore, "Kept")
    rows = tstore.tracked_tasks

    def missing():
        raise tasks.promptcore.CoreBinaryMissing("diplomat-core not found")

    monkeypatch.setattr("diplomat_runtime.promptcore.core_bin", missing)
    tstore.refresh_tracked_tasks()
    assert tstore.tracked_tasks is rows
    assert tstore.tracker_list_error == "diplomat-core not found"


@needs_core
def test_a_create_a_link_and_an_unarchive_each_sync_and_nothing_else_does(tstore, github):
    pr8 = "https://github.com/o/r/pull/8"
    github.answer(o_r_7=gh_pr(PR7, "Seven", draft=True), o_r_8=gh_pr(pr8, "Eight"))
    task_id = _create(tstore, "Seven", links=[PR7])
    assert len(github.calls()) == 1
    assert _view(tstore, task_id)["linkStates"] == {PR7: "draft"}

    def calls_after(**op) -> int:
        assert _edit(tstore, {"id": task_id, **op}) is None
        return len(github.calls())

    assert calls_after(op="note", text="x") == 1
    assert calls_after(op="update", tags=["a"]) == 1
    assert calls_after(op="link", url=pr8) == 2
    assert _view(tstore, task_id)["linkStates"] == {PR7: "draft", pr8: "open"}
    assert calls_after(op="archive") == 2
    assert calls_after(op="unarchive") == 3


# MARK: - the Qt thread never waits on the CLI


def _one_task(task_id: str = "t-1", **task) -> dict:
    return {"task": {"id": task_id, "title": "T", "links": [], **task}, "status": "open",
            "reasons": [], "history": [], "linkStates": {}}


def test_every_tracker_cli_call_runs_off_the_qt_thread(tstore, monkeypatch):
    from diplomat_app.tasksview import TasksView

    ran: list[tuple[str, threading.Thread]] = []
    monkeypatch.setattr(tasks, "views", lambda archived=False, me="": ran.append(
        ("tasks-list", threading.current_thread())) or [_one_task()])
    monkeypatch.setattr(tasks, "edit", lambda op: ran.append(
        (f"tasks-edit {op['op']}", threading.current_thread())) or {})
    monkeypatch.setattr(tasks, "sync", lambda n: ran.append(
        ("tasks-sync", threading.current_thread())) or {"ok": True, "events": 0})
    tstore.tracked_tasks = [_one_task()]
    view = TasksView(tstore)

    # A worker mid-read holds the list lock; nothing the Qt thread does may wait on it.
    held, release = threading.Event(), threading.Event()

    def reading() -> None:
        with tstore._tracker_lock:
            held.set()
            release.wait(30)

    reader = threading.Thread(target=reading)
    reader.start()
    held.wait(5)
    try:
        started = time.monotonic()
        view.open()
        view.archived_box.setChecked(True)
        view.card("t-1").buttons["pin-done"].click()
        view.new_title.setText("New")
        view.create()
        view.sync_btn.click()
        assert time.monotonic() - started < 1
    finally:
        release.set()
        reader.join()
    settle(tstore)
    assert {name for name, _ in ran} == {"tasks-list", "tasks-edit update", "tasks-edit create",
                                         "tasks-sync"}
    assert all(thread is not threading.main_thread() for _, thread in ran)


def test_edits_run_one_at_a_time_in_the_order_asked(tstore, monkeypatch):
    applied, answers = [], []
    gate = threading.Event()

    def edit(op):
        if op["n"] == 0:
            gate.wait(30)
        applied.append(op["n"])
        return {}

    monkeypatch.setattr(tasks, "edit", edit)
    monkeypatch.setattr(tasks, "views", lambda archived=False, me="": [])
    tstore.tracked_task_edited.connect(lambda origin, error: answers.append((origin, error)))
    for n in range(4):
        tstore.edit_tracked_task_async({"op": "note", "n": n}, n)
    gate.set()
    settle(tstore)
    assert applied == [0, 1, 2, 3]
    assert answers == [(0, None), (1, None), (2, None), (3, None)]


@pytest.mark.parametrize("live, asked_mid_sync, follow_up", [
    # Any number of asks while one runs is one more sync after it.
    (True, [False, False, True], 1),
    # A "Sync now" among them forces the one that follows, as it would have alone.
    (False, [False, True], 1),
    (False, [False], 0),
])
def test_a_sync_asked_for_mid_sync_runs_after_it(tstore, monkeypatch, live, asked_mid_sync,
                                                 follow_up):
    synced, in_sync, release = [], threading.Event(), threading.Event()

    def sync(n):
        synced.append(n)
        in_sync.set()
        release.wait(30)
        return {"ok": True, "events": 0}

    monkeypatch.setattr(tasks, "sync", sync)
    monkeypatch.setattr(tasks, "views",
                        lambda archived=False, me="": [_one_task(archivedAt=None if live else "x")])
    tstore.run_tracker_sync_async(force=True)
    assert in_sync.wait(10)
    for force in asked_mid_sync:
        tstore.run_tracker_sync_async(force=force)
    release.set()
    settle(tstore)
    assert len(synced) == 1 + follow_up
    # And the asks after it start a sync of their own.
    tstore.run_tracker_sync_async(force=True)
    settle(tstore)
    assert len(synced) == 2 + follow_up and not tstore.tracker_syncing
