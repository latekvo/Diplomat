"""Can this machine afford to start another automatic task right now?

The pure arithmetic is :func:`autofix.budget_decide` and the pieces it is fed;
this is the assembly — the telemetry ledger folded and priced, the live quota
probe, and the operator's three knobs — in one place because it has two callers in
two processes, exactly as :func:`appconfig.auto_task_limit` does:

- the applet's dispatch gate (``Store.dispatch_agent``), for work the monitors
  originate here;
- the **mesh node**'s host (``szponthost.DiplomatHost.can_afford_job``), for work a
  peer routes in — which the applet never sees, and which spends this machine's
  rate limit just the same.

A budget the two disagreed on would be no budget at all, so neither of them
assembles these inputs itself.

Stdlib-only, like everything the node imports: the ledger is JSON, the probe is
``urllib``, and the knobs are the shared config file.
"""

from __future__ import annotations

import time

from . import autofix

#: How long one answer is reused. The quota probe behind it is already cached for
#: ~a minute (:data:`quota._TTL_SECS`), but the ledger fold and the summarize pass
#: are not, and a poll that finds eight units of owed work asks this eight times in
#: a row about a machine whose spend cannot have moved meanwhile.
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

    Priced against the 5-hour window the same way the Telemetry screen prices it:
    the ledger's finished-and-locally-run tasks, each as a share of the window it
    was spent from (:func:`telemetry.summarize`), give a mean and a spread, and the
    upper prediction bound on those is what one more task is required to fit
    inside. The 7-day window is the same tasks rescaled by the ratio of the two
    calibrations, since a task is a fixed number of tokens and only the divisor
    differs.

    What is LEFT comes from the probe rather than from the ledger's last sample:
    samples are written every 15 minutes, and a gate reading one of those would let
    a quarter-hour of spending — several agents' worth — go unnoticed.

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


def _decide_uncached() -> autofix.Budget:
    from . import appconfig, core, quota, telemetry

    try:
        session_left, week_left = quota.fractions_left()
    except Exception:  # noqa: BLE001 — a probe failure must never take a dispatch down
        session_left = week_left = None

    session_cost = week_cost = None
    try:
        model = telemetry.model()
        ledger = telemetry.load()
        # The screen's DEFAULT lookback, not its longest and not whatever range the
        # operator last flipped it to: the gate is a background decision in another
        # process, and the one thing that makes it auditable is that "Limit per task"
        # as the screen opens is the figure it was priced from. `steps`/`bin_count`
        # are floors — the series and the histogram are the screen's, and only the
        # distribution's moments and the two calibrations are read here.
        summary = telemetry.summarize(
            ledger, now=time.time(), days=float(model["defaultRangeDays"]),
            steps=2, bin_count=1, z=float(model["confidence"]["z"]),
        )
        session_cost, week_cost = _costs(
            summary,
            z=autofix.budget_z(appconfig.auto_budget_confidence()),
            min_sample=int(model["minSample"]),
        )
    except (core.CoreError, OSError, KeyError, IndexError, TypeError, ValueError):
        pass  # no price for a task — the floor answers instead

    return autofix.budget_decide(
        session_left_pct=None if session_left is None else 100.0 * session_left,
        week_left_pct=None if week_left is None else 100.0 * week_left,
        session_cost_pct=session_cost,
        week_cost_pct=week_cost,
        floor_pct=appconfig.auto_budget_floor_pct(),
    )


def _costs(summary, *, z: float, min_sample: int) -> tuple[float | None, float | None]:
    """``(session, week)`` upper bounds on what one more task costs, each as a
    percentage of its own window, or None where the ledger cannot price it.

    ``summary.per_task`` is already a share-of-the-5-hour-window distribution. The
    week's is that one scaled by ``sessionLimitTokens / weekLimitTokens``: both
    windows are priced in tokens from the same samples (:func:`telemetry.calibrate`),
    so a task worth *t* tokens is ``100·t/session`` of one and ``100·t/week`` of the
    other — a constant ratio, which mean, spread and bound all carry.
    """
    d = summary.per_task
    session = autofix.task_cost_bound(d.mean, d.sd, d.count, z=z, min_sample=min_sample)
    if session is None:
        return None, None
    week_limit = summary.week_limit_tokens
    session_limit = summary.session_limit_tokens
    if not week_limit or not session_limit or week_limit <= 0 or session_limit <= 0:
        return session, None  # the 5-hour window is priced, the weekly one isn't
    return session, session * session_limit / week_limit


def shortfall(budget: autofix.Budget) -> str:
    """Why a refusal refused, for the activity feed: which window is short, by how
    much, and whether the figure it was held to was measured or is the standing
    floor.

    A clause rather than a sentence, because what is being deferred differs by
    caller — the applet's own monitors, or a peer's job the node just declined —
    while the arithmetic behind it is the one thing both must quote identically."""
    from . import telemetry

    window = "7-day" if budget.window == autofix.WINDOW_WEEK else "5-hour"
    left = telemetry.percent(budget.left_pct)
    needed = telemetry.percent(budget.needed_pct)
    if budget.measured:
        return (f"{left} of the {window} rate limit left, and a task needs "
                f"up to {needed} of it")
    return (f"{left} of the {window} rate limit left, under the {needed} kept "
            f"in hand until the ledger can price a task")
