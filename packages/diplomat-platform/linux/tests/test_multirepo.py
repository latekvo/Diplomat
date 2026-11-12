"""Several repo roots: the monitors watch every one, the header picks the one the right
column works on, and Settings edits the list.

The store fixture and the evidence stub come from ``test_autofix``. Most tests pin the
roots with :func:`review.repo_roots`, which every consumer asks; the Settings test
resolves real checkouts instead, since what it edits is the stored list."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from test_autofix import fake_probes, store  # noqa: F401 — fixture

from diplomat_runtime import (
    activity, appconfig, autofix, core, diskfloor, localrepos, review, telemetry, usagescan,
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
    """The two roots, the primary standing for the configured repo on the mesh."""
    monkeypatch.setattr(review, "repo_roots", lambda: [PRIMARY, SECOND])
    monkeypatch.setattr(review, "roots_complete", lambda: True)
    monkeypatch.setattr(review, "mesh_checkout", lambda: PRIMARY.path)


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

    def fetch(root, *a, **k):
        asked.append(root.slug)
        return table.get(root.slug, [])

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
        ("Resolve · r#5", 5, "/src/r"), ("Resolve · other#5", 5, "/src/other")]
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
    cfg = core.config()
    monkeypatch.setattr(core, "config", lambda: {**cfg, "owner": "o", "repo": "r"})
    # The one mark a single-repo build stored: the configured repo's.
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


def test_a_single_builds_mark_is_the_configured_repos_whatever_root_is_first(quiet,
                                                                              monkeypatch):
    """A single-repo build scanned the configured repo. A first root resolving to
    another repo is baselined, not handed that repo's old mark."""
    quiet.community_issues_enabled = True
    quiet._settings.setValue("communityIssuesSince", (NOW - timedelta(hours=1)).isoformat())
    _by_repo(monkeypatch, "fetch_open_issues", {
        "o/r": [_issue(PRIMARY, 11, NOW - timedelta(minutes=30))]})

    quiet._poll_community_issues([PRIMARY, SECOND])

    assert quiet.requested_work == []


def test_a_root_removed_and_added_back_queues_none_of_its_backlog(quiet, monkeypatch):
    quiet.community_issues_enabled = True
    _by_repo(monkeypatch, "fetch_open_issues", {
        "o/r": [_issue(PRIMARY, 11, NOW - timedelta(minutes=30))],
        "o/other": [_issue(SECOND, 21, NOW - timedelta(minutes=30))]})
    quiet._poll_community_issues([PRIMARY, SECOND])
    quiet._poll_community_issues([PRIMARY])
    assert set(json.loads(quiet._settings.value("communityIssuesSince"))) == {"o/r"}

    later = NOW + timedelta(hours=1)
    _by_repo(monkeypatch, "fetch_open_issues", {
        "o/other": [_issue(SECOND, n, later + timedelta(minutes=n)) for n in (22, 23, 24)]})
    quiet._poll_community_issues([PRIMARY, SECOND])

    assert quiet.requested_work == []


def test_a_root_that_failed_to_resolve_keeps_its_issue_mark(quiet, monkeypatch):
    quiet.community_issues_enabled = True
    _by_repo(monkeypatch, "fetch_open_issues", {
        "o/other": [_issue(SECOND, 21, NOW - timedelta(minutes=30))]})
    quiet._poll_community_issues([PRIMARY, SECOND])
    quiet._poll_community_issues([PRIMARY], complete=False)

    filed = NOW + timedelta(minutes=5)
    _by_repo(monkeypatch, "fetch_open_issues", {"o/other": [_issue(SECOND, 22, filed)]})
    quiet._poll_community_issues([PRIMARY, SECOND])

    assert [(w.repo, w.number) for w in quiet.requested_work] == [("o/other", 22)]


def test_a_root_that_failed_to_resolve_keeps_its_concluded_reviews(quiet, monkeypatch):
    """A concluded record is kept only while its request is seen owed, and a root that
    did not resolve is not read."""
    quiet.review_requests_enabled = True
    dispatched: list = []
    monkeypatch.setattr(quiet, "_dispatch_review_request",
                        lambda r, attempt: dispatched.append((r.item, attempt)) or True)
    _by_repo(monkeypatch, "fetch_review_requests", {"o/other": [_requested(SECOND, 7)]})
    long_ago = time.time() - autofix.RETRY_MAX_BACKOFF - 60
    quiet._save_attempts("reviewReqAttempts", {"o/other#7": autofix.ReviewAttempt(
        "2026-01-02", long_ago, 1, concluded_at=long_ago)})
    quiet._poll_review_requests([PRIMARY], complete=False)

    quiet._poll_review_requests([PRIMARY, SECOND])

    assert dispatched == []


def test_a_poll_goes_by_the_roots_reading_it_started_with(quiet, monkeypatch):
    """The roots are re-read on a timer, possibly during the poll's fetches: a root back
    by the end of the poll was still missing from the list it polled."""
    quiet.community_issues_enabled = True
    marks = {"o/r": NOW.isoformat(), "o/other": NOW.isoformat()}
    quiet._settings.setValue("communityIssuesSince", json.dumps(marks))
    monkeypatch.setattr(review, "repo_roots", lambda: [PRIMARY])
    readings = iter([False])
    monkeypatch.setattr(review, "roots_complete", lambda: next(readings, True))

    quiet._autofix_poll_once()

    assert set(json.loads(quiet._settings.value("communityIssuesSince"))) == {"o/r", "o/other"}


