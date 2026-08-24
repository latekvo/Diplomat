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

from diplomat_runtime import activity, autofix, core
from diplomat_runtime.models import Fmt
from . import glyphs, szpont
from .settingsview import SettingsView
from .store import Store, tool_by_id
from .widgets import (
    ActionCard,
    ActivityRow,
    BanRow,
    FreeSlotRow,
    GlyphLabel,
    IconChip,
    QueueAutoRunRow,
    QueuedTaskRow,
    ResultRow,
    RunningTaskRow,
    SectionHeader,
    StartingTaskRow,
    ToolCard,
    CARD_FILL_ALERT,
    card_host,
    hline,
    muted,
    tint_bg,
)
from .conflictwizardview import ConflictWizardView
from .issuewizardview import IssueWizardView
from .auditwizardview import AuditWizardView
from .telemetryview import TelemetryView
from .wizardview import WizardView
# `.meshview` is imported in __init__ instead, behind `szpont.AVAILABLE`: it paints
# SzpontNet's topology and so imports the library at its own top level, which up
# here would make the add-on a hard dependency of the whole applet.

_REVIEW_TINT = "#FF2D78"
_ISSUES_TINT = "#00C7BE"
_CONFLICT_TINT = "#32ADE6"
_AUDIT_TINT = "#5856D6"

# Panel width: the two-pane body. Fixed — every screen (Actions, Mesh,
# Settings) renders at the same size, so switching never moves the window.
_BASE_WIDTH = 1080


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


def _queued_label(task: autofix.QueuedTask) -> str:
    """The row a task not yet running wears — the same string its dispatch will log
    and its session will carry, so nothing is renamed the moment it starts."""
    return autofix.dispatch_label(autofix.SOURCE_AUTO, task.job.label, task.attempt,
                                  requested=task.job.requested)


def _task_look(kind: str) -> tuple[str, str]:
    """The glyph and tint one agent task wears, from ``AgentJob.kind``. The same pair
    its action card carries in the grid, so a queued conflict fix reads as the
    Resolve-conflicts action it is."""
    if kind == "conflicts":
        return glyphs.G_CONFLICT, _CONFLICT_TINT
    if kind == "audit":
        return glyphs.G_AUDIT, _AUDIT_TINT
    if kind == "issues":
        return glyphs.G_ISSUES, _ISSUES_TINT
    return glyphs.G_REVIEW, _REVIEW_TINT


#: What each resolved state reads as on a row. "unknown" is the one word that used
#: not to exist: the applet would pick "running" or drop the row entirely rather than
#: admit a probe had failed, which is how a wrong verdict became invisible.
_STATE_WORD = {
    "running": "running",
    "awaiting_input": "awaiting input",
    "starting": "starting",
    "unknown": "unknown",
    "finished": "finished",
    "merged": "merged",
}


