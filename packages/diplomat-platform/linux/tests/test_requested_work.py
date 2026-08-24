"""The work a sweep asks for: fan-out, persistence, and what retires it.

Pressing SPAWN on a whose-PRs scope queues one review per PR, and on a Fix-issues
scope one fix per issue, rather than handing ONE agent every item at once — fifty
reviews in one session on a repo with fifty drafts. That puts them through the
machinery the monitors' own work already goes through: the task cap starts them a bay
at a time, the panel draws and reorders them, "execute now" jumps one ahead.

What is theirs alone is that they are the only queued tasks the applet has to
REMEMBER. Every other row is re-derived from GitHub each poll, and neither a PR nor an
issue records anything about somebody having swept it — so the ask is stored,
re-offered until it is dispatched, and dropped when something answers for it. That
cycle is most of what is tested here.

The two kinds share every one of those mechanics and one namespace, which is the other
thing tested here: a fix is keyed in the ISSUE number space, so it must neither collide
with the review of the PR that shares its number nor be retired by what happens to it.

The store fixture, the spawn recorder and the evidence stub come from
``test_autofix``: this is the same Store on the same machinery, asked a different
question.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from test_autofix import _spawn_recorder, fake_probes, store  # noqa: F401 — fixture

from diplomat_app import bans, issues
from diplomat_app.store import Store
from diplomat_runtime import autofix, review
from diplomat_runtime.models import OpenIssue, OpenPR
from diplomat_runtime.prtarget import PRTarget

pytest.importorskip("PySide6")

NOW = datetime.now(timezone.utc)

#: PR numbers of agents this applet has no record of — enough of them to hold every
#: bay of the default cap, so a poll's drain starts nothing and what the queue holds
#: afterwards is what the ask put there.
CAP_FULL = {90, 91}


def _pr(number: int, *, author: str = "alice", draft: bool = True) -> OpenPR:
    return OpenPR(number, f"PR {number}", f"https://github.com/o/r/pull/{number}",
                  draft, author, NOW, None, [], None, [])


def _issue(number: int, *, author: str = "carol", assignees=()) -> OpenIssue:
    return OpenIssue(number, f"Issue {number}",
                     f"https://github.com/o/r/issues/{number}", author, "CONTRIBUTOR",
                     NOW, NOW, 0, list(assignees), [], False)


@pytest.fixture
def swept_store(store):  # noqa: F811 — the shared Store fixture
    """The store with a panel fetch behind it: two drafts of mine, a ready PR of mine
    and a draft of somebody else's; four open issues, one of them claimed already.

    Issue #2 shares its number with a PR on purpose — the two number spaces overlap in
    every repo, and what the queue does about that is tested below."""
    store.has_loaded = True
    store.prs = [_pr(1), _pr(2), _pr(3, draft=False), _pr(4, author="bob")]
    store.issues = [_issue(2), _issue(31), _issue(41, author="dana"),
                    _issue(42, assignees=["eve"])]
    return store


def _sweep(store, **kwargs) -> review.ReviewConfig:  # noqa: F811
    cfg = dict(target=PRTarget.MINE, me=store.me, depth="deep",
               include_drafts=True, include_ready=False)
    cfg.update(kwargs)
    return review.ReviewConfig(**cfg)


def _issue_sweep(store, **kwargs) -> issues.IssueConfig:  # noqa: F811
    cfg = dict(target=issues.Target.CONTRIBUTORS, me=store.me, depth="deep",
               unassigned_only=True)
    cfg.update(kwargs)
    return issues.IssueConfig(**cfg)


def _poll(store, monkeypatch, closed=()) -> None:  # noqa: F811
    """One monitor cycle with both PR fetches empty, so what the queue holds afterwards
    is what the ask put there. ``closed`` is the third read — the PRs this cycle finds
    merged or closed."""
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_review_requests",
                        lambda *a, **k: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_closed_prs",
                        lambda *a, **k: set(closed))
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    store._autofix_poll_once()


# ---- the fan-out ----------------------------------------------------------


def test_a_sweep_queues_one_review_per_pr_instead_of_one_agent_for_all(swept_store):
    """Each PR becomes a task of its own, keyed by PR, so the cap starts them a few at
    a time and the panel can hold, reorder and cancel them."""
    queued, already = swept_store.request_review_sweep(_sweep(swept_store))

    assert (queued, already) == (2, 0)  # #3 is ready-for-review, #4 is bob's
    assert [t.id for t in swept_store.queued_tasks] == ["review:1", "review:2"]
    assert [t.job.pr_number for t in swept_store.queued_tasks] == [1, 2]
    assert [t.job.pr_url for t in swept_store.queued_tasks] == [
        "https://github.com/o/r/pull/1", "https://github.com/o/r/pull/2",
    ]


def test_each_queued_review_is_scoped_to_its_own_pr(swept_store):
    """The prompt is the single-PR review the wizard would have built for that PR,
    not the sweep's — a queued task that carried the sweep prompt would be the same
    fifty-reviews-at-once agent, dispatched fifty times over."""
    swept_store.request_review_sweep(_sweep(swept_store))

    assert [t.job.prompt for t in swept_store.queued_tasks] == [
        "PROMPT:review:1", "PROMPT:review:2",
    ]


def test_a_queued_review_belongs_to_no_monitor(swept_store):
    """No counter, so no auto-handled figure counts it and neither toggle pauses it;
    no ledger key, because the Telemetry screen measures the monitors; no work key, so
    it is never claimed as mesh origination."""
    swept_store.request_review_sweep(_sweep(swept_store))
    job = swept_store.queued_tasks[0].job

    assert (job.counter, job.ledger_key, job.work_key) == (None, "", "")
    assert (job.kind, job.duty, job.audit_action) == ("review", "review", "review")
    assert not swept_store.is_paused(job.counter)
    assert job.requested


def test_a_sweep_of_someone_elses_prs_carries_the_author_for_the_ban_check(swept_store):
    swept_store.request_review_sweep(
        _sweep(swept_store, target=PRTarget.SOMEONE, username="bob", include_ready=True))

    assert [(t.id, t.job.author_login) for t in swept_store.queued_tasks] == [
        ("review:4", "bob")
    ]


def test_a_sweep_of_my_own_prs_claims_no_author(swept_store):
    """My own PRs have no ban dimension — a login here would check the ban list
    against myself."""
    swept_store.request_review_sweep(_sweep(swept_store))

    assert [t.job.author_login for t in swept_store.queued_tasks] == [None, None]


def test_sweeping_twice_asks_once(swept_store):
    """The queue is keyed by PR, so a second ask would be one row that dispatches
    twice — and the second dispatch would find the first agent still on the PR. A
    press repeated, or two sweeps whose scopes overlap, must be idempotent."""
    swept_store.request_review_sweep(_sweep(swept_store))
    queued, already = swept_store.request_review_sweep(
        _sweep(swept_store, include_ready=True))

    assert (queued, already) == (1, 2)  # only the ready PR is new
    assert [t.id for t in swept_store.queued_tasks] == ["review:1", "review:2", "review:3"]


def test_two_sweeps_that_overlap_in_time_ask_once(swept_store, monkeypatch):
    """The same idempotence, for two fan-outs running at once rather than one after
    the other.

    Each assembles a prompt per PR between reading what is already asked for and
    storing what it adds, and that half is a core subprocess apiece — long enough for
    the other press to have stored its asks in the meantime. Deciding what is new from
    the set read before that gap stores those PRs twice."""
    store = swept_store
    at_assembly = threading.Barrier(2, timeout=10)
    build = store._requested_task
    started: set[int] = set()

    def gated(entry):
        # Hold each fan-out inside its assembly window until both are in it.
        ident = threading.get_ident()
        if ident not in started:
            started.add(ident)
            at_assembly.wait()
        return build(entry)

    monkeypatch.setattr(store, "_requested_task", gated)
    presses: list[tuple[int, int]] = [(-1, -1), (-1, -1)]

    def press(slot):
        presses[slot] = store.request_review_sweep(_sweep(store))

    threads = [threading.Thread(target=press, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert [r.number for r in store.requested_work] == [1, 2]
    assert [t.id for t in store.queued_tasks] == ["review:1", "review:2"]
    # …and the press that lost the race says so, rather than counting the same two
    # reviews a second time at the operator.
    assert sorted(presses) == [(0, 2), (2, 0)]


def test_a_sweep_that_cannot_build_a_prompt_leaves_nothing_behind(swept_store, monkeypatch):
    """A prompt is assembled by a subprocess, so a press can fail halfway through the
    PRs. Two things must survive that: the asks are not stored (each outlives the
    press, so one carrying an unbuildable prompt is one every later poll trips over),
    and the prompts already built for it are not kept — the operator's next press is a
    different sweep, and a PR that answered to the first one would be running the
    depth they just moved away from."""
    def flaky(cfg):
        if cfg["specificPR"] == "2" and cfg["depth"] == "quick":
            raise RuntimeError("diplomat-core timed out assembling the prompt")
        return f"PROMPT:{cfg['specificPR']}:{cfg['depth']}"

    monkeypatch.setattr("diplomat_runtime.promptcore.build_prompt", flaky)

    with pytest.raises(RuntimeError):
        swept_store.request_review_sweep(_sweep(swept_store, depth="quick"))
    assert swept_store.requested_work == []
    assert swept_store.queued_tasks == []

    swept_store.request_review_sweep(_sweep(swept_store, depth="max"))

    assert [t.job.prompt for t in swept_store.queued_tasks] == ["PROMPT:1:max",
                                                               "PROMPT:2:max"]
    assert [t.job.label for t in swept_store.queued_tasks] == ["Review · #1 · max",
                                                              "Review · #2 · max"]


def test_a_sweep_with_no_pr_in_scope_queues_nothing(swept_store):
    swept_store.prs = [_pr(9, author="carol")]

    assert swept_store.request_review_sweep(_sweep(swept_store)) == (0, 0)
    assert swept_store.queued_tasks == []
    assert swept_store.requested_work == []


# ---- the same fan-out, for issues -----------------------------------------


def test_a_sweep_queues_one_fix_per_issue_instead_of_one_agent_for_all(swept_store):
    """The Fix-issues twin of the fan-out above, and the reason it exists: one agent
    handed a whole scope works a repo's worth of issues in a single session, with no
    cap on it, no order to it and nothing to cancel."""
    queued, already = swept_store.request_issue_sweep(_issue_sweep(swept_store))

    assert (queued, already) == (3, 0)  # #42 is claimed already
    assert [t.id for t in swept_store.queued_tasks] == ["issues:2", "issues:31",
                                                        "issues:41"]
    assert [t.job.label for t in swept_store.queued_tasks] == [
        "Issues · #2 · deep", "Issues · #31 · deep", "Issues · #41 · deep",
    ]


def test_each_queued_fix_is_scoped_to_its_own_issue(swept_store):
    """The prompt is the one-issue fix the wizard would have built for that issue, not
    the scope's — a queued task carrying the scope prompt would be the same
    every-issue-at-once agent, dispatched once per issue."""
    swept_store.request_issue_sweep(_issue_sweep(swept_store))

    assert [t.job.prompt for t in swept_store.queued_tasks] == [
        "PROMPT:issues:2", "PROMPT:issues:31", "PROMPT:issues:41",
    ]


def test_the_scope_decides_which_issues_are_queued(swept_store):
    """The scope selector never reaches an agent any more — it is read here, against
    the panel's own fetch, and all it does is decide what gets queued."""
    assert swept_store.request_issue_sweep(
        _issue_sweep(swept_store, target=issues.Target.SOMEONE, username="dana")) == (1, 0)
    assert [t.id for t in swept_store.queued_tasks] == ["issues:41"]


