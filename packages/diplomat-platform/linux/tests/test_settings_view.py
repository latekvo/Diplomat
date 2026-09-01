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
    return [row._detail for row in view._rows if row._detail.text()]


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