def test_a_queued_task_keeps_its_root_when_another_becomes_primary(quiet, monkeypatch):
    _by_repo(monkeypatch, "fetch_snapshots", {"o/r": [_conflicting(PRIMARY, 5)]})
    quiet._autofix_poll_once()
    spawned: list[dict] = []
    monkeypatch.setattr(review, "spawn", lambda prompt, preferred, **kw: (
        spawned.append(kw) or kw.get("prompt_file") or "/tmp/prompt.txt"))
    monkeypatch.setattr(review, "repo_roots", lambda: [SECOND, PRIMARY])
    monkeypatch.setattr(review, "repo_path", lambda: SECOND.path)
    quiet.pr_autofix_enabled = True

    quiet._autofix_poll_once()

    assert [kw.get("checkout") for kw in spawned] == ["/src/r"]


def test_every_monitor_prompt_names_its_roots_repo(quiet, monkeypatch):
    """The prompt's repo is its config's home; without one it is the configured repo's."""
    monkeypatch.setattr("diplomat_runtime.promptcore.build_prompt", lambda cfg: (
        f"{cfg.get('kind')} in {(cfg.get('home') or {}).get('repo')}"))
    prompts: list[tuple[str, str | None]] = []
    monkeypatch.setattr(quiet, "dispatch_agent", lambda job, source, attempt=1: (
        prompts.append((job.prompt, job.checkout)) or "spawned"))
    url = "https://github.com/o/other/pull/5"
    snap = PRSnapshot(number=5, title="t", url=url, is_draft=False, mergeable="MERGEABLE",
                      review_decision="", threads_unresolved=1, threads_i_owe=1, head_sha="s")

    assert quiet._dispatch_conflict_fix(5, url, 1, autofix.SOURCE_AUTO)
    assert quiet._dispatch_my_review(snap)
    assert quiet._dispatch_review_request(_requested(SECOND, 5))
    assert prompts == [("conflicts in other", "/src/other"), ("review in other", "/src/other"),
                       ("review in other", "/src/other")]

    quiet.community_issues_enabled = True
    _by_repo(monkeypatch, "fetch_open_issues", {"o/other": [_issue(SECOND, 21, NOW)]})
    quiet._settings.setValue("communityIssuesSince", json.dumps(
        {"o/other": (NOW - timedelta(hours=1)).isoformat()}))
    quiet._poll_community_issues([PRIMARY, SECOND])
    assert [w.config["home"]["repo"] for w in quiet.requested_work] == ["other"]


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


def test_a_legacy_ask_keeps_its_repo_whichever_root_is_primary(store, monkeypatch):  # noqa: F811
    """A row a single-repo build stored names no repo: its link's, else the configured
    repo's, written back on the first read so no later primary re-homes it."""
    from diplomat_runtime import core

    cfg = core.config()
    configured = f"{cfg['owner']}/{cfg['repo']}".lower()
    home = LocalRepo(cfg["owner"], cfg["repo"], "/src/home")
    review_cfg = {"kind": "review", "specificPR": "7", "depth": "deep"}
    rows = [autofix.RequestedWork(number=7, url="https://github.com/O/There/pull/7",
                                  author="", config=review_cfg).to_json(),
            autofix.RequestedWork(number=8, url="", author="", config=review_cfg).to_json()]
    for row in rows:
        del row["repo"]
    store._settings.setValue("requestedReviews", json.dumps(rows))

    monkeypatch.setattr(review, "repo_roots", lambda: [SECOND, home])
    assert [w.repo for w in store.requested_work] == ["o/there", configured]
    stored = json.loads(store._settings.value("requestedReviews"))
    assert [r["repo"] for r in stored] == ["o/there", configured]

    monkeypatch.setattr(review, "repo_roots", lambda: [home, SECOND])
    assert [w.repo for w in store.requested_work] == ["o/there", configured]


def _issue_of(root: LocalRepo, n: int) -> OpenIssue:
    return OpenIssue(n, f"Issue {n}", f"https://github.com/{root.slug}/issues/{n}",
                     "carol", "CONTRIBUTOR", NOW, NOW, 0, [], [], False)


def test_a_review_sweep_asks_of_its_home_root(quiet):
    sweep = review.ReviewConfig(target=PRTarget.MINE, me=quiet.me, depth="deep",
                                include_drafts=True, include_ready=True)
    quiet.prs = [dataclasses.replace(_pr(PRIMARY, 5), author=quiet.me)]
    assert quiet.request_review_sweep(dataclasses.replace(sweep, home=PRIMARY)) == (1, 0)
    quiet.prs = [dataclasses.replace(_pr(SECOND, 5), author=quiet.me)]

    assert quiet.request_review_sweep(dataclasses.replace(sweep, home=SECOND)) == (1, 0)
    assert [w.key for w in quiet.requested_work] == ["review:o/r#5", "review:o/other#5"]


def test_an_issue_sweep_asks_of_its_home_root_and_works_in_its_checkout(quiet):
    from diplomat_app import issues

    quiet.issues = [_issue_of(SECOND, 7)]
    sweep = issues.IssueConfig(target=issues.Target.CONTRIBUTORS, me=quiet.me, depth="deep",
                               unassigned_only=True, home=SECOND)

    assert quiet.request_issue_sweep(sweep) == (1, 0)
    assert [w.key for w in quiet.requested_work] == ["issues:o/other#7"]
    quiet._offer_requested_work()
    assert [t.job.checkout for t in quiet._staged_queue] == ["/src/other"]


