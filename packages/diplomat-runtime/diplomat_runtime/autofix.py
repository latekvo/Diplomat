"""Pure PR auto-fix logic — the Python twin of DiplomatCore's Autofix.swift,
ReviewReconcile.swift and the VerdictPolicy in Review.swift.

Kept deterministic and side-effect-free so it's testable in isolation: the
GitHub reads live in :mod:`autofixmonitor`, and the spawn/track/persistence in
the Store. This module only decides *what* should happen given snapshots and
prior state.
"""

from __future__ import annotations

import math
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
    # so two nodes observing the same commit derive the same key (szpontnet-spec/docs/12).
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

# The ``ReviewAttempt.requested_at`` stamp each monitor files its dispatches under.
# The two level-triggered reconcilers have no GitHub timestamp to use — the PR
# simply is or isn't in the state they watch — so a constant stands in. (A review
# request has a real timestamp, and ``"-"`` is :func:`decide`'s unknown-stamp
# sentinel for one that is missing.)
#
# Single-sourced because two places write the same stamp: the reconciler when it
# dispatches, and the queue when it runs a dispatch the cap deferred
# (``AgentJob.attempt_stamp``). Two spellings of "conflicting" would not fail
# anything loudly — :func:`decide` would just read the queue's record as a
# *different* request and hold the retry for the 1h re-request cooldown instead of
# the 5m→3h ladder. Twin of ``Store.AttemptStamp`` on macOS.
STAMP_UNRESOLVED_REVIEW = "unresolved"
STAMP_CONFLICTING = "conflicting"


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
    my_last_comment_at: str | None = None  # my latest top-level comment (ISO8601)

    @property
    def my_last_response_at(self) -> str | None:
        """The most recent time I responded to this PR — a formal review submission
        OR a top-level comment, whichever is later. A clean PR's auto-response is a
        friendly soft-approve *comment* (never a review verdict), so a review-only
        signal misses it and the request reads as forever-owed. ISO8601 strings
        compare chronologically, so ``max`` picks the latest."""
        times = [t for t in (self.my_last_review_at, self.my_last_comment_at) if t]
        return max(times) if times else None

    @property
    def stamp(self) -> str:
        """The ``ReviewAttempt.requested_at`` this request's dispatches are filed
        under — its own request timestamp, or :func:`decide`'s unknown-stamp
        sentinel when GitHub reported none. Read in two places (the monitor when it
        dispatches, the queue when it runs a dispatch the cap deferred), which is
        why it is one expression rather than two."""
        return self.requested_at or "-"

    @property
    def owe_review(self) -> bool:
        """I owe a review when I'm requested and that request is newer than my last
        response to this PR (review or comment). A genuine re-request stamps a newer
        timestamp and re-arms this even after I've responded once."""
        if self.requested_at is None:
            return True
        last = self.my_last_response_at
        if last is None:
            return True
        return self.requested_at > last


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
# each is an independent origin of the same work (szpontnet-spec/docs/12-work-claims.md).
# Every machine scans; the Store routes each auto find through claim-gated DISPATCH
# (`Store._route_via_mesh`): the mesh runs it once, on the best-surplus node, and
# the EXECUTOR holds the work-key claim for its agent's lifetime — so a concurrent
# or repeat scan is suppressed, a crash frees it for a retry, and a node death frees
# it for failover. There is deliberately NO duty-assignment stand-down: it deferred
# to a node that might not be scanning, silently dropping the operator's work.

WORK_REVIEW_REQ = "review"  # reviews requested of me → duty "review"
WORK_REVIEW_REPLY = "review-reply"  # replies to reviews on MY PRs → duty "review"
WORK_CONFLICTS = "conflicts"  # conflict fixes on MY PRs → duty "conflicts"


def _pr_ref(pr_url: str) -> str | None:
    """``<host>/<owner>/<repo>#<n>`` for a PR URL, or None when it isn't one. Split
    out of :func:`work_key` so :func:`ledger_key` cannot parse a URL differently
    from the claim key — the two identify the same unit of work and are compared
    against each other in the telemetry ledger."""
    from urllib.parse import urlparse

    try:
        u = urlparse(pr_url)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    parts = [p for p in (u.path or "").split("/") if p]
    if not host or len(parts) != 4 or parts[2] != "pull" or not parts[3].isdigit():
        return None
    return f"{host}/{parts[0]}/{parts[1]}#{parts[3]}"


def work_key(kind: str, pr_url: str, head_sha: str) -> str:
    """The origination-dedup key for one unit of monitor work — the reference
    convention from szpontnet-spec/docs/12: ``<kind>:<host>/<owner>/<repo>#<n>@<sha>``.

    Derived from the PR's own URL so every node observing the same PR agrees
    byte-for-byte (the Swift twin must produce identical strings — see the parity
    tests). Returns ``""`` — claim gate skipped, the safe pre-claims degradation —
    when the URL doesn't look like a PR URL or the head sha is unknown."""
    if not head_sha:
        return ""
    ref = _pr_ref(pr_url)
    return f"{kind}:{ref}@{head_sha}" if ref else ""


