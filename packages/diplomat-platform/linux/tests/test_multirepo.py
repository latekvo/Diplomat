"""Several repo roots: the monitors watch every one, the header picks the one the right
column works on, and Settings edits the list.

The store fixture and the evidence stub come from ``test_autofix``. Most tests pin the
roots with :func:`review.repo_roots`, which every consumer asks; the Settings test
resolves real checkouts instead, since what it edits is the stored list."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_autofix import fake_probes, store  # noqa: F401 — fixture

from diplomat_runtime import (
    activity, appconfig, autofix, diskfloor, review, telemetry, usagescan,
)
from diplomat_runtime.autofix import PRSnapshot, ReviewRequest
from diplomat_runtime.models import API, OpenIssue, OpenPR
from diplomat_runtime.prref import LocalRepo
from diplomat_runtime.prtarget import PRTarget

pytest.importorskip("PySide6")

PRIMARY = LocalRepo("o", "r", "/src/r")
SECOND = LocalRepo("o", "other", "/src/other")
NOW = datetime.now(timezone.utc)


@pytest.fixture
def roots(monkeypatch):
    monkeypatch.setattr(review, "repo_roots", lambda: [PRIMARY, SECOND])


@pytest.fixture
def quiet(store, roots, monkeypatch):  # noqa: F811 — the shared Store fixture
    """Both monitors off, so a poll queues what it finds; GitHub answers nothing
    unless a test says otherwise."""
    store.pr_autofix_enabled = False
    store.review_requests_enabled = False
    store.community_issues_enabled = False
    store.auto_task_limit = 4
    for name in ("fetch_snapshots", "fetch_review_requests", "fetch_open_issues"):
        _by_repo(monkeypatch, name, {})
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    return store


def _by_repo(monkeypatch, name: str, table: dict) -> list[str]:
    """Stub ``autofixmonitor.<name>`` to answer each repo from ``table``; returns the
    repos it was asked about, in order."""
    asked: list[str] = []

    def fetch(owner, repo, *a, **k):
        asked.append(f"{owner}/{repo}")
        return table.get(f"{owner}/{repo}", [])

    monkeypatch.setattr(f"diplomat_app.autofixmonitor.{name}", fetch)
    return asked


def _conflicting(root: LocalRepo, n: int) -> PRSnapshot:
    return PRSnapshot(number=n, title=f"PR {n}", url=f"https://github.com/{root.slug}/pull/{n}",
                      is_draft=False, mergeable="CONFLICTING", review_decision="",
                      threads_unresolved=0, threads_i_owe=0, head_sha=f"{root.repo}-sha")


def _requested(root: LocalRepo, n: int) -> ReviewRequest:
    return ReviewRequest(number=n, title="t", url=f"https://github.com/{root.slug}/pull/{n}",
                         author="bob", author_association="MEMBER", files=[],
                         requested_at="2026-01-02", my_last_review_at=None,
                         head_sha=f"{root.repo}-sha")


def _issue(root: LocalRepo, n: int, at: datetime) -> OpenIssue:
    return OpenIssue(n, f"Issue {n}", f"https://github.com/{root.slug}/issues/{n}",
                     f"u{n}", "NONE", at, at, 0, [], [], False)


# ---- the monitors -----------------------------------------------------------


def test_two_roots_one_pr_number_queue_as_two_tasks(quiet, monkeypatch):
    asked = _by_repo(monkeypatch, "fetch_snapshots", {
        "o/r": [_conflicting(PRIMARY, 5)], "o/other": [_conflicting(SECOND, 5)]})

    quiet._autofix_poll_once()

    assert asked == ["o/r", "o/other"]
    assert sorted(t.id for t in quiet.queued_tasks) == [
        "conflicts:o/other#5", "conflicts:o/r#5"]


def test_two_roots_one_pr_number_dispatch_each_in_its_own_checkout(quiet, monkeypatch):
    quiet.pr_autofix_enabled = True
    _by_repo(monkeypatch, "fetch_snapshots", {
        "o/r": [_conflicting(PRIMARY, 5)], "o/other": [_conflicting(SECOND, 5)]})
    spawned: list[dict] = []
    monkeypatch.setattr(review, "spawn", lambda prompt, preferred, **kw: (
        spawned.append(kw) or kw.get("prompt_file") or "/tmp/prompt.txt"))
    jobs: list[autofix.AgentJob] = []
    dispatch = quiet.dispatch_agent
    monkeypatch.setattr(quiet, "dispatch_agent", lambda job, source, attempt=1: (
        jobs.append(job) or dispatch(job, source, attempt)))

    quiet._autofix_poll_once()

    assert [(j.label, j.pr_number, j.checkout) for j in jobs] == [
        ("Resolve · r#5", 5, None), ("Resolve · other#5", 5, "/src/other")]
    # The primary's checkout is where every spawn starts anyway.
    assert [kw.get("checkout") for kw in spawned] == [None, "/src/other"]
    assert len({j.work_key for j in jobs}) == 2


def test_the_owed_ledger_keeps_every_roots_pending_work(quiet, monkeypatch):
    reqs = {"o/r": [_requested(PRIMARY, 7)], "o/other": [_requested(SECOND, 7)]}
    snaps = {"o/r": [_conflicting(PRIMARY, 5)], "o/other": [_conflicting(SECOND, 5)]}
    keys = {
        *(autofix.ledger_key(autofix.WORK_REVIEW_REQ, r.url, r.head_sha)
          for rs in reqs.values() for r in rs),
        *(autofix.ledger_key(autofix.WORK_CONFLICTS, s.url, s.head_sha)
          for ss in snaps.values() for s in ss),
    }
    for key in keys:
        telemetry.record_queued(key, "review", 7)
    _by_repo(monkeypatch, "fetch_review_requests", reqs)
    _by_repo(monkeypatch, "fetch_snapshots", snaps)

    quiet._autofix_poll_once()

    tasks = {t.key: t for t in telemetry._with_unwritten().tasks if t.key in keys}
    assert set(tasks) == keys
    assert [k for k, t in tasks.items() if t.cleared_at is not None] == []


def test_each_root_keeps_its_own_issue_mark_and_a_new_one_starts_now(quiet, monkeypatch):
    quiet.community_issues_enabled = True
    # The one mark a single-repo build stored: the primary's.
    quiet._settings.setValue("communityIssuesSince", (NOW - timedelta(hours=1)).isoformat())
    _by_repo(monkeypatch, "fetch_open_issues", {
        "o/r": [_issue(PRIMARY, 11, NOW - timedelta(minutes=30))],
        "o/other": [_issue(SECOND, 21, NOW - timedelta(minutes=15))]})

    quiet._poll_community_issues([PRIMARY, SECOND])

    assert [(w.repo, w.number) for w in quiet.requested_work] == [("o/r", 11)]
    marks = json.loads(quiet._settings.value("communityIssuesSince"))
    assert set(marks) == {"o/r", "o/other"}
    # Its backlog is baselined past, not queued.
    assert datetime.fromisoformat(marks["o/other"]) == NOW - timedelta(minutes=15)

    later = NOW + timedelta(hours=1)
    _by_repo(monkeypatch, "fetch_open_issues", {
        "o/r": [_issue(PRIMARY, 12, later)], "o/other": [_issue(SECOND, 22, later)]})
    quiet._poll_community_issues([PRIMARY, SECOND])

    assert [(w.repo, w.number) for w in quiet.requested_work] == [
        ("o/r", 11), ("o/r", 12), ("o/other", 22)]


def test_state_from_a_single_repo_build_names_the_primary_root(quiet):
    s = quiet._settings
    s.setValue("queuedTaskOrder", ["conflicts:5", "review-req:o/other#6", "investigate:1.2"])
    attempt = {"requestedAt": "", "lastDispatchedAt": 1.0, "attempts": 1}
    for key in ("reviewReqAttempts", "myReviewAttempts", "myConflictAttempts"):
        s.setValue(key, json.dumps({"7": attempt, "o/other#7": attempt}))
    s.setValue("autofixFingerprints", json.dumps({"5": {"mergeable": "MERGEABLE"}}))

    assert quiet.queued_task_order == [
        "conflicts:o/r#5", "review-req:o/other#6", "investigate:1.2"]
    for key in ("reviewReqAttempts", "myReviewAttempts", "myConflictAttempts"):
        assert sorted(quiet._load_attempts(key)) == ["o/other#7", "o/r#7"], key
    assert list(quiet._load_fingerprints()) == ["o/r#5"]


def test_an_ask_whose_root_is_gone_is_dropped_and_said(quiet):
    quiet.requested_work = [
        autofix.RequestedWork(number=3, url="https://github.com/o/gone/pull/3", author="",
                              config={"kind": "review", "specificPR": "3", "depth": "deep"}, repo="o/gone"),
        autofix.RequestedWork(number=3, url="https://github.com/o/other/pull/3", author="",
                              config={"kind": "review", "specificPR": "3", "depth": "deep"}, repo="o/other"),
    ]

    quiet._offer_requested_work()

    assert [w.repo for w in quiet.requested_work] == ["o/other"]
    assert [t.job.checkout for t in quiet._staged_queue] == ["/src/other"]
    assert [t.job.label for t in quiet._staged_queue] == ["Review · other#3 · deep"]
    assert [e.detail for e in activity.read() if e.action == "queue-drop"] == [
        "Review · gone#3 · deep - o/gone is no longer a repo root, not run"]


def test_the_mesh_takes_only_the_primary_roots_work(store, roots, monkeypatch):  # noqa: F811
    """A peer on a Diplomat from before repo routing runs every job in its single root,
    so another root's work stays here. What does go names its repo."""
    from szpontnet import ctl
    from test_autofix import _mesh_store, _spawned

    calls = _mesh_store(monkeypatch, store, dispatch=_spawned())
    inner, repos = ctl.dispatch, []
    monkeypatch.setattr(ctl, "dispatch",
                        lambda *a, repo="", **k: repos.append(repo) or inner(*a, **k))

    def job(repo, checkout):
        return autofix.AgentJob(kind="review", audit_action="review", label="L", prompt="P",
                                pr_url=f"https://github.com/O/{repo}/pull/3", pr_number=3,
                                duty="review", work_key=f"review:github.com/O/{repo}#3@s",
                                checkout=checkout)

    assert store._route_via_mesh(job("other", "/src/gone")) == (None, False, "")
    assert store._route_via_mesh(job("other", "/src/other")) == (None, False, "")
    assert calls == []
    assert store._route_via_mesh(job("r", None))[0] == "spawned"
    assert store._route_via_mesh(job("r", "/src/r"))[0] == "spawned"
    assert calls == [("review", "review:github.com/O/r#3@s")] * 2
    assert repos == ["o/r"] * 2


