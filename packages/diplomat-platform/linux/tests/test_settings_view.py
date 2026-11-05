"""The Settings controls that are more than a switch bound to a property.

A switch that stops writing through is loud — the knob springs back. A *number*
that stops writing through is silent: the slider sits where you dragged it, the
setting keeps its old value, and the only symptom is behaviour you can't explain.
So the automatic-task cap's two directions are pinned here — the store's value
reaching the slider, and the slider's reaching the store.

The *Explain* switch gets the same treatment for the same reason: it decides
whether a row's paragraph is on screen at all, and a row built after the switch
was last read would otherwise open in the wrong state with nothing to show for it.
"""

from __future__ import annotations

import pytest

from diplomat_runtime import autofix
from diplomat_app.settingsview import SettingsView
from diplomat_app.store import Store

pytest.importorskip("PySide6")


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def make_view(app, monkeypatch):
    # Opening the real screen otherwise shells the allocator installer and starts
    # an update check; neither belongs in a test about one slider.
    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    built: list[SettingsView] = []

    def build(**settings) -> SettingsView:
        store = Store()
        store.auto_task_limit = 3
        for key, value in settings.items():
            setattr(store, key, value)
        view = SettingsView(store)
        built.append(view)
        return view

    yield build
    for view in built:
        view.deleteLater()


@pytest.fixture
def view(make_view):
    return make_view()


def test_the_slider_opens_on_the_stored_cap(view):
    assert view._auto_limit.value() == 3


def test_moving_the_slider_writes_the_cap_through(view):
    view._auto_limit.set_value(5)
    view._auto_limit.changed.emit(5)
    assert view.store.auto_task_limit == 5


def test_the_slider_cannot_offer_a_cap_the_gate_would_clamp(view):
    """Range mismatch would let the UI show a number the store silently changes."""
    assert view._auto_limit.minimum() == autofix.MIN_AUTO_TASK_LIMIT
    assert view._auto_limit.maximum() == autofix.MAX_AUTO_TASK_LIMIT
    view._auto_limit.set_value(999)
    assert view._auto_limit.value() == autofix.MAX_AUTO_TASK_LIMIT


def test_the_cap_reads_back_on_the_card(view):
    view._auto_limit._slider.setValue(4)
    assert "4" in view._limits_pill.text()


def _details(view) -> list:
    """The paragraphs of the rows on screen; a row its switch hides shows nothing."""
    return [row._detail for row in view._rows
            if row._detail.text() and row.isVisibleTo(view)]


def test_a_screen_opened_with_explain_off_shows_no_paragraph(make_view):
    view = make_view(settings_explain=False)
    assert _details(view), "no row carries a paragraph — the test proves nothing"
    assert not any(d.isVisibleTo(view) for d in _details(view))


def test_a_screen_opened_with_explain_on_shows_them(make_view):
    view = make_view(settings_explain=True)
    assert all(d.isVisibleTo(view) for d in _details(view))


def test_the_budget_floor_reads_as_a_percentage_to_one_place(view):
    """The same rendering the panel's own budget readouts and the macOS twin use.
    A bare integer here would report a different number than the Telemetry screen
    for the same setting."""
    view._budget_floor.set_value(20)
    assert view._budget_floor.badge() == "20.0%"


def test_the_budget_reserve_reads_as_money_and_persists(view):
    """The other currency's knob, in the spelling the feed quotes it back in. It
    writes to the SHARED config file rather than QSettings, because the mesh node
    that spends this account on peer-routed work is a separate process reading the
    same reserve."""
    from diplomat_runtime import appconfig

    # Driven through the slider itself rather than `set_value`, which is the store
    # writing to the UI and deliberately silent — this is the other direction.
    view._budget_reserve._slider.setValue(5)

    assert view._budget_reserve.badge() == "$5.00"
    assert appconfig.auto_budget_reserve_usd() == 5.0


