"""Comms, the Slack watcher, as the Linux front-end reaches it.

Everything Comms decides is ``DiplomatCore``, reached through the ``diplomat-core`` CLI
(``comms-tick``, ``comms-auth``, ``comms-feed``, ``comms-effect``, ``comms-reply``), which
reaches Slack under Claude Code's own Slack login. What lives here is what the CLI cannot
do for this process: appending the front-end's own lines (``action``, ``started``,
``finished``) to the log the CLI folds, and reading whether the session is locked.

Stdlib-only, like the rest of the runtime.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import core, promptcore

#: A Slack token that overrides Claude Code's login in the core (``SlackToken``). Every
#: agent spawn unsets it (:func:`review.spawn_env`), and the core strips it from the
#: processes it starts.
TOKEN_ENV = "DIPLOMAT_SLACK_TOKEN"

#: A ping's outcome when its run never started (``Store.commsNeverStartedOutcome`` on
#: macOS).
NEVER_STARTED = "its agent never started, so nothing ran"


def never_started(item: dict) -> bool:
    return str(item.get("outcome") or "").startswith(NEVER_STARTED)


#: A ping's outcome once a retry of a run that never started (or of work that left the
#: queue) starts (``Store.commsRetriedOutcome``).
RETRIED = "started on a later attempt"

#: A ping's outcome when the monitor find it took left the queue unrun and nothing here
#: can put it back (``Store.commsLeftQueueOutcome``).
LEFT_QUEUE = "its queued task left the queue before it ran"


def left_queue(item: dict) -> bool:
    return str(item.get("outcome") or "").startswith(LEFT_QUEUE)


#: Starts the reason of the ``none`` a banned sender's message gets in place of a triage
#: (``CommsWatch.bannedPrefix``).
BANNED_PREFIX = "sender banned: "


def from_banned_sender(decision: dict | None) -> bool:
    return str((decision or {}).get("reason") or "").startswith(BANNED_PREFIX)


def unstarted(item: dict) -> bool:
    """Twin of ``Store.commsUnstarted``: a ping whose last run never started is waiting
    for one again."""
    return not item.get("finishedAt") and (not item.get("startedAt") or never_started(item))


def comms_dir() -> Path:
    """Must match ``CommsLog.dir``: the lines written here land in the file the CLI
    folds."""
    env = os.environ.get("DIPLOMAT_COMMS_DIR")
    return Path(env) if env else Path.home() / ".diplomat" / "comms"


def events_path() -> Path:
    return comms_dir() / "events.jsonl"


def reports_dir() -> Path:
    return comms_dir() / "reports"


# MARK: - tunables (assets/comms.json)


def tunables() -> dict:
    try:
        return core.comms()
    except core.CoreError:
        return {}


#: A day: well inside a ``QTimer`` interval's int32 milliseconds.
MAX_POLL_SECS = 86400.0


def poll_secs() -> float:
    """``DIPLOMAT_COMMS_SECS``, else ``pollSecs``; held between ``minPollSecs`` and a
    day."""
    t = tunables()
    default = float(t.get("pollSecs", 60))
    floor = float(t.get("minPollSecs", 20))
    try:
        secs = float(os.environ.get("DIPLOMAT_COMMS_SECS") or default)
    except ValueError:
        secs = default
    if not math.isfinite(secs):
        secs = default
    return min(MAX_POLL_SECS, max(floor, secs))


#: Core bounds comms.json does not hold: ``SlackAPI``'s grace past a request's timeout
#: and ``LoginShell.timeout`` for finding ``claude``; plus a margin for the rest.
_HTTP_GRACE_SECS = 5
_LOGIN_SHELL_SECS = 10
_MARGIN_SECS = 30


def _sign_in_secs(t: dict) -> float:
    """``SlackAPI.signIn``: two tries, each opening a session (``initialize``,
    ``notifications/initialized``) and reading the operator's profile, around one login
    refresh (finding ``claude``, then ``claude mcp list``)."""
    call = float(t.get("httpTimeoutSecs", 15)) + _HTTP_GRACE_SECS
    return (2 * 3 * call + _LOGIN_SHELL_SECS
            + float(t.get("loginRefreshTimeoutSecs", 60)))


def tick_timeout() -> float:
    """The sum of the bounds the ``comms-tick`` child holds itself to, so it is never
    killed inside them: signing in, the deadline every later Slack call shares, and the
    triage: one batch call, then one call per message of it when its answers hold an
    ``injection``."""
    t = tunables()
    slack = float(t.get("slackBudgetSecs", 120)) + _HTTP_GRACE_SECS
    triage = (_LOGIN_SHELL_SECS
              + (1 + float(t.get("maxPerTick", 8))) * float(t.get("classifierTimeoutSecs", 90)))
    return _sign_in_secs(t) + slack + triage + _MARGIN_SECS


def reply_timeout() -> float:
    """``comms-reply``'s own bounds: signing in, the away answer's ``claude`` call, and
    the post under ``slackBudgetSecs``."""
    t = tunables()
    answer = float((t.get("answer") or {}).get("timeoutSecs", 180))
    slack = float(t.get("slackBudgetSecs", 120)) + _HTTP_GRACE_SECS
    return _sign_in_secs(t) + _LOGIN_SHELL_SECS + answer + slack + _MARGIN_SECS


def away_after_secs() -> float:
    return float((tunables().get("replies") or {}).get("awayAfterSecs", 10800))


# MARK: - the session lock


def session_locked() -> bool | None:
    """logind's ``LockedHint`` for this user's graphical session (``Display``, else
    ``$XDG_SESSION_ID``); ``None`` when logind cannot say. Never idle time, the
    screensaver or display sleep, which a keep-awake holds off."""
    exe = shutil.which("loginctl")
    if exe is None:
        return None

    def ask(*args: str) -> str:
        try:
            proc = subprocess.run(  # noqa: S603 — argv is a literal list
                [exe, *args], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return proc.stdout.strip() if proc.returncode == 0 else ""

    session = (ask("show-user", str(os.getuid()), "-p", "Display", "--value")
               or os.environ.get("XDG_SESSION_ID", ""))
    if not session:
        return None
    return {"yes": True, "no": False}.get(
        ask("show-session", session, "-p", "LockedHint", "--value"))


# MARK: - the CLI


def _core(args: list[str], *, stdin: object = None, timeout: float = 30) -> str:
    """stdout, or :class:`RuntimeError` saying why not."""
    full = dict(os.environ)
    full.setdefault("DIPLOMAT_CORE", str(core.assets_dir()))
    try:
        proc = subprocess.run(  # noqa: S603 — argv is a literal list
            [promptcore.core_bin(), *args],
            input=None if stdin is None else json.dumps(stdin),
            stdin=subprocess.DEVNULL if stdin is None else None,
            capture_output=True, text=True, env=full, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"diplomat-core {args[0]} did not answer within "
                           f"{int(timeout)}s") from exc
    except OSError as exc:
        raise RuntimeError(f"diplomat-core could not be run: {exc}") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"diplomat-core {args[0]} failed: {proc.stderr.strip()}")
    return proc.stdout


def tick(settings: dict) -> dict:
    """``{"me", "pings": [message], "items": [{"message", "decision"}], "error",
    "notices"}``: ``pings`` the messages this tick took, ``items`` triage answers, each
    for a ping of this tick or an earlier one. Never raises: a CLI failure is the
    tick's ``error``."""
    try:
        out = _core(["comms-tick"], stdin=settings, timeout=tick_timeout())
        result = json.loads(out)
    except (RuntimeError, promptcore.CoreBinaryMissing, ValueError) as exc:
        return {"me": None, "pings": [], "items": [], "error": str(exc), "notices": []}
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        return {"me": None, "pings": [], "items": [], "notices": [],
                "error": "diplomat-core comms-tick answered something that is not a tick"}
    pings, notices = result.get("pings"), result.get("notices")
    result["pings"] = ([p for p in pings if isinstance(p, dict)]
                       if isinstance(pings, list) else [])
    result["notices"] = ([n for n in notices if isinstance(n, str)]
                         if isinstance(notices, list) else [])
    return result


