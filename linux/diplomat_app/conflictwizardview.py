"""Resolve-conflicts wizard — pick whose PRs to sweep, then SPAWN.

The Linux analogue of ConflictWizardView.swift. Collects the same choice (mine /
someone else's / one specific PR) and builds the prompt from the shared
core/conflicts.json. Dispatching it — over the mesh, or locally through the same
gate the auto-fix monitor rides — is :class:`~diplomat_app.wizardbase.SpawnWizard`'s
job. Persistent widget (state survives data refreshes).
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QLineEdit,
    QVBoxLayout,
)

from . import conflicts, glyphs, widgets
from .conflicts import Target
from .store import Store
from .wizardbase import SpawnWizard

_TINT = "#32ADE6"  # cyan, matching the macOS Resolve-conflicts card


class ConflictWizardView(SpawnWizard):
    def __init__(self, store: Store) -> None:
        super().__init__(store, kind="conflicts", tint=_TINT)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        root.addWidget(widgets.wizard_title(glyphs.G_CONFLICT, "Resolve conflicts"))

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

        self.pr_warning = widgets.wizard_warning()
        root.addWidget(self.pr_warning)

        root.addWidget(widgets.wizard_blurb(
            "Merges the latest main into each PR; where that conflicts, resolves it "
            "and pushes the merge. Clean merges are left untouched."
        ))

        # Mesh routing (visible only while the LAN mesh is enabled + running).
        self._add_dispatch_controls(root)

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

        self._restyle_spawn()

    def _label(self) -> str:
        scope = self._config().specific_pr.strip() or "main"
        return f"Resolve conflicts · {scope}"
