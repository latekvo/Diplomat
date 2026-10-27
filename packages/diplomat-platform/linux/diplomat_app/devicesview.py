"""The Devices screen: everything about the device-allocator module in one place.

The Linux twin of DevicesScreen.swift, swapped in for the panel body by the header's
device button or the left pane's Devices header. Lists on the left, controls on the
right, as on the main panel: every device in the daemon's snapshot with the actions
the daemon takes for it, and the allocator events from the activity feed; then the
allocator itself (daemon, install, idle limits) and the remote device pool (its
provider module, its status, sign-in, what it lists, and the devices leased from it).

Every remote field is optional: against a daemon with no remote pool the card says so
and the rest of the screen is unchanged.

Also home to the allocator controls Settings builds its own card from.
"""

from __future__ import annotations

import html
import os
import time
from datetime import datetime, timezone

from PySide6 import QtGui
from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from diplomat_runtime import activity, appconfig
from diplomat_runtime.models import Fmt
from . import deviceallocator as da
from . import glyphs
from .store import RemoteCall, Store
from .widgets import (
    ActivityRow,
    ElidedLabel,
    GlyphLabel,
    IconChip,
    Pill,
    SettingRow,
    SliderSetting,
    muted,
    settings_card,
    tint_bg,
)

_CYAN, _INDIGO, _BLUE = "#32ADE6", "#5E5CE6", "#0A84FF"
_GREEN, _ORANGE, _RED = "#34C759", "#FF9500", "#FF3B30"
_PURPLE, _GREY = "#AF52DE", da.GREY


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


def _plain(text: str = "") -> QLabel:
    """A label that shows its text as it is. Qt's default guesses HTML from the text,
    and much of what this screen shows (agent names, provider reasons, paths) comes
    from the daemon or a provider."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def _note(text: str, tint: str | None = None) -> QLabel:
    label = _plain(text)
    label.setWordWrap(True)
    label.setStyleSheet(muted(10) if tint is None else f"color: {tint}; font-size: 10px;")
    return label


def _clock(epoch: float) -> str:
    return Fmt.clock(datetime.fromtimestamp(epoch, tz=timezone.utc))


# MARK: - allocator controls (shared with Settings)

def allocator_pill(s: dict | None) -> tuple[str, str]:
    """"Installed" alone would be a true statement about a machine still running the
    copies some earlier checkout laid down, so the stale case says so and the marks
    name what drifted. Amber for stale, not green: it is working, but not from this
    checkout."""
    if s is None:
        return "checking…", _GREY
    version = s.get("version") or "?"
    if s.get("outdated"):
        return f"out of date · v{version}", _ORANGE
    if s.get("installed"):
        return f"v{version}", _GREEN
    return "not installed", _GREY


def allocator_ready() -> bool:
    return da.package_available() and da.resolve_node() is not None


def allocator_summary() -> tuple[str, str | None]:
    """The install row's one-liner, and its tint when it is a warning."""
    if not da.package_available():
        return (f"⚠ Package not found at {da.package_dir()}. Set "
                "DIPLOMAT_DEVICE_ALLOCATOR_DIR to point at it.", _ORANGE)
    if da.resolve_node() is None:
        return ("⚠ Node.js not found. Install Node (or set DIPLOMAT_NODE) — the "
                "allocator's MCP server and daemon need it to run.", _ORANGE)
    return "So two agents never drive the same emulator at once.", None


PREDATES_POOL_NOTE = ("The running allocator daemon reports no remote pool: it predates "
                      "one. Update the checkout if it has none either, then Restart daemon.")
STOPPED_PREDATES_POOL_NOTE = ("The allocator daemon is not running, and the one that left "
                              "this snapshot predates remote pools. Update the checkout if it "
                              "has none either, then Start daemon.")
#: Before the first ``--check`` says whether a daemon runs.
SNAPSHOT_PREDATES_POOL_NOTE = ("The allocator's snapshot has no remote pool: the daemon "
                               "that wrote it predates one.")


def predates_pool(s: dict | None) -> tuple[str, str | None]:
    """What the REMOTE POOL card says, and offers (``"start"``, ``"restart"`` or
    nothing), over a snapshot with no remote pool while a provider is set. The snapshot
    outlives its daemon, so only one that answers is called running. Twin of
    ``AllocatorUI.predatesPool`` in DevicesScreen.swift."""
    if s is None:
        return SNAPSHOT_PREDATES_POOL_NOTE, None
    if s.get("daemonRunning"):
        return PREDATES_POOL_NOTE, "restart"
    return STOPPED_PREDATES_POOL_NOTE, "start"


#: Twin of ``AllocatorUI.restartHelp`` in DevicesScreen.swift.
RESTART_HELP = ("Stop the running daemon and start this checkout's. Agents keep their "
                "local leases; a simulator or emulator the allocator booted is shut down as "
                "the old daemon exits, and a remote device goes back to its pool.")


