"""Prompt-injection ban list - the Linux port of BanList.swift.

The device-allocator daemon (and the macOS monitor) write banned authors to
``~/.diplomat/pr-monitor/banned.json`` as ``{"banned": [BannedAuthor, ...]}``. The
panel reads it, surfacing who is blocked from automated work. One list holds two
namespaces: a GitHub login, and a Slack sender as ``slack:<user id>`` (core's
``CommsBan``), which no GitHub login can collide with since none holds a colon.

The one write here is Comms banning a Slack sender (:func:`report_comms_ban`), through
the daemon like every other ban, and directly only when no daemon answers.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

from diplomat_runtime import activity, atomicjson
from diplomat_runtime.models import Fmt

#: ``CommsBan.loginPrefix``.
SLACK_PREFIX = "slack:"
#: The daemon's ``SLACK_USER_ID``: it refuses a ban on anything else.
_SLACK_USER_ID = re.compile(r"^[UW][A-Z0-9]{2,}$")
#: The ``reportedBy`` of a ban Comms reported.
COMMS_REPORTER = "Comms triage"


def ban_dir() -> Path:
    """Relocated by ``DA_BAN_DIR``, as in the daemon's ``src/paths.js``."""
    env = os.environ.get("DA_BAN_DIR")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".diplomat" / "pr-monitor"


def banned_path() -> Path:
    return ban_dir() / "banned.json"


def slack_login(user_id: str) -> str:
    return SLACK_PREFIX + user_id


