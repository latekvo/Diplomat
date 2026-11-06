"""The Tasks screen and its ✓ header button, drawn from what the real ``diplomat-core``
answers in this test's ``DIPLOMAT_TASKS_DIR`` after a sync against a stub ``gh``; every
control's edit read back through the CLI; and the ``tasks`` render mode through the real
entry point.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from diplomat_app import tasksview
from diplomat_app.render import gh_issue, gh_pr
from diplomat_app.tasksview import TaskCard, TasksView
from diplomat_runtime import tasks
from test_tasks_store import PR7, GitHub, _create, _sync, watched
from test_tasks_store import app, github, tstore  # noqa: F401 — fixtures

pytest.importorskip("PySide6")

pytestmark = pytest.mark.skipif(not os.environ.get("DIPLOMAT_CORE_BIN"),
                                reason="DIPLOMAT_CORE_BIN not set")

_LINUX_DIR = Path(__file__).resolve().parents[1]
SLACK = "https://ws.slack.com/archives/C1/p1"


@pytest.fixture
def opened(monkeypatch) -> list[str]:
    """Every URL the screen hands the desktop, in order."""
    seen: list[str] = []
    monkeypatch.setattr(tasksview.QDesktopServices, "openUrl",
                        lambda url: seen.append(url.toString()))
    return seen


def _click(widget) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    QTest.mouseClick(widget, Qt.MouseButton.LeftButton)


def _chip_texts(widget) -> list[str]:
    from PySide6.QtWidgets import QLabel

    return [w.text() for w in widget.findChildren(QLabel)]


def _blocked_task(store, github: GitHub) -> str:
    """``Seven``: o/r#7 failing CI with a comment since the first sync, watched issue
    #12, which GitHub no longer has, a Slack thread and a doc."""
    github.answer(o_r_7=gh_pr(PR7, "Seven", comments=[("C1", "kacper", "old")]))
    task_id = _create(store, "Seven", tags=["area:devices", "blocked-on:apple"],
                      links=[PR7, "#12", SLACK, "https://docs.example.com/x"])
    _sync(store)
    github.answer(o_r_7=gh_pr(PR7, "Seven", ci="FAILURE",
                              comments=[("C1", "kacper", "old"),
                                        ("C2", "hubert", "needs  a signing\nidentity")]))
    _sync(store)
    return task_id


def test_a_task_draws_its_status_reasons_tags_and_links(tstore, github, opened):
    task_id = _blocked_task(tstore, github)
    view = TasksView(tstore)
    card = view.card(task_id)
    assert card.status_chip.text() == "BLOCKED"
    # A blocked task's reasons are what blocks it.
    assert card.reasons == ["o/r#7:ci-failing"]
    assert card.tags == ["area:devices", "blocked-on:apple"]
    labels = _chip_texts(card)
    for text in ("BLOCKED", "Seven", "o/r#7:ci-failing", "area:devices", "blocked-on:apple"):
        assert text in labels
    assert [(c.text.text(), c.state, c.dot.isHidden()) for c in card.link_chips] == [
        ("o/r#7", "open", False), ("#12", "missing", False), ("slack", None, True),
        ("docs.example.com", None, True)]
    assert view.status.text().startswith("1 task · 1 blocked · last sync ")
    assert view.status.text().endswith(", 2 new events")


def test_clicking_a_link_opens_exactly_its_url(tstore, github, opened):
    task_id = _blocked_task(tstore, github)
    card = TasksView(tstore).card(task_id)
    for chip in card.link_chips:
        _click(chip)
    assert opened == [PR7, watched(12), SLACK, "https://docs.example.com/x"]


