"""Resolve-conflicts wizard — pick whose PRs to sweep, then SPAWN.

The Linux analogue of ConflictWizardView.swift. Collects the same choice (mine /
someone else's / one specific PR), builds the prompt from the shared
core/conflicts.json, and opens a detached terminal running ``claude`` with it.
Reuses the terminal spawner from :mod:`review`. Persistent widget (state survives
data refreshes).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import conflicts, review
from .conflicts import Target
from .store import Store

_TINT = "#32ADE6"  # cyan, matching the macOS Resolve-conflicts card


class ConflictWizardView(QWidget):
    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        title = QLabel("🔀  Resolve conflicts")
        title.setStyleSheet("font-weight: 700; font-size: 13px;")
        root.addWidget(title)

        # Target: mine / someone else's / a specific PR.
        self.target = QComboBox()
        for t in (Target.MINE, Target.SOMEONE, Target.SPECIFIC):
            self.target.addItem(t.title, t)
        self.target.currentIndexChanged.connect(self._sync)
        root.addWidget(self.target)

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

        blurb = QLabel(
            "Merges the latest main into each PR; where that conflicts, resolves it "
            "and pushes the merge. Clean merges are left untouched."
        )
        blurb.setWordWrap(True)
        blurb.setStyleSheet("color: palette(mid); font-size: 10px;")
        root.addWidget(blurb)

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

    def _config(self) -> conflicts.ConflictConfig:
        return conflicts.ConflictConfig(
            target=self.target.currentData(),
            username=self.username.text(),
            me=self.store.effective_me,
            specific_pr=self.specific_pr.text(),
        )

    def _sync(self) -> None:
        cfg = self._config()
        # Show only the field that applies to the current target.
        self.username.setVisible(cfg.target == Target.SOMEONE)
        show_pr = cfg.target == Target.SPECIFIC
        self.specific_pr.setVisible(show_pr)

        ref = cfg.pr_ref
        if show_pr and ref.repo_mismatch:
            owner, repo = cfg.target_repo
            self.pr_warning.setText(f"That PR isn't in {owner}/{repo}.")
            self.pr_warning.setVisible(True)
        else:
            self.pr_warning.setVisible(False)

        self.spawn_btn.setEnabled(cfg.is_valid)
        tint = _TINT if cfg.is_valid else "#888888"
        self.spawn_btn.setStyleSheet(
            f"QPushButton {{ background-color: {tint}; color: white; font-weight: 700;"
            f" padding: 8px; border-radius: 7px; }}"
        )

    def refresh_identity(self) -> None:
        """Re-validate after the viewer login resolves (used as @handle for 'mine')."""
        self._sync()

    def _spawn(self) -> None:
        cfg = self._config()
        term = review.resolved(self.store.terminal)
        try:
            review.spawn(cfg.build_prompt(), self.store.terminal)
            self.status.setText(f"Launched {term.title}")
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"Failed: {exc}")