def test_the_run_deadline_switch_opens_on_the_stored_value_and_writes_through(view):
    """The knob that decides whether a four-hour run is given up on. It writes to the
    SHARED config file rather than QSettings, beside the cap whose bays it hands back —
    and the row is titled from the resolver's own constant, so the number the operator
    reads is the number that fires."""
    from diplomat_runtime import agentstate, apiwatch, appconfig

    assert view._sw_deadline.isChecked() is True  # on unless it was turned off

    view._sw_deadline.setChecked(False)
    assert appconfig.run_deadline() is None
    view._sw_deadline.setChecked(True)
    assert appconfig.run_deadline() == agentstate.RUN_DEADLINE

    cutoff = apiwatch.human_interval(agentstate.RUN_DEADLINE)
    assert cutoff in view._sw_deadline.accessibleName()


def test_the_run_deadline_switch_opens_off_when_it_was_turned_off(make_view):
    """The other half of "opens on the stored value", and the half a screen built at
    the default cannot show: `setChecked(True)` passes the test above, because ON is
    what the config says when nothing turned it off. An operator who switched a
    destructive default-on backstop off has to see it off when they come back — the
    twin `test_the_slider_opens_on_the_stored_cap` builds at a non-default for exactly
    this reason."""
    from diplomat_runtime import appconfig

    appconfig.set_bool(appconfig.RUN_DEADLINE, False)
    assert make_view()._sw_deadline.isChecked() is False


def test_the_stalled_agents_pill_answers_for_both_of_its_switches(make_view):
    """One pill over two switches. With the watcher off and the deadline on the card
    still retires runs and closes their windows, so a pill reading "off" sends an
    operator looking for the cause anywhere but here. The sibling AUTOMATIC WORK card
    is the shape to match: it greys only when every switch under it is off."""
    from diplomat_runtime import appconfig

    appconfig.set_bool(appconfig.RUN_DEADLINE, True)
    view = make_view()
    view.store.api_watch_enabled = False

    view._refresh_apiwatch_ui()
    assert "deadline" in view._apiwatch_pill.text()

    appconfig.set_bool(appconfig.RUN_DEADLINE, False)
    view._refresh_apiwatch_ui()
    assert "deadline" not in view._apiwatch_pill.text()


def test_the_author_allowlist_opens_on_its_stored_line(make_view):
    view = make_view(review_allowlist_raw="carol")
    assert view._allowlist_setting_row._control.text() == "carol"


def test_typing_an_author_allowlist_writes_through(view):
    """A text field that stops writing through is silent in the worst way here: the
    line sits where it was typed and the monitor goes on reviewing everyone."""
    row = view._allowlist_setting_row
    assert row._control.text() == ""

    row._control.setText("@Alice, bob")
    assert view.store.review_allowlist_raw == "@Alice, bob"
    assert view.store.review_allowlist == ["Alice", "bob"]
    assert row.summary() == ""

    row._control.setText("")
    assert view.store.review_allowlist == []


def test_the_author_allowlist_stays_editable_while_the_monitor_is_off(make_view):
    """A switched-off monitor still polls and still lists what it finds, and the list
    is what decides which requests those are — so it is the one row in that nest whose
    effect does not end with the switch. Hidden with the rest, it would go on filtering
    rows the operator has no way to reach. The verdict rows do end with it: nothing
    they govern happens until a review runs."""
    view = make_view(review_requests_enabled=False)
    assert view._allowlist_setting_row.isVisibleTo(view)
    assert not view._verdict_rows.isVisibleTo(view)

    view._sw_review_req.setChecked(True)
    assert view._allowlist_setting_row.isVisibleTo(view)
    assert view._verdict_rows.isVisibleTo(view)


def test_every_row_names_its_control_for_a_screen_reader(view):
    """The row's name is a separate label, so an unnamed control reads as a bare
    switch. Every row is checked because the naming is one line in `SettingRow`:
    a control that arrives already named keeps its own, and nothing else can."""
    unnamed = [r for r in view._rows if not r._control.accessibleName()]
    assert not unnamed


def test_the_explain_switch_writes_through_and_reveals(view):
    view._explain.setChecked(True)
    assert view.store.settings_explain is True
    assert all(d.isVisibleTo(view) for d in _details(view))
    view._explain.setChecked(False)
    assert view.store.settings_explain is False
    assert not any(d.isVisibleTo(view) for d in _details(view))


