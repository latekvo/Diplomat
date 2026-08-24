"""Fix-issues config + prompt builder.

The prompt text (depth fragments, the one-issue scope sentence, action blocks) all
comes from the shared ``assets/issues.json``; the *assembly* order/conditions live
in Swift (DiplomatCore/Issues.swift) and are reached by shelling out to the
``diplomat-core`` CLI, so the two front-ends can't drift. ``IssueConfig`` mirrors
the Swift struct's inputs and derived toggles. The terminal spawner is shared with
:mod:`diplomat_runtime.review` (``review.spawn`` / ``review.resolved``).

**Every prompt this builds works ONE issue** — ``specific_issue`` names it. A scope
is never handed to an agent as a scope: the front-end enumerates it against the
repo's open issues (``Filters.swept_issues``) and queues one run per issue, each
built from :meth:`IssueConfig.for_issue`. So the axes below are read by different
readers:

* ``target`` / ``username`` / ``unassigned_only`` — WHICH issues the sweep picks up
  (all / mine / one user's / the community's / the org's / one specific issue,
  narrowed to the ones nobody has claimed). The front-end reads these; the prompt
  reads them only to know whether this issue was swept, and so whether it has to
  re-check the state the sweep selected on;
* ``depth`` — how hard the one issue is proven;
* ``assign_to_me`` / ``open_prs`` / ``comment_on_issue`` / ``include_features`` —
  what the run may DO about what it finds.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

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
    #: The one issue this run works. Typed by hand under the SPECIFIC scope, and
    #: filled in per issue by :meth:`for_issue` for every other one.
    specific_issue: str = ""

    #: Skip every issue that already has an assignee — somebody is on it already.
    unassigned_only: bool = True
    #: Claim the issue (assign it to me) before starting on it, and hand it back if
    #: the run abandons it.
    assign_to_me: bool = True
    #: Deliver the fix as its own draft PR. Off ⇒ nothing reaches the remote.
    open_prs: bool = True
    #: Report the outcome on the issue itself.
    comment_on_issue: bool = True
    #: The one escalation: also take on feature requests, not just bug reports.
    include_features: bool = False

    def __post_init__(self) -> None:
        if not self.depth:
            self.depth = default_depth_id()

    # An issue run is deliberately NOT PR-scoped, so ``single_pr_number`` /
    # ``single_pr_url`` stay ``None`` (inherited from RepoConfig, as for the audit).
    # The dispatch pipeline's dedup is PR-shaped throughout — the in-flight check
    # matches a ``/pull/<n>`` URL and keys on a PR number — so handing it an issue
    # number would collide with the PR that happens to share it. What keeps two agents
    # off one issue instead is ``assign_to_me``, which claims it on GitHub where every
    # machine can see the claim, not just this one.

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
        """Fix exactly one issue named by hand, instead of sweeping a scope."""
        return self.target == Target.SPECIFIC

    @property
    def sweep_author(self) -> str:
        """The login whose open issues this sweep expands into one queued fix each, or
        "" when the scope names nobody in particular (all / contributors / members) or
        there is nothing to expand (one named issue, or my own issues before the viewer
        login has resolved).

        Not :attr:`author_handle`, which falls back to the literal "me" for the prompt
        to address: matched against real issue authors, where the account called "me"
        has opened nothing."""
        if self.target == Target.MINE:
            return self.me.strip()
        if self.target == Target.SOMEONE:
            return self.username.strip()
        return ""

    def for_issue(self, number: int) -> "IssueConfig":
        """This sweep, narrowed to one of the issues it covers — the config behind one
        queued fix.

        Same depth and same action toggles, because they are what the operator chose;
        only the issue is added. The scope is deliberately KEPT rather than collapsed
        to SPECIFIC: it is what still says this issue was swept rather than named, and
        so that the run re-checks the state the sweep selected on before working an
        issue whose turn came hours later."""
        return replace(self, specific_issue=str(number))

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

    def prompt_payload(self) -> dict:
        """The inputs the prompt is assembled from — everything the builder reads, and
        nothing derived. Split out of :meth:`build_prompt` because a queued fix is
        stored as this payload: it is already the serialised form of the config, kept
        in step with the Swift builder by the golden-prompt tests, so persisting it
        needs no second spelling of these fields (``Store.requested_work``)."""
        return {
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
        }

    def build_prompt(self) -> str:
        # Single-sourced in Swift (DiplomatCore) via the diplomat-core CLI.
        from diplomat_runtime import promptcore

        return promptcore.build_prompt(self.prompt_payload())
