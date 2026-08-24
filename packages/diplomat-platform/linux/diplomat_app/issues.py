"""Fix-issues config + prompt builder.

The prompt text (depth fragments, scope templates, enumeration and action blocks)
all comes from the shared ``assets/issues.json``; the *assembly* order/conditions
live in Swift (DiplomatCore/Issues.swift) and are reached by shelling out to the
``diplomat-core`` CLI, so the two front-ends can't drift. ``IssueConfig`` mirrors
the Swift struct's inputs and derived toggles. The terminal spawner is shared with
:mod:`diplomat_runtime.review` (``review.spawn`` / ``review.resolved``).

Three axes, deliberately independent:

* ``target`` — WHICH issues (all / mine / one user's / the community's / the org's /
  one specific issue), the widest of the three because an issue's author association
  is a scope of its own (see :class:`~diplomat_runtime.issuetarget.IssueTarget`);
* ``unassigned_only`` — narrowed to the ones nobody has claimed, so a sweep picks up
  only what is actually going spare (moot for one specific issue);
* ``assign_to_me`` / ``open_prs`` / ``comment_on_issue`` / ``include_features`` —
  what the run may DO about what it finds.
"""

from __future__ import annotations

from dataclasses import dataclass

from diplomat_runtime import core
from diplomat_runtime.configbase import RepoConfig
from diplomat_runtime.depths import DepthLadder
from diplomat_runtime.issuetarget import IssueTarget
from diplomat_runtime.prref import PRRef, parse_pr_ref

# Whose issues we sweep. Re-exported as ``Target`` so the wizard reads like its
# Review/Resolve-conflicts siblings.
Target = IssueTarget

#: The Fix-issues depth ladder, over the same helper the review one uses.
LADDER = DepthLadder(core.issues)


def depths() -> list[dict]:
    return LADDER.all()


def depth_ids() -> list[str]:
    return LADDER.ids()


def depth_by_id(depth_id: str) -> dict:
    return LADDER.by_id(depth_id)


def default_depth_id() -> str:
    return LADDER.default_id()


@dataclass
class IssueConfig(RepoConfig):
    depth: str = ""  # depth id; "" -> default
    target: Target = Target.ALL
    username: str = ""  # the "someone else's" handle
    me: str = ""  # authenticated viewer login, used as the @handle for "mine"
    specific_issue: str = ""

    #: Skip every issue that already has an assignee — somebody is on it already.
    unassigned_only: bool = True
    #: Claim each issue (assign it to me) before starting on it, and hand it back if
    #: the run abandons it.
    assign_to_me: bool = True
    #: Deliver each fix as its own draft PR. Off ⇒ nothing reaches the remote.
    open_prs: bool = True
    #: Report the outcome on the issue itself, one comment per issue worked.
    comment_on_issue: bool = True
    #: The one escalation: also take on feature requests, not just bug reports.
    include_features: bool = False

    def __post_init__(self) -> None:
        if not self.depth:
            self.depth = default_depth_id()

    # An issue run is deliberately NOT PR-scoped, so ``single_pr_number`` /
    # ``single_pr_url`` stay ``None`` (inherited from RepoConfig, as for the audit).
    # The dispatch pipeline's dedup is PR-shaped throughout — the in-flight check
    # matches a ``/pull/<n>`` URL and the queue keys on a PR number — so handing it an
    # issue number would collide with the PR that happens to share it. What keeps two
    # agents off one issue instead is ``assign_to_me``, which claims it on GitHub
    # where every machine can see the claim, not just this one.

    @property
    def author_handle(self) -> str:
        """The @handle whose issues we sweep — empty for every scope that names no one
        person (all / contributors / members / one specific issue)."""
        if self.target == Target.MINE:
            return self.me or "me"
        if self.target == Target.SOMEONE:
            return self.username.strip()
        return ""

    @property
    def is_single_issue(self) -> bool:
        return self.target == Target.SPECIFIC

    @property
    def can_filter_unassigned(self) -> bool:
        """Whether the wizard offers the unassigned filter at all. It only means
        something for a sweep: a specific issue was named by hand, so filtering it back
        out would just be a run that does nothing."""
        return not self.is_single_issue

    @property
    def issue_ref(self) -> PRRef:
        """The single-issue field parsed as a number / URL / ``owner/repo#n``
        shorthand, checked against the target repo."""
        owner, repo = self.target_repo
        return parse_pr_ref(self.specific_issue, owner, repo, kind="issues")

    @property
    def is_valid(self) -> bool:
        if self.is_single_issue:
            return self.issue_ref.is_valid
        # A scope that names a person needs that person; the rest need nothing.
        return bool(self.author_handle) if self.target.needs_handle else True

    def build_prompt(self) -> str:
        # Single-sourced in Swift (DiplomatCore) via the diplomat-core CLI.
        from diplomat_runtime import promptcore

        return promptcore.build_prompt({
            "kind": "issues",
            "depth": self.depth,
            "target": self.target.wire_name,
            "username": self.username,
            "me": self.me,
            "specificIssue": self.specific_issue,
            "unassignedOnly": self.unassigned_only,
            "assignToMe": self.assign_to_me,
            "openPRs": self.open_prs,
            "commentOnIssue": self.comment_on_issue,
            "includeFeatures": self.include_features,
        })
