"""The popup panel: header, reverse-lookup search, tool grid, results pane.

The Linux analogue of ContentView.swift, rendered as a frameless top-level
window shown from the tray. Persistent inputs (search, wizard, settings) are
built once; only data-dependent areas (grid counts, results list) are rebuilt
when the Store changes, so typing is never interrupted by a refresh.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QUrl

import time

from . import activity, bans, core, glyphs
from .models import Fmt
from .settingsview import SettingsView
from .store import Store, tool_by_id
from .widgets import (
    ActionCard,
    ActivityRow,
    BanRow,
    GlyphLabel,
    IconChip,
    ResultRow,
    SectionHeader,
    ToolCard,
    hline,
    tint_bg,
)
from .conflictwizardview import ConflictWizardView
from .auditwizardview import AuditWizardView
from .wizardview import WizardView

_REVIEW_TINT = "#FF2D78"
_CONFLICT_TINT = "#32ADE6"
_AUDIT_TINT = "#5856D6"


def _clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)  # remove from display synchronously …
            w.deleteLater()  # … then free it on the next loop turn
        elif item.layout() is not None:
            _clear_layout(item.layout())


def _icon_button(glyph: str, tooltip: str) -> QToolButton:
    btn = QToolButton()
    btn.setText(glyph)
    btn.setToolTip(tooltip)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.setStyleSheet(
        "QToolButton { border: none; font-size: 14px; padding: 2px 4px; }"
        "QToolButton:hover { color: palette(highlight); }"
    )
    return btn


def _device_badge(dev: dict, allocated: bool) -> tuple[str, str]:
    status = dev.get("status", "free")
    if status == "ready":
        return ("in use", "#34C759") if allocated else ("free", "gray")
    if status == "booting":
        return ("booting", "#FF9500")
    if status == "repairing":
        return ("repairing", "#AF52DE")
    if status == "error":
        return ("error", "#FF3B30")
    return ("free", "gray")


def _device_detail(dev: dict, allocated: bool) -> str:
    if dev.get("status") == "repairing":
        reason = dev.get("brokenReason")
        return f"repair: {reason}" if reason else "repair dispatched"
    owner = dev.get("owner") or {}
    if allocated and owner.get("agentName"):
        parts = [owner["agentName"]]
        started = dev.get("allocatedAt")
        if started:
            parts.append(f"held {Fmt.duration(time.time() - started / 1000)}")
        idle = dev.get("idleMs")
        if idle and idle > 60000:
            parts.append(f"idle {int(idle / 60000)}m")
        return " · ".join(parts)
    return dev.get("handle") or "available"


class Panel(QWidget):
    refresh_requested = Signal()
    quit_requested = Signal()

    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self._show_settings = False
        self._active_action: str | None = None  # None | "review" | "conflicts" | "audit"
        # Devices section: In use expanded, Free collapsed by default. Persisted on the
        # instance so a poll-driven rebuild doesn't reset the user's collapse choice.
        self._inuse_expanded = True
        self._free_expanded = False
        # Left-pane telemetry sections (both expanded by default).
        self._activity_expanded = True
        self._bans_expanded = True

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        # Two-pane layout (matching the macOS popover): a left telemetry column
        # (devices · activity · bans) beside the right interactive column (search ·
        # tool grid · results). Widened to give both panes room; height tracks the
        # screen's safe area (availableGeometry excludes the taskbar/panel).
        self.setFixedWidth(1080)
        self.setFixedHeight(self._screen_high())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        outer.addLayout(self._build_header())

        self.body = QStackedWidget()
        outer.addWidget(self.body, 1)

        # Page 0: main
        self.main_page = QWidget()
        self._build_main_page()
        self.body.addWidget(self.main_page)

        # Page 1: settings
        self.settings_view = SettingsView(store)
        self.settings_view.done.connect(self._close_settings)
        self.body.addWidget(self.settings_view)

        # Shortcuts
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_search)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self.refresh_requested.emit)

        store.changed.connect(self._on_data_changed)
        store.loading_changed.connect(self._on_loading)
        store.devices_changed.connect(self._rebuild_devices)
        store.activity_changed.connect(self._rebuild_telemetry)

        # Poll the device-allocator state file + the shared activity/ban files on a
        # light cadence (cheap file reads).
        self._device_timer = QTimer(self)
        self._device_timer.timeout.connect(self.store.refresh_device_state)
        self._device_timer.timeout.connect(self.store.refresh_activity)
        self._device_timer.start(8000)
        self.store.refresh_device_state()
        self.store.refresh_activity()

        # Advance the "held" durations on in-use devices even when the pool itself
        # hasn't changed (allocatedAt is fixed; the elapsed time is not).
        self._duration_timer = QTimer(self)
        self._duration_timer.timeout.connect(self._rebuild_devices)
        self._duration_timer.start(30000)

        self._rebuild_grid()
        self._rebuild_devices()
        self._rebuild_telemetry()
        self._update_results()

    @staticmethod
    def _screen_high() -> int:
        """Panel height: the primary screen's usable height (minus a small margin
        so it doesn't kiss the edges), floored so it's never uselessly short."""
        from PySide6.QtWidgets import QApplication

        screen = QApplication.primaryScreen()
        avail = screen.availableGeometry().height() if screen else 800
        return max(560, avail - 16)

    # MARK: header

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(GlyphLabel(glyphs.G_APP, 18, "#0A84FF", font_px=16))
        name = QLabel("Argent Utils")
        name.setStyleSheet("font-weight: 700; font-size: 14px;")
        row.addWidget(name)
        repo = QLabel(f"{core.config()['owner']}/{core.config()['repo']}")
        repo.setStyleSheet("color: palette(mid); font-size: 9px;")
        row.addWidget(repo)
        row.addStretch(1)

        self.spinner = QLabel("⟳")
        self.spinner.setStyleSheet("color: palette(mid); font-size: 12px;")
        self.spinner.setVisible(False)
        row.addWidget(self.spinner)

        self.updated = QLabel("upd —")
        self.updated.setStyleSheet("color: palette(mid); font-size: 9px;")
        row.addWidget(self.updated)

        refresh = _icon_button("⟲", "Refresh")
        refresh.clicked.connect(self.refresh_requested.emit)
        row.addWidget(refresh)

        self.settings_btn = _icon_button("⚙", "Settings")
        self.settings_btn.clicked.connect(self._toggle_settings)
        row.addWidget(self.settings_btn)

        quit_btn = _icon_button("⏻", "Quit")
        quit_btn.clicked.connect(self.quit_requested.emit)
        row.addWidget(quit_btn)
        return row

    # MARK: main page

    def _build_main_page(self) -> None:
        layout = QHBoxLayout(self.main_page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._build_left_pane(), 1)
        layout.addWidget(self._build_right_pane(), 1)

    def _build_left_pane(self) -> QWidget:
        """Telemetry column: device-allocator pool, activity feed, ban list. Each
        section is rebuilt in place from the shared ~/.argent files and hidden when
        empty. Wrapped in a scroll area so a busy feed scrolls within the pane."""
        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)

        # Device-allocator pool (the shared simulators/emulators + who holds what).
        # Rebuilt in place from the daemon's state file; hidden when the pool is empty.
        self.devices_host = QWidget()
        self.devices_col = QVBoxLayout(self.devices_host)
        self.devices_col.setContentsMargins(7, 7, 7, 7)
        self.devices_col.setSpacing(4)
        self.devices_host.setStyleSheet(
            "background-color: rgba(128,128,128,0.07); border-radius: 8px;"
        )
        self.devices_host.setVisible(False)
        col.addWidget(self.devices_host)

        # Activity feed — the shared audit.jsonl action log (panel + daemon + agents).
        self.activity_host = QWidget()
        self.activity_col = QVBoxLayout(self.activity_host)
        self.activity_col.setContentsMargins(7, 7, 7, 7)
        self.activity_col.setSpacing(4)
        self.activity_host.setStyleSheet(
            "background-color: rgba(128,128,128,0.07); border-radius: 8px;"
        )
        self.activity_host.setVisible(False)
        col.addWidget(self.activity_host)

        # Banned authors (prompt-injection blocklist; read-only here).
        self.bans_host = QWidget()
        self.bans_col = QVBoxLayout(self.bans_host)
        self.bans_col.setContentsMargins(7, 7, 7, 7)
        self.bans_col.setSpacing(4)
        self.bans_host.setStyleSheet(
            "background-color: rgba(255,59,48,0.06); border-radius: 8px;"
        )
        self.bans_host.setVisible(False)
        col.addWidget(self.bans_host)

        # Shown only while all three sections are empty, so the pane reads as
        # "nothing yet" rather than looking broken (the feed fills as you dispatch
        # actions; devices/bans appear when the daemon reports them).
        self.telemetry_empty = QLabel(
            "No devices, activity, or bans yet.\n"
            "Dispatch a review, conflict, or audit and it shows up here."
        )
        self.telemetry_empty.setWordWrap(True)
        self.telemetry_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.telemetry_empty.setStyleSheet(
            "color: palette(mid); font-size: 11px; padding: 24px 8px;"
        )
        col.addWidget(self.telemetry_empty)

        col.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(host)
        return scroll

    def _build_right_pane(self) -> QWidget:
        """Interactive column: reverse-lookup search, tool grid, and the
        results/wizard stack — the panel's original single-column content."""
        host = QWidget()
        layout = QVBoxLayout(host)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # Search (reverse lookup)
        search_box = QWidget()
        sb = QHBoxLayout(search_box)
        sb.setContentsMargins(6, 2, 6, 2)
        sb.setSpacing(6)
        sb.addWidget(GlyphLabel(glyphs.G_SEARCH, 16, "#9aa0a6", font_px=15))
        self.search = QLineEdit()
        self.search.setPlaceholderText("PR / issue #  (Ctrl+F)")
        self.search.setFrame(False)
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(lambda _: self._update_results())
        sb.addWidget(self.search, 1)
        search_box.setStyleSheet(
            "background-color: rgba(128,128,128,0.10); border-radius: 6px;"
        )
        layout.addWidget(search_box)

        # Error banner
        self.error_banner = QLabel("")
        self.error_banner.setWordWrap(True)
        self.error_banner.setStyleSheet(
            "background-color: rgba(220,40,40,0.85); color: white; border-radius: 6px;"
            " padding: 6px; font-size: 10px;"
        )
        self.error_banner.setVisible(False)
        layout.addWidget(self.error_banner)

        # Tool grid. Pin to its content height so the results stack (stretch=1)
        # below can't compress the rows into each other.
        self.grid_host = QWidget()
        self.grid_host.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum
        )
        self.grid = QGridLayout(self.grid_host)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(8)
        layout.addWidget(self.grid_host)

        layout.addWidget(hline())

        # Results stack
        self.results = QStackedWidget()
        layout.addWidget(self.results, 1)

        self.tool_results_scroll = QScrollArea()
        self.tool_results_scroll.setWidgetResizable(True)
        self.tool_results_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.results.addWidget(self.tool_results_scroll)  # index 0

        self.lookup_scroll = QScrollArea()
        self.lookup_scroll.setWidgetResizable(True)
        self.lookup_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.results.addWidget(self.lookup_scroll)  # index 1

        self.hint = QLabel("Type a PR or issue number.")
        self.hint.setStyleSheet("color: palette(mid); font-size: 11px;")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.results.addWidget(self.hint)  # index 2

        self.wizard = WizardView(self.store)
        wizard_scroll = QScrollArea()
        wizard_scroll.setWidgetResizable(True)
        wizard_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        wizard_scroll.setWidget(self.wizard)
        self.results.addWidget(wizard_scroll)  # index 3

        self.conflict_wizard = ConflictWizardView(self.store)
        conflict_scroll = QScrollArea()
        conflict_scroll.setWidgetResizable(True)
        conflict_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        conflict_scroll.setWidget(self.conflict_wizard)
        self.results.addWidget(conflict_scroll)  # index 4

        self.audit_wizard = AuditWizardView(self.store)
        audit_scroll = QScrollArea()
        audit_scroll.setWidgetResizable(True)
        audit_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        audit_scroll.setWidget(self.audit_wizard)
        self.results.addWidget(audit_scroll)  # index 5

        return host

    # MARK: grid

    def _rebuild_grid(self) -> None:
        _clear_layout(self.grid)
        loaded = self.store.has_loaded
        col = 0
        rowi = 0
        for tool in self.store.visible_tools:
            card = ToolCard(
                emoji=tool.glyph,
                title=tool.title,
                subtitle=tool.subtitle,
                hex_color=self.store.tint(tool.id),
                count=self.store.count(tool.id) if loaded else None,
                selected=(self.store.selected == tool.id and self._active_action is None),
            )
            card.clicked.connect(lambda tid=tool.id: self._select_tool(tid))
            self.grid.addWidget(card, rowi, col)
            col += 1
            if col == 2:
                col = 0
                rowi += 1

        review_card = ActionCard(
            emoji=glyphs.G_REVIEW,
            title="Review PRs",
            subtitle="spawn a review agent",
            hex_color=_REVIEW_TINT,
            selected=self._active_action == "review",
        )
        review_card.clicked.connect(lambda: self._open_action("review"))
        self.grid.addWidget(review_card, rowi, col)
        col += 1
        if col == 2:
            col = 0
            rowi += 1

        conflict_card = ActionCard(
            emoji=glyphs.G_CONFLICT,
            title="Resolve conflicts",
            subtitle="merge main, fix conflicts",
            hex_color=_CONFLICT_TINT,
            selected=self._active_action == "conflicts",
        )
        conflict_card.clicked.connect(lambda: self._open_action("conflicts"))
        self.grid.addWidget(conflict_card, rowi, col)
        col += 1
        if col == 2:
            col = 0
            rowi += 1

        audit_card = ActionCard(
            emoji=glyphs.G_AUDIT,
            title="Full E2E test",
            subtitle="swarm-test the whole repo",
            hex_color=_AUDIT_TINT,
            selected=self._active_action == "audit",
        )
        audit_card.clicked.connect(lambda: self._open_action("audit"))
        self.grid.addWidget(audit_card, rowi, col)

    # MARK: device-allocator pool

    def _rebuild_devices(self) -> None:
        _clear_layout(self.devices_col)
        state = self.store.device_state
        devices = (state or {}).get("devices", [])
        if not devices:
            self.devices_host.setVisible(False)
            self._update_telemetry_placeholder()
            return
        self.devices_host.setVisible(True)

        from . import deviceallocator

        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(GlyphLabel(glyphs.G_DEVICES, 14, "#9aa0a6", font_px=12))
        title = QLabel("Devices")
        title.setStyleSheet("color: palette(mid); font-weight: 700; font-size: 10px;")
        head.addWidget(title)
        head.addStretch(1)
        self.devices_col.addLayout(head)

        # Within a section: by platform, then name.
        def sort_key(d: dict):
            return (d.get("platform", ""), d.get("name") or "")

        in_use = sorted((d for d in devices if deviceallocator.is_allocated(d)), key=sort_key)
        free = sorted((d for d in devices if not deviceallocator.is_allocated(d)), key=sort_key)

        if in_use:
            self._device_section("In use", "#34C759", self._inuse_expanded,
                                 in_use, self._toggle_inuse)
        if free:
            self._device_section("Free", "gray", self._free_expanded,
                                 free, self._toggle_free)
        self._update_telemetry_placeholder()

    def _update_telemetry_placeholder(self) -> None:
        """Show the left-pane placeholder only when every telemetry section is empty."""
        any_visible = (
            not self.devices_host.isHidden()
            or not self.activity_host.isHidden()
            or not self.bans_host.isHidden()
        )
        self.telemetry_empty.setVisible(not any_visible)

    def _toggle_inuse(self) -> None:
        self._inuse_expanded = not self._inuse_expanded
        self._rebuild_devices()

    def _toggle_free(self) -> None:
        self._free_expanded = not self._free_expanded
        self._rebuild_devices()

    # MARK: activity feed + bans

    def _rebuild_telemetry(self) -> None:
        self._rebuild_activity()
        self._rebuild_bans()
        self._update_telemetry_placeholder()

    def _rebuild_activity(self) -> None:
        _clear_layout(self.activity_col)
        entries = self.store.audit_entries
        if not entries:
            self.activity_host.setVisible(False)
            return
        self.activity_host.setVisible(True)

        header = SectionHeader(glyph=glyphs.G_ACTIVITY, title="Activity",
                               count=len(entries),
                               expanded=self._activity_expanded)
        header.clicked.connect(self._toggle_activity)
        self.activity_col.addWidget(header)
        if self._activity_expanded:
            # Cap at 30 rows (matching macOS) — the feed grows forever.
            for e in entries[:30]:
                self.activity_col.addWidget(ActivityRow(
                    glyph=activity.glyph_for(e.action),
                    glyph_color=activity.color_for(e.action),
                    detail=e.detail,
                    source=e.source,
                    source_color=activity.source_color(e.source),
                    clock=Fmt.clock(e.date),
                ))

    def _toggle_activity(self) -> None:
        self._activity_expanded = not self._activity_expanded
        self._rebuild_activity()

    def _rebuild_bans(self) -> None:
        _clear_layout(self.bans_col)
        banned = self.store.banned_authors
        if not banned:
            self.bans_host.setVisible(False)
            return
        self.bans_host.setVisible(True)

        header = SectionHeader(glyph=glyphs.G_BAN, title="Banned",
                               count=len(banned), glyph_color="#FF3B30",
                               caption="prompt injection · no auto-reviews",
                               expanded=self._bans_expanded)
        header.clicked.connect(self._toggle_bans)
        self.bans_col.addWidget(header)
        if self._bans_expanded:
            for b in banned:
                self.bans_col.addWidget(BanRow(login=b.login, reason=b.reason))

    def _toggle_bans(self) -> None:
        self._bans_expanded = not self._bans_expanded
        self._rebuild_bans()

    def _device_section(self, title: str, color: str, expanded: bool,
                        devices: list[dict], toggle_slot) -> None:
        header = QToolButton()
        header.setText(f"{'▾' if expanded else '▸'}  {title.upper()}    {len(devices)}")
        header.setCursor(Qt.CursorShape.PointingHandCursor)
        header.setStyleSheet(
            "QToolButton { border: none; color: palette(mid); font-weight: 700;"
            " font-size: 9px; padding: 2px 0; text-align: left; }"
            "QToolButton:hover { color: palette(text); }"
        )
        header.clicked.connect(toggle_slot)
        self.devices_col.addWidget(header)
        if expanded:
            for dev in devices:
                self.devices_col.addWidget(self._device_row(dev))

    def _device_row(self, dev: dict) -> QWidget:
        from . import deviceallocator

        allocated = deviceallocator.is_allocated(dev)
        platform = dev.get("platform", "")
        glyph = glyphs.PLATFORM_GLYPH.get(platform, glyphs.G_PHONE)
        tint = {"ios": "#0A84FF", "apple-tv": "#0A84FF", "android": "#34C759",
                "android-tv": "#34C759", "vega": "#FF9500"}.get(platform, "#8E8E93")
        status = dev.get("status", "free")
        badge_text, badge_color = _device_badge(dev, allocated)

        row = QWidget()
        rl = QHBoxLayout(row)
        rl.setContentsMargins(6, 6, 6, 6)
        rl.setSpacing(8)

        rl.addWidget(IconChip(glyph, tint, 22, active=allocated))

        text = QVBoxLayout()
        text.setSpacing(1)
        name_row = QHBoxLayout()
        name_row.setSpacing(4)
        name = QLabel(dev.get("name") or dev.get("handle") or dev.get("key", "?"))
        name.setStyleSheet("font-size: 11px;")
        name_row.addWidget(name)
        if dev.get("version"):
            ver = QLabel(str(dev["version"]))
            ver.setStyleSheet("color: palette(mid); font-size: 9px;")
            name_row.addWidget(ver)
        if dev.get("format"):
            fmt = QLabel(str(dev["format"]))
            fmt.setStyleSheet("color: palette(mid); font-size: 9px;")
            name_row.addWidget(fmt)
        name_row.addStretch(1)
        text.addLayout(name_row)

        detail = QLabel(_device_detail(dev, allocated))
        detail.setStyleSheet(
            f"font-size: 9px; color: {'#AF52DE' if status == 'repairing' else (tint if allocated else 'palette(mid)')};"
        )
        text.addWidget(detail)
        rl.addLayout(text, 1)

        badge = QLabel(badge_text)
        badge.setStyleSheet(
            f"color: {badge_color}; font-weight: 700; font-size: 9px;"
            f" background-color: {tint_bg(badge_color, 0.14)}; border-radius: 7px;"
            " padding: 2px 6px;"
        )
        rl.addWidget(badge)

        row.setStyleSheet("background-color: rgba(128,128,128,0.06); border-radius: 6px;")
        return row

    # MARK: navigation

    def _select_tool(self, tool_id: str) -> None:
        self._active_action = None
        self.store.selected = tool_id
        self._rebuild_grid()
        self._update_results()

    def _open_action(self, name: str) -> None:
        self._active_action = name
        self._rebuild_grid()
        self._update_results()

    def _toggle_settings(self) -> None:
        self._show_settings = not self._show_settings
        self.body.setCurrentIndex(1 if self._show_settings else 0)

    def _close_settings(self) -> None:
        self._show_settings = False
        self.body.setCurrentIndex(0)
        self._rebuild_grid()
        self._update_results()

    def _focus_search(self) -> None:
        if self._show_settings:
            self._close_settings()
        self.search.setFocus()

    # MARK: results

    def _update_results(self) -> None:
        trimmed = self.search.text().strip()
        if self._active_action == "review":
            self.results.setCurrentIndex(3)
            return
        if self._active_action == "conflicts":
            self.results.setCurrentIndex(4)
            return
        if self._active_action == "audit":
            self.results.setCurrentIndex(5)
            return
        if trimmed and trimmed.isdigit():
            self._rebuild_lookup(int(trimmed))
            self.results.setCurrentIndex(1)
        elif trimmed:
            self.results.setCurrentIndex(2)
        else:
            self._rebuild_tool_results()
            self.results.setCurrentIndex(0)

    def _rebuild_tool_results(self) -> None:
        container = QWidget()
        col = QVBoxLayout(container)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)

        vis = self.store.visible_tools
        selected = self.store.selected
        if not any(t.id == selected for t in vis):
            selected = vis[0].id if vis else None

        if selected is None:
            empty = QLabel("All tools hidden — re-enable some under ⚙ Settings.")
            empty.setStyleSheet("color: palette(mid); font-size: 11px;")
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(empty)
            col.addStretch(1)
            self.tool_results_scroll.setWidget(container)
            return

        tool = tool_by_id(selected)
        tint = self.store.tint(selected)
        items = self.store.items_for(selected)

        header = QHBoxLayout()
        em = QLabel(tool.emoji)
        em.setStyleSheet("font-size: 13px;")
        header.addWidget(em)
        title = QLabel(tool.title)
        title.setStyleSheet("font-weight: 700; font-size: 12px;")
        header.addWidget(title)
        cnt = QLabel(str(len(items)))
        cnt.setStyleSheet("color: palette(mid); font-family: monospace; font-size: 10px;")
        header.addWidget(cnt)
        header.addStretch(1)
        col.addLayout(header)

        if not items:
            msg = "Loading…" if self.store.is_loading else "Nothing here."
            empty = QLabel(msg)
            empty.setStyleSheet("color: palette(mid); font-size: 11px;")
            col.addWidget(empty)
        else:
            for it in items:
                row = ResultRow(
                    badge=it.badge,
                    title=it.title,
                    line2=it.line2,
                    line3=it.line3,
                    hex_color=tint,
                )
                row.clicked.connect(lambda url=it.url: QDesktopServices.openUrl(QUrl(url)))
                col.addWidget(row)
        col.addStretch(1)
        self.tool_results_scroll.setWidget(container)

    def _rebuild_lookup(self, n: int) -> None:
        r = self.store.lookup(n)
        cfg = core.config()
        link = r.url or f"https://github.com/{cfg['owner']}/{cfg['repo']}/issues/{n}"

        container = QWidget()
        col = QVBoxLayout(container)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)

        top = QHBoxLayout()
        badge = QLabel(f"#{n}")
        badge.setStyleSheet("font-weight: 700; font-family: monospace; font-size: 15px;")
        top.addWidget(badge)
        on = r.is_on_any_list
        status = QLabel(
            f"on {len(r.on_lists)} list{'' if len(r.on_lists) == 1 else 's'}"
            if on else "on no list"
        )
        status.setStyleSheet(
            f"font-weight: 700; font-size: 10px; color: {'#34C759' if on else 'gray'};"
        )
        top.addWidget(status)
        top.addStretch(1)
        open_btn = _icon_button("↗", f"Open #{n} on GitHub")
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(link)))
        top.addWidget(open_btn)
        col.addLayout(top)

        presence = QLabel(r.presence)
        presence.setStyleSheet("color: palette(mid); font-size: 10px;")
        col.addWidget(presence)

        for tool in self.store.visible_tools:
            is_on = tool.id in r.on_lists
            tint = self.store.tint(tool.id)
            roww = QWidget()
            rl = QHBoxLayout(roww)
            rl.setContentsMargins(7, 7, 7, 7)
            rl.setSpacing(8)
            chip = QLabel(tool.emoji)
            chip.setFixedSize(22, 22)
            chip.setAlignment(Qt.AlignmentFlag.AlignCenter)
            chip.setStyleSheet(
                f"background-color: {tint if is_on else 'rgba(128,128,128,0.35)'};"
                f" border-radius: 5px; font-size: 11px;"
            )
            rl.addWidget(chip)
            name = QLabel(tool.title)
            name.setStyleSheet(
                f"font-size: 11px; color: {'palette(text)' if is_on else 'palette(mid)'};"
            )
            rl.addWidget(name)
            rl.addStretch(1)
            mark = QLabel("✓" if is_on else "—")
            mark.setStyleSheet(f"color: {tint if is_on else 'gray'}; font-weight: 700;")
            rl.addWidget(mark)
            bg = tint_bg(tint, 0.12) if is_on else "rgba(128,128,128,0.05)"
            roww.setStyleSheet(f"background-color: {bg}; border-radius: 6px;")
            col.addWidget(roww)

        col.addStretch(1)
        self.lookup_scroll.setWidget(container)

    # MARK: store reactions

    def _on_data_changed(self) -> None:
        self.updated.setText(f"upd {Fmt.clock(self.store.last_updated)}")
        self.error_banner.setVisible(bool(self.store.error))
        if self.store.error:
            self.error_banner.setText(self.store.error)
        self._rebuild_grid()
        self.wizard.refresh_identity()
        self.conflict_wizard.refresh_identity()
        self.audit_wizard.refresh_identity()
        if not self._show_settings:
            self._update_results()

    def _on_loading(self, loading: bool) -> None:
        self.spinner.setVisible(loading)

    # MARK: window behaviour

    def event(self, event) -> bool:  # noqa: N802
        # Transient dismissal, matching the macOS MenuBarExtra(.window): hide when
        # the user clicks/focuses outside the whole panel. We only act when focus
        # has left this application entirely (activeWindow() is None). Our own child
        # popups — a QComboBox dropdown, the tray context menu, the Quit dialog —
        # either don't deactivate the panel at all (popups) or leave activeWindow()
        # pointing at an app-owned window, so an inside interaction never hides us.
        if event.type() == QEvent.Type.WindowDeactivate:
            from PySide6.QtWidgets import QApplication

            if QApplication.activeWindow() is None:
                self.hide()
        return super().event(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            if self._show_settings:
                self._close_settings()
            else:
                self.hide()
            return
        super().keyPressEvent(event)