def test_the_runner_row_names_the_opencode_a_spawn_would_run(make_view, monkeypatch):
    """Resolved through the user's shell, as the spawn resolves it: an install only an
    rc or profile puts on PATH is not on this app's, and is still what runs."""
    from diplomat_app import settingsview
    from diplomat_runtime import runner, usagescan

    monkeypatch.setattr(usagescan, "opencode_binary", lambda: "/rc-only/opencode")
    monkeypatch.setattr(settingsview.shutil, "which", lambda name: None)
    view = make_view(agent_runner=runner.OPENCODE)
    assert view._runner_row.summary() == ""
    assert view._runner_row._detail.text().startswith(
        "Spawns run `opencode` (/rc-only/opencode). ")


def test_a_missing_opencode_is_not_promised_to_an_rc(make_view, monkeypatch):
    """The lookup already asked the login and interactive shell, so a miss is not one
    an rc-only install could still fill."""
    from diplomat_app import settingsview
    from diplomat_runtime import runner, usagescan

    monkeypatch.setattr(usagescan, "opencode_binary", lambda: None)
    monkeypatch.setattr(settingsview.shutil, "which", lambda name: None)
    view = make_view(agent_runner=runner.OPENCODE)
    assert view._runner_row._summary.text() == (
        "Neither your login shell nor this app's PATH has an `opencode` executable — "
        "check it if spawned runs finish instantly without doing anything.")


def test_the_allocator_card_leaves_the_idle_limits_to_the_devices_screen(view):
    from diplomat_app.widgets import SliderSetting

    assert view._alloc_row.parentWidget().findChildren(SliderSetting) == []


def _other_repo_rows(view) -> list[list[str]]:
    from PySide6.QtWidgets import QLabel

    out = []
    for i in range(view._other_repos_list.count()):
        row = view._other_repos_list.itemAt(i).widget()
        out.append([w.toolTip() or w.text() for w in row.findChildren(QLabel)])
    return out


def test_other_repos_are_added_named_by_their_remote_and_removed(view, tmp_path, monkeypatch):
    import subprocess

    from PySide6.QtWidgets import QFileDialog, QPushButton

    radon = tmp_path / "radon"
    radon.mkdir()
    subprocess.run(["git", "-C", str(radon), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(radon), "remote", "add", "origin",
                    "git@github.com:software-mansion/radon.git"], check=True)
    plain = tmp_path / "plain"
    plain.mkdir()
    assert _other_repo_rows(view) == [] and view._other_repos_pill.text() == ""

    for picked in (radon, plain, radon):  # the second pick of radon is a no-op
        monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                            staticmethod(lambda *a, p=picked: str(p)))
        view._add_other_repo()
    assert view.store.other_repo_paths == [str(radon), str(plain)]
    assert _other_repo_rows(view) == [["software-mansion/radon", str(radon)],
                                      ["⚠ No GitHub remote", str(plain)]]
    assert view._other_repos_pill.text() == "check this"
    assert [r.repo for r in view.store.local_repos] == ["radon"]

    plain_row = view._other_repos_list.itemAt(1).widget()
    plain_row.findChild(QPushButton).click()
    assert view.store.other_repo_paths == [str(radon)]
    assert view._other_repos_pill.text() == "1 repo"


def test_other_repos_persist_and_resolve_again_at_start(make_view, tmp_path):
    view = make_view(other_repo_paths=[str(tmp_path / "gone")])
    assert _other_repo_rows(view) == [["⚠ Folder not found", str(tmp_path / "gone")]]
    from diplomat_app.store import Store

    assert Store().other_repo_paths == [str(tmp_path / "gone")]


def _row_of(control):
    from diplomat_app.widgets import SettingRow

    row = control.parent()
    while not isinstance(row, SettingRow):
        row = row.parent()
    return row


def test_the_reply_switch_sits_with_the_comms_switches_and_writes_through(make_view):
    from diplomat_app.store import app_settings

    view = make_view()
    sw = view._sw_comms_replies
    assert sw.isChecked() is True
    assert sw.accessibleName() == "Reply in Slack"
    assert _row_of(sw).parent() is _row_of(view._sw_comms_bumps).parent()
    assert "While you are away" in _row_of(view._sw_comms_reply_questions)._detail.text()
    sw.setChecked(False)
    assert app_settings().value("commsReplies") is False
    assert view.store.comms_toggles()["replies"] is False
    assert make_view()._sw_comms_replies.isChecked() is False