def restart_button(store: Store) -> QPushButton:
    button = QPushButton("Restart daemon")
    button.setToolTip(RESTART_HELP)
    button.setEnabled(allocator_ready() and not store.allocator_busy)
    button.clicked.connect(store.restart_allocator_daemon_async)
    return button


def start_button(store: Store) -> QPushButton:
    button = QPushButton("Start daemon")
    button.setEnabled(allocator_ready() and not store.allocator_busy)
    button.clicked.connect(store.start_allocator_daemon_async)
    return button


class AllocatorControls(QWidget):
    """The install's four marks (and the daemon's), what drifted, and the buttons that
    install, update, uninstall, start or restart the daemon and re-check.
    :meth:`refresh` takes the installer's latest ``--check``."""

    _MARKS = (("mcpRegistered", "MCP"), ("skillInstalled", "skill"),
              ("ruleInstalled", "rule"), ("claudeMdInjected", "CLAUDE.md"))

    def __init__(self, store: Store, *, offers_daemon: bool = False) -> None:
        super().__init__()
        self.store = store
        self._offers_daemon = offers_daemon
        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(7)

        marks = QHBoxLayout()
        marks.setSpacing(4)
        self.marks: dict[str, Pill] = {}
        for key, _label in self._MARKS:
            self.marks[key] = Pill("")
            marks.addWidget(self.marks[key])
        self.daemon = Pill("")
        marks.addWidget(self.daemon)
        marks.addStretch(1)
        col.addLayout(marks)

        self.drift = _plain("")
        self.drift.setWordWrap(True)
        self.drift.setStyleSheet(f"color: {_ORANGE}; font-size: 9px; font-family: monospace;")
        col.addWidget(self.drift)

        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.install = QPushButton("Install")
        self.install.setStyleSheet("font-weight: 700;")
        self.install.clicked.connect(store.install_allocator_async)
        buttons.addWidget(self.install)
        self.uninstall = QPushButton("Uninstall")
        self.uninstall.clicked.connect(store.uninstall_allocator_async)
        buttons.addWidget(self.uninstall)
        self.start = start_button(store)
        buttons.addWidget(self.start)
        self.restart = restart_button(store)
        buttons.addWidget(self.restart)
        self.recheck = QPushButton("⟲")
        self.recheck.setFixedWidth(34)
        self.recheck.setToolTip("Re-check status")
        self.recheck.clicked.connect(store.refresh_allocator_install_async)
        buttons.addWidget(self.recheck)
        buttons.addStretch(1)
        col.addLayout(buttons)
        self.refresh(None)

    def refresh(self, s: dict | None) -> None:
        installed = bool(s and s.get("installed"))
        outdated = bool(s and s.get("outdated"))
        for key, label in self._MARKS:
            if s is None:
                self.marks[key].set_state("")
            else:
                self.marks[key].set_mark(label, bool(s.get(key)))
        running = bool(s and s.get("daemonRunning"))
        self.daemon.set_state("⚡ daemon" if running else "", _GREEN)
        drift = (s or {}).get("drift") or []
        self.drift.setVisible(bool(outdated and drift))
        self.drift.setText("stale: " + ", ".join(str(d) for d in drift) if drift else "")
        self.install.setText("Update" if outdated else "Reinstall" if installed else "Install")
        self.uninstall.setVisible(installed)
        self.start.setVisible(self._offers_daemon and installed and not running)
        self.restart.setVisible(self._offers_daemon and installed and running)
        idle = not self.store.allocator_busy
        ready = allocator_ready() and idle
        for button in (self.install, self.start, self.restart):
            button.setEnabled(ready)
        self.uninstall.setEnabled(idle)
        self.recheck.setEnabled(idle)


class _IdleLimitSlider(SliderSetting):
    """One idle limit's slider, written through ``save`` and re-read on every
    ``store.changed``: Settings and the Devices screen both set the same two limits."""

    def __init__(self, store: Store, attr: str, save, *, label: str, maximum: int,
                 step: int) -> None:
        super().__init__(label=label, minimum=0, maximum=maximum, step=step,
                         min_label="never", max_label=f"{maximum} min", tint=_CYAN)
        self._store = store
        self._attr = attr
        self._write = save
        self.set_badge_text(lambda n: "never" if n == 0 else f"{n} min")
        self.sync()
        self.changed.connect(self._on_changed)
        store.changed.connect(self.sync)

    def sync(self) -> None:
        self.set_value(getattr(self._store, self._attr))

    def _on_changed(self, value: int) -> None:
        self._write(lambda: setattr(self._store, self._attr, value))
        self._store.changed.emit()


