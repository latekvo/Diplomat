"""GitLab GraphQL answers in the shape of their GitHub twins.

Each ``assets/graphql/gitlab-*.graphql`` answer is turned into the exact envelope the
GitHub query of the same job returns, so one parser reads both. Pure functions, twin
of ``GitLab.swift``; the shared fixtures in
``packages/diplomat-core/Tests/fixtures/gitlab`` pin the two to one output.

GitLab has no author association, so a project member reads as ``MEMBER`` and anyone
else as ``NONE``. It records a review request, an approval, a requested change and a
ready-for-review as system ("activity") notes only, so those are read off note bodies.
"""

from __future__ import annotations

import re

from .gh import GHError

#: ``detailedMergeStatus`` values GitLab answers while it has not decided yet.
_UNDECIDED = {"UNCHECKED", "CHECKING", "PREPARING"}
_READY = "marked this merge request as **ready**"
_REQUESTED = "requested review from "
_UNREQUESTED = " and removed review request"
_REVIEWED = ("approved this merge request", "requested changes")
_MENTION = re.compile(r"@([\w.-]+)")
_MR_STATES = {"opened": "OPEN", "locked": "OPEN", "closed": "CLOSED", "merged": "MERGED"}


def _project(env: dict) -> dict:
    return (env.get("data") or {}).get("project") or {}


def _nodes(conn: dict | None) -> list[dict]:
    return [n for n in ((conn or {}).get("nodes") or []) if n]


def _login(user: dict | None) -> dict | None:
    return {"login": user["username"]} if user else None


def _members(project: dict) -> set[str]:
    return {(m.get("user") or {}).get("username", "").lower()
            for m in _nodes(project.get("projectMembers"))} - {""}


def _association(user: dict | None, members: set[str]) -> str:
    return "MEMBER" if user and user["username"].lower() in members else "NONE"


def _mergeable(mr: dict) -> str:
    if mr.get("conflicts") is True:
        return "CONFLICTING"
    status = mr.get("detailedMergeStatus")
    return "UNKNOWN" if status is None or status in _UNDECIDED else "MERGEABLE"


def rest_mergeable(obj: dict) -> str:
    """A REST ``projects/:id/merge_requests/:iid`` answer's mergeable, mapped as the
    GraphQL one is."""
    status = obj.get("detailed_merge_status")
    return _mergeable({"conflicts": obj.get("has_conflicts"),
                       "detailedMergeStatus": status.upper() if status else None})


def _review_decision(mr: dict) -> str | None:
    reviewers = _nodes(mr.get("reviewers"))
    states = {((r.get("mergeRequestInteraction") or {}).get("reviewState")) for r in reviewers}
    if "REQUESTED_CHANGES" in states:
        return "CHANGES_REQUESTED"
    if mr.get("approved"):
        return "APPROVED"
    return "REVIEW_REQUIRED" if reviewers else None


def _threads(mr: dict) -> dict:
    """Resolvable discussions as GitHub review threads, which are asked
    ``comments(last: 1)``: the count of the notes people wrote, and the last of them.
    A system note (a push's "changed this line") is no comment, so it never answers
    the thread; whether I can resolve it is still read off its last note."""
    out = []
    for d in _nodes(mr.get("discussions")):
        if d.get("resolvable") is not True:
            continue
        notes = _nodes(d.get("notes"))
        said = [n for n in notes if n.get("system") is not True]
        last = said[-1] if said else None
        out.append({
            "isResolved": bool(d.get("resolved")),
            "viewerCanResolve": (notes[-1].get("userPermissions") or {}).get("resolveNote")
            if notes else None,
            "comments": {
                "totalCount": len(said),
                "nodes": [{"author": _login(last.get("author")), "body": last.get("body")}]
                if last else [],
            },
        })
    return {"nodes": out}


def _files(mr: dict) -> dict:
    return {"nodes": [{"path": f["path"]} for f in (mr.get("diffStats") or []) if f]}


def _ready_events(mr: dict) -> dict:
    ready = [n for n in _nodes(mr.get("notes")) if (n.get("body") or "").startswith(_READY)]
    return {"nodes": [{"__typename": "ReadyForReviewEvent", "createdAt": ready[-1]["createdAt"]}]
            if ready else []}


def prs(env: dict) -> dict:
    """``gitlab-prs`` as ``prs``."""
    project = _project(env)
    nodes = [{
        "number": int(mr["iid"]),
        "title": mr.get("title"),
        "url": mr.get("webUrl"),
        "isDraft": bool(mr.get("draft")),
        "createdAt": mr.get("createdAt"),
        "reviewDecision": _review_decision(mr),
        "mergeable": _mergeable(mr),
        "author": _login(mr.get("author")),
        "files": _files(mr),
        "timelineItems": _ready_events(mr),
        "reviewThreads": _threads(mr),
    } for mr in _nodes(project.get("mergeRequests"))]
    return {"data": {"repository": {"pullRequests": {"nodes": nodes}}}}


