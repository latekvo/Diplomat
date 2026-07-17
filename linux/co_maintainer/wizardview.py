"""Review-PRs wizard — target, scope, depth, action toggles, then SPAWN.

The Linux analogue of ReviewWizardView.swift. Collects the same choices, builds
the prompt from the shared core/review.json, and opens a detached terminal
running ``claude`` with it. Persistent widget (state survives data refreshes).
"""

from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from . import glyphs, review
from .meshspawn import MeshSpawnRow
from .prtarget import PRTarget
from .review import SpecificAuthor
from .store import Store

_TINT = "#FF2D78"  # pink, matching the macOS Review card


class WizardView(QWidget):
    # Emitted (queued to the main thread) when a background author poll resolves.
    # Carries (pending_pr_text, author_login_or_empty) so the slot can ignore a
    # result superseded by newer keystrokes - mirrors the macOS `pending` guard.
    _author_resolved = Signal(str, str)

    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self._depths = review.depths()

        # Specific-PR author disposition (mine / theirs / unknown) + loading flag,
        # resolved off the UI thread - mirrors ReviewWizardView's @State in Swift.
        self._specific_author = SpecificAuthor.UNKNOWN
        self._author_loading = False
        # The PR text the in-flight poll was launched for (debounce/supersede guard).
        self._author_pending: str | None = None
        self._author_resolved.connect(self._on_author_resolved)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        title = QLabel(f"{glyphs.G_REVIEW}  Review PRs")
        title.setStyleSheet("font-weight: 700; font-size: 13px;")
        root.addWidget(title)

        # Target: mine / someone else's / a specific PR (matches the Merge wizard).
        self.target = QComboBox()
        for t in (PRTarget.MINE, PRTarget.SOMEONE, PRTarget.SPECIFIC):
            self.target.addItem(t.title, t)
        self.target.currentIndexChanged.connect(self._sync)
        root.addWidget(self.target)

        self.mine_caption = QLabel("")
        self.mine_caption.setStyleSheet("color: palette(mid); font-size: 10px;")
        root.addWidget(self.mine_caption)

        # The username field (someone else's) and the single-PR field share this
        # slot; only the one matching the current target shows (see _sync).
        self.username = QLineEdit()
        self.username.setPlaceholderText("github username")
        self.username.textChanged.connect(self._sync)
        root.addWidget(self.username)

        self.specific_pr = QLineEdit()
        self.specific_pr.setPlaceholderText("PR # or URL")
        self.specific_pr.textChanged.connect(self._sync)
        root.addWidget(self.specific_pr)

        self.pr_warning = QLabel("")
        self.pr_warning.setWordWrap(True)
        self.pr_warning.setStyleSheet("color: #e0563f; font-size: 10px;")
        root.addWidget(self.pr_warning)

        # A one-line note under the single-PR field: whose PR it is once polled, so
        # the user knows why some toggles disappeared (mirrors macOS `authorHint`).
        self.author_hint = QLabel("")
        self.author_hint.setWordWrap(True)
        self.author_hint.setStyleSheet("font-size: 10px;")
        root.addWidget(self.author_hint)

        # Scope
        self.drafts = QCheckBox("Review draft PRs")
        self.drafts.setChecked(True)
        self.ready = QCheckBox("Review ready-for-review PRs")
        self.ready.setChecked(True)
        self.drafts.toggled.connect(self._sync)
        self.ready.toggled.connect(self._sync)
        root.addWidget(self.drafts)
        root.addWidget(self.ready)

        # Depth
        depth_header = QHBoxLayout()
        dl = QLabel("Review depth")
        dl.setStyleSheet("color: palette(mid); font-weight: 700; font-size: 10px;")
        self.depth_title = QLabel()
        self.depth_title.setStyleSheet("font-weight: 700; font-size: 10px;")
        self.depth_title.setAlignment(Qt.AlignmentFlag.AlignRight)
        depth_header.addWidget(dl)
        depth_header.addWidget(self.depth_title, 1)
        root.addLayout(depth_header)

        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setMinimum(0)
        self.slider.setMaximum(len(self._depths) - 1)
        self.slider.setSingleStep(1)
        self.slider.setPageStep(1)
        self.slider.setValue(self._default_depth_index())
        self.slider.valueChanged.connect(self._sync)
        root.addWidget(self.slider)

        self.depth_blurb = QLabel()
        self.depth_blurb.setWordWrap(True)
        self.depth_blurb.setStyleSheet("color: palette(mid); font-size: 10px;")
        root.addWidget(self.depth_blurb)

        # Action toggles
        self.mark_ready = QCheckBox("Mark clean PRs ready for review")
        self.mark_ready.setChecked(True)
        self.leave_reviews = QCheckBox("Leave reviews (CLAUDE.md format)")
        self.leave_reviews.setChecked(True)
        self.reply = QCheckBox("Reply to others' review threads")
        self.reply.setChecked(True)
        for cb in (self.mark_ready, self.leave_reviews, self.reply):
            root.addWidget(cb)

        # The "final pass" escalation — off by default, visually highlighted (amber)
        # so it reads as the special "go all the way" option.
        self.final_pass = QCheckBox(f"{glyphs.G_FINAL}  Final E2E pass + verdict")
        self.final_pass.setChecked(False)
        self.final_pass.setStyleSheet(
            "QCheckBox { font-weight: 600; padding: 6px; border: 1px solid #d8a200;"
            " border-radius: 7px; background: rgba(255, 214, 0, 0.16); }"
        )
        self.final_pass.setToolTip(
            "One last full-E2E pass with big swarms: approve clean PRs, "
            "request changes on real blockers."
        )
        root.addWidget(self.final_pass)

        # Mesh routing (visible only while the LAN mesh is enabled + running).
        self.mesh_row = MeshSpawnRow(store, "review")
        self.mesh_row.dispatched.connect(self._mesh_done)
        root.addWidget(self.mesh_row)

        # Spawn
        self.spawn_btn = QPushButton("▶  SPAWN AGENT")
        self.spawn_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.spawn_btn.clicked.connect(self._spawn)
        root.addWidget(self.spawn_btn)

        self.status = QLabel("")
        self.status.setStyleSheet("color: palette(mid); font-family: monospace; font-size: 10px;")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

        root.addStretch(1)
        self._sync()

    # MARK: config from widgets

    def _default_depth_index(self) -> int:
        ids = review.depth_ids()
        try:
            return ids.index(review.default_depth_id())
        except ValueError:
            return 0

    def _config(self) -> review.ReviewConfig:
        return review.ReviewConfig(
            depth=review.depth_ids()[self.slider.value()],
            target=self.target.currentData(),
            username=self.username.text(),
            me=self.store.effective_me,
            mark_ready=self.mark_ready.isChecked(),
            leave_reviews=self.leave_reviews.isChecked(),
            reply_to_reviews=self.reply.isChecked(),
            include_drafts=self.drafts.isChecked(),
            include_ready=self.ready.isChecked(),
            specific_pr=self.specific_pr.text(),
            final_pass=self.final_pass.isChecked(),
            specific_author=self._specific_author,
        )

    def _sync(self) -> None:
        # Kick off / reset the specific-PR author poll BEFORE reading the config, so
        # a target/PR change that flips us back to UNKNOWN is reflected this pass.
        self._refresh_author()

        cfg = self._config()
        depth = review.depth_by_id(cfg.depth)
        self.depth_title.setText(depth["title"])
        self.depth_blurb.setText(depth["blurb"])

        # The context field follows the target; only one shows. Specific PR also
        # hides the draft/ready scope, which only applies to a whose-PRs sweep.
        is_mine = cfg.target == PRTarget.MINE
        is_specific = cfg.target == PRTarget.SPECIFIC
        self.username.setVisible(cfg.target == PRTarget.SOMEONE)
        self.specific_pr.setVisible(is_specific)
        self.drafts.setVisible(not is_specific)
        self.ready.setVisible(not is_specific)

        me = self.store.effective_me
        self.mine_caption.setText(f"PRs authored by @{me}" if (is_mine and me) else "")
        self.mine_caption.setVisible(bool(is_mine and me))

        ref = cfg.pr_ref
        if is_specific and ref.repo_mismatch:
            owner, repo = cfg.target_repo
            self.pr_warning.setText(f"That PR isn't in {owner}/{repo}.")
            self.pr_warning.setVisible(True)
        else:
            self.pr_warning.setVisible(False)

        self._update_author_hint(is_specific)

        # Which action toggles apply follows the disposition (mine / theirs / unknown).
        # macOS wraps each toggle in `if config.canX`, hiding it entirely - mirror that
        # with setVisible so a specific PR resolving to mine/theirs hides the toggles
        # that don't apply, exactly like the macOS wizard.
        self.mark_ready.setVisible(cfg.can_mark_ready)
        self.leave_reviews.setVisible(cfg.can_leave_reviews)
        self.reply.setVisible(cfg.can_reply_to_reviews)
        self.final_pass.setVisible(cfg.can_final_pass)

        self.spawn_btn.setEnabled(cfg.is_valid)
        tint = _TINT if cfg.is_valid else "#888888"
        self.spawn_btn.setStyleSheet(
            f"QPushButton {{ background-color: {tint}; color: white; font-weight: 700;"
            f" padding: 8px; border-radius: 7px; }}"
        )

    def _update_author_hint(self, is_specific: bool) -> None:
        """The whose-PR-is-it note under the single-PR field. Only shown for a
        specific PR; mirrors macOS `authorHint` wording + colours."""
        if not is_specific:
            self.author_hint.setVisible(False)
            return
        if self._author_loading:
            icon, text, color = "⧗", "Checking who authored this PR...", "palette(mid)"
        elif self._specific_author == SpecificAuthor.MINE:
            icon, text, color = "●", "Your PR - fix-on-branch review.", "#2e9e4f"
        elif self._specific_author == SpecificAuthor.THEIRS:
            icon, text, color = "◑", "Someone else's PR - review only, hands off.", "#e08a2f"
        else:
            icon, text, color = "?", "Enter a PR to detect whether it's yours.", "palette(mid)"
        self.author_hint.setText(f"{icon}  {text}")
        self.author_hint.setStyleSheet(f"font-size: 10px; color: {color};")
        self.author_hint.setVisible(True)

    def _refresh_author(self) -> None:
        """Poll the specific PR's author (debounced, off the UI thread) so the wizard
        can hide the toggles that don't apply and pick the right mine/theirs prompt -
        no author-guessing left to the spawned agent. Mirrors ReviewWizardView.refreshAuthor.
        """
        cfg = self._config()
        if cfg.target != PRTarget.SPECIFIC:
            # Not a specific PR: reset to a clean unknown state, cancel any in-flight poll.
            self._author_pending = None
            self._author_loading = False
            if self._specific_author != SpecificAuthor.UNKNOWN:
                self._specific_author = SpecificAuthor.UNKNOWN
            return

        pending = self.specific_pr.text()
        if pending == self._author_pending:
            return  # already resolving / resolved this exact input

        ref = cfg.pr_ref
        if not ref.is_valid or ref.number is None:
            # No usable PR ref yet: unknown, nothing to poll.
            self._author_pending = None
            self._author_loading = False
            self._specific_author = SpecificAuthor.UNKNOWN
            return

        owner, repo = cfg.target_repo
        number = ref.number
        self._author_pending = pending
        self._author_loading = True
        self._specific_author = SpecificAuthor.UNKNOWN  # offer all toggles while resolving

        def work() -> None:
            # Debounce keystrokes: pause, then bail if newer input already superseded
            # this poll (mirrors the 400ms Task.sleep in macOS refreshAuthor). The
            # _author_pending read is a best-effort guard - the main-thread slot
            # re-checks it authoritatively before applying the result.
            import time

            time.sleep(0.4)
            if self._author_pending != pending:
                return
            login = review.fetch_specific_author(owner, repo, number)
            # Emit back to the main thread; the slot re-checks the guard + computes
            # mine-vs-theirs against the (possibly changed) viewer login there.
            self._author_resolved.emit(pending, login or "")

        threading.Thread(target=work, daemon=True).start()

    def _on_author_resolved(self, pending: str, login: str) -> None:
        """Main-thread slot: fold a resolved author login into the disposition,
        unless newer input superseded it. Mirrors the tail of macOS refreshAuthor."""
        # Superseded by newer input, or the user left the specific-PR target.
        if pending != self._author_pending:
            return
        if self.target.currentData() != PRTarget.SPECIFIC:
            return
        self._author_loading = False
        me = self.store.effective_me
        if login and me:
            self._specific_author = (
                SpecificAuthor.MINE if login.lower() == me.lower() else SpecificAuthor.THEIRS
            )
        else:
            self._specific_author = SpecificAuthor.UNKNOWN
        self._sync()

    def refresh_identity(self) -> None:
        """Refresh the @handle caption after the viewer login resolves. The viewer
        login also decides mine-vs-theirs for a specific PR, so re-poll it."""
        # A newly resolved viewer login can change the disposition of the current PR;
        # drop the pending guard so _sync re-polls instead of short-circuiting.
        self._author_pending = None
        self._sync()

    def _spawn(self) -> None:
        from . import activity

        cfg = self._config()
        scope = cfg.specific_pr.strip() or "PRs"
        if self.mesh_row.use_mesh():
            self.spawn_btn.setEnabled(False)
            self.status.setText("Dispatching over the mesh…")
            activity.log("panel", "review", f"Review · {scope} · {cfg.depth} · via mesh")
            self.mesh_row.dispatch(cfg.build_prompt())
            return
        term = review.resolved(self.store.terminal)
        try:
            review.spawn(cfg.build_prompt(), self.store.terminal)
            self.status.setText(f"Launched {term.title}")
            activity.log("panel", "review", f"Review · {scope} · {cfg.depth}")
            self.store.refresh_activity()
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"Failed: {exc}")

    def _mesh_done(self, results: list, err: str) -> None:
        self.spawn_btn.setEnabled(True)
        self.status.setText(MeshSpawnRow.summarize(results, err))
        self.store.refresh_activity()
        self._sync()  # spawn_btn styling tracks validity