def test_an_ask_works_where_its_root_is_now_not_where_it_was_asked(quiet):
    def ask(root: LocalRepo, old_path: str) -> autofix.RequestedWork:
        home = {"owner": root.owner, "repo": root.repo, "path": old_path}
        return autofix.RequestedWork(
            number=3, url=f"https://github.com/{root.slug}/pull/3", author="",
            config={"kind": "review", "specificPR": "3", "depth": "deep", "home": home},
            repo=root.slug)

    quiet.requested_work = [ask(PRIMARY, "/old/r"), ask(SECOND, "/old/other")]
    quiet._offer_requested_work()

    assert [t.job.checkout for t in quiet._staged_queue] == ["/src/r", "/src/other"]


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


def test_the_drain_starts_nothing_in_a_root_just_taken_out(quiet, monkeypatch):
    """The commit at the end of the poll drops its rows; the drain at its top runs
    before anything else knows the root is gone."""
    quiet.queue_auto_run = False
    _by_repo(monkeypatch, "fetch_review_requests", {"o/other": [_requested(SECOND, 7)]})
    quiet.requested_work = [autofix.RequestedWork(
        number=11, url="https://github.com/o/other/issues/11", author="",
        config={"kind": "fix_issues", "target": "specific", "specificIssue": "11",
                "home": {"owner": "o", "repo": "other", "path": "/src/other"}},
        repo="o/other")]
    quiet._autofix_poll_once()
    assert sorted(t.id for t in quiet.queued_tasks) == [
        "fix_issues:o/other#11", "review-req:o/other#7"]
    monkeypatch.setattr(review, "repo_roots", lambda: [PRIMARY])
    quiet.queue_auto_run = True
    started: list[str] = []
    monkeypatch.setattr(quiet, "dispatch_agent", lambda job, source, attempt=1, **kw: (
        started.append(job.label) or "spawned"))

    quiet._autofix_poll_once()

    assert quiet.autofix_poll_error is None
    assert started == [] and quiet.queued_tasks == [] and quiet.requested_work == []
    assert [e.detail for e in activity.read() if e.action == "queue-drop"][-1].endswith(
        "o/other is no longer a repo root, not run")


def test_an_ask_whose_root_failed_to_resolve_is_kept_and_not_run(quiet, monkeypatch):
    """A root path that fails to resolve (a checkout briefly missing, a git timeout)
    drops its repo from the roots; its asks wait for it rather than being forgotten."""
    monkeypatch.setattr(review, "roots_complete", lambda: False)
    quiet.requested_work = [
        autofix.RequestedWork(number=3, url="https://github.com/o/gone/pull/3", author="",
                              config={"kind": "review", "specificPR": "3", "depth": "deep"}, repo="o/gone"),
        autofix.RequestedWork(number=3, url="https://github.com/o/other/pull/3", author="",
                              config={"kind": "review", "specificPR": "3", "depth": "deep"}, repo="o/other"),
    ]

    quiet._offer_requested_work()

    assert [w.repo for w in quiet.requested_work] == ["o/gone", "o/other"]
    assert [t.job.checkout for t in quiet._staged_queue] == ["/src/other"]
    assert [e for e in activity.read() if e.action == "queue-drop"] == []


def test_the_mesh_takes_only_the_configured_repos_work(store, roots, monkeypatch):  # noqa: F811
    """A peer on a Diplomat from before repo routing runs every job in its single root,
    a checkout of the configured repo, so another root's work stays here, whichever
    root is primary. What does go names its repo."""
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

    monkeypatch.setattr(review, "mesh_checkout", lambda: SECOND.path)
    assert store._route_via_mesh(job("r", None)) == (None, False, "")
    assert store._route_via_mesh(job("other", "/src/other"))[0] == "spawned"
    monkeypatch.setattr(review, "mesh_checkout", lambda: None)
    assert store._route_via_mesh(job("r", "/src/r")) == (None, False, "")
    assert len(calls) == 3


def test_the_mesh_checkout_is_the_configured_repos_root(monkeypatch):
    from diplomat_runtime import core

    cfg = core.config()
    configured = LocalRepo(cfg["owner"].upper(), cfg["repo"], "/src/conf")
    monkeypatch.setattr(review, "repo_roots", lambda: [SECOND, configured])
    assert review.mesh_checkout() == "/src/conf"
    monkeypatch.setattr(review, "repo_roots", lambda: [PRIMARY, SECOND])
    assert review.mesh_checkout() is None


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


def test_the_closed_pr_read_covers_every_root(quiet, monkeypatch):
    asked = _by_repo(monkeypatch, "fetch_closed_prs", {})

    quiet._autofix_poll_once()

    assert asked == ["o/r", "o/other"]


def test_a_comms_bump_moves_the_named_roots_item(quiet):
    def task(key):
        return autofix.QueuedTask(id=key, job=autofix.AgentJob(
            kind="review", audit_action="review", label=key, prompt="P", pr_url=None,
            pr_number=None, duty="review"), attempt=1)

    quiet.queued_tasks = [task(k) for k in ("review:o/r#9", "review:o/r#5", "review:o/other#5")]

    quiet._comms_bump(5, "o/other")
    assert [t.id for t in quiet.queued_tasks] == [
        "review:o/other#5", "review:o/r#9", "review:o/r#5"]
    quiet._comms_bump(5, "")
    assert [t.id for t in quiet.queued_tasks][0] == "review:o/r#5"