def idle_limit_rows(store: Store, save) -> list[tuple[SliderSetting, SettingRow]]:
    """The daemon's two idle limits, in minutes, where 0 turns either off: a slider and
    the row it sits in for each. ``save`` runs one write to the shared config and says
    whether it landed."""
    free = _IdleLimitSlider(store, "device_idle_free_minutes", save,
                            label="Shut down a free device after",
                            maximum=appconfig.MAX_DEVICE_IDLE_FREE_MINUTES, step=1)
    leased = _IdleLimitSlider(store, "device_idle_leased_minutes", save,
                              label="Take a held device back after",
                              maximum=appconfig.MAX_DEVICE_IDLE_LEASED_MINUTES, step=5)
    return [
        (free, SettingRow(
            "Shut down a free device after", free, stacked=True,
            summary="A booted device no agent holds, once its screen stops changing.")),
        (leased, SettingRow(
            "Take a held device back after", leased, stacked=True,
            summary="From its agent, once its screen stops changing. It then counts "
                    "as free.",
            detail="A change to either reaches the allocator within a minute.")),
    ]


# MARK: - remote pool pieces

def remote_pill(r: da.RemoteStatus | None, *, snapshot: bool) -> tuple[str, str]:
    """One word per state the daemon publishes; an unknown one is shown as it is."""
    if r is None:
        return ("not available" if snapshot else "unknown"), _GREY
    return {
        "on": ("✓ on", _GREEN),
        "off": ("off", _GREY),
        "loading": ("loading…", _GREY),
        "error": ("✕ error", _RED),
        "signed-out": ("signed out", _ORANGE),
        "signing-in": ("signing in…", _BLUE),
        "sign-in-unanswered": ("☾ away", _ORANGE),
    }.get(r.state, (r.state, _GREY))


def _link(text: str, url: str, px: int, *, mono: bool = False) -> QLabel:
    family = " font-family: monospace;" if mono else ""
    label = QLabel(f'<a href="{html.escape(url)}">{html.escape(text)}</a>')
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setOpenExternalLinks(True)
    label.setStyleSheet(f"font-size: {px}px;{family}")
    return label


def _remote_status_view(status: da.RemoteStatus) -> QWidget:
    """The provider's status, every key of it: the known ones in words, the rest as
    ``key value`` lines."""
    host = QWidget()
    host.setObjectName("remoteStatus")
    host.setStyleSheet(f"#remoteStatus {{ background-color: {tint_bg(_INDIGO, 0.07)};"
                       " border-radius: 6px; }")
    col = QVBoxLayout(host)
    col.setContentsMargins(7, 7, 7, 7)
    col.setSpacing(4)
    head = QHBoxLayout()
    head.setSpacing(6)
    name = _plain(status.provider or "provider")
    name.setStyleSheet("font-size: 11px; font-weight: 600;")
    head.addWidget(name)
    state = _plain(status.state)
    state.setStyleSheet(muted(9, mono=True))
    head.addWidget(state)
    head.addStretch(1)
    col.addLayout(head)
    if status.reason is not None:
        col.addWidget(_note(f"⚠ {status.reason}",
                            _RED if status.state == "error" else _ORANGE))
    if status.retry_at is not None:
        at = _clock(status.retry_at)
        col.addWidget(_note(
            f"☾ Sign-in went unanswered: treated as away, no prompt until {at}."
            if status.state == "sign-in-unanswered" else f"☾ Retry at {at}.", _ORANGE))
    if status.login_url and QUrl(status.login_url).isValid():
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(_link("Open the sign-in page", status.login_url, 10))
        shown = ElidedLabel(status.login_url, 9, glyphs.MUTED)
        row.addWidget(shown, 1)
        col.addLayout(row)
    if status.url and QUrl(status.url).isValid():
        col.addWidget(_link(status.url, status.url, 9, mono=True))
    for key, value in status.extras:
        row = QHBoxLayout()
        row.setSpacing(6)
        k = _plain(key)
        k.setStyleSheet(muted(9, mono=True))
        k.setMinimumWidth(70)
        row.addWidget(k)
        v = _plain(value)
        v.setWordWrap(True)
        v.setStyleSheet("font-size: 9px; font-family: monospace;")
        row.addWidget(v, 1)
        col.addLayout(row)
    return host


def _specs(d: da.RemotePoolDevice) -> str:
    parts = [d.platform, d.version and f"v{d.version}",
             d.api_version and f"API {d.api_version}", d.format]
    return " · ".join(p for p in parts if p)


def _call_line(call: RemoteCall, label: str, running: str) -> QWidget | None:
    """What the last sign-in or listing answered, or that it is still waiting."""
    if call.running:
        return _note(f"⟳ {running}")
    reply = call.reply
    if reply is None:
        return None
    at = _clock(call.at) if call.at is not None else "—"
    if reply.ok:
        return _note(f"{label}: signed in · {at}", _GREEN)
    if reply.devices is None:
        return _note(f"{label}: {reply.reason} · {at}", _ORANGE)
    host = QWidget()
    col = QVBoxLayout(host)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(2)
    n = len(reply.devices)
    col.addWidget(_note(f"{label}: {n} device{'' if n == 1 else 's'} · {at}"))
    for d in reply.devices:
        row = QWidget()
        row.setObjectName("poolRow")
        row.setStyleSheet("#poolRow { background-color: rgba(128,128,128,0.06);"
                          " border-radius: 5px; }")
        rl = QHBoxLayout(row)
        rl.setContentsMargins(6, 3, 6, 3)
        rl.setSpacing(6)
        name = _plain(d.name or d.id)
        name.setStyleSheet("font-size: 10px;")
        rl.addWidget(name)
        specs = _plain(_specs(d))
        specs.setStyleSheet(muted(9))
        rl.addWidget(specs)
        rl.addStretch(1)
        if d.state:
            state = _plain(d.state)
            state.setStyleSheet(muted(9, bold=True))
            rl.addWidget(state)
        col.addWidget(row)
    return host


