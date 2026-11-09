"""The Machines screen, twin of the macOS one: the remote-machines MCP's status, one
panel per machine it can offload work to (best first, as agents are told to pick
them), and the controls that take a machine out of that pool.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from datetime import datetime, timezone

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from diplomat_runtime import activity
from diplomat_runtime.models import Fmt
from . import deviceallocator as da
from . import glyphs
from . import remotemachines as rm
from .devicesview import AllocatorControls, Installer, allocator_pill
from .store import Store
from .telemetryview import SplitBar, legend, qcolor
from .widgets import ActivityRow, GlyphLabel, Pill, SettingRow, muted, settings_card, tint_bg

_TEAL, _BLUE, _PURPLE = "#30B0C7", "#0A84FF", "#AF52DE"
_GREEN, _ORANGE, _RED, _GREY = rm.GREEN, rm.AMBER, rm.RED, rm.GREY
_CPU, _RAM, _SSH = _BLUE, _PURPLE, _TEAL

#: The pause lengths the Pause menu offers.
PAUSES = (("1 hour", 3600), ("4 hours", 4 * 3600), ("24 hours", 24 * 3600))

OLD_DAEMON_NOTE = ("The daemon predates this screen; restart it to read usage and apply "
                   "exclusions.")

IDLE_NOTE = ("Idle, which is no fault: the daemon exits 10 min after its last agent "
             "session and starts with the next one. Refresh starts it now.")

#: The feed's remote-machines lines.
EVENT_ACTIONS = frozenset({
    "machines-install", "machines-uninstall", "machines-scan", "machine-pause",
    "machine-exclude", "machine-resume", "machine-include",
})


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            _clear(item.layout())


def _plain(text: str = "", style: str = "") -> QLabel:
    """Host names, aliases, warnings and errors come from the daemon, and an mDNS
    peer names itself: never read any of it as HTML."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    if style:
        label.setStyleSheet(style)
    return label


def _note(text: str, tint: str | None = None) -> QLabel:
    label = _plain(text, muted(10) if tint is None else f"color: {tint}; font-size: 10px;")
    label.setWordWrap(True)
    return label


def _clock(epoch: float) -> str:
    return Fmt.clock(datetime.fromtimestamp(epoch, tz=timezone.utc))


def _chip(text: str, tint: str) -> QLabel:
    return _plain(text, f"color: {tint}; background-color: {tint_bg(tint, 0.14)};"
                        " border-radius: 7px; padding: 1px 6px; font-size: 9px;"
                        " font-weight: 700;")


def machines_ready() -> bool:
    return rm.package_available() and da.resolve_node() is not None


def machines_summary() -> tuple[str, str | None]:
    if not rm.package_available():
        return (f"⚠ Package not found at {rm.package_dir()}. Set "
                "DIPLOMAT_REMOTE_MACHINES_DIR to point at it.", _ORANGE)
    if da.resolve_node() is None:
        return ("⚠ Node.js not found. Install Node (or set DIPLOMAT_NODE): the MCP "
                "server and its daemon need it to run.", _ORANGE)
    return ("So agents run builds and tests on the least loaded machine they can "
            "reach over SSH.", None)


RESTART_HELP = ("Stop the running daemon and start this checkout's. Its table is "
                "rebuilt by the new daemon's first scan.")


def machines_installer(store: Store) -> Installer:
    return Installer(
        ready=machines_ready, busy=lambda: store.machines_busy,
        install=store.install_machines_async, uninstall=store.uninstall_machines_async,
        start=store.start_machines_daemon_async,
        restart=store.restart_machines_daemon_async,
        recheck=store.refresh_machines_install_async, restart_help=RESTART_HELP)


def machine_events(entries: list) -> list:
    return [e for e in entries if e.action in EVENT_ACTIONS][:6]


def counts(state: rm.Snapshot) -> str:
    paused = sum(1 for m in state.excluded if not m.excluded_for_good)
    parts = [f"{len(state.machines)} in pool"]
    if paused:
        parts.append(f"{paused} paused")
    if len(state.excluded) > paused:
        parts.append(f"{len(state.excluded) - paused} excluded")
    return " · ".join(parts)


# MARK: - the history graph