def monitor_prs(env: dict) -> dict:
    """``gitlab-monitor-prs`` as ``monitor-prs``."""
    nodes = [{
        "number": int(mr["iid"]),
        "title": mr.get("title"),
        "url": mr.get("webUrl"),
        "isDraft": bool(mr.get("draft")),
        "mergeable": _mergeable(mr),
        "reviewDecision": _review_decision(mr),
        "headRefOid": mr.get("diffHeadSha"),
        "reviewThreads": _threads(mr),
    } for mr in _nodes(_project(env).get("mergeRequests"))]
    return {"data": {"search": {"nodes": nodes}}}


def closed_prs(env: dict) -> dict:
    """``gitlab-closed-prs`` (merged and closed) as ``closed-prs``."""
    project = _project(env)
    nodes = [{"number": int(n["iid"])}
             for kind in ("merged", "closed") for n in _nodes(project.get(kind))]
    return {"data": {"search": {"nodes": nodes}}}


def _requests(notes: list[dict]) -> list[dict]:
    """One ``ReviewRequestedEvent`` per user a "requested review from" note names,
    leaving out the users the same note removed a request for."""
    out = []
    for n in notes:
        body = n.get("body") or ""
        if not body.startswith(_REQUESTED):
            continue
        added = body.split(_UNREQUESTED, 1)[0]
        out += [{"createdAt": n.get("createdAt"),
                 "requestedReviewer": {"__typename": "User", "login": login}}
                for login in _MENTION.findall(added)]
    return out


def _request_of(reviewer: str, events: list[dict], mr: dict, activity: list[dict]) -> list[dict]:
    """``reviewer``'s request when no note in the window names them: the query only
    answers merge requests that request their review, so it is at least as old as the
    window's oldest note, or the merge request when the window is empty."""
    if any(e["requestedReviewer"]["login"].lower() == reviewer.lower() for e in events):
        return []
    times = [n["createdAt"] for n in activity if n.get("createdAt")]
    return [{"createdAt": min(times) if times else mr.get("createdAt"),
             "requestedReviewer": {"__typename": "User", "login": reviewer}}]


def review_requests(env: dict, reviewer: str) -> dict:
    """``gitlab-review-requests`` asked for ``reviewer`` as ``review-requests``;
    ``files`` only when the query asked for ``diffStats``."""
    project = _project(env)
    members = _members(project)
    nodes = []
    for mr in _nodes(project.get("mergeRequests")):
        activity = _nodes(mr.get("activity"))
        events = _requests(activity)
        node = {
            "number": int(mr["iid"]),
            "title": mr.get("title"),
            "url": mr.get("webUrl"),
            "author": _login(mr.get("author")),
            "authorAssociation": _association(mr.get("author"), members),
            "headRefOid": mr.get("diffHeadSha"),
            "timelineItems": {"nodes": _request_of(reviewer, events, mr, activity) + events},
            "reviews": {"nodes": [
                {"author": _login(n.get("author")), "submittedAt": n.get("createdAt")}
                for n in activity if (n.get("body") or "").startswith(_REVIEWED)]},
            "comments": {"nodes": [
                {"author": _login(n.get("author")), "createdAt": n.get("createdAt")}
                for n in _nodes(mr.get("comments"))]},
        }
        if "diffStats" in mr:
            node["files"] = _files(mr)
        nodes.append(node)
    return {"data": {"search": {"nodes": nodes}}}


def issues(env: dict) -> dict:
    """``gitlab-issues`` as ``issues``."""
    project = _project(env)
    members = _members(project)
    nodes = [{
        "number": int(i["iid"]),
        "title": i.get("title"),
        "url": i.get("webUrl"),
        "createdAt": i.get("createdAt"),
        "updatedAt": i.get("updatedAt"),
        "authorAssociation": _association(i.get("author"), members),
        "author": _login(i.get("author")),
        "assignees": {"nodes": [{"login": a["username"]} for a in _nodes(i.get("assignees"))]},
        "labels": {"nodes": [{"name": label["title"]} for label in _nodes(i.get("labels"))]},
        "comments": {
            "totalCount": i.get("userNotesCount") or 0,
            "nodes": [{"authorAssociation": _association(n.get("author"), members)}
                      for n in _nodes(i.get("notes"))],
        },
    } for i in _nodes(project.get("issues"))]
    return {"data": {"repository": {"issues": {"nodes": nodes}}}}


def mr(env: dict) -> dict:
    """``gitlab-mr`` as ``gh pr view --json state,author``."""
    found = _project(env).get("mergeRequest")
    if found is None:
        raise GHError("GitLab merge request not found")
    state = found.get("state") or ""
    return {"state": _MR_STATES.get(state, state.upper()), "author": _login(found.get("author"))}


def item_view(obj: dict) -> dict:
    """A REST ``projects/:id/merge_requests/:iid`` or ``.../issues/:iid`` answer as
    ``gh pr view --json number,url,author,state`` (``gh issue view``) prints one."""
    state = obj.get("state") or ""
    return {"number": obj.get("iid"), "url": obj.get("web_url"),
            "author": _login(obj.get("author")),
            "state": _MR_STATES.get(state, state.upper())}


def viewer(env: dict, host: str) -> str:
    """``gitlab-viewer``: the signed-in username on ``host``."""
    user = (env.get("data") or {}).get("currentUser")
    if not user or not user.get("username"):
        raise GHError(f"not signed in to {host}")
    return user["username"]
