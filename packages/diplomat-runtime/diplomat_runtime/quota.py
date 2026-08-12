"""How much of the Claude rate-limit windows is left — the other half of the
telemetry sample.

One GET against the OAuth usage endpoint (the same data Claude Code's ``/usage``
screen shows) using the OAuth access token Claude Code already holds, converted
to the fraction of each window still unspent. That, paired with the token
counters from :mod:`usagescan`, is what lets the Telemetry screen say a task cost
a *share of the limit* rather than an unanchored token count: Anthropic publishes
a utilization percentage and never a token budget, and the budget is dynamic, so
the window has to be priced from what actually happened (:func:`telemetry.calibrate`).

This is deliberately Diplomat's own probe and not a call into
``szpontnet.usage``. The mesh is an optional add-on — the applet ships and runs
with the SzpontNet packages deleted outright, and CI proves it — so a screen that
imported the library for its numbers would be a screen that blanks on exactly the
machines least likely to have it. The two probes are twins by construction (same
endpoint, same beta header, same reading of ``utilization``); this one is smaller
because the mesh needs a routing signal and the panel only needs a reading.

``DIPLOMAT_QUOTA_PROBE=0`` disables it (the tests run offline and deterministic);
``DIPLOMAT_CLAUDE_DIR`` moves where the credentials are read from.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_BETA = "oauth-2025-04-20"
_TIMEOUT_SECS = 4.0

#: Minimum gap between endpoint attempts. The sample cadence is already 15
#: minutes, so this only guards a panel that opens repeatedly.
_TTL_SECS = 55.0
#: Faster retry while there is no good reading yet, so a transient failure at
#: startup doesn't leave the screen blank for a full TTL.
_RETRY_SECS = 10.0

#: (last attempt, last good, session fraction, week fraction).
_cache: dict = {"attempt": 0.0, "good": 0.0, "session": None, "week": None}
#: How long a last-good reading keeps answering through failures before the probe
#: admits it doesn't know. A sample carrying a stale fraction would price the
#: window against tokens that were spent after it, so this is deliberately short
#: relative to how long a window runs.
_KEEP_SECS = 1800.0


def probe_enabled() -> bool:
    return os.environ.get("DIPLOMAT_QUOTA_PROBE", "1") != "0"


def _reset_cache() -> None:
    """Test hook: forget any cached reading."""
    _cache.update(attempt=0.0, good=0.0, session=None, week=None)


def _claude_dir():
    from .usagescan import claude_dir

    return claude_dir()


def _oauth_token() -> str | None:
    """Claude Code's OAuth access token: the credentials file it writes (Linux, and
    any explicit ``DIPLOMAT_CLAUDE_DIR`` sandbox), else the macOS login Keychain.
    Re-read per probe because Claude Code refreshes it as it runs."""
    try:
        raw = json.loads((_claude_dir() / ".credentials.json").read_text(encoding="utf-8"))
        token = (raw.get("claudeAiOauth") or {}).get("accessToken")
        if isinstance(token, str) and token:
            return token
    except (OSError, ValueError, AttributeError):
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(  # noqa: S603 — fixed argv, reads the user's own item
                ["security", "find-generic-password",
                 "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=_TIMEOUT_SECS, check=False)
            raw = json.loads(out.stdout.strip() or "{}")
            token = (raw.get("claudeAiOauth") or {}).get("accessToken")
            if isinstance(token, str) and token:
                return token
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            pass
    return None


def _fetch() -> dict | None:
    """One GET. None on any failure — no token, offline, a 401 after the token
    expired mid-window, a body that isn't an object."""
    token = _oauth_token()
    if not token:
        return None
    req = urllib.request.Request(_USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": _BETA,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT_SECS) as resp:  # noqa: S310
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — a probe failure must never take a poll down
        return None
    return raw if isinstance(raw, dict) else None


def _fraction_left(window: object) -> float | None:
    """The unspent fraction of one window from its ``utilization`` percent.
    Clamped to [0, 1]: utilization can exceed 100 during a burst, and a negative
    fraction would show up as a negative task cost."""
    if not isinstance(window, dict):
        return None
    util = window.get("utilization")
    if not isinstance(util, (int, float)) or isinstance(util, bool):
        return None
    return round(max(0.0, min(1.0, 1.0 - float(util) / 100.0)), 4)


def fractions_left() -> tuple[float | None, float | None]:
    """``(session, week)`` — the unspent fraction of the 5-hour and 7-day windows,
    or ``(None, None)`` when unavailable (probe disabled, no credentials, or
    offline past the keep window). Never raises."""
    if not probe_enabled():
        return None, None
    now = time.monotonic()
    interval = _TTL_SECS if _cache["session"] is not None else _RETRY_SECS
    if _cache["attempt"] == 0.0 or now - _cache["attempt"] >= interval:
        _cache["attempt"] = now
        payload = _fetch()
        session = _fraction_left((payload or {}).get("five_hour"))
        if session is not None:
            _cache["good"] = now
            _cache["session"] = session
            _cache["week"] = _fraction_left(payload.get("seven_day"))
    if _cache["session"] is not None and now - _cache["good"] > _KEEP_SECS:
        _cache["session"] = _cache["week"] = None  # stale beyond trust
    return _cache["session"], _cache["week"]
