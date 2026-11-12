"""The main window is a regular desktop window, and only an attended launch opens it.

A window the window manager decorates, lists in the taskbar and lets the operator
resize; closing it hides it while the app keeps working, and quitting is an explicit
action. ``DIPLOMAT_BACKGROUND=1`` - the login autostart, the 06:00 update's relaunch,
the watchdog's revival - starts the app with no window at all, so an unattended start
never takes focus. The launchers that set it are pinned in ``test_selfupdate.py`` and
below.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from diplomat_app import app as app_module
from diplomat_app import migrate, selfupdate
from diplomat_app.app import DiplomatApp
from diplomat_app.panel import _BASE_WIDTH, Panel
from diplomat_app.store import Store
from test_autofix import fake_probes

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QMenu, QMenuBar, QSystemTrayIcon  # noqa: E402

LINUX = Path(__file__).resolve().parents[1]


@pytest.fixture
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def panel(qapp, monkeypatch):
    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    fake_probes(monkeypatch)
    store = Store()
    store.me = "alice"
    p = Panel(store)
    yield p
    p.hide()
    p.deleteLater()


def _settle(qapp) -> None:
    for _ in range(3):
        qapp.processEvents()


def test_the_window_is_a_regular_titled_top_level_window(panel):
    # The type, not a flag test: Tool's bits include Window's.
    assert panel.windowType() == Qt.WindowType.Window
    flags = panel.windowFlags()
    for flag in (Qt.WindowType.FramelessWindowHint, Qt.WindowType.WindowStaysOnTopHint):
        assert not flags & flag, flag
    assert panel.windowTitle() == "Diplomat"
    assert panel.minimumWidth() == _BASE_WIDTH


def test_a_window_shorter_than_its_content_resizes_and_scrolls(panel, qapp):
    panel.show()
    panel.resize(_BASE_WIDTH, 360)
    _settle(qapp)
    assert panel.height() == 360
    # Every screen, the main one included, scrolls inside the window rather than
    # pushing the window's minimum height past what was asked for.
    assert panel.minimumSizeHint().height() <= 360


def test_focus_leaving_the_app_keeps_the_window_open(panel, qapp, monkeypatch):
    panel.show()
    _settle(qapp)
    monkeypatch.setattr(QApplication, "activeWindow", staticmethod(lambda: None))
    QApplication.sendEvent(panel, QEvent(QEvent.Type.WindowDeactivate))
    assert panel.isVisible()


def test_escape_on_the_main_screen_keeps_the_window_open(panel, qapp):
    panel.show()
    _settle(qapp)
    panel.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                  Qt.KeyboardModifier.NoModifier))
    assert panel.isVisible()


def test_closing_the_window_hides_it_and_it_opens_again(panel, qapp):
    panel.show()
    _settle(qapp)
    assert panel.close()
    assert not panel.isVisible()
    app = DiplomatApp.__new__(DiplomatApp)
    app.panel = panel
    app.show_window()
    assert panel.isVisible()


def test_a_minimized_window_polls_nothing_and_catches_up_when_restored(panel, qapp,
                                                                      monkeypatch):
    """The 8 s `ps` scan runs only while the window is on screen, and a minimized
    window is still ``isVisible()``."""
    calls: list[str] = []
    monkeypatch.setattr(panel.store, "refresh_auto_task_count_async",
                        lambda: calls.append("ps"))
    panel.show()
    _settle(qapp)
    calls.clear()
    panel._tasks_tick()
    assert calls == ["ps"]

    calls.clear()
    panel.setWindowState(Qt.WindowState.WindowMinimized)
    _settle(qapp)
    assert panel.isVisible() and panel.isMinimized()
    panel._tasks_tick()
    assert calls == []

    panel.setWindowState(Qt.WindowState.WindowNoState)
    _settle(qapp)
    assert calls == ["ps"]
    calls.clear()
    panel._tasks_tick()
    assert calls == ["ps"]


def test_file_quit_asks_to_quit(panel):
    bar = panel.findChild(QMenuBar)
    assert bar is not None
    actions = {a.text().replace("&", ""): a
               for menu in bar.findChildren(QMenu)
               for a in menu.actions()}
    quit_action = actions["Quit"]
    assert quit_action.shortcut().toString() == "Ctrl+Q"
    asked = []
    panel.quit_requested.connect(lambda: asked.append(True))
    quit_action.trigger()
    assert asked == [True]


def test_clicking_the_tray_icon_shows_the_window_and_never_hides_it(panel, qapp):
    app = DiplomatApp.__new__(DiplomatApp)
    app.panel = panel
    assert not panel.isVisible()
    app._on_tray_activated(QSystemTrayIcon.ActivationReason.Trigger)
    assert panel.isVisible()
    app._on_tray_activated(QSystemTrayIcon.ActivationReason.Trigger)
    assert panel.isVisible()


class _Launched:
    """``DiplomatApp`` without the app: records whether the window was opened."""

    shown: list[bool] = []

    def __init__(self) -> None:
        _Launched.shown.append(False)

    def show_window(self) -> None:
        _Launched.shown[-1] = True

    def exec(self) -> int:
        return 0


@pytest.fixture
def launched(monkeypatch):
    _Launched.shown = []
    monkeypatch.setattr(app_module, "DiplomatApp", _Launched)
    monkeypatch.setattr(app_module.SingleInstance, "acquire_newest_wins",
                        staticmethod(lambda: None))
    monkeypatch.setattr(selfupdate, "clear_operator_quit", lambda: None)
    monkeypatch.setattr(selfupdate, "record_display_env", lambda: None)
    return _Launched


@pytest.mark.parametrize("tray", [True, False])
def test_a_plain_launch_opens_the_window(launched, monkeypatch, tray):
    monkeypatch.delenv("DIPLOMAT_BACKGROUND", raising=False)
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: tray))
    assert app_module.run_app() == 0
    assert launched.shown == [True]


@pytest.mark.parametrize("tray", [True, False])
def test_a_background_launch_opens_nothing(launched, monkeypatch, tray):
    """And takes the variable out of its environment, so nothing it starts - an agent
    that runs ``./diplomat`` - inherits a hidden launch."""
    monkeypatch.setenv("DIPLOMAT_BACKGROUND", "1")
    monkeypatch.setattr(QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: tray))
    assert app_module.run_app() == 0
    assert launched.shown == [False]
    assert "DIPLOMAT_BACKGROUND" not in os.environ


def test_background_is_not_a_mode():
    """Not refused at the entry point, not a headless run, and a GUI that newest-wins
    may end like any other."""
    from diplomat_app import headless, singleton

    env = {"DIPLOMAT_BACKGROUND": "1"}
    assert headless.unrunnable(env) == []
    assert not headless.active(env)
    assert "DIPLOMAT_BACKGROUND" not in singleton.headless_markers()
    assert not singleton._environ_is_headless(b"DIPLOMAT_BACKGROUND=1\0")


# ---- the installer -------------------------------------------------------------


def _desktop_exec(entry: Path) -> list[str]:
    line = next(ln for ln in entry.read_text().splitlines() if ln.startswith("Exec="))
    return line.removeprefix("Exec=").split()


def _install(tmp_path: Path):
    """Run the installer against a copy of the package whose launcher and helper
    scripts are stubs; the launcher records whether it started in the background."""
    pkg = tmp_path / "linux"
    (pkg / "install").mkdir(parents=True)
    shutil.copy(LINUX / "install" / "install-autostart.sh", pkg / "install")
    for helper in ("build-core.sh", "install-autoupdate.sh", "install-watchdog.sh"):
        (pkg / "install" / helper).write_text("#!/usr/bin/env bash\nexit 0\n")
        (pkg / "install" / helper).chmod(0o755)
    record = tmp_path / "launched"
    launcher = pkg / "diplomat"
    launcher.write_text(
        f'#!/usr/bin/env bash\necho "bg=${{DIPLOMAT_BACKGROUND:-}}" >> "{record}"\n')
    home = tmp_path / "home"
    env = {**os.environ, "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
           "XDG_DATA_HOME": str(home / ".local/share"),
           "XDG_STATE_HOME": str(home / ".local/state")}
    env.pop("DIPLOMAT_BACKGROUND", None)
    proc = subprocess.run(["bash", str(pkg / "install" / "install-autostart.sh")],
                          env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    # The start the installer makes itself is the operator's: it opens the window.
    deadline = time.monotonic() + 10
    while not record.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert record.read_text().splitlines() == ["bg="]
    record.unlink()

    login = home / ".config" / "autostart" / "diplomat.desktop"
    menu = home / ".local" / "share" / "applications" / "diplomat.desktop"
    return launcher, env, record, login, menu


def test_the_login_entry_starts_in_the_background_and_the_menu_entry_does_not(tmp_path):
    """The ``Exec`` lines the installer writes are run as written."""
    _launcher, env, record, login, menu = _install(tmp_path)
    for entry, expected in ((login, "bg=1"), (menu, "bg=")):
        subprocess.run(_desktop_exec(entry), env=env, check=True)
        assert record.read_text().splitlines() == [expected], entry
        record.unlink()
    assert "X-GNOME-Autostart-enabled=true" in login.read_text()


# ---- an install from before unattended starts were hidden ------------------------

_OLD_LOGIN_ENTRY = """[Desktop Entry]
Type=Application
Name=Diplomat
Comment=software-mansion/argent triage tools in the system tray
Exec={launcher}
Icon=applications-development
Terminal=false
Categories=Development;Utility;
X-GNOME-Autostart-enabled=true
"""


@pytest.fixture
def old_install(tmp_path, monkeypatch):
    """What the installer writes now, then the login entry the earlier installer
    wrote, and no menu entry - the state an existing machine is in."""
    launcher, env, record, login, menu = _install(tmp_path)
    installed = login.read_text(), menu.read_text()
    login.write_text(_OLD_LOGIN_ENTRY.format(launcher=launcher))
    menu.unlink()
    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(name, env[name])
    return env, record, login, menu, installed


def _exec_line(text: str) -> str:
    return next(ln for ln in text.splitlines() if ln.startswith("Exec="))


def test_startup_brings_an_old_install_to_what_the_installer_writes(old_install):
    env, record, login, menu, (installed_login, installed_menu) = old_install
    old = login.read_text()
    migrate.migrate_autostart_entry()

    # The Exec line is the installer's; every other line, the old Comment included,
    # is kept.
    assert _exec_line(login.read_text()) == _exec_line(installed_login)
    assert login.read_text() == old.replace(_exec_line(old), _exec_line(installed_login))
    assert menu.read_text() == installed_menu
    for entry, expected in ((login, "bg=1"), (menu, "bg=")):
        subprocess.run(_desktop_exec(entry), env=env, check=True)
        assert record.read_text().splitlines() == [expected], entry
        record.unlink()

    # Idempotent: a second start rewrites neither file.
    stamps = [(p.stat().st_ino, p.stat().st_mtime_ns) for p in (login, menu)]
    migrate.migrate_autostart_entry()
    assert [(p.stat().st_ino, p.stat().st_mtime_ns) for p in (login, menu)] == stamps


def test_startup_leaves_an_existing_menu_entry_alone(old_install):
    _env, _record, login, menu, _installed = old_install
    menu.write_text("[Desktop Entry]\nExec=/somewhere/else\n")
    migrate.migrate_autostart_entry()
    assert menu.read_text() == "[Desktop Entry]\nExec=/somewhere/else\n"
    assert "DIPLOMAT_BACKGROUND=1" in _exec_line(login.read_text())


def test_startup_creates_no_entry_where_none_is_installed(old_install):
    _env, _record, login, menu, _installed = old_install
    login.unlink()
    migrate.migrate_autostart_entry()
    assert not login.exists() and not menu.exists()


def test_a_login_entry_that_cannot_be_rewritten_never_stops_the_start(old_install):
    _env, _record, login, menu, _installed = old_install
    old = login.read_text()
    login.parent.chmod(0o555)
    try:
        migrate.migrate_autostart_entry()
    finally:
        login.parent.chmod(0o755)
    assert login.read_text() == old
    assert [p.name for p in login.parent.iterdir()] == ["diplomat.desktop"]
