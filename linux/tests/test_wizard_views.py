"""Behavioural tests for the three Qt spawn wizards.

The three wizards share their chrome and their mesh-or-local dispatch branch, so
what is worth pinning is what each one actually *does* with the inputs it collects
- that is where they genuinely differ, and it is what the sharing underneath must
not flatten:

* which contextual fields show for which target, and the repo-mismatch warning
  (asserted via ``isHidden()``: ``isVisible()`` is false for every child while the
  wizard itself was never ``show()``n, so it cannot tell the two states apart);
* whether SPAWN is enabled, and that its fill tracks validity;
* the local dispatch path — that the click builds the right
  :class:`autofix.AgentJob` and hands it to the shared gate, not a bare spawn;
* the mesh dispatch path — that it routes through the row instead, disables the
  button, and logs the dispatch;
* the status line each outcome produces.

Every test drives the real widget under the offscreen Qt platform; the store's
``dispatch_agent`` and the mesh row are stubbed, so nothing launches an agent.
"""

from __future__ import annotations

import pytest

from diplomat_app import audit, autofix, conflicts, review
from diplomat_app.auditwizardview import AuditWizardView
from diplomat_app.conflictwizardview import ConflictWizardView
from diplomat_app.prtarget import PRTarget
from diplomat_app.review import SpecificAuthor
from diplomat_app.store import Store
from diplomat_app.wizardview import WizardView

pytest.importorskip("PySide6")


@pytest.fixture
def app():
    """One process-wide QApplication, as the panel has in the live applet."""
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def store(app):
    s = Store()
    s.me = "latekvo"
    s.has_loaded = True
    return s


@pytest.fixture
def dispatched(monkeypatch, store):
    """Capture jobs instead of dispatching them; the wizard sees "spawned"."""
    jobs: list[tuple] = []

    def fake_dispatch(job, source, attempt=1):
        jobs.append((job, source))
        return "spawned"

    monkeypatch.setattr(store, "dispatch_agent", fake_dispatch)
    return jobs


@pytest.fixture
def local_only(monkeypatch):
    """Force the local path: pretend the mesh row is never live."""
    from diplomat_app.meshspawn import MeshSpawnRow

    monkeypatch.setattr(MeshSpawnRow, "use_mesh", lambda self: False)


@pytest.fixture
def mesh_live(monkeypatch):
    """Force the mesh path and capture what would be dispatched over it."""
    from diplomat_app.meshspawn import MeshSpawnRow

    sent: list[str] = []
    monkeypatch.setattr(MeshSpawnRow, "use_mesh", lambda self: True)
    monkeypatch.setattr(MeshSpawnRow, "dispatch", lambda self, prompt: sent.append(prompt))
    return sent


# ---- Review wizard: contextual fields follow the target -------------------


def test_review_shows_only_the_field_its_target_needs(store):
    w = WizardView(store)

    w.target.setCurrentIndex(w.target.findData(PRTarget.MINE))
    assert w.username.isHidden() and w.specific_pr.isHidden()

    w.target.setCurrentIndex(w.target.findData(PRTarget.SOMEONE))
    assert not w.username.isHidden() and w.specific_pr.isHidden()

    w.target.setCurrentIndex(w.target.findData(PRTarget.SPECIFIC))
    assert not w.specific_pr.isHidden() and w.username.isHidden()


def test_review_hides_draft_ready_scope_for_a_single_pr(store):
    """Draft/ready is a whose-PRs sweep axis; it means nothing for one PR."""
    w = WizardView(store)
    assert not w.drafts.isHidden() and not w.ready.isHidden()

    w.target.setCurrentIndex(w.target.findData(PRTarget.SPECIFIC))
    assert w.drafts.isHidden() and w.ready.isHidden()


def test_review_warns_when_a_pasted_pr_is_from_another_repo(store):
    w = WizardView(store)
    w.target.setCurrentIndex(w.target.findData(PRTarget.SPECIFIC))
    w.specific_pr.setText("https://github.com/some-org/other-repo/pull/42")

    assert not w.pr_warning.isHidden()
    owner, repo = w._config().target_repo
    assert f"{owner}/{repo}" in w.pr_warning.text()


def test_review_spawn_is_disabled_until_the_target_is_usable(store):
    w = WizardView(store)
    w.target.setCurrentIndex(w.target.findData(PRTarget.SOMEONE))
    w.username.setText("")
    assert not w.spawn_btn.isEnabled()

    w.username.setText("octocat")
    assert w.spawn_btn.isEnabled()


def test_review_spawn_fill_tracks_validity(store):
    """The button greys out when it can't fire, rather than looking armed."""
    w = WizardView(store)
    w.target.setCurrentIndex(w.target.findData(PRTarget.SOMEONE))
    w.username.setText("")
    assert "#888888" in w.spawn_btn.styleSheet()

    w.username.setText("octocat")
    assert "#888888" not in w.spawn_btn.styleSheet()


