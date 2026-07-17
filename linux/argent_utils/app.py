"""Tray applet entry point: QSystemTrayIcon + the popup Panel.

Universal Linux tray via Qt6's StatusNotifierItem/XEmbed support — works under
XFCE (notification-area / status-notifier plugin), KDE, and GNOME (with an
AppIndicator extension). The heavy fetch runs on a worker thread; an idle timer
auto-refreshes so counts are fresh the moment the wrench is clicked.
"""

from __future__ import annotations

import os
import signal
import sys
import threading

from PySide6.QtCore import QTimer
from PySide6.QtGui import QAction, QCursor, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QMessageBox,
    QSystemTrayIcon,
)

from . import glyphs
from .panel import Panel
from .store import Store
from .singleton import SingleInstance
from .widgets import glyph_icon


def _wrench_icon() -> QIcon:
    """The tray icon: a monochrome wrench glyph, ink-centred and tinted to match
    the panel header (matching the macOS SF-Symbol look, never a colour-emoji)."""
    return glyph_icon(glyphs.G_APP, 64, "#0A84FF")


def auto_refresh_secs() -> float:
    raw = os.environ.get("ARGENT_UTILS_REFRESH_SECS")
    try:
        secs = float(raw) if raw else 5 * 60
    except ValueError:
        secs = 5 * 60
    return max(5.0, secs)


def autofix_poll_secs() -> float:
    """Cadence of the PR auto-fix monitor poll (matches the macOS 3-min default).
    Overridable with ARGENT_UTILS_AUTOFIX_SECS; floored at 30s to protect the
    shared GitHub rate-limit budget."""
    raw = os.environ.get("ARGENT_UTILS_AUTOFIX_SECS")
    try:
        secs = float(raw) if raw else 3 * 60
    except ValueError:
        secs = 3 * 60
    return max(30.0, secs)


def apiwatch_poll_secs() -> float:
    """Cadence of the Claude-API-error watcher scan (matches the macOS 20s default).
    Overridable with ARGENT_UTILS_APIWATCH_SECS; floored at 5s."""
    raw = os.environ.get("ARGENT_UTILS_APIWATCH_SECS")
    try:
        secs = float(raw) if raw else 20.0
    except ValueError:
        secs = 20.0
    return max(5.0, secs)


class ArgentUtilsApp:
    def __init__(self) -> None:
        self.app = QApplication.instance() or QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        self.app.setApplicationName("Argent Utils")
        self.app.setOrganizationName("argent-utils")

        self.store = Store()
        self.panel = Panel(self.store)
        self.panel.refresh_requested.connect(self.trigger_refresh)
        self.panel.quit_requested.connect(self.confirm_quit)

        self._fetch_thread: threading.Thread | None = None

        self._build_tray()

        # Auto-refresh, independent of whether the panel is open.
        self.timer = QTimer()
        self.timer.setInterval(int(auto_refresh_secs() * 1000))
        self.timer.timeout.connect(self.trigger_refresh)
        self.timer.start()

        # PR auto-fix monitor: poll on a background cadence, independent of the panel
        # (matches the macOS monitor). The poll no-ops when both toggles are off.
        self.autofix_timer = QTimer()
        self.autofix_timer.setInterval(int(autofix_poll_secs() * 1000))
        self.autofix_timer.timeout.connect(self.store.run_autofix_poll_async)
        self.autofix_timer.start()
        # First poll shortly after launch, once identity has had a moment to resolve.
        QTimer.singleShot(3000, self.store.run_autofix_poll_async)

        # Claude-API-error watcher: scan tmux panes on a fast cadence (matches the
        # macOS 20s watcher). The scan no-ops when the toggle is off or tmux is absent.
        self.apiwatch_timer = QTimer()
        self.apiwatch_timer.setInterval(int(apiwatch_poll_secs() * 1000))
        self.apiwatch_timer.timeout.connect(self.store.run_apiwatch_poll_async)
        self.apiwatch_timer.start()
        # First scan shortly after launch.
        QTimer.singleShot(5000, self.store.run_apiwatch_poll_async)

        # Resolve identity + first fetch eagerly.
        threading.Thread(target=self.store.fetch_me, daemon=True).start()
        self.trigger_refresh()

        # First-run: auto-install the device-allocator MCP so every local agent is
        # forced to reserve simulators/emulators. One-shot and respects an explicit
        # uninstall (see Store.ensure_allocator_installed_async).
        self.store.ensure_allocator_installed_async()

        # Join the LAN mesh if the user opted in (no-ops when disabled). Starts a
        # background node so duty coordination is live the moment the panel opens.
        self.store.ensure_mesh_running_async()

        # Optional prefill (also used for manual UI checks).
        prefill = os.environ.get("ARGENT_UTILS_PREFILL")
        if prefill:
            self.panel.search.setText(prefill)
            QTimer.singleShot(150, self.show_panel)

    # MARK: tray

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(_wrench_icon())
        self.tray.setToolTip("Argent Utils")
        menu = QMenu()
        act_open = QAction("Open", menu)
        act_open.triggered.connect(self.show_panel)
        act_refresh = QAction("Refresh", menu)
        act_refresh.triggered.connect(self.trigger_refresh)
        act_quit = QAction("Quit", menu)
        act_quit.triggered.connect(self.confirm_quit)
        menu.addAction(act_open)
        menu.addAction(act_refresh)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.MiddleClick,
        ):
            self.toggle_panel()

    # MARK: panel show/hide

    def toggle_panel(self) -> None:
        if self.panel.isVisible():
            self.panel.hide()
        else:
            self.show_panel()

    def show_panel(self) -> None:
        self._position_panel()
        self.panel.show()
        self.panel.raise_()
        self.panel.activateWindow()

    def _position_panel(self) -> None:
        screen = self.app.primaryScreen().availableGeometry()
        pos = QCursor.pos()
        w, h = self.panel.width(), self.panel.height()
        x = min(max(pos.x() - w // 2, screen.left()), screen.right() - w)
        # Prefer opening upward from the cursor (panel usually at screen bottom).
        y = pos.y() - h
        if y < screen.top():
            y = min(pos.y(), screen.bottom() - h)
        self.panel.move(x, y)

    # MARK: data

    def trigger_refresh(self) -> None:
        if self._fetch_thread and self._fetch_thread.is_alive():
            return
        self._fetch_thread = threading.Thread(target=self.store.refresh, daemon=True)
        self._fetch_thread.start()

    # MARK: quit

    def confirm_quit(self) -> None:
        box = QMessageBox(self.panel if self.panel.isVisible() else None)
        box.setWindowTitle("Quit Argent Utils?")
        box.setText("Quit Argent Utils?")
        box.setInformativeText("The tray wrench disappears until you launch it again.")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setStandardButtons(QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)
        box.button(QMessageBox.StandardButton.Yes).setText("Quit")
        if box.exec() == QMessageBox.StandardButton.Yes:
            self.quit()

    def quit(self) -> None:
        SingleInstance.release()
        self.tray.hide()
        self.app.quit()

    def exec(self) -> int:
        # Let Ctrl+C in a terminal kill the app cleanly.
        signal.signal(signal.SIGINT, lambda *_: self.quit())
        # A periodic no-op timer lets the Python signal handler run.
        nudge = QTimer()
        nudge.start(250)
        nudge.timeout.connect(lambda: None)
        return self.app.exec()


def run_app() -> int:
    SingleInstance.acquire_newest_wins()
    app = ArgentUtilsApp()
    if not QSystemTrayIcon.isSystemTrayAvailable():
        # No tray host — still usable: just show the panel.
        app.show_panel()
    return app.exec()
