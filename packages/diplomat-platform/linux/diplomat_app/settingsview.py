"""Settings screen — GitHub handle, per-tool colour/visibility, spawn terminal.

The Linux analogue of SettingsView.swift. Persists through the Store (QSettings).
Built once and updated in place so typing in the handle field is never disrupted
by a background data refresh.
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import apiwatch, appconfig, autofix, core, deviceallocator, review, szpont
from .store import Store, tools
from .widgets import IconChip, muted


def _section_label(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(muted(9, bold=True) + " letter-spacing: 1px;")
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

        # Two columns, matching the macOS SettingsView and the main panel: identity +
        # automation behaviour on the left, appearance + environment on the right. Each
        # column pushes its sections up with a bottom stretch so the two stay top-aligned
        # regardless of differing heights.
        body = QHBoxLayout()
        body.setSpacing(12)

        left = QVBoxLayout()
        left.setSpacing(14)
        left.addLayout(self._identity_section())
        left.addLayout(self._repo_section())
        left.addLayout(self._autofix_section())
        left.addLayout(self._apiwatch_section())
        left.addStretch(1)

        right = QVBoxLayout()
        right.setSpacing(14)
        right.addLayout(self._tools_section())
        right.addLayout(self._terminal_section())
        right.addLayout(self._allocator_section())
        right.addLayout(self._mesh_section())
        right.addLayout(self._update_section())
        right.addStretch(1)

        body.addLayout(left, 1)
        body.addLayout(right, 1)
        root.addLayout(body)
        root.addStretch(1)

        store.allocator_changed.connect(self._refresh_allocator_ui)
        store.mesh_changed.connect(self._refresh_mesh_ui)
        store.update_changed.connect(self._refresh_update_ui)
        store.autofix_changed.connect(self._refresh_autofix_ui)
        store.apiwatch_changed.connect(self._refresh_apiwatch_ui)
        self._refresh_allocator_ui()
        self._refresh_mesh_ui()
        self._refresh_update_ui()
        self._refresh_autofix_ui()
        self._refresh_apiwatch_ui()
        store.refresh_allocator_install_async()
        store.refresh_update_status_async()
        if store.mesh_enabled:
            # Only touch the mesh state file when the user actually uses the mesh;
            # otherwise this is a needless real-HOME read on every Settings open
            # (and in non-mesh render/test paths). The Panel's own poll keeps
            # mesh_state fresh while a node is live.
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
        hint.setStyleSheet(muted(10))

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

    # MARK: repo root (where the agents work)

    def _repo_section(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(6)
        col.addWidget(_section_label("REPO ROOT"))

        row = QHBoxLayout()
        row.setSpacing(6)
        self._repo_field = QLineEdit(self.store.repo_path_override)
        self._repo_field.setPlaceholderText(review.default_repo_path())
        self._repo_field.setClearButtonEnabled(True)
        row.addWidget(self._repo_field, 1)
        browse = QPushButton("Browse…")
        browse.setToolTip("Pick the local checkout agents should work in")
        browse.clicked.connect(self._browse_repo)
        row.addWidget(browse)
        col.addLayout(row)

        self._repo_hint = QLabel()
        self._repo_hint.setWordWrap(True)
        col.addWidget(self._repo_hint)

        def on_text(text: str) -> None:
            self.store.repo_path_override = text
            self._refresh_repo_ui()

        self._repo_field.textChanged.connect(on_text)
        self._refresh_repo_ui()
        return col

    def _browse_repo(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Choose the repo root", review.repo_path()
        )
        if chosen:
            # Writes through the field so the text, the setting and the hint all move
            # together (textChanged -> on_text).
            self._repo_field.setText(chosen)

    def _repo_state(self) -> str:
        """Which of the four hint states applies. A relative entry gets its own:
        an "is it a checkout?" stat would be judged against THIS process's working
        directory while the spawn's ``cd`` runs in the terminal's — the two disagree,
        so neither verdict is honest. Mirrors ``RepoPaths.agentRepoState`` in Swift."""
        if os.environ.get("DIPLOMAT_REPO"):
            return "env-shadowed"
        resolved = review.repo_path()
        if not os.path.isabs(resolved):
            return "not-absolute"
        return "ok" if os.path.exists(os.path.join(resolved, ".git")) else "not-a-checkout"

    def _refresh_repo_ui(self) -> None:
        """Hint + colour, from one state read so the two can't disagree."""
        state = self._repo_state()
        resolved = review.repo_path()
        owner, repo = core.config()["owner"], core.config()["repo"]
        if state == "env-shadowed":
            env = os.environ.get("DIPLOMAT_REPO", "")
            text = (
                "DIPLOMAT_REPO is set in this app's environment — agents run in "
                f"{os.path.expanduser(env)}, whatever this field says. Unset it to use "
                "the picker again."
            )
        elif state == "not-absolute":
            text = (
                "Use an absolute path — a relative one resolves against whatever "
                "directory the spawned terminal happens to start in, not this app's."
            )
        elif state == "not-a-checkout":
            text = (
                f"No git checkout at {resolved} — the spawn's `cd` is best-effort, so an "
                "agent would start in your home directory instead. Pick the clone of "
                f"{owner}/{repo}."
            )
        else:
            tail = "" if self.store.repo_path_override.strip() else " Blank = the default path."
            text = (
                f"Every spawned agent starts with `cd {resolved}` — your local clone of "
                f"{owner}/{repo}.{tail}"
            )
        self._repo_hint.setText(text)
        self._repo_hint.setStyleSheet(
            f"color: {'palette(mid)' if state == 'ok' else '#FF9500'}; font-size: 10px;"
        )

    # MARK: PR auto-fix monitor

    def _autofix_section(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(6)
        col.addWidget(_section_label("PR AUTO-FIX"))

        self._cb_autofix = QCheckBox("Auto-fix my PRs (conflicts + reviews)")
        self._cb_autofix.setChecked(self.store.pr_autofix_enabled)
        self._cb_autofix.toggled.connect(self._on_autofix_toggled)
        col.addWidget(self._cb_autofix)

        self._autofix_status = QLabel("")
        self._autofix_status.setStyleSheet("font-size: 10px;")
        col.addWidget(self._autofix_status)

        self._autofix_poll_err = QLabel("")
        self._autofix_poll_err.setWordWrap(True)
        self._autofix_poll_err.setStyleSheet("color: #FF3B30; font-size: 10px;")
        col.addWidget(self._autofix_poll_err)

        hint = QLabel(
            "When on, an agent watches your open PRs and automatically resolves merge "
            "conflicts and addresses new review threads. Off, the monitor keeps "
            "looking and lists what it finds under Agent tasks — as queued work only "
            "you can start, with “execute now”."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(muted(10))
        col.addWidget(hint)

        self._cb_review_req = QCheckBox("Full-E2E review PRs that request my review")
        self._cb_review_req.setChecked(self.store.review_requests_enabled)
        self._cb_review_req.toggled.connect(self._on_review_req_toggled)
        col.addWidget(self._cb_review_req)

        self._review_req_hint = QLabel("")
        self._review_req_hint.setWordWrap(True)
        self._review_req_hint.setStyleSheet(muted(10))
        col.addWidget(self._review_req_hint)

        self._unaddressed = QLabel("")
        self._unaddressed.setStyleSheet("color: #FF9500; font-size: 10px;")
        col.addWidget(self._unaddressed)

        # Auto-approve block — a container so it (and the verdict sub-block) can be
        # shown/hidden as a unit (a QLayout can't be toggled; a QWidget can).
        self._approve_container = QWidget()
        approve = QVBoxLayout(self._approve_container)
        approve.setContentsMargins(12, 0, 0, 0)
        approve.setSpacing(4)

        self._cb_auto_approve = QCheckBox("Let auto-reviews approve / request changes")
        self._cb_auto_approve.setChecked(self.store.auto_approve_enabled)
        self._cb_auto_approve.toggled.connect(self._on_auto_approve_toggled)
        approve.addWidget(self._cb_auto_approve)

        approve_hint = QLabel(
            "Off ⇒ every auto-review leaves inline comments only; the approve / "
            "request-changes call stays with you. On ⇒ a clean review may submit a "
            "verdict, except where withheld below."
        )
        approve_hint.setWordWrap(True)
        approve_hint.setStyleSheet(muted(10))
        approve.addWidget(approve_hint)

        self._verdict_container = QWidget()
        verdict = QVBoxLayout(self._verdict_container)
        verdict.setContentsMargins(12, 0, 0, 0)
        verdict.setSpacing(2)
        verdict.addWidget(_section_label("WITHHOLD THE FINAL VERDICT WHEN THE PR…"))
        self._cb_verdict_skill = QCheckBox("…touches a SKILL")
        self._cb_verdict_skill.setChecked(self.store.verdict_withhold_skill)
        self._cb_verdict_skill.toggled.connect(lambda on: self._set_verdict("skill", on))
        verdict.addWidget(self._cb_verdict_skill)
        self._cb_verdict_installer = QCheckBox("…touches the installer")
        self._cb_verdict_installer.setChecked(self.store.verdict_withhold_installer)
        self._cb_verdict_installer.toggled.connect(
            lambda on: self._set_verdict("installer", on)
        )
        verdict.addWidget(self._cb_verdict_installer)
        self._cb_verdict_community = QCheckBox("…is a community PR (author outside the org)")
        self._cb_verdict_community.setChecked(self.store.verdict_withhold_community)
        self._cb_verdict_community.toggled.connect(
            lambda on: self._set_verdict("community", on)
        )
        verdict.addWidget(self._cb_verdict_community)
        approve.addWidget(self._verdict_container)

        # Soft-approvals: what a comments-only review does on a perfectly-clean PR -
        # leave a friendly thank-you note (no APPROVE action) instead of staying silent.
        # On by default; independent of the verdict toggle above.
        self._cb_soft_approve = QCheckBox(
            "Soft-approve clean PRs (thank-you comment, no approval)"
        )
        self._cb_soft_approve.setChecked(self.store.soft_approve_enabled)
        self._cb_soft_approve.toggled.connect(self._on_soft_approve_toggled)
        approve.addWidget(self._cb_soft_approve)
        soft_hint = QLabel(
            "On ⇒ a review that comes back perfectly clean leaves one friendly "
            "“ran the sweep, all clean, thanks for contributing” comment — "
            "never an APPROVE action. Off ⇒ a clean review stays silent."
        )
        soft_hint.setWordWrap(True)
        soft_hint.setStyleSheet(muted(10))
        approve.addWidget(soft_hint)

        col.addWidget(self._approve_container)
        col.addLayout(self._auto_limit_row())
        col.addLayout(self._auto_budget_rows())
        return col

    def _auto_limit_row(self) -> QVBoxLayout:
        """The device-wide ceiling on concurrent automatic agents. Sits at the foot
        of the section because it governs BOTH monitors above it — a poll of either
        one can find any number of pending units at once, and this is what keeps
        them from all opening at the same moment."""
        col = QVBoxLayout()
        col.setSpacing(2)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("Run at most"))
        self._auto_limit = QSpinBox()
        self._auto_limit.setRange(
            autofix.MIN_AUTO_TASK_LIMIT, autofix.MAX_AUTO_TASK_LIMIT
        )
        self._auto_limit.setValue(self.store.auto_task_limit)
        self._auto_limit.valueChanged.connect(self._on_auto_limit_changed)
        row.addWidget(self._auto_limit)
        row.addWidget(QLabel("automatic tasks at a time"))
        row.addStretch(1)
        col.addLayout(row)

        hint = QLabel(
            "A hard cap for this machine, across both monitors above and any work a "
            "mesh peer routes here. Agents you spawn yourself from the panel don't "
            "count against it. Work over the cap isn't dropped — it waits in the "
            "Agent-tasks list, in the order you put it, and starts as soon as a "
            "running agent finishes. The panel draws whatever is left of the cap as "
            "empty slots."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(muted(10))
        col.addWidget(hint)
        return col

    def _on_auto_limit_changed(self, value: int) -> None:
        self.store.auto_task_limit = value
        self.store.changed.emit()

    def _auto_budget_rows(self) -> QVBoxLayout:
        """The rate-limit budget: whether automatic work waits when the account is
        running low, how sure of that it has to be, and what to keep in hand while the
        ledger cannot yet price a task.

        Under the task cap because they are the two halves of one question — the cap
        bounds how many automatic agents run at once, this bounds whether any of them
        should start at all — and the confidence and floor are meaningless with the
        gate off, so they are disabled with it."""
        col = QVBoxLayout()
        col.setSpacing(2)

        self._cb_budget = QCheckBox("Hold automatic work when the rate limit runs low")
        self._cb_budget.setChecked(appconfig.auto_budget_gate())
        self._cb_budget.toggled.connect(self._on_budget_gate_toggled)
        col.addWidget(self._cb_budget)

        self._budget_knobs = QWidget()
        knobs = QVBoxLayout(self._budget_knobs)
        knobs.setContentsMargins(18, 0, 0, 0)
        knobs.setSpacing(2)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(QLabel("Start one only when"))
        self._budget_confidence = QComboBox()
        for level in sorted(autofix.BUDGET_CONFIDENCE_Z):
            self._budget_confidence.addItem(f"{level}%", level)
        self._budget_confidence.setCurrentIndex(
            self._budget_confidence.findData(appconfig.auto_budget_confidence())
        )
        self._budget_confidence.currentIndexChanged.connect(
            self._on_budget_confidence_changed
        )
        row.addWidget(self._budget_confidence)
        row.addWidget(QLabel("sure it fits, and keep"))
        self._budget_floor = QDoubleSpinBox()
        self._budget_floor.setRange(0.0, 100.0)
        self._budget_floor.setSingleStep(5.0)
        self._budget_floor.setDecimals(1)
        self._budget_floor.setSuffix("%")
        self._budget_floor.setValue(appconfig.auto_budget_floor_pct())
        self._budget_floor.valueChanged.connect(self._on_budget_floor_changed)
        row.addWidget(self._budget_floor)
        row.addWidget(QLabel("in hand until then"))
        row.addStretch(1)
        knobs.addLayout(row)

        hint = QLabel(
            "Priced from Telemetry → limit per task, against both rate-limit windows: "
            "higher confidence is stricter. Held work isn't dropped — it waits in the "
            "Agent-tasks list until a window refills, and “execute now” overrides it. "
            "Nothing is held while the usage probe can't read a window at all."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(muted(10))
        knobs.addWidget(hint)

        col.addWidget(self._budget_knobs)
        self._budget_knobs.setEnabled(self._cb_budget.isChecked())
        return col

    def _on_budget_gate_toggled(self, on: bool) -> None:
        appconfig.set_bool(appconfig.AUTO_BUDGET_GATE, on)
        self._budget_knobs.setEnabled(on)
        self.store.changed.emit()

    def _on_budget_confidence_changed(self, index: int) -> None:
        appconfig.set_int(appconfig.AUTO_BUDGET_CONFIDENCE,
                          int(self._budget_confidence.itemData(index)))
        self.store.changed.emit()

    def _on_budget_floor_changed(self, value: float) -> None:
        appconfig.set_float(appconfig.AUTO_BUDGET_FLOOR_PCT, float(value))
        self.store.changed.emit()

    def _on_autofix_toggled(self, on: bool) -> None:
        self.store.pr_autofix_enabled = on
        self.store.changed.emit()
        # This monitor's queued rows say whether it is switched off, so they are
        # stale the moment it is; an immediate poll would only redraw them 3 minutes
        # later, and only if it succeeded.
        self.store.tasks_changed.emit()
        self._refresh_autofix_ui()
        if on:
            self.store.run_autofix_poll_async()

    def _on_review_req_toggled(self, on: bool) -> None:
        self.store.review_requests_enabled = on
        self.store.changed.emit()
        self.store.tasks_changed.emit()
        self._refresh_autofix_ui()
        if on:
            self.store.run_autofix_poll_async()

    def _on_auto_approve_toggled(self, on: bool) -> None:
        self.store.auto_approve_enabled = on
        self.store.changed.emit()
        self._refresh_autofix_ui()

    def _on_soft_approve_toggled(self, on: bool) -> None:
        self.store.soft_approve_enabled = on
        self.store.changed.emit()

    def _set_verdict(self, which: str, on: bool) -> None:
        setattr(self.store, f"verdict_withhold_{which}", on)
        self.store.changed.emit()

    def _refresh_autofix_ui(self) -> None:
        autofix_on = self.store.pr_autofix_enabled
        review_on = self.store.review_requests_enabled

        self._autofix_status.setVisible(autofix_on)
        if autofix_on:
            st = self.store.autofix_status
            live = bool(st) and (time.time() - st.get("updatedAt", 0)) < 15 * 60
            if live:
                n = st.get("watching", 0)
                plural = "" if n == 1 else "s"
                self._autofix_status.setText(f"● Active — watching {n} open PR{plural}.")
                self._autofix_status.setStyleSheet("color: #34C759; font-size: 10px;")
            else:
                self._autofix_status.setText("○ Enabled, but no monitor has polled yet.")
                self._autofix_status.setStyleSheet("color: #FF9500; font-size: 10px;")

        err = self.store.autofix_poll_error
        self._autofix_poll_err.setVisible(bool(err) and autofix_on)
        if err:
            self._autofix_poll_err.setText(f"⚠ Polls failing — {err}")

        handled = self.store.review_requests_handled
        suffix = f"  Reviewed {handled} so far." if handled else ""
        self._review_req_hint.setText(
            "When someone requests my review, spawns the most thorough review (Full "
            "E2E, inline comments) — read-only, never touches their branch. A review "
            "left unaddressed is retried automatically until it lands. Off, the "
            "requests still list under Agent tasks, queued for you to start by "
            "hand." + suffix
        )

        n = self.store.unaddressed_reviews
        self._unaddressed.setVisible(review_on and n > 0)
        if n > 0:
            plural = "" if n == 1 else "s"
            self._unaddressed.setText(f"↻ {n} unaddressed review{plural} — retrying")

        self._approve_container.setVisible(review_on)
        self._verdict_container.setVisible(review_on and self.store.auto_approve_enabled)

    # MARK: Claude API-error watcher

    def _apiwatch_section(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(6)
        col.addWidget(_section_label("CLAUDE API ERRORS"))

        self._cb_apiwatch = QCheckBox("Auto-continue agents on API errors")
        self._cb_apiwatch.setChecked(self.store.api_watch_enabled)
        self._cb_apiwatch.toggled.connect(self._on_apiwatch_toggled)
        col.addWidget(self._cb_apiwatch)

        self._apiwatch_status = QLabel("")
        self._apiwatch_status.setWordWrap(True)
        self._apiwatch_status.setStyleSheet("font-size: 10px;")
        col.addWidget(self._apiwatch_status)

        hint = QLabel(
            "Watches every tmux pane; when a Claude API error shows up (e.g. “529 "
            "Overloaded”), it types “" + apiwatch.CONTINUE_MESSAGE + "” so a stalled "
            "agent resumes on its own. Out-of-quota stalls (“You've hit your weekly "
            "limit”) are left alone — nudging can't help until the limit resets. Run "
            "your agents inside tmux for this to reach them."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(muted(10))
        col.addWidget(hint)
        return col

    def _on_apiwatch_toggled(self, on: bool) -> None:
        self.store.api_watch_enabled = on
        self.store.changed.emit()
        self._refresh_apiwatch_ui()
        if on:
            self.store.run_apiwatch_poll_async()  # kick a scan immediately

    def _refresh_apiwatch_ui(self) -> None:
        on = self.store.api_watch_enabled
        self._apiwatch_status.setVisible(on)
        if not on:
            return
        count = self.store.api_watch_continues
        tail = f"  Continued {count}× so far." if count else ""
        st = self.store.apiwatch_status
        live = bool(st) and (time.time() - st.get("updatedAt", 0)) < 15 * 60
        if st is not None and not st.get("tmux", True):
            self._apiwatch_status.setText(
                "⚠ tmux not found — this watcher drives tmux panes; install tmux and "
                "run agents inside it." + tail
            )
            self._apiwatch_status.setStyleSheet("color: #FF9500; font-size: 10px;")
        elif live:
            n = st.get("watching", 0)
            plural = "" if n == 1 else "s"
            self._apiwatch_status.setText(
                f"● Active — watching {n} tmux pane{plural}." + tail
            )
            self._apiwatch_status.setStyleSheet("color: #34C759; font-size: 10px;")
        else:
            self._apiwatch_status.setText(
                "○ Enabled, but no scan has run yet." + tail
            )
            self._apiwatch_status.setStyleSheet("color: #FF9500; font-size: 10px;")

    # MARK: tool colour & visibility

    def _tools_section(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(8)
        col.addWidget(_section_label("TOOLS — COLOR & VISIBILITY"))
        for tool in tools():
            col.addLayout(self._tool_row(tool.id, tool.title, tool.subtitle, tool.glyph))
        return col

    def _tool_row(self, tool_id: str, title: str, subtitle: str, glyph: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(8)
        chip = IconChip(glyph, self.store.tint(tool_id), size=22)
        self._chips[tool_id] = chip
        row.addWidget(chip)

        text = QVBoxLayout()
        text.setSpacing(1)
        t = QLabel(title)
        t.setStyleSheet("font-weight: 600; font-size: 11px;")
        s = QLabel(subtitle)
        s.setStyleSheet(muted(9))
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
        self._alloc_detail.setStyleSheet(muted(9, mono=True))
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
            "Set DIPLOMAT_DEVICE_ALLOCATOR_DIR to point at it."
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
        outdated = bool(s.get("outdated"))
        version = s.get("version") or "?"

        def mark(b: object) -> str:
            return "✓" if b else "✗"

        # "Installed" alone would be a true statement about a machine still running
        # the copies some earlier checkout laid down, so the stale case says so and
        # names what drifted. Amber, not green: it is working, but not from here.
        self._alloc_status.setText(
            f"Out of date (v{version})" if outdated
            else f"Installed · v{version}" if installed
            else "Not installed"
        )
        self._alloc_status.setStyleSheet(
            "font-weight: 700; font-size: 11px;"
            + (" color: #FF9500;" if outdated else "")
        )
        detail = (
            f"MCP {mark(s.get('mcpRegistered'))} · skill {mark(s.get('skillInstalled'))}"
            f" · rule {mark(s.get('ruleInstalled'))} · CLAUDE.md {mark(s.get('claudeMdInjected'))}"
        )
        drift = s.get("drift") or []
        if outdated and drift:
            detail += f"  ⟳ stale: {', '.join(str(d) for d in drift)}"
        self._alloc_detail.setText(detail)
        self._alloc_install.setText(
            "Update" if outdated else "Reinstall" if installed else "Install"
        )
        self._alloc_uninstall.setVisible(installed)
        self._alloc_daemon.setVisible(bool(s.get("daemonRunning")))

    # MARK: mesh (LAN P2P duty coordination)

    def _mesh_section(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(6)
        col.addWidget(_section_label("MESH (LAN P2P)"))

        toggle = QCheckBox("Coordinate duties with other machines on this LAN")
        toggle.setChecked(self.store.mesh_enabled)
        # Dead without the add-on: the mesh is SzpontNet, and there is no node for
        # this switch to start. Shown-but-disabled rather than hidden, so the
        # feature is discoverable and the status line below can say what is missing.
        toggle.setEnabled(szpont.AVAILABLE)
        toggle.toggled.connect(self._on_mesh_toggled)
        col.addWidget(toggle)

        self._mesh_status = QLabel("")
        self._mesh_status.setStyleSheet("font-weight: 700; font-size: 11px;")
        col.addWidget(self._mesh_status)

        hint = QLabel(
            "Runs a small peer-to-peer node that discovers the other Diplomat "
            "machines on your LAN (UDP beacons) and routes duty work — reviews, "
            "conflict fixes, the full E2E audit — to whichever node fits the "
            "placement policy (surplus-first by default, token- and platform-aware). "
            "Configure the whole mesh from the ⬡ Mesh screen (the ⬡ button in the "
            "panel header). "
            "Off by default; no node opens on the network until you enable it here."
            if szpont.AVAILABLE else
            # The ⬡ screen and its header button are not built without the add-on,
            # so pointing at them here would send the reader looking for a control
            # that isn't there.
            "Coordinating duties across machines is an add-on: it needs the "
            f"SzpontNet library, which is not installed (looked for "
            f"{szpont.package_dir()}). Everything else in Diplomat runs on this "
            "machine alone and is unaffected."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(muted(10))
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
        # Before ``mesh_enabled``, which is also False here — the disabled toggle
        # above needs a reason beside it, and "Off" reads as a choice the user made.
        if not szpont.AVAILABLE:
            self._mesh_status.setText("SzpontNet not installed")
            self._mesh_status.setStyleSheet(
                "font-weight: 700; font-size: 11px; color: palette(mid);"
            )
            return
        if not self.store.mesh_enabled:
            self._mesh_status.setText("Off")
            self._mesh_status.setStyleSheet(
                "font-weight: 700; font-size: 11px; color: palette(mid);"
            )
            return

        from szpontnet import statefile

        state = self.store.mesh_state
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

    # MARK: applet update

    def _update_section(self) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(6)
        col.addWidget(_section_label("UPDATE"))

        self._update_status = QLabel("Checking…")
        self._update_status.setStyleSheet("font-weight: 700; font-size: 11px;")
        col.addWidget(self._update_status)

        self._update_detail = QLabel("comparing with origin…")
        self._update_detail.setWordWrap(True)
        self._update_detail.setStyleSheet(
            muted(9, mono=True)
        )
        col.addWidget(self._update_detail)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._update_btn = QPushButton("Update")
        self._update_btn.setStyleSheet("font-weight: 700;")
        self._update_btn.setEnabled(False)
        self._update_btn.clicked.connect(self.store.update_applet_async)
        btn_row.addWidget(self._update_btn)
        recheck = QPushButton("⟲")
        recheck.setFixedWidth(34)
        recheck.setToolTip("Re-check for updates")
        recheck.clicked.connect(self.store.refresh_update_status_async)
        btn_row.addWidget(recheck)
        btn_row.addStretch(1)
        col.addLayout(btn_row)

        hint = QLabel(
            "Pulls the latest applet from GitHub, rebuilds the diplomat-core "
            "prompt engine, and relaunches the tray app in place."
        )
        hint.setWordWrap(True)
        hint.setStyleSheet(muted(10))
        col.addWidget(hint)
        return col

    def _refresh_update_ui(self) -> None:
        s = self.store.update_state or {"phase": "checking"}
        phase = s.get("phase")

        def status(text: str, color: str | None = None) -> None:
            suffix = f" color: {color};" if color else ""
            self._update_status.setText(text)
            self._update_status.setStyleSheet(
                f"font-weight: 700; font-size: 11px;{suffix}"
            )

        if phase == "checking":
            status("Checking…")
            self._update_detail.setText("comparing with origin…")
            self._update_btn.setEnabled(False)
        elif phase == "updating":
            status("Updating…", "#FF9500")
            self._update_detail.setText(s.get("step") or "")
            self._update_btn.setEnabled(False)
        elif phase == "restarting":
            status("Restarting…", "#34C759")
            self._update_detail.setText(
                f"relaunched at {s.get('commit')} — this instance is handing over"
            )
            self._update_btn.setEnabled(False)
        elif phase == "error":
            status("Update failed", "#FF3B30")
            self._update_detail.setText(s.get("error") or "unknown error")
            self._update_btn.setEnabled(True)
        elif s.get("error"):
            status("Check failed", "#FF9500")
            self._update_detail.setText(s["error"])
            self._update_btn.setEnabled(True)
        else:
            behind = s.get("behind") or 0
            ahead = s.get("ahead") or 0
            if behind:
                plural = "" if behind == 1 else "s"
                status(f"Update available · {behind} commit{plural} behind", "#0A84FF")
            else:
                status("Up to date")
            detail = f"{s.get('commit')} on {s.get('branch')} · upstream {s.get('upstream')}"
            if ahead:
                # A diverged checkout still updates — via a merge, not a discard.
                detail += f" · {ahead} local ahead (will merge)"
            self._update_detail.setText(detail)
            self._update_btn.setEnabled(True)

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
        hint.setStyleSheet(muted(10))
        col.addWidget(hint)
        return col
