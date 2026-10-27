"""Comms, the Slack watcher, as the Linux front-end reaches it.

Everything Comms decides — the tick (search, filter, triage, log), the fold of its log
into the screen's rows, and the plan a triaged ping turns into — is ``DiplomatCore``,
reached through the ``diplomat-core`` CLI (``comms-tick``, ``comms-feed``,
``comms-effect``) the way :mod:`promptcore` reaches the prompt builders. What lives
here is what the CLI cannot do for this process: reading the Slack token from where the
operator keeps it, handing it to the child alone, and appending the front-end's own
lines (``action``, ``started``, ``finished``) to the log the CLI folds.

Stdlib-only, like the rest of the runtime.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import appconfig, core, promptcore

#: Where the Slack token is read from on Linux, by path only (``appconfig``): the token
#: itself never enters the shared config file, which is world-readable and mesh-copied.
DEFAULT_TOKEN_FILE = "~/.diplomat/slack-token"
#: Overrides the file (tests, the headless poll), and is how the token reaches the
#: ``comms-tick`` child: in that child's environment and nowhere else.
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


def poll_secs() -> float:
    """The tick period: ``DIPLOMAT_COMMS_SECS``, else ``pollSecs``, floored at
    ``minPollSecs`` — two searches plus one per keyword a tick, against Slack's Tier 2
    budget of about twenty calls a minute."""
    t = tunables()
    default = float(t.get("pollSecs", 60))
    floor = float(t.get("minPollSecs", 20))
    try:
        secs = float(os.environ.get("DIPLOMAT_COMMS_SECS") or default)
    except ValueError:
        secs = default
    return max(floor, secs)


def tick_timeout() -> float:
    """How long one ``comms-tick`` may take: a classifier call per message it triages,
    each bounded by the core, plus a minute for the searches."""
    t = tunables()
    return float(t.get("maxPerTick", 8)) * float(t.get("classifierTimeoutSecs", 60)) + 60


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
    """One watcher tick: ``{"me", "items": [{"message", "decision"}], "error"}``.

    Never raises: a CLI that cannot be run or answers nonsense is the tick's ``error``,
    the same as Slack refusing the token."""
    try:
        out = _core(["comms-tick"], stdin=settings, env={TOKEN_ENV: token},
                    timeout=tick_timeout())
        result = json.loads(out)
    except (RuntimeError, promptcore.CoreBinaryMissing, ValueError) as exc:
        return {"me": None, "items": [], "error": str(exc)}
    if not isinstance(result, dict) or not isinstance(result.get("items"), list):
        return {"me": None, "items": [], "error": "diplomat-core comms-tick answered "
                                                  "something that is not a tick"}
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

    ``O_APPEND``, so a line the tick writes at the same moment is never overwritten,
    and the keys the Swift encoder writes, absent ones left out. The core rotates the
    file on its own appends, which come with every ping these lines are about."""
    event: dict = {"at": iso(when), "id": message_id, "kind": kind}
    if label is not None:
        event["label"] = label
    if outcome is not None:
        event["outcome"] = outcome
    if queue_key is not None:
        event["queueKey"] = queue_key
    line = (json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        comms_dir().mkdir(parents=True, exist_ok=True)
        fd = os.open(str(events_path()), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        return False
    return True


# MARK: - the Settings "Test" button


def _slack_base() -> str:
    base = os.environ.get("DIPLOMAT_SLACK_API") or "https://slack.com/api/"
    return base if base.endswith("/") else base + "/"


def _describe(code: str) -> str:
    """``SlackError.errorDescription``'s wording, so the Test button and the error
    banner say the same thing about the same token."""
    if code == "missing_scope":
        return ("Slack: missing_scope. The token needs the user scopes search:read "
                "and users:read.")
    if code in ("invalid_auth", "not_authed", "token_revoked", "token_expired",
                "account_inactive"):
        return f"Slack rejected the token ({code})"
    if code == "ratelimited":
        return "Slack rate-limited the watcher"
    return f"Slack: {code}"


def auth_test(token: str, timeout: float | None = None) -> tuple[dict | None, str]:
    """Who ``token`` is, by Slack's ``auth.test``: ``({userID, teamID, handle}, "")``
    or ``(None, why not)``. The CLI has no auth-only command — ``comms-tick`` would
    search and triage — so the Test button asks Slack itself."""
    if not token:
        return None, "no Slack token is configured"
    req = urllib.request.Request(_slack_base() + "auth.test",
                                 headers={"Authorization": f"Bearer {token}"})
    wait = timeout if timeout is not None else float(tunables().get("httpTimeoutSecs", 15))
    try:
        with urllib.request.urlopen(req, timeout=wait) as resp:  # noqa: S310 — https or the test stub
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return None, "Slack rate-limited the watcher"
        return None, f"Slack answered HTTP {exc.code}"
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return None, f"Slack could not be reached: {reason}"
    except ValueError:
        return None, "Slack answered something that is not its JSON"
    if not isinstance(body, dict):
        return None, "Slack answered something that is not its JSON"
    if body.get("ok") is not True:
        return None, _describe(str(body.get("error") or "unknown_error"))
    user, team = body.get("user_id"), body.get("team_id")
    if not isinstance(user, str) or not isinstance(team, str):
        return None, "Slack answered something that is not its JSON"
    return {"userID": user, "teamID": team, "handle": str(body.get("user") or "")}, ""
