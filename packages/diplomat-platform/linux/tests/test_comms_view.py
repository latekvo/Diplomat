"""The Comms screen and its ✉ header button, built offscreen in each state, and the
three ``DIPLOMAT_RENDER`` modes through the real entry point.

``Store.comms_items`` is the ``comms-feed`` fold, which the store tests pin, so here the
items are literals.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from test_autofix import fake_probes

from diplomat_app.commsview import CommsView, JobRow, PingRow
from diplomat_app.store import Store
from diplomat_runtime import autofix, comms

pytest.importorskip("PySide6")

_LINUX_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def store(app, monkeypatch):
    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    fake_probes(monkeypatch)
    st = Store()
    st.me = "alice"
    return st


def _item(n: int, at: str, *, queue_key=None, started=None, finished=None, **extra) -> dict:
    item = {"at": at, "message": {"id": f"D1:{n}.0", "isDM": True, "userName": f"u{n}",
                                  "text": f"text {n}", "reasons": ["dm"],
                                  "permalink": f"https://x.slack.com/p{n}"},
            "decision": {"action": "review", "number": n, "summary": f"sum {n}"},
            "outcome": f"outcome {n}", **extra}
    if queue_key:
        item.update(queueKey=queue_key, label=f"Job {n}")
    if started:
        item["startedAt"] = started
    if finished:
        item["finishedAt"] = finished
    return item


def _rows(view, cls) -> list:
    return [w for w in view.findChildren(cls) if w.parent() is not None]


def _texts(widget) -> list[str]:
    from PySide6.QtWidgets import QLabel

    return [label.text() for label in widget.findChildren(QLabel)]


def _buttons(widget) -> list[str]:
    from PySide6.QtWidgets import QPushButton

    return [b.text() for b in widget.findChildren(QPushButton)]


def _elided(widget) -> list[str]:
    from diplomat_app.widgets import ElidedLabel

    return [w._full for w in widget.findChildren(ElidedLabel)]


def _click(widget) -> None:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest

    QTest.mouseClick(widget, Qt.MouseButton.LeftButton)


def _open_earlier(view) -> None:
    view.earlier_btn.click()


NOT_SIGNED_IN = ("Claude Code is not signed in to Slack: in Claude Code, run /mcp and "
                 "authenticate the Slack server")


def test_until_a_tick_knows_the_login_the_card_says_why_not(store):
    """The card holds the core's own words, once, and no setup steps; once a tick has
    said who the login is, the screen is the feed and the error a banner."""
    store.comms_enabled = True
    store.comms_error = NOT_SIGNED_IN
    view = CommsView(store)
    assert not view.empty.isHidden()
    assert view.body.isHidden()
    assert view.error_banner.isHidden()
    assert _texts(view.empty) == [
        "Comms reads Slack as you, through Claude Code's Slack login", NOT_SIGNED_IN]
    assert not view.empty_error.isHidden()

    store.comms_error = None
    store.comms_changed.emit()
    assert not view.empty.isHidden()
    assert view.empty_error.isHidden()

    store.comms_identity = {"handle": "operator", "teamID": "Ours"}
    store.comms_error = NOT_SIGNED_IN
    store.comms_changed.emit()
    assert view.empty.isHidden()
    assert not view.body.isHidden()
    assert view.error_banner.text() == f"⚠ {NOT_SIGNED_IN}"
    assert not view.error_banner.isHidden()


def test_a_login_and_nothing_yet(store):
    store.comms_enabled = True
    store.comms_identity = {"handle": "operator", "teamID": "Ours"}
    view = CommsView(store)
    assert view.empty.isHidden()
    assert not view.body.isHidden()
    assert view.error_banner.isHidden()
    assert view.status.text() == "Watching as @operator in Ours · waiting for the first poll"
    assert "No pings yet. A DM, an @-mention or a keyword lands here." in _texts(view)
    assert "Nothing queued from Slack yet." in _texts(view)


def test_pings_jobs_and_what_is_new(store):
    comms.reports_dir().mkdir(parents=True)
    (comms.reports_dir() / "42.md").write_text("# report\n")
    store.comms_items = [
        _item(4, "2026-10-27T10:40:00Z"),
        _item(3, "2026-10-27T10:30:00Z", queue_key="review:3"),
        _item(2, "2026-10-27T10:20:00Z", queue_key="investigate:42",
              started="2026-10-27T10:21:00Z"),
        _item(1, "2026-10-27T10:10:00Z", queue_key="review:1",
              started="2026-10-27T10:11:00Z", finished="2026-10-27T10:19:00Z"),
        _item(5, "2026-10-27T10:05:00Z", queue_key="review:5"),
    ]
    store.queued_tasks = [autofix.QueuedTask(
        id="review:3", job=autofix.AgentJob(kind="review", audit_action="review",
                                            label="Job 3", prompt="p"), attempt=1)]
    store.comms_seen_at = "2026-10-27T10:25:00Z"
    store.comms_identity = {"handle": "operator", "teamID": "T1"}
    store.comms_enabled = True
    view = CommsView(store)

    # The read three wait under a closed "Earlier".
    pings = _rows(view, PingRow)
    assert [p.unread for p in pings] == [True, True]
    assert view.earlier_btn.text() == "▸ Earlier · 3"
    _open_earlier(view)
    pings = _rows(view, PingRow)
    assert [p.unread for p in pings] == [True, True, False, False, False]
    assert [p.dot.text() for p in pings] == ["●", "●", "", "", ""]
    assert "REVIEW #4" in _texts(pings[0])
    assert _buttons(pings[0]) == []
    _click(pings[0])
    assert _buttons(pings[0]) == ["Open in Slack"]
    assert "→ outcome 4" in _texts(pings[0])
    assert view.status.text().startswith("Watching as @operator in T1")

    jobs = _rows(view, JobRow)
    status = {j.label._full: [t for t in _texts(j) if t.isupper()][0] for j in jobs}
    assert status == {"Job 3": "QUEUED", "Job 2": "RUNNING", "Job 1": "DONE",
                      "Job 5": "NOT QUEUED"}
    reports = {j.label._full: _buttons(j) for j in jobs}
    assert reports == {"Job 3": [], "Job 2": ["Report"], "Job 1": [], "Job 5": []}
    assert {j.label._full: j.when.text() for j in jobs} == {
        "Job 3": f"asked {_clock('2026-10-27T10:30:00Z')}",
        "Job 2": f"started {_clock('2026-10-27T10:21:00Z')}",
        "Job 1": f"done {_clock('2026-10-27T10:19:00Z')}",
        "Job 5": f"asked {_clock('2026-10-27T10:05:00Z')}"}
    assert {j.label._full: j.toolTip() for j in jobs}["Job 1"] == (
        f"asked {_clock('2026-10-27T10:10:00Z')} · started {_clock('2026-10-27T10:11:00Z')}"
        f" · done {_clock('2026-10-27T10:19:00Z')}")


def _clock(iso: str) -> str:
    from diplomat_runtime.models import Fmt

    return Fmt.clock(comms.parse_iso(iso))


def _one_line(widget) -> bool:
    """No label in it wraps, and it is no taller than one line of its tallest child."""
    from PySide6.QtWidgets import QLabel

    labels = widget.findChildren(QLabel)
    tallest = max(w.sizeHint().height() for w in labels)
    margins = widget.layout().contentsMargins()
    return (not any(w.wordWrap() for w in labels)
            and widget.sizeHint().height() <= tallest + margins.top() + margins.bottom())


def test_a_ping_is_one_line_until_a_click_opens_the_rest(store):
    item = _item(1, "2026-10-27T10:10:00Z", reply="[Diplomat]: Queued a review of #1",
                 replyOutcome="sent")
    item["message"]["text"] = "first line\nsecond line\n" + "x " * 400
    item["decision"]["reason"] = "a direct ask"
    store.comms_items = [item]
    store.comms_enabled = True
    view = CommsView(store)
    [row] = _rows(view, PingRow)
    assert _one_line(row)
    assert row.details is None
    assert _texts(row) == ["●", "u1", "DM", "REVIEW #1", "", "REPLIED",
                           _clock("2026-10-27T10:10:00Z")]
    assert row.text._full == "first line second line " + " ".join(["x"] * 400)

    _click(row)
    assert row.expanded
    shown = _texts(row.details)
    assert shown == ["DM", item["message"]["text"], "sum 1 — a direct ask", "→ outcome 1",
                     "↩ [Diplomat]: Queued a review of #1"]
    assert _buttons(row) == ["Open in Slack"]

    # Every store signal rebuilds the list; an opened row stays open.
    store.comms_changed.emit()
    [row] = _rows(view, PingRow)
    assert row.expanded
    _click(row)
    assert not row.expanded and row.details.isHidden()
    store.comms_changed.emit()
    [row] = _rows(view, PingRow)
    assert row.details is None


def test_read_pings_wait_under_one_closed_earlier(store):
    store.comms_items = [_item(3, "2026-10-27T10:30:00Z"), _item(2, "2026-10-27T10:20:00Z"),
                         _item(1, "2026-10-27T10:10:00Z")]
    store.comms_seen_at = "2026-10-27T10:25:00Z"
    store.comms_enabled = True
    view = CommsView(store)
    assert [r.unread for r in _rows(view, PingRow)] == [True]
    assert view.earlier_btn.text() == "▸ Earlier · 2"
    _open_earlier(view)
    assert view.earlier_btn.text() == "▾ Earlier · 2"
    assert [r.unread for r in _rows(view, PingRow)] == [True, False, False]
    assert all(_one_line(r) for r in _rows(view, PingRow))
    store.comms_changed.emit()
    assert len(_rows(view, PingRow)) == 3
    _open_earlier(view)
    assert len(_rows(view, PingRow)) == 1

    # Opening the screen makes all three read: nothing new, and "Earlier" closed again.
    _open_earlier(view)
    view.open()
    view.open()
    assert _rows(view, PingRow) == []
    assert "Nothing new." in _texts(view.pings_host)
    assert view.earlier_btn.text() == "▸ Earlier · 3"


def test_nothing_read_draws_no_earlier(store):
    store.comms_items = [_item(1, "2026-10-27T10:10:00Z")]
    view = CommsView(store)
    assert view.earlier_btn is None
    assert "Nothing new." not in _texts(view)


@pytest.mark.parametrize(("extra", "chip", "detail"), [
    ({"replyOutcome": "sent", "reply": "[Diplomat]: hi"}, "REPLIED", "↩ [Diplomat]: hi"),
    ({"replyOutcome": "sending", "reply": "[Diplomat]: hi"}, "REPLYING", "↩ reply sending"),
    ({"replyOutcome": "failed: channel_not_found", "reply": "[Diplomat]: hi"},
     "REPLY FAILED", "↩ reply failed: channel_not_found"),
    ({"replyOutcome": "skipped: the docs are silent"}, "NOT ANSWERED",
     "↩ reply skipped: the docs are silent"),
])
def test_a_reply_is_a_pill_on_the_line_and_its_words_in_the_details(store, extra, chip,
                                                                     detail):
    item = _item(1, "2026-10-27T10:10:00Z", **extra)
    item["decision"] = {"action": "question", "number": 0, "summary": "asks"}
    store.comms_items = [item]
    view = CommsView(store)
    [row] = _rows(view, PingRow)
    assert [t for t in _texts(row) if t.isupper()] == ["DM", "QUESTION", chip]
    _click(row)
    assert _texts(row.details)[-1] == detail


def test_a_question_has_its_own_word_and_tint():
    from diplomat_app.commsview import action_chip

    assert action_chip({"action": "question", "number": 0}) == ("QUESTION", "#AF52DE")
    assert action_chip({"action": "review", "number": 4})[0] == "REVIEW #4"
    assert action_chip({"action": "review", "number": 4, "targets": [
        {"action": "review", "number": 4, "repo": "acme/widget"}]})[0] == "REVIEW WIDGET#4"
    assert action_chip(None)[0] == "AWAITING TRIAGE"


def test_a_ping_still_queued_for_triage_says_so(store):
    """A ping has no decision until the core triages its batch, up to ``flushAfterSecs``
    after it was taken."""
    item = _item(1, "2026-10-27T10:10:00Z")
    del item["decision"], item["outcome"]
    store.comms_items = [item]
    store.comms_enabled = True
    view = CommsView(store)
    [row] = _rows(view, PingRow)
    _click(row)
    texts = _texts(row)
    assert "AWAITING TRIAGE" in texts
    assert not any(t.startswith(("REVIEW", "NO ACTION", "→")) for t in texts)


def test_text_from_slack_or_the_triage_is_never_read_as_markup(store):
    """A message can steer the triage's summary, and so the investigation's label,
    into HTML; every label showing Slack's or the model's words draws it as typed."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel

    markup = '<h1 style="font-size:60px">URGENT</h1>'
    item = _item(1, "2026-10-27T10:10:00Z", queue_key="investigate:1")
    item["message"].update(userName=f"<b>{markup}</b>", channelName=markup, isDM=False,
                           text=markup, keyword=markup, reasons=["keyword"])
    item["decision"]["summary"] = markup
    item.update(outcome=markup, label=markup)
    store.comms_items = [item]
    store.comms_enabled = True
    store.comms_identity = {"handle": markup, "teamID": markup}
    store.comms_error = markup
    view = CommsView(store)
    _click(_rows(view, PingRow)[0])
    shown = [w for w in view.findChildren(QLabel) if markup in w.text()]
    # sender, channel, keyword chip, text, summary, outcome, status, banner, and the
    # signed-out card's line
    assert len(shown) == 9
    assert [w.text() for w in shown if w.textFormat() != Qt.TextFormat.PlainText] == []
    # The line's text and the job's label are painted, never parsed.
    assert _elided(view).count(markup) == 2


