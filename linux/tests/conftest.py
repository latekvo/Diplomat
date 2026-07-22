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

The host's agent launchers are fenced off for the same reason: a test that
reaches a dispatch path is running on the operator's own machine, where a spawn
opens a real terminal and turns a stub prompt loose in their checkout.
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
def no_host_agent_spawn(monkeypatch):
    """Fail loudly instead of launching a real agent on the machine running the tests.

    ``review.spawn`` (and ``spawnjob._spawn_macos``, its macOS mesh twin) are the two
    paths that detach a terminal window running ``claude`` in :func:`review.repo_path`
    — the operator's own checkout, with their credentials. A test that reaches a
    dispatch path without stubbing them therefore turns a *stub* prompt into a live
    agent in a real repo, and the suite still passes green because the spawn is
    fire-and-forget. Tests that exercise dispatch stub the spawner themselves (see
    ``_spawn_recorder`` in test_autofix.py); this is the backstop for the ones that
    reach it by accident.

    The confined/override mesh runners (``DIPLOMAT_MESH_SPAWN``,
    ``DIPLOMAT_MESH_FOREIGN_SPAWN``) are deliberately left alone: they are empty by
    default and the mesh tests point them at a harmless ``cp`` template.
    """
    from diplomat_app import review
    from diplomat_app.mesh import spawnjob

    def refuse(*args, **kwargs):
        raise AssertionError(
            "a test reached a real agent launch — stub the spawner "
            "(see _spawn_recorder in tests/test_autofix.py)"
        )

    monkeypatch.setattr(review, "spawn", refuse)
    monkeypatch.setattr(spawnjob, "_spawn_macos", refuse)
    yield


@pytest.fixture(autouse=True)
def isolated_activity_feed(tmp_path, monkeypatch):
    """Redirect the shared ``~/.diplomat/pr-monitor`` activity feed to a per-test temp
    dir so tests never scribble on the user's real audit.jsonl. The monitor + API-error
    watcher dispatch paths call :func:`activity.log`, which otherwise appends to the
    user's live feed (``activity._dir`` resolves via ``Path.home()``, which the
    QSettings redirect above does not cover)."""
    from diplomat_app import activity

    feed = tmp_path / "diplomat-feed"
    feed.mkdir()
    monkeypatch.setattr(activity, "_dir", lambda: feed)
    yield


@pytest.fixture(autouse=True)
def isolated_app_config(tmp_path, monkeypatch):
    """Redirect the shared ``~/.diplomat/config.json`` to a per-test temp file, for the
    same reason as the two fixtures above: it holds the repo root every spawn `cd`s
    into, so a test that writes it would retarget the operator's real agents. Uses the
    documented ``DIPLOMAT_CONFIG`` hook, so the redirect also reaches any child process
    a test starts (a mesh node reads the same file)."""
    monkeypatch.setenv("DIPLOMAT_CONFIG", str(tmp_path / "diplomat-config.json"))
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
