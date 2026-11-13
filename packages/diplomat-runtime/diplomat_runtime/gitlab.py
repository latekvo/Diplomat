"""Each ``gitlab-*.graphql`` answer turned into the envelope of its GitHub twin, so one
parser reads both. Twin of ``GitLab.swift``, pinned to it by the shared fixtures in
``diplomat-core/Tests/fixtures/gitlab``; nothing here calls glab but ``lookup``.

GitLab has no author association: a project member reads as ``MEMBER``, anyone else as
``NONE``, and an author past a full member page is asked of ``lookup``. Review
requests, approvals, requested changes and ready-for-review exist only as system notes,
so they are read off note bodies; "left review comments" is GitHub's COMMENTED review.
GitLab gives times in the instance's offset; they are converted to UTC ``Z`` form, as
GitHub's, so they compare as strings.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timezone

from .gh import GHError

#: User ids -> the project members among them.
Lookup = Callable[[list[int]], set[int]]

#: ``detailedMergeStatus`` values GitLab answers while it has not decided yet.
_UNDECIDED = {"UNCHECKED", "CHECKING", "PREPARING"}
_READY = "marked this merge request as **ready**"
_REQUESTED = "requested review from "
_UNREQUESTED = " and removed review request"
_REVIEWED = ("approved this merge request", "requested changes", "left review comments")
#: A reviewer's ``reviewState`` once they have answered the request.
_ANSWERED = {"REVIEWED", "APPROVED", "REQUESTED_CHANGES"}
_MENTION = re.compile(r"@([\w.-]+)")
_MR_STATES = {"opened": "OPEN", "locked": "OPEN", "closed": "CLOSED", "merged": "MERGED"}
_USER_GID = re.compile(r"gid://gitlab/User/([0-9]+)")
#: User ids per ``members/all`` read: under its page of 100, so one page answers them
#: all, and few enough for the ``Link`` header, which repeats each id twice: a
#: self-hosted GitLab's proxy answered 502 past about 80 ids.
_MEMBER_CHUNK = 40


def _project(env: dict) -> dict:
    return (env.get("data") or {}).get("project") or {}


def _nodes(conn: dict | None) -> list[dict]:
    return [n for n in ((conn or {}).get("nodes") or []) if n]


def _utc(at: str | None) -> str | None:
    if at is None:
        return None
    try:
        when = datetime.fromisoformat(at.replace("Z", "+00:00"))
    except ValueError:
        raise GHError(f"GitLab time {at} is unreadable") from None
    return when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _login(user: dict | None) -> dict | None:
    return {"login": user["username"]} if user else None


def _members(project: dict, users: list[dict | None], lookup: Lookup) -> set[str]:
    """Lowercased member usernames: the member page, plus, when it is full, those of
    ``users`` missing from it that ``lookup`` confirms."""
    page = project.get("projectMembers") or {}
    members = {(m.get("user") or {}).get("username", "").lower() for m in _nodes(page)} - {""}
    if (page.get("pageInfo") or {}).get("hasNextPage"):
        unlisted = {_user_id(u): u["username"].lower() for u in users
                    if u and u["username"].lower() not in members}
        members |= {unlisted[i] for i in lookup(sorted(unlisted)) if i in unlisted}
    return members


def _user_id(user: dict) -> int:
    found = _USER_GID.fullmatch(user.get("id") or "")
    if not found:
        raise GHError(f"GitLab user {user['username']} has no readable id")
    return int(found.group(1))


def member_paths(slug: str, ids: list[int]) -> list[str]:
    """REST reads of which of ``ids`` are members of ``slug`` (direct, inherited or via
    a shared group), :data:`_MEMBER_CHUNK` ids a read, in order."""
    project = urllib.parse.quote(slug, safe="")
    return [f"projects/{project}/members/all?per_page=100"
            + "".join(f"&user_ids[]={i}" for i in ids[start:start + _MEMBER_CHUNK])
            for start in range(0, len(ids), _MEMBER_CHUNK)]


def _association(user: dict | None, members: set[str]) -> str:
    return "MEMBER" if user and user["username"].lower() in members else "NONE"


def _mergeable(mr: dict) -> str:
    if mr.get("conflicts") is True:
        return "CONFLICTING"
    status = mr.get("detailedMergeStatus")
    return "UNKNOWN" if status is None or status in _UNDECIDED else "MERGEABLE"


def rest_mergeable(obj: dict) -> str:
    """A REST merge request answer's mergeable, mapped as the GraphQL one is."""
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
    """Resolvable discussions as GitHub review threads (asked ``comments(last: 1)``):
    the count of human notes and the last one. A system note (a push's "changed this
    line") never answers a thread, but resolvability is read off the last note."""
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
    return {"nodes": [{"__typename": "ReadyForReviewEvent",
                       "createdAt": _utc(ready[-1]["createdAt"])}] if ready else []}