def test_a_prompt_names_an_item_whatever_the_slugs_case():
    text = "Review PR #5 in O/Other. Use the `gh` CLI"
    assert list(autofix.prompt_items(text, {"O/Other"})) == ["o/other#5"]


def test_a_run_with_a_pr_number_but_no_pr_link_holds_an_unscoped_bay():
    from diplomat_runtime import agentstate

    run = agentstate.RunRecord(run_id="r1", dispatched_at=0.0, pr_number=5, pr_url="",
                               tty="ttys003")
    assert run.item is None
    assert autofix.unscoped_auto_ttys([run], "ttys003 01:00 claude do it\n") == {"ttys003"}


def test_the_tick_scans_for_live_agents_on_every_root(roots, monkeypatch):
    from diplomat_app import probes
    from diplomat_runtime import agentstate

    monkeypatch.setattr(probes, "_ps_dump", lambda now: agentstate.Observation.present(
        "  42 ttys003 60 claude Review PR #5 in o/other. Use the `gh` CLI\n"
        "  43 ttys004 60 claude Review PR #6 in o/r. Use the `gh` CLI\n"))

    scan = probes.gather([], 0.0, mesh_enabled=False).live_agents

    assert scan.value == {"o/other#5": "ttys003", "o/r#6": "ttys004"}


def test_the_disk_floor_measures_every_root(quiet, monkeypatch):
    measured: list[str] = []
    monkeypatch.setenv(diskfloor.ENV_OVERRIDE, "1")
    monkeypatch.setattr(diskfloor, "free", lambda p: measured.append(p) or 1 << 40)
    monkeypatch.setattr(review, "spawn", lambda *a, **kw: "/tmp/prompt.txt")

    quiet._dispatch_conflict_fix(5, "https://github.com/o/other/pull/5", 1, autofix.SOURCE_AUTO)

    assert measured == ["/src/r", "/src/other", str(Path.home() / ".diplomat")]


def test_asks_are_forgotten_by_one_roots_reading(quiet, monkeypatch):
    """A re-read mid-pass that has every root resolved says nothing of the reading
    that left these repos out."""
    readings = iter([False])
    monkeypatch.setattr(review, "roots_complete", lambda: next(readings, True))
    quiet.requested_work = [
        autofix.RequestedWork(number=n, url=f"https://github.com/o/gone/pull/{n}", author="",
                              config={"kind": "review", "specificPR": str(n), "depth": "deep"},
                              repo="o/gone")
        for n in (3, 4)]

    quiet._offer_requested_work()

    assert [w.number for w in quiet.requested_work] == [3, 4]


def test_a_monitor_dispatch_in_no_root_is_skipped(quiet, monkeypatch):
    jobs: list[autofix.AgentJob] = []
    monkeypatch.setattr(quiet, "dispatch_agent",
                        lambda job, source, attempt=1: jobs.append(job) or "spawned")
    gone = "https://github.com/o/gone/pull/5"
    snap = PRSnapshot(number=5, title="t", url=gone, is_draft=False, mergeable="MERGEABLE",
                      review_decision="", threads_unresolved=1, threads_i_owe=1, head_sha="s")
    req = ReviewRequest(number=5, title="t", url=gone, author="bob", author_association="MEMBER",
                        files=[], requested_at="2026-01-02", my_last_review_at=None, head_sha="s")

    assert not quiet._dispatch_conflict_fix(5, gone, 1, autofix.SOURCE_AUTO)
    assert not quiet._dispatch_my_review(snap)
    assert not quiet._dispatch_review_request(req)
    assert jobs == []


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
        lambda root: fetched.append(root.slug) or lists[root.slug]))
    monkeypatch.setattr(API, "fetch_open_issues", staticmethod(lambda root: []))

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


def test_a_pick_during_a_fetch_is_fetched_before_the_lists_change(quiet, monkeypatch):
    """A refresh already running is the only one: the panel's next ask is dropped
    while it runs."""
    lists = {"o/r": [_pr(PRIMARY, 5)], "o/other": [_pr(SECOND, 6)]}
    fetched: list[str] = []

    def fetch(root):
        fetched.append(root.slug)
        if len(fetched) == 1:
            quiet.select_repo("o/other")
        return lists[root.slug]

    monkeypatch.setattr(API, "fetch_viewer_login", staticmethod(lambda: "alice"))
    monkeypatch.setattr(API, "fetch_open_prs", staticmethod(fetch))
    monkeypatch.setattr(API, "fetch_open_issues", staticmethod(lambda root: []))

    quiet.refresh()

    assert fetched == ["o/r", "o/other"]
    assert quiet.prs == lists["o/other"]


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
    assert job.label.startswith("Review · other#5 · ")


def test_a_conflict_run_names_the_selected_root(wizard_store, dispatched):
    from diplomat_app.conflictwizardview import ConflictWizardView

    wizard_store.select_repo("o/other")
    w = ConflictWizardView(wizard_store)
    w.target.setCurrentIndex(w.target.findData(PRTarget.SPECIFIC))
    w.specific_pr.setText("5")
    w._spawn()
    [job] = dispatched
    assert job.label == "Resolve conflicts · other#5"


