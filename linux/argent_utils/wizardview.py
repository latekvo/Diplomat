"""Review-PRs wizard — target, scope, depth, action toggles, then SPAWN.

The Linux analogue of ReviewWizardView.swift. Collects the same choices, builds
the prompt from the shared core/review.json, and opens a detached terminal
running ``claude`` with it. Persistent widget (state survives data refreshes).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
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

from . import review
from .store import Store

_TINT = "#FF2D78"  # pink, matching the macOS Review card


class WizardView(QWidget):
    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store
        self._depths = review.depths()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        title = QLabel("✅  Review PRs")
        title.setStyleSheet("font-weight: 700; font-size: 13px;")
        root.addWidget(title)

        # Target
        self.target = QComboBox()
        self._refresh_target_labels()
        self.target.currentIndexChanged.connect(self._sync)
        root.addWidget(self.target)

        self.username = QLineEdit()
        self.username.setPlaceholderText("github username")
        self.username.textChanged.connect(self._sync)
        root.addWidget(self.username)

        # Scope
        self.drafts = QCheckBox("Review draft PRs")
        self.drafts.setChecked(True)
        self.ready = QCheckBox("Review ready-for-review PRs")
        self.ready.setChecked(True)
        self.drafts.toggled.connect(self._sync)
        self.ready.toggled.connect(self._sync)
        root.addWidget(self.drafts)
        root.addWidget(self.ready)

        self.specific_pr = QLineEdit()
        self.specific_pr.setPlaceholderText("PR # to review (when neither box above)")
        self.specific_pr.textChanged.connect(self._sync)
        root.addWidget(self.specific_pr)

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

    def _refresh_target_labels(self) -> None:
        me = self.store.effective_me
        mine = f"My PRs (@{me})" if me else "My PRs"
        prev = self.target.currentIndex() if self.target.count() else 0
        self.target.blockSignals(True)
        self.target.clear()
        self.target.addItem(mine, True)
        self.target.addItem("Someone else's", False)
        self.target.setCurrentIndex(prev)
        self.target.blockSignals(False)

    def _config(self) -> review.ReviewConfig:
        return review.ReviewConfig(
            depth=review.depth_ids()[self.slider.value()],
            target_is_mine=bool(self.target.currentData()),
            username=self.username.text(),
            me=self.store.effective_me,
            mark_ready=self.mark_ready.isChecked(),
            leave_reviews=self.leave_reviews.isChecked(),
            reply_to_reviews=self.reply.isChecked(),
            include_drafts=self.drafts.isChecked(),
            include_ready=self.ready.isChecked(),
            specific_pr=self.specific_pr.text(),
        )

    def _sync(self) -> None:
        cfg = self._config()
        depth = review.depth_by_id(cfg.depth)
        self.depth_title.setText(depth["title"])
        self.depth_blurb.setText(depth["blurb"])

        self.username.setVisible(not cfg.target_is_mine)
        self.specific_pr.setEnabled(cfg.is_single_pr)

        self.mark_ready.setEnabled(cfg.can_mark_ready)
        self.leave_reviews.setEnabled(cfg.can_leave_reviews)
        self.reply.setEnabled(cfg.can_reply_to_reviews)

        self.spawn_btn.setEnabled(cfg.is_valid)
        tint = _TINT if cfg.is_valid else "#888888"
        self.spawn_btn.setStyleSheet(
            f"QPushButton {{ background-color: {tint}; color: white; font-weight: 700;"
            f" padding: 8px; border-radius: 7px; }}"
        )

    def refresh_identity(self) -> None:
        """Re-label the target picker after the viewer login resolves."""
        self._refresh_target_labels()
        self._sync()

    def _spawn(self) -> None:
        cfg = self._config()
        term = review.resolved(self.store.terminal)
        try:
            review.spawn(cfg.build_prompt(), self.store.terminal)
            self.status.setText(f"Launched {term.title}")
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"Failed: {exc}")