def prs(env: dict) -> dict:
    """``gitlab-prs`` as ``prs``."""
    project = _project(env)
    nodes = [{
        "number": int(mr["iid"]),
        "title": mr.get("title"),
        "url": mr.get("webUrl"),
        "isDraft": bool(mr.get("draft")),
        "createdAt": _utc(mr.get("createdAt")),
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
    """One ``ReviewRequestedEvent`` per user a "requested review from" note adds, minus
    those the same note un-requests."""
    out = []
    for n in notes:
        body = n.get("body") or ""
        if not body.startswith(_REQUESTED):
            continue
        added = body.split(_UNREQUESTED, 1)[0]
        out += [{"createdAt": _utc(n.get("createdAt")),
                 "requestedReviewer": {"__typename": "User", "login": login}}
                for login in _MENTION.findall(added)]
    return out


def _request_of(reviewer: str, events: list[dict], mr: dict,
                activity: list[dict]) -> list[dict] | None:
    """``reviewer``'s request when no note in the window names them: ``None`` when their
    ``reviewState`` says they answered it, as GitHub drops an answered request from
    ``review-requested:``; else at least as old as the window's oldest note (the merge
    request, for an empty window), the query answering only merge requests requesting
    their review."""
    if any(e["requestedReviewer"]["login"].lower() == reviewer.lower() for e in events):
        return []
    if any((r.get("username") or "").lower() == reviewer.lower()
           and (r.get("mergeRequestInteraction") or {}).get("reviewState") in _ANSWERED
           for r in _nodes(mr.get("reviewers"))):
        return None
    times = [_utc(n["createdAt"]) for n in activity if n.get("createdAt")]
    return [{"createdAt": min(times) if times else _utc(mr.get("createdAt")),
             "requestedReviewer": {"__typename": "User", "login": reviewer}}]


def review_requests(env: dict, reviewer: str, lookup: Lookup) -> dict:
    """``gitlab-review-requests`` asked for ``reviewer`` as ``review-requests``, less the
    merge requests ``reviewer`` answered (:func:`_request_of`); ``files`` only when the
    query asked for ``diffStats``."""
    project = _project(env)
    owed = []
    for mr in _nodes(project.get("mergeRequests")):
        activity = _nodes(mr.get("activity"))
        events = _requests(activity)
        mine = _request_of(reviewer, events, mr, activity)
        if mine is not None:
            owed.append((mr, activity, mine + events))
    members = _members(project, [mr.get("author") for mr, _, _ in owed], lookup)
    nodes = []
    for mr, activity, requests in owed:
        node = {
            "number": int(mr["iid"]),
            "title": mr.get("title"),
            "url": mr.get("webUrl"),
            "author": _login(mr.get("author")),
            "authorAssociation": _association(mr.get("author"), members),
            "headRefOid": mr.get("diffHeadSha"),
            "timelineItems": {"nodes": requests},
            "reviews": {"nodes": [
                {"author": _login(n.get("author")), "submittedAt": _utc(n.get("createdAt"))}
                for n in activity if (n.get("body") or "").startswith(_REVIEWED)]},
            "comments": {"nodes": [
                {"author": _login(n.get("author")), "createdAt": _utc(n.get("createdAt"))}
                for n in _nodes(mr.get("comments"))]},
        }
        if "diffStats" in mr:
            node["files"] = _files(mr)
        nodes.append(node)
    return {"data": {"search": {"nodes": nodes}}}


def issues(env: dict, lookup: Lookup) -> dict:
    """``gitlab-issues`` as ``issues``."""
    project = _project(env)
    members = _members(project, [user for i in _nodes(project.get("issues"))
                                 for user in [i.get("author")]
                                 + [n.get("author") for n in _nodes(i.get("notes"))]], lookup)
    nodes = [{
        "number": int(i["iid"]),
        "title": i.get("title"),
        "url": i.get("webUrl"),
        "createdAt": _utc(i.get("createdAt")),
        "updatedAt": _utc(i.get("updatedAt")),
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