# MARK: - one device

def _badge(dev: dict) -> QLabel:
    text, color = da.status_badge(dev)
    badge = _plain(text)
    badge.setStyleSheet(
        f"color: {color}; font-weight: 700; font-size: 9px;"
        f" background-color: {tint_bg(color, 0.14)}; border-radius: 7px; padding: 2px 6px;"
    )
    return badge


def _platform(dev: dict) -> tuple[str, str]:
    platform = dev.get("platform", "")
    return (glyphs.PLATFORM_GLYPH.get(platform, glyphs.G_PHONE),
            da.PLATFORM_TINT.get(platform, _GREY))


class DeviceScreenRow(QWidget):
    """A device on the Devices screen: what it is, where it comes from, who holds it
    and for how long, and the daemon actions that apply to it."""

    def __init__(self, dev: dict, *, provider: str | None, idle_limit: int,
                 store: Store) -> None:
        super().__init__()
        self.setObjectName("deviceRow")
        self.setStyleSheet("#deviceRow { background-color: rgba(128,128,128,0.06);"
                           " border-radius: 6px; }")
        allocated = da.is_allocated(dev)
        remote = da.is_remote(dev)
        glyph, tint = _platform(dev)
        status = dev.get("status")
        owner = dev.get("owner") or {}
        key = dev.get("key", "")

        row = QHBoxLayout(self)
        row.setContentsMargins(6, 6, 6, 6)
        row.setSpacing(8)
        row.addWidget(IconChip(glyph, tint, 22, active=allocated), 0, Qt.AlignmentFlag.AlignTop)

        text = QVBoxLayout()
        text.setSpacing(2)
        name_row = QHBoxLayout()
        name_row.setSpacing(4)
        name = _plain(dev.get("name") or dev.get("handle") or key)
        name.setStyleSheet("font-size: 11px; font-weight: 700;")
        name_row.addWidget(name)
        for field in ("version", "format"):
            if dev.get(field):
                extra = _plain(str(dev[field]))
                extra.setStyleSheet(muted(9))
                name_row.addWidget(extra)
        chip_text = (f"remote · {provider}" if provider else "remote") if remote else "local"
        chip_tint = _INDIGO if remote else _GREY
        chip = _plain(chip_text)
        chip.setStyleSheet(
            f"color: {chip_tint}; background-color: {tint_bg(chip_tint, 0.14)};"
            " border-radius: 6px; padding: 0px 5px; font-size: 8px; font-weight: 700;")
        name_row.addWidget(chip)
        name_row.addStretch(1)
        text.addLayout(name_row)

        pid = owner.get("ownerPid")
        if status != "repairing" and pid is not None:
            holder = QHBoxLayout()
            holder.setSpacing(6)
            who = _plain(f"● {owner.get('agentName') or f'pid {pid}'}")
            who.setStyleSheet(f"color: {tint}; font-size: 9px; font-weight: 600;")
            holder.addWidget(who)
            started = dev.get("allocatedAt")
            if isinstance(started, (int, float)):
                held = _plain(f"since {_clock(started / 1000)} "
                              f"({Fmt.duration(time.time() - started / 1000)})")
                held.setStyleSheet(muted(9))
                holder.addWidget(held)
            minutes = da.idle_minutes(dev)
            if minutes is not None:
                idle = _plain(f"idle {minutes}m")
                color = da.idle_color(minutes, idle_limit)
                idle.setStyleSheet(muted(9) if color is None
                                   else f"color: {color}; font-size: 9px;")
                holder.addWidget(idle)
            holder.addStretch(1)
            text.addLayout(holder)

        handle = ElidedLabel(dev.get("handle") or key, 9, glyphs.MUTED)
        text.addWidget(handle)
        if status == "repairing":
            reason = dev.get("brokenReason")
            repair = _plain(f"repair: {reason}" if reason else "repair dispatched")
            repair.setWordWrap(True)
            repair.setStyleSheet(f"color: {_PURPLE}; font-size: 9px;")
            text.addWidget(repair)
        row.addLayout(text, 1)

        trailing = QVBoxLayout()
        trailing.setSpacing(5)
        trailing.addWidget(_badge(dev), 0, Qt.AlignmentFlag.AlignRight)
        actions = QHBoxLayout()
        actions.setSpacing(4)
        actions.addStretch(1)
        self.actions: dict[str, QPushButton] = {}
        # Only what the daemon does for this row: a lease's holder can be dropped, a
        # quarantine ended, and any running device killed (a remote one returned).
        if remote:
            # A remote row exists only while leased.
            self._action(actions, "Return", _INDIGO, lambda: store.kill_device_async(key),
                         "Hand the lease back to the remote pool. The agent loses the device.")
        else:
            if pid is not None and status != "repairing":
                self._action(actions, "Free", _BLUE, lambda: store.release_device_async(dev),
                             f"Take it back from {owner.get('agentName') or 'its agent'}. "
                             "One the allocator booted is shut down.")
            if status == "repairing":
                self._action(actions, "Repaired", _PURPLE,
                             lambda: store.mark_device_repaired_async(key),
                             "End the quarantine and put it back in the pool.")
            if status != "free":
                self._action(actions, "Kill", _RED, lambda: store.kill_device_async(key),
                             "Free it and shut the simulator/emulator down.")
        trailing.addLayout(actions)
        trailing.addStretch(1)
        row.addLayout(trailing)

    def _action(self, layout: QHBoxLayout, title: str, tint: str, slot, tip: str) -> None:
        btn = QPushButton(title)
        # A tooltip guesses HTML from its text, and the tip may carry an agent's name.
        btn.setToolTip(QtGui.Qt.convertFromPlainText(tip))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton {{ color: {tint}; background-color: {tint_bg(tint, 0.14)};"
            " border: none; border-radius: 5px; padding: 2px 7px; font-size: 9px;"
            " font-weight: 700; }"
            f"QPushButton:hover {{ background-color: {tint_bg(tint, 0.26)}; }}")
        btn.clicked.connect(slot)
        layout.addWidget(btn)
        self.actions[title] = btn


# MARK: - the screen

#: The feed's allocator lines. An ``auto`` kill-device line is the agent reaper
#: closing a window, not a device, so it stays out.
EVENT_ACTIONS = frozenset({
    "kill-device", "release-device", "repair-done", "remote-lease-ended",
    "allocator-install", "allocator-uninstall",
})


def allocator_events(entries: list) -> list:
    return [e for e in entries
            if e.action in EVENT_ACTIONS
            and not (e.action == "kill-device" and e.source == "auto")][:6]


class DevicesView(QWidget):
    """The Devices screen. Emits ``done`` when the user is finished, like the other
    screens. ``live`` keeps the install status fresh while the screen is up; off in a
    render, whose seeded status a real ``--check`` would overwrite."""

    done = Signal()

    def __init__(self, store: Store, *, live: bool | None = None) -> None:
        super().__init__()
        self.store = store
        self.live = "DIPLOMAT_RENDER" not in os.environ if live is None else live
        #: The provider field while it is being edited; None shows the stored value.
        self._provider_draft: str | None = None
        self._inuse_expanded = True
        self._free_expanded = True
        #: The leased idle limit the rows were last coloured toward.
        self._drawn_limit: int | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        root.addLayout(self._build_header())

        self._save_error = _plain("")
        self._save_error.setWordWrap(True)
        self._save_error.setStyleSheet(f"color: {_RED}; font-size: 11px;")
        self._save_error.hide()
        root.addWidget(self._save_error)

        body = QHBoxLayout()
        body.setSpacing(12)
        left = QVBoxLayout()
        left.setSpacing(10)
        self._devices_card, self._devices_col, self._devices_pill = settings_card(
            glyphs.G_DEVICES, "DEVICES", _CYAN)
        left.addWidget(self._devices_card)
        events_card, self._events_col, _ = settings_card(glyphs.G_RECENT, "RECENT", _GREY)
        left.addWidget(events_card)
        left.addStretch(1)
        right = QVBoxLayout()
        right.setSpacing(10)
        right.addWidget(self._build_allocator_card())
        right.addWidget(self._build_remote_card())
        right.addStretch(1)
        body.addLayout(left, 1)
        body.addLayout(right, 1)
        root.addLayout(body)
        root.addStretch(1)

        # `daemonRunning` comes from `--check` alone, which no poll runs, and the
        # provider is also set by hand.
        self._install_timer = QTimer(self)
        self._install_timer.setInterval(15_000)
        self._install_timer.timeout.connect(self._poll)
        # Advances the held-since durations, counted from a fixed instant.
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(30_000)
        self._clock_timer.timeout.connect(self._rebuild_devices)

        store.devices_changed.connect(self.rebuild)
        store.activity_changed.connect(self._rebuild_events)
        store.allocator_changed.connect(self._on_allocator_changed)
        store.remote_changed.connect(self._rebuild_remote)
        store.changed.connect(self._sync_limits)
        self.rebuild()

    def opened(self) -> None:
        self.rebuild()
        if self.live:
            self.store.refresh_allocator_install_async()
            self._install_timer.start()
            self._clock_timer.start()

    def closed(self) -> None:
        self._install_timer.stop()
        self._clock_timer.stop()

    def _poll(self) -> None:
        self._rebuild_remote()
        self.store.refresh_allocator_install_async()

    def _on_allocator_changed(self) -> None:
        # The remote card offers a start or a restart by whether the daemon runs.
        self._refresh_allocator()
        self._rebuild_remote()

    def rebuild(self) -> None:
        self._refresh_header()
        self._rebuild_devices()
        self._rebuild_events()
        self._refresh_allocator()
        self._rebuild_remote()

    def _save(self, write) -> bool:
        """Run one write to the shared config file and say whether it landed."""
        try:
            write()
        except OSError as exc:
            self._save_error.setText(f"⚠ Couldn't save that change "
                                     f"({exc.strerror or exc}), so it has no effect "
                                     "until it is made again.")
            self._save_error.show()
            return False
        self._save_error.hide()
        return True

    # MARK: header

    def _daemon_light(self) -> tuple[str, str]:
        s = self.store.allocator_install
        if s is None:
            return _GREY, "checking"
        if s.get("daemonRunning"):
            return _GREEN, "daemon running"
        return (_RED, "daemon not running") if s.get("installed") else (_GREY, "not installed")

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(6)
        row.addWidget(GlyphLabel(glyphs.G_DEVICES, 16, glyphs.MUTED, font_px=14))
        title = _plain("Devices")
        title.setStyleSheet("font-weight: 700; font-size: 13px;")
        row.addWidget(title)
        self._counts = _plain("")
        self._counts.setStyleSheet(muted(10, mono=True))
        row.addWidget(self._counts)
        row.addStretch(1)
        self._dot = _plain("●")
        row.addWidget(self._dot)
        self._light = _plain("")
        self._light.setStyleSheet(muted(9))
        row.addWidget(self._light)
        done = QPushButton("Done")
        done.setStyleSheet("font-weight: 700;")
        done.clicked.connect(self.done.emit)
        row.addWidget(done)
        return row

    def _refresh_header(self) -> None:
        state = self.store.device_state
        self._counts.setText(
            f"{da.allocated_count(state)} in use · {da.free_count(state)} free" if state else "")
        self._counts.setVisible(state is not None)
        color, label = self._daemon_light()
        self._dot.setStyleSheet(f"color: {color}; font-size: 9px;")
        self._light.setText(label)

    # MARK: devices

    def _rebuild_devices(self) -> None:
        _clear(self._devices_col)
        state = self.store.device_state
        devices = (state or {}).get("devices", [])

        def sort_key(d: dict):
            return (d.get("platform", ""), d.get("name") or "")

        in_use = sorted((d for d in devices if da.is_allocated(d)), key=sort_key)
        free = sorted((d for d in devices if not da.is_allocated(d)), key=sort_key)
        if state is None:
            self._devices_pill.set_state("")
            self._devices_col.addWidget(_note(
                "No snapshot yet: the allocator daemon has not run on this machine."))
        else:
            self._devices_pill.set_state(f"{len(in_use)} in use", _GREEN if in_use else _GREY)
            if not devices:
                self._devices_col.addWidget(_note(
                    "The allocator sees no simulator or emulator on this machine."))
            if (self.store.allocator_install or {}).get("daemonRunning") is False:
                self._devices_col.addWidget(_note(
                    "The daemon is not running, so this is its last snapshot, not the "
                    "pool now.", _ORANGE))
        remote = (state or {}).get("remote")
        provider = remote.provider if remote is not None else None
        limit = self._drawn_limit = self.store.device_idle_leased_minutes
        for title, tint, expanded, group, toggle in (
                ("In use", _GREEN, self._inuse_expanded, in_use, self._toggle_inuse),
                ("Free", _GREY, self._free_expanded, free, self._toggle_free)):
            if not group:
                continue
            header = QToolButton()
            header.setText(f"{'▾' if expanded else '▸'}  {title.upper()}    {len(group)}")
            header.setCursor(Qt.CursorShape.PointingHandCursor)
            header.setStyleSheet(
                f"QToolButton {{ border: none; color: {tint}; font-weight: 700;"
                " font-size: 9px; padding: 2px 0; text-align: left; }"
                "QToolButton:hover { color: palette(text); }")
            header.clicked.connect(toggle)
            self._devices_col.addWidget(header)
            if expanded:
                for dev in group:
                    self._devices_col.addWidget(DeviceScreenRow(
                        dev, provider=provider, idle_limit=limit, store=self.store))

    def _toggle_inuse(self) -> None:
        self._inuse_expanded = not self._inuse_expanded
        self._rebuild_devices()

    def _toggle_free(self) -> None:
        self._free_expanded = not self._free_expanded
        self._rebuild_devices()

    def _rebuild_events(self) -> None:
        _clear(self._events_col)
        events = allocator_events(self.store.audit_entries)
        if not events:
            self._events_col.addWidget(_note("No allocator events in the activity feed."))
        for e in events:
            self._events_col.addWidget(ActivityRow(
                glyph=activity.glyph_for(e.action),
                glyph_color=activity.color_for(e.action),
                detail=e.detail, source=e.source,
                source_color=activity.source_color(e.source),
                clock=Fmt.clock(e.date)))

    # MARK: allocator

    def _build_allocator_card(self) -> QWidget:
        card, body, self._alloc_pill = settings_card(glyphs.G_ALLOCATOR, "ALLOCATOR", _CYAN)
        self._daemon_pill = Pill("")
        self._daemon_row = SettingRow("Daemon", self._daemon_pill)
        body.addWidget(self._daemon_row)
        self.allocator_controls = AllocatorControls(self.store, offers_daemon=True)
        summary, tint = allocator_summary()
        reserve = SettingRow("Reserve a simulator before using it", self.allocator_controls,
                             stacked=True)
        reserve.set_summary(summary, color=tint)
        body.addWidget(reserve)
        self._idle = idle_limit_rows(self.store, self._save)
        for _slider, row in self._idle:
            body.addWidget(row)
        return card

    def _daemon_summary(self) -> str:
        s = self.store.allocator_install
        if s is None:
            return "Asking the installer…"
        if s.get("daemonRunning"):
            pid = (self.store.device_state or {}).get("daemonPid")
            return f"{f'pid {pid} · ' if pid is not None else ''}{da.socket_path()}"
        if s.get("installed"):
            return "Not answering. An agent's first request starts it; Start runs it now."
        return "Install the allocator to run it."

    def _refresh_allocator(self) -> None:
        s = self.store.allocator_install
        self._alloc_pill.set_state(*allocator_pill(s))
        color, label = self._daemon_light()
        running = bool(s and s.get("daemonRunning"))
        self._daemon_pill.set_state(f"{'⚡' if running else '⊘'} {label}", color)
        self._daemon_row.set_summary(self._daemon_summary())
        self.allocator_controls.refresh(s)
        self._refresh_header()

    def _sync_limits(self) -> None:
        """Idle time is coloured toward the leased limit, wherever that was moved."""
        if self._drawn_limit != self.store.device_idle_leased_minutes:
            self._rebuild_devices()

    # MARK: remote pool

    def _remote(self) -> da.RemoteStatus | None:
        return (self.store.device_state or {}).get("remote")

    def _build_remote_card(self) -> QWidget:
        card, body, self._remote_pill = settings_card(glyphs.G_REMOTE, "REMOTE POOL", _INDIGO)
        self._provider_row = SettingRow("Provider module", self._build_provider_field(),
                                        stacked=True)
        body.addWidget(self._provider_row)
        self._status_col = QVBoxLayout()
        self._status_col.setSpacing(4)
        body.addLayout(self._status_col)

        calls = QWidget()
        col = QVBoxLayout(calls)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(6)
        buttons = QHBoxLayout()
        buttons.setSpacing(8)
        self.sign_in = QPushButton("Sign in")
        self.sign_in.setStyleSheet("font-weight: 700;")
        self.sign_in.clicked.connect(self.store.remote_sign_in_async)
        buttons.addWidget(self.sign_in)
        self.list_pool = QPushButton("List pool devices")
        self.list_pool.clicked.connect(self.store.list_remote_pool_async)
        buttons.addWidget(self.list_pool)
        buttons.addStretch(1)
        col.addLayout(buttons)
        self._calls_col = QVBoxLayout()
        self._calls_col.setSpacing(4)
        col.addLayout(self._calls_col)
        body.addWidget(SettingRow("Pool", calls, stacked=True))

        self._leases_col = QVBoxLayout()
        self._leases_col.setSpacing(4)
        body.addLayout(self._leases_col)
        return card

    def _build_provider_field(self) -> QWidget:
        host = QWidget()
        host.setObjectName("providerField")
        host.setStyleSheet("#providerField { background-color: rgba(128,128,128,0.12);"
                           " border-radius: 6px; }")
        row = QHBoxLayout(host)
        row.setContentsMargins(7, 4, 7, 4)
        row.setSpacing(6)
        self.provider_field = QLineEdit(self.store.device_remote_provider)
        self.provider_field.setPlaceholderText("~/path/to/provider.mjs")
        self.provider_field.setFrame(False)
        self.provider_field.setAccessibleName("Provider module")
        self.provider_field.textEdited.connect(self._on_provider_edited)
        self.provider_field.returnPressed.connect(self.apply_provider)
        row.addWidget(self.provider_field, 1)
        self.provider_clear = QToolButton()
        self.provider_clear.setText("✕")
        self.provider_clear.setToolTip("Clear: turn the remote pool off")
        self.provider_clear.setStyleSheet("QToolButton { border: none; color: palette(mid); }")
        self.provider_clear.clicked.connect(self.clear_provider)
        row.addWidget(self.provider_clear)
        self.provider_apply = QPushButton("Apply")
        self.provider_apply.setStyleSheet("font-weight: 700;")
        self.provider_apply.clicked.connect(self.apply_provider)
        row.addWidget(self.provider_apply)
        choose = QPushButton("Choose…")
        choose.clicked.connect(self._choose_provider)
        row.addWidget(choose)
        return host

    def _on_provider_edited(self, text: str) -> None:
        self._provider_draft = text
        self._refresh_provider()

    def apply_provider(self) -> None:
        """Committed on Apply/Return rather than per keystroke: the daemon loads
        whatever path the file names on its next request, half-typed or not."""
        if self._provider_draft is None:
            return
        draft = self._provider_draft
        if self._save(lambda: setattr(self.store, "device_remote_provider", draft)):
            self._provider_draft = None
        self._rebuild_remote()

    def clear_provider(self) -> None:
        self._provider_draft = None
        self._save(lambda: setattr(self.store, "device_remote_provider", ""))
        self._rebuild_remote()

    def _choose_provider(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose the module that lends devices from the remote pool",
            os.path.expanduser("~"))
        if path:
            self._provider_draft = None
            self._save(lambda: setattr(self.store, "device_remote_provider", path))
            self._rebuild_remote()

    def _provider_summary(self) -> tuple[str, str | None]:
        p = self.store.device_remote_provider.strip()
        if not p:
            return "Blank = no remote pool: agents get this machine's devices only.", None
        path = os.path.expanduser(p)
        if not os.path.exists(path):
            return f"⚠ No file at {path}.", _ORANGE
        return "Tried before local devices. The allocator re-reads this on every request.", None

    def _refresh_provider(self) -> None:
        stored = self.store.device_remote_provider
        if self._provider_draft is None and self.provider_field.text() != stored:
            self.provider_field.setText(stored)
        draft = self.provider_field.text()
        self.provider_clear.setVisible(bool(draft))
        self.provider_apply.setEnabled(self._provider_draft is not None
                                       and self._provider_draft != stored)
        summary, tint = self._provider_summary()
        self._provider_row.set_summary(summary, color=tint)

    def _rebuild_remote(self) -> None:
        state = self.store.device_state
        remote = self._remote()
        self._remote_pill.set_state(*remote_pill(remote, snapshot=state is not None))
        self._refresh_provider()

        _clear(self._status_col)
        if state is None:
            self._status_col.addWidget(_note(
                "No snapshot from the allocator yet, so no remote status either."))
        elif remote is not None:
            self._status_col.addWidget(_remote_status_view(remote))
        elif self.store.device_remote_provider.strip():
            note, action = predates_pool(self.store.allocator_install)
            self._status_col.addWidget(_note(note, _ORANGE))
            if action is not None:
                row = QHBoxLayout()
                row.addWidget(restart_button(self.store) if action == "restart"
                              else start_button(self.store))
                row.addStretch(1)
                self._status_col.addLayout(row)
        else:
            self._status_col.addWidget(_note("No remote pool configured."))

        # Whether the daemon has a pool to call: one it reports, and not off.
        callable_ = remote is not None and remote.state != "off"
        why = "" if callable_ else " (no remote pool is on)"
        self.sign_in.setEnabled(callable_ and not self.store.remote_sign_in.running)
        self.sign_in.setToolTip(f"Ask the pool for a browser sign-in{why}")
        self.list_pool.setEnabled(callable_ and not self.store.remote_listing.running)
        self.list_pool.setToolTip(f"What the pool lends now. Never prompts a sign-in{why}")
        _clear(self._calls_col)
        for call, label, running in (
                (self.store.remote_sign_in, "Sign-in",
                 "Waiting for the browser sign-in (up to 10 min)…"),
                (self.store.remote_listing, "Pool", "Asking the pool…")):
            line = _call_line(call, label, running)
            if line is not None:
                self._calls_col.addWidget(line)

        _clear(self._leases_col)
        leases = [d for d in (state or {}).get("devices", []) if da.is_remote(d)]
        if leases:
            head = _plain(f"LEASED HERE · {len(leases)}")
            head.setStyleSheet(muted(9, bold=True) + " letter-spacing: 0.5px;")
            self._leases_col.addWidget(head)
        for dev in leases:
            row = QHBoxLayout()
            row.setSpacing(6)
            glyph, tint = _platform(dev)
            row.addWidget(GlyphLabel(glyph, 12, tint, font_px=9))
            name = _plain(dev.get("name") or dev.get("key", ""))
            name.setStyleSheet("font-size: 10px;")
            row.addWidget(name)
            handle = _plain(dev.get("handle") or "attaching")
            handle.setStyleSheet(muted(9, mono=True))
            row.addWidget(handle)
            row.addStretch(1)
            who = (dev.get("owner") or {}).get("agentName")
            if who:
                agent = _plain(who)
                agent.setStyleSheet(muted(9))
                row.addWidget(agent)
            row.addWidget(_badge(dev))
            holder = QWidget()
            holder.setLayout(row)
            holder.layout().setContentsMargins(0, 0, 0, 0)
            holder.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
            self._leases_col.addWidget(holder)