def test_the_unassigned_filter_is_applied_before_anything_is_queued(swept_store):
    """Turned off, the issue somebody already holds is queued like the rest; on, it
    never becomes a row at all rather than becoming one that stops on arrival."""
    swept_store.request_issue_sweep(_issue_sweep(swept_store, unassigned_only=False))

    assert [t.id for t in swept_store.queued_tasks] == ["issues:2", "issues:31",
                                                        "issues:41", "issues:42"]


def test_a_queued_fix_is_not_pr_scoped(swept_store):
    """The dedup key of the pipeline is a PR URL, so an issue number in it would
    collide with the PR that shares it. What keeps two agents off one issue is the
    GitHub assignee claim, which every machine can see."""
    swept_store.request_issue_sweep(_issue_sweep(swept_store))
    job = swept_store.queued_tasks[0].job

    assert (job.pr_number, job.pr_url) == (None, None)
    assert (job.kind, job.duty, job.audit_action) == ("issues", "issues", "issues")
    assert (job.counter, job.ledger_key, job.work_key) == (None, "", "")
    assert job.requested


def test_someone_elses_issues_carry_their_author_for_the_ban_check(swept_store):
    """The ban dimension is per ISSUE, not per press: the scope that names a person is
    the one the wizard warns about, and each ask it queues carries the login that
    filed its own issue."""
    swept_store.request_issue_sweep(
        _issue_sweep(swept_store, target=issues.Target.SOMEONE, username="dana"),
    )
    assert [(t.id, t.job.author_login) for t in swept_store.queued_tasks] == [
        ("issues:41", "dana")
    ]


