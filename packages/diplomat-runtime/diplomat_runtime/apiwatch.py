"""Pure predicates over an agent pane's visible buffer — the Python twin of
DiplomatCore's ApiErrorMatch.swift and AgentActivity.swift (plus the backoff
constants that live in Store.swift on macOS).

Two questions are asked of the same tail, and both are answered here because both
are read off the CLI's own status bar: whether the session is *stalled on an API
error* (and how long to wait before nudging it again), and whether it is *working
at all* — a session that has finished its turn sits at its prompt indefinitely,
which is neither an error nor a reason to keep holding a slot of the task cap.

The two questions have different reach across runners, and the difference is
deliberate. **Busy** is answered for all three, because getting it wrong empties
the task cap under a machine that is full. **Stalled on an API error** is answered
only for Claude Code, whose error banners these patterns were read off. A foreign
runner surfaces a failed turn as the provider's own JSON (OpenCode:
``Unauthorized: {"error":{…,"type":"api_error",…}}``) — one shape per provider,
and none of them observed here beyond an auth rejection, which is precisely the
kind of permanent failure the quota banners below are excluded for. So a foreign
agent that hits a transient error is not nudged. It is not stranded either, and
its own session is what says so rather than this: an errored turn is stamped
completed like any other (OpenCode) or carries a terminal ``finish_reason``
(Hermes), so it reads as idle, gives its bay back, and the monitor that owed the
work dispatches it again on a later tick.

Kept deterministic and side-effect-free so it's unit-testable in isolation: the
terminal reads/writes live in :mod:`tmuxwatch`, and the scan/dispatch/persistence
in the Store.
"""

from __future__ import annotations

import re

# The nudge submitted to a stalled session (verbatim from ApiErrorWatcher.swift so
# both platforms send the identical line).
CONTINUE_MESSAGE = "Go on, there was a Claude API error, continue as normal"

# How many non-empty visible lines from the bottom we scan for the error. A tall
# prompt/status box under the error line can push it ~17 lines up, so 30 keeps it in
# view while still staying out of older scrollback (matches scannedTailLines).
SCANNED_TAIL_LINES = 30

# Backoff before re-nudging the SAME pane, mirroring Store.swift: base 2 min,
# doubling on every successive retry to a session that keeps erroring, capped at 3h
# so an agent stuck on a persistent overload isn't hammered forever.
APIWATCH_COOLDOWN = 120.0
APIWATCH_MAX_BACKOFF = 3 * 60 * 60.0  # 3h

# Connectivity failures the CLI prints with NO status code — e.g.
#   "API Error: Unable to connect to API" / "API Error: Connection error."
# so a dropped/returning network resumes the agent just like a 5xx would.
_CONNECTIVITY_PHRASES = [
    "unable to connect", "connection error", "connection refused",
    "connection reset", "connection timed out", "network error",
    "fetch failed", "econnrefused", "enotfound", "etimedout", "getaddrinfo",
]

# Out-of-token-quota banners. The CLI prints these WITHOUT any "API Error" prefix.
# They're detected only to be IGNORED: an out-of-quota agent can't progress until
# its window resets, so nudging it does nothing but churn. A quota banner also
# SUPPRESSES a co-occurring API-error match in the same tail.
_QUOTA_PHRASES = [
    "usage limit reached",
    "hour limit reached",     # "5-hour limit reached ∙ resets …"
    "weekly limit reached",
    "session limit reached",
    "limit will reset at",    # "Your limit will reset at 4pm (…)"
    "out of tokens",
]
# "You've hit your weekly/usage/session/5-hour limit" — the "hit your … limit"
# family, matched with a small gap so new limit names keep matching.
_HIT_YOUR_LIMIT = re.compile(r"hit your [a-z0-9\- ]{0,16}limit")
_API_ERROR_CODE = re.compile(r"API Error:?\s*[0-9]{3}")
_BARE_429 = re.compile(r"\b429\b")


