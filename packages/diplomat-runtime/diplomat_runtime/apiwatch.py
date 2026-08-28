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
(Hermes), so it reads as idle once nothing is still owed to it, gives its bay
back, and the monitor that owed the work dispatches it again on a later tick.

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

# Transient failures the CLI prints with NO status code, all under its "API Error:"
# prefix — a connectivity drop ("Unable to connect to API", "Connection error.") or a
# turn cut short ("Server error mid-response. The response above may be incomplete.",
# "Connection lost before a response was produced. Try again."). Both resume on a nudge
# exactly as a 5xx does. The CLI builds the cut-short line from a cause — server error,
# lost connection, a sleeping computer, a response that stopped arriving — plus one of
# two endings; the endings are what's listed here, so a new cause is covered too.
_CODELESS_PHRASES = [
    "unable to connect", "connection error", "connection refused",
    "connection reset", "connection timed out", "network error",
    "fetch failed", "econnrefused", "enotfound", "etimedout", "getaddrinfo",
    "the response above may be incomplete", "before a response was produced",
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
# Spend caps an org sets on a member/workspace, which the API rejects with a 403 —
#   "API Error: 403 Org member budget limit exceeded (daily limit). Contact your
#    org admin."
# Same gap trick as above, so org/workspace/monthly wordings and the "reached"
# spelling all match. Filed with the quota banners rather than the errors because
# the code is the only thing transient-looking about it: the cap holds until its
# window rolls over or an admin raises it, neither of which a nudge can do.
_BUDGET_LIMIT = re.compile(r"budget[a-z0-9\- ]{0,16}(exceeded|reached)")
# A banner OPENS its own line, colon and all: only decoration may precede it — the "⏺"
# bullet, the "⎿" tool-result elbow, box rules, indentation, a log timestamp. `[\W\d_]`
# is every character that is NOT a letter, in any script, because prose reaches a quoted
# banner through words and decoration never does. That is what separates the CLI's banner
# from an agent QUOTING one: a session merely discussing API errors goes static the moment
# its turn ends, which is indistinguishable from a stall downstream (see
# :func:`is_confirmed_stall`). Every arm below carries the anchor: any one of them alone
# is enough to nudge, so an arm without it is a hole in the whole predicate.
_BANNER_OPENS_LINE = re.compile(r"^[\W\d_]*API Error:", re.IGNORECASE | re.MULTILINE)
_API_ERROR_CODE = re.compile(r"^[\W\d_]*API Error:?\s*[0-9]{3}",
                             re.IGNORECASE | re.MULTILINE)
_BARE_429 = re.compile(r"^[\W\d_]*\b429\b", re.MULTILINE)

_WRAPPED_LINE = re.compile(r"\n\s*")


def _rejoined(text: str) -> str:
    """``text`` with terminal wrapping undone, for the phrase evidence a banner carries.
    A cut-short banner runs 70-90 columns, so a narrow pane splits it mid-phrase ("…may
    be\n  incomplete.") and a contiguous search finds nothing — the widest banner family
    the watcher exists for, invisible in exactly the panes most likely to wrap. The
    banner's own line-opening position is read off the ORIGINAL, where the line structure
    still exists.

    Blank rows are already gone by here (:func:`last_lines`), so this fuses every
    adjacent pair, not just the halves of a wrapped line — which is why only the
    banner's own evidence is read off it. Quota suppression is not: fusing two lines
    of prose into "budget … exceeded" would strand the stalled session it silences."""
    return _WRAPPED_LINE.sub(" ", text)


def looks_like_api_error(text: str) -> bool:
    """True when ``text`` shows a transient Claude API error the watcher should nudge
    past — a server 5xx / rate-limit ("API Error: <3-digit code>"), a status-page
    error, or a codeless failure (network out, DNS, timeout, a stream cut off).

    The banner must OPEN a line; one quoted mid-sentence is prose, not a stall. The
    phrases it is read for are matched with the pane's wrapping rejoined, so a banner
    the pane split mid-phrase still reads.

    Out-of-quota and org budget-cap banners return False: nudging a capped session
    does nothing until the window resets, so the watcher intentionally leaves them
    alone. Either banner also SUPPRESSES any API-error text in the same tail.
    """
    lower = text.lower()
    # Quota banner present ⇒ ignore this session entirely (and suppress any stray
    # API-error text sharing the tail). Read off the ORIGINAL rather than the rejoined
    # copy: these phrases are short enough to survive a wrap, and suppression is the one
    # answer that cannot be retried, so it must not be assembled out of two lines.
    if any(p in lower for p in _QUOTA_PHRASES):
        return False
    if _HIT_YOUR_LIMIT.search(lower) or _BUDGET_LIMIT.search(lower):
        return False
    unwrapped = _rejoined(lower)
    # "API Error: <3-digit code>" — the exact CLI format (529/500/503/429/…).
    if _API_ERROR_CODE.search(text):
        return True
    # A bare "429 Rate limited" banner. Newer CLI builds print a rate-limit error
    # WITHOUT the "API Error:" prefix. A 429 is a transient RPM/TPM rate limit (the
    # window resets in seconds, unlike a weekly/usage quota cap), so nudge past it.
    # It opens its line like the prefixed banners do, which is what keeps ordinary prose
    # about rate limits off it — a retry branch, a status-code table, a note about a 429.
    # The code on its own cannot: three digits are three digits.
    if _BARE_429.search(text) and (
        "rate limit" in unwrapped or "too many requests" in unwrapped
    ):
        return True
    if not _BANNER_OPENS_LINE.search(text):
        return False
    # A codeless API failure the banner names: the status page, connectivity, or a
    # stream cut off part-way.
    return "status.claude.com" in unwrapped or any(
        p in unwrapped for p in _CODELESS_PHRASES
    )


def is_confirmed_stall(previous_tail: str | None, current_tail: str) -> bool:
    """Idle-confirmation gate (mirrors ApiErrorMatch.isConfirmedStall). A session is
    treated as genuinely STALLED — and so eligible for a nudge — only when its erroring
    tail is UNCHANGED since the previous scan. It separates a session still REDRAWING
    from one at rest: a CLI mid auto-retry with a live countdown, or one still printing
    past the error, changes between scans and must not be nudged. ``previous_tail`` is
    None the first scan a pane is seen erroring, which is never a confirmed stall.

    What it cannot separate is one static screen from another — a session stalled on the
    banner and a finished session whose last screen merely CONTAINS one are both frozen,
    and the second reads as a confirmed stall as soon as two scans see it unchanged.
    Telling those apart is :func:`looks_like_api_error`'s job, not this gate's."""
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
