"""Domain models, pure filter logic, and the gh-backed API.

A faithful Python port of the macOS Models.swift, but with every tunable
constant (skill suffix, installer prefixes, stale-days threshold, team
associations, the "APPROVED" sentinel) sourced from the shared
``core/filters.json`` so the two front-ends can never drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from . import core, gh


# MARK: - Datetime helpers


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    # gh emits ISO-8601 with a trailing Z; normalise to an offset Python parses.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def now() -> datetime:
    return datetime.now(timezone.utc)


# MARK: - Domain models


@dataclass(frozen=True)
class ReviewThread:
    is_resolved: bool
    viewer_can_resolve: bool
    last_comment_author: str | None


@dataclass(frozen=True)
class OpenPR:
    number: int
    title: str
    url: str
    is_draft: bool
    author: str
    created_at: datetime
    ready_for_review_at: datetime | None
    files: list[str]
    review_decision: str | None
    review_threads: list[ReviewThread]

    @property
    def id(self) -> int:
        return self.number

    @property
    def ready_at(self) -> datetime:
        """Best-effort 'has been ready since' timestamp."""
        return self.ready_for_review_at or self.created_at

    def unaddressed_threads(self, me: str) -> list[ReviewThread]:
        """Threads on *my* PR I still owe a response on: resolvable, unresolved,
        and whose most-recent comment isn't mine."""
        return [
            t
            for t in self.review_threads
            if t.viewer_can_resolve and not t.is_resolved and t.last_comment_author != me
        ]


@dataclass(frozen=True)
class OpenIssue:
    number: int
    title: str
    url: str
    author: str
    author_association: str
    created_at: datetime
    updated_at: datetime
    comment_count: int
    assignees: list[str]
    labels: list[str]
    member_responded: bool

    @property
    def id(self) -> int:
        return self.number

    @property
    def is_external(self) -> bool:
        return self.author_association not in set(core.filters()["orgAssociations"])

    @property
    def is_addressed(self) -> bool:
        return self.member_responded or bool(self.assignees)


# MARK: - Filters (the tool logic, data-driven from core/filters.json)


class Filters:
    @staticmethod
    def _cfg() -> dict:
        return core.filters()

    @staticmethod
    def is_skill_file(path: str) -> bool:
        return path.lower().endswith(Filters._cfg()["skillSuffix"])

    @staticmethod
    def is_installer_file(path: str) -> bool:
        return any(p in path for p in Filters._cfg()["installerPrefixes"])

    @staticmethod
    def team() -> set[str]:
        return set(Filters._cfg()["team"])

    @staticmethod
    def skill_prs(prs: list[OpenPR]) -> list[OpenPR]:
        return [p for p in prs if any(Filters.is_skill_file(f) for f in p.files)]

    @staticmethod
    def installer_prs(prs: list[OpenPR]) -> list[OpenPR]:
        return [p for p in prs if any(Filters.is_installer_file(f) for f in p.files)]

    @staticmethod
    def stale_ready_prs(prs: list[OpenPR], at: datetime | None = None) -> list[OpenPR]:
        at = at or now()
        days = Filters._cfg()["staleReadyDays"]
        return [
            p
            for p in prs
            if not p.is_draft and (at - p.ready_at).total_seconds() > days * 86400
        ]

    @staticmethod
    def unaddressed_external_issues(issues: list[OpenIssue]) -> list[OpenIssue]:
        return [i for i in issues if i.is_external and not i.is_addressed]

    @staticmethod
    def my_approved_prs(prs: list[OpenPR], me: str) -> list[OpenPR]:
        if not me:
            return []
        approved = Filters._cfg()["approvedDecision"]
        return [p for p in prs if p.author == me and p.review_decision == approved]

    @staticmethod
    def my_unaddressed_review_prs(prs: list[OpenPR], me: str) -> list[OpenPR]:
        if not me:
            return []
        return [p for p in prs if p.author == me and p.unaddressed_threads(me)]


# MARK: - Tiny formatting helpers


class Fmt:
    @staticmethod
    def age(date: datetime, at: datetime | None = None) -> str:
        at = at or now()
        s = max(0.0, (at - date).total_seconds())
        if s >= 86400:
            return f"{int(s // 86400)}d"
        if s >= 3600:
            return f"{int(s // 3600)}h"
        return f"{int(s // 60)}m"

    @staticmethod
    def days(date: datetime, at: datetime | None = None) -> int:
        at = at or now()
        return int(max(0.0, (at - date).total_seconds()) // 86400)

    @staticmethod
    def skill_name(path: str) -> str:
        parts = [p for p in path.split("/") if p]
        return parts[-2] if len(parts) >= 2 else path

    @staticmethod
    def short_path(path: str) -> str:
        return path.replace("packages/", "")

    @staticmethod
    def clock(date: datetime | None) -> str:
        if date is None:
            return "—"
        return date.astimezone().strftime("%H:%M")


# MARK: - GitHub API (GraphQL via the gh CLI)


class API:
    @staticmethod
    def fetch_viewer_login() -> str:
        env = gh.graphql("viewer", with_repo=False)
        return env["data"]["viewer"]["login"]

    @staticmethod
    def fetch_open_prs() -> list[OpenPR]:
        env = gh.graphql("prs", with_repo=True)
        nodes = env["data"]["repository"]["pullRequests"]["nodes"]
        out: list[OpenPR] = []
        for n in nodes:
            author = (n.get("author") or {}).get("login", "ghost")
            timeline = n["timelineItems"]["nodes"]
            ready_at = next(
                (_parse_dt(t.get("createdAt")) for t in timeline if t.get("createdAt")),
                None,
            )
            threads = [
                ReviewThread(
                    is_resolved=t["isResolved"],
                    viewer_can_resolve=t["viewerCanResolve"],
                    last_comment_author=_last_comment_author(t),
                )
                for t in n["reviewThreads"]["nodes"]
            ]
            out.append(
                OpenPR(
                    number=n["number"],
                    title=n["title"],
                    url=n["url"],
                    is_draft=n["isDraft"],
                    author=author,
                    created_at=_parse_dt(n["createdAt"]),
                    ready_for_review_at=ready_at,
                    files=[f["path"] for f in n["files"]["nodes"]],
                    review_decision=n.get("reviewDecision"),
                    review_threads=threads,
                )
            )
        return out

    @staticmethod
    def fetch_open_issues() -> list[OpenIssue]:
        env = gh.graphql("issues", with_repo=True)
        nodes = env["data"]["repository"]["issues"]["nodes"]
        team = Filters.team()
        out: list[OpenIssue] = []
        for n in nodes:
            author = (n.get("author") or {}).get("login", "ghost")
            comments = n["comments"]
            out.append(
                OpenIssue(
                    number=n["number"],
                    title=n["title"],
                    url=n["url"],
                    author=author,
                    author_association=n["authorAssociation"],
                    created_at=_parse_dt(n["createdAt"]),
                    updated_at=_parse_dt(n["updatedAt"]),
                    comment_count=comments["totalCount"],
                    assignees=[a["login"] for a in n["assignees"]["nodes"]],
                    labels=[l["name"] for l in n["labels"]["nodes"]],
                    member_responded=any(
                        c["authorAssociation"] in team for c in comments["nodes"]
                    ),
                )
            )
        return out


def _last_comment_author(thread: dict) -> str | None:
    nodes = thread["comments"]["nodes"]
    if not nodes:
        return None
    return (nodes[-1].get("author") or {}).get("login")