def test_a_wizard_follows_the_picker(wizard_store, monkeypatch):
    w = _review_wizard(wizard_store, "5")
    assert w._config().home == PRIMARY and w.mesh_row._applicable
    wizard_store.select_repo("o/other")
    assert w._config().home == SECOND and not w.mesh_row._applicable

    # The mesh row follows the configured repo's root, not the primary.
    monkeypatch.setattr(review, "mesh_checkout", lambda: SECOND.path)
    w._sync()
    assert w.mesh_row._applicable
    wizard_store.select_repo("o/r")
    assert not w.mesh_row._applicable


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


@pytest.mark.parametrize("selected, text", [
    ("o/other", "12"), ("o/other", "https://github.com/o/other/issues/12"),
    ("o/r", "https://github.com/o/other/issues/12")])
def test_comms_adopts_a_fix_the_issues_wizard_is_running(wizard_store, dispatched,
                                                         selected, text):
    from diplomat_app import issues
    from diplomat_app.issuewizardview import IssueWizardView
    from diplomat_app.store import Store

    wizard_store.select_repo(selected)
    w = IssueWizardView(wizard_store)
    w.target.setCurrentIndex(w.target.findData(issues.Target.SPECIFIC))
    w.specific_issue.setText(text)
    w._spawn()
    [job] = dispatched
    rows = [autofix.RunningAgent(pr_number=None, label=job.label,
                                 kind=autofix.QUEUE_ISSUES_ACTION, tracked=True)]

    assert job.label.startswith("Issues · other#12 · ")
    assert Store._comms_row_doing(
        autofix.queue_key(autofix.QUEUE_ISSUES_ACTION, "o/other#12"), rows) is rows[0]
    assert Store._comms_row_doing(
        autofix.queue_key(autofix.QUEUE_ISSUES_ACTION, "o/r#12"), rows) is None


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


@pytest.fixture
def remotes(monkeypatch):
    """The roots resolved for real, from these checkouts' remotes: drop one to make its
    path fail to resolve. :func:`_reread` ages the reading past its TTL."""
    table = {"/src/r": "remote.origin.url https://github.com/o/r.git\n",
             "/src/other": "remote.origin.url https://github.com/o/other.git\n"}
    monkeypatch.setattr(review, "repo_root_paths", lambda: ["/src/r", "/src/other"])
    monkeypatch.setattr(localrepos, "remote_config", lambda p: table.get(p))
    monkeypatch.setattr(review, "_roots_cache", None)
    return table


def _reread() -> None:
    review._roots_cache = (review._roots_cache[0], float("-inf"), *review._roots_cache[2:])
    review.repo_roots()


def test_the_header_and_lists_follow_a_root_that_resolves_later(app, remotes, monkeypatch):
    """Whoever re-reads the roots: one that did not resolve when the panel was built
    joins the picker, and the lists follow the stored selection to it at once."""
    from diplomat_app.panel import Panel
    from diplomat_app.store import Store

    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    fake_probes(monkeypatch)
    other = remotes.pop("/src/other")
    store_ = Store()
    store_.me = "alice"
    store_._settings.setValue("selectedRepo", "o/other")
    store_._panel_data = {"o/r": ([_pr(PRIMARY, 5)], [], NOW),
                          "o/other": ([_pr(SECOND, 6)], [], NOW)}
    store_._show_panel_data(store_.selected_repo)
    panel = Panel(store_)
    assert panel.repo_picker.isHidden() and store_.prs == [_pr(PRIMARY, 5)]
    refetch: list[int] = []
    panel.refresh_requested.connect(lambda: refetch.append(1))

    remotes["/src/other"] = other
    _reread()

    assert not panel.repo_picker.isHidden()
    assert panel.repo_picker.currentData() == "o/other"
    assert store_.prs == [_pr(SECOND, 6)] and refetch == [1]
    panel.deleteLater()


def _checkout(tmp_path: Path, name: str) -> str:
    where = tmp_path / name
    where.mkdir()
    subprocess.run(["git", "-C", str(where), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(where), "remote", "add", "origin",
                    f"git@github.com:o/{name}.git"], check=True)
    return str(where)


def test_adding_a_root_with_none_listed_keeps_the_default_one(app, tmp_path, monkeypatch):
    """With none listed the agents work in the default root, which an added one joins
    rather than replaces."""
    from PySide6.QtWidgets import QFileDialog

    from diplomat_app.settingsview import SettingsView
    from diplomat_app.store import Store

    monkeypatch.delenv("DIPLOMAT_REPO", raising=False)
    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    appconfig.set_repo_roots([])
    view = SettingsView(Store())
    other = _checkout(tmp_path, "other")
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a: other))
    view._add_root()
    assert appconfig.repo_roots() == [review.default_repo_path(), other]

    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a: review.default_repo_path()))
    appconfig.set_repo_roots([])
    view._add_root()
    assert review.repo_root_paths() == [review.default_repo_path()]
    view.deleteLater()


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


# ---- roots that do not resolve --------------------------------------------------


def test_the_first_root_is_the_primary_only_while_its_path_resolves():
    table = {"/src/r": "remote.origin.url https://github.com/o/r.git\n",
             "/src/other": "remote.origin.url https://github.com/o/other.git\n"}
    two = ["/src/r", "/src/other"]
    second_only = {"/src/r": "remote.origin.url https://bitbucket.org/o/r.git\n",
                   "/src/other": table["/src/other"]}

    assert localrepos.primary_resolves(two, table) and not localrepos.guessed(two, table)
    assert not localrepos.primary_resolves(two, second_only)
    assert not localrepos.guessed(two, second_only)
    assert not localrepos.primary_resolves(two, {}) and localrepos.guessed(two, {})
    first_only = {"/src/r": table["/src/r"]}
    assert localrepos.primary_resolves(two, first_only)
    assert not localrepos.guessed(two, first_only)
    # One path is the single root it always was.
    assert localrepos.primary_resolves(["/src/r"], {})
    assert not localrepos.guessed(["/src/r"], {})