def _running_detail(agent: autofix.RunningAgent) -> str:
    """The status line under a running agent's label: what it is doing, how long it
    has been up, and whatever else this machine knows about where it came from.

    "awaiting input" is the one state the operator has to act on: the agent finished
    its turn and the window is waiting on a human. Saying it is the whole reason the
    row outlives the work — the bay is already back (`Store.auto_tasks_shown`), so
    without the word the row would look like a slot being held for nothing.

    "unknown" is the one state that needs its reason spelled out beside it. It means a
    probe could not answer, the run is holding its bay on purpose, and which probe
    failed is the difference between "install tmux" and "your mesh node is down".

    An untracked agent says so instead of ageing: its record was lost (a restart, or
    it was never this applet's to begin with), so there is no honest start time to
    count from — and the row's label is a bare PR number, which without the word
    would read as a dispatch that forgot its own name.
    """
    state = _STATE_WORD.get(agent.state, agent.state)
    if agent.state == "unknown" and agent.reason:
        state = f"{state} · {agent.reason}"
    if not agent.tracked:
        return f"{state} · untracked"
    parts = [state]
    # Under a minute Fmt gives "just now", which is not how long a thing has been
    # running — the first minute simply says "running".
    age = Fmt.duration(time.time() - agent.started_at)
    if age != "just now":
        parts.append(f"for {age}")
    if agent.mesh:
        parts.append("via mesh")
    return " · ".join(parts)


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
        # Which screen the body shows: "main" (Actions) | "settings" | "mesh".
        self._screen = "main"
        self._active_action: str | None = None  # None | "review" | "issues" | "conflicts" | "audit"
        # Devices section: In use expanded, Free collapsed by default. Persisted on the
        # instance so a poll-driven rebuild doesn't reset the user's collapse choice.
        self._inuse_expanded = True
        self._free_expanded = False
        # Left-pane monitoring sections (all expanded by default).
        self._activity_expanded = True
        self._bans_expanded = True
        self._tasks_expanded = True

        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        # Two-pane layout (matching the macOS popover): a left monitoring column
        # (devices · activity · bans) beside the right interactive column (search ·
        # tool grid · results). Widened to give both panes room; height tracks the
        # screen's safe area (availableGeometry excludes the taskbar/panel).
        self.setFixedWidth(_BASE_WIDTH)
        self.setFixedHeight(self._screen_high())

        outer = QVBoxLayout(self)
        outer.setContentsMargins(10, 10, 10, 10)
        outer.setSpacing(8)

        outer.addLayout(self._build_header())

        self.body = QStackedWidget()
        outer.addWidget(self.body, 1)

        # Screen name → page index, filled in as the pages are added, because the
        # last one is conditional. `_set_screen` looks up here rather than in a
        # literal map, so a build without the mesh page has no index to switch to
        # rather than switching to whatever happens to sit at that number.
        self._screens: dict[str, int] = {}

        # Page: main
        self.main_page = QWidget()
        self._build_main_page()
        self._screens["main"] = self.body.addWidget(self.main_page)

        # Page: telemetry (what the monitors cost and what they still owe). Sits
        # between Mesh and Settings, matching the header button order.
        self.telemetry_view = TelemetryView(store)
        self.telemetry_view.done.connect(lambda: self._set_screen("main"))
        telemetry_scroll = QScrollArea()
        telemetry_scroll.setWidgetResizable(True)
        telemetry_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        telemetry_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        telemetry_scroll.setWidget(self.telemetry_view)
        self._screens["telemetry"] = self.body.addWidget(telemetry_scroll)

        # Page: settings. Scrolled like the two screens above, and for the same
        # reason: its left column is a stack of toggles each carrying the paragraph
        # that says what it does, and on a short window Qt buys the space back by
        # squeezing those paragraphs into each other rather than by hiding anything.
        self.settings_view = SettingsView(store)
        self.settings_view.done.connect(lambda: self._set_screen("main"))
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        settings_scroll.setWidget(self.settings_view)
        self._screens["settings"] = self.body.addWidget(settings_scroll)

        # Page: mesh management (topology graph, node cards, duty routing) —
        # present only when the add-on is.
        self.mesh_view = None
        if szpont.AVAILABLE:
            from .meshview import MeshView

            self.mesh_view = MeshView(store)
            self.mesh_view.done.connect(lambda: self._set_screen("main"))
            mesh_scroll = QScrollArea()
            mesh_scroll.setWidgetResizable(True)
            mesh_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
            mesh_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            mesh_scroll.setWidget(self.mesh_view)
            self._screens["mesh"] = self.body.addWidget(mesh_scroll)

        # Shortcuts
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_search)
        QShortcut(QKeySequence("Ctrl+R"), self, activated=self.refresh_requested.emit)

        store.changed.connect(self._on_data_changed)
        store.loading_changed.connect(self._on_loading)
        store.devices_changed.connect(self._rebuild_devices)
        store.activity_changed.connect(self._rebuild_left_pane)
        store.tasks_changed.connect(self._rebuild_agent_tasks)

        # Poll the device-allocator state file + the shared activity/ban files on a
        # light cadence (cheap file reads), and re-measure the running automatic
        # agents on the same tick (a `ps` scan, so only while on screen).
        self._device_timer = QTimer(self)
        self._device_timer.timeout.connect(self.store.refresh_device_state)
        self._device_timer.timeout.connect(self.store.refresh_activity)
        self._device_timer.timeout.connect(self._tasks_tick)
        self._device_timer.start(8000)
        self.store.refresh_device_state()
        self.store.refresh_activity()

        # Advance the elapsed times that nothing else redraws: how long an in-use
        # device has been held, and how long a running agent has been up. Both are
        # counted from a fixed instant (allocatedAt, the agent's start), so the tick
        # that changes is the clock's, not the store's.
        self._duration_timer = QTimer(self)
        self._duration_timer.timeout.connect(self._rebuild_devices)
        self._duration_timer.timeout.connect(self._rebuild_agent_tasks)
        self._duration_timer.start(30000)

        # Mesh should feel live: poll the topology snapshot on a tight 2s cadence,
        # but only while the panel is visible AND the mesh is enabled (the tick
        # guards both, so a hidden panel or an opted-out user costs nothing). No
        # add-on, no timer at all — there is nothing the user could switch on that
        # would give it something to read.
        self._mesh_timer = None
        if szpont.AVAILABLE:
            self._mesh_timer = QTimer(self)
            self._mesh_timer.timeout.connect(self._mesh_tick)
            self._mesh_timer.start(2000)

        self._rebuild_grid()
        self._rebuild_devices()
        self._rebuild_left_pane()
        self._rebuild_agent_tasks()
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
        name = QLabel("Diplomat")
        name.setStyleSheet("font-weight: 700; font-size: 14px;")
        row.addWidget(name)
        repo = QLabel(f"{core.config()['owner']}/{core.config()['repo']}")
        repo.setStyleSheet(muted(9))
        row.addWidget(repo)
        row.addStretch(1)

        self.spinner = QLabel("⟳")
        self.spinner.setStyleSheet(muted(12))
        self.spinner.setVisible(False)
        row.addWidget(self.spinner)

        self.updated = QLabel("upd —")
        self.updated.setStyleSheet(muted(9))
        row.addWidget(self.updated)

        refresh = _icon_button("⟲", "Refresh")
        refresh.clicked.connect(self.refresh_requested.emit)
        row.addWidget(refresh)

        # No add-on, no ⬡ button: the screen behind it does not exist, and a
        # control that opens nothing is worse than an absent one.
        self.mesh_btn = None
        if szpont.AVAILABLE:
            self.mesh_btn = _icon_button(glyphs.G_MESH, "Mesh management")
            self.mesh_btn.clicked.connect(self._toggle_mesh)
            row.addWidget(self.mesh_btn)

        self.telemetry_btn = _icon_button(glyphs.G_TELEMETRY, "Telemetry")
        self.telemetry_btn.clicked.connect(self._toggle_telemetry)
        row.addWidget(self.telemetry_btn)

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
        """Monitoring column: agent tasks, device-allocator pool, activity feed, ban
        list. Each section is rebuilt in place — from the store's live queue, or from
        the shared ~/.diplomat files — and every one but the tasks list is hidden
        while empty. Wrapped in a scroll area so a busy feed scrolls within the pane.
        """
        host = QWidget()
        col = QVBoxLayout(host)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(8)

        # Agent tasks: the automatic agents this machine is running, the work it is
        # holding, and an empty bay per free slot of its cap. The one section always
        # drawn — a machine with nothing to do is still a machine with free slots,
        # and this is where the panel says how many. It is also what keeps the pane
        # from ever reading as empty on a quiet machine.
        self.tasks_host, self.tasks_col = card_host(spacing=4)
        col.addWidget(self.tasks_host)

        # Device-allocator pool (the shared simulators/emulators + who holds what).
        # Rebuilt in place from the daemon's state file; hidden when the pool is empty.
        self.devices_host, self.devices_col = card_host(spacing=4)
        self.devices_host.setVisible(False)
        col.addWidget(self.devices_host)

        # Activity feed — the shared audit.jsonl action log (panel + daemon + agents).
        self.activity_host, self.activity_col = card_host(spacing=4)
        self.activity_host.setVisible(False)
        col.addWidget(self.activity_host)

        # Banned authors (prompt-injection blocklist; read-only here).
        self.bans_host, self.bans_col = card_host(fill=CARD_FILL_ALERT, spacing=4)
        self.bans_host.setVisible(False)
        col.addWidget(self.bans_host)

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
        self.hint.setStyleSheet(muted(11))
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.results.addWidget(self.hint)  # index 2

        self.wizard = WizardView(self.store)
        wizard_scroll = QScrollArea()
        wizard_scroll.setWidgetResizable(True)
        wizard_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        wizard_scroll.setWidget(self.wizard)
        self.results.addWidget(wizard_scroll)  # index 3

        self.issue_wizard = IssueWizardView(self.store)
        issue_scroll = QScrollArea()
        issue_scroll.setWidgetResizable(True)
        issue_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        issue_scroll.setWidget(self.issue_wizard)
        self.results.addWidget(issue_scroll)  # index 4

        self.conflict_wizard = ConflictWizardView(self.store)
        conflict_scroll = QScrollArea()
        conflict_scroll.setWidgetResizable(True)
        conflict_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        conflict_scroll.setWidget(self.conflict_wizard)
        self.results.addWidget(conflict_scroll)  # index 5

        self.audit_wizard = AuditWizardView(self.store)
        audit_scroll = QScrollArea()
        audit_scroll.setWidgetResizable(True)
        audit_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        audit_scroll.setWidget(self.audit_wizard)
        self.results.addWidget(audit_scroll)  # index 6

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

        issue_card = ActionCard(
            emoji=glyphs.G_ISSUES,
            title="Fix issues",
            subtitle="reproduce & fix open issues",
            hex_color=_ISSUES_TINT,
            selected=self._active_action == "issues",
        )
        issue_card.clicked.connect(lambda: self._open_action("issues"))
        self.grid.addWidget(issue_card, rowi, col)
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
            return
        self.devices_host.setVisible(True)

        from . import deviceallocator

        head = QHBoxLayout()
        head.setSpacing(6)
        head.addWidget(GlyphLabel(glyphs.G_DEVICES, 14, "#9aa0a6", font_px=12))
        title = QLabel("Devices")
        title.setStyleSheet(muted(10, bold=True))
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

    def _toggle_inuse(self) -> None:
        self._inuse_expanded = not self._inuse_expanded
        self._rebuild_devices()

    def _toggle_free(self) -> None:
        self._free_expanded = not self._free_expanded
        self._rebuild_devices()

    # MARK: agent tasks — the bays of the cap, filled and free, and the queue behind

    def _rebuild_agent_tasks(self) -> None:
        """The Agent-tasks list: what this machine is doing, what it is about to do,
        and how much room it has left.

        What is running first, then what is starting, then the free slots, then the
        queue — the reading order `AgentTaskStatus` fixes on macOS, for the statuses
        this front-end can tell apart. A detached `Popen` gives it no window handle,
        so a running agent is a status and not a session: nothing to click, and no
        *done* (this side never learns that a window was closed, only that its
        process left). *Awaiting input* it does have — the agents run in tmux panes,
        which can be read (`Store._idle_pr_agents`).

        So the list can be longer than the cap: an agent waiting at its prompt keeps
        its row AND gives its bay back, which draws both. That pair is the point —
        the free bay says work can start, the row above it says what is still open.
        """
        _clear_layout(self.tasks_col)
        queued = self.store.queued_tasks
        starting = self.store.starting_tasks
        free = self.store.free_auto_slots
        running = self.store.running_tasks
        parts = [f"{len(queued)} queued" if queued else "",
                 f"{free} free" if free else ""]
        caption = " · ".join(p for p in parts if p)

        # The tasks — a starting one among them, or clicking the last queued row
        # would drop the count for as long as its spawn takes. Empty bays are not:
        # a device with nothing to do reads "0", not "2".
        header = SectionHeader(glyph=glyphs.G_TASKS, title="Agent tasks",
                               count=len(running) + len(starting) + len(queued),
                               caption=caption or None,
                               expanded=self._tasks_expanded)
        header.clicked.connect(self._toggle_tasks)
        self.tasks_col.addWidget(header)
        if not self._tasks_expanded:
            return
        for agent in running:
            glyph, tint = _task_look(agent.kind)
            self.tasks_col.addWidget(RunningTaskRow(
                label=agent.label or f"#{agent.pr_number}",
                detail=_running_detail(agent),
                glyph=glyph,
                hex_color=tint,
                tracked=agent.tracked,
                awaiting_input=agent.awaiting_input,
            ))
        for task in starting:
            glyph, tint = _task_look(task.job.kind)
            self.tasks_col.addWidget(StartingTaskRow(
                label=_queued_label(task),
                glyph=glyph,
                hex_color=tint,
            ))
        for _ in range(free):
            self.tasks_col.addWidget(FreeSlotRow())
        # Drawn with the queue, and also when it is empty but switched off: otherwise
        # the one state you cannot leave is the one that empties the list.
        if queued or not self.store.queue_auto_run:
            switch = QueueAutoRunRow(on=self.store.queue_auto_run)
            switch.toggled.connect(self._set_queue_auto_run)
            self.tasks_col.addWidget(switch)
        for task in queued:
            glyph, tint = _task_look(task.job.kind)
            row = QueuedTaskRow(
                task_id=task.id,
                label=_queued_label(task),
                glyph=glyph,
                hex_color=tint,
                paused=self.store.is_paused(task.job.counter),
                # Only an ask can be called off. A monitor's row stands for work
                # GitHub is owed: dropping it would put it straight back on the next
                # poll, so the button would do nothing anyone could see.
                cancellable=task.job.requested,
            )
            row.run_requested.connect(
                lambda tid=task.id: self.store.execute_queued_task_async(tid)
            )
            row.cancel_requested.connect(
                lambda tid=task.id: self.store.cancel_requested_review(tid)
            )
            row.dropped.connect(
                lambda dragged, tid=task.id: self.store.move_queued_task(dragged, tid)
            )
            self.tasks_col.addWidget(row)

    def _toggle_tasks(self) -> None:
        self._tasks_expanded = not self._tasks_expanded
        self._rebuild_agent_tasks()

    def _set_queue_auto_run(self, on: bool) -> None:
        """Switched back on, poll now: the drain runs at the top of a poll, so the
        queue would otherwise sit still for a whole period after the press."""
        self.store.queue_auto_run = on
        self.store.tasks_changed.emit()
        if on:
            self.store.run_autofix_poll_async()

    def _tasks_tick(self) -> None:
        """Re-measure the running automatic agents while the panel is on screen, so a
        finished agent frees its bay within a tick rather than at the next 3-minute
        poll. Gated on visibility: nothing else reads the count, and the measurement
        shells out to `ps`."""
        if self.isVisible():
            self.store.refresh_auto_task_count_async()

    # MARK: activity feed + bans

    def _rebuild_left_pane(self) -> None:
        self._rebuild_activity()
        self._rebuild_bans()

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
            ver.setStyleSheet(muted(9))
            name_row.addWidget(ver)
        if dev.get("format"):
            fmt = QLabel(str(dev["format"]))
            fmt.setStyleSheet(muted(9))
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

    def _set_screen(self, name: str) -> None:
        """Flip the body to one of the screens: Actions ("main"), Telemetry,
        Settings, or — when the mesh add-on is installed — Mesh management."""
        self._screen = name
        self.body.setCurrentIndex(self._screens[name])
        if name == "mesh" and self.store.mesh_enabled:
            # Fresh topology the moment the screen opens (the 2s poll follows).
            self.store.refresh_mesh_state()
        if name == "telemetry":
            # Re-fold on open. The screen is otherwise only repainted when a
            # sample lands (every 15 min), and the monitors append between those.
            self.telemetry_view.rebuild()
        if name == "main":
            self._rebuild_grid()
            self._update_results()

    def _toggle_settings(self) -> None:
        self._set_screen("main" if self._screen == "settings" else "settings")

    def _toggle_mesh(self) -> None:
        self._set_screen("main" if self._screen == "mesh" else "mesh")

    def _toggle_telemetry(self) -> None:
        self._set_screen("main" if self._screen == "telemetry" else "telemetry")

    def _focus_search(self) -> None:
        if self._screen != "main":
            self._set_screen("main")
        self.search.setFocus()

    # MARK: results

    def _update_results(self) -> None:
        trimmed = self.search.text().strip()
        if self._active_action == "review":
            self.results.setCurrentIndex(3)
            return
        if self._active_action == "issues":
            self.results.setCurrentIndex(4)
            return
        if self._active_action == "conflicts":
            self.results.setCurrentIndex(5)
            return
        if self._active_action == "audit":
            self.results.setCurrentIndex(6)
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
            empty.setStyleSheet(muted(11))
            empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            col.addWidget(empty)
            col.addStretch(1)
            self.tool_results_scroll.setWidget(container)
            return

        tool = tool_by_id(selected)
        tint = self.store.tint(selected)
        items = self.store.items_for(selected)

        header = QHBoxLayout()
        header.addWidget(IconChip(tool.glyph, tint, 20))
        title = QLabel(tool.title)
        title.setStyleSheet("font-weight: 700; font-size: 12px;")
        header.addWidget(title)
        cnt = QLabel(str(len(items)))
        cnt.setStyleSheet(muted(10, mono=True))
        header.addWidget(cnt)
        header.addStretch(1)
        col.addLayout(header)

        if not items:
            msg = "Loading…" if self.store.is_loading else "Nothing here."
            empty = QLabel(msg)
            empty.setStyleSheet(muted(11))
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
        presence.setStyleSheet(muted(10))
        col.addWidget(presence)

        for tool in self.store.visible_tools:
            is_on = tool.id in r.on_lists
            tint = self.store.tint(tool.id)
            roww = QWidget()
            rl = QHBoxLayout(roww)
            rl.setContentsMargins(7, 7, 7, 7)
            rl.setSpacing(8)
            rl.addWidget(IconChip(tool.glyph, tint, 22, active=is_on))
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
        self.issue_wizard.refresh_identity()
        self.conflict_wizard.refresh_identity()
        self.audit_wizard.refresh_identity()
        if self._screen == "main":
            self._update_results()

    def _on_loading(self, loading: bool) -> None:
        self.spinner.setVisible(loading)

    # MARK: mesh poll

    def _mesh_tick(self) -> None:
        """2s poll of the mesh topology — cheap file read, gated so it's free when
        the panel is hidden or the user hasn't opted into the mesh."""
        if self.isVisible() and self.store.mesh_enabled:
            self.store.refresh_mesh_state()

    def showEvent(self, event) -> None:  # noqa: N802
        # One immediate mesh refresh on show so the mesh screen isn't a
        # poll-cycle stale when the panel pops open.
        if self.store.mesh_enabled:
            self.store.refresh_mesh_state()
        # Likewise the free bays: an agent that finished while the panel was hidden
        # must not leave its slot drawn as taken until the next tick.
        self.store.refresh_auto_task_count_async()
        super().showEvent(event)

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
            if self._screen != "main":
                self._set_screen("main")
            else:
                self.hide()
            return
        super().keyPressEvent(event)