def test_a_wizards_mesh_dispatch_carries_its_repo(store, monkeypatch):  # noqa: F811
    from szpontnet import ctl

    sent: list = []
    monkeypatch.setattr("diplomat_app.szpont.require", lambda: None)
    monkeypatch.setattr(store, "start_background", lambda work, name=None: work())
    monkeypatch.setattr(store, "refresh_mesh_state", lambda: None)
    monkeypatch.setattr(ctl, "dispatch",
                        lambda duty, prompt, repo="": sent.append((duty, repo)) or [])
    store.mesh_dispatch("review", "P", repo="o/other")
    assert sent == [("review", "o/other")]


def test_the_disk_floor_measures_every_root(quiet, monkeypatch):
    measured: list[str] = []
    monkeypatch.setenv(diskfloor.ENV_OVERRIDE, "1")
    monkeypatch.setattr(diskfloor, "free", lambda p: measured.append(p) or 1 << 40)
    monkeypatch.setattr(review, "spawn", lambda *a, **kw: "/tmp/prompt.txt")

    quiet._dispatch_conflict_fix(5, "https://github.com/o/other/pull/5", 1, autofix.SOURCE_AUTO)

    assert measured == ["/src/r", "/src/other", str(Path.home() / ".diplomat")]


def test_usage_counts_every_root_and_its_worktrees(roots):
    got = usagescan.repo_roots()
    assert [str(p) for p in got] == [
        str(Path(p).resolve()) for p in
        ("/src/r", "/src/r-worktrees", "/src/other", "/src/other-worktrees")]