def ledger_key(kind: str, pr_url: str, head_sha: str) -> str:
    """The telemetry ledger's identity for one unit of work.

    The claim key when a head sha is known — the same string, so the two records
    of one job agree — and the same shape WITHOUT ``@sha`` when it isn't.
    :func:`work_key` deliberately returns ``""`` there, because skipping the mesh
    claim is the safe degradation for a *claim*; skipping the ledger entry is not,
    since the work still gets dispatched and would then be missing from every
    figure on the Telemetry screen. The cost of the fallback is that two pushes to
    one PR fold into one ledger task while the sha is unknown, which understates
    the count rather than inventing one.
    """
    ref = _pr_ref(pr_url)
    if ref is None:
        return ""
    return f"{kind}:{ref}@{head_sha}" if head_sha else f"{kind}:{ref}"


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
# - capacity: only auto work is held to the device's automatic-task cap - a
#   human's click is one deliberate agent, not a monitor emptying its queue
#   (dispatch_decide);
# - budget: only auto work is held to what is left of the limits it spends
#   against - a human spending their own last slice is their call (dispatch_decide);
# - mesh: only auto origination is mesh-gated - a human clicking THIS machine's
#   button has already decided placement (dispatch_decide);
# - counters: only a monitor's FIRST dispatch counts as auto-handled work
#   (dispatch_bumps_counter);
# - label: rows a monitor found carry the "Auto · " prefix, retries are surfaced
#   the same way on both (dispatch_label).
#
# Swift twin: AgentDispatchGate in DiplomatCore/Autofix.swift - keep semantics
# byte-equivalent (see the parity tests on both sides).

SOURCE_PANEL = "panel"
SOURCE_AUTO = "auto"

VERDICT_PROCEED = "proceed"
VERDICT_IN_FLIGHT = "in_flight"  # an agent already works this PR - whoever asks
VERDICT_BANNED = "banned"  # prompt-injection ban on the author - whoever asks
VERDICT_STAND_DOWN = "stand_down"  # mesh: another node originates (auto only)
VERDICT_AT_CAPACITY = "at_capacity"  # this device already runs its cap (auto only)
VERDICT_UNAFFORDABLE = "unaffordable"  # not enough rate limit left (auto only)


def dispatch_decide(
    source: str,
    banned: bool,
    agent_on_pr: bool,
    mesh_stands_down: bool,
    at_capacity: bool,
    unaffordable: bool = False,
) -> str:
    """The one decision both interfaces obey, in fixed precedence: ban, then
    in-flight, then (auto only) this device's concurrency cap, then (auto only)
    its rate-limit budget, then (auto only) mesh.

    Capacity outranks mesh so a saturated device never *originates*: the claim
    that routing takes has gossip side effects, and a node holding the claim for
    work it then refuses to start is worse than not asking. It is safe to leave
    the work for a later poll — every machine scans, so on a mesh a peer with room
    picks the same unit up, and off a mesh the reconciler retries it here on the
    next tick (the refusal writes no attempt record, so no backoff engages).

    The budget sits between the two for the same reason and with the same
    consequence: an account with nothing left to spend cannot finish the agent it
    would claim the work for, and holding the job costs nothing but the wait for a
    window to refill or a balance to be topped up. It ranks BELOW capacity only
    because capacity is the measurement already in hand — a saturated device has no
    slot to spend a budget on, so the probe is never worth taking."""
    if banned:
        return VERDICT_BANNED
    if agent_on_pr:
        return VERDICT_IN_FLIGHT
    if source == SOURCE_AUTO and at_capacity:
        return VERDICT_AT_CAPACITY
    if source == SOURCE_AUTO and unaffordable:
        return VERDICT_UNAFFORDABLE
    if source == SOURCE_AUTO and mesh_stands_down:
        return VERDICT_STAND_DOWN
    return VERDICT_PROCEED


# MARK: - The device's automatic-task cap
#
# A poll finds every unit of pending work at once — N conflicted PRs, N reviews
# owed — and, before this cap existed, dispatched all of them in one pass: N
# terminal windows, N `claude` sessions, one machine. The cap is the device's,
# not the monitor's: it bounds how many automatic agents Diplomat has RUNNING
# here, so it holds across the review monitor, the conflict reconciler and work a
# mesh peer routes in (szponthost.DiplomatHost.at_job_capacity, which is what
# the node asks before it spawns).

DEFAULT_AUTO_TASK_LIMIT = 2
MIN_AUTO_TASK_LIMIT = 1
MAX_AUTO_TASK_LIMIT = 16