def test_a_sweep_across_an_association_claims_no_author(swept_store):
    """"Everything the community filed" names nobody, so there is nobody to ban-check
    — the same answer the whose-PRs sweep gives for my own PRs."""
    swept_store.request_issue_sweep(_issue_sweep(swept_store))

    assert {t.job.author_login for t in swept_store.queued_tasks} == {None}


def test_sweeping_the_same_issues_twice_asks_once(swept_store):
    """Idempotent for the same reason a repeated PR sweep is: the queue is keyed by
    the ask, so a second one would be a row that dispatches twice."""
    swept_store.request_issue_sweep(_issue_sweep(swept_store))
    queued, already = swept_store.request_issue_sweep(
        _issue_sweep(swept_store, unassigned_only=False))

    assert (queued, already) == (1, 3)  # only the claimed issue is new
    assert [t.id for t in swept_store.queued_tasks] == ["issues:2", "issues:31",
                                                        "issues:41", "issues:42"]


def test_a_fix_and_a_review_of_the_same_number_are_two_rows(swept_store):
    """Issue #2 and PR #2 are different work on different things. The queue key
    carries the verb, so they are two rows — and cancelling one leaves the other."""
    swept_store.request_review_sweep(_sweep(swept_store))
    swept_store.request_issue_sweep(_issue_sweep(swept_store))

    assert {t.id for t in swept_store.queued_tasks} == {
        "review:1", "review:2", "issues:2", "issues:31", "issues:41",
    }

    swept_store.cancel_requested_work("issues:2")

    assert [t.id for t in swept_store.queued_tasks] == ["review:1", "review:2",
                                                        "issues:31", "issues:41"]
    assert [(r.action, r.number) for r in swept_store.requested_work] == [
        ("review", 1), ("review", 2), ("issues", 31), ("issues", 41),
    ]