def test_the_history_opens_with_time_source_tags_link_and_excerpt(tstore, github, opened):
    task_id = _blocked_task(tstore, github)
    view = TasksView(tstore)
    assert view.card(task_id).history_rows == []
    assert view.card(task_id).history_btn.text() == "▸ History · 9"
    view.card(task_id).history_btn.click()
    card = view.card(task_id)
    assert card.history_btn.text() == "▾ History · 9"
    rows = card.history_rows
    assert [r.tags for r in rows[:3]] == [["ci:failing"], ["link:missing"],
                                          ["linked", "pr:open"]]
    # At GitHub's own time, a day before the sync that saw it.
    comment = rows[-1]
    assert comment.tags == ["comment", "@hubert"]
    assert comment.text._full == "needs a signing identity"
    assert comment.link.text.text() == "o/r#7"
    assert comment.when.text() == datetime(2026, 11, 5, 10, tzinfo=timezone.utc).astimezone(
    ).strftime("%b %d %H:%M")
    # An event with no link of its own opens the item it is about.
    missing = rows[1]
    assert (missing.link.text.text(), missing.link.url) == ("#12", watched(12))
    _click(comment.link)
    _click(missing.link)
    assert opened == [f"{PR7}#issuecomment-C2", watched(12)]
    # It stays open across the rebuild a change causes, and closes again.
    tstore.tracked_tasks_changed.emit()
    assert len(view.card(task_id).history_rows) == 9
    view.card(task_id).history_btn.click()
    assert view.card(task_id).history_rows == []


def test_a_new_task_is_created_through_the_cli(tstore):
    view = TasksView(tstore)
    assert view.new_host.isHidden()
    view.new_btn.click()
    assert not view.new_host.isHidden()
    view.new_title.setText("Tor transport")
    view.new_tags.setText("area:network, kind:feature  waiting")
    view.new_links.setPlainText(f"{PR7}\n\n  o/r#9 \n{SLACK}\n")
    view.create_btn.click()
    [made] = tasks.views()
    assert made["task"]["title"] == "Tor transport"
    assert made["task"]["tags"] == ["area:network", "kind:feature", "waiting"]
    assert [lk["url"] for lk in made["task"]["links"]] == [
        PR7, "https://github.com/o/r/issues/9", SLACK]
    assert view.new_host.isHidden() and view.new_title.text() == ""
    assert [c.id for c in view.cards] == [made["task"]["id"]]


def test_a_refused_new_task_keeps_the_form_and_says_why(tstore):
    view = TasksView(tstore)
    view.new_btn.click()
    view.new_title.setText("   ")
    view.new_links.setPlainText("x")
    view.create_btn.click()
    assert view.new_error.text() == \
        "diplomat-core tasks-edit failed: diplomat-core: a task needs a title"
    assert not view.new_host.isHidden() and tasks.views() == []


def test_each_card_control_edits_through_the_cli(tstore):
    task_id = _create(tstore, "Tor transport", tags=["area:network"])
    view = TasksView(tstore)

    def card() -> TaskCard:
        return view.card(task_id)

    def task() -> dict:
        return next(v for v in tasks.views(archived=True) if v["task"]["id"] == task_id)

    card().buttons["tags"].click()
    assert card().fields["tags"].text() == "area:network"
    card().fields["tags"].setText("area:network kind:feature")
    card().save_btn.click()
    assert task()["task"]["tags"] == ["area:network", "kind:feature"]
    assert card().tags == ["area:network", "kind:feature"]

    card().buttons["link"].click()
    card().fields["url"].setText("o/r#9")
    card().fields["url"].returnPressed.emit()
    assert [lk["url"] for lk in task()["task"]["links"]] == ["https://github.com/o/r/issues/9"]
    assert [c.text.text() for c in card().link_chips] == ["o/r#9"]

    card().buttons["note"].click()
    card().fields["text"].setText("asked on Slack")
    card().fields["tags"].setText("waiting")
    card().save_btn.click()
    note = task()["history"][0]
    assert (note["source"], note["tags"], note["text"]) == ("note", ["waiting"],
                                                            "asked on Slack")

    card().buttons["pin-done"].click()
    assert (task()["status"], task()["task"]["pinnedStatus"]) == ("done", "done")
    assert card().status_chip.text() == "DONE" and "pin-done" not in card().buttons
    card().buttons["unpin"].click()
    assert "pinnedStatus" not in task()["task"]
    card().buttons["pin-paused"].click()
    assert task()["task"]["pinnedStatus"] == "paused"

    card().buttons["archive"].click()
    assert task()["task"]["archivedAt"] and view.cards == []
    view.archived_box.setChecked(True)
    assert [c.id for c in view.cards] == [task_id] and "ARCHIVED" in _chip_texts(card())
    card().buttons["unarchive"].click()
    assert "archivedAt" not in task()["task"]


