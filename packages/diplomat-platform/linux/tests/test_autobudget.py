"""The rate-limit budget: what the telemetry ledger says an auto-task costs, and
what that stops from starting.

The arithmetic is covered against the Swift twin in ``test_autofix.py`` (and the
smoke's "the device's rate-limit budget" section). What is covered here is
everything around it: that the assembler prices the same tasks the Telemetry
screen prices, that a probe which cannot answer never holds work back, and that
the verdict actually reaches the four places automatic work can start from — the
monitors, the queue drain, "execute now", and a mesh peer routing a job in.
"""

from __future__ import annotations

import json
import time

import pytest

from diplomat_runtime import activity, appconfig, autobudget, autofix, telemetry
from test_autofix import _spawn_recorder, store  # noqa: F401 — the shared
# Store fixture and spawn stub, reused rather than rebuilt: a second copy would
# be a second set of probe stubs to keep in step with the real one.

# One task's tokens, and a pair of samples that price the windows from them. The
# window is worth `tokens / d_util`, so the numbers below are chosen to make the
# per-task percentages exact rather than approximately right:
#   session: 200_000 tokens spent for 0.10 of the window -> 2_000_000 per window
#   week:    200_000 tokens spent for 0.02 of the window -> 10_000_000 per window
# A 100_000-token task is therefore 5% of a session and 1% of a week.
_SESSION_LIMIT = 2_000_000.0
_WEEK_LIMIT = 10_000_000.0


#: Five tasks around a 100k mean, with enough spread that a prediction bound is
#: visibly above it — the whole point of the statistic being tested.
_SPREAD = [60_000.0, 80_000.0, 100_000.0, 120_000.0, 140_000.0]


@pytest.fixture(autouse=True)
def _fresh():
    """No cached verdict or fold from the last test. The ledger, the config file and
    the activity feed are already redirected per-test by conftest; these two caches
    are keyed on a clock and a stat, which a temp file can collide on."""
    telemetry._reset_cache()
    autobudget._reset_cache()
    yield


def _price_the_windows(at: float) -> None:
    """Two samples spanning one interval, which is what :func:`telemetry.calibrate`
    needs to say what a window is worth in tokens."""
    telemetry.append({"at": at, "ev": "sample", "sessionLeft": 0.9, "weekLeft": 0.9,
                      "repoTokens": 0.0, "otherTokens": 0.0})
    telemetry.append({"at": at + 900, "ev": "sample", "sessionLeft": 0.8,
                      "weekLeft": 0.88, "repoTokens": 200_000.0, "otherTokens": 0.0})


def _finished_tasks(at: float, tokens: list[float]) -> None:
    """Auto-tasks that ran HERE and finished, each priced by its own transcript —
    the population the per-task distribution is built from."""
    for i, tok in enumerate(tokens):
        key = f"review:h/o/r#{i}@sha{i}"
        telemetry.append({"at": at, "ev": "started", "key": key, "remote": False,
                          "attempt": 1})
        telemetry.append({"at": at + 60, "ev": "done", "key": key, "tokens": tok})


def _probe(monkeypatch, session, week):
    """What the OAuth usage probe reports is left, as fractions."""
    monkeypatch.setattr("diplomat_runtime.quota.fractions_left", lambda: (session, week))


def _ledger_with_priced_tasks(monkeypatch, tokens: list[float]) -> None:
    now = time.time()
    _price_the_windows(now - 7200)
    _finished_tasks(now - 3600, tokens)
    telemetry._reset_cache()
    autobudget._reset_cache()


# MARK: - Pricing


