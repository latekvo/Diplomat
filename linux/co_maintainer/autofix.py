"""Pure PR auto-fix logic — the Linux port of CoMaintainerCore's Autofix.swift,
ReviewReconcile.swift and the VerdictPolicy in Review.swift.

Kept deterministic and side-effect-free so it's testable in isolation: the
GitHub reads live in :mod:`autofixmonitor`, and the spawn/track/persistence in
the Store. This module only decides *what* should happen given snapshots and
prior state.
"""

from __future__ import annotations

from dataclasses import dataclass

# MARK: - Snapshot + fingerprint


@dataclass(frozen=True)
class PRSnapshot:
    """One open PR of mine, as the monitor sees it each poll (mirrors PRSnapshot
    in Autofix.swift)."""

    number: int
    title: str
    url: str
    is_draft: bool
    mergeable: str  # "MERGEABLE" / "CONFLICTING" / "UNKNOWN"
    review_decision: str  # "" / "CHANGES_REQUESTED" / "APPROVED" / …
    threads_unresolved: int
    threads_i_owe: int


@dataclass(frozen=True)
class PRFingerprint:
    """The subset of a snapshot the edge-trigger compares poll-to-poll."""

    mergeable: str
    review_decision: str
    threads_unresolved: int


def compute_diff(
    prior: dict[int, PRFingerprint], now: list[PRSnapshot]
) -> tuple[list[tuple[str, PRSnapshot]], dict[int, PRFingerprint]]:
    """Edge-triggered diff (mirrors AutofixDiff.compute).

    Returns ``(events, fingerprints)`` where each event is ``("conflict", snap)``
    or ``("review", snap)``. A PR with no prior fingerprint is seeded silently
    (never fires on first sighting). A transient ``UNKNOWN`` mergeable carries the
    prior value forward so a conflict is neither lost nor faked.
    """
    events: list[tuple[str, PRSnapshot]] = []
    fingerprints: dict[int, PRFingerprint] = {}
    for s in now:
        p = prior.get(s.number)
        mergeable = s.mergeable
        if s.mergeable in ("UNKNOWN", "") and p is not None:
            mergeable = p.mergeable
        if p is not None:
            if p.mergeable != "CONFLICTING" and mergeable == "CONFLICTING":
                events.append(("conflict", s))
            more_threads = s.threads_unresolved > p.threads_unresolved
            now_changes = (
                p.review_decision != "CHANGES_REQUESTED"
                and s.review_decision == "CHANGES_REQUESTED"
            )
            if more_threads or now_changes:
                events.append(("review", s))
        fingerprints[s.number] = PRFingerprint(
            mergeable=mergeable,
            review_decision=s.review_decision,
            threads_unresolved=s.threads_unresolved,
        )
    return events, fingerprints


# MARK: - Retry reconciler (mirrors ReviewReconcile.swift)

RETRY_BASE = 5 * 60.0  # 5 min between the 1st and 2nd attempt
RETRY_MAX_BACKOFF = 3 * 60 * 60.0  # 3 h ceiling
RE_REQUEST_COOLDOWN = 60 * 60.0  # 1 h suppression on a changed request stamp


def retry_delay(attempts: int) -> float:
    """Exponential backoff before the ``attempts``-th dispatch may retry: 5m, 10m,
    20m, … capped at 3h. ``attempts`` is the number already made."""
    if attempts < 1:
        return 0.0
    return min(RETRY_BASE * (2 ** (attempts - 1)), RETRY_MAX_BACKOFF)


@dataclass
class ReviewAttempt:
    """A record of the last dispatch for one PR (keyed by PR number as a string)."""

    requested_at: str  # ISO8601 stamp, or the sentinel "unresolved"/"conflicting"
    last_dispatched_at: float  # epoch seconds
    attempts: int


def decide(
    prior: ReviewAttempt | None,
    stamp: str,
    in_flight: bool,
    banned: bool,
    now_ts: float,
) -> tuple[str, float]:
    """Whether to (re)dispatch an agent for a PR (mirrors ReviewReconcile.decide).

    Returns ``(action, value)`` where action is one of ``"banned"``,
    ``"in_flight"``, ``"cooling"`` (value = seconds remaining) or ``"dispatch"``
    (value = attempt number, ``1`` for the first).
    """
    if banned:
        return ("banned", 0.0)
    if in_flight:
        return ("in_flight", 0.0)
    if prior is None:
        return ("dispatch", 1)
    elapsed = now_ts - prior.last_dispatched_at
    if prior.requested_at == stamp:
        delay = retry_delay(prior.attempts)
        if elapsed < delay:
            return ("cooling", delay - elapsed)
        return ("dispatch", prior.attempts + 1)
    # A different request stamp (e.g. force-push churn): suppress for the cooldown.
    if elapsed < RE_REQUEST_COOLDOWN:
        return ("cooling", RE_REQUEST_COOLDOWN - elapsed)
    return ("dispatch", 1)


# MARK: - Review request (mirrors AutofixMonitor.ReviewRequest)


@dataclass(frozen=True)
class ReviewRequest:
    """A PR that has requested MY review, with the timestamps needed to decide
    whether I still owe a review."""

    number: int
    title: str
    url: str
    author: str
    author_association: str
    files: list[str]
    requested_at: str | None  # latest "review requested from me" (ISO8601)
    my_last_review_at: str | None  # my latest review submission (ISO8601)

    @property
    def owe_review(self) -> bool:
        """I owe a review when I'm requested and that request is newer than my last
        review of this PR (ISO8601 strings compare chronologically)."""
        if self.requested_at is None:
            return True
        if self.my_last_review_at is None:
            return True
        return self.requested_at > self.my_last_review_at


# MARK: - Verdict-withhold policy (mirrors VerdictPolicy in Review.swift)


def is_community(author_association: str) -> bool:
    """A PR author outside the trusted associations (OWNER/MEMBER/COLLABORATOR/
    CONTRIBUTOR by default, from filters.json)."""
    from . import core

    trusted = {a.upper() for a in (core.filters().get("trustedAssociations") or [])}
    if not trusted:
        trusted = {"OWNER", "MEMBER", "COLLABORATOR", "CONTRIBUTOR"}
    return author_association.upper() not in trusted


@dataclass(frozen=True)
class VerdictPolicy:
    """The three configurable suppressors for an auto-review's final verdict. A PR
    matching any enabled row gets inline comments only; otherwise it may get a
    verdict."""

    withhold_skill: bool = True
    withhold_installer: bool = True
    withhold_community: bool = True

    def withhold_reasons(self, files: list[str], author_association: str) -> list[str]:
        from .models import Filters

        reasons: list[str] = []
        if self.withhold_skill and any(Filters.is_skill_file(f) for f in files):
            reasons.append("touches a SKILL")
        if self.withhold_installer and any(Filters.is_installer_file(f) for f in files):
            reasons.append("touches the installer")
        if self.withhold_community and is_community(author_association):
            reasons.append("community PR")
        return reasons

    def allows_verdict(self, files: list[str], author_association: str) -> bool:
        return not self.withhold_reasons(files, author_association)