def test_an_ask_names_its_repo_only_among_several_roots():
    ask = autofix.RequestedWork(number=3, url="", author="",
                                config={"kind": "review", "specificPR": "3", "depth": "deep"}, repo="o/other")
    assert ask.label == "Review · #3 · deep"
    assert ask.label_among(2) == "Review · other#3 · deep"


# ---- the selected root ------------------------------------------------------


def _pr(root: LocalRepo, n: int) -> OpenPR:
    """An open PR ready for weeks, so the stale-ready list carries it."""
    old = NOW - timedelta(days=60)
    return OpenPR(n, f"{root.repo} {n}", f"https://github.com/{root.slug}/pull/{n}", False,
                  "bob", old, None, [], None, [])


def test_switching_roots_shows_its_last_fetch_and_refetches_it(quiet, monkeypatch):
    lists = {"o/r": [_pr(PRIMARY, 5)], "o/other": [_pr(SECOND, 5), _pr(SECOND, 6)]}
    fetched: list[str] = []
    monkeypatch.setattr(API, "fetch_viewer_login", staticmethod(lambda: "alice"))
    monkeypatch.setattr(API, "fetch_open_prs", staticmethod(
        lambda o, r: fetched.append(f"{o}/{r}") or lists[f"{o}/{r}"]))
    monkeypatch.setattr(API, "fetch_open_issues", staticmethod(lambda o, r: []))

    def listed() -> set[str]:
        return {i.url for t in quiet.visible_tools for i in quiet.items_for(t.id)}

    quiet.refresh()
    assert fetched == ["o/r"]
    assert quiet.lookup(5).url == "https://github.com/o/r/pull/5"
    assert listed() == {"https://github.com/o/r/pull/5"}

    quiet.select_repo("o/other")
    assert quiet.selected_repo == SECOND
    assert quiet.prs == [] and not quiet.has_loaded, "nothing fetched for it yet"
    quiet.refresh()
    assert fetched == ["o/r", "o/other"]
    assert quiet.lookup(5).url == "https://github.com/o/other/pull/5"
    assert listed() == {"https://github.com/o/other/pull/5", "https://github.com/o/other/pull/6"}

    quiet.select_repo("O/R")
    assert quiet.lookup(5).url == "https://github.com/o/r/pull/5", "shown from the cache"
    assert fetched == ["o/r", "o/other"]