def test_review_hides_toggles_that_do_not_apply_to_someone_elses_pr(store):
    """A specific PR resolved to someone else's: we review, we don't push, so
    mark-ready drops away."""
    w = WizardView(store)
    w.target.setCurrentIndex(w.target.findData(PRTarget.SPECIFIC))
    w.specific_pr.setText("455")
    w._specific_author = SpecificAuthor.THEIRS
    w._sync()

    assert w.mark_ready.isHidden()
    assert not w.leave_reviews.isHidden()


# ---- Review wizard: dispatch ----------------------------------------------


def test_review_local_spawn_goes_through_the_shared_gate(store, dispatched, local_only):
    w = WizardView(store)
    w.target.setCurrentIndex(w.target.findData(PRTarget.SPECIFIC))
    w.specific_pr.setText("455")
    w._spawn()

    assert len(dispatched) == 1
    job, source = dispatched[0]
    assert isinstance(job, autofix.AgentJob)
    assert (job.kind, job.duty, job.audit_action) == ("review", "review", "review")
    assert job.pr_number == 455
    assert job.pr_url.endswith("/pull/455")
    assert source == autofix.SOURCE_PANEL
    assert job.prompt


def test_review_sweep_carries_no_pr_dedup_key(store, dispatched, local_only):
    """A whose-PRs sweep isn't scoped to one PR, so it must not claim one."""
    w = WizardView(store)
    w.target.setCurrentIndex(w.target.findData(PRTarget.MINE))
    w._spawn()

    job, _ = dispatched[0]
    assert job.pr_number is None
    assert job.pr_url is None


def test_review_sweep_ignores_a_leftover_pr_number(store, dispatched, local_only):
    """The single-PR field keeps its text when the target moves off it. A sweep
    that still claimed that PR would take its dedup key, and the pipeline would
    refuse the next agent for a PR this run was never scoped to."""
    w = WizardView(store)
    w.target.setCurrentIndex(w.target.findData(PRTarget.SPECIFIC))
    w.specific_pr.setText("455")
    w.target.setCurrentIndex(w.target.findData(PRTarget.SOMEONE))
    w.username.setText("octocat")
    w._spawn()

    job, _ = dispatched[0]
    assert w.specific_pr.text() == "455"  # the field really did keep its value
    assert job.pr_number is None
    assert job.pr_url is None


def test_review_passes_the_reviewed_author_for_the_ban_check(store, dispatched, local_only):
    w = WizardView(store)
    w.target.setCurrentIndex(w.target.findData(PRTarget.SOMEONE))
    w.username.setText("octocat")
    w._spawn()

    job, _ = dispatched[0]
    assert job.author_login == "octocat"


def test_review_claims_no_author_for_my_own_prs(store, dispatched, local_only):
    """My own PRs have no ban dimension — passing a login here would check the
    ban list against myself."""
    w = WizardView(store)
    w.target.setCurrentIndex(w.target.findData(PRTarget.MINE))
    w._spawn()

    job, _ = dispatched[0]
    assert job.author_login is None


def test_review_mesh_spawn_routes_over_the_mesh_and_disables_the_button(
    store, dispatched, mesh_live
):
    w = WizardView(store)
    w._spawn()

    assert len(mesh_live) == 1  # the prompt went to the mesh row
    assert dispatched == []  # ...and NOT to the local pipeline
    assert not w.spawn_btn.isEnabled()
    assert "mesh" in w.status.text().lower()


# ---- Resolve-conflicts wizard ---------------------------------------------


def test_conflicts_shows_only_the_field_its_target_needs(store):
    w = ConflictWizardView(store)

    w.target.setCurrentIndex(w.target.findData(PRTarget.SOMEONE))
    assert not w.username.isHidden() and w.specific_pr.isHidden()

    w.target.setCurrentIndex(w.target.findData(PRTarget.SPECIFIC))
    assert not w.specific_pr.isHidden() and w.username.isHidden()


def test_conflicts_warns_on_a_foreign_repo_url(store):
    w = ConflictWizardView(store)
    w.target.setCurrentIndex(w.target.findData(PRTarget.SPECIFIC))
    w.specific_pr.setText("https://github.com/some-org/other-repo/pull/42")
    assert not w.pr_warning.isHidden()


def test_conflicts_local_spawn_dispatches_a_conflicts_job(store, dispatched, local_only):
    w = ConflictWizardView(store)
    w.target.setCurrentIndex(w.target.findData(PRTarget.SPECIFIC))
    w.specific_pr.setText("455")
    w._spawn()

    job, source = dispatched[0]
    assert (job.kind, job.duty, job.audit_action) == ("conflicts", "conflicts", "conflicts")
    assert job.pr_number == 455
    assert source == autofix.SOURCE_PANEL


