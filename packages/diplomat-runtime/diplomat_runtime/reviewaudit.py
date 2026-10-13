"""Whether a finished review run's review went through GitHub's lifecycle.

The prompt has the agent open one review PENDING, put every comment inside it, and
submit it (``assets/review.json`` → ``leaveReviews``). Two ways of not doing that
look like success from here: a review left PENDING is visible to nobody but me, and
findings posted one at a time each become a review of their own. When a review run
retires, the store reads my reviews on its PR (``assets/graphql/my-reviews``) and
logs what :func:`findings` names. Mirrors ``ReviewAudit`` in ReviewAudit.swift.
"""

from __future__ import annotations

import re
from datetime import datetime

from . import gh

_PR_URL = re.compile(r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+)/pull/([0-9]+)/?$")


def pr_coordinates(pr_url: str) -> tuple[str, str, int] | None:
    """``(owner, repo, number)`` for a github.com PR URL, else None — gh's GraphQL
    answers for its default host only."""
    m = _PR_URL.match(pr_url.strip())
    return (m.group(1), m.group(2), int(m.group(3))) if m else None


def _at(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def findings(pull_request: dict, me: str, since: float) -> list[str]:
    """What the reviews of mine on one PR say a run that started at ``since`` left
    wrong, as sentences for the activity feed.

    Only what the run itself made is judged: a PENDING review opened earlier may be
    my own draft in the browser. A split is only judged on someone else's PR, the
    only kind the prompt has a run review rather than reply on.
    """
    reviews = [rv for rv in ((pull_request.get("reviews") or {}).get("nodes") or []) if rv]
    out: list[str] = []
    if any(rv.get("state") == "PENDING" and (_at(rv.get("createdAt")) or 0) >= since
           for rv in reviews):
        out.append("left its review PENDING — never submitted, so only I can see it")
    author = ((pull_request.get("author") or {}).get("login") or "").lower()
    if author and author != me.lower():
        opened = sum(
            1 for rv in reviews
            if (_at(rv.get("submittedAt")) or 0) >= since
            and any(c and c.get("replyTo") is None
                    for c in ((rv.get("comments") or {}).get("nodes") or []))
        )
        if opened > 1:
            out.append(f"posted its findings as {opened} separate reviews instead of one")
    return out


def audit(pr_url: str, me: str, since: float) -> list[str]:
    """Read my reviews on ``pr_url`` and return :func:`findings`. ``[]`` for a URL
    that names no github.com PR. Raises :class:`gh.GHError` when GitHub cannot be
    asked."""
    coords = pr_coordinates(pr_url)
    if coords is None or not me:
        return []
    owner, repo, number = coords
    env = gh.graphql("my-reviews", with_repo=False,
                     variables={"owner": owner, "name": repo, "me": me},
                     typed_variables={"number": str(number)})
    pr = (((env.get("data") or {}).get("repository") or {}).get("pullRequest")) or {}
    return findings(pr, me, since)
