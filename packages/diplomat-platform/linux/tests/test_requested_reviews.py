"""The reviews a PR sweep asks for: fan-out, persistence, and what retires them.

Pressing SPAWN on a whose-PRs sweep queues one review per PR rather than handing ONE
agent every PR at once — fifty reviews in one session on a repo with fifty drafts.
That puts them through the machinery the monitors' own work already goes through: the
task cap starts them a bay at a time, the panel draws and reorders them, "execute now"
jumps one ahead.

What is theirs alone is that they are the only queued tasks the applet has to
REMEMBER. Every other row is re-derived from GitHub each poll, and a PR records
nothing about somebody having swept it — so the ask is stored, re-offered until it is
dispatched, and dropped when something answers for it. That cycle is most of what is
tested here.

The store fixture, the spawn recorder and the evidence stub come from
``test_autofix``: this is the same Store on the same machinery, asked a different
question.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from test_autofix import _spawn_recorder, fake_probes, store  # noqa: F401 — fixture

from diplomat_app import autofix, bans, review
from diplomat_app.models import OpenPR
from diplomat_app.prtarget import PRTarget
from diplomat_app.store import Store

pytest.importorskip("PySide6")

NOW = datetime.now(timezone.utc)

#: PR numbers of agents this applet has no record of — enough of them to hold every
#: bay of the default cap, so a poll's drain starts nothing and what the queue holds
#: afterwards is what the ask put there.
CAP_FULL = {90, 91}


def _pr(number: int, *, author: str = "alice", draft: bool = True) -> OpenPR:
    return OpenPR(number, f"PR {number}", f"https://github.com/o/r/pull/{number}",
                  draft, author, NOW, None, [], None, [])


@pytest.fixture
def swept_store(store):  # noqa: F811 — the shared Store fixture
    """The store with a panel fetch behind it: two drafts of mine, a ready PR of
    mine, and a draft of somebody else's."""
    store.has_loaded = True
    store.prs = [_pr(1), _pr(2), _pr(3, draft=False), _pr(4, author="bob")]
    return store


def _sweep(store, **kwargs) -> review.ReviewConfig:  # noqa: F811
    cfg = dict(target=PRTarget.MINE, me=store.me, depth="deep",
               include_drafts=True, include_ready=False)
    cfg.update(kwargs)
    return review.ReviewConfig(**cfg)


def _poll(store, monkeypatch) -> None:  # noqa: F811
    """One monitor cycle with both GitHub fetches empty, so what the queue holds
    afterwards is what the ask put there."""
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_snapshots", lambda *a, **k: [])
    monkeypatch.setattr("diplomat_app.autofixmonitor.fetch_review_requests",
                        lambda *a, **k: [])
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

    assert [r.number for r in store.requested_reviews] == [1, 2]
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

    monkeypatch.setattr("diplomat_app.promptcore.build_prompt", flaky)

    with pytest.raises(RuntimeError):
        swept_store.request_review_sweep(_sweep(swept_store, depth="quick"))
    assert swept_store.requested_reviews == []
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
    assert swept_store.requested_reviews == []


# ---- what remembers them, and what forgets them ---------------------------


def test_the_ask_outlives_the_process_that_took_it(swept_store):
    """The one thing here GitHub cannot re-derive. A fifty-PR sweep runs for hours,
    which is exactly long enough to be interrupted, and a restart that dropped the
    rest of it would do so silently."""
    swept_store.request_review_sweep(_sweep(swept_store))

    reopened = Store()
    assert [r.number for r in reopened.requested_reviews] == [1, 2]
    assert reopened.requested_reviews[0].config["specificPR"] == "1"


def test_a_stored_row_that_makes_no_sense_is_dropped_not_raised(swept_store):
    """Read back off disk, so it can be part-written or hand-edited. One unusable row
    must cost that row, not the whole queue."""
    swept_store._settings.setValue(
        "requestedReviews",
        '[{"pr": 1, "config": {"kind": "review"}}, {"pr": "x"}, "nonsense", {}]',
    )
    assert [r.number for r in swept_store.requested_reviews] == [1]

    swept_store._settings.setValue("requestedReviews", "{not json")
    assert swept_store.requested_reviews == []


def test_every_poll_re_offers_an_ask_nothing_has_started(swept_store, monkeypatch):
    """A monitor re-offers its work by finding it on GitHub again. Nothing on GitHub
    says a PR was swept, so what re-offers these is the ask itself — and a commit
    rebuilds the published queue from this cycle's offers alone, so an ask missing
    from them is an ask that vanishes off the panel."""
    fake_probes(monkeypatch, live_prs=CAP_FULL)
    swept_store.request_review_sweep(_sweep(swept_store))

    _poll(swept_store, monkeypatch)

    assert [t.id for t in swept_store.queued_tasks] == ["review:1", "review:2"]


def test_a_started_review_leaves_the_list_for_good(swept_store, monkeypatch):
    """The dispatch is what answers the ask. Left in the list it would be re-offered
    on the next poll, and the PR reviewed again, and again."""
    _spawn_recorder(monkeypatch)
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    swept_store.request_review_sweep(_sweep(swept_store))

    swept_store._run_queued_task(swept_store.queued_tasks[0], forced=False)

    assert [r.number for r in swept_store.requested_reviews] == [2]
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
    assert [r.number for r in swept_store.requested_reviews] == [1, 2]


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
    assert swept_store.requested_reviews == []


def test_cancelling_drops_an_ask_and_it_does_not_come_back(swept_store, monkeypatch):
    """A sweep is the one thing here that can be asked for by the fifty, and nothing
    else retires it. Without a way out, a mis-aimed sweep is a day of agents nobody
    can call off."""
    fake_probes(monkeypatch, live_prs=CAP_FULL)
    swept_store.request_review_sweep(_sweep(swept_store))

    swept_store.cancel_requested_review("review:1")

    assert [t.id for t in swept_store.queued_tasks] == ["review:2"]
    _poll(swept_store, monkeypatch)
    assert [t.id for t in swept_store.queued_tasks] == ["review:2"]


def test_cancel_leaves_a_monitors_row_alone(swept_store):
    """A monitor's row stands for work GitHub is owed: dropping it would put it
    straight back on the next poll, so the button is not offered and the store refuses
    it even if it were."""
    swept_store.queued_tasks = [_monitor_task()]

    swept_store.cancel_requested_review("conflicts:7")

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
    from diplomat_app import activity

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
    assert swept_store.requested_reviews == []


def _monitor_task() -> autofix.QueuedTask:
    return autofix.QueuedTask(
        id=autofix.queue_key("conflicts", 7),
        job=autofix.AgentJob(kind="conflicts", audit_action="conflicts",
                             label="Resolve · #7", prompt="p",
                             pr_url="https://github.com/o/r/pull/7", pr_number=7,
                             counter="conflicts"),
        attempt=1,
    )
