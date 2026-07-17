"""Prompt-injection ban list — the Linux read-only port of BanList.swift.

The device-allocator daemon (and the macOS monitor) write banned authors to
``~/.argent/pr-monitor/banned.json`` as ``{"banned": [BannedAuthor, ...]}``. The
panel only reads it (bans are managed by the daemon), surfacing who is blocked
from automated reviews.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


def banned_path() -> Path:
    return Path.home() / ".argent" / "pr-monitor" / "banned.json"


@dataclass(frozen=True)
class BannedAuthor:
    login: str
    reason: str | None = None
    pr: str | None = None


def read() -> list[BannedAuthor]:
    """The current banned authors; empty when the file is absent or malformed."""
    try:
        obj = json.loads(banned_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: list[BannedAuthor] = []
    for b in obj.get("banned", []) or []:
        login = (b or {}).get("login")
        if not login:
            continue
        out.append(BannedAuthor(login=login, reason=b.get("reason"), pr=b.get("pr")))
    return out


def is_banned(login: str, banned: list[BannedAuthor]) -> bool:
    """Whether ``login`` is on the ban list (case-insensitive, as GitHub logins are).
    Mirrors BanList.isBanned — the monitor must never dispatch an agent for a PR by a
    banned (prompt-injection) author."""
    low = (login or "").lower()
    return any(b.login.lower() == low for b in banned)