def clamp_auto_task_limit(value: int) -> int:
    """The configured cap, held inside the range the UI offers. A stored 0 would
    silently stop all automatic work while both monitor toggles still read "on",
    so the floor is 1 — pausing is what those toggles are for."""
    return max(MIN_AUTO_TASK_LIMIT, min(MAX_AUTO_TASK_LIMIT, value))


def running_auto_prs(
    live_prs: set[int], auto_prs: set[int], manual_prs: set[int],
    idle_prs: set[int] | None = None,
) -> set[int]:
    """Which PRs have an automatic agent *working* on this device (one agent per PR
    is what the in-flight dedup guarantees, so a PR *is* an agent here).

    Four inputs, because no single one of them is both complete and attributable:

    - ``live_prs`` — PRs with a live ``claude`` visible in ``ps``. The ground truth,
      and the only evidence that survives an applet restart, but it cannot say who
      started an agent;
    - ``manual_prs`` — PRs whose live agent this applet tracked as a *panel* spawn.
      Subtracted, because a click is the operator's own act and never spends the
      automatic budget;
    - ``auto_prs`` — PRs with a tracked auto agent. Added, because a just-spawned
      agent takes a moment to appear in ``ps`` and would otherwise be counted zero
      times by the very poll that started it.

    - ``idle_prs`` — PRs whose agent has finished its turn and is sitting at its
      prompt (:func:`apiwatch.looks_busy`). Subtracted LAST, from the union, because
      an idle agent is idle however it was found: a tracked ``auto_prs`` entry that
      went quiet has to leave too, or re-adding it here would hold the very slot the
      subtraction is for.

    An agent nobody tracked therefore counts as automatic. That is the safe way to
    be wrong: the cost is deferring auto work behind an untracked agent for as long
    as it runs, where the opposite error is the burst this cap exists to stop.

    Idleness is subtracted rather than merely labelled because an agent is spawned
    into an INTERACTIVE session, which does not exit when its work is done — it waits
    at the prompt for a human who may not come for hours. The cap exists to bound
    concurrent LOAD, and a session waiting on input is spending none; left counted, a
    finished agent holds its slot until someone closes the window, and a machine whose
    every bay is held that way defers automatic work indefinitely while doing nothing.

    Only positive evidence of idleness qualifies (a pane that was read and showed no
    interrupt hint): an agent with no readable pane counts as working, so the failure
    direction stays the deferral, never the burst.

    The set, not just its size, because the panel draws a row per running agent and
    a row needs to say *which* PR it is on (``Store.running_tasks``)."""
    return ((live_prs - manual_prs) | auto_prs) - (idle_prs or set())


def running_auto_tasks(
    live_prs: set[int], auto_prs: set[int], manual_prs: set[int],
    idle_prs: set[int] | None = None,
) -> int:
    """How many automatic agents are working on this device — the number the cap is
    compared against, and the size of :func:`running_auto_prs`."""
    return len(running_auto_prs(live_prs, auto_prs, manual_prs, idle_prs))


# MARK: - The device's spending budget
#
# The cap above bounds how many automatic agents run at once; this bounds whether
# any of them should start at all. A machine can have three empty bays and 4% of
# its 5-hour window left, and spending that on an auto-review is how the operator
# finds the limit gone the next time they sit down to work.
#
# There are two currencies, because there are two ways an agent is paid for. Claude
# Code spends a rate-limit window Anthropic publishes only as a percentage; every
# other runner spends an account billed in money. The arithmetic below is the same
# either way and is written in neither unit — `budget_decide` compares what a
# ceiling has left against what a task needs, and the caller says which currency
# both are in.
#
# What a task costs is a measurement, not a guess: the telemetry ledger prices
# every finished agent — against the window it was spent from (telemetry.summarize
# → `per_task`), or, for a runner billed in money, at what the provider charged for
# the model it ran on (`per_task_usd`). So the question "can we afford one more" has
# a statistical answer — and the one worth asking is about the NEXT task, not about
# the average one. Half of all tasks cost more than the mean, and the distribution
# is right-skewed (most small, a few enormous), so a gate set at the mean would wave
# through the expensive tail every time.
#
# Hence a one-sided upper PREDICTION bound: the cost that one more task will come
# in under, with the configured confidence. That is what `autoBudgetConfidence`
# buys — at 95%, roughly one auto-task in twenty may still overrun what it was
# gated on.
#
# Swift twin: AgentDispatchGate in DiplomatCore/Autofix.swift.

#: Supported confidence levels (percent) and their ONE-SIDED standard-normal
#: quantiles. One-sided because only the upper tail is a budget question: nothing
#: goes wrong when a task turns out cheaper than predicted. (The Telemetry
#: screen's own band is a different statistic — a two-sided interval on the MEAN,
#: z = 1.96 — and the two are not interchangeable.)
BUDGET_CONFIDENCE_Z = {50: 0.0, 80: 0.8416, 90: 1.2816, 95: 1.6449, 99: 2.3263}

