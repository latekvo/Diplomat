"""The device-allocator daemon opens repair agents in the terminal this front-end
publishes as ``spawnTerminal`` in the shared config. Pinned here: what is published
is the RESOLVED terminal, a change republishes it, and the daemon's own table of Linux
terminals (``device-allocator/src/terminal.js``) matches :data:`review.TERMINALS`.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from diplomat_runtime import appconfig, review
from diplomat_app.store import Store

pytest.importorskip("PySide6")

TERMINAL_JS = (Path(__file__).resolve().parents[3]
               / "device-allocator" / "src" / "terminal.js")


@pytest.fixture
def installed(monkeypatch):
    """Pretend exactly ``keys`` are installed."""
    def pick(*keys: str) -> None:
        monkeypatch.setattr(review.SpawnTerminal, "is_installed",
                            property(lambda t: t.key in keys))
    return pick


def test_the_resolved_terminal_is_published(installed):
    installed("konsole", "xterm")
    store = Store()
    store.terminal_choice = "kitty"  # stored, but not installed
    appconfig.set_value(appconfig.SPAWN_TERMINAL, "")
    store.publish_spawn_terminal()
    assert appconfig.get(appconfig.SPAWN_TERMINAL) == "konsole"


def test_building_a_store_publishes_nothing():
    """Only the running applet publishes (``DiplomatApp``): a Store built by a test
    fixture, a render or a self-test must leave the shared file alone."""
    Store()
    assert appconfig.SPAWN_TERMINAL not in appconfig.read()


def test_a_change_is_published(installed):
    installed("konsole", "xterm")
    store = Store()
    store.terminal_choice = "xterm"
    assert appconfig.get(appconfig.SPAWN_TERMINAL) == "xterm"


def test_the_daemon_knows_every_linux_terminal():
    body = re.search(r"LINUX_TERMINALS = \{(.*?)\n\};", TERMINAL_JS.read_text(), re.S).group(1)
    table = {
        key: re.findall(r"'([^']*)'", argv)
        for key, argv in re.findall(r"'?([\w-]+)'?: \[([^\]]*)\]", body)
    }
    assert table == {t.key: [t.exec_name, *t.prefix] for t in review.TERMINALS}
