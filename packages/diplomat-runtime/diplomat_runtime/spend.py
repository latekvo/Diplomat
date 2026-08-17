"""How many dollars are left to spend on OpenRouter — the other currency a task
can be priced in.

:mod:`quota` asks Anthropic what *share of a rate-limit window* is unspent, which is
the only figure that account publishes. An OpenRouter account has no window: it has
money, and two ceilings that money runs out against.

* the **key limit** — the cap set on the API key itself (``limit``), which resets on
  a period the account chose, and which is what a key provisioned for automation is
  usually held to;
* the **credit balance** — what was bought minus what has been spent, account-wide,
  which does not reset at all.

Either can be the one that stops work, so both are read and the gate takes the
tighter (:func:`autobudget.decide`). A machine whose key is uncapped has only the
second; both are optional and a missing one is skipped rather than guessed.

The key is read from the runner's own store, never from Diplomat's config: Hermes
keeps its providers' credentials in ``~/.hermes/.env``, which is exactly where
:mod:`runner` says a secret belongs. Read per probe, because the operator can rotate
it under a running applet.

``DIPLOMAT_SPEND_PROBE=0`` disables it (the tests run offline and deterministic);
``DIPLOMAT_HERMES_ENV`` moves where the key is read from, the twin of the
``DIPLOMAT_HERMES_DB`` override :mod:`hermesstore` reads the session store through.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_KEY_URL = "https://openrouter.ai/api/v1/key"
_CREDITS_URL = "https://openrouter.ai/api/v1/credits"
_TIMEOUT_SECS = 4.0

#: Minimum gap between endpoint attempts, and the faster retry used while there is
#: no good reading yet — both for the reasons :mod:`quota` gives.
_TTL_SECS = 55.0
_RETRY_SECS = 10.0
#: How long a last-good reading keeps answering through failures. Shorter than the
#: dollars it reports can plausibly be spent, so a stale balance can't wave through
#: work the account can no longer pay for.
_KEEP_SECS = 1800.0

#: (last attempt, last good, reading).
_cache: dict = {"attempt": 0.0, "good": 0.0, "reading": None}


@dataclass(frozen=True)
class Balance:
    """Dollars left on each ceiling, or ``None`` for one this account doesn't have
    (an uncapped key) or that could not be read."""

    key_left: float | None = None
    credit_left: float | None = None

    @property
    def known(self) -> bool:
        return self.key_left is not None or self.credit_left is not None


def probe_enabled() -> bool:
    return os.environ.get("DIPLOMAT_SPEND_PROBE", "1") != "0"


def _reset_cache() -> None:
    """Test hook: forget any cached reading."""
    _cache.update(attempt=0.0, good=0.0, reading=None)


def env_path() -> Path:
    """Hermes' provider environment file, where its OpenRouter key is written."""
    override = os.environ.get("DIPLOMAT_HERMES_ENV")
    return Path(override) if override else Path.home() / ".hermes" / ".env"


def api_key() -> str | None:
    """The OpenRouter API key: Hermes' own env file, else this process's environment.

    The file wins because it is the one the *agent* will be billed through — a
    stale key exported into the applet's shell would otherwise price a task against
    an account the run never touches. Values may be quoted, as any env file's may.
    """
    try:
        for raw in env_path().read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip() != "OPENROUTER_API_KEY":
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value:
                return value
    except OSError:
        pass
    return os.environ.get("OPENROUTER_API_KEY") or None


def _get(url: str, key: str) -> dict | None:
    """One GET, unwrapped from OpenRouter's ``{"data": …}`` envelope. None on any
    failure — no network, a 401 after the key was rotated, a body that isn't an
    object."""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECS) as resp:  # noqa: S310
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — a probe failure must never take a poll down
        return None
    data = raw.get("data") if isinstance(raw, dict) else None
    return data if isinstance(data, dict) else None


def _money(raw: object) -> float | None:
    """A dollar figure from the payload, or None for anything that isn't one.
    Negatives are dropped rather than clamped: an account that reports owing money
    has told us something this gate has no reading for, and pricing it as "zero
    left" would be a guess wearing a measurement's clothes."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    return value if value >= 0 else None


def _fetch() -> Balance | None:
    """Both ceilings in one probe. ``None`` when neither endpoint answered, which is
    what keeps a previous good reading in service through a blip."""
    key = api_key()
    if not key:
        return None
    # `limit_remaining` is null for an uncapped key — a real answer meaning "this
    # ceiling does not exist", not a failed read, so it does not fail the probe.
    key_data = _get(_KEY_URL, key)
    credits = _get(_CREDITS_URL, key)
    if key_data is None and credits is None:
        return None
    key_left = _money((key_data or {}).get("limit_remaining"))
    credit_left = None
    if credits is not None:
        total = _money(credits.get("total_credits"))
        used = _money(credits.get("total_usage"))
        if total is not None and used is not None:
            credit_left = max(0.0, total - used)
    return Balance(key_left=key_left, credit_left=credit_left)


def balance() -> Balance:
    """Dollars left on each ceiling, or an empty :class:`Balance` when unavailable
    (probe disabled, no key, or offline past the keep window). Never raises."""
    if not probe_enabled():
        return Balance()
    now = time.monotonic()
    interval = _TTL_SECS if _cache["reading"] is not None else _RETRY_SECS
    if _cache["attempt"] == 0.0 or now - _cache["attempt"] >= interval:
        _cache["attempt"] = now
        reading = _fetch()
        if reading is not None and reading.known:
            _cache["good"] = now
            _cache["reading"] = reading
    if _cache["reading"] is not None and now - _cache["good"] > _KEEP_SECS:
        _cache["reading"] = None  # stale beyond trust
    return _cache["reading"] or Balance()