def test_the_windows_are_priced_from_the_same_samples_the_screen_uses(monkeypatch):
    """A sanity anchor for every expected percentage below: if calibration moved,
    these tests would be asserting against a differently-priced window and the
    failure would look like a gate bug."""
    _ledger_with_priced_tasks(monkeypatch, [100_000.0] * 6)
    model = telemetry.model()
    summary = telemetry.summarize(
        telemetry.load(), now=time.time(), days=float(model["defaultRangeDays"]),
        steps=2, bin_count=1, z=1.96,
    )
    assert summary.session_limit_tokens == pytest.approx(_SESSION_LIMIT)
    assert summary.week_limit_tokens == pytest.approx(_WEEK_LIMIT)
    assert summary.per_task.count == 6
    assert summary.per_task.mean == pytest.approx(5.0)  # 100k of a 2M window


def test_a_measured_bound_prices_both_windows_from_one_distribution(monkeypatch):
    """The week's figure is the session's rescaled by the ratio of the two
    calibrations — a task is a fixed number of tokens and only the divisor
    differs — so the two can never disagree about how big a task is."""
    _ledger_with_priced_tasks(monkeypatch, [80_000.0, 100_000.0, 120_000.0,
                                            90_000.0, 110_000.0])
    model = telemetry.model()
    summary = telemetry.summarize(
        telemetry.load(), now=time.time(), days=float(model["defaultRangeDays"]),
        steps=2, bin_count=1, z=1.96,
    )
    session, week = autobudget._costs(summary, z=autofix.budget_z(95), min_sample=5)
    assert session is not None and week is not None
    assert week == pytest.approx(session * _SESSION_LIMIT / _WEEK_LIMIT)
    assert week == pytest.approx(session / 5)  # the week is 5x the session window
    # And it really is the prediction bound, not the mean: the mean is 5%.
    assert session > 5.0


def test_a_week_the_ledger_cannot_price_leaves_the_session_measured(monkeypatch):
    """Every quota sample carries a weekly reading that may be missing while the
    5-hour one is fine. The window that IS priced must still gate on its own
    figure rather than both falling back to the floor together."""
    now = time.time()
    telemetry.append({"at": now - 7200, "ev": "sample", "sessionLeft": 0.9,
                      "weekLeft": None, "repoTokens": 0.0, "otherTokens": 0.0})
    telemetry.append({"at": now - 6300, "ev": "sample", "sessionLeft": 0.8,
                      "weekLeft": None, "repoTokens": 200_000.0, "otherTokens": 0.0})
    _finished_tasks(now - 3600, [100_000.0] * 6)
    telemetry._reset_cache()

    model = telemetry.model()
    summary = telemetry.summarize(
        telemetry.load(), now=time.time(), days=float(model["defaultRangeDays"]),
        steps=2, bin_count=1, z=1.96,
    )
    session, week = autobudget._costs(summary, z=autofix.budget_z(95), min_sample=5)
    assert session is not None
    assert week is None


# MARK: - The assembled verdict


def test_a_priced_ledger_refuses_a_window_that_cannot_cover_a_task(monkeypatch):
    _ledger_with_priced_tasks(monkeypatch, _SPREAD)
    _probe(monkeypatch, session=0.02, week=0.9)  # 2% of the 5-hour window left

    budget = autobudget.decide()

    assert not budget.affordable
    assert budget.window == autofix.WINDOW_SESSION
    assert budget.measured, "the ledger priced this — it is not the standing floor"
    assert budget.left == pytest.approx(2.0)
    # The mean task is 5% of the window (100k of 2M); what is REQUIRED is above
    # that, because half of all tasks cost more than the mean.
    assert budget.needed > 5.0


def test_the_same_ledger_proceeds_once_the_window_has_refilled(monkeypatch):
    _ledger_with_priced_tasks(monkeypatch, _SPREAD)
    _probe(monkeypatch, session=0.95, week=0.9)

    assert autobudget.decide().affordable


def test_a_ledger_of_identical_tasks_asks_for_exactly_the_mean(monkeypatch):
    """No observed spread, no inflation: the bound is the measurement and nothing
    more. It is the `sqrt(1 + 1/n)` factor that would otherwise make a machine with
    perfectly predictable tasks refuse work it can plainly afford."""
    _ledger_with_priced_tasks(monkeypatch, [100_000.0] * 8)
    _probe(monkeypatch, session=0.06, week=0.9)

    budget = autobudget.decide()

    assert budget.affordable and budget.measured
    assert budget.needed == pytest.approx(5.0)


