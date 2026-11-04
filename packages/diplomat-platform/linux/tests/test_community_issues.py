"""The new-community-issue monitor: each issue filed from outside the org after the
monitor's mark is queued once, as the same reproduce-and-fix ask a Fix-issues sweep of
the community leaves.

The store fixture and the evidence stub come from ``test_autofix``; the poll stubs
mirror ``test_requested_work``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from test_autofix import _spawn_recorder, fake_probes, store  # noqa: F401 — fixture

from diplomat_runtime import activity
from diplomat_runtime.models import Filters, OpenIssue

pytest.importorskip("PySide6")

NOW = datetime.now(timezone.utc)

#: Agents this applet has no record of, filling every bay of the default cap, so a
#: poll's drain starts nothing and the queue holds what the monitor put there.
CAP_FULL = {90, 91}


def _filed(number: int, assoc: str = "NONE", *, minutes: float = 1, author: str = "",
           assignees=(), member_responded: bool = False) -> OpenIssue:
    at = NOW + timedelta(minutes=minutes)
    return OpenIssue(number, f"Issue {number}", f"https://github.com/o/r/issues/{number}",
                     author or f"u{number}", assoc, at, at, 0, list(assignees), [],
                     member_responded)


# ---- the scan ---------------------------------------------------------------

ISSUES = [_filed(405, minutes=9), _filed(401, minutes=-5),
          _filed(402, "CONTRIBUTOR", minutes=2), _filed(403, "MEMBER", minutes=4),
          _filed(404, minutes=6, assignees=["dev"]),
          _filed(406, "COLLABORATOR", minutes=7, member_responded=True),
          _filed(407, "COLLABORATOR", minutes=8)]


def test_the_first_scan_queues_none_of_the_backlog():
    fresh, mark = Filters.new_community_issues(ISSUES, None, NOW)
    assert (fresh, mark) == ([], NOW + timedelta(minutes=9))
    assert Filters.new_community_issues([], None, NOW) == ([], NOW)


def test_a_scan_takes_unanswered_outsiders_issues_after_the_mark_oldest_first():
    fresh, mark = Filters.new_community_issues(ISSUES, NOW, NOW)
    assert [i.number for i in fresh] == [402, 407, 405]
    assert mark == NOW + timedelta(minutes=9)
    assert Filters.new_community_issues(ISSUES, mark, NOW)[0] == []


def test_the_mark_never_moves_backwards():
    late = NOW + timedelta(hours=1)
    assert Filters.new_community_issues(ISSUES, late, NOW) == ([], late)


# ---- the monitor ------------------------------------------------------------


@pytest.fixture
def watched(store, monkeypatch):  # noqa: F811 — the shared Store fixture
    """The store, the cap full, and a poll whose issue read answers ``found``.

    ``found`` starts with a member's issue at ``NOW``, so the first scan's mark is
    fixture time rather than the wall clock, which has moved on since import."""
    fake_probes(monkeypatch, live_prs=CAP_FULL)
    found: list[OpenIssue] = [_filed(1, "MEMBER", minutes=0)]
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_review_requests",
                        lambda *a, **k: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_open_issues", lambda: found)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    store.found = found
    return store


def _feed(action: str) -> list[str]:
    return [e.detail for e in activity.read() if e.action == action]


def test_an_issue_filed_after_the_first_poll_is_queued_as_a_fix(watched):
    watched._autofix_poll_once()
    assert watched.requested_work == []

    watched.found.append(_filed(7, author="carol"))
    watched._autofix_poll_once()

    [ask] = watched.requested_work
    assert (ask.key, ask.author, ask.url) == ("issues:7", "carol",
                                              "https://github.com/o/r/issues/7")
    assert ask.config["target"] == "contributors"
    assert [t.id for t in watched.queued_tasks] == ["issues:7"]
    assert watched.queued_tasks[0].job.prompt == "PROMPT:issues:7"
    assert _feed("community-issue") == ["Issue #7 by @carol - fix queued"]


def test_an_issue_is_queued_once_even_after_its_ask_is_gone(watched):
    watched._autofix_poll_once()
    watched.found[:] = [_filed(7)]
    watched._autofix_poll_once()
    watched.cancel_requested_work("issues:7")

    watched._autofix_poll_once()

    assert watched.requested_work == []
    assert len(_feed("community-issue")) == 1


def test_switched_off_it_queues_nothing_and_restarts_from_the_next_scan(watched):
    watched._autofix_poll_once()
    watched.community_issues_enabled = False
    watched._autofix_poll_once()
    watched.found[:] = [_filed(7)]
    watched._autofix_poll_once()
    assert watched.requested_work == []

    watched.community_issues_enabled = True
    watched._autofix_poll_once()
    assert watched.requested_work == []

    watched.found.append(_filed(8, minutes=2))
    watched._autofix_poll_once()
    assert [a.key for a in watched.requested_work] == ["issues:8"]


def test_a_banned_authors_issue_is_not_queued(watched, monkeypatch):
    from diplomat_app import bans

    watched._autofix_poll_once()
    monkeypatch.setattr("diplomat_app.bans.read",
                        lambda: [bans.BannedAuthor("mallory")])
    watched.found[:] = [_filed(7, author="mallory")]
    watched._autofix_poll_once()

    assert watched.requested_work == []
    assert _feed("ban-skip") == ["Issue #7 by @mallory - author is banned, not queued"]


def test_a_failed_read_fails_the_poll_and_keeps_the_mark(watched, monkeypatch):
    watched._autofix_poll_once()

    def down():
        raise RuntimeError("gh: network down")

    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_open_issues", down)
    watched._autofix_poll_once()
    assert "network down" in (watched.autofix_poll_error or "")

    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_open_issues",
                        lambda: watched.found)
    watched.found[:] = [_filed(7)]
    watched._autofix_poll_once()
    assert [a.key for a in watched.requested_work] == ["issues:7"]


def test_an_issue_a_sweep_already_queued_is_not_asked_for_twice(watched):
    watched._autofix_poll_once()
    issue = _filed(7)
    watched.issues = [issue]
    watched.request_issue_sweep(_contributors_sweep(watched))
    watched.found[:] = [issue]

    watched._autofix_poll_once()

    assert [a.key for a in watched.requested_work] == ["issues:7"]
    assert _feed("community-issue") == []


def _contributors_sweep(store):  # noqa: F811
    from diplomat_app import issues

    return issues.IssueConfig(target=issues.Target.CONTRIBUTORS, me=store.me)


def test_an_unreadable_mark_restarts_the_baseline_instead_of_failing_polls(watched):
    watched._settings.setValue("communityIssuesSince", "not a date")
    watched.found.append(_filed(7))

    watched._autofix_poll_once()

    assert watched.autofix_poll_error is None
    assert watched.requested_work == []
    watched.found.append(_filed(8, minutes=2))
    watched._autofix_poll_once()
    assert [a.key for a in watched.requested_work] == ["issues:8"]