DEFAULT_BUDGET_CONFIDENCE = 95
#: Share of a window to keep in hand when the ledger cannot price a task yet.
DEFAULT_BUDGET_FLOOR_PCT = 20.0
#: Dollars to keep in hand for the same reason, on an account billed in money. A
#: floor expressed as a share would mean nothing there: the ceilings are a key's cap
#: and a credit balance, and 20% of a balance is 20% of however much was last topped
#: up rather than any fixed amount of work.
DEFAULT_BUDGET_RESERVE_USD = 1.0
#: The most it can be set to. A percentage has 100 to stop at and money has nothing,
#: so this is a chosen bound rather than a derived one — chosen well past any real
#: setting (it is four times this machine's whole weekly key cap) and shared with the
#: slider that sets it, because a knob whose range and whose clamp disagreed would
#: quietly rewrite a hand-edited file the first time it was touched.
MAX_BUDGET_RESERVE_USD = 100.0
#: A prediction bound needs a spread, and the sample standard deviation of one
#: observation is 0 — which would report a single cheap task as certainty. Below
#: this the ledger has no answer and the floor stands in, however the caller's own
#: minimum is configured.
MIN_BUDGET_SAMPLES = 2

WINDOW_SESSION = "session"  # the 5-hour rate-limit window
WINDOW_WEEK = "week"  # the 7-day one
WINDOW_KEY = "orKey"  # the OpenRouter API key's own spend cap
WINDOW_CREDITS = "orCredits"  # the OpenRouter account's credit balance

#: What a verdict's figures are denominated in. A rate limit is only ever published
#: as a percentage and an OpenRouter account only ever as money, so the unit follows
#: from which ceiling was read and is carried so the feed can say which it meant.
UNIT_PCT = "pct"
UNIT_USD = "usd"


def clamp_budget_confidence(value: int) -> int:
    """The configured confidence, snapped to a level :data:`BUDGET_CONFIDENCE_Z`
    has a quantile for.

    Rounds UP to the next supported level rather than to the nearest, so a
    hand-edited file lands on the stricter of the two neighbours: a value this
    table cannot honour should hold work back, never wave it through on a looser
    bound than was asked for."""
    levels = sorted(BUDGET_CONFIDENCE_Z)
    return next((lvl for lvl in levels if lvl >= value), levels[-1])


def budget_z(confidence: int) -> float:
    """The one-sided normal quantile for a confidence level (percent)."""
    return BUDGET_CONFIDENCE_Z[clamp_budget_confidence(confidence)]


def clamp_budget_floor_pct(value: float) -> float:
    """The configured floor, held to a real share of a window. 0 is allowed and
    means "spend it to the last drop while the ledger is still thin"."""
    if not math.isfinite(value):
        return DEFAULT_BUDGET_FLOOR_PCT
    return max(0.0, min(100.0, value))


def clamp_budget_reserve_usd(value: float) -> float:
    """The configured dollar reserve, held to what the knob can express. 0 is allowed
    and means "spend it to the last cent while the ledger is still thin"."""
    if not math.isfinite(value):
        return DEFAULT_BUDGET_RESERVE_USD
    return max(0.0, min(MAX_BUDGET_RESERVE_USD, value))


def task_cost_bound(mean: float, sd: float, count: int, *,
                    z: float, min_sample: int) -> float | None:
    """What one more auto-task will cost at most, in whatever unit ``mean``/``sd``
    are measured in — the upper end of a one-sided prediction interval,
    ``mean + z·sd·√(1 + 1/n)``.

    The ``√(1 + 1/n)`` is what makes this a bound on the NEXT observation rather
    than on the mean: it carries the spread of the tasks themselves plus the
    uncertainty in where their average sits, and so stops narrowing as the ledger
    fills. (The interval the Telemetry screen draws is the other one, ``z·sd/√n``,
    and converges on the mean — a gate built from it would end up approving the
    average task, which by construction half of them cost more than.)

    None when the ledger cannot answer: fewer finished-and-priced tasks than the
    caller's minimum, or a non-finite figure from an unusable one. The caller then
    falls back to the configured floor."""
    if count < max(MIN_BUDGET_SAMPLES, min_sample):
        return None
    if not (math.isfinite(mean) and math.isfinite(sd) and math.isfinite(z)):
        return None
    return mean + z * sd * math.sqrt(1.0 + 1.0 / count)


