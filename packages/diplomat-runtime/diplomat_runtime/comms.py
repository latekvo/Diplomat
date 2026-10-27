"""Comms, the Slack watcher, as the Linux front-end reaches it.

Everything Comms decides — the tick (search, filter, triage, log), the fold of its log
into the screen's rows, and the plan a triaged ping turns into — is ``DiplomatCore``,
reached through the ``diplomat-core`` CLI (``comms-tick``, ``comms-auth``,
``comms-feed``, ``comms-effect``) the way :mod:`promptcore` reaches the prompt
builders. What lives here is what the CLI cannot do for this process: reading the Slack
token from where the operator keeps it, handing it to the children that ask Slack, and
appending the front-end's own lines (``action``, ``started``, ``finished``) to the log
the CLI folds.

Stdlib-only, like the rest of the runtime.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import appconfig, core, promptcore

#: Where the Slack token is read from on Linux, by path only (``appconfig``): the token
#: itself never enters the shared config file, which is world-readable and mesh-copied.
DEFAULT_TOKEN_FILE = "~/.diplomat/slack-token"
#: Overrides the file (tests, the headless poll), and is how the token reaches the
#: ``comms-tick`` and ``comms-auth`` children: in their environment, which the core
#: strips it from before it starts any process of its own.
TOKEN_ENV = "DIPLOMAT_SLACK_TOKEN"


def comms_dir() -> Path:
    """``$DIPLOMAT_COMMS_DIR``, else ``~/.diplomat/comms`` — ``CommsLog.dir``'s twin,
    so the lines written here land in the file the CLI folds."""
    env = os.environ.get("DIPLOMAT_COMMS_DIR")
    return Path(env) if env else Path.home() / ".diplomat" / "comms"


def events_path() -> Path:
    return comms_dir() / "events.jsonl"


def reports_dir() -> Path:
    return comms_dir() / "reports"


# MARK: - tunables (assets/comms.json)


def tunables() -> dict:
    """The numbers the front-end paces itself by. The prompts, queries and schema in
    the same file are the core's business and are not read here."""
    try:
        return core.comms()
    except core.CoreError:
        return {}


#: The longest period ``DIPLOMAT_COMMS_SECS`` can set: a day, well inside the int32
#: milliseconds of a ``QTimer`` interval.
MAX_POLL_SECS = 86400.0


def poll_secs() -> float:
    """The tick period: ``DIPLOMAT_COMMS_SECS``, else ``pollSecs``, floored at
    ``minPollSecs`` — two searches plus one per keyword a tick, against Slack's Tier 2
    budget of about twenty calls a minute — and capped at :data:`MAX_POLL_SECS`."""
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


#: The core's own bounds that comms.json does not hold: the grace ``SlackAPI`` waits
#: past a request's ``httpTimeoutSecs``, and ``LoginShell.timeout`` for finding
#: ``claude``. Plus a margin for starting the process and writing the log and state.
_HTTP_GRACE_SECS = 5
_LOGIN_SHELL_SECS = 10
_MARGIN_SECS = 30


def _http_call_secs() -> float:
    return float(tunables().get("httpTimeoutSecs", 15)) + _HTTP_GRACE_SECS


def keyword_count(settings: dict) -> int:
    """How many keyword searches a tick runs: ``keywordsRaw`` split as
    ``CommsSettings.keywordList`` splits it, on commas and new lines."""
    parts = re.split(r"[,\r\n]", str(settings.get("keywordsRaw") or ""))
    return len({p.strip() for p in parts if p.strip()})


def tick_timeout(keywords: int = 0) -> float:
    """How long one ``comms-tick`` may take: the sum of the bounds the child holds
    itself to — ``auth.test``, every page of every query (two plus one per keyword), a
    login-shell lookup of ``claude`` and a classifier call per message it triages — so
    it is never killed inside them. A killed tick's triaged messages wait for the next
    tick to hand them over."""
    t = tunables()
    calls = 1 + (2 + keywords) * int(t.get("maxSearchPages", 3))
    triage = float(t.get("maxPerTick", 8)) * float(t.get("classifierTimeoutSecs", 60))
    return calls * _http_call_secs() + _LOGIN_SHELL_SECS + triage + _MARGIN_SECS


# MARK: - the token


def token_file() -> str:
    """The configured token path, ``~`` expanded."""
    raw = appconfig.get(appconfig.SLACK_TOKEN_FILE).strip() or DEFAULT_TOKEN_FILE
    return os.path.expanduser(raw)


