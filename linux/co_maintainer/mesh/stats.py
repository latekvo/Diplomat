"""Per-node load-balancing accounting: a 21-day rolling average of token usage
and the quota a node has left, account-type aware.

This is the data behind SzpontNet's load balancing: a dispatcher picks the node
with the most **surplus** (``quotaLeft − usageAvg``, in plan-relative units), so
work flows to whoever has spare budget. Two quantities are tracked locally and
advertised on the node's :class:`~co_maintainer.mesh.protocol.NodeInfo`:

- **usageAvg** — an exponentially-weighted rolling average of consumption, in
  capacity units per day, with a ~21-day time constant. Implemented as a decaying
  reservoir ``acc`` (``acc *= exp(-Δdays / τ)`` per elapsed day, ``+= units`` per
  event); the steady-state of a constant rate ``r`` is ``acc = r·τ``, so
  ``usageAvg = acc / τ`` recovers the mean daily rate.
- **quotaLeft** — remaining capacity in the current quota window. Capacity is
  ``plan.weight × capacityPerWeight`` (Max 20× has 4× the room of Max 5×); the
  window rolls every ``quotaWindowDays`` and resets what's been used. Absolute
  token quotas are deliberately not modelled — Anthropic's limits are dynamic —
  so everything is compared in plan-relative units. When the node's REAL quota
  probe is live (usage.py), the *advertised* quotaLeft is additionally capped by
  the binding rate-limit window — see :meth:`NodeStats.advertise`.

State persists to ``~/.argent/mesh/stats.json`` (machine-local; only the derived
``advertise()`` view is gossiped). All time arithmetic takes an injectable
``now`` so tests can fast-forward without sleeping.
"""

from __future__ import annotations

import json
import math
import os
import time
from dataclasses import dataclass, replace

from . import config, identity

_DAY_SECS = 86_400.0


def stats_path():
    return identity.mesh_dir() / "stats.json"


@dataclass(frozen=True)
class NodeStats:
    """This node's persisted accounting state."""

    plan: str
    acc: float          # decaying usage reservoir (units); usageAvg = acc / τdays
    quota_used: float   # units consumed in the current window
    window_start: float # wall-clock start of the current quota window
    updated_at: float   # wall-clock of the last decay/record

    # MARK: - derived quantities

    def capacity(self) -> float:
        acc = config.accounts()
        return config.plan_weight(self.plan) * float(acc.get("capacityPerWeight", 1.0))

    def _tau_days(self) -> float:
        return float(config.accounts().get("usageTimeConstantDays", 21.0)) or 21.0

    def _window_secs(self) -> float:
        return float(config.accounts().get("quotaWindowDays", 7.0)) * _DAY_SECS

    def decayed(self, now: float) -> "NodeStats":
        """Advance the EMA decay and roll the quota window forward to ``now``.
        Pure — returns a new snapshot, persists nothing."""
        dt = max(0.0, now - self.updated_at)
        acc = self.acc * math.exp(-(dt / _DAY_SECS) / self._tau_days())
        quota_used, window_start = self.quota_used, self.window_start
        win = self._window_secs()
        if win > 0 and now - window_start >= win:
            quota_used, window_start = 0.0, now  # fresh window: the budget resets
        return replace(self, acc=acc, quota_used=quota_used,
                       window_start=window_start, updated_at=now)

    def usage_avg(self) -> float:
        return self.acc / self._tau_days()

    def quota_left(self) -> float:
        return max(0.0, self.capacity() - self.quota_used)

    def surplus(self) -> float:
        return self.quota_left() - self.usage_avg()

    def advertise(self, real_frac: float | None = None) -> dict:
        """The gossiped view — what rides on NodeInfo.stats.

        ``real_frac`` is the account's REAL remaining fraction in its binding
        rate-limit window — min(5-hour session, 7-day week) — when the OAuth
        quota probe is live, else None. It caps the advertised ``quotaLeft``:
        whatever the local bookkeeping says, the account has no more room than
        its tightest real window, so surplus-first dispatch can't route work to
        a node that would run dry mid-task (e.g. 2% of the session left but 80%
        of the week — the session gates the next job, not the week). Heuristic
        fallback estimates deliberately do NOT cap: they can read 0 for heavy
        users and would wrongly zero an actually-fresh node's surplus."""
        left = self.quota_left()
        if real_frac is not None:
            left = min(left, self.capacity() * max(0.0, min(1.0, real_frac)))
        return {
            "plan": self.plan,
            "usageAvg": round(self.usage_avg(), 4),
            "quotaLeft": round(left, 4),
        }


def _default(now: float) -> NodeStats:
    return NodeStats(
        plan=str(config.accounts().get("defaultPlan", "max-5x")),
        acc=0.0, quota_used=0.0, window_start=now, updated_at=now,
    )


def load(now: float | None = None) -> NodeStats:
    """Load (or initialise) this node's accounting, decayed to ``now``."""
    now = time.time() if now is None else now
    raw: dict = {}
    try:
        raw = json.loads(stats_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    if not raw:
        return _default(now)
    try:
        st = NodeStats(
            plan=str(raw.get("plan") or config.accounts().get("defaultPlan", "max-5x")),
            acc=float(raw.get("acc", 0.0)),
            quota_used=float(raw.get("quotaUsed", 0.0)),
            window_start=float(raw.get("windowStart", now)),
            updated_at=float(raw.get("updatedAt", now)),
        )
    except (TypeError, ValueError):
        return _default(now)
    return st.decayed(now)


def save(st: NodeStats) -> None:
    """Atomic write (tmp + rename); best-effort, never raises."""
    path = stats_path()
    payload = {
        "plan": st.plan,
        "acc": st.acc,
        "quotaUsed": st.quota_used,
        "windowStart": st.window_start,
        "updatedAt": st.updated_at,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def record(st: NodeStats, units: float, now: float | None = None) -> NodeStats:
    """Book ``units`` of consumption: decay to ``now``, then add to both the
    usage reservoir and the quota window."""
    now = time.time() if now is None else now
    st = st.decayed(now)
    return replace(st, acc=st.acc + max(0.0, units),
                   quota_used=st.quota_used + max(0.0, units))


def apply_stat_attrs(st: NodeStats, attrs: dict, now: float | None = None) -> NodeStats:
    """Apply operator/CLI edits (a subset of a ``set-attr`` message). Recognised
    keys: ``plan`` (switch plan), ``quotaLeft`` (set remaining directly),
    ``usageAvg`` (set the rolling average directly), ``usage`` (book a delta).
    Unknown keys are ignored, like every other attr edit."""
    now = time.time() if now is None else now
    st = st.decayed(now)
    if isinstance(attrs.get("plan"), str) and attrs["plan"]:
        st = replace(st, plan=attrs["plan"])
    if "quotaLeft" in attrs:
        try:
            left = max(0.0, float(attrs["quotaLeft"]))
            st = replace(st, quota_used=max(0.0, st.capacity() - left), window_start=now)
        except (TypeError, ValueError):
            pass
    if "usageAvg" in attrs:
        try:
            st = replace(st, acc=max(0.0, float(attrs["usageAvg"])) * st._tau_days())
        except (TypeError, ValueError):
            pass
    if "usage" in attrs:
        try:
            st = record(st, float(attrs["usage"]), now=now)
        except (TypeError, ValueError):
            pass
    return st


def touches_stats(attrs: dict) -> bool:
    """Whether a set-attr edit carries any stat key (so the node knows to route
    it through here as well as through identity)."""
    return any(k in attrs for k in ("plan", "quotaLeft", "usageAvg", "usage"))
