"""GitHub reads for the PR auto-fix monitor — the Linux port of AutofixMonitor.swift.

Two GraphQL searches over the `gh` CLI, decoded into the pure types in
:mod:`autofix`. The queries themselves are the shared ones in ``core/graphql/``
(single source of truth with the macOS monitor), driven by a search ``$q``
qualifier rather than the owner/name form that :func:`gh.graphql` wraps — so the
requests are issued directly here.
"""

from __future__ import annotations

import json

from . import core, gh
from .autofix import PRSnapshot, ReviewRequest


def _owed_thread(thread: dict, me: str) -> bool:
    """A review thread I still owe a reply on (mirrors ThreadTriage.owed): unresolved,
    resolvable by me, and whose most-recent comment isn't mine."""
    if thread.get("isResolved"):
        return False
    vcr = thread.get("viewerCanResolve")
    if vcr is None:
        vcr = True
    if not vcr:
        return False
    comments = (thread.get("comments") or {}).get("nodes") or []
    last = comments[-1] if comments else None
    login = ((last or {}).get("author") or {}).get("login") if last else None
    return (login or "").lower() != me.lower()


def _parse_snapshots(env: dict, me: str) -> list[PRSnapshot]:
    nodes = ((env.get("data") or {}).get("search") or {}).get("nodes") or []
    out: list[PRSnapshot] = []
    for n in nodes:
        if not n:
            continue
        number = n.get("number")
        if number is None:  # non-PR search node
            continue
        threads = (n.get("reviewThreads") or {}).get("nodes") or []
        unresolved = sum(1 for t in threads if t and not t.get("isResolved"))
        i_owe = sum(1 for t in threads if t and _owed_thread(t, me))
        out.append(
            PRSnapshot(
                number=number,
                title=n.get("title") or "",
                url=n.get("url") or "",
                is_draft=bool(n.get("isDraft")),
                mergeable=n.get("mergeable") or "UNKNOWN",
                review_decision=n.get("reviewDecision") or "",
                threads_unresolved=unresolved,
                threads_i_owe=i_owe,
            )
        )
    return out


def fetch_snapshots(owner: str, repo: str, me: str) -> list[PRSnapshot]:
    """One GraphQL search over my open, authored PRs (``core/graphql/monitor-prs``)."""
    q = f"repo:{owner}/{repo} author:{me} is:pr is:open"
    query = core.read_graphql("monitor-prs")
    data = gh.run(["api", "graphql", "-f", f"query={query}", "-f", f"q={q}"])
    return _parse_snapshots(json.loads(data), me)


def _parse_review_requests(env: dict, me: str) -> list[ReviewRequest]:
    nodes = ((env.get("data") or {}).get("search") or {}).get("nodes") or []
    lower = me.lower()
    out: list[ReviewRequest] = []
    for n in nodes:
        if not n:
            continue
        number = n.get("number")
        if number is None:
            continue
        # Latest "review requested from me" event.
        req_times = [
            ev.get("createdAt")
            for ev in ((n.get("timelineItems") or {}).get("nodes") or [])
            if ev
            and ((ev.get("requestedReviewer") or {}).get("login") or "").lower() == lower
            and ev.get("createdAt")
        ]
        # My latest review submission on this PR.
        my_times = [
            rv.get("submittedAt")
            for rv in ((n.get("reviews") or {}).get("nodes") or [])
            if rv
            and ((rv.get("author") or {}).get("login") or "").lower() == lower
            and rv.get("submittedAt")
        ]
        out.append(
            ReviewRequest(
                number=number,
                title=n.get("title") or "",
                url=n.get("url") or "",
                author=((n.get("author") or {}).get("login")) or "",
                author_association=n.get("authorAssociation") or "NONE",
                files=[
                    f.get("path")
                    for f in ((n.get("files") or {}).get("nodes") or [])
                    if f and f.get("path")
                ],
                requested_at=max(req_times) if req_times else None,
                my_last_review_at=max(my_times) if my_times else None,
            )
        )
    return out


def fetch_review_requests(
    owner: str, repo: str, me: str, include_files: bool = False
) -> list[ReviewRequest]:
    """PRs that request MY review, with request/last-review timestamps
    (``core/graphql/review-requests``). ``include_files`` pulls each PR's changed
    paths for the verdict-withhold gate — a big chunk of the rate-limit cost, so
    the caller passes ``False`` unless auto-approvals are on."""
    q = f"repo:{owner}/{repo} review-requested:{me} is:pr is:open"
    query = core.read_graphql("review-requests")
    data = gh.run(
        [
            "api",
            "graphql",
            "-f",
            f"query={query}",
            "-f",
            f"q={q}",
            "-F",
            f"withFiles={'true' if include_files else 'false'}",
        ]
    )
    return _parse_review_requests(json.loads(data), me)