def test_no_root_resolving_takes_nothing_onto_the_mesh(remotes):
    from diplomat_runtime import core

    cfg = core.config()
    remotes["/src/r"] = f"remote.origin.url https://github.com/{cfg['owner']}/{cfg['repo']}\n"
    assert review.mesh_checkout() == "/src/r" and not review.roots_guessed()
    remotes.clear()
    _reread()
    assert [r.path for r in review.repo_roots()] == ["/src/r"]
    assert review.roots_guessed() and not review.primary_resolved()
    assert review.mesh_checkout() is None


def test_the_monitors_stand_down_when_no_root_resolves(quiet, monkeypatch):
    """The one root left is the configured repo at the first path, which may check
    out another repo."""
    quiet.pr_autofix_enabled = True
    asked = _by_repo(monkeypatch, "fetch_snapshots", {"o/r": [_conflicting(PRIMARY, 5)]})
    monkeypatch.setattr(review, "roots_guessed", lambda: True)
    monkeypatch.setattr(quiet, "dispatch_agent", lambda *a, **k: pytest.fail("dispatched"))

    quiet._autofix_poll_once()

    assert asked == []
    assert quiet.autofix_poll_error == (
        "no repo root in Settings resolves to a GitHub or GitLab checkout")


def test_comms_waits_while_the_first_root_is_unresolved(store, remotes,  # noqa: F811
                                                        monkeypatch):
    """The core reads a bare #N as the first root's, which would be the next one's."""
    ticks: list[dict] = []
    monkeypatch.setattr("diplomat_app.store.comms.tick", lambda settings: (
        ticks.append(settings) or {"items": [], "pings": [], "notices": []}))
    monkeypatch.setattr("diplomat_app.store.comms.session_locked", lambda: None)
    first = remotes.pop("/src/r")

    plan = store._comms_poll_once()

    assert ticks == [] and plan["items"] == []
    assert plan["error"] == ("waiting for the first repo root, /src/r, to resolve to a "
                             "GitHub or GitLab checkout")
    remotes["/src/r"] = first
    _reread()
    assert store._comms_poll_once()["error"] is None
    assert [t["repos"] for t in ticks] == [["o/r", "o/other"]]


def test_one_roots_unreadable_issues_hold_no_other_roots_work(quiet, monkeypatch):
    """Nothing owed is read from the issues, and a repo gh cannot see would otherwise
    hold every root's queue for as long as it stays listed."""
    quiet.community_issues_enabled = True
    _by_repo(monkeypatch, "fetch_snapshots", {"o/r": [_conflicting(PRIMARY, 5)]})

    def issues(root, *a, **k):
        if root.repo == "other":
            raise RuntimeError("Could not resolve to a Repository")
        return []

    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_open_issues", issues)
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(activity, "log", lambda kind, event, text, *a, **k: (
        logged.append((event, text))))

    quiet._autofix_poll_once()
    quiet._autofix_poll_once()

    assert quiet.autofix_poll_error is None
    assert [t.id for t in quiet.queued_tasks] == ["conflicts:o/r#5"]
    assert [e for e in logged if e[0].startswith("root-read-")] == [
        ("root-read-failed", "Issues of o/other unreadable, its work waits: "
                             "Could not resolve to a Repository")]

    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_open_issues", lambda *a, **k: [])
    quiet._autofix_poll_once()
    assert logged[-1] == ("root-read-recovered", "Issues of o/other readable again")


def _unreadable(monkeypatch, name: str, table: dict, bad: str = "o/other") -> None:
    """``autofixmonitor.<name>`` answering from ``table``, and failing for ``bad``."""
    def fetch(root, *a, **k):
        if root.slug == bad:
            raise RuntimeError("The listed users and repositories cannot be searched")
        return table.get(root.slug, [])

    monkeypatch.setattr(f"diplomat_app.autofixmonitor.{name}", fetch)


