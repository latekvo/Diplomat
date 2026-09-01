"""Settings screen — identity, what the monitors may do, and the environment
spawns land in.

The Linux analogue of SettingsView.swift, card for card and row for row. Persists
through the Store (QSettings, or the shared ``~/.diplomat/config.json`` for the
knobs another process also reads). Built once and updated in place so typing in
the handle field is never disrupted by a background data refresh.

Each row states what it does in a line; the paragraph behind that line is drawn
only while the header's *Explain* switch is on.
"""

from __future__ import annotations

import os
import shutil
import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from diplomat_runtime import (
    agentstate,
    apiwatch,
    appconfig,
    autofix,
    core,
    review,
    runner,
)
from . import (
    deviceallocator,
    glyphs,
    szpont,
)
from .store import Store, tools
from .widgets import (
    ChoiceChips,
    IconChip,
    Pill,
    SegmentedControl,
    SettingRow,
    SliderSetting,
    SwitchToggle,
    ToggleChip,
    muted,
    nested_settings,
    settings_card,
)

#: Card tints, mirroring the SF Symbol tints of the macOS twin.
_BLUE, _PURPLE, _TEAL = "#0A84FF", "#BF5AF2", "#40C8E0"
_ORANGE, _PINK, _INDIGO = "#FF9500", "#FF375F", "#5E5CE6"
_BROWN, _CYAN, _MINT = "#AC8E68", "#32ADE6", "#00C7BE"
_GREEN, _RED, _GREY = "#34C759", "#FF3B30", "#8E8E93"


def _style_swatch(btn: QPushButton, hex_color: str) -> None:
    """The tool tint well: the colour itself, as a rounded block. A "●" glyph in the
    tint (what this was) is mostly the button's own background."""
    btn.setStyleSheet(
        f"QPushButton {{ background-color: {hex_color}; border-radius: 4px;"
        " border: 1px solid rgba(128,128,128,0.5); }"
    )


