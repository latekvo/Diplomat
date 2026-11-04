"""The per-test Qt drain in ``conftest.py``: what it is given to delete is gone."""

from __future__ import annotations

import shiboken6
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget

from conftest import drain_qt_widgets


def test_the_drain_deletes_every_window_and_stops_its_timers():
    app = QApplication.instance() or QApplication([])
    windows = [QWidget() for _ in range(5)]
    timers = []
    for window in windows:
        timer = QTimer(window)
        timer.start(10_000)
        timers.append(timer)

    drain_qt_widgets(app)

    assert not any(shiboken6.isValid(o) for o in windows + timers)