def test_a_refused_card_edit_stays_open_with_its_reason(tstore):
    task_id = _create(tstore, "Tor transport")
    view = TasksView(tstore)
    card = view.card(task_id)
    card.buttons["link"].click()
    card.fields["url"].setText("ftp://nope")
    card.save_btn.click()
    assert view.card(task_id) is card and not card.editor.isHidden()
    assert card.error.text() == \
        "diplomat-core tasks-edit failed: diplomat-core: not a link: ftp://nope"
    assert tasks.views()[0]["task"]["links"] == []


def test_a_change_waits_for_an_open_editor_rather_than_drop_what_is_typed(tstore):
    task_id = _create(tstore, "Tor transport")
    view = TasksView(tstore)
    card = view.card(task_id)
    card.buttons["note"].click()
    card.fields["text"].setText("half a thought")
    tasks.edit({"op": "create", "title": "From an agent"})
    tstore.refresh_tracked_tasks()
    tstore.tracked_tasks_changed.emit()
    assert view.card(task_id) is card and card.fields["text"].text() == "half a thought"
    assert len(view.cards) == 1
    card.cancel()
    assert sorted(c.task["title"] for c in view.cards) == ["From an agent", "Tor transport"]


def test_a_sync_error_is_a_banner_and_sync_now_forces_a_sync(tstore, github):
    _create(tstore, "Seven", links=[PR7])
    github.fail("HTTP 502: Bad Gateway")
    view = TasksView(tstore)
    assert view.error_banner.isHidden()
    view.sync_btn.click()
    assert tstore.wait_for_background(60) == []
    from PySide6.QtWidgets import QApplication

    QApplication.processEvents()
    assert not view.error_banner.isHidden()
    assert view.error_banner.text() == "⚠ gh exited 1: HTTP 502: Bad Gateway"
    assert view.sync_btn.isEnabled()


def test_the_header_button_counts_blocked_tasks_and_opens_the_screen(tstore, github):
    from diplomat_app.panel import Panel

    pr8 = "https://github.com/o/r/pull/8"
    github.answer(o_r_7=gh_pr(PR7, "Seven", review="CHANGES_REQUESTED"),
                  o_r_8=gh_pr(pr8, "Eight"), o_r_9=gh_issue("https://github.com/o/r/issues/9",
                                                            "Nine"))
    _create(tstore, "Seven", links=[PR7])
    _create(tstore, "Eight", links=[pr8, "o/r#9"])
    panel = Panel(tstore)
    try:
        assert panel.tasks_btn.text() == "✓"
        assert panel.tasks_btn.toolTip() == "Tasks"
        _sync(tstore)
        from PySide6.QtWidgets import QApplication

        QApplication.processEvents()
        assert panel.tasks_btn.text() == "✓ 1"
        assert panel.tasks_btn.toolTip() == "Tasks — 1 blocked"
        panel.tasks_btn.click()
        assert panel._screen == "tasks"
        assert sorted(c.status_chip.text() for c in panel.tasks_view.cards) == [
            "BLOCKED", "IN-REVIEW"]
        panel.tasks_view.done.emit()
        assert panel._screen == "main"
    finally:
        panel.deleteLater()


def test_the_render_mode_draws_through_the_entry_point(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    out = tmp_path / "tasks.png"
    env = {k: v for k, v in os.environ.items()
           if k not in ("DIPLOMAT_TASKS_DIR", "DIPLOMAT_COMMS_DIR", "DIPLOMAT_GH",
                        "DIPLOMAT_CONFIG")}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               QT_QPA_PLATFORM="offscreen", DIPLOMAT_SELF_REPO="/nonexistent",
               DIPLOMAT_RENDER="tasks", DIPLOMAT_RENDER_OUT=str(out),
               PYTHONPATH=os.pathsep.join([str(_LINUX_DIR),
                                           str(_LINUX_DIR.parents[1] / "diplomat-runtime")]))
    done = subprocess.run([sys.executable, "-m", "diplomat_app"], cwd=str(_LINUX_DIR),
                          env=env, capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr
    assert "rendered tasks -> " in done.stdout
    assert out.stat().st_size > 10_000
    assert not (home / ".diplomat" / "tasks").exists()
    assert not (home / ".diplomat" / "comms").exists()