def test_a_thin_ledger_holds_the_configured_floor(monkeypatch):
    """The default answer on a machine that has not finished enough auto-tasks to
    price one: keep 20% of each window in hand."""
    _ledger_with_priced_tasks(monkeypatch, [100_000.0])  # one task is not a spread
    _probe(monkeypatch, session=0.15, week=0.9)

    budget = autobudget.decide()

    assert not budget.affordable
    assert not budget.measured
    assert budget.needed == pytest.approx(autofix.DEFAULT_BUDGET_FLOOR_PCT)
    assert budget.left == pytest.approx(15.0)

    autobudget._reset_cache()
    _probe(monkeypatch, session=0.25, week=0.9)
    assert autobudget.decide().affordable


def test_an_empty_ledger_still_answers_on_the_floor(monkeypatch):
    """Nothing recorded at all — a fresh install — is the thin case, not a crash."""
    _probe(monkeypatch, session=0.05, week=0.9)

    budget = autobudget.decide()

    assert not budget.affordable and not budget.measured


def test_a_probe_that_cannot_answer_never_holds_work_back(monkeypatch):
    """THE fail-open, assembled end to end: with the probe off (the conftest
    default, and a documented configuration) nothing is measured and nothing is
    gated."""
    _ledger_with_priced_tasks(monkeypatch, _SPREAD)
    _probe(monkeypatch, session=None, week=None)

    assert autobudget.decide().affordable

    # A probe that RAISES is the same answer, not an exception into a dispatch.
    autobudget._reset_cache()

    def boom():
        raise OSError("network unreachable")

    monkeypatch.setattr("diplomat_runtime.quota.fractions_left", boom)
    assert autobudget.decide().affordable


def test_a_corrupt_ledger_degrades_to_the_floor_rather_than_raising(monkeypatch):
    """A half-written tail is normal for a file two processes append to. The gate
    must lose the measurement, not the dispatch."""
    telemetry.ledger_path().parent.mkdir(parents=True, exist_ok=True)
    telemetry.ledger_path().write_text("{not json at all\n" * 5, encoding="utf-8")
    telemetry._reset_cache()
    _probe(monkeypatch, session=0.5, week=0.9)

    budget = autobudget.decide()

    assert budget.affordable and not budget.measured  # 50% clears the 20% floor


def test_the_confidence_knob_moves_the_bound(monkeypatch):
    """Higher confidence is a stricter gate — the whole point of the knob."""
    _ledger_with_priced_tasks(monkeypatch, [40_000.0, 100_000.0, 160_000.0,
                                            60_000.0, 140_000.0])
    model = telemetry.model()
    summary = telemetry.summarize(
        telemetry.load(), now=time.time(), days=float(model["defaultRangeDays"]),
        steps=2, bin_count=1, z=1.96,
    )
    bounds = [
        autobudget._costs(summary, z=autofix.budget_z(level), min_sample=5)[0]
        for level in (50, 80, 95, 99)
    ]
    assert bounds == sorted(bounds)
    assert bounds[0] < bounds[-1]


def test_the_floor_knob_is_read_from_the_shared_config(monkeypatch):
    _probe(monkeypatch, session=0.30, week=0.9)
    assert autobudget.decide().affordable  # 30% clears the default 20% floor

    appconfig.set_float(appconfig.AUTO_BUDGET_FLOOR_PCT, 50.0)
    autobudget._reset_cache()
    assert not autobudget.decide().affordable


def test_the_gate_can_be_switched_off_entirely():
    assert autobudget.enabled()  # on by default
    appconfig.set_bool(appconfig.AUTO_BUDGET_GATE, False)
    assert not autobudget.enabled()


