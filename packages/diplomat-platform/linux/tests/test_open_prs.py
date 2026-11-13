"""The panel's open PRs are read from prs.graphql a page at a time, each page asked for
after the cursor the one before it ended on (mirrors the Swift smoke's paging check)."""

from __future__ import annotations

from diplomat_runtime import gh
from diplomat_runtime.models import API
from diplomat_runtime.prref import LocalRepo

ROOT = LocalRepo("o", "r", "/src/r")


def _pr(number):
    return {"number": number, "title": "t", "url": "u", "isDraft": False,
            "createdAt": "2026-01-01T00:00:00Z", "reviewDecision": None,
            "mergeable": "MERGEABLE", "author": {"login": "a"}, "files": {"nodes": []},
            "timelineItems": {"nodes": []}, "reviewThreads": {"nodes": []}}


def _pages(monkeypatch, pages):
    """Stub gh.graphql with `pages` (cursor -> (number, next cursor or None)); returns
    the log of what each call asked for."""
    calls = []

    def graphql(name, *, repo=None, variables=None):
        after = (variables or {}).get("after")
        calls.append((name, repo, after))
        number, nxt = pages[after]
        info = {"hasNextPage": nxt is not None, "endCursor": nxt or "end"}
        return {"data": {"repository": {"pullRequests": {
            "pageInfo": info, "nodes": [_pr(number)]}}}}

    monkeypatch.setattr(gh, "graphql", graphql)
    return calls


def test_four_pages_are_read_in_order_and_no_fifth(monkeypatch):
    calls = _pages(monkeypatch, {None: (1, "c1"), "c1": (2, "c2"), "c2": (3, "c3"),
                                 "c3": (4, "c4"), "c4": (5, "c5")})
    assert [p.number for p in API.fetch_open_prs(ROOT)] == [1, 2, 3, 4]
    assert calls == [("prs", ("o", "r"), None), ("prs", ("o", "r"), "c1"),
                     ("prs", ("o", "r"), "c2"), ("prs", ("o", "r"), "c3")]


def test_the_last_page_ends_the_reading(monkeypatch):
    calls = _pages(monkeypatch, {None: (1, "c1"), "c1": (2, None)})
    assert [p.number for p in API.fetch_open_prs(ROOT)] == [1, 2]
    assert [after for _, _, after in calls] == [None, "c1"]