@dataclass(frozen=True)
class Budget:
    """Whether what is left of the ceilings this machine spends against covers one
    more auto-task, and the arithmetic that decided it — the numbers the activity
    feed quotes back when work is held."""

    affordable: bool
    #: The ceiling the verdict came from: the one with the LEAST headroom, whether
    #: it refused or not, so the same field explains an approval and a refusal.
    #: Empty when none of them had a reading and nothing was decided.
    window: str = ""
    #: What that ceiling had left, and what a task was required to fit inside — both
    #: in :attr:`unit`, and only ever comparable to each other.
    left: float = 0.0
    needed: float = 0.0
    #: True when ``needed`` was priced from the ledger, False when the telemetry was
    #: too thin and the configured floor stood in for it.
    measured: bool = False
    #: Which currency the two figures are in (:data:`UNIT_PCT`, :data:`UNIT_USD`).
    unit: str = UNIT_PCT


def budget_decide(windows: list[tuple[str, float | None, float | None]],
                  floor: float, unit: str = UNIT_PCT) -> Budget:
    """Can one more automatic task be afforded right now?

    ``windows`` is the ceilings this machine's work is spent against, each as
    ``(name, left, cost-of-one-task)`` in a single unit — percentages of a rate limit
    for a Claude Code machine, dollars for one billed by an OpenRouter account. Every
    one of them gates, because any can be the one that runs out: the 5-hour window is
    what stops work this afternoon and the 7-day window is the ceiling a busy week
    walks into, exactly as a key's cap is what stops work this week and the credit
    balance is what stops it altogether. A task has to fit inside what is left of
    each.

    A ceiling with no cost measurement falls back to ``floor`` — "keep this much in
    hand" — which is the whole of the answer on a machine whose ledger has not priced
    a task yet.

    A ceiling with **no reading at all** is skipped, and a call where none has one is
    affordable. That is deliberate: a usage probe can be switched off
    (``DIPLOMAT_QUOTA_PROBE=0``, ``DIPLOMAT_SPEND_PROBE=0``), logged out, or simply
    offline, and a gate that read silence as "no budget" would take a machine's
    automatic work with it every time the network dropped. The gate exists to spend a
    *measured* limit carefully; with nothing measured it has no opinion, and the task
    cap is still in front of it.

    Ties go to the ceiling listed first, so a caller decides which of two equally
    binding ones it would rather name.
    """
    tightest: Budget | None = None
    for window, left, cost in windows:
        if left is None:
            continue
        needed = floor if cost is None else cost
        if tightest is not None and left - needed >= tightest.left - tightest.needed:
            continue  # an earlier ceiling is the binding one
        tightest = Budget(affordable=left >= needed, window=window, left=left,
                          needed=needed, measured=cost is not None, unit=unit)
    return tightest if tightest is not None else Budget(affordable=True, unit=unit)


def dispatch_label(source: str, core: str, attempt: int = 1,
                   requested: bool = False) -> str:
    """The activity/session label both interfaces produce: same core, the source
    prefix and retry suffix applied identically everywhere.

    ``requested`` drops the prefix for work the operator asked for by name and the
    queue merely chose the moment for — a review from a PR sweep. Such a job is
    dispatched as ``SOURCE_AUTO`` in every other respect (it waits for the cap, it
    holds a bay while it runs), but "Auto · " answers *who decided there was work
    here*, and for this one that was the operator. Without it a requested review of
    #12 and the review-reply monitor's own dispatch on #12 read as the same row."""
    retry = f" · retry {attempt}" if attempt > 1 else ""
    prefix = "Auto · " if source == SOURCE_AUTO and not requested else ""
    return f"{prefix}{core}{retry}"


def dispatch_bumps_counter(source: str, attempt: int) -> bool:
    """Auto-handled counters bump only on a monitor's first dispatch - a retry is
    not new work handled, and a manual run is the user's own action."""
    return source == SOURCE_AUTO and attempt == 1


# MARK: - The queue behind the cap (mirrors AgentTasks.swift)
#
# The panel answers one question — what is this machine doing about my PRs — with
# one list, so the automatic work its cap is HOLDING and the slots of that cap with
# nothing in them are rows of the same list. Both the order those rows are shown in
# and the order the queue is drained in are decided here, pure: the sequence the
# operator reads off the panel and the sequence the monitor actually runs are then
# the same rules, not two implementations of an intention.
#
# Most of the queue is a *view* of what the monitors would re-offer, not a second
# copy of their state: the cap defers work by writing no attempt record, so every
# poll re-offers everything GitHub still owes and the list is rebuilt from that. Only
# the operator's arrangement of it is remembered, because that is the one thing a
# poll cannot reconstruct.
#
# The exception is the reviews the operator asks for by sweeping their PRs
# (QUEUE_REQUESTED_ACTION). GitHub has nothing to re-offer them from — a PR does not
# record that someone wanted it reviewed — so that ask is the front-end's own list
# (``Store.requested_reviews``), and it is the front-end that offers one task per PR
# on each poll until each is dispatched or its PR leaves the open state.


