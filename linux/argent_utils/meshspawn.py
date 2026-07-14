"""The wizards' "run on mesh" row — one shared widget for all three SPAWNs.

When the mesh is enabled and a local node is running, each wizard grows a
checked-by-default toggle that routes SPAWN AGENT through the mesh's duty
placement (weakest-first / platform spread / token failover) instead of
opening a local terminal. The row also previews where the job would land
right now, straight from the topology snapshot's assignments — so what the
button says is what dispatch will do.

Dispatch runs on a worker thread (Store.mesh_dispatch); the result comes back
through a queued Qt signal, so callers just connect ``dispatched`` and update
their status label on the UI thread.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QLabel, QVBoxLayout, QWidget

from . import core
from .store import Store


def _duty_meta(duty_id: str) -> dict:
    return next((d for d in core.mesh()["duties"] if d["id"] == duty_id), {})


class MeshSpawnRow(QWidget):
    """`🕸 Run on mesh` toggle + a live "→ where it would land" caption."""

    # (results, error) marshalled back from the dispatch worker thread. Qt
    # queues cross-thread signal emissions, so slots run on the UI thread.
    dispatched = Signal(list, str)

    def __init__(self, store: Store, duty_id: str) -> None:
        super().__init__()
        self.store = store
        self.duty_id = duty_id

        col = QVBoxLayout(self)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)

        self.toggle = QCheckBox("🕸  Run on mesh")
        self.toggle.setChecked(True)
        self.toggle.setToolTip(
            "Route this spawn to the machine(s) the mesh strategy picks "
            "(uncheck to open a local terminal instead)."
        )
        self.toggle.toggled.connect(lambda _: self._sync())
        col.addWidget(self.toggle)

        self.where = QLabel("")
        self.where.setWordWrap(True)
        self.where.setStyleSheet("color: #30B0C7; font-size: 9px; padding-left: 22px;")
        col.addWidget(self.where)

        store.mesh_changed.connect(self._sync)
        self._sync()

    # MARK: - state

    @property
    def _mesh_live(self) -> bool:
        from .mesh import statefile

        return self.store.mesh_enabled and statefile.node_running(self.store.mesh_state)

    def use_mesh(self) -> bool:
        """True when this SPAWN should go through the mesh."""
        return self._mesh_live and self.toggle.isChecked()

    def _sync(self) -> None:
        live = self._mesh_live
        self.setVisible(live)
        if not live:
            return
        self.where.setVisible(self.toggle.isChecked())
        self.where.setText(f"→ {self._destination_preview()}")

    def _destination_preview(self) -> str:
        """Where the duty's slots land right now, from the live snapshot."""
        state = self.store.mesh_state or {}
        a = (state.get("assignments") or {}).get(self.duty_id) or {}
        me = (state.get("self") or {}).get("id")
        names = {p.get("id"): p.get("name") for p in state.get("peers", [])}
        names[me] = f"{(state.get('self') or {}).get('name', 'this machine')} (here)"
        parts = [names.get(nid, nid[:8]) for nid in a.get("assigned", [])]
        for miss in a.get("shortfall", []):
            parts.append(f"⚠ missing {miss.get('missing')}×{miss.get('platform')}")
        return " + ".join(parts) if parts else "∅ no eligible node"

    # MARK: - dispatch

    def dispatch(self, prompt: str) -> None:
        """Fire the mesh dispatch; listen on ``dispatched`` for the outcome."""
        self.store.mesh_dispatch(
            self.duty_id, prompt,
            lambda results, err: self.dispatched.emit(results or [], err or ""),
        )

    @staticmethod
    def summarize(results: list, err: str) -> str:
        """One status-label line for a dispatch outcome."""
        if err:
            return f"Mesh dispatch failed: {err}"
        if not results:
            return "Mesh dispatch failed: no result"
        parts = []
        for r in results:
            mark = "✓" if r.get("status") == "spawned" else "✗"
            where = r.get("nodeName") or "∅"
            reason = f" ({r['reason']})" if r.get("reason") else ""
            parts.append(f"{mark} {where}{reason}")
        return "Mesh: " + " · ".join(parts)
