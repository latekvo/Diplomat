"""Pure PR auto-fix logic — the Linux port of DiplomatCore's Autofix.swift,
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
    # Head commit sha (headRefOid) — the "which push" part of the mesh work key,
    # so two nodes observing the same commit derive the same key (docs/szpontnet/12).
    head_sha: str = ""


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
    head_sha: str = ""  # head commit sha — the mesh work key's "@sha" part

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


# MARK: - Mesh coordination for the auto-monitors (mirrors AutofixMesh in Autofix.swift)
#
# Two machines running this monitor poll the same GitHub state as the same user, so
# each is an independent origin of the same work (docs/szpontnet/12-work-claims.md).
# Every machine scans; the Store routes each auto find through claim-gated DISPATCH
# (`Store._route_via_mesh`): the mesh runs it once, on the best-surplus node, and
# the EXECUTOR holds the work-key claim for its agent's lifetime — so a concurrent
# or repeat scan is suppressed, a crash frees it for a retry, and a node death frees
# it for failover. There is deliberately NO duty-assignment stand-down: it deferred
# to a node that might not be scanning, silently dropping the operator's work.

WORK_REVIEW_REQ = "review"  # reviews requested of me → duty "review"
WORK_REVIEW_REPLY = "review-reply"  # replies to reviews on MY PRs → duty "review"
WORK_CONFLICTS = "conflicts"  # conflict fixes on MY PRs → duty "conflicts"


def work_key(kind: str, pr_url: str, head_sha: str) -> str:
    """The origination-dedup key for one unit of monitor work — the reference
    convention from docs/szpontnet/12: ``<kind>:<host>/<owner>/<repo>#<n>@<sha>``.

    Derived from the PR's own URL so every node observing the same PR agrees
    byte-for-byte (the Swift twin must produce identical strings — see the parity
    tests). Returns ``""`` — claim gate skipped, the safe pre-claims degradation —
    when the URL doesn't look like a PR URL or the head sha is unknown."""
    if not head_sha:
        return ""
    from urllib.parse import urlparse

    try:
        u = urlparse(pr_url)
    except ValueError:
        return ""
    host = (u.hostname or "").lower()
    parts = [p for p in (u.path or "").split("/") if p]
    if not host or len(parts) != 4 or parts[2] != "pull" or not parts[3].isdigit():
        return ""
    return f"{kind}:{host}/{parts[0]}/{parts[1]}#{parts[3]}@{head_sha}"


def parse_work_key(key: str) -> tuple[str, str, str, int] | None:
    """Inverse of :func:`work_key`: split ``<kind>:<host>/<owner>/<repo>#<n>@<sha>``
    into ``(kind, owner, repo, pr_number)``. Returns None when ``key`` isn't a PR
    work key (empty, or any shape :func:`work_key` never emits).

    The executor's ps ground-truth floor uses this to learn which PR a dispatched
    unit of work is for, then asks :func:`live_pr_numbers` whether an agent for it
    is already alive on the host — so it dedups on the PR (like the ps-scan), never
    on the exact key, and a fresh push (new ``@sha``) can't sneak a second agent
    onto a PR already under review."""
    if not key or ":" not in key:
        return None
    kind, rest = key.split(":", 1)
    # <host>/<owner>/<repo>#<n>@<sha> — owner/repo/host never contain '#' or '@',
    # and a sha is hex, so peeling from the right is unambiguous.
    if "#" not in rest or "@" not in rest:
        return None
    left, _sha = rest.rsplit("@", 1)
    path, num = left.rsplit("#", 1)
    if not num.isdigit():
        return None
    segs = [p for p in path.split("/") if p]
    if len(segs) != 3:  # host / owner / repo
        return None
    _host, owner, repo = segs
    try:
        # str.isdigit() is True for Unicode superscripts (¹²³) and for decimal runs
        # longer than CPython's 4300-digit int() limit — neither of which int() will
        # parse. work_key never emits those, so a raise here would break every caller's
        # fail-open contract (the executor's _pr_agent_running dedup floor tears the
        # dispatching peer's link on a hostile work_key); treat them as a non-PR key.
        return kind, owner, repo, int(num)
    except ValueError:
        return None