def queue_key(audit_action: str, pr_number: int) -> str:
    """A queued task's identity, stable across polls and applet restarts: the
    monitor's verb plus the PR. Not the mesh work key — that one is scoped to a head
    sha, so a push during the wait would read as a different task and lose the
    operator's place for it.

    The verb is part of the key because a PR can owe two different monitors at once
    (a conflict *and* an unaddressed review); they are two tasks, and the one that
    dispatches first makes the other read as in-flight rather than overwriting it."""
    return f"{audit_action}:{pr_number}"


# The verb a review the operator asked for is queued under — the same one a
# Review-PRs spawn writes to the activity feed, because it is that spawn, split into
# one task per PR.
QUEUE_REQUESTED_ACTION = "review"

# The verbs whose work waits behind the rest, nearest-first — a requested review, then
# a conflict fix, which waits behind everything. Matched off the queue key rather than
# the job, because the operator's saved arrangement is a list of keys and has to be
# banded the same way after a restart, with no job to consult (`queue_order`).
QUEUE_LAST_ACTIONS = (QUEUE_REQUESTED_ACTION, "conflicts")


def queue_band(key: str) -> int:
    """Which band of the queue a task waits in: 0 for the monitors' own finds, 1 for a
    review the operator asked for, 2 for a conflict fix. Bands outrank the operator's
    arrangement; within one, the arrangement decides.

    A monitor's find is first because it is answering something GitHub is already owed
    — a review requested of me, a thread on my PR waiting on a reply — and that debt is
    visible to other people. A requested review is a sweep the operator started when
    they had the time for it; it is worth the whole cap eventually, but not ahead of
    the work the repository is waiting on. Sweeping fifty drafts otherwise buries every
    review request behind them for a day.

    Resolving a conflict stays last: it is the one unit of work that another agent's
    run routinely makes unnecessary — a review-reply agent works the same branch and
    lands its own merge on the way, and a review of someone else's PR can leave this
    one behind a rebase. Run first, a conflict fix spends a bay of the cap on the state
    of the branch as it was BEFORE the work in front of it landed — and often on a
    conflict that no longer exists by the time it opens the diff. It is also the
    cheapest to re-derive: the reconciler re-offers it every poll for as long as
    GitHub still calls the PR conflicting, so a fix deferred is never a fix lost."""
    verb, _, _ = key.partition(":")
    return QUEUE_LAST_ACTIONS.index(verb) + 1 if verb in QUEUE_LAST_ACTIONS else 0


def still_owed(audit_action: str, pr_number: int, conflicting: set[int],
               owing_reply: set[int], closed: set[int]) -> bool:
    """Does the evidence of THIS poll still owe a task the queue is holding?

    A queued task carries the prompt and the verdict of the poll that staged it, which
    can be a whole poll period old by the time a slot frees — and in that gap the
    agent ahead of it in the queue was working the very branch it is about to open. So
    the drain asks again before it spends a bay: a conflict fix on a PR GitHub no
    longer calls conflicting, or a reply on a PR whose threads are answered, is work
    somebody already did.

    A PR that has left the open state retires every verb, the operator's own ask
    included: merged or closed, there is no branch left to fix and a review lands on a
    diff nobody will open again. ``closed`` is positive evidence — the PRs this cycle
    SAW closed — so a PR missing from it reads as open and its row stands, which is
    the safe direction for the one answer that also forgets the ask behind the row.

    While the PR is open, only the two verbs ``conflicting``/``owing_reply`` come from
    are answerable — both are jobs on MY PRs, and ``snaps`` is the fetch of exactly
    those. A review requested of me lives in the other fetch, and nothing on this
    machine retires it: it is owed until I review it, which is what the agent is for.
    A review the operator asked for is owed for the same reason, by their word rather
    than GitHub's. Unanswerable is not stale, so it stands."""
    if pr_number in closed:
        return False
    if audit_action == "conflicts":
        return pr_number in conflicting
    if audit_action == "review-reply":
        return pr_number in owing_reply
    return True


def free_slots(limit: int, running: int) -> int:
    """Slots of the device's automatic-task cap with nothing running in them — the
    empty bays the panel draws under the queue.

    Clamped at zero because ``running`` can legitimately exceed the cap: it counts
    agents this device did not necessarily start (an untracked ``claude`` in ``ps``
    counts as automatic), and lowering the cap while agents run leaves them running.
    Both would otherwise render as a negative number of free slots."""
    return max(0, limit - running)


