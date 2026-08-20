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

_HERE = os.path.dirname(os.path.abspath(__file__))
_LINUX_DIR = os.path.dirname(_HERE)
_PACKAGES = os.path.dirname(os.path.dirname(_LINUX_DIR))
sys.path.insert(0, _LINUX_DIR)
# The shared runtime, which the applet's own ``__init__`` would put here anyway —
# spelled out because plenty of these tests import ``diplomat_runtime`` without ever
# touching ``diplomat_app``.
sys.path.insert(0, os.path.join(_PACKAGES, "diplomat-runtime"))
# The mesh add-on, from this checkout rather than an install, so the suite runs
# against the SzpontNet in the tree it is testing. Normalised, because whichever
# of the two suites' conftests lands first decides the ``__file__`` every module
# in the package reports for the rest of the session.
sys.path.insert(0, os.path.join(_PACKAGES, "szpontnet-core"))

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(autouse=True)
def no_legacy_mesh_env(monkeypatch):
    """Clear the pre-rename ``DIPLOMAT_MESH_*`` names out of the environment.

    SzpontNet falls back to them when the ``SZPONTNET_*`` spelling is unset, which
    is right for a machine mid-migration and wrong for a test: a developer whose
    shell still exports ``DIPLOMAT_MESH_SECRET`` would have the mesh tests run
    against a fenced node while CI runs them against an open one.

    It clears the prefix wholesale, which includes Diplomat's own switches under it.
    The only one this suite uses is ``DIPLOMAT_MESH_E2E``, read at *collection* in a
    ``skipif`` — before any fixture runs — so the opt-in still works.
    """
    for key in [k for k in os.environ if k.startswith("DIPLOMAT_MESH_")]:
        monkeypatch.delenv(key)
    yield


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

    ``review.spawn`` (and ``szponthost._spawn_macos``, the macOS half of Diplomat's
    answer to "run a mesh job here") are the two paths that detach a terminal window
    running ``claude`` in :func:`review.repo_path` — the operator's own checkout,
    with their credentials. A test that reaches a
    dispatch path without stubbing them therefore turns a *stub* prompt into a live
    agent in a real repo, and the suite still passes green because the spawn is
    fire-and-forget. Tests that exercise dispatch stub the spawner themselves (see
    ``_spawn_recorder`` in test_autofix.py); this is the backstop for the ones that
    reach it by accident.

    The confined/override mesh runners (``SZPONTNET_SPAWN``,
    ``SZPONTNET_FOREIGN_SPAWN``) are deliberately left alone: they are empty by
    default and the mesh tests point them at a harmless ``cp`` template.
    """
    from diplomat_runtime import review, szponthost

    def refuse(*args, **kwargs):
        raise AssertionError(
            "a test reached a real agent launch — stub the spawner "
            "(see _spawn_recorder in tests/test_autofix.py)"
        )

    monkeypatch.setattr(review, "spawn", refuse)
    monkeypatch.setattr(szponthost, "_spawn_macos", refuse)
    yield


@pytest.fixture(autouse=True)
def no_github_reads(monkeypatch):
    """Fail loudly instead of shelling out to the real ``gh``.

    Every GitHub read in the applet funnels through :func:`gh.run`, and a test that
    reaches it is a test whose answer depends on the operator's own PRs, their auth,
    and the network — green here, red on a runner with no ``GH_TOKEN``, and slow and
    rate-limit-consuming in both. It is easy to reach by accident: one poll drives
    BOTH monitors, so stubbing one monitor's fetch leaves the other's live.

    Tests that mean to exercise a fetch stub ``autofixmonitor.fetch_*`` (or
    ``models.API``) themselves; this is the backstop for the ones that don't.
    """
    from diplomat_runtime import gh

    def refuse(args, timeout=60.0):
        raise AssertionError(
            f"a test reached the real `gh` ({' '.join(args[:2])}…) — stub the fetch "
            "it goes through (autofixmonitor.fetch_snapshots / fetch_review_requests "
            "/ fetch_closed_prs)"
        )

    monkeypatch.setattr(gh, "run", refuse)
    yield


@pytest.fixture(autouse=True)
def nothing_closed(monkeypatch):
    """Answer the poll's closed-PR read with "nothing has closed", and yield the real
    function for the one test that is about the read itself.

    The third fetch of a cycle, and the only one whose neutral answer is not the one a
    missing stub gives: unreachable, it reads as a poll failure and stands the whole
    drain down (:meth:`Store._fetch_closed_prs`), which would silently take the queue
    out of every test that is not about closure. An empty set is what those were
    written against; a test about a PR leaving the open state names the numbers itself.
    """
    from diplomat_app import autofixmonitor

    real = autofixmonitor.fetch_closed_prs
    monkeypatch.setattr(autofixmonitor, "fetch_closed_prs", lambda *a, **k: set())
    yield real


@pytest.fixture(autouse=True)
def isolated_activity_feed(tmp_path, monkeypatch):
    """Redirect the shared ``~/.diplomat/pr-monitor`` activity feed to a per-test temp
    dir so tests never scribble on the user's real audit.jsonl. The monitor + API-error
    watcher dispatch paths call :func:`activity.log`, which otherwise appends to the
    user's live feed (``activity._dir`` resolves via ``Path.home()``, which the
    QSettings redirect above does not cover)."""
    from diplomat_runtime import activity

    feed = tmp_path / "diplomat-feed"
    feed.mkdir()
    monkeypatch.setattr(activity, "_dir", lambda: feed)
    yield


@pytest.fixture(autouse=True)
def isolated_claude_dir(tmp_path, monkeypatch):
    """Fence the telemetry gatherers off from the developer's own Claude Code state.

    :mod:`usagescan` walks ``~/.claude/projects`` — every prompt and every reply the
    operator has ever sent — and :mod:`quota` reads the OAuth token beside it and
    spends it on a live request to Anthropic. Neither belongs in a test: the scan
    would be slow and machine-dependent, and the probe would make the suite need the
    network and a logged-in Claude Code.

    Both honour ``DIPLOMAT_CLAUDE_DIR``; the probe additionally has an off switch, so
    a test that reaches a sample path gets ``(None, None)`` instead of a socket. With
    no reading, the auto-work budget has no opinion (:func:`autofix.budget_decide`),
    so a test that reaches a dispatch path is gated by the task cap alone unless it
    stubs the probe itself.

    The four caches are cleared around each test because all four are module state a
    per-test temp file can collide on: the fold is keyed on the ledger's mtime and
    size, two are keyed on a clock, and the last remembers a path to an ``opencode``
    that the next test replaces with its own.
    """
    from diplomat_runtime import autobudget, quota, telemetry, usagescan

    monkeypatch.setenv("DIPLOMAT_CLAUDE_DIR", str(tmp_path / "claude"))
    monkeypatch.setenv("DIPLOMAT_QUOTA_PROBE", "0")
    quota._reset_cache()
    telemetry._reset_cache()
    autobudget._reset_cache()
    usagescan._reset_cache()
    yield
    quota._reset_cache()
    telemetry._reset_cache()
    autobudget._reset_cache()
    usagescan._reset_cache()


@pytest.fixture(autouse=True)
def isolated_hermes_state(tmp_path, monkeypatch):
    """Fence the Hermes readers off from the developer's own ``~/.hermes``.

    Same reasoning as the Claude Code redirect above, for all three files Diplomat
    reads there: ``state.db`` is every session the operator has ever run, so a test
    reaching :mod:`hermesstore` would match a run against their real work,
    ``config.yaml`` holds the model their picker is on, which the attribution tag
    names, and ``.env`` holds the OpenRouter key :mod:`spend` prices an account
    through — which a test must neither read nor spend a live request on. All three
    point at files that do not exist, which is the "nothing to read" case each reader
    already degrades to; a test that wants one writes it there.

    The spend probe additionally has an off switch, like the quota probe: with no
    reading the money budget has no opinion, so a test reaching a dispatch path is
    gated by the task cap alone unless it stubs the probe itself."""
    monkeypatch.setenv("DIPLOMAT_HERMES_DB", str(tmp_path / "hermes" / "state.db"))
    monkeypatch.setenv("DIPLOMAT_HERMES_CONFIG", str(tmp_path / "hermes" / "config.yaml"))
    monkeypatch.setenv("DIPLOMAT_HERMES_ENV", str(tmp_path / "hermes" / ".env"))
    monkeypatch.setenv("DIPLOMAT_SPEND_PROBE", "0")
    # …and out of the environment, which `spend.api_key` falls back to and a
    # developer running the suite in their working shell may well have exported.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    from diplomat_runtime import spend

    spend._reset_cache()
    yield
    spend._reset_cache()


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
def isolated_agent_registry(tmp_path, monkeypatch):
    """Redirect the run registry (``~/.diplomat/agents``) to a per-test temp dir.

    Same reasoning as the fixtures above, with sharper teeth: :func:`agentregistry.forget`
    deletes run directories, so a test running against the real one would delete the
    operator's live agents' prompts and pid files. Uses the ``DIPLOMAT_AGENTS_DIR`` hook
    so the redirect reaches a spawned mesh node too, which reads the same book to answer
    whether this machine has room."""
    monkeypatch.setenv("DIPLOMAT_AGENTS_DIR", str(tmp_path / "agents"))
    yield


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    """Drop the shared ``ps`` cache between tests — it is module state, and a test that
    stubs the process table would otherwise be answered from the previous test's real
    one (or vice versa)."""
    from diplomat_app import probes
    probes.reset_cache()
    yield
    probes.reset_cache()


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
