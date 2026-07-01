"""Reader/writer for the external PR auto-fix monitor's heartbeat + control files.

The monitor (a Claude session watching my open PRs and dispatching conflict-resolve /
review-fix agents) runs OUTSIDE the applet and writes ~/.argent/pr-monitor/status.json
each tick. The applet reads it to show whether auto-fixing is actually live — the pill
only reads "active" on a FRESH heartbeat, so it never claims active when nothing runs.
The settings toggle writes control.json, which the monitor honors. Mirrors
AutofixStatus.swift.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

_DIR = Path.home() / ".argent" / "pr-monitor"
_STATUS = _DIR / "status.json"
_CONTROL = _DIR / "control.json"

# A heartbeat older than this ⇒ the monitor isn't running (tick is ~10 min; allow 2.5×).
_LIVE_WINDOW_S = 25 * 60


def read_status() -> dict | None:
    try:
        return json.loads(_STATUS.read_text())
    except (OSError, ValueError):
        return None


def is_live(status: dict | None) -> bool:
    ts = (status or {}).get("updatedAt")
    if not ts:
        return False
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    return (datetime.now(timezone.utc) - dt).total_seconds() < _LIVE_WINDOW_S


def total_fixed(status: dict | None) -> int:
    s = status or {}
    return int(s.get("conflictsResolved", 0)) + int(s.get("reviewsAddressed", 0))


def write_enabled(enabled: bool) -> None:
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        _CONTROL.write_text(json.dumps({"enabled": bool(enabled)}) + "\n")
    except OSError:
        pass