def feed(limit: int = 200) -> list[dict]:
    """The Comms screen's rows, newest ping first (``CommsLog.fold``)."""
    rows = json.loads(_core(["comms-feed", "--limit", str(limit)]))
    return rows if isinstance(rows, list) else []


def effect(decision: dict, message: dict, toggles: dict, settings: dict) -> dict:
    """What a triaged ping makes the front-end do (``CommsPlan.effect``): ``{"kind":
    "none"}``, ``{"kind": "review" | "fix_issue" | "bump", "number": N}``,
    ``{"kind": "investigate", "config": InvestigateConfig}``, or ``{"kind": "ban",
    "ban": CommsBan}``."""
    out = json.loads(_core(["comms-effect"], stdin={
        "decision": decision, "message": message, "toggles": toggles,
        "settings": settings}))
    return out if isinstance(out, dict) else {"kind": "none"}


def reply(request: dict) -> dict:
    """``comms-reply``: ``{"outcome": "sent" | "failed: …" | "skipped: …" | None,
    "text"}``. Never raises: a CLI that could not answer is a ``failed`` outcome, though
    the core may have posted before it went quiet."""
    try:
        out = json.loads(_core(["comms-reply"], stdin=request, timeout=reply_timeout()))
    except (RuntimeError, promptcore.CoreBinaryMissing, ValueError) as exc:
        return {"outcome": f"failed: {exc}", "text": None}
    if not isinstance(out, dict):
        return {"outcome": "failed: diplomat-core comms-reply answered something that is "
                           "not a reply", "text": None}
    outcome, text = out.get("outcome"), out.get("text")
    return {"outcome": outcome if isinstance(outcome, str) else None,
            "text": text if isinstance(text, str) else None}