def test_one_roots_unreadable_prs_hold_no_other_roots_work(quiet, monkeypatch):
    """The unread root's rows, fingerprints and records stand untouched, as one that
    failed to resolve: nothing this cycle speaks for them."""
    _by_repo(monkeypatch, "fetch_snapshots", {
        "o/r": [_conflicting(PRIMARY, 5)], "o/other": [_conflicting(SECOND, 6)]})
    quiet._autofix_poll_once()
    assert sorted(t.id for t in quiet.queued_tasks) == [
        "conflicts:o/other#6", "conflicts:o/r#5"]
    quiet._save_attempts("myConflictAttempts", {"o/other#6": autofix.ReviewAttempt(
        autofix.STAMP_CONFLICTING, time.time(), 1)})
    quiet._save_attempts("myReviewAttempts", {"o/other#6": autofix.ReviewAttempt(
        autofix.STAMP_UNRESOLVED_REVIEW, time.time(), 1)})
    fingerprints = quiet._load_fingerprints()
    assert set(fingerprints) == {"o/r#5", "o/other#6"}
    _unreadable(monkeypatch, "fetch_snapshots", {"o/r": [_conflicting(PRIMARY, 7)]})
    asked = _by_repo(monkeypatch, "fetch_review_requests", {})
    quiet.community_issues_enabled = True
    issues_asked = _by_repo(monkeypatch, "fetch_open_issues", {})
    logged: list[tuple[str, str]] = []
    monkeypatch.setattr(activity, "log", lambda kind, event, text, *a, **k: (
        logged.append((event, text))))
    quiet.pr_autofix_enabled = True
    started: list[str] = []
    monkeypatch.setattr(quiet, "dispatch_agent", lambda job, source, attempt=1: (
        started.append(job.item) or "spawned"))

    quiet._autofix_poll_once()
    quiet._autofix_poll_once()

    assert quiet.autofix_poll_error is None
    assert "o/other#6" not in started and "o/r#7" in started
    assert "conflicts:o/other#6" in [t.id for t in quiet.queued_tasks]
    assert set(quiet._load_attempts("myConflictAttempts")) >= {"o/other#6"}
    assert set(quiet._load_attempts("myReviewAttempts")) == {"o/other#6"}
    assert asked == ["o/r", "o/r"]
    assert issues_asked == ["o/r", "o/r"]
    assert quiet._load_fingerprints()["o/other#6"] == fingerprints["o/other#6"]
    assert [e for e in logged if e[0].startswith("root-read-")] == [
        ("root-read-failed", "PRs of o/other unreadable, its work waits: "
                             "The listed users and repositories cannot be searched")]


def test_a_read_failing_in_every_root_fails_the_poll(quiet, monkeypatch):
    """The other reads are still asked of every root: one that answers is no less true
    for it."""
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: (
        _ for _ in ()).throw(RuntimeError("gh: network down")))
    asked = _by_repo(monkeypatch, "fetch_review_requests", {})
    quiet._autofix_poll_once()
    assert quiet.autofix_poll_error == "gh: network down"
    assert asked == ["o/r", "o/other"]


def test_one_roots_unreadable_review_requests_keep_its_records(quiet, monkeypatch):
    quiet.review_requests_enabled = True
    dispatched: list = []
    monkeypatch.setattr(quiet, "_dispatch_review_request",
                        lambda r, attempt: dispatched.append(r.item) or True)
    long_ago = time.time() - autofix.RETRY_MAX_BACKOFF - 60
    quiet._save_attempts("reviewReqAttempts", {"o/other#7": autofix.ReviewAttempt(
        "2026-01-02", long_ago, 1, concluded_at=long_ago)})
    _unreadable(monkeypatch, "fetch_review_requests", {"o/r": [_requested(PRIMARY, 8)]})

    quiet._autofix_poll_once()

    assert quiet.autofix_poll_error is None and dispatched == ["o/r#8"]
    assert "o/other#7" in quiet._load_attempts("reviewReqAttempts")
    assert quiet._held_slugs == {"o/other"}
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_review_requests",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    quiet._autofix_poll_once()
    assert quiet.autofix_poll_error == "down"


@pytest.mark.parametrize("wizard", ["review", "issues"])
def test_a_link_into_the_configured_repo_is_its_whatever_root_is_picked(
        wizard_store, dispatched, monkeypatch, wizard):
    """The configured repo's link parses as a bare number, which means the home root."""
    from diplomat_app import issues
    from diplomat_app.issuewizardview import IssueWizardView
    from diplomat_runtime import core

    cfg = core.config()
    conf = LocalRepo(cfg["owner"], cfg["repo"], "/src/conf")
    monkeypatch.setattr(review, "repo_roots", lambda: [conf, SECOND])
    wizard_store.select_repo("o/other")
    if wizard == "review":
        url = f"https://github.com/{conf.slug}/pull/9"
        w = _review_wizard(wizard_store, url)
    else:
        url = f"https://github.com/{conf.slug}/issues/9"
        w = IssueWizardView(wizard_store)
        w.target.setCurrentIndex(w.target.findData(issues.Target.SPECIFIC))
        w.specific_issue.setText(url)
    w._spawn()
    [job] = dispatched
    assert job.checkout in (None, "/src/conf")
    assert conf.repo in job.label and "other" not in job.label


def test_an_audit_prompt_names_its_home_root():
    from diplomat_app.audit import AuditConfig
    from diplomat_runtime import core

    cfg = core.config()
    prompt = AuditConfig(home=SECOND).build_prompt()
    assert "o/other" in prompt and f"{cfg['owner']}/{cfg['repo']}" not in prompt


def test_a_held_root_is_known_however_its_slug_is_cased(quiet, monkeypatch):
    """A root's slug comes cased as its remote spells it; item keys are lowercase. And
    a cycle missing its read keeps every record of a repo it did not read."""
    mixed = LocalRepo("O", "Other", "/src/other")
    monkeypatch.setattr(review, "repo_roots", lambda: [PRIMARY, mixed])
    _unreadable(monkeypatch, "fetch_snapshots", {}, bad="O/Other")
    seen: list[bool] = []
    monkeypatch.setattr(quiet, "_poll_review_requests",
                        lambda roots, complete=True: seen.append(complete))
    quiet._autofix_poll_once()
    assert quiet._held("o/other#6") and seen == [False]


def _starts(st, monkeypatch) -> list[str]:
    started: list[str] = []
    monkeypatch.setattr(st, "dispatch_agent", lambda job, source, attempt=1, **kw: (
        started.append(job.label) or "spawned"))
    return started