def test_the_settings_card_draws_slacks_words_as_typed(app, store, monkeypatch):
    from PySide6.QtCore import Qt

    from diplomat_app.settingsview import SettingsView

    markup = "<b>bold</b>"
    store.comms_enabled = True
    store.comms_error = markup
    view = SettingsView(store)
    try:
        assert view._comms_row.summary() == markup
        assert view._comms_row._summary.textFormat() == Qt.TextFormat.PlainText
        assert view._comms_pill.textFormat() == Qt.TextFormat.PlainText
        view._on_comms_tested({"handle": markup, "team": "Ours"}, "")
        assert view._comms_test_status.text() == f"✓ @{markup} in Ours"
        assert view._comms_test_status.textFormat() == Qt.TextFormat.PlainText
    finally:
        view.deleteLater()


def test_the_watch_slack_pill_reads_the_last_error_cut_to_fit(app, store):
    from diplomat_app.settingsview import SettingsView

    long_error = ("Slack refused Claude Code's Slack login: in Claude Code, run /mcp and "
                  "authenticate the Slack server again")
    store.comms_enabled = True
    store.comms_error = "invalid_auth"
    view = SettingsView(store)
    try:
        assert view._comms_pill.text() == "invalid_auth"
        store.comms_error = long_error
        view._refresh_comms_ui()
        assert view._comms_pill.text() == long_error[:47] + "…"
        assert len(view._comms_pill.text()) == 48
    finally:
        view.deleteLater()