def test_a_closed_pr_does_not_retire_the_issue_that_shares_its_number(swept_store,
                                                                     monkeypatch):
    """The drain prices every row against the PRs that closed this cycle. An issue ask
    is numbered in the issue space, so pricing it there would retire the fix for issue
    #2 because PR #2 merged — work nobody did, dropped silently."""
    fake_probes(monkeypatch, live_prs=CAP_FULL)  # no bay, so nothing starts either way
    swept_store.request_review_sweep(_sweep(swept_store))
    swept_store.request_issue_sweep(_issue_sweep(swept_store))

    _poll(swept_store, monkeypatch, closed=[2])

    assert [t.id for t in swept_store.queued_tasks] == [
        "review:1", "issues:2", "issues:31", "issues:41",
    ]
    assert [(r.action, r.number) for r in swept_store.requested_work] == [
        ("review", 1), ("issues", 2), ("issues", 31), ("issues", 41),
    ]


def test_a_queued_fix_runs_when_a_bay_frees(swept_store, monkeypatch):
    """The row the drain skips over on the retirement pass is still one it starts: a
    fix waits for the cap and spends a bay exactly like a review does."""
    calls = _spawn_recorder(monkeypatch)
    swept_store.request_issue_sweep(_issue_sweep(swept_store))

    _poll(swept_store, monkeypatch)

    assert [c["prompt"] for c in calls] == ["PROMPT:issues:2", "PROMPT:issues:31"]
    assert [t.id for t in swept_store.queued_tasks] == ["issues:41"]
    assert [(r.action, r.number) for r in swept_store.requested_work] == [("issues", 41)]


