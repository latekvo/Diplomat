"""Activity-feed taxonomy + parsing tests.

The category-mapping assertions mirror the Swift smoke test
(Sources/ArgentUtilsCoreSmoke/main.swift, "audit category" section) so the shared
core/audit-categories.json can't drift from Sources/ArgentUtilsCore/AuditCategory.swift.
Pure, offline — no display, no ~/.argent files required.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from argent_utils import activity  # noqa: E402


def test_category_of_matches_swift_taxonomy() -> None:
    cases = {
        "review": "review",
        "review-req": "review",
        "review-reply": "reply",
        "conflicts": "conflicts",
        "audit": "audit",
        "nudge": "apiRestart",
        "quota-stall": "quota",
        "quota-resume": "quota",
        "merge": "merge",
        "merge-failed": "merge",
        "ban": "bans",
        "unban": "bans",
        # everything else falls through to system, so a row never vanishes
        "kill-device": "system",
        "repair-done": "system",
        "allocator-install": "system",
        "poll-failed": "system",
        "spawn-failed": "system",
        "warn": "system",
        "totally-new-verb": "system",
    }
    for action, expected in cases.items():
        assert activity.category_of(action) == expected, action


def test_taxonomy_has_nine_categories_in_order() -> None:
    cats = activity.categories()
    ids = [c.id for c in cats]
    assert ids == [
        "review", "reply", "conflicts", "audit", "apiRestart",
        "quota", "merge", "bans", "system",
    ]
    # every category the mapping can produce is a real declared category
    assert activity.category_of("ban") in ids


def test_glyph_falls_back_to_category_emoji() -> None:
    # a verb with no per-action glyph uses its category's emoji, never blank
    assert activity.glyph_for("quota-resume")  # category "quota" -> ⏳
    assert activity.glyph_for("ban") == "🚫"


def test_read_absent_file_is_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(activity, "audit_path", lambda: tmp_path / "nope.jsonl")
    assert activity.read() == []


def test_read_parses_newest_first(tmp_path, monkeypatch) -> None:
    f = tmp_path / "audit.jsonl"
    f.write_text(
        '{"at":"2026-07-14T10:00:00Z","source":"panel","action":"review","detail":"a"}\n'
        '{"at":"2026-07-14T10:05:00Z","source":"auto","action":"merge","detail":"b"}\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(activity, "audit_path", lambda: f)
    entries = activity.read()
    assert [e.detail for e in entries] == ["b", "a"]  # newest first
    assert entries[0].date is not None  # fractional/Z timestamps parse
