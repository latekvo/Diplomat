"""Full-E2E-test wizard — one-click whole-repo swarm audit, then SPAWN.

The Linux analogue of AuditWizardView.swift. No target picker — it always tests the
entire repository. Two toggles escalate the scope: open a PR for every confirmed
finding, and also reproduce + fix the open BUG issues. Builds the prompt from the
shared core/audit.json and opens a detached terminal running ``claude`` with it.
Reuses the terminal spawner from :mod:`review`. Persistent widget (state survives
data refreshes).
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import audit, review
from .meshspawn import MeshSpawnRow
from .store import Store

_TINT = "#5856D6"  # indigo, matching the macOS Full-E2E-test card


class AuditWizardView(QWidget):
    def __init__(self, store: Store) -> None:
        super().__init__()
        self.store = store

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        title = QLabel("🐞  Full E2E test")
        title.setStyleSheet("font-weight: 700; font-size: 13px;")
        root.addWidget(title)

        blurb = QLabel(
            "Dispatches a massive swarm to end-to-end test the whole repo — every "
            "module, flow, build and test. By default it only finds and reports "
            "defects; nothing is changed."
        )
        blurb.setWordWrap(True)
        blurb.setStyleSheet("color: palette(mid); font-size: 10px;")
        root.addWidget(blurb)

        bar = QLabel(
            "✔  Every finding is hard-reproduced — 100% proof of existence, no guesses."
        )
        bar.setWordWrap(True)
        bar.setStyleSheet(
            f"color: palette(mid); font-size: 10px; padding: 7px;"
            f" background-color: rgba(88,86,214,0.10); border-radius: 7px;"
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
        self.mesh_row = MeshSpawnRow(store, "audit")
        self.mesh_row.dispatched.connect(self._mesh_done)
        root.addWidget(self.mesh_row)

        self.spawn_btn = QPushButton("▶  SPAWN AGENT")
        self.spawn_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.spawn_btn.clicked.connect(self._spawn)
        self.spawn_btn.setStyleSheet(
            f"QPushButton {{ background-color: {_TINT}; color: white; font-weight: 700;"
            f" padding: 8px; border-radius: 7px; }}"
        )
        root.addWidget(self.spawn_btn)

        self.status = QLabel("")
        self.status.setStyleSheet("color: palette(mid); font-family: monospace; font-size: 10px;")
        self.status.setWordWrap(True)
        root.addWidget(self.status)

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

    def refresh_identity(self) -> None:
        """Kept for parity with the other wizards (the audit needs no identity to
        validate, but the panel calls this on every data refresh)."""

    def _spawn(self) -> None:
        from . import activity

        cfg = self._config()
        extra = " · ".join(
            x for x in (["issues"] if cfg.fix_issues else []) + (["open PRs"] if cfg.open_prs else []) if x
        )
        if self.mesh_row.use_mesh():
            self.spawn_btn.setEnabled(False)
            self.status.setText("Dispatching over the mesh…")
            activity.log("panel", "audit",
                         f"Full E2E audit{(' · ' + extra) if extra else ''} · via mesh")
            self.mesh_row.dispatch(cfg.build_prompt())
            return
        term = review.resolved(self.store.terminal)
        try:
            review.spawn(cfg.build_prompt(), self.store.terminal)
            self.status.setText(f"Launched {term.title}")
            activity.log("panel", "audit", f"Full E2E audit{(' · ' + extra) if extra else ''}")
            self.store.refresh_activity()
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"Failed: {exc}")

    def _mesh_done(self, results: list, err: str) -> None:
        self.spawn_btn.setEnabled(True)
        self.status.setText(MeshSpawnRow.summarize(results, err))
        self.store.refresh_activity()