class UsageChart(QWidget):
    """One machine's last day: CPU and RAM on a 0-100 % axis, SSH sessions as a step
    line on their own axis, each line broken wherever the daemon was not reading."""

    def __init__(self, rows: tuple[rm.Reading, ...], interval_ms: float,
                 now: float) -> None:
        super().__init__()
        self.setFixedHeight(90)
        self._rows = rows
        self._interval_ms = interval_ms
        self._now = now

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        f = painter.font()
        f.setPixelSize(8)
        painter.setFont(f)
        pad_l, pad_r, pad_t, pad_b = 26.0, 16.0, 6.0, 13.0
        w = self.width() - pad_l - pad_r
        h = self.height() - pad_t - pad_b
        if not self._rows:
            painter.setPen(QColor(glyphs.MUTED))
            painter.drawText(QRectF(self.rect()), int(Qt.AlignmentFlag.AlignCenter),
                             "no readings yet")
            painter.end()
            return
        start = rm.chart_start(self._rows, self._now)
        span = self._now - start
        runs = rm.chart_runs(self._rows, start=start, end=self._now, columns=int(w),
                             interval_ms=self._interval_ms)
        top_ssh = max([1.0] + [r[3] for run in runs for r in run if r[3] is not None])

        def x_of(at: float) -> float:
            return pad_l + w * min(1.0, max(0.0, (at - start) / span))

        def y_pct(v: float) -> float:
            return pad_t + h * (1.0 - min(1.0, max(0.0, v)))

        def y_ssh(v: float) -> float:
            return pad_t + h * (1.0 - v / top_ssh)

        for frac in (0.0, 0.5, 1.0):
            pen = QPen(qcolor(glyphs.MUTED, 0.18))
            pen.setWidthF(1.0)
            painter.setPen(pen)
            painter.drawLine(int(pad_l), int(y_pct(frac)), int(pad_l + w), int(y_pct(frac)))

        for index, color, y_of, step in ((3, _SSH, y_ssh, True), (2, _RAM, y_pct, False),
                                         (1, _CPU, y_pct, False)):
            pen = QPen(QColor(color))
            pen.setWidthF(1.2 if step else 1.6)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            for run in runs:
                path = QPainterPath()
                prev: tuple[float, float] | None = None
                for row in run:
                    if row[index] is None:
                        prev = None
                        continue
                    px, py = x_of(row[0]), y_of(row[index])
                    if prev is None:
                        path.moveTo(px, py)
                        # A lone reading draws as a tick, not as nothing.
                        path.lineTo(px + 1.0, py)
                    else:
                        if step:
                            path.lineTo(px, prev[1])
                        path.lineTo(px, py)
                    prev = (px, py)
                painter.drawPath(path)

        painter.setPen(QColor(glyphs.MUTED))
        left = int(Qt.AlignmentFlag.AlignLeft)
        right = int(Qt.AlignmentFlag.AlignRight)
        painter.drawText(QRectF(0, pad_t - 5, pad_l - 3, 10), right, "100%")
        painter.drawText(QRectF(0, pad_t + h - 5, pad_l - 3, 10), right, "0%")
        painter.drawText(QRectF(pad_l + w + 3, pad_t - 5, pad_r, 10), left,
                         rm.number(top_ssh))
        painter.drawText(QRectF(pad_l + w + 3, pad_t + h - 5, pad_r, 10), left, "0")
        painter.drawText(QRectF(pad_l, pad_t + h + 2, 90, 11), left,
                         f"{Fmt.duration(span)} ago")
        painter.drawText(QRectF(pad_l + w - 90, pad_t + h + 2, 90, 11), right, "now")
        painter.end()


# MARK: - one machine