class SettingsView(QWidget):
    done = Signal()

    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self._chips: dict[str, IconChip] = {}
        #: Every row that has a long-form paragraph, so the header switch can
        #: reveal them together rather than each row watching the store.
        self._rows: list[SettingRow] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(12)

        root.addLayout(self._header_row())

        # Two columns, matching the macOS SettingsView and the main panel: identity +
        # automation behaviour on the left, appearance + environment on the right. Each
        # column pushes its cards up with a bottom stretch so the two stay top-aligned
        # regardless of differing heights.
        body = QHBoxLayout()
        body.setSpacing(12)

        left = QVBoxLayout()
        left.setSpacing(10)
        left.addWidget(self._identity_card())
        left.addWidget(self._runner_card())
        left.addWidget(self._repo_card())
        left.addWidget(self._autofix_card())
        left.addWidget(self._limits_card())
        left.addStretch(1)

        right = QVBoxLayout()
        right.setSpacing(10)
        right.addWidget(self._tools_card())
        right.addWidget(self._terminal_card())
        right.addWidget(self._apiwatch_card())
        right.addWidget(self._allocator_card())
        right.addWidget(self._mesh_card())
        right.addWidget(self._update_card())
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
        self._apply_explain(store.settings_explain)
        store.refresh_allocator_install_async()
        store.refresh_update_status_async()
        if store.mesh_enabled:
            # Only touch the mesh state file when the user actually uses the mesh;
            # otherwise this is a needless real-HOME read on every Settings open
            # (and in non-mesh render/test paths). The Panel's own poll keeps
            # mesh_state fresh while a node is live.
            store.refresh_mesh_state()

    def _track(self, row: SettingRow) -> SettingRow:
        self._rows.append(row)
        return row

    # MARK: header

    def _header_row(self) -> QHBoxLayout:
        row = QHBoxLayout()
        title = QLabel("⚙  Settings")
        title.setStyleSheet("font-weight: 700; font-size: 13px;")
        row.addWidget(title)
        row.addStretch(1)

        # One switch for the screen, not a disclosure arrow per row: the paragraphs
        # are read together, on the visit where the automation is being set up, and
        # never again after it.
        explain_label = QLabel("Explain")
        explain_label.setStyleSheet(muted(11))
        row.addWidget(explain_label)
        self._explain = SwitchToggle()
        self._explain.setToolTip("Show what each setting does, in full")
        self._explain.setChecked(self.store.settings_explain)
        self._explain.toggled.connect(self._on_explain_toggled)
        row.addWidget(self._explain)

        done = QPushButton("Done")
        done.setStyleSheet("font-weight: 700;")
        done.clicked.connect(self.done.emit)
        row.addWidget(done)
        return row

    def _on_explain_toggled(self, on: bool) -> None:
        self.store.settings_explain = on
        self._apply_explain(on)

    def _apply_explain(self, on: bool) -> None:
        for row in self._rows:
            row.set_explain(on)

    # MARK: GitHub identity

    def _identity_card(self) -> QWidget:
        card, body, self._identity_pill = settings_card(
            glyphs.G_IDENTITY, "IDENTITY", _BLUE
        )

        field = QLineEdit(self.store.username_override)
        field.setPlaceholderText(self.store.me or "your github handle")
        field.setClearButtonEnabled(True)

        self._identity_row = self._track(SettingRow(
            "GitHub username", field, stacked=True,
            detail="Scopes the “My …” tools and the Review wizard: which PRs count "
                   "as mine, and whose reviews the monitors owe.",
        ))
        body.addWidget(self._identity_row)

        def refresh() -> None:
            override = self.store.username_override.strip()
            effective = override or self.store.me
            self._identity_pill.set_state(
                f"@{effective}" if effective else "not signed in",
                _BLUE if override else _GREY,
            )
            self._identity_row.set_summary(
                "Overriding the gh-authenticated user."
                if override else "Blank = whoever `gh` is authenticated as."
            )

        def on_text(text: str) -> None:
            self.store.username_override = text
            refresh()
            self.store.changed.emit()

        field.textChanged.connect(on_text)
        refresh()
        return card

    # MARK: agent runner (which CLI the agents are)

    def _runner_card(self) -> QWidget:
        card, body, self._runner_pill = settings_card(
            glyphs.G_RUNNER, "AGENT RUNNER", _PURPLE
        )

        picker = SegmentedControl(
            [(runner.LABELS[key], key) for key in runner.RUNNERS], tint=_PURPLE
        )
        picker.set_value(self.store.agent_runner)
        picker.changed.connect(self._on_runner_changed)
        self._runner_row = self._track(SettingRow(
            "Which CLI a spawn runs", picker, stacked=True,
        ))
        body.addWidget(self._runner_row)

        # The model + provider controls, shown only for a runner that has them.
        self._runner_nest, nest = nested_settings(_PURPLE)
        model_row = QHBoxLayout()
        model_row.setSpacing(6)
        self._model_field = QLineEdit(self.store.agent_model)
        self._model_field.setClearButtonEnabled(True)
        self._model_field.textChanged.connect(
            lambda text: setattr(self.store, "agent_model", text)
        )
        model_row.addWidget(self._model_field, 1)
        providers = QPushButton("Connect a provider…")
        providers.setToolTip(
            "Open the runner's own login wizard: it knows every provider in its "
            "catalog and stores the credential itself. Diplomat never holds an API key."
        )
        providers.clicked.connect(self._open_provider_setup)
        model_row.addWidget(providers)
        holder = QWidget()
        holder.setLayout(model_row)
        nest.addWidget(holder)
        body.addWidget(self._runner_nest)

        self._refresh_runner_ui()
        return card

    def _on_runner_changed(self, key: object) -> None:
        self.store.agent_runner = str(key)
        self._refresh_runner_ui()

    def _refresh_runner_ui(self) -> None:
        """Show the model and provider controls only for a runner that has them, and
        say which CLI has to be on PATH.

        A missing binary is otherwise near-silent: the window opens, the shell prints
        "command not found", the completion sentinel takes the 127 straight away, and
        the applet records a run that finished in a second without doing anything."""
        chosen = self.store.agent_runner
        label = runner.LABELS.get(chosen, chosen)
        foreign = chosen != runner.CLAUDE
        self._runner_pill.set_state(label, _PURPLE)
        self._runner_nest.setVisible(foreign)
        self._model_field.setPlaceholderText(f"model — blank lets {label} choose")
        found = shutil.which(chosen)
        if found:
            where = f"Spawns run `{chosen}` ({found})."
        else:
            where = (f"`{chosen}` is not on this app's PATH. Agents run under your "
                     f"login shell, so an rc-only install still works — but check it "
                     f"if spawned runs finish instantly without doing anything.")
        self._runner_row.set_summary(where, color=None if found else _ORANGE)
        self._runner_row.set_detail(
            f"Diplomat never holds an API key: {label} stores its own credential, and "
            f"*Connect a provider* opens its login wizard, which knows every provider "
            f"in its catalog."
            if foreign else
            "SPAWN AGENT picks up whatever flags your shell alias for `claude` gives it."
        )

    def _open_provider_setup(self) -> None:
        try:
            review.open_terminal(runner.setup_command(), self.store.terminal)
        except review.SpawnError as exc:
            self._runner_row.set_summary(f"Could not open a terminal: {exc}",
                                         color=_ORANGE)

    # MARK: repo root (where the agents work)

    def _repo_card(self) -> QWidget:
        card, body, self._repo_pill = settings_card(glyphs.G_REPO, "REPO ROOT", _TEAL)

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
        holder = QWidget()
        holder.setLayout(row)

        self._repo_row = self._track(SettingRow(
            "Where every spawned agent starts", holder, stacked=True,
        ))
        body.addWidget(self._repo_row)

        def on_text(text: str) -> None:
            self.store.repo_path_override = text
            self._refresh_repo_ui()

        self._repo_field.textChanged.connect(on_text)
        self._refresh_repo_ui()
        return card

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
        """Pill, summary and colour, from one state read so the three can't disagree.

        A problem states itself on the face of the card; only the happy path is short
        enough to fold into the summary line and put the rest behind *Explain*."""
        state = self._repo_state()
        resolved = review.repo_path()
        owner, repo = core.config()["owner"], core.config()["repo"]
        default = review.default_repo_path()
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
            text = f"`cd {resolved}` — your local clone of {owner}/{repo}."
        ok = state == "ok"
        self._repo_pill.set_state("checkout ok" if ok else "check this",
                                  _GREEN if ok else _ORANGE)
        self._repo_row.set_summary(text, color=None if ok else _ORANGE)
        self._repo_row.set_detail(
            f"Blank = the default path, {default}. DIPLOMAT_REPO in this app's "
            "environment outranks both." if ok else None
        )

    # MARK: PR auto-fix monitor

    def _autofix_card(self) -> QWidget:
        card, body, self._autofix_pill = settings_card(
            glyphs.G_AUTO, "AUTOMATIC WORK", _ORANGE
        )

        self._sw_autofix = SwitchToggle(_ORANGE)
        self._sw_autofix.setChecked(self.store.pr_autofix_enabled)
        self._sw_autofix.toggled.connect(self._on_autofix_toggled)
        body.addWidget(self._track(SettingRow(
            "Auto-queue fixes for my PRs", self._sw_autofix,
            summary="Merge conflicts and new review threads.",
            detail="Off, the monitor still lists what it finds under Agent tasks — "
                   "queued, for “execute now” only.",
        )))

        # The failing-poll line. The card's pill flags it; this names the error.
        self._autofix_poll_err = QLabel("")
        self._autofix_poll_err.setWordWrap(True)
        self._autofix_poll_err.setStyleSheet(f"color: {_RED}; font-size: 10px;")
        body.addWidget(self._autofix_poll_err)

        # Two counts that only exist once the monitor has run: how many reviews it
        # has delivered, and how many it currently owes.
        self._reviewed_pill = Pill("")
        self._sw_review_req = SwitchToggle(_ORANGE)
        self._sw_review_req.setChecked(self.store.review_requests_enabled)
        self._sw_review_req.toggled.connect(self._on_review_req_toggled)
        review_control = QWidget()
        review_row = QHBoxLayout(review_control)
        review_row.setContentsMargins(0, 0, 0, 0)
        review_row.setSpacing(6)
        review_row.addWidget(self._reviewed_pill)
        review_row.addWidget(self._sw_review_req)
        body.addWidget(self._track(SettingRow(
            "Auto-queue reviews that request me", review_control,
            summary="Full E2E · max, inline comments — read-only, never their branch.",
            detail="A review that never lands (agent died, window closed) is retried. "
                   "Off, the requests still list under Agent tasks, queued for "
                   "“execute now” only.",
        )))

        # What an auto-review may submit. Nested under the switch that creates them,
        # because none of it means anything while no auto-review runs.
        self._approve_nest, approve = nested_settings(_ORANGE)
        self._sw_auto_approve = SwitchToggle(_ORANGE)
        self._sw_auto_approve.setChecked(self.store.auto_approve_enabled)
        self._sw_auto_approve.toggled.connect(self._on_auto_approve_toggled)
        approve.addWidget(self._track(SettingRow(
            "May approve / request changes", self._sw_auto_approve,
            summary="Off ⇒ inline comments only; the verdict stays with you.",
            detail="On ⇒ a clean review may submit a verdict, except on the classes "
                   "withheld below.",
        )))
        approve.addWidget(self._verdict_block())

        self._sw_soft_approve = SwitchToggle(_ORANGE)
        self._sw_soft_approve.setChecked(self.store.soft_approve_enabled)
        self._sw_soft_approve.toggled.connect(self._on_soft_approve_toggled)
        approve.addWidget(self._track(SettingRow(
            "Soft-approve clean PRs", self._sw_soft_approve,
            summary="One “ran the sweep, all clean” comment — never an APPROVE.",
            detail="Off ⇒ a review that finds nothing says nothing. Independent of "
                   "the verdict switch above: a soft approval is a comment, not a "
                   "GitHub approval.",
        )))
        body.addWidget(self._approve_nest)
        return card

    def _verdict_block(self) -> QWidget:
        """The three suppressors for an auto-review's final verdict, as chips: a PR
        matching any lit chip gets comments only."""
        self._verdict_container = QWidget()
        col = QVBoxLayout(self._verdict_container)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(5)
        caption = QLabel("WITHHOLD IT WHEN THE PR TOUCHES…")
        caption.setStyleSheet(
            "color: palette(mid); font-size: 9px; font-weight: 700;"
            " letter-spacing: 0.5px;"
        )
        col.addWidget(caption)

        chips = QHBoxLayout()
        chips.setSpacing(5)
        for label, which, tip in (
            ("a SKILL", "skill", "Comments only on a PR that edits a SKILL"),
            ("the installer", "installer", "Comments only on a PR that edits the installer"),
            ("community", "community",
             "Comments only on a PR whose author is outside the org"),
        ):
            chip = ToggleChip(label, tint=_ORANGE)
            chip.setToolTip(tip)
            chip.setChecked(getattr(self.store, f"verdict_withhold_{which}"))
            chip.toggled.connect(
                lambda on, w=which: self._set_verdict(w, on)
            )
            chips.addWidget(chip)
        chips.addStretch(1)
        col.addLayout(chips)
        return self._verdict_container

    # MARK: limits (the cap, and the budget it is spent against)

    def _limits_card(self) -> QWidget:
        """How much automatic work this machine may run, and whether it can afford
        it. Its own card, beside the monitors rather than under them: both rows
        bound *every* automatic agent — the two monitors and anything a mesh peer
        routes here."""
        card, body, self._limits_pill = settings_card(glyphs.G_LIMITS, "LIMITS", _ORANGE)

        self._auto_limit = SliderSetting(
            label="Run at most",
            minimum=autofix.MIN_AUTO_TASK_LIMIT,
            maximum=autofix.MAX_AUTO_TASK_LIMIT,
            min_label=str(autofix.MIN_AUTO_TASK_LIMIT),
            max_label=str(autofix.MAX_AUTO_TASK_LIMIT),
            tint=_ORANGE,
        )
        self._auto_limit.set_badge_text(
            lambda n: f"{n} task" + ("" if n == 1 else "s")
        )
        self._auto_limit.set_value(self.store.auto_task_limit)
        self._auto_limit.changed.connect(self._on_auto_limit_changed)
        body.addWidget(self._track(SettingRow(
            "Run at most", self._auto_limit, stacked=True,
            summary="Across both monitors, a PR sweep's reviews, and anything a mesh "
                    "peer routes here.",
            detail="The agent a wizard press opens on the spot is outside the cap; a "
                   "review it queues instead is inside. Work over the cap waits in "
                   "Agent tasks, in the order you put it, and starts when a bay frees "
                   "— unless you switch off Auto-execute queue there.",
        )))

        self._sw_budget = SwitchToggle(_ORANGE)
        self._sw_budget.setChecked(appconfig.auto_budget_gate())
        self._sw_budget.toggled.connect(self._on_budget_gate_toggled)
        body.addWidget(self._track(SettingRow(
            "Hold work when the limit runs low", self._sw_budget,
            summary="Wait for a window to refill rather than start what won't fit.",
            detail="Priced from Telemetry → limit per task: against both rate-limit "
                   "windows under Claude Code, or — under a runner billed in money — "
                   "against what one task costs on the model it runs, and what your "
                   "OpenRouter key and credit balance have left. Higher confidence is "
                   "stricter. Held work waits in Agent tasks, and “execute now” "
                   "overrides it. Nothing is held while the probe can't read a limit.",
        )))

        self._budget_knobs, knobs = nested_settings(_ORANGE)
        self._budget_confidence = SegmentedControl(
            [(f"{level}%", level) for level in sorted(autofix.BUDGET_CONFIDENCE_Z)],
            tint=_ORANGE,
        )
        self._budget_confidence.set_value(appconfig.auto_budget_confidence())
        self._budget_confidence.changed.connect(self._on_budget_confidence_changed)
        knobs.addWidget(self._track(SettingRow(
            "Start one only when it fits", self._budget_confidence, stacked=True,
        )))

        self._budget_floor = SliderSetting(
            label="Keep in hand until it can be priced",
            minimum=0, maximum=100, step=5,
            min_label="spend it all", max_label="spend nothing", tint=_ORANGE,
        )
        self._budget_floor.set_badge_text(lambda pct: f"{pct:.1f}%")
        self._budget_floor.set_value(round(appconfig.auto_budget_floor_pct()))
        self._budget_floor.changed.connect(self._on_budget_floor_changed)
        knobs.addWidget(self._track(SettingRow(
            "Keep in hand until it can be priced", self._budget_floor, stacked=True,
            summary="Of each rate-limit window, under Claude Code.",
        )))

        # The same knob in the other currency. Both are shown whichever runner is
        # selected: the setting outlives the choice of runner, and a knob that
        # appeared and disappeared as that changed would look like it had been reset.
        self._budget_reserve = SliderSetting(
            label="Keep on the account until it can be priced",
            minimum=0, maximum=int(autofix.MAX_BUDGET_RESERVE_USD), step=5,
            min_label="spend it all", max_label="spend nothing", tint=_ORANGE,
        )
        self._budget_reserve.set_badge_text(lambda usd: f"${usd:.2f}")
        self._budget_reserve.set_value(round(appconfig.auto_budget_reserve_usd()))
        self._budget_reserve.changed.connect(self._on_budget_reserve_changed)
        knobs.addWidget(self._track(SettingRow(
            "Keep on the account until it can be priced", self._budget_reserve,
            stacked=True,
            summary="Of your OpenRouter balance, under a runner billed in money.",
        )))
        body.addWidget(self._budget_knobs)
        self._budget_knobs.setVisible(self._sw_budget.isChecked())
        self._refresh_limits_pill()
        return card

    def _refresh_limits_pill(self) -> None:
        self._limits_pill.set_state(f"≤ {self.store.auto_task_limit} at a time", _GREY)

    def _on_auto_limit_changed(self, value: int) -> None:
        self.store.auto_task_limit = value
        self._refresh_limits_pill()
        self.store.changed.emit()

    def _on_budget_gate_toggled(self, on: bool) -> None:
        appconfig.set_bool(appconfig.AUTO_BUDGET_GATE, on)
        self._budget_knobs.setVisible(on)
        self.store.changed.emit()

    def _on_budget_confidence_changed(self, level: object) -> None:
        appconfig.set_int(appconfig.AUTO_BUDGET_CONFIDENCE, int(level))
        self.store.changed.emit()

    def _on_budget_floor_changed(self, value: int) -> None:
        appconfig.set_float(appconfig.AUTO_BUDGET_FLOOR_PCT, float(value))
        self.store.changed.emit()

    def _on_budget_reserve_changed(self, value: int) -> None:
        appconfig.set_float(appconfig.AUTO_BUDGET_RESERVE_USD, float(value))
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
        """The monitors' own health on the card's pill, so it is answered before any
        row is read. A failing poll used to be invisible: the switches said "on", the
        counts froze stale, and nothing dispatched."""
        autofix_on = self.store.pr_autofix_enabled
        review_on = self.store.review_requests_enabled
        err = self.store.autofix_poll_error
        st = self.store.autofix_status
        live = bool(st) and (time.time() - st.get("updatedAt", 0)) < 15 * 60

        if not (autofix_on or review_on):
            self._autofix_pill.set_state("manual", _GREY)
        elif err:
            self._autofix_pill.set_state("polls failing", _RED)
        elif live:
            n = st.get("watching", 0)
            self._autofix_pill.set_state(
                f"watching {n} PR" + ("" if n == 1 else "s"), _GREEN
            )
        else:
            self._autofix_pill.set_state("no monitor yet", _ORANGE)

        self._autofix_poll_err.setVisible(bool(err) and autofix_on)
        if err:
            self._autofix_poll_err.setText(f"⚠ Polls failing — {err}")

        owed = self.store.unaddressed_reviews
        handled = self.store.review_requests_handled
        if review_on and owed > 0:
            self._reviewed_pill.set_state(f"↻ {owed} owed", _ORANGE)
            self._reviewed_pill.setToolTip(
                f"{owed} unaddressed review" + ("" if owed == 1 else "s")
                + " — the reconciler is retrying"
            )
        elif handled:
            self._reviewed_pill.set_state(f"{handled} done", _GREY)
            self._reviewed_pill.setToolTip("Reviews delivered so far")
        else:
            self._reviewed_pill.set_state("")

        self._approve_nest.setVisible(review_on)
        self._verdict_container.setVisible(review_on and self.store.auto_approve_enabled)

    # MARK: Claude API-error watcher

    def _apiwatch_card(self) -> QWidget:
        card, body, self._apiwatch_pill = settings_card(
            glyphs.G_STALLED, "STALLED AGENTS", _PINK
        )
        self._sw_apiwatch = SwitchToggle(_PINK)
        self._sw_apiwatch.setChecked(self.store.api_watch_enabled)
        self._sw_apiwatch.toggled.connect(self._on_apiwatch_toggled)
        self._apiwatch_row = self._track(SettingRow(
            "Auto-continue on API errors", self._sw_apiwatch,
            summary="A 529 stops an agent dead; this types it back into motion.",
            detail="Watches the tmux panes an agent is running in and types “"
                   + apiwatch.CONTINUE_MESSAGE
                   + "” when a Claude API error shows up. A pane with nobody's agent in "
                   "it is left alone whatever it shows — the nudge is submitted as a "
                   "line of input, which in a plain shell would run as a command. "
                   "Out-of-quota stalls (“You've hit your weekly limit”) are left alone "
                   "too — nudging can't help until the limit resets. Run your agents "
                   "inside tmux for this to reach them. Claude Code runs only: the banners it matches are Claude "
                   "Code's. An OpenCode or Hermes agent that hits an error reads as "
                   "idle instead, frees its task-cap slot, and is dispatched again by "
                   "whichever monitor owed the work.",
        ))
        body.addWidget(self._apiwatch_row)

        # Beside the nudge because they are the two answers to one question. The nudge
        # gets a stalled agent moving again; this is what happens when nothing does.
        cutoff = apiwatch.human_interval(agentstate.RUN_DEADLINE)
        self._sw_deadline = SwitchToggle(_PINK)
        self._sw_deadline.setChecked(appconfig.run_deadline() is not None)
        self._sw_deadline.toggled.connect(self._on_run_deadline_toggled)
        body.addWidget(self._track(SettingRow(
            f"Give up on a task after {cutoff}", self._sw_deadline,
            summary="Hand its bay back when the agent's own report never arrives.",
            detail="Agents report their turn boundaries through hooks staged into each "
                   "run, and a run whose report never comes — a runner without hooks, "
                   "settings that would not stage, an agent wedged with its status bar "
                   "frozen — holds a task-cap bay until you close its window. Past "
                   f"{cutoff} such a run is called done whatever its screen still "
                   "shows: its tmux session is killed, its row leaves Agent tasks and "
                   "its bay comes back. Only while your account has limit left to "
                   "spend — agents parked waiting for a window to refill age exactly "
                   "like stuck ones. A peer's run is left to the machine running it, "
                   "and so is an agent you started by hand.",
        )))
        return card

    def _on_run_deadline_toggled(self, on: bool) -> None:
        appconfig.set_bool(appconfig.RUN_DEADLINE, on)
        self.store.changed.emit()

    def _on_apiwatch_toggled(self, on: bool) -> None:
        self.store.api_watch_enabled = on
        self.store.changed.emit()
        self._refresh_apiwatch_ui()
        if on:
            self.store.run_apiwatch_poll_async()  # kick a scan immediately

    def _refresh_apiwatch_ui(self) -> None:
        count = self.store.api_watch_continues
        if not self.store.api_watch_enabled:
            self._apiwatch_pill.set_state(
                f"{count} continued" if count else "off", _GREY
            )
            return
        st = self.store.apiwatch_status
        live = bool(st) and (time.time() - st.get("updatedAt", 0)) < 15 * 60
        if st is not None and not st.get("tmux", True):
            self._apiwatch_pill.set_state("tmux not found", _ORANGE)
            self._apiwatch_row.set_summary(
                "This watcher drives tmux panes; install tmux and run agents inside it.",
                color=_ORANGE,
            )
            return
        self._apiwatch_row.set_summary(
            "A 529 stops an agent dead; this types it back into motion."
        )
        if live:
            n = st.get("watching", 0)
            self._apiwatch_pill.set_state(
                f"watching {n} pane" + ("" if n == 1 else "s"), _GREEN
            )
        else:
            self._apiwatch_pill.set_state(
                f"{count} continued" if count else "no scan yet", _ORANGE
            )

    # MARK: tool colour & visibility

    def _tools_card(self) -> QWidget:
        card, body, self._tools_pill = settings_card(glyphs.G_TOOLS, "TOOLS", _INDIGO)
        rows = QWidget()
        col = QVBoxLayout(rows)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(5)
        self._tool_rows: dict[str, QWidget] = {}
        for tool in tools():
            col.addWidget(self._tool_row(tool.id, tool.title, tool.subtitle, tool.glyph))
        body.addWidget(self._track(SettingRow(
            "Cards in the panel grid", rows, stacked=True,
            detail="The tint colours the card and every result row under it. Hiding "
                   "the selected tool selects the first one still shown.",
        )))
        self._refresh_tools_pill()
        return card

    def _refresh_tools_pill(self) -> None:
        total = len(tools())
        shown = sum(1 for t in tools() if t.id not in self.store.hidden_tools)
        self._tools_pill.set_state(f"{shown} of {total} shown", _GREY)

    def _tool_row(self, tool_id: str, title: str, subtitle: str, glyph: str) -> QWidget:
        """One tool. The whole row dims while it is hidden, so which cards the grid
        will actually draw reads off the column without checking six switches."""
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(6, 4, 6, 4)
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

        color_btn = QPushButton()
        color_btn.setFixedSize(34, 16)
        color_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        color_btn.setToolTip(f"Tint for {title}")
        _style_swatch(color_btn, self.store.tint(tool_id))
        color_btn.clicked.connect(lambda: self._pick_color(tool_id, color_btn))
        row.addWidget(color_btn)

        toggle = SwitchToggle(self.store.tint(tool_id))
        toggle.setChecked(tool_id not in self.store.hidden_tools)
        toggle.setToolTip(f"Show {title} in the grid")
        toggle.toggled.connect(lambda on, tid=tool_id: self._on_tool_toggled(tid, on))
        row.addWidget(toggle)

        self._tool_rows[tool_id] = host
        self._style_tool_row(tool_id)
        return host

    def _on_tool_toggled(self, tool_id: str, on: bool) -> None:
        self.store.set_tool(tool_id, on)
        self._style_tool_row(tool_id)
        self._refresh_tools_pill()

    def _style_tool_row(self, tool_id: str) -> None:
        visible = tool_id not in self.store.hidden_tools
        host = self._tool_rows[tool_id]
        host.setStyleSheet(
            "background-color: rgba(128,128,128,0.07); border-radius: 7px;"
            if visible else "background-color: transparent;"
        )
        # Qt has no view-wide opacity in a stylesheet; the chip carries the "off"
        # rendering it already has for the panel's hidden tools.
        self._chips[tool_id].set_active(visible)

    def _pick_color(self, tool_id: str, btn: QPushButton) -> None:
        initial = QColor(self.store.tint(tool_id))
        chosen = QColorDialog.getColor(initial, self, f"Tint for {tool_id}")
        if chosen.isValid():
            hex_color = chosen.name(QColor.NameFormat.HexRgb).upper()
            self.store.set_tint(hex_color, tool_id)
            _style_swatch(btn, hex_color)
            chip = self._chips.get(tool_id)
            if chip:
                chip.set_tint(hex_color)

    # MARK: terminal

    def _terminal_card(self) -> QWidget:
        card, body, pill = settings_card(glyphs.G_TERMINAL, "SPAWN TERMINAL", _BROWN)
        # Chips rather than a segmented row: Linux knows seven terminals, and which
        # of them are actually on this machine is the whole question — a dropdown
        # hides six of the answers behind a click.
        picker = ChoiceChips(
            [(term.title, term.key) for term in review.TERMINALS],
            columns=3, tint=_BROWN,
        )
        for term in review.TERMINALS:
            chip = picker.chip(term.key)
            if chip is not None and not term.is_installed:
                chip.setToolTip("Not installed — spawns fall back to the first that is")
                chip.setEnabled(False)
        picker.set_value(self.store.terminal_choice)
        picker.changed.connect(self._on_terminal_changed)
        self._terminal_pill = pill
        self._terminal_row = self._track(SettingRow(
            "Window SPAWN AGENT opens", picker, stacked=True,
            detail="A greyed chip is a terminal this machine does not have. The "
                   "spawn resolves to the first installed one, and to xterm if none "
                   "of them is.",
        ))
        body.addWidget(self._terminal_row)
        self._refresh_terminal_ui()
        return card

    def _on_terminal_changed(self, key: object) -> None:
        self.store.terminal_choice = str(key)
        self._refresh_terminal_ui()

    def _refresh_terminal_ui(self) -> None:
        resolved = review.resolved(self.store.terminal)
        self._terminal_pill.set_state(resolved.title, _GREY)
        self._terminal_row.set_summary(
            f"Runs the agent runner with the review prompt in {resolved.title}."
        )

    # MARK: device allocator (MCP server + skill + rule)

    def _allocator_card(self) -> QWidget:
        card, body, self._alloc_pill = settings_card(
            glyphs.G_DEVICES, "DEVICE ALLOCATOR", _CYAN
        )
        controls = QWidget()
        col = QVBoxLayout(controls)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(7)

        marks = QHBoxLayout()
        marks.setSpacing(4)
        self._alloc_marks: dict[str, Pill] = {}
        for key, label in (("mcpRegistered", "MCP"), ("skillInstalled", "skill"),
                           ("ruleInstalled", "rule"), ("claudeMdInjected", "CLAUDE.md")):
            pill = Pill("")
            self._alloc_marks[key] = pill
            marks.addWidget(pill)
        self._alloc_daemon = Pill("")
        marks.addWidget(self._alloc_daemon)
        marks.addStretch(1)
        col.addLayout(marks)

        self._alloc_drift = QLabel("")
        self._alloc_drift.setWordWrap(True)
        self._alloc_drift.setStyleSheet(
            f"color: {_ORANGE}; font-size: 9px; font-family: monospace;"
        )
        col.addWidget(self._alloc_drift)

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

        self._alloc_row = self._track(SettingRow(
            "Reserve a simulator before using it", controls, stacked=True,
            detail="Installs an MCP server, a skill and an always-on rule. Reclaims a "
                   "device when its agent dies or it sits idle for 15 minutes.",
        ))
        if deviceallocator.package_available():
            self._alloc_row.set_summary(
                "So two agents never drive the same emulator at once."
            )
        else:
            self._alloc_row.set_summary(
                f"⚠ Package not found at {deviceallocator.package_dir()}. Set "
                "DIPLOMAT_DEVICE_ALLOCATOR_DIR to point at it.", color=_ORANGE
            )
        body.addWidget(self._alloc_row)
        return card

    def _refresh_allocator_ui(self) -> None:
        s = self.store.allocator_install
        if s is None:
            self._alloc_pill.set_state("checking…", _GREY)
            for pill in self._alloc_marks.values():
                pill.set_state("")
            self._alloc_daemon.set_state("")
            self._alloc_drift.setVisible(False)
            self._alloc_uninstall.setVisible(False)
            return
        installed = bool(s.get("installed"))
        outdated = bool(s.get("outdated"))
        version = s.get("version") or "?"

        # "Installed" alone would be a true statement about a machine still running
        # the copies some earlier checkout laid down, so the stale case says so and
        # the marks name what drifted. Amber, not green: it is working, but not
        # from here.
        if outdated:
            self._alloc_pill.set_state(f"out of date · v{version}", _ORANGE)
        elif installed:
            self._alloc_pill.set_state(f"v{version}", _GREEN)
        else:
            self._alloc_pill.set_state("not installed", _GREY)

        for key, pill in self._alloc_marks.items():
            pill.set_mark({"mcpRegistered": "MCP", "skillInstalled": "skill",
                           "ruleInstalled": "rule",
                           "claudeMdInjected": "CLAUDE.md"}[key], bool(s.get(key)))
        if s.get("daemonRunning"):
            self._alloc_daemon.set_state("⚡ daemon", _GREEN)
        else:
            self._alloc_daemon.set_state("")

        drift = s.get("drift") or []
        self._alloc_drift.setVisible(bool(outdated and drift))
        if outdated and drift:
            self._alloc_drift.setText(
                "stale: " + ", ".join(str(d) for d in drift)
            )
        self._alloc_install.setText(
            "Update" if outdated else "Reinstall" if installed else "Install"
        )
        self._alloc_uninstall.setVisible(installed)

    # MARK: mesh (LAN P2P duty coordination)

    def _mesh_card(self) -> QWidget:
        card, body, self._mesh_pill = settings_card(glyphs.G_MESH, "MESH (LAN P2P)", _MINT)
        self._sw_mesh = SwitchToggle(_MINT)
        self._sw_mesh.setChecked(self.store.mesh_enabled)
        # Dead without the add-on: the mesh is SzpontNet, and there is no node for
        # this switch to start. Shown-but-disabled rather than hidden, so the
        # feature is discoverable and the pill can say what is missing.
        self._sw_mesh.setEnabled(szpont.AVAILABLE)
        self._sw_mesh.toggled.connect(self._on_mesh_toggled)
        self._mesh_row = self._track(SettingRow(
            "Coordinate duties with this LAN", self._sw_mesh,
            summary="Routes reviews, conflict fixes and audits to whichever machine "
                    "fits the policy."
                    if szpont.AVAILABLE else
                    # The ⬡ screen and its header button are not built without the
                    # add-on, so pointing at them would send the reader looking for a
                    # control that isn't there.
                    "Coordinating duties across machines is an add-on: it needs the "
                    f"SzpontNet library, which is not installed (looked for "
                    f"{szpont.package_dir()}). Everything else in Diplomat runs on "
                    "this machine alone and is unaffected.",
            detail="Runs a small peer-to-peer node that discovers the other Diplomat "
                   "machines on your LAN (UDP beacons); placement is surplus-first by "
                   "default, token- and platform-aware. Configure the whole mesh from "
                   "the ⬡ Mesh screen (the ⬡ button in the panel header). Off by "
                   "default; no node opens on the network until you enable it here."
                   if szpont.AVAILABLE else None,
        ))
        body.addWidget(self._mesh_row)
        return card

    def _on_mesh_toggled(self, on: bool) -> None:
        self.store.mesh_enabled = on
        if on:
            self.store.ensure_mesh_running_async()
        else:
            self.store.stop_mesh_async()
        self._refresh_mesh_ui()

    def _refresh_mesh_ui(self) -> None:
        # Before ``mesh_enabled``, which is also False here — the disabled switch
        # needs a reason beside it, and "off" reads as a choice the user made.
        if not szpont.AVAILABLE:
            self._mesh_pill.set_state("add-on missing", _GREY)
            return
        if not self.store.mesh_enabled:
            self._mesh_pill.set_state("off", _GREY)
            return

        from szpontnet import statefile

        state = self.store.mesh_state
        if statefile.node_running(state):
            peers = len((state or {}).get("peers", []))
            self._mesh_pill.set_state(
                f"{peers} peer" + ("" if peers == 1 else "s"), _GREEN
            )
        else:
            self._mesh_pill.set_state(
                "starting…" if state is None else "node not running", _ORANGE
            )

    # MARK: applet update

    def _update_card(self) -> QWidget:
        card, body, self._update_pill = settings_card(glyphs.G_UPDATE, "UPDATE", _BLUE)
        btn_row = QWidget()
        row = QHBoxLayout(btn_row)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self._update_btn = QPushButton("Update")
        self._update_btn.setStyleSheet("font-weight: 700;")
        self._update_btn.setEnabled(False)
        self._update_btn.clicked.connect(self.store.update_applet_async)
        row.addWidget(self._update_btn)
        recheck = QPushButton("⟲")
        recheck.setFixedWidth(34)
        recheck.setToolTip("Re-check for updates")
        recheck.clicked.connect(self.store.refresh_update_status_async)
        row.addWidget(recheck)
        row.addStretch(1)

        self._update_row = self._track(SettingRow(
            "This applet", btn_row, stacked=True,
            summary="comparing with origin…",
            detail="Pulls the latest applet from GitHub, rebuilds the diplomat-core "
                   "prompt engine, and relaunches the tray app in place.",
        ))
        body.addWidget(self._update_row)
        return card

    def _refresh_update_ui(self) -> None:
        s = self.store.update_state or {"phase": "checking"}
        phase = s.get("phase")

        if phase == "checking":
            self._update_pill.set_state("checking…", _GREY)
            self._update_row.set_summary("comparing with origin…")
            self._update_btn.setEnabled(False)
        elif phase == "updating":
            self._update_pill.set_state("updating…", _ORANGE)
            self._update_row.set_summary(s.get("step") or "")
            self._update_btn.setEnabled(False)
        elif phase == "restarting":
            self._update_pill.set_state("restarting…", _GREEN)
            self._update_row.set_summary(
                f"relaunched at {s.get('commit')} — this instance is handing over"
            )
            self._update_btn.setEnabled(False)
        elif phase == "error":
            self._update_pill.set_state("update failed", _RED)
            self._update_row.set_summary(s.get("error") or "unknown error", color=_RED)
            self._update_btn.setEnabled(True)
        elif s.get("error"):
            self._update_pill.set_state("check failed", _ORANGE)
            self._update_row.set_summary(s["error"], color=_ORANGE)
            self._update_btn.setEnabled(True)
        else:
            behind = s.get("behind") or 0
            ahead = s.get("ahead") or 0
            if behind:
                self._update_pill.set_state(f"{behind} behind", _BLUE)
            else:
                self._update_pill.set_state("up to date", _GREEN)
            detail = f"{s.get('commit')} on {s.get('branch')} · upstream {s.get('upstream')}"
            if ahead:
                # A diverged checkout still updates — via a merge, not a discard.
                detail += f" · {ahead} local ahead" + (" (will merge)" if behind else "")
            self._update_row.set_summary(detail)
            self._update_btn.setEnabled(True)