def read_token() -> tuple[str, str]:
    """``(token, "")``, or ``("", why there is none)``.

    ``DIPLOMAT_SLACK_TOKEN`` wins. Otherwise the file is read under the rules the agent
    token's is (``review.check_token``): an absolute path, a regular file — a FIFO
    with no writer would block this read and ``/dev/zero`` never end it — holding
    something other than whitespace."""
    env = os.environ.get(TOKEN_ENV, "").strip()
    if env:
        return env, ""
    path = token_file()
    if not os.path.isabs(path):
        return "", f"slackTokenFile is not an absolute path: {path}"
    try:
        if not stat.S_ISREG(os.stat(path).st_mode):
            return "", f"{path} is not a regular file"
        with open(path, "rb") as f:
            words = f.read(64 * 1024).replace(b"\0", b"").split()
    except FileNotFoundError:
        return "", f"no Slack token at {path}"
    except OSError as exc:
        return "", f"cannot read {path}: {exc.strerror or exc}"
    if not words:
        return "", f"{path} is empty"
    return words[0].decode("utf-8", "replace"), ""


# MARK: - the CLI


def _core(args: list[str], *, stdin: object = None, env: dict | None = None,
          timeout: float = 30) -> str:
    """One ``diplomat-core`` run; stdout, or :class:`RuntimeError` saying why not."""
    full = dict(os.environ)
    full.setdefault("DIPLOMAT_CORE", str(core.assets_dir()))
    # The token reaches a child only when the caller hands it one.
    full.pop(TOKEN_ENV, None)
    full.update(env or {})
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


def tick(settings: dict, token: str) -> dict:
    """One watcher tick: ``{"me", "items": [{"message", "decision"}], "error",
    "notices"}``, ``notices`` being what the tick has to tell the operator that is not
    an error (a catch-up past ``catchUpHours``, a query cut short by
    ``maxSearchPages``).

    Never raises: a CLI that cannot be run or answers nonsense is the tick's ``error``,
    the same as Slack refusing the token."""
    try:
        out = _core(["comms-tick"], stdin=settings, env={TOKEN_ENV: token},
                    timeout=tick_timeout(keyword_count(settings)))
        result = json.loads(out)
    except (RuntimeError, promptcore.CoreBinaryMissing, ValueError) as exc:
        return {"me": None, "items": [], "error": str(exc), "notices": []}
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        return {"me": None, "items": [], "notices": [],
                "error": "diplomat-core comms-tick answered something that is not a tick"}
    notices = result.get("notices")
    result["notices"] = ([n for n in notices if isinstance(n, str)]
                         if isinstance(notices, list) else [])
    return result


def feed(limit: int = 200) -> list[dict]:
    """The Comms screen's rows, newest ping first (``CommsLog.fold``)."""
    rows = json.loads(_core(["comms-feed", "--limit", str(limit)]))
    return rows if isinstance(rows, list) else []


def effect(decision: dict, message: dict, toggles: dict, settings: dict) -> dict:
    """What a triaged ping makes the front-end do (``CommsPlan.effect``): ``{"kind":
    "none"}``, ``{"kind": "review" | "fix_issue" | "bump", "number": N}``, or
    ``{"kind": "investigate", "config": InvestigateConfig}``."""
    out = json.loads(_core(["comms-effect"], stdin={
        "decision": decision, "message": message, "toggles": toggles,
        "settings": settings}))
    return out if isinstance(out, dict) else {"kind": "none"}


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
    """Append one ``CommsEvent`` line (``action`` / ``started`` / ``finished``).

    Under the exclusive ``flock`` on ``events.lock`` that every append and rotation
    of the log holds, so the core's rotation (read, rewrite, rename) cannot drop it;
    with the keys the Swift encoder writes, absent ones left out. The log holds
    private DMs: the directory is created 0700 and the file 0600."""
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


def auth_test(token: str) -> tuple[dict | None, str]:
    """Who ``token`` is: ``({userID, teamID, handle, team}, "")`` or ``(None, why
    not)``. Asked through ``diplomat-core comms-auth``, Slack's ``auth.test`` over the
    transport the tick uses, so a token that passes here is one the tick can poll with
    and a refusal reads as the tick's error would. Never raises."""
    if not token:
        return None, "no Slack token is configured"
    try:
        out = _core(["comms-auth"], env={TOKEN_ENV: token},
                    timeout=_http_call_secs() + _MARGIN_SECS)
    except (RuntimeError, promptcore.CoreBinaryMissing) as exc:
        return None, str(exc)
    try:
        answer = json.loads(out)
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        return None, "diplomat-core comms-auth answered something that is not an answer"
    if answer.get("ok") is not True:
        return None, str(answer.get("error") or "Slack did not say who the token is")
    return {"userID": str(answer.get("userId") or ""),
            "teamID": str(answer.get("teamId") or ""),
            "handle": str(answer.get("user") or ""),
            "team": str(answer.get("team") or "")}, ""