def test_one_answer_is_reused_across_a_poll_that_asks_repeatedly(monkeypatch):
    """A poll finding eight units of owed work asks eight times about a machine
    whose spend cannot have moved in between. Only the first costs a fold."""
    calls = []

    def counted():
        calls.append(1)
        return (0.9, 0.9)

    monkeypatch.setattr("diplomat_runtime.quota.fractions_left", counted)

    now = time.time()
    for _ in range(8):
        autobudget.decide(now=now)
    assert len(calls) == 1

    # …and the answer is re-taken once the TTL is past, so a window that emptied
    # mid-poll-cycle is noticed.
    autobudget.decide(now=now + autobudget._TTL_SECS + 1)
    assert len(calls) == 2


def test_the_shortfall_line_says_which_window_and_against_what():
    measured = autofix.Budget(affordable=False, window=autofix.WINDOW_SESSION,
                              left=3.5, needed=12.0, measured=True)
    line = autobudget.shortfall(measured)
    assert "5-hour" in line and "3.5%" in line and "12.0%" in line
    assert "a task needs" in line

    floored = autofix.Budget(affordable=False, window=autofix.WINDOW_WEEK,
                             left=8.0, needed=20.0, measured=False)
    line = autobudget.shortfall(floored)
    assert "7-day" in line and "kept in hand" in line


# MARK: - The other currency
#
# A machine whose runner is billed in money is gated on money. Everything above
# still applies — same distribution, same prediction bound, same fail-open — only
# the ledger's dollars, the account's two ceilings and the reserve replace the
# window shares. The Claude path must keep answering in percentages throughout,
# which is what the last test in this section pins.

#: A model id spelled as the runner that ran it spells it, which is the spelling the
#: ledger carries and the only one anything here groups by.
_MODEL = "deepseek/deepseek-v4-flash-0731"
_OTHER_MODEL = "anthropic/claude-opus-5"

#: Five billed tasks around a $0.10 mean, with a spread wide enough that the
#: prediction bound sits visibly above it.
_USD_SPREAD = [0.06, 0.08, 0.10, 0.12, 0.14]


def _hermes(monkeypatch) -> None:
    """A machine whose next spawn is a Hermes agent, not a Claude Code one."""
    appconfig.set_value(appconfig.AGENT_RUNNER, "hermes")


def _billed_tasks(at: float, prices: list[float], model: str = _MODEL,
                  tag: str = "a") -> None:
    """Auto-tasks that ran HERE, finished, and were charged for — the population the
    dollar distribution is built from.

    ``tag`` distinguishes one batch's ledger keys from another's. Keys are the
    identity the fold folds on, so two batches sharing them would be one batch with
    the second silently discarded — which reads as the filter under test working."""
    for i, usd in enumerate(prices):
        key = f"review:h/o/r#{i}@usd-{tag}{i}"
        telemetry.append({"at": at, "ev": "started", "key": key, "remote": False,
                          "attempt": 1})
        telemetry.append({"at": at + 60, "ev": "done", "key": key, "tokens": 90_000.0,
                          "runner": "hermes", "usd": usd, "model": model})
    telemetry._reset_cache()
    autobudget._reset_cache()


def _balance(monkeypatch, key_left=None, credit_left=None):
    """What the OpenRouter probe reports is left, in dollars."""
    from diplomat_runtime import spend

    monkeypatch.setattr(
        "diplomat_runtime.spend.balance",
        lambda: spend.Balance(key_left=key_left, credit_left=credit_left))


def test_a_hermes_machine_is_priced_in_dollars_not_window_shares(monkeypatch):
    """The whole point. A Hermes task is billed by OpenRouter, so the figures the
    verdict carries are money — and the Anthropic window it never draws on has no
    say, even when that probe is answering."""
    _hermes(monkeypatch)
    _billed_tasks(time.time() - 3600, _USD_SPREAD)
    _probe(monkeypatch, session=0.01, week=0.01)  # a spent Claude window…
    _balance(monkeypatch, key_left=16.85, credit_left=17.03)

    budget = autobudget.decide()

    assert budget.affordable, "…which this machine does not spend"
    assert budget.unit == autofix.UNIT_USD
    assert budget.window == autofix.WINDOW_KEY
    assert budget.left == pytest.approx(16.85)
    # The mean billed task is $0.10; what is REQUIRED is above it, for the reason the
    # percentage bound is above its own mean.
    assert budget.measured and budget.needed > 0.10