# ---- what remembers them, and what forgets them ---------------------------


def test_the_ask_outlives_the_process_that_took_it(swept_store):
    """The one thing here GitHub cannot re-derive. A fifty-PR sweep runs for hours,
    which is exactly long enough to be interrupted, and a restart that dropped the
    rest of it would do so silently."""
    swept_store.request_review_sweep(_sweep(swept_store))

    reopened = Store()
    assert [r.number for r in reopened.requested_work] == [1, 2]
    assert reopened.requested_work[0].config["specificPR"] == "1"


def test_a_fix_outlives_the_process_too_and_remembers_it_is_one(swept_store):
    """Both kinds share one stored list, so what comes back has to say which it is:
    read as a review, an issue ask would key itself in the PR space and hand the
    number to a PR-scoped job."""
    swept_store.request_issue_sweep(_issue_sweep(swept_store))

    reopened = Store()
    assert [(r.action, r.number) for r in reopened.requested_work] == [
        ("issues", 2), ("issues", 31), ("issues", 41),
    ]
    assert reopened.requested_work[0].config["specificIssue"] == "2"
    assert reopened.requested_work[0].key == "issues:2"


def test_a_review_asked_for_by_an_older_build_still_reads_back(swept_store):
    """The list is stored under the key the review-only build wrote, so an applet
    updated mid-sweep finds the asks it took before the update. Those rows name no
    action and no ``number`` — read as nothing, a fifty-PR sweep would vanish on the
    upgrade with no row left to cancel by."""
    swept_store._settings.setValue(
        "requestedReviews",
        '[{"pr": 7, "url": "https://github.com/o/r/pull/7", "author": "bob",'
        ' "config": {"kind": "review", "specificPR": "7", "depth": "deep"}}]',
    )

    [ask] = swept_store.requested_work
    assert (ask.action, ask.number, ask.key) == ("review", 7, "review:7")
    assert ask.label == "Review · #7 · deep"


def test_a_stored_row_that_makes_no_sense_is_dropped_not_raised(swept_store):
    """Read back off disk, so it can be part-written or hand-edited. One unusable row
    must cost that row, not the whole queue."""
    swept_store._settings.setValue(
        "requestedReviews",
        '[{"pr": 1, "config": {"kind": "review"}}, {"pr": "x"}, "nonsense", {}]',
    )
    assert [r.number for r in swept_store.requested_work] == [1]

    swept_store._settings.setValue("requestedReviews", "{not json")
    assert swept_store.requested_work == []


def test_every_poll_re_offers_an_ask_nothing_has_started(swept_store, monkeypatch):
    """A monitor re-offers its work by finding it on GitHub again. Nothing on GitHub
    says a PR was swept, so what re-offers these is the ask itself — and a commit
    rebuilds the published queue from this cycle's offers alone, so an ask missing
    from them is an ask that vanishes off the panel."""
    fake_probes(monkeypatch, live_prs=CAP_FULL)
    swept_store.request_review_sweep(_sweep(swept_store))

    _poll(swept_store, monkeypatch)

    assert [t.id for t in swept_store.queued_tasks] == ["review:1", "review:2"]