def looks_like_api_error(text: str) -> bool:
    """True when ``text`` shows a transient Claude API error the watcher should nudge
    past — a server 5xx / rate-limit ("API Error: <3-digit code>"), a status-page
    error, or a codeless connectivity failure (network out, DNS, timeout).

    Out-of-quota banners return False: nudging a quota-limited session does nothing
    until the window resets, so the watcher intentionally leaves them alone. A quota
    banner also SUPPRESSES any API-error text in the same tail.
    """
    lower = text.lower()
    # Quota banner present ⇒ ignore this session entirely (and suppress any stray
    # API-error text sharing the tail).
    if any(p in lower for p in _QUOTA_PHRASES):
        return False
    if _HIT_YOUR_LIMIT.search(lower):
        return False
    # "API Error: <3-digit code>" — the exact CLI format (529/500/503/429/…).
    if _API_ERROR_CODE.search(text):
        return True
    # A bare "429 Rate limited" banner. Newer CLI builds print a rate-limit error
    # WITHOUT the "API Error:" prefix. A 429 is a transient RPM/TPM rate limit (the
    # window resets in seconds, unlike a weekly/usage quota cap), so nudge past it.
    # Requiring the 429 code keeps ordinary prose about rate limits from tripping it.
    if _BARE_429.search(lower) and (
        "rate limit" in lower or "too many requests" in lower
    ):
        return True
    # Or any API error that points at the status page.
    if "api error" in lower and "status.claude.com" in lower:
        return True
    # Or a codeless API connectivity error (network out, DNS, timeout, …).
    if "api error" in lower and any(p in lower for p in _CONNECTIVITY_PHRASES):
        return True
    return False


def is_confirmed_stall(previous_tail: str | None, current_tail: str) -> bool:
    """Idle-confirmation gate (mirrors ApiErrorMatch.isConfirmedStall). A session is
    treated as genuinely STALLED — and so eligible for a nudge — only when its erroring
    tail is UNCHANGED since the previous scan. An actively-working session changes
    between scans and must not be nudged: one merely printing/discussing an API-error
    string, one that already recovered while the error line is still on screen, or a CLI
    mid auto-retry with a live countdown. ``previous_tail`` is None the first scan a pane
    is seen erroring, which is never a confirmed stall."""
    return looks_like_api_error(current_tail) and previous_tail == current_tail


# The interrupt hint a CLI renders only while a turn is actually in flight — one
# spelling per runner, verbatim from AgentActivity.swift so both front-ends read the
# same markers. Claude Code writes "esc to interrupt", OpenCode "esc interrupt", and
# Hermes "Ctrl+C to interrupt…". No string here contains another, so a pane is read
# against all of them: the applet cannot ask a pane which CLI drew it, and a runner
# missing from this list is one whose agents read as idle the whole time they work —
# their bays go back to the task cap and the monitors dispatch over the top of them.
BUSY_MARKERS = ("esc to interrupt", "esc interrupt", "ctrl+c to interrupt")

# How many non-empty visible lines from the bottom carry the LIVE status bar. Far
# shorter than SCANNED_TAIL_LINES: an error banner sits well above the prompt box and
# has to be reached for, but the interrupt hint is always the last line or two, and
# reaching further would match the same hint left in scrollback by an earlier turn —
# reading a finished agent as busy for as long as its window stays open, which is the
# one mistake this must not make (mirrors AgentActivity.scannedTailLines).
BUSY_TAIL_LINES = 5


def looks_busy(visible: str) -> bool:
    """True when a pane shows the CLI mid-turn — its interrupt hint is on the live
    status bar. False means the turn ended and the session is back at its prompt,
    awaiting input (mirrors AgentActivity.looksBusy).

    An agent is spawned into an INTERACTIVE session (``review.shell_command``), so
    finishing its work is not an exit: the process lives on at the prompt until a
    human closes the window. Absence of the hint is the only thing that separates
    those two states from the outside — ``ps`` shows the same live agent for both,
    and the completion sentinel only ever fires on exit.
    """
    tail = last_lines(visible, BUSY_TAIL_LINES).lower()
    return any(marker in tail for marker in BUSY_MARKERS)


def next_backoff(prev_interval: float | None) -> float:
    """The delay before the next nudge to a pane: the base cooldown on the first hit,
    then double the prior interval each retry, capped at the 3h ceiling."""
    if prev_interval is None:
        return APIWATCH_COOLDOWN
    return min(prev_interval * 2, APIWATCH_MAX_BACKOFF)


def last_lines(text: str, n: int = SCANNED_TAIL_LINES) -> str:
    """The last ``n`` non-empty visible lines — enough to catch a stall's error line
    even under a tall prompt/status box, without matching the phrase in older
    scrollback (mirrors ApiErrorWatcher.lastLines)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def human_interval(seconds: float) -> str:
    """A short human duration for the audit line: "2m", "45m", "1h 30m", "3h"."""
    total = int(round(seconds))
    hours, rem = divmod(total, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return f"{total}s"