# MARK: - Unified dispatch gate (one workflow, two triggers)
#
# The SPAWN buttons and the auto-monitors are two TRIGGERS for the very same
# workflow: run one agent job. Everything from "run X (on PR #n)" onward - the
# ban check, in-flight dedup, mesh coordination, spawn focus, activity label,
# counters - is decided HERE, once, so the interfaces cannot drift apart.
# Triggers stay thin: a click, or a poll's backoff decision. (2026-07-20: the
# drift was not hypothetical - dedup lived only on some paths, dupes followed.)
#
# The intended trigger asymmetries, in full (anything else is a bug):
# - focus: a panel spawn brings the terminal forward, an auto spawn must not
#   steal focus (moot on Linux - review.spawn is always a new window);
# - mesh: only auto origination is mesh-gated - a human clicking THIS machine's
#   button has already decided placement (dispatch_decide);
# - counters: only a monitor's FIRST dispatch counts as auto-handled work
#   (dispatch_bumps_counter);
# - label: auto rows carry the "Auto · " prefix, retries are surfaced the same
#   way on both (dispatch_label).
#
# Swift twin: AgentDispatchGate in DiplomatCore/Autofix.swift - keep semantics
# byte-equivalent (see the parity tests on both sides).

SOURCE_PANEL = "panel"
SOURCE_AUTO = "auto"

VERDICT_PROCEED = "proceed"
VERDICT_IN_FLIGHT = "in_flight"  # an agent already works this PR - whoever asks
VERDICT_BANNED = "banned"  # prompt-injection ban on the author - whoever asks
VERDICT_STAND_DOWN = "stand_down"  # mesh: another node originates (auto only)


def dispatch_decide(
    source: str, banned: bool, agent_on_pr: bool, mesh_stands_down: bool
) -> str:
    """The one decision both interfaces obey, in fixed precedence: ban, then
    in-flight, then (auto only) mesh. Mesh comes last so a claim - which has
    gossip side effects - is only attempted when the job would otherwise run."""
    if banned:
        return VERDICT_BANNED
    if agent_on_pr:
        return VERDICT_IN_FLIGHT
    if source == SOURCE_AUTO and mesh_stands_down:
        return VERDICT_STAND_DOWN
    return VERDICT_PROCEED


def dispatch_label(source: str, core: str, attempt: int = 1) -> str:
    """The activity/session label both interfaces produce: same core, the source
    prefix and retry suffix applied identically everywhere."""
    retry = f" · retry {attempt}" if attempt > 1 else ""
    prefix = "Auto · " if source == SOURCE_AUTO else ""
    return f"{prefix}{core}{retry}"


def dispatch_bumps_counter(source: str, attempt: int) -> bool:
    """Auto-handled counters bump only on a monitor's first dispatch - a retry is
    not new work handled, and a manual run is the user's own action."""
    return source == SOURCE_AUTO and attempt == 1


@dataclass(frozen=True)
class AgentJob:
    """One agent job, whoever triggers it. The trigger supplies WHAT to run
    (config -> prompt, labels, PR identity); the store's ``dispatch_agent`` owns
    everything that HAPPENS - ban check, in-flight dedup, mesh policy, spawn,
    registration, counters. Twin of Store.AgentJob on macOS."""

    kind: str  # activity tint: "review" | "conflicts" | "audit"
    audit_action: str  # activity-feed verb
    label: str  # label core (source prefix / retry suffix added by dispatch_label)
    prompt: str
    pr_url: str | None = None  # None = not PR-scoped -> no PR dedup possible
    pr_number: int | None = None
    author_login: str | None = None  # whose PR we'd review - the ban dimension
    duty: str = ""  # mesh duty, for auto-origination gating
    work_key: str = ""  # mesh claim key ("" = no claim)
    counter: str | None = None  # "review_requests" | "my_reviews" | "conflicts"


_LIVE_AGENT_RE_TMPL = r"PR #(\d+) in {repo}"


def live_pr_numbers(ps_output: str, owner: str, repo: str) -> set[int]:
    """PR numbers of ``claude`` agents alive in a ``ps`` args dump — the
    tracking-independent half of the monitor's in-flight dedup (twin of
    ProcessMonitor.liveAgentPRNumbers on macOS).

    Every single-PR prompt the applet dispatches opens with
    ``… PR #<n> in <owner>/<repo> …`` and ``claude`` receives the whole prompt as
    one argv, so a live agent is visible in ``ps`` no matter what happened to the
    in-memory ``_autofix_inflight`` list (an applet restart wipes it while the
    agents run on). Only lines containing ``claude`` count: the spawning shell's
    argv holds the unexpanded ``$(cat …)``, never the prompt text."""
    import re

    pat = re.compile(_LIVE_AGENT_RE_TMPL.format(repo=re.escape(f"{owner}/{repo}")))
    out: set[int] = set()
    for line in ps_output.splitlines():
        if "claude" not in line:
            continue
        for m in pat.finditer(line):
            out.add(int(m.group(1)))
    return out
