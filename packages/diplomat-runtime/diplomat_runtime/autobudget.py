"""Can this machine afford to start another automatic task right now?

The pure arithmetic is :func:`autofix.budget_decide` and the pieces it is fed;
this is the assembly — the telemetry ledger folded and priced, the live probe, and
the operator's knobs — in one place because it has two callers in two processes,
exactly as :func:`appconfig.auto_task_limit` does:

- the applet's dispatch gate (``Store.dispatch_agent``), for work the monitors
  originate here;
- the **mesh node**'s host (``szponthost.DiplomatHost.can_afford_job``), for work a
  peer routes in — which the applet never sees, and which is paid for out of this
  machine's account just the same.

A budget the two disagreed on would be no budget at all, so neither of them
assembles these inputs itself.

**Which currency.** What an agent spends depends on what runs it, so the runner
picks the ceilings and the unit both sides of the comparison are in
(:func:`_decide_uncached`): Claude Code draws on Anthropic rate-limit windows,
published only as a percentage; every other runner is billed in money by whichever
provider it is logged into. Neither reading substitutes for the other — a Hermes
task held against a Claude window is gated on a limit it never touches, and a
machine not logged into Claude Code at all would have no gate whatsoever.

Stdlib-only, like everything the node imports: the ledger is JSON, the probes are
``urllib``, and the knobs are the shared config file.
"""

from __future__ import annotations

import time

from . import autofix

#: How long one answer is reused. The probe behind it is already cached for ~a
#: minute (:data:`quota._TTL_SECS`, :data:`spend._TTL_SECS`), but the ledger fold and
#: the summarize pass are not, and a poll that finds eight units of owed work asks
#: this eight times in a row about a machine whose spend cannot have moved meanwhile.
_TTL_SECS = 20.0

#: (expiry, answer) — see :func:`decide`.
_cache: tuple[float, autofix.Budget] | None = None


def _reset_cache() -> None:
    """Test hook: forget the cached answer."""
    global _cache
    _cache = None


def enabled() -> bool:
    """Whether the gate is switched on at all (Settings → PR AUTO-FIX)."""
    from . import appconfig

    return appconfig.auto_budget_gate()


def decide(now: float | None = None) -> autofix.Budget:
    """The budget verdict for one more automatic task, from live evidence.

    Never raises. A probe that cannot answer, a ledger that will not parse and an
    unreadable config all degrade to the same place: no measurement, and
    :func:`autofix.budget_decide`'s fail-open.
    """
    global _cache
    now = time.time() if now is None else now
    if _cache is not None and now < _cache[0]:
        return _cache[1]
    budget = _decide_uncached()
    _cache = (now + _TTL_SECS, budget)
    return budget


def _summary():
    """The ledger, summarized the way the Telemetry screen summarizes it.

    The screen's DEFAULT lookback, not its longest and not whatever range the
    operator last flipped it to: the gate is a background decision in another
    process, and the one thing that makes it auditable is that "Limit per task" as
    the screen opens is the figure it was priced from. ``steps``/``bin_count`` are
    floors — the series and the histogram are the screen's, and only the
    distributions' moments and the two calibrations are read here.
    """
    from . import telemetry

    model = telemetry.model()
    return telemetry.summarize(
        telemetry.load(), now=time.time(), days=float(model["defaultRangeDays"]),
        steps=2, bin_count=1, z=float(model["confidence"]["z"]),
    ), int(model["minSample"])


def _decide_uncached() -> autofix.Budget:
    from . import runner

    return (_decide_claude() if runner.selected() == runner.CLAUDE
            else _decide_money())


def _decide_claude() -> autofix.Budget:
    """The verdict for a machine spending Anthropic rate-limit windows.

    Priced against both windows the same way the Telemetry screen prices them: the
    ledger's finished-and-locally-run tasks, each as a share of the window it was
    spent from, give a mean and a spread per window, and the upper prediction bound
    on those is what one more task is required to fit inside.

    What is LEFT comes from the probe rather than from the ledger's last sample:
    samples are written every 15 minutes, and a gate reading one of those would let
    a quarter-hour of spending — several agents' worth — go unnoticed.
    """
    from . import appconfig, core, quota

    try:
        session_left, week_left = quota.fractions_left()
    except Exception:  # noqa: BLE001 — a probe failure must never take a dispatch down
        session_left = week_left = None

    session_cost = week_cost = None
    try:
        summary, min_sample = _summary()
        session_cost, week_cost = _costs(
            summary, z=autofix.budget_z(appconfig.auto_budget_confidence()),
            min_sample=min_sample)
    except (core.CoreError, OSError, KeyError, IndexError, TypeError, ValueError):
        pass  # no price for a task — the floor answers instead

    return autofix.budget_decide(
        [(autofix.WINDOW_SESSION,
          None if session_left is None else 100.0 * session_left, session_cost),
         (autofix.WINDOW_WEEK,
          None if week_left is None else 100.0 * week_left, week_cost)],
        floor=appconfig.auto_budget_floor_pct(), unit=autofix.UNIT_PCT)


