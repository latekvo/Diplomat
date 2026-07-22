"""Per-node load-balancing accounting: a 21-day rolling average of token usage
and the quota a node has left, account-type aware.

This is the data behind SzpontNet's load balancing: a dispatcher picks the node
with the most **surplus**, a *relative* burn-down ratio (budget left ÷ clock left
until the quota resets — see :meth:`NodeStats.surplus`), so work flows to whoever
is most flush against its own reset clock, not merely whoever holds the most raw
budget. Three quantities are advertised on the node's
:class:`~diplomat_app.mesh.protocol.NodeInfo`:

- **usageAvg** — an exponentially-weighted rolling average of consumption, in
  capacity units per day, with a ~21-day time constant. Implemented as a decaying
  reservoir ``acc`` (``acc *= exp(-Δdays / τ)`` per elapsed day, ``+= units`` per
  event); the steady-state of a constant rate ``r`` is ``acc = r·τ``, so
  ``usageAvg = acc / τ`` recovers the mean daily rate. Retained for display.
- **quotaLeft** — remaining capacity in the current quota window. Capacity is
  ``plan.weight × capacityPerWeight`` (Max 20× has 4× the room of Max 5×); the
  window rolls every ``quotaWindowDays`` and resets what's been used. Absolute
  token quotas are deliberately not modelled — Anthropic's limits are dynamic.
  Retained for display; when the node's REAL quota probe is live (usage.py) it is
  additionally capped by the binding rate-limit window — see
  :meth:`NodeStats.advertise`.
- **surplus** — the burn-down ratio routing actually ranks on. From the real
  probe's per-window reset instants when live, else the local bookkeeping window
  paced against its own roll (:meth:`NodeStats.local_window`).

State persists to ``~/.diplomat/mesh/stats.json`` (machine-local; only the derived
``advertise()`` view is gossiped). All time arithmetic takes an injectable
``now`` so tests can fast-forward without sleeping.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, replace

from . import config, identity
from .atomicjson import write_atomic
from .usage import QuotaWindow

_DAY_SECS = 86_400.0


def _finite(v: object) -> float:
    """``float(v)`` for a stats field, raising ``ValueError`` on a NON-FINITE result
    (∞/NaN — which ``float()`` accepts, unlike the ``int(inf)`` OverflowError). A
    hand-edited/corrupt stats.json or a ``set-attr`` edit carrying ``Infinity``/``NaN``
    (json.loads parses those RFC-8259-extension tokens by default) would otherwise put a
    non-finite ``acc`` into NodeStats, which both drives ``surplus()`` to ±inf/nan (so the
    node mis-ranks itself in surplus-first dispatch) AND rides ``advertise()`` verbatim
    into the snapshot, where json.dumps (allow_nan=True default) emits the bare
    RFC-8259-invalid token a strict reader (Swift MeshSnapshot.decode) rejects WHOLESALE,
    blanking the topology. Callers catch the ValueError (load -> _default; apply_stat_attrs
    -> skip the attr). Mirrors protocol._finite for the wire path."""
    f = float(v)  # type: ignore[arg-type]
    if not math.isfinite(f):
        raise ValueError("non-finite float in stats")
    return f


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

    def local_window(self, now: float) -> QuotaWindow:
        """The bookkeeping window as a paceable :class:`QuotaWindow` — the offline
        stand-in for a real rate-limit window. ``quota_used`` against capacity
        gives the remaining fraction, and the window rolls a fixed
        ``quotaWindowDays`` after it started, which gives the reset instant."""
        cap = self.capacity()
        span = self._window_secs()
        return QuotaWindow(
            frac_left=(self.quota_left() / cap) if cap > 0 else 1.0,
            resets_at=self.window_start + span,
            length_secs=span,
        )

    def surplus(self, now: float | None = None, pace: float | None = None) -> float:
        """This node's spare capacity as a burn-down ratio — budget left over
        clock left until the quota resets. See :meth:`QuotaWindow.pace`.

        Relative by construction, which is the whole point: an absolute "units
        remaining" figure ranks a node with a big balance above one whose smaller
        balance is about to expire unused, and starves the node that actually has
        room to spend. ``pace`` supplies the real probe's answer when it is live;
        otherwise this paces the local bookkeeping window."""
        if pace is not None:
            return pace
        now = time.time() if now is None else now
        return self.local_window(now).pace(now)

    def advertise(self, real_frac: float | None = None, pace: float | None = None,
                  now: float | None = None) -> dict:
        """The gossiped view — what rides on NodeInfo.stats.

        ``pace`` is the account's REAL burn-down ratio across its rate-limit
        windows (the tighter of the 5-hour session and 7-day week) when the OAuth
        quota probe is live, else None. It becomes the advertised ``surplus``,
        the figure peers rank on; without it the local bookkeeping window is
        paced instead, so an offline node still advertises a comparable number.

        ``real_frac`` is the matching remaining *fraction*, and still caps the
        advertised ``quotaLeft``: that field is retained for display and for
        peers on older builds, and must not overstate the room the account has.
        Heuristic fallback estimates deliberately do NOT cap — they can read 0
        for heavy users and would wrongly zero an actually-fresh node.
        """
        now = time.time() if now is None else now
        left = self.quota_left()
        if real_frac is not None:
            left = min(left, self.capacity() * max(0.0, min(1.0, real_frac)))
        return {
            "plan": self.plan,
            "usageAvg": round(self.usage_avg(), 4),
            "quotaLeft": round(left, 4),
            "surplus": round(self.surplus(now=now, pace=pace), 4),
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
    # A truthy valid-JSON non-object (a bare scalar/array/bool) makes raw.get below
    # raise AttributeError — which the coercion except tuple doesn't catch — so guard
    # the type here (empty/falsy already falls through to _default).
    if not isinstance(raw, dict) or not raw:
        return _default(now)
    try:
        st = NodeStats(
            plan=str(raw.get("plan") or config.accounts().get("defaultPlan", "max-5x")),
            acc=_finite(raw.get("acc", 0.0)),
            quota_used=_finite(raw.get("quotaUsed", 0.0)),
            window_start=_finite(raw.get("windowStart", now)),
            updated_at=_finite(raw.get("updatedAt", now)),
        )
    except (TypeError, ValueError, OverflowError):
        # A non-finite (∞/NaN) or otherwise-uncoercible field makes stats.json corrupt;
        # honour the corrupt-file-tolerance contract and fall back to the clean default.
        return _default(now)
    return st.decayed(now)


def save(st: NodeStats) -> None:
    """Atomic write (tmp + rename); best-effort, never raises."""
    write_atomic(stats_path(), {
        "plan": st.plan,
        "acc": st.acc,
        "quotaUsed": st.quota_used,
        "windowStart": st.window_start,
        "updatedAt": st.updated_at,
    })


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
    # Each numeric edit goes through _finite on BOTH the input AND the stored result: a
    # non-finite (∞/NaN) input — which float() accepts and the max(0.0, ...) clamp folds
    # only for NaN, not for +inf — is rejected, AND a FINITE input whose arithmetic
    # OVERFLOWS the field (usageAvg * _tau_days, or record()'s running sum — a float
    # product/sum overflows to inf with NO exception, unlike int) is rejected too. Either
    # raises ValueError and the edit is SKIPPED (the field keeps its prior value), so a
    # set-attr can't put a non-finite acc into this node's stats and poison surplus()/the
    # snapshot. quotaLeft stays finite by construction (capacity()-left, both ≥0 finite).
    if "quotaLeft" in attrs:
        try:
            left = max(0.0, _finite(attrs["quotaLeft"]))
            st = replace(st, quota_used=max(0.0, st.capacity() - left), window_start=now)
        except (TypeError, ValueError, OverflowError):
            pass
    if "usageAvg" in attrs:
        try:
            st = replace(st, acc=_finite(max(0.0, _finite(attrs["usageAvg"])) * st._tau_days()))
        except (TypeError, ValueError, OverflowError):
            pass
    if "usage" in attrs:
        try:
            booked = record(st, _finite(attrs["usage"]), now=now)
            _finite(booked.acc); _finite(booked.quota_used)  # a finite delta can still OVERFLOW the running sum
            st = booked
        except (TypeError, ValueError, OverflowError):
            pass
    return st


def touches_stats(attrs: dict) -> bool:
    """Whether a set-attr edit carries any stat key (so the node knows to route
    it through here as well as through identity)."""
    return any(k in attrs for k in ("plan", "quotaLeft", "usageAvg", "usage"))
