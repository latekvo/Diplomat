"""Shared pytest setup: import path + QSettings and Qt-lifetime isolation.

Every test that builds a Store must never touch the user's real QSettings
(a user who e.g. hid tools would otherwise change test outcomes — and tests
would scribble on their live config). Redirect all QSettings to a per-test
temp dir before anything constructs one.

Qt object lifetime is isolated the same way: a test that builds a widget with
a running QTimer (the panel/mesh views do) must not let that timer outlive the
test. All tests share one process-wide QApplication, so a leaked QTimer fires
into freed memory during a *later* test's ``processEvents`` — a segfault whose
victim depends on ordering. Draining leftover widgets after each test keeps
that from leaking across the boundary.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_qsettings(tmp_path):
    """Point QSettings at a fresh temp dir for the duration of each test."""
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(
        QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(tmp_path)
    )
    yield


@pytest.fixture(autouse=True)
def _drain_qt_widgets():
    """After each test, delete any leftover top-level widgets and drain the
    event loop, so no QTimer/QObject survives into the next test's event loop.

    No-op for the many tests that never build a QApplication.
    """
    yield
    app = QApplication.instance()
    if app is None:
        return
    for widget in app.topLevelWidgets():
        widget.deleteLater()
    # Bounded spins to let deleteLater + timer teardown actually run.
    for _ in range(3):
        app.processEvents()