def test_an_activity_line_is_drawn_as_typed(app):
    """The feed quotes what triage and Slack wrote: an investigate label carries the
    model's summary and the sender's display name."""
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel

    from diplomat_app.widgets import ActivityRow

    detail = "Investigate · Slack · bob · <h1 style='color:red'>URGENT rot — queued"
    source = "<b>auto</b>"
    row = ActivityRow(glyph="x", detail=detail, source=source, source_color="#888",
                      clock=None)
    shown = {w.text(): w.textFormat() for w in row.findChildren(QLabel)
             if w.text() in (detail, source)}
    assert shown == {detail: Qt.TextFormat.PlainText, source: Qt.TextFormat.PlainText}


def test_the_test_button_comes_back_whatever_the_ask_raises(app, store, monkeypatch):
    import http.client

    from diplomat_app.settingsview import SettingsView

    def broken():
        raise http.client.IncompleteRead(b"12345678", 92)

    monkeypatch.setattr(comms, "auth", broken)
    view = SettingsView(store)
    try:
        view._test_comms_login()
        assert not view._comms_test.isEnabled()
        assert store.wait_for_background() == []
        app.processEvents()
        assert view._comms_test.isEnabled()
        assert view._comms_test_status.text().startswith("✗ IncompleteRead(8 bytes read")
    finally:
        view.deleteLater()


