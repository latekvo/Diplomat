"""How much of the Claude rate-limit windows is left — the other half of the
telemetry sample.

A GET against the OAuth usage endpoint (the same data Claude Code's ``/usage``
screen shows) using the OAuth access token Claude Code already holds, converted
to the fraction of each window still unspent. The endpoint's budget is per account
and every Claude Code session on the machine spends it, so a caller that can
afford to wait retries the refusals (``insist``). That, paired with the token
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

#: How old a reading may be before a caller probes again rather than taking it. The
#: sample cadence is already 15 minutes, so this only guards a panel that opens
#: repeatedly. An insisting caller's retries are inside the probe this gates, and
#: pace themselves.
_TTL_SECS = 55.0
#: Faster retry while there is no good reading yet, so a transient failure at
#: startup doesn't leave the screen blank for a full TTL.
_RETRY_SECS = 10.0
#: The extra attempts an *insisting* caller makes once the first is refused, and the
#: wait between them: six attempts over two and a half minutes.
#:
#: On a machine running several Claude Code sessions a single attempt is refused
#: (HTTP 429) more often than it succeeds, which is what leaves most of the telemetry
#: ledger's readings missing and the quota chart in fragments. The bucket was seen
#: refilling on a roughly two-minute cycle, so the waits are even rather than
#: doubling: what decides whether an attempt lands is how soon after a refill it
#: arrives.
_INSIST_ATTEMPTS = 5
_INSIST_WAIT_SECS = 30.0

#: (last attempt, last good, session fraction, week fraction).
_cache: dict = {"attempt": 0.0, "good": 0.0, "session": None, "week": None}
#: How many probe rounds have been made, and how many came back with a reading.
#: ``autofix.budget_decide`` SKIPS a ceiling with no reading and calls one with none
#: affordable, so a probe that stops answering does not gate less — it stops gating,
#: and nothing else on the machine looks wrong. Hence the ratio.
_probes: dict = {"rounds": 0, "readings": 0}
#: How long a last-good reading keeps answering through failures before the probe
#: admits it doesn't know. A sample carrying a stale fraction would price the
#: window against tokens that were spent after it, so this is deliberately short
#: relative to how long a window runs.
_KEEP_SECS = 1800.0


def probe_enabled() -> bool:
    return os.environ.get("DIPLOMAT_QUOTA_PROBE", "1") != "0"


def _reset_cache() -> None:
    """Test hook: forget any cached reading, and any memory of having probed."""
    _cache.update(attempt=0.0, good=0.0, session=None, week=None)
    _probes.update(rounds=0, readings=0)


def probe_stats() -> tuple[int, int]:
    """``(probe rounds made, rounds that came back with a reading)``."""
    return _probes["rounds"], _probes["readings"]


def _claude_dir():
    from .usagescan import claude_dir

    return claude_dir()


def _token_in(raw: object) -> str | None:
    """The ``claudeAiOauth.accessToken`` inside a credentials blob."""
    if not isinstance(raw, dict):
        return None
    token = (raw.get("claudeAiOauth") or {}).get("accessToken")
    return token if isinstance(token, str) and token else None


def _oauth_tokens() -> list[str]:
    """Claude Code's OAuth access tokens to try, in order: the credentials file it
    writes (Linux, and any explicit ``DIPLOMAT_CLAUDE_DIR`` sandbox), then the macOS
    login Keychain. Re-read per probe because Claude Code refreshes them as it runs.

    A LIST rather than the first one found, because the two sources drift apart and
    the file is the one that goes stale: on macOS Claude Code refreshes the Keychain
    item and never rewrites a ``.credentials.json`` an older login left behind. Asking
    only the file pins the probe to a dead credential for as long as that file exists,
    which is not a loud failure — ``autofix.budget_decide`` skips a ceiling it cannot
    read, so the dispatch budget just stops gating. Four days of it here, ending in a
    night of agents dispatched into an exhausted weekly window.

    The file stays FIRST, so pointing ``DIPLOMAT_CLAUDE_DIR`` at a fixture decides the
    probe's answer rather than being shadowed by the real login Keychain.
    """
    out: list[str] = []
    try:
        raw = json.loads((_claude_dir() / ".credentials.json").read_text(encoding="utf-8"))
        token = _token_in(raw)
        if token:
            out.append(token)
    except (OSError, ValueError, AttributeError):
        pass
    if sys.platform == "darwin":
        try:
            proc = subprocess.run(  # noqa: S603 — fixed argv, reads the user's own item
                ["security", "find-generic-password",
                 "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=_TIMEOUT_SECS, check=False)
            token = _token_in(json.loads(proc.stdout.strip() or "{}"))
            if token and token not in out:
                out.append(token)
        except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
            pass
    return out


def _fetch(token: str) -> dict | None:
    """One GET with one token. None on any failure — offline, a 401 after the token
    expired mid-window, a body that isn't an object."""
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


def _attempt(now: float) -> bool:
    """One round of the probe, folded into the cache. True when it came back with a
    reading.

    Every credential is tried until one answers: "the token was refused" and "the
    account has no reading" are the same silence to every caller above, and only this
    loop can tell them apart. It stops at the first that yields a window, so the extra
    request is spent only where the probe was already failing.
    """
    _cache["attempt"] = now
    tokens = _oauth_tokens()
    if not tokens:
        return False   # nothing to ask with: not a round, and not a refusal
    _probes["rounds"] += 1
    for token in tokens:
        payload = _fetch(token)
        session = _fraction_left((payload or {}).get("five_hour"))
        if session is None:
            continue
        _cache["good"] = now
        _cache["session"] = session
        _cache["week"] = _fraction_left(payload.get("seven_day"))
        _probes["readings"] += 1
        return True
    return False


def fractions_left(*, insist: bool = False) -> tuple[float | None, float | None]:
    """``(session, week)`` — the unspent fraction of the 5-hour and 7-day windows,
    or ``(None, None)`` when unavailable (probe disabled, no credentials, or
    offline past the keep window). Never raises.

    ``insist`` keeps trying (:data:`_INSIST_ATTEMPTS`) rather than settling for one
    refused attempt, so the call blocks for up to two and a half minutes. It is for a
    caller with its own long cadence and nobody waiting on it — the telemetry sample,
    which gets one turn every 15 minutes and leaves a hole in the ledger if it comes
    back empty. A caller gating a dispatch takes the single attempt: a stale reading
    now beats a fresh one after the agent should have started.
    """
    if not probe_enabled():
        return None, None
    now = time.monotonic()
    interval = _TTL_SECS if _cache["session"] is not None else _RETRY_SECS
    if _cache["attempt"] == 0.0 or now - _cache["attempt"] >= interval:
        # No token is the one failure retrying cannot fix.
        if not _attempt(now) and insist and _oauth_tokens():
            for _ in range(_INSIST_ATTEMPTS):
                time.sleep(_INSIST_WAIT_SECS)
                if _attempt(time.monotonic()):
                    break
        now = time.monotonic()
    if _cache["session"] is not None and now - _cache["good"] > _KEEP_SECS:
        _cache["session"] = _cache["week"] = None  # stale beyond trust
    return _cache["session"], _cache["week"]