class MachinePanel(QWidget):
    """Everything about one machine. ``rank`` is its place in the pool, None out of it;
    ``local`` is this machine, which is never offloaded to and has no actions."""

    def __init__(self, m: rm.Machine, *, rank: int | None, local: bool,
                 history: tuple[rm.Reading, ...], interval_ms: float, now: float,
                 store: Store, ages: list) -> None:
        super().__init__()
        self.setObjectName("machinePanel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet("#machinePanel { background-color: rgba(128,128,128,0.08);"
                           " border: 1px solid rgba(128,128,128,0.18); border-radius: 8px; }")
        self.machine = m
        col = QVBoxLayout(self)
        col.setContentsMargins(9, 8, 9, 8)
        col.setSpacing(5)
        col.addLayout(self._title(m, rank, local))
        line = rm.specs(m)
        if line:
            col.addWidget(_plain(line, muted(9)))
        if local:
            col.addWidget(_plain("Never offloaded to: the other machines are measured "
                                 "against it.", muted(9)))
        if m.status not in ("ok", "excluded"):
            col.addWidget(_note(f"⚠ {m.status}: {m.error or 'not read'}", _ORANGE))
        col.addLayout(self._bars(m))
        col.addLayout(self._detail(m, ages))
        for w in m.warnings:
            col.addWidget(_note(f"⚠ {w}", _ORANGE))
        col.addWidget(UsageChart(history, interval_ms, now))
        col.addLayout(legend([(_CPU, "CPU"), (_RAM, "RAM"), (_SSH, "SSH")]))
        self.actions: dict[str, QPushButton] = {}
        if not local:
            col.addLayout(self._actions(m, store))

    def _title(self, m: rm.Machine, rank: int | None, local: bool) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        if rank is not None:
            row.addWidget(_chip(f"#{rank}", _BLUE))
        name = _plain("This machine" if local else (m.target or m.id),
                      "font-size: 12px; font-weight: 700;")
        row.addWidget(name)
        secondary = m.machine_name if local else (
            f"also {', '.join(m.aliases)}" if m.aliases else None)
        if secondary:
            row.addWidget(_plain(secondary, muted(9)))
        if not local and rm.address(m):
            row.addWidget(_plain(rm.address(m), muted(9, mono=True)))
        row.addStretch(1)
        if m.paused_until is not None:
            row.addWidget(_chip(f"Paused until {_clock(m.paused_until)}", _ORANGE))
        elif m.excluded_for_good:
            row.addWidget(_chip("Excluded", _GREY))
        if m.warnings:
            n = len(m.warnings)
            row.addWidget(_chip(f"{n} warning{'' if n == 1 else 's'}", _ORANGE))
        return row

    def _bars(self, m: rm.Machine) -> QGridLayout:
        grid = QGridLayout()
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(4)
        for line, (label, value, warn_above, text) in enumerate((
                ("CPU", m.usage_cpu, 1.0, rm.cpu_text(m)),
                ("RAM", m.usage_memory, 0.9, rm.memory_text(m)))):
            grid.addWidget(_plain(label, muted(9, bold=True)), line, 0)
            if value is None:
                grid.addWidget(SplitBar(0, 1, _GREY, _GREY, height=10), line, 1)
                grid.addWidget(_plain("—", muted(10, mono=True)), line, 2)
                why = "not read while excluded" if m.excluded_for_good else "not read"
                grid.addWidget(_plain(why, muted(9)), line, 3)
                continue
            tint = rm.usage_color(value, warn_above)
            fill = min(1.0, max(0.0, value))
            grid.addWidget(SplitBar(fill, 1 - fill, tint, _GREY, height=10), line, 1)
            pct = _plain(rm.percent(value),
                         f"color: {tint}; font-size: 10px; font-weight: 700;"
                         " font-family: monospace;")
            pct.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            pct.setMinimumWidth(36)
            grid.addWidget(pct, line, 2)
            grid.addWidget(_plain(text, muted(9)), line, 3)
        grid.setColumnStretch(1, 3)
        grid.setColumnStretch(3, 2)
        return grid

    def _detail(self, m: rm.Machine, ages: list) -> QHBoxLayout:
        row = QHBoxLayout()
        parts = []
        if m.ssh_sessions is not None:
            parts.append(f"SSH sessions {rm.number(m.ssh_sessions)}")
        if m.disk_free is not None:
            parts.append(f"{rm.gb(m.disk_free)} disk free")
        head = " · ".join(parts)
        label = _plain("", muted(9))
        probed = m.probed_at

        def text(now: float) -> str:
            read = f"read {rm.ago(now - probed)}" if probed is not None else "never read"
            return f"{head} · {read}" if head else read

        ages.append((label.setText, text))
        row.addWidget(label)
        row.addStretch(1)
        return row

    def _actions(self, m: rm.Machine, store: Store) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addStretch(1)
        if m.excluded_for_good:
            self._action(row, "Include", _GREEN, lambda: store.include_machine_async(m),
                         "Put it back in the pool: it is read again within seconds.")
            return row
        if m.paused_until is not None:
            self._action(row, "Resume", _GREEN, lambda: store.resume_machine_async(m),
                         "End the pause: agents may offload to it again.")
        else:
            pause = self._action(row, "Pause ▾", _ORANGE, None,
                                 "Take it out of the pool for a while. It is still read.")
            menu = QMenu(pause)
            for label, seconds in PAUSES:
                menu.addAction(label).triggered.connect(
                    lambda _=False, s=seconds: store.pause_machine_async(m, s))
            pause.setMenu(menu)
        self._action(row, "Exclude", _RED, lambda: store.exclude_machine_async(m),
                     "Take it out of the pool for good: it is not contacted at all.")
        return row

    def _action(self, layout: QHBoxLayout, title: str, tint: str, slot,
                tip: str) -> QPushButton:
        btn = QPushButton(title)
        btn.setToolTip(tip)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(
            f"QPushButton {{ color: {tint}; background-color: {tint_bg(tint, 0.14)};"
            " border: none; border-radius: 5px; padding: 2px 9px; font-size: 9px;"
            " font-weight: 700; }"
            f"QPushButton:hover {{ background-color: {tint_bg(tint, 0.26)}; }}"
            " QPushButton::menu-indicator { width: 0px; }")
        if slot is not None:
            btn.clicked.connect(slot)
        layout.addWidget(btn)
        self.actions[title] = btn
        return btn