def test_a_key_with_less_than_one_task_left_on_it_holds_the_work(monkeypatch):
    _hermes(monkeypatch)
    _billed_tasks(time.time() - 3600, _USD_SPREAD)
    _balance(monkeypatch, key_left=0.05, credit_left=200.0)

    budget = autobudget.decide()

    assert not budget.affordable
    assert budget.window == autofix.WINDOW_KEY
    assert budget.left == pytest.approx(0.05)


def test_a_drained_balance_holds_the_work_even_on_a_fresh_key_limit(monkeypatch):
    """The two ceilings are independent: a key whose weekly cap just reset still
    cannot spend money the account does not have."""
    _hermes(monkeypatch)
    _billed_tasks(time.time() - 3600, _USD_SPREAD)
    _balance(monkeypatch, key_left=25.0, credit_left=0.02)

    budget = autobudget.decide()

    assert not budget.affordable
    assert budget.window == autofix.WINDOW_CREDITS


def test_an_uncapped_key_leaves_the_credit_balance_to_gate(monkeypatch):
    _hermes(monkeypatch)
    _billed_tasks(time.time() - 3600, _USD_SPREAD)
    _balance(monkeypatch, key_left=None, credit_left=0.02)

    assert not autobudget.decide().affordable


def test_a_switch_of_model_is_a_switch_of_rates(monkeypatch):
    """Cost per task is a property of the model. Left mixed, one expensive model's
    runs would price the next task on a cheap one at several times what it costs —
    and, far worse, a cheap history would wave through work on an expensive one.

    So the sample is the current model's alone, and a machine that has just switched
    has no spread yet and falls back to the reserve."""
    _hermes(monkeypatch)
    now = time.time()
    _billed_tasks(now - 7200, _USD_SPREAD)  # a long history on the cheap model…
    # …and one run on an expensive one
    _billed_tasks(now - 60, [4.80], _OTHER_MODEL, tag="b")
    _balance(monkeypatch, key_left=6.0, credit_left=200.0)

    budget = autobudget.decide()

    assert not budget.measured, "one task on the new model is not a spread"
    assert budget.needed == pytest.approx(autofix.DEFAULT_BUDGET_RESERVE_USD)

    # With a history of its own, the new model prices itself — and at ITS rates.
    _billed_tasks(now - 30, [4.60, 5.00, 4.90, 5.10], _OTHER_MODEL, tag="c")
    _balance(monkeypatch, key_left=1.00, credit_left=200.0)
    autobudget._reset_cache()
    budget = autobudget.decide()

    assert budget.measured
    assert budget.needed > 4.0, "priced at the expensive model's rates, not the cheap one's"
    # The cheap model's five tasks are still in range and still say $0.10 — counted,
    # they would make this dollar look like room for half a dozen more runs.
    assert not budget.affordable


def test_a_thin_but_billed_ledger_holds_the_configured_reserve(monkeypatch):
    _hermes(monkeypatch)
    _billed_tasks(time.time() - 3600, [0.10])  # one task is not a spread
    _balance(monkeypatch, key_left=0.50, credit_left=200.0)

    budget = autobudget.decide()

    assert not budget.affordable and not budget.measured
    assert budget.needed == pytest.approx(autofix.DEFAULT_BUDGET_RESERVE_USD)

    autobudget._reset_cache()
    _balance(monkeypatch, key_left=1.50, credit_left=200.0)
    assert autobudget.decide().affordable


