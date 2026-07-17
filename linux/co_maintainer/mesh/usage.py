"""Token-budget signal: real Anthropic quota when reachable, local logs as fallback.

Primary source — the **OAuth usage endpoint** (the same data Claude Code's
``/usage`` screen shows): utilization percentages for the account's real
rate-limit windows, the 5-hour session and the 7-day week. The probe reads the
Claude Code OAuth access token (``~/.claude/.credentials.json``, or the macOS
Keychain item ``Claude Code-credentials``), GETs the endpoint, and converts each
window's utilization into a remaining fraction. Results are cached ~1 min and a
last-good read outlives transient failures, so the node never hammers the
endpoint nor flaps on a dropped packet. ``CO_MAINTAINER_MESH_OAUTH_PROBE=0`` disables
the probe entirely (the tests run offline and deterministic).

Fallback — when no token is available (or the probe is disabled/offline for
long), a node measures its *own* recent consumption instead: Claude Code appends
every turn to ``~/.claude/projects/**/*.jsonl`` with a ``usage`` block. This
module sums the tokens spent in the last ``accounts.usageWindowHours`` and
compares them to a heuristic per-plan ceiling (``plan.weight ×
accounts.tokensPerWeight``). That estimate is deliberately rough (real limits
are dynamic and account-specific) — it exists so an offline node still degrades
to *some* ok/low/out signal rather than none.

Stdlib-only; honours ``HOME`` and ``CO_MAINTAINER_CLAUDE_DIR`` (so the tests, which
sandbox HOME, never read a developer's real logs or credentials).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from . import config

_HOUR_SECS = 3600.0

# MARK: - real quota probe (OAuth usage endpoint)

_OAUTH_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_OAUTH_BETA = "oauth-2025-04-20"
_PROBE_TIMEOUT_SECS = 4.0
# Min interval between endpoint attempts — the node refreshes its token state
# every 30s, so this makes roughly every other refresh hit the network (the
# cadence Claude statusline tools poll at).
_PROBE_TTL_SECS = 55.0
# Faster retry while there is NO last-good read yet (e.g. the seed fetch hit a
# transient failure): stuck on the rough heuristic, the next refresh should try
# again rather than wait out the full TTL.
_PROBE_RETRY_SECS = 10.0
# How long a last-good read keeps answering through failures before the module
# gives up and falls back to the local heuristic.
_PROBE_KEEP_SECS = 1800.0

_probe_cache: dict = {"attempt": 0.0, "good": 0.0, "session": None, "week": None}


def probe_enabled() -> bool:
    """CO_MAINTAINER_MESH_OAUTH_PROBE=0 turns the network probe off (tests, air-gapped)."""
    return os.environ.get("CO_MAINTAINER_MESH_OAUTH_PROBE", "1") != "0"


def _reset_probe_cache() -> None:
    """Test hook: forget any cached probe result."""
    _probe_cache.update(attempt=0.0, good=0.0, session=None, week=None)


def claude_dir() -> Path:
    """Claude Code's home (credentials + transcripts). CO_MAINTAINER_CLAUDE_DIR overrides."""
    override = os.environ.get("CO_MAINTAINER_CLAUDE_DIR")
    return Path(override) if override else Path.home() / ".claude"


def _oauth_token() -> str | None:
    """The Claude Code OAuth access token: the credentials file where Claude Code
    writes it (Linux, and any explicit CO_MAINTAINER_CLAUDE_DIR sandbox), else the macOS
    login-Keychain item. Claude Code refreshes the token as it runs, so re-reading
    per probe always picks up the freshest one. None when absent — probe skipped."""
    try:
        raw = json.loads((claude_dir() / ".credentials.json").read_text(encoding="utf-8"))
        tok = (raw.get("claudeAiOauth") or {}).get("accessToken")
        if isinstance(tok, str) and tok:
            return tok
    except (OSError, ValueError):
        pass
    if sys.platform == "darwin":
        try:
            out = subprocess.run(  # noqa: S603 — fixed argv, reads the user's own item
                ["security", "find-generic-password",
                 "-s", "Claude Code-credentials", "-w"],
                capture_output=True, text=True, timeout=_PROBE_TIMEOUT_SECS, check=False)
            raw = json.loads(out.stdout.strip() or "{}")
            tok = (raw.get("claudeAiOauth") or {}).get("accessToken")
            if isinstance(tok, str) and tok:
                return tok
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return None


def _fetch_usage_payload() -> dict | None:
    """One GET against the OAuth usage endpoint; None on any failure (no token,
    offline, 401 after the token expired mid-window, garbage body)."""
    token = _oauth_token()
    if not token:
        return None
    req = urllib.request.Request(_OAUTH_USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": _OAUTH_BETA,
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT_SECS) as resp:  # noqa: S310
            raw = json.loads(resp.read().decode("utf-8"))
    except Exception:  # noqa: BLE001 — a probe failure must never take the node down
        return None
    return raw if isinstance(raw, dict) else None


