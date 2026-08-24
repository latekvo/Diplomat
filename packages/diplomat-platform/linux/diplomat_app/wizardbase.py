"""What the four spawn wizards do, as opposed to how they look.

Review, Fix-issues, Resolve-conflicts and Full-E2E each collect different inputs,
but from the SPAWN click onwards they are one routine, which lives here once:

* a Review-PRs or Fix-issues SCOPE opens no session at all — it queues one task
  per PR / per issue for the task cap to start (:meth:`_queue_sweep`);
* otherwise, if the mesh row is live and ticked, hand the prompt to the local node
  and let it place the job — disabling SPAWN so a second click can't
  double-dispatch;
* otherwise run the job through :meth:`Store.dispatch_agent`, the same gate the
  PR auto-fix monitor rides (dedup by PR, ban check, registration) — only the
  trigger and its policies differ;
* either way, report the outcome in the status line.

A copy of the dispatch decision per wizard is the kind of duplication that goes
wrong quietly: the two branches decide whether an agent runs on this machine or
someone else's, and a guard added to one copy is a guard the others skip.
The per-wizard parts stay in the subclasses — the label a run is tracked under,
the PR it is scoped to, and whose PRs it touches.

The macOS twins are the `spawn()` methods in ReviewWizard/IssueWizard/
ConflictWizard/AuditWizard.swift, which mirror this same branch.
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QVBoxLayout, QWidget

from diplomat_runtime import review
from . import widgets
from .meshspawn import MeshSpawnRow
from .store import Store


class SpawnWizard(QWidget):
    """Base for the four wizards: owns the mesh row, the SPAWN button and the
    dispatch branch. Subclasses build their own inputs and supply the hooks below."""

    #: ``(queued, already_queued, error)`` from a sweep's fan-out worker, which
    #: assembles a prompt per item off the UI thread. Queued to the main thread,
    #: because that is the only one Qt lets touch the status line.
    _swept = Signal(int, int, str)

    #: The nouns a sweep's status line is written out of: one item it covers ("PR" /
    #: "issue"), many of them ("PRs" / "issues"), and what it queues one of per item,
    #: singular and plural ("review"/"reviews", "fix"/"fixes" — the plural is spelt
    #: out because "fix" does not take a bare -s). Set by the two wizards that sweep.
    _sweep_item = ""
    _sweep_plural = ""
    _sweep_unit = ""
    _sweep_units = ""

    def __init__(self, store: Store, *, kind: str, tint: str) -> None:
        super().__init__()
        self._swept.connect(self._sweep_queued)
        self.store = store
        # The duty id, the AgentJob kind and the activity-feed action verb are one
        # and the same string for every wizard ("review" / "issues" / "conflicts" /
        # "audit").
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

    def _sweeps(self) -> bool:
        """Whether this click queues a sweep instead of opening one session. False for
        the two wizards that have no scope axis at all; the other two override the hook
        below and answer it from their target."""
        return False

    def _spawn(self) -> None:
        from diplomat_runtime import activity, autofix

        if self._sweeps():
            self._queue_sweep()
            return
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

    def _queue_sweep(self) -> None:
        """Expand a scope into one queued task per item it covers.

        SPAWN is disabled for the round trip, as the mesh branch does with its own —
        the fan-out assembles a prompt per item and a second press meanwhile would ask
        for the sweep twice. What lands is reported by :meth:`_sweep_queued`, back on
        the GUI thread."""
        if not self.store.has_loaded:
            self.status.setText(
                f"{self._sweep_plural} haven't loaded yet — refresh, then sweep.")
            return
        self._dispatch_inflight = True
        self._restyle_spawn()
        self.status.setText(f"Queueing one {self._sweep_unit} per {self._sweep_item}…")
        self.store.request_sweep_async(
            self._config(),
            lambda queued, already, err: self._swept.emit(queued, already, err))

    def _sweep_queued(self, queued: int, already: int, err: str) -> None:
        """Report what the sweep put in the queue, and hand SPAWN back through
        ``_sync`` so it returns to whatever the CURRENT inputs warrant."""
        self._dispatch_inflight = False
        if err:
            self.status.setText(f"Couldn't queue the sweep: {err}")
        elif queued:
            waiting = f" ({already} already queued)" if already else ""
            unit = self._sweep_unit if queued == 1 else self._sweep_units
            self.status.setText(
                f"Queued {queued} {unit}{waiting} — they start as slots free.")
        elif already:
            self.status.setText(f"All {already} are queued already.")
        else:
            self.status.setText(f"No open {self._sweep_plural} in that scope.")
        self._sync()

    def _mesh_done(self, results: list, err: str) -> None:
        """A mesh dispatch settled: hand SPAWN back and report where it landed.

        The button comes back through ``_sync`` rather than a bare re-enable, so it
        returns to whatever the *current* config warrants — a wizard whose input
        went invalid while the dispatch was in flight stays disabled."""
        self._dispatch_inflight = False
        self.status.setText(MeshSpawnRow.summarize(results, err))
        self.store.refresh_activity()
        self._sync()