def test_a_machine_that_has_never_been_charged_has_no_opinion(monkeypatch):
    """A runner pointed at a local model spends nothing, and one Diplomat cannot
    price reports nothing. Holding either to the reserve would gate its work against
    an account it never draws on, purely because a key for that account is on disk —
    the very mistake this whole path exists to stop the Claude window making."""
    _hermes(monkeypatch)
    _ledger_with_priced_tasks(monkeypatch, _SPREAD)  # tokens, but not one charge

    def never():
        raise AssertionError("a machine that spends no money must not be asked "
                             "about an account's balance")

    monkeypatch.setattr("diplomat_runtime.spend.balance", never)

    budget = autobudget.decide()

    assert budget.affordable
    assert budget.window == "", "nothing was decided, rather than decided cheaply"


def test_an_unreachable_account_never_holds_work_back(monkeypatch):
    """The same fail-open the quota probe gets, on the same reasoning."""
    _hermes(monkeypatch)
    _billed_tasks(time.time() - 3600, _USD_SPREAD)
    _balance(monkeypatch, key_left=None, credit_left=None)

    assert autobudget.decide().affordable

    autobudget._reset_cache()

    def boom():
        raise OSError("network unreachable")

    monkeypatch.setattr("diplomat_runtime.spend.balance", boom)
    assert autobudget.decide().affordable


def test_the_reserve_knob_is_read_from_the_shared_config(monkeypatch):
    _hermes(monkeypatch)
    _billed_tasks(time.time() - 3600, [0.10])
    _balance(monkeypatch, key_left=2.0, credit_left=200.0)
    assert autobudget.decide().affordable  # $2 clears the default $1 reserve

    appconfig.set_float(appconfig.AUTO_BUDGET_RESERVE_USD, 5.0)
    autobudget._reset_cache()
    assert not autobudget.decide().affordable


def test_the_shortfall_line_reads_in_the_currency_it_decided_in():
    measured = autofix.Budget(affordable=False, window=autofix.WINDOW_KEY,
                              left=0.05, needed=0.21, measured=True,
                              unit=autofix.UNIT_USD)
    line = autobudget.shortfall(measured)
    assert "OpenRouter key limit" in line
    assert "$0.050" in line and "$0.210" in line
    assert "%" not in line, "a balance is not a percentage of anything"

    floored = autofix.Budget(affordable=False, window=autofix.WINDOW_CREDITS,
                             left=0.40, needed=1.0, measured=False,
                             unit=autofix.UNIT_USD)
    line = autobudget.shortfall(floored)
    assert "OpenRouter credit balance" in line and "kept in hand" in line
    assert "$1.00" in line


def test_the_claude_runner_still_decides_in_window_shares(monkeypatch):
    """The other half of the fork, and the regression that would be silent: a Claude
    Code machine must not start reading an OpenRouter balance it does not spend."""
    _ledger_with_priced_tasks(monkeypatch, _SPREAD)
    _probe(monkeypatch, session=0.02, week=0.9)

    def never():
        raise AssertionError("the Claude path must not probe OpenRouter")

    monkeypatch.setattr("diplomat_runtime.spend.balance", never)

    budget = autobudget.decide()

    assert not budget.affordable
    assert budget.unit == autofix.UNIT_PCT
    assert budget.window == autofix.WINDOW_SESSION


# MARK: - What it stops


def _job(number=9, counter="review_requests", action="review"):
    return autofix.AgentJob(
        kind="review",
        audit_action=action,
        label=f"Review · #{number}",
        prompt="PROMPT",
        pr_url=f"https://github.com/o/r/pull/{number}",
        pr_number=number,
        counter=counter,
        duty="review",
    )


@pytest.fixture
def broke(monkeypatch):
    """A machine with a priced ledger and almost none of its 5-hour window left."""
    _ledger_with_priced_tasks(monkeypatch, _SPREAD)
    _probe(monkeypatch, session=0.01, week=0.9)