def _frac_left(window: object) -> float | None:
    """A window's remaining fraction from its ``utilization`` percent (0–100+)."""
    if not isinstance(window, dict):
        return None
    util = window.get("utilization")
    if not isinstance(util, (int, float)):
        return None
    return round(max(0.0, min(1.0, 1.0 - float(util) / 100.0)), 4)


def quota_left() -> tuple[float | None, float | None]:
    """(session_frac_left, week_frac_left) from the account's REAL rate-limit
    windows (5-hour session, 7-day week). (None, None) when unavailable —
    disabled, no credentials, or offline past the keep window."""
    if not probe_enabled():
        return None, None
    now = time.monotonic()
    interval = _PROBE_TTL_SECS if _probe_cache["session"] is not None else _PROBE_RETRY_SECS
    if now - _probe_cache["attempt"] >= interval or _probe_cache["attempt"] == 0.0:
        _probe_cache["attempt"] = now
        payload = _fetch_usage_payload()
        session = _frac_left((payload or {}).get("five_hour"))
        if session is not None:
            _probe_cache["good"] = now
            _probe_cache["session"] = session
            _probe_cache["week"] = _frac_left(payload.get("seven_day"))
    if _probe_cache["session"] is not None and now - _probe_cache["good"] > _PROBE_KEEP_SECS:
        _probe_cache["session"] = _probe_cache["week"] = None  # stale beyond trust
    return _probe_cache["session"], _probe_cache["week"]


# MARK: - fallback heuristic (local transcript consumption)


def claude_projects_dir() -> Path:
    """Where Claude Code writes its per-session transcripts."""
    return claude_dir() / "projects"


def _token_cost(usage: dict) -> float:
    """Billable-ish token count for one turn: input + output + cache creation.
    Cache *reads* are deliberately excluded — they're huge and cheap, and counting
    them would swamp the signal."""
    total = 0.0
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens"):
        try:
            total += float(usage.get(key, 0) or 0)
        except (TypeError, ValueError):
            continue
    return total


def _entry_time(rec: dict) -> float | None:
    """Wall-clock epoch of a transcript record from its ISO ``timestamp``; None if
    absent/unparseable (such a record just isn't counted)."""
    ts = rec.get("timestamp")
    if not isinstance(ts, str) or not ts:
        return None
    try:
        # Python's fromisoformat handles the trailing 'Z' only from 3.11; normalise.
        from datetime import datetime

        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def window_tokens(now: float | None = None, window_hours: float | None = None) -> float:
    """Total tokens consumed across all local Claude sessions within the trailing
    window. Best-effort: unreadable/garbage files and lines are skipped, never
    fatal (this feeds a coarse ok/low/out signal, not billing)."""
    now = time.time() if now is None else now
    if window_hours is None:
        window_hours = config.usage_window_hours()
    cutoff = now - window_hours * _HOUR_SECS
    root = claude_projects_dir()
    if not root.is_dir():
        return 0.0

    total = 0.0
    for path in root.rglob("*.jsonl"):
        try:
            # Cheap pre-filter: a file untouched since the cutoff holds nothing in
            # the window (transcripts are append-only).
            if path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue
        try:
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    usage = ((rec.get("message") or {}).get("usage")
                             if isinstance(rec.get("message"), dict) else None)
                    if usage is None:
                        usage = rec.get("usage")
                    if not isinstance(usage, dict):
                        continue
                    et = _entry_time(rec)
                    if et is not None and et < cutoff:
                        continue
                    total += _token_cost(usage)
        except OSError:
            continue
    return total


def token_ceiling(plan: str) -> float:
    """Heuristic budget for the trailing window: ``plan.weight × tokensPerWeight``.
    Rough by design (real limits are dynamic); tune ``tokensPerWeight`` in the model."""
    return config.plan_weight(plan) * config.tokens_per_weight()


def fraction_remaining(plan: str, now: float | None = None) -> float:
    """1 − used/ceiling, clamped to [0, 1]. 1.0 = fresh, 0.0 = at/over the ceiling."""
    ceiling = token_ceiling(plan)
    if ceiling <= 0:
        return 1.0
    used = window_tokens(now)
    return max(0.0, min(1.0, 1.0 - used / ceiling))


def state_from_fraction(frac: float) -> str:
    """Map a remaining-fraction to the coarse token state the mesh routes around."""
    if frac <= 0.0:
        return "out"
    if frac < config.low_threshold():
        return "low"
    return "ok"


def token_state(plan: str, now: float | None = None
                ) -> tuple[str, float, float | None, float | None]:
    """(ok|low|out, binding_fraction, session_frac, week_frac) for this machine.

    Prefers the account's REAL quota (OAuth usage endpoint; the binding fraction
    is the tighter of the session/week windows). Falls back to the local log
    heuristic — then session/week are None, marking the fraction an estimate."""
    session, week = quota_left()
    if session is not None:
        frac = min(f for f in (session, week) if f is not None)
        return state_from_fraction(frac), frac, session, week
    frac = fraction_remaining(plan, now)
    return state_from_fraction(frac), frac, None, None