def test_opening_the_screen_marks_everything_seen_but_still_draws_it_new(store):
    store.comms_items = [_item(1, "2026-10-27T10:10:00Z")]
    store.comms_seen_at = "2026-10-27T10:00:00Z"
    view = CommsView(store)
    assert store.comms_unread == 1
    view.open()
    assert store.comms_unread == 0
    assert [p.unread for p in _rows(view, PingRow)] == [True]
    view.open()
    assert _rows(view, PingRow) == []
    _open_earlier(view)
    assert [p.unread for p in _rows(view, PingRow)] == [False]


def test_a_failing_tick_is_a_banner(store):
    store.comms_enabled = True
    store.comms_identity = {"handle": "operator", "teamID": "Ours"}
    store.comms_error = "Slack rate-limited the watcher"
    view = CommsView(store)
    assert not view.error_banner.isHidden()
    assert view.error_banner.text() == "⚠ Slack rate-limited the watcher"
    store.comms_error = None
    store.comms_changed.emit()
    assert view.error_banner.isHidden()


def test_off_says_so(store):
    assert CommsView(store).status.text() == "Off — turn Comms on in Settings to watch Slack."


def test_the_header_button_counts_unread_and_opening_clears_it(store):
    from diplomat_app.panel import Panel

    store.comms_items = [_item(2, "2026-10-27T10:20:00Z"), _item(1, "2026-10-27T10:10:00Z")]
    store.comms_seen_at = "2026-10-27T10:15:00Z"
    panel = Panel(store)
    try:
        assert panel.comms_btn.text() == "✉ 1"
        assert panel.comms_btn.toolTip() == "Comms — 1 unread"
        panel._toggle_comms()
        assert panel._screen == "comms"
        assert panel.comms_btn.text() == "✉"
        assert panel.comms_btn.toolTip() == "Comms"
        panel.comms_view.done.emit()
        assert panel._screen == "main"
    finally:
        panel.deleteLater()