def test_an_auto_dispatch_is_deferred_and_queued(store, monkeypatch, broke):
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    calls = _spawn_recorder(monkeypatch)

    verdict = store.dispatch_agent(_job(), autofix.SOURCE_AUTO)

    assert verdict == autofix.VERDICT_UNAFFORDABLE
    assert calls == [], "nothing was spawned"
    # Deferred, not dropped: it is staged for this cycle's queue like any other hold.
    store.commit_queue()
    assert [e.id for e in store.queued_tasks] == ["review:9"]


def test_an_auto_dispatch_is_deferred_when_the_money_runs_out(store, monkeypatch):
    """The same hold, reached through the other currency — the whole chain from the
    configured runner, through the ledger's dollars and the account's ceilings, to a
    spawn that does not happen and a feed line that says why in money."""
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    calls = _spawn_recorder(monkeypatch)
    _hermes(monkeypatch)
    _billed_tasks(time.time() - 3600, _USD_SPREAD)
    _balance(monkeypatch, key_left=0.02, credit_left=0.02)

    verdict = store.dispatch_agent(_job(), autofix.SOURCE_AUTO)

    assert verdict == autofix.VERDICT_UNAFFORDABLE
    assert calls == [], "nothing was spawned"
    line = [e for e in activity.read() if e.action == "no-budget"][0]
    assert "OpenRouter key limit left" in line.detail
    store.commit_queue()
    assert [e.id for e in store.queued_tasks] == ["review:9"]


def test_a_panel_spawn_is_never_gated_by_the_budget(store, monkeypatch, broke):
    """Spending the operator's own last slice of the limit is the operator's call.
    The same asymmetry the task cap already draws."""
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    calls = _spawn_recorder(monkeypatch)

    assert store.dispatch_agent(_job(), autofix.SOURCE_PANEL) == "spawned"
    assert len(calls) == 1


def test_the_drain_stops_at_the_budget_and_keeps_its_queue(store, monkeypatch, broke):
    """The drain bypasses the CAP its caller already counted, not the budget: work
    that could not be afforded when it was found is not afforded by having waited."""
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    calls = _spawn_recorder(monkeypatch)
    monkeypatch.setattr(type(store), "_auto_tasks_running", lambda self: 0)
    store.queued_tasks = [
        autofix.QueuedTask("review:3", _job(number=3), 1),
        autofix.QueuedTask("review:4", _job(number=4), 1),
    ]

    store._drain_queued_tasks([], closed=set())

    assert calls == []
    # The one it tried is re-staged rather than dropped — a refusal writes no attempt
    # record, so it is offered again at the end of this very cycle…
    assert [e.id for e in store._staged_queue] == ["review:3"]
    # …and the first refusal ends the drain, because every entry behind it would be
    # priced against the same windows and get the same answer. The untried one is
    # still on the list its own monitor will re-offer.
    assert [e.id for e in store.queued_tasks] == ["review:4"]


def test_execute_now_overrides_the_budget(store, monkeypatch, broke):
    """The one override, and it is the operator's: they are looking at the row and
    know something the ledger does not."""
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    calls = _spawn_recorder(monkeypatch)
    entry = autofix.QueuedTask("review:3", _job(number=3), 1)
    store.queued_tasks = [entry]

    store._execute_queued_task(entry)

    assert len(calls) == 1
    assert not store.error, "a forced run that spawned must not also report a refusal"


def test_the_gate_switched_off_lets_a_broke_machine_dispatch(store, monkeypatch, broke):
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    calls = _spawn_recorder(monkeypatch)
    appconfig.set_bool(appconfig.AUTO_BUDGET_GATE, False)

    assert store.dispatch_agent(_job(), autofix.SOURCE_AUTO) == "spawned"
    assert len(calls) == 1