def test_only_questions_get_a_reply_until_the_operator_says_otherwise(make_view):
    from diplomat_app.store import app_settings

    view = make_view()
    assert view._sw_comms_reply_questions.isChecked() is True
    switches = {"replyToReviews": view._sw_comms_reply_reviews,
                "replyToFixes": view._sw_comms_reply_fixes,
                "replyToInvestigations": view._sw_comms_reply_investigations}
    toggles = view.store.comms_toggles()
    assert toggles["replyToQuestions"] is True
    for key, sw in switches.items():
        assert sw.isChecked() is False and toggles[key] is False
        assert _row_of(sw).parent() is _row_of(view._sw_comms_replies).parent()
    view._sw_comms_reply_fixes.setChecked(True)
    view._sw_comms_reply_questions.setChecked(False)
    assert app_settings().value("commsReplyToFixes") is True
    toggles = view.store.comms_toggles()
    assert (toggles["replyToFixes"], toggles["replyToQuestions"],
            toggles["replyToReviews"]) == (True, False, False)
    again = make_view()
    assert again._sw_comms_reply_fixes.isChecked() is True
    assert again._sw_comms_reply_questions.isChecked() is False


def test_the_batch_size_slider_stops_at_powers_of_two_and_writes_through(make_view):
    from diplomat_app.store import app_settings

    view = make_view()
    slider = view._comms_batch
    assert _row_of(slider).parent() is _row_of(view._sw_comms_replies).parent()
    assert slider._slider.accessibleName() == "Batch size"
    assert (slider.minimum(), slider.maximum(), slider.badge()) == (0, 6, "8")
    seen = []
    for at in range(7):
        slider._slider.setValue(at)
        seen.append((slider.badge(), view.store.comms_batch_size))
    assert seen == [(str(n), n) for n in (1, 2, 4, 8, 16, 32, 64)]
    assert app_settings().value("commsBatchSize") == 64
    assert view.store.comms_settings_payload()["batchSize"] == 64
    assert make_view()._comms_batch.badge() == "64"