def test_an_ask_whose_prompt_will_not_assemble_costs_only_its_own_row(swept_store,
                                                                     monkeypatch):
    """The press builds every prompt before storing anything, but the memo holding
    those results does not survive a restart — so the core binary is met again in the
    poll, once per standing ask. A raise there ends the cycle, and a cycle that ends
    early commits nothing: the panel loses the monitors' finds too, and the ask that
    caused it has no row left to cancel by."""
    fake_probes(monkeypatch, live_prs=CAP_FULL)
    store = swept_store
    store.request_review_sweep(_sweep(store))
    # A restart: the asks and the arrangement survive it, the built tasks do not.
    store.queued_tasks = []
    store._requested_tasks = {}

    def poisoned(payload):
        if payload["specificPR"] == "1":
            raise RuntimeError("diplomat-core failed: unknown depth")
        return "PROMPT"

    monkeypatch.setattr("diplomat_runtime.promptcore.build_prompt", poisoned)

    _poll(store, monkeypatch)

    assert [t.id for t in store.queued_tasks] == ["review:2"]
    # …and the skipped one is kept, because a core mid-self-update is a reason to
    # ask again next poll rather than to throw the ask away.
    assert [r.number for r in store.requested_work] == [1, 2]


def test_a_started_review_leaves_the_list_for_good(swept_store, monkeypatch):
    """The dispatch is what answers the ask. Left in the list it would be re-offered
    on the next poll, and the PR reviewed again, and again."""
    _spawn_recorder(monkeypatch)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    swept_store.request_review_sweep(_sweep(swept_store))

    swept_store._run_queued_task(swept_store.queued_tasks[0], forced=False)

    assert [r.number for r in swept_store.requested_work] == [2]
    fake_probes(monkeypatch, live_prs=CAP_FULL)
    _poll(swept_store, monkeypatch)
    assert [t.id for t in swept_store.queued_tasks] == ["review:2"]


def test_a_review_that_failed_to_start_is_asked_for_again(swept_store, monkeypatch):
    """A terminal that would not open is a reason to try again next poll, not a
    reason to drop work the operator asked for."""
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    monkeypatch.setattr(
        review, "spawn",
        lambda *a, **k: (_ for _ in ()).throw(review.SpawnError("no terminal")))
    swept_store.request_review_sweep(_sweep(swept_store))

    verdict = swept_store._run_queued_task(swept_store.queued_tasks[0], forced=False)

    assert verdict == "failed"
    assert [r.number for r in swept_store.requested_work] == [1, 2]


def test_a_banned_author_retires_the_ask(swept_store, monkeypatch):
    """The agent would refuse for as long as the ban stands, so the row would sit
    there for ever saying this machine is about to do something it will not do."""
    _spawn_recorder(monkeypatch)
    monkeypatch.setattr("diplomat_app.bans.read",
                        lambda: [bans.BannedAuthor("bob", "prompt injection")])
    swept_store.request_review_sweep(
        _sweep(swept_store, target=PRTarget.SOMEONE, username="bob", include_ready=True))

    verdict = swept_store._run_queued_task(swept_store.queued_tasks[0], forced=False)

    assert verdict == autofix.VERDICT_BANNED
    assert swept_store.requested_work == []


def test_an_ask_whose_pr_landed_is_dropped_and_forgotten(swept_store, monkeypatch):
    """A sweep asks for the PRs as they were when the button was pressed, and fifty of
    them take a day to work through at two bays. One that merges while it waits is a
    review of a diff nobody will open again.

    The ask has to go with the row: the row is rebuilt from the ask on every poll, so
    dropping one and keeping the other is a row that comes straight back."""
    from diplomat_runtime import activity

    fake_probes(monkeypatch, live_prs=CAP_FULL)  # no bay, so nothing starts either way
    swept_store.request_review_sweep(_sweep(swept_store))

    _poll(swept_store, monkeypatch, closed=[1])

    assert [t.id for t in swept_store.queued_tasks] == ["review:2"]
    assert [r.number for r in swept_store.requested_work] == [2]
    # And said out loud. This is the only thing that takes an ask off the list without
    # the operator, and a row that vanished silently reads exactly like one that ran.
    assert [e.detail for e in activity.read() if e.action == "queue-drop"] == [
        "Review · #1 · deep — PR no longer open, not run"
    ]


