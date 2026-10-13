"""The check a review run gets when it retires: did its review go through GitHub's
lifecycle (opened PENDING, filled, submitted once)? The pure judgement
(reviewaudit.findings) and the store wiring that runs it."""

from __future__ import annotations

import dataclasses

import pytest

from diplomat_runtime import activity, agentstate, gh, reviewaudit
from diplomat_runtime.agentstate import RunRecord

SINCE = 1_760_000_000.0  # 2025-10-09T08:53:20Z
BEFORE = "2025-10-09T08:00:00Z"
AFTER = "2025-10-09T09:30:00Z"
URL = "https://github.com/acme/widgets/pull/7"


def _review(state="COMMENTED", created=AFTER, submitted=AFTER, threads=0, replies=0):
    comments = [{"replyTo": None}] * threads + [{"replyTo": {"id": "c"}}] * replies
    return {"state": state, "createdAt": created,
            "submittedAt": None if state == "PENDING" else submitted,
            "comments": {"nodes": comments}}


def _pr(*reviews, author="bob"):
    return {"author": {"login": author}, "reviews": {"nodes": list(reviews)}}


# MARK: - findings


def test_a_pending_review_the_run_opened_is_reported():
    assert reviewaudit.findings(_pr(_review("PENDING", threads=2)), "alice", SINCE) == [
        "left its review PENDING — never submitted, so only I can see it"]


def test_a_pending_review_older_than_the_run_is_left_alone():
    """It may be my own draft in the browser, which no run is to blame for."""
    assert reviewaudit.findings(_pr(_review("PENDING", created=BEFORE)), "alice",
                                SINCE) == []


def test_findings_spread_over_several_reviews_are_reported():
    pr = _pr(_review(threads=1), _review(threads=1), _review(threads=1))
    assert reviewaudit.findings(pr, "alice", SINCE) == [
        "posted its findings as 3 separate reviews instead of one"]


def test_one_review_with_its_findings_inside_is_clean():
    """Replies posted outside it, and a body-only soft approval, open no thread."""
    pr = _pr(_review(threads=4, replies=1), _review(replies=1), _review())
    assert reviewaudit.findings(pr, "alice", SINCE) == []


def test_reviews_submitted_before_the_run_are_not_its_own():
    pr = _pr(_review(submitted=BEFORE, threads=1), _review(threads=1))
    assert reviewaudit.findings(pr, "alice", SINCE) == []


def test_a_split_on_my_own_pr_is_not_judged():
    """The prompt has a run on my own PR reply, not review, so it asks nothing of one."""
    pr = _pr(_review(threads=1), _review(threads=1), author="ALICE")
    assert reviewaudit.findings(pr, "alice", SINCE) == []


def test_both_findings_together():
    pr = _pr(_review("PENDING"), _review(threads=1), _review(threads=2))
    assert reviewaudit.findings(pr, "alice", SINCE) == [
        "left its review PENDING — never submitted, so only I can see it",
        "posted its findings as 2 separate reviews instead of one"]


def test_an_empty_or_missing_answer_finds_nothing():
    assert reviewaudit.findings({}, "alice", SINCE) == []
    assert reviewaudit.findings({"reviews": {"nodes": [None]}}, "alice", SINCE) == []


# MARK: - which PR is asked about


@pytest.mark.parametrize("url, want", [
    (URL, ("acme", "widgets", 7)),
    (URL + "/", ("acme", "widgets", 7)),
    ("https://github.com/acme/widgets/issues/7", None),
    ("https://ghe.example.com/acme/widgets/pull/7", None),
    ("https://github.com/acme/widgets/pull/7/files", None),
    ("", None),
])
def test_pr_coordinates(url, want):
    assert reviewaudit.pr_coordinates(url) == want