def test_the_drain_starts_a_mixed_case_roots_tasks_and_an_investigation(quiet, monkeypatch):
    """A root's slug keeps its remote's case; queue keys are lowercase. An investigation's
    key names no item, so no root is outside it."""
    monkeypatch.setattr(review, "repo_roots", lambda: [PRIMARY, LocalRepo("O", "Other", "/src/other")])
    quiet.queue_auto_run = False
    quiet.review_requests_enabled = True
    _by_repo(monkeypatch, "fetch_review_requests", {"O/Other": [_requested(SECOND, 7)]})
    quiet.requested_work = [autofix.RequestedWork(
        number=1, url="", author="", config={"kind": "investigate"}, repo="o/r")]
    quiet._autofix_poll_once()
    assert sorted(t.id for t in quiet.queued_tasks) == ["investigate:1", "review-req:o/other#7"]
    quiet.queue_auto_run = True
    started = _starts(quiet, monkeypatch)

    quiet._drain_queued_tasks([], set())

    assert len(started) == 2


def test_a_review_request_waits_while_its_roots_requests_stay_unreadable(quiet, monkeypatch):
    """Its row stands on a failed read; until a read speaks for it again, nothing says
    the request is still open."""
    quiet.queue_auto_run = False
    quiet.review_requests_enabled = True
    _by_repo(monkeypatch, "fetch_review_requests", {"o/other": [_requested(SECOND, 7)]})
    quiet._autofix_poll_once()
    _unreadable(monkeypatch, "fetch_review_requests", {})
    quiet._autofix_poll_once()
    quiet.queue_auto_run = True
    started = _starts(quiet, monkeypatch)

    quiet._autofix_poll_once()
    assert started == [] and [t.id for t in quiet.queued_tasks] == ["review-req:o/other#7"]

    _by_repo(monkeypatch, "fetch_review_requests", {"o/other": [_requested(SECOND, 7)]})
    quiet._autofix_poll_once()
    assert len(started) == 1


def test_a_mixed_case_roots_failed_review_request_read_holds_it(quiet, monkeypatch):
    monkeypatch.setattr(review, "repo_roots", lambda: [PRIMARY, LocalRepo("O", "Other", "/src/other")])
    _unreadable(monkeypatch, "fetch_review_requests", {}, bad="O/Other")
    quiet._autofix_poll_once()
    assert quiet._held("o/other#7")


def test_a_community_issue_is_named_among_every_root_when_one_is_unread(quiet, monkeypatch):
    quiet.community_issues_enabled = True
    _by_repo(monkeypatch, "fetch_open_issues", {})
    quiet._poll_community_issues([PRIMARY, SECOND])
    _by_repo(monkeypatch, "fetch_open_issues", {
        "o/r": [_issue(PRIMARY, 12, NOW + timedelta(hours=1))]})
    logged: list[str] = []
    monkeypatch.setattr(activity, "log", lambda kind, event, text, *a, **k: logged.append(text))

    quiet._poll_community_issues([PRIMARY], complete=False)

    assert any(t.startswith("Issue r#12 by @u12") for t in logged), logged


def test_a_root_taken_out_and_added_back_logs_its_failed_read_again(quiet, monkeypatch):
    quiet.community_issues_enabled = True
    _by_repo(monkeypatch, "fetch_snapshots", {})
    _unreadable(monkeypatch, "fetch_open_issues", {})
    logged: list[str] = []
    monkeypatch.setattr(activity, "log", lambda kind, event, text, *a, **k: (
        logged.append(event)))
    quiet._autofix_poll_once()
    monkeypatch.setattr(review, "repo_roots", lambda: [PRIMARY])
    quiet._autofix_poll_once()
    monkeypatch.setattr(review, "repo_roots", lambda: [PRIMARY, SECOND])
    quiet._autofix_poll_once()
    assert logged.count("root-read-failed") == 2


def test_the_drain_starts_no_investigation_asked_in_a_root_just_taken_out(quiet, monkeypatch):
    quiet.requested_work = [autofix.RequestedWork(
        number=1, url="", author="", config={"kind": "investigate", "checkout": "/src/other"},
        repo="o/other")]
    quiet.queue_auto_run = False
    quiet._autofix_poll_once()
    assert [t.id for t in quiet.queued_tasks] == ["investigate:1"]
    monkeypatch.setattr(review, "repo_roots", lambda: [PRIMARY])
    quiet.queue_auto_run = True
    started = _starts(quiet, monkeypatch)

    quiet._drain_queued_tasks([], set())

    assert started == []


def test_a_guessed_root_is_shown_as_unresolved(app, tmp_path, monkeypatch):
    """With several paths and none resolving, the first stands in for the configured
    repo; its row must not read as a checkout of it."""
    from PySide6.QtWidgets import QLabel

    from diplomat_app.settingsview import SettingsView
    from diplomat_app.store import Store

    monkeypatch.delenv("DIPLOMAT_REPO", raising=False)
    monkeypatch.setattr(Store, "refresh_allocator_install_async", lambda self: None)
    monkeypatch.setattr(Store, "refresh_update_status_async", lambda self: None)
    first, second = tmp_path / "a", tmp_path / "b"
    for where in (first, second):
        where.mkdir()
        subprocess.run(["git", "-C", str(where), "init", "-q"], check=True)
    appconfig.set_repo_roots([str(first), str(second)])
    assert review.roots_guessed()
    view = SettingsView(Store())

    texts = [w.text() for w in view._roots_list.itemAt(0).widget().findChildren(QLabel)]
    assert any("No GitHub or GitLab remote" in t for t in texts), texts
    view.deleteLater()
