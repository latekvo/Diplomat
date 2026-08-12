"""What the three spawn wizards do, as opposed to how they look.

Review, Resolve-conflicts and Full-E2E each collect different inputs, but from
the SPAWN click onwards they are one routine, which lives here once:

* if the mesh row is live and ticked, hand the prompt to the local node and let
  it place the job — disabling SPAWN so a second click can't double-dispatch;
* otherwise run the job through :meth:`Store.dispatch_agent`, the same gate the
  PR auto-fix monitor rides (dedup by PR, ban check, registration) — only the
  trigger and its policies differ;
* either way, report the outcome in the status line.

Three copies of a dispatch decision is the kind of duplication that goes wrong
quietly: the two branches decide whether an agent runs on this machine or
someone else's, and a guard added to one copy is a guard the other two skip.
The per-wizard parts stay in the subclasses — the label a run is tracked under,
the PR it is scoped to, and whose PRs it touches.

The macOS twins are the `spawn()` methods in ReviewWizard/ConflictWizard/
AuditWizard.swift, which mirror this same branch.
"""

from __future__ import annotations

from PySide6.QtWidgets import QVBoxLayout, QWidget

from . import review, widgets
from .meshspawn import MeshSpawnRow
from .store import Store


class SpawnWizard(QWidget):
    """Base for the three wizards: owns the mesh row, the SPAWN button and the
    dispatch branch. Subclasses build their own inputs and supply the four hooks
    below."""

    def __init__(self, store: Store, *, kind: str, tint: str) -> None:
        super().__init__()
        self.store = store
        # The duty id, the AgentJob kind and the activity-feed action verb are one
        # and the same string for every wizard ("review" / "conflicts" / "audit").
        self._kind = kind
        self._tint = tint
        # Set while a dispatch this wizard started has yet to answer — a mesh round
        # trip, or a sweep's fan-out. SPAWN is disabled for it, and stays disabled
        # across a _sync(), which re-derives the button from config validity alone
        # and would otherwise hand it back mid-flight: every input change calls one,
        # and so does the panel's own periodic refresh_identity().
        self._dispatch_inflight = False

    # ---- hooks the subclasses fill in ------------------------------------

    def _config(self):
        """This wizard's ``*Config`` built from its current widget state."""
        raise NotImplementedError

    def _label(self) -> str:
        """A short description of the run for the sessions list and audit feed."""
        raise NotImplementedError

    def _author_login(self) -> str | None:
        """Whose work this run touches, when known — the pipeline's ban dimension.
        ``None`` where there is no such author (my own PRs, a whole-repo audit)."""
        return None

    def _sync(self) -> None:
        """Re-derive the wizard's widget state from its config. The base calls it
        after a mesh dispatch settles; wizards with validity-dependent fields
        override it, and the default keeps SPAWN's fill in step."""
        self._restyle_spawn()

    # ---- shared construction ---------------------------------------------

    def _add_dispatch_controls(self, root: QVBoxLayout) -> None:
        """Append the mesh row, the SPAWN button and the status line — the tail
        every wizard's layout ends with."""
        self.mesh_row = MeshSpawnRow(self.store, self._kind)
        self.mesh_row.dispatched.connect(self._mesh_done)
        root.addWidget(self.mesh_row)

        self.spawn_btn = widgets.spawn_button(self._spawn)
        root.addWidget(self.spawn_btn)

        self.status = widgets.wizard_status()
        root.addWidget(self.status)

    def _restyle_spawn(self) -> None:
        """Enable + fill SPAWN according to whether the config can actually spawn —
        which a dispatch still in flight is reason enough on its own for it not to."""
        widgets.style_spawn_button(
            self.spawn_btn, self._tint,
            self._config().is_valid and not self._dispatch_inflight,
        )

    def refresh_identity(self) -> None:
        """Re-validate after the viewer login resolves — the panel calls this on
        every data refresh. ``me`` is the @handle for the "mine" target, so it can
        change what this wizard would spawn."""
        self._sync()

    # ---- the dispatch branch ---------------------------------------------

    def _spawn(self) -> None:
        from . import activity, autofix

        cfg = self._config()
        label = self._label()
        if self.mesh_row.use_mesh():
            # Mesh: the local node picks the executor (strategy, platform spread,
            # token failover) and runs it there. Nothing is spawned on this machine.
            self._dispatch_inflight = True
            self._restyle_spawn()
            self.status.setText("Dispatching over the mesh…")
            activity.log("panel", self._kind, f"{label} · via mesh")
            self.mesh_row.dispatch(cfg.build_prompt())
            return
        # Local: the SAME pipeline the auto-monitor rides — dedup, ban check,
        # registration — only the trigger (this click) and its policies differ
        # (see autofix.dispatch_decide).
        term = review.resolved(self.store.terminal)
        verdict = self.store.dispatch_agent(
            autofix.AgentJob(
                kind=self._kind,
                audit_action=self._kind,
                label=label,
                prompt=cfg.build_prompt(),
                pr_url=cfg.single_pr_url,
                pr_number=cfg.single_pr_number,
                author_login=self._author_login(),
                duty=self._kind,
            ),
            autofix.SOURCE_PANEL,
        )
        self.status.setText(widgets.dispatch_status_text(verdict, term.title))

    def _mesh_done(self, results: list, err: str) -> None:
        """A mesh dispatch settled: hand SPAWN back and report where it landed.

        The button comes back through ``_sync`` rather than a bare re-enable, so it
        returns to whatever the *current* config warrants — a wizard whose input
        went invalid while the dispatch was in flight stays disabled."""
        self._dispatch_inflight = False
        self.status.setText(MeshSpawnRow.summarize(results, err))
        self.store.refresh_activity()
        self._sync()