def _parse(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class BannedAuthor:
    login: str
    reason: str | None = None
    #: A PR reference for a GitHub ban, the message's permalink for a Slack one.
    pr: str | None = None
    #: ``"slack"`` for a Slack sender; absent for a GitHub author.
    source: str | None = None
    #: A Slack sender's display name.
    handle: str | None = None
    evidence: str | None = None
    evidence_dir: str | None = None
    reported_by: str | None = None
    #: The latest ban; ``first_at`` is the first, kept across re-bans.
    at: str | None = None
    first_at: str | None = None

    @property
    def is_slack(self) -> bool:
        return self.login.startswith(SLACK_PREFIX)

    @property
    def slack_id(self) -> str | None:
        return self.login[len(SLACK_PREFIX):] if self.is_slack else None

    @property
    def name(self) -> str:
        """``@login``; a Slack sender by handle, or by id when none was recorded."""
        if self.is_slack:
            return f"@{self.handle}" if self.handle else str(self.slack_id)
        return f"@{self.login}"

    @property
    def banned_at(self) -> datetime | None:
        return _parse(self.at)

    @property
    def first_banned_at(self) -> datetime | None:
        """Only when it differs from :attr:`banned_at`: the author was banned again."""
        first = _parse(self.first_at)
        return first if first is not None and first != self.banned_at else None


def read() -> list[BannedAuthor]:
    """The current banned authors; empty when the file is absent or malformed."""
    try:
        obj = json.loads(banned_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(obj, dict):
        return []
    out: list[BannedAuthor] = []
    for b in obj.get("banned", []) or []:
        if not isinstance(b, dict) or not b.get("login"):
            continue

        def s(key: str) -> str | None:
            v = b.get(key)
            return str(v) if v not in (None, "") else None

        out.append(BannedAuthor(
            login=str(b["login"]), reason=s("reason"), pr=s("pr"), source=s("source"),
            handle=s("handle"), evidence=s("evidence"), evidence_dir=s("evidenceDir"),
            reported_by=s("reportedBy"), at=s("at"), first_at=s("firstAt")))
    return out


def is_banned(login: str, banned: list[BannedAuthor]) -> bool:
    """Whether ``login`` is on the ban list (case-insensitive, as GitHub logins are).
    Mirrors BanList.isBanned — the monitor must never dispatch an agent for a PR by a
    banned (prompt-injection) author."""
    low = (login or "").lower()
    return any(b.login.lower() == low for b in banned)


def slack_ids(banned: list[BannedAuthor]) -> list[str]:
    """The Slack user ids on the list: ``CommsSettings.bannedSenders``."""
    return [b.slack_id for b in banned if b.slack_id]


def _stamp(d: datetime) -> str:
    return d.astimezone().strftime("%b %d %H:%M")


def when_text(b: BannedAuthor, now: datetime | None = None) -> str:
    """``banned 3h ago (Oct 28 14:02)``, and when first banned if this is a re-ban."""
    at = b.banned_at
    if at is None:
        return "banned at an unrecorded time"
    text = f"banned {Fmt.age(at, now)} ago ({_stamp(at)})"
    first = b.first_banned_at
    return text + (f", first {_stamp(first)}" if first else "")


def place_text(b: BannedAuthor) -> str:
    """Where the injection was, short: ``PR #N``, the PR reference as recorded, or the
    Slack message (whose permalink is :func:`place_url`)."""
    if b.is_slack:
        return "Slack message" if b.pr else "Slack"
    if not b.pr:
        return "GitHub"
    m = re.search(r"/pull/(\d+)", b.pr)
    return f"PR #{m.group(1)}" if m else b.pr


def place_url(b: BannedAuthor) -> str | None:
    return b.pr if b.pr and b.pr.startswith(("https://", "http://")) else None


# MARK: - Comms reporting a Slack sender


class Reported(NamedTuple):
    #: The daemon recorded it, and wrote the feed line.
    by_daemon: bool
    #: Why nothing was recorded; None once it was.
    error: str | None = None


def ban_line(entry: dict) -> str:
    """The daemon's ``banLine`` for a ban Comms flagged, word for word."""
    user_id = entry["login"][len(SLACK_PREFIX):]
    handle = entry.get("handle")
    who = f"Slack user {'@' + handle + ' ' if handle else ''}({user_id})"
    where = f" ({entry['pr']})" if entry.get("pr") else ""
    evidence = " ".join(str(entry.get("evidence") or "").split())[:160]
    quote = f': "{evidence}"' if evidence else ""
    return f"Banned {who} for prompt injection{where}{quote} - flagged by Comms triage"


def report_comms_ban(ban: dict, *, timeout_secs: float = 10) -> Reported:
    """Ban the sender of one ``CommsBan`` (``{userID, handle, place, permalink,
    evidence, reason}``). Blocking, so off the GUI thread.

    Through the daemon's ``/report-injection``, which serializes every ban-list write
    and logs the ``ban`` line itself. A daemon that does not answer is not a reason to
    leave the sender free: the entry is then written here, in the daemon's shape, with
    the line it would have logged. A daemon that answers and refuses is final."""
    from . import deviceallocator

    user_id = str(ban.get("userID") or "")
    handle = str(ban.get("handle") or "").strip().lstrip("@")
    permalink = str(ban.get("permalink") or "")
    evidence = str(ban.get("evidence") or "")
    if not _SLACK_USER_ID.match(user_id):
        return Reported(False, f"no Slack user id to ban ({user_id or 'none'})")
    reply = deviceallocator.call("report-injection", {
        "person": user_id, "source": "slack", "handle": handle, "pr": permalink,
        "evidence": evidence, "agentName": COMMS_REPORTER, "via": "comms",
    }, timeout_secs=timeout_secs)
    if reply is not None:
        if reply.ok:
            return Reported(True)
        return Reported(True, f"the device-allocator refused it "
                              f"({reply.get('error') or reply.status})")
    return Reported(False, _write_directly(slack_login(user_id), handle, permalink, evidence))


def _write_directly(login: str, handle: str, permalink: str, evidence: str) -> str | None:
    """The daemon's write, minus its evidence dir; returns why it failed, or None. A
    file that is present but unreadable is left for the daemon to set aside: writing
    over it would lift every ban in it."""
    path = banned_path()
    data = atomicjson.read_object(path)
    if data is None and path.exists():
        return f"{path} is unreadable, so it was left as it is"
    data = data or {}
    if not isinstance(data.get("banned"), list):
        data["banned"] = []
    at = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    entry = {"login": login, "reason": "prompt injection", "pr": permalink or None,
             "evidence": evidence[:800], "evidenceDir": None, "reportedBy": COMMS_REPORTER,
             "at": at, "screenshot": False, "ghCaptured": False,
             "source": "slack", "handle": handle or None}
    existing = next((b for b in data["banned"] if isinstance(b, dict)
                     and str(b.get("login", "")).lower() == login.lower()), None)
    if existing is not None:
        first = existing.get("firstAt") or existing.get("at")
        existing.update(entry, firstAt=first)
    else:
        data["banned"].append({**entry, "firstAt": at})
    try:
        atomicjson.write_atomic(path, data)
    except OSError as exc:
        return f"ban on {login} not recorded: {exc.strerror or exc}"
    activity.log("auto", "ban", ban_line(entry))
    return None