def test_audit_asks_for_my_reviews_on_that_pr(monkeypatch):
    seen = {}

    def graphql(name, **kw):
        seen.update(name=name, **kw)
        return {"data": {"repository": {"pullRequest": _pr(_review("PENDING"))}}}

    monkeypatch.setattr(gh, "graphql", graphql)
    assert reviewaudit.audit(URL, "alice", SINCE) == [
        "left its review PENDING — never submitted, so only I can see it"]
    assert seen == {"name": "my-reviews", "with_repo": False,
                    "variables": {"owner": "acme", "name": "widgets", "me": "alice"},
                    "typed_variables": {"number": "7"}}


def test_audit_asks_nothing_about_a_url_that_is_no_pr(monkeypatch):
    monkeypatch.setattr(gh, "graphql", lambda *a, **k: pytest.fail("asked GitHub"))
    assert reviewaudit.audit("https://github.com/acme/widgets/issues/7", "alice",
                             SINCE) == []
    assert reviewaudit.audit(URL, "", SINCE) == []


# MARK: - the store runs it on what retires


def _run(run_id, **kw):
    base = dict(run_id=run_id, dispatched_at=SINCE, pr_number=7, pr_url=URL,
                kind="review", label=f"Review-req · {run_id}")
    base.update(kw)
    return RunRecord(**base)


def _retire(monkeypatch, records, failed=(), me="alice", answer=None):
    from diplomat_app.store import Store

    asked = []

    def audit(url, who, since):
        asked.append((url, who, since))
        if isinstance(answer, Exception):
            raise answer
        return answer or []

    monkeypatch.setattr(reviewaudit, "audit", audit)
    store = Store()
    store.me = me
    monkeypatch.setattr(store, "start_background", lambda work, name=None: work())
    states = {r.run_id: agentstate.Resolution(
        r.run_id, agentstate.FAILED if r.run_id in failed else agentstate.FINISHED, "test")
        for r in records}
    store._retire_finished(agentstate.Tick(records=[], states=states, rows=[],
                                           cap_load=set(), retirable=list(records),
                                           reapable=[], free_slots=0))
    warns = [e.detail for e in activity.read() if e.action == "warn"]
    return asked, warns


def test_only_a_review_run_that_ran_here_is_checked(monkeypatch):
    records = [
        _run("ok"),
        _run("conflicts", kind="conflicts"),
        _run("peer", placement=agentstate.PLACEMENT_MESH_PEER),
        _run("no-pr", pr_url="", pr_number=None),
        _run("never-ran"),
        _run("mesh-here", placement=agentstate.PLACEMENT_MESH_HERE),
    ]
    asked, _ = _retire(monkeypatch, records, failed={"never-ran"})
    assert asked == [(URL, "alice", SINCE), (URL, "alice", SINCE)]


def test_what_it_finds_lands_in_the_feed_under_the_runs_label(monkeypatch):
    _, warns = _retire(monkeypatch, [_run("r1")], answer=["left it PENDING"])
    assert warns == ["Review-req · r1 left it PENDING"]


def test_a_check_that_could_not_run_says_so(monkeypatch):
    _, warns = _retire(monkeypatch, [_run("r1")], answer=gh.GHError("gh exited 1: boom"))
    assert warns == ["Review-req · r1 could not check its review: gh exited 1: boom"]


def test_no_login_says_it_could_not_check(monkeypatch):
    """A PENDING review is listed to its author alone, so without the login there is
    nobody to ask as — which the feed says rather than reading as a clean run."""
    asked, warns = _retire(monkeypatch, [_run("r1")], me="")
    assert asked == []
    assert warns == ["Review-req · r1 could not check its review: "
                     "the gh login is not known yet"]


def test_the_username_override_is_not_who_is_asked(monkeypatch):
    from diplomat_app.store import Store

    monkeypatch.setattr(Store, "username_override",
                        property(lambda self: "someone-else"))
    asked, _ = _retire(monkeypatch, [dataclasses.replace(_run("r1"))])
    assert [who for _, who, _ in asked] == ["alice"]
