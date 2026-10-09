"""A variable that asks for a one-shot mode this build does not run is refused at the
entry point.

Without the refusal it falls through the ladder in `__main__.main` and launches the
tray: newest-wins terminates the operator's running applet and the stray acts on the
real state (issue #149, where it was the macOS twin).
"""

from __future__ import annotations

import importlib
import os
import re

import pytest

from diplomat_app import headless

_HERE = os.path.dirname(os.path.abspath(__file__))
_PACKAGES = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))


@pytest.mark.parametrize(
    "env, refused",
    [
        # A macOS mode, which this applet does not run. The first is the 2026-09-09
        # trigger.
        ({"DIPLOMAT_REPOPATHS_TEST": "1"}, ["DIPLOMAT_REPOPATHS_TEST=1"]),
        ({"DIPLOMAT_SETTINGS_DUMP": "1"}, ["DIPLOMAT_SETTINGS_DUMP=1"]),
        ({"DIPLOMAT_APIWATCH_SCAN": "1"}, ["DIPLOMAT_APIWATCH_SCAN=1"]),
        ({"DIPLOMAT_AUTOFIX_POLL": "1"}, ["DIPLOMAT_AUTOFIX_POLL=1"]),
        # A known mode, with a value that does not turn it on.
        ({"DIPLOMAT_DUMP": "true"}, ["DIPLOMAT_DUMP=true"]),
        ({"DIPLOMAT_SELF_UPDATE": "0"}, ["DIPLOMAT_SELF_UPDATE=0"]),
        ({"DIPLOMAT_AGENTS": "true"}, ["DIPLOMAT_AGENTS=true"]),
        ({"DIPLOMAT_LOOKUP": ""}, ["DIPLOMAT_LOOKUP="]),
        # Every offender is named, not just the first.
        ({"DIPLOMAT_B_TEST": "1", "DIPLOMAT_A_TEST": "1", "DIPLOMAT_DUMP": "1"},
         ["DIPLOMAT_A_TEST=1", "DIPLOMAT_B_TEST=1"]),
    ],
)
def test_a_mode_this_build_does_not_run_is_refused(env, refused):
    assert headless.unrunnable(env) == refused


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"DIPLOMAT_DUMP": "1"},
        {"DIPLOMAT_AGENTS": "1"},
        {"DIPLOMAT_LOOKUP": "337"},
        {"DIPLOMAT_PRINT_PROMPT": "mine"},
        {"DIPLOMAT_RENDER": "panel", "DIPLOMAT_RENDER_OUT": "/tmp/x.png"},
        # The legacy 6AM unit runs the `argent-utils` shim, which adds the current
        # marker and leaves its own in place.
        {"ARGENT_UTILS_SELF_UPDATE": "1", "DIPLOMAT_SELF_UPDATE": "1"},
        # Shaped like a mode, but not Diplomat's.
        {"PYTEST_CURRENT_TEST": "tests/test_x.py::test_y (call)"},
        # Settings, not modes.
        {"DIPLOMAT_AGENTS_DIR": "/x", "DIPLOMAT_MESH_E2E": "1", "DIPLOMAT_CORE_BIN": "/x",
         "DIPLOMAT_QUOTA_PROBE": "0", "DIPLOMAT_MESH_POLL_SECS": "5"},
    ],
)
def test_a_run_this_build_can_do_is_not_refused(env):
    assert headless.unrunnable(env) == []


def _names_read_by_the_apps() -> set[str]:
    """Every `DIPLOMAT_*` name spelled in either app's sources or the libraries
    below them."""
    roots = [
        "diplomat-platform/macos/Sources",
        "diplomat-platform/linux/diplomat_app",
        "diplomat-core/Sources",
        "diplomat-runtime/diplomat_runtime",
        "szpontnet-core/szpontnet",
    ]
    names: set[str] = set()
    for root in roots:
        for dirpath, _, files in os.walk(os.path.join(_PACKAGES, root)):
            for f in files:
                if f.endswith((".swift", ".py")):
                    with open(os.path.join(dirpath, f), encoding="utf-8") as src:
                        names |= set(re.findall(r"\bDIPLOMAT_[A-Z0-9_]*[A-Z0-9]\b",
                                                src.read()))
    return names


def test_no_setting_either_app_reads_looks_like_a_mode():
    """The refusal is keyed on how a name looks, so a setting shaped like a mode would
    stop the app starting wherever it is exported."""
    names = _names_read_by_the_apps()
    assert {"DIPLOMAT_AUDIT_DIR", "DIPLOMAT_QUEUE_TEST", "DIPLOMAT_AGENTS_DIR"} <= names
    from test_headless_modes import _dispatched

    modes = set(headless.MODES) | _dispatched()
    shaped = sorted(n for n in names - modes if headless.looks_like_mode(n))
    assert not shaped, shaped


@pytest.fixture
def entry(monkeypatch):
    """`__main__.main` with every mode's runner and the tray launch replaced by a
    tripwire, under an environment holding no `DIPLOMAT_*` name but the test's."""
    for name in [k for k in os.environ if k.startswith("DIPLOMAT_")]:
        monkeypatch.delenv(name)
    ran: list[str] = []

    def tripwire(what):
        def run(*_a, **_k):
            ran.append(what)
            return 0
        return run

    from diplomat_app import agentdump, app, migrate, render, selftest, selfupdate

    monkeypatch.setattr(migrate, "migrate_legacy_state_dir", tripwire("migrate"))
    monkeypatch.setattr(app, "run_app", tripwire("tray"))
    monkeypatch.setattr(selfupdate, "run_scheduled", tripwire("self-update"))
    monkeypatch.setattr(agentdump, "run", tripwire("agents"))
    monkeypatch.setattr(selftest, "run_dump", tripwire("dump"))
    monkeypatch.setattr(render, "run", tripwire("render"))
    return importlib.import_module("diplomat_app.__main__").main, ran


def test_the_entry_point_refuses_before_anything_runs(entry, monkeypatch, capsys):
    main, ran = entry
    monkeypatch.setenv("DIPLOMAT_REPOPATHS_TEST", "1")
    assert main() == 64
    assert ran == []
    err = capsys.readouterr().err
    assert "refusing to start: DIPLOMAT_REPOPATHS_TEST=1 " in err
    assert "DIPLOMAT_DUMP=1" in err and "DIPLOMAT_RENDER=<value>" in err


def test_the_entry_point_still_runs_a_known_mode(entry, monkeypatch):
    main, ran = entry
    monkeypatch.setenv("DIPLOMAT_AGENTS", "1")
    assert main() == 0
    assert ran == ["agents"]


def test_the_entry_point_still_starts_the_tray(entry):
    main, ran = entry
    assert main() == 0
    assert ran == ["migrate", "tray"]
