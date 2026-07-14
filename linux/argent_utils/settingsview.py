"""Settings screen — GitHub handle, per-tool colour/visibility, spawn terminal.

The Linux analogue of SettingsView.swift. Persists through the Store (QSettings).
Built once and updated in place so typing in the handle field is never disrupted
by a background data refresh.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import deviceallocator, review
from .store import Store, tools
from .widgets import IconChip


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet("color: palette(mid); font-weight: 700; font-size: 9px; letter-spacing: 1px;")
    return lbl


class SettingsView(QWidget):
    done = Signal()

    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self._chips: dict[str, IconChip] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(14)

        root.addLayout(self._header_row())
        root.addLayout(self._identity_section())
        root.addLayout(self._tools_section())
        root.addLayout(self._terminal_section())
        root.addLayout(self._allocator_section())
        root.addLayout(self._mesh_section())
        root.addStretch(1)

        store.allocator_changed.connect(self._refresh_allocator_ui)
        store.mesh_changed.connect(self._refresh_mesh_ui)
        self._refresh_allocator_ui()
        self._refresh_mesh_ui()
        store.refresh_allocator_install_async()
        store.refresh_mesh_state()

    # MARK: header

    def _header_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        title = QLabel("⚙  Settings")
        title.setStyleSheet("font-weight: 700; font-size: 13px;")
        row.addWidget(title)
        row.addStretch(1)
        done = QPushButton("Done")
        done.setStyleSheet("font-weight: 700;")
        done.clicked.connect(self.done.emit)
        row.addWidget(done)
        return row

    # MARK: GitHub identity

    def _identity_section(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(6)
        col.addWidget(_section_label("GITHUB USERNAME"))

        field = QLineEdit(self.store.username_override)
        field.setPlaceholderText(self.store.me or "your github handle")
        field.setClearButtonEnabled(True)

        hint = QLabel()
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid); font-size: 10px;")

        def update_hint() -> None:
            o = self.store.username_override.strip()
            if o:
                hint.setText(f"Overriding to @{o} for the “My …” tools and the Review wizard.")
            else:
                who = f" (@{self.store.me})" if self.store.me else ""
                hint.setText(
                    f"Using the gh-authenticated user{who}. Scopes the “My …” tools and the Review wizard."
                )

        def on_text(text: str) -> None:
            self.store.username_override = text
            update_hint()
            self.store.changed.emit()

        field.textChanged.connect(on_text)
        update_hint()
        col.addWidget(field)
        col.addWidget(hint)
        return col

    # MARK: tool colour & visibility

    def _tools_section(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(8)
        col.addWidget(_section_label("TOOLS — COLOR & VISIBILITY"))
        for tool in tools():
            col.addLayout(self._tool_row(tool.id, tool.title, tool.subtitle, tool.emoji))
        return col

    def _tool_row(self, tool_id: str, title: str, subtitle: str, emoji: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        chip = IconChip(emoji, self.store.tint(tool_id), size=22)
        self._chips[tool_id] = chip
        row.addWidget(chip)

        text = QVBoxLayout()
        text.setSpacing(1)
        t = QLabel(title)
        t.setStyleSheet("font-weight: 600; font-size: 11px;")
        s = QLabel(subtitle)
        s.setStyleSheet("color: palette(mid); font-size: 9px;")
        text.addWidget(t)
        text.addWidget(s)
        row.addLayout(text, 1)

        color_btn = QPushButton("●")
        color_btn.setFixedWidth(34)
        color_btn.setStyleSheet(f"color: {self.store.tint(tool_id)}; font-size: 16px;")
        color_btn.setToolTip(f"Tint for {title}")
        color_btn.clicked.connect(lambda: self._pick_color(tool_id, color_btn))
        row.addWidget(color_btn)

        toggle = QCheckBox()
        toggle.setChecked(tool_id not in self.store.hidden_tools)
        toggle.setToolTip(f"Show {title} in the grid")
        toggle.toggled.connect(lambda on: self.store.set_tool(tool_id, on))
        row.addWidget(toggle)
        return row

    def _pick_color(self, tool_id: str, btn: QPushButton) -> None:
        initial = QColor(self.store.tint(tool_id))
        chosen = QColorDialog.getColor(initial, self, f"Tint for {tool_id}")
        if chosen.isValid():
            hex_color = chosen.name(QColor.NameFormat.HexRgb).upper()
            self.store.set_tint(hex_color, tool_id)
            btn.setStyleSheet(f"color: {hex_color}; font-size: 16px;")
            chip = self._chips.get(tool_id)
            if chip:
                chip.setStyleSheet(
                    f"background-color: {hex_color}; border-radius: 6px; font-size: 11px;"
                )

    # MARK: device allocator (MCP server + skill + rule)

    def _allocator_section(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(6)
        col.addWidget(_section_label("DEVICE ALLOCATOR (MCP)"))

        status_row = QHBoxLayout()
        status_row.setSpacing(8)
        self._alloc_status = QLabel("Checking…")
        self._alloc_status.setStyleSheet("font-weight: 700; font-size: 11px;")
        status_row.addWidget(self._alloc_status)
        status_row.addStretch(1)
        self._alloc_daemon = QLabel("⚡ daemon")
        self._alloc_daemon.setStyleSheet("color: #34C759; font-size: 9px;")
        self._alloc_daemon.setVisible(False)
        status_row.addWidget(self._alloc_daemon)
        col.addLayout(status_row)

        self._alloc_detail = QLabel("querying the installer…")
        self._alloc_detail.setStyleSheet("color: palette(mid); font-family: monospace; font-size: 9px;")
        col.addWidget(self._alloc_detail)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._alloc_install = QPushButton("Install")
        self._alloc_install.setStyleSheet("font-weight: 700;")
        self._alloc_install.setEnabled(deviceallocator.package_available())
        self._alloc_install.clicked.connect(self.store.install_allocator_async)
        btn_row.addWidget(self._alloc_install)
        self._alloc_uninstall = QPushButton("Uninstall")
        self._alloc_uninstall.setVisible(False)
        self._alloc_uninstall.clicked.connect(self.store.uninstall_allocator_async)
        btn_row.addWidget(self._alloc_uninstall)
        recheck = QPushButton("⟲")
        recheck.setFixedWidth(34)
        recheck.setToolTip("Re-check status")
        recheck.clicked.connect(self.store.refresh_allocator_install_async)
        btn_row.addWidget(recheck)
        btn_row.addStretch(1)
        col.addLayout(btn_row)

        avail = deviceallocator.package_available()
        hint = QLabel(
            "Forces every local agent to reserve an emulator/simulator before using it "
            "(MCP server + skill + always-on rule), so agents never collide on a shared "
            "device. Reclaims a device when its agent dies or it sits idle for 1h."
            if avail else
            f"Package not found at {deviceallocator.package_dir()}. "
            "Set ARGENT_DEVICE_ALLOCATOR_DIR to point at it."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(
            f"color: {'palette(mid)' if avail else '#FF9500'}; font-size: 10px;"
        )
        col.addWidget(hint)
        return col

    def _refresh_allocator_ui(self) -> None:
        s = self.store.allocator_install
        if s is None:
            self._alloc_status.setText("Checking…")
            self._alloc_detail.setText("querying the installer…")
            self._alloc_uninstall.setVisible(False)
            self._alloc_daemon.setVisible(False)
            return
        installed = bool(s.get("installed"))

        def mark(b: object) -> str:
            return "✓" if b else "✗"

        self._alloc_status.setText("Installed" if installed else "Not installed")
        self._alloc_detail.setText(
            f"MCP {mark(s.get('mcpRegistered'))} · skill {mark(s.get('skillInstalled'))}"
            f" · rule {mark(s.get('ruleInstalled'))} · CLAUDE.md {mark(s.get('claudeMdInjected'))}"
        )
        self._alloc_install.setText("Reinstall" if installed else "Install")
        self._alloc_uninstall.setVisible(installed)
        self._alloc_daemon.setVisible(bool(s.get("daemonRunning")))

    # MARK: mesh (LAN P2P duty coordination)

    def _mesh_section(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(6)
        col.addWidget(_section_label("MESH (LAN P2P)"))

        toggle = QCheckBox("Coordinate duties with other machines on this LAN")
        toggle.setChecked(self.store.mesh_enabled)
        toggle.toggled.connect(self._on_mesh_toggled)
        col.addWidget(toggle)

        self._mesh_status = QLabel("")
        self._mesh_status.setStyleSheet("font-weight: 700; font-size: 11px;")
        col.addWidget(self._mesh_status)

        hint = QLabel(
            "Runs a small peer-to-peer node that discovers the other Argent Utils "
            "machines on your LAN (UDP beacons) and routes duty work — reviews, "
            "conflict fixes, the full E2E audit — to whichever node fits the "
            "placement policy (weakest-first by default, token- and platform-aware). "
            "Configure the whole mesh from the 🕸️ column on the left of the panel. "
            "Off by default; no node opens on the network until you enable it here."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid); font-size: 10px;")
        col.addWidget(hint)
        return col

    def _on_mesh_toggled(self, on: bool) -> None:
        self.store.mesh_enabled = on
        if on:
            self.store.ensure_mesh_running_async()
        else:
            self.store.stop_mesh_async()
        self._refresh_mesh_ui()

    def _refresh_mesh_ui(self) -> None:
        from .mesh import statefile

        state = self.store.mesh_state
        if not self.store.mesh_enabled:
            self._mesh_status.setText("Off")
            self._mesh_status.setStyleSheet(
                "font-weight: 700; font-size: 11px; color: palette(mid);"
            )
            return
        if statefile.node_running(state):
            peers = len((state or {}).get("peers", []))
            plural = "" if peers == 1 else "s"
            self._mesh_status.setText(f"Node running · {peers} peer{plural}")
            self._mesh_status.setStyleSheet(
                "font-weight: 700; font-size: 11px; color: #34C759;"
            )
        else:
            self._mesh_status.setText("Starting node…" if state is None
                                      else "Node not running")
            self._mesh_status.setStyleSheet(
                "font-weight: 700; font-size: 11px; color: #FF9500;"
            )

    # MARK: terminal

    def _terminal_section(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(6)
        col.addWidget(_section_label("SPAWN TERMINAL"))
        combo = QComboBox()
        for term in review.TERMINALS:
            suffix = "" if term.is_installed else "  (not installed)"
            combo.addItem(term.title + suffix, term.key)
        idx = combo.findData(self.store.terminal_choice)
        if idx >= 0:
            combo.setCurrentIndex(idx)
        combo.currentIndexChanged.connect(
            lambda: setattr(self.store, "terminal_choice", combo.currentData())
        )
        col.addWidget(combo)

        hint = QLabel(
            "SPAWN AGENT opens a new terminal window running `claude` with the review prompt."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid); font-size: 10px;")
        col.addWidget(hint)
        return col