def _decide_money() -> autofix.Budget:
    """The verdict for a machine whose agents are billed in money.

    The same statistic in the other currency: the ledger's finished tasks, each at
    what the provider charged for it, bound the cost of one more, and what is left is
    what the account has on each of its two ceilings (:mod:`spend`). Both are dollars
    already, so unlike the windows above neither needs converting into the other's
    terms — and the same task cost gates both, since a dollar spent is a dollar off
    each of them.

    The key's own cap is listed first, so it is the one named when both bind
    equally: it is the ceiling that refills on its own, and naming it tells the
    operator to wait rather than to go and top the account up.

    **A ledger with no billed task at all decides nothing.** The reserve exists to
    hold work back while a machine that spends money has not yet been measured, and
    applying it to a machine that spends none — a runner pointed at a local model, or
    one Diplomat cannot price — would hold that machine's work against an account it
    never draws on, purely because a key for that account is on disk. So the standing
    reserve engages only once something has actually been charged here; before that
    this is the same fail-open a silent probe gets, and the task cap is still in
    front of it.
    """
    from . import appconfig, core, spend

    cost = None
    try:
        summary, min_sample = _summary()
        d = summary.per_task_usd
        if d.count == 0:
            return autofix.Budget(affordable=True, unit=autofix.UNIT_USD)
        cost = autofix.task_cost_bound(
            d.mean, d.sd, d.count,
            z=autofix.budget_z(appconfig.auto_budget_confidence()),
            min_sample=min_sample)
    except (core.CoreError, OSError, KeyError, IndexError, TypeError, ValueError):
        # A ledger that will not parse cannot show the evidence the reserve needs
        # either, so it lands where an unbilled machine does rather than on a floor
        # it was never shown to owe.
        return autofix.Budget(affordable=True, unit=autofix.UNIT_USD)

    try:
        balance = spend.balance()
    except Exception:  # noqa: BLE001 — a probe failure must never take a dispatch down
        balance = spend.Balance()

    return autofix.budget_decide(
        [(autofix.WINDOW_KEY, balance.key_left, cost),
         (autofix.WINDOW_CREDITS, balance.credit_left, cost)],
        floor=appconfig.auto_budget_reserve_usd(), unit=autofix.UNIT_USD)


def _costs(summary, *, z: float, min_sample: int) -> tuple[float | None, float | None]:
    """``(session, week)`` upper bounds on what one more task costs, each as a
    percentage of its own window, or None where the ledger cannot price that window.

    One bound per window from that window's own distribution — the same pair the
    Telemetry screen draws. Each is priced from its own quota readings, so a week the
    samples cannot price leaves the 5-hour gate measured, and the reverse.
    """
    return (
        autofix.task_cost_bound(summary.per_task.mean, summary.per_task.sd,
                                summary.per_task.count, z=z, min_sample=min_sample),
        autofix.task_cost_bound(summary.per_task_week.mean, summary.per_task_week.sd,
                                summary.per_task_week.count, z=z,
                                min_sample=min_sample),
    )


#: What each ceiling is called in the one line the feed prints about it.
_CEILINGS = {
    autofix.WINDOW_SESSION: "5-hour rate limit",
    autofix.WINDOW_WEEK: "7-day rate limit",
    autofix.WINDOW_KEY: "OpenRouter key limit",
    autofix.WINDOW_CREDITS: "OpenRouter credit balance",
}


def shortfall(budget: autofix.Budget) -> str:
    """Why a refusal refused, for the activity feed: which ceiling is short, by how
    much, and whether the figure it was held to was measured or is the standing
    floor.

    A clause rather than a sentence, because what is being deferred differs by
    caller — the applet's own monitors, or a peer's job the node just declined —
    while the arithmetic behind it is the one thing both must quote identically."""
    from . import telemetry

    ceiling = _CEILINGS.get(budget.window, "limit")
    fmt = telemetry.money if budget.unit == autofix.UNIT_USD else telemetry.percent
    left, needed = fmt(budget.left), fmt(budget.needed)
    if budget.measured:
        return (f"{left} of the {ceiling} left, and a task needs "
                f"up to {needed} of it")
    return (f"{left} of the {ceiling} left, under the {needed} kept "
            f"in hand until the ledger can price a task")