def test_a_closed_pr_retires_only_its_own_ask(swept_store, monkeypatch):
    """The read is the repo's recent closures, not an answer about this queue: it names
    PRs nothing here asked for, and every other row has to survive it. One merge that
    emptied the panel would take the whole sweep with it."""
    fake_probes(monkeypatch, live_prs=CAP_FULL)
    swept_store.request_review_sweep(_sweep(swept_store))

    _poll(swept_store, monkeypatch, closed=[3, 4, 99])

    assert [t.id for t in swept_store.queued_tasks] == ["review:1", "review:2"]
    assert [r.number for r in swept_store.requested_work] == [1, 2]


def test_cancelling_drops_an_ask_and_it_does_not_come_back(swept_store, monkeypatch):
    """A sweep is the one thing here that can be asked for by the fifty, and while its
    PR is open nothing GitHub does retires it. Without a way out, a mis-aimed sweep is
    a day of agents nobody can call off."""
    fake_probes(monkeypatch, live_prs=CAP_FULL)
    swept_store.request_review_sweep(_sweep(swept_store))

    swept_store.cancel_requested_work("review:1")

    assert [t.id for t in swept_store.queued_tasks] == ["review:2"]
    _poll(swept_store, monkeypatch)
    assert [t.id for t in swept_store.queued_tasks] == ["review:2"]


def test_cancel_leaves_a_monitors_row_alone(swept_store):
    """A monitor's row stands for work GitHub is owed: dropping it would put it
    straight back on the next poll, so the button is not offered and the store refuses
    it even if it were."""
    swept_store.queued_tasks = [_monitor_task()]

    swept_store.cancel_requested_work("conflicts:7")

    assert [t.id for t in swept_store.queued_tasks] == ["conflicts:7"]


# ---- how they run ---------------------------------------------------------


def test_a_queued_review_waits_for_the_cap_like_any_automatic_agent(swept_store,
                                                                    monkeypatch):
    """The whole point of queueing them: the cap is two, so a sweep of five starts two
    and the other three wait, rather than one agent taking all five at once."""
    calls = _spawn_recorder(monkeypatch)
    swept_store.prs = [_pr(n) for n in (1, 2, 3, 4, 5)]
    swept_store.request_review_sweep(_sweep(swept_store))

    _poll(swept_store, monkeypatch)

    assert len(calls) == swept_store.auto_task_limit == 2
    assert [t.id for t in swept_store.queued_tasks] == ["review:3", "review:4", "review:5"]


def test_a_running_requested_review_is_not_labelled_automatic(swept_store, monkeypatch):
    """It is dispatched as auto work — it waits for the cap and holds a bay — but the
    "Auto · " prefix says a MONITOR found the work, and this one the operator did.
    Left on, a requested review of #1 reads exactly like the review-reply monitor's
    own dispatch on #1."""
    from diplomat_runtime import activity

    _spawn_recorder(monkeypatch)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    swept_store.request_review_sweep(_sweep(swept_store))

    swept_store._run_queued_task(swept_store.queued_tasks[0], forced=False)

    logged = [e for e in activity.read() if e.action == "review"]
    assert [e.detail for e in logged] == ["Review · #1 · deep"]


def test_the_monitor_toggles_do_not_hold_an_ask(swept_store, monkeypatch):
    """The toggles say what this machine may go LOOKING for. Switching them off does
    not mean it should stop doing what it was told to do."""
    calls = _spawn_recorder(monkeypatch)
    swept_store.pr_autofix_enabled = False
    swept_store.review_requests_enabled = False
    swept_store.request_review_sweep(_sweep(swept_store))

    _poll(swept_store, monkeypatch)

    assert len(calls) == 2  # both bays spent on the operator's own asks
    assert swept_store.requested_work == []


def _monitor_task() -> autofix.QueuedTask:
    return autofix.QueuedTask(
        id=autofix.queue_key("conflicts", 7),
        job=autofix.AgentJob(kind="conflicts", audit_action="conflicts",
                             label="Resolve · #7", prompt="p",
                             pr_url="https://github.com/o/r/pull/7", pr_number=7,
                             counter="conflicts"),
        attempt=1,
    )