def test_the_batch_interval_slider_is_logarithmic_and_writes_through(make_view):
    view = make_view()
    slider = view._comms_interval
    assert slider._slider.accessibleName() == "Batch interval"
    assert slider.badge() == "30m"
    slider._slider.setValue(slider.minimum())
    assert (slider.badge(), view.store.comms_flush_after_secs) == ("1m", 60)
    slider._slider.setValue(slider.maximum())
    assert (slider.badge(), view.store.comms_flush_after_secs) == ("1d", 86400)
    assert view.store.comms_settings_payload()["flushAfterSecs"] == 86400
    # Half the slider's travel is around the geometric middle of a minute and a day
    # (38m), where a linear one would read 12h.
    slider._slider.setValue(slider.maximum() // 2)
    assert 1200 <= view.store.comms_flush_after_secs <= 3600
    assert make_view()._comms_interval.value() == slider.maximum() // 2


def test_a_hand_written_setting_finds_the_nearest_stop_and_stays_until_moved(make_view):
    from diplomat_app.store import app_settings

    app_settings().setValue("commsBatchSize", 5)
    view = make_view()
    assert view._comms_batch.badge() == "4"
    assert view.store.comms_batch_size == 5


def test_the_away_slider_steps_in_quarter_hours_and_writes_through(make_view):
    from diplomat_app.store import app_settings

    view = make_view()
    slider = view._comms_away
    assert _row_of(slider).parent() is _row_of(view._sw_comms_replies).parent()
    assert slider._slider.accessibleName() == "Away after"
    assert (slider.minimum(), slider.maximum(), slider.value()) == (900, 43200, 10800)
    assert slider.badge() == "3h"
    slider._slider.setValue(4400)
    assert slider.value() == 4500 and slider.badge() == "1h 15m"
    assert app_settings().value("commsAwayAfterSecs") == 4500
    assert view.store.comms_away_after_secs == 4500
    assert make_view()._comms_away.value() == 4500


def test_the_keywords_hint_names_the_wildcard(view):
    assert "a * ending a word" in _row_of(view._comms_keywords)._detail.text()


def test_the_keywords_field_shows_the_format_while_empty(view):
    assert view._comms_keywords.placeholderText() == "deploy, release train, argent*"


def test_a_healthy_screen_draws_no_line_under_any_row(make_view, monkeypatch, tmp_path):
    """The line under a row is for live state: a problem, a test result, an update in
    progress. With none of those, every row is its title and its control, and what a
    setting does waits behind Explain."""
    from diplomat_app import deviceallocator, settingsview, szpont
    from diplomat_runtime import comms, review

    (tmp_path / ".git").mkdir()
    monkeypatch.delenv("DIPLOMAT_REPO", raising=False)
    monkeypatch.delenv(comms.TOKEN_ENV, raising=False)
    monkeypatch.setattr(review, "repo_path", lambda: str(tmp_path))
    monkeypatch.setattr(settingsview.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(deviceallocator, "package_available", lambda: True)
    monkeypatch.setattr(deviceallocator, "resolve_node", lambda: "/usr/bin/node")
    monkeypatch.setattr(szpont, "AVAILABLE", True)
    view = make_view(update_state={"phase": "idle", "commit": "abc1234", "branch": "main",
                                   "upstream": "origin/main", "ahead": 0, "behind": 0})
    assert [(r._control.accessibleName(), r.summary()) for r in view._rows
            if r.summary()] == []
    assert str(tmp_path) in view._repo_row._detail.text()
    assert "Devices screen" in view._alloc_row._detail.text()


@pytest.mark.parametrize("ahead,behind,line", [
    (0, 0, ""),
    (0, 2, "abc1234 on main · upstream origin/main"),
    (1, 0, "abc1234 on main · upstream origin/main · 1 local ahead"),
])
def test_the_update_line_is_drawn_only_when_there_is_something_to_pull_or_keep(
        view, ahead, behind, line):
    view.store.update_state = {"phase": "idle", "commit": "abc1234", "branch": "main",
                               "upstream": "origin/main", "ahead": ahead,
                               "behind": behind}
    view._refresh_update_ui()
    assert view._update_row.summary() == line
    assert view._update_row._summary.isVisibleTo(view) is bool(line)


def test_the_slack_options_are_drawn_only_while_slack_is_watched(make_view, monkeypatch):
    """Like the verdict rows: nothing under the switch acts until the watcher runs."""
    monkeypatch.setattr(Store, "run_comms_poll_async", lambda self: None)
    view = make_view(comms_enabled=False)
    nested = [view._comms_keywords, view._comms_allow, view._sw_comms_requests,
              view._sw_comms_notify, view._comms_model, view._comms_test]
    assert view._comms_row.isVisibleTo(view)
    assert not any(w.isVisibleTo(view) for w in nested)

    view._sw_comms.setChecked(True)
    assert all(w.isVisibleTo(view) for w in nested)
    view._sw_comms.setChecked(False)
    assert not any(w.isVisibleTo(view) for w in nested)


@pytest.fixture
def panel(app, monkeypatch):
    from diplomat_app.panel import Panel

    monkeypatch.delenv("DIPLOMAT_RENDER", raising=False)
    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_auto_task_count_async", lambda self: None)
    p = Panel(Store())
    p.show()
    yield p
    p.hide()
    p.deleteLater()


def test_closing_the_panel_on_settings_reopens_it_on_the_main_screen(panel):
    panel._set_screen("settings")
    panel.close()
    assert panel._screen == "main"
    assert panel.body.currentIndex() == panel._screens["main"]
    panel.show()
    assert panel._screen == "main"


def test_closing_the_panel_on_another_screen_keeps_it(panel):
    panel._set_screen("devices")
    panel.close()
    assert panel._screen == "devices"


def test_minimizing_the_panel_on_settings_keeps_it(panel):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    panel._set_screen("settings")
    panel.setWindowState(Qt.WindowState.WindowMinimized)
    for _ in range(3):
        QApplication.processEvents()
    assert panel.isMinimized()
    assert panel._screen == "settings"
