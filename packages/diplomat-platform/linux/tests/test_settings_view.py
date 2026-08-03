"""The Settings controls that are more than a checkbox bound to a property.

A toggle that stops writing through is loud — the box springs back. A *number*
that stops writing through is silent: the stepper shows what you typed, the
setting keeps its old value, and the only symptom is behaviour you can't explain.
So the automatic-task cap's two directions are pinned here — the store's value
reaching the stepper, and the stepper's reaching the store.
"""

from __future__ import annotations

import pytest

from diplomat_app import autofix
from diplomat_app.settingsview import SettingsView
from diplomat_app.store import Store

pytest.importorskip("PySide6")


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def view(app, monkeypatch):
    # Opening the real screen otherwise shells the allocator installer and starts
    # an update check; neither belongs in a test about one stepper.
    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    store = Store()
    store.auto_task_limit = 3
    v = SettingsView(store)
    yield v
    v.deleteLater()


def test_the_stepper_opens_on_the_stored_cap(view):
    assert view._auto_limit.value() == 3


def test_moving_the_stepper_writes_the_cap_through(view):
    view._auto_limit.setValue(5)
    assert view.store.auto_task_limit == 5


def test_the_stepper_cannot_offer_a_cap_the_gate_would_clamp(view):
    """Range mismatch would let the UI show a number the store silently changes."""
    assert view._auto_limit.minimum() == autofix.MIN_AUTO_TASK_LIMIT
    assert view._auto_limit.maximum() == autofix.MAX_AUTO_TASK_LIMIT
    view._auto_limit.setValue(999)
    assert view._auto_limit.value() == autofix.MAX_AUTO_TASK_LIMIT
    assert view.store.auto_task_limit == autofix.MAX_AUTO_TASK_LIMIT