def queue_order(offered: list[str], saved: list[str]) -> list[str]:
    """The queue for this poll: everything still offered, in the order the operator
    last dragged it into, with tasks they have never arranged appended in the order
    the monitors found them.

    Keys that are no longer offered fall out — the work was taken by an agent,
    resolved, or its author banned — because a queue that outlived its evidence
    would hand "execute now" a task GitHub no longer owes. (Not a mesh claim: the
    cap outranks the mesh gate, so a device with anything queued is by definition
    one that never asked a peer. Peer-owned work leaves the queue when the drain
    reaches it and the mesh answers.)

    Requested reviews and then conflict fixes fall to the back whatever order they
    were found in (:func:`queue_band`). The monitors find their work mid-cycle — the
    conflict reconciler runs before the review-request fetch even begins — so without
    the bands a poll's own sequence would decide, and a sweep of fifty drafts offered
    first would hold up every review GitHub is waiting on."""
    live = set(offered)
    out: list[str] = []
    seen: set[str] = set()
    for key in saved:
        if key in live and key not in seen:
            out.append(key)
            seen.add(key)
    for key in offered:
        if key not in seen:
            out.append(key)
            seen.add(key)
    # Stable, so the band is the only thing this re-orders: everything above keeps
    # its place within the band it lands in.
    return sorted(out, key=queue_band)


def queue_reorder(order: list[str], moving: str, onto: str) -> list[str]:
    """One drag: ``moving`` lands where it was dropped relative to ``onto`` — after
    it when it came from above, before it when it came from below.

    Both directions are needed for every position to be reachable. An "always insert
    before the row you dropped on" rule can never move a task to the end of the
    queue, which is exactly the arrangement someone reaches for first (this one is
    not urgent — run it last).

    A drag onto a key that is not in the queue, onto itself, or into another band is
    not a rearrangement and leaves the order alone. The last of those is the same
    answer as the first two rather than a partial move, because a conflict fix dragged
    above a review would be re-banded on the next poll and snap back: a drag that
    cannot survive one poll is better refused than shown landing."""
    if moving == onto or moving not in order or onto not in order:
        return order
    if queue_band(moving) != queue_band(onto):
        return order
    out = list(order)
    from_i, to_i = out.index(moving), out.index(onto)
    out.remove(moving)
    anchor = out.index(onto)
    out.insert(anchor + 1 if from_i < to_i else anchor, moving)
    return out


@dataclass(frozen=True)
class AgentJob:
    """One agent job, whoever triggers it. The trigger supplies WHAT to run
    (config -> prompt, labels, PR identity); the store's ``dispatch_agent`` owns
    everything that HAPPENS - ban check, in-flight dedup, mesh policy, spawn,
    registration, counters. Twin of Store.AgentJob on macOS."""

    kind: str  # activity tint: "review" | "issues" | "conflicts" | "audit"
    audit_action: str  # activity-feed verb
    label: str  # label core (source prefix / retry suffix added by dispatch_label)
    prompt: str
    pr_url: str | None = None  # None = not PR-scoped -> no PR dedup possible
    pr_number: int | None = None
    author_login: str | None = None  # whose PR we'd review - the ban dimension
    duty: str = ""  # mesh duty, for auto-origination gating
    work_key: str = ""  # mesh claim key ("" = no claim)
    # Telemetry ledger identity - the claim key, or its sha-less shape when the
    # head sha is unknown (see ledger_key). Set for auto jobs only: the screen
    # measures the MONITORS, and a wizard click is the operator's own doing.
    ledger_key: str = ""
    counter: str | None = None  # "review_requests" | "my_reviews" | "conflicts"
    # The stamp the monitor that owns this job records against the PR when an agent
    # launches (``ReviewAttempt.requested_at``) — the request timestamp for a review
    # request, one of the STAMP_* constants for the two level-triggered reconcilers.
    # Carried on the job so a dispatch the *queue* runs later starts the same retry
    # backoff the reconciler's own dispatch would have. Read only on that path: a
    # panel spawn keeps no attempt record, and a job with no monitor behind it (the
    # sweeps) has no stamp to carry.
    attempt_stamp: str = ""

    @property
    def requested(self) -> bool:
        """Whether the operator asked for this exact unit of work, as opposed to a
        monitor having found it. Read off the verb, which already distinguishes them:
        the monitors dispatch under ``review-req`` and ``review-reply``, and a plain
        ``review`` is a Review-PRs spawn — a click, or one PR of the sweep a click
        queued. It decides the label (:func:`dispatch_label`) and, in the front-end,
        which queued rows can be cancelled."""
        return self.audit_action == QUEUE_REQUESTED_ACTION


@dataclass(frozen=True)
class QueuedTask:
    """One unit of automatic work nothing has started yet: the whole job, held by the
    device's task cap, by the rate-limit budget, or by a switch the operator set (its
    own monitor, or the queue itself), until a slot frees or the operator runs it.
    Rebuilt from live evidence on each poll — see ``Store.queued_tasks``. Twin of
    Store.QueuedAgentTask on macOS."""

    # :func:`queue_key` — stable across polls and applet restarts, which is what
    # lets the operator's drag order outlive the list itself.
    id: str
    job: AgentJob
    # The attempt number the monitor would have dispatched under, so a queued retry
    # keeps its place on the 5m→3h backoff ladder instead of restarting it.
    attempt: int


