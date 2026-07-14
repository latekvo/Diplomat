"""The unified activity/audit feed — the Linux port of AuditLog.swift + AuditCategory.

Every action the panel or the daemon takes appends one JSON line to
``~/.argent/pr-monitor/audit.jsonl`` (written by the macOS app and the
device-allocator daemon). This module tail-reads that shared file and groups the
raw ``action`` verbs into the activity *categories* the panel filters by, using
the shared taxonomy in ``core/audit-categories.json`` (mirrors the Swift enum).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import core


def _dir() -> Path:
    return Path.home() / ".argent" / "pr-monitor"


def audit_path() -> Path:
    return _dir() / "audit.jsonl"


def log(source: str, action: str, detail: str) -> None:
    """Append one entry to the shared audit.jsonl — the Linux analogue of
    AuditLog.log. Best-effort and atomic (O_APPEND, so a concurrent daemon append
    can't be clobbered); never raises into the caller. This is what gives the Linux
    activity feed a data source: the panel logs here whenever it dispatches an
    action, the same way the macOS app and the device-allocator daemon do."""
    import json
    import os
    from datetime import datetime, timezone

    entry = {
        "at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "action": action,
        "detail": detail,
    }
    line = (json.dumps(entry) + "\n").encode("utf-8")
    try:
        _dir().mkdir(parents=True, exist_ok=True)
        fd = os.open(str(audit_path()), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass


# MARK: - Category taxonomy (from core/audit-categories.json)


@dataclass(frozen=True)
class AuditCategory:
    id: str
    title: str
    emoji: str
    glyph: str
    color_hex: str


def categories() -> list[AuditCategory]:
    """All activity categories in canonical display order."""
    return [
        AuditCategory(c["id"], c["title"], c["emoji"],
                      c.get("linuxGlyph", c["emoji"]), c["colorHex"])
        for c in core.audit_categories()["categories"]
    ]


def category_of(action: str) -> str:
    """Map one audit ``action`` verb to its category id. Unknown verbs fall
    through to the taxonomy's fallback so a row never vanishes from every chip."""
    data = core.audit_categories()
    return data["actions"].get(action, data["fallback"])


# Per-action row glyph, mirroring AuditRow.icon in ContentView.swift. Finer-grained
# than the category glyph (e.g. ban vs unban), falling back to the category's glyph.
# Monochrome text glyphs (never colour-emoji) so the feed tints cleanly like macOS.
_ACTION_GLYPH: dict[str, str] = {
    "review": "☑", "review-req": "☑",
    "review-reply": "↩",
    "conflicts": "⋔",
    "audit": "◉",
    "nudge": "ϟ",
    "quota-stall": "⧗",
    "merge": "✓",
    "kill-device": "✕",
    "unban": "○",
    "ban": "⊘",
    "repair-done": "⚒",
    "allocator-install": "⧉", "allocator-uninstall": "⧉",
    "merge-failed": "△", "spawn-failed": "△", "poll-failed": "△", "warn": "△",
    "poll-recovered": "✓",
}


def glyph_for(action: str) -> str:
    """The monochrome row glyph for an action (per-action override, else the
    category glyph). Never a colour-emoji - the Linux feed tints text glyphs."""
    if action in _ACTION_GLYPH:
        return _ACTION_GLYPH[action]
    cat = category_of(action)
    return next((c.glyph for c in categories() if c.id == cat), "•")


def color_for(action: str) -> str:
    """The tint colour for an action's row glyph (its category's colour)."""
    cat = category_of(action)
    return next((c.color_hex for c in categories() if c.id == cat), "#9AA0A6")


# Source → accent colour for the row's source badge (matches AuditRow.sourceColor).
_SOURCE_COLOR = {"panel": "#0A84FF", "auto": "#34C759", "agent": "#FF3B30"}


def source_color(source: str) -> str:
    return _SOURCE_COLOR.get(source, "#8E8E93")


# MARK: - Feed entries


@dataclass(frozen=True)
class AuditEntry:
    at: str
    source: str
    action: str
    detail: str

    @property
    def date(self) -> datetime | None:
        return _parse_date(self.at)


def _parse_date(s: str) -> datetime | None:
    # Swift stamps plain ISO8601; the daemon stamps JS toISOString() with
    # fractional seconds and a trailing Z. datetime.fromisoformat handles both
    # (incl. the Z suffix) on 3.11+.
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def read(limit: int = 200) -> list[AuditEntry]:
    """The most recent ``limit`` entries, newest first. Tail-reads the last 256KB
    only — the log grows forever and this runs on the panel's 8s poll, so a
    full-file read would eventually hitch the UI."""
    import json

    path = audit_path()
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            tail = 256 * 1024
            start = size - tail if size > tail else 0
            fh.seek(start)
            data = fh.read()
    except OSError:
        return []
    # A mid-file start lands mid-line (and maybe mid-UTF-8-char); drop up to the
    # first newline on the raw BYTES before decoding so a partial leading sequence
    # can't blank the whole feed.
    if start > 0:
        nl = data.find(b"\n")
        if nl >= 0:
            data = data[nl + 1 :]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return []
    entries: list[AuditEntry] = []
    for line in text.splitlines()[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            entries.append(
                AuditEntry(
                    at=obj.get("at", ""),
                    source=obj.get("source", ""),
                    action=obj.get("action", ""),
                    detail=obj.get("detail", ""),
                )
            )
        except (ValueError, TypeError):
            continue
    entries.reverse()
    return entries