def test_conflicts_config_mirrors_the_widgets(store):
    w = ConflictWizardView(store)
    w.target.setCurrentIndex(w.target.findData(PRTarget.SOMEONE))
    w.username.setText("octocat")
    cfg = w._config()

    assert isinstance(cfg, conflicts.ConflictConfig)
    assert cfg.target == PRTarget.SOMEONE
    assert cfg.username == "octocat"
    assert cfg.me == "latekvo"


# ---- Full-E2E wizard ------------------------------------------------------


def test_audit_is_always_spawnable(store):
    """No target to fill in — the whole repo is the target."""
    w = AuditWizardView(store)
    assert w.spawn_btn.isEnabled()
    assert w._config().is_valid


def test_audit_config_mirrors_its_two_toggles(store):
    w = AuditWizardView(store)
    w.open_prs.setChecked(True)
    w.fix_issues.setChecked(False)
    cfg = w._config()

    assert isinstance(cfg, audit.AuditConfig)
    assert cfg.open_prs is True
    assert cfg.fix_issues is False


def test_audit_local_spawn_dispatches_an_unscoped_audit_job(store, dispatched, local_only):
    w = AuditWizardView(store)
    w._spawn()

    job, source = dispatched[0]
    assert (job.kind, job.duty, job.audit_action) == ("audit", "audit", "audit")
    # An audit isn't PR-scoped, so it must claim no dedup key.
    assert job.pr_number is None
    assert job.pr_url is None
    assert source == autofix.SOURCE_PANEL


def test_audit_label_names_the_escalations_that_are_on(store, dispatched, local_only):
    """The ongoing-sessions row has to say what this run is allowed to change."""
    w = AuditWizardView(store)
    w.fix_issues.setChecked(True)
    w.open_prs.setChecked(True)
    w._spawn()

    job, _ = dispatched[0]
    assert "issues" in job.label and "open PRs" in job.label


def test_audit_mesh_spawn_routes_over_the_mesh(store, dispatched, mesh_live):
    w = AuditWizardView(store)
    w._spawn()

    assert len(mesh_live) == 1
    assert dispatched == []
    assert not w.spawn_btn.isEnabled()


# ---- shared chrome behaves the same in all three --------------------------


@pytest.mark.parametrize("build", [
    pytest.param(lambda s: WizardView(s), id="review"),
    pytest.param(lambda s: ConflictWizardView(s), id="conflicts"),
    pytest.param(lambda s: AuditWizardView(s), id="audit"),
])
def test_every_wizard_starts_with_an_empty_status_line(store, build):
    assert build(store).status.text() == ""


@pytest.mark.parametrize("build", [
    pytest.param(lambda s: WizardView(s), id="review"),
    pytest.param(lambda s: ConflictWizardView(s), id="conflicts"),
    pytest.param(lambda s: AuditWizardView(s), id="audit"),
])
def test_every_wizard_reports_a_local_dispatch_in_its_status_line(
    store, dispatched, local_only, build
):
    from diplomat_app import widgets

    w = build(store)
    w._spawn()
    term = review.resolved(store.terminal)
    assert w.status.text() == widgets.dispatch_status_text("spawned", term.title)


@pytest.mark.parametrize("build", [
    pytest.param(lambda s: WizardView(s), id="review"),
    pytest.param(lambda s: ConflictWizardView(s), id="conflicts"),
    pytest.param(lambda s: AuditWizardView(s), id="audit"),
])
def test_every_wizard_re_enables_spawn_when_a_mesh_dispatch_returns(
    store, mesh_live, build
):
    """A finished mesh dispatch must hand the button back — otherwise the wizard
    is dead until the panel is rebuilt."""
    w = build(store)
    w._spawn()
    assert not w.spawn_btn.isEnabled()

    w._mesh_done([], "")
    assert w.spawn_btn.isEnabled()


def test_mesh_completion_leaves_spawn_disabled_while_input_is_invalid(
    store, mesh_live
):
    """The button comes back to what the *current* config warrants, not to a flat
    "enabled": if the user emptied the handle while the dispatch was in flight,
    re-enabling would offer a click that can only fail."""
    w = WizardView(store)
    w.target.setCurrentIndex(w.target.findData(PRTarget.SOMEONE))
    w.username.setText("octocat")
    w._spawn()
    assert not w.spawn_btn.isEnabled()

    w.username.setText("")  # input went invalid mid-dispatch
    w._mesh_done([], "")
    assert not w.spawn_btn.isEnabled()


@pytest.mark.parametrize("build", [
    pytest.param(lambda s: WizardView(s), id="review"),
    pytest.param(lambda s: ConflictWizardView(s), id="conflicts"),
    pytest.param(lambda s: AuditWizardView(s), id="audit"),
])
def test_every_wizard_survives_refresh_identity(store, build):
    """The panel calls this on every data refresh, including on the audit wizard
    which has no identity to refresh."""
    w = build(store)
    w.refresh_identity()  # must not raise
