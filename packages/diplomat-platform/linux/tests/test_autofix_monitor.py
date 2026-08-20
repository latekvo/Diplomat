"""The two GitHub reads on the monitor's poll path.

They are searches, so they carry a ``$q`` qualifier rather than the owner/name pair
:func:`gh.graphql` fills in itself — but they are also the heaviest and hottest
queries the applet issues, and a single failed one costs the whole poll cycle. What
is pinned here is that they still go through the shared retrying entry point, and
that the search qualifier and the ``Boolean!`` flag reach gh in the spelling each
needs.
"""

from __future__ import annotations

import json

import pytest

from diplomat_app import autofixmonitor
from diplomat_runtime import gh

SNAPSHOTS = json.dumps({"data": {"search": {"nodes": [{
    "number": 7,
    "title": "Seven",
    "url": "https://github.com/o/r/pull/7",
    "isDraft": False,
    "mergeable": "CONFLICTING",
    "reviewDecision": "",
    "reviewThreads": {"nodes": []},
    "headRefOid": "abc123",
}]}}}).encode()

REVIEW_REQUESTS = json.dumps({"data": {"search": {"nodes": [{
    "number": 9,
    "title": "Nine",
    "url": "https://github.com/o/r/pull/9",
    "author": {"login": "bob"},
    "authorAssociation": "CONTRIBUTOR",
    "headRefOid": "def456",
    "timelineItems": {"nodes": []},
    "reviews": {"nodes": []},
    "comments": {"nodes": []},
    "files": {"nodes": []},
}]}}}).encode()


@pytest.fixture(autouse=True)
def instant_backoff(monkeypatch):
    """The retry's 0.8s wait, spent for nothing in a test that never reaches GitHub."""
    monkeypatch.setattr(gh.time, "sleep", lambda _: None)


def _flaky(monkeypatch, payload: bytes, failures: int):
    """Stub gh so the first ``failures`` calls blow up the way a network hole does."""
    calls = []

    def fake_run(args, timeout=60.0):
        calls.append(args)
        if len(calls) <= failures:
            raise gh.GHError(
                'gh exited 1: Post "https://api.github.com/graphql": '
                "net/http: TLS handshake timeout"
            )
        return payload

    monkeypatch.setattr(gh, "run", fake_run)
    return calls


# MARK: - A blip costs an attempt, not the cycle


def test_snapshot_fetch_survives_a_transient_failure(monkeypatch):
    calls = _flaky(monkeypatch, SNAPSHOTS, failures=1)
    snaps = autofixmonitor.fetch_snapshots("o", "r", "alice")
    assert [s.number for s in snaps] == [7]
    assert snaps[0].mergeable == "CONFLICTING"
    assert len(calls) == 2


def test_review_request_fetch_survives_a_transient_failure(monkeypatch):
    calls = _flaky(monkeypatch, REVIEW_REQUESTS, failures=1)
    reqs = autofixmonitor.fetch_review_requests("o", "r", "alice")
    assert [r.number for r in reqs] == [9]
    assert reqs[0].author == "bob"
    assert len(calls) == 2


@pytest.mark.parametrize(
    "fetch, payload",
    [
        (autofixmonitor.fetch_snapshots, SNAPSHOTS),
        (autofixmonitor.fetch_review_requests, REVIEW_REQUESTS),
    ],
)
def test_a_failure_that_outlasts_the_retry_still_reaches_the_poll(
    monkeypatch, fetch, payload
):
    """The retry absorbs a blip, never an outage: the poll has to hear about the
    second failure or it would treat an empty answer as "no work"."""
    calls = _flaky(monkeypatch, payload, failures=2)
    with pytest.raises(gh.GHError, match="TLS handshake timeout"):
        fetch("o", "r", "alice")
    assert len(calls) == 2


# MARK: - What reaches gh


def test_snapshot_fetch_searches_for_my_open_prs(monkeypatch):
    calls = _flaky(monkeypatch, SNAPSHOTS, failures=0)
    autofixmonitor.fetch_snapshots("software-mansion", "argent", "latekvo")
    args = calls[0]
    assert args[:3] == ["api", "graphql", "-f"]
    assert "-f" in args and "q=repo:software-mansion/argent author:latekvo is:pr is:open" in args
    # A search takes its repo through $q; owner/name would be an undeclared variable.
    assert not any(a.startswith(("owner=", "name=")) for a in args)


@pytest.mark.parametrize("include_files, expected", [(True, "true"), (False, "false")])
def test_review_request_fetch_types_the_with_files_flag(
    monkeypatch, include_files, expected
):
    """``$withFiles`` is a ``Boolean!``, so it goes through ``-F`` (gh parses the
    value) — sent through ``-f`` it would arrive as the string "true"."""
    calls = _flaky(monkeypatch, REVIEW_REQUESTS, failures=0)
    autofixmonitor.fetch_review_requests(
        "software-mansion", "argent", "latekvo", include_files=include_files
    )
    args = calls[0]
    assert args[args.index(f"withFiles={expected}") - 1] == "-F"
    assert "q=repo:software-mansion/argent review-requested:latekvo is:pr is:open" in args