@pytest.mark.skipif(not os.environ.get("DIPLOMAT_CORE_BIN"), reason="DIPLOMAT_CORE_BIN not set")
@pytest.mark.parametrize("mode", ["comms", "comms-panel", "comms-empty"])
def test_each_render_mode_draws_through_the_entry_point(tmp_path, mode):
    home = tmp_path / "home"
    home.mkdir()
    out = tmp_path / f"{mode}.png"
    env = {k: v for k, v in os.environ.items()
           if k not in (comms.TOKEN_ENV, "DIPLOMAT_COMMS_DIR", "DIPLOMAT_CONFIG")}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               QT_QPA_PLATFORM="offscreen", DIPLOMAT_SELF_REPO="/nonexistent",
               DIPLOMAT_RENDER=mode, DIPLOMAT_RENDER_OUT=str(out),
               PYTHONPATH=os.pathsep.join([str(_LINUX_DIR),
                                           str(_LINUX_DIR.parents[1] / "diplomat-runtime")]))
    done = subprocess.run([sys.executable, "-m", "diplomat_app"], cwd=str(_LINUX_DIR),
                          env=env, capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr
    assert out.stat().st_size > 10_000
    # Nothing the fixture wrote reached the private HOME's real Comms dir or config.
    assert not (home / ".diplomat" / "comms").exists()
    assert not (home / ".diplomat" / "config.json").exists()