def test_the_selection_outlives_the_process_and_falls_back_to_the_primary(quiet):
    from diplomat_app.store import Store

    quiet.select_repo("o/other")
    assert Store().selected_repo == SECOND
    quiet._settings.setValue("selectedRepo", "o/removed")
    assert Store().selected_repo == PRIMARY


@pytest.fixture
def app():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def wizard_store(app, roots):
    """A plain Store, as ``test_wizard_views`` builds: one from ``test_autofix``'s
    fixture outlives its wizard and crashes a later test's event loop."""
    from diplomat_app.store import Store

    s = Store()
    s.me = "alice"
    s.has_loaded = True
    return s


@pytest.fixture
def dispatched(wizard_store, monkeypatch):
    from diplomat_app.meshspawn import MeshSpawnRow

    monkeypatch.setattr(MeshSpawnRow, "use_mesh", lambda self: False)
    jobs: list[autofix.AgentJob] = []
    monkeypatch.setattr(wizard_store, "dispatch_agent",
                        lambda job, source, attempt=1: jobs.append(job) or "spawned")
    return jobs


def _review_wizard(st, text):
    from diplomat_app.wizardview import WizardView

    w = WizardView(st)
    w.target.setCurrentIndex(w.target.findData(PRTarget.SPECIFIC))
    w.specific_pr.setText(text)
    return w


def test_a_bare_number_means_the_selected_root(wizard_store, dispatched):
    wizard_store.select_repo("o/other")
    w = _review_wizard(wizard_store, "5")
    assert w._config().home == SECOND
    assert not w.mesh_row._applicable, "a peer would start it in its primary root"
    w._spawn()
    [job] = dispatched
    assert (job.pr_number, job.checkout) == (5, "/src/other")


def test_a_wizard_follows_the_picker(wizard_store):
    w = _review_wizard(wizard_store, "5")
    assert w._config().home == PRIMARY and w.mesh_row._applicable
    wizard_store.select_repo("o/other")
    assert w._config().home == SECOND and not w.mesh_row._applicable