# MARK: - the screen

class MachinesView(QWidget):
    """``live`` polls the snapshot and the install while the screen is up; off in a
    render, whose seeded state a real read would overwrite. ``clock`` is what every
    age and the graphs measure against; a render pins it."""

    done = Signal()

    def __init__(self, store: Store, *, live: bool | None = None) -> None:
        super().__init__()
        self.store = store
        self.live = "DIPLOMAT_RENDER" not in os.environ if live is None else live
        self.clock: Callable[[], float] = time.time
        #: Text that says how long ago something was, as (its setter, text(now)): the
        #: status card's, and the panels'.
        self._ages: list[tuple[Callable[[str], None], Callable[[float], str]]] = []
        self._panel_ages: list[tuple[Callable[[str], None], Callable[[float], str]]] = []
        self._drawn_running: bool | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)
        root.addLayout(self._build_header())

        body = QHBoxLayout()
        body.setSpacing(12)
        left = QVBoxLayout()
        card, self._panels_col, self._pool_pill = settings_card(
            glyphs.G_MACHINES, "MACHINES", _TEAL)
        self._panels_col.setSpacing(8)
        left.addWidget(card)
        left.addStretch(1)
        right = QVBoxLayout()
        right.setSpacing(10)
        right.addWidget(self._build_status_card())
        events_card, self._events_col, _ = settings_card(glyphs.G_RECENT, "RECENT", _GREY)
        right.addWidget(events_card)
        right.addStretch(1)
        body.addLayout(left, 3)
        body.addLayout(right, 2)
        root.addLayout(body)
        root.addStretch(1)

        self._state_timer = QTimer(self)
        self._state_timer.setInterval(2_000)
        self._state_timer.timeout.connect(self._tick)
        # `daemonRunning` comes from `--check` alone.
        self._install_timer = QTimer(self)
        self._install_timer.setInterval(15_000)
        self._install_timer.timeout.connect(self.store.refresh_machines_install_async)

        store.machines_changed.connect(self.rebuild)
        store.machines_install_changed.connect(self._on_install_changed)
        store.activity_changed.connect(self._rebuild_events)
        self.rebuild()

    def opened(self) -> None:
        if self.live:
            self.store.refresh_machines_state()
            self.store.refresh_machines_install_async()
            self._state_timer.start()
            self._install_timer.start()
        self.rebuild()

    def closed(self) -> None:
        self._state_timer.stop()
        self._install_timer.stop()

    def _tick(self) -> None:
        self.store.refresh_machines_state()
        self._refresh_ages()

    def _refresh_ages(self) -> None:
        now = self.clock()
        for set_text, text in self._ages + self._panel_ages:
            set_text(text(now))

    def _on_install_changed(self) -> None:
        self._refresh_status()
        # The panels say whether the snapshot is the last an idle daemon left.
        if self._running() != self._drawn_running:
            self._rebuild_panels()

    def rebuild(self) -> None:
        self._rebuild_panels()
        self._rebuild_events()
        self._refresh_status()

    def _running(self) -> bool | None:
        s = self.store.machines_install
        return None if s is None else bool(s.get("daemonRunning"))

    # MARK: header

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(6)
        row.addWidget(GlyphLabel(glyphs.G_MACHINES, 16, glyphs.MUTED, font_px=14))
        row.addWidget(_plain("Machines", "font-weight: 700; font-size: 13px;"))
        self._counts = _plain("", muted(10, mono=True))
        row.addWidget(self._counts)
        row.addStretch(1)
        self._dot = _plain("●")
        row.addWidget(self._dot)
        self._light = _plain("", muted(9))
        row.addWidget(self._light)
        done = QPushButton("Done")
        done.setStyleSheet("font-weight: 700;")
        done.clicked.connect(self.done.emit)
        row.addWidget(done)
        return row

    def _daemon_light(self) -> tuple[str, str]:
        s = self.store.machines_install
        if s is None:
            return _GREY, "checking"
        if s.get("daemonRunning"):
            return _GREEN, "daemon running"
        return (_GREY, "daemon idle") if s.get("installed") else (_GREY, "not installed")

    def _refresh_header(self) -> None:
        state = self.store.machines_state
        self._counts.setText(counts(state) if state else "")
        self._counts.setVisible(state is not None)
        color, label = self._daemon_light()
        self._dot.setStyleSheet(f"color: {color}; font-size: 9px;")
        self._light.setText(label)

    # MARK: panels

    def _section(self, title: str, count: int | None = None) -> QLabel:
        text = title if count is None else f"{title} · {count}"
        return _plain(text, muted(9, bold=True) + " letter-spacing: 0.5px;")

    def _rebuild_panels(self) -> None:
        _clear(self._panels_col)
        self._panel_ages = []
        self._drawn_running = self._running()
        state = self.store.machines_state
        self._pool_pill.set_state(f"{len(state.machines)} in the pool" if state else "",
                                  _GREEN if state and state.machines else _GREY)
        if state is None:
            self._panels_col.addWidget(_note(
                "No snapshot yet: the remote-machines daemon has not run on this machine."))
            return
        if self._running() is False and state.updated_at is not None:
            stamp = state.updated_at
            note = _note("", _ORANGE)
            self._panel_ages.append((note.setText, lambda now: (
                "The daemon is idle, so this is its last snapshot, as of "
                f"{rm.ago(now - stamp)}.")))
            self._panels_col.addWidget(note)
        history = self.store.machines_history
        interval = history.interval_ms if history else 60_000
        now = self.clock()

        def panel(m: rm.Machine, rank: int | None, local: bool = False) -> MachinePanel:
            rows = history.machines.get(m.id, ()) if history else ()
            return MachinePanel(m, rank=rank, local=local, history=rows,
                                interval_ms=interval, now=now, store=self.store,
                                ages=self._panel_ages)

        self._panels_col.addWidget(self._section("IN THE POOL", len(state.machines)))
        if not state.machines:
            self._panels_col.addWidget(_note(
                "No remote machine is usable right now, so agents run their work here."))
        for rank, m in enumerate(state.machines, 1):
            self._panels_col.addWidget(panel(m, rank))
        if state.excluded:
            self._panels_col.addWidget(self._section("OUT OF THE POOL", len(state.excluded)))
            for m in state.excluded:
                self._panels_col.addWidget(panel(m, None))
        if state.local is not None:
            self._panels_col.addWidget(self._section("THIS MACHINE"))
            self._panels_col.addWidget(panel(state.local, None, local=True))
        line = rm.unavailable_line(state.unavailable)
        if line:
            self._panels_col.addWidget(_note(line))
        self._refresh_ages()

    def _rebuild_events(self) -> None:
        _clear(self._events_col)
        events = machine_events(self.store.audit_entries)
        if not events:
            self._events_col.addWidget(_note("No remote-machines events in the activity feed."))
        for e in events:
            self._events_col.addWidget(ActivityRow(
                glyph=activity.glyph_for(e.action),
                glyph_color=activity.color_for(e.action),
                detail=e.detail, source=e.source,
                source_color=activity.source_color(e.source),
                clock=Fmt.clock(e.date)))

    # MARK: status

    def _build_status_card(self) -> QWidget:
        card, body, self._install_pill = settings_card(
            glyphs.G_MACHINES, "REMOTE MACHINES", _TEAL)
        self._daemon_pill = Pill("")
        self._daemon_row = SettingRow("Daemon", self._daemon_pill)
        body.addWidget(self._daemon_row)
        self._old_daemon = _note(OLD_DAEMON_NOTE, _ORANGE)
        body.addWidget(self._old_daemon)

        facts = QGridLayout()
        facts.setHorizontalSpacing(10)
        facts.setVerticalSpacing(3)
        self._facts: dict[str, QLabel] = {}
        for line, key in enumerate(("Agent sessions", "Last scan", "Last discovery",
                                    "Probe interval")):
            facts.addWidget(_plain(key, muted(10)), line, 0)
            value = _plain("", "font-size: 10px;")
            facts.addWidget(value, line, 1)
            self._facts[key] = value
        facts.setColumnStretch(1, 1)
        body.addLayout(facts)

        self.refresh = QPushButton("Refresh")
        self.refresh.setStyleSheet("font-weight: 700;")
        self.refresh.clicked.connect(self.store.refresh_machines_async)
        refresh_row = QHBoxLayout()
        refresh_row.addWidget(self.refresh)
        self._scan_note = _plain("", muted(9))
        refresh_row.addWidget(self._scan_note, 1)
        body.addLayout(refresh_row)

        self.controls = AllocatorControls(self.store, offers_daemon=True,
                                          installer=machines_installer(self.store))
        summary, tint = machines_summary()
        install = SettingRow("Offload work to other machines", self.controls, stacked=True)
        install.set_summary(summary, color=tint)
        body.addWidget(install)
        return card

    def _refresh_status(self) -> None:
        s = self.store.machines_install
        state = self.store.machines_state
        self._install_pill.set_state(*allocator_pill(s))
        color, label = self._daemon_light()
        running = self._running()
        mark = "⚡ " if running else "☾ " if s and s.get("installed") else ""
        self._daemon_pill.set_state(mark + label, color)
        daemon = state.daemon if state else None
        self._old_daemon.setVisible(state is not None and daemon is None)
        self._ages = []
        if running and daemon is not None:
            pid = f"pid {daemon.pid}" if daemon.pid is not None else "running"
            started = daemon.started_at

            def summary(now: float) -> str:
                return (f"{pid} · started {rm.ago(now - started)}" if started is not None
                        else pid)
            self._ages.append((self._daemon_row.set_summary, summary))
        elif running:
            self._daemon_row.set_summary("Running; its first snapshot is on the way.")
        elif s is None:
            self._daemon_row.set_summary("Asking the installer…")
        elif s.get("installed"):
            self._daemon_row.set_summary(IDLE_NOTE)
        else:
            self._daemon_row.set_summary("Install it to run it.")

        sessions = daemon.sessions if daemon and running else None
        self._facts["Agent sessions"].setText("—" if sessions is None else str(sessions))
        for key, at in (("Last scan", state and state.updated_at),
                        ("Last discovery", state and state.discovered_at)):
            if at:
                self._ages.append((self._facts[key].setText,
                                   lambda now, at=at: rm.ago(now - at)))
            else:
                self._facts[key].setText("never")
        interval = state.probe_interval_ms if state else None
        self._facts["Probe interval"].setText(
            "—" if interval is None else f"{rm.number(interval / 1000)} s")

        scanning = self.store.machines_scanning
        self.refresh.setText("Scanning…" if scanning else "Refresh")
        reachable = bool(s and (s.get("installed") or running))
        self.refresh.setEnabled(reachable and machines_ready() and not self.store.machines_busy)
        self.refresh.setToolTip(
            "Rediscover the machines and read every one now" if running
            else "Start the daemon, which reads every machine as it starts")
        self._scan_note.setText("Reading every machine…" if scanning else "")
        self.controls.refresh(s)
        self._refresh_header()
        self._refresh_ages()
