"""Full-E2E-test wizard — one-click whole-repo swarm audit, then SPAWN.

The Linux analogue of AuditWizardView.swift. No target picker — it always tests the
entire repository. Two toggles escalate the scope: open a PR for every confirmed
finding, and also reproduce + fix the open BUG issues. Builds the prompt from the
shared core/audit.json; dispatching it is
:class:`~diplomat_app.wizardbase.SpawnWizard`'s job. Persistent widget (state
survives data refreshes).
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QLabel,
    QVBoxLayout,
)

from . import audit, glyphs, widgets
from .store import Store
from .wizardbase import SpawnWizard

_TINT = "#5856D6"  # indigo, matching the macOS Full-E2E-test card


class AuditWizardView(SpawnWizard):
    def __init__(self, store: Store) -> None:
        super().__init__(store, kind="audit", tint=_TINT)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        root.addWidget(widgets.wizard_title(glyphs.G_AUDIT, "Full E2E test"))

        root.addWidget(widgets.wizard_blurb(
            "Dispatches a massive swarm to end-to-end test the whole repo — every "
            "module, flow, build and test. By default it only finds and reports "
            "defects; nothing is changed."
        ))

        bar = QLabel(
            "✔  Every finding is hard-reproduced — 100% proof of existence, no guesses."
        )
        bar.setWordWrap(True)
        bar.setStyleSheet(
            "color: palette(mid); font-size: 10px; padding: 7px;"
            " background-color: rgba(88,86,214,0.10); border-radius: 7px;"
        )
        root.addWidget(bar)

        # Both toggles let the swarm change code / GitHub state, well beyond the
        # default find-only run, so each is highlighted.
        self.open_prs = QCheckBox("Open PRs for every finding")
        self.open_prs.setToolTip(
            "Deliver each confirmed finding / fix as its own focused PR. "
            "Off: read-only audit that only reports findings."
        )
        self._style_toggle(self.open_prs)
        root.addWidget(self.open_prs)

        self.fix_issues = QCheckBox("Also fix open bug issues")
        self.fix_issues.setToolTip(
            "Reproduce + fix the repo's open BUG issues too. "
            "Feature requests are always skipped."
        )
        self._style_toggle(self.fix_issues)
        root.addWidget(self.fix_issues)

        # Mesh routing — the audit's spread means one Linux AND one macOS node
        # each run the bundle E2E (visible only while the mesh is enabled + running).
        self._add_dispatch_controls(root)
        self._restyle_spawn()

        root.addStretch(1)

    @staticmethod
    def _style_toggle(box: QCheckBox) -> None:
        box.setStyleSheet(
            "QCheckBox { font-weight: 700; font-size: 11px; padding: 7px;"
            " background-color: rgba(255,149,0,0.14); border: 1px solid rgba(255,149,0,0.5);"
            " border-radius: 7px; }"
        )

    def _config(self) -> audit.AuditConfig:
        return audit.AuditConfig(
            fix_issues=self.fix_issues.isChecked(),
            open_prs=self.open_prs.isChecked(),
        )

    def _label(self) -> str:
        """Names the escalations that are on, so the sessions list says what this
        run is allowed to change."""
        cfg = self._config()
        extra = " · ".join(
            (["issues"] if cfg.fix_issues else []) + (["open PRs"] if cfg.open_prs else [])
        )
        return f"Full E2E audit{(' · ' + extra) if extra else ''}"