def test_a_link_to_another_root_is_deduped_by_its_number(wizard_store, dispatched):
    w = _review_wizard(wizard_store, "https://github.com/o/other/pull/9")
    assert w.spawn_btn.isEnabled()
    w._spawn()
    w = _review_wizard(wizard_store, "https://github.com/o/r/pull/9")
    wizard_store.select_repo("o/other")
    w._spawn()
    assert [(j.pr_number, j.pr_url, j.checkout) for j in dispatched] == [
        (9, "https://github.com/o/other/pull/9", "/src/other"),
        (9, "https://github.com/o/r/pull/9", None)]


# ---- the header and Settings ------------------------------------------------


@pytest.fixture
def make_panel(app, monkeypatch):
    from diplomat_app.panel import Panel
    from diplomat_app.store import Store

    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    fake_probes(monkeypatch)
    built = []

    def build(roots_: list[LocalRepo]):
        monkeypatch.setattr(review, "repo_roots", lambda: list(roots_))
        store_ = Store()
        store_.me = "alice"
        built.append(Panel(store_))
        return built[-1]

    yield build
    for p in built:
        p.deleteLater()


def test_the_header_offers_a_picker_only_with_several_roots(make_panel):
    one = make_panel([PRIMARY])
    assert one.repo_picker.isHidden() and not one.repo_label.isHidden()
    assert one.repo_label.text() == "o/r"

    two = make_panel([PRIMARY, SECOND])
    assert not two.repo_picker.isHidden() and two.repo_label.isHidden()
    assert [two.repo_picker.itemText(i) for i in range(two.repo_picker.count())] == [
        "o/r", "o/other"]
    refreshed = []
    two.refresh_requested.connect(lambda: refreshed.append(1))
    two.repo_picker.setCurrentIndex(1)
    two.repo_picker.activated.emit(1)
    assert two.store.selected_repo == SECOND and refreshed == [1]


def _checkout(tmp_path: Path, name: str) -> str:
    where = tmp_path / name
    where.mkdir()
    subprocess.run(["git", "-C", str(where), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(where), "remote", "add", "origin",
                    f"git@github.com:o/{name}.git"], check=True)
    return str(where)


def test_the_roots_list_adds_raises_and_removes_through_the_config(app, tmp_path,
                                                                   monkeypatch):
    from PySide6.QtWidgets import QFileDialog, QLabel

    from diplomat_app.settingsview import SettingsView
    from diplomat_app.store import Store

    monkeypatch.delenv("DIPLOMAT_REPO", raising=False)
    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    first, second = _checkout(tmp_path, "r"), _checkout(tmp_path, "other")
    appconfig.set_repo_roots([first])
    view = SettingsView(Store())

    def names() -> list[str]:
        return [view._roots_list.itemAt(i).widget().findChildren(QLabel)[0].text()
                for i in range(view._roots_list.count())]

    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a: second))
    view._add_root()
    assert appconfig.repo_roots() == [first, second]
    assert names() == ["o/r", "o/other"]

    view._raise_root(1)
    assert appconfig.repo_roots() == [second, first]
    assert names() == ["o/other", "o/r"]
    assert review.repo_path() == second

    view._remove_root(1)
    assert appconfig.repo_roots() == [second]
    assert names() == ["o/other"]
    view.deleteLater()


def test_a_comms_review_ask_matches_only_the_agent_on_its_own_repos_pr():
    """Two review agents on #12, one per root: the ask about the second root's #12 is
    the second agent's, not whichever comes first."""
    from diplomat_app.store import Store

    def row(item):
        return autofix.RunningAgent(pr_number=12, label="", kind=autofix.QUEUE_REVIEW_ACTION,
                                    tracked=True, item=item)

    rows = [row("o/r#12"), row("o/other#12")]
    key = autofix.queue_key(autofix.QUEUE_REVIEW_ACTION, "o/other#12")
    assert Store._comms_row_doing(key, rows) is rows[1]
    assert Store._comms_row_doing(autofix.queue_key(autofix.QUEUE_REVIEW_ACTION, "o/x#12"),
                                  rows) is None