def test_the_feed_gets_one_line_per_episode_and_another_when_it_clears(
    store, monkeypatch, broke
):
    """A machine under its floor owes N units of work and polls every 3 minutes;
    one line per PR per poll would bury the feed for as long as the window takes to
    refill."""
    monkeypatch.setattr("diplomat_app.bans.read", lambda: [])
    _spawn_recorder(monkeypatch)

    for n in (1, 2, 3):
        store.dispatch_agent(_job(number=n), autofix.SOURCE_AUTO)

    lines = [e for e in activity.read() if e.action == "no-budget"]
    assert len(lines) == 1
    assert "5-hour rate limit left" in lines[0].detail

    # The window refills: the next dispatch proceeds AND re-arms the notice, so the
    # next episode gets a line of its own instead of being swallowed.
    autobudget._reset_cache()
    _probe(monkeypatch, session=0.95, week=0.9)
    assert store.dispatch_agent(_job(number=4), autofix.SOURCE_AUTO) == "spawned"

    autobudget._reset_cache()
    _probe(monkeypatch, session=0.01, week=0.9)
    store.dispatch_agent(_job(number=5), autofix.SOURCE_AUTO)
    assert len([e for e in activity.read() if e.action == "no-budget"]) == 2


def test_a_mesh_peers_job_is_declined_so_the_slot_fails_over(monkeypatch, broke):
    """The other half of the same rule. Work a peer routes here spends THIS
    machine's limit, and the applet never sees it — so the node's host asks too,
    and a decline sends the mesh looking for a node with surplus."""
    from diplomat_runtime.szponthost import DiplomatHost

    assert DiplomatHost().at_job_capacity([]) is True
    lines = [e for e in activity.read() if e.action == "mesh-no-budget"]
    assert len(lines) == 1
    assert "Declined a peer's job" in lines[0].detail


def test_a_mesh_peers_job_is_taken_when_the_window_has_room(monkeypatch):
    """The decline above must be the budget talking, not the host failing closed on
    every call."""
    from diplomat_runtime.szponthost import DiplomatHost

    _ledger_with_priced_tasks(monkeypatch, _SPREAD)
    _probe(monkeypatch, session=0.95, week=0.9)
    monkeypatch.setattr("diplomat_runtime.szponthost._ps_dump", lambda: "")
    monkeypatch.setattr("diplomat_runtime.tmuxwatch.pane_tails_for_ttys", lambda ttys: {})

    assert DiplomatHost().at_job_capacity([]) is False


def test_the_knobs_reach_a_node_through_the_shared_config_file():
    """All three live in ~/.diplomat/config.json rather than a front-end's own
    store, because the mesh node that spends this machine's limit on peer-routed
    work is a separate Qt-less process reading the same file."""
    assert appconfig.auto_budget_gate() is True
    assert appconfig.auto_budget_confidence() == 95
    assert appconfig.auto_budget_floor_pct() == 20.0

    appconfig.set_bool(appconfig.AUTO_BUDGET_GATE, False)
    appconfig.set_int(appconfig.AUTO_BUDGET_CONFIDENCE, 99)
    appconfig.set_float(appconfig.AUTO_BUDGET_FLOOR_PCT, 35.5)

    assert appconfig.auto_budget_gate() is False
    assert appconfig.auto_budget_confidence() == 99
    assert appconfig.auto_budget_floor_pct() == 35.5

    # A hand-edited file is clamped on the way out, not trusted.
    raw = json.loads(appconfig.path().read_text(encoding="utf-8"))
    raw[appconfig.AUTO_BUDGET_CONFIDENCE] = 93
    raw[appconfig.AUTO_BUDGET_FLOOR_PCT] = 900
    appconfig.path().write_text(json.dumps(raw), encoding="utf-8")
    assert appconfig.auto_budget_confidence() == 95
    assert appconfig.auto_budget_floor_pct() == 100.0


def test_a_hand_edited_number_is_not_read_as_a_flag():
    """``1`` in the file is as likely a stray count as an intended "on"; the gate's
    default is what answers, not a coincidence of JSON types."""
    appconfig.set_int(appconfig.AUTO_BUDGET_GATE, 0)
    assert appconfig.auto_budget_gate() is True