# MARK: - the front-end's lines of the log


def iso(when: datetime | None = None) -> str:
    """``CommsLog.isoString``: whole seconds, UTC, ``Z``."""
    when = (when or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def append_event(kind: str, message_id: str, *, outcome: str | None = None,
                 queue_key: str | None = None, label: str | None = None,
                 when: datetime | None = None) -> bool:
    """Under the ``flock`` on ``events.lock`` the core's rotation also holds, so a
    rotation cannot drop the line. Keys as the Swift encoder writes them, absent ones
    left out. The log holds private DMs, hence 0700 / 0600."""
    event: dict = {"at": iso(when), "id": message_id, "kind": kind}
    if label is not None:
        event["label"] = label
    if outcome is not None:
        event["outcome"] = outcome
    if queue_key is not None:
        event["queueKey"] = queue_key
    line = (json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        comms_dir().mkdir(mode=0o700, parents=True, exist_ok=True)
        lock = os.open(str(comms_dir() / "events.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            fd = os.open(str(events_path()), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
        finally:
            os.close(lock)
    except OSError:
        return False
    return True


# MARK: - the Settings "Test" button


def auth() -> tuple[dict | None, str]:
    """``({userID, teamID, handle, team}, "")``, who Claude Code's Slack login is, or
    ``(None, why not)``. Over the tick's own transport and sign-in, so a pass here means
    the tick can poll. ``team`` is the workspace's name, ``teamID`` the key a sender's
    workspace is matched on. Never raises."""
    try:
        out = _core(["comms-auth"], timeout=_sign_in_secs(tunables()) + _MARGIN_SECS)
    except (RuntimeError, promptcore.CoreBinaryMissing) as exc:
        return None, str(exc)
    try:
        answer = json.loads(out)
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        return None, "diplomat-core comms-auth answered something that is not an answer"
    if answer.get("ok") is not True:
        return None, str(answer.get("error") or "Slack did not say who the login is")
    return {"userID": str(answer.get("userId") or ""),
            "teamID": str(answer.get("teamId") or ""),
            "handle": str(answer.get("user") or ""),
            "team": str(answer.get("team") or "")}, ""