@dataclass(frozen=True)
class RunningAgent:
    """One automatic agent up on this device right now — a slot of the cap with
    something in it, and the row the panel draws where a free bay would be.

    Not a session: this front-end spawns a detached ``Popen`` and keeps no window
    handle, so what a row can say is what the *bookkeeping* knows, and how much of
    that there is differs per agent. A tracked one carries the label its dispatch
    logged and the moment it started; an agent found only in ``ps`` (``tracked`` is
    False) has neither — an applet restart loses the book while the agents run on —
    and is drawn by its PR alone, which is the whole of what ``ps`` yields.
    """

    pr_number: int
    # The label the activity feed and the queued row carried — already through
    # dispatch_label, so a retry keeps its "retry N". Empty for an untracked agent.
    label: str
    # AgentJob.kind, for the row's glyph and tint. Empty for an untracked agent,
    # which takes whatever look an unrecognised kind gets.
    kind: str
    tracked: bool
    started_at: float = 0.0  # epoch seconds; 0 when untracked (nothing to date it by)
    mesh: bool = False  # the mesh placed this job somewhere other than a local spawn
    # What the run resolved to this tick (`agentstate.RunState`) and the one fact that
    # decided it. The reason is on the row because the states this list can now show
    # include "unknown", and a row that says only "unknown" invites exactly the
    # guesswork this whole mechanism replaced.
    state: str = "running"
    reason: str = ""

    @property
    def awaiting_input(self) -> bool:
        """The session finished its turn and sits at its prompt. Such a row keeps its
        place in the list — it is the one thing on screen that says why a window is
        still open — but it has given its bay back, so a free slot is drawn beside
        it."""
        return self.state == "awaiting_input"


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
    return {pr for _tty, pr in agent_lines(ps_output, owner, repo)}


def agent_ttys(ps_output: str, owner: str, repo: str) -> set[str]:
    """The ttys the live agents of :func:`live_pr_numbers` are running on, spelled as
    ``ps`` spells them (``pts/13``, no ``/dev/``).

    Which panes are worth capturing, and nothing more: reading every tmux pane to
    find two agents would put a ``capture-pane`` per pane on the panel's 8-second
    tick, where this puts one per agent. A process with no controlling tty appears
    as ``?``, matches no pane, and is simply never asked about."""
    return {tty for tty, _pr in agent_lines(ps_output, owner, repo)}


def idle_pr_numbers(
    ps_output: str, pane_tails: dict[str, str], owner: str, repo: str
) -> set[int]:
    """Of the agents alive in ``ps``, the PR numbers whose session has finished its
    turn and is waiting at its prompt.

    ``ps_output`` must be a ``tty=,args=`` dump (``live_pr_numbers`` reads the same
    lines — the regex finds the prompt wherever on the line it sits — so one scan
    answers both). ``pane_tails`` maps a tty to that pane's visible buffer, spelled
    as ``ps`` spells a tty (:func:`tmuxwatch.pane_tails_for_ttys`).

    The tty is the join: an agent runs inside a tmux pane (``review.agent_argv``), so
    the pane on the same tty as the ``claude`` process IS that agent's screen, and
    :func:`apiwatch.looks_busy` reads the CLI's own status bar off it.

    An agent whose tty has no pane is absent from the result, never idle in it —
    a session outside tmux, a capture that failed, a pane that closed between the two
    reads. Each is missing evidence, and this answer only ever REMOVES an agent from
    the cap's count, so silence has to mean "still working".
    """
    from . import apiwatch

    out: set[int] = set()
    for tty, pr in agent_lines(ps_output, owner, repo):
        tail = pane_tails.get(tty)
        if tail is not None and not apiwatch.looks_busy(tail):
            out.add(pr)
    return out


def agent_lines(ps_output: str, owner: str, repo: str):
    """Yield ``(tty, pr_number)`` for every live agent in a ``ps`` dump — the one
    parse the answers above are each a projection of, so they can never come to
    disagree about what counts as an agent.

    The tty is whatever leads the line, normalised free of ``/dev/`` (``ps`` omits it,
    tmux does not). On a bare ``args=`` dump — which the argv scan predates the tty by
    and must keep working on — that first token is the start of the command instead,
    and so simply matches no pane: a garbage tty can only ever fail to find evidence,
    never manufacture it.
    """
    import re

    from . import runner

    pat = re.compile(_LIVE_AGENT_RE_TMPL.format(repo=re.escape(f"{owner}/{repo}")))
    for line in ps_output.splitlines():
        if not runner.is_agent_line(line):
            continue
        tty, _, _rest = line.strip().partition(" ")
        tty = tty.removeprefix("/dev/")
        for m in pat.finditer(line):
            yield tty, int(m.group(1))
