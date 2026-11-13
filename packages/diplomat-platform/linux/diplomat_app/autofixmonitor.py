"""GitHub and GitLab reads for the PR auto-fix monitor — the Linux port of
AutofixMonitor.swift.

Three GraphQL searches over the `gh` CLI, decoded into the pure types in
:mod:`autofix`. The queries themselves are the shared ones in ``assets/graphql/``
(single source of truth with the macOS monitor), driven by a search ``$q``
qualifier rather than the owner/name form :func:`gh.graphql` fills in itself. A GitLab
root asks their ``gitlab-*`` twins through ``glab``, which :mod:`gitlab` adapts into the
GitHub shape.

They are the heaviest requests the applet makes and the only two on the poll's hot
path, where either one failing costs the cycle: the queue drain stands down and
nothing is committed until a cycle succeeds. So they go through
:func:`gh.graphql` for its retry rather than issuing the request raw.
"""

from __future__ import annotations

import json
import urllib.parse

from diplomat_runtime import core, gh, gitlab, glab
from diplomat_runtime.autofix import PRSnapshot, ReviewRequest
from diplomat_runtime.models import API, OpenIssue, is_unanswered_run_finding, thread_owed
from diplomat_runtime.prref import LocalRepo, item_key


def _owed_thread(thread: dict, me: str) -> bool:
    """A review thread I still owe a reply on (:func:`models.thread_owed`)."""
    comments = (thread.get("comments") or {}).get("nodes") or []
    last = comments[-1] if comments else None
    login = ((last or {}).get("author") or {}).get("login") if last else None
    return thread_owed(bool(thread.get("isResolved")), thread.get("viewerCanResolve"), login, me,
                       is_unanswered_run_finding(thread))


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
                head_sha=n.get("headRefOid") or "",
            )
        )
    return out


def _recheck(root: LocalRepo, number: int) -> str:
    """GitLab rechecks a merge status only when asked, and GraphQL never asks, so after
    a push to the target it reads unchecked. This REST read asks, and answers when it
    can; a failed read stays undecided."""
    project = urllib.parse.quote(root.slug, safe="")
    path = f"projects/{project}/merge_requests/{number}?with_merge_status_recheck=true"
    try:
        return gitlab.rest_mergeable(json.loads(
            glab.run(["api", "--hostname", root.host, path], timeout=30)))
    except Exception:  # noqa: BLE001 — never costs the root its read
        return "UNKNOWN"


def fetch_snapshots(root: LocalRepo, me: str) -> list[PRSnapshot]:
    """One GraphQL search over my open, authored PRs (``assets/graphql/monitor-prs``),
    and on GitLab a recheck of each one whose merge status is undecided."""
    if root.is_gitlab:
        env = gitlab.monitor_prs(glab.project("gitlab-monitor-prs", root, {"author": me}))
        for node in env["data"]["search"]["nodes"]:
            if node["mergeable"] == "UNKNOWN":
                node["mergeable"] = _recheck(root, node["number"])
    else:
        q = f"repo:{root.slug} author:{me} is:pr is:open"
        env = gh.graphql("monitor-prs", variables={"q": q})
    return _parse_snapshots(env, me)


def fetch_closed_prs(root: LocalRepo) -> set[str]:
    """The PRs (item keys) this repo has recently merged or closed — what the queue is
    re-checked against (``assets/graphql/closed-prs``).

    Not scoped to me, to my queue, or to a monitor's verb: any PR can be sitting in
    the queue (a sweep asks for whoever's PRs the operator aimed it at), and a search
    cannot be given a list of numbers to ask about. So it asks the cheap question the
    other way round — who closed lately — and the caller intersects.
    """
    if root.is_gitlab:
        env = gitlab.closed_prs(glab.project("gitlab-closed-prs", root))
    else:
        q = f"repo:{root.slug} is:pr is:closed sort:updated-desc"
        query = core.read_graphql("closed-prs")
        env = json.loads(gh.run(["api", "graphql", "-f", f"query={query}", "-f", f"q={q}"]))
    nodes = ((env.get("data") or {}).get("search") or {}).get("nodes") or []
    return {item_key(root.owner, root.repo, n["number"], root.host)
            for n in nodes if n and n.get("number") is not None}


def fetch_open_issues(root: LocalRepo) -> list[OpenIssue]:
    """The repo's newest open issues, the same read the panel lists them from, for
    the new-community-issue monitor (:meth:`Store._poll_community_issues`)."""
    return API.fetch_open_issues(root)


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
        # My latest top-level comment on this PR — it answers the request too.
        my_comment_times = [
            c.get("createdAt")
            for c in ((n.get("comments") or {}).get("nodes") or [])
            if c
            and ((c.get("author") or {}).get("login") or "").lower() == lower
            and c.get("createdAt")
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
                head_sha=n.get("headRefOid") or "",
                my_last_comment_at=max(my_comment_times) if my_comment_times else None,
            )
        )
    return out


def fetch_review_requests(
    root: LocalRepo, me: str, include_files: bool = False
) -> list[ReviewRequest]:
    """PRs that request MY review, with request/last-review timestamps
    (``assets/graphql/review-requests``). ``include_files`` pulls each PR's changed
    paths for the verdict-withhold gate — a big chunk of the rate-limit cost, so
    the caller passes ``False`` unless auto-approvals are on."""
    typed = {"withFiles": "true" if include_files else "false"}
    if root.is_gitlab:
        env = gitlab.review_requests(
            glab.project("gitlab-review-requests", root, {"reviewer": me}, typed), me,
            lambda ids: glab.members(root, ids))
    else:
        q = f"repo:{root.slug} review-requested:{me} is:pr is:open"
        env = gh.graphql("review-requests", variables={"q": q}, typed_variables=typed)
    return _parse_review_requests(env, me)
